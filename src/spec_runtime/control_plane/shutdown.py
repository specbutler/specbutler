"""Interrupt and shutdown tracking.

The first interrupt requests a graceful shutdown and records that shutdown has
started. A second interrupt forces best-effort termination and records that the
host moved past graceful cleanup. The state file lets a restarted autopilot
reconcile a previous interrupted shutdown into a clean retry.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from spec_runtime.platform_fs import FileLock


class ShutdownPhase(str, Enum):
    RUNNING = "running"
    GRACEFUL = "graceful"
    FORCED = "forced"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ShutdownState:
    phase: ShutdownPhase
    instance_id: str = ""
    pid: int = 0
    process_started_at: str = ""
    nonce: str = ""
    requested_at: str = ""
    forced_at: str = ""
    completed_at: str = ""
    reason: str = ""
    interrupt_count: int = 0


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()


def shutdown_state_path(state_run_dir: Path) -> Path:
    return Path(state_run_dir) / "shutdown.json"


def _load(state_run_dir: Path) -> ShutdownState:
    path = shutdown_state_path(state_run_dir)
    if not path.exists():
        return ShutdownState(phase=ShutdownPhase.RUNNING)
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return ShutdownState(phase=ShutdownPhase.RUNNING)
    if not isinstance(payload, dict):
        return ShutdownState(phase=ShutdownPhase.RUNNING)
    try:
        phase = ShutdownPhase(str(payload.get("phase", ShutdownPhase.RUNNING.value)))
    except ValueError:
        phase = ShutdownPhase.RUNNING
    try:
        interrupt_count = int(payload.get("interrupt_count", 0) or 0)
    except (TypeError, ValueError):
        interrupt_count = 0
    try:
        pid = int(payload.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    return ShutdownState(
        phase=phase,
        instance_id=str(payload.get("instance_id", "")).strip(),
        pid=pid,
        process_started_at=str(payload.get("process_started_at", "")).strip(),
        nonce=str(payload.get("nonce", "")).strip(),
        requested_at=str(payload.get("requested_at", "")).strip(),
        forced_at=str(payload.get("forced_at", "")).strip(),
        completed_at=str(payload.get("completed_at", "")).strip(),
        reason=str(payload.get("reason", "")).strip(),
        interrupt_count=interrupt_count,
    )


def _save(state_run_dir: Path, state: ShutdownState) -> Path:
    path = shutdown_state_path(state_run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(state)
    payload["phase"] = state.phase.value
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def record_shutdown_initiated(
    state_run_dir: Path,
    *,
    reason: str = "",
    now: datetime | None = None,
) -> ShutdownState:
    """First interrupt: request graceful shutdown.

    A second call escalates to forced shutdown so the host stops waiting on
    cleanup once the operator clearly wants out.
    """

    current = _load(state_run_dir)
    timestamp = _now_iso(now)
    if current.phase is ShutdownPhase.RUNNING:
        new_state = ShutdownState(
            phase=ShutdownPhase.GRACEFUL,
            requested_at=timestamp,
            reason=reason or current.reason,
            interrupt_count=current.interrupt_count + 1,
        )
    elif current.phase is ShutdownPhase.GRACEFUL:
        new_state = ShutdownState(
            phase=ShutdownPhase.FORCED,
            requested_at=current.requested_at,
            forced_at=timestamp,
            reason=reason or current.reason,
            interrupt_count=current.interrupt_count + 1,
        )
    else:
        new_state = ShutdownState(
            phase=current.phase,
            requested_at=current.requested_at,
            forced_at=current.forced_at,
            completed_at=current.completed_at,
            reason=reason or current.reason,
            interrupt_count=current.interrupt_count + 1,
        )
    _save(state_run_dir, new_state)
    return new_state


def record_shutdown_complete(
    state_run_dir: Path,
    *,
    now: datetime | None = None,
) -> ShutdownState:
    current = _load(state_run_dir)
    new_state = ShutdownState(
        phase=ShutdownPhase.COMPLETE,
        requested_at=current.requested_at,
        forced_at=current.forced_at,
        completed_at=_now_iso(now),
        reason=current.reason,
        interrupt_count=current.interrupt_count,
    )
    _save(state_run_dir, new_state)
    return new_state


class ShutdownTracker:
    """In-memory + on-disk tracker for graceful/forced shutdown progression."""

    def __init__(
        self,
        state_run_dir: Path,
        *,
        instance_id: str = "",
        pid: int = 0,
        process_started_at: str = "",
        nonce: str = "",
    ):
        self._state_run_dir = Path(state_run_dir)
        self._instance_id = instance_id
        self._pid = pid
        self._process_started_at = process_started_at
        self._nonce = nonce

    @property
    def _lock_path(self) -> Path:
        return self._state_run_dir / "shutdown.lock"

    def initialize(self) -> ShutdownState:
        """Publish a fresh dispatcher generation before accepting requests."""
        with FileLock(self._lock_path):
            state = ShutdownState(
                phase=ShutdownPhase.RUNNING,
                instance_id=self._instance_id,
                pid=self._pid,
                process_started_at=self._process_started_at,
                nonce=self._nonce,
            )
            _save(self._state_run_dir, state)
            return state

    @property
    def state_path(self) -> Path:
        return shutdown_state_path(self._state_run_dir)

    def state(self) -> ShutdownState:
        state = _load(self._state_run_dir)
        if self._instance_id and state.instance_id != self._instance_id:
            return ShutdownState(phase=ShutdownPhase.RUNNING, instance_id=self._instance_id)
        if self._nonce and state.nonce != self._nonce:
            return ShutdownState(phase=ShutdownPhase.RUNNING, instance_id=self._instance_id)
        return state

    def record_interrupt(self, *, reason: str = "", now: datetime | None = None) -> ShutdownState:
        with FileLock(self._lock_path):
            current = self.state()
            timestamp = _now_iso(now)
            phase = ShutdownPhase.FORCED if current.phase is ShutdownPhase.GRACEFUL else ShutdownPhase.GRACEFUL
            state = ShutdownState(
                phase=phase,
                instance_id=self._instance_id or current.instance_id,
                pid=self._pid or current.pid,
                process_started_at=self._process_started_at or current.process_started_at,
                nonce=self._nonce or current.nonce,
                requested_at=current.requested_at or timestamp,
                forced_at=timestamp if phase is ShutdownPhase.FORCED else current.forced_at,
                reason=reason or current.reason,
                interrupt_count=current.interrupt_count + 1,
            )
            _save(self._state_run_dir, state)
            return state

    def mark_complete(self, *, now: datetime | None = None) -> ShutdownState:
        with FileLock(self._lock_path):
            current = self.state()
            state = ShutdownState(
                phase=ShutdownPhase.COMPLETE,
                instance_id=self._instance_id or current.instance_id,
                pid=self._pid or current.pid,
                process_started_at=self._process_started_at or current.process_started_at,
                nonce=self._nonce or current.nonce,
                requested_at=current.requested_at,
                forced_at=current.forced_at,
                completed_at=_now_iso(now),
                reason=current.reason,
                interrupt_count=current.interrupt_count,
            )
            _save(self._state_run_dir, state)
            return state

    def reconcile_stale(self, *, now: datetime | None = None) -> ShutdownState:
        """If a previous run left a graceful or forced shutdown without a
        completion record, mark it complete so a restart can proceed."""

        current = self.state()
        if current.phase in {ShutdownPhase.GRACEFUL, ShutdownPhase.FORCED}:
            return self.mark_complete(now=now)
        return current

    def is_graceful_requested(self) -> bool:
        return self.state().phase in {ShutdownPhase.GRACEFUL, ShutdownPhase.FORCED, ShutdownPhase.COMPLETE}

    def is_forced(self) -> bool:
        return self.state().phase is ShutdownPhase.FORCED

    def is_complete(self) -> bool:
        return self.state().phase is ShutdownPhase.COMPLETE
