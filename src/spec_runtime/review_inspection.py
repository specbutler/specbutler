"""Small read-only, checkout-scoped stdio MCP server for review sessions.

Run with the trusted host Python: python -m spec_runtime.review_inspection ROOT.
The protocol is newline-delimited JSON-RPC, as required by MCP stdio.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

from .provider_env import protected_operator_paths

LIMIT = 64 * 1024
MAX_ENTRIES = 2000


# Restrict each endpoint to a ref/object name plus parent selectors. In
# particular, do not accept object peeling, reflog expansion, or path selectors.
_COMMIT_REF = r"[A-Za-z0-9_][A-Za-z0-9_./-]*(?:[~^][0-9]*)*"


def commit_revision(revision: str) -> str:
    """Require commit objects for single revisions and both range endpoints."""
    if not isinstance(revision, str) or len(revision) > 200:
        raise ValueError("Invalid commit revision")
    endpoints = re.split(r"(\.\.\.?)", revision)
    if len(endpoints) not in {1, 3}:
        raise ValueError("Invalid commit revision")
    for endpoint in endpoints[::2]:
        if not re.fullmatch(_COMMIT_REF, endpoint) or ".." in endpoint:
            raise ValueError("Invalid commit revision")
    return "".join(part + "^{commit}" if i % 2 == 0 else part
                   for i, part in enumerate(endpoints))


def glob_matches(relative: str, pattern: str) -> bool:
    """Match path segments; only ** can consume zero or more directories."""
    parts = relative.split("/")
    reachable = {0}
    for segment in Path(pattern).parts:
        if segment == "**":
            reachable = set(range(min(reachable), len(parts) + 1)) if reachable else set()
        else:
            reachable = {i + 1 for i in reachable if i < len(parts)
                         and fnmatch.fnmatchcase(parts[i], segment)}
    return len(parts) in reachable


class Inspection:
    def __init__(self, root: Path, protected: tuple[Path, ...] | None = None):
        self.root = root.resolve(strict=True)
        self.protected = tuple(p.resolve() for p in (
            protected_operator_paths() if protected is None else protected
        ))

    def path(self, path: str = ".") -> Path:
        raw = Path(path)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError("Only relative paths within the checkout are allowed")
        target = (self.root / raw).resolve()
        if not target.is_relative_to(self.root) or any(
            target == p or target.is_relative_to(p) for p in self.protected
        ):
            raise ValueError("Path is outside the inspection boundary or protected")
        return target

    def read(self, path: str) -> dict:
        target = self.path(path)
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Only regular files can be read")
        with target.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        return {"text": data[:LIMIT].decode("utf-8", errors="replace"),
                "truncated": len(data) > LIMIT}

    def files(self, path: str):
        target = self.path(path)
        if stat.S_ISREG(target.stat().st_mode):
            yield target.relative_to(self.root).as_posix()
            return
        if not target.is_dir():
            raise ValueError("Only regular files and directories can be searched")
        # Never descend through links, junctions, protected paths, or git metadata.
        for directory, dirs, files in os.walk(target, followlinks=False):
            safe = []
            for name in dirs:
                candidate = Path(directory) / name
                try:
                    self.path(candidate.relative_to(self.root).as_posix())
                except ValueError:
                    continue
                if name != ".git" and not candidate.is_symlink() and not (
                    hasattr(candidate, "is_junction") and candidate.is_junction()
                ):
                    safe.append(name)
            dirs[:] = sorted(safe)
            for name in sorted(files):
                relative = (Path(directory) / name).relative_to(self.root).as_posix()
                try:
                    target = self.path(relative)
                    if stat.S_ISREG(target.stat().st_mode):
                        yield relative
                except (ValueError, OSError):
                    continue

    def git(self, operation: str = "show", revision: str = "HEAD", paths: list | None = None):
        if operation not in {"show", "log", "diff"}:
            raise ValueError("Only show, log and diff are allowed")
        revision = commit_revision(revision)
        paths = paths or []
        for path in paths:
            self.path(path)
            if path.startswith("-") or any(c in path for c in "*?[:"):
                raise ValueError("Git paths must be literal relative paths")
        # No inherited Git configuration, external diffs, textconv, pager, or hooks.
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", GIT_NO_LAZY_FETCH="1",
                   GIT_ALLOW_PROTOCOL="", GIT_OPTIONAL_LOCKS="0")
        args = ["git", "--no-pager", "--work-tree=" + str(self.root),
                "-c", "core.hooksPath=" + os.devnull,
                "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
                "-c", "diff.submodule=short", "-c", "submodule.recurse=false",
                operation, "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                "--ignore-submodules=all"]
        if operation == "log":
            args += ["-n", "50", "--format=medium"]
        # Require paths: unrestricted history could expose protected in-root files.
        if not paths:
            raise ValueError("At least one explicit file path is required")
        for path in paths:
            if not self.path(path).is_file():
                raise ValueError("Git paths must identify current regular files")
        args.append(revision)
        if operation == "diff" and ".." not in revision:
            # Review inspects a pinned checkout, never uncommitted worktree
            # conversions: clean/process filters can execute checkout code.
            args.append("HEAD^{commit}")
        args += ["--", *paths]
        with subprocess.Popen(args, cwd=self.root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            timer = threading.Timer(10, process.kill)
            timer.start()
            try:
                data = process.stdout.read(LIMIT + 1)
                if len(data) > LIMIT:
                    process.kill()
                process.wait()
            finally:
                timer.cancel()
            if process.returncode and len(data) <= LIMIT:
                raise ValueError("Git inspection failed or timed out")
        return {"text": data[:LIMIT].decode("utf-8", errors="replace"),
                "truncated": len(data) > LIMIT}

    def call(self, name: str, arguments: dict) -> dict:
        if name == "read_file":
            return self.read(**arguments)
        if name == "git_show":
            return self.git(**arguments)
        path = arguments.get("path", ".")
        self.path(path)
        output = []
        size = 0
        if name == "list_dir":
            for child in sorted(self.path(path).iterdir()):
                try:
                    self.path(child.relative_to(self.root).as_posix())
                except ValueError:
                    continue
                output.append(child.name)
                size += len(child.name.encode("utf-8"))
                if len(output) >= MAX_ENTRIES or size > LIMIT:
                    return {"entries": output[:-1], "truncated": True}
            return {"entries": output, "truncated": False}
        if name not in {"glob", "grep"}:
            raise ValueError("Unknown tool")
        pattern = arguments.get("pattern", "")
        if not isinstance(pattern, str) or len(pattern) > 1000:
            raise ValueError("Invalid pattern")
        if name == "glob" and (Path(pattern).is_absolute() or ".." in Path(pattern).parts):
            raise ValueError("Invalid glob")
        truncated = False
        base = self.path(path)
        if not base.is_dir():
            base = base.parent
        for relative in self.files(path):
            if name == "glob":
                local = (self.root / relative).relative_to(base).as_posix()
                matches = [relative] if glob_matches(local, pattern) else []
            else:
                result = self.read(relative)
                truncated |= result["truncated"]
                matches = [f"{relative}:{n}:{line}" for n, line in
                           enumerate(result["text"].splitlines(), 1) if pattern in line]
            for match in matches:
                size += len(match.encode("utf-8"))
                if size > LIMIT or len(output) >= MAX_ENTRIES:
                    return {"entries": output, "truncated": True}
                output.append(match)
        return {"entries": output, "truncated": truncated}


def tool_schema(name: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": "Read-only checkout inspection. grep uses literal text. "
            "git_show requires explicit current file paths and commit revisions, never command options. "
            "It inspects committed history only; a single-revision diff compares with HEAD.",
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required or [], "additionalProperties": False}}


STRING = {"type": "string"}
TOOLS = [
    tool_schema("read_file", {"path": STRING}, ["path"]),
    tool_schema("list_dir", {"path": STRING}),
    tool_schema("glob", {"path": STRING, "pattern": STRING}, ["pattern"]),
    tool_schema("grep", {"path": STRING, "pattern": STRING}, ["pattern"]),
    tool_schema("git_show", {"operation": {"enum": ["show", "log", "diff"]},
                             "revision": STRING, "paths": {"type": "array", "items": STRING}},
                ["paths"]),
]


def main():
    inspection = Inspection(Path(sys.argv[1]),
                            (*protected_operator_paths(), *(Path(p) for p in sys.argv[2:])))
    for line in sys.stdin.buffer:
        request = {}
        try:
            if len(line) > LIMIT:
                raise ValueError("Request too large")
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request["method"]
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get(
                    "protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "spec-review-inspection", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request["params"]
                try:
                    value = inspection.call(params["name"], params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value)}]}
                except (ValueError, OSError, TypeError) as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            else:
                raise ValueError("Unknown method")
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32600, "message": str(exc)}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
