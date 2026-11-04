"""Missing-spec guidance uses bounded fetches and leaves the checkout alone."""

import argparse
import subprocess

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.spec_lint import cmd_lint, lint_specs
from spec_runtime.spec_lookup import missing_spec_guidance


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def behind_repo(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.email", "test@example.com")
    git(remote, "config", "user.name", "Test")
    (remote / ".spec.toml").write_text('[workflow]\nbase_ref = "origin/main"\n', encoding="utf-8")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "initial")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    (remote / "specs").mkdir()
    (remote / "specs" / "new-spec.md").write_text("new spec", encoding="utf-8")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "spec")
    return checkout, remote


def test_refresh_and_count_without_checkout_changes(behind_repo):
    root, _ = behind_repo
    head = git(root, "rev-parse", "HEAD")
    message = missing_spec_guidance(root, "new-spec", base_ref="origin/main")
    assert "exists on origin/main but not in this checkout" in message
    assert "behind origin/main by 1 commits" in message
    assert "git pull --ff-only" in message
    assert git(root, "rev-parse", "HEAD") == head
    assert not (root / "specs" / "new-spec.md").exists()


def test_feature_branch_hint(behind_repo):
    root, _ = behind_repo
    git(root, "checkout", "-b", "feature")
    assert "git switch main" in missing_spec_guidance(root, "new-spec", base_ref="origin/main")
    assert git(root, "branch", "--show-current") == "feature"


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_fetch_uses_cache(behind_repo, monkeypatch, timeout):
    root, _ = behind_repo
    git(root, "fetch", "origin")
    original = subprocess.run

    def run(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            assert kwargs["timeout"] <= 5
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            assert "BatchMode=yes" in kwargs["env"]["GIT_SSH_COMMAND"]
            if timeout:
                raise subprocess.TimeoutExpired(command, 5)
            return subprocess.CompletedProcess(command, 1, "", "failed")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert "remote state may be stale" in missing_spec_guidance(root, "new-spec", base_ref="origin/main")


def test_truly_missing_unchanged(behind_repo, capsys):
    root, _ = behind_repo
    assert missing_spec_guidance(root, "absent", base_ref="origin/main") is None
    assert lint_specs(root, "absent")[0].errors == ["spec not found"]
    assert cmd_lint(argparse.Namespace(spec="absent", critique=False, json=False), repo_root=root) == 1
    assert "absent: error: spec not found" in capsys.readouterr().out


def test_commands_reject_without_starting_run(behind_repo, monkeypatch, capsys):
    root, _ = behind_repo
    # Exercise the actual command preflight, before any run contract is created.
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: root)
    monkeypatch.setattr(orch, "_acquire_coordinator_lease", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_release_coordinator_lease", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_create_spec_run", lambda *a, **k: pytest.fail("run started"))
    args = argparse.Namespace(spec="new-spec", agent=None, base="origin/main", branch="", run=None)
    assert orch.cmd_run(args) != 0
    assert "exists on origin/main" in capsys.readouterr().out
    # Use an explicit config base for the other public command paths.
    (root / ".spec.toml").write_text('base_ref = "origin/main"\n', encoding="utf-8")
    assert cmd_lint(argparse.Namespace(spec="new-spec", critique=False, json=False), repo_root=root) == 1
    assert "exists on origin/main" in capsys.readouterr().out
    assert orch.cmd_status(argparse.Namespace(spec="new-spec", run=None)) == 1
    assert "exists on origin/main" in capsys.readouterr().err
