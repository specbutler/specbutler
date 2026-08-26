from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from spec_runtime import autopilot
from spec_runtime.config import load_spec_runtime_config
from spec_runtime.execution_backend import WorkspaceHandle


def _write_config(path: Path, body: str) -> Path:
    config_path = path / ".spec.toml"
    config_path.write_text(textwrap.dedent(body))
    return config_path


def test_autopilot_backend_policy_defaults_to_worktree_before_rollout(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(tmp_path, ""),
    )

    policy = autopilot.resolve_autopilot_backend_policy(config)

    assert policy.backend == "worktree"
    assert policy.source == "legacy-default"
    assert policy.safety_mode == "safe"


def test_autopilot_backend_policy_defaults_to_container_after_rollout_gate(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true
            """,
        ),
    )

    policy = autopilot.resolve_autopilot_backend_policy(config)

    assert policy.backend == "container"
    assert policy.source == "rollout-policy"
    assert policy.safety_mode == "safe"


def test_autopilot_backend_policy_honors_explicit_worktree_escape_hatch(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true

            [execution]
            backend = "worktree"
            """,
        ),
    )

    policy = autopilot.resolve_autopilot_backend_policy(config)

    assert policy.backend == "worktree"
    assert policy.source == "repo-config"


def test_autopilot_actor_env_flips_implicit_backend_only(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [autopilot]
        container_default_enabled = true
        """,
    )

    manual = load_spec_runtime_config(require=True, config_path=config_path, env={})
    unattended = load_spec_runtime_config(
        require=True,
        config_path=config_path,
        env={"SPEC_ACTOR": "autopilot"},
    )

    assert manual.execution.backend == "worktree"
    assert unattended.execution.backend == "container"
    assert unattended.execution.safety_mode == "safe"


def test_autopilot_actor_env_preserves_explicit_clone_backend(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true

            [execution]
            backend = "clone"
            """,
        ),
        env={"SPEC_ACTOR": "autopilot"},
    )

    assert config.execution.backend == "clone"
    assert config.execution.backend_explicit is True


def test_computed_concurrency_uses_conservative_container_cap(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true
            container_memory_mb = 3072
            """,
        ),
    )

    policy = autopilot.compute_autopilot_concurrency(
        config,
        explicit=None,
        host_memory_bytes=64 * 1024 * 1024 * 1024,
        host_cpus=32,
    )

    assert policy.cap == 2
    assert policy.source == "computed"
    assert policy.backend == "container"


def test_explicit_concurrency_is_honored_as_operator_set(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true
            """,
        ),
    )

    policy = autopilot.compute_autopilot_concurrency(
        config,
        explicit=9,
        host_memory_bytes=1024 * 1024 * 1024,
        host_cpus=1,
    )

    assert policy.cap == 9
    assert policy.source == "operator-set"


def test_explicit_zero_concurrency_is_not_clamped(tmp_path: Path) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(tmp_path, ""),
    )

    policy = autopilot.compute_autopilot_concurrency(
        config,
        explicit=0,
        host_memory_bytes=1024 * 1024 * 1024,
        host_cpus=1,
    )

    assert policy.cap == 0
    assert policy.source == "operator-set"


def test_missing_container_engine_diagnostic_is_actionable(tmp_path: Path, monkeypatch) -> None:
    config = load_spec_runtime_config(
        require=True,
        config_path=_write_config(
            tmp_path,
            """
            [autopilot]
            container_default_enabled = true

            [execution.container]
            engine = "definitely-not-a-container-engine"
            """,
        ),
    )
    monkeypatch.setattr(autopilot.shutil, "which", lambda _engine: None)

    message = autopilot.validate_autopilot_backend(
        autopilot.resolve_autopilot_backend_policy(config),
        config,
    )

    assert "Docker-compatible CLI" in message
    assert "definitely-not-a-container-engine" in message
    assert "Install Docker Desktop" in message


