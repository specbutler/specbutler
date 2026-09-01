from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from rich.console import Group
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from spec_runtime import autopilot
from spec_runtime import orchestrator as orch
from spec_runtime.autopilot_tui.dashboard import (  # noqa: F401 — re-exported for backwards compat
    SPEC_RUNTIME_CONFIG,
    VISIBLE_DASHBOARD_RUN_STATUSES,
    DashboardSnapshot,
    SpecRow,
    _latest_run,
    _read_active_data,
    _resolve_live_process_group,
    _row_sort_key,
    _run_requires_live_guard,
    _state_roots,
    is_spec_live,
    load_dashboard_snapshot,
    resolve_log_path,
)
from spec_runtime.config import load_repo_spec_runtime_config
from spec_runtime.container import container_image_source
from spec_runtime.platform_fs import remove_tree
from spec_runtime.spec_metadata import iter_spec_metadata


def _launch_make_code(
    repo_root: Path,
    spec_id: str,
    *,
    run_id: str = "",
    agent: str = "",
    actor: str = "autopilot-watch",
) -> Path:
    command = ["spec", "implement", "--spec", spec_id]
    if run_id:
        command += ["--run", run_id]
    elif agent:
        command += ["--agent", agent]

    log_root = autopilot.autopilot_runs_root(repo_root)
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"{spec_id}--{autopilot.timestamp_token()}.log"
    env = dict(os.environ)
    env["SPEC_ACTOR"] = actor

    log_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    try:
        subprocess.Popen(
            command,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    return log_path


def _launch_make_code_compat(
    repo_root: Path,
    spec_id: str,
    *,
    run_id: str = "",
    agent: str = "",
    actor: str = "autopilot-watch",
) -> Path:
    try:
        return _launch_make_code(
            repo_root,
            spec_id,
            run_id=run_id,
            agent=agent,
            actor=actor,
        )
    except TypeError as exc:
        if "unexpected keyword argument 'actor'" not in str(exc):
            raise
        return _launch_make_code(repo_root, spec_id, run_id=run_id, agent=agent)


def resume_spec_run(repo_root: Path, spec_id: str, *, actor: str = "autopilot-watch") -> Path:
    if is_spec_live(repo_root, spec_id):
        raise RuntimeError(f"Spec '{spec_id}' already has a live run. Stop it before resuming.")
    latest = _latest_run(repo_root, spec_id)
    if latest is None:
        raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")
    return _launch_make_code_compat(repo_root, spec_id, run_id=latest.run_id, actor=actor)


def _clear_spec_runtime_artifacts(repo_root: Path, spec_id: str) -> None:
    for state_root in _state_roots(repo_root):
        legacy_dir = state_root / spec_id
        remove_tree(legacy_dir, ignore_errors=True)

        autopilot_runs = state_root / "autopilot" / "runs"
        if autopilot_runs.exists():
            for log_path in autopilot_runs.glob(f"{spec_id}--*.log"):
                log_path.unlink(missing_ok=True)

        active_path = state_root / "autopilot" / "active.json"
        if active_path.exists():
            try:
                payload = json.loads(active_path.read_text())
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            if isinstance(payload, dict) and spec_id in payload:
                payload.pop(spec_id, None)
                active_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _clear_run_implement_results(repo_root: Path, run: orch.RunState) -> None:
    state_roots = list(_state_roots(repo_root))
    worktree_state_root = orch._worktree_state_root(orch.resolve_worktree_path(run, repo_root))
    if worktree_state_root not in state_roots:
        state_roots.append(worktree_state_root)

    for state_root in state_roots:
        result_path = state_root / "runs" / run.run_id / "implement-result.json"
        result_path.unlink(missing_ok=True)


def _remove_spec_run_state(repo_root: Path, spec_id: str) -> None:
    for state_root in _state_roots(repo_root):
        runs_root = state_root / "runs"
        if runs_root.exists():
            for run_json in runs_root.glob("*.json"):
                try:
                    payload = json.loads(run_json.read_text())
                except (json.JSONDecodeError, OSError, TypeError):
                    continue
                if str(payload.get("spec_id", "")).strip() != spec_id:
                    continue
                run_id = str(payload.get("run_id", "")).strip() or run_json.stem
                run_json.unlink(missing_ok=True)
                remove_tree(runs_root / run_id, ignore_errors=True)

    _clear_spec_runtime_artifacts(repo_root, spec_id)


def reset_spec_run(
    repo_root: Path,
    spec_id: str,
    *,
    agent: str,
    actor: str = "autopilot-watch",
) -> None:
    latest = _latest_run(repo_root, spec_id)
    if _resolve_live_process_group(repo_root, spec_id, run=latest) is not None:
        orch.stop_run(spec_id, repo_root=repo_root)
    elif _run_requires_live_guard(latest):
        raise RuntimeError(f"Spec '{spec_id}' still appears to be running. Wait for it to exit before resetting.")
    relaunch_run_id = ""
    if latest is not None and latest.run_mode == "task":
        relaunch_run_id = latest.run_id
    _run_code_clean(repo_root, spec_id)
    if relaunch_run_id:
        _clear_spec_runtime_artifacts(repo_root, spec_id)
        _launch_make_code_compat(repo_root, spec_id, run_id=relaunch_run_id, actor=actor)
        return
    _remove_spec_run_state(repo_root, spec_id)
    _launch_make_code_compat(repo_root, spec_id, agent=agent, actor=actor)


def _run_code_clean(repo_root: Path, spec_id: str) -> None:
    result = subprocess.run(
        ["spec", "clean", "--spec", spec_id],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "spec clean failed"
        raise RuntimeError(detail)


def _mark_spec_obsolete(spec_path: Path) -> None:
    text = spec_path.read_text()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(f"Spec file does not have YAML frontmatter: {spec_path}")
    end_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise RuntimeError(f"Spec file does not have a closed YAML frontmatter block: {spec_path}")

    replaced = False
    for idx in range(1, end_idx):
        if lines[idx].startswith("obsolete:"):
            lines[idx] = "obsolete: true"
            replaced = True
            break
        if lines[idx].startswith("status:"):
            lines[idx] = "obsolete: true"
            replaced = True
            break
    if not replaced:
        insert_at = 1
        for idx in range(1, end_idx):
            if lines[idx].startswith("id:"):
                insert_at = idx + 1
                break
        lines.insert(insert_at, "obsolete: true")
    spec_path.write_text("\n".join(lines) + "\n")


def _clear_spec_status_override(repo_root: Path, spec_id: str) -> None:
    path = autopilot.autopilot_spec_overrides_path(repo_root)
    overrides = autopilot.read_spec_status_overrides(repo_root)
    if spec_id not in overrides:
        return
    overrides.pop(spec_id, None)
    if overrides:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overrides, indent=2, sort_keys=True) + "\n")
        return
    path.unlink(missing_ok=True)


def _mark_product_spec_obsolete(repo_root: Path, spec_id: str) -> Path:
    spec_path = repo_root / SPEC_RUNTIME_CONFIG.paths.specs_dir / f"{spec_id}.md"
    if not spec_path.exists():
        raise RuntimeError(f"Could not find product spec '{spec_id}' in {repo_root}.")
    _mark_spec_obsolete(spec_path)
    _clear_spec_status_override(repo_root, spec_id)
    return spec_path


def _delete_task_spec(repo_root: Path, spec_id: str) -> Path:
    task_path = repo_root / SPEC_RUNTIME_CONFIG.paths.task_specs_dir / f"{spec_id}.md"
    if not task_path.exists():
        raise RuntimeError(f"Could not find task spec '{spec_id}' in {repo_root}.")
    task_path.unlink()
    _clear_spec_status_override(repo_root, spec_id)
    return task_path


def _has_task_run_records(repo_root: Path, spec_id: str) -> bool:
    for state_root in _state_roots(repo_root):
        runs_root = state_root / "runs"
        if not runs_root.exists():
            continue
        for run_json in runs_root.glob("*.json"):
            try:
                payload = json.loads(run_json.read_text())
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            if (
                str(payload.get("spec_id", "")).strip() == spec_id
                and str(payload.get("run_mode", "")).strip() == "task"
            ):
                return True
    return False


def delete_spec_artifacts(repo_root: Path, spec_id: str) -> str:
    common_root = autopilot.resolve_common_root(repo_root)
    task_path = common_root / SPEC_RUNTIME_CONFIG.paths.task_specs_dir / f"{spec_id}.md"
    if task_path.exists():
        _run_code_clean(common_root, spec_id)
        _remove_spec_run_state(repo_root, spec_id)
        deleted_path = _delete_task_spec(common_root, spec_id)
        return f"Deleted task '{spec_id}' at '{deleted_path}' and removed its run state."

    spec_path = common_root / SPEC_RUNTIME_CONFIG.paths.specs_dir / f"{spec_id}.md"
    if spec_path.exists():
        updated_path = _mark_product_spec_obsolete(common_root, spec_id)
        return f"Marked product spec '{spec_id}' obsolete in '{updated_path}'."

    if _has_task_run_records(repo_root, spec_id):
        try:
            _run_code_clean(common_root, spec_id)
        except RuntimeError:
            pass
        _remove_spec_run_state(repo_root, spec_id)
        return f"Removed run state for orphan task '{spec_id}' (no spec file on disk)."

    raise RuntimeError(f"Could not find a task or product spec for '{spec_id}'.")


def resolve_input_run(repo_root: Path, spec_id: str, answer: str) -> Path:
    run = _latest_run(repo_root, spec_id)
    if run is None:
        raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")
    common_root = autopilot.resolve_common_root(repo_root)
    request = orch._ensure_operator_request(
        common_root,
        run,
        allow_debugger_promotion=True,
    )
    if request is None:
        diagnosis = orch.BlockDiagnosis.load(common_root, run.run_id) if run.run_id else None
        raise RuntimeError(_resolve_input_unavailable_message(spec_id, run.status, diagnosis))
    if run.status != "waiting-for-input" and request.status != "pending":
        diagnosis = orch.BlockDiagnosis.load(common_root, run.run_id) if run.run_id else None
        raise RuntimeError(_resolve_input_unavailable_message(spec_id, run.status, diagnosis))
    question = request.prompt or "Operator intervention required"
    if request.requires_full_session:
        raise RuntimeError(
            f"This request likely needs code changes or test validation. "
            f"Use `spec input --spec {spec_id}` instead.\n\nQuestion: {question}"
        )
    response = answer.strip()
    if not response:
        raise RuntimeError("Input response cannot be empty.")

    continuation = orch.resolve_operator_request(
        common_root,
        run,
        response,
        source="tui-chat",
        session_completed_implement=False,
        allow_debugger_promotion=True,
    )
    if continuation.resumes_implement:
        _clear_run_implement_results(common_root, run)
    return _launch_make_code_compat(repo_root, spec_id, run_id=run.run_id, actor="tui-chat")


def record_operator_steering(repo_root: Path, spec_id: str, guidance: str) -> orch.OperatorSteering:
    run = _latest_run(repo_root, spec_id)
    if run is None:
        raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")
    common_root = autopilot.resolve_common_root(repo_root)
    return orch._record_operator_steering(
        common_root,
        run,
        guidance,
        source="tui-chat",
    )


def _default_agent(repo_root: Path, spec_id: str, *, fallback_agent: str = "") -> str:
    if fallback_agent and fallback_agent != "—":
        return fallback_agent
    repo_config = load_repo_spec_runtime_config(repo_root)
    for metadata in iter_spec_metadata(repo_root):
        if metadata.spec_id == spec_id:
            return autopilot.select_agent(metadata, config=repo_config)
    latest = _latest_run(repo_root, spec_id)
    if latest is not None and latest.agent:
        return latest.agent
    return repo_config.agents.default


def _available_chat_agents(repo_root: Path) -> tuple[str, ...]:
    """Return configured chat agents whose CLI is available on this host."""
    config = load_repo_spec_runtime_config(repo_root)
    configured = tuple(
        agent for agent in config.agents.allowed if agent in {"claude", "codex"}
    )
    ordered = dict.fromkeys((config.agents.default, *configured))
    return tuple(
        agent
        for agent in ordered
        if agent in configured and shutil.which(agent) is not None
    )


