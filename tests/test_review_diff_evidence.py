"""The Codex review prompt carries a bounded diff; the rest is read on demand (#129)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import spec_runtime.orchestrator as orch
from spec_runtime import review_diff_evidence as evidence
from spec_runtime.agent_adapter import CodexAgent
from spec_runtime.review_inspection import TOOLS, Inspection

GIT_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.org"]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _commit(root: Path, files: dict[str, str | None], message: str) -> str:
    for name, content in files.items():
        path = root / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _schema(entries: int) -> str:
    return json.dumps({"paths": {f"/thing/{i}": {"get": {"summary": f"Get thing {i}"}}
                                 for i in range(entries)}}, indent=2) + "\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def _full_diff(root: Path, base: str, head: str, *paths: str) -> str:
    return _git(root, "diff", "--unified=80", "--no-ext-diff", "--no-textconv",
                f"{base}...{head}", *(["--", *paths] if paths else []))


# --- splitting and planning -------------------------------------------------


def test_split_reproduces_the_diff_and_names_each_file(repo):
    base = _commit(repo, {"keep.py": "a = 1\n", "gone.py": "x = 1\n", "old name.py": "y = 1\n" * 30,
                          "logo.bin": "\0\1\2"}, "base")
    _git(repo, "mv", "old name.py", "new name.py")
    head = _commit(repo, {"keep.py": "a = 2\n", "gone.py": None, "added.py": "b = 1\nc = 2\n",
                          "new name.py": "y = 1\n" * 29 + "y = 2\n", "logo.bin": "\3\4\5"}, "head")
    diff = _full_diff(repo, base, head)
    files = evidence.split_diff_by_file(diff)
    assert "".join(f.text for f in files) == diff
    assert sorted(f.path for f in files) == ["added.py", "gone.py", "keep.py", "logo.bin", "new name.py"]
    counts = {f.path: (f.added, f.removed) for f in files}
    assert counts["keep.py"] == (1, 1)
    assert counts["added.py"] == (2, 0)
    assert counts["gone.py"] == (0, 1)


def test_small_diff_is_inlined_whole():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    plan = evidence.plan_review_diff(diff, budget=1000)
    assert plan.complete
    assert plan.inline_text() == diff


def _section(path: str, size: int) -> str:
    body = "".join(f"+line {i}\n" for i in range(size))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{size} @@\n{body}"


def test_generated_files_are_never_inlined_and_the_rest_fill_the_budget():
    diff = _section("a.py", 10) + _section("api.json", 10) + _section("big.py", 500) + _section("c.py", 10)
    plan = evidence.plan_review_diff(diff, generated={"api.json"}, budget=2000)
    assert plan.statuses == {"a.py": "inline", "api.json": "generated", "big.py": "read_diff", "c.py": "inline"}
    assert not plan.complete
    assert plan.inline_text() == _section("a.py", 10) + _section("c.py", 10)
    manifest = evidence.render_manifest(plan)
    assert "generated\t+10\t-0\t" in manifest and manifest.rstrip().endswith("c.py")


# --- the Codex review prompt ------------------------------------------------


def _review_note(repo: Path, base: str, head: str, evidence_path: Path | None) -> str:
    return orch._codex_review_sandbox_prompt_note(
        repo, repo.parent / "scratch", base_sha=base, head_sha=head, evidence_path=evidence_path)


def test_large_generated_file_stays_out_of_the_prompt(repo, tmp_path):
    """The reported case: a ~400k-character generated schema beside small changes."""
    base = _commit(repo, {".gitattributes": "contracts/openapi.json linguist-generated\n",
                          "contracts/openapi.json": "{}\n", "src/handler.py": "def get():\n    pass\n"},
                   "base")
    head = _commit(repo, {"contracts/openapi.json": _schema(5000),
                          "src/handler.py": "def get():\n    return 'thing'\n"}, "head")
    assert len(_full_diff(repo, base, head)) > 400_000
    path = tmp_path / "state" / "review-diff-evidence.json"

    note = _review_note(repo, base, head, path)

    assert len(note) < orch.LOCAL_REVIEW_INLINE_DIFF_MAX_CHARS
    assert "generated\t+" in note and "contracts/openapi.json" in note
    assert "read_diff" in note
    assert "return 'thing'" in note  # the hand-written change is still inline
    payload = json.loads(path.read_text(encoding="utf-8"))
    for name in ("contracts/openapi.json", "src/handler.py"):
        assert payload["files"][name] == _full_diff(repo, base, head, name)


def test_large_hand_written_diff_without_attributes_is_still_bounded(repo, tmp_path):
    base = _commit(repo, {"small.py": "a = 1\n", "huge.py": "x = 0\n"}, "base")
    head = _commit(repo, {"small.py": "a = 2\n",
                          "huge.py": "".join(f"x{i} = {i}\n" for i in range(40_000))}, "head")
    path = tmp_path / "evidence.json"
    note = _review_note(repo, base, head, path)
    assert len(note) < orch.LOCAL_REVIEW_INLINE_DIFF_MAX_CHARS
    assert "read_diff\t+40000\t-1\t" in note
    assert "+a = 2" in note
    assert json.loads(path.read_text())["files"]["huge.py"] == _full_diff(repo, base, head, "huge.py")


def test_pr_cannot_mark_its_own_files_generated(repo, tmp_path):
    base = _commit(repo, {"src/a.py": "a = 1\n"}, "base")
    head = _commit(repo, {".gitattributes": "src/** linguist-generated\n",
                          "src/a.py": "a = 2\n" + "# padding\n" * 30_000}, "head")
    paths = [f.path for f in evidence.split_diff_by_file(_full_diff(repo, base, head))]
    assert orch._review_generated_paths(repo, base_sha=base, paths=paths) == frozenset()
    note = _review_note(repo, base, head, tmp_path / "evidence.json")
    assert "generated\t" not in note


def test_small_pr_prompt_is_unchanged(repo, tmp_path):
    base = _commit(repo, {"a.py": "a = 1\n"}, "base")
    head = _commit(repo, {"a.py": "a = 2\n"}, "head")
    diff = _full_diff(repo, base, head)
    note = _review_note(repo, base, head, tmp_path / "evidence.json")
    assert note.endswith(
        "The orchestrator materialized the authoritative diff on the host below; "
        "the gate results already in this prompt are authoritative.\n\n"
        f"<BEGIN_HOST_MATERIALIZED_DIFF>\n{diff}\n<END_HOST_MATERIALIZED_DIFF>")
    assert "MANIFEST" not in note
    assert _review_note(repo, base, head, None) == note


def test_oversized_incremental_diff_lists_files(repo):
    previous = _commit(repo, {"a.py": "a = 1\n"}, "reviewed")
    head = _commit(repo, {"a.py": "".join(f"a{i} = {i}\n" for i in range(40_000))}, "fix")
    text, ranges = orch._local_review_incremental_diff(
        repo, last_reviewed_head_sha=previous, head_sha=head)
    assert len(text) < 2000
    assert "too large to include" in text and "+40000 -1\ta.py" in text
    assert ranges and "a.py" in ranges


# --- the inspection server --------------------------------------------------


def _evidence_file(tmp_path: Path, files: dict[str, str]) -> Path:
    diff = "".join(files.values())
    plan = evidence.plan_review_diff(diff, budget=10)
    path = tmp_path / "evidence.json"
    evidence.write_evidence(path, plan, base_sha="b", head_sha="h")
    return path


def test_read_diff_pages_through_a_file(tmp_path):
    long = _section("long.py", 20_000)
    path = _evidence_file(tmp_path, {"a": _section("a.py", 3), "long": long})
    server = Inspection(tmp_path, (), diff_evidence=path)
    assert "long.py" in server.call("read_diff", {})["text"]
    pages, offset = [], 0
    while offset is not None:
        page = server.call("read_diff", {"path": "long.py", "offset": offset})
        pages.append(page["text"])
        offset = page["next_offset"]
    assert len(pages) > 1 and "".join(pages) == long
    for bad in ({"path": "missing.py"}, {"path": "a.py", "offset": -1}, {"path": "a.py", "offset": True}):
        with pytest.raises(ValueError):
            server.call("read_diff", bad)
    with pytest.raises(ValueError):
        Inspection(tmp_path, ()).call("read_diff", {})


def test_read_diff_is_listed_only_with_evidence_over_stdio(tmp_path):
    path = _evidence_file(tmp_path, {"a": _section("a.py", 3)})
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "read_diff", "arguments": {"path": "a.py"}}},
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    for args, expected in (([], len(TOOLS)), ([f"--diff-evidence={path}"], len(TOOLS) + 1)):
        result = subprocess.run(
            [sys.executable, "-m", "spec_runtime.review_inspection", str(checkout), *args],
            input=stdin, text=True, capture_output=True, timeout=10, check=True)
        listed, called = (json.loads(line) for line in result.stdout.splitlines())
        assert len(listed["result"]["tools"]) == expected
        if args:
            assert "+line 2" in called["result"]["content"][0]["text"]
        else:
            assert called["result"]["isError"]


def test_codex_launch_passes_evidence_to_the_inspection_server(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    path = tmp_path / "state" / "evidence.json"
    with_evidence = CodexAgent().build_review_command(
        prompt="p", output_path=tmp_path / "out", review_checkout=checkout,
        writable_temp_dir=tmp_path, provider_home=tmp_path / "auth", review_diff_evidence=path)
    without = CodexAgent().build_review_command(
        prompt="p", output_path=tmp_path / "out", review_checkout=checkout,
        writable_temp_dir=tmp_path, provider_home=tmp_path / "auth")
    assert "--diff-evidence=" in " ".join(with_evidence)
    assert "--diff-evidence=" not in " ".join(without)
