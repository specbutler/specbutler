"""Regression tests for host-owned control-plane policy helpers.

These tests cover:
- stale lease detection vs. valid lease adoption
- bounded ``git fetch`` and timeout classification
- verify gate timeout classification and durable gate records
- conservative process adoption decisions
- interrupt/shutdown reconciliation
- canonical status projection for active, stale, and blocked runs
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spec_runtime.control_plane import (
    AdoptionDecision,
    CanonicalRunStatus,
    GateRecordStore,
    GateStatus,
    GitFetchOutcomeKind,
    GitFetchTimeoutError,
    LeaseStatus,
    ProcessAdoptionOutcome,
    RunLease,
    ShutdownPhase,
    ShutdownTracker,
    classify_git_fetch_failure,
    classify_lease,
    evaluate_process_adoption,
    load_run_lease,
    project_run_status,
    record_gate_completed,
    record_gate_started,
    record_gate_timeout,
    record_shutdown_complete,
    record_shutdown_initiated,
    run_git_fetch_with_timeout,
    save_run_lease,
)
from spec_runtime.control_plane.lease import build_lease

# --------------------------------------------------------------------------- #
# Lease tests
# --------------------------------------------------------------------------- #


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def test_lease_persists_round_trip(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    lease = build_lease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        backend="worktree",
        worker_id="worker-7",
        owner_host="host-a",
        timeout_seconds=900.0,
        process_pid=4242,
        process_started_at="Fri Jan 1 00:00:00 2026",
        now=_utc("2026-01-01T00:00:00"),
    )
    save_run_lease(runs_dir, lease)

    loaded = load_run_lease(runs_dir, "run-1")
    assert loaded is not None
    assert loaded.run_id == "run-1"
    assert loaded.spec_id == "spec-a"
    assert loaded.backend == "worktree"
    assert loaded.process_pid == 4242
    assert loaded.timeout_seconds == 900.0
    assert loaded.heartbeat_at == lease.heartbeat_at


def test_lease_active_when_heartbeat_fresh() -> None:
    lease = build_lease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        timeout_seconds=600.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    status = classify_lease(lease, now=_utc("2026-01-01T00:05:00"))
    assert status is LeaseStatus.ACTIVE


def test_lease_expired_when_heartbeat_stale() -> None:
    lease = build_lease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        timeout_seconds=300.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    status = classify_lease(lease, now=_utc("2026-01-01T01:00:00"))
    assert status is LeaseStatus.EXPIRED


def test_lease_stale_when_process_dead_but_heartbeat_fresh() -> None:
    lease = build_lease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        timeout_seconds=600.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    status = classify_lease(
        lease,
        now=_utc("2026-01-01T00:01:00"),
        process_alive=False,
    )
    assert status is LeaseStatus.STALE


def test_lease_unknown_without_heartbeat() -> None:
    lease = RunLease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        backend="worktree",
        worker_id="w",
        owner_host="h",
        started_at="",
        heartbeat_at="",
    )
    assert classify_lease(lease) is LeaseStatus.UNKNOWN
    assert classify_lease(None) is LeaseStatus.UNKNOWN


def test_lease_with_heartbeat_advances_timestamp() -> None:
    lease = build_lease(
        run_id="run-1",
        spec_id="spec-a",
        phase="implement",
        now=_utc("2026-01-01T00:00:00"),
    )
    refreshed = lease.with_heartbeat(now=_utc("2026-01-01T00:05:00"))
    assert refreshed.heartbeat_at != lease.heartbeat_at
    assert refreshed.run_id == lease.run_id


def test_lease_load_returns_none_on_missing(tmp_path: Path) -> None:
    assert load_run_lease(tmp_path, "missing") is None


def test_lease_load_returns_none_on_invalid_json(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "lease.json").write_text("not-json")
    assert load_run_lease(runs_dir, "run-1") is None


def test_lease_path_does_not_collide_with_run_state_glob(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    lease = build_lease(
        run_id="run-42",
        spec_id="spec-x",
        phase="implement",
        now=_utc("2026-01-01T00:00:00"),
    )
    save_run_lease(runs_dir, lease)
    # Run-state discovery scans ``runs_dir.glob("*.json")`` for synthetic run
    # records keyed by ``spec_id``. Lease files must not match that glob.
    top_level_json = list(runs_dir.glob("*.json"))
    assert top_level_json == []


def test_orchestrator_refresh_active_run_lease_persists_lease(tmp_path: Path) -> None:
    from spec_runtime import orchestrator as orch
    from spec_runtime.orchestrator import RunState

    repo_root = tmp_path
    (repo_root / ".spec-state" / "runs").mkdir(parents=True)
    run = RunState(
        spec_id="spec-y",
        run_id="run-y-1",
        branch="spec/spec-y",
        phase="implement",
    )

    orch._refresh_active_run_lease(repo_root, run, "implement")

    lease = load_run_lease(repo_root / ".spec-state" / "runs", "run-y-1")
    assert lease is not None
    assert lease.run_id == "run-y-1"
    assert lease.spec_id == "spec-y"
    assert lease.phase == "implement"
    assert lease.heartbeat_at  # must be set
    first_heartbeat = lease.heartbeat_at

    # A second refresh should advance the heartbeat (or at least not fail) and
    # preserve the lease record so status/watch can keep reading it.
    orch._refresh_active_run_lease(repo_root, run, "verify")
    refreshed = load_run_lease(repo_root / ".spec-state" / "runs", "run-y-1")
    assert refreshed is not None
    assert refreshed.phase == "verify"
    assert refreshed.heartbeat_at >= first_heartbeat


# --------------------------------------------------------------------------- #
# Git fetch timeout tests
# --------------------------------------------------------------------------- #


def test_classify_git_fetch_success() -> None:
    assert (
        classify_git_fetch_failure(returncode=0, timed_out=False)
        is GitFetchOutcomeKind.SUCCESS
    )


def test_classify_git_fetch_timeout() -> None:
    assert (
        classify_git_fetch_failure(returncode=124, timed_out=True)
        is GitFetchOutcomeKind.TIMEOUT
    )


def test_classify_git_fetch_failure_generic() -> None:
    assert (
        classify_git_fetch_failure(returncode=128, timed_out=False, stderr="fatal: not a git repo")
        is GitFetchOutcomeKind.FAILURE
    )


def test_run_git_fetch_with_timeout_success() -> None:
    def fake_runner(command, **kwargs):  # noqa: ANN001
        assert "fetch" in command
        assert kwargs["timeout"] == 30.0
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok\n", stderr="")

    outcome = run_git_fetch_with_timeout(
        ["origin", "main"],
        cwd=Path("."),
        timeout_seconds=30.0,
        runner=fake_runner,
        monotonic=iter([0.0, 0.5]).__next__,
    )
    assert outcome.is_success
    assert outcome.kind is GitFetchOutcomeKind.SUCCESS
    assert outcome.timeout_seconds == 30.0
    assert outcome.command == ("git", "fetch", "origin", "main")
    assert outcome.duration_seconds == pytest.approx(0.5)


def test_run_git_fetch_with_timeout_failure_passes_through_returncode() -> None:
    def fake_runner(command, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(
            args=command, returncode=128, stdout="", stderr="fatal: bad ref"
        )

    outcome = run_git_fetch_with_timeout(
        ["origin"],
        timeout_seconds=10.0,
        runner=fake_runner,
        monotonic=iter([0.0, 0.1]).__next__,
    )
    assert not outcome.is_success
    assert outcome.kind is GitFetchOutcomeKind.FAILURE
    assert outcome.returncode == 128


def test_run_git_fetch_with_timeout_raises_on_timeout() -> None:
    def fake_runner(command, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"], stderr=b"hung")

    with pytest.raises(GitFetchTimeoutError) as excinfo:
        run_git_fetch_with_timeout(
            ["origin"],
            timeout_seconds=2.5,
            runner=fake_runner,
            monotonic=iter([0.0, 2.5]).__next__,
        )
    err = excinfo.value
    assert err.timeout_seconds == 2.5
    assert err.command[:2] == ("git", "fetch")
    assert "hung" in err.partial_output


# --------------------------------------------------------------------------- #
# Gate record tests
# --------------------------------------------------------------------------- #


def test_gate_records_round_trip(tmp_path: Path) -> None:
    record = record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest", "tests/"],
        cwd=str(tmp_path),
        timeout_seconds=600.0,
        log_path=str(tmp_path / "pytest.log"),
        now=_utc("2026-01-01T00:00:00"),
    )
    assert record.status is GateStatus.STARTED
    assert record.command == ("pytest", "tests/")
    assert record.timeout_seconds == 600.0

    completed = record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=0,
        diagnostic="all green",
        now=_utc("2026-01-01T00:10:00"),
    )
    assert completed.status is GateStatus.PASSED
    assert completed.exit_status == 0
    assert completed.command == ("pytest", "tests/")
    assert completed.started_at == record.started_at
    assert completed.completed_at  # set
    assert completed.timeout_seconds == 600.0

    store = GateRecordStore(tmp_path)
    history = store.load()
    assert len(history) == 1
    assert history[0].status is GateStatus.PASSED


def test_gate_failure_distinct_from_timeout(tmp_path: Path) -> None:
    record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    failed = record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=1,
        diagnostic="2 failed",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert failed.status is GateStatus.FAILED
    assert failed.exit_status == 1
    assert not failed.is_timeout()

    record_gate_started(
        tmp_path,
        name="ruff",
        command=["ruff", "check", "."],
        cwd=str(tmp_path),
        timeout_seconds=30.0,
        now=_utc("2026-01-01T00:02:00"),
    )
    timed = record_gate_timeout(
        tmp_path,
        name="ruff",
        diagnostic="ruff exceeded 30s",
        now=_utc("2026-01-01T00:03:00"),
    )
    assert timed.status is GateStatus.TIMED_OUT
    assert timed.exit_status is None
    assert timed.is_timeout()
    assert timed.diagnostic == "ruff exceeded 30s"


def test_gate_record_persistence_survives_reload(tmp_path: Path) -> None:
    record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=0,
        now=_utc("2026-01-01T00:01:00"),
    )

    fresh_store = GateRecordStore(tmp_path)
    history = fresh_store.load()
    assert len(history) == 1
    assert history[0].status is GateStatus.PASSED
    assert fresh_store.latest_for_gate("pytest").status is GateStatus.PASSED
    assert fresh_store.pending_gates() == ()


def test_orchestrator_verify_subprocess_timeout_marks_completed_process(
    tmp_path: Path,
) -> None:
    from spec_runtime import orchestrator as orch

    def _raise(*args, **kwargs):  # noqa: ANN001 - signature matches run_subprocess
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr(orch, "run_subprocess", _raise)
        completed = orch._run_verify_subprocess_with_timeout(
            ["sleep", "5"],
            cwd=tmp_path,
            env={},
            timeout_seconds=1.0,
        )
    assert completed.returncode == 124
    assert getattr(completed, "_timed_out", False) is True
    assert "exceeded" in completed.stderr


def test_orchestrator_verify_gate_records_timeout(tmp_path: Path) -> None:
    from spec_runtime import orchestrator as orch

    record_gate_started(
        tmp_path,
        name="test",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=30.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    timed_out_proc = subprocess.CompletedProcess(
        args=["pytest"], returncode=124, stdout="", stderr="timed out"
    )
    timed_out_proc._timed_out = True  # type: ignore[attr-defined]
    result = orch.VerifyGateResult(completed_process=timed_out_proc)
    orch._record_verify_gate_finished(tmp_path, "test", result)

    store = GateRecordStore(tmp_path)
    latest = store.latest_for_gate("test")
    assert latest is not None
    assert latest.is_timeout()
    assert latest.exit_status is None


def test_gate_pending_gates_reports_started_only(tmp_path: Path) -> None:
    record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    record_gate_started(
        tmp_path,
        name="ruff",
        command=["ruff", "check", "."],
        cwd=str(tmp_path),
        timeout_seconds=30.0,
        now=_utc("2026-01-01T00:00:01"),
    )
    record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=0,
        now=_utc("2026-01-01T00:01:00"),
    )

    store = GateRecordStore(tmp_path)
    assert set(store.pending_gates()) == {"ruff"}


def test_gate_records_log_path_is_persisted(tmp_path: Path) -> None:
    log_path = str(tmp_path / "pytest.log")
    record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=10.0,
        log_path=log_path,
        now=_utc("2026-01-01T00:00:00"),
    )
    completed = record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=0,
        now=_utc("2026-01-01T00:00:05"),
    )
    assert completed.log_path == log_path

    payload = json.loads((tmp_path / "gate-records.json").read_text())
    assert payload["records"][0]["log_path"] == log_path


# --------------------------------------------------------------------------- #
# Process adoption tests
# --------------------------------------------------------------------------- #


def _make_lease(
    *,
    run_id: str = "run-1",
    spec_id: str = "spec-a",
    timeout_seconds: float = 600.0,
    started_at: str = "2026-01-01T00:00:00+00:00",
) -> RunLease:
    return build_lease(
        run_id=run_id,
        spec_id=spec_id,
        phase="implement",
        timeout_seconds=timeout_seconds,
        process_pid=42,
        process_started_at="Fri Jan 1 00:00:00 2026",
        now=_utc(started_at),
    )


def test_adopt_when_lease_active_and_process_alive() -> None:
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        recorded_process_started_at="Fri Jan 1 00:00:00 2026",
        process_alive=True,
        live_process_started_at="Fri Jan 1 00:00:00 2026",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.ADOPT
    assert outcome.should_wait
    assert not outcome.should_cleanup


def test_adoption_lease_missing_returns_retryable() -> None:
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=None,
        recorded_pid=0,
    )
    assert outcome.decision is AdoptionDecision.LEASE_MISSING
    assert outcome.should_retry
    assert not outcome.should_wait


def test_adoption_lease_expired_recommends_cleanup() -> None:
    lease = _make_lease(timeout_seconds=300.0, started_at="2026-01-01T00:00:00+00:00")
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=lease,
        recorded_pid=42,
        process_alive=True,
        now=_utc("2026-01-01T01:00:00"),
    )
    assert outcome.decision is AdoptionDecision.LEASE_EXPIRED
    assert outcome.should_cleanup
    assert outcome.should_retry
    assert not outcome.should_wait


def test_adoption_process_dead_returns_dead() -> None:
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        process_alive=False,
        now=_utc("2026-01-01T00:01:00"),
    )
    # process_alive=False with a still-fresh heartbeat is classified as STALE
    # by the lease layer, not PROCESS_DEAD; the cleanup signal is the same.
    assert outcome.decision is AdoptionDecision.LEASE_STALE
    assert outcome.should_cleanup


def test_adoption_run_mismatch_blocks_adoption() -> None:
    lease = _make_lease(run_id="other-run")
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=lease,
        recorded_pid=42,
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.RUN_MISMATCH
    assert not outcome.should_wait


def test_adoption_pid_recycled_returns_mismatch() -> None:
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        recorded_process_started_at="Fri Jan 1 00:00:00 2026",
        process_alive=True,
        live_process_started_at="Fri Jan 1 12:00:00 2026",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.PROCESS_MISMATCH
    assert outcome.should_cleanup


def test_adoption_refuses_when_process_alive_unverified() -> None:
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        recorded_process_started_at="Fri Jan 1 00:00:00 2026",
        live_process_started_at="Fri Jan 1 00:00:00 2026",
        process_alive=None,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.PROCESS_UNVERIFIED
    assert not outcome.should_wait
    assert outcome.should_retry


def test_adoption_lease_pid_disagrees_with_recorded_pid() -> None:
    # active.json points at pid=42 but the durable lease records a different
    # worker (pid=99). Adopting pid=42 would wait on a process that does not
    # belong to the lease, so adoption must be refused.
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=99,
        recorded_process_started_at="Fri Jan 1 00:00:00 2026",
        process_alive=True,
        live_process_started_at="Fri Jan 1 00:00:00 2026",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.PROCESS_MISMATCH
    assert outcome.should_cleanup
    assert not outcome.should_wait


def test_adoption_lease_started_at_disagrees_with_recorded() -> None:
    # Lease and active.json agree on the PID, but the lease's recorded
    # process_started_at differs from the recorded entry — the active.json
    # snapshot is stale and refers to a different process generation.
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        recorded_process_started_at="Fri Jan 1 12:00:00 2026",
        process_alive=True,
        live_process_started_at="Fri Jan 1 12:00:00 2026",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.PROCESS_MISMATCH
    assert outcome.should_cleanup
    assert not outcome.should_wait


def test_adoption_lease_started_at_disagrees_with_live() -> None:
    # Active.json was missing process_started_at (legacy) but the lease has
    # one. The live process has a different started_at than the lease worker,
    # which means the recorded PID has been recycled into a process that is
    # not the lease worker.
    outcome = evaluate_process_adoption(
        expected_run_id="run-1",
        expected_spec_id="spec-a",
        lease=_make_lease(),
        recorded_pid=42,
        recorded_process_started_at="",
        process_alive=True,
        live_process_started_at="Fri Jan 1 12:00:00 2026",
        now=_utc("2026-01-01T00:01:00"),
    )
    assert outcome.decision is AdoptionDecision.PROCESS_MISMATCH
    assert outcome.should_cleanup
    assert not outcome.should_wait


def test_adoption_outcome_helpers() -> None:
    o = ProcessAdoptionOutcome(AdoptionDecision.ADOPT)
    assert o.should_wait
    assert not o.should_cleanup
    assert not o.should_retry


# --------------------------------------------------------------------------- #
# Shutdown / interrupt tests
# --------------------------------------------------------------------------- #


def test_shutdown_first_interrupt_records_graceful(tmp_path: Path) -> None:
    state = record_shutdown_initiated(tmp_path, reason="user", now=_utc("2026-01-01T00:00:00"))
    assert state.phase is ShutdownPhase.GRACEFUL
    assert state.requested_at == "2026-01-01T00:00:00+00:00"
    assert state.interrupt_count == 1
    assert state.reason == "user"


def test_shutdown_second_interrupt_escalates_to_forced(tmp_path: Path) -> None:
    record_shutdown_initiated(tmp_path, now=_utc("2026-01-01T00:00:00"))
    state = record_shutdown_initiated(tmp_path, now=_utc("2026-01-01T00:00:30"))
    assert state.phase is ShutdownPhase.FORCED
    assert state.forced_at == "2026-01-01T00:00:30+00:00"
    assert state.interrupt_count == 2


def test_shutdown_complete_marks_terminal(tmp_path: Path) -> None:
    record_shutdown_initiated(tmp_path, now=_utc("2026-01-01T00:00:00"))
    state = record_shutdown_complete(tmp_path, now=_utc("2026-01-01T00:00:10"))
    assert state.phase is ShutdownPhase.COMPLETE
    assert state.completed_at == "2026-01-01T00:00:10+00:00"


def test_shutdown_tracker_reconciles_stale(tmp_path: Path) -> None:
    record_shutdown_initiated(tmp_path, now=_utc("2026-01-01T00:00:00"))
    tracker = ShutdownTracker(tmp_path)
    assert tracker.is_graceful_requested()
    assert not tracker.is_complete()

    reconciled = tracker.reconcile_stale(now=_utc("2026-01-01T00:01:00"))
    assert reconciled.phase is ShutdownPhase.COMPLETE
    assert tracker.is_complete()


def test_shutdown_state_file_is_atomic(tmp_path: Path) -> None:
    state = record_shutdown_initiated(tmp_path, now=_utc("2026-01-01T00:00:00"))
    payload = json.loads((tmp_path / "shutdown.json").read_text())
    assert payload["phase"] == ShutdownPhase.GRACEFUL.value
    assert payload["interrupt_count"] == state.interrupt_count


def test_shutdown_running_state_when_no_file(tmp_path: Path) -> None:
    tracker = ShutdownTracker(tmp_path / "missing")
    assert tracker.state().phase is ShutdownPhase.RUNNING
    assert not tracker.is_graceful_requested()


def test_shutdown_tracker_targets_one_dispatcher_generation(tmp_path: Path) -> None:
    first = ShutdownTracker(
        tmp_path, instance_id="first", pid=101, process_started_at="one", nonce="nonce-one"
    )
    first.initialize()
    assert first.record_interrupt().phase is ShutdownPhase.GRACEFUL

    second = ShutdownTracker(
        tmp_path, instance_id="second", pid=202, process_started_at="two", nonce="nonce-two"
    )
    second.initialize()
    assert second.state().phase is ShutdownPhase.RUNNING
    assert first.is_graceful_requested() is False

    requested = second.record_interrupt()
    assert requested.phase is ShutdownPhase.GRACEFUL
    assert requested.instance_id == "second"
    assert requested.pid == 202
    assert requested.process_started_at == "two"
    assert requested.nonce == "nonce-two"


def test_shutdown_tracker_second_targeted_request_forces(tmp_path: Path) -> None:
    tracker = ShutdownTracker(tmp_path, instance_id="dispatcher", pid=303, nonce="secret")
    tracker.initialize()
    assert tracker.record_interrupt().phase is ShutdownPhase.GRACEFUL
    assert tracker.record_interrupt().phase is ShutdownPhase.FORCED


def test_shutdown_tracker_repeated_interrupts_keep_forced_latched(tmp_path: Path) -> None:
    tracker = ShutdownTracker(tmp_path, instance_id="dispatcher", pid=303, nonce="secret")
    tracker.initialize()
    assert tracker.record_interrupt(now=_utc("2026-01-01T00:00:00")).phase is ShutdownPhase.GRACEFUL
    forced = tracker.record_interrupt(now=_utc("2026-01-01T00:00:01"))

    third = tracker.record_interrupt(now=_utc("2026-01-01T00:00:02"))
    fourth = tracker.record_interrupt(now=_utc("2026-01-01T00:00:03"))

    assert third.phase is ShutdownPhase.FORCED
    assert fourth.phase is ShutdownPhase.FORCED
    assert third.forced_at == forced.forced_at
    assert fourth.forced_at == forced.forced_at
    assert fourth.interrupt_count == 4


def test_shutdown_tracker_interrupt_does_not_reopen_complete_state(tmp_path: Path) -> None:
    tracker = ShutdownTracker(tmp_path, instance_id="dispatcher", pid=303, nonce="secret")
    tracker.initialize()
    tracker.record_interrupt(now=_utc("2026-01-01T00:00:00"))
    complete = tracker.mark_complete(now=_utc("2026-01-01T00:00:01"))

    repeated = tracker.record_interrupt(now=_utc("2026-01-01T00:00:02"))

    assert repeated.phase is ShutdownPhase.COMPLETE
    assert repeated.completed_at == complete.completed_at
    assert repeated.interrupt_count == complete.interrupt_count + 1


# --------------------------------------------------------------------------- #
# Status projection tests
# --------------------------------------------------------------------------- #


def test_projection_active_when_lease_fresh() -> None:
    lease = _make_lease()
    projection = project_run_status(
        run_status="running",
        lease=lease,
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.ACTIVE
    assert projection.lease_status is LeaseStatus.ACTIVE


def test_projection_stale_when_lease_expired() -> None:
    lease = _make_lease(timeout_seconds=120.0)
    projection = project_run_status(
        run_status="running",
        lease=lease,
        process_alive=True,
        now=_utc("2026-01-01T01:00:00"),
    )
    assert projection.status is CanonicalRunStatus.STALE
    assert projection.lease_status is LeaseStatus.EXPIRED


def test_projection_stale_when_process_dead_but_status_live() -> None:
    lease = _make_lease()
    projection = project_run_status(
        run_status="running",
        lease=lease,
        process_alive=False,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.STALE
    assert projection.lease_status is LeaseStatus.STALE


def test_projection_blocked_for_blocked_run_status() -> None:
    projection = project_run_status(
        run_status="blocked",
        lease=_make_lease(),
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.BLOCKED


def test_projection_retryable_for_failed_run_status() -> None:
    projection = project_run_status(
        run_status="failed",
        lease=_make_lease(),
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.RETRYABLE


def test_projection_non_retryable_hint_surfaces_needs_attention() -> None:
    projection = project_run_status(
        run_status="failed",
        lease=_make_lease(),
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
        retryable_hint=False,
        retryable_detail="git push failed: ! [rejected] (fetch first)",
    )
    assert projection.status is CanonicalRunStatus.NEEDS_ATTENTION
    # The original error is preserved in the detail for operators.
    assert "fetch first" in projection.detail


def test_projection_retryable_hint_none_preserves_legacy_behavior() -> None:
    # Pre-existing run records have no classification field (hint None); they
    # must keep today's behavior (failed -> retryable).
    projection = project_run_status(
        run_status="failed",
        lease=_make_lease(),
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
        retryable_hint=None,
    )
    assert projection.status is CanonicalRunStatus.RETRYABLE


def test_projection_retryable_hint_true_stays_retryable() -> None:
    projection = project_run_status(
        run_status="failed",
        lease=_make_lease(),
        process_alive=True,
        now=_utc("2026-01-01T00:01:00"),
        retryable_hint=True,
    )
    assert projection.status is CanonicalRunStatus.RETRYABLE


def test_projection_merged_overrides_run_status() -> None:
    projection = project_run_status(
        run_status="running",
        lease=_make_lease(),
        is_merged=True,
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.MERGED


def test_projection_passed_terminal() -> None:
    projection = project_run_status(
        run_status="passed",
        lease=None,
    )
    assert projection.status is CanonicalRunStatus.PASSED


def test_projection_needs_input() -> None:
    projection = project_run_status(
        run_status="waiting-for-input",
        lease=_make_lease(),
        now=_utc("2026-01-01T00:01:00"),
    )
    assert projection.status is CanonicalRunStatus.NEEDS_INPUT


def test_projection_includes_pending_and_failed_gates(tmp_path: Path) -> None:
    record_gate_started(
        tmp_path,
        name="pytest",
        command=["pytest"],
        cwd=str(tmp_path),
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    record_gate_completed(
        tmp_path,
        name="pytest",
        exit_status=1,
        now=_utc("2026-01-01T00:01:00"),
    )
    record_gate_started(
        tmp_path,
        name="ruff",
        command=["ruff", "check", "."],
        cwd=str(tmp_path),
        timeout_seconds=10.0,
        now=_utc("2026-01-01T00:00:30"),
    )

    store = GateRecordStore(tmp_path)
    projection = project_run_status(
        run_status="running",
        lease=_make_lease(),
        process_alive=True,
        gate_records=store.load(),
        now=_utc("2026-01-01T00:01:00"),
    )
    assert "ruff" in projection.pending_gates
    assert "pytest" in projection.failed_gates


def test_projection_warns_on_live_status_without_lease() -> None:
    projection = project_run_status(
        run_status="running",
        lease=None,
    )
    assert projection.status is CanonicalRunStatus.STALE
    assert any("lease" in warning for warning in projection.warnings)


# --------------------------------------------------------------------------- #
# Cross-helper smoke tests
# --------------------------------------------------------------------------- #


def test_lease_classification_matches_projection_active() -> None:
    lease = _make_lease()
    now = _utc("2026-01-01T00:01:00")
    assert classify_lease(lease, now=now, process_alive=True) is LeaseStatus.ACTIVE
    projection = project_run_status(
        run_status="running",
        lease=lease,
        process_alive=True,
        now=now,
    )
    assert projection.is_active


def test_lease_classification_matches_projection_stale_after_timeout() -> None:
    lease = build_lease(
        run_id="r",
        spec_id="s",
        phase="implement",
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00"),
    )
    later = _utc("2026-01-01T00:30:00")
    assert classify_lease(lease, now=later) is LeaseStatus.EXPIRED
    projection = project_run_status(
        run_status="running",
        lease=lease,
        now=later,
    )
    assert projection.is_stale


def test_old_run_lease_can_be_validated_for_adoption() -> None:
    lease = build_lease(
        run_id="r",
        spec_id="s",
        phase="implement",
        timeout_seconds=60.0,
        now=_utc("2026-01-01T00:00:00") - timedelta(hours=2),
    )
    outcome = evaluate_process_adoption(
        expected_run_id="r",
        expected_spec_id="s",
        lease=lease,
        recorded_pid=99,
        process_alive=True,
        now=_utc("2026-01-01T00:00:00"),
    )
    assert outcome.decision is AdoptionDecision.LEASE_EXPIRED


def test_fetch_timeout_kills_whole_process_group(tmp_path):
    """A hung transport child must die with the fetch, not become orphaned."""
    import time as _time

    from spec_runtime.control_plane.git_timeouts import _run_fetch_process_group
    from spec_runtime.process_supervisor import inspect_process

    pidfile = tmp_path / "child.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
        "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(30)"
    )
    cmd = [sys.executable, "-c", parent_code, str(pidfile), child_code]
    started = _time.monotonic()
    try:
        _run_fetch_process_group(cmd, timeout=0.5)
        raise AssertionError("expected TimeoutExpired")
    except subprocess.TimeoutExpired:
        pass
    assert _time.monotonic() - started < 5.0
    child_pid = int(pidfile.read_text().strip())
    deadline = _time.monotonic() + 2.0
    while _time.monotonic() < deadline:
        if inspect_process(child_pid) is None:
            break
        _time.sleep(0.05)
    assert inspect_process(child_pid) is None


def test_consumed_operator_request_projects_retryable_not_needs_input(tmp_path):
    """Waiting-for-input with an already-consumed operator request
    has nothing left to answer — project retryable so resume paths engage."""
    import json

    from spec_runtime.control_plane import CanonicalRunStatus
    from spec_runtime.spec_status import project_run_record_status

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "wedged-run-9"
    run_dir.mkdir(parents=True)
    (run_dir / "operator-request.json").write_text(json.dumps({"status": "consumed"}))
    record = {"run_id": "wedged-run-9", "status": "waiting-for-input"}

    projection = project_run_record_status(runs_dir, record)
    assert projection is not None
    assert projection.status is CanonicalRunStatus.RETRYABLE
    assert "consumed" in projection.detail


def test_pending_operator_request_still_projects_needs_input(tmp_path):
    import json

    from spec_runtime.control_plane import CanonicalRunStatus
    from spec_runtime.spec_status import project_run_record_status

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "asking-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "operator-request.json").write_text(json.dumps({"status": "pending"}))
    record = {"run_id": "asking-run-1", "status": "waiting-for-input"}

    projection = project_run_record_status(runs_dir, record)
    assert projection is not None
    assert projection.status is CanonicalRunStatus.NEEDS_INPUT
