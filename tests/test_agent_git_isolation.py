from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from spec_runtime.agent_git_isolation import (
    UnsafeAgentGitIsolationError,
    agent_git_added_paths,
    agent_git_head,
    agent_git_show_file,
    append_agent_git_exclude_patterns,
    cleanup_agent_git_isolation,
    prepare_agent_git_isolation,
    prepare_agent_git_isolation_if_linked,
    reconcile_agent_git_isolation,
    reset_agent_git_isolation,
)


def _git(
    cwd: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.com")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    _git(repository, "branch", "sibling")
    _git(repository, "worktree", "add", "-b", "code/test", str(worktree), "main")
    return repository, worktree


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
    return digest.hexdigest()


def _agent_git(worktree: Path, environment: dict[str, str], *arguments: str) -> str:
    return _git(worktree, *arguments, env=environment).stdout.strip()


def test_private_git_commit_does_not_touch_shared_metadata(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    shared_objects = repository / ".git" / "objects"
    shared_refs = repository / ".git" / "refs"
    objects_before = _tree_fingerprint(shared_objects)
    refs_before = _tree_fingerprint(shared_refs)
    config_before = (repository / ".git" / "config").read_bytes()
    sibling_before = _git(repository, "rev-parse", "sibling").stdout.strip()

    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    assert _agent_git(worktree, environment, "status", "--porcelain") == ""
    (worktree / "tracked.txt").write_text("private commit\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")
    _agent_git(worktree, environment, "branch", "sibling")

    assert _tree_fingerprint(shared_objects) == objects_before
    assert _tree_fingerprint(shared_refs) == refs_before
    assert (repository / ".git" / "config").read_bytes() == config_before
    assert _git(repository, "rev-parse", "sibling").stdout.strip() == sibling_before
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == isolation.initial_head
    assert isolation.writable_paths == (isolation.private_git_dir,)
    assert worktree / ".git" in isolation.read_only_paths
    assert repository / ".git" / "objects" in isolation.read_only_paths


def test_private_git_inherits_excludes_and_supports_private_recovery_patterns(
    tmp_path: Path,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    real_exclude = repository / ".git" / "info" / "exclude"
    real_exclude.write_text("*.operator-only\n", encoding="utf-8")
    isolation = prepare_agent_git_isolation(worktree)

    assert isolation.info_exclude_path.read_bytes() == real_exclude.read_bytes()
    append_agent_git_exclude_patterns(
        isolation,
        (".spec-recovery/", "*.provider-only"),
    )

    assert isolation.info_exclude_path.read_text(encoding="utf-8") == (
        "*.operator-only\n.spec-recovery/\n*.provider-only\n"
    )
    assert real_exclude.read_text(encoding="utf-8") == "*.operator-only\n"


def test_private_git_copies_context_and_fetches_only_into_private_metadata(
    tmp_path: Path,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    _git(repository, "tag", "v1")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(repository), str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "fetch", "origin")
    original_origin_main = _git(
        repository,
        "rev-parse",
        "refs/remotes/origin/main",
    ).stdout.strip()

    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    assert isolation.origin_url == str(remote)
    assert _agent_git(worktree, environment, "rev-parse", "origin/main") == (
        original_origin_main
    )
    assert _agent_git(worktree, environment, "rev-parse", "v1") == original_origin_main
    assert _agent_git(worktree, environment, "remote", "get-url", "--push", "origin") == (
        "specbutler-no-push://origin"
    )

    updater = tmp_path / "updater"
    _git(tmp_path, "clone", str(remote), str(updater))
    _git(updater, "config", "user.name", "Remote Operator")
    _git(updater, "config", "user.email", "remote@example.com")
    (updater / "remote.txt").write_text("new remote commit\n", encoding="utf-8")
    _git(updater, "add", "remote.txt")
    _git(updater, "commit", "-m", "remote update")
    _git(updater, "push", "origin", "main")
    remote_head = _git(updater, "rev-parse", "HEAD").stdout.strip()

    shared_objects_before = _tree_fingerprint(repository / ".git" / "objects")
    shared_refs_before = _tree_fingerprint(repository / ".git" / "refs")
    _agent_git(worktree, environment, "fetch", "origin")

    assert _agent_git(worktree, environment, "rev-parse", "origin/main") == remote_head
    assert _agent_git(worktree, environment, "log", "-1", "--format=%H", "origin/main") == (
        remote_head
    )
    _agent_git(worktree, environment, "diff", "origin/main")
    assert _git(repository, "rev-parse", "refs/remotes/origin/main").stdout.strip() == (
        original_origin_main
    )
    assert _tree_fingerprint(repository / ".git" / "objects") == shared_objects_before
    assert _tree_fingerprint(repository / ".git" / "refs") == shared_refs_before

    _agent_git(worktree, environment, "merge", "origin/main")
    result = reconcile_agent_git_isolation(isolation)
    assert result.final_head == remote_head
    assert result.imported_commit_count == 1
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == remote_head


def test_private_git_accepts_plain_local_origin_with_internal_spaces(
    tmp_path: Path,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    remote = tmp_path / "remote repository.git"
    _git(tmp_path, "clone", "--bare", str(repository), str(remote))
    _git(repository, "remote", "add", "origin", str(remote))

    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})

    assert isolation.origin_url == str(remote)
    _agent_git(worktree, environment, "fetch", "origin")
    assert _agent_git(worktree, environment, "rev-parse", "origin/main") == (
        isolation.initial_head
    )


@pytest.mark.parametrize(
    "remote_url",
    (
        "https://user:secret@example.com/repository.git",
        "u:secret@host:path",
        "https://example.com/repository.git?access_token=secret",
        "https://example.com/repository.git#secret",
        "https://example.com/repository name.git",
        "host:path with space",
        "/tmp/remote\tname.git",
        "https://example.com/repository.git\nmalicious",
        "ext::sh -c attacker",
        "helper://example.com/repository.git",
    ),
)
def test_private_git_rejects_unsafe_origin_transport(
    tmp_path: Path,
    remote_url: str,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    _git(repository, "remote", "add", "origin", remote_url)

    with pytest.raises(UnsafeAgentGitIsolationError, match="remote.origin.url"):
        prepare_agent_git_isolation(worktree)


def test_reconcile_imports_multiple_commits_and_preserves_uncommitted_files(
    tmp_path: Path,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})

    (worktree / "tracked.txt").write_text("commit one\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "one")
    (worktree / "second.txt").write_text("commit two\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "second.txt")
    _agent_git(worktree, environment, "commit", "-m", "two")
    private_head = _agent_git(worktree, environment, "rev-parse", "HEAD")

    # These edits are deliberately left out of the private commit chain.
    (worktree / "tracked.txt").write_text("still modified\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("still untracked\n", encoding="utf-8")

    result = reconcile_agent_git_isolation(isolation)

    assert result.final_head == private_head
    assert result.imported_commit_count == 2
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == private_head
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "still modified\n"
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "still untracked\n"
    status = _git(worktree, "status", "--porcelain").stdout.splitlines()
    assert " M tracked.txt" in status
    assert "?? untracked.txt" in status


def test_private_git_read_api_returns_only_committed_content(tmp_path: Path) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    task = worktree / "specs" / "tasks" / "private-task.md"
    task.parent.mkdir(parents=True)
    task.write_text("committed\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", task.relative_to(worktree).as_posix())
    _agent_git(worktree, environment, "commit", "-m", "private task")
    private_head = _agent_git(worktree, environment, "rev-parse", "HEAD")
    task.write_text("uncommitted\n", encoding="utf-8")

    assert agent_git_head(isolation) == private_head
    assert agent_git_added_paths(
        isolation,
        isolation.initial_head,
        "specs/tasks/*.md",
    ) == ("specs/tasks/private-task.md",)
    assert agent_git_show_file(
        isolation,
        private_head,
        "specs/tasks/private-task.md",
    ) == "committed\n"


@pytest.mark.parametrize("path", ["../outside", "/absolute", "specs/../outside"])
def test_private_git_read_api_rejects_unsafe_paths(
    tmp_path: Path,
    path: str,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)

    with pytest.raises(UnsafeAgentGitIsolationError, match="path is unsafe"):
        agent_git_added_paths(isolation, isolation.initial_head, path)
    with pytest.raises(UnsafeAgentGitIsolationError, match="path is unsafe"):
        agent_git_show_file(isolation, isolation.initial_head, path)


def test_reconcile_refuses_real_config_poisoning(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")

    with (repository / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("[core]\n\thooksPath = attacker-hooks\n")

    with pytest.raises(UnsafeAgentGitIsolationError, match="metadata changed"):
        reconcile_agent_git_isolation(isolation)
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == isolation.initial_head


def test_reconcile_tolerates_only_new_empty_real_worktree_config(
    tmp_path: Path,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    real_worktree_config = isolation.real_git_dir / "config.worktree"
    assert not real_worktree_config.exists()

    # Claude Code 2.1.257 creates this content-free file while its SDK client
    # connects. Empty Git configuration has no directives and changes no Git
    # behavior, so it is the sole tolerated real-metadata transition.
    real_worktree_config.write_bytes(b"")

    assert agent_git_head(isolation) == isolation.initial_head
    result = reconcile_agent_git_isolation(isolation)
    assert result.final_head == isolation.initial_head
    assert result.imported_commit_count == 0


def test_reconcile_refuses_nonempty_new_real_worktree_config(tmp_path: Path) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    (isolation.real_git_dir / "config.worktree").write_text(
        "[core]\n\thooksPath = attacker-hooks\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsafeAgentGitIsolationError, match="metadata changed"):
        reconcile_agent_git_isolation(isolation)


def test_reconcile_refuses_hardlinked_empty_real_worktree_config(
    tmp_path: Path,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    empty_source = tmp_path / "empty-source"
    empty_source.write_bytes(b"")
    try:
        os.link(empty_source, isolation.real_git_dir / "config.worktree")
    except OSError:
        pytest.skip("hardlink creation is unavailable on this filesystem")

    with pytest.raises(UnsafeAgentGitIsolationError, match="metadata changed"):
        reconcile_agent_git_isolation(isolation)


def test_reconcile_refuses_real_alternates_poisoning(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")
    (repository / ".git" / "objects" / "info" / "alternates").write_text(
        f"{tmp_path}\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsafeAgentGitIsolationError, match="metadata changed"):
        reconcile_agent_git_isolation(isolation)
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == isolation.initial_head


@pytest.mark.parametrize("target", ["config", "objects/info/alternates"])
def test_reconcile_refuses_private_config_or_alternates_poisoning(
    tmp_path: Path,
    target: str,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")
    (isolation.private_git_dir / target).write_text("poisoned\n", encoding="utf-8")

    with pytest.raises(UnsafeAgentGitIsolationError, match="changed"):
        reconcile_agent_git_isolation(isolation)
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == isolation.initial_head


def test_reconcile_refuses_layout_or_branch_changes(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    (worktree / ".git").write_text("gitdir: /tmp/not-the-worktree\n", encoding="utf-8")

    with pytest.raises(UnsafeAgentGitIsolationError):
        reconcile_agent_git_isolation(isolation)
    assert _git(repository, "rev-parse", "code/test").stdout.strip() == isolation.initial_head


def test_reconcile_refuses_an_independently_advanced_real_branch(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")

    outside = _git(
        repository,
        "commit-tree",
        f"{isolation.initial_head}^{{tree}}",
        "-p",
        isolation.initial_head,
        "-m",
        "outside",
    ).stdout.strip()
    _git(
        repository,
        "update-ref",
        isolation.branch_ref,
        outside,
        isolation.initial_head,
    )

    with pytest.raises(UnsafeAgentGitIsolationError, match="metadata changed"):
        reconcile_agent_git_isolation(isolation)
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == outside


def test_reconcile_refuses_private_history_that_drops_initial_head(tmp_path: Path) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")
    _agent_git(worktree, environment, "commit-tree", "HEAD^{tree}", "-m", "unrelated")
    unrelated = _agent_git(
        worktree,
        environment,
        "commit-tree",
        "HEAD^{tree}",
        "-m",
        "unrelated",
    )
    _agent_git(
        worktree,
        environment,
        "update-ref",
        isolation.branch_ref,
        unrelated,
    )

    with pytest.raises(UnsafeAgentGitIsolationError, match="merge-base"):
        reconcile_agent_git_isolation(isolation)


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook executable test")
def test_reconciliation_never_executes_repository_configured_hooks(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    hook_directory = tmp_path / "attacker-hooks"
    hook_directory.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hook_directory / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o700)
    _git(repository, "config", "core.hooksPath", str(hook_directory))
    isolation = prepare_agent_git_isolation(worktree)
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    (worktree / "tracked.txt").write_text("private\n", encoding="utf-8")
    _agent_git(worktree, environment, "add", "tracked.txt")
    _agent_git(worktree, environment, "commit", "-m", "private")

    reconcile_agent_git_isolation(isolation)

    assert not marker.exists()


def test_private_symlink_is_rejected_before_reconciliation(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    shutil_target = repository / ".git" / "objects" / "pack"
    private_pack = isolation.private_git_dir / "objects" / "pack"
    private_pack.rmdir()
    try:
        private_pack.symlink_to(shutil_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(UnsafeAgentGitIsolationError, match="symlink"):
        reconcile_agent_git_isolation(isolation)


def test_private_hardlink_is_rejected_before_reconciliation(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    alias = isolation.private_git_dir / "shared-config-alias"
    try:
        os.link(repository / ".git" / "config", alias)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this filesystem")

    with pytest.raises(UnsafeAgentGitIsolationError, match="hardlink"):
        reconcile_agent_git_isolation(isolation)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("codex") is None,
    reason="requires the installed Codex Linux sandbox",
)
def test_real_codex_sandbox_blocks_hardlinks_from_read_only_git_metadata(
    tmp_path: Path,
) -> None:
    from spec_runtime.provider_env import minimal_provider_environment
    from spec_runtime.web.bridge_codex import _CodexSession

    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    overrides = [
        item
        for item in _CodexSession._safety_config_overrides(
            codex_home,
            git_write_paths=isolation.writable_paths,
            git_read_only_paths=isolation.read_only_paths,
        )
        if item != "--strict-config"
    ]
    source = repository / ".git" / "config"
    alias = isolation.private_git_dir / "hardlink-alias"
    script = (
        "import os\n"
        f"source = {str(source)!r}\n"
        f"alias = {str(alias)!r}\n"
        "try:\n"
        "    os.link(source, alias)\n"
        "except OSError:\n"
        "    raise SystemExit(0)\n"
        "else:\n"
        "    os.unlink(alias)\n"
        "    raise SystemExit(17)\n"
    )
    result = subprocess.run(
        [
            "codex",
            "sandbox",
            "-C",
            str(worktree),
            *overrides,
            "-P",
            "specbutler-web",
            sys.executable,
            "-c",
            script,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=minimal_provider_environment("codex"),
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not alias.exists()


def test_real_claude_sandbox_blocks_hardlinks_from_read_only_git_metadata(
    tmp_path: Path,
) -> None:
    if os.environ.get("SPEC_LINUX_CLAUDE_REAL_PROVIDER") != "1":
        pytest.skip("set SPEC_LINUX_CLAUDE_REAL_PROVIDER=1 for the credentialed canary")
    if not sys.platform.startswith("linux") or shutil.which("claude") is None:
        pytest.skip("requires the installed Claude Linux sandbox")
    from spec_runtime.web.bridge_claude import (
        _web_provider_environment,
        _web_sandbox,
    )

    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    source = repository / ".git" / "config"
    alias = isolation.private_git_dir / "hardlink-alias"
    result_path = worktree / "hardlink-result"
    probe = worktree / "hardlink_probe.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"source = {str(source)!r}\n"
        f"alias = {str(alias)!r}\n"
        f"result = Path({str(result_path)!r})\n"
        "try:\n"
        "    os.link(source, alias)\n"
        "except OSError:\n"
        "    result.write_text('BLOCKED', encoding='utf-8')\n"
        "else:\n"
        "    os.unlink(alias)\n"
        "    result.write_text('ALLOWED', encoding='utf-8')\n",
        encoding="utf-8",
    )
    settings_path = worktree / "claude-hardlink-settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "sandbox": _web_sandbox(dict(os.environ), isolation),
                "permissions": {"disableBypassPermissionsMode": "disable"},
            }
        ),
        encoding="utf-8",
    )
    environment = _web_provider_environment(dict(os.environ))
    environment.update(isolation.env_overrides)
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"
    completed = subprocess.run(
        [
            "claude",
            "-p",
            "--restricted",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Bash",
            "--allowedTools",
            "Bash",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--max-budget-usd",
            "0.10",
            "--settings",
            str(settings_path),
            "--",
            f"Use the Bash tool exactly once to run `{command}`. Do nothing else.",
        ],
        cwd=worktree,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert result_path.read_text(encoding="utf-8") == "BLOCKED"
    assert not alias.exists()


def test_cleanup_and_explicit_reset_support_repeat_launches(tmp_path: Path) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    first = prepare_agent_git_isolation(worktree)
    with pytest.raises(UnsafeAgentGitIsolationError, match="already exists"):
        prepare_agent_git_isolation(worktree)

    cleanup_agent_git_isolation(first)
    assert not first.private_git_dir.exists()
    second = prepare_agent_git_isolation(worktree)
    marker = second.private_git_dir / "provider-marker"
    marker.write_text("discard me", encoding="utf-8")
    third = reset_agent_git_isolation(worktree)

    assert third.private_git_dir == second.private_git_dir
    assert not marker.exists()
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == third.initial_head


def test_prepare_if_linked_classifies_full_clone_without_mutation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    git_before = _tree_fingerprint(repository / ".git")

    assert prepare_agent_git_isolation_if_linked(repository) is None
    assert _tree_fingerprint(repository / ".git") == git_before


def test_prepare_if_linked_prepares_linked_worktree(tmp_path: Path) -> None:
    _repository, worktree = _linked_worktree(tmp_path)

    isolation = prepare_agent_git_isolation_if_linked(worktree)

    assert isolation is not None
    assert isolation.private_git_dir.is_dir()


def test_prepare_if_linked_rejects_symlinked_dot_git(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = tmp_path / "fake-git"
    target.mkdir()
    try:
        (repository / ".git").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(UnsafeAgentGitIsolationError, match="symlink"):
        prepare_agent_git_isolation_if_linked(repository)
