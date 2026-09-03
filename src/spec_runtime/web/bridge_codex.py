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
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator

from spec_runtime.agent_adapter import (
    CODEX_AMBIENT_CAPABILITY_OVERRIDES,
    codex_capability_probe_command,
    codex_capability_probe_unavailability_reason,
    codex_isolation_unavailability_reason,
)
from spec_runtime.agent_git_isolation import AgentGitIsolation
from spec_runtime.git_publish_guard import apply_host_owned_publication_guard
from spec_runtime.process_supervisor import LifetimeMode, ManagedAsyncProcess, ProcessSupervisor
from spec_runtime.provider_env import (
    CODEX_SECRET_ENV_KEYS,
    PROXY_ENV_KEYS,
    create_ephemeral_codex_home,
    minimal_provider_environment,
    protected_operator_paths,
)

from .bridge import AgentEvent

logger = logging.getLogger(__name__)

_PROCESS_STOP_TIMEOUT_SECONDS = 5.0
_WEB_PERMISSION_PROFILE = "specbutler-web"
_PENDING_PROCESS_WAITS: set[asyncio.Task] = set()


def _retain_pending_process_wait(task: asyncio.Task) -> None:
    """Own a cancellation-resistant wait task until it eventually exits."""
    _PENDING_PROCESS_WAITS.add(task)

    def finished(done: asyncio.Task) -> None:
        _PENDING_PROCESS_WAITS.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)


def _codex_available() -> bool:
    """Check whether Codex provides every enforced web-chat control."""
    return not _codex_unavailability_reason()


