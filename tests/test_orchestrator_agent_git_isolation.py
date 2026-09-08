from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.agent_git_isolation import (
    cleanup_agent_git_isolation,
    prepare_agent_git_isolation,
)


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.com")
    _git(repository, "remote", "add", "origin", "https://github.com/example/project.git")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    _git(repository, "branch", "sibling")
    _git(repository, "worktree", "add", "-b", "code/test", str(worktree), "main")
    return repository, worktree


def test_implement_launch_imports_private_commit_before_return(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    initial_head = _git(worktree, "rev-parse", "HEAD")
    sibling_head = _git(repository, "rev-parse", "sibling")
    config_before = (repository / ".git" / "config").read_bytes()
    provider_code = "\n".join(
        (
            "from pathlib import Path",
            "import subprocess",
            "Path('tracked.txt').write_text('provider commit\\n', encoding='utf-8')",
            "subprocess.run(['git', 'add', 'tracked.txt'], check=True)",
            "subprocess.run(['git', 'commit', '-m', 'provider'], check=True)",
        )
    )
    environment = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
    plan = orch.ImplementLaunchPlan(
        use_stream_json=False,
        agent_env=environment,
        agent_cmd=[sys.executable, "-c", provider_code],
        popen_kwargs={"text": True},
        git_isolation=isolation,
    )
    run = orch.RunState(
        run_id="test-20260101T000000",
        spec_id="test",
        branch="code/test",
        agent="recording",
    )

    class LocalBackend:
        identity = SimpleNamespace(backend="worktree")

        @staticmethod
        def launch_agent(request, *, monitor):
            proc = orch.ProcessSupervisor(orch.LifetimeMode.RUN_OWNED).spawn(
                request.argv,
                cwd=request.cwd,
                env=request.env,
                **request.popen_kwargs,
            )
            return SimpleNamespace(returncode=monitor(proc))

    with patch.object(orch, "_resolve_execution_backend", return_value=LocalBackend()):
        assert orch._launch_implement_attempt(run, repository, worktree, plan) == 0

    final_head = _git(worktree, "rev-parse", "HEAD")
    assert final_head != initial_head
    assert _git(worktree, "show", "--format=%s", "--no-patch", "HEAD") == "provider"
    assert _git(repository, "rev-parse", "sibling") == sibling_head
    assert (repository / ".git" / "config").read_bytes() == config_before
    assert not isolation.private_git_dir.exists()


def test_claude_sandbox_writes_only_private_git_metadata(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    try:
        orch._write_sandbox_config("claude", worktree, git_isolation=isolation)
        settings = json.loads(
            (worktree / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        filesystem = settings["sandbox"]["filesystem"]
        assert filesystem["allowWrite"] == [str(isolation.private_git_dir)]
        assert str(worktree / ".git") in filesystem["denyWrite"]
        assert str(repository / ".git" / "objects") in filesystem["denyWrite"]
        assert str(repository / ".git" / "refs") in filesystem["denyWrite"]
    finally:
        cleanup_agent_git_isolation(isolation)


def test_git_cleanup_failure_cannot_skip_provider_credential_cleanup(
    tmp_path: Path,
) -> None:
    _repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    auth_root = tmp_path / "codex-home"
    auth_root.mkdir()
    plan = orch.ImplementLaunchPlan(
        use_stream_json=False,
        agent_env=isolation.apply_to_environment({"PATH": os.environ["PATH"]}),
        agent_cmd=[sys.executable, "-c", "pass"],
        popen_kwargs={"text": True},
        codex_auth_root=auth_root,
        git_isolation=isolation,
    )
    run = orch.RunState(
        run_id="test-20260101T000000",
        spec_id="test",
        branch="code/test",
        agent="codex",
    )
    backend = SimpleNamespace(
        launch_agent=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("launch failed")
        )
    )

    with (
        patch.object(orch, "_resolve_execution_backend", return_value=backend),
        patch.object(
            orch,
            "cleanup_agent_git_isolation",
            side_effect=RuntimeError("git cleanup failed"),
        ),
        patch.object(orch, "_remove_codex_launch_home") as remove_auth,
        pytest.raises(RuntimeError, match="git cleanup failed"),
    ):
        orch._launch_implement_attempt(run, tmp_path, worktree, plan)

    remove_auth.assert_called_once_with(None, auth_root, preserve_home=False)
    # The patched cleanup deliberately left this fixture behind.
    cleanup_agent_git_isolation(isolation)


def test_recovery_excludes_modify_only_private_git_metadata(tmp_path: Path) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    common_exclude = repository / ".git" / "info" / "exclude"
    common_before = common_exclude.read_bytes()
    isolation = prepare_agent_git_isolation(worktree)
    try:
        orch._seed_recovery_commit_excludes(
            worktree,
            git_isolation=isolation,
        )

        private_exclude = isolation.info_exclude_path.read_text(encoding="utf-8")
        assert ".tmp/" in private_exclude
        assert "pytest-of-*" in private_exclude
        assert common_exclude.read_bytes() == common_before
    finally:
        cleanup_agent_git_isolation(isolation)


def test_implement_plan_prepares_and_forwards_private_git_metadata(
    tmp_path: Path,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    run = orch.RunState(
        run_id="test-20260101T000000",
        spec_id="test",
        branch="code/test",
        agent="recording",
    )
    context = orch.ImplementContext(run_id=run.run_id, spec_path="specs/test.md")
    prepared: list = []
    captured_command: dict[str, object] = {}
    adapter = SimpleNamespace(capabilities=SimpleNamespace(supports_mcp=False))
    backend = SimpleNamespace(identity=SimpleNamespace(backend="worktree"))

    def build_command(*_args, **kwargs):
        captured_command.update(kwargs)
        return ["recording"]

    with (
        patch.object(orch, "_resolve_execution_backend", return_value=backend),
        patch.object(orch, "_require_agent_available_for_backend"),
        patch.object(
            orch,
            "_run_implement_setup_command",
            return_value=orch.ImplementSetupManifest(),
        ),
        patch.object(orch, "_register_setup_manifest_processes"),
        patch.object(orch, "get_agent_adapter", return_value=adapter),
        patch.object(orch, "_build_agent_command", side_effect=build_command),
    ):
        plan = orch._prepare_implement_launch_plan(
            run,
            repository,
            worktree,
            context,
            reason="initial",
            use_stream_json=False,
            git_isolations_to_cleanup=prepared,
        )

    assert plan.git_isolation is not None
    assert prepared == [plan.git_isolation]
    assert plan.agent_env["GIT_DIR"] == str(plan.git_isolation.private_git_dir)
    assert plan.agent_env["GIT_WORK_TREE"] == str(worktree)
    assert captured_command["git_isolation"] is plan.git_isolation
    cleanup_agent_git_isolation(plan.git_isolation)


def test_interactive_command_builders_forward_private_git_metadata(
    tmp_path: Path,
) -> None:
    repository, worktree = _linked_worktree(tmp_path)
    isolation = prepare_agent_git_isolation(worktree)
    captured: list[dict[str, object]] = []

    class RecordingAdapter:
        def build_authoring_command(self, **kwargs):
            captured.append(kwargs)
            return ["recording"]

    try:
        with patch.object(orch, "get_agent_adapter", return_value=RecordingAdapter()):
            orch._build_spec_authoring_command(
                "recording",
                repository,
                worktree,
                "test",
                "spec/test",
                resume=False,
                git_isolation=isolation,
            )
            orch._build_task_scoping_command(
                "recording",
                worktree,
                "prompt",
                "initial",
                git_isolation=isolation,
            )
        assert captured[0]["git_isolation"] is isolation
        assert captured[1]["git_isolation"] is isolation
    finally:
        cleanup_agent_git_isolation(isolation)
