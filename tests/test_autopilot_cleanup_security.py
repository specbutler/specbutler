from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_runtime import autopilot
from spec_runtime.execution_backend import WorkspaceHandle


def _write_stale_run(
    repo: Path,
    *,
    spec_id: str = "container-spec",
    run_id: object = "container-spec-20260903T120000",
    record: dict[str, object] | None = None,
) -> None:
    # Keep resolve_common_root anchored even when a developer machine happens
    # to have a Git checkout above pytest's temporary directory.
    (repo / ".git").mkdir(exist_ok=True)
    (repo / ".spec.toml").write_text("")
    active_path = autopilot.autopilot_active_path(repo)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(
        json.dumps({spec_id: {"run_id": run_id, "pid": 999_999}}) + "\n"
    )
    if not isinstance(run_id, str):
        return
    run_record = {
        "run_id": run_id,
        "spec_id": spec_id,
        "branch": f"code/{spec_id}--20260903T120000",
        "backend": "container",
        "safety_mode": "safe",
    }
    if record:
        run_record.update(record)
    state_runs = autopilot.runs_dir(repo)
    state_runs.mkdir(parents=True, exist_ok=True)
    try:
        (state_runs / f"{run_id}.json").write_text(json.dumps(run_record) + "\n")
    except OSError:
        # Invalid absolute/traversal ids are expected to be rejected before a
        # run-record read; no unsafe fixture path should be materialized.
        pass


class _CleanupRecorder:
    def __init__(self) -> None:
        self.calls: list[WorkspaceHandle] = []

    def cleanup(self, workspace: WorkspaceHandle) -> None:
        self.calls.append(workspace)


def test_unadopted_cleanup_passes_canonical_owned_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "container-spec-20260903T120000"
    _write_stale_run(repo, run_id=run_id)
    run_root = repo / ".spec-workspaces" / run_id
    (run_root / "source").mkdir(parents=True)
    (run_root / "outbox").mkdir()
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert len(recorder.calls) == 1
    handle = recorder.calls[0]
    assert handle.path == run_root / "source"
    assert handle.outbox_path == run_root / "outbox"
    assert handle.metadata == {
        "run_id": run_id,
        "spec_id": "container-spec",
        "repo_root": str(repo.resolve()),
        "workspace_root": str((repo / ".spec-workspaces").resolve()),
    }


@pytest.mark.parametrize(
    "run_id",
    [
        "../victim",
        "/tmp/victim",
        r"C:\victim",
        "other-spec-20260903T120000",
        "container-spec-",
        " container-spec-20260903T120000",
    ],
)
def test_unadopted_cleanup_rejects_noncanonical_run_ids_before_backend_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_stale_run(repo, run_id=run_id)
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert recorder.calls == []


def test_unadopted_cleanup_rejects_mismatched_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_stale_run(repo, record={"spec_id": "some-other-spec"})
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert recorder.calls == []


def test_unadopted_cleanup_rejects_symlinked_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "container-spec-20260903T120000"
    _write_stale_run(repo, run_id=run_id)
    run_path = autopilot.runs_dir(repo) / f"{run_id}.json"
    run_path.unlink()
    outside = tmp_path / "outside-run-record.json"
    outside.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "container-spec",
                "branch": "code/container-spec--20260903T120000",
                "backend": "container",
            }
        )
    )
    run_path.symlink_to(outside)
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert recorder.calls == []
    assert outside.is_file()


def test_unadopted_cleanup_rejects_symlinked_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "container-spec-20260903T120000"
    _write_stale_run(repo, run_id=run_id)
    outside = tmp_path / "outside-workspaces"
    (outside / run_id / "source").mkdir(parents=True)
    (outside / run_id / "outbox").mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n")
    (repo / ".spec-workspaces").symlink_to(outside, target_is_directory=True)
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert recorder.calls == []
    assert sentinel.read_text() == "keep\n"


@pytest.mark.parametrize("linked_component", ["run", "source", "outbox"])
def test_unadopted_cleanup_rejects_linked_run_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_component: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_id = "container-spec-20260903T120000"
    _write_stale_run(repo, run_id=run_id)
    workspace_root = repo / ".spec-workspaces"
    outside = tmp_path / f"outside-{linked_component}"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n")
    run_root = workspace_root / run_id
    if linked_component == "run":
        workspace_root.mkdir()
        run_root.symlink_to(outside, target_is_directory=True)
    else:
        run_root.mkdir(parents=True)
        other_component = "outbox" if linked_component == "source" else "source"
        (run_root / other_component).mkdir()
        (run_root / linked_component).symlink_to(outside, target_is_directory=True)
    recorder = _CleanupRecorder()
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _config: recorder)

    autopilot.cleanup_unadopted_container_runs(repo, {})

    assert recorder.calls == []
    assert sentinel.read_text() == "keep\n"
