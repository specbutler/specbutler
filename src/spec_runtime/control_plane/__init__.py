"""Host-owned control-plane policy helpers.

These helpers implement explicit lease, timeout, gate, adoption, shutdown, and
status projection policies for the orchestrator. They are intentionally small,
side-effect aware, and independently testable so they can be reused by the
worktree backend and a future container backend without re-implementing
host-side rules in each call site.
"""

from .gate_records import (
    GateRecord,
    GateRecordStore,
    GateStatus,
    record_gate_completed,
    record_gate_started,
    record_gate_timeout,
)
from .git_timeouts import (
    DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
    GitFetchOutcome,
    GitFetchOutcomeKind,
    GitFetchTimeoutError,
    classify_git_fetch_failure,
    run_git_fetch_with_timeout,
)
from .lease import (
    DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS,
    LeaseStatus,
    RunLease,
    classify_lease,
    load_run_lease,
    save_run_lease,
)
from .process_adoption import (
    AdoptionDecision,
    ProcessAdoptionOutcome,
    evaluate_process_adoption,
)
from .shutdown import (
    ShutdownPhase,
    ShutdownTracker,
    record_shutdown_complete,
    record_shutdown_initiated,
)
from .status_projection import (
    CanonicalRunStatus,
    RunStatusProjection,
    project_run_status,
)

__all__ = [
    "AdoptionDecision",
    "CanonicalRunStatus",
    "DEFAULT_GIT_FETCH_TIMEOUT_SECONDS",
    "DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS",
    "GateRecord",
    "GateRecordStore",
    "GateStatus",
    "GitFetchOutcome",
    "GitFetchOutcomeKind",
    "GitFetchTimeoutError",
    "LeaseStatus",
    "ProcessAdoptionOutcome",
    "RunLease",
    "RunStatusProjection",
    "ShutdownPhase",
    "ShutdownTracker",
    "classify_git_fetch_failure",
    "classify_lease",
    "evaluate_process_adoption",
    "load_run_lease",
    "project_run_status",
    "record_gate_completed",
    "record_gate_started",
    "record_gate_timeout",
    "record_shutdown_complete",
    "record_shutdown_initiated",
    "run_git_fetch_with_timeout",
    "save_run_lease",
]