def _coerce_spec_row_key(row_key: object) -> str:
    value = getattr(row_key, "value", row_key)
    return str(value).strip()


def _human_attention_guidance(spec_id: str, diagnosis: orch.BlockDiagnosis) -> str:
    next_action = diagnosis.next_best_action.strip() or "Inspect the latest run details before deciding how to proceed."
    return (
        f"{spec_id} needs human attention.\n\n"
        f"**Summary:** {diagnosis.summary}\n"
        f"**Root cause:** {diagnosis.root_cause}\n"
        f"**Suggested action:** {next_action}\n\n"
        "Use `answer:` only when the run has an active operator request.\n"
        "If you want a full interactive session, run `spec input --spec <id>` from the worktree.\n"
        "For this run you can also:\n"
        "- `steer: <guidance>` to attach advisory context for the next implement attempt\n"
        "- `show run` or `tail log` to inspect the current state\n"
        "- `add retries N` and `resume` if you want to try again with more budget\n"
        "- `reset` to start fresh"
    )


def _resolve_input_unavailable_message(
    spec_id: str,
    status: str,
    diagnosis: orch.BlockDiagnosis | None = None,
) -> str:
    if diagnosis is not None and diagnosis.requires_human_attention:
        next_action = diagnosis.next_best_action.strip() or "Inspect the latest run details before deciding how to proceed."
        return (
            f"Spec '{spec_id}' is not waiting for operator intervention; current status is '{status}'. "
            "`answer:` only works for active operator requests.\n\n"
            f"Suggested action: {next_action}\n\n"
            "Use `show run` or `tail log` for context, then `add retries N` and `resume` or `reset`."
        )
    return f"Spec '{spec_id}' is not waiting for operator intervention; current status is '{status}'."


CHAT_GLOBAL_SESSION_KEY = "__global__"
CHAT_DEFAULT_LOG_LINES = 40
CHAT_STREAM_DELAY_SECONDS = 0.01
CHAT_PROVIDER_MAX_ATTEMPTS = 3
CHAT_PROVIDER_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# Provider chat has no legitimate long-running tools: Codex is read-only and
# Claude is launched without tools. Bound silence so a lost SDK/CLI stream
# cannot wedge Textual's worker indefinitely.
CHAT_PROVIDER_INACTIVITY_TIMEOUT_SECONDS = 180.0
CHAT_PROVIDER_POLL_SECONDS = 0.1
CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS = 5.0
CHAT_PROVIDER_STDERR_MAX_BYTES = 64 * 1024
CHAT_DESTRUCTIVE_ACTIONS = frozenset({"stop_run", "reset_run", "delete_spec"})
CHAT_MUTATING_ACTIONS = frozenset(
    {
        "stop_run",
        "resume_run",
        "reset_run",
        "add_retries",
        "delete_spec",
        "resolve_input",
        "record_steering",
    }
)
CHAT_STATUS_VALUES = frozenset({"running", "waiting", "blocked", "failed", "stale", "passed"})
CHAT_INPUT_CODE_CHANGE_HINTS = (
    "api version",
    "code change",
    "edit the code",
    "implementation",
    "implement",
    "refactor",
    "migration",
    "schema",
    "endpoint",
    "backend",
    "frontend",
    "database",
    "test",
    "file",
)
CHAT_TRANSIENT_PROVIDER_FAILURE_HINTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "connection aborted",
    "connection error",
    "connection refused",
    "connection reset",
    "econnaborted",
    "econnrefused",
    "econnreset",
    "gateway timeout",
    "network",
    "overloaded",
    "rate limit",
    "server error",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "try again",
    "unavailable",
)
CHAT_PROVIDER_COMMANDS = frozenset(
    {
        "list_runs",
        "show_run",
        "tail_log",
        "stop_run",
        "resume_run",
        "reset_run",
        "add_retries",
        "delete_spec",
        "resolve_input",
        "record_steering",
        "help",
    }
)
CHAT_PROVIDER_MESSAGE_DELIMITER = "\nMESSAGE:\n"
CHAT_PROVIDER_COMMAND_PREFIX = "COMMAND_JSON:"


def _provider_command_requires_confirmation(command: ChatCommand) -> bool:
    return command.name in CHAT_MUTATING_ACTIONS


@dataclass(frozen=True)
class ChatCommand:
    name: str
    spec_id: str = ""
    spec_ids: tuple[str, ...] = ()
    status_filter: str = ""
    agent_filter: str = ""
    phase_filter: str = ""
    count: int = 0
    extra_retries: int = 0
    lines: int = CHAT_DEFAULT_LOG_LINES
    answer: str = ""
    guidance: str = ""


def _normalize_spec_ids(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        items = (values,)
    elif isinstance(values, (list, tuple)):
        items = tuple(values)
    else:
        return ()
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        spec_id = str(item).strip()
        if not spec_id or spec_id in seen:
            continue
        seen.add(spec_id)
        ordered.append(spec_id)
    return tuple(ordered)


def _command_target_spec_ids(command: ChatCommand) -> tuple[str, ...]:
    values: list[str] = []
    if command.spec_id:
        values.append(command.spec_id)
    values.extend(command.spec_ids)
    return _normalize_spec_ids(values)


def _format_spec_targets(spec_ids: tuple[str, ...], *, limit: int = 3) -> str:
    if not spec_ids:
        return "the selected spec"
    if len(spec_ids) == 1:
        return spec_ids[0]
    preview = ", ".join(spec_ids[:limit])
    if len(spec_ids) > limit:
        preview = f"{preview}, ..."
    return f"{len(spec_ids)} specs ({preview})"


@dataclass
class PendingChatConfirmation:
    command: ChatCommand
    prompt: str


@dataclass
class ChatMessageRecord:
    message_id: str
    role: str
    text: str = ""
    renderable: Any | None = None


@dataclass
class ChatSessionState:
    session_key: str
    agent: str
    spec_id: str = ""
    messages: list[ChatMessageRecord] = field(default_factory=list)
    pending_confirmation: PendingChatConfirmation | None = None
    busy: bool = False
    context_summary: str = ""


@dataclass(frozen=True)
class ChatActionOutcome:
    summary: str
    renderable: Any | None = None


@dataclass(frozen=True)
class ChatProviderResult:
    text: str
    command: ChatCommand | None = None


class ChatProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _read_chat_provider_stderr(stderr_file: Any) -> str:
    """Return bounded stderr while retaining both the beginning and end."""
    try:
        stderr_file.flush()
        stderr_file.seek(0, os.SEEK_END)
        size = stderr_file.tell()
        if size <= CHAT_PROVIDER_STDERR_MAX_BYTES:
            stderr_file.seek(0)
            raw = stderr_file.read()
        else:
            half = CHAT_PROVIDER_STDERR_MAX_BYTES // 2
            stderr_file.seek(0)
            start = stderr_file.read(half)
            stderr_file.seek(-half, os.SEEK_END)
            end = stderr_file.read(half)
            raw = start + b"\n... stderr truncated ...\n" + end
    except (OSError, ValueError):
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _wait_chat_provider_process(
    proc: subprocess.Popen[str],
    *,
    timeout: float,
) -> int | None:
    """Wait for a provider process, tolerating small test-double APIs."""
    try:
        return proc.wait(timeout=timeout)
    except TypeError:
        return proc.wait()


def _signal_chat_provider_process_group(
    proc: subprocess.Popen[str],
    sig: signal.Signals,
) -> bool:
    """Signal the isolated provider process group, falling back to its leader."""
    pid = getattr(proc, "pid", 0)
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, sig)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return True
    except (OSError, ProcessLookupError):
        return False


def _terminate_chat_provider_process(proc: subprocess.Popen[str]) -> None:
    """Terminate a provider group, escalate to kill, and reap its leader."""
    try:
        running = proc.poll() is None
    except (OSError, ProcessLookupError):
        running = False

    if running:
        _signal_chat_provider_process_group(proc, signal.SIGTERM)
        try:
            _wait_chat_provider_process(
                proc,
                timeout=CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS,
            )
            return
        except subprocess.TimeoutExpired:
            _signal_chat_provider_process_group(proc, signal.SIGKILL)

    # Even an already-exited process needs wait() to release its process table
    # entry. After SIGKILL this second bounded wait normally returns at once.
    try:
        _wait_chat_provider_process(
            proc,
            timeout=CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS,
        )
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def _stream_chat_provider_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    provider_name: str,
    parse_line: Callable[[str], str],
):
    """Yield parsed stdout without allowing either provider pipe to deadlock.

    Stderr goes to a temporary file, so an arbitrarily chatty provider cannot
    fill a pipe while stdout is being streamed. A dedicated reader thread puts
    stdout lines onto a queue, allowing the consumer to enforce inactivity and
    react to generator cancellation. Every exit path terminates a still-live
    process group and waits for the leader.
    """
    with tempfile.TemporaryFile(mode="w+b") as stderr_file:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
        )
        assert proc.stdout is not None
        stdout = proc.stdout
        events: queue.Queue[tuple[str, object]] = queue.Queue()

        def _read_stdout() -> None:
            try:
                for line in stdout:
                    events.put(("line", line))
            except (OSError, ValueError) as exc:
                events.put(("error", exc))
            finally:
                events.put(("eof", None))

        reader = threading.Thread(
            target=_read_stdout,
            name=f"spec-chat-{provider_name.lower()}-stdout",
            daemon=True,
        )
        reader.start()
        stdout_eof = False
        return_code: int | None = None
        last_activity = time.monotonic()

        try:
            while True:
                if stdout_eof and events.empty():
                    return_code = proc.poll()
                    if return_code is not None:
                        break

                idle_for = time.monotonic() - last_activity
                remaining = CHAT_PROVIDER_INACTIVITY_TIMEOUT_SECONDS - idle_for
                if remaining <= 0:
                    raise ChatProviderError(
                        f"{provider_name} chat provider timed out after "
                        f"{CHAT_PROVIDER_INACTIVITY_TIMEOUT_SECONDS:g} seconds "
                        "without output.",
                        retryable=True,
                    )

                try:
                    event_type, payload = events.get(
                        timeout=min(CHAT_PROVIDER_POLL_SECONDS, remaining),
                    )
                except queue.Empty:
                    continue

                if event_type == "line":
                    last_activity = time.monotonic()
                    chunk = parse_line(str(payload))
                    if chunk:
                        yield chunk
                elif event_type == "eof":
                    stdout_eof = True
                else:
                    raise ChatProviderError(
                        f"{provider_name} chat provider stdout failed: {payload}",
                        retryable=True,
                    )
        finally:
            _terminate_chat_provider_process(proc)
            reader.join(timeout=CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS)
            stdout.close()

        if return_code != 0:
            stderr_output = _read_chat_provider_stderr(stderr_file)
            detail = stderr_output.strip() or f"{provider_name.lower()} exited with {return_code}"
            raise ChatProviderError(
                f"{provider_name} chat provider failed: {detail}",
                retryable=_is_transient_chat_provider_failure(detail),
            )


class ChatProvider(Protocol):
    def run_turn(
        self,
        *,
        session: ChatSessionState,
        user_message: str,
        context_summary: str,
        stream_text: Callable[[str], None],
    ) -> ChatProviderResult: ...


class _ChatEnvelopeBuffer:
    def __init__(self) -> None:
        self._raw = ""
        self._visible_length = 0
        self._visible_started = False

    def push(self, chunk: str) -> str:
        self._raw += chunk
        if not self._visible_started:
            marker_index = self._raw.find(CHAT_PROVIDER_MESSAGE_DELIMITER)
            if marker_index < 0:
                return ""
            self._visible_started = True
            visible = self._raw[marker_index + len(CHAT_PROVIDER_MESSAGE_DELIMITER) :]
            self._visible_length = len(visible)
            return visible

        marker_index = self._raw.find(CHAT_PROVIDER_MESSAGE_DELIMITER)
        if marker_index < 0:
            return ""
        visible = self._raw[marker_index + len(CHAT_PROVIDER_MESSAGE_DELIMITER) :]
        delta = visible[self._visible_length :]
        self._visible_length = len(visible)
        return delta

    def finalize(self) -> ChatProviderResult:
        raw = self._raw.strip()
        if not raw:
            return ChatProviderResult(text="")
        command: ChatCommand | None = None
        text = raw
        if CHAT_PROVIDER_MESSAGE_DELIMITER in raw:
            command_blob, text = raw.split(CHAT_PROVIDER_MESSAGE_DELIMITER, 1)
            command_line = command_blob.strip().splitlines()[0] if command_blob.strip() else ""
            if command_line.startswith(CHAT_PROVIDER_COMMAND_PREFIX):
                payload = command_line[len(CHAT_PROVIDER_COMMAND_PREFIX) :].strip()
                command = _parse_provider_command_json(payload)
        return ChatProviderResult(text=text.strip(), command=command)


