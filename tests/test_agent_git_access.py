"""Agents use Git natively in clone and worktree workspaces.

Covers the generated Claude settings and Codex permission profiles for both
layouts, the removal of the private-GIT_DIR guards from the default path, the
agent commit identity, the clone default backend, and the model-free probe.
All tests are hermetic: no provider sandbox or model is started.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runtime import agent_git_probe as probe
from spec_runtime import doctor
from spec_runtime import orchestrator as orch
from spec_runtime.agent_adapter import CodexAgent
from spec_runtime.agent_git_access import (
    UnsafeAgentGitLayoutError,
    agent_git_access,
    apply_agent_git_identity,
)
from spec_runtime.config import (
    ExecutionConfig,
    default_execution_backend,
    load_repo_spec_runtime_config,
)


def _git(cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.com")
    _git(repository, "remote", "add", "origin", "https://github.com/example/project.git")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    _git(repository, "branch", "sibling")
    return repository


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repository = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "-b", "code/test", str(worktree), "main")
    return repository.resolve(), worktree.resolve()


def _clone(tmp_path: Path) -> Path:
    repository = _repository(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--local", str(repository), str(clone))
    _git(clone, "checkout", "-B", "code/test")
    # The clone backend copies the operator identity into the clone.
    _git(clone, "config", "user.name", "Test Operator")
    _git(clone, "config", "user.email", "operator@example.com")
    return clone.resolve()


def _claude_filesystem(workspace: Path) -> dict[str, list[str]]:
    orch._write_sandbox_config("claude", workspace)
    settings = json.loads(
        (workspace / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    return settings["sandbox"]["filesystem"]


def _codex_filesystem_policy(cmd: list[str]) -> str:
    return next(
        cmd[index + 1]
        for index, value in enumerate(cmd[:-1])
        if value == "-c" and cmd[index + 1].startswith("permissions.specbutler-implement.filesystem=")
    )


def _add_dirs(cmd: list[str]) -> list[str]:
    return [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "--add-dir"]


# ---------------------------------------------------------------------------
# Git layout
# ---------------------------------------------------------------------------


def test_linked_worktree_grants_its_gitdir_and_the_shared_common_dir(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    access = agent_git_access(worktree)

    assert access is not None and access.linked
    common = repository / ".git"
    assert access.writable_paths == (common / "worktrees" / "worktree", common)
    assert access.protected_refs == ("refs/heads/main",)
    assert access.read_only_paths == (
        common / "config",
        common / "config.worktree",
        common / "hooks",
        common / "info",
        common / "worktrees" / "worktree" / "config.worktree",
        common / "HEAD",
        common / "index",
        common / "refs" / "heads" / "main",
        common / "refs" / "heads" / "main.lock",
        common / "packed-refs",
        common / "packed-refs.lock",
    )


def test_clone_grants_only_its_own_git_directory(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    access = agent_git_access(clone)

    assert access is not None and not access.linked
    assert access.writable_paths == (clone / ".git",)
    assert access.read_only_paths == tuple(
        clone / ".git" / name for name in ("config", "config.worktree", "hooks", "info")
    )


def test_symlinked_git_metadata_is_rejected(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / ".git").symlink_to(clone / ".git", target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable")

    with pytest.raises(UnsafeAgentGitLayoutError, match="symlinked"):
        agent_git_access(workspace)
    with pytest.raises(ValueError, match="symlinked"):
        orch._write_sandbox_config("claude", workspace)
    with pytest.raises(RuntimeError, match="symlinked"):
        CodexAgent().build_implement_command(
            prompt="Do the work", worktree_path=workspace, state_dir=tmp_path / "state"
        )


def test_repointed_worktree_git_file_is_rejected(tmp_path: Path) -> None:
    """A rewritten .git pointer must not redirect grants to other metadata."""
    _repository_root, worktree = _linked_worktree(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_repository = _repository(other_root)
    other_worktree = other_root / "other-worktree"
    _git(other_repository, "worktree", "add", "-b", "code/other", str(other_worktree), "main")
    other_git_dir = other_repository.resolve() / ".git" / "worktrees" / "other-worktree"
    pointer = worktree / ".git"
    os.chmod(pointer, stat.S_IREAD | stat.S_IWRITE)
    # Git for Windows marks the pointer hidden; Windows refuses a truncating
    # open of a hidden file, so rewrite it in place.
    with open(pointer, "r+", encoding="utf-8") as handle:
        handle.write(f"gitdir: {other_git_dir}\n")
        handle.truncate()

    with pytest.raises(UnsafeAgentGitLayoutError, match="registered to another worktree"):
        agent_git_access(worktree)


def test_workspace_without_git_has_no_git_grants(tmp_path: Path) -> None:
    assert agent_git_access(tmp_path) is None
    assert agent_git_access(tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# Claude settings
# ---------------------------------------------------------------------------


def test_claude_worktree_settings_allow_shared_git_but_not_its_controls(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    filesystem = _claude_filesystem(worktree)
    common = repository / ".git"

    assert filesystem["allowWrite"] == sorted(
        [str(common), str(common / "worktrees" / "worktree")]
    )
    for control in ("config", "hooks", "info", "HEAD", "index"):
        assert str(common / control) in filesystem["denyWrite"]
    # The operator's checked-out branch cannot be moved from the session.
    assert str(common / "refs" / "heads" / "main") in filesystem["denyWrite"]
    assert str(common / "refs" / "heads" / "main.lock") in filesystem["denyWrite"]
    assert str(common / "refs" / "heads" / "sibling") not in filesystem["denyWrite"]
    # Objects, refs and the stash live in the common dir and stay writable.
    for shared in ("objects", "refs", "logs"):
        assert str(common / shared) not in filesystem["denyWrite"]
    assert not any("specbutler-private-git" in path for path in filesystem["allowWrite"])


def test_claude_clone_settings_allow_git_except_config_hooks_and_info(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    filesystem = _claude_filesystem(clone)
    git_dir = clone / ".git"

    assert filesystem["allowWrite"] == [str(git_dir)]
    for control in ("config", "config.worktree", "hooks", "info"):
        assert str(git_dir / control) in filesystem["denyWrite"]
    settings = json.loads((clone / ".claude" / "settings.local.json").read_text())
    hooks_pattern = (git_dir / "hooks").as_posix()
    if not hooks_pattern.startswith("//"):
        hooks_pattern = "/" + hooks_pattern
    for tool in ("Write", "Edit"):
        assert f"{tool}({hooks_pattern}/**)" in settings["permissions"]["deny"]


# ---------------------------------------------------------------------------
# Codex permission profile
# ---------------------------------------------------------------------------


def test_codex_worktree_profile_writes_common_dir_with_read_only_controls(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    cmd = CodexAgent().build_implement_command(
        prompt="Do the work", worktree_path=worktree, state_dir=tmp_path / "state"
    )
    common = repository / ".git"
    policy = _codex_filesystem_policy(cmd)

    assert str(common) in _add_dirs(cmd)
    assert str(common / "worktrees" / "worktree") in _add_dirs(cmd)
    assert f'{json.dumps(str(common))}="write"' in policy
    assert f'{json.dumps(str(common / "worktrees" / "worktree"))}="write"' in policy
    for control in ("config", "hooks", "info"):
        assert f'{json.dumps(str(common / control))}="read"' in policy
    # Codex rules must name existing paths; absent config.worktree is omitted.
    assert "config.worktree" not in policy


def test_codex_clone_profile_writes_git_with_read_only_controls(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    cmd = CodexAgent().build_implement_command(
        prompt="Do the work", worktree_path=clone, state_dir=tmp_path / "state"
    )
    policy = _codex_filesystem_policy(cmd)

    assert _add_dirs(cmd) == [str(tmp_path / "state"), str(clone / ".git")]
    assert f'{json.dumps(str(clone / ".git"))}="write"' in policy
    for control in ("config", "hooks", "info"):
        assert f'{json.dumps(str(clone / ".git" / control))}="read"' in policy


# ---------------------------------------------------------------------------
# Host: no private Git, no metadata guards
# ---------------------------------------------------------------------------


def test_worktree_launches_use_real_git_metadata_by_default(tmp_path: Path) -> None:
    _repository_root, worktree = _linked_worktree(tmp_path)

    assert orch._prepare_agent_git_boundary(worktree) is None
    assert not list((worktree.parent / "repository" / ".git" / "worktrees").glob(
        "*/specbutler-private-git"
    ))


def test_private_git_remains_available_behind_a_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEC_PRIVATE_AGENT_GIT", "1")
    _repository_root, worktree = _linked_worktree(tmp_path)
    isolation = orch._prepare_agent_git_boundary(worktree)
    try:
        assert isolation is not None
        assert isolation.private_git_dir.is_dir()
    finally:
        orch.cleanup_agent_git_isolation(isolation)


@pytest.fixture(autouse=True)
def _no_ambient_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)


def _agent_environment(worktree: Path) -> dict[str, str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(worktree.parent)}
    orch._apply_agent_git_environment(env, worktree, None)
    return env


def test_agent_git_works_in_linked_worktree_while_operator_changes_shared_metadata(
    tmp_path: Path,
) -> None:
    """The removed guards failed runs when shared metadata changed mid-session."""
    repository, worktree = _linked_worktree(tmp_path)
    env = _agent_environment(worktree)
    assert orch._prepare_agent_git_boundary(worktree) is None

    # Operator activity on the shared repository during the session.
    _git(repository, "commit", "--allow-empty", "-m", "operator", env=None)
    _git(repository, "config", "core.operatorNote", "changed")

    (worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt", env=env)
    _git(worktree, "commit", "-m", "agent work", env=env)
    (worktree / "tracked.txt").write_text("scratch\n", encoding="utf-8")
    _git(worktree, "stash", "push", "-m", "scratch", env=env)
    _git(worktree, "branch", "agent-side", "main", env=env)
    _git(worktree, "rebase", "main", env=env)
    log = _git(worktree, "log", "--format=%an <%ae> %s", "-1", env=env)

    assert log == "Test Operator <operator@example.com> agent work"
    orch._reconcile_and_cleanup_agent_git(None)
    assert _git(repository, "log", "--format=%s", "-1", "code/test") == "agent work"
    assert _git(repository, "stash", "list")


def test_agent_environment_sets_identity_husky_and_upstream_policy(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    env = _agent_environment(clone)

    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Test Operator"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "operator@example.com"
    assert env["HUSKY"] == "0"
    assert "GIT_DIR" not in env
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert entries["branch.autoSetupMerge"] == "false"
    assert entries["gc.auto"] == "0"
    assert entries["credential.helper"] == ""


def test_agent_identity_falls_back_when_the_operator_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    env: dict[str, str] = {}
    apply_agent_git_identity(env, workspace)

    assert env["GIT_AUTHOR_NAME"] == "Spec Butler"
    assert env["GIT_COMMITTER_EMAIL"] == "spec-butler@localhost"


def test_clone_backend_fetches_origin_before_cloning(tmp_path: Path) -> None:
    from spec_runtime.execution_backend import CloneExecutionBackend

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    repository = tmp_path / "repository"
    _git(tmp_path, "clone", str(remote), str(repository))
    for key, value in (("user.name", "Test Operator"), ("user.email", "operator@example.com")):
        _git(repository, "config", key, value)
    (repository / "README.md").write_text("one\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "one")
    _git(repository, "push", "origin", "main")
    # Someone else advances origin; the orchestration checkout has not fetched.
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "-c", "user.name=O", "-c", "user.email=o@x", "commit", "--allow-empty", "-m", "two")
    _git(other, "push", "origin", "main")
    remote_head = _git(remote, "rev-parse", "main")
    assert _git(repository, "rev-parse", "origin/main") != remote_head

    backend = CloneExecutionBackend(
        ExecutionConfig(backend="clone", workspace_root=".spec-workspaces", backend_explicit=True)
    )
    handle = backend.prepare_workspace(
        run_id="probe-abc",
        spec_id="probe",
        branch="code/probe--abc",
        repo_root=repository,
        base_ref="origin/main",
    )

    assert _git(handle.path, "rev-parse", "origin/main") == remote_head
    assert _git(handle.path, "rev-parse", "HEAD") == remote_head


def test_host_clone_borrows_host_objects_instead_of_hardlinking(tmp_path: Path) -> None:
    """The agent may write its clone's .git, so it must not share host inodes."""
    from spec_runtime.execution_backend import (
        CloneExecutionBackend,
        ContainerExecutionBackend,
    )

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    repository = tmp_path / "repository"
    _git(tmp_path, "clone", str(remote), str(repository))
    for key, value in (("user.name", "Test Operator"), ("user.email", "operator@example.com")):
        _git(repository, "config", key, value)
    (repository / "README.md").write_text("one\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "one")
    _git(repository, "push", "origin", "main")
    _git(repository, "gc", "-q")

    backend = CloneExecutionBackend(
        ExecutionConfig(backend="clone", workspace_root=".spec-workspaces", backend_explicit=True)
    )
    handle = backend.prepare_workspace(
        run_id="probe-abc",
        spec_id="probe",
        branch="code/probe--abc",
        repo_root=repository,
        base_ref="origin/main",
    )

    alternates = handle.path / ".git" / "objects" / "info" / "alternates"
    assert alternates.read_text(encoding="utf-8").strip()
    host_inodes = {
        path.stat().st_ino
        for path in (repository / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    clone_inodes = {
        path.stat().st_ino
        for path in (handle.path / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    assert not host_inodes & clone_inodes
    assert ContainerExecutionBackend._share_host_objects is False


# ---------------------------------------------------------------------------
# Default backend
# ---------------------------------------------------------------------------


def test_clone_is_the_default_backend_without_explicit_config(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text('base_ref = "origin/main"\n', encoding="utf-8")
    config = load_repo_spec_runtime_config(tmp_path)

    expected = "worktree" if sys.platform == "win32" else "clone"
    assert config.execution.backend == expected
    assert not config.execution.backend_explicit
    assert ExecutionConfig().backend == expected


def test_native_windows_keeps_its_qualified_worktree_default() -> None:
    assert default_execution_backend("win32") == "worktree"
    assert default_execution_backend("darwin") == "clone"
    assert default_execution_backend("linux") == "clone"


def test_explicit_worktree_backend_is_still_supported(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        '[execution]\nbackend = "worktree"\n', encoding="utf-8"
    )
    config = load_repo_spec_runtime_config(tmp_path)

    assert config.execution.backend == "worktree"
    assert config.execution.backend_explicit


def test_spec_init_writes_the_default_backend() -> None:
    from spec_runtime.init import _generate_spec_toml

    text = _generate_spec_toml(
        base_ref="origin/main",
        default_agent="claude",
        review_default="codex",
        allowed_agents=["claude", "codex"],
        gates=[],
    )

    assert f'[execution]\n# clone gives each run its own .git so agents can commit; worktree also works.\nbackend = "{default_execution_backend()}"' in text


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _unsandboxed_runner(calls: list[list[str]]):
    """Run the probe script directly, as if the sandbox enforced nothing."""

    def run(argv, cwd, env, timeout):
        calls.append(list(argv))
        if argv[0].endswith("srt"):
            inner = argv[-1]
            return subprocess.run(
                inner, shell=True, cwd=cwd, env=dict(env), text=True, capture_output=True
            )
        inner = argv[argv.index(sys.executable):]
        return subprocess.run(inner, cwd=cwd, env=dict(env), text=True, capture_output=True)

    return run


@pytest.mark.parametrize("backend", ["clone", "worktree"])
def test_codex_probe_runs_git_under_codex_sandbox_with_the_implement_profile(
    backend: str,
) -> None:
    calls: list[list[str]] = []
    result = probe.run_agent_git_probe(
        "codex",
        backend,
        runner=_unsandboxed_runner(calls),
        which=lambda name: "/opt/bin/codex" if name == "codex" else None,
    )

    argv = calls[0]
    assert argv[:3] == ["/opt/bin/codex", "sandbox", "-C"]
    assert argv[argv.index("-P") + 1] == "specbutler-implement"
    assert result.runner == "codex sandbox"
    assert result.failed_steps == ()
    assert result.git_works
    # Nothing enforced the policy here, so every denied write is reported.
    assert result.leaked_writes == probe.PROBE_DENIED_WRITES
    assert not result.passed


@pytest.mark.skipif(sys.platform == "win32", reason="srt supports macOS and Linux only")
@pytest.mark.parametrize("backend", ["clone", "worktree"])
def test_claude_probe_runs_git_under_srt_with_generated_settings(backend: str) -> None:
    calls: list[list[str]] = []
    settings_seen: list[dict[str, object]] = []

    def run(argv, cwd, env, timeout):
        settings_seen.append(json.loads(Path(argv[2]).read_text(encoding="utf-8")))
        return _unsandboxed_runner(calls)(argv, cwd, env, timeout)

    result = probe.run_agent_git_probe(
        "claude",
        backend,
        runner=run,
        which=lambda name: "/opt/bin/srt" if name == "srt" else None,
    )

    assert calls[0][:2] == ["/opt/bin/srt", "--settings"]
    filesystem = settings_seen[0]["filesystem"]
    assert any(Path(path).name == ".git" for path in filesystem["allowWrite"]) or any(
        Path(path).parent.name == "worktrees" for path in filesystem["allowWrite"]
    )
    assert any(Path(path).name == "hooks" for path in filesystem["denyWrite"])
    assert any(Path(path).name == "config" for path in filesystem["denyWrite"])
    assert result.runner == "srt"
    assert result.git_works


@pytest.mark.skipif(sys.platform == "win32", reason="srt supports macOS and Linux only")
def test_claude_probe_reports_missing_sandbox_runner() -> None:
    result = probe.run_agent_git_probe("claude", "clone", which=lambda _name: None)

    assert not result.ran
    assert "srt" in result.unavailable_reason
    assert "@anthropic-ai/sandbox-runtime" in result.unavailable_reason


def test_probe_reports_nested_sandbox_failures_as_not_run() -> None:
    completed = subprocess.CompletedProcess(
        ["codex"], 71, "", "sandbox-exec: sandbox_apply: Operation not permitted"
    )
    result = probe.parse_probe_output("codex", "clone", "codex sandbox", completed)

    assert not result.ran
    assert "normal terminal" in result.summary()


def test_probe_result_parses_failed_steps_and_blocked_writes() -> None:
    payload = {"steps": {"add": "", "commit": "fatal: unable to write"}, "leaks": []}
    completed = subprocess.CompletedProcess(
        ["codex"], 0, "noise\nSPEC_AGENT_GIT_PROBE_RESULT=" + json.dumps(payload), ""
    )
    result = probe.parse_probe_output("codex", "worktree", "codex sandbox", completed)

    assert result.ran
    assert result.failed_steps == (("commit", "fatal: unable to write"),)
    assert not result.git_works
    assert "commit (fatal: unable to write)" in result.summary()


def _probe_result(agent: str, backend: str, **kwargs) -> probe.AgentGitProbeResult:
    return probe.AgentGitProbeResult(agent=agent, backend=backend, runner="test", **kwargs)


def test_doctor_reports_each_provider_and_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    config = SimpleNamespace(
        agents=SimpleNamespace(allowed=["claude", "codex", "custom"]),
        execution=SimpleNamespace(backend="clone"),
    )
    outcomes = {
        ("codex", "clone"): _probe_result("codex", "clone"),
        ("codex", "worktree"): _probe_result("codex", "worktree", leaked_writes=("git config",)),
        ("claude", "clone"): _probe_result(
            "claude", "clone", failed_steps=(("commit", "denied"),)
        ),
        ("claude", "worktree"): probe.AgentGitProbeResult(
            agent="claude", backend="worktree", unavailable_reason="no srt"
        ),
    }
    checks = {
        check.name: check
        for check in doctor._agent_git_probe_checks(
            config, lambda agent, backend: outcomes[(agent, backend)]
        )
    }

    assert set(checks) == {
        "agent git (claude, clone)",
        "agent git (claude, worktree)",
        "agent git (codex, clone)",
        "agent git (codex, worktree)",
    }
    assert checks["agent git (codex, clone)"].status == "ok"
    # Worktree mode reports an unblocked write rather than failing.
    assert checks["agent git (codex, worktree)"].status == "warning"
    assert checks["agent git (claude, clone)"].status == "error"
    assert checks["agent git (claude, worktree)"].status == "warning"


def test_doctor_blocks_a_clone_write_leak_but_not_one_for_the_other_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    config = SimpleNamespace(
        agents=SimpleNamespace(allowed=["codex"]),
        execution=SimpleNamespace(backend="clone"),
    )
    checks = doctor._agent_git_probe_checks(
        config,
        lambda agent, backend: _probe_result(agent, backend, leaked_writes=("git hooks",)),
    )

    assert [check.status for check in checks] == ["error", "warning"]


def test_doctor_probe_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(probe.PROBE_DISABLE_ENV, "0")
    config = SimpleNamespace(
        agents=SimpleNamespace(allowed=["codex"]),
        execution=SimpleNamespace(backend="clone"),
    )

    assert doctor._agent_git_probe_checks(config, lambda *_args: pytest.fail("ran")) == []


def test_launch_preflight_blocks_only_when_git_fails_in_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    monkeypatch.setattr(orch, "_AGENT_GIT_PROBE_RESULTS", {})
    backend = SimpleNamespace(identity=SimpleNamespace(backend="clone"))
    results = iter(
        [
            _probe_result("codex", "clone", failed_steps=(("add", "denied"),)),
        ]
    )
    calls: list[tuple[str, str]] = []

    def fake_probe(agent: str, backend_name: str):
        calls.append((agent, backend_name))
        return next(results)

    with patch.object(orch, "run_agent_git_probe", side_effect=fake_probe):
        first = orch._agent_git_launch_preflight_error("codex", backend)
        second = orch._agent_git_launch_preflight_error("codex", backend)

    assert "Git does not work inside the codex sandbox" in first
    assert second == first
    assert calls == [("codex", "clone")]


@pytest.mark.parametrize(
    ("backend_name", "leak"),
    [("clone", "git hooks"), ("clone", "outside workspace"), ("worktree", "outside workspace")],
)
def test_launch_preflight_blocks_writes_the_sandbox_should_deny(
    monkeypatch: pytest.MonkeyPatch,
    backend_name: str,
    leak: str,
) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    monkeypatch.setattr(orch, "_AGENT_GIT_PROBE_RESULTS", {})
    backend = SimpleNamespace(identity=SimpleNamespace(backend=backend_name))
    result = _probe_result("codex", backend_name, leaked_writes=(leak,))

    with patch.object(orch, "run_agent_git_probe", return_value=result):
        error = orch._agent_git_launch_preflight_error("codex", backend)

    assert f"did not block writes for the {backend_name} backend ({leak})" in error


@pytest.mark.parametrize(
    "result",
    [
        _probe_result("claude", "worktree", leaked_writes=("git config",)),
        probe.AgentGitProbeResult(agent="claude", backend="worktree", unavailable_reason="no srt"),
        probe.AgentGitProbeResult(agent="claude", backend="worktree", error="nested sandbox"),
    ],
)
def test_launch_preflight_does_not_block_on_unverified_or_widened_policy(
    monkeypatch: pytest.MonkeyPatch,
    result: probe.AgentGitProbeResult,
) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    monkeypatch.setattr(orch, "_AGENT_GIT_PROBE_RESULTS", {})
    backend = SimpleNamespace(identity=SimpleNamespace(backend="worktree"))

    with patch.object(orch, "run_agent_git_probe", return_value=result):
        assert orch._agent_git_launch_preflight_error("claude", backend) == ""


def test_launch_preflight_skips_container_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe.PROBE_DISABLE_ENV, raising=False)
    backend = SimpleNamespace(identity=SimpleNamespace(backend="container"))

    with patch.object(orch, "run_agent_git_probe", side_effect=AssertionError):
        assert orch._agent_git_launch_preflight_error("codex", backend) == ""
