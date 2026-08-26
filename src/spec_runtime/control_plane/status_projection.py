"""Canonical status projection for runs.

``spec status``, ``spec list``, ``spec watch``, and the autopilot dispatcher
should derive their view of a run from the same data so callers do not
disagree about whether work is active, stale, blocked, retryable, or merged.
This helper turns a few primitive inputs (run record, lease, gate records,
mergedness) into a stable :class:`RunStatusProjection`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable

from .gate_records import GateRecord, GateStatus
from .lease import LeaseStatus, RunLease, classify_lease


class CanonicalRunStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    BLOCKED = "blocked"
    RETRYABLE = "retryable"
    # A failed run that must NOT be retried automatically (deterministic /
    # classified non-retryable failure). Surfaces to operators instead of
    # being re-dispatched.
    NEEDS_ATTENTION = "needs-attention"
    MERGED = "merged"
    PASSED = "passed"
    NEEDS_INPUT = "needs-input"
    NOT_STARTED = "not-started"


_BLOCKED_RUN_STATUSES = frozenset({"blocked"})
_NEEDS_INPUT_STATUSES = frozenset({"waiting-for-input"})
_TERMINAL_RUN_STATUSES = frozenset({"passed", "abandoned", "superseded"})
_LIVE_RUN_STATUSES = frozenset({"pending", "running"})
_RETRYABLE_RUN_STATUSES = frozenset({"failed"})


@dataclass(frozen=True)
class RunStatusProjection:
    status: CanonicalRunStatus
    run_status: str = ""
    lease_status: LeaseStatus = LeaseStatus.UNKNOWN
    pending_gates: tuple[str, ...] = ()
    timed_out_gates: tuple[str, ...] = ()
    failed_gates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def is_active(self) -> bool:
        return self.status is CanonicalRunStatus.ACTIVE

    @property
    def is_stale(self) -> bool:
        return self.status is CanonicalRunStatus.STALE


def _gate_summary(records: Iterable[GateRecord]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    latest: dict[str, GateRecord] = {}
    for record in records:
        latest[record.name] = record
    pending = tuple(sorted(name for name, rec in latest.items() if rec.status is GateStatus.STARTED))
    timed_out = tuple(sorted(name for name, rec in latest.items() if rec.status is GateStatus.TIMED_OUT))
    failed = tuple(sorted(name for name, rec in latest.items() if rec.status is GateStatus.FAILED))
    return pending, timed_out, failed


def project_run_status(
    *,
    run_status: str,
    lease: RunLease | None,
    process_alive: bool | None = None,
    gate_records: Iterable[GateRecord] | None = None,
    is_merged: bool = False,
    now: datetime | None = None,
    retryable_hint: bool | None = None,
    retryable_detail: str = "",
    operator_request_state: str = "",
) -> RunStatusProjection:
    """Project a single canonical view from the inputs callers already have.

    ``retryable_hint`` carries the failure classification persisted on the run
    record: ``False`` means the latest failure is non-retryable, so a failed
    run projects as :attr:`CanonicalRunStatus.NEEDS_ATTENTION` (surfaced to
    operators) instead of :attr:`CanonicalRunStatus.RETRYABLE`. ``None``
    (pre-existing records without the field) preserves today's retryable
    behavior. ``retryable_detail`` is the original error, carried into the
    projection detail so operators see why it needs attention.
    """

    normalized_run_status = (run_status or "").strip().lower()
    pending_gates, timed_out_gates, failed_gates = _gate_summary(gate_records or [])
    lease_status = classify_lease(lease, now=now, process_alive=process_alive)
    warnings: list[str] = []

    if is_merged:
        return RunStatusProjection(
            status=CanonicalRunStatus.MERGED,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            detail="spec merged",
        )

    if normalized_run_status in _TERMINAL_RUN_STATUSES:
        if lease_status is LeaseStatus.ACTIVE and lease is not None:
            warnings.append("terminal run still has a live lease")
        return RunStatusProjection(
            status=CanonicalRunStatus.PASSED if normalized_run_status == "passed" else CanonicalRunStatus.NOT_STARTED,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            warnings=tuple(warnings),
            detail=f"run terminal: {normalized_run_status}" if normalized_run_status else "",
        )

    if normalized_run_status in _NEEDS_INPUT_STATUSES:
        if operator_request_state.strip().lower() in ("consumed", "resolved"):
            # The request was already answered/consumed but the run record
            # never transitioned (process died mid-flight): nothing is left
            # for the operator to answer, so surface it as retryable instead
            # of waiting forever.
            return RunStatusProjection(
                status=CanonicalRunStatus.RETRYABLE,
                run_status=normalized_run_status,
                lease_status=lease_status,
                pending_gates=pending_gates,
                timed_out_gates=timed_out_gates,
                failed_gates=failed_gates,
                detail="operator request already consumed; run record lagged — resumable",
            )
        return RunStatusProjection(
            status=CanonicalRunStatus.NEEDS_INPUT,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            detail="run waiting for input",
        )

    if normalized_run_status in _BLOCKED_RUN_STATUSES:
        return RunStatusProjection(
            status=CanonicalRunStatus.BLOCKED,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            detail="run blocked",
        )

    if normalized_run_status in _RETRYABLE_RUN_STATUSES:
        if retryable_hint is False:
            return RunStatusProjection(
                status=CanonicalRunStatus.NEEDS_ATTENTION,
                run_status=normalized_run_status,
                lease_status=lease_status,
                pending_gates=pending_gates,
                timed_out_gates=timed_out_gates,
                failed_gates=failed_gates,
                detail=(retryable_detail.strip() or "run failed; not retryable"),
            )
        return RunStatusProjection(
            status=CanonicalRunStatus.RETRYABLE,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            detail="run failed; retry available",
        )

    if normalized_run_status in _LIVE_RUN_STATUSES:
        if lease_status is LeaseStatus.ACTIVE:
            return RunStatusProjection(
                status=CanonicalRunStatus.ACTIVE,
                run_status=normalized_run_status,
                lease_status=lease_status,
                pending_gates=pending_gates,
                timed_out_gates=timed_out_gates,
                failed_gates=failed_gates,
                detail="lease active",
            )
        if lease_status in {LeaseStatus.STALE, LeaseStatus.EXPIRED}:
            return RunStatusProjection(
                status=CanonicalRunStatus.STALE,
                run_status=normalized_run_status,
                lease_status=lease_status,
                pending_gates=pending_gates,
                timed_out_gates=timed_out_gates,
                failed_gates=failed_gates,
                detail=(
                    "lease expired" if lease_status is LeaseStatus.EXPIRED else "lease stale: process gone"
                ),
            )
        warnings.append("run shows live status but no lease heartbeat")
        return RunStatusProjection(
            status=CanonicalRunStatus.STALE,
            run_status=normalized_run_status,
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            warnings=tuple(warnings),
            detail="run live but lease unknown",
        )

    if not normalized_run_status:
        return RunStatusProjection(
            status=CanonicalRunStatus.NOT_STARTED,
            run_status="",
            lease_status=lease_status,
            pending_gates=pending_gates,
            timed_out_gates=timed_out_gates,
            failed_gates=failed_gates,
            detail="no run record",
        )

    warnings.append(f"unrecognized run status {normalized_run_status!r}")
    return RunStatusProjection(
        status=CanonicalRunStatus.NOT_STARTED,
        run_status=normalized_run_status,
        lease_status=lease_status,
        pending_gates=pending_gates,
        timed_out_gates=timed_out_gates,
        failed_gates=failed_gates,
        warnings=tuple(warnings),
    )