def _codex_unavailability_reason() -> str:
    path = shutil.which("codex")
    if path is None:
        return "Codex CLI was not found on PATH."
    try:
        exec_help = subprocess.run(
            [path, "exec", "--help"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        sandbox_help = subprocess.run(
            [path, "sandbox", "--help"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        app_server_help = subprocess.run(
            [path, "app-server", "--help"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        with tempfile.TemporaryDirectory(prefix="spec-codex-capability-probe-") as home:
            probe_env = minimal_provider_environment("codex")
            for key in CODEX_SECRET_ENV_KEYS:
                probe_env.pop(key, None)
            probe_env["CODEX_HOME"] = home
            capability_probe = subprocess.run(
                codex_capability_probe_command(path),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                env=probe_env,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not validate Codex isolation controls: {exc}"
    if app_server_help.returncode != 0:
        return "Codex CLI does not provide a usable app-server command."
    reason = codex_isolation_unavailability_reason(
        f"{exec_help.stdout}\n{exec_help.stderr}" if exec_help.returncode == 0 else "",
        f"{sandbox_help.stdout}\n{sandbox_help.stderr}"
        if sandbox_help.returncode == 0
        else "",
    )
    if reason:
        return reason
    return codex_capability_probe_unavailability_reason(
        capability_probe.returncode,
        capability_probe.stdout,
        capability_probe.stderr,
    )


async def _terminate_process(proc: ManagedAsyncProcess) -> None:
    """Terminate and raise unless provider exit is positively confirmed."""
    if getattr(proc, "returncode", None) is None:
        proc.terminate()
    if await _wait_for_process_exit(proc):
        return

    try:
        proc.kill()
    except ProcessLookupError:
        pass

    if await _wait_for_process_exit(proc):
        return
    raise RuntimeError("Codex app-server did not exit after kill")


async def _wait_for_process_exit(proc: ManagedAsyncProcess) -> bool:
    """Bound a wait and positively confirm the whole owned process tree."""
    wait_task = asyncio.create_task(proc.wait())
    done, _ = await asyncio.wait(
        {wait_task},
        timeout=_PROCESS_STOP_TIMEOUT_SECONDS,
    )
    if wait_task not in done:
        _retain_pending_process_wait(wait_task)
        wait_task.cancel()
        return False
    try:
        wait_task.result()
    except (OSError, RuntimeError):
        return False
    owned_tree_active = getattr(proc, "owned_tree_active", None)
    if callable(owned_tree_active):
        try:
            if owned_tree_active():
                return False
        except (OSError, RuntimeError):
            return False
    return True


class CodexBridge:
    """AgentBridge implementation backed by the Codex app-server protocol."""

    def __init__(self) -> None:
        if not _codex_available():
            reason = _codex_unavailability_reason()
            raise RuntimeError(
                "Codex backend unavailable — " + reason + " "
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
        git_isolation: AgentGitIsolation | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        session = _CodexSession(
            cwd=cwd,
            allowed_tools=allowed_tools,
            git_isolation=git_isolation,
        )
        session.initial_prompt = initial_prompt
        # Publish provisional ownership before the handshake. A failed
        # app-server handshake can leave a live process when bounded cleanup
        # fails, and Stop must retain a route back to that exact process.
        self._sessions[session_id] = session
        try:
            await session.start(prompt)
        except BaseException:
            if session._proc is None:
                # start() either failed before launch or already confirmed
                # process exit. Finish credential cleanup and retire the entry.
                try:
                    await self.stop_session(session_id)
                except Exception:
                    logger.warning(
                        "Codex startup cleanup remains pending for %s",
                        session_id,
                        exc_info=True,
                    )
            raise
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
            if session._last_turn_error:
                try:
                    await self.stop_session(session_id)
                except Exception:
                    logger.warning(
                        "Codex provider cleanup remains pending for %s",
                        session_id,
                        exc_info=True,
                    )
        except Exception as exc:
            # A failed turn leaves no usable browser session. Reap the
            # app-server immediately instead of waiting for an explicit stop
            # or web-server shutdown.
            try:
                await self.stop_session(session_id)
            except Exception:
                logger.warning(
                    "Codex provider cleanup remains pending for %s",
                    session_id,
                    exc_info=True,
                )
            yield AgentEvent(kind="error", text=str(exc))

        yield AgentEvent(kind="done")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            await session.stop()
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id, None)


class _CodexSession:
    """Manages a single codex app-server subprocess."""

    _STDERR_BUFFER_MAX = 50
    _REQUEST_TIMEOUT_SECONDS = 30.0
    _TURN_INACTIVITY_TIMEOUT_SECONDS = 180.0

    def __init__(
        self,
        cwd: str,
        allowed_tools: list[str] | None = None,
        git_isolation: AgentGitIsolation | None = None,
    ) -> None:
        self._cwd = cwd
        self._allowed_tools = allowed_tools
        self._git_isolation = git_isolation
        self._proc: ManagedAsyncProcess | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._request_id = 0
        self._thread_id: str | None = None
        self._stop_lock = asyncio.Lock()
        self.initial_prompt: str = ""
        self._started: bool = False
        self._saw_delta_text: bool = False
        self._last_turn_error: str = ""
        self._isolated_home_context: object | None = None

    @staticmethod
    def _isolated_provider_home(env: dict[str, str]) -> tuple[object, Path]:
        """Create an auth-only Codex home outside the repository checkout."""
        return create_ephemeral_codex_home(env)

    @staticmethod
    def _safety_config_overrides(
        codex_home: Path,
        *,
        operator_codex_home: Path | None = None,
        git_write_paths: tuple[Path, ...] = (),
        git_read_only_paths: tuple[Path, ...] = (),
    ) -> list[str]:
        """Return fail-closed web-chat filesystem and environment controls."""
        protected_candidates = {
            codex_home.resolve(strict=False),
            *(
                path.resolve(strict=False)
                for path in protected_operator_paths()
            ),
        }
        if operator_codex_home is not None:
            protected_candidates.add(operator_codex_home.resolve(strict=False))
        # Codex 0.151 constructs Linux deny mounts from shallowest to deepest.
        # Asking it to deny both an operator-state root and the launch-scoped
        # CODEX_HOME below that root fails before app-server initialization:
        # bubblewrap cannot create the nested mount point after denying its
        # ancestor.  A denied ancestor already covers every descendant, so
        # omit only those redundant entries.  This preserves the stronger
        # boundary around the complete operator-state tree while leaving the
        # provider process itself able to use its copied credential.
        protected_paths: list[Path] = []
        for candidate in sorted(
            protected_candidates,
            key=lambda path: (len(path.parts), str(path)),
        ):
            if any(candidate.is_relative_to(parent) for parent in protected_paths):
                continue
            protected_paths.append(candidate)
        filesystem_entries = [
            '":root"="read"',
            '":workspace_roots"="write"',
            *(
                f"{json.dumps(str(path))}=\"write\""
                for path in git_write_paths
            ),
            *(
                f"{json.dumps(str(path))}=\"read\""
                for path in git_read_only_paths
            ),
            *(
                f"{json.dumps(str(path))}=\"deny\""
                for path in protected_paths
            ),
        ]
        tool_env_exclude = sorted(
            {
                *CODEX_SECRET_ENV_KEYS,
                *PROXY_ENV_KEYS,
                "CODEX_APP_SERVER",
                "CODEX_HOME",
                "OPENAI_API_BASE",
                "OPENAI_BASE_URL",
                "OPENAI_ORG_ID",
                "OPENAI_ORGANIZATION",
                "OPENAI_PROJECT_ID",
            }
        )
        overrides = [
            "--strict-config",
            "-c",
            f'default_permissions="{_WEB_PERMISSION_PROFILE}"',
            "-c",
            (
                f"permissions.{_WEB_PERMISSION_PROFILE}.filesystem="
                "{" + ",".join(filesystem_entries) + "}"
            ),
            "-c",
            # Codex's command runner requires core process variables such as
            # PATH. The provider parent already has an allowlisted minimal
            # environment; inherit it while withholding every credential,
            # routing, and session-control value from model-run commands.
            "shell_environment_policy.inherit=all",
            "-c",
            "shell_environment_policy.exclude=" + json.dumps(tool_env_exclude),
            "-c",
            "allow_login_shell=false",
        ]
        # Do not let user configuration or evolving CLI defaults reintroduce
        # capabilities outside browser chat's shell/edit surface.
        for override in CODEX_AMBIENT_CAPABILITY_OVERRIDES:
            overrides += ["-c", override]
        if os.name == "nt":
            overrides += ["-c", 'windows.sandbox="unelevated"']
        return overrides

    async def start(self, system_prompt: str) -> None:
        codex_bin = shutil.which("codex")
        if codex_bin is None:
            raise RuntimeError(
                "codex CLI not found on PATH. "
                "Install it with: npm install -g @openai/codex"
            )
        env = minimal_provider_environment("codex")
        operator_codex_home = Path(
            env.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        isolated_home_context, isolated_home = self._isolated_provider_home(env)
        self._isolated_home_context = isolated_home_context
        for key in CODEX_SECRET_ENV_KEYS:
            env.pop(key, None)
        env["CODEX_HOME"] = str(isolated_home)
        env["CODEX_APP_SERVER"] = "1"
        apply_host_owned_publication_guard(env, Path(self._cwd))
        if self._git_isolation is not None:
            env.update(self._git_isolation.env_overrides)
        cmd = [
            codex_bin,
            *self._safety_config_overrides(
                isolated_home,
                operator_codex_home=operator_codex_home,
                git_write_paths=(
                    self._git_isolation.writable_paths
                    if self._git_isolation is not None
                    else ()
                ),
                git_read_only_paths=(
                    self._git_isolation.read_only_paths
                    if self._git_isolation is not None
                    else ()
                ),
            ),
            "app-server",
        ]
        try:
            self._proc = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
            )
        except BaseException:
            isolated_home_context.cleanup()
            self._isolated_home_context = None
            raise

        # Drain stderr in the background so the pipe buffer never fills
        # and blocks the child process.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            # Initialize the transport, then create a thread that carries
            # the web chat prompt as developer instructions.
            init_params: dict = {
                "clientInfo": {"name": "spec-web-chat", "version": "0.1.0"},
                # thread/start.permissions is intentionally experimental in
                # the app-server protocol.  Advertise support explicitly so
                # current Codex versions accept the named fail-closed profile
                # instead of rejecting the session before its first turn.
                "capabilities": {"experimentalApi": True},
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
                            # Named profiles classify :workspace_roots
                            # independently from cwd in the app-server API.
                            # Register the isolated chat checkout explicitly
                            # so the write grant cannot resolve to an empty set.
                            "runtimeWorkspaceRoots": [self._cwd],
                            "developerInstructions": system_prompt,
                            "approvalPolicy": "never",
                            "permissions": _WEB_PERMISSION_PROFILE,
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
        self._last_turn_error = ""

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
                exit_code = self._proc.returncode if self._proc else None
                exit_info = (
                    f" (exit code {exit_code})" if exit_code is not None else ""
                )
                stderr_detail = await self._collect_stderr_detail()
                await self.stop()
                raise RuntimeError(
                    "Codex app-server exited before the chat turn completed"
                    f"{exit_info}{stderr_detail}"
                )

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
        if event_type in (
            "turn.completed",
            "turn/completed",
            "turn.failed",
            "turn/failed",
            "turn.interrupted",
            "turn/interrupted",
            "turn.cancelled",
            "turn/cancelled",
            "turn.canceled",
            "turn/canceled",
        ):
            self._saw_delta_text = False
            failure = _codex_terminal_failure(msg, event_type)
            if failure:
                self._last_turn_error = failure
                return [
                    AgentEvent(kind="error", text=failure),
                    AgentEvent(kind="done"),
                ]
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
            if proc is not None:
                cleanup = asyncio.create_task(_terminate_process(proc))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # A caller cancellation must not detach the provider.
                    await cleanup
                    raise

            # Release process and credential ownership only after wait()
            # confirms that the provider leader has exited.
            self._proc = None
            self._stderr_task = None
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if self._isolated_home_context is not None:
                self._isolated_home_context.cleanup()
                self._isolated_home_context = None


def _codex_terminal_failure(msg: dict, event_type: str) -> str:
    """Return an actionable error for failed/interrupted terminal events."""
    params = msg.get("params", {})
    if not isinstance(params, dict):
        params = {}
    turn = msg.get("turn") or params.get("turn") or {}
    if not isinstance(turn, dict):
        turn = {}

    status = str(
        turn.get("status")
        or msg.get("status")
        or params.get("status")
        or ""
    ).strip().lower()
    terminal_kind = event_type.replace("/", ".").rsplit(".", 1)[-1].lower()
    if terminal_kind == "completed" and status in {
        "",
        "completed",
        "success",
        "succeeded",
    }:
        return ""

    failure_status = status or terminal_kind or "failed"
    detail: object = (
        turn.get("error")
        or turn.get("message")
        or params.get("error")
        or params.get("message")
        or msg.get("error")
        or msg.get("message")
        or ""
    )
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("text") or detail
    detail_text = str(detail).strip()
    message = f"Codex chat turn {failure_status}"
    return f"{message}: {detail_text}" if detail_text else message


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