class CliChatProvider:
    def __init__(self, *, agent: str, repo_root: Path) -> None:
        self.agent = agent
        self.repo_root = repo_root

    def run_turn(
        self,
        *,
        session: ChatSessionState,
        user_message: str,
        context_summary: str,
        stream_text: Callable[[str], None],
    ) -> ChatProviderResult:
        prompt = self._build_prompt(
            session=session,
            user_message=user_message,
            context_summary=context_summary,
        )
        for attempt in range(1, CHAT_PROVIDER_MAX_ATTEMPTS + 1):
            buffer = _ChatEnvelopeBuffer()
            visible_started = False
            try:
                for chunk in self._stream_model_output(prompt):
                    visible = buffer.push(chunk)
                    if visible:
                        visible_started = True
                        stream_text(visible)
                result = buffer.finalize()
                if not result.text and not result.command:
                    raise ChatProviderError(
                        "Chat provider returned an empty response.",
                        retryable=True,
                    )
                return result
            except ChatProviderError as exc:
                if visible_started or not exc.retryable or attempt >= CHAT_PROVIDER_MAX_ATTEMPTS:
                    if exc.retryable and not visible_started and attempt > 1:
                        raise RuntimeError(f"{exc} after {attempt} attempts with backoff.") from exc
                    raise RuntimeError(str(exc)) from exc
                time.sleep(_chat_provider_retry_delay_seconds(attempt))
        raise RuntimeError("Chat provider failed without producing a result.")

    def _build_prompt(
        self,
        *,
        session: ChatSessionState,
        user_message: str,
        context_summary: str,
    ) -> str:
        transcript = []
        for record in session.messages[-8:]:
            if record.role == "tool":
                continue
            content = (record.text or "").strip()
            if not content:
                continue
            transcript.append(f"{record.role.upper()}: {content}")
        transcript_text = "\n".join(transcript) or "(no prior conversation)"
        session_scope = (
            f"per-spec chat for {session.spec_id}" if session.spec_id else "global chat across all visible runs"
        )
        spec_hint = (
            f"Default spec_id for this session: {session.spec_id}"
            if session.spec_id
            else "No default spec_id is set for this session."
        )
        return (
            "You are the provider-backed chat control plane for the spec autopilot TUI.\n"
            "\n"
            "You help the user inspect run state and decide whether the host should execute one tool command.\n"
            "Use the supplied context summary directly; it contains the current run state and, for per-spec chats, "
            "the preloaded spec content and recent log tail.\n"
            "\n"
            "Rules:\n"
            "- Keep the visible reply concise and directly grounded in the provided context.\n"
            "- Every state-changing command requires host confirmation when you select it; "
            "you may suggest one, but the host will prompt separately.\n"
            "- `tail_log` and `stop_run` are live-run-only actions.\n"
            "- `add_retries` works on both live and dead runs; for failed/blocked runs it also auto-resumes.\n"
            "- Batch actions across multiple runs are allowed; use `spec_ids` for those targets.\n"
            "- In per-spec chats, the session spec_id is only a default. If the user explicitly names "
            "a different spec, use the explicit target from the latest message.\n"
            "- For commands other than `list_runs`, identify concrete targets with `spec_id` or "
            "`spec_ids`; do not rely on filters alone.\n"
            "- Only use `resolve_input` when the latest user message explicitly provides a final "
            "answer with a prefix like `answer:` or `resolve input:`.\n"
            "- Only use `record_steering` when the latest user message explicitly provides steering "
            "guidance with a prefix like `steer:` or `add steering:`.\n"
            "- If the recorded operator request looks like it needs code changes, tests, schema work, "
            "or implementation, tell the user to use `spec input --spec <id>` instead of resolving "
            "it inline.\n"
            "- If no tool action should run, return `null` for the command.\n"
            "\n"
            "Available command schema:\n"
            "{"
            '"name":"list_runs|show_run|tail_log|stop_run|resume_run|reset_run|add_retries|delete_spec|resolve_input|record_steering|help",'
            '"spec_id":"string",'
            '"spec_ids":["string"],'
            '"status_filter":"running|waiting|blocked|failed|stale|passed",'
            '"agent_filter":"claude|codex",'
            '"phase_filter":"bootstrap|scoping|intake|implement|verify|publish|review|merge|cleanup",'
            '"count":0,'
            '"extra_retries":0,'
            '"lines":40,'
            '"answer":"string",'
            '"guidance":"string"'
            "}\n"
            "\n"
            "Output format is mandatory:\n"
            "COMMAND_JSON: <JSON object or null>\n"
            "MESSAGE:\n"
            "<plain text for the user>\n"
            "\n"
            f"Session scope: {session_scope}\n"
            f"{spec_hint}\n"
            f"Selected agent/provider: {session.agent}\n"
            "\n"
            "Context summary:\n"
            f"{context_summary}\n"
            "\n"
            "Recent conversation:\n"
            f"{transcript_text}\n"
            "\n"
            "Latest user message:\n"
            f"{user_message}\n"
        )

    def _stream_model_output(self, prompt: str) -> list[str]:
        if self.agent == "codex":
            return self._stream_codex_output(prompt)
        if self.agent == "claude":
            return self._stream_claude_output(prompt)
        raise RuntimeError(f"Unsupported chat agent '{self.agent}'.")

    def _provider_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
            env.pop(name, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _stream_codex_output(self, prompt: str):
        command = [
            "codex",
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-c",
            "shell_environment_policy.inherit=none",
            "--json",
            prompt,
        ]
        with tempfile.TemporaryDirectory(prefix="autopilot-tui-chat-") as workspace:
            message_offsets: dict[str, int] = {}
            visible_order: list[str] = []

            def _parse_line(line: str) -> str:
                return _extract_codex_chat_text(line, message_offsets, visible_order)

            try:
                yield from _stream_chat_provider_process(
                    command,
                    cwd=Path(workspace),
                    env=self._provider_env(),
                    provider_name="Codex",
                    parse_line=_parse_line,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"Codex CLI is not installed: {exc}") from exc

    def _stream_claude_output(self, prompt: str):
        command = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--tools",
            "",
            "--",
            prompt,
        ]
        with tempfile.TemporaryDirectory(prefix="autopilot-tui-chat-") as workspace:
            try:
                yield from _stream_chat_provider_process(
                    command,
                    cwd=Path(workspace),
                    env=self._provider_env(),
                    provider_name="Claude",
                    parse_line=_extract_claude_chat_text,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"Claude CLI is not installed: {exc}") from exc


def _chat_session_key(spec_id: str = "") -> str:
    return f"spec:{spec_id}" if spec_id else CHAT_GLOBAL_SESSION_KEY


def _role_title(role: str) -> tuple[str, str]:
    if role == "user":
        return ("You", "cyan")
    if role == "assistant":
        return ("Agent", "green")
    if role == "tool":
        return ("Result", "yellow")
    return ("System", "magenta")


def _status_style(status: str) -> str:
    return {
        "running": "green",
        "waiting": "yellow",
        "blocked": "yellow",
        "failed": "red",
        "stale": "red",
        "passed": "cyan",
        "pending": "blue",
    }.get(status, "white")


def _render_status_transition(label: str, before: str, after: str) -> Text:
    before_value = before or "—"
    after_value = after or "—"
    return Text.assemble(
        (f"{label}: ", "bold"),
        (before_value, _status_style(before_value)),
        (" -> ", "bold"),
        (after_value, _status_style(after_value)),
    )


