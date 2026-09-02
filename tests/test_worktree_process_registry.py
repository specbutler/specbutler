from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spec_runtime import worktree_process_registry as registry


@pytest.mark.skipif(
    os.name != "posix"
    or not hasattr(os, "pidfd_open")
    or not hasattr(signal, "pidfd_send_signal"),
    reason="PID-scope cleanup requires a stable kernel process handle",
)
def test_pid_scope_reaps_only_registered_process_in_shared_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,signal,sys,time; "
        "open(sys.argv[1],'w').write(str(os.getpid())); "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        "time.sleep(60)"
    )
    launcher_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
        "child.wait(); time.sleep(60)"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_code, str(child_pid_path), child_code]
    )
    child_identity = None
    try:
        deadline = time.monotonic() + 10
        while not child_pid_path.exists():
            assert launcher.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        child_identity = registry.read_process_identity(child_pid)
        assert child_identity is not None
        assert os.getpgid(child_pid) == os.getpgid(launcher.pid)

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        registry.register_process(
            tmp_path,
            worktree,
            name="shared-group-child",
            kind="probe",
            pid=child_pid,
            started_at=child_identity.started_at,
            command=child_identity.command,
            termination_scope="pid",
        )

        report = registry.reap_registered_processes(tmp_path, worktree)

        assert report.terminated == (f"shared-group-child pid={child_pid} terminated",)
        assert report.surviving == ()
        assert launcher.poll() is None
        assert not registry.is_process_alive(child_pid, child_identity.started_at)
        assert registry.list_registered_worktrees(tmp_path) == []
    finally:
        if launcher.poll() is None:
            launcher.terminate()
        try:
            launcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            launcher.kill()
            launcher.wait(timeout=5)


def test_invalid_supervision_token_fails_closed_per_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    entry = registry.RegisteredProcess(
        name="corrupt",
        kind="probe",
        pid=42,
        started_at="created",
        supervision_token={"version": 2},
    )
    path = registry._registry_path(tmp_path, worktree)
    registry._write_json_file_atomically(
        path,
        {"worktree_path": str(worktree), "processes": [asdict(entry)]},
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: True)

    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.terminated == ()
    assert report.surviving == ("corrupt pid=42 has an invalid supervision token",)
    assert registry.load_registered_processes(tmp_path, worktree) == [entry]


def test_supervision_token_must_match_registered_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    other_identity = registry.ProcessIdentity(99, "other-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        other_identity,
        7,
        "owner-created",
        "other-boundary",
    )
    entry = registry.RegisteredProcess(
        name="mismatch",
        kind="probe",
        pid=42,
        started_at="created",
        supervision_token=token.to_dict(),
    )
    registry._write_json_file_atomically(
        registry._registry_path(tmp_path, worktree),
        {"worktree_path": str(worktree), "processes": [asdict(entry)]},
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: True)
    terminate = MagicMock()
    monkeypatch.setattr(registry, "terminate", terminate)

    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.surviving == (
        "mismatch pid=42 supervision token does not match the registered process",
    )
    terminate.assert_not_called()


def test_dead_payload_is_reaped_through_its_live_owned_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    keeper = registry.ProcessIdentity(41, "keeper-created")
    payload = registry.ProcessIdentity(42, "payload-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "setup-boundary",
        pgid=keeper.pid,
        payload_identity=payload,
    )
    registry.register_process(
        tmp_path,
        worktree,
        name="short-lived-server",
        kind="probe",
        pid=payload.pid,
        started_at=payload.started_at,
        supervision_token=token,
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: False)
    monkeypatch.setattr(
        registry,
        "supervision_boundary_is_inactive",
        lambda _token: False,
    )
    terminate = MagicMock(return_value=True)
    monkeypatch.setattr(registry, "terminate", terminate)

    assert registry.prune_dead_processes(tmp_path, worktree) == ()
    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.terminated == (
        f"short-lived-server pid={payload.pid} terminated",
    )
    assert report.stale == ()
    assert report.surviving == ()
    terminate.assert_called_once_with(
        token,
        grace_seconds=registry.PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )


def test_dead_token_is_pruned_after_complete_boundary_inactivity_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    keeper = registry.ProcessIdentity(41, "keeper-created")
    payload = registry.ProcessIdentity(42, "payload-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "completed-boundary",
        pgid=keeper.pid,
        payload_identity=payload,
    )
    registry.register_process(
        tmp_path,
        worktree,
        name="completed-service",
        kind="server",
        pid=payload.pid,
        started_at=payload.started_at,
        supervision_token=token,
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: False)
    boundary_inactive = MagicMock(return_value=True)
    monkeypatch.setattr(
        registry,
        "supervision_boundary_is_inactive",
        boundary_inactive,
    )

    assert registry.prune_dead_processes(tmp_path, worktree) == (
        f"completed-service pid={payload.pid} owned boundary already inactive",
    )
    boundary_inactive.assert_called_once_with(token)
    assert registry.load_registered_processes(tmp_path, worktree) == []
    assert registry.list_registered_worktrees(tmp_path) == []


