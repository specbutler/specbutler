#!/usr/bin/env python3
"""Standalone dispatcher for orchestrated spec runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib import request as urllib_request

from spec_runtime import worktree_process_registry
from spec_runtime.config import (
    ExecutionConfig,
    SpecRuntimeConfig,
    load_repo_spec_runtime_config,
    load_spec_runtime_config,
    resolve_spec_path,
)
from spec_runtime.control_plane import (
    LeaseStatus,
    RunLease,
    ShutdownPhase,
    ShutdownTracker,
    classify_lease,
    evaluate_process_adoption,
    load_run_lease,
)
from spec_runtime.control_plane.lease import lease_actor
from spec_runtime.coordination import (
    CoordinatorError,
    CoordinatorLeaseConflictError,
    lease_age_seconds,
)
from spec_runtime.coordination import (
    build_client as build_coordinator_client,
)
from spec_runtime.execution_backend import (
    ContainerCapacityResult,
    WorkspaceHandle,
    get_execution_backend,
    inspect_container_capacity,
)
from spec_runtime.git_common import resolve_common_root
from spec_runtime.orchestrator import (
    BASE_REF,
    BLOCK_DEBUGGER_AUTO_RESUME_LIMIT,
    BlockDiagnosis,
    OperatorSteering,
    RunState,
    _branch_commits_ahead_of_base,
    _latest_non_superseded_run,
    _normalize_error_for_fingerprint,
    _operator_continuation_for_run,
    _select_default_run,
    format_attempt_progress,
    read_spec_lock_owner,
)
from spec_runtime.spec_merge_tags import (
    MergeTagProvenance,
    annotated_tag_command,
    build_tag_message,
    merge_tag_name,
    push_tag_command,
    utc_timestamp_now,
)
from spec_runtime.spec_metadata import DEFAULT_SPEC_PRIORITY, SpecMetadata, iter_spec_metadata
from spec_runtime.spec_status import (
    TERMINAL_RUN_STATUSES,
    blocks_on_required_intake,
    collect_git_spec_state,
    find_intake_resume_run,
    get_spec_status,
    project_canonical_spec_status,
    project_run_record_status,
    run_has_completed_intake,
)

DEFAULT_WORKTREE_CONCURRENCY = 8
DEFAULT_CONTAINER_CONCURRENCY = 2
DEFAULT_CLONE_CONCURRENCY = 4
STALE_HEARTBEAT_SECONDS = 600  # 10 minutes
ABANDONED_TASK_ID_RE = re.compile(r"^task-\d{8}T\d+[a-f0-9]*$")
DEFAULT_POLL_INTERVAL_SECONDS = 5
ERROR_TAIL_LINES = 20
LOW_MEMORY_THRESHOLD_BYTES = 2 * 1024 * 1024 * 1024
STATUS_QUEUE_PREVIEW = 6
AUTOPILOT_TITLE = "Spec Autopilot"
AREA_AGENT_MAP = {
    "frontend": "claude",
    "fullstack": "claude",
    "backend": "codex",
    "orchestrator": "codex",
}
# Must stay in sync with orchestrator.AUTO_RESUME_PHASES (a consistency test
# pins them together): "verify" resumes runs left passed/verify by manual
# phase recovery.
AUTO_RESUME_PHASES = frozenset(("verify", "publish", "review", "merge"))
UI_HEURISTIC_RE = re.compile(
    r"\b(react|component|wizard|modal|css|frontend|page|form|ui|ux|layout|responsive)\b",
    re.IGNORECASE,
)
VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (?P<bytes>\d+) bytes", re.IGNORECASE)
VM_STAT_RECLAIMABLE_KEYS = (
    "pages free",
    "pages inactive",
    "pages speculative",
)


SPEC_RUNTIME_CONFIG = load_spec_runtime_config(require=False)


DISPATCH_BREAKER_THRESHOLD = 3
DISPATCH_BREAKER_BASE_BACKOFF_SECONDS = 30.0
DISPATCH_BREAKER_MAX_BACKOFF_SECONDS = 3600.0


@dataclass
class _BreakerEntry:
    key: tuple[str, str, str]
    count: int
    error_text: str
    backoff_until: float


class DispatchCircuitBreaker:
    """Same-error circuit breaker for autopilot dispatch.

    Keyed on ``(spec_id, phase, normalized error fingerprint)``. This is the
    safety net for deterministic failures that the persisted retryable
    classification misses (or classifies as retryable). Repeated *identical*
    failures back off exponentially; after ``threshold`` identical failures
    dispatch is suppressed entirely and the spec surfaces as needs-attention
    with the original error. A different error fingerprint or a successful
    phase resets the breaker.

    State lives for the life of the autopilot process, so it survives queue
    rebuilds within a run. It is intentionally *not* persisted across
    restarts: a fresh process re-observes failures from scratch, which is the
    conservative choice (at worst a few extra dispatches after a restart, never
    a permanently wedged spec that outlives the failing condition).
    """

    def __init__(
        self,
        *,
        threshold: int = DISPATCH_BREAKER_THRESHOLD,
        base_backoff: float = DISPATCH_BREAKER_BASE_BACKOFF_SECONDS,
        max_backoff: float = DISPATCH_BREAKER_MAX_BACKOFF_SECONDS,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._base_backoff = float(base_backoff)
        self._max_backoff = float(max_backoff)
        self._entries: dict[str, _BreakerEntry] = {}

    def record_failure(self, spec_id: str, phase: str, error_text: str, *, now: float) -> int:
        """Record a failed run; returns the consecutive identical-failure count."""
        fingerprint = _normalize_error_for_fingerprint(error_text or "")
        key = (spec_id, str(phase or ""), fingerprint)
        entry = self._entries.get(spec_id)
        if entry is not None and entry.key == key:
            entry.count += 1
            if error_text:
                entry.error_text = error_text
        else:
            # New spec, new phase, or a different error fingerprint resets the
            # count so only *identical* repeated failures trip the breaker.
            entry = _BreakerEntry(key=key, count=1, error_text=error_text or "", backoff_until=0.0)
            self._entries[spec_id] = entry
        # Cap the exponent before multiplying: the backoff is clamped to
        # ``_max_backoff`` anyway, so a large ``count`` need not compute an
        # astronomically large ``2 ** (count - 1)`` (a float * oversized-int
        # multiply raises ``OverflowError`` and would crash the dispatch loop).
        exponent = min(entry.count - 1, 32)
        backoff = min(self._base_backoff * (2**exponent), self._max_backoff)
        entry.backoff_until = now + backoff
        return entry.count

    def record_success(self, spec_id: str) -> None:
        """A successful phase clears the breaker for the spec."""
        self._entries.pop(spec_id, None)

    def should_dispatch(self, spec_id: str, now: float) -> bool:
        entry = self._entries.get(spec_id)
        if entry is None:
            return True
        if entry.count >= self._threshold:
            return False
        return now >= entry.backoff_until

    def is_tripped(self, spec_id: str) -> bool:
        entry = self._entries.get(spec_id)
        return entry is not None and entry.count >= self._threshold

    def backoff_remaining(self, spec_id: str, now: float) -> float:
        entry = self._entries.get(spec_id)
        if entry is None:
            return 0.0
        return max(0.0, entry.backoff_until - now)

    def failure_count(self, spec_id: str) -> int:
        entry = self._entries.get(spec_id)
        return entry.count if entry is not None else 0

    def failure_detail(self, spec_id: str) -> str:
        entry = self._entries.get(spec_id)
        return entry.error_text if entry is not None else ""


DEFAULT_LOCK_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_LOCK_BACKOFF_MAX_SECONDS = 1800.0
DEFAULT_OPERATOR_GRACE_SECONDS = 600.0


@dataclass
class _LockContentionEntry:
    owner_key: str
    owner_detail: str
    count: int
    backoff_until: float


@dataclass(frozen=True)
class LockContentionOutcome:
    should_log: bool
    escalated: bool
    backoff_seconds: float
    backoff_until: float
    owner_detail: str


class LockContentionTracker:
    """Exponential backoff for specs whose per-spec lock is held elsewhere.

    Autopilot must not re-dispatch a lock-contended spec every poll cycle and
    launch a doomed ``spec implement`` child each time. This tracker instead
    records the owner once, then backs off exponentially
    (``base`` → ``base*2`` → ``base*4`` …, capped at ``max``) for that spec while
    the lock stays held. A changed owner or a freed lock resets the schedule.
    State is keyed on ``spec_id`` and lives for the life of the process.
    """

    def __init__(
        self,
        *,
        base_backoff: float = DEFAULT_LOCK_BACKOFF_BASE_SECONDS,
        max_backoff: float = DEFAULT_LOCK_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._base = max(1.0, float(base_backoff))
        self._max = max(self._base, float(max_backoff))
        self._entries: dict[str, _LockContentionEntry] = {}

    def record_locked(
        self,
        spec_id: str,
        owner_key: str,
        owner_detail: str,
        *,
        now: float,
    ) -> LockContentionOutcome:
        entry = self._entries.get(spec_id)
        if entry is None or entry.owner_key != owner_key:
            # First observation, or the lock changed hands — a state change
            # worth a single log line at the base backoff.
            entry = _LockContentionEntry(
                owner_key=owner_key,
                owner_detail=owner_detail,
                count=1,
                backoff_until=now + self._base,
            )
            self._entries[spec_id] = entry
            return LockContentionOutcome(
                should_log=True,
                escalated=True,
                backoff_seconds=self._base,
                backoff_until=entry.backoff_until,
                owner_detail=owner_detail,
            )
        entry.owner_detail = owner_detail
        if now >= entry.backoff_until:
            # Still contended after the prior window elapsed — escalate. Cap the
            # exponent so a long-lived holder cannot overflow the multiply.
            entry.count += 1
            exponent = min(entry.count - 1, 20)
            backoff = min(self._base * (2**exponent), self._max)
            entry.backoff_until = now + backoff
            return LockContentionOutcome(
                should_log=False,
                escalated=True,
                backoff_seconds=backoff,
                backoff_until=entry.backoff_until,
                owner_detail=owner_detail,
            )
        return LockContentionOutcome(
            should_log=False,
            escalated=False,
            backoff_seconds=max(0.0, entry.backoff_until - now),
            backoff_until=entry.backoff_until,
            owner_detail=owner_detail,
        )

    def record_free(self, spec_id: str) -> bool:
        """Clear a spec's contention state; returns True if it was contended."""
        return self._entries.pop(spec_id, None) is not None

    def is_backing_off(self, spec_id: str, now: float) -> bool:
        entry = self._entries.get(spec_id)
        return entry is not None and now < entry.backoff_until

    def is_tracked(self, spec_id: str) -> bool:
        return spec_id in self._entries

    def backoff_until(self, spec_id: str) -> float:
        entry = self._entries.get(spec_id)
        return entry.backoff_until if entry is not None else 0.0

    def backoff_remaining(self, spec_id: str, now: float) -> float:
        entry = self._entries.get(spec_id)
        if entry is None:
            return 0.0
        return max(0.0, entry.backoff_until - now)

    def owner_detail(self, spec_id: str) -> str:
        entry = self._entries.get(spec_id)
        return entry.owner_detail if entry is not None else ""


@dataclass(frozen=True)
class OperatorGraceDecision:
    yield_to_operator: bool
    reason: str = ""
    detail: str = ""


def evaluate_operator_grace(
    *,
    actor: str,
    touched_at: str,
    now: datetime,
    grace_seconds: float,
    process_alive: bool | None,
    lease_held: bool,
) -> OperatorGraceDecision:
    """Decide whether autopilot should yield a run to a non-autopilot actor.

    An operator resume can lose the run lock to an autopilot dispatch. After
    any non-autopilot actor touches a run,
    autopilot leaves it alone for ``grace_seconds`` — *unless* the operator
    process has exited AND the run is no longer lease-held, in which case the
    operator is gone and autopilot may safely reclaim it.
    """
    normalized_actor = (actor or "").strip()
    if not normalized_actor or normalized_actor == "autopilot":
        return OperatorGraceDecision(yield_to_operator=False)
    # Operator is gone: process exited and the run is not lease-held. Reclaim.
    if process_alive is False and not lease_held:
        return OperatorGraceDecision(
            yield_to_operator=False,
            detail=f"operator {normalized_actor} exited and lease released",
        )
    touched_dt = _parse_iso_datetime(touched_at)
    within_grace = False
    remaining = 0.0
    if touched_dt is not None:
        elapsed = (now.astimezone(UTC) - touched_dt).total_seconds()
        within_grace = elapsed < grace_seconds
        remaining = max(0.0, grace_seconds - elapsed)
    if within_grace or lease_held:
        detail = f"operator {normalized_actor} active"
        if within_grace:
            detail += f"; grace {remaining:.0f}s remaining"
        elif lease_held:
            detail += "; run lease still held"
        return OperatorGraceDecision(
            yield_to_operator=True,
            reason="operator-grace",
            detail=detail,
        )
    return OperatorGraceDecision(yield_to_operator=False)


def _lease_is_held(lease: RunLease | None, *, process_alive: bool | None, now: datetime) -> bool:
    status = classify_lease(lease, now=now, process_alive=process_alive)
    return status is LeaseStatus.ACTIVE


@dataclass(frozen=True)
class StrandedWorkDecision:
    needs_attention: bool
    detail: str = ""


def evaluate_stranded_committed_work(
    *,
    resumable: bool,
    commits_ahead: int,
    run_id: str,
    branch: str,
    base_ref: str,
    status: str,
) -> StrandedWorkDecision:
    """Decide whether a fresh dispatch would strand committed work.

    Autopilot must *never supersede* a run whose branch holds commits
    ahead of base. Runs that can still be resumed are handled by the resume path
    (``_select_default_run`` returns them). When a run carries committed work but
    is *not* resumable, superseding it and reimplementing from scratch is exactly
    that policy forbids — so instead of dispatching a fresh run the spec must
    surface needs-attention explaining what work exists and where.
    """
    if commits_ahead <= 0 or resumable:
        return StrandedWorkDecision(needs_attention=False)
    plural = "s" if commits_ahead != 1 else ""
    detail = (
        f"run {run_id} has {commits_ahead} commit{plural} on {branch} ahead of "
        f"{base_ref} (status={status}); resume it or clean up the branch — "
        f"autopilot will not supersede committed work"
    )
    return StrandedWorkDecision(needs_attention=True, detail=detail)


@dataclass(frozen=True)
class DispatchCandidate:
    spec_id: str
    agent: str
    area: str
    priority: int
    unlock_count: int
    status: str
    backend: str = ""
    safety_mode: str = ""
    backend_source: str = ""
    run_id: str = ""
    reason: str = "new"
    lease_state: str = ""
    lease_owner: str = ""
    lease_heartbeat_age: str = ""
    lease_expires_at: str = ""
    lease_run_id: str = ""
    lease_message: str = ""
    lock_owner_pid: int = 0
    lock_owner: str = ""
    operator_grace: bool = False
    operator_actor: str = ""
    operator_grace_detail: str = ""
    stranded_commits_detail: str = ""


