"""Conservative process adoption decisions.

When the orchestrator resumes a run or autopilot restarts, it must validate
that any recorded process or worker still belongs to the expected run before
waiting on it. This helper turns the ambient checks (PID alive, started_at
matches, lease still valid) into an explicit decision that callers can act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .lease import LeaseStatus, RunLease, classify_lease


class AdoptionDecision(str, Enum):
    ADOPT = "adopt"
    NO_PROCESS_RECORDED = "no_process_recorded"
    PROCESS_DEAD = "process_dead"
    PROCESS_MISMATCH = "process_mismatch"
    PROCESS_UNVERIFIED = "process_unverified"
    LEASE_EXPIRED = "lease_expired"
    LEASE_STALE = "lease_stale"
    LEASE_MISSING = "lease_missing"
    SPEC_MISMATCH = "spec_mismatch"
    RUN_MISMATCH = "run_mismatch"


@dataclass(frozen=True)
class ProcessAdoptionOutcome:
    decision: AdoptionDecision
    reason: str = ""
    follow_up: str = ""

    @property
    def should_wait(self) -> bool:
        return self.decision is AdoptionDecision.ADOPT

    @property
    def should_cleanup(self) -> bool:
        return self.decision in {
            AdoptionDecision.PROCESS_DEAD,
            AdoptionDecision.PROCESS_MISMATCH,
            AdoptionDecision.LEASE_EXPIRED,
            AdoptionDecision.LEASE_STALE,
        }

    @property
    def should_retry(self) -> bool:
        return self.decision in {
            AdoptionDecision.NO_PROCESS_RECORDED,
            AdoptionDecision.LEASE_MISSING,
            AdoptionDecision.LEASE_EXPIRED,
            AdoptionDecision.LEASE_STALE,
            AdoptionDecision.PROCESS_UNVERIFIED,
        }


def evaluate_process_adoption(
    *,
    expected_run_id: str,
    expected_spec_id: str,
    lease: RunLease | None,
    recorded_pid: int = 0,
    recorded_process_started_at: str = "",
    process_alive: bool | None = None,
    live_process_started_at: str = "",
    now: datetime | None = None,
) -> ProcessAdoptionOutcome:
    """Decide whether to wait on, cleanup, or retry a recorded process.

    Inputs:
    - ``expected_run_id`` and ``expected_spec_id`` — the run we are resuming.
    - ``lease`` — the durable lease record, if any.
    - ``recorded_pid`` and ``recorded_process_started_at`` — what the prior
      session persisted as the worker process identity.
    - ``process_alive`` and ``live_process_started_at`` — observations about
      whether the recorded PID still exists. ``process_alive`` is ``None`` when
      the caller did not check.

    The lease's own ``process_pid`` / ``process_started_at`` are also checked
    against the recorded and live identities. A divergence here means the
    durable lease and the ``active.json`` entry disagree on which worker
    process owns the run, so adoption must be refused even when the recorded
    PID is alive — the live process is not the lease worker.
    """

    if lease is None:
        if recorded_pid <= 0:
            return ProcessAdoptionOutcome(
                AdoptionDecision.LEASE_MISSING,
                reason="No lease recorded; resume must restart the run.",
            )
        return ProcessAdoptionOutcome(
            AdoptionDecision.LEASE_MISSING,
            reason="Process recorded but no lease; cannot validate ownership.",
            follow_up="cleanup",
        )

    if expected_run_id and lease.run_id and lease.run_id != expected_run_id:
        return ProcessAdoptionOutcome(
            AdoptionDecision.RUN_MISMATCH,
            reason=f"Lease run_id={lease.run_id!r} does not match expected={expected_run_id!r}.",
        )
    if expected_spec_id and lease.spec_id and lease.spec_id != expected_spec_id:
        return ProcessAdoptionOutcome(
            AdoptionDecision.SPEC_MISMATCH,
            reason=f"Lease spec_id={lease.spec_id!r} does not match expected={expected_spec_id!r}.",
        )

    lease_status = classify_lease(lease, now=now, process_alive=process_alive)
    if lease_status is LeaseStatus.EXPIRED:
        return ProcessAdoptionOutcome(
            AdoptionDecision.LEASE_EXPIRED,
            reason="Lease heartbeat exceeded timeout.",
        )
    if lease_status is LeaseStatus.STALE:
        return ProcessAdoptionOutcome(
            AdoptionDecision.LEASE_STALE,
            reason="Lease heartbeat is fresh but recorded process is gone.",
        )
    if lease_status is LeaseStatus.UNKNOWN:
        return ProcessAdoptionOutcome(
            AdoptionDecision.LEASE_MISSING,
            reason="Lease heartbeat missing; cannot validate.",
        )

    if recorded_pid <= 0:
        return ProcessAdoptionOutcome(
            AdoptionDecision.NO_PROCESS_RECORDED,
            reason="Lease is active but no worker PID recorded; restart needed.",
        )

    if lease.process_pid > 0 and lease.process_pid != recorded_pid:
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_MISMATCH,
            reason=(
                f"Lease worker pid={lease.process_pid} does not match recorded pid={recorded_pid}; "
                "the active.json entry refers to a different process than the durable lease."
            ),
        )
    if (
        lease.process_started_at
        and recorded_process_started_at
        and lease.process_started_at != recorded_process_started_at
    ):
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_MISMATCH,
            reason=(
                f"pid={recorded_pid} started_at differs from lease "
                f"(lease={lease.process_started_at!r}, recorded={recorded_process_started_at!r}); "
                "active.json does not match the durable lease worker."
            ),
        )

    if process_alive is False:
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_DEAD,
            reason=f"Recorded pid={recorded_pid} is no longer alive.",
        )

    if (
        recorded_process_started_at
        and live_process_started_at
        and recorded_process_started_at != live_process_started_at
    ):
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_MISMATCH,
            reason=(
                f"pid={recorded_pid} started_at differs (recorded={recorded_process_started_at!r}, "
                f"live={live_process_started_at!r}); pid was likely recycled."
            ),
        )
    if (
        lease.process_started_at
        and live_process_started_at
        and lease.process_started_at != live_process_started_at
    ):
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_MISMATCH,
            reason=(
                f"pid={recorded_pid} started_at differs from lease "
                f"(lease={lease.process_started_at!r}, live={live_process_started_at!r}); "
                "live process is not the lease worker."
            ),
        )

    if process_alive is not True:
        return ProcessAdoptionOutcome(
            AdoptionDecision.PROCESS_UNVERIFIED,
            reason=(
                f"Recorded pid={recorded_pid} liveness was not validated; refusing "
                "to adopt without explicit aliveness check."
            ),
            follow_up="check_process_alive_then_retry",
        )

    return ProcessAdoptionOutcome(AdoptionDecision.ADOPT)