def test_run_loop_surfaces_effective_concurrency_in_dry_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_config(tmp_path, "")
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        concurrency=None,
        poll_interval=1,
        notify=[],
        notify_backends=[],
        notify_success=False,
        dry_run=True,
        agent="",
    )

    monkeypatch.setattr(autopilot, "resolve_repo_root", lambda _repo_root: tmp_path)
    monkeypatch.setattr(autopilot, "ensure_pid_file", lambda _repo_root: None)
    monkeypatch.setattr(autopilot, "remove_pid_file", lambda _repo_root: None)
    monkeypatch.setattr(autopilot, "adopt_active_processes", lambda _repo_root: {})
    monkeypatch.setattr(autopilot, "refresh_runtime_git_refs", lambda _repo_root: (True, ""))
    monkeypatch.setattr(autopilot, "collect_git_spec_state", lambda _repo_root: object())
    monkeypatch.setattr(autopilot, "load_run_record_index", lambda _repo_root: autopilot.RunRecordIndex())
    monkeypatch.setattr(autopilot, "fetch_coordinator_lease_snapshot", lambda _repo_root: autopilot.CoordinatorLeaseSnapshot())
    monkeypatch.setattr(
        autopilot,
        "build_dispatch_queue",
        lambda _repo_root, agent_override="", git_state=None, run_index=None, coordinator_snapshot=None: [],
    )
    monkeypatch.setattr(autopilot.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(autopilot, "available_memory_bytes", lambda: 16 * 1024 * 1024 * 1024)

    assert autopilot.run_loop(args) == 0

    captured = capsys.readouterr()
    assert "backend=worktree" in captured.out
    assert "concurrency=8" in captured.out
    assert "concurrency_source=computed" in captured.out


def test_run_loop_cleans_stale_container_artifacts_on_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_config(tmp_path, "")
    run_id = "container-spec-20260428T120000"
    active_path = autopilot.autopilot_active_path(tmp_path)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(
        json.dumps(
            {
                "container-spec": {
                    "pid": 999999,
                    "agent": "codex",
                    "started_at": "2026-04-28T12:00:00+00:00",
                    "phase": "implement",
                    "run_id": run_id,
                    "log_path": str(tmp_path / "stale.log"),
                    "process_started_at": "Tue Apr 28 12:00:00 2026",
                }
            }
        )
        + "\n"
    )
    runs_dir = autopilot.runs_dir(tmp_path)
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": "container-spec",
                "branch": "code/container-spec--20260428T120000",
                "backend": "container",
                "safety_mode": "safe",
            }
        )
        + "\n"
    )
    cleanup_calls: list[WorkspaceHandle] = []

    class CleanupBackend:
        def cleanup(self, workspace: WorkspaceHandle) -> None:
            cleanup_calls.append(workspace)

    args = argparse.Namespace(
        repo_root=str(tmp_path),
        concurrency=None,
        poll_interval=1,
        notify=[],
        notify_backends=[],
        notify_success=False,
        dry_run=True,
        agent="",
    )

    monkeypatch.setattr(autopilot, "resolve_repo_root", lambda _repo_root: tmp_path)
    monkeypatch.setattr(autopilot, "ensure_pid_file", lambda _repo_root: None)
    monkeypatch.setattr(autopilot, "remove_pid_file", lambda _repo_root: None)
    monkeypatch.setattr(autopilot, "adopt_active_processes", lambda _repo_root: {})
    monkeypatch.setattr(autopilot, "get_execution_backend", lambda _execution: CleanupBackend())
    monkeypatch.setattr(autopilot, "refresh_runtime_git_refs", lambda _repo_root: (True, ""))
    monkeypatch.setattr(autopilot, "collect_git_spec_state", lambda _repo_root: object())
    monkeypatch.setattr(autopilot, "load_run_record_index", lambda _repo_root: autopilot.RunRecordIndex())
    monkeypatch.setattr(autopilot, "fetch_coordinator_lease_snapshot", lambda _repo_root: autopilot.CoordinatorLeaseSnapshot())
    monkeypatch.setattr(
        autopilot,
        "build_dispatch_queue",
        lambda _repo_root, agent_override="", git_state=None, run_index=None, coordinator_snapshot=None: [],
    )
    monkeypatch.setattr(autopilot.signal, "signal", lambda *_args, **_kwargs: None)

    assert autopilot.run_loop(args) == 0

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].backend == "container"
    assert cleanup_calls[0].path == tmp_path / ".spec-workspaces" / run_id / "source"


def test_watch_screen_surfaces_backend_and_safety_for_runs_and_queue() -> None:
    rendered = autopilot._render_watch_screen(
        [
            {
                "spec_id": "active-spec",
                "agent": "codex",
                "phase": "implement",
                "retries": "1/3",
                "owner": "autopilot",
                "backend": "container",
                "safety_mode": "safe",
                "backend_source": "rollout-policy",
                "lease": "",
                "lease_heartbeat": "",
                "lease_expires": "",
                "elapsed": "1m",
                "tag": "",
                "status": "running",
            }
        ],
        [
            autopilot.DispatchCandidate(
                spec_id="queued-spec",
                agent="codex",
                area="orchestrator",
                priority=80,
                unlock_count=0,
                status="ready",
                backend="container",
                safety_mode="safe",
                backend_source="rollout-policy",
                reason="new",
            )
        ],
    )

    assert "BACKEND" in rendered
    assert "SAFETY" in rendered
    assert "active-spec" in rendered
    assert "queued-spec" in rendered
    assert "container" in rendered
    assert "safe" in rendered
    assert "backend_source=rollout-policy" in rendered


