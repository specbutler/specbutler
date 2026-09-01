"""Durable gate execution records.

Verify gates record start time, command, cwd, timeout, completion time, exit
status, and log path. The orchestrator already persists rolled-up gate state in
``gate-status.json``; this helper adds an explicit per-attempt record that
distinguishes a timeout from a generic failure and survives across resumes so
``spec status`` and ``spec watch`` can report what ran and what is pending.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ..platform_fs import atomic_write_text


class GateStatus(str, Enum):
    STARTED = "started"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class GateRecord:
    name: str
    status: GateStatus
    command: tuple[str, ...] = ()
    cwd: str = ""
    timeout_seconds: float = 0.0
    started_at: str = ""
    completed_at: str = ""
    exit_status: int | None = None
    log_path: str = ""
    diagnostic: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["command"] = list(self.command)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GateRecord:
        if not isinstance(payload, dict):
            raise TypeError("gate record payload must be a dict")
        try:
            status = GateStatus(str(payload.get("status", "")).strip())
        except ValueError:
            status = GateStatus.STARTED
        command = payload.get("command") or []
        if not isinstance(command, (list, tuple)):
            command = []
        try:
            timeout = float(payload.get("timeout_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            timeout = 0.0
        exit_status = payload.get("exit_status")
        try:
            exit_status_value = int(exit_status) if exit_status is not None else None
        except (TypeError, ValueError):
            exit_status_value = None
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            name=str(payload.get("name", "")).strip(),
            status=status,
            command=tuple(str(item) for item in command),
            cwd=str(payload.get("cwd", "")).strip(),
            timeout_seconds=timeout,
            started_at=str(payload.get("started_at", "")).strip(),
            completed_at=str(payload.get("completed_at", "")).strip(),
            exit_status=exit_status_value,
            log_path=str(payload.get("log_path", "")).strip(),
            diagnostic=str(payload.get("diagnostic", "")).strip(),
            metadata=dict(metadata),
        )

    def is_terminal(self) -> bool:
        return self.status in {GateStatus.PASSED, GateStatus.FAILED, GateStatus.TIMED_OUT, GateStatus.SKIPPED}

    def is_timeout(self) -> bool:
        return self.status is GateStatus.TIMED_OUT


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()


def gate_records_path(state_run_dir: Path) -> Path:
    return Path(state_run_dir) / "gate-records.json"


class GateRecordStore:
    """Append-only durable log of gate execution attempts for a single run."""

    def __init__(self, state_run_dir: Path):
        self._path = gate_records_path(state_run_dir)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[GateRecord]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        items = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        records: list[GateRecord] = []
        for item in items:
            if isinstance(item, dict):
                records.append(GateRecord.from_dict(item))
        return records

    def append(self, record: GateRecord) -> GateRecord:
        records = self.load()
        records.append(record)
        self._write(records)
        return record

    def replace_pending(self, record: GateRecord) -> GateRecord:
        """Replace the latest started record for the same gate with ``record``.

        Used by callers that ``record_gate_started`` then update the entry once
        the gate completes. If no started record exists, behaves like append.
        """

        records = self.load()
        for index in range(len(records) - 1, -1, -1):
            existing = records[index]
            if existing.name == record.name and existing.status is GateStatus.STARTED:
                records[index] = record
                self._write(records)
                return record
        records.append(record)
        self._write(records)
        return record

    def latest_for_gate(self, name: str) -> GateRecord | None:
        for record in reversed(self.load()):
            if record.name == name:
                return record
        return None

    def pending_gates(self) -> tuple[str, ...]:
        seen: dict[str, GateStatus] = {}
        for record in self.load():
            seen[record.name] = record.status
        return tuple(name for name, status in seen.items() if status is GateStatus.STARTED)

    def _write(self, records: Iterable[GateRecord]) -> None:
        payload = {
            "version": 1,
            "updated_at": _now_iso(),
            "records": [record.to_dict() for record in records],
        }
        atomic_write_text(
            self._path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )


def record_gate_started(
    state_run_dir: Path,
    *,
    name: str,
    command: Iterable[str],
    cwd: str,
    timeout_seconds: float,
    log_path: str = "",
    now: datetime | None = None,
) -> GateRecord:
    record = GateRecord(
        name=name,
        status=GateStatus.STARTED,
        command=tuple(str(item) for item in command),
        cwd=str(cwd),
        timeout_seconds=float(timeout_seconds),
        started_at=_now_iso(now),
        log_path=log_path,
    )
    GateRecordStore(state_run_dir).append(record)
    return record


def record_gate_completed(
    state_run_dir: Path,
    *,
    name: str,
    exit_status: int,
    diagnostic: str = "",
    log_path: str = "",
    now: datetime | None = None,
) -> GateRecord:
    store = GateRecordStore(state_run_dir)
    previous = store.latest_for_gate(name)
    started_at = previous.started_at if previous is not None else _now_iso(now)
    command = previous.command if previous is not None else ()
    cwd = previous.cwd if previous is not None else ""
    timeout_seconds = previous.timeout_seconds if previous is not None else 0.0
    log_target = log_path or (previous.log_path if previous is not None else "")
    status = GateStatus.PASSED if exit_status == 0 else GateStatus.FAILED
    record = GateRecord(
        name=name,
        status=status,
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        started_at=started_at,
        completed_at=_now_iso(now),
        exit_status=int(exit_status),
        log_path=log_target,
        diagnostic=diagnostic,
    )
    store.replace_pending(record)
    return record


def record_gate_timeout(
    state_run_dir: Path,
    *,
    name: str,
    diagnostic: str = "",
    log_path: str = "",
    now: datetime | None = None,
) -> GateRecord:
    store = GateRecordStore(state_run_dir)
    previous = store.latest_for_gate(name)
    started_at = previous.started_at if previous is not None else _now_iso(now)
    command = previous.command if previous is not None else ()
    cwd = previous.cwd if previous is not None else ""
    timeout_seconds = previous.timeout_seconds if previous is not None else 0.0
    log_target = log_path or (previous.log_path if previous is not None else "")
    record = GateRecord(
        name=name,
        status=GateStatus.TIMED_OUT,
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        started_at=started_at,
        completed_at=_now_iso(now),
        exit_status=None,
        log_path=log_target,
        diagnostic=diagnostic or f"gate '{name}' exceeded timeout",
    )
    store.replace_pending(record)
    return record
