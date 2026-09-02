"""Durable run leases for host orchestration.

A lease records who owns a run, where it started, when it last heartbeat, and
how long the host should wait before treating the lease as stale. Leases live
alongside the run state file so the orchestrator can validate ownership before
adopting or waiting on active work.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from ..platform_fs import atomic_write_text

DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS = 600.0


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunLease:
    run_id: str
    spec_id: str
    phase: str
    backend: str
    worker_id: str
    owner_host: str
    started_at: str
    heartbeat_at: str
    timeout_seconds: float = DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS
    process_pid: int = 0
    process_started_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timeout_seconds"] = float(self.timeout_seconds)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunLease:
        if not isinstance(payload, dict):
            raise TypeError("lease payload must be a dict")
        try:
            timeout_seconds = float(payload.get("timeout_seconds", DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS
        try:
            pid = int(payload.get("process_pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            run_id=str(payload.get("run_id", "")).strip(),
            spec_id=str(payload.get("spec_id", "")).strip(),
            phase=str(payload.get("phase", "")).strip(),
            backend=str(payload.get("backend", "")).strip() or "worktree",
            worker_id=str(payload.get("worker_id", "")).strip(),
            owner_host=str(payload.get("owner_host", "")).strip(),
            started_at=str(payload.get("started_at", "")).strip(),
            heartbeat_at=str(payload.get("heartbeat_at", "")).strip(),
            timeout_seconds=timeout_seconds,
            process_pid=pid if pid > 0 else 0,
            process_started_at=str(payload.get("process_started_at", "")).strip(),
            metadata=dict(metadata),
        )

    def with_heartbeat(self, *, now: datetime | None = None) -> RunLease:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
        return replace(self, heartbeat_at=timestamp)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def classify_lease(
    lease: RunLease | None,
    *,
    now: datetime | None = None,
    process_alive: bool | None = None,
) -> LeaseStatus:
    """Classify a lease as active, stale, or expired.

    A lease is *expired* when the configured heartbeat timeout has elapsed.
    A lease is *stale* when the recorded process is known to be dead even if
    the heartbeat is still recent. A lease without a heartbeat is *unknown*
    so callers can treat it conservatively rather than mistaking it for live.
    """

    if lease is None:
        return LeaseStatus.UNKNOWN

    heartbeat = _parse_iso(lease.heartbeat_at)
    if heartbeat is None:
        return LeaseStatus.UNKNOWN

    current = (now or datetime.now(UTC)).astimezone(UTC)
    timeout = max(float(lease.timeout_seconds), 1.0)
    if current - heartbeat > timedelta(seconds=timeout):
        return LeaseStatus.EXPIRED

    if process_alive is False:
        return LeaseStatus.STALE

    return LeaseStatus.ACTIVE


def lease_path_for_run(state_runs_dir: Path, run_id: str) -> Path:
    # Stored inside the per-run state subdirectory so it is not mistaken for a
    # run-state record by ``runs_dir.glob("*.json")`` discovery.
    return Path(state_runs_dir) / run_id / "lease.json"


def load_run_lease(state_runs_dir: Path, run_id: str) -> RunLease | None:
    path = lease_path_for_run(state_runs_dir, run_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return RunLease.from_dict(payload)
    except TypeError:
        return None


def save_run_lease(state_runs_dir: Path, lease: RunLease) -> Path:
    if not lease.run_id:
        raise ValueError("lease.run_id must be set to save a lease")
    path = lease_path_for_run(state_runs_dir, lease.run_id)
    atomic_write_text(
        path,
        json.dumps(lease.to_dict(), indent=2, sort_keys=True) + "\n",
    )
    return path


def build_lease(
    *,
    run_id: str,
    spec_id: str,
    phase: str,
    backend: str = "worktree",
    worker_id: str = "",
    owner_host: str = "",
    timeout_seconds: float = DEFAULT_LEASE_HEARTBEAT_TIMEOUT_SECONDS,
    process_pid: int = 0,
    process_started_at: str = "",
    actor: str = "",
    now: datetime | None = None,
) -> RunLease:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
    metadata: dict[str, Any] = {}
    if actor:
        metadata["actor"] = actor
    return RunLease(
        run_id=run_id,
        spec_id=spec_id,
        phase=phase,
        backend=backend,
        worker_id=worker_id or f"pid-{os.getpid()}",
        owner_host=owner_host or socket.gethostname(),
        started_at=timestamp,
        heartbeat_at=timestamp,
        timeout_seconds=float(timeout_seconds),
        process_pid=process_pid,
        process_started_at=process_started_at,
        metadata=metadata,
    )


def lease_actor(lease: RunLease | None) -> str:
    """Return the recorded actor for a lease (empty string when unknown)."""
    if lease is None:
        return ""
    value = lease.metadata.get("actor") if isinstance(lease.metadata, dict) else ""
    return str(value or "").strip()