def test_dogfood_performance_budgets_pass_when_within_limits() -> None:
    results = autopilot.evaluate_dogfood_performance_budgets(
        autopilot.DogfoodPerformanceSample(
            platform="linux",
            cold_start_cached_seconds=14.5,
            snapshot_restore_seconds=4.5,
            retry_cycle_seconds=90.0,
            worktree_retry_cycle_seconds=60.0,
            cold_full_image_build_seconds=299.0,
        )
    )

    assert {result.name for result in results} == {
        "cold-start-cached-image",
        "snapshot-restore",
        "post-snapshot-retry-cycle",
        "cold-full-image-build",
    }
    assert all(result.passed for result in results)


def test_dogfood_performance_budgets_fail_when_limits_are_exceeded() -> None:
    results = autopilot.evaluate_dogfood_performance_budgets(
        autopilot.DogfoodPerformanceSample(
            platform="macos",
            cold_start_cached_seconds=31.0,
            snapshot_restore_seconds=11.0,
            retry_cycle_seconds=91.0,
            worktree_retry_cycle_seconds=60.0,
            cold_full_image_build_seconds=301.0,
        )
    )

    failed = {result.name: result for result in results if not result.passed}
    assert set(failed) == {
        "cold-start-cached-image",
        "snapshot-restore",
        "post-snapshot-retry-cycle",
        "cold-full-image-build",
    }
    assert failed["cold-start-cached-image"].limit_seconds == 30.0
    assert failed["snapshot-restore"].limit_seconds == 10.0
    assert failed["post-snapshot-retry-cycle"].limit_seconds == 90.0


# ---------------------------------------------------------------------------
# Same-error dispatch circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_trips_after_threshold_identical_failures() -> None:
    breaker = autopilot.DispatchCircuitBreaker(threshold=3, base_backoff=10.0)
    now = 1000.0
    # Volatile tokens (SHAs, counters) differ each cycle but normalize to the
    # same fingerprint, so these count as identical failures.
    err = "git push failed: ! [rejected] abc1234 main -> main (fetch first)"

    assert breaker.should_dispatch("spec-a", now) is True
    assert breaker.record_failure("spec-a", "publish", err, now=now) == 1
    # Within the backoff window dispatch is suppressed; after it, allowed again.
    assert breaker.should_dispatch("spec-a", now) is False
    assert breaker.should_dispatch("spec-a", now + 10.0) is True

    err2 = "git push failed: ! [rejected] deadbeef main -> main (fetch first)"
    assert breaker.record_failure("spec-a", "publish", err2, now=now + 10.0) == 2
    assert breaker.record_failure("spec-a", "publish", err, now=now + 40.0) == 3

    assert breaker.is_tripped("spec-a") is True
    # Once tripped, dispatch stays suppressed no matter how much time passes.
    assert breaker.should_dispatch("spec-a", now + 1_000_000.0) is False
    assert "fetch first" in breaker.failure_detail("spec-a")


def test_circuit_breaker_resets_on_different_fingerprint() -> None:
    breaker = autopilot.DispatchCircuitBreaker(threshold=2, base_backoff=5.0)
    breaker.record_failure("spec-a", "verify", "failure kind A", now=0.0)
    breaker.record_failure("spec-a", "verify", "failure kind A", now=0.0)
    assert breaker.is_tripped("spec-a") is True

    # A genuinely different error resets the consecutive-failure counter.
    assert breaker.record_failure("spec-a", "verify", "an entirely different failure", now=0.0) == 1
    assert breaker.is_tripped("spec-a") is False


def test_circuit_breaker_resets_on_success() -> None:
    breaker = autopilot.DispatchCircuitBreaker(threshold=2, base_backoff=5.0)
    breaker.record_failure("spec-a", "verify", "boom", now=0.0)
    breaker.record_failure("spec-a", "verify", "boom", now=0.0)
    assert breaker.is_tripped("spec-a") is True

    breaker.record_success("spec-a")
    assert breaker.is_tripped("spec-a") is False
    assert breaker.should_dispatch("spec-a", 0.0) is True


