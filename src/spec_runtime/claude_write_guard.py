"""Claude Code PreToolUse hook that keeps file-editing tools inside the workspace.

Claude's OS sandbox confines Bash, but Edit, Write, MultiEdit and NotebookEdit
run in the Claude process itself, and bypassPermissions mode skips their
protected-path prompts. Spec Butler registers this script for those tools in
the generated settings of native unattended launches.

It runs as ``python -I claude_write_guard.py --allow ROOT ... --deny PATH ...``
and must import nothing outside the standard library. It reads the hook
payload from stdin and exits 2 (block, message on stderr) unless the resolved
target is inside an ``--allow`` root and outside every ``--deny`` path. Any
malformed input is blocked.
"""

from __future__ import annotations

import json
import os
import stat
import sys

GUARDED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
BLOCK = 2


def _block(message: str) -> int:
    print(f"Spec Butler write guard: {message}", file=sys.stderr)
    return BLOCK


def _parse_args(argv: list[str]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    denied: list[str] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--allow", "--deny"} or index + 1 >= len(argv):
            raise ValueError(f"unexpected argument {flag!r}")
        target = allowed if flag == "--allow" else denied
        target.append(os.path.realpath(argv[index + 1]))
        index += 2
    return allowed, denied


def _within(path: str, root: str, *, fold_case: bool) -> bool:
    if fold_case:
        path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def check(payload: object, allowed: list[str], denied: list[str]) -> str:
    """Return an empty string to allow the call, else the block reason."""
    if not isinstance(payload, dict):
        return "hook input is not a JSON object"
    tool = payload.get("tool_name")
    if tool not in GUARDED_TOOLS:
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return f"{tool} call has no tool_input"
    raw = tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if not isinstance(raw, str) or not raw.strip():
        return f"{tool} call has no target path"
    cwd = payload.get("cwd")
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
    # realpath follows symlinks, so a link inside the workspace cannot carry
    # a write outside it.
    target = os.path.realpath(os.path.join(base, os.path.expanduser(raw)))
    # Case-insensitive volumes (the macOS default) let a differently cased
    # spelling reach a protected path, so deny matching folds case.
    for path in denied:
        if _within(target, path, fold_case=True):
            return (
                f"{tool} may not modify {target}: it is protected by the "
                "orchestrator. Change files in the workspace instead."
            )
    if not any(_within(target, root, fold_case=False) for root in allowed):
        roots = ", ".join(allowed) or "(none)"
        return (
            f"{tool} may not write {target}: it is outside the workspace. "
            f"Allowed locations: {roots}."
        )
    try:
        info = os.lstat(target)
    except OSError:
        return ""
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        return (
            f"{tool} may not modify {target}: it is a hard link and may share "
            "its contents with a file outside the workspace."
        )
    return ""


def main(argv: list[str] | None = None) -> int:
    try:
        allowed, denied = _parse_args(list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        return _block(f"misconfigured hook ({exc})")
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        return _block(f"could not read hook input ({exc})")
    reason = check(payload, allowed, denied)
    return _block(reason) if reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