@dataclass
class ActiveRunProcess:
    spec_id: str
    agent: str
    pid: int
    started_at: str
    started_monotonic: float
    log_path: str
    run_id: str = ""
    phase: str = "launching"
    process_started_at: str = ""


@dataclass(frozen=True)
class LoggedOutcome:
    timestamp: str
    spec_id: str
    agent: str
    outcome: str
    duration_seconds: float
    phase_reached: str
    exit_code: int
    error_tail: list[str]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str
    command: str


@dataclass(frozen=True)
class PidFileRecord:
    pid: int
    started_at: str = ""
    command: str = ""


@dataclass(frozen=True)
class RunRecordIndex:
    records: tuple[dict, ...] = ()
    latest_by_spec: dict[str, dict] = field(default_factory=dict)
    by_run_id: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class CoordinatorLeaseView:
    spec_id: str
    state: str
    owner: str = ""
    run_id: str = ""
    heartbeat_age: str = ""
    expires_at: str = ""
    agent: str = ""
    message: str = ""


@dataclass(frozen=True)
class CoordinatorLeaseSnapshot:
    enabled: bool = False
    unavailable_message: str = ""
    leases_by_spec: dict[str, CoordinatorLeaseView] = field(default_factory=dict)

    @property
    def unavailable(self) -> bool:
        return bool(self.unavailable_message)


@dataclass(frozen=True)
class AutopilotBackendPolicy:
    backend: str
    safety_mode: str
    source: str
    backend_explicit: bool


@dataclass(frozen=True)
class AutopilotConcurrencyPolicy:
    cap: int
    source: str
    backend: str
    memory_mb_per_run: int
    host_memory_mb: int = 0
    host_cpus: int = 0