def _tail_text_lines(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Could not read log file '{path}': {exc}") from exc
    if lines <= 0:
        raise RuntimeError("Line count must be positive.")
    return "\n".join(content[-lines:]) if content else "(log is empty)"


def _input_requires_full_session(question: str) -> bool:
    normalized = question.lower()
    return any(hint in normalized for hint in CHAT_INPUT_CODE_CHANGE_HINTS)


def _is_transient_chat_provider_failure(detail: str) -> bool:
    normalized = detail.lower()
    if not normalized.strip():
        return True
    if "exited with" in normalized:
        return True
    return any(hint in normalized for hint in CHAT_TRANSIENT_PROVIDER_FAILURE_HINTS)


def _chat_provider_retry_delay_seconds(attempt: int) -> float:
    index = max(0, min(attempt - 1, len(CHAT_PROVIDER_RETRY_BACKOFF_SECONDS) - 1))
    return CHAT_PROVIDER_RETRY_BACKOFF_SECONDS[index]


def _normalize_chat_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_provider_command_json(payload: str) -> ChatCommand | None:
    if not payload or payload == "null":
        return None
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chat provider returned invalid command JSON: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuntimeError("Chat provider command payload must be a JSON object or null.")
    name = str(raw.get("name", "")).strip()
    if name not in CHAT_PROVIDER_COMMANDS:
        raise RuntimeError(f"Chat provider returned unsupported command '{name or '?'}'.")
    raw_spec_ids = raw.get("spec_ids", ())
    if raw_spec_ids in (None, ""):
        spec_ids: tuple[str, ...] = ()
    elif isinstance(raw_spec_ids, str):
        spec_ids = _normalize_spec_ids((raw_spec_ids,))
    elif isinstance(raw_spec_ids, (list, tuple)):
        spec_ids = _normalize_spec_ids(raw_spec_ids)
    else:
        raise RuntimeError("Chat provider spec_ids must be a string, array, or omitted.")
    return ChatCommand(
        name=name,
        spec_id=str(raw.get("spec_id", "")).strip(),
        spec_ids=spec_ids,
        status_filter=str(raw.get("status_filter", "")).strip(),
        agent_filter=str(raw.get("agent_filter", "")).strip(),
        phase_filter=str(raw.get("phase_filter", "")).strip(),
        count=_normalize_chat_int(raw.get("count", 0), 0),
        extra_retries=_normalize_chat_int(raw.get("extra_retries", 0), 0),
        lines=_normalize_chat_int(raw.get("lines", CHAT_DEFAULT_LOG_LINES), CHAT_DEFAULT_LOG_LINES),
        answer=str(raw.get("answer", "")).strip(),
        guidance=str(raw.get("guidance", "")).strip(),
    )


def _extract_codex_chat_text(
    line: str,
    offsets: dict[str, int],
    order: list[str],
) -> str:
    text = line.strip()
    if not text:
        return ""
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return ""
    event_type = str(event.get("type", "")).strip()
    if event_type not in {"item.updated", "item.completed"}:
        return ""
    item = event.get("item") or {}
    if str(item.get("type", "")).strip() != "agent_message":
        return ""
    item_id = str(item.get("id", "")).strip() or "assistant"
    message = str(item.get("text", ""))
    previous_length = offsets.get(item_id, 0)
    if item_id not in order:
        order.append(item_id)
    if previous_length > len(message):
        previous_length = 0
    offsets[item_id] = len(message)
    return message[previous_length:]


def _extract_claude_chat_text(line: str) -> str:
    text = line.strip()
    if not text:
        return ""
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if event.get("type") != "assistant":
        return ""
    message = event.get("message") or {}
    chunks: list[str] = []
    for item in message.get("content", []):
        if item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
    return "".join(chunks)


def _parse_explicit_resolve_input(
    message: str,
    spec_id: str,
) -> ChatCommand | None:
    if not spec_id:
        return None
    stripped = message.strip()
    patterns = (
        r"^answer\s*:\s*(.+)$",
        r"^answer\s+(.+)$",
        r"^resolve input\s*:\s*(.+)$",
        r"^resolve input\s+(.+)$",
        r"^resolve_input\s*:\s*(.+)$",
        r"^resolve_input\s+(.+)$",
        r"^submit answer\s*:\s*(.+)$",
        r"^submit answer\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, stripped, flags=re.IGNORECASE | re.DOTALL)
        if match is not None:
            answer = match.group(1).strip()
            return ChatCommand(name="resolve_input", spec_id=spec_id, answer=answer)
    return None


def _parse_explicit_operator_steering(
    message: str,
    spec_id: str,
) -> ChatCommand | None:
    stripped = message.strip()
    targeted_patterns = (
        r"^steer\s+([a-z0-9][a-z0-9-]*)\s*:\s*(.+)$",
        r"^steering\s+([a-z0-9][a-z0-9-]*)\s*:\s*(.+)$",
        r"^add steering\s+([a-z0-9][a-z0-9-]*)\s*:\s*(.+)$",
        r"^replace steering\s+([a-z0-9][a-z0-9-]*)\s*:\s*(.+)$",
    )
    for pattern in targeted_patterns:
        match = re.match(pattern, stripped, flags=re.IGNORECASE | re.DOTALL)
        if match is not None:
            target_spec = match.group(1).strip()
            guidance = match.group(2).strip()
            return ChatCommand(name="record_steering", spec_id=target_spec, guidance=guidance)
    if not spec_id:
        return None
    patterns = (
        r"^steer\s*:\s*(.+)$",
        r"^steering\s*:\s*(.+)$",
        r"^add steering\s*:\s*(.+)$",
        r"^replace steering\s*:\s*(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, stripped, flags=re.IGNORECASE | re.DOTALL)
        if match is not None:
            guidance = match.group(1).strip()
            return ChatCommand(name="record_steering", spec_id=spec_id, guidance=guidance)
    return None


def _latest_visible_rows(
    repo_root: Path,
    *,
    status_filter: str = "",
    agent_filter: str = "",
    phase_filter: str = "",
) -> tuple[SpecRow, ...]:
    rows = load_dashboard_snapshot(repo_root).rows
    filtered: list[SpecRow] = []
    for row in rows:
        if status_filter and row.status != status_filter:
            continue
        if agent_filter and row.agent != agent_filter:
            continue
        if phase_filter and row.phase != phase_filter:
            continue
        filtered.append(row)
    return tuple(filtered)


def _render_runs_table(rows: tuple[SpecRow, ...]) -> Any:
    if not rows:
        return Text("No runs matched the current filters.", style="yellow")

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("SPEC")
    table.add_column("AGENT")
    table.add_column("PHASE")
    table.add_column("ATTEMPTS")
    table.add_column("ENGINE")
    table.add_column("WORKER")
    table.add_column("STATUS")
    table.add_column("BRANCH")
    for row in rows:
        table.add_row(
            row.display_spec_id,
            row.agent,
            row.phase,
            row.retries,
            row.container_engine or "—",
            row.worker_source or "—",
            Text(row.status, style=_status_style(row.status)),
            row.branch,
        )
    return table


def _render_run_detail(repo_root: Path, run: orch.RunState) -> Table:
    worktree_path = run.worktree_path or str(orch.resolve_worktree_path(run, repo_root))
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Spec", run.spec_id)
    table.add_row("Run ID", run.run_id)
    table.add_row("Status", Text(run.status, style=_status_style(run.status)))
    table.add_row("Phase", run.phase or "—")
    table.add_row("Agent", run.agent or "—")
    if run.backend or run.safety_mode:
        backend = run.backend or "unknown"
        if run.backend_source:
            backend = f"{backend} ({run.backend_source})"
        table.add_row("Backend", backend)
        if run.backend == "container":
            repo_config = load_repo_spec_runtime_config(repo_root)
            table.add_row("Container Engine", repo_config.execution.container.engine)
            table.add_row("Worker Source", container_image_source(repo_config, repo_root))
        table.add_row("Safety Mode", run.safety_mode or "unknown")
    table.add_row("Attempts", orch.format_attempt_progress(run.attempts, run.retry_cap))
    table.add_row("Branch", run.branch or "—")
    table.add_row("Worktree", worktree_path)
    if run.readiness_status or run.readiness_head_sha or run.readiness_blocker:
        table.add_row("Readiness", run.readiness_status or "unknown")
        table.add_row("Ready Head", orch._short_sha(run.readiness_head_sha) if run.readiness_head_sha else "—")
        table.add_row("Ready Blocker", run.readiness_blocker or "—")
    table.add_row("Last Error", run.last_error or "—")

    common_root = autopilot.resolve_common_root(repo_root)
    operator_request = orch._load_operator_request(common_root, run)
    if operator_request is not None:
        table.add_row("Request Kind", operator_request.kind or "—")
        table.add_row("Request State", operator_request.status or "—")
        table.add_row("Request Prompt", operator_request.prompt or "—")
        table.add_row("Request Response", operator_request.response or "—")
    operator_steering = orch.OperatorSteering.load(common_root, run.run_id)
    if operator_steering is not None:
        table.add_row("Steering State", operator_steering.status or "—")
        table.add_row("Steering Guidance", operator_steering.message or "—")
        table.add_row("Steering By", operator_steering.provided_by or "—")
        if operator_steering.influenced_attempt_number is not None:
            table.add_row("Steering Attempt", str(operator_steering.influenced_attempt_number))
    diagnosis = orch.BlockDiagnosis.load(common_root, run.run_id)
    if diagnosis is not None:
        table.add_row("", "")
        table.add_row(
            "Needs Human",
            Text("YES", style="bold magenta") if diagnosis.requires_human_attention else Text("no"),
        )
        table.add_row("Diagnosis", diagnosis.summary or "—")
        table.add_row("Root Cause", diagnosis.root_cause or "—")
        table.add_row("Next Action", diagnosis.next_best_action or "—")

    return table


def _render_log_output(spec_id: str, lines: int, content: str) -> Any:
    return Group(
        Text(f"{spec_id} log tail ({lines} lines)", style="bold"),
        Text(content),
    )


def _render_batch_chat_results(
    results: list[tuple[str, ChatActionOutcome | None, str | None]],
) -> Any:
    panels: list[Panel] = []
    for spec_id, outcome, error in results:
        if error is None and outcome is not None:
            body = outcome.renderable if outcome.renderable is not None else Text(outcome.summary)
            border_style = "green"
        else:
            body = Text(error or "Unknown error.", style="red")
            border_style = "red"
        panels.append(Panel(body, title=spec_id, border_style=border_style, expand=True))
    return Group(*panels)


def _batch_action_summary(
    command_name: str,
    spec_ids: tuple[str, ...],
    success_count: int,
    failure_count: int,
) -> str:
    prefix = {
        "show_run": "Showing run details for",
        "tail_log": "Showing log output for",
        "stop_run": "Processed stop requests for",
        "resume_run": "Processed resume requests for",
        "reset_run": "Processed reset requests for",
        "add_retries": "Processed retry-cap updates for",
        "delete_spec": "Processed delete requests for",
        "resolve_input": "Processed input resolution for",
        "record_steering": "Processed steering updates for",
    }.get(command_name, "Processed chat command for")
    return f"{prefix} {_format_spec_targets(spec_ids)}. {success_count} succeeded, {failure_count} failed."


def _known_spec_ids(repo_root: Path) -> tuple[str, ...]:
    known: set[str] = set()
    for metadata in iter_spec_metadata(repo_root):
        known.add(metadata.spec_id)
    for row in load_dashboard_snapshot(repo_root).rows:
        known.add(row.spec_id)
    for run in orch.RunState.list_all(repo_root):
        known.add(run.spec_id)
    return tuple(sorted(known, key=len, reverse=True))


def _extract_spec_ids(message: str, repo_root: Path, *, fallback: str = "") -> tuple[str, ...]:
    lowered = message.lower()
    matches: list[tuple[int, int, str]] = []
    for spec_id in _known_spec_ids(repo_root):
        pattern = rf"(?<![a-z0-9-]){re.escape(spec_id)}(?![a-z0-9-])"
        for match in re.finditer(pattern, lowered):
            matches.append((match.start(), -len(spec_id), spec_id))
    matches.sort()
    spec_ids = _normalize_spec_ids([spec_id for _, _, spec_id in matches])
    if spec_ids:
        return spec_ids
    if fallback:
        return (fallback,)
    return ()


def _extract_spec_id(message: str, repo_root: Path, *, fallback: str = "") -> str:
    spec_ids = _extract_spec_ids(message, repo_root, fallback=fallback)
    return spec_ids[0] if spec_ids else ""


def _targeted_chat_command(name: str, spec_ids: tuple[str, ...], **kwargs: Any) -> ChatCommand | None:
    if not spec_ids:
        return None
    if len(spec_ids) == 1:
        return ChatCommand(name=name, spec_id=spec_ids[0], **kwargs)
    return ChatCommand(name=name, spec_ids=spec_ids, **kwargs)


def _parse_first_int(message: str) -> int:
    match = re.search(r"\b(\d+)\b", message)
    if match is None:
        return 0
    return int(match.group(1))


def _parse_list_runs_filters(message: str) -> ChatCommand:
    lowered = message.lower()
    status_filter = ""
    agent_filter = ""
    phase_filter = ""

    for status in CHAT_STATUS_VALUES:
        if re.search(rf"\b{re.escape(status)}\b", lowered):
            status_filter = status
            break

    agent_match = re.search(r"\bagent\s*(?:=|:)?\s*(claude|codex)\b", lowered)
    if agent_match:
        agent_filter = agent_match.group(1)
    elif " codex" in f" {lowered} ":
        agent_filter = "codex"
    elif " claude" in f" {lowered} ":
        agent_filter = "claude"

    phase_match = re.search(r"\bphase\s*(?:=|:)?\s*([a-z-]+)\b", lowered)
    if phase_match:
        phase_filter = phase_match.group(1)
    else:
        for phase in ("bootstrap", "scoping", "intake", "implement", "verify", "publish", "review", "merge", "cleanup"):
            if re.search(rf"\b{phase}\b", lowered):
                phase_filter = phase
                break

    return ChatCommand(
        name="list_runs",
        status_filter=status_filter,
        agent_filter=agent_filter,
        phase_filter=phase_filter,
    )


def parse_chat_command(message: str, repo_root: Path, *, default_spec_id: str = "") -> ChatCommand | None:
    lowered = re.sub(r"\s+", " ", message.lower()).strip()
    if not lowered:
        return None

    explicit_spec_ids = _extract_spec_ids(lowered, repo_root)
    spec_ids = explicit_spec_ids or ((default_spec_id,) if default_spec_id else ())
    spec_id = spec_ids[0] if spec_ids else ""
    resolve_input_spec_id = default_spec_id or spec_id
    explicit_resolution = _parse_explicit_resolve_input(message, resolve_input_spec_id)
    if explicit_resolution is not None:
        return explicit_resolution
    explicit_steering = _parse_explicit_operator_steering(message, resolve_input_spec_id)
    if explicit_steering is not None:
        return explicit_steering

    _list_run_phrases = (
        "list runs",
        "show runs",
        "current runs",
        "what is running",
        "what's running",
        "which runs",
    )
    if any(phrase in lowered for phrase in _list_run_phrases):
        return _parse_list_runs_filters(lowered)
    if lowered.startswith("list ") and "run" in lowered:
        return _parse_list_runs_filters(lowered)

    if any(token in lowered for token in ("tail log", "show log", "view log", "log tail")):
        return _targeted_chat_command(
            name="tail_log",
            spec_ids=spec_ids,
            lines=_parse_first_int(lowered) or CHAT_DEFAULT_LOG_LINES,
        )
    if lowered.startswith("log ") or lowered == "log":
        return _targeted_chat_command(
            name="tail_log",
            spec_ids=spec_ids,
            lines=_parse_first_int(lowered) or CHAT_DEFAULT_LOG_LINES,
        )

    if lowered.startswith("stop") or " stop " in f" {lowered} ":
        return _targeted_chat_command("stop_run", spec_ids)

    if lowered.startswith("reset") or " reset " in f" {lowered} ":
        return _targeted_chat_command("reset_run", spec_ids)

    if lowered.startswith("delete") or lowered.startswith("remove spec") or lowered.startswith("remove task"):
        return _targeted_chat_command("delete_spec", spec_ids)

    if "retry" in lowered and any(word in lowered for word in ("add", "increase", "raise", "more", "extra")):
        return _targeted_chat_command(
            name="add_retries",
            spec_ids=spec_ids,
            count=_parse_first_int(lowered) or 1,
        )

    if lowered.startswith("resume") or " resume " in f" {lowered} ":
        extra_retries = 0
        retry_match = re.search(r"(?:add|with|plus)\s+(\d+)\s+(?:more\s+)?retries?", lowered)
        if retry_match is not None:
            extra_retries = int(retry_match.group(1))
        return _targeted_chat_command(
            name="resume_run",
            spec_ids=spec_ids,
            extra_retries=extra_retries,
        )

    if lowered.startswith("show") or lowered.startswith("status") or lowered.startswith("details"):
        if spec_id:
            return _targeted_chat_command("show_run", spec_ids)

    if lowered.startswith("help") or lowered == "?":
        return ChatCommand(name="help")

    return None


def _chat_help_text(spec_id: str = "") -> str:
    examples = [
        "list runs",
        "show run my-feature",
        "show run my-feature and ui-feature",
        "tail log my-feature 50",
        "resume my-feature with 2 more retries",
        "resume my-feature and ui-feature with 2 more retries",
        "add 3 retries to my-feature",
        "steer: retry with the smaller schema change first",
        "stop my-feature",
        "stop my-feature and ui-feature",
        "reset my-feature",
        "delete my-feature",
    ]
    if spec_id:
        examples.extend(
            [
                "show run",
                "tail log",
                "stop it",
            ]
        )
    return "Supported commands:\n" + "\n".join(f"- {item}" for item in examples)


def _append_chat_action_log(
    repo_root: Path,
    *,
    action: str,
    spec_id: str = "",
    status: str,
    detail: str = "",
    arguments: dict[str, Any] | None = None,
) -> None:
    path = autopilot.autopilot_log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": autopilot.now_iso(),
        "source": "tui-chat",
        "action": action,
        "spec_id": spec_id,
        "status": status,
        "detail": detail,
        "arguments": arguments or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(classes="modal"):
            with Vertical(classes="dialog"):
                yield Label(self._title, classes="dialog-title")
                yield Static(self._message, classes="dialog-body")
                yield Static("Press y to confirm, n or Esc to cancel.", classes="dialog-help")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ResetScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("y", "reset", "Reset"),
        Binding("enter", "reset", "Reset"),
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "Cancel"),
    ]

    def __init__(self, spec_id: str) -> None:
        super().__init__()
        self._spec_id = spec_id

    def compose(self) -> ComposeResult:
        with Container(classes="modal"):
            with Vertical(classes="dialog"):
                yield Label(f"Reset {self._spec_id}?", classes="dialog-title")
                yield Static(
                    "This stops any live run first, removes stale worktrees, branches, "
                    "logs, and run state for the current attempt, then starts a fresh run.",
                    classes="dialog-body",
                )
                yield Static(
                    "Press y or Enter to reset and start a fresh run, or Esc to cancel.",
                    classes="dialog-help",
                )

    def action_reset(self) -> None:
        self.dismiss("reset")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RetryCountScreen(ModalScreen[int | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, spec_id: str) -> None:
        super().__init__()
        self._spec_id = spec_id

    def compose(self) -> ComposeResult:
        with Container(classes="modal"):
            with Vertical(classes="dialog"):
                yield Label(f"Add retries for {self._spec_id}", classes="dialog-title")
                yield Static("Enter a positive integer and press Enter.", classes="dialog-body")
                yield Input(
                    placeholder="Retries",
                    restrict=r"[0-9]*",
                    type="integer",
                    id="retry-count-input",
                )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        try:
            count = int(raw)
        except ValueError:
            self.app.notify("Retry count must be an integer.", severity="error")
            return
        if count <= 0:
            self.app.notify("Retry count must be positive.", severity="error")
            return
        self.dismiss(count)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SpecDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("c", "chat", "Chat"),
        Binding("up", "scroll_up", "Up", show=False),
        Binding("down", "scroll_down", "Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("end", "follow", "Follow", show=False),
        Binding("home", "scroll_home", "Top", show=False),
    ]

    def __init__(self, spec_id: str) -> None:
        super().__init__()
        self.spec_id = spec_id
        self._current_log_path: Path | None = None
        self._log_position = 0
        self._follow_log = True
        self._placeholder_visible = False

    def compose(self) -> ComposeResult:
        yield Static(id="detail-header")
        yield Static(id="detail-hint")
        yield RichLog(id="detail-log", wrap=False, highlight=False, markup=False, auto_scroll=False)

    def on_mount(self) -> None:
        if isinstance(self.app, AutopilotWatchApp):
            self.app.refresh_detail_screen(self)

    def refresh_row(self, row: SpecRow | None, repo_root: Path) -> None:
        header = self.query_one("#detail-header", Static)
        hint = self.query_one("#detail-hint", Static)
        if row is None:
            header.update(f"{self.spec_id}  phase=—  attempts=—  agent=—  elapsed=—  branch=—")
            hint.update("No visible run state for this spec.")
            self._refresh_log(None)
            return

        header.update(
            f"{row.display_spec_id}  phase={row.phase}  attempts={row.retries}  "
            f"agent={row.agent}  elapsed={row.elapsed}  branch={row.branch}"
        )
        if row.requires_human_attention:
            hint.update(
                f"[bold magenta]Needs human attention:[/] {rich_escape(row.diagnosis_summary)}\n"
                f"[bold]Suggested action:[/] {rich_escape(row.diagnosis_next_action)}\n"
                "Press c to open chat and respond."
            )
        else:
            hint.update("Up/PageUp freeze auto-follow. End jumps to bottom and resumes follow mode.")
        self._refresh_log(resolve_log_path(repo_root, row.spec_id, run_id=row.run_id))

    def _refresh_log(self, log_path: Path | None) -> None:
        widget = self.query_one("#detail-log", RichLog)
        if log_path is None or not log_path.exists():
            if self._current_log_path is not None:
                self._current_log_path = None
                self._log_position = 0
                widget.clear()
                self._placeholder_visible = False
            if not self._placeholder_visible:
                widget.clear()
                widget.write("Waiting for a log file...")
                self._placeholder_visible = True
            return

        if self._current_log_path != log_path:
            self._current_log_path = log_path
            self._log_position = 0
            widget.clear()
            self._placeholder_visible = False

        try:
            if log_path.stat().st_size < self._log_position:
                self._log_position = 0
                widget.clear()
                self._placeholder_visible = False
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._log_position)
                chunk = handle.read()
                self._log_position = handle.tell()
        except OSError:
            return

        if chunk:
            if self._placeholder_visible:
                widget.clear()
                self._placeholder_visible = False
            for line in chunk.splitlines():
                widget.write(line)
            if self._follow_log:
                widget.scroll_end(animate=False)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_chat(self) -> None:
        if isinstance(self.app, AutopilotWatchApp):
            self.app.open_chat(self.spec_id)

    def action_scroll_up(self) -> None:
        self._follow_log = False
        self.query_one("#detail-log", RichLog).scroll_up(animate=False)

    def action_scroll_down(self) -> None:
        self._follow_log = False
        self.query_one("#detail-log", RichLog).scroll_down(animate=False)

    def action_page_up(self) -> None:
        self._follow_log = False
        self.query_one("#detail-log", RichLog).scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._follow_log = False
        self.query_one("#detail-log", RichLog).scroll_page_down(animate=False)

    def action_follow(self) -> None:
        self._follow_log = True
        self.query_one("#detail-log", RichLog).scroll_end(animate=False)

    def action_scroll_home(self) -> None:
        self._follow_log = False
        self.query_one("#detail-log", RichLog).scroll_home(animate=False)


class ChatScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("up", "scroll_up", "Up", show=False),
        Binding("down", "scroll_down", "Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("end", "follow", "Follow", show=False),
        Binding("home", "scroll_home", "Top", show=False),
    ]

    def __init__(self, session_key: str) -> None:
        super().__init__()
        self.session_key = session_key
        self._follow_log = True

    def compose(self) -> ComposeResult:
        yield Static(id="chat-header")
        yield Static(id="chat-hint")
        yield RichLog(id="chat-log", wrap=True, highlight=False, markup=False, auto_scroll=False)
        yield Input(placeholder="Ask about runs or issue a command", id="chat-input")

    def on_mount(self) -> None:
        if isinstance(self.app, AutopilotWatchApp):
            self.app.refresh_chat_screen(self)

    def refresh_session(self, session: ChatSessionState) -> None:
        header = self.query_one("#chat-header", Static)
        hint = self.query_one("#chat-hint", Static)
        widget = self.query_one("#chat-log", RichLog)
        input_widget = self.query_one("#chat-input", Input)

        if session.spec_id:
            header.update(f"{session.spec_id} chat  agent={session.agent}")
        else:
            header.update(f"Global chat  agent={session.agent}")

        if session.pending_confirmation is not None:
            hint.update("Pending confirmation. Reply with y/n. Esc returns to the previous screen.")
        elif session.busy:
            hint.update("Agent is responding. Esc returns to the previous screen.")
        else:
            hint.update("Ask for run status, logs, retries, resume/reset/delete, or waiting-input resolution.")

        input_widget.disabled = session.busy
        input_widget.placeholder = (
            "Reply y/n" if session.pending_confirmation is not None else "Ask about runs or issue a command"
        )

        widget.clear()
        for record in session.messages:
            widget.write(self._render_message(record), scroll_end=False)
        if not session.messages:
            widget.write(
                Panel(
                    Text("No messages yet."),
                    title="System",
                    border_style="magenta",
                ),
                scroll_end=False,
            )
        if self._follow_log:
            widget.scroll_end(animate=False)
        if not session.busy:
            input_widget.focus()

    def _render_message(self, record: ChatMessageRecord) -> Panel:
        title, color = _role_title(record.role)
        body = record.renderable if record.renderable is not None else Text(record.text or " ")
        return Panel(body, title=title, border_style=color, expand=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not isinstance(self.app, AutopilotWatchApp):
            return
        value = event.value.strip()
        if not value:
            return
        event.input.value = ""
        self.app.handle_chat_user_message(self.session_key, value)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_scroll_up(self) -> None:
        self._follow_log = False
        self.query_one("#chat-log", RichLog).scroll_up(animate=False)

    def action_scroll_down(self) -> None:
        self._follow_log = False
        self.query_one("#chat-log", RichLog).scroll_down(animate=False)

    def action_page_up(self) -> None:
        self._follow_log = False
        self.query_one("#chat-log", RichLog).scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._follow_log = False
        self.query_one("#chat-log", RichLog).scroll_page_down(animate=False)

    def action_follow(self) -> None:
        self._follow_log = True
        self.query_one("#chat-log", RichLog).scroll_end(animate=False)

    def action_scroll_home(self) -> None:
        self._follow_log = False
        self.query_one("#chat-log", RichLog).scroll_home(animate=False)


class AutopilotWatchApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #watch-body {
        height: 1fr;
        padding: 1 1 0 1;
    }

    #summary {
        padding: 0 1;
        height: auto;
        text-style: bold;
    }

    .section-title {
        padding: 1 0 0 0;
        text-style: bold;
    }

    DataTable {
        height: 1fr;
    }

    #queue-table {
        height: 12;
    }

    #detail-header {
        padding: 1 1 0 1;
        text-style: bold;
    }

    #detail-hint {
        padding: 0 1 1 1;
        color: $text-muted;
    }

    #detail-log {
        height: 1fr;
        margin: 0 1 1 1;
        border: round $accent;
    }

    #chat-header {
        padding: 1 1 0 1;
        text-style: bold;
    }

    #chat-hint {
        padding: 0 1 1 1;
        color: $text-muted;
    }

    #chat-log {
        height: 1fr;
        margin: 0 1;
        border: round $accent;
    }

    #chat-input {
        margin: 0 1 1 1;
    }

    .modal {
        align: center middle;
        width: 100%;
        height: 100%;
    }

    .dialog {
        width: 72;
        max-width: 96;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }

    .dialog-title {
        padding-bottom: 1;
        text-style: bold;
    }

    .dialog-help {
        padding-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "open_selected", "Open", show=False),
        Binding("c", "chat_global", "Chat"),
        Binding("s", "stop_selected", "Stop"),
        Binding("r", "resume_selected", "Resume"),
        Binding("R", "reset_selected", "Reset"),
        Binding("+", "add_retries_selected", "Add Retries"),
        Binding("d", "delete_selected", "Delete"),
    ]

    def __init__(
        self,
        *,
        repo_root: Path,
        refresh_interval: int,
        agent_filter: str = "",
        snapshot_loader=load_dashboard_snapshot,
        live_checker=None,
        stop_runner=None,
        retry_runner=None,
        resume_runner=None,
        reset_runner=None,
        delete_runner=None,
        chat_provider_factory=None,
        chat_stream_delay: float = CHAT_STREAM_DELAY_SECONDS,
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.refresh_interval = refresh_interval
        self.agent_filter = agent_filter
        self.chat_stream_delay = chat_stream_delay
        self.snapshot_loader = snapshot_loader
        self._uses_default_resume_runner = resume_runner is None
        self._uses_default_reset_runner = reset_runner is None
        self.live_checker = live_checker or (lambda spec_id: is_spec_live(self.repo_root, spec_id))
        self.stop_runner = stop_runner or (lambda spec_id: orch.stop_run(spec_id, repo_root=self.repo_root))
        self.retry_runner = retry_runner or (
            lambda spec_id, count: orch.add_retries(spec_id, count, repo_root=self.repo_root)
        )
        self.resume_runner = resume_runner or (lambda spec_id: resume_spec_run(self.repo_root, spec_id))
        self.reset_runner = reset_runner or (
            lambda spec_id, agent: reset_spec_run(
                self.repo_root,
                spec_id,
                agent=agent,
            )
        )
        self.delete_runner = delete_runner or (lambda spec_id: delete_spec_artifacts(self.repo_root, spec_id))
        self.chat_provider_factory = chat_provider_factory or (
            lambda agent, repo_root: CliChatProvider(agent=agent, repo_root=repo_root)
        )
        self.snapshot = DashboardSnapshot(rows=(), queue=(), merged_count=0)
        self._row_order: list[str] = []
        self._queue_order: list[str] = []
        self.chat_sessions: dict[str, ChatSessionState] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="watch-body"):
            yield Static(id="summary")
            yield Static("Specs", classes="section-title")
            yield DataTable(id="spec-table")
            yield Static("Queue", classes="section-title")
            yield DataTable(id="queue-table")
        yield Footer()

    def on_mount(self) -> None:
        spec_table = self.query_one("#spec-table", DataTable)
        spec_table.cursor_type = "row"
        spec_table.add_columns(
            "SPEC",
            "AGENT",
            "PHASE",
            "ATTEMPTS",
            "OWNER",
            "BACKEND",
            "ENGINE",
            "WORKER",
            "SAFETY",
            "LEASE",
            "HEARTBEAT",
            "EXPIRES",
            "ELAPSED",
            "STATUS",
        )

        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.cursor_type = "row"
        queue_table.add_columns("SPEC", "AGENT", "BACKEND", "ENGINE", "WORKER", "SAFETY", "UNLOCKS", "PRIORITY", "STATE")

        self.refresh_snapshot()
        self._restore_table_focus("spec-table")
        self.set_interval(self.refresh_interval, self.refresh_snapshot)

    def refresh_snapshot(self) -> None:
        selected_spec = self.current_selected_spec_id()
        selected_table = self.current_selected_table_id()
        self.snapshot = self.snapshot_loader(self.repo_root, self.agent_filter)
        self._refresh_summary()
        self._refresh_spec_table(selected_spec)
        self._refresh_queue_table(selected_spec)

        if not isinstance(self.screen, (SpecDetailScreen, ChatScreen, ModalScreen)):
            self._restore_table_focus(selected_table)

        if isinstance(self.screen, SpecDetailScreen):
            self.refresh_detail_screen(self.screen)
        elif isinstance(self.screen, ChatScreen):
            self.refresh_chat_screen(self.screen)

    def refresh_detail_screen(self, screen: SpecDetailScreen) -> None:
        row = self._row_for_spec(screen.spec_id)
        screen.refresh_row(row, self.repo_root)

    def _container_engine_for_backend(self, backend: str) -> str:
        if backend != "container":
            return "—"
        return load_repo_spec_runtime_config(self.repo_root).execution.container.engine

    def _worker_source_for_backend(self, backend: str) -> str:
        if backend != "container":
            return "—"
        repo_config = load_repo_spec_runtime_config(self.repo_root)
        return container_image_source(repo_config, self.repo_root)

    def _spec_id_from_session_key(self, session_key: str) -> str:
        if session_key == CHAT_GLOBAL_SESSION_KEY:
            return ""
        return session_key.split(":", 1)[1] if ":" in session_key else ""

    def _chat_agent(self, spec_id: str = "") -> str:
        if self.agent_filter:
            return self.agent_filter
        config = load_repo_spec_runtime_config(self.repo_root)
        if spec_id:
            row = self._row_for_spec(spec_id)
            preferred = _default_agent(
                self.repo_root,
                spec_id,
                fallback_agent=row.agent if row else "",
            )
        else:
            preferred = config.agents.default
        available = _available_chat_agents(self.repo_root)
        if preferred in available:
            return preferred
        if available:
            return available[0]
        return preferred

    def ensure_chat_session(self, spec_id: str = "") -> ChatSessionState:
        session_key = _chat_session_key(spec_id)
        existing = self.chat_sessions.get(session_key)
        if existing is not None:
            existing.agent = self._chat_agent(existing.spec_id)
            existing.context_summary = self._build_chat_context_summary(existing.spec_id)
            return existing

        self.chat_sessions[session_key] = ChatSessionState(
            session_key=session_key,
            spec_id=spec_id,
            agent=self._chat_agent(spec_id),
            context_summary=self._build_chat_context_summary(spec_id),
        )
        if spec_id:
            self._add_chat_message(
                session_key,
                "system",
                text=f"Loaded run state, spec content, and recent log tail for {spec_id}.",
            )
            run = _latest_run(self.repo_root, spec_id)
            common_root = autopilot.resolve_common_root(self.repo_root)
            request = (
                orch._load_operator_request(common_root, run)
                if run is not None
                else None
            )
            steering = (
                orch.OperatorSteering.load(common_root, run.run_id)
                if run is not None and run.run_id
                else None
            )
            if run is not None and run.status == "waiting-for-input" and request is not None:
                question = request.prompt or "Operator intervention required."
                if request.requires_full_session:
                    message = (
                        f"{spec_id} is waiting for operator intervention.\n\nRequest: {question}\n\n"
                        f"This looks like it needs code changes or verification. "
                        f"Use `spec input --spec {spec_id}` from the worktree."
                    )
                else:
                    message = (
                        f"{spec_id} is waiting for operator intervention.\n\n"
                        f"Kind: {request.kind}\n"
                        f"Request: {question}\n\n"
                        "Reply with `answer: <your response>` and I’ll record it and resume the run."
                    )
                self._add_chat_message(session_key, "assistant", text=message)
            elif run is not None and run.run_id:
                diagnosis = orch.BlockDiagnosis.load(common_root, run.run_id)
                if diagnosis is not None and diagnosis.requires_human_attention:
                    message = _human_attention_guidance(spec_id, diagnosis)
                    self._add_chat_message(session_key, "assistant", text=message)
                elif steering is not None and steering.status == "active":
                    self._add_chat_message(
                        session_key,
                        "assistant",
                        text=(
                            f"{spec_id} has active proactive steering recorded.\n\n"
                            f"Guidance: {steering.message}\n\n"
                            "Use `resume`, `add retries N`, or replace it with `steer: <guidance>`."
                        ),
                    )
                else:
                    self._add_chat_message(
                        session_key,
                        "assistant",
                        text="Ask for run details, logs, retries, steering, resume/reset/delete, or input resolution.",
                    )
            else:
                self._add_chat_message(
                    session_key,
                    "assistant",
                    text="Ask for run details, logs, retries, steering, resume/reset/delete, or input resolution.",
                )
        else:
            self._add_chat_message(
                session_key,
                "assistant",
                text="Global run control is ready. Ask me to list runs or inspect and control a specific spec.",
            )
        return self.chat_sessions[session_key]

    def _build_chat_context_summary(self, spec_id: str = "") -> str:
        if not spec_id:
            rows = self.snapshot.rows or self.snapshot_loader(self.repo_root, self.agent_filter).rows
            if not rows:
                return "No visible runs are loaded for global chat."
            summary_lines = [
                (
                    f"{row.spec_id}: status={row.status}, phase={row.phase}, agent={row.agent}, "
                    f"attempts={row.retries}, branch={row.branch}"
                )
                for row in rows
            ]
            return f"{len(rows)} visible runs loaded for global chat.\nCurrent runs:\n" + "\n".join(summary_lines)

        run = _latest_run(self.repo_root, spec_id)
        log_summary = "No log tail loaded."
        log_path = resolve_log_path(self.repo_root, spec_id, run_id=run.run_id if run else "")
        if log_path is not None and log_path.exists():
            log_summary = _tail_text_lines(log_path, min(CHAT_DEFAULT_LOG_LINES, 10))
        spec_path = self.repo_root / SPEC_RUNTIME_CONFIG.paths.specs_dir / f"{spec_id}.md"
        if run is not None:
            try:
                worktree_root = orch.resolve_worktree_path(run, self.repo_root)
            except RuntimeError:
                worktree_root = self.repo_root
            candidate = worktree_root / (run.spec_path or "")
            if run.spec_path and candidate.exists():
                spec_path = candidate
        if not spec_path.exists():
            task_path = self.repo_root / SPEC_RUNTIME_CONFIG.paths.task_specs_dir / f"{spec_id}.md"
            if task_path.exists():
                spec_path = task_path
        spec_summary = ""
        if spec_path.exists():
            spec_summary = spec_path.read_text(encoding="utf-8", errors="replace")
        status = run.status if run is not None else "no-run"
        common_root = autopilot.resolve_common_root(self.repo_root)
        operator_request = (
            orch._load_operator_request(common_root, run)
            if run is not None
            else None
        )
        operator_steering = (
            orch.OperatorSteering.load(common_root, run.run_id)
            if run is not None and run.run_id
            else None
        )
        question = operator_request.prompt if operator_request is not None else (run.input_question if run is not None else "")
        steering_context = ""
        if operator_steering is not None:
            steering_context = (
                f"\noperator_steering:\n"
                f"  status={operator_steering.status}\n"
                f"  guidance={operator_steering.message}\n"
                f"  provided_by={operator_steering.provided_by}\n"
                f"  provided_at={operator_steering.provided_at}"
            )
        diagnosis_context = ""
        if run is not None and run.run_id:
            diagnosis = orch.BlockDiagnosis.load(common_root, run.run_id)
            if diagnosis is not None:
                diagnosis_context = (
                    f"\nblock_diagnosis:\n"
                    f"  summary={diagnosis.summary}\n"
                    f"  root_cause={diagnosis.root_cause}\n"
                    f"  category={diagnosis.category}\n"
                    f"  requires_human_attention={diagnosis.requires_human_attention}\n"
                    f"  next_best_action={diagnosis.next_best_action}"
                )
        return (
            f"status={status}\nquestion={question}{steering_context}{diagnosis_context}\n"
            f"log_tail=\n{log_summary}\nspec=\n{spec_summary}"
        )

    def refresh_chat_screen(self, screen: ChatScreen) -> None:
        session = self.ensure_chat_session(self._spec_id_from_session_key(screen.session_key))
        try:
            screen.refresh_session(session)
        except NoMatches:
            return

    def _add_chat_message(
        self,
        session_key: str,
        role: str,
        *,
        text: str = "",
        renderable: Any | None = None,
    ) -> str:
        session = self.chat_sessions.setdefault(
            session_key,
            ChatSessionState(session_key=session_key, agent=self._chat_agent()),
        )
        message_id = uuid.uuid4().hex
        session.messages.append(
            ChatMessageRecord(
                message_id=message_id,
                role=role,
                text=text,
                renderable=renderable,
            )
        )
        if isinstance(self.screen, ChatScreen) and self.screen.session_key == session_key:
            self.refresh_chat_screen(self.screen)
        return message_id

    def _append_chat_chunk(self, session_key: str, message_id: str, chunk: str) -> None:
        session = self.chat_sessions[session_key]
        for message in session.messages:
            if message.message_id == message_id:
                message.text += chunk
                break
        if isinstance(self.screen, ChatScreen) and self.screen.session_key == session_key:
            self.refresh_chat_screen(self.screen)

    def _set_chat_busy(self, session_key: str, busy: bool) -> None:
        session = self.chat_sessions[session_key]
        session.busy = busy
        if isinstance(self.screen, ChatScreen) and self.screen.session_key == session_key:
            self.refresh_chat_screen(self.screen)

    def _set_pending_confirmation(
        self,
        session_key: str,
        pending: PendingChatConfirmation | None,
    ) -> None:
        session = self.chat_sessions[session_key]
        session.pending_confirmation = pending
        if isinstance(self.screen, ChatScreen) and self.screen.session_key == session_key:
            self.refresh_chat_screen(self.screen)

    def _create_chat_provider(self, agent: str) -> ChatProvider:
        return self.chat_provider_factory(agent, self.repo_root)

    def open_chat(self, spec_id: str = "") -> None:
        session = self.ensure_chat_session(spec_id)
        if isinstance(self.screen, ChatScreen) and self.screen.session_key == session.session_key:
            return
        self.push_screen(ChatScreen(session.session_key))

    def handle_chat_user_message(self, session_key: str, message: str) -> None:
        session = self.chat_sessions[session_key]
        if session.busy:
            return
        self._add_chat_message(session_key, "user", text=message)
        if session.pending_confirmation is None:
            command = parse_chat_command(
                message,
                self.repo_root,
                default_spec_id=session.spec_id,
            )
            if command is not None and command.name in CHAT_DESTRUCTIVE_ACTIONS:
                prompt = self._confirmation_prompt(command)
                self._set_pending_confirmation(
                    session_key,
                    PendingChatConfirmation(command=command, prompt=prompt),
                )
                self._add_chat_message(session_key, "assistant", text=prompt)
                return
        self._set_chat_busy(session_key, True)
        self.run_worker(
            lambda: self._process_chat_turn(session_key, message),
            thread=True,
            exclusive=True,
            group=f"chat-{session_key}",
        )

    def _process_chat_turn(self, session_key: str, message: str) -> None:
        try:
            session = self.chat_sessions[session_key]
            provider_selected_command = False
            if session.pending_confirmation is not None:
                self._process_confirmation(session_key, message)
                return

            run = _latest_run(self.repo_root, session.spec_id) if session.spec_id else None
            session.context_summary = self._build_chat_context_summary(session.spec_id)
            command = parse_chat_command(
                message,
                self.repo_root,
                default_spec_id=session.spec_id,
            )
            if command is not None and command.name == "help":
                self._stream_assistant_text(session_key, _chat_help_text(session.spec_id))
                return

            if (
                command is not None
                and command.name == "resolve_input"
                and run is not None
                and (
                    (
                        (request := orch._load_operator_request(autopilot.resolve_common_root(self.repo_root), run))
                        is not None
                        and request.requires_full_session
                    )
                    or _input_requires_full_session(run.input_question or "")
                )
            ):
                request_prompt = (
                    request.prompt
                    if request is not None
                    else (run.input_question or "Agent requires human input.")
                )
                self._stream_assistant_text(
                    session_key,
                    (
                        f"{session.spec_id} is waiting for operator intervention, but the recorded request "
                        f"likely needs code changes or verification. Use "
                        f"`spec input --spec {session.spec_id}` instead.\n\n"
                        f"Question: {request_prompt}"
                    ),
                )
                return

            if command is None:
                provider = self._create_chat_provider(session.agent)
                assistant_message_id: str | None = None
                streamed_any = False

                def ensure_assistant_message() -> str:
                    nonlocal assistant_message_id
                    if assistant_message_id is None:
                        assistant_message_id = self.call_from_thread(
                            self._add_chat_message,
                            session_key,
                            "assistant",
                            text="",
                            renderable=None,
                        )
                    return assistant_message_id

                def stream_text(chunk: str) -> None:
                    nonlocal streamed_any
                    streamed_any = True
                    self.call_from_thread(
                        self._append_chat_chunk,
                        session_key,
                        ensure_assistant_message(),
                        chunk,
                    )

                result = provider.run_turn(
                    session=session,
                    user_message=message,
                    context_summary=session.context_summary,
                    stream_text=stream_text,
                )
                if not streamed_any and result.text.strip():
                    self.call_from_thread(
                        self._append_chat_chunk,
                        session_key,
                        ensure_assistant_message(),
                        result.text,
                    )
                command = result.command
                provider_selected_command = command is not None
                if command is None and not result.text.strip():
                    self._stream_assistant_text(session_key, _chat_help_text(session.spec_id))
                    return

            if command is None:
                return

            if (
                command.name in CHAT_DESTRUCTIVE_ACTIONS
                or (
                    provider_selected_command
                    and _provider_command_requires_confirmation(command)
                )
            ):
                prompt = self._confirmation_prompt(command)
                self.call_from_thread(
                    self._set_pending_confirmation,
                    session_key,
                    PendingChatConfirmation(command=command, prompt=prompt),
                )
                self._stream_assistant_text(session_key, prompt)
                return

            outcome = self._execute_chat_command(command)
            self._stream_assistant_text(session_key, outcome.summary)
            if outcome.renderable is not None:
                self.call_from_thread(
                    self._add_chat_message,
                    session_key,
                    "tool",
                    text="",
                    renderable=outcome.renderable,
                )
        except RuntimeError as exc:
            self._stream_assistant_text(session_key, str(exc))
        finally:
            self.call_from_thread(self._set_chat_busy, session_key, False)

    def _process_confirmation(self, session_key: str, message: str) -> None:
        session = self.chat_sessions[session_key]
        pending = session.pending_confirmation
        if pending is None:
            self._stream_assistant_text(session_key, "There is no pending confirmation.")
            return

        lowered = message.strip().lower()
        if lowered in {"y", "yes"}:
            self.call_from_thread(self._set_pending_confirmation, session_key, None)
            outcome = self._execute_chat_command(pending.command)
            self._stream_assistant_text(session_key, outcome.summary)
            if outcome.renderable is not None:
                self.call_from_thread(
                    self._add_chat_message,
                    session_key,
                    "tool",
                    text="",
                    renderable=outcome.renderable,
                )
            return

        if lowered in {"n", "no"}:
            self.call_from_thread(self._set_pending_confirmation, session_key, None)
            target_spec_ids = _command_target_spec_ids(pending.command)
            _append_chat_action_log(
                self.repo_root,
                action=pending.command.name,
                spec_id=pending.command.spec_id,
                status="cancelled",
                detail="Cancelled by user",
                arguments=pending.command.__dict__.copy(),
            )
            self._stream_assistant_text(
                session_key,
                f"Cancelled {pending.command.name.replace('_', ' ')} for {_format_spec_targets(target_spec_ids)}.",
            )
            return

        self._stream_assistant_text(session_key, "Reply with y or n.")

    def _stream_assistant_text(self, session_key: str, text: str) -> None:
        on_app_thread = self._thread_id == threading.get_ident()
        if on_app_thread:
            message_id = self._add_chat_message(
                session_key,
                "assistant",
                text="",
                renderable=None,
            )
        else:
            message_id = self.call_from_thread(
                self._add_chat_message,
                session_key,
                "assistant",
                text="",
                renderable=None,
            )
        tokens = re.findall(r"\S+\s*", text) or [text]
        for index in range(0, len(tokens), 4):
            chunk = "".join(tokens[index : index + 4])
            if on_app_thread:
                self._append_chat_chunk(session_key, message_id, chunk)
            else:
                self.call_from_thread(self._append_chat_chunk, session_key, message_id, chunk)
            if self.chat_stream_delay > 0:
                time.sleep(self.chat_stream_delay)

    def _confirmation_prompt(self, command: ChatCommand) -> str:
        target_label = _format_spec_targets(_command_target_spec_ids(command))
        if command.name == "stop_run":
            return f"This will stop {target_label} and mark the run failed. Are you sure? y/n"
        if command.name == "reset_run":
            return f"This will reset {target_label}, remove its run artifacts, and start a fresh run. Are you sure? y/n"
        if command.name == "delete_spec":
            return f"This will delete or obsolete {target_label}. Are you sure? y/n"
        return (
            f"This will run {command.name.replace('_', ' ')} for "
            f"{target_label}. Are you sure? y/n"
        )

    def _execute_single_target_chat_command(
        self,
        command: ChatCommand,
        spec_id: str,
    ) -> ChatActionOutcome:
        latest = _latest_run(self.repo_root, spec_id)
        single_command = replace(command, spec_id=spec_id, spec_ids=())

        if command.name == "show_run":
            if latest is None:
                raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")
            summary = f"Showing the latest non-superseded run for {spec_id}."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=summary,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(summary=summary, renderable=_render_run_detail(self.repo_root, latest))

        if command.name == "tail_log":
            if latest is None or not self.live_checker(spec_id):
                raise RuntimeError(f"No live run is currently active for '{spec_id}'.")
            log_path = resolve_log_path(self.repo_root, spec_id, run_id=latest.run_id)
            if log_path is None:
                raise RuntimeError(f"No active log file was found for '{spec_id}'.")
            content = _tail_text_lines(log_path, command.lines)
            summary = f"Showing the latest {command.lines} log lines for {spec_id}."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=summary,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=_render_log_output(spec_id, command.lines, content),
            )

        if command.name == "add_retries":
            previous_cap = latest.retry_cap if latest is not None else 0
            new_cap = self.retry_runner(spec_id, command.count)
            auto_resumed = False
            if not self.live_checker(spec_id) and latest is not None and latest.status in ("failed", "blocked"):
                self.resume_runner(spec_id)
                auto_resumed = True
            summary = f"Raised the retry cap for {spec_id} by {command.count}."
            if auto_resumed:
                summary += " Resumed the failed run."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=f"retry_cap={new_cap}" + (", auto_resumed=true" if auto_resumed else ""),
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=_render_status_transition("Retry cap", str(previous_cap), str(new_cap)),
            )

        if command.name == "stop_run":
            before = latest.status if latest is not None else "running"
            stopped = self.stop_runner(spec_id)
            summary = f"Stopped {spec_id}."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=summary,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=_render_status_transition("Status", before, stopped.status),
            )

        if command.name == "resume_run":
            before = latest.status if latest is not None else "failed"
            retry_note = ""
            if command.extra_retries:
                new_cap = self.retry_runner(spec_id, command.extra_retries)
                retry_note = f" Added {command.extra_retries} retries (cap={new_cap})."
            log_path = (
                resume_spec_run(self.repo_root, spec_id, actor="tui-chat")
                if self._uses_default_resume_runner
                else self.resume_runner(spec_id)
            )
            summary = f"Requested a resume for {spec_id}.{retry_note}".strip()
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=str(log_path),
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=Group(
                    _render_status_transition("Resume", before, "pending"),
                    Text(f"log={log_path}", style="bold"),
                ),
            )

        if command.name == "reset_run":
            row = self._row_for_spec(spec_id)
            agent = _default_agent(self.repo_root, spec_id, fallback_agent=row.agent if row else "")
            if self._uses_default_reset_runner:
                reset_spec_run(
                    self.repo_root,
                    spec_id,
                    agent=agent,
                    actor="tui-chat",
                )
            else:
                self.reset_runner(spec_id, agent)
            summary = f"Reset {spec_id} and requested a fresh run."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=summary,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=Text("Fresh run requested.", style="green"),
            )

        if command.name == "delete_spec":
            if self.live_checker(spec_id):
                raise RuntimeError(f"Stop '{spec_id}' before deleting it.")
            message = self.delete_runner(spec_id)
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=message,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(summary=message, renderable=Text(message, style="yellow"))

        if command.name == "record_steering":
            steering = record_operator_steering(self.repo_root, spec_id, command.guidance)
            summary = f"Recorded proactive steering for {spec_id}."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=steering.message,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=Group(
                    Text(summary, style="cyan"),
                    Text(f"guidance={steering.message}", style="bold"),
                ),
            )

        if command.name == "resolve_input":
            before = latest.status if latest is not None else "waiting-for-input"
            log_path = resolve_input_run(self.repo_root, spec_id, command.answer)
            summary = f"Recorded the answer for {spec_id} and resumed the workflow."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                spec_id=spec_id,
                status="ok",
                detail=summary,
                arguments=single_command.__dict__.copy(),
            )
            return ChatActionOutcome(
                summary=summary,
                renderable=Group(
                    _render_status_transition("Status", before, "pending"),
                    Text(f"log={log_path}", style="bold"),
                ),
            )

        raise RuntimeError(f"Unsupported chat command '{command.name}'.")

    def _execute_batch_chat_command(
        self,
        command: ChatCommand,
        spec_ids: tuple[str, ...],
    ) -> ChatActionOutcome:
        if command.name in {"resolve_input", "record_steering"}:
            raise RuntimeError("This command only supports one spec at a time.")

        results: list[tuple[str, ChatActionOutcome | None, str | None]] = []
        success_count = 0
        failure_count = 0
        for spec_id in spec_ids:
            single_command = replace(command, spec_id=spec_id, spec_ids=())
            try:
                outcome = self._execute_single_target_chat_command(single_command, spec_id)
            except RuntimeError as exc:
                failure_count += 1
                _append_chat_action_log(
                    self.repo_root,
                    action=command.name,
                    spec_id=spec_id,
                    status="error",
                    detail=str(exc),
                    arguments=single_command.__dict__.copy(),
                )
                results.append((spec_id, None, str(exc)))
                continue
            success_count += 1
            results.append((spec_id, outcome, None))

        return ChatActionOutcome(
            summary=_batch_action_summary(command.name, spec_ids, success_count, failure_count),
            renderable=_render_batch_chat_results(results),
        )

    def _execute_chat_command(self, command: ChatCommand) -> ChatActionOutcome:
        if command.name == "help":
            return ChatActionOutcome(summary=_chat_help_text(command.spec_id))

        if command.name == "list_runs":
            rows = tuple(
                row
                for row in self.snapshot_loader(self.repo_root, self.agent_filter).rows
                if (not command.status_filter or row.status == command.status_filter)
                and (not command.agent_filter or row.agent == command.agent_filter)
                and (not command.phase_filter or row.phase == command.phase_filter)
            )
            filters = ", ".join(
                part
                for part in (
                    f"status={command.status_filter}" if command.status_filter else "",
                    f"agent={command.agent_filter}" if command.agent_filter else "",
                    f"phase={command.phase_filter}" if command.phase_filter else "",
                )
                if part
            )
            summary = "Showing current runs." if not filters else f"Showing current runs filtered by {filters}."
            _append_chat_action_log(
                self.repo_root,
                action=command.name,
                status="ok",
                detail=summary,
                arguments=command.__dict__.copy(),
            )
            return ChatActionOutcome(summary=summary, renderable=_render_runs_table(rows))

        spec_ids = _command_target_spec_ids(command)
        if not spec_ids:
            raise RuntimeError("Specify one or more spec ids for that command.")
        if len(spec_ids) == 1:
            return self._execute_single_target_chat_command(command, spec_ids[0])
        return self._execute_batch_chat_command(command, spec_ids)

    def _refresh_summary(self) -> None:
        parts = [
            f"{self.snapshot.active_count} active",
            f"{self.snapshot.failed_count} failed",
            f"{self.snapshot.merged_count} merged",
        ]
        if self.snapshot.passed_count:
            parts.append(f"{self.snapshot.passed_count} passed")
        parts.append(f"{self.snapshot.queued_count} queued")
        if self.snapshot.coordinator_unavailable:
            parts.append("[bold yellow]coordinator unavailable[/]")
        attention = self.snapshot.needs_attention_count
        if attention:
            parts.append(f"[bold magenta]{attention} need attention[/]")
        self.query_one("#summary", Static).update(" / ".join(parts))

    def _refresh_spec_table(self, selected_spec: str | None) -> None:
        table = self.query_one("#spec-table", DataTable)
        table.clear(columns=False)
        self._row_order = []
        for row in self.snapshot.rows:
            if row.requires_human_attention:
                status_cell = Text(f"{row.status} \u26a0", style="bold magenta")
            elif row.has_active_steering:
                status_cell = Text(f"{row.status} steer", style="bold cyan")
            else:
                status_cell = row.status
            table.add_row(
                row.display_spec_id,
                row.agent,
                row.phase,
                row.retries,
                row.lease_owner or "—",
                row.backend or "—",
                row.container_engine or "—",
                row.worker_source or "—",
                row.safety_mode or "—",
                row.lease_status or "—",
                row.lease_heartbeat_age or "—",
                row.lease_expires_at or "—",
                row.elapsed,
                status_cell,
                key=row.spec_id,
            )
            self._row_order.append(row.spec_id)

        if not self._row_order:
            return
        try:
            row_index = self._row_order.index(selected_spec) if selected_spec else 0
        except ValueError:
            row_index = 0
        table.move_cursor(row=row_index)

    def _refresh_queue_table(self, selected_spec: str | None) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.clear(columns=False)
        self._queue_order = []
        for item in self.snapshot.queue:
            if item.lease_state == "waiting-remote":
                state = f"waiting: {item.lease_owner or 'unknown'} ({item.lease_heartbeat_age or 'unknown'})"
            elif item.lease_state == "expired":
                state = f"reclaimable: {item.lease_owner or 'unknown'}"
            elif item.lease_state == "coordinator-unavailable":
                state = "coordinator unavailable"
            else:
                state = item.reason
            table.add_row(
                item.spec_id,
                item.agent,
                item.backend or "—",
                self._container_engine_for_backend(item.backend),
                self._worker_source_for_backend(item.backend),
                item.safety_mode or "—",
                str(item.unlock_count),
                str(item.priority),
                state,
                key=item.spec_id,
            )
            self._queue_order.append(item.spec_id)

        if not self._queue_order:
            return
        try:
            row_index = self._queue_order.index(selected_spec) if selected_spec else 0
        except ValueError:
            row_index = 0
        table.move_cursor(row=row_index)

    def _row_for_spec(self, spec_id: str) -> SpecRow | None:
        for row in self.snapshot.rows:
            if row.spec_id == spec_id:
                return row
        return None

    def current_selected_table_id(self) -> str | None:
        if isinstance(self.screen, (SpecDetailScreen, ChatScreen)):
            return None
        focused = self.focused
        if isinstance(focused, DataTable) and focused.id in {"spec-table", "queue-table"}:
            return focused.id
        if self._row_order:
            return "spec-table"
        if self._queue_order:
            return "queue-table"
        return None

    def _selected_spec_from_table(self, table_id: str) -> str | None:
        order = self._row_order if table_id == "spec-table" else self._queue_order
        if not order:
            return None
        table = self.query_one(f"#{table_id}", DataTable)
        row_index = table.cursor_row
        if row_index < 0 or row_index >= len(order):
            return None
        return order[row_index]

    def _restore_table_focus(self, preferred_table: str | None) -> None:
        if preferred_table == "queue-table" and self._queue_order:
            self.query_one("#queue-table", DataTable).focus()
            return
        if self._row_order:
            self.query_one("#spec-table", DataTable).focus()
            return
        if self._queue_order:
            self.query_one("#queue-table", DataTable).focus()

    def current_selected_spec_id(self) -> str | None:
        if isinstance(self.screen, SpecDetailScreen):
            return self.screen.spec_id
        if isinstance(self.screen, ChatScreen):
            return self._spec_id_from_session_key(self.screen.session_key) or None
        table_id = self.current_selected_table_id()
        if table_id is None:
            return None
        return self._selected_spec_from_table(table_id)

    def action_chat_global(self) -> None:
        if isinstance(self.screen, (SpecDetailScreen, ChatScreen)):
            return
        self.open_chat()

    def action_open_selected(self) -> None:
        if isinstance(self.screen, (SpecDetailScreen, ChatScreen)):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        self.push_screen(SpecDetailScreen(spec_id))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if isinstance(self.screen, SpecDetailScreen):
            return
        if event.data_table.id not in {"spec-table", "queue-table"}:
            return
        spec_id = _coerce_spec_row_key(event.row_key)
        if not spec_id:
            return
        self.push_screen(SpecDetailScreen(spec_id))

    def action_stop_selected(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        if not self.live_checker(spec_id):
            self.notify(f"No live run is currently active for '{spec_id}'.", severity="error")
            return
        self.push_screen(
            ConfirmScreen(
                f"Stop {spec_id}?",
                "This sends SIGTERM to the orchestrator process group and marks the run failed.",
            ),
            lambda confirmed: self._handle_stop(spec_id, confirmed),
        )

    def _handle_stop(self, spec_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            self.stop_runner(spec_id)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Stopped '{spec_id}'.")
        self.refresh_snapshot()

    def action_resume_selected(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        if self.live_checker(spec_id):
            self.notify(
                f"Spec '{spec_id}' already has a live run. Stop it before resuming.",
                severity="error",
            )
            return
        try:
            self.resume_runner(spec_id)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Resumed '{spec_id}'.")
        self.refresh_snapshot()

    def action_reset_selected(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        self.push_screen(ResetScreen(spec_id), lambda choice: self._handle_reset(spec_id, choice))

    def _handle_reset(self, spec_id: str, choice: str | None) -> None:
        if choice is None:
            return
        row = self._row_for_spec(spec_id)
        agent = _default_agent(self.repo_root, spec_id, fallback_agent=row.agent if row else "")
        try:
            self.reset_runner(spec_id, agent)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Reset '{spec_id}'.")
        if isinstance(self.screen, SpecDetailScreen) and self.screen.spec_id == spec_id:
            self.pop_screen()
        self.refresh_snapshot()

    def action_add_retries_selected(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        self.push_screen(RetryCountScreen(spec_id), lambda count: self._handle_add_retries(spec_id, count))

    def _handle_add_retries(self, spec_id: str, count: int | None) -> None:
        if count is None:
            return
        try:
            new_cap = self.retry_runner(spec_id, count)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
            return
        run = _latest_run(self.repo_root, spec_id)
        if not self.live_checker(spec_id) and run is not None and run.status in ("failed", "blocked"):
            try:
                self.resume_runner(spec_id)
                self.notify(f"Updated '{spec_id}' retry cap to {new_cap} and resumed.")
            except RuntimeError as exc:
                self.notify(f"Retry cap updated to {new_cap} but resume failed: {exc}", severity="warning")
        else:
            self.notify(f"Updated '{spec_id}' retry cap to {new_cap}.")
        self.refresh_snapshot()

    def action_delete_selected(self) -> None:
        if isinstance(self.screen, ChatScreen):
            return
        spec_id = self.current_selected_spec_id()
        if not spec_id:
            return
        if self.live_checker(spec_id):
            self.notify(f"Stop '{spec_id}' before deleting it.", severity="error")
            return
        row = self._row_for_spec(spec_id)
        noun = "task" if row is not None and row.run_mode == "task" else "spec"
        self.push_screen(
            ConfirmScreen(
                f"Delete {spec_id}?",
                f"This will delete the selected {noun} or mark the product spec obsolete.",
            ),
            lambda confirmed: self._handle_delete(spec_id, confirmed),
        )

    def _handle_delete(self, spec_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            message = self.delete_runner(spec_id)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(message)
        if isinstance(self.screen, SpecDetailScreen) and self.screen.spec_id == spec_id:
            self.pop_screen()
        self.refresh_snapshot()
