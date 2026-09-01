from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from spec_runtime import cli
from spec_runtime.config import ExecutionConfig, SpecPathConfig, SpecRuntimeConfig


class _SpecLock:
    def __init__(self, *_args: object):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _config(*, backend: str = "worktree") -> SpecRuntimeConfig:
    return SpecRuntimeConfig(
        paths=SpecPathConfig(),
        execution=ExecutionConfig(backend=backend, workspace_root=".spec-workspaces"),
    )


def _run(repo: Path, *, backend: str = "worktree", worktree_path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        run_id="my-feature-20260814T120000000000",
        spec_id="my-feature",
        branch="code/my-feature--20260814T120000000000",
        backend=backend,
        safety_mode="safe",
        worktree_path=worktree_path,
    )


def _orch(
    runs: list[object],
    *,
    process_group: tuple[int, str] | None = None,
    identity: object | None = None,
    group_alive: bool = False,
) -> SimpleNamespace:
    run_state = MagicMock()
    run_state.list_for_spec.return_value = runs
    return SimpleNamespace(
        RunState=run_state,
        SpecLock=_SpecLock,
        _resolve_recorded_process_group=MagicMock(return_value=process_group),
        read_process_identity=MagicMock(return_value=identity),
        _is_process_group_alive=MagicMock(return_value=group_alive),
    )


