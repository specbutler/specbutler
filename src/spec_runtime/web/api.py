"""/api/v1 route handlers for the spec web server."""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessSupervisor,
    SupervisionToken,
)


def _repo_root(request: Request) -> Path:
    return request.app.state.repo_root


def _started_processes(request: Request) -> dict[str, subprocess.Popen]:
    """Return the process registry owned by this web application instance.

    The registry must not be module-global: test clients, embedded apps, and
    sequential server instances can otherwise inherit stale ``Popen`` objects
    and signal an unrelated process whose PID has since been reused.
    ``create_app`` initializes the state, but the lazy fallback keeps direct
    route tests and third-party Starlette embeddings compatible.
    """
    registry = getattr(request.app.state, "web_started_procs", None)
    if registry is None:
        registry = {}
        request.app.state.web_started_procs = registry
    return registry


def _spec_lifecycle_lock(request: Request, spec_id: str) -> asyncio.Lock:
    """Return the per-app lock serializing one spec's Start and Stop."""
    locks = getattr(request.app.state, "web_spec_lifecycle_locks", None)
    if locks is None:
        locks = {}
        request.app.state.web_spec_lifecycle_locks = locks
    return locks.setdefault(spec_id, asyncio.Lock())


def _remove_started_process(
    started_processes: dict,
    spec_id: str,
    process: object,
) -> None:
    """Drop a registry entry only when it still names this exact launch."""
    if started_processes.get(spec_id) is process:
        started_processes.pop(spec_id, None)


def _tracked_process_matches_group(process: object, pg: tuple[int, str]) -> bool:
    """Compare both PID and creation identity when the handle exposes one."""
    if getattr(process, "pid", None) != pg[0]:
        return False
    token = getattr(process, "token", None)
    identity = getattr(token, "identity", None)
    started_at = getattr(identity, "started_at", "")
    if not isinstance(started_at, str) or started_at in {"", "test-double"}:
        return True
    return not pg[1] or started_at == pg[1]


def _persisted_run_matches_tracked_process(
    run_state: dict,
    process: object,
) -> bool:
    """Correlate late-published run state to one exact retained launch."""
    raw_token = run_state.get("supervision_token")
    process_token = getattr(process, "token", None)
    if not isinstance(raw_token, dict) or process_token is None:
        return False

    def identity_pair(value: object) -> tuple[int, str] | None:
        if isinstance(value, dict):
            pid = value.get("pid")
            started_at = value.get("started_at")
        else:
            pid = getattr(value, "pid", None)
            started_at = getattr(value, "started_at", None)
        if not isinstance(pid, int) or not isinstance(started_at, str) or not started_at:
            return None
        return pid, started_at

    persisted_identity = identity_pair(raw_token.get("identity"))
    persisted_payload = identity_pair(
        raw_token.get("payload_identity") or raw_token.get("payload")
    )
    tracked_identity = identity_pair(getattr(process_token, "identity", None))
    tracked_payload = identity_pair(getattr(process_token, "payload", None))
    if tracked_payload is None:
        tracked_payload = identity_pair(
            getattr(process_token, "payload_identity", None)
        )
    if not {persisted_identity, persisted_payload} & {
        tracked_identity,
        tracked_payload,
    }:
        return False
    persisted_pgid = run_state.get("pgid")
    tracked_pgid = getattr(process_token, "pgid", 0)
    return not (
        isinstance(persisted_pgid, int)
        and persisted_pgid > 0
        and isinstance(tracked_pgid, int)
        and tracked_pgid > 0
        and persisted_pgid != tracked_pgid
    )


def _terminalize_matching_run(
    repo_root: Path,
    spec_id: str,
    run_id: str,
    *,
    expected_process: object | None = None,
) -> dict | None:
    """Mark only the exact web-launched running record stopped."""
    if not run_id:
        return None
    from spec_runtime.orchestrator import (
        _locked_state_path,
        _now_iso,
        _read_json_dict,
        _run_state_path,
        _write_json_file_atomically,
    )

    path = _run_state_path(repo_root, run_id)
    with _locked_state_path(path):
        payload = _read_json_dict(path)
        if payload is None or payload.get("spec_id") != spec_id:
            return None
        if (
            expected_process is not None
            and isinstance(getattr(expected_process, "token", None), SupervisionToken)
            and not _persisted_run_matches_tracked_process(
                payload,
                expected_process,
            )
        ):
            return None
        if payload.get("status") in {"running", "in_progress"}:
            payload["status"] = "failed"
            payload["last_error"] = "stopped by user"
            payload["updated_at"] = _now_iso()
            _write_json_file_atomically(path, payload)
        return payload


