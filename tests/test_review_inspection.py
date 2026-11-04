"""Hermetic inspection boundary tests and opt-in real Codex MCP smoke."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from spec_runtime.agent_adapter import CODEX_REVIEW_CHECKOUT_SHELL, CodexAgent
from spec_runtime.review_inspection import LIMIT, TOOLS, Inspection


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "hello.txt").write_bytes(b"hello review\n")
    for args in (["init"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.org",
                  "commit", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def test_each_tool(checkout):
    server = Inspection(checkout, ())
    assert server.call("read_file", {"path": "hello.txt"})["text"] == "hello review\n"
    assert "hello.txt" in server.call("list_dir", {})["entries"]
    assert server.call("glob", {"pattern": "*.txt"})["entries"] == ["hello.txt"]
    assert server.call("grep", {"pattern": "review"})["entries"] == ["hello.txt:1:hello review"]
    for operation in ("show", "log", "diff"):
        result = server.call("git_show", {"operation": operation, "paths": ["hello.txt"]})
        assert not result["truncated"]
        if operation != "diff":
            assert "fixture" in result["text"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_file_newlines(tmp_path, newline):
    text = "hello review" + newline
    (tmp_path / "hello.txt").write_bytes(text.encode("utf-8"))
    server = Inspection(tmp_path, ())
    assert server.call("read_file", {"path": "hello.txt"})["text"] == text
    assert server.call("grep", {"path": "hello.txt", "pattern": "review"})["entries"] == [
        "hello.txt:1:hello review"]


def test_escapes_and_protected(checkout, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("secret", encoding="utf-8")
    try:
        (checkout / "link").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    protected = checkout / "credentials"
    protected.mkdir()
    (protected / "key").write_text("secret", encoding="utf-8")
    server = Inspection(checkout, (protected,))
    for path in ("../secret", str(outside), "link", "credentials/key"):
        with pytest.raises(ValueError):
            server.read(path)
    assert "credentials" not in server.call("list_dir", {})["entries"]
    assert not server.call("grep", {"pattern": "secret"})["entries"]
    with pytest.raises(ValueError):
        server.git(paths=["credentials/key"])


def test_limits_and_git_rejection(checkout):
    (checkout / "large").write_text("x" * (LIMIT + 100), encoding="utf-8")
    server = Inspection(checkout, ())
    assert server.read("large")["truncated"]
    assert server.call("grep", {"pattern": "x"})["truncated"]
    for revision in ("--ext-diff", "--textconv", "HEAD:hello.txt", "HEAD;echo", "@{!x}"):
        with pytest.raises(ValueError):
            server.git(revision=revision, paths=["hello.txt"])
    for paths in (["../secret"], ["*"], ["."], []):
        with pytest.raises(ValueError):
            server.git(paths=paths)
    with pytest.raises(ValueError):
        server.git(operation="status", paths=["hello.txt"])


def test_stdio(checkout):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "read_file", "arguments": {"path": "hello.txt"}}},
    ]
    result = subprocess.run([sys.executable, "-m", "spec_runtime.review_inspection", str(checkout)],
                            input="".join(json.dumps(r) + "\n" for r in requests),
                            text=True, capture_output=True, timeout=10, check=True)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses[1]["result"]["tools"]) == 5
    assert "hello review" in responses[2]["result"]["content"][0]["text"]


def test_launch(checkout, tmp_path):
    cmd = CodexAgent().build_review_command(
        prompt="inspect", output_path=tmp_path / "out", review_checkout=checkout,
        writable_temp_dir=tmp_path, provider_home=tmp_path / "auth")
    assert not CODEX_REVIEW_CHECKOUT_SHELL
    assert "--strict-config" in cmd
    assert "features.shell_tool=false" in cmd
    assert "features.unified_exec=false" in cmd
    servers = [a for a in cmd if a.startswith("mcp_servers.")]
    assert servers and all(a.startswith("mcp_servers.review_inspection.") for a in servers)
    assert str(checkout).replace("\\", "\\\\") in " ".join(servers)




def test_default_provider_protection(checkout, monkeypatch):
    provider = checkout / "provider-home"
    provider.mkdir()
    (provider / "auth.json").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(provider))
    server = Inspection(checkout)
    with pytest.raises(ValueError):
        server.read("provider-home/auth.json")
    assert "provider-home/auth.json" not in server.call("glob", {"pattern": "*"})["entries"]


def test_git_environment_and_truncation(checkout, monkeypatch):
    (checkout / "large").write_text("x" * (LIMIT + 100), encoding="utf-8")
    subprocess.run(["git", "add", "large"], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.org",
                    "commit", "-m", "large"], cwd=checkout, check=True, capture_output=True)
    original = subprocess.Popen
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.external")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "untrusted-command")
    assert Inspection(checkout, ()).git(paths=["large"])["truncated"]
    env = calls[0][1]["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_COUNT" not in env


@pytest.mark.parametrize("tool", ["list_dir", "glob", "grep"])
def test_directory_tool_escapes(checkout, tool):
    with pytest.raises(ValueError):
        Inspection(checkout, ()).call(tool, {"path": "..", "pattern": "*"})


@pytest.mark.requires_network
def test_real_codex_inspection_smoke(checkout, tmp_path, record_property):
    """Exercise the installed CLI using local Responses SSE; no provider or credentials."""
    if os.environ.get("SPEC_CODEX_INSPECTION_SMOKE") != "1":
        pytest.skip("set SPEC_CODEX_INSPECTION_SMOKE=1 for the real CLI smoke")
    if not shutil.which("codex"):
        pytest.skip("Codex CLI unavailable")
    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    record_property("codex_cli_version", version)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                item = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                        "namespace": "mcp__review_inspection", "name": "read_file",
                        "arguments": json.dumps({"path": "hello.txt"})}
            else:
                item = {"type": "message", "id": "msg_1", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text",
                        "text": "smoke complete", "annotations": []}]}
            response = {"id": f"resp_{len(requests)}", "object": "response",
                        "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [
                {"type": "response.created", "response": {
                    **response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            body = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    home = tmp_path / "codex-home"
    home.mkdir()
    cmd = CodexAgent().build_review_command(
        prompt="", output_path=tmp_path / "result", review_checkout=checkout,
        writable_temp_dir=tmp_path, provider_home=home)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    overrides = [
        'model_provider="inspection_smoke"', 'model="inspection-smoke"',
        'model_providers.inspection_smoke.name="inspection smoke"',
        f'model_providers.inspection_smoke.base_url="http://127.0.0.1:{server.server_port}/v1"',
        'model_providers.inspection_smoke.wire_api="responses"',
    ]
    cmd[cmd.index("-o"):cmd.index("-o")] = [arg for value in overrides for arg in ("-c", value)]
    try:
        result = subprocess.run(cmd, input="Read hello.txt with read_file.", text=True,
                                capture_output=True, timeout=45,
                                env={**os.environ, "CODEX_HOME": str(home)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    calls = [e["item"] for e in events if e.get("type") == "item.completed"
             and e.get("item", {}).get("type") == "mcp_tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "read_file" and calls[0]["status"] == "completed"
    assert "hello review" in json.dumps(calls[0]["result"])
    assert len(requests) == 2
    tools = requests[0]["tools"]
    assert not any(t.get("name") in {"shell", "exec_command", "apply_patch"} for t in tools)
    assert [t["name"] for t in tools if t["type"] == "namespace"] == ["mcp__review_inspection"]


def test_git_disables_checkout_execution(checkout, tmp_path):
    """A local fsmonitor hook must never run during worktree diff inspection."""
    marker = tmp_path / "executed"
    hook = checkout / "monitor.py"
    hook.write_text(
        "import pathlib\npathlib.Path(" + repr(str(marker)) + ").touch()\n",
        encoding="utf-8")
    subprocess.run(["git", "config", "core.fsmonitor", f'{sys.executable} "{hook}"'],
                   cwd=checkout, check=True)
    subprocess.run(["git", "config", "core.worktree", str(tmp_path)], cwd=checkout, check=True)
    assert Inspection(checkout, ()).git(operation="diff", paths=["hello.txt"])["text"] == ""
    assert not marker.exists()


# Derive operation coverage from the published MCP contract, rather than a
# separate handwritten list that could miss a future Git operation.
GIT_OPERATIONS = next(t for t in TOOLS if t["name"] == "git_show")[
    "inputSchema"]["properties"]["operation"]["enum"]
SEARCH_TOOLS = [t["name"] for t in TOOLS
                if "pattern" in t["inputSchema"]["properties"]]


def git_fixture(checkout, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.org", *args],
        cwd=checkout, text=True).strip()


@pytest.mark.parametrize("operation", GIT_OPERATIONS)
@pytest.mark.parametrize("kind", ["blob", "tree", "blob-tag", "tree-tag"])
def test_git_rejects_noncommit_objects(checkout, operation, kind):
    protected = checkout / "credentials"
    protected.write_text("protected blob contents", encoding="utf-8")
    git_fixture(checkout, "add", "credentials")
    git_fixture(checkout, "commit", "-m", "protected fixture")
    blob = git_fixture(checkout, "rev-parse", "HEAD:credentials")
    tree = git_fixture(checkout, "rev-parse", "HEAD^{tree}")
    object_id = blob if kind.startswith("blob") else tree
    if kind.endswith("-tag"):
        git_fixture(checkout, "tag", "-a", "unsafe-object", object_id, "-m", "tag")
        object_id = "unsafe-object"
    server = Inspection(checkout, (protected,))
    for revision in (object_id, f"HEAD..{object_id}", f"{object_id}...HEAD"):
        with pytest.raises(ValueError, match="Git inspection failed"):
            server.git(operation=operation, revision=revision, paths=["hello.txt"])


@pytest.mark.parametrize("operation", GIT_OPERATIONS)
def test_git_commit_selectors_and_ranges(checkout, operation):
    git_fixture(checkout, "tag", "-a", "safe-commit", "HEAD", "-m", "commit tag")
    (checkout / "hello.txt").write_text("updated review\n", encoding="utf-8")
    git_fixture(checkout, "add", "hello.txt")
    git_fixture(checkout, "commit", "-m", "second commit")
    server = Inspection(checkout, ())
    for revision in ("HEAD", "safe-commit", "HEAD~1", "HEAD^", "safe-commit..HEAD",
                     "safe-commit...HEAD"):
        result = server.git(operation=operation, revision=revision, paths=["hello.txt"])
        assert not result["truncated"]
        if operation == "diff":
            if revision == "HEAD":
                assert result["text"] == ""
            else:
                assert "+updated review" in result["text"]
    for revision in ("HEAD^@", "HEAD^{tree}", "HEAD^{}", "HEAD@{0}", "HEAD....HEAD"):
        with pytest.raises(ValueError):
            server.git(operation=operation, revision=revision, paths=["hello.txt"])


@pytest.mark.parametrize("operation", GIT_OPERATIONS)
@pytest.mark.parametrize("driver", ["clean", "process"])
def test_git_never_runs_conversion_filters(checkout, tmp_path, operation, driver):
    marker = tmp_path / "filter-executed"
    hook = tmp_path / "filter.py"
    hook.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n",
                    encoding="utf-8")
    (checkout / ".gitattributes").write_text("hello.txt filter=untrusted\n", encoding="utf-8")
    git_fixture(checkout, "add", ".gitattributes")
    git_fixture(checkout, "commit", "-m", "attributes")
    git_fixture(checkout, "config", f"filter.untrusted.{driver}",
                shlex.join([sys.executable, str(hook)]))
    git_fixture(checkout, "config", "filter.untrusted.required", "true")
    # A worktree diff would need conversion even though the review only needs
    # committed history. Neither the single-ref nor range form may touch it.
    (checkout / "hello.txt").write_text("uncommitted conversion trigger\n", encoding="utf-8")
    server = Inspection(checkout, ())
    for revision in ("HEAD", "HEAD~1..HEAD", "HEAD~1...HEAD"):
        result = server.git(operation=operation, revision=revision, paths=["hello.txt"])
        assert "uncommitted conversion trigger" not in result["text"]
        assert not marker.exists()


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
def test_search_file_and_directory_paths(checkout, tool):
    server = Inspection(checkout, ())
    pattern = "review" if tool == "grep" else "*.txt"
    expected = "hello.txt:1:hello review" if tool == "grep" else "hello.txt"
    assert server.call(tool, {"path": "hello.txt", "pattern": pattern})["entries"] == [expected]
    assert server.call(tool, {"path": ".", "pattern": pattern})["entries"] == [expected]
    for path in ("..", str(checkout / "hello.txt")):
        with pytest.raises(ValueError):
            server.call(tool, {"path": path, "pattern": pattern})


@pytest.mark.parametrize(("pattern", "expected"), [
    ("**/*.txt", ["hello.txt", "src/nested/deep.txt", "src/one.txt"]),
    ("*.txt", ["hello.txt"]),
    ("src/*.py", ["src/one.py"]),
    ("src/**/*.py", ["src/nested/deep.py", "src/one.py"]),
    ("**/nested/**/*.py", ["src/nested/deep.py"]),
    ("src/??e.[pt][xy]", ["src/one.py"]),
    ("src/missing/**/*.py", []),
])
def test_glob_segments(checkout, pattern, expected):
    (checkout / "src" / "nested").mkdir(parents=True)
    for name in ("src/one.py", "src/one.txt", "src/nested/deep.py", "src/nested/deep.txt"):
        (checkout / name).write_text("review\n", encoding="utf-8")
    server = Inspection(checkout, ())
    assert sorted(server.call("glob", {"pattern": pattern})["entries"]) == expected
    assert server.call("glob", {"path": "src", "pattern": "*.py"})["entries"] == ["src/one.py"]
    assert sorted(server.call("glob", {"path": "src", "pattern": "**/*.py"})["entries"]) == [
        "src/nested/deep.py", "src/one.py"]