def _git_read_only(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
    if cmd[:3] == ["git", "worktree", "list"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd[:2] == ["git", "branch"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if cmd[:2] == ["git", "show-ref"]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess: {cmd}")


@pytest.mark.parametrize("spec_id", ["*", "[x]", "../other", "x/y", "UPPER"])
def test_clean_rejects_invalid_spec_id_before_any_lookup_or_mutation(
    spec_id: str,
    capsys,
) -> None:
    with (
        patch("spec_runtime.git_common.resolve_common_root") as resolve_root,
        patch.object(cli, "_lazy_config") as load_config,
        patch.object(cli, "_lazy_orchestrator") as load_orchestrator,
        patch("spec_runtime.git_common.subprocess.run") as run_subprocess,
        patch.object(shutil, "rmtree") as remove_tree,
    ):
        result = cli._cmd_clean(argparse.Namespace(spec=spec_id))

    assert result == 1
    assert "Invalid spec ID" in capsys.readouterr().err
    resolve_root.assert_not_called()
    load_config.assert_not_called()
    load_orchestrator.assert_not_called()
    run_subprocess.assert_not_called()
    remove_tree.assert_not_called()


def test_clean_refuses_live_identity_matched_process_group_before_any_deletion(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run = _run(repo, backend="container")
    started_at = "Fri Aug 14 12:00:00 2026"
    orch = _orch(
        [run],
        process_group=(4321, started_at),
        identity=SimpleNamespace(started_at=started_at),
        group_alive=True,
    )

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config(backend="container")),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch(
            "spec_runtime.git_common.subprocess.run",
            side_effect=AssertionError("no subprocess after refusal"),
        ),
        patch.object(shutil, "rmtree", side_effect=AssertionError("no rmtree after refusal")),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature", force=True))

    captured = capsys.readouterr()
    assert result == 1
    assert "live orchestrator process group 4321" in captured.err
    assert "spec stop --spec my-feature" in captured.err
    assert "spec status --spec my-feature" in captured.err
    assert "spec container gc --apply" in captured.err


def test_clean_refuses_orphaned_live_group_when_recorded_leader_exited(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run = _run(repo)
    orch = _orch(
        [run],
        process_group=(4321, "Fri Aug 14 12:00:00 2026"),
        identity=None,
        group_alive=True,
    )

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config()),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature"))

    assert result == 1
    assert "live orchestrator process group" in capsys.readouterr().err


def test_clean_treats_reused_pid_with_different_start_time_as_stale(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    source = repo / ".spec-workspaces" / "my-feature-20260814T120000000000" / "source"
    source.mkdir(parents=True)
    run = _run(repo, backend="clone", worktree_path=str(source))
    orch = _orch(
        [run],
        process_group=(4321, "Fri Aug 14 12:00:00 2026"),
        identity=SimpleNamespace(started_at="Fri Aug 14 13:00:00 2026"),
        group_alive=True,
    )
    backend = MagicMock()

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config(backend="clone")),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch("spec_runtime.execution_backend.get_execution_backend", return_value=backend),
        patch("spec_runtime.git_common.subprocess.run", side_effect=_git_read_only),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature"))

    assert result == 0
    orch._is_process_group_alive.assert_not_called()
    backend.cleanup.assert_called_once()
    assert backend.cleanup.call_args.kwargs["allow_unpushed_work"] is True


def test_clean_routes_container_workspace_and_volumes_through_backend_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run_root = repo / ".spec-workspaces" / "my-feature-20260814T120000000000"
    source = run_root / "source"
    source.mkdir(parents=True)
    state_path = run_root / "backend-state" / "container-backend-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}\n")
    run = _run(repo, backend="container", worktree_path=str(source))
    orch = _orch([run])
    backend = MagicMock()

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config(backend="container")),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch("spec_runtime.execution_backend.get_execution_backend", return_value=backend),
        patch("spec_runtime.git_common.subprocess.run", side_effect=_git_read_only),
        patch.object(shutil, "rmtree", side_effect=AssertionError("CLI must not raw-delete backend resources")),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature"))

    assert result == 0
    backend.cleanup.assert_called_once()
    workspace = backend.cleanup.call_args.args[0]
    assert workspace.path == source
    assert workspace.outbox_path == run_root / "outbox"
    assert backend.cleanup.call_args.kwargs["allow_unpushed_work"] is True


def test_clean_missing_container_state_preserves_workspace_and_recommends_gc(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    source = repo / ".spec-workspaces" / "my-feature-20260814T120000000000" / "source"
    source.mkdir(parents=True)
    run = _run(repo, backend="container", worktree_path=str(source))
    orch = _orch([run])
    backend_factory = MagicMock()

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config(backend="container")),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch("spec_runtime.execution_backend.get_execution_backend", backend_factory),
        patch("spec_runtime.git_common.subprocess.run", side_effect=_git_read_only),
        patch.object(shutil, "rmtree", side_effect=AssertionError("workspace must survive")),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature"))

    assert result == 1
    assert source.is_dir()
    backend_factory.assert_not_called()
    assert "Container state is missing" in capsys.readouterr().err


def test_clean_refuses_live_registered_agent_with_pid_start_identity(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    worktree = repo / ".worktrees" / "code-my-feature--20260814T120000000000"
    worktree.mkdir(parents=True)
    run = _run(repo, worktree_path=str(worktree))
    orch = _orch([run])
    entry = SimpleNamespace(pid=9876, started_at="Fri Aug 14 12:00:00 2026", kind="agent")

    with (
        patch("spec_runtime.git_common.resolve_common_root", return_value=repo),
        patch.object(cli, "_lazy_config", return_value=_config()),
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch(
            "spec_runtime.worktree_process_registry.load_registered_processes",
            return_value=[entry],
        ),
        patch("spec_runtime.worktree_process_registry.is_process_alive", return_value=True),
        patch(
            "spec_runtime.git_common.subprocess.run",
            side_effect=AssertionError("no destructive subprocess"),
        ),
    ):
        result = cli._cmd_clean(argparse.Namespace(spec="my-feature"))

    assert result == 1
    assert "live registered agent process 9876" in capsys.readouterr().err


def test_stop_command_is_available_for_clean_remediation(tmp_path: Path, capsys) -> None:
    stopped = SimpleNamespace(run_id="my-feature-20260814T120000000000")
    orch = SimpleNamespace(stop_run=MagicMock(return_value=stopped))

    with (
        patch.object(cli, "_lazy_orchestrator", return_value=orch),
        patch.object(cli, "_resolve_repo_root", return_value=tmp_path),
    ):
        result = cli._cmd_stop(argparse.Namespace(spec="my-feature"))

    assert result == 0
    orch.stop_run.assert_called_once_with("my-feature", repo_root=tmp_path)
    assert "Stopped run my-feature-20260814T120000000000" in capsys.readouterr().out


def test_stop_command_parses_on_public_cli() -> None:
    with (
        patch.object(cli, "_lazy_config", return_value=_config()),
        patch.object(cli, "_emit_startup_update_notice"),
        patch.object(cli, "_cmd_stop", return_value=0) as stop,
    ):
        result = cli.main(["stop", "--spec", "my-feature"])

    assert result == 0
    stop.assert_called_once()
    assert stop.call_args.args[0].spec == "my-feature"
