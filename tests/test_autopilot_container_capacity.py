from __future__ import annotations

import json
import subprocess
from pathlib import Path

from spec_runtime import autopilot
from spec_runtime.config import ExecutionConfig, load_spec_runtime_config
from spec_runtime.execution_backend import ContainerCapacityResult, inspect_container_capacity


class FakeRunner:
    def __init__(self, endpoint_counts: list[int]) -> None:
        self.endpoint_counts = endpoint_counts
        self.calls = 0

    def run(self, argv, *, cwd, timeout):  # noqa: ANN001
        count = self.endpoint_counts[min(self.calls, len(self.endpoint_counts) - 1)]
        self.calls += 1
        payload = [{"Containers": {str(index): {} for index in range(count)}}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def candidate(spec_id: str, backend: str) -> autopilot.DispatchCandidate:
    return autopilot.DispatchCandidate(spec_id, "codex", "backend", 1, 0, "ready", backend=backend)


def test_saturated_bridge_pauses_only_container_candidates(tmp_path: Path) -> None:
    result = inspect_container_capacity(
        ExecutionConfig(backend="container"),
        threshold=3,
        cwd=tmp_path,
        runner=FakeRunner([3]),
    )
    container = candidate("container-spec", "container")
    worktree = candidate("worktree-spec", "worktree")

    launchable, paused = autopilot.apply_container_capacity_gate([container, worktree], result)

    assert launchable == [worktree]
    assert paused == [container]
    assert "3 endpoints" in result.warning
    assert result.warning in autopilot.render_queue([container, worktree], capacity_warning=result.warning)


def test_healthy_bridge_does_not_change_dispatch(tmp_path: Path) -> None:
    runner = FakeRunner([2])
    result = inspect_container_capacity(
        ExecutionConfig(backend="container"), threshold=3, cwd=tmp_path, runner=runner
    )
    candidates = [candidate("one", "container"), candidate("two", "container")]

    launchable, paused = autopilot.apply_container_capacity_gate(candidates, result)

    assert launchable == candidates
    assert paused == []
    assert runner.calls == 1


def test_preflight_is_cached_per_interval_and_recovers() -> None:
    now = [10.0]
    results = iter(
        [
            ContainerCapacityResult(False, 950, 950, "saturated"),
            ContainerCapacityResult(True, 20, 950),
        ]
    )
    calls = 0

    def check() -> ContainerCapacityResult:
        nonlocal calls
        calls += 1
        return next(results)

    preflight = autopilot.ContainerCapacityPreflight(
        recheck_seconds=30, checker=check, clock=lambda: now[0]
    )
    candidates = [candidate("one", "container"), candidate("two", "container")]

    first = preflight.evaluate(candidates)
    assert preflight.evaluate(candidates) is first
    assert calls == 1
    assert autopilot.apply_container_capacity_gate(candidates, first)[0] == []

    now[0] += 30
    recovered = preflight.evaluate(candidates)
    assert calls == 2
    assert recovered is not None and recovered.available
    assert autopilot.apply_container_capacity_gate(candidates, recovered)[0] == candidates


def test_non_container_queue_does_not_run_preflight() -> None:
    calls = 0

    def check() -> ContainerCapacityResult:
        nonlocal calls
        calls += 1
        return ContainerCapacityResult(False)

    preflight = autopilot.ContainerCapacityPreflight(recheck_seconds=30, checker=check)

    assert preflight.evaluate([candidate("one", "clone")]) is None
    assert calls == 0


def test_capacity_threshold_and_interval_are_configurable(tmp_path: Path) -> None:
    config_path = tmp_path / ".spec.toml"
    config_path.write_text(
        """
[autopilot]
container_bridge_endpoint_threshold = 900
container_capacity_recheck_seconds = 12.5
"""
    )

    config = load_spec_runtime_config(require=True, config_path=config_path)

    assert config.autopilot.container_bridge_endpoint_threshold == 900
    assert config.autopilot.container_capacity_recheck_seconds == 12.5
