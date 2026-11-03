"""Merge dependency recovery uses only local Git remotes and a stub forge."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from spec_runtime import doctor
from spec_runtime import orchestrator as orch
from spec_runtime.forge import GitHubForge


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def clone(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base")
    git(upstream, "tag", "spec/merged/dep")
    root = tmp_path / "clone"
    git(tmp_path, "clone", "--no-tags", str(upstream), str(root))
    (root / "specs").mkdir()
    (root / "specs" / "feature.md").write_text("---\nid: feature\ndepends_on: [dep]\n---\n")
    (root / "specs" / "dep.md").write_text("---\nid: dep\n---\n")
    monkeypatch.setattr(orch, "BASE_REF", "origin/main")
    monkeypatch.setattr(orch, "PR_BASE_BRANCH", "main")
    (root / ".spec.toml").write_text('base_ref = "origin/main"\n')
    monkeypatch.setattr(orch, "get_forge_adapter", lambda: SimpleNamespace(is_spec_merged=lambda *a, **kw: False))
    return root, upstream


def test_fetch_recovers_missing_tags(clone, monkeypatch):
    root, _ = clone
    monkeypatch.setattr(orch, "get_forge_adapter", lambda: pytest.fail("forge should not be needed"))
    assert not git(root, "tag", "--list")
    assert orch.check_dependencies_merged(root, "feature") == []
    assert "spec/merged/dep" in git(root, "tag", "--list")


def test_forge_fallback(clone, monkeypatch, caplog):
    root, upstream = clone
    git(upstream, "tag", "-d", "spec/merged/dep")
    monkeypatch.setattr(orch, "get_forge_adapter", lambda: SimpleNamespace(is_spec_merged=lambda *a, **kw: True))
    assert orch.check_dependencies_merged(root, "feature") == []
    assert "spec-backfill-merge-tags backfill" in caplog.text


def test_unmerged_dependency_and_failed_fetch(clone):
    root, upstream = clone
    git(upstream, "tag", "-d", "spec/merged/dep")
    git(root, "remote", "remove", "origin")
    blockers = orch.check_dependencies_merged(root, "feature")
    assert len(blockers) == 1
    assert "dep (not-started)" in blockers[0]
    assert "merge tags could not be fetched" in blockers[0]


def test_doctor_missing_tags(clone):
    root, _ = clone
    checks = doctor._git_checks(
        root,
        SimpleNamespace(base_ref="origin/main"),
        lambda cmd, cwd, timeout: subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
        ),
    )
    check = next(check for check in checks if check.name == "merge tags")
    assert check.status == "warning"
    assert "1 remote merge tags missing" in check.detail


@pytest.mark.parametrize(
    "branch,merged,base,expected",
    [
        ("code/dep--abc", True, "main", True),
        ("spec/dep", True, "main", False),
        ("code/dep-other--abc", True, "main", False),
        ("code/dep--abc", False, "main", False),
        ("code/dep--abc", True, "other", False),
    ],
)
def test_forge_requires_matching_merged_implementation(monkeypatch, tmp_path, branch, merged, base, expected):
    forge = GitHubForge()

    def gh(args, **kwargs):
        assert "--paginate" in args
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    [
                        {
                            "head": {"ref": branch},
                            "base": {"ref": base},
                            "merged_at": "now" if merged else None,
                        }
                    ]
                ]
            ),
        )

    monkeypatch.setattr(forge, "_gh", gh)
    assert forge.is_spec_merged("dep", base_branch="main", cwd=tmp_path) is expected


def test_merged_dependencies_do_not_fetch(clone, monkeypatch):
    root, _ = clone
    git(root, "fetch", "origin", "refs/tags/spec/merged/*:refs/tags/spec/merged/*")
    monkeypatch.setattr(orch, "run_git_fetch_with_timeout", lambda *a, **kw: pytest.fail("unnecessary fetch"))
    assert orch.check_dependencies_merged(root, "feature") == []


def test_fetch_timeout_is_reported(clone, monkeypatch):
    root, _ = clone

    def timeout(*args, **kwargs):
        raise orch.GitFetchTimeoutError(("git", "fetch"), 30, "")

    monkeypatch.setattr(orch, "run_git_fetch_with_timeout", timeout)
    blockers = orch.check_dependencies_merged(root, "feature")
    assert "merge tags could not be fetched" in blockers[0]
    assert "30.0s timeout" in blockers[0]
