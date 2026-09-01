"""/api/v1 route handlers for the spec web server."""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor


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


_UNSAFE_LINK_RE = re.compile(
    r"""((?:href|src)\s*=\s*)(["']?)\s*(javascript|vbscript|data)\s*:""",
    re.IGNORECASE,
)


def _render_markdown(text: str) -> str:
    """Render markdown to HTML with raw HTML and dangerous links disabled.

    Raw HTML in the source is entity-escaped *before* markdown processing so
    that only markdown-generated tags appear in the output.  After rendering,
    dangerous URL schemes (``javascript:``, ``vbscript:``, ``data:``) are
    stripped from ``href`` and ``src`` attributes to prevent XSS via
    markdown link syntax like ``[click](javascript:...)``.
    """
    import markdown as md

    safe_text = html_mod.escape(text)
    rendered = md.markdown(safe_text, extensions=["tables", "fenced_code"])
    return _UNSAFE_LINK_RE.sub(r"\1\2#blocked-", rendered)


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

    # Give the process a moment to fail on startup (e.g. bad arguments,
    # permission errors).  If it exits immediately with a non-zero code the
    # request was invalid and we should not report success.
    await asyncio.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None and exit_code != 0:
        return _json(
            {"error": f"spec implement exited immediately (code {exit_code})", "spec_id": spec_id},
            422,
        )

    # Track the Popen object so stop_spec can terminate it.  Storing the
    # object (not a bare PID) lets us call proc.poll() to verify the child
    # is still alive before signalling, preventing PID-reuse kills.
    _started_processes(request)[spec_id] = proc

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
    repo_root = _repo_root(request)

    from spec_runtime.autopilot_tui.dashboard import _resolve_live_process_group

    pg = _resolve_live_process_group(repo_root, spec_id)
    started_processes = _started_processes(request)

    leader_pid: int | None = None
    proc = started_processes.get(spec_id)

    if proc is not None:
        # Prefer the managed process retained by the launch endpoint even
        # after the orchestrator has persisted its process metadata.  Its
        # termination boundary validates identity and owns the Windows Job;
        # reducing it back to a PID/process group loses both guarantees.
        if proc.poll() is not None:
            # Child already exited — PID may have been reused; clean up.
            started_processes.pop(spec_id, None)
            proc = None
        else:
            leader_pid = proc.pid
    elif pg is not None:
        leader_pid = pg[0]

    if leader_pid is None:
        return _json({"spec_id": spec_id, "status": "no_active_run"}, 404)

    if proc is not None:
        try:
            proc.terminate(grace_seconds=3)
        except (ProcessLookupError, PermissionError, OSError):
            return _json({"spec_id": spec_id, "status": "not_found"}, 404)
    else:
        try:
            from spec_runtime.orchestrator import stop_run

            stop_run(spec_id, repo_root=repo_root)
        except (RuntimeError, ProcessLookupError, PermissionError, OSError):
            return _json({"spec_id": spec_id, "status": "not_found"}, 404)

    # Wait briefly for the process to exit so the persisted run record
    # has a chance to update before we read it back.
    proc = started_processes.pop(spec_id, None)
    if proc is not None:
        try:
            proc.wait(timeout=3)
        except Exception:
            pass  # Best-effort; we already sent the signal

    # Return latest run state for the spec
    from spec_runtime.autopilot import load_run_record_index

    run_index = load_run_record_index(repo_root)
    latest_run = run_index.latest_by_spec.get(spec_id)

    # Ensure the returned state reflects the stop even if the persisted
    # record hasn't been flushed yet.
    run_state = dict(latest_run) if latest_run else None
    if run_state and run_state.get("status") in ("running", "in_progress"):
        run_state["status"] = "stopped"

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
