"""Codex app-server backend for the AgentBridge.

Spawns the Codex app-server as a subprocess communicating via JSON-RPC over
stdio.  ``start_session`` sends the ``initialize`` handshake and
``thread/start``.  ``send_message`` resumes the existing thread and then
sends ``turn/start`` so follow-up turns retain the thread's prior
conversation context.

The Codex CLI must be installed. If it is unavailable, ``CodexBridge`` raises
``RuntimeError`` at construction time and the web UI disables the Codex agent
option.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import uuid
from typing import AsyncIterator

from .bridge import AgentEvent

logger = logging.getLogger(__name__)

_PROCESS_STOP_TIMEOUT_SECONDS = 5.0


def _codex_available() -> bool:
    """Check whether the Codex CLI that provides ``app-server`` is on PATH."""
    return shutil.which("codex") is not None


def _signal_process_group(
    proc: asyncio.subprocess.Process,
    sig: signal.Signals,
) -> None:
    """Signal the app-server session, including any command subprocesses."""
    pid = getattr(proc, "pid", 0)
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, sig)
            return
        except (OSError, ProcessLookupError, PermissionError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate, escalate when necessary, and always wait for the leader."""
    if getattr(proc, "returncode", None) is None:
        _signal_process_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=_PROCESS_STOP_TIMEOUT_SECONDS,
        )
        return
    except asyncio.TimeoutError:
        _signal_process_group(proc, signal.SIGKILL)

    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=_PROCESS_STOP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Codex app-server did not exit after kill")


