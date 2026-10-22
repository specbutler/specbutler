"""Autopilot infrastructure backoff and circuit breaker.

Regression coverage for relaunch storms: a spec whose environment is broken
(container image build, in-worker service startup, missing toolchain) used to
be relaunched every poll interval, thousands of times a day.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

import pytest

from spec_runtime import autopilot
from spec_runtime import orchestrator as orch
from spec_runtime.infra_breaker import (
    BLOCKED_ON_INFRASTRUCTURE,
    InfrastructureCircuitBreaker,
    backoff_seconds,
    open_breakers_for_spec,
    read_infra_breaker_entries,
)

IMAGE_BUILD_ERROR = (
    "Container backend image build failed for spec-worker:a9db77abb0330705662dcbf8. "
    "See /repo/.spec-workspaces/{run_id}/logs/image-build.log"
)


def _image_build(run_id: str) -> orch.FailureClassification:
    return orch.classify_failure(IMAGE_BUILD_ERROR.format(run_id=run_id))


def _breaker(tmp_path: Path | None = None, **overrides) -> InfrastructureCircuitBreaker:
    options = {
        "backend": "container",
        "threshold": 3,
        "base_backoff": 60.0,
        "max_backoff": 1800.0,
        "cooldown": 1800.0,
        "rand": lambda: 1.0,
    }
    options.update(overrides)
    if tmp_path is not None:
        options["state_path"] = tmp_path / "infra-breaker.json"
    return InfrastructureCircuitBreaker(**options)


def _record(breaker, spec_id: str, classification, *, now: float, error: str = "boom"):
    return breaker.record_failure(
        spec_id,
        signature=classification.signature,
        kind=classification.kind,
        scope=classification.scope,
        error=error,
        now=now,
    )


def test_backoff_is_exponential_with_bounded_jitter():
    assert backoff_seconds(1, base=60, maximum=1800, rand=lambda: 0.0) == 30.0
    assert backoff_seconds(1, base=60, maximum=1800, rand=lambda: 1.0) == 60.0
    assert backoff_seconds(3, base=60, maximum=1800, rand=lambda: 1.0) == 240.0
    assert backoff_seconds(50, base=60, maximum=1800, rand=lambda: 1.0) == 1800.0
    for streak in range(1, 8):
        window = min(60 * 2 ** (streak - 1), 1800)
        assert window / 2 <= backoff_seconds(streak, base=60, maximum=1800) <= window


def test_spec_backs_off_after_infrastructure_failure():
    breaker = _breaker()
    outcome = _record(breaker, "alpha", _image_build("alpha-20260630T004645226547"), now=1000.0)

    assert outcome.backoff_seconds == 60.0
    assert not outcome.tripped
    assert breaker.check_dispatch("alpha", now=1059.0).reason == "backoff"
    assert breaker.check_dispatch("alpha", now=1061.0).allowed
    # Another spec is unaffected until the breaker opens.
    assert breaker.check_dispatch("beta", now=1001.0).allowed


def test_relaunches_with_new_run_ids_trip_the_backend_breaker(tmp_path: Path):
    breaker = _breaker(tmp_path)
    counts, tripped = [], []
    for i, spec in enumerate(["alpha", "alpha", "beta"]):
        outcome = _record(breaker, spec, _image_build(f"{spec}-2026063{i}T004645226547"), now=1000.0 + i)
        counts.append(outcome.entry.count)
        tripped.append(outcome.tripped)
    outcomes = [outcome]

    assert counts == [1, 2, 3]
    assert tripped == [False, False, True]
    entry = outcomes[-1].entry
    assert entry.is_open
    assert entry.scope == "backend"
    # A backend-scoped breaker holds every spec, including ones that never ran.
    for spec in ("alpha", "beta", "gamma"):
        decision = breaker.check_dispatch(spec, now=1100.0)
        assert not decision.allowed
        assert decision.reason == "blocked"

    persisted = read_infra_breaker_entries(tmp_path / "infra-breaker.json")
    assert [item.signature for item in persisted] == [entry.signature]
    assert open_breakers_for_spec(tmp_path / "infra-breaker.json", "gamma")[0].describe().startswith(
        BLOCKED_ON_INFRASTRUCTURE
    )


def test_open_breaker_allows_one_probe_per_cooldown_and_closes_on_progress(tmp_path: Path):
    breaker = _breaker(tmp_path, threshold=1)
    classification = _image_build("alpha-20260630T004645226547")
    _record(breaker, "alpha", classification, now=0.0)

    assert not breaker.check_dispatch("alpha", now=1799.0).allowed
    probe = breaker.check_dispatch("beta", now=1800.0)
    assert probe.allowed and probe.reason == "probe"
    # Everyone else stays held while the probe runs.
    assert not breaker.check_dispatch("alpha", now=1801.0).allowed

    # The probe failed the same way: another full cooldown.
    _record(breaker, "beta", classification, now=1900.0)
    assert not breaker.check_dispatch("gamma", now=3699.0).allowed

    closed = breaker.record_progress("beta")
    assert [entry.signature for entry in closed] == [classification.signature]
    assert breaker.check_dispatch("alpha", now=3700.0).allowed
    assert read_infra_breaker_entries(tmp_path / "infra-breaker.json") == []


def test_spec_scoped_breaker_only_holds_the_failing_spec():
    breaker = _breaker(threshold=2)
    push = orch.classify_failure("git push failed: error: RPC failed; HTTP 500 curl 22")
    assert push.scope == "spec"
    _record(breaker, "alpha", push, now=0.0)
    outcome = _record(breaker, "alpha", push, now=100.0)

    assert outcome.tripped
    assert breaker.check_dispatch("alpha", now=200.0).reason == "blocked"
    assert breaker.check_dispatch("beta", now=200.0).allowed


def test_breaker_state_survives_restart(tmp_path: Path):
    breaker = _breaker(tmp_path, threshold=1)
    _record(breaker, "alpha", _image_build("alpha-20260630T004645226547"), now=0.0)

    restarted = _breaker(tmp_path, threshold=1)

    assert not restarted.check_dispatch("alpha", now=10.0).allowed
    restarted.reset()
    assert restarted.check_dispatch("alpha", now=10.0).allowed
    assert read_infra_breaker_entries(tmp_path / "infra-breaker.json") == []


def test_dispatch_breaker_fingerprint_ignores_run_ids():
    """The pre-existing same-error breaker never tripped for fresh runs."""
    breaker = autopilot.DispatchCircuitBreaker(threshold=3, base_backoff=0.0)
    for i in range(3):
        breaker.record_failure(
            "alpha",
            "bootstrap",
            IMAGE_BUILD_ERROR.format(run_id=f"alpha-2026063{i}T00464522654{i}"),
            now=float(i),
        )
    assert breaker.is_tripped("alpha")


# ---------------------------------------------------------------------------
# run_loop: a repeating infrastructure failure stops relaunching
# ---------------------------------------------------------------------------


class _LoopDone(Exception):
    pass


class _FakeTime:
    """Stands in for ``autopilot.time`` so the dispatch loop runs on a fake clock."""

    def __init__(self, *, horizon: float) -> None:
        self.now = 1_000_000.0
        self._end = self.now + horizon

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.001)
        if self.now >= self._end:
            raise _LoopDone()


def _write_config(path: Path, body: str = "") -> None:
    (path / ".git").mkdir(exist_ok=True)
    (path / ".spec.toml").write_text(textwrap.dedent(body))


def _run_storm(tmp_path: Path, monkeypatch, *, errors, hours: float, specs=("alpha",)):
    """Drive run_loop against specs whose every launch fails with ``errors(n)``."""
    _write_config(
        tmp_path,
        """
        [autopilot]
        infra_backoff_base_seconds = 60
        infra_backoff_max_seconds = 1800
        infra_breaker_threshold = 3
        infra_breaker_cooldown_seconds = 1800
        """,
    )
    clock = _FakeTime(horizon=hours * 3600)
    launches: list[tuple[float, str]] = []
    notifications: list[str] = []
    records: dict[str, dict] = {}

    def start_candidate(_repo_root, candidate, *, preallocated_run_id="", **_kwargs):
        index = len(launches)
        launches.append((clock.now, candidate.spec_id))
        run_id = f"{candidate.spec_id}-20260630T{index:012d}"
        records[candidate.spec_id] = {
            "run_id": run_id,
            "spec_id": candidate.spec_id,
            "status": "failed",
            "phase": "bootstrap",
            "last_error": errors(index, run_id),
        }
        return autopilot.ActiveRunProcess(
            spec_id=candidate.spec_id,
            agent=candidate.agent,
            pid=424242,
            started_at=autopilot.now_iso(),
            started_monotonic=clock.monotonic(),
            log_path="",
            run_id=run_id,
        )

    def queue(*_args, **_kwargs):
        return [
            autopilot.DispatchCandidate(
                spec_id=spec,
                agent="codex",
                area="backend",
                priority=1,
                unlock_count=0,
                status="not-started",
                backend="container",
            )
            for spec in specs
        ]

    class _QuietWatch:
        def check(self) -> str:
            return ""

    monkeypatch.setattr(autopilot, "time", clock)
    monkeypatch.setattr(autopilot, "resolve_repo_root", lambda _repo_root: tmp_path)
    monkeypatch.setattr(autopilot, "validate_autopilot_backend", lambda *_a: "")
    monkeypatch.setattr(autopilot, "current_process_identity", lambda: autopilot.ProcessIdentity(1, "t", "spec"))
    monkeypatch.setattr(autopilot, "ensure_pid_file", lambda *_a, **_k: None)
    monkeypatch.setattr(autopilot, "remove_pid_file", lambda _repo_root: None)
    monkeypatch.setattr(autopilot, "adopt_active_processes", lambda _repo_root: {})
    monkeypatch.setattr(autopilot, "cleanup_unadopted_container_runs", lambda *_a: None)
    monkeypatch.setattr(autopilot, "write_active_state", lambda *_a: None)
    monkeypatch.setattr(autopilot, "refresh_runtime_git_refs", lambda _repo_root: (True, ""))
    monkeypatch.setattr(autopilot, "collect_git_spec_state", lambda _repo_root: object())
    monkeypatch.setattr(autopilot, "load_run_record_index", lambda _repo_root: autopilot.RunRecordIndex())
    monkeypatch.setattr(
        autopilot, "fetch_coordinator_lease_snapshot", lambda _repo_root: autopilot.CoordinatorLeaseSnapshot()
    )
    monkeypatch.setattr(autopilot, "build_dispatch_queue", queue)
    monkeypatch.setattr(autopilot, "apply_container_capacity_gate", lambda items, _result: (items, []))
    monkeypatch.setattr(autopilot.ContainerCapacityPreflight, "evaluate", lambda self, _items: None)
    monkeypatch.setattr(autopilot, "SourceStalenessWatch", _QuietWatch)
    monkeypatch.setattr(autopilot, "available_memory_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(autopilot, "acquire_candidate_lease", lambda *_a: ("", None))
    monkeypatch.setattr(autopilot, "durable_metadata_path", lambda sid: tmp_path / f"{sid}.ready")
    monkeypatch.setattr(autopilot, "start_candidate", start_candidate)
    monkeypatch.setattr(autopilot, "is_pid_alive", lambda *_a: False)
    monkeypatch.setattr(autopilot, "read_latest_run_phase", lambda *_a, **_k: "bootstrap")
    monkeypatch.setattr(
        autopilot,
        "read_latest_run_record",
        lambda _repo_root, spec_id, run_id="", **_k: records.get(spec_id, {}),
    )
    monkeypatch.setattr(autopilot, "read_log_tail", lambda *_a, **_k: ["Workflow failed"])
    monkeypatch.setattr(autopilot, "maybe_write_run_log_alias", lambda *_a: None)
    monkeypatch.setattr(autopilot, "append_log", lambda *_a: None)
    monkeypatch.setattr(autopilot, "notify", lambda _backends, *, title, message: notifications.append(message))
    monkeypatch.setattr(autopilot.signal, "signal", lambda *_a, **_k: None)

    args = argparse.Namespace(
        repo_root=str(tmp_path),
        concurrency=4,
        poll_interval=5,
        notify=[],
        notify_backends=["test"],
        notify_success=False,
        dry_run=False,
        agent="",
        reset_infra_breaker=False,
    )
    with pytest.raises(_LoopDone):
        autopilot.run_loop(args)
    return launches, notifications


def test_repeating_infrastructure_failure_stops_relaunching(tmp_path: Path, monkeypatch, capsys):
    launches, notifications = _run_storm(
        tmp_path,
        monkeypatch,
        errors=lambda _index, run_id: IMAGE_BUILD_ERROR.format(run_id=run_id),
        hours=17,
    )

    times = [when for when, _spec in launches]
    # Unbounded, a 5s poll relaunches ~12,000 times in 17h. With the breaker:
    # three backed-off launches, then one probe per 30-minute cooldown.
    assert 3 <= len(launches) <= 3 + 17 * 2 + 1
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert gaps[0] >= 30.0  # first backoff window is [30s, 60s]
    assert gaps[1] >= 60.0  # second is [60s, 120s]
    assert all(gap >= 1800.0 for gap in gaps[2:])  # probes after the breaker opened

    entries = read_infra_breaker_entries(autopilot.autopilot_infra_breaker_path(tmp_path))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.is_open
    assert entry.kind == "container_image_build"
    assert entry.count == len(launches)
    # One failure notification, one breaker notification; probes stay quiet.
    assert len(notifications) == 2
    assert "Infrastructure circuit breaker open" in notifications[1]
    out = capsys.readouterr().out
    assert BLOCKED_ON_INFRASTRUCTURE in out


def test_code_failures_are_not_held_by_the_infrastructure_breaker(tmp_path: Path, monkeypatch):
    launches, _notifications = _run_storm(
        tmp_path,
        monkeypatch,
        # Distinct failing tests each time (digits alone are normalized away).
        errors=lambda index, _run_id: (
            f"Gate 'test' failed (exit 1). output: tests/test_{'abcdefghijklmnopqrstuvwxyz'[index % 26]}.py failed"
        ),
        hours=1,
    )

    # Distinct code failures keep relaunching on the existing per-spec
    # same-error breaker schedule; nothing is blocked on infrastructure.
    assert len(launches) > 10
    assert read_infra_breaker_entries(autopilot.autopilot_infra_breaker_path(tmp_path)) == []


def test_breaker_open_is_visible_to_watch_screen(tmp_path: Path):
    breaker = _breaker(threshold=1, state_path=tmp_path / "state.json")
    _record(breaker, "alpha", _image_build("alpha-20260630T004645226547"), now=0.0, error="image build failed")

    screen = autopilot._render_watch_screen([], [], infrastructure_breakers=breaker.entries)

    assert BLOCKED_ON_INFRASTRUCTURE in screen
    assert "container_image_build" in screen
    payload = json.loads((tmp_path / "state.json").read_text())
    assert payload["entries"][0]["status"] == BLOCKED_ON_INFRASTRUCTURE