class ContainerCapacityPreflight:
    """Interval-cached container capacity gate for dispatch cycles."""

    def __init__(
        self,
        *,
        recheck_seconds: float,
        checker: Callable[[], ContainerCapacityResult],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._recheck_seconds = max(1.0, float(recheck_seconds))
        self._checker = checker
        self._clock = clock
        self._checked_at: float | None = None
        self._result: ContainerCapacityResult | None = None

    def evaluate(self, candidates: list[DispatchCandidate]) -> ContainerCapacityResult | None:
        if not any(candidate.backend == "container" for candidate in candidates):
            return None
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._recheck_seconds:
            self._result = self._checker()
            self._checked_at = now
        return self._result


def apply_container_capacity_gate(
    candidates: list[DispatchCandidate],
    result: ContainerCapacityResult | None,
) -> tuple[list[DispatchCandidate], list[DispatchCandidate]]:
    """Return launchable and capacity-paused candidates."""
    if result is None or result.available:
        return candidates, []
    paused = [candidate for candidate in candidates if candidate.backend == "container"]
    launchable = [candidate for candidate in candidates if candidate.backend != "container"]
    return launchable, paused


@dataclass(frozen=True)
class DogfoodPerformanceSample:
    platform: str
    cold_start_cached_seconds: float | None = None
    snapshot_restore_seconds: float | None = None
    retry_cycle_seconds: float | None = None
    worktree_retry_cycle_seconds: float | None = None
    cold_full_image_build_seconds: float | None = None


@dataclass(frozen=True)
class DogfoodBudgetResult:
    name: str
    passed: bool
    actual_seconds: float
    limit_seconds: float
    detail: str


def resolve_autopilot_backend_policy(config: SpecRuntimeConfig) -> AutopilotBackendPolicy:
    execution = config.execution
    if execution.backend_explicit:
        source = "repo-config"
        backend = execution.backend
    elif config.autopilot.container_default_enabled:
        source = "rollout-policy"
        backend = "container"
    else:
        source = "legacy-default"
        backend = execution.backend
    return AutopilotBackendPolicy(
        backend=backend,
        safety_mode=execution.safety_mode,
        source=source,
        backend_explicit=execution.backend_explicit,
    )


def execution_config_for_autopilot(config: SpecRuntimeConfig) -> ExecutionConfig:
    policy = resolve_autopilot_backend_policy(config)
    if policy.backend == config.execution.backend:
        return config.execution
    return ExecutionConfig(
        backend=policy.backend,
        safety_mode=config.execution.safety_mode,
        workspace_root=config.execution.workspace_root,
        container=config.execution.container,
        backend_explicit=False,
        safety_mode_explicit=config.execution.safety_mode_explicit,
    )


def _backend_memory_mb(config: SpecRuntimeConfig, backend: str) -> int:
    if backend == "container":
        return config.autopilot.container_memory_mb
    if backend == "clone":
        return config.autopilot.clone_memory_mb
    return config.autopilot.worktree_memory_mb


def _backend_base_concurrency(backend: str) -> int:
    if backend == "container":
        return DEFAULT_CONTAINER_CONCURRENCY
    if backend == "clone":
        return DEFAULT_CLONE_CONCURRENCY
    return DEFAULT_WORKTREE_CONCURRENCY


def compute_autopilot_concurrency(
    config: SpecRuntimeConfig,
    *,
    explicit: int | None,
    host_memory_bytes: int | None = None,
    host_cpus: int | None = None,
) -> AutopilotConcurrencyPolicy:
    policy = resolve_autopilot_backend_policy(config)
    memory_mb_per_run = _backend_memory_mb(config, policy.backend)
    cpus = host_cpus if host_cpus is not None else (os.cpu_count() or 1)
    memory_bytes = available_memory_bytes() if host_memory_bytes is None else host_memory_bytes
    host_memory_mb = int(memory_bytes // (1024 * 1024)) if memory_bytes and memory_bytes > 0 else 0
    if explicit is not None:
        return AutopilotConcurrencyPolicy(
            cap=int(explicit),
            source="operator-set",
            backend=policy.backend,
            memory_mb_per_run=memory_mb_per_run,
            host_memory_mb=host_memory_mb,
            host_cpus=cpus,
        )
    candidates = [_backend_base_concurrency(policy.backend), max(1, cpus)]
    if host_memory_mb:
        candidates.append(max(1, host_memory_mb // memory_mb_per_run))
    return AutopilotConcurrencyPolicy(
        cap=max(1, min(candidates)),
        source="computed",
        backend=policy.backend,
        memory_mb_per_run=memory_mb_per_run,
        host_memory_mb=host_memory_mb,
        host_cpus=cpus,
    )


def validate_autopilot_backend(policy: AutopilotBackendPolicy, config: SpecRuntimeConfig) -> str:
    if policy.backend != "container":
        return ""
    engine = config.execution.container.engine
    if shutil.which(engine):
        return ""
    return (
        "Container autopilot backend requires a Docker-compatible CLI, but "
        f"{engine!r} was not found on PATH. Install Docker Desktop, OrbStack, "
        "Colima, Rancher Desktop, Docker Engine, or configure a Docker-compatible Podman CLI. "
        "Run `spec container doctor` for host-specific diagnostics."
    )


def evaluate_dogfood_performance_budgets(
    sample: DogfoodPerformanceSample,
) -> tuple[DogfoodBudgetResult, ...]:
    platform = sample.platform.strip().lower()
    cold_start_limit = 30.0 if platform in {"macos", "darwin"} else 15.0
    snapshot_limit = 10.0 if platform in {"macos", "darwin"} else 5.0
    checks: list[tuple[str, float | None, float, str]] = [
        (
            "cold-start-cached-image",
            sample.cold_start_cached_seconds,
            cold_start_limit,
            f"cached-image cold start must reach builder-ready in <= {cold_start_limit:g}s",
        ),
        (
            "snapshot-restore",
            sample.snapshot_restore_seconds,
            snapshot_limit,
            f"pre-implement snapshot restore must reach builder-ready in <= {snapshot_limit:g}s",
        ),
        (
            "cold-full-image-build",
            sample.cold_full_image_build_seconds,
            300.0,
            "cold full image build must finish in <= 300s",
        ),
    ]
    if (
        sample.retry_cycle_seconds is not None
        and sample.worktree_retry_cycle_seconds is not None
        and sample.worktree_retry_cycle_seconds > 0
    ):
        retry_limit = sample.worktree_retry_cycle_seconds * 1.5
        checks.append(
            (
                "post-snapshot-retry-cycle",
                sample.retry_cycle_seconds,
                retry_limit,
                "post-snapshot retry cycle must stay within 1.5x worktree retry cycle",
            )
        )

    results: list[DogfoodBudgetResult] = []
    for name, actual, limit, detail in checks:
        if actual is None:
            continue
        actual_value = float(actual)
        results.append(
            DogfoodBudgetResult(
                name=name,
                passed=actual_value <= limit,
                actual_seconds=actual_value,
                limit_seconds=limit,
                detail=detail,
            )
        )
    return tuple(results)


def _run_record_sort_key(data: dict) -> tuple[str, str, str]:
    return (
        str(data.get("created_at", "")).strip(),
        str(data.get("updated_at", "")).strip(),
        str(data.get("run_id", "")).strip(),
    )


def _parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _format_lease_age(lease: dict[str, object]) -> str:
    age = lease_age_seconds(lease)
    if age is None:
        return "unknown"
    if age < 60:
        return f"{age:.0f}s"
    minutes = int(age // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h{minutes % 60:02d}m"


def _lease_owner(lease: dict[str, object]) -> str:
    return str(lease.get("machine_id") or lease.get("hostname") or "unknown").strip() or "unknown"


def _lease_view_from_payload(
    lease: dict[str, object],
    *,
    local_machine_id: str,
) -> CoordinatorLeaseView | None:
    spec_id = str(lease.get("spec_id") or "").strip()
    if not spec_id:
        return None
    raw_status = str(lease.get("status") or "").strip().lower()
    if raw_status == "released":
        return None
    expires_at = str(lease.get("expires_at") or "").strip()
    expires_dt = _parse_iso_datetime(expires_at)
    expired = raw_status == "expired" or (expires_dt is not None and expires_dt <= datetime.now(UTC))
    owner = _lease_owner(lease)
    if expired:
        state = "expired"
    elif owner == local_machine_id:
        state = "local"
    elif raw_status == "active":
        state = "waiting-remote"
    else:
        state = raw_status or "unknown"
    return CoordinatorLeaseView(
        spec_id=spec_id,
        state=state,
        owner=owner,
        run_id=str(lease.get("run_id") or "").strip(),
        heartbeat_age=_format_lease_age(lease),
        expires_at=expires_at or "unknown",
        agent=str(lease.get("agent") or "").strip(),
    )


def _lease_priority(view: CoordinatorLeaseView) -> tuple[int, str]:
    return {
        "waiting-remote": (0, view.expires_at),
        "local": (1, view.expires_at),
        "expired": (2, view.expires_at),
    }.get(view.state, (3, view.expires_at))


def fetch_coordinator_lease_snapshot(repo_root: Path) -> CoordinatorLeaseSnapshot:
    config = load_repo_spec_runtime_config(repo_root).coordination
    if not config.enabled:
        return CoordinatorLeaseSnapshot(enabled=False)
    try:
        payload = build_coordinator_client(config).list_leases(repo_id=_coordination_repo_id(repo_root))
    except CoordinatorError as exc:
        return CoordinatorLeaseSnapshot(enabled=True, unavailable_message=f"{exc}; run `spec coord doctor`")
    except Exception as exc:  # noqa: BLE001 - watch/status must degrade clearly
        return CoordinatorLeaseSnapshot(enabled=True, unavailable_message=f"{exc}; run `spec coord doctor`")

    views: dict[str, CoordinatorLeaseView] = {}
    for raw_lease in payload.get("leases", []):
        if not isinstance(raw_lease, dict):
            continue
        view = _lease_view_from_payload(raw_lease, local_machine_id=config.machine_id)
        if view is None:
            continue
        current = views.get(view.spec_id)
        if current is None or _lease_priority(view) < _lease_priority(current):
            views[view.spec_id] = view
    return CoordinatorLeaseSnapshot(enabled=True, leases_by_spec=views)


def _annotate_candidate_with_lease(
    candidate: DispatchCandidate,
    snapshot: CoordinatorLeaseSnapshot | None,
) -> DispatchCandidate:
    if snapshot is None or not snapshot.enabled:
        return candidate
    if snapshot.unavailable:
        return DispatchCandidate(
            **{
                **asdict(candidate),
                "lease_state": "coordinator-unavailable",
                "lease_message": snapshot.unavailable_message,
            }
        )
    lease = snapshot.leases_by_spec.get(candidate.spec_id)
    if lease is None:
        return DispatchCandidate(**{**asdict(candidate), "lease_state": "launchable"})
    return DispatchCandidate(
        **{
            **asdict(candidate),
            "lease_state": lease.state,
            "lease_owner": lease.owner,
            "lease_heartbeat_age": lease.heartbeat_age,
            "lease_expires_at": lease.expires_at,
            "lease_run_id": lease.run_id,
            "lease_message": lease.message,
        }
    )


def _annotate_candidate_with_dispatch_discipline(
    repo_root: Path,
    candidate: DispatchCandidate,
    *,
    run_index: RunRecordIndex | None,
    config: SpecRuntimeConfig | None,
    now: datetime,
    grace_seconds: float,
) -> DispatchCandidate:
    """Attach lock-owner and operator-grace signals used by dispatch discipline.

    Probing is cheap: ``read_spec_lock_owner`` only touches the lock file, and
    the run lease and operator-steering record are read only when a run exists
    for the spec. Autopilot's own active runs are filtered from the queue by the
    dispatch loop, and their leases record ``actor=autopilot`` so they never
    trigger operator grace. Operator grace is granted either by a non-autopilot
    lease actor (operator resume / manual phase) or by an active ``spec steer``
    inside the grace window, since steering does not refresh the lease.
    """
    updates: dict[str, object] = {}
    owner = read_spec_lock_owner(repo_root, candidate.spec_id)
    if owner is not None:
        updates["lock_owner_pid"] = owner.pid
        updates["lock_owner"] = owner.describe()

    latest_run_id = candidate.run_id or str(
        read_latest_run_record(repo_root, candidate.spec_id, run_index=run_index).get("run_id", "")
    ).strip()
    if latest_run_id:
        lease = load_run_lease(runs_dir(repo_root, config=config), latest_run_id)
        if lease is not None:
            actor = lease_actor(lease)
            process_alive: bool | None = None
            if lease.process_pid:
                process_alive = is_pid_alive(lease.process_pid, lease.process_started_at)
            lease_held = _lease_is_held(lease, process_alive=process_alive, now=now)
            decision = evaluate_operator_grace(
                actor=actor,
                touched_at=lease.heartbeat_at,
                now=now,
                grace_seconds=grace_seconds,
                process_alive=process_alive,
                lease_held=lease_held,
            )
            if decision.yield_to_operator:
                updates["operator_grace"] = True
                updates["operator_actor"] = actor
                updates["operator_grace_detail"] = decision.detail

    # ``spec steer`` (and other non-autopilot operator touches recorded as
    # steering) must also grant grace. Steering never refreshes the run lease,
    # so a lease still tagged ``actor=autopilot`` (or with no actor) would leave
    # the lease-based check above blind — autopilot would re-dispatch and steal
    # the lock from the operator's intended manual resume. Yield while an active
    # operator steering sits inside the grace window.
    if not updates.get("operator_grace") and latest_run_id:
        steering = OperatorSteering.load(resolve_common_root(repo_root), latest_run_id)
        if steering is not None and steering.status == "active":
            steer_decision = evaluate_operator_grace(
                actor=steering.provided_by,
                touched_at=steering.provided_at,
                now=now,
                grace_seconds=grace_seconds,
                process_alive=None,
                lease_held=False,
            )
            if steer_decision.yield_to_operator:
                updates["operator_grace"] = True
                updates["operator_actor"] = steering.provided_by
                updates["operator_grace_detail"] = (
                    steer_decision.detail
                    or f"operator {steering.provided_by} steered run {latest_run_id}"
                )

    # A fresh dispatch (no resume run selected) whose latest
    # non-superseded run carries committed work would strand that work if we let
    # the child supersede it and reimplement from scratch. Resume the run in
    # place when the default-run selector still considers it resumable; otherwise
    # surface needs-attention explaining what work exists and where.
    if not candidate.run_id:
        latest = _latest_non_superseded_run(repo_root, candidate.spec_id, ensure_identity=False)
        if latest is not None:
            commits_ahead = _branch_commits_ahead_of_base(
                repo_root, latest.branch, latest.base_ref or BASE_REF
            )
            if commits_ahead > 0:
                default_run = _select_default_run(
                    repo_root, candidate.spec_id, ensure_identity=False
                )
                stranded = evaluate_stranded_committed_work(
                    resumable=default_run is not None,
                    commits_ahead=commits_ahead,
                    run_id=latest.run_id,
                    branch=latest.branch,
                    base_ref=latest.base_ref or BASE_REF,
                    status=latest.status,
                )
                if stranded.needs_attention:
                    updates["stranded_commits_detail"] = stranded.detail
                elif default_run is not None:
                    # Resumable committed work that autopilot's own
                    # ``resolve_resume_run`` did not pick (e.g. a generic
                    # implement failure). Resume it in place — same run id,
                    # existing branch — instead of dispatching a fresh run that
                    # would supersede and strand the commits.
                    updates["run_id"] = default_run.run_id
                    updates["reason"] = "resume-committed"

    if not updates:
        return candidate
    return DispatchCandidate(**{**asdict(candidate), **updates})


def resolve_repo_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def autopilot_state_root(repo_root: Path) -> Path:
    return resolve_common_root(repo_root) / SPEC_RUNTIME_CONFIG.paths.state_dir / "autopilot"


def autopilot_runs_root(repo_root: Path) -> Path:
    return autopilot_state_root(repo_root) / "runs"


def run_log_alias_path(repo_root: Path, run_id: str) -> Path:
    return autopilot_runs_root(repo_root) / f"{run_id}.path"


def write_run_log_alias(repo_root: Path, run_id: str, log_path: str) -> None:
    normalized_run_id = run_id.strip()
    normalized_log_path = log_path.strip()
    if not normalized_run_id or not normalized_log_path:
        return
    alias_path = run_log_alias_path(repo_root, normalized_run_id)
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text(normalized_log_path + "\n")


def maybe_write_run_log_alias(repo_root: Path, proc: ActiveRunProcess, run_record: dict) -> None:
    if not proc.run_id:
        return
    record_run_id = str(run_record.get("run_id", "")).strip()
    if not record_run_id or record_run_id != proc.run_id:
        return
    write_run_log_alias(repo_root, proc.run_id, proc.log_path)


def runs_dir(repo_root: Path, *, config: SpecRuntimeConfig | None = None) -> Path:
    cfg = config or load_repo_spec_runtime_config(repo_root)
    return resolve_common_root(repo_root) / cfg.paths.state_dir / "runs"


def load_run_record_index(repo_root: Path, *, config: SpecRuntimeConfig | None = None) -> RunRecordIndex:
    rd = runs_dir(repo_root, config=config)
    if not rd.exists():
        return RunRecordIndex()

    all_records: list[dict] = []
    by_run_id: dict[str, dict] = {}

    for path in rd.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        all_records.append(data)
        run_id = str(data.get("run_id", "")).strip()
        if run_id:
            by_run_id[run_id] = data

    # Populate latest_by_spec: latest non-superseded record per spec
    grouped: dict[str, list[dict]] = {}
    for data in all_records:
        spec_id = str(data.get("spec_id", "")).strip()
        if not spec_id:
            continue
        if str(data.get("status", "")).strip() == "superseded":
            continue
        if str(data.get("superseded_by", "")).strip():
            continue
        grouped.setdefault(spec_id, []).append(data)

    latest_by_spec: dict[str, dict] = {}
    for spec_id, records in grouped.items():
        records.sort(key=_run_record_sort_key, reverse=True)
        latest_by_spec[spec_id] = records[0]

    return RunRecordIndex(
        records=tuple(all_records),
        latest_by_spec=latest_by_spec,
        by_run_id=by_run_id,
    )


def autopilot_log_path(repo_root: Path) -> Path:
    return autopilot_state_root(repo_root) / "log.jsonl"


def autopilot_active_path(repo_root: Path) -> Path:
    return autopilot_state_root(repo_root) / "active.json"


def autopilot_spec_overrides_path(repo_root: Path) -> Path:
    return autopilot_state_root(repo_root) / "spec-overrides.json"


def autopilot_pid_path(repo_root: Path) -> Path:
    return autopilot_state_root(repo_root) / "autopilot.pid"


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def timestamp_token() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_spec_status_overrides(repo_root: Path) -> dict[str, dict[str, str]]:
    path = autopilot_spec_overrides_path(repo_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    overrides: dict[str, dict[str, str]] = {}
    for spec_id, value in payload.items():
        if not isinstance(value, dict):
            continue
        normalized = {
            str(key).strip(): str(entry).strip()
            for key, entry in value.items()
            if str(key).strip() and entry is not None
        }
        if normalized:
            overrides[str(spec_id).strip()] = normalized
    return overrides


def hidden_spec_ids(repo_root: Path) -> set[str]:
    return {
        spec_id for spec_id, data in read_spec_status_overrides(repo_root).items() if data.get("status") == "obsolete"
    }


def write_spec_status_override(
    repo_root: Path,
    spec_id: str,
    *,
    status: str,
    worktree_path: Path | None = None,
) -> None:
    path = autopilot_spec_overrides_path(repo_root)
    overrides = read_spec_status_overrides(repo_root)
    entry = {
        "status": status,
        "updated_at": now_iso(),
    }
    if worktree_path is not None:
        entry["worktree_path"] = str(worktree_path)
    overrides[spec_id] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2, sort_keys=True) + "\n")


def select_agent(spec: SpecMetadata, *, override: str = "", config: SpecRuntimeConfig | None = None) -> str:
    if override:
        return override
    mapped = AREA_AGENT_MAP.get(spec.area)
    if mapped:
        return mapped
    if UI_HEURISTIC_RE.search(spec.body):
        return "claude"
    return (config or SPEC_RUNTIME_CONFIG).agents.default


def compute_unlock_counts(
    specs: list[SpecMetadata],
    *,
    merged_specs: frozenset[str] | set[str] | None = None,
) -> dict[str, int]:
    merged = set(merged_specs or ())
    reverse: dict[str, set[str]] = {}
    valid_specs = {
        spec.spec_id
        for spec in specs
        if not spec.superseded_by and not spec.obsolete and spec.spec_id not in merged
    }
    for spec in specs:
        if spec.superseded_by or spec.obsolete or spec.spec_id in merged:
            continue
        for dep in spec.depends_on:
            if dep in valid_specs:
                reverse.setdefault(dep, set()).add(spec.spec_id)

    cache: dict[str, set[str]] = {}

    def descendants(spec_id: str) -> set[str]:
        if spec_id in cache:
            return cache[spec_id]
        result: set[str] = set()
        for child in reverse.get(spec_id, set()):
            result.add(child)
            result.update(descendants(child))
        cache[spec_id] = result
        return result

    return {spec.spec_id: len(descendants(spec.spec_id)) if spec.spec_id in valid_specs else 0 for spec in specs}


def _block_debugger_auto_resume_available(run_record: dict) -> bool:
    try:
        used = max(0, int(run_record.get("block_debugger_auto_resumes", 0) or 0))
    except (TypeError, ValueError):
        return False
    return used < BLOCK_DEBUGGER_AUTO_RESUME_LIMIT


def resolve_resume_run(
    repo_root: Path,
    spec: SpecMetadata,
    *,
    spec_path: Path | None = None,
    run_index: RunRecordIndex | None = None,
) -> tuple[str, str]:
    latest = read_latest_run_record(repo_root, spec.spec_id, run_index=run_index)
    if latest:
        run_id = str(latest.get("run_id", "")).strip()
        if run_id and is_retryable_failed_implement_run(latest):
            return run_id, "resume-implement"
        if (
            run_id
            and str(latest.get("status", "")).strip() == "passed"
            and str(latest.get("phase", "")).strip() in AUTO_RESUME_PHASES
            and (
                not spec.intake_required
                or run_has_completed_intake(
                    repo_root,
                    run_id,
                    spec_id=spec.spec_id,
                    spec_path=spec_path,
                )
            )
        ):
            return run_id, "resume-run"
        # Auto-resume after operator input resolution or proactive steering.
        if (
            run_id
            and str(latest.get("status", "")).strip() == "passed"
            and str(latest.get("phase", "")).strip() == "implement"
        ):
            run_payload = dict(latest)
            run_payload.setdefault("branch", "")
            continuation = _operator_continuation_for_run(
                resolve_common_root(repo_root),
                RunState.from_dict(run_payload),
            )
            if continuation is not None:
                return run_id, "resume-implement" if continuation.resumes_implement else "resume-run"
        # Auto-resume blocked runs where the debugger diagnosed the issue
        # and determined no human attention is needed.
        if (
            run_id
            and str(latest.get("status", "")).strip() == "blocked"
        ):
            pending_signature = str(latest.get("pending_block_debugger_signature", "")).strip()
            diagnosis = (
                BlockDiagnosis.load(resolve_common_root(repo_root), run_id)
                if pending_signature
                else None
            )
            if (
                pending_signature
                and diagnosis is not None
                and not diagnosis.requires_human_attention
                and diagnosis.blocker_signature == pending_signature
                and _block_debugger_auto_resume_available(latest)
            ):
                return run_id, "resume-blocked"

    if not spec.intake_required:
        return "", ""

    run_id = find_intake_resume_run(repo_root, spec.spec_id) or ""
    if run_id:
        return run_id, "resume-intake"
    return "", ""


def build_dispatch_queue(
    repo_root: Path,
    *,
    agent_override: str = "",
    git_state=None,
    include_needs_intake: bool = False,
    run_index: RunRecordIndex | None = None,
    coordinator_snapshot: CoordinatorLeaseSnapshot | None = None,
    now: datetime | None = None,
    operator_grace_seconds: float | None = None,
) -> list[DispatchCandidate]:
    repo_config = load_repo_spec_runtime_config(repo_root)
    if run_index is None:
        run_index = load_run_record_index(repo_root, config=repo_config)
    now = now or datetime.now(UTC)
    grace_seconds = (
        operator_grace_seconds
        if operator_grace_seconds is not None
        else repo_config.autopilot.operator_grace_seconds
    )
    hidden_specs = hidden_spec_ids(repo_root)
    specs = [
        spec
        for spec in iter_spec_metadata(repo_root)
        if not spec.superseded_by and not spec.obsolete and spec.spec_id not in hidden_specs
    ]
    git_state = git_state or collect_git_spec_state(repo_root, config=repo_config)
    unlock_counts = compute_unlock_counts(specs, merged_specs=git_state.merged_specs)
    backend_policy = resolve_autopilot_backend_policy(repo_config)

    queue: list[DispatchCandidate] = []
    for spec in specs:
        spec_path = resolve_spec_path(repo_root, spec.spec_id, config=repo_config)
        run_id, reason = resolve_resume_run(repo_root, spec, spec_path=spec_path, run_index=run_index)
        status = get_spec_status(repo_root, spec.spec_id, spec_path, git_state=git_state, config=repo_config)
        if status in {"merged", "needs-attention", "obsolete"}:
            # needs-attention driven purely by pending intake is actionable by
            # the operator, so surface it when the caller asked for intake rows.
            pending_intake = (
                include_needs_intake
                and status == "needs-attention"
                and not run_id
                and blocks_on_required_intake(
                    repo_root,
                    spec.spec_id,
                    spec,
                    git_state=git_state,
                    config=repo_config,
                )
            )
            if not pending_intake:
                continue
        # Skip waiting-for-input runs — they need interactive resolution.
        # Consult the projection rather than the raw status: a run whose
        # operator request was already consumed (process died mid-transition)
        # projects RETRYABLE and must be dispatchable again.
        if not run_id:
            latest_record = read_latest_run_record(repo_root, spec.spec_id, run_index=run_index)
            if str(latest_record.get("status", "")).strip() == "waiting-for-input":
                from spec_runtime.control_plane import CanonicalRunStatus as _Canonical

                stranded_projection = project_run_record_status(
                    runs_dir(repo_root, config=repo_config), latest_record
                )
                if stranded_projection is None or stranded_projection.status is _Canonical.NEEDS_INPUT:
                    continue
                resume_id = str(latest_record.get("run_id", "")).strip()
                if resume_id:
                    run_id, reason = resume_id, "resume-run"
        # Consult the canonical control-plane projection so dispatch agrees
        # with `spec status`/`spec list` about whether a run is truly active
        # vs. stale/blocked/needs-input.
        canonical_projection = project_canonical_spec_status(
            repo_root,
            spec.spec_id,
            git_state=git_state,
            config=repo_config,
        )
        if canonical_projection is not None:
            from spec_runtime.control_plane import CanonicalRunStatus as _Canonical

            if canonical_projection.status is _Canonical.NEEDS_INPUT:
                continue
            if canonical_projection.status is _Canonical.ACTIVE and not reason.startswith("resume"):
                continue
        if status == "in-progress" and not reason.startswith("resume"):
            continue
        if not dependencies_are_merged(repo_root, spec.depends_on, git_state=git_state):
            continue

        if not run_id:
            reason = "new"
        if spec.intake_required:
            if not run_id:
                if not include_needs_intake:
                    continue
                reason = "needs-intake"

        candidate = DispatchCandidate(
            spec_id=spec.spec_id,
            agent=select_agent(spec, override=agent_override, config=repo_config),
            area=spec.area,
            priority=(spec.priority if spec.priority is not None else DEFAULT_SPEC_PRIORITY),
            unlock_count=unlock_counts.get(spec.spec_id, 0),
            status=status,
            backend=backend_policy.backend,
            safety_mode=backend_policy.safety_mode,
            backend_source=backend_policy.source,
            run_id=run_id,
            reason=reason,
        )
        candidate = _annotate_candidate_with_lease(candidate, coordinator_snapshot)
        candidate = _annotate_candidate_with_dispatch_discipline(
            repo_root,
            candidate,
            run_index=run_index,
            config=repo_config,
            now=now,
            grace_seconds=grace_seconds,
        )
        queue.append(candidate)

    queue.sort(
        key=lambda item: (-item.unlock_count, item.priority, item.spec_id),
    )
    return queue


def dependencies_are_merged(repo_root: Path, depends_on: tuple[str, ...], *, git_state) -> bool:
    repo_config = load_repo_spec_runtime_config(repo_root)
    for dep in depends_on:
        dep_status = get_spec_status(
            repo_root,
            dep,
            resolve_spec_path(repo_root, dep, config=repo_config),
            git_state=git_state,
            config=repo_config,
        )
        if dep_status != "merged":
            return False
    return True


def read_latest_run_phase(
    repo_root: Path,
    spec_id: str,
    run_id: str = "",
    *,
    run_index: RunRecordIndex | None = None,
) -> str:
    run = read_latest_run_record(repo_root, spec_id, run_id=run_id, run_index=run_index)
    return run.get("phase", "") if run else ""


def read_latest_run_record(
    repo_root: Path,
    spec_id: str,
    *,
    run_id: str = "",
    run_index: RunRecordIndex | None = None,
) -> dict:
    if run_index is None:
        run_index = load_run_record_index(repo_root)

    if run_id:
        data = run_index.by_run_id.get(run_id)
        if data is None:
            return {}
        return data if str(data.get("spec_id", "")).strip() == spec_id else {}

    return run_index.latest_by_spec.get(spec_id, {})


def is_retryable_failed_implement_run(run_record: dict) -> bool:
    if not isinstance(run_record, dict):
        return False
    if str(run_record.get("status", "")).strip() != "failed":
        return False
    if str(run_record.get("phase", "")).strip() != "implement":
        return False
    last_error = str(run_record.get("last_error", "")).strip().lower()
    if "no_handshake" not in last_error and "agent became inactive" not in last_error:
        return False
    try:
        attempts = int(run_record.get("attempts", 0))
    except (TypeError, ValueError):
        attempts = 0
    try:
        retry_cap = int(run_record.get("retry_cap", 0))
    except (TypeError, ValueError):
        retry_cap = 0
    if retry_cap > 0 and attempts >= retry_cap:
        return False
    return True


def _meminfo_available_bytes() -> int | None:
    """Linux MemAvailable — the kernel's estimate of memory usable by new
    workloads INCLUDING reclaimable page cache. SC_AVPHYS_PAGES reports only
    free pages, which on a warm-cache box underreports by an order of
    magnitude and silently throttled autopilot concurrency to 1 on a 60GB
    host with 53GB available."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) * 1024
                    break
    except OSError:
        pass
    return None


def available_memory_bytes() -> int | None:
    meminfo = _meminfo_available_bytes()
    if meminfo is not None:
        return meminfo

    def read_sysconf(name: str) -> int | None:
        try:
            value = os.sysconf(name)
        except (AttributeError, OSError, ValueError):
            return None
        if not isinstance(value, int) or value < 0:
            return None
        return value

    pages = read_sysconf("SC_AVPHYS_PAGES")
    page_size = read_sysconf("SC_PAGE_SIZE") or read_sysconf("SC_PAGESIZE")
    if pages is not None and page_size is not None:
        return pages * page_size

    try:
        result = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    page_size_match = VM_STAT_PAGE_SIZE_RE.search(result.stdout)
    if page_size_match is None:
        return None

    reclaimable_pages = 0
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        normalized_key = key.strip().lower()
        if normalized_key not in VM_STAT_RECLAIMABLE_KEYS:
            continue
        digits = "".join(ch for ch in raw_value if ch.isdigit())
        if not digits:
            continue
        reclaimable_pages += int(digits)

    return reclaimable_pages * int(page_size_match.group("bytes"))


def format_status_line(event: str, detail: str) -> str:
    return f"[{timestamp_token()}] {event}: {detail}"


SOURCE_FINGERPRINT_INTERVAL_SECONDS = 300


def source_fingerprint(package_root: Path | None = None) -> str:
    """Fingerprint the on-disk ``spec_runtime`` sources.

    ``spec`` is commonly installed editable, so a long-lived ``spec auto run``
    keeps executing whatever it imported at launch. Pulling a fix has no effect
    on the daemon already running, and nothing surfaces that: a stale autopilot
    silently re-applies the bugs it was upgraded to fix.
    """
    if package_root is None:
        package_root = Path(__file__).resolve().parent
    parts: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class SourceStalenessWatch:
    """Warn once when the orchestrator's own sources change under a running daemon."""

    def __init__(
        self,
        *,
        interval_seconds: float = SOURCE_FINGERPRINT_INTERVAL_SECONDS,
        fingerprinter: Callable[[], str] = source_fingerprint,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval_seconds
        self._fingerprint = fingerprinter
        self._clock = clock
        self._loaded = fingerprinter()
        self._last_checked = clock()
        self._announced: set[str] = set()

    def check(self) -> str:
        """Return a warning to print, or "" when nothing needs saying."""
        now = self._clock()
        if now - self._last_checked < self._interval:
            return ""
        self._last_checked = now
        current = self._fingerprint()
        if current == self._loaded or current in self._announced:
            return ""
        self._announced.add(current)
        return (
            "spec_runtime sources changed on disk since this autopilot started; "
            "dispatches keep using the code loaded at launch. Restart "
            "`spec auto run` to pick up the change."
        )


def refresh_runtime_git_refs(repo_root: Path) -> tuple[bool, str]:
    """Refresh remote refs used by spec_status before queue evaluation."""
    from .control_plane import (
        DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        GitFetchTimeoutError,
        run_git_fetch_with_timeout,
    )

    try:
        outcome = run_git_fetch_with_timeout(
            ["--quiet", "--tags", "--prune", "origin"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        )
    except GitFetchTimeoutError as exc:
        return False, f"git fetch timed out after {exc.timeout_seconds:.0f}s"

    if outcome.is_success:
        return True, ""

    output = (outcome.stderr or outcome.stdout).strip()
    if not output:
        output = "git fetch failed"
    return False, output


def write_active_state(repo_root: Path, active: dict[str, ActiveRunProcess]) -> None:
    payload = {
        spec_id: {
            "pid": proc.pid,
            "agent": proc.agent,
            "started_at": proc.started_at,
            "phase": proc.phase,
            "run_id": proc.run_id,
            "log_path": proc.log_path,
            "process_started_at": proc.process_started_at,
        }
        for spec_id, proc in sorted(active.items())
    }
    path = autopilot_active_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_log_tail(log_path: str, n: int = ERROR_TAIL_LINES) -> list[str]:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
    except OSError:
        return []


def is_pid_alive(pid: int, expected_started_at: str) -> bool:
    identity = read_process_identity(pid)
    if identity is None:
        return False
    if expected_started_at and identity.started_at != expected_started_at:
        return False
    return True


def adopt_active_processes(repo_root: Path) -> dict[str, ActiveRunProcess]:
    path = autopilot_active_path(repo_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    state_runs_dir = runs_dir(repo_root)

    adopted: dict[str, ActiveRunProcess] = {}
    for spec_id, item in payload.items():
        pid = int(item.get("pid", 0))
        process_started_at = str(item.get("process_started_at", "")).strip()
        run_id = str(item.get("run_id", "")).strip()
        if pid <= 0:
            continue
        if not process_started_at:
            print(
                format_status_line(
                    "stale",
                    f"{spec_id} pid={pid} missing process identity; skipping",
                )
            )
            continue

        process_alive = is_pid_alive(pid, process_started_at)
        live_identity = read_process_identity(pid) if process_alive else None
        live_started_at = live_identity.started_at if live_identity is not None else ""

        if not run_id:
            # Newly dispatched spec: the child `spec implement` has not yet
            # created a run record (and therefore no lease) but the process
            # we spawned may still be live. Fall back to PID-identity
            # adoption so we wait on the existing child instead of starting
            # a duplicate.
            if not process_alive:
                print(format_status_line("stale", f"{spec_id} pid={pid} no longer alive"))
                continue
            adopted[spec_id] = ActiveRunProcess(
                spec_id=spec_id,
                agent=str(item.get("agent", "")),
                pid=pid,
                started_at=str(item.get("started_at", "")),
                started_monotonic=0.0,
                log_path=str(item.get("log_path", "")),
                run_id="",
                phase=str(item.get("phase", "unknown")),
                process_started_at=process_started_at,
            )
            print(
                format_status_line(
                    "adopt",
                    f"{spec_id} pid={pid} phase={adopted[spec_id].phase} (pre-lease)",
                )
            )
            continue

        lease = load_run_lease(state_runs_dir, run_id)
        outcome = evaluate_process_adoption(
            expected_run_id=run_id,
            expected_spec_id=spec_id,
            lease=lease,
            recorded_pid=pid,
            recorded_process_started_at=process_started_at,
            process_alive=process_alive,
            live_process_started_at=live_started_at,
        )
        if not outcome.should_wait:
            reason = outcome.reason or outcome.decision.value
            print(
                format_status_line(
                    "stale",
                    f"{spec_id} pid={pid} not adopted ({outcome.decision.value}): {reason}",
                )
            )
            continue
        adopted[spec_id] = ActiveRunProcess(
            spec_id=spec_id,
            agent=str(item.get("agent", "")),
            pid=pid,
            started_at=str(item.get("started_at", "")),
            started_monotonic=0.0,
            log_path=str(item.get("log_path", "")),
            run_id=run_id,
            phase=str(item.get("phase", "unknown")),
            process_started_at=process_started_at,
        )
        print(format_status_line("adopt", f"{spec_id} pid={pid} phase={adopted[spec_id].phase}"))
    return adopted


def cleanup_unadopted_container_runs(
    repo_root: Path,
    adopted: dict[str, ActiveRunProcess],
) -> None:
    """Clean container artifacts for active-state entries that restart could not adopt."""
    path = autopilot_active_path(repo_root)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, TypeError):
        return
    if not isinstance(payload, dict):
        return

    repo_config = load_repo_spec_runtime_config(repo_root)
    state_runs_dir = runs_dir(repo_root, config=repo_config)
    handled_run_ids: set[str] = set()
    for spec_id, item in payload.items():
        if spec_id in adopted:
            continue
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id", "")).strip()
        if not run_id or run_id in handled_run_ids:
            continue
        handled_run_ids.add(run_id)
        run_path = state_runs_dir / f"{run_id}.json"
        try:
            run_record = json.loads(run_path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        if not isinstance(run_record, dict):
            continue
        if str(run_record.get("backend", "")).strip() != "container":
            continue

        execution = replace(
            repo_config.execution,
            backend="container",
            safety_mode=str(run_record.get("safety_mode") or repo_config.execution.safety_mode),
        )
        run_root = repo_root / execution.workspace_root / run_id
        workspace = WorkspaceHandle(
            path=run_root / "source",
            outbox_path=run_root / "outbox",
            branch=str(run_record.get("branch", "")).strip(),
            backend="container",
        )
        try:
            get_execution_backend(execution).cleanup(workspace)
        except (OSError, RuntimeError) as exc:
            print(
                format_status_line(
                    "warning",
                    f"{spec_id} stale container cleanup failed for run={run_id}: {exc}",
                )
            )


def append_log(repo_root: Path, outcome: LoggedOutcome) -> None:
    path = autopilot_log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(outcome), sort_keys=True) + "\n")


def parse_notify_backends(raw_values: list[str], *, default: bool) -> list[str]:
    backends: list[str] = ["macos"] if default else []
    for value in raw_values:
        for entry in value.split(","):
            normalized = entry.strip()
            if normalized and normalized not in backends:
                backends.append(normalized)
    return backends


def notify(backends: list[str], *, title: str, message: str) -> None:
    for backend in backends:
        if backend == "macos":
            _notify_macos(title, message)
            continue
        if backend.startswith("ntfy:"):
            _notify_ntfy(backend.split(":", 1)[1], message)
            continue
        if backend == "slack":
            _notify_slack(title, message)


def _notify_macos(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = ('display notification "{message}" with title "{title}"').format(
        message=message.replace("\\", "\\\\").replace('"', '\\"'),
        title=title.replace("\\", "\\\\").replace('"', '\\"'),
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    sound = Path("/System/Library/Sounds/Glass.aiff")
    if sound.exists():
        subprocess.run(["afplay", str(sound)], capture_output=True, text=True)


def _notify_ntfy(topic: str, message: str) -> None:
    topic = topic.strip()
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    subprocess.run(
        ["curl", "-fsS", "-d", message, f"{server}/{topic}"],
        capture_output=True,
        text=True,
    )


def _notify_slack(title: str, message: str) -> None:
    webhook = os.environ.get("AUTOPILOT_SLACK_WEBHOOK", "").strip()
    if not webhook:
        return
    payload = json.dumps({"text": f"*{title}*\n{message}"}).encode("utf-8")
    request = urllib_request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=10):
            return
    except Exception:
        return


def classify_completion(
    tail: list[str],
    *,
    run_record: dict | None = None,
    expected_run_id: str = "",
) -> str:
    if run_record:
        record_run_id = str(run_record.get("run_id", "")).strip()
        if not expected_run_id or record_run_id == expected_run_id:
            status = str(run_record.get("status", "")).strip()
            phase = str(run_record.get("phase", "")).strip()
            if status == "passed" and phase in ("merge", "cleanup"):
                return "success"
            if status == "waiting-for-input":
                return "waiting"
    joined = "\n".join(tail).lower()
    if "lock contention" in joined or "holds the lock" in joined:
        return "skip"
    if "already merged on origin/master" in joined or "already merged" in joined:
        return "skip"
    if "already in-progress" in joined or "already in progress" in joined:
        return "skip"
    if "waiting for input" in joined:
        return "waiting"
    return "failure"


def format_failure_notification(spec_id: str, phase_reached: str, tail: list[str]) -> str:
    if tail:
        output = "\n".join(tail[-ERROR_TAIL_LINES:])
        line_count = min(len(tail), ERROR_TAIL_LINES)
    else:
        output = "No output captured."
        line_count = 0
    return f"Spec: {spec_id}\nPhase: {phase_reached}\nOutput tail ({line_count} lines):\n{output}"


def render_queue(
    queue: list[DispatchCandidate],
    lease_backoff_until: dict[str, float] | None = None,
    *,
    capacity_warning: str = "",
    lock_backoff_until: dict[str, float] | None = None,
) -> str:
    if not queue:
        return f"Queue: empty\n  warning={capacity_warning}" if capacity_warning else "Queue: empty"
    lease_backoff_until = lease_backoff_until or {}
    lock_backoff_until = lock_backoff_until or {}
    now = time.monotonic()
    lines = ["Queue:"]
    if capacity_warning:
        lines.append(f"  warning={capacity_warning}")
    for item in queue:
        resume = f" run={item.run_id}" if item.run_id else ""
        area = item.area or "-"
        backoff_remaining = lease_backoff_until.get(item.spec_id, 0.0) - now
        dispatch_status = ""
        if item.lock_owner:
            lock_remaining = lock_backoff_until.get(item.spec_id, 0.0) - now
            backoff_suffix = f" backoff={max(0, round(lock_remaining))}s" if lock_remaining > 0 else ""
            dispatch_status += f" locked by {item.lock_owner}{backoff_suffix}"
        if item.operator_grace:
            dispatch_status += f" operator-grace({item.operator_actor or 'operator'})"
        if item.stranded_commits_detail:
            dispatch_status += " needs-attention(stranded-commits)"
        if item.lease_state == "waiting-remote":
            lease_status = (
                f" lease=waiting-remote owner={item.lease_owner or 'unknown'} "
                f"heartbeat={item.lease_heartbeat_age or 'unknown'} expires={item.lease_expires_at or 'unknown'}"
            )
        elif item.lease_state == "expired":
            lease_status = (
                f" lease=reclaimable owner={item.lease_owner or 'unknown'} "
                f"expired={item.lease_expires_at or 'unknown'}"
            )
        elif item.lease_state == "coordinator-unavailable":
            lease_status = f" lease=coordinator-unavailable({item.lease_message or 'unknown'})"
        elif backoff_remaining > 0:
            lease_status = f" lease=backoff({max(0, round(backoff_remaining))}s)"
        else:
            lease_status = ""
        lines.append(
            "  "
            f"{item.spec_id} agent={item.agent} area={area} unlocks={item.unlock_count} "
            f"priority={item.priority} mode={item.reason}{resume}{lease_status}{dispatch_status}"
        )
    return "\n".join(lines)


def _candidate_run_id(spec_id: str) -> str:
    return f"{spec_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"


def _coordination_repo_id(repo_root: Path) -> str:
    config = load_repo_spec_runtime_config(repo_root).coordination
    if config.repo_id.strip():
        return config.repo_id.strip()
    try:
        return resolve_common_root(repo_root).name or "repo"
    except Exception:
        return repo_root.name or "repo"


def _candidate_lease_payload(
    repo_root: Path,
    candidate: DispatchCandidate,
    *,
    run_id: str,
) -> dict[str, object]:
    config = load_repo_spec_runtime_config(repo_root).coordination
    return {
        "repo_id": _coordination_repo_id(repo_root),
        "spec_id": candidate.spec_id,
        "run_id": run_id,
        "machine_id": config.machine_id,
        "agent": candidate.agent,
        "ttl_seconds": int(os.environ.get("SPEC_COORDINATOR_LEASE_TTL_SECONDS", "900")),
        "hostname": config.machine_id,
    }


def format_lease_conflict(lease: dict[str, object]) -> str:
    owner = str(lease.get("machine_id") or lease.get("hostname") or "unknown")
    run_id = str(lease.get("run_id") or "unknown")
    expires_at = str(lease.get("expires_at") or "unknown")
    age = lease_age_seconds(lease)
    age_text = "unknown" if age is None else f"{age:.0f}s"
    return f"leased elsewhere owner={owner} heartbeat_age={age_text} expires_at={expires_at} run={run_id}"


def acquire_candidate_lease(repo_root: Path, candidate: DispatchCandidate) -> tuple[str, dict[str, object] | None]:
    config = load_repo_spec_runtime_config(repo_root).coordination
    if not config.enabled:
        return candidate.run_id, None
    run_id = candidate.run_id or _candidate_run_id(candidate.spec_id)
    payload = _candidate_lease_payload(repo_root, candidate, run_id=run_id)
    lease = build_coordinator_client(config).acquire_lease(payload)
    return run_id, lease


def start_candidate(
    repo_root: Path,
    candidate: DispatchCandidate,
    *,
    preallocated_run_id: str = "",
) -> ActiveRunProcess:
    state_root = autopilot_runs_root(repo_root)
    state_root.mkdir(parents=True, exist_ok=True)
    log_path = state_root / f"{candidate.spec_id}--{timestamp_token()}.log"
    command = [
        "spec",
        "implement",
        "--spec",
        candidate.spec_id,
        "--agent",
        candidate.agent,
    ]
    if candidate.run_id:
        command += ["--run", candidate.run_id]
    log_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    child_env = os.environ.copy()
    child_env["SPEC_ACTOR"] = "autopilot"
    if preallocated_run_id and not candidate.run_id:
        child_env["SPEC_PREALLOCATED_RUN_ID"] = preallocated_run_id
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=child_env,
        start_new_session=True,
    )
    log_handle.close()
    identity = read_process_identity(process.pid)
    if identity is None:
        raise RuntimeError(
            f"Could not read process identity for pid {process.pid} ({candidate.spec_id}); cannot track child safely.",
        )
    proc = ActiveRunProcess(
        spec_id=candidate.spec_id,
        agent=candidate.agent,
        pid=process.pid,
        started_at=now_iso(),
        started_monotonic=time.monotonic(),
        log_path=str(log_path),
        run_id=candidate.run_id or preallocated_run_id,
        process_started_at=identity.started_at,
    )
    if proc.run_id:
        write_run_log_alias(repo_root, proc.run_id, proc.log_path)
    return proc


def read_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "pid=", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    line = next((item.strip() for item in result.stdout.splitlines() if item.strip()), "")
    if not line:
        return None
    parts = line.split(None, 6)
    if len(parts) != 7:
        return None
    try:
        live_pid = int(parts[0])
    except ValueError:
        return None
    return ProcessIdentity(
        pid=live_pid,
        started_at=" ".join(parts[1:6]),
        command=parts[6].strip(),
    )


def current_process_identity() -> ProcessIdentity:
    identity = read_process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Could not determine autopilot process identity.")
    return identity


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _is_interpreter_token(token: str) -> bool:
    name = PurePosixPath(token).name
    return name.startswith("python") or name in {"uv", "uvx", "pipx"}


def _is_autopilot_run_command(command: str) -> bool:
    """Does this `ps` command line belong to a `spec auto run` dispatcher?

    The dispatcher appears under several spellings depending on how it was
    launched, and every spelling must be recognised: a form we miss makes
    `spec auto stop` mistake a live daemon for a recycled pid and refuse to
    signal it. The console-script form is what pipx and `pip
    install` produce and was the one originally missing.
    """
    # Legacy pre-console-script form: `python scripts/spec_autopilot.py run`.
    if "spec_autopilot.py" in command and re.search(r"(^|\s)run(\s|$)", command):
        return True
    tokens = _command_tokens(command)
    if len(tokens) < 3:
        return False
    # `python -m spec_runtime.autopilot run`
    for index, token in enumerate(tokens[:-2]):
        if token == "-m" and tokens[index + 1] == "spec_runtime.autopilot" and tokens[index + 2] == "run":
            return True
    # Console script: `/path/to/spec auto run ...` or `/path/to/python /path/to/spec auto run ...`.
    # The `spec` token must be argv[0], or argv[1] behind an interpreter — otherwise
    # unrelated commands that merely mention the words (e.g. `grep spec auto run`)
    # would be mistaken for the dispatcher and could be signalled.
    for index in (0, 1):
        if index + 2 >= len(tokens):
            break
        if index == 1 and not _is_interpreter_token(tokens[0]):
            break
        if PurePosixPath(tokens[index]).name != "spec":
            continue
        if tokens[index + 1] == "auto" and tokens[index + 2] == "run":
            return True
    return False


def _read_pid_file(path: Path) -> PidFileRecord | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            return PidFileRecord(pid=int(raw))
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return PidFileRecord(
        pid=pid,
        started_at=str(payload.get("started_at", "")).strip(),
        command=str(payload.get("command", "")).strip(),
    )


def _pid_record_matches_process(record: PidFileRecord, identity: ProcessIdentity) -> bool:
    if record.pid != identity.pid:
        return False
    if not _is_autopilot_run_command(identity.command):
        return False
    if record.started_at and record.started_at != identity.started_at:
        return False
    if record.command and record.command != identity.command:
        return False
    return True


def _pid_record_mismatch_reasons(record: PidFileRecord, identity: ProcessIdentity) -> list[str]:
    """Human-readable list of every field that disagrees between record and process."""
    reasons: list[str] = []
    if record.pid != identity.pid:
        reasons.append(f"pid: recorded {record.pid}, process reports {identity.pid}")
    if not _is_autopilot_run_command(identity.command):
        reasons.append(f"command is not an autopilot run command: {identity.command!r}")
    if record.started_at and record.started_at != identity.started_at:
        reasons.append(f"started_at: recorded {record.started_at!r}, actual {identity.started_at!r}")
    if record.command and record.command != identity.command:
        reasons.append(f"command: recorded {record.command!r}, actual {identity.command!r}")
    return reasons


def read_process_cwd(pid: int) -> Path | None:
    """Working directory of a live process, or None when it cannot be determined.

    Used only to scope process scans to this repository. Callers must treat None
    as "unknown, do not touch" — never as a match.
    """
    if pid <= 0:
        return None
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            candidate = line[1:].strip()
            if candidate:
                try:
                    return Path(candidate).resolve()
                except OSError:
                    return None
    return None


def _cwd_belongs_to_repo(cwd: Path, repo_root: Path) -> bool:
    if cwd == repo_root or repo_root in cwd.parents:
        return True
    # A linked worktree and its main checkout are the same repository: they share
    # one `.spec-state/autopilot` tree, so one pid file covers both. Map each side
    # back to its common root before deciding. A cwd outside any git repo resolves
    # to itself, which cannot collide with ours.
    try:
        return resolve_common_root(cwd) == resolve_common_root(repo_root)
    except (OSError, subprocess.SubprocessError):
        return False


def _process_belongs_to_repo(pid: int, repo_root: Path) -> bool | None:
    """Is ``pid`` running inside this repository? ``None`` means undeterminable.

    ``None`` is not a soft "no" — callers must refuse to signal on it rather than
    fall back to a guess. This host runs one autopilot per repo, so an unproven
    ownership claim is the difference between stopping our dispatcher and killing
    a sibling repo's.
    """
    cwd = read_process_cwd(pid)
    if cwd is None:
        return None
    return _cwd_belongs_to_repo(cwd, repo_root.resolve())


def find_autopilot_processes_for_repo(repo_root: Path) -> list[ProcessIdentity]:
    """Live `spec auto run` processes whose cwd is inside ``repo_root``.

    This host may run one autopilot per repository, so the cwd check is not a
    nicety: a candidate whose cwd cannot be read, or reads as another tree, is
    dropped rather than guessed at. Never returns a process we could not place
    inside this repo.
    """
    repo_root = repo_root.resolve()
    try:
        result = subprocess.run(
            ["ps", "-ww", "-e", "-o", "pid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    own_pid = os.getpid()
    matches: list[ProcessIdentity] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 6)
        if len(parts) != 7:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        command = parts[6].strip()
        if not _is_autopilot_run_command(command):
            continue
        if _process_belongs_to_repo(pid, repo_root) is not True:
            continue
        matches.append(
            ProcessIdentity(pid=pid, started_at=" ".join(parts[1:6]), command=command)
        )
    return matches


def _write_pid_file(path: Path, identity: ProcessIdentity) -> None:
    payload = {
        "pid": identity.pid,
        "started_at": identity.started_at,
        "command": identity.command,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def ensure_pid_file(repo_root: Path) -> None:
    path = autopilot_pid_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        record = _read_pid_file(path)
        if record is not None and record.pid != os.getpid():
            live_identity = read_process_identity(record.pid)
            if live_identity is not None and _pid_record_matches_process(record, live_identity):
                raise RuntimeError(f"Autopilot already running with pid {record.pid}.")
    _write_pid_file(path, current_process_identity())


def remove_pid_file(repo_root: Path) -> None:
    autopilot_pid_path(repo_root).unlink(missing_ok=True)


def run_loop(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    repo_config = load_repo_spec_runtime_config(repo_root)
    backend_policy = resolve_autopilot_backend_policy(repo_config)
    concurrency_policy = compute_autopilot_concurrency(
        repo_config,
        explicit=getattr(args, "concurrency", None),
    )
    backend_error = validate_autopilot_backend(backend_policy, repo_config)
    if backend_error:
        print(format_status_line("error", backend_error), file=sys.stderr)
        return 1
    args.concurrency = concurrency_policy.cap
    ensure_pid_file(repo_root)
    print(
        format_status_line(
            "config",
            (
                f"backend={backend_policy.backend} safety={backend_policy.safety_mode} "
                f"backend_source={backend_policy.source} concurrency={concurrency_policy.cap} "
                f"concurrency_source={concurrency_policy.source}"
            ),
        )
    )
    if backend_policy.backend == "container":
        from .container import container_image_source

        print(
            format_status_line(
                "config",
                (
                    f"container_engine={repo_config.execution.container.engine} "
                    f"worker_source={container_image_source(repo_config, repo_root)}"
                ),
            )
        )

    shutdown_tracker = ShutdownTracker(autopilot_state_root(repo_root))
    reconciled = shutdown_tracker.reconcile_stale()
    if reconciled.phase is ShutdownPhase.COMPLETE and reconciled.requested_at:
        print(
            format_status_line(
                "resume",
                f"reconciled stale shutdown from {reconciled.requested_at}",
            )
        )

    stop_requested = False
    force_shutdown = False
    active = adopt_active_processes(repo_root)
    cleanup_unadopted_container_runs(repo_root, active)
    last_queue_signature: tuple[tuple[str, str, str], ...] = ()
    last_refresh_error = ""
    low_memory_active = False
    lease_backoff_until: dict[str, float] = {}
    dispatch_breaker = DispatchCircuitBreaker()
    breaker_announced: set[str] = set()
    lock_tracker = LockContentionTracker(
        base_backoff=repo_config.autopilot.lock_backoff_base_seconds,
        max_backoff=repo_config.autopilot.lock_backoff_max_seconds,
    )
    operator_grace_announced: set[str] = set()
    stranded_commits_announced: set[str] = set()
    capacity_preflight = ContainerCapacityPreflight(
        recheck_seconds=repo_config.autopilot.container_capacity_recheck_seconds,
        checker=lambda: inspect_container_capacity(
            repo_config,
            threshold=repo_config.autopilot.container_bridge_endpoint_threshold,
            cwd=repo_root,
        ),
    )
    capacity_pause_active = False
    last_capacity_inspection_warning = ""
    source_staleness = SourceStalenessWatch()

    def request_stop(signum: int, _frame) -> None:  # noqa: ANN001
        nonlocal stop_requested, force_shutdown
        state = shutdown_tracker.record_interrupt(reason=f"signal:{signum}")
        stop_requested = True
        if state.phase is ShutdownPhase.FORCED:
            force_shutdown = True
            print(
                format_status_line(
                    "signal",
                    "second interrupt received; forcing shutdown after best-effort cleanup",
                )
            )
        else:
            print(
                format_status_line(
                    "signal",
                    "shutdown requested; waiting for in-flight runs (interrupt again to force)",
                )
            )

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        while True:
            stale_source_warning = source_staleness.check()
            if stale_source_warning:
                print(format_status_line("stale-source", stale_source_warning))
            completed: list[str] = []
            for spec_id, proc in list(active.items()):
                phase = read_latest_run_phase(repo_root, spec_id, proc.run_id)
                if phase:
                    proc.phase = phase
                if is_pid_alive(proc.pid, proc.process_started_at):
                    continue

                tail = read_log_tail(proc.log_path)
                run_record = read_latest_run_record(repo_root, spec_id, run_id=proc.run_id)
                maybe_write_run_log_alias(repo_root, proc, run_record)
                classification = classify_completion(
                    tail,
                    run_record=run_record,
                    expected_run_id=proc.run_id,
                )
                if proc.started_monotonic > 0:
                    duration = round(time.monotonic() - proc.started_monotonic, 3)
                else:
                    try:
                        started = datetime.fromisoformat(proc.started_at)
                        duration = round((datetime.now(UTC) - started).total_seconds(), 3)
                    except (ValueError, TypeError):
                        duration = 0.0
                phase_reached = proc.phase or "unknown"

                if classification not in ("skip", "waiting"):
                    outcome = LoggedOutcome(
                        timestamp=now_iso(),
                        spec_id=spec_id,
                        agent=proc.agent,
                        outcome="success" if classification == "success" else "failure",
                        duration_seconds=duration,
                        phase_reached=phase_reached,
                        exit_code=-1,
                        error_tail=[] if classification == "success" else tail,
                    )
                    append_log(repo_root, outcome)

                if classification == "success":
                    # Forward progress resets the same-error circuit breaker.
                    dispatch_breaker.record_success(spec_id)
                    breaker_announced.discard(spec_id)
                    print(
                        format_status_line(
                            "done",
                            f"{spec_id} outcome=success phase={phase_reached} duration={duration}s",
                        )
                    )
                    if args.notify_success:
                        notify(
                            args.notify_backends,
                            title=AUTOPILOT_TITLE,
                            message=f"{spec_id} completed successfully at phase {phase_reached}.",
                        )
                elif classification == "waiting":
                    print(
                        format_status_line(
                            "wait",
                            f"{spec_id} is waiting for operator intervention. Run: spec input --spec {spec_id}",
                        )
                    )
                    notify(
                        args.notify_backends,
                        title=AUTOPILOT_TITLE,
                        message=(f"{spec_id} is waiting for operator intervention. Run: spec input --spec {spec_id}"),
                    )
                elif classification == "skip":
                    print(
                        format_status_line(
                            "skip",
                            f"{spec_id} reason={tail[-1] if tail else 'state changed'}",
                        )
                    )
                else:
                    # Feed the failure into the same-error circuit breaker so
                    # deterministic, repeated-identical failures back off and
                    # eventually stop being re-dispatched.
                    failure_error = str((run_record or {}).get("last_error", "")).strip()
                    if not failure_error and tail:
                        failure_error = tail[-1]
                    dispatch_breaker.record_failure(
                        spec_id,
                        phase_reached,
                        failure_error,
                        now=time.monotonic(),
                    )
                    print(
                        format_status_line(
                            "done",
                            f"{spec_id} outcome=failure phase={phase_reached} duration={duration}s",
                        )
                    )
                    notify(
                        args.notify_backends,
                        title=AUTOPILOT_TITLE,
                        message=format_failure_notification(spec_id, phase_reached, tail),
                    )
                completed.append(spec_id)

            for spec_id in completed:
                active.pop(spec_id, None)

            write_active_state(repo_root, active)

            if stop_requested:
                if not active or force_shutdown:
                    break
                time.sleep(args.poll_interval)
                continue

            if len(active) < args.concurrency or args.dry_run:
                refresh_ok, refresh_error = refresh_runtime_git_refs(repo_root)
                if not refresh_ok:
                    if refresh_error != last_refresh_error:
                        print(
                            format_status_line(
                                "warning",
                                f"git ref refresh failed; using local refs ({refresh_error})",
                            )
                        )
                    last_refresh_error = refresh_error
                elif last_refresh_error:
                    print(format_status_line("resume", "git ref refresh recovered"))
                    last_refresh_error = ""

                git_state = collect_git_spec_state(repo_root)
                run_index = load_run_record_index(repo_root)
                coordinator_snapshot = fetch_coordinator_lease_snapshot(repo_root)
                full_queue = [
                    candidate
                    for candidate in build_dispatch_queue(
                        repo_root,
                        agent_override=args.agent or "",
                        git_state=git_state,
                        run_index=run_index,
                        coordinator_snapshot=coordinator_snapshot,
                    )
                    if candidate.spec_id not in active
                ]
                capacity_result = capacity_preflight.evaluate(full_queue)
                queue, capacity_paused = apply_container_capacity_gate(full_queue, capacity_result)
                capacity_warning = capacity_result.warning if capacity_result is not None else ""
                saturated = bool(capacity_result is not None and not capacity_result.available)
                if saturated and not capacity_pause_active:
                    print(format_status_line("pause", capacity_warning))
                elif not saturated and capacity_pause_active:
                    print(format_status_line("resume", "container capacity recovered; scheduling resumes"))
                capacity_pause_active = saturated
                inspection_warning = capacity_warning if capacity_result is not None and capacity_result.available else ""
                if inspection_warning and inspection_warning != last_capacity_inspection_warning:
                    print(format_status_line("warning", inspection_warning))
                last_capacity_inspection_warning = inspection_warning
                now_monotonic = time.monotonic()
                leased_count = sum(
                    1
                    for item in queue
                    if item.lease_state == "waiting-remote"
                    or lease_backoff_until.get(item.spec_id, 0.0) > now_monotonic
                )
                unavailable_count = sum(1 for item in queue if item.lease_state == "coordinator-unavailable")
                locked_count = sum(1 for item in queue if item.lock_owner)
                grace_count = sum(1 for item in queue if item.operator_grace)
                stranded_count = sum(1 for item in queue if item.stranded_commits_detail)
                launchable_count = (
                    len(queue) - leased_count - locked_count - grace_count - stranded_count
                )
                signature = tuple(
                    (
                        item.spec_id,
                        item.agent,
                        item.run_id,
                        item.lease_state,
                        item.lease_owner,
                        lease_backoff_until.get(item.spec_id, 0.0) > now_monotonic,
                        item.lock_owner,
                        item.operator_grace,
                        bool(item.stranded_commits_detail),
                    )
                    for item in queue
                )
                if signature != last_queue_signature:
                    preview = ", ".join(f"{item.spec_id}:{item.agent}" for item in queue[:STATUS_QUEUE_PREVIEW])
                    print(
                        format_status_line(
                            "queue",
                            (
                                f"ready={max(0, launchable_count)} leased={leased_count} "
                                f"locked={locked_count} operator_grace={grace_count} "
                                f"needs_attention={stranded_count} "
                                f"unavailable={unavailable_count} capacity_paused={len(capacity_paused)} {preview}"
                            ).rstrip(),
                        )
                    )
                    last_queue_signature = signature

                if args.dry_run:
                    print(
                        render_queue(
                            full_queue,
                            lease_backoff_until=lease_backoff_until,
                            capacity_warning=capacity_warning if saturated else "",
                            lock_backoff_until={
                                item.spec_id: lock_tracker.backoff_until(item.spec_id) for item in full_queue
                            },
                        )
                    )
                    return 0

                free_memory = available_memory_bytes()
                if free_memory is not None and free_memory < LOW_MEMORY_THRESHOLD_BYTES:
                    if not low_memory_active:
                        print(
                            format_status_line(
                                "pause",
                                "available memory below 2 GiB; pausing new launches",
                            )
                        )
                        low_memory_active = True
                    time.sleep(args.poll_interval)
                    continue
                if low_memory_active:
                    print(format_status_line("resume", "memory recovered; scheduling resumes"))
                    low_memory_active = False

                while len(active) < args.concurrency and queue:
                    candidate = queue.pop(0)
                    if lease_backoff_until.get(candidate.spec_id, 0.0) > time.monotonic():
                        continue
                    # Operator grace: a non-autopilot actor (operator resume,
                    # steer, manual phase) touched this run recently. Yield to
                    # them instead of racing for the lock. One log line per
                    # state change.
                    if candidate.operator_grace:
                        if candidate.spec_id not in operator_grace_announced:
                            operator_grace_announced.add(candidate.spec_id)
                            detail = candidate.operator_grace_detail or (
                                f"operator {candidate.operator_actor or 'operator'} active"
                            )
                            print(
                                format_status_line(
                                    "operator-grace",
                                    f"{candidate.spec_id} yielding to operator ({detail})",
                                )
                            )
                        continue
                    operator_grace_announced.discard(candidate.spec_id)
                    # Stranded committed work: the latest run carries
                    # commits ahead of base but cannot be resumed. Dispatching a
                    # fresh run would supersede that work and reimplement from
                    # scratch, so surface needs-attention once and skip instead.
                    if candidate.stranded_commits_detail:
                        if candidate.spec_id not in stranded_commits_announced:
                            stranded_commits_announced.add(candidate.spec_id)
                            print(
                                format_status_line(
                                    "needs-attention",
                                    (
                                        f"{candidate.spec_id} not dispatched: "
                                        f"{candidate.stranded_commits_detail}"
                                    ),
                                )
                            )
                        continue
                    stranded_commits_announced.discard(candidate.spec_id)
                    # Lock contention: the per-spec lock is held elsewhere. Back
                    # off exponentially and surface the owner once rather than
                    # re-launching a doomed child every cycle.
                    lock_now = time.monotonic()
                    if candidate.lock_owner:
                        outcome = lock_tracker.record_locked(
                            candidate.spec_id,
                            str(candidate.lock_owner_pid),
                            candidate.lock_owner,
                            now=lock_now,
                        )
                        if outcome.should_log:
                            print(
                                format_status_line(
                                    "locked",
                                    (
                                        f"{candidate.spec_id} locked by {candidate.lock_owner}; "
                                        f"backing off {outcome.backoff_seconds:.0f}s"
                                    ),
                                )
                            )
                        continue
                    if lock_tracker.record_free(candidate.spec_id):
                        print(
                            format_status_line(
                                "resume",
                                f"{candidate.spec_id} lock released; dispatching",
                            )
                        )
                    # Same-error circuit breaker: suppress dispatch for specs
                    # that keep failing identically. Once tripped, surface the
                    # spec as needs-attention (once) instead of re-dispatching.
                    breaker_now = time.monotonic()
                    if not dispatch_breaker.should_dispatch(candidate.spec_id, breaker_now):
                        if (
                            dispatch_breaker.is_tripped(candidate.spec_id)
                            and candidate.spec_id not in breaker_announced
                        ):
                            breaker_announced.add(candidate.spec_id)
                            detail = dispatch_breaker.failure_detail(candidate.spec_id) or "repeated identical failure"
                            print(
                                format_status_line(
                                    "needs-attention",
                                    (
                                        f"{candidate.spec_id} circuit breaker tripped after "
                                        f"{dispatch_breaker.failure_count(candidate.spec_id)} identical failures; "
                                        f"{detail}"
                                    ),
                                )
                            )
                        continue
                    breaker_announced.discard(candidate.spec_id)
                    if candidate.lease_state == "waiting-remote":
                        lease_backoff_until[candidate.spec_id] = time.monotonic() + max(args.poll_interval * 3, 30)
                        print(
                            format_status_line(
                                "leased",
                                (
                                    f"{candidate.spec_id} leased elsewhere owner={candidate.lease_owner or 'unknown'} "
                                    f"heartbeat_age={candidate.lease_heartbeat_age or 'unknown'} "
                                    f"expires_at={candidate.lease_expires_at or 'unknown'} "
                                    f"run={candidate.lease_run_id or 'unknown'}"
                                ),
                            )
                        )
                        continue
                    if candidate.lease_state == "coordinator-unavailable":
                        lease_message = candidate.lease_message or "unknown"
                        if "spec coord doctor" not in lease_message:
                            lease_message = f"{lease_message}; run `spec coord doctor`"
                        print(
                            format_status_line(
                                "warning",
                                (
                                    f"{candidate.spec_id} lease inspection unavailable; attempting acquire: "
                                    f"{lease_message}"
                                ),
                            )
                        )
                    try:
                        leased_run_id, _lease = acquire_candidate_lease(repo_root, candidate)
                    except CoordinatorLeaseConflictError as exc:
                        lease_backoff_until[candidate.spec_id] = time.monotonic() + max(args.poll_interval * 3, 30)
                        print(
                            format_status_line(
                                "leased",
                                f"{candidate.spec_id} {format_lease_conflict(exc.lease)}",
                            )
                        )
                        continue
                    except CoordinatorError as exc:
                        lease_backoff_until[candidate.spec_id] = time.monotonic() + max(args.poll_interval * 3, 30)
                        print(format_status_line("warning", f"{candidate.spec_id} coordinator unavailable: {exc}"))
                        continue
                    active_run = start_candidate(repo_root, candidate, preallocated_run_id=leased_run_id)
                    active[candidate.spec_id] = active_run
                    write_active_state(repo_root, active)
                    detail = f"{candidate.spec_id} agent={candidate.agent} mode={candidate.reason}"
                    if candidate.run_id:
                        detail += f" run={candidate.run_id}"
                    print(format_status_line("start", detail))

            time.sleep(args.poll_interval)
    finally:
        write_active_state(repo_root, active)
        remove_pid_file(repo_root)
        if stop_requested:
            shutdown_tracker.mark_complete()

    return 0


def _format_elapsed(created_at: str) -> str:
    try:
        started = datetime.fromisoformat(created_at)
        delta = datetime.now(UTC) - started
        total_seconds = int(delta.total_seconds())
    except (ValueError, TypeError):
        return "—"
    if total_seconds < 0:
        return "—"
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def _is_abandoned_scoping_run(data: dict) -> bool:
    """Return True for task runs that failed/blocked during scoping with a generated ID.

    Only matches runs whose spec_id is a generated placeholder (e.g.
    ``task-20260101T010101abc123``). Named task runs that happen to fail at
    bootstrap/scoping are legitimate and must NOT be classified as abandoned.
    """
    spec_id = str(data.get("spec_id", "")).strip()
    phase = str(data.get("phase", "")).strip()
    status = str(data.get("status", "")).strip()
    is_generated_placeholder = bool(ABANDONED_TASK_ID_RE.match(spec_id))
    is_early = phase in ("", "scoping", "bootstrap")
    is_terminal = status in ("failed", "blocked")
    return is_generated_placeholder and is_early and is_terminal


def _is_stale_run(data: dict, state_runs_dir: Path | None = None) -> bool:
    """Detect runs that appear active but are genuinely dead.

    ``run.heartbeat_at`` is NOT a liveness signal: it is not refreshed while a
    phase executes, so any phase that outlasts ``STALE_HEARTBEAT_SECONDS`` --
    a merge waiting on CI, or a test gate on a large suite -- leaves it stale
    on a perfectly healthy run. Judging on it alone could make ``spec gc
    --apply`` flip an in-flight run to ``failed`` while its lease and worktree
    are still live.

    ``lease.json`` is the liveness signal, refreshed by the phase heartbeat
    thread. So check what the reason string has always claimed: the worktree is
    gone *and* the lease is not live. Only then fall back to the heartbeat age.
    """
    status = str(data.get("status", "")).strip()
    if status not in ("running", "pending"):
        return False

    worktree = str(data.get("worktree_path", "")).strip()
    if worktree and Path(worktree).is_dir():
        return False

    run_id = str(data.get("run_id", "")).strip()
    if state_runs_dir is not None and run_id:
        lease = load_run_lease(state_runs_dir, run_id)
        if classify_lease(lease) is LeaseStatus.ACTIVE:
            return False

    heartbeat_raw = (
        str(data.get("heartbeat_at", "")).strip()
        or str(data.get("updated_at", "")).strip()
        or str(data.get("created_at", "")).strip()
    )
    if not heartbeat_raw:
        return False
    try:
        heartbeat_dt = datetime.fromisoformat(heartbeat_raw)
        age_seconds = (datetime.now(UTC) - heartbeat_dt).total_seconds()
    except (ValueError, TypeError):
        return False
    return age_seconds >= STALE_HEARTBEAT_SECONDS


def _merged_pr_for_branch(repo_root: Path, branch: str) -> dict | None:
    """Return merged PR metadata for a branch when GitHub CLI can resolve it."""
    repo_config = load_repo_spec_runtime_config(repo_root)
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--base",
                repo_config.pr_base_branch,
                "--state",
                "merged",
                "--json",
                "number,headRefName,mergeCommit,mergedAt,mergedBy",
                "--jq",
                ".[0] // empty",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        payload = json.loads(result.stdout)
        return payload if isinstance(payload, dict) else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    except json.JSONDecodeError:
        return None


def _branch_has_merged_pr(repo_root: Path, branch: str) -> bool:
    """Check via gh CLI if the branch has a merged PR against the configured PR base."""
    return _merged_pr_for_branch(repo_root, branch) is not None


def _create_merge_tag(repo_root: Path, spec_id: str, branch: str) -> bool:
    """Create and push a spec/merged/ tag for a spec that was merged without one.

    Returns True if both git-tag and git-push succeed.
    """
    tag_name = merge_tag_name(spec_id)
    pr_data = _merged_pr_for_branch(repo_root, branch) or {}

    merge_commit_sha = ""
    merge_commit = pr_data.get("mergeCommit")
    if isinstance(merge_commit, dict):
        merge_commit_sha = str(merge_commit.get("oid", "") or "").strip()
    if not merge_commit_sha:
        print(f"  WARNING: could not resolve merge commit for {tag_name}; gh pr metadata did not include mergeCommit")
        return False

    merged_by = ""
    merged_by_data = pr_data.get("mergedBy")
    if isinstance(merged_by_data, dict):
        merged_by = str(merged_by_data.get("login", "") or "").strip()

    provenance = MergeTagProvenance(
        spec_id=spec_id,
        merge_commit_sha=merge_commit_sha,
        pr_number=pr_data.get("number") if isinstance(pr_data.get("number"), int) else None,
        source_branch=str(pr_data.get("headRefName", "") or "").strip() or branch,
        actor=merged_by or os.getenv("USER") or "autopilot",
        timestamp=str(pr_data.get("mergedAt", "") or "").strip() or utc_timestamp_now(),
    )

    tag_result = subprocess.run(
        annotated_tag_command(tag_name, merge_commit_sha, build_tag_message(provenance)),
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if tag_result.returncode != 0:
        print(f"  WARNING: git tag {tag_name} failed: {tag_result.stderr.strip()}")
        return False
    push_result = subprocess.run(
        push_tag_command(tag_name),
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if push_result.returncode != 0:
        print(f"  WARNING: git push tag {tag_name} failed: {push_result.stderr.strip()}")
        return False
    return True


def _collect_run_rows(
    repo_root: Path,
    active_data: dict,
    *,
    agent_filter: str = "",
    merged_specs: frozenset[str] | None = None,
    run_index: RunRecordIndex | None = None,
    coordinator_snapshot: CoordinatorLeaseSnapshot | None = None,
) -> list[dict]:
    if run_index is None:
        run_index = load_run_record_index(repo_root)

    from spec_runtime.control_plane import CanonicalRunStatus
    from spec_runtime.spec_status import ACTIVE_RUN_STATUSES

    if merged_specs is None:
        merged_specs = frozenset()

    active_spec_ids = set(active_data.keys())
    default_retry_cap = load_repo_spec_runtime_config(repo_root).retry_cap
    metadata_by_id = {metadata.spec_id: metadata for metadata in iter_spec_metadata(repo_root)}
    common_root = resolve_common_root(repo_root)
    state_runs_dir = runs_dir(repo_root)
    rows: list[dict] = []
    seen_specs: set[str] = set()

    for data in run_index.records:
        spec_id = str(data.get("spec_id", "")).strip()
        run_id = str(data.get("run_id", "")).strip()
        status = str(data.get("status", "")).strip()
        if not spec_id:
            continue
        steering = OperatorSteering.load(common_root, run_id) if run_id else None
        has_active_steering = steering is not None and steering.status == "active"
        # Skip terminal/superseded runs that aren't interesting
        if status not in ACTIVE_RUN_STATUSES and status not in TERMINAL_RUN_STATUSES:
            continue
        if status in ACTIVE_RUN_STATUSES:
            pass  # always consider
        elif status in TERMINAL_RUN_STATUSES:
            if spec_id not in merged_specs and not (
                status == "passed" and has_active_steering
            ):
                continue  # abandoned/passed/superseded: skip unless merged
        if str(data.get("superseded_by", "")).strip():
            continue
        agent = str(data.get("agent", "")).strip()
        if agent_filter and agent != agent_filter:
            continue

        # Hide abandoned scoping runs
        if _is_abandoned_scoping_run(data):
            continue

        # Reconcile against merge tags
        run_mode = str(data.get("run_mode", "")).strip()
        if run_mode != "task":
            metadata = metadata_by_id.get(spec_id)
            if metadata and (metadata.superseded_by or metadata.obsolete):
                continue

        if spec_id in merged_specs:
            # Merged specs are shown only in the summary count, not the table
            continue

        # Derive display status through the canonical control-plane projection
        # so ``spec watch`` agrees with ``spec status``/``spec list``/dispatch
        # about lease-expired, stale-process, blocked, and retryable runs. We
        # only override when a durable lease exists so runs that pre-date the
        # lease infrastructure keep their legacy heartbeat-based labels.
        canonical = (
            project_run_record_status(state_runs_dir, data, require_lease=True)
            if state_runs_dir.exists()
            else None
        )
        canonical_status = canonical.status if canonical is not None else None
        if canonical_status is CanonicalRunStatus.STALE:
            tag = "  [stale]"
            display_status = "stale"
        elif canonical_status is CanonicalRunStatus.BLOCKED:
            tag = "  [blocked]"
            display_status = "blocked"
        elif canonical_status is CanonicalRunStatus.RETRYABLE:
            tag = "  [failed]"
            display_status = "failed"
        elif canonical_status is CanonicalRunStatus.NEEDS_INPUT:
            tag = "  [waiting-for-input]"
            display_status = "waiting-for-input"
        elif canonical_status is CanonicalRunStatus.NEEDS_ATTENTION:
            tag = "  [needs-attention]"
            display_status = "needs-attention"
        elif _is_stale_run(data, state_runs_dir):
            tag = "  [stale]"
            display_status = "stale"
        elif status == "failed":
            tag = "  [failed]"
            display_status = status
        elif status == "blocked":
            tag = "  [blocked]"
            display_status = status
        elif status == "waiting-for-input":
            tag = "  [waiting-for-input]"
            display_status = status
        else:
            tag = ""
            display_status = status
        if has_active_steering:
            tag = f"{tag}  [steer]" if tag else "  [steer]"

        owner = str(data.get("requested_by", "")).strip()
        if not owner and spec_id in active_spec_ids:
            active_run_id = str(active_data[spec_id].get("run_id", "")).strip()
            if active_run_id and run_id == active_run_id:
                owner = "autopilot"

        phase = str(data.get("phase", "")).strip()
        if spec_id in active_data:
            active_phase = str(active_data[spec_id].get("phase", "")).strip()
            if active_phase:
                phase = active_phase

        try:
            attempts = int(data.get("attempts", 0))
        except (TypeError, ValueError):
            attempts = 0
        try:
            retry_cap = int(data.get("retry_cap", default_retry_cap))
        except (TypeError, ValueError):
            retry_cap = default_retry_cap

        created_at = str(data.get("created_at", "")).strip()
        is_terminal = display_status in ("failed", "blocked", "stale")
        elapsed = "—" if is_terminal else _format_elapsed(created_at)

        # Prefix spec_id with task/ when run_mode is task
        display_spec_id = f"task/{spec_id}" if run_mode == "task" else spec_id

        lease_view = coordinator_snapshot.leases_by_spec.get(spec_id) if coordinator_snapshot is not None else None
        if lease_view is not None:
            if lease_view.state == "waiting-remote":
                owner = lease_view.owner
                tag = f"{tag}  [lease:remote]" if tag else "  [lease:remote]"
            elif lease_view.state == "expired":
                tag = f"{tag}  [lease:expired]" if tag else "  [lease:expired]"
            elif lease_view.state == "local":
                tag = f"{tag}  [lease:local]" if tag else "  [lease:local]"

        rows.append(
            {
                "spec_id": display_spec_id,
                "agent": agent or "—",
                "phase": phase or "—",
                "retries": format_attempt_progress(attempts, retry_cap),
                "owner": owner or "—",
                "backend": str(data.get("backend", "")).strip() or "—",
                "safety_mode": str(data.get("safety_mode", "")).strip() or "—",
                "backend_source": str(data.get("backend_source", "")).strip(),
                "elapsed": elapsed,
                "lease": lease_view.state if lease_view is not None else "",
                "lease_heartbeat": lease_view.heartbeat_age if lease_view is not None else "",
                "lease_expires": lease_view.expires_at if lease_view is not None else "",
                "tag": tag,
                "status": display_status,
                "created_at": created_at,
                "has_active_steering": has_active_steering,
            }
        )
        seen_specs.add(spec_id)

    if coordinator_snapshot is not None:
        metadata_by_id = {metadata.spec_id: metadata for metadata in iter_spec_metadata(repo_root)}
        for spec_id, lease_view in coordinator_snapshot.leases_by_spec.items():
            if spec_id in seen_specs or spec_id in merged_specs:
                continue
            metadata = metadata_by_id.get(spec_id)
            if metadata is None or metadata.superseded_by or metadata.obsolete:
                continue
            if agent_filter and lease_view.agent and lease_view.agent != agent_filter:
                continue
            if lease_view.state == "waiting-remote":
                display_status = "running"
                tag = "  [lease:remote]"
                phase = "leased"
            elif lease_view.state == "expired":
                display_status = "stale"
                tag = "  [lease:expired]"
                phase = "reclaimable"
            else:
                continue
            rows.append(
                {
                    "spec_id": spec_id,
                    "agent": lease_view.agent or "—",
                    "phase": phase,
                    "retries": "—",
                    "owner": lease_view.owner or "—",
                    "backend": "—",
                    "safety_mode": "—",
                    "backend_source": "",
                    "elapsed": lease_view.heartbeat_age or "—",
                    "lease": lease_view.state,
                    "lease_heartbeat": lease_view.heartbeat_age,
                    "lease_expires": lease_view.expires_at,
                    "tag": tag,
                    "status": display_status,
                    "created_at": "",
                    "has_active_steering": False,
                }
            )

    # Sort: active first (by created_at ascending), then terminal newest-first
    terminal_statuses = ("failed", "blocked", "stale")
    active_rows = [r for r in rows if r["status"] not in terminal_statuses]
    terminal_rows = [r for r in rows if r["status"] in terminal_statuses]
    active_rows.sort(key=lambda r: r.get("created_at", ""))
    terminal_rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    rows = active_rows + terminal_rows
    return rows


def _render_watch_screen(
    run_rows: list[dict],
    queue: list[DispatchCandidate],
    *,
    agent_filter: str = "",
    merged_count: int = 0,
    passed_count: int = 0,
    coordinator_unavailable: str = "",
) -> str:
    non_active_statuses = ("failed", "blocked", "stale")
    active_count = sum(1 for r in run_rows if r["status"] not in non_active_statuses)
    failed_count = sum(1 for r in run_rows if r["status"] in ("failed", "blocked"))
    stale_count = sum(1 for r in run_rows if r["status"] == "stale")
    queued_count = len(queue)

    lines: list[str] = []
    summary_parts = []
    if active_count:
        summary_parts.append(f"{active_count} active")
    if failed_count:
        summary_parts.append(f"{failed_count} failed")
    if merged_count:
        summary_parts.append(f"{merged_count} merged")
    if passed_count:
        summary_parts.append(f"{passed_count} passed")
    if stale_count:
        summary_parts.append(f"{stale_count} stale")
    if queued_count:
        summary_parts.append(f"{queued_count} queued")
    summary = " / ".join(summary_parts) if summary_parts else "idle"

    header = "Spec Runs"
    lines.append(f"{header:<50}{summary:>30}")
    lines.append("─" * 80)
    if coordinator_unavailable:
        lines.append(f"Coordinator unavailable: {coordinator_unavailable}")
        lines.append("─" * 80)

    if run_rows:
        # Compute dynamic column widths from actual data (minimum = header width)
        spec_w = max(len("SPEC"), *(len(r["spec_id"]) for r in run_rows)) + 2
        agent_w = max(len("AGENT"), *(len(r["agent"]) for r in run_rows)) + 2
        phase_w = max(len("PHASE"), *(len(r["phase"]) for r in run_rows)) + 2
        retries_w = max(len("ATTEMPTS"), *(len(r["retries"]) for r in run_rows)) + 2
        owner_w = max(len("OWNER"), *(len(r["owner"]) for r in run_rows)) + 2
        backend_w = max(len("BACKEND"), *(len(r.get("backend") or "—") for r in run_rows)) + 2
        safety_w = max(len("SAFETY"), *(len(r.get("safety_mode") or "—") for r in run_rows)) + 2
        lease_w = max(len("LEASE"), *(len(r.get("lease") or "—") for r in run_rows)) + 2
        heartbeat_w = max(len("HEARTBEAT"), *(len(r.get("lease_heartbeat") or "—") for r in run_rows)) + 2
        expires_w = max(len("EXPIRES"), *(len(r.get("lease_expires") or "—") for r in run_rows)) + 2

        lines.append(
            f"{'SPEC':<{spec_w}}{'AGENT':<{agent_w}}{'PHASE':<{phase_w}}"
            f"{'ATTEMPTS':<{retries_w}}{'OWNER':<{owner_w}}"
            f"{'BACKEND':<{backend_w}}{'SAFETY':<{safety_w}}{'LEASE':<{lease_w}}"
            f"{'HEARTBEAT':<{heartbeat_w}}{'EXPIRES':<{expires_w}}{'ELAPSED'}"
        )
        for row in run_rows:
            lease = row.get("lease") or "—"
            heartbeat = row.get("lease_heartbeat") or "—"
            expires = row.get("lease_expires") or "—"
            backend = row.get("backend") or "—"
            safety = row.get("safety_mode") or "—"
            source = row.get("backend_source") or ""
            backend_tag = f"  [backend:{source}]" if source == "rollout-policy" else ""
            lines.append(
                f"{row['spec_id']:<{spec_w}}{row['agent']:<{agent_w}}"
                f"{row['phase']:<{phase_w}}{row['retries']:<{retries_w}}"
                f"{row['owner']:<{owner_w}}{backend:<{backend_w}}{safety:<{safety_w}}"
                f"{lease:<{lease_w}}{heartbeat:<{heartbeat_w}}{expires:<{expires_w}}"
                f"{row['elapsed']}{row['tag']}{backend_tag}"
            )
    else:
        lines.append("  (no active runs)")

    lines.append("")
    lines.append("Queue")
    lines.append("─" * 80)
    if queue:
        # Compute dynamic column widths for queue table
        q_spec_w = max(len("SPEC"), *(len(item.spec_id) for item in queue)) + 2
        q_agent_w = max(len("AGENT"), *(len(item.agent) for item in queue)) + 2
        q_backend_w = max(len("BACKEND"), *(len(item.backend or "—") for item in queue)) + 2
        q_safety_w = max(len("SAFETY"), *(len(item.safety_mode or "—") for item in queue)) + 2
        q_unlocks_w = max(len("UNLOCKS"), *(len(str(item.unlock_count)) for item in queue)) + 2
        q_priority_w = max(len("PRIORITY"), *(len(str(item.priority)) for item in queue)) + 2

        lines.append(
            f"{'SPEC':<{q_spec_w}}{'AGENT':<{q_agent_w}}{'BACKEND':<{q_backend_w}}"
            f"{'SAFETY':<{q_safety_w}}{'UNLOCKS':<{q_unlocks_w}}{'PRIORITY':<{q_priority_w}}{'STATE'}"
        )
        for item in queue:
            if item.lease_state == "waiting-remote":
                state = (
                    f"waiting-remote owner={item.lease_owner or 'unknown'} "
                    f"heartbeat={item.lease_heartbeat_age or 'unknown'} "
                    f"expires={item.lease_expires_at or 'unknown'}"
                )
            elif item.lease_state == "expired":
                state = (
                    f"reclaimable owner={item.lease_owner or 'unknown'} "
                    f"expired={item.lease_expires_at or 'unknown'}"
                )
            elif item.lease_state == "coordinator-unavailable":
                state = "coordinator-unavailable"
            else:
                state = f"launchable mode={item.reason}"
            if item.backend_source == "rollout-policy":
                state = f"{state} backend_source=rollout-policy"
            lines.append(
                f"{item.spec_id:<{q_spec_w}}{item.agent:<{q_agent_w}}"
                f"{(item.backend or '—'):<{q_backend_w}}{(item.safety_mode or '—'):<{q_safety_w}}"
                f"{item.unlock_count:<{q_unlocks_w}}{item.priority:<{q_priority_w}}"
                f"{state}"
            )
    else:
        lines.append("  (queue empty)")

    return "\n".join(lines)


def watch_command(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    interactive = sys.stdout.isatty() and sys.stdin.isatty()
    interval = args.interval
    agent_filter = args.agent or ""

    ok, error = refresh_runtime_git_refs(repo_root)
    if not ok:
        print(f"warning: {error}", file=sys.stderr)

    def _read_active_data() -> dict:
        active_path = autopilot_active_path(repo_root)
        if active_path.exists():
            try:
                return json.loads(active_path.read_text())
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return {}

    def _render_once() -> str:
        active_data = _read_active_data()
        git_state = collect_git_spec_state(repo_root)
        run_index = load_run_record_index(repo_root)
        coordinator_snapshot = fetch_coordinator_lease_snapshot(repo_root)
        run_rows = _collect_run_rows(
            repo_root,
            active_data,
            agent_filter=agent_filter,
            merged_specs=git_state.merged_specs,
            run_index=run_index,
            coordinator_snapshot=coordinator_snapshot,
        )
        queue = build_dispatch_queue(
            repo_root,
            agent_override=agent_filter,
            git_state=git_state,
            include_needs_intake=True,
            run_index=run_index,
            coordinator_snapshot=coordinator_snapshot,
        )
        passed_count = sum(
            1
            for r in run_rows
            if r["status"] == "passed" and not r.get("has_active_steering", False)
        )
        run_rows = [
            r
            for r in run_rows
            if r["status"] != "passed" or r.get("has_active_steering", False)
        ]
        return _render_watch_screen(
            run_rows,
            queue,
            agent_filter=agent_filter,
            merged_count=len(git_state.merged_specs),
            passed_count=passed_count,
            coordinator_unavailable=coordinator_snapshot.unavailable_message,
        )

    if not interactive:
        print(_render_once())
        return 0
    try:
        from spec_runtime.autopilot_tui.app import AutopilotWatchApp
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Interactive autopilot watch requires the 'textual' and 'rich' "
            "dependencies. Install with: pip install textual rich"
        ) from exc

    app = AutopilotWatchApp(
        repo_root=repo_root,
        refresh_interval=interval,
        agent_filter=agent_filter,
    )
    app.run()
    return 0


def _signal_autopilot(pid: int) -> int:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"Failed to stop autopilot pid {pid}: {exc}", file=sys.stderr)
        print(f"Stop it manually with: kill -TERM {pid}", file=sys.stderr)
        return 1
    print(f"Sent SIGTERM to autopilot pid {pid}.")
    return 0


def _stop_via_repo_scan(repo_root: Path, *, why: str) -> int:
    """Last-resort recovery: find this repo's autopilot without a usable pid file.

    Only processes whose cwd resolves inside ``repo_root`` are eligible, so a
    sibling repository's daemon on the same host is never signalled. Ambiguity
    is reported, never resolved by guessing.
    """
    candidates = find_autopilot_processes_for_repo(repo_root)
    if not candidates:
        # Keep the literal phrase "not running": `spec web`'s dispatch/stop
        # endpoint classifies a benign no-op by matching on it.
        print(
            f"{why} Autopilot is not running (no autopilot process has a working directory in {repo_root}).",
            file=sys.stderr,
        )
        return 1
    if len(candidates) > 1:
        print(f"{why} Multiple autopilot processes found in {repo_root}:", file=sys.stderr)
        for candidate in candidates:
            print(f"  pid {candidate.pid} started {candidate.started_at}: {candidate.command}", file=sys.stderr)
        print("Refusing to guess. Stop the intended one with: kill -TERM <pid>", file=sys.stderr)
        return 1
    found = candidates[0]
    print(
        f"{why} Recovered by scan: pid {found.pid} (started {found.started_at}) "
        f"is running in {repo_root}: {found.command}",
        file=sys.stderr,
    )
    return _signal_autopilot(found.pid)


def stop_command(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo_root)
    pid_path = autopilot_pid_path(repo_root)
    if not pid_path.exists():
        return _stop_via_repo_scan(repo_root, why="No autopilot pid file.")
    record = _read_pid_file(pid_path)
    if record is None:
        return _stop_via_repo_scan(repo_root, why="Autopilot pid file is invalid.")
    live_identity = read_process_identity(record.pid)
    if live_identity is None:
        # The recorded process is gone. Nothing is lost by clearing the record.
        pid_path.unlink(missing_ok=True)
        return _stop_via_repo_scan(
            repo_root,
            why=f"Autopilot pid {record.pid} (recorded start {record.started_at or 'unknown'}) is no longer running.",
        )
    if not _is_autopilot_run_command(live_identity.command):
        # Live, but it is not an autopilot at all — almost certainly a recycled
        # pid. Refuse to signal it, and keep the pid file: deleting it could
        # leave the real daemon without a usable stop record, while a stale
        # record never blocks a fresh `spec auto run` (ensure_pid_file rewrites
        # records it does not recognise).
        print(
            f"Autopilot pid file points at pid {record.pid}, which is live but is not an autopilot process.",
            file=sys.stderr,
        )
        for reason in _pid_record_mismatch_reasons(record, live_identity):
            print(f"  {reason}", file=sys.stderr)
        print("Refusing to signal it. Remove the pid file by hand if it is obsolete.", file=sys.stderr)
        return 1
    # The pid is live and still looks like our dispatcher.
    reasons = _pid_record_mismatch_reasons(record, live_identity)
    if not reasons and record.started_at and record.command:
        # The record carries a full identity and every field matches the live
        # process. That is proof of ownership by itself, so ownership is not
        # re-derived from cwd here: `spec auto run --repo-root X` legitimately
        # runs with a working directory outside the repo, and demanding a cwd
        # match would reintroduce the very "cannot stop my own daemon" failure
        # this path exists to fix.
        return _signal_autopilot(record.pid)

    # Relaxed path. `started_at`/`command` equality is a nice-to-have, not a
    # gate: the pid file is written once at launch, while `spec auto stop` runs
    # whatever version is installed today, so a daemon that outlives an upgrade
    # would otherwise read as permanently "stale".
    #
    # But dropping that check also drops the pid-reuse guard, and "looks like an
    # autopilot" is not enough on a host that runs one autopilot per repo: a
    # stale pid file whose pid has been recycled by a *sibling* repo's dispatcher
    # would otherwise get that dispatcher killed. So ownership must be positively
    # confirmed from the process's working directory before drift is ignored.
    ownership = _process_belongs_to_repo(record.pid, repo_root)
    if ownership is None:
        print(
            f"Autopilot pid file for pid {record.pid} does not match the live process, "
            "and its working directory could not be read to confirm it belongs to this repo.",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  {reason}", file=sys.stderr)
        print(
            f"Refusing to signal an unconfirmed process. Verify it is this repo's dispatcher "
            f"and stop it with: kill -TERM {record.pid}",
            file=sys.stderr,
        )
        return 1
    if not ownership:
        cwd = read_process_cwd(record.pid)
        print(
            f"Autopilot pid file is stale: pid {record.pid} is now an autopilot for a different "
            f"repository (working directory {cwd}, not {repo_root}). Refusing to signal it.",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  {reason}", file=sys.stderr)
        # Deliberately no `kill -TERM` hint here — that pid is someone else's
        # dispatcher. Look for our own instead; the scan is repo-scoped.
        return _stop_via_repo_scan(repo_root, why="Recorded pid belongs to another repository.")
    print(
        f"Autopilot pid file for pid {record.pid} does not match the live process exactly, but the "
        f"process is an autopilot run working in {repo_root}; stopping it anyway.",
        file=sys.stderr,
    )
    for reason in reasons:
        print(f"  {reason}", file=sys.stderr)
    return _signal_autopilot(record.pid)


def gc_command(args: argparse.Namespace) -> int:
    """Reconcile stale run state in .spec-state/runs/*.json."""
    repo_root = resolve_repo_root(args.repo_root)
    apply = args.apply
    common_root = resolve_common_root(repo_root)
    state_root = common_root / SPEC_RUNTIME_CONFIG.paths.state_dir
    runs_dir = state_root / "runs"
    if not runs_dir.exists():
        print("No runs directory found.")
        return 0

    git_state = collect_git_spec_state(repo_root)
    changes: list[str] = []
    active_worktrees: set[Path] = set()

    for path in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        spec_id = str(data.get("spec_id", "")).strip()
        if not spec_id:
            continue
        if str(data.get("superseded_by", "")).strip():
            continue

        status = str(data.get("status", "")).strip()
        modified = False
        reason = ""

        # Condition 1: merge tag exists for spec_id
        if spec_id in git_state.merged_specs and status != "passed":
            data["status"] = "passed"
            data["phase"] = "cleanup"
            reason = f"merge tag exists for {spec_id}"
            modified = True

        # Condition 2: PR was merged but no merge tag (e.g. merged outside orchestrator)
        if not modified and status == "failed":
            branch = str(data.get("branch", "")).strip()
            if branch and _branch_has_merged_pr(repo_root, branch):
                data["status"] = "passed"
                data["phase"] = "cleanup"
                reason = f"PR for {branch} already merged, missing merge tag"
                modified = True
                # Create the merge tag so future lookups work
                if apply:
                    if _create_merge_tag(repo_root, spec_id, branch):
                        reason += "; backfilled merge tag"
                    else:
                        reason += "; merge tag backfill failed"

        # Condition 3: task run failed at scoping with generated ID
        if not modified and _is_abandoned_scoping_run(data) and status != "abandoned":
            data["status"] = "abandoned"
            reason = f"abandoned scoping task {spec_id}"
            modified = True

        # Condition 4: genuinely dead — no worktree, no live lease, stale heartbeat
        if not modified and _is_stale_run(data, runs_dir):
            data["status"] = "failed"
            data["gc_reason"] = "stale heartbeat, no worktree"
            reason = f"stale run {spec_id} (heartbeat expired, no worktree)"
            modified = True

        # Condition 5: the canonical projection (leases + durable gate
        # records) says the run is stale even when heartbeat fields lag —
        # e.g. verify ran all gates but the process died before
        # finalization, leaving status=running on disk.
        if not modified and str(data.get("status", "")).strip() in ("running", "pending"):
            from spec_runtime.control_plane import CanonicalRunStatus as _Canonical

            # require_lease: only a durable lease that has genuinely gone
            # stale may drive the flip. Leaseless records (pre-lease era or
            # freshly created) would otherwise project STALE with "lease
            # unknown" and --apply could fail healthy runs; those stay under
            # condition 4's heartbeat heuristic.
            projection = project_run_record_status(runs_dir, data, require_lease=True)
            if projection is not None and projection.status is _Canonical.STALE:
                data["status"] = "failed"
                data["gc_reason"] = "canonical stale projection"
                reason = f"stale run {spec_id} (canonical projection: no live owner)"
                modified = True

        if modified:
            run_id = str(data.get("run_id", path.stem)).strip()
            action = "would update" if not apply else "updated"
            changes.append(f"  {action} {run_id}: {reason}")
            if apply:
                data["updated_at"] = datetime.now(UTC).isoformat()
                path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

        final_status = str(data.get("status", "")).strip()
        raw_worktree_path = str(data.get("worktree_path", "")).strip()
        if raw_worktree_path and final_status and final_status not in TERMINAL_RUN_STATUSES:
            active_worktrees.add(Path(raw_worktree_path).expanduser().resolve(strict=False))

    for worktree_path in worktree_process_registry.list_registered_worktrees(state_root):
        if worktree_path in active_worktrees:
            continue
        if apply:
            report = worktree_process_registry.reap_registered_processes(
                state_root,
                worktree_path,
                reason="autopilot gc",
            )
            summary_parts = []
            if report.terminated:
                summary_parts.append(f"{len(report.terminated)} terminated")
            if report.stale:
                summary_parts.append(f"{len(report.stale)} stale")
            if report.surviving:
                summary_parts.append(f"{len(report.surviving)} surviving")
            suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""
            changes.append(f"  reaped helper registry {worktree_path}{suffix}")
        else:
            changes.append(f"  would reap helper registry {worktree_path}")

    if changes:
        mode = "Applied" if apply else "Dry-run"
        print(f"{mode} — {len(changes)} change(s):")
        for line in changes:
            print(line)
        if not apply:
            print("\nRe-run with --apply to mutate run records.")
    else:
        print("No reconciliation needed.")
    return 0