def _spec_executable() -> str:
    """Resolve the ``spec`` console-script path.

    Prefer the executable installed alongside the interpreter running this
    server so action endpoints work even when the server was launched without
    ``~/.local/bin`` on ``PATH`` (e.g. under systemd or a bare daemon).  Fall
    back to a ``PATH`` lookup, then to the bare name.
    """
    candidate = Path(sys.executable).parent / "spec"
    if candidate.exists():
        return str(candidate)
    return shutil.which("spec") or "spec"



def _json(data: object, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _valid_spec_id(spec_id: str) -> bool:
    """Return whether a URL identifier is safe and canonical."""
    from spec_runtime.spec_identity import SPEC_ID_RE

    return bool(SPEC_ID_RE.fullmatch(spec_id))


_MARKDOWN_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_MARKDOWN_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "img": {"alt", "src", "title"},
    "ol": {"start"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan", "scope"},
}
_MARKDOWN_URL_SCHEMES = {"http", "https", "mailto"}
_URL_ATTRIBUTES = {"href", "src"}


def _sanitize_url_attribute(value: str) -> str | None:
    """Allow relative URLs and an explicit set of absolute URL schemes.

    URL parsers discard or decode more syntax than a string-prefix check. Run
    entity and percent decoding to a fixed point, remove ASCII whitespace and
    controls for scheme classification, and fail closed on replacement
    characters emitted for malformed/NUL input. The original value is returned
    only after the normalized scheme passes the allowlist.
    """
    normalized = value
    for _ in range(8):
        decoded = html_mod.unescape(unquote(normalized))
        if decoded == normalized:
            break
        normalized = decoded
    if "\ufffd" in normalized:
        return None
    compact = "".join(
        character
        for character in normalized
        if ord(character) > 0x20 and ord(character) != 0x7F
    )
    try:
        scheme = urlsplit(compact).scheme.lower()
    except ValueError:
        return None
    if scheme and scheme not in _MARKDOWN_URL_SCHEMES:
        return None
    return value


def _markdown_attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    del tag
    if attribute in _URL_ATTRIBUTES:
        return _sanitize_url_attribute(value)
    return value


def _render_markdown(text: str) -> str:
    """Render Markdown and sanitize its complete parsed HTML output.

    Raw HTML in the source is entity-escaped *before* markdown processing so
    that only Markdown-generated tags should appear. The parsed-output
    sanitizer is still the authoritative boundary: it allowlists tags,
    attributes, and URL schemes and adds ``noopener noreferrer`` to links.
    """
    import markdown as md
    import nh3

    safe_text = html_mod.escape(text)
    rendered = md.markdown(safe_text, extensions=["tables", "fenced_code"])
    return nh3.clean(
        rendered,
        tags=_MARKDOWN_TAGS,
        clean_content_tags={"script", "style"},
        attributes=_MARKDOWN_ATTRIBUTES,
        attribute_filter=_markdown_attribute_filter,
        url_schemes=_MARKDOWN_URL_SCHEMES,
        url_relative="pass_through",
        link_rel="noopener noreferrer",
    )


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


async def list_specs(request: Request) -> Response:
    repo_root = _repo_root(request)
    from spec_runtime.config import load_repo_spec_runtime_config
    from spec_runtime.spec_metadata import iter_spec_metadata
    from spec_runtime.spec_status import collect_git_spec_state, get_spec_status

    config = load_repo_spec_runtime_config(repo_root)
    git_state = collect_git_spec_state(repo_root)
    records = list(iter_spec_metadata(repo_root))

    specs = []
    for record in records:
        if record.obsolete:
            status = "obsolete"
        elif record.superseded_by:
            status = "superseded"
        else:
            status = get_spec_status(
                repo_root,
                record.spec_id,
                repo_root / config.paths.specs_dir / f"{record.spec_id}.md",
                git_state=git_state,
            )
        specs.append({
            "spec_id": record.spec_id,
            "status": status,
            "area": record.area,
            "priority": record.priority,
            "depends_on": list(record.depends_on),
            "description": record.description,
        })

    return _json(specs)


async def get_spec(request: Request) -> Response:
    spec_id = request.path_params["spec_id"]
    if not _valid_spec_id(spec_id):
        return _json({"error": "Invalid spec ID"}, 400)
    repo_root = _repo_root(request)
    from spec_runtime.config import load_repo_spec_runtime_config
    from spec_runtime.spec_metadata import iter_spec_metadata
    from spec_runtime.spec_status import collect_git_spec_state, get_spec_status

    config = load_repo_spec_runtime_config(repo_root)
    spec_path = repo_root / config.paths.specs_dir / f"{spec_id}.md"
    if not spec_path.exists():
        return _json({"error": f"Spec not found: {spec_id}"}, 404)

    metadata = None
    for record in iter_spec_metadata(repo_root):
        if record.spec_id == spec_id:
            metadata = record
            break

    if metadata is None:
        return _json({"error": f"Spec not found: {spec_id}"}, 404)

    git_state = collect_git_spec_state(repo_root)
    if metadata.obsolete:
        status = "obsolete"
    elif metadata.superseded_by:
        status = "superseded"
    else:
        status = get_spec_status(repo_root, spec_id, spec_path, git_state=git_state)

    from spec_runtime.autopilot import _format_elapsed, load_run_record_index

    run_index = load_run_record_index(repo_root)
    latest_run = run_index.latest_by_spec.get(spec_id)

    # Collect all runs for this spec (run history)
    all_runs = []
    for data in run_index.records:
        if str(data.get("spec_id", "")).strip() == spec_id:
            created_at = data.get("created_at", "")
            all_runs.append({
                "run_id": data.get("run_id", ""),
                "phase": data.get("phase", ""),
                "status": data.get("status", ""),
                "agent": data.get("agent", ""),
                "attempts": data.get("attempts", 0),
                "created_at": created_at,
                "updated_at": data.get("updated_at", ""),
                "elapsed": _format_elapsed(created_at),
            })
    # Sort by created_at descending (newest first)
    all_runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    return _json({
        "spec_id": metadata.spec_id,
        "status": status,
        "area": metadata.area,
        "priority": metadata.priority,
        "depends_on": list(metadata.depends_on),
        "description": metadata.description,
        "body": metadata.body,
        "body_html": _render_markdown(metadata.body) if metadata.body else "",
        "latest_run": latest_run,
        "runs": all_runs,
    })


async def list_runs(request: Request) -> Response:
    repo_root = _repo_root(request)
    from spec_runtime.autopilot import _format_elapsed, load_run_record_index

    run_index = load_run_record_index(repo_root)
    runs = []
    for data in run_index.records:
        created_at = data.get("created_at", "")
        runs.append({
            "run_id": data.get("run_id", ""),
            "spec_id": data.get("spec_id", ""),
            "phase": data.get("phase", ""),
            "status": data.get("status", ""),
            "agent": data.get("agent", ""),
            "attempts": data.get("attempts", 0),
            "created_at": created_at,
            "updated_at": data.get("updated_at", ""),
            "elapsed": _format_elapsed(created_at),
        })
    return _json(runs)


async def get_run(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    repo_root = _repo_root(request)
    from spec_runtime.autopilot import load_run_record_index

    run_index = load_run_record_index(repo_root)
    data = run_index.by_run_id.get(run_id)
    if data is None:
        return _json({"error": f"Run not found: {run_id}"}, 404)

    # Enrich with sidecar artifacts: review findings and block diagnosis.
    result = dict(data)

    from spec_runtime.review_feedback import ReviewResult

    review = ReviewResult.load(repo_root, run_id)
    if review is not None:
        result["review_findings"] = [asdict(f) for f in review.findings]
        result["review_status"] = review.status
        result["review_summary"] = review.summary

    from spec_runtime.orchestrator import BlockDiagnosis

    block = BlockDiagnosis.load(repo_root, run_id)
    if block is not None:
        result["block_diagnosis"] = asdict(block)

    return _json(result)


async def get_run_log(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    repo_root = _repo_root(request)
    lines_param = request.query_params.get("lines", "200")
    try:
        max_lines = int(lines_param)
    except (ValueError, TypeError):
        max_lines = 200

    # Look up the run record to find the spec_id for log resolution
    from spec_runtime.autopilot import load_run_record_index
    from spec_runtime.autopilot_tui.dashboard import resolve_log_path

    run_index = load_run_record_index(repo_root)
    run_data = run_index.by_run_id.get(run_id)
    spec_id = run_data.get("spec_id", "") if run_data else ""

    log_path = None
    if spec_id:
        log_path = resolve_log_path(repo_root, spec_id, run_id=run_id)

    if log_path is None or not log_path.exists():
        return _json({"run_id": run_id, "lines": []})

    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return _json({"run_id": run_id, "lines": []})

    tail = all_lines[-max_lines:] if len(all_lines) > max_lines else all_lines
    return _json({"run_id": run_id, "lines": tail})


async def dashboard(request: Request) -> Response:
    repo_root = _repo_root(request)
    from spec_runtime.autopilot_tui.dashboard import load_dashboard_snapshot

    snapshot = load_dashboard_snapshot(repo_root)
    data = _serialize_snapshot(snapshot)

    # Include full spec list so the dashboard can show all specs with status badges
    from spec_runtime.config import load_repo_spec_runtime_config
    from spec_runtime.spec_metadata import iter_spec_metadata
    from spec_runtime.spec_status import collect_git_spec_state, get_spec_status

    config = load_repo_spec_runtime_config(repo_root)
    git_state = collect_git_spec_state(repo_root)
    specs = []
    for record in iter_spec_metadata(repo_root):
        if record.obsolete:
            continue
        if record.superseded_by:
            status = "superseded"
        else:
            status = get_spec_status(
                repo_root,
                record.spec_id,
                repo_root / config.paths.specs_dir / f"{record.spec_id}.md",
                git_state=git_state,
            )
        specs.append({
            "spec_id": record.spec_id,
            "status": status,
            "area": record.area,
            "priority": record.priority,
            "depends_on": list(record.depends_on),
            "description": record.description,
        })
    data["specs"] = specs

    return _json(data)


def _serialize_snapshot(snapshot: object) -> dict:
    from spec_runtime.autopilot_tui.dashboard import DashboardSnapshot

    assert isinstance(snapshot, DashboardSnapshot)
    return {
        "rows": [asdict(row) for row in snapshot.rows],
        "queue": [asdict(item) for item in snapshot.queue],
        "merged_count": snapshot.merged_count,
        "passed_count": snapshot.passed_count,
        "active_count": snapshot.active_count,
        "failed_count": snapshot.failed_count,
        "queued_count": snapshot.queued_count,
    }


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


async def implement_spec(request: Request) -> Response:
    spec_id = request.path_params["spec_id"]
    if not _valid_spec_id(spec_id):
        return _json({"error": "Invalid spec ID"}, 400)
    async with _spec_lifecycle_lock(request, spec_id):
        return await _implement_spec_locked(request)


async def _implement_spec_locked(request: Request) -> Response:
    spec_id = request.path_params["spec_id"]
    if not _valid_spec_id(spec_id):
        return _json({"error": "Invalid spec ID"}, 400)
    repo_root = _repo_root(request)

    # Validate the spec exists before spawning a subprocess.
    from spec_runtime.config import load_repo_spec_runtime_config

    config = load_repo_spec_runtime_config(repo_root)
    spec_path = repo_root / config.paths.specs_dir / f"{spec_id}.md"
    if not spec_path.exists():
        return _json({"error": f"Spec not found: {spec_id}"}, 404)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    agent = body.get("agent", "")
    review_agent = body.get("review_agent", "")

    # Snapshot the current latest run_id so we can detect whether a new
    # run record was created after spawning the subprocess.
    from spec_runtime.autopilot import load_run_record_index

    pre_index = load_run_record_index(repo_root)
    pre_run = pre_index.latest_by_spec.get(spec_id)
    pre_run_id = pre_run.get("run_id", "") if pre_run else ""

    started_processes = _started_processes(request)
    existing = started_processes.get(spec_id)
    if existing is not None:
        if existing.poll() is None:
            return _json(
                {"error": "An implementation launch is already active", "spec_id": spec_id},
                409,
            )
        _remove_started_process(started_processes, spec_id, existing)

    proc = ProcessSupervisor(LifetimeMode.ADOPTABLE).spawn(
        [
            _spec_executable(),
            "implement",
            "--spec",
            spec_id,
            *(["--agent", agent] if agent else []),
            *(["--review-agent", review_agent] if review_agent else []),
        ],
        cwd=str(repo_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Publish the exact ownership capability before the first await.
    setattr(proc, "_spec_web_run_id", "")
    started_processes[spec_id] = proc

    # Give the process a moment to fail on startup (e.g. bad arguments,
    # permission errors).  If it exits immediately with a non-zero code the
    # request was invalid and we should not report success.
    await asyncio.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None and exit_code != 0:
        _remove_started_process(started_processes, spec_id, proc)
        return _json(
            {"error": f"spec implement exited immediately (code {exit_code})", "spec_id": spec_id},
            422,
        )

    # Track the Popen object so stop_spec can terminate it.  Storing the
    # object (not a bare PID) lets us call proc.poll() to verify the child
    # is still alive before signalling, preventing PID-reuse kills.
    # Return latest known run state for the spec.  Only return a persisted
    # run record when it belongs to *this* launch (run_id changed from the
    # pre-spawn snapshot).  Otherwise fall back to a synthesized "starting"
    # state so callers never correlate a stale historical run with this action.
    post_index = load_run_record_index(repo_root)
    latest_run = post_index.latest_by_spec.get(spec_id)

    new_run_id = latest_run.get("run_id", "") if latest_run else ""
    if latest_run and new_run_id and new_run_id != pre_run_id:
        run_id = new_run_id
        run_state = latest_run
        if started_processes.get(spec_id) is proc:
            setattr(proc, "_spec_web_run_id", run_id)
    else:
        run_id = ""
        run_state = {
            "spec_id": spec_id,
            "status": "starting",
            "agent": agent,
            "pid": proc.pid,
        }

    return _json({
        "spec_id": spec_id,
        "agent": agent,
        "pid": proc.pid,
        "status": "started",
        "run_id": run_id,
        "run_state": run_state,
    })


async def stop_spec(request: Request) -> Response:
    spec_id = request.path_params["spec_id"]
    if not _valid_spec_id(spec_id):
        return _json({"error": "Invalid spec ID"}, 400)
    async with _spec_lifecycle_lock(request, spec_id):
        return await _stop_spec_locked(request)


async def _stop_spec_locked(request: Request) -> Response:
    spec_id = request.path_params["spec_id"]
    if not _valid_spec_id(spec_id):
        return _json({"error": "Invalid spec ID"}, 400)
    repo_root = _repo_root(request)

    from spec_runtime.autopilot_tui.dashboard import _resolve_live_process_group

    pg = _resolve_live_process_group(repo_root, spec_id)
    started_processes = _started_processes(request)

    leader_pid: int | None = None
    tracked_exit_confirmed = False
    proc = started_processes.get(spec_id)
    owned_process = proc
    tracked_run_id = ""

    if proc is not None:
        value = getattr(proc, "_spec_web_run_id", "")
        tracked_run_id = value if isinstance(value, str) else ""
        if not tracked_run_id and pg is not None and _tracked_process_matches_group(proc, pg):
            from spec_runtime.autopilot import load_run_record_index

            current = load_run_record_index(repo_root).latest_by_spec.get(spec_id)
            if current:
                candidate = current.get("run_id", "")
                tracked_run_id = candidate if isinstance(candidate, str) else ""
        # Prefer the managed process retained by the launch endpoint even
        # after the orchestrator has persisted its process metadata.  Its
        # termination boundary validates identity and owns the Windows Job;
        # reducing it back to a PID/process group loses both guarantees.
        leader_exited = proc.poll() is not None
        tree_active = getattr(proc, "owned_tree_active", None)
        if leader_exited and callable(tree_active):
            try:
                leader_exited = not tree_active()
            except (OSError, RuntimeError):
                leader_exited = False
        if leader_exited:
            # A retained ManagedProcess proves ownership. Its bounded
            # termination path already covers descendants; a completed
            # leader on a later retry is enough to reap the registry handle.
            leader_pid = proc.pid
            _remove_started_process(started_processes, spec_id, proc)
            # A live persisted group belongs to a current run, even when an
            # older web-started handle under the same spec key has exited.
            tracked_exit_confirmed = pg is None
            proc = None
            if pg is not None:
                leader_pid = pg[0]
        else:
            leader_pid = proc.pid
            if pg is not None and not _tracked_process_matches_group(proc, pg):
                return _json(
                    {
                        "error": "Tracked launch does not match the current persisted run",
                        "spec_id": spec_id,
                        "status": "stopping",
                        "pid": leader_pid,
                    },
                    409,
                )
    elif pg is not None:
        leader_pid = pg[0]

    if leader_pid is None:
        return _json({"spec_id": spec_id, "status": "no_active_run"}, 404)

    if proc is not None:
        try:
            termination_confirmed = proc.terminate(grace_seconds=3)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            return _json(
                {
                    "error": f"Process stop was not confirmed: {exc}",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                },
                503,
            )

        # Reap the leader as a separate positive confirmation. Lightweight
        # compatibility doubles may not yet return the ManagedProcess boolean,
        # so an ordinary successful wait remains an accepted boundary for them.
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return _json(
                {
                    "error": "Process stop is still pending",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                },
                504,
            )
        except (OSError, RuntimeError) as exc:
            return _json(
                {
                    "error": f"Process stop was not confirmed: {exc}",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                },
                503,
            )
        if termination_confirmed is False:
            return _json(
                {
                    "error": "Owned process tree exit was not confirmed",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                },
                503,
            )
        _remove_started_process(started_processes, spec_id, proc)
        tracked_exit_confirmed = True
    elif not tracked_exit_confirmed:
        try:
            from spec_runtime.orchestrator import stop_run

            stopped_run = stop_run(spec_id, repo_root=repo_root)
            tracked_run_id = stopped_run.run_id
        except (RuntimeError, ProcessLookupError, PermissionError, OSError) as exc:
            return _json(
                {
                    "error": f"Process stop was not confirmed: {exc}",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                },
                503,
            )

    if tracked_exit_confirmed and not tracked_run_id and owned_process is not None:
        # The child can publish its RunState after Stop's initial process-group
        # snapshot but before its retained tree exits. Reload only after that
        # positive exit boundary, and correlate the durable identity to this
        # exact launch before terminalizing anything.
        from spec_runtime.autopilot import load_run_record_index

        late_run = load_run_record_index(repo_root).latest_by_spec.get(spec_id)
        if (
            late_run
            and isinstance(late_run.get("run_id"), str)
            and _persisted_run_matches_tracked_process(late_run, owned_process)
        ):
            tracked_run_id = late_run["run_id"]

    if tracked_exit_confirmed:
        terminalized = _terminalize_matching_run(
            repo_root,
            spec_id,
            tracked_run_id,
            expected_process=owned_process,
        )
        if tracked_run_id and terminalized is None:
            # The exact run disappeared or changed ownership while Stop was
            # correlating it. Preserve the current state and take the
            # non-success path below instead of reporting a false Stop.
            tracked_run_id = ""

    # Return latest run state for the spec
    from spec_runtime.autopilot import load_run_record_index

    run_index = load_run_record_index(repo_root)
    latest_run = run_index.latest_by_spec.get(spec_id)

    run_state = dict(latest_run) if latest_run else None
    if run_state and run_state.get("status") in ("running", "in_progress"):
        if tracked_run_id and run_state.get("run_id") != tracked_run_id:
            return _json(
                {
                    "error": "A newer implementation run is still active",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                    "run_state": run_state,
                },
                409,
            )
        if not tracked_run_id:
            # No persisted identity was correlated to the exited startup
            # process. Never relabel or hide an arbitrary current run while
            # returning a false successful Stop response.
            return _json(
                {
                    "error": "A running implementation could not be correlated to the stopped launch",
                    "spec_id": spec_id,
                    "status": "stopping",
                    "pid": leader_pid,
                    "run_state": run_state,
                },
                409,
            )

    return _json({
        "spec_id": spec_id,
        "status": "stopped",
        "pid": leader_pid,
        "run_id": (run_state or {}).get("run_id", ""),
        "run_state": run_state,
    })


async def dispatch_start(request: Request) -> Response:
    repo_root = _repo_root(request)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    concurrency = body.get("concurrency", 8)
    agent = body.get("agent", "")
    dry_run = bool(body.get("dry_run", False))

    cmd = [_spec_executable(), "auto", "run", "--concurrency", str(concurrency)]
    if agent:
        cmd += ["--agent", agent]
    if dry_run:
        cmd += ["--dry-run"]

    # In dry-run mode we want the dispatch preview, so capture output and
    # return it synchronously rather than daemonising a long-running loop.
    if dry_run:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return _json({
            "status": "dry_run",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        })

    proc = ProcessSupervisor(LifetimeMode.DETACHED).spawn(
        cmd,
        cwd=str(repo_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Give the process a moment to fail on startup (e.g. autopilot already
    # running, bad arguments).  Mirrors the check in implement_spec.
    await asyncio.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None and exit_code != 0:
        return _json(
            {"error": f"spec auto run exited immediately (code {exit_code})"},
            422,
        )

    return _json({"status": "started", "pid": proc.pid})


async def dispatch_stop(request: Request) -> Response:
    repo_root = _repo_root(request)

    proc = subprocess.run(
        [_spec_executable(), "auto", "stop"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    if "not running" in combined:
        status = "not_running"
    elif proc.returncode == 0:
        status = "stopped"
    else:
        status = "error"

    return _json({
        "status": status,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    })


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def events(request: Request) -> Response:
    repo_root = _repo_root(request)

    async def event_generator():
        from spec_runtime.autopilot_tui.dashboard import load_dashboard_snapshot
        from spec_runtime.config import load_repo_spec_runtime_config
        from spec_runtime.spec_metadata import iter_spec_metadata
        from spec_runtime.spec_status import collect_git_spec_state, get_spec_status

        last_hash = ""
        tick = 0
        cached_specs: list[dict] = []
        while True:
            if await request.is_disconnected():
                break
            try:
                snapshot = load_dashboard_snapshot(repo_root)
                data = _serialize_snapshot(snapshot)

                # Refresh the full spec list periodically (expensive);
                # always include cached_specs so run_update payloads
                # never blank the Specs table on the frontend.
                if tick % 10 == 0 or tick == 0:
                    config = load_repo_spec_runtime_config(repo_root)
                    git_state = collect_git_spec_state(repo_root)
                    specs = []
                    for record in iter_spec_metadata(repo_root):
                        if record.obsolete:
                            continue
                        if record.superseded_by:
                            status = "superseded"
                        else:
                            status = get_spec_status(
                                repo_root,
                                record.spec_id,
                                repo_root / config.paths.specs_dir / f"{record.spec_id}.md",
                                git_state=git_state,
                            )
                        specs.append({
                            "spec_id": record.spec_id,
                            "status": status,
                            "area": record.area,
                            "priority": record.priority,
                            "depends_on": list(record.depends_on),
                            "description": record.description,
                        })
                    cached_specs = specs

                data["specs"] = cached_specs

                data_json = json.dumps(data, default=str)
                current_hash = str(hash(data_json))

                if tick == 0 or current_hash != last_hash:
                    yield f"event: run_update\ndata: {data_json}\n\n"
                    last_hash = current_hash

                if tick % 10 == 0:
                    yield f"event: dashboard\ndata: {data_json}\n\n"
            except Exception:
                pass

            tick += 1
            await asyncio.sleep(3)

    from starlette.responses import StreamingResponse

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------


api_routes = [
    Route("/api/v1/specs", list_specs),
    Route("/api/v1/specs/{spec_id}", get_spec),
    Route("/api/v1/runs", list_runs),
    Route("/api/v1/runs/{run_id}", get_run),
    Route("/api/v1/runs/{run_id}/log", get_run_log),
    Route("/api/v1/dashboard", dashboard),
    Route("/api/v1/specs/{spec_id}/implement", implement_spec, methods=["POST"]),
    Route("/api/v1/specs/{spec_id}/stop", stop_spec, methods=["POST"]),
    Route("/api/v1/dispatch/start", dispatch_start, methods=["POST"]),
    Route("/api/v1/dispatch/stop", dispatch_stop, methods=["POST"]),
    Route("/api/v1/events", events),
]
