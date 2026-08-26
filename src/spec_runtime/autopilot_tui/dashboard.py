"""Dashboard data models and utility functions.

Extracted from ``app.py`` so that the web API layer and tests can import
these without pulling in the heavy ``rich``/``textual`` TUI dependencies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spec_runtime import autopilot
from spec_runtime import orchestrator as orch
from spec_runtime.config import load_repo_spec_runtime_config, load_spec_runtime_config
from spec_runtime.container import container_image_source
from spec_runtime.spec_metadata import iter_spec_metadata
from spec_runtime.spec_status import (
    LIVE_ACTIVE_RUN_STATUSES,
    collect_git_spec_state,
)

VISIBLE_DASHBOARD_RUN_STATUSES = frozenset(
    {
        "pending",
        "running",
        "failed",
        "blocked",
        "waiting-for-input",
        "passed",
    }
)


SPEC_RUNTIME_CONFIG = load_spec_runtime_config(require=False)
_RUN_ID_TIMESTAMP_RE = re.compile(r"-(\d{8}T\d{6})(\d+)?$")
_LOG_TIMESTAMP_RE = re.compile(r"--(\d{8}T\d{6})Z\.log$")


@dataclass(frozen=True)
class SpecRow:
    spec_id: str
    display_spec_id: str
    agent: str
    phase: str
    retries: str
    elapsed: str
    status: str
    branch: str
    run_id: str
    run_mode: str
    created_at: str
    requires_human_attention: bool = False
    diagnosis_summary: str = ""
    diagnosis_next_action: str = ""
    has_active_steering: bool = False
    steering_summary: str = ""
    lease_owner: str = ""
    lease_status: str = ""
    lease_heartbeat_age: str = ""
    lease_expires_at: str = ""
    lease_message: str = ""
    backend: str = ""
    safety_mode: str = ""
    backend_source: str = ""
    container_engine: str = ""
    worker_source: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    rows: tuple[SpecRow, ...]
    queue: tuple[autopilot.DispatchCandidate, ...]
    merged_count: int
    passed_count: int = 0
    coordinator_unavailable: str = ""

    @property
    def active_count(self) -> int:
        return sum(1 for row in self.rows if row.status == "running")

    @property
    def failed_count(self) -> int:
        return sum(1 for row in self.rows if row.status in {"failed", "blocked", "stale"})

    @property
    def needs_attention_count(self) -> int:
        return sum(1 for row in self.rows if row.requires_human_attention)

    @property
    def queued_count(self) -> int:
        return len(self.queue)


def _state_roots(repo_root: Path) -> tuple[Path, ...]:
    common_root = autopilot.resolve_common_root(repo_root) / SPEC_RUNTIME_CONFIG.paths.state_dir
    local_root = repo_root / SPEC_RUNTIME_CONFIG.paths.state_dir
    unique: list[Path] = []
    for candidate in (common_root, local_root):
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _read_active_data(repo_root: Path) -> dict:
    active_path = autopilot.autopilot_active_path(repo_root)
    if not active_path.exists():
        return {}
    try:
        payload = json.loads(active_path.read_text())
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _row_sort_key(row: SpecRow) -> tuple[int, str, str]:
    status_rank = {
        "running": 0,
        "pending": 1,
        "waiting": 2,
        "blocked": 3,
        "failed": 4,
        "stale": 5,
        "passed": 6,
    }
    base_rank = status_rank.get(row.status, 99)
    if row.requires_human_attention and base_rank >= 2:
        base_rank = 1
    if row.status in {"running", "pending", "waiting"} or row.requires_human_attention:
        created_component = row.created_at
    else:
        created_component = "".join(chr(255 - ord(ch)) for ch in row.created_at)
    return (base_rank, created_component, row.spec_id)


def _latest_row_records(
    repo_root: Path,
    *,
    run_index: autopilot.RunRecordIndex | None = None,
) -> dict[str, dict]:
    if run_index is None:
        run_index = autopilot.load_run_record_index(repo_root)
    metadata_by_id = {metadata.spec_id: metadata for metadata in iter_spec_metadata(repo_root)}
    merged_specs = collect_git_spec_state(repo_root).merged_specs

    if not run_index.records:
        return {}

    grouped: dict[str, dict] = {}
    for data in run_index.records:
        spec_id = str(data.get("spec_id", "")).strip()
        if not spec_id:
            continue
        status = str(data.get("status", "")).strip()
        if status not in VISIBLE_DASHBOARD_RUN_STATUSES:
            continue
        if str(data.get("superseded_by", "")).strip() or status == "superseded":
            continue
        if autopilot._is_abandoned_scoping_run(data):
            continue

        run_mode = str(data.get("run_mode", "")).strip()
        if run_mode != "task":
            metadata = metadata_by_id.get(spec_id)
            if metadata is None:
                continue
            if metadata.superseded_by or metadata.obsolete:
                continue
            if spec_id in merged_specs:
                continue

        current = grouped.get(spec_id)
        if current is None:
            grouped[spec_id] = data
            continue
        candidate_key = (
            str(data.get("created_at", "")).strip(),
            str(data.get("updated_at", "")).strip(),
            str(data.get("run_id", "")).strip(),
        )
        current_key = (
            str(current.get("created_at", "")).strip(),
            str(current.get("updated_at", "")).strip(),
            str(current.get("run_id", "")).strip(),
        )
        if candidate_key > current_key:
            grouped[spec_id] = data
    return grouped


def load_dashboard_snapshot(repo_root: Path, agent_filter: str = "") -> DashboardSnapshot:
    repo_config = load_repo_spec_runtime_config(repo_root)
    default_retry_cap = repo_config.retry_cap
    active_data = _read_active_data(repo_root)
    git_state = collect_git_spec_state(repo_root)
    run_index = autopilot.load_run_record_index(repo_root)
    coordinator_snapshot = autopilot.fetch_coordinator_lease_snapshot(repo_root)
    hidden_specs = autopilot.hidden_spec_ids(repo_root)
    queue = tuple(
        item
        for item in autopilot.build_dispatch_queue(
            repo_root,
            agent_override=agent_filter,
            git_state=git_state,
            include_needs_intake=True,
            run_index=run_index,
            coordinator_snapshot=coordinator_snapshot,
        )
        if item.spec_id not in hidden_specs
    )

    common_root = autopilot.resolve_common_root(repo_root)
    rows: list[SpecRow] = []
    for spec_id, data in _latest_row_records(repo_root, run_index=run_index).items():
        if spec_id in hidden_specs:
            continue
        agent = str(data.get("agent", "")).strip() or "—"
        if agent_filter and agent != agent_filter:
            continue
        status = str(data.get("status", "")).strip()
        phase = str(data.get("phase", "")).strip() or "—"
        run_id = str(data.get("run_id", "")).strip()
        active_process = False
        active_entry = active_data.get(spec_id)
        if isinstance(active_entry, dict):
            active_run_id = str(active_entry.get("run_id", "")).strip()
            active_phase = str(active_entry.get("phase", "")).strip()
            if not active_run_id or active_run_id == run_id:
                active_process = True
                if active_phase:
                    phase = active_phase

        if autopilot._is_stale_run(data):
            display_status = "stale"
        elif status == "running":
            display_status = "running"
        elif status == "pending":
            display_status = "running" if active_process else "pending"
        elif status == "waiting-for-input":
            display_status = "waiting"
        elif status in {"failed", "blocked", "passed"}:
            display_status = status
        else:
            continue

        try:
            attempts = int(data.get("attempts", 0))
        except (TypeError, ValueError):
            attempts = 0
        try:
            retry_cap = int(data.get("retry_cap", default_retry_cap))
        except (TypeError, ValueError):
            retry_cap = default_retry_cap

        created_at = str(data.get("created_at", "")).strip()
        run_mode = str(data.get("run_mode", "")).strip()

        requires_human_attention = False
        diagnosis_summary = ""
        diagnosis_next_action = ""
        has_active_steering = False
        steering_summary = ""
        if run_id and display_status in {"blocked", "failed", "pending", "running"}:
            diagnosis = orch.BlockDiagnosis.load(common_root, run_id)
            if diagnosis is not None and diagnosis.requires_human_attention:
                requires_human_attention = True
                diagnosis_summary = diagnosis.summary
                diagnosis_next_action = diagnosis.next_best_action
        if run_id:
            steering = orch.OperatorSteering.load(common_root, run_id)
            if steering is not None and steering.status == "active":
                has_active_steering = True
                steering_summary = steering.message

        lease_view = coordinator_snapshot.leases_by_spec.get(spec_id)
        lease_owner = ""
        lease_status = ""
        lease_heartbeat_age = ""
        lease_expires_at = ""
        if lease_view is not None:
            lease_owner = lease_view.owner
            lease_status = lease_view.state
            lease_heartbeat_age = lease_view.heartbeat_age
            lease_expires_at = lease_view.expires_at

        backend = str(data.get("backend", "")).strip()
        rows.append(
            SpecRow(
                spec_id=spec_id,
                display_spec_id=f"task/{spec_id}" if run_mode == "task" else spec_id,
                agent=agent,
                phase=phase,
                retries=orch.format_attempt_progress(attempts, retry_cap),
                elapsed=autopilot._format_elapsed(created_at),
                status=display_status,
                branch=str(data.get("branch", "")).strip() or "—",
                run_id=run_id,
                run_mode=run_mode,
                created_at=created_at,
                requires_human_attention=requires_human_attention,
                diagnosis_summary=diagnosis_summary,
                diagnosis_next_action=diagnosis_next_action,
                has_active_steering=has_active_steering,
                steering_summary=steering_summary,
                lease_owner=lease_owner,
                lease_status=lease_status,
                lease_heartbeat_age=lease_heartbeat_age,
                lease_expires_at=lease_expires_at,
                backend=backend,
                safety_mode=str(data.get("safety_mode", "")).strip(),
                backend_source=str(data.get("backend_source", "")).strip(),
                container_engine=repo_config.execution.container.engine if backend == "container" else "",
                worker_source=container_image_source(repo_config, repo_root) if backend == "container" else "",
            )
        )

    visible_spec_ids = {row.spec_id for row in rows}
    metadata_by_id = {metadata.spec_id: metadata for metadata in iter_spec_metadata(repo_root)}
    for spec_id, lease_view in coordinator_snapshot.leases_by_spec.items():
        if spec_id in hidden_specs or spec_id in visible_spec_ids or spec_id in git_state.merged_specs:
            continue
        metadata = metadata_by_id.get(spec_id)
        if metadata is None or metadata.superseded_by or metadata.obsolete:
            continue
        if agent_filter and lease_view.agent and lease_view.agent != agent_filter:
            continue
        if lease_view.state == "waiting-remote":
            status = "running"
            phase = "leased"
        elif lease_view.state == "expired":
            status = "stale"
            phase = "reclaimable"
        else:
            continue
        rows.append(
            SpecRow(
                spec_id=spec_id,
                display_spec_id=spec_id,
                agent=lease_view.agent or "—",
                phase=phase,
                retries="—",
                elapsed=lease_view.heartbeat_age or "—",
                status=status,
                branch="—",
                run_id=lease_view.run_id,
                run_mode="spec",
                created_at="",
                lease_owner=lease_view.owner,
                lease_status=lease_view.state,
                lease_heartbeat_age=lease_view.heartbeat_age,
                lease_expires_at=lease_view.expires_at,
                backend="",
                safety_mode="",
                backend_source="",
            )
        )

    passed_count = sum(1 for r in rows if r.status == "passed" and not r.has_active_steering)
    rows = [r for r in rows if r.status != "passed" or r.has_active_steering]
    rows.sort(key=_row_sort_key)
    return DashboardSnapshot(
        rows=tuple(rows),
        queue=queue,
        merged_count=len(git_state.merged_specs),
        passed_count=passed_count,
        coordinator_unavailable=coordinator_snapshot.unavailable_message,
    )


def _latest_run(repo_root: Path, spec_id: str) -> orch.RunState | None:
    return orch._latest_non_superseded_run(repo_root, spec_id)


def _run_requires_live_guard(run: orch.RunState | None) -> bool:
    if run is None or run.status not in LIVE_ACTIVE_RUN_STATUSES:
        return False
    return not autopilot._is_stale_run(
        {
            "status": run.status,
            "heartbeat_at": run.heartbeat_at,
            "updated_at": run.updated_at,
            "created_at": run.created_at,
        }
    )


def _resolve_live_process_group(
    repo_root: Path,
    spec_id: str,
    *,
    run: orch.RunState | None = None,
) -> tuple[int, str] | None:
    current_run = run or _latest_run(repo_root, spec_id)
    if current_run is not None:
        process_group = orch._resolve_recorded_process_group(repo_root, current_run)
        if process_group is not None:
            leader_pid, leader_started_at = process_group
            if orch.is_pid_alive(leader_pid, leader_started_at):
                return process_group

    active_entry = _read_active_data(repo_root).get(spec_id)
    if not isinstance(active_entry, dict):
        return None

    active_run_id = str(active_entry.get("run_id", "")).strip()
    if current_run is not None and active_run_id and current_run.run_id:
        if active_run_id != current_run.run_id:
            return None

    try:
        leader_pid = int(active_entry.get("pid", 0))
    except (TypeError, ValueError):
        return None
    leader_started_at = str(active_entry.get("process_started_at", "")).strip()
    if leader_pid <= 0 or not leader_started_at:
        return None
    if not orch.is_pid_alive(leader_pid, leader_started_at):
        return None
    return (leader_pid, leader_started_at)


def is_spec_live(repo_root: Path, spec_id: str) -> bool:
    run = _latest_run(repo_root, spec_id)
    if _resolve_live_process_group(repo_root, spec_id, run=run) is not None:
        return True
    return _run_requires_live_guard(run)


def resolve_log_path(repo_root: Path, spec_id: str, *, run_id: str = "") -> Path | None:
    active_data = _read_active_data(repo_root)
    active_entry = active_data.get(spec_id)
    if isinstance(active_entry, dict):
        active_run_id = str(active_entry.get("run_id", "")).strip()
        log_path = str(active_entry.get("log_path", "")).strip()
        if log_path and (not run_id or not active_run_id or active_run_id == run_id):
            candidate = Path(log_path)
            if candidate.exists():
                return candidate

    if run_id:
        alias_path = autopilot.run_log_alias_path(repo_root, run_id)
        try:
            aliased_log_path = alias_path.read_text().strip()
        except OSError:
            aliased_log_path = ""
        if aliased_log_path:
            candidate = Path(aliased_log_path)
            if candidate.exists():
                return candidate

    runs_root = autopilot.autopilot_runs_root(repo_root)
    if not runs_root.exists():
        return None
    candidates = sorted(runs_root.glob(f"{spec_id}--*.log"))
    if not candidates:
        return None
    # When a specific run_id is requested, try to match by the run's
    # timestamp token embedded in the run_id (e.g. "spec-20260401T052036046531"
    # matches log "spec--20260401T052036046531.log").  If there is no exact
    # token match, prefer the candidate whose file activity is closest to the
    # run record's updated_at timestamp. This keeps historical detail views
    # attached to the right run even when autopilot relaunches or retries reuse
    # the same run_id hours later.
    if run_id and len(candidates) > 1:
        # Extract the full timestamp token from the run_id so that multiple
        # runs on the same day are not aliased (previous code only matched
        # YYYYMMDD which conflated same-day runs).
        parts = run_id.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) >= 8:
            ts_token = parts[1]  # e.g. "20260401T052036046531"
            for c in reversed(candidates):
                if ts_token in c.name:
                    return c
        best_candidate = _best_log_candidate_for_run(repo_root, run_id, candidates)
        if best_candidate is not None:
            return best_candidate
    return candidates[-1]


def _run_start_time_from_run_id(run_id: str) -> datetime | None:
    match = _RUN_ID_TIMESTAMP_RE.search(run_id.strip())
    if match is None:
        return None
    base = match.group(1)
    fractional = (match.group(2) or "")[:6].ljust(6, "0")
    try:
        parsed = datetime.strptime(f"{base}{fractional}", "%Y%m%dT%H%M%S%f")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _log_start_time_from_path(path: Path) -> datetime | None:
    match = _LOG_TIMESTAMP_RE.search(path.name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _best_log_candidate_for_run(repo_root: Path, run_id: str, candidates: list[Path]) -> Path | None:
    run_record = autopilot.load_run_record_index(repo_root).by_run_id.get(run_id)
    if not isinstance(run_record, dict):
        return None
    target_time = _run_activity_time_from_record(run_record)
    if target_time is None:
        target_time = _run_start_time_from_run_id(run_id)
    if target_time is None:
        return None

    ranked: list[tuple[float, float, int, Path]] = []
    for candidate in candidates:
        candidate_time = _candidate_activity_time(candidate)
        if candidate_time is None:
            continue
        try:
            candidate_size = candidate.stat().st_size
        except OSError:
            candidate_size = 0
        ranked.append(
            (
                abs((candidate_time - target_time).total_seconds()),
                0.0 if candidate_time <= target_time else 1.0,
                -candidate_size,
                candidate,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[:3])
    return ranked[0][3]


def _candidate_activity_time(path: Path) -> datetime | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _log_start_time_from_path(path)
    return datetime.fromtimestamp(mtime, tz=UTC)


def _run_activity_time_from_record(run_record: dict) -> datetime | None:
    for key in ("updated_at", "heartbeat_at", "created_at"):
        value = str(run_record.get(key, "")).strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            continue
    return None
