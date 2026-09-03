from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runtime import orchestrator as orch

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX ownership and shared-temp protections",
)


def _outbox_directories(repo: Path, run_id: str, launch: int) -> list[Path]:
    result = orch._scoped_agent_completion_outbox_path(repo, run_id, launch)
    launch_dir = result.parent
    run_dir = launch_dir.parent
    repo_dir = run_dir.parent
    root = repo_dir.parent
    return [root, repo_dir, run_dir, launch_dir]


def _completion_payload(
    *,
    run_id: str,
    spec_id: str,
    attempt: int,
    launch: int,
) -> dict[str, object]:
    return {
        "artifact": "spec-agent-completion-report",
        "version": 1,
        "spec_id": spec_id,
        "run_id": run_id,
        "implement_result": {
            "status": "passed",
            "summary": "done",
            "attempt": attempt,
            "launch_number": launch,
            "result_source": "agent_report_outbox",
        },
    }


def test_outbox_fails_closed_when_shared_root_cannot_be_tightened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    root, repo_dir, _, _ = _outbox_directories(tmp_path / "repo", "run-1", 1)
    root.mkdir(mode=0o755)

    real_chmod = Path.chmod

    def guarded_chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        if path == root:
            raise PermissionError("owned by another local account")
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", guarded_chmod)

    with pytest.raises(PermissionError, match="another local account"):
        orch._prepare_scoped_agent_completion_outbox(
            tmp_path / "repo", "run-1", 1,
        )

    assert not repo_dir.exists()


def test_outbox_rejects_directory_owned_by_another_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    root, repo_dir, _, _ = _outbox_directories(tmp_path / "repo", "run-2", 2)
    root.mkdir(mode=0o700)
    real_stat = Path.stat

    def foreign_root_stat(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        value = real_stat(path, *args, **kwargs)
        if path == root:
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_uid=os.geteuid() + 1,
            )
        return value

    monkeypatch.setattr(Path, "stat", foreign_root_stat)

    with pytest.raises(PermissionError, match="owned by another user"):
        orch._prepare_scoped_agent_completion_outbox(
            tmp_path / "repo", "run-2", 2,
        )

    assert not repo_dir.exists()


@pytest.mark.parametrize("link_index", range(4))
def test_outbox_rejects_link_at_every_managed_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_index: int,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    directories = _outbox_directories(tmp_path / "repo", "run-3", 3)
    target = tmp_path / "attacker-controlled"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("preserve", encoding="utf-8")
    link = directories[link_index]
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="link-shaped"):
        orch._prepare_scoped_agent_completion_outbox(
            tmp_path / "repo", "run-3", 3,
        )

    assert link.is_symlink()
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_outbox_creates_private_current_user_directory_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))

    result = orch._prepare_scoped_agent_completion_outbox(
        tmp_path / "repo", "run-4", 4,
    )

    for directory in _outbox_directories(tmp_path / "repo", "run-4", 4):
        metadata = directory.stat(follow_symlinks=False)
        assert metadata.st_uid == os.geteuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o700
    assert result.name == orch.AGENT_COMPLETION_OUTBOX_FILENAME
    orch._cleanup_scoped_agent_completion_outbox(tmp_path / "repo", "run-4", 4)


def test_prepare_outbox_discards_preplanted_current_launch_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    repo = tmp_path / "repo"
    run_id = "feature-run"
    result_path = orch._scoped_agent_completion_outbox_path(repo, run_id, 5)
    result_path.parent.mkdir(mode=0o700, parents=True)
    result_path.write_text(
        json.dumps(
            _completion_payload(
                run_id=run_id,
                spec_id="feature",
                attempt=2,
                launch=5,
            )
        )
    )

    prepared = orch._prepare_scoped_agent_completion_outbox(repo, run_id, 5)

    assert prepared == result_path
    assert not result_path.exists()
    orch._cleanup_scoped_agent_completion_outbox(repo, run_id, 5)


