"""Small dependency-free stdio MCP transport for the repository workflow."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import autopilot as auto
from . import orchestrator as orch
from .coordination import CoordinatorError
from .platform_fs import FileLock
from .spec_identity import SPEC_ID_RE
from .spec_metadata import iter_spec_metadata

_DESCRIPTIONS = {
    "list_specs": "Read-only: list specs; ready_only uses the dispatch DAG and eligibility rules.",
    "spec_status": "Read-only: inspect the latest run and gate status. Poll to watch progress.",
    "lint_spec": "Read-only: deterministically lint a spec, without model critique.",
    "implement_spec": "Changes state: start a fully detached run, or return the existing active run id.",
    "steer_run": "Changes state: deliver guidance to the next implementation attempt.",
    "stop_run": "Changes state: stop the latest run and its processes.",
    "attach_command": "Takeover: return spec attach output; may restore archived provider sessions locally.",
}


def tool_definitions():
    result = []
    for name, description in _DESCRIPTIONS.items():
        properties = (
            {"ready_only": {"type": "boolean", "default": False}}
            if name == "list_specs"
            else {"spec_id": {"type": "string"}}
        )
        if name == "steer_run":
            properties["message"] = {"type": "string"}
        result.append(
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "additionalProperties": False,
                    "required": ([] if name == "list_specs" else ["spec_id"])
                    + (["message"] if name == "steer_run" else []),
                },
            }
        )
    return result


def implement_spec(root: Path, spec_id: str):
    """Serialize launches across server instances and persist adoption identity."""
    state = auto.autopilot_state_root(root) / "mcp"
    state.mkdir(parents=True, exist_ok=True)
    with FileLock(state / f"{spec_id}.lock"):
        path = state / f"{spec_id}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            if auto.is_pid_alive(saved["pid"], saved["process_started_at"]):
                return {"run_id": saved["run_id"]}
        for run in orch.RunState.list_for_spec(root, spec_id):
            if run.status == "running":
                from .spec_status import project_canonical_spec_status

                projection = project_canonical_spec_status(root, spec_id)
                if projection is not None and projection.is_active:
                    return {"run_id": run.run_id}
        candidates = {c.spec_id: c for c in auto.build_dispatch_queue(root)}
        if spec_id not in candidates:
            raise ValueError(f"Spec {spec_id} is not ready for dispatch")
        candidate = candidates[spec_id]
        run_id, _lease = auto.acquire_candidate_lease(root, candidate)
        run_id = run_id or orch._run_id(spec_id)
        proc = auto.start_candidate(root, candidate, preallocated_run_id=run_id)
        path.write_text(json.dumps(asdict(proc)))
        return {"run_id": proc.run_id}


def call_tool(root: Path, name: str, arguments: dict):
    definition = next((t for t in tool_definitions() if t["name"] == name), None)
    if definition is None:
        raise ValueError(f"Unknown tool: {name}")
    schema = definition["inputSchema"]
    if set(arguments) - set(schema["properties"]) or any(k not in arguments for k in schema["required"]):
        raise ValueError("Invalid tool arguments")
    for key, value in arguments.items():
        expected = bool if key == "ready_only" else str
        if not isinstance(value, expected):
            raise ValueError(f"Invalid {key}")
    if name == "list_specs":
        if arguments.get("ready_only", False):
            return [asdict(c) for c in auto.build_dispatch_queue(root)]
        return [asdict(s) for s in iter_spec_metadata(root)]
    spec_id = arguments["spec_id"]
    if not SPEC_ID_RE.fullmatch(spec_id):
        raise ValueError("Invalid spec ID")
    if name == "implement_spec":
        return implement_spec(root, spec_id)
    if name == "lint_spec":
        from .spec_lint import lint_specs

        return [asdict(r) for r in lint_specs(root, spec_id)]
    command = {"spec_status": "status", "steer_run": "steer", "stop_run": "stop", "attach_command": "attach"}[name]
    argv = [sys.executable, "-m", "spec_runtime.cli", command, "--spec", spec_id]
    if name == "steer_run":
        argv += ["--message", arguments["message"]]
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return {"output": result.stdout.strip()}


def handle_request(root: Path, request: dict):
    """Handle a single MCP JSON-RPC message (also used by in-process clients)."""
    if "id" not in request:
        return None
    response = {"jsonrpc": "2.0", "id": request["id"]}
    method = request.get("method")
    if method == "initialize":
        response["result"] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "specbutler", "version": "0.5.0"},
        }
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": tool_definitions()}
    elif method == "tools/call":
        try:
            params = request.get("params", {})
            result = call_tool(root, params["name"], params.get("arguments", {}))
            response["result"] = {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except (CoordinatorError, ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            response["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def serve(root: Path) -> int:
    for line in sys.stdin:
        try:
            response = handle_request(root, json.loads(line))
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0