def test_circuit_breaker_backoff_is_exponential() -> None:
    breaker = autopilot.DispatchCircuitBreaker(threshold=99, base_backoff=10.0, max_backoff=1000.0)
    breaker.record_failure("s", "p", "e", now=0.0)
    assert breaker.backoff_remaining("s", 0.0) == 10.0  # 10 * 2**0
    breaker.record_failure("s", "p", "e", now=0.0)
    assert breaker.backoff_remaining("s", 0.0) == 20.0  # 10 * 2**1
    breaker.record_failure("s", "p", "e", now=0.0)
    assert breaker.backoff_remaining("s", 0.0) == 40.0  # 10 * 2**2


def test_circuit_breaker_backoff_capped_at_max() -> None:
    breaker = autopilot.DispatchCircuitBreaker(threshold=99, base_backoff=10.0, max_backoff=25.0)
    for _ in range(6):
        breaker.record_failure("s", "p", "e", now=0.0)
    assert breaker.backoff_remaining("s", 0.0) == 25.0

    # A spec with no recorded failures is always dispatchable.
    assert breaker.should_dispatch("fresh-spec", 0.0) is True
    assert breaker.failure_count("fresh-spec") == 0


def test_circuit_breaker_backoff_survives_large_failure_counts() -> None:
    # A very large consecutive-failure count must not raise (the naive
    # ``base * 2 ** (count - 1)`` overflows float when count is large, which
    # would crash the dispatch loop). The exponent is capped and the backoff
    # stays clamped to ``max_backoff``.
    breaker = autopilot.DispatchCircuitBreaker(threshold=99999, base_backoff=10.0, max_backoff=1000.0)
    for _ in range(5000):
        breaker.record_failure("s", "p", "e", now=0.0)
    assert breaker.backoff_remaining("s", 0.0) == 1000.0
    assert breaker.failure_count("s") == 5000


def test_available_memory_prefers_meminfo_memavailable(tmp_path, monkeypatch):
    """SC_AVPHYS_PAGES excludes reclaimable page cache and throttled a 60GB
    host (53GB available) to concurrency 1; MemAvailable is authoritative."""
    from spec_runtime import autopilot

    monkeypatch.setattr(autopilot, "_meminfo_available_bytes", lambda: 54244352 * 1024)
    assert autopilot.available_memory_bytes() == 54244352 * 1024


def test_meminfo_parser_reads_memavailable(tmp_path, monkeypatch):
    import builtins
    import io

    from spec_runtime import autopilot

    meminfo = "MemTotal:       61454336 kB\nMemFree:         3641344 kB\nMemAvailable:   54244352 kB\n"
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if path == "/proc/meminfo":
            return io.StringIO(meminfo)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert autopilot._meminfo_available_bytes() == 54244352 * 1024


class TestSourceStalenessWatch:
    """A long-lived autopilot keeps running the code it imported at launch."""

    @staticmethod
    def _watch(fingerprints, clock_values):
        from spec_runtime import autopilot

        pending = list(fingerprints)
        ticks = list(clock_values)

        def fingerprinter():
            return pending.pop(0) if pending else "same"

        def clock():
            return ticks.pop(0) if ticks else 10_000.0

        return autopilot.SourceStalenessWatch(
            interval_seconds=300,
            fingerprinter=fingerprinter,
            clock=clock,
        )

    def test_quiet_while_sources_are_unchanged(self):
        watch = self._watch(["abc", "abc"], [0.0, 400.0])
        assert watch.check() == ""

    def test_quiet_before_the_interval_elapses(self):
        # Would report a change, but it is not time to look yet.
        watch = self._watch(["abc", "def"], [0.0, 10.0])
        assert watch.check() == ""

    def test_warns_when_sources_change_after_launch(self):
        watch = self._watch(["abc", "def"], [0.0, 400.0])

        warning = watch.check()

        assert "Restart `spec auto run`" in warning

    def test_warns_only_once_per_change(self):
        watch = self._watch(["abc", "def", "def"], [0.0, 400.0, 800.0])
        assert watch.check() != ""
        assert watch.check() == ""


def test_source_fingerprint_changes_with_the_sources(tmp_path):
    from spec_runtime import autopilot

    (tmp_path / "mod.py").write_text("x = 1\n")
    before = autopilot.source_fingerprint(tmp_path)
    assert before == autopilot.source_fingerprint(tmp_path)

    (tmp_path / "other.py").write_text("y = 2\n")
    assert autopilot.source_fingerprint(tmp_path) != before

    (tmp_path / "notes.txt").write_text("not a source file\n")
    assert autopilot.source_fingerprint(tmp_path) == autopilot.source_fingerprint(tmp_path)


def test_source_fingerprint_defaults_to_the_installed_package(tmp_path):
    from spec_runtime import autopilot

    package_root = Path(autopilot.__file__).resolve().parent

    assert autopilot.source_fingerprint() == autopilot.source_fingerprint(package_root)