def test_prepare_outbox_fails_closed_when_preplanted_launch_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    repo = tmp_path / "repo"
    run_id = "feature-run"
    result_path = orch._scoped_agent_completion_outbox_path(repo, run_id, 5)
    result_path.parent.mkdir(mode=0o700, parents=True)
    result_path.write_text(
        json.dumps(
            _completion_payload(
                run_id=run_id,
                spec_id="feature",
                attempt=2,
                launch=5,
            )
        )
    )
    real_rmtree = orch.shutil.rmtree

    def refuse_launch_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path == result_path.parent:
            raise PermissionError("preplanted launch is read-only")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(orch.shutil, "rmtree", refuse_launch_cleanup)

    with pytest.raises(PermissionError, match="preplanted launch is read-only"):
        orch._prepare_scoped_agent_completion_outbox(repo, run_id, 5)

    result, loaded_from_local = orch._load_matching_implement_result(
        repo,
        tmp_path / "worktree",
        run_id,
        attempt=2,
        launch_number=5,
        spec_id="feature",
    )
    assert result is None
    assert loaded_from_local is False


def test_live_launch_rejects_unprepared_preplanted_scoped_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_id = "feature-run"
    result_path = orch._scoped_agent_completion_outbox_path(repo, run_id, 6)
    result_path.parent.mkdir(mode=0o700, parents=True)
    result_path.write_text(
        json.dumps(
            _completion_payload(
                run_id=run_id,
                spec_id="feature",
                attempt=2,
                launch=6,
            )
        )
    )

    result, loaded_from_local = orch._load_matching_implement_result(
        repo,
        worktree,
        run_id,
        attempt=2,
        launch_number=6,
        spec_id="feature",
    )

    assert result is None
    assert loaded_from_local is False


def test_live_launch_never_accepts_matching_common_or_worktree_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(orch.tempfile, "gettempdir", lambda: str(shared))
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_id = "feature-run"
    orch._prepare_scoped_agent_completion_outbox(repo, run_id, 7)
    forged = orch.ImplementResult(
        status="passed",
        summary="preplanted alias",
        attempt=2,
        launch_number=7,
    )
    forged.save(repo, run_id)
    forged.save_to_state_root(worktree / ".spec-state", run_id)

    result, loaded_from_local = orch._load_matching_implement_result(
        repo,
        worktree,
        run_id,
        attempt=2,
        launch_number=7,
        spec_id="feature",
    )

    assert result is None
    assert loaded_from_local is False
    orch._cleanup_scoped_agent_completion_outbox(repo, run_id, 7)


def test_container_prelaunch_discards_preplanted_matching_current_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    run_root = tmp_path / "workspace"
    worktree = run_root / "source"
    worktree.mkdir(parents=True)
    (run_root / "logs").mkdir()
    (run_root / "backend-state").mkdir()
    (run_root / "backend-state" / "container-backend-state.json").write_text("{}")
    run_id = "feature-run"
    result_path = run_root / "outbox" / orch.AGENT_COMPLETION_OUTBOX_FILENAME
    result_path.parent.mkdir()
    result_path.write_text(
        json.dumps(
            _completion_payload(
                run_id=run_id,
                spec_id="feature",
                attempt=2,
                launch=8,
            )
        )
    )
    monkeypatch.setattr(orch, "_state_root", lambda _repo: repo / ".spec-state")

    orch._discard_prelaunch_completion_artifacts(repo, worktree, run_id)
    result, loaded_from_local = orch._load_matching_implement_result(
        repo,
        worktree,
        run_id,
        attempt=2,
        launch_number=8,
        spec_id="feature",
    )

    assert not result_path.exists()
    assert result is None
    assert loaded_from_local is False


@pytest.mark.parametrize("failing_alias", ["container", "common", "worktree"])
def test_prelaunch_discard_fails_closed_when_alias_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_alias: str,
) -> None:
    repo = tmp_path / "repo"
    run_root = tmp_path / "workspace"
    worktree = run_root / "source"
    worktree.mkdir(parents=True)
    (run_root / "logs").mkdir()
    (run_root / "backend-state").mkdir()
    (run_root / "backend-state" / "container-backend-state.json").write_text("{}")
    run_id = "feature-run"
    paths = {
        "container": run_root / "outbox" / orch.AGENT_COMPLETION_OUTBOX_FILENAME,
        "common": repo / ".spec-state" / "runs" / run_id / "implement-result.json",
        "worktree": worktree / ".spec-state" / "runs" / run_id / "implement-result.json",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    blocked_path = paths[failing_alias]
    real_unlink = Path.unlink

    def fail_one_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == blocked_path:
            raise PermissionError("read-only stale artifact")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one_unlink)
    monkeypatch.setattr(orch, "_state_root", lambda _repo: repo / ".spec-state")

    with pytest.raises(OSError, match="Refusing a fresh agent launch"):
        orch._discard_prelaunch_completion_artifacts(repo, worktree, run_id)

    assert blocked_path.is_file()