class CodexBridge:
    """AgentBridge implementation backed by the Codex app-server protocol."""

    def __init__(self) -> None:
        if not _codex_available():
            raise RuntimeError(
                "Codex backend unavailable — requires the codex CLI on PATH "
                "(npm install -g @openai/codex)"
            )
        self._sessions: dict[str, _CodexSession] = {}

    async def start_session(
        self,
        prompt: str,
        *,
        agent: str,
        cwd: str,
        allowed_tools: list[str] | None = None,
        session_id: str | None = None,
        initial_prompt: str = "",
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        session = _CodexSession(cwd=cwd, allowed_tools=allowed_tools)
        session.initial_prompt = initial_prompt
        await session.start(prompt)
        self._sessions[session_id] = session
        return session_id

    async def send_message(
        self,
        session_id: str,
        text: str,
    ) -> AsyncIterator[AgentEvent]:
        session = self._sessions.get(session_id)
        if session is None:
            yield AgentEvent(kind="error", text=f"Unknown session: {session_id}")
            yield AgentEvent(kind="done")
            return

        try:
            async for event in session.send_turn(text):
                yield event
        except Exception as exc:
            # A failed turn leaves no usable browser session. Reap the
            # app-server immediately instead of waiting for an explicit stop
            # or web-server shutdown.
            self._sessions.pop(session_id, None)
            await session.stop()
            yield AgentEvent(kind="error", text=str(exc))

        yield AgentEvent(kind="done")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.stop()


class _CodexSession:
    """Manages a single codex app-server subprocess."""

    _STDERR_BUFFER_MAX = 50
    _REQUEST_TIMEOUT_SECONDS = 30.0
    _TURN_INACTIVITY_TIMEOUT_SECONDS = 180.0

    def __init__(
        self, cwd: str, allowed_tools: list[str] | None = None,
    ) -> None:
        self._cwd = cwd
        self._allowed_tools = allowed_tools
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._request_id = 0
        self._thread_id: str | None = None
        self._stop_lock = asyncio.Lock()
        self.initial_prompt: str = ""
        self._started: bool = False
        self._saw_delta_text: bool = False

    async def start(self, system_prompt: str) -> None:
        codex_bin = shutil.which("codex")
        if codex_bin is None:
            raise RuntimeError(
                "codex CLI not found on PATH. "
                "Install it with: npm install -g @openai/codex"
            )
        cmd = [codex_bin, "app-server"]
        env = {**os.environ, "CODEX_APP_SERVER": "1"}
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
            start_new_session=True,
        )

        # Drain stderr in the background so the pipe buffer never fills
        # and blocks the child process.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            # Initialize the transport, then create a thread that carries
            # the web chat prompt as developer instructions.
            init_params: dict = {
                "clientInfo": {"name": "spec-web-chat", "version": "0.1.0"},
            }
            try:
                init_resp, _ = await asyncio.wait_for(
                    self._send_request("initialize", init_params),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                stderr_detail = await self._collect_stderr_detail()
                exit_code = self._proc.returncode if self._proc else None
                exit_info = f" (exit code {exit_code})" if exit_code is not None else ""
                raise RuntimeError(
                    f"Codex app-server timed out during initialize (30s)"
                    f"{exit_info}{stderr_detail}"
                )
            await self._check_handshake_error(init_resp, "initialize")

            # Send thread/start and capture the thread identifier
            try:
                thread_resp, _notifs = await asyncio.wait_for(
                    self._send_request(
                        "thread/start",
                        {
                            "cwd": self._cwd,
                            "developerInstructions": system_prompt,
                            "approvalPolicy": "never",
                            "sandbox": "workspace-write",
                        },
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                stderr_detail = await self._collect_stderr_detail()
                exit_code = self._proc.returncode if self._proc else None
                exit_info = f" (exit code {exit_code})" if exit_code is not None else ""
                raise RuntimeError(
                    f"Codex app-server timed out during thread/start (30s)"
                    f"{exit_info}{stderr_detail}"
                )
            await self._check_handshake_error(thread_resp, "thread/start")
            if thread_resp and "result" in thread_resp:
                thread_obj = thread_resp["result"].get("thread", {})
                self._thread_id = thread_obj.get("id")
            if not self._thread_id:
                raise RuntimeError(
                    "Codex app-server did not return a valid thread id"
                )
        except BaseException:
            # Handshake failed — kill the subprocess so it doesn't leak.
            await self.stop()
            raise

    async def _drain_stderr(self) -> None:
        """Read stderr, log it, and buffer recent lines for error reporting."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                logger.debug("codex stderr: %s", decoded)
                if len(self._stderr_lines) >= self._STDERR_BUFFER_MAX:
                    self._stderr_lines.pop(0)
                self._stderr_lines.append(decoded)
        except Exception:
            pass

    async def _collect_stderr_detail(self) -> str:
        """Give the drain task a moment to collect output, then format stderr."""
        if self._stderr_task and not self._stderr_task.done():
            await asyncio.sleep(0.1)
        if not self._stderr_lines:
            return ""
        return ":\n  " + "\n  ".join(self._stderr_lines)

    async def _check_handshake_error(self, resp: dict | None, method: str) -> None:
        """Like _check_jsonrpc_error but enriches with stderr and exit code."""
        if resp is None:
            exit_code = None
            if self._proc is not None:
                exit_code = self._proc.returncode
            exit_info = f" (exit code {exit_code})" if exit_code is not None else ""
            stderr_detail = await self._collect_stderr_detail()
            raise RuntimeError(
                f"Codex app-server returned no response for {method}{exit_info}"
                f"{stderr_detail}"
            )
        err = resp.get("error")
        if err:
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            raise RuntimeError(
                f"Codex app-server error on {method}: {msg}"
            )

    async def send_turn(self, text: str) -> AsyncIterator[AgentEvent]:
        if self._proc is None or self._proc.stdout is None:
            return
        if not self._thread_id:
            raise RuntimeError("Codex session has no active thread")

        prompt_text = (
            self.initial_prompt
            if not self._started and self.initial_prompt
            else text
        )

        try:
            resume_resp, _ = await asyncio.wait_for(
                self._send_request(
                    "thread/resume",
                    {"threadId": self._thread_id},
                ),
                timeout=self._REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                "Codex app-server timed out while resuming the chat thread"
            ) from exc
        # Tolerate resume errors — the SDK itself silently ignores them
        # (the thread may already be active after the initial start).
        if resume_resp and resume_resp.get("error"):
            logger.debug(
                "thread/resume returned error (ignored): %s",
                resume_resp["error"],
            )

        params: dict = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt_text}],
        }

        # _send_request buffers any notifications that arrive before the
        # JSON-RPC response so we don't lose streamed turn events.
        try:
            resp, buffered = await asyncio.wait_for(
                self._send_request("turn/start", params),
                timeout=self._REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                "Codex app-server timed out while starting the chat turn"
            ) from exc
        _check_jsonrpc_error(resp, "turn/start")
        self._started = True

        # Yield events from notifications buffered during the handshake.
        for msg in buffered:
            for event in self._parse_notification(msg):
                if event.kind == "done":
                    return
                yield event

        # Read remaining JSON events until turn is complete.
        while True:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=self._TURN_INACTIVITY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                await self.stop()
                raise RuntimeError(
                    "Codex chat turn timed out after "
                    f"{self._TURN_INACTIVITY_TIMEOUT_SECONDS:g} seconds "
                    "without output"
                ) from exc
            if not line:
                break

            try:
                msg = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            if await self._handle_server_request(msg):
                continue

            for event in self._parse_notification(msg):
                if event.kind == "done":
                    return
                yield event

    # ------------------------------------------------------------------

    def _parse_notification(self, msg: dict) -> list[AgentEvent]:
        """Convert a JSON message from the app-server into AgentEvents.

        Returns an empty list for messages that should be silently ignored
        (e.g. ``turn.started``, ``item.started``).  Returns a list
        containing an ``AgentEvent(kind="done")`` sentinel for
        turn-complete signals.
        """
        # Support both plain-JSON ``type`` and JSON-RPC ``method``.
        event_type = msg.get("type") or msg.get("method", "")

        if event_type in ("turn.completed", "turn/completed"):
            self._saw_delta_text = False
            return [AgentEvent(kind="done")]
        elif event_type in ("item.started", "item/started"):
            params = msg.get("params", {})
            item = msg.get("item") or params.get("item", {})
            item_type = item.get("type", "")
            if item_type in ("agent_message", "agentMessage"):
                self._saw_delta_text = False
            return []
        elif event_type in (
            "item.delta",
            "item/delta",
            "item/agentMessage/delta",
        ):
            # Incremental content — surface agent message text deltas
            # so the frontend can render them as they arrive.
            params = msg.get("params", {})
            delta = msg.get("delta") or params.get("delta", {})
            item = msg.get("item") or params.get("item", {})
            # Delta text can live under delta.text or item.text
            if isinstance(delta, str):
                text = delta
                item_type = "agentMessage"
            else:
                text = delta.get("text", "") or item.get("text", "")
                item_type = delta.get("type", "") or item.get("type", "")
            if text and item_type in (
                "agent_message", "agentMessage", "text", "",
            ):
                self._saw_delta_text = True
                return [AgentEvent(kind="text", text=text)]
            return []
        elif event_type in ("item.updated", "item/updated"):
            # Intermediate cumulative update — suppress entirely.
            # Incremental content is handled by item.delta; the final
            # snapshot is handled by item.completed.
            return []
        elif event_type in ("item.completed", "item/completed"):
            # JSON-RPC: item payload lives under ``params.item``;
            # plain JSON: directly under ``item``.
            params = msg.get("params", {})
            item = msg.get("item") or params.get("item", {})
            item_type = item.get("type", "")

            if item_type in ("agent_message", "agentMessage"):
                # Only emit completed text as fallback when no deltas
                # were streamed — otherwise the text would be duplicated.
                if self._saw_delta_text:
                    self._saw_delta_text = False
                    return []
                self._saw_delta_text = False
                return [AgentEvent(
                    kind="text",
                    text=item.get("text", ""),
                )]
            elif item_type in ("command_execution", "commandExecution"):
                ec = item.get("exit_code")
                if ec is None:
                    ec = item.get("exitCode")
                return [AgentEvent(
                    kind="command",
                    cmd=item.get("command", ""),
                    exit_code=ec,
                    output=_truncate(
                        item.get("aggregatedOutput") or item.get("output") or "",
                        500,
                    ),
                )]
            elif item_type in ("mcp_tool_call", "mcpToolCall"):
                server = item.get("server", "")
                tool = item.get("tool", "")
                tool_name = f"{server}/{tool}" if server else tool
                events: list[AgentEvent] = [AgentEvent(
                    kind="tool_call",
                    tool_name=tool_name,
                    tool_input=_truncate(
                        json.dumps(
                            item.get("arguments", {}), default=str,
                        ),
                        500,
                    ),
                )]
                # Completed MCP items include the result — emit a
                # tool_result event so the frontend can render it.
                result_val = item.get("result") or item.get("output", "")
                if result_val:
                    if not isinstance(result_val, str):
                        result_val = json.dumps(result_val, default=str)
                    events.append(AgentEvent(
                        kind="tool_result",
                        tool_name=tool_name,
                        tool_output=_truncate(result_val, 500),
                    ))
                return events
            elif item_type in ("file_change", "fileChange"):
                changes = item.get("changes")
                if isinstance(changes, list):
                    return [
                        AgentEvent(
                            kind="file_change",
                            path=str(change.get("path", "")),
                            diff=_truncate(str(change.get("diff", "")), 1000),
                        )
                        for change in changes
                        if isinstance(change, dict)
                    ]
                return [AgentEvent(
                    kind="file_change",
                    path=item.get("path", ""),
                    diff=_truncate(item.get("diff", ""), 1000),
                )]
            elif item_type == "error":
                return [AgentEvent(
                    kind="error",
                    text=item.get("text", item.get("message", "")),
                )]
            # reasoning, userMessage, item.started, etc. → silently ignored
            return []
        elif event_type == "error":
            err = msg.get("params", {}).get("error", {})
            text = err.get("message", "") if err else ""
            if not text:
                text = msg.get("message", str(msg))
            return [AgentEvent(
                kind="error",
                text=text,
            )]
        # turn.started, thread.started, item.started → ignored
        return []

    async def _send_request(
        self, method: str, params: dict,
    ) -> tuple[dict | None, list[dict]]:
        """Send a JSON-RPC request and return ``(response, buffered_notifications)``.

        Any notification messages (lines without an ``"id"`` field) that
        arrive before the matching response are collected in
        *buffered_notifications* so callers can process them instead of
        losing them.
        """
        if (
            self._proc is None
            or self._proc.stdin is None
            or self._proc.stdout is None
        ):
            return None, []
        self._request_id += 1
        req_id = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        data = json.dumps(msg) + "\n"
        self._proc.stdin.write(data.encode("utf-8"))
        await self._proc.stdin.drain()

        buffered: list[dict] = []

        # Read lines until we find the JSON-RPC response matching our id.
        # Notifications (no "id") are buffered for the caller.
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None, buffered
            try:
                resp = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if await self._handle_server_request(resp):
                continue
            # Buffer notifications (they lack an "id" field)
            if "id" not in resp:
                buffered.append(resp)
                continue
            if resp.get("id") == req_id:
                return resp, buffered

    async def _handle_server_request(self, msg: dict) -> bool:
        """Decline unexpected approval requests instead of deadlocking stdio."""
        method = msg.get("method")
        if "id" not in msg or method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return False
        if self._proc is None or self._proc.stdin is None:
            return True
        response = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"decision": "decline"},
        }
        self._proc.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        logger.warning(
            "Declined unexpected Codex approval request despite approvalPolicy=never: %s",
            method,
        )
        return True

    async def stop(self) -> None:
        async with self._stop_lock:
            stderr_task = self._stderr_task
            proc = self._proc
            try:
                if proc is not None:
                    cleanup = asyncio.create_task(_terminate_process(proc))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # A caller cancellation must not detach the provider.
                        await cleanup
                        raise
            finally:
                self._proc = None
                self._stderr_task = None
                if stderr_task is not None:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)


def _check_jsonrpc_error(resp: dict | None, method: str) -> None:
    """Raise ``RuntimeError`` if *resp* is ``None`` or contains an error."""
    if resp is None:
        raise RuntimeError(
            f"Codex app-server returned no response for {method}"
        )
    err = resp.get("error")
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        raise RuntimeError(
            f"Codex app-server error on {method}: {msg}"
        )


def _truncate(s: object, max_len: int) -> str:
    s = "" if s is None else str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
