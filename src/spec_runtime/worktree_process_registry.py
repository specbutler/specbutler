from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .platform_fs import atomic_write_text
from .process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    SupervisionToken,
    inspect_process,
    supervision_boundary_is_inactive,
    terminate,
    terminate_exact_process,
)

PROCESS_TERMINATION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RegisteredProcess:
    name: str
    kind: str
    pid: int
    started_at: str
    command: str = ""
    termination_scope: str = "pid"
    pgid: int = 0
    registered_at: str = ""
    supervision_token: dict[str, object] | None = None


@dataclass(frozen=True)
class ReapReport:
    terminated: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    surviving: tuple[str, ...] = ()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _normalize_worktree_path(worktree_path: Path) -> Path:
    return Path(worktree_path).expanduser().resolve(strict=False)


def _registry_root(state_root: Path) -> Path:
    return state_root / "worktree-processes"


def _registry_filename(worktree_path: Path) -> str:
    normalized = str(_normalize_worktree_path(worktree_path))
    suffix = _normalize_worktree_path(worktree_path).name or "worktree"
    safe_suffix = re.sub(r"[^A-Za-z0-9._-]+", "-", suffix).strip("-") or "worktree"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{safe_suffix}-{digest}.json"


def _registry_path(state_root: Path, worktree_path: Path) -> Path:
    return _registry_root(state_root) / _registry_filename(worktree_path)


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_file_atomically(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_process_identity(pid: int) -> ProcessIdentity | None:
    return inspect_process(pid)


def is_process_alive(pid: int, expected_started_at: str = "") -> bool:
    identity = read_process_identity(pid)
    if identity is None:
        return False
    if expected_started_at and identity.started_at != expected_started_at:
        return False
    return True


def load_registered_processes(state_root: Path, worktree_path: Path) -> list[RegisteredProcess]:
    payload = _read_json_dict(_registry_path(state_root, worktree_path))
    if payload is None:
        return []
    items = payload.get("processes")
    if not isinstance(items, list):
        return []

    loaded: list[RegisteredProcess] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid", 0))
            pgid = int(item.get("pgid", 0))
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        started_at = str(item.get("started_at", "")).strip()
        if not started_at:
            continue
        loaded.append(
            RegisteredProcess(
                name=str(item.get("name", "")).strip() or "process",
                kind=str(item.get("kind", "")).strip() or "process",
                pid=pid,
                started_at=started_at,
                command=str(item.get("command", "")).strip(),
                termination_scope=str(item.get("termination_scope", "pid")).strip() or "pid",
                pgid=pgid if pgid > 0 else 0,
                registered_at=str(item.get("registered_at", "")).strip(),
                supervision_token=(
                    dict(item["supervision_token"])
                    if isinstance(item.get("supervision_token"), dict)
                    else None
                ),
            )
        )
    return loaded


def list_registered_worktrees(state_root: Path) -> list[Path]:
    root = _registry_root(state_root)
    if not root.exists():
        return []

    worktrees: list[Path] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.json")):
        payload = _read_json_dict(path)
        if payload is None:
            continue
        raw_path = str(payload.get("worktree_path", "")).strip()
        if not raw_path:
            continue
        normalized = str(_normalize_worktree_path(Path(raw_path)))
        if normalized in seen:
            continue
        seen.add(normalized)
        worktrees.append(Path(normalized))
    return worktrees


def register_process(
    state_root: Path,
    worktree_path: Path,
    *,
    name: str,
    kind: str,
    pid: int,
    started_at: str,
    command: str = "",
    termination_scope: str = "pid",
    pgid: int = 0,
    supervision_token: SupervisionToken | None = None,
) -> None:
    if pid <= 0 or not started_at.strip():
        return
    register_processes(
        state_root,
        worktree_path,
        (
            RegisteredProcess(
                name=name,
                kind=kind,
                pid=pid,
                started_at=started_at,
                command=command,
                termination_scope=termination_scope,
                pgid=pgid if pgid > 0 else 0,
                supervision_token=(
                    supervision_token.to_dict()
                    if supervision_token is not None
                    else None
                ),
            ),
        ),
    )


def register_processes(
    state_root: Path,
    worktree_path: Path,
    processes: Sequence[RegisteredProcess],
) -> None:
    """Persist one setup manifest's cleanup entries in a single replacement."""
    if not processes:
        return
    seen_identities: set[tuple[int, str]] = set()
    seen_names: set[tuple[str, str]] = set()
    for process in processes:
        if process.pid <= 0 or not process.started_at.strip():
            raise ValueError("registered processes require a positive pid and start identity")
        identity_key = (process.pid, process.started_at.strip())
        name_key = (process.name, process.kind)
        if identity_key in seen_identities or name_key in seen_names:
            raise ValueError("registered process batches require unique identities and names")
        seen_identities.add(identity_key)
        seen_names.add(name_key)

    normalized_worktree = _normalize_worktree_path(worktree_path)
    path = _registry_path(state_root, normalized_worktree)
    entries = load_registered_processes(state_root, normalized_worktree)
    registered_at = _now_iso()
    for process in processes:
        started_at = process.started_at.strip()
        entries = [
            entry
            for entry in entries
            if (entry.pid, entry.started_at) != (process.pid, started_at)
            and (entry.name, entry.kind) != (process.name, process.kind)
        ]
        entries.append(
            RegisteredProcess(
                name=process.name,
                kind=process.kind,
                pid=process.pid,
                started_at=started_at,
                command=process.command,
                termination_scope=process.termination_scope,
                pgid=process.pgid if process.pgid > 0 else 0,
                registered_at=process.registered_at or registered_at,
                supervision_token=process.supervision_token,
            )
        )
    payload = {
        "updated_at": _now_iso(),
        "worktree_path": str(normalized_worktree),
        "processes": [asdict(entry) for entry in entries],
    }
    _write_json_file_atomically(path, payload)


def clear_registered_processes(state_root: Path, worktree_path: Path) -> None:
    _registry_path(state_root, worktree_path).unlink(missing_ok=True)


def prune_dead_processes(state_root: Path, worktree_path: Path) -> tuple[str, ...]:
    normalized_worktree = _normalize_worktree_path(worktree_path)
    path = _registry_path(state_root, normalized_worktree)
    entries = load_registered_processes(state_root, normalized_worktree)
    if not entries:
        path.unlink(missing_ok=True)
        return ()

    alive: list[RegisteredProcess] = []
    removed: list[str] = []
    for entry in entries:
        if entry.supervision_token is not None:
            # The registered payload may exit while other members remain in
            # its owned Job/process group. Reaping must consult the boundary
            # token before this entry can be classified as stale. Conversely,
            # retaining every token forever strands normally completed agents
            # and setup services after their empty Job/group disappears.
            try:
                token = SupervisionToken.from_dict(entry.supervision_token)
            except (KeyError, TypeError, ValueError):
                alive.append(entry)
                continue
            if (
                token.payload.pid != entry.pid
                or token.payload.started_at != entry.started_at
            ):
                alive.append(entry)
                continue
            if (
                not is_process_alive(entry.pid, entry.started_at)
                and supervision_boundary_is_inactive(token)
            ):
                removed.append(
                    f"{entry.name} pid={entry.pid} owned boundary already inactive"
                )
                continue
            alive.append(entry)
            continue
        if is_process_alive(entry.pid, entry.started_at):
            alive.append(entry)
            continue
        removed.append(f"{entry.name} pid={entry.pid} already exited")

    if alive:
        payload = {
            "updated_at": _now_iso(),
            "worktree_path": str(normalized_worktree),
            "processes": [asdict(entry) for entry in alive],
        }
        _write_json_file_atomically(path, payload)
    else:
        path.unlink(missing_ok=True)
    return tuple(removed)


def _wait_for_exit(entry: RegisteredProcess, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_process_alive(entry.pid, entry.started_at):
            return True
        time.sleep(0.1)
    return not is_process_alive(entry.pid, entry.started_at)


def reap_registered_processes(
    state_root: Path,
    worktree_path: Path,
    *,
    reason: str = "",
    timeout_seconds: float = PROCESS_TERMINATION_TIMEOUT_SECONDS,
) -> ReapReport:
    del reason
    normalized_worktree = _normalize_worktree_path(worktree_path)
    path = _registry_path(state_root, normalized_worktree)
    if not path.exists():
        return ReapReport()
    payload = _read_json_dict(path)
    items = payload.get("processes") if payload is not None else None
    recorded_worktree = str(payload.get("worktree_path", "")).strip() if payload else ""
    if (
        payload is None
        or not isinstance(items, list)
        or not recorded_worktree
        or _normalize_worktree_path(Path(recorded_worktree)) != normalized_worktree
    ):
        return ReapReport(
            surviving=(f"process registry for {normalized_worktree} is malformed; refusing cleanup",)
        )
    malformed_supervision_token = any(
        isinstance(item, dict)
        and "supervision_token" in item
        and item["supervision_token"] is not None
        and not isinstance(item["supervision_token"], dict)
        for item in items
    )
    entries = load_registered_processes(state_root, normalized_worktree)
    if len(entries) != len(items) or malformed_supervision_token:
        return ReapReport(
            surviving=(
                f"process registry for {normalized_worktree} contains malformed entries; "
                "refusing cleanup",
            )
        )
    if not entries:
        path.unlink(missing_ok=True)
        return ReapReport()

    terminated: list[str] = []
    stale: list[str] = []
    surviving: list[str] = []
    still_alive: list[RegisteredProcess] = []
    terminated_boundaries: set[tuple[str, int, str]] = set()

    for entry in entries:
        entry_alive = is_process_alive(entry.pid, entry.started_at)
        stopped = False
        if entry.supervision_token is not None:
            try:
                token = SupervisionToken.from_dict(entry.supervision_token)
            except (KeyError, TypeError, ValueError):
                surviving.append(f"{entry.name} pid={entry.pid} has an invalid supervision token")
                still_alive.append(entry)
                continue
            if (
                token.payload.pid != entry.pid
                or token.payload.started_at != entry.started_at
            ):
                surviving.append(
                    f"{entry.name} pid={entry.pid} supervision token does not match "
                    "the registered process"
                )
                still_alive.append(entry)
                continue
            boundary_key = (
                token.token,
                token.identity.pid,
                token.identity.started_at,
            )
            if boundary_key in terminated_boundaries:
                terminated.append(
                    f"{entry.name} pid={entry.pid} owned boundary terminated"
                )
                continue
            if not entry_alive and supervision_boundary_is_inactive(token):
                stale.append(
                    f"{entry.name} pid={entry.pid} owned boundary already inactive"
                )
                continue
            stopped = terminate(token, grace_seconds=timeout_seconds)
            if not stopped and not entry_alive:
                # The boundary may have completed between the preflight proof
                # and the termination attempt. Recheck before preserving a
                # registry entry that would otherwise block worktree cleanup.
                if supervision_boundary_is_inactive(token):
                    stale.append(
                        f"{entry.name} pid={entry.pid} owned boundary became inactive"
                    )
                    continue
                # A dead payload does not prove its complete owned boundary is
                # empty. Preserve the cleanup entry/workspace rather than
                # silently orphaning a worker after its declared service exits.
                surviving.append(
                    f"{entry.name} pid={entry.pid} exited but its owned boundary "
                    "could not be reaped"
                )
                still_alive.append(entry)
                continue
        elif not entry_alive:
            stale.append(f"{entry.name} pid={entry.pid} already exited")
            continue
        elif os.name == "posix" and entry.termination_scope == "pid":
            identity = ProcessIdentity(entry.pid, entry.started_at, command=entry.command)
            stopped = terminate_exact_process(identity, grace_seconds=timeout_seconds)
        elif os.name == "posix" and entry.termination_scope == "pgid":
            identity = ProcessIdentity(entry.pid, entry.started_at, command=entry.command)
            token = SupervisionToken(
                LifetimeMode.RUN_OWNED,
                identity,
                os.getpid(),
                "registry-reaper",
                f"registry-{entry.pid}-{entry.started_at}",
                entry.pgid,
            )
            stopped = terminate(token, grace_seconds=timeout_seconds)
        elif os.name == "posix":
            surviving.append(
                f"{entry.name} pid={entry.pid} has unknown termination scope "
                f"{entry.termination_scope!r}"
            )
            still_alive.append(entry)
            continue
        else:
            surviving.append(f"{entry.name} pid={entry.pid} has no Windows supervision token")
            still_alive.append(entry)
            continue
        if stopped and _wait_for_exit(entry, 1.0):
            terminated.append(f"{entry.name} pid={entry.pid} terminated")
            if entry.supervision_token is not None:
                terminated_boundaries.add(boundary_key)
            continue

        surviving.append(f"{entry.name} pid={entry.pid} survived reap")
        still_alive.append(entry)

    if still_alive:
        payload = {
            "updated_at": _now_iso(),
            "worktree_path": str(normalized_worktree),
            "processes": [asdict(entry) for entry in still_alive],
        }
        _write_json_file_atomically(path, payload)
    else:
        path.unlink(missing_ok=True)

    return ReapReport(
        terminated=tuple(terminated),
        stale=tuple(stale),
        surviving=tuple(surviving),
    )