def test_dead_token_is_preserved_without_complete_boundary_inactivity_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    keeper = registry.ProcessIdentity(41, "keeper-created")
    payload = registry.ProcessIdentity(42, "payload-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "active-boundary",
        pgid=keeper.pid,
        payload_identity=payload,
    )
    registry.register_process(
        tmp_path,
        worktree,
        name="possibly-active-service",
        kind="server",
        pid=payload.pid,
        started_at=payload.started_at,
        supervision_token=token,
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: False)
    monkeypatch.setattr(
        registry,
        "supervision_boundary_is_inactive",
        lambda _token: False,
    )

    assert registry.prune_dead_processes(tmp_path, worktree) == ()
    assert registry.load_registered_processes(tmp_path, worktree)[0].pid == payload.pid


def test_reap_retires_dead_token_when_boundary_is_definitively_inactive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    keeper = registry.ProcessIdentity(41, "keeper-created")
    payload = registry.ProcessIdentity(42, "payload-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "completed-before-reap",
        pgid=keeper.pid,
        payload_identity=payload,
    )
    registry.register_process(
        tmp_path,
        worktree,
        name="completed-service",
        kind="server",
        pid=payload.pid,
        started_at=payload.started_at,
        supervision_token=token,
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: False)
    monkeypatch.setattr(
        registry,
        "supervision_boundary_is_inactive",
        lambda _token: True,
    )
    terminate_boundary = MagicMock(side_effect=AssertionError("inactive boundary terminated"))
    monkeypatch.setattr(registry, "terminate", terminate_boundary)

    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.terminated == ()
    assert report.stale == (
        f"completed-service pid={payload.pid} owned boundary already inactive",
    )
    assert report.surviving == ()
    terminate_boundary.assert_not_called()
    assert registry.list_registered_worktrees(tmp_path) == []


def test_mismatched_posix_keeper_and_group_token_is_never_retired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from spec_runtime import process_supervisor

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    keeper = registry.ProcessIdentity(41, "keeper-created")
    payload = registry.ProcessIdentity(42, "payload-created")
    token = registry.SupervisionToken(
        registry.LifetimeMode.RUN_OWNED,
        keeper,
        7,
        "owner-created",
        "corrupt-posix-boundary",
        pgid=99,
        payload_identity=payload,
    )
    registry.register_process(
        tmp_path,
        worktree,
        name="corrupt-boundary-service",
        kind="server",
        pid=payload.pid,
        started_at=payload.started_at,
        supervision_token=token,
    )
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: False)
    monkeypatch.setattr(process_supervisor.os, "name", "posix")
    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: False)
    inventory = MagicMock(side_effect=AssertionError("mismatched group inventoried"))
    monkeypatch.setattr(
        process_supervisor,
        "list_live_process_group_members",
        inventory,
    )
    terminate_boundary = MagicMock(return_value=False)
    monkeypatch.setattr(registry, "terminate", terminate_boundary)

    assert registry.prune_dead_processes(tmp_path, worktree) == ()
    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.stale == ()
    assert report.surviving == (
        f"corrupt-boundary-service pid={payload.pid} exited but its owned "
        "boundary could not be reaped",
    )
    assert registry.load_registered_processes(tmp_path, worktree)[0].pid == payload.pid
    assert terminate_boundary.call_count == 1
    inventory.assert_not_called()


def test_setup_registration_batch_is_atomic_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    registry.register_process(
        tmp_path,
        worktree,
        name="existing",
        kind="probe",
        pid=40,
        started_at="existing-created",
    )
    before = registry.load_registered_processes(tmp_path, worktree)
    candidates = (
        registry.RegisteredProcess(
            name="service-a",
            kind="server",
            pid=41,
            started_at="service-a-created",
        ),
        registry.RegisteredProcess(
            name="service-b",
            kind="server",
            pid=42,
            started_at="service-b-created",
        ),
    )
    monkeypatch.setattr(
        registry,
        "_write_json_file_atomically",
        MagicMock(side_effect=OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        registry.register_processes(tmp_path, worktree, candidates)

    assert registry.load_registered_processes(tmp_path, worktree) == before


def test_malformed_registry_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    path = registry._registry_path(tmp_path, worktree)
    path.parent.mkdir(parents=True)
    path.write_text("{not-json\n", encoding="utf-8")

    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.surviving == (
        f"process registry for {worktree} is malformed; refusing cleanup",
    )
    assert path.read_text(encoding="utf-8") == "{not-json\n"


@pytest.mark.parametrize("malformed_token", ["corrupt", ["corrupt"], 7, False])
def test_non_mapping_supervision_token_is_malformed_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_token: object,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    path = registry._registry_path(tmp_path, worktree)
    payload = {
        "worktree_path": str(worktree),
        "processes": [
            {
                "name": "corrupt",
                "kind": "probe",
                "pid": 42,
                "started_at": "created",
                "command": "probe",
                "termination_scope": "pid",
                "pgid": 0,
                "registered_at": "now",
                "supervision_token": malformed_token,
            }
        ],
    }
    registry._write_json_file_atomically(path, payload)
    monkeypatch.setattr(registry, "is_process_alive", lambda *_args: True)
    terminate_exact = MagicMock()
    monkeypatch.setattr(registry, "terminate_exact_process", terminate_exact)

    report = registry.reap_registered_processes(tmp_path, worktree)

    assert report.terminated == ()
    assert report.surviving == (
        f"process registry for {worktree} contains malformed entries; refusing cleanup",
    )
    terminate_exact.assert_not_called()
    assert registry._read_json_dict(path) == payload
