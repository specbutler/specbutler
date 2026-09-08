"""Claude Agent SDK backend for the AgentBridge.

Uses ``claude-agent-sdk`` to drive Claude Code sessions.  A single
``ClaudeSDKClient`` is kept alive for the lifetime of each chat session
so that conversation context (system prompt, prior turns) is preserved
naturally in the same Claude Code subprocess — no ``--resume`` needed.

The SDK is a soft dependency — if not installed, ``ClaudeBridge`` raises
``RuntimeError`` at construction time rather than at import time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..agent_adapter import (
    claude_restricted_mode_unavailability_reason,
    host_agent_unavailability_reason,
)
from ..agent_git_isolation import AgentGitIsolation
from ..git_publish_guard import apply_host_owned_publication_guard
from ..provider_env import (
    CLAUDE_PROVIDER_CREDENTIAL_ENV_KEYS,
    protected_operator_paths,
    provider_environment_overlay,
)
from .bridge import AgentEvent

logger = logging.getLogger(__name__)

_PENDING_CLIENT_ACTIONS: set[asyncio.Task] = set()


def _retain_pending_client_action(task: asyncio.Task) -> None:
    """Own a cancellation-resistant SDK action until it eventually exits."""
    _PENDING_CLIENT_ACTIONS.add(task)

    def finished(done: asyncio.Task) -> None:
        _PENDING_CLIENT_ACTIONS.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)

_WEB_TOOLS = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep"})
_WEB_NETWORK_DOMAINS = (
    "github.com",
    "api.github.com",
    "*.blob.core.windows.net",
    "api.anthropic.com",
    "*.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.yarnpkg.com",
    "localhost",
    "127.0.0.1",
)
_WEB_PERMISSION_DENY = (
    "Bash(git push*)",
    "Bash(*git push*)",
    "Bash(git push --force*)",
    "Bash(git push * --force*)",
    "Bash(git push -f*)",
    "Bash(git push * -f*)",
    "Bash(git reset --hard*)",
    "Bash(git reset * --hard*)",
)
def _web_provider_environment(source: dict[str, str]) -> dict[str, str]:
    """Return the SDK overlay, including Claude's child-process scrub mode."""
    env = provider_environment_overlay("claude", source)
    # A custom Claude config directory may contain the operator's active OAuth
    # login. Claude needs its location, while the sandbox denylist below keeps
    # model-selected tools from reading it.
    if source.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = source["CLAUDE_CONFIG_DIR"]
    # Claude itself still receives the credential needed to call the provider,
    # but Bash, hooks, and MCP subprocesses must not inherit provider or cloud
    # credentials.  On Linux this also gives Bash an isolated PID namespace so
    # it cannot recover the parent environment through /proc.
    env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
    return env


def _web_sandbox(
    source: dict[str, str],
    git_isolation: AgentGitIsolation | None,
) -> dict[str, object]:
    """Return the fail-closed OS sandbox policy for browser chat commands."""
    protected = sorted(
        {
            str(path.resolve(strict=False))
            for path in protected_operator_paths(source)
        }
    )
    read_only = sorted(
        {
            str(path.resolve(strict=False))
            for path in (
                git_isolation.read_only_paths if git_isolation is not None else ()
            )
        }
    )
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "excludedCommands": [],
        "allowUnsandboxedCommands": False,
        "enableWeakerNestedSandbox": False,
        "filesystem": {
            "allowWrite": [
                str(path)
                for path in (
                    git_isolation.writable_paths if git_isolation is not None else ()
                )
            ],
            "denyRead": protected,
            "denyWrite": sorted({*protected, *read_only}),
        },
        "credentials": {
            "envVars": [
                {"name": name, "mode": "deny"}
                for name in sorted(CLAUDE_PROVIDER_CREDENTIAL_ENV_KEYS)
            ],
            "files": [
                {"path": path, "mode": "deny"}
                for path in protected
            ],
        },
        "network": {
            "allowedDomains": list(_WEB_NETWORK_DOMAINS),
            "strictAllowlist": True,
            "allowLocalBinding": True,
            "allowAllUnixSockets": False,
        },
    }


_CLAUDE_DECOY_EXACT_NAMES = frozenset(
    {
        ".gitmodules",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "bun.lock",
        "bun.lockb",
        "bunfig.toml",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    }
)


@dataclass(frozen=True)
class _DecoyFileState:
    device: int
    inode: int
    mode: int
    size: int
    digest: str | None


def _is_claude_decoy_candidate(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(".env")
        or name.startswith(".yarnrc")
        or (name.startswith("package") and name.endswith(".json"))
        or name in _CLAUDE_DECOY_EXACT_NAMES
    )


def _decoy_file_state(path: Path) -> _DecoyFileState | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    digest: str | None = None
    size = metadata.st_size
    if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        try:
            payload = path.read_bytes()
            after = path.lstat()
        except OSError:
            return None
        if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
            return None
        metadata = after
        size = len(payload)
        digest = hashlib.sha256(payload).hexdigest()
    return _DecoyFileState(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=size,
        digest=digest,
    )


def _snapshot_claude_decoy_candidates(cwd: Path) -> dict[str, _DecoyFileState]:
    """Snapshot candidate names without following links outside the checkout."""
    names = set(_CLAUDE_DECOY_EXACT_NAMES)
    names.update({".env", "package.json", "package-lock.json"})
    try:
        names.update(
            path.name
            for path in cwd.iterdir()
            if _is_claude_decoy_candidate(path)
        )
    except OSError:
        pass
    snapshot: dict[str, _DecoyFileState] = {}
    for name in names:
        state = _decoy_file_state(cwd / name)
        if state is not None:
            snapshot[name] = state
    return snapshot


def _record_claude_launch_decoys(session: dict) -> None:
    """Record zero-byte candidates created during provider startup only."""
    cwd = session["cwd"]
    before = session["decoy_before"]
    after = _snapshot_claude_decoy_candidates(cwd)
    empty_digest = hashlib.sha256(b"").hexdigest()
    session["launch_decoys"] = {
        name: state
        for name, state in after.items()
        if name not in before
        and state.size == 0
        and state.digest == empty_digest
    }


def _cleanup_claude_launch_decoys(session: dict) -> None:
    """Remove only unchanged files positively identified as startup decoys."""
    cwd = session["cwd"]
    for name, expected in session.get("launch_decoys", {}).items():
        path = cwd / name
        current = _decoy_file_state(path)
        if current != expected:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove Claude startup decoy %s", path)


def _selected_tools(allowed_tools: list[str] | None) -> list[str]:
    """Validate the narrow tool surface supported by browser chat."""
    selected = list(dict.fromkeys(allowed_tools or ()))
    unsupported = sorted(set(selected) - _WEB_TOOLS)
    if unsupported:
        raise ValueError(
            "Unsupported Claude web-chat tools: " + ", ".join(unsupported)
        )
    return selected


def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401

        return True
    except ImportError:
        return False


def _claude_cli_unavailability_reason() -> str:
    """Check every host CLI control used by the web security boundary."""
    sandbox_reason = host_agent_unavailability_reason("claude")
    if sandbox_reason:
        return sandbox_reason
    path = shutil.which("claude")
    if not path:
        return "Claude CLI was not found on PATH."
    try:
        result = subprocess.run(
            [path, "--help"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not validate the Claude CLI isolation controls: {exc}"
    if result.returncode != 0:
        return "Claude CLI --help failed while validating isolation controls."
    return claude_restricted_mode_unavailability_reason(
        f"{result.stdout}\n{result.stderr}"
    )


class ClaudeBridge:
    """AgentBridge implementation backed by the Claude Agent SDK."""

    _TURN_INACTIVITY_TIMEOUT_SECONDS = 180.0
    _CLIENT_STOP_TIMEOUT_SECONDS = 5.0

    def __init__(self) -> None:
        if not _sdk_available():
            raise RuntimeError(
                "claude-agent-sdk is not installed. "
                "Install it with: pip install claude-agent-sdk"
            )
        self._sessions: dict[str, dict] = {}

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
        import claude_agent_sdk

        session_id = session_id or uuid.uuid4().hex
        selected_tools = _selected_tools(allowed_tools)
        ambient_env = dict(os.environ)
        provider_env = _web_provider_environment(ambient_env)
        apply_host_owned_publication_guard(provider_env, Path(cwd))
        if git_isolation is not None:
            provider_env.update(git_isolation.env_overrides)
        cwd_path = Path(cwd).resolve(strict=False)
        decoy_before = _snapshot_claude_decoy_candidates(cwd_path)

        opts = claude_agent_sdk.ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="dontAsk",
            system_prompt=prompt,
            # The SDK bundles a Claude CLI that can lag the host installation.
            # Prefer the host CLI because the web boundary requires its
            # restricted and safe modes.  If no host CLI exists, the bundled
            # CLI receives unknown required flags and refuses to start rather
            # than silently launching with weaker isolation.
            cli_path=shutil.which("claude"),
            tools=selected_tools,
            allowed_tools=selected_tools,
            disallowed_tools=[],
            setting_sources=[],
            skills=[],
            plugins=[],
            mcp_servers={},
            sandbox=_web_sandbox(ambient_env, git_isolation),
            settings=json.dumps(
                {
                    "permissions": {
                        "disableBypassPermissionsMode": "disable",
                        "deny": list(_WEB_PERMISSION_DENY),
                    }
                }
            ),
            extra_args={
                "restricted": None,
                "safe-mode": None,
                "strict-mcp-config": None,
                "no-session-persistence": None,
                "disable-slash-commands": None,
                "no-chrome": None,
            },
            # The SDK merges this mapping over its own os.environ rather than
            # replacing the child environment. Include empty entries for every
            # non-allowlisted ambient variable so unrelated operator secrets
            # are neutralized in the Claude subprocess.
            env=provider_env,
        )

        client = claude_agent_sdk.ClaudeSDKClient(options=opts)
        # Register provisional ownership before connect. A failed connect can
        # still leave an SDK child behind, and Stop must be able to retry an
        # unconfirmed disconnect instead of losing the only client handle.
        session = {
            "client": client,
            "initial_prompt": initial_prompt,
            "started": False,
            "cancelled": False,
            "stop_lock": asyncio.Lock(),
            "stop_tasks": {},
            "cwd": cwd_path,
            "decoy_before": decoy_before,
            "launch_decoys": {},
        }
        self._sessions[session_id] = session
        try:
            await client.connect()
        except BaseException:
            _record_claude_launch_decoys(session)
            try:
                await self.stop_session(session_id)
            except Exception:
                logger.warning(
                    "Claude startup cleanup remains pending for %s",
                    session_id,
                    exc_info=True,
                )
            raise
        _record_claude_launch_decoys(session)
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

        import claude_agent_sdk

        # On the first turn, use the initial_prompt stored at session
        # creation so the bridge always receives the user's prompt —
        # even if the caller passes different text.
        if not session["started"] and session.get("initial_prompt"):
            prompt_text = session["initial_prompt"]
        else:
            prompt_text = text

        client = session["client"]

        try:
            await client.query(prompt_text)

            # Map tool_use_id → (tool_name, input_dict) so ToolResultBlocks
            # can emit structured command/file_change events.
            tool_info: dict[str, tuple[str, dict]] = {}
            had_text = False
            saw_terminal_result = False

            response_iter = client.receive_response().__aiter__()
            while True:
                try:
                    message = await asyncio.wait_for(
                        anext(response_iter),
                        timeout=self._TURN_INACTIVITY_TIMEOUT_SECONDS,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        "Claude chat turn timed out after "
                        f"{self._TURN_INACTIVITY_TIMEOUT_SECONDS:g} seconds "
                        "without output"
                    )

                if session.get("cancelled"):
                    break

                if not session["started"]:
                    session["started"] = True

                is_assistant = isinstance(
                    message,
                    claude_agent_sdk.AssistantMessage,
                )
                is_user = isinstance(message, claude_agent_sdk.UserMessage)
                if is_assistant or is_user:
                    blocks = message.content if isinstance(message.content, list) else []
                    for block in blocks:
                        if is_assistant and isinstance(block, claude_agent_sdk.TextBlock):
                            had_text = True
                            yield AgentEvent(kind="text", text=block.text)
                        elif is_assistant and isinstance(block, claude_agent_sdk.ToolUseBlock):
                            inp = block.input if isinstance(block.input, dict) else {}
                            tool_info[block.id] = (block.name, inp)
                            yield AgentEvent(
                                kind="tool_call",
                                tool_name=block.name,
                                tool_input=_truncate(
                                    json.dumps(block.input, default=str),
                                    500,
                                ),
                            )
                        elif isinstance(block, claude_agent_sdk.ToolResultBlock):
                            content = block.content
                            if isinstance(content, list):
                                content = json.dumps(content, default=str)
                            use_id = getattr(block, "tool_use_id", "")
                            t_name, t_input = tool_info.get(use_id, ("", {}))
                            is_error = getattr(block, "is_error", False)
                            yield AgentEvent(
                                kind="tool_result",
                                tool_name=t_name,
                                tool_output=_truncate(str(content or ""), 500),
                            )
                            # Emit structured events for specific tools
                            # so the frontend can render command/file cards.
                            for ev in _structured_events(
                                t_name, t_input, str(content or ""),
                                is_error=is_error,
                            ):
                                yield ev
                elif isinstance(message, claude_agent_sdk.ResultMessage):
                    saw_terminal_result = True
                    subtype = str(getattr(message, "subtype", "") or "")
                    is_error = bool(getattr(message, "is_error", False))
                    if is_error or subtype != "success":
                        detail = str(getattr(message, "result", "") or "").strip()
                        failure = f"Claude chat turn {subtype or 'failed'}"
                        if detail:
                            failure = f"{failure}: {detail}"
                        yield AgentEvent(kind="error", text=failure)
                        try:
                            await self.stop_session(session_id)
                        except Exception:
                            # Keep the bridge-owned session registered so Stop
                            # can retry provider cleanup. The terminal error is
                            # already explicit to the API relay.
                            logger.warning(
                                "Claude provider cleanup remains pending for %s",
                                session_id,
                                exc_info=True,
                            )
                        break
                    # Only use ResultMessage.result as a fallback — the
                    # text was already streamed via AssistantMessage blocks.
                    if message.result and not had_text:
                        yield AgentEvent(kind="text", text=message.result)

            if (
                not saw_terminal_result
                and not session.get("cancelled")
            ):
                raise RuntimeError(
                    "Claude response ended without a terminal result"
                )

        except Exception as exc:
            if not session.get("cancelled"):
                # A provider/SDK failure leaves this conversational subprocess
                # unusable. Reap it immediately instead of retaining an
                # auth-bearing client until an operator later clicks Stop or
                # the entire web service shuts down.
                try:
                    await self.stop_session(session_id)
                except Exception:
                    logger.warning(
                        "Claude provider cleanup remains pending for %s",
                        session_id,
                        exc_info=True,
                    )
                yield AgentEvent(kind="error", text=str(exc))

        yield AgentEvent(kind="done")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            async with session["stop_lock"]:
                if self._sessions.get(session_id) is not session:
                    return
                session["cancelled"] = True
                client = session.get("client")
                if client is not None:
                    cleanup = asyncio.create_task(self._stop_client(client, session))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # Web shutdown cancellation must not detach the SDK child.
                        await cleanup
                        raise
                _cleanup_claude_launch_decoys(session)
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)

    async def _stop_client(self, client: object, session: dict) -> None:
        interrupt = getattr(client, "interrupt", None)
        if callable(interrupt):
            try:
                await self._run_client_action(
                    session,
                    interrupt,
                    action_name="interrupt",
                )
            except Exception:
                # Interrupt is advisory. A confirmed disconnect below is the
                # authoritative provider-exit boundary.
                logger.debug("Claude interrupt failed during stop", exc_info=True)

        disconnect = getattr(client, "disconnect", None)
        if not callable(disconnect):
            raise RuntimeError(
                "Claude provider stop could not be confirmed: disconnect is unavailable"
            )
        await self._run_client_action(
            session,
            disconnect,
            action_name="disconnect",
        )

    async def _run_client_action(
        self,
        session: dict,
        action: object,
        *,
        action_name: str,
    ) -> None:
        stop_tasks = session["stop_tasks"]
        task = stop_tasks.get(action_name)
        if task is None:
            try:
                result = action()
            except Exception as exc:
                raise RuntimeError(
                    f"Claude provider {action_name} failed: {exc}"
                ) from exc
            task = asyncio.create_task(result)
            stop_tasks[action_name] = task
        done, _ = await asyncio.wait(
            {task},
            timeout=self._CLIENT_STOP_TIMEOUT_SECONDS,
        )
        if task not in done:
            # Keep the exact action live and session-owned. A retry joins it;
            # issuing a second disconnect could return early and falsely
            # retire ownership while the first action still holds the child.
            _retain_pending_client_action(task)
            raise RuntimeError(
                "Claude provider stop could not be confirmed: "
                f"{action_name} timed out"
            )
        try:
            task.result()
        except Exception as exc:
            stop_tasks.pop(action_name, None)
            raise RuntimeError(
                f"Claude provider {action_name} failed: {exc}"
            ) from exc
        stop_tasks.pop(action_name, None)


def _structured_events(
    tool_name: str, tool_input: dict, output: str,
    *, is_error: bool = False,
) -> list[AgentEvent]:
    """Derive structured ``command`` / ``file_change`` events from tool results.

    The Claude SDK surfaces Bash, Edit, and Write as generic ToolUseBlock /
    ToolResultBlock pairs.  This helper inspects the tool name and input to
    emit the richer event kinds that the chat UI expects for rendering
    command cards and file-change cards.
    """
    if tool_name == "Bash":
        return [AgentEvent(
            kind="command",
            cmd=tool_input.get("command", ""),
            exit_code=1 if is_error else 0,
            output=_truncate(output, 500),
        )]

    if tool_name in ("Edit", "Write"):
        if is_error:
            return []
        path = tool_input.get("file_path", "")
        if tool_name == "Edit":
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            diff = _make_edit_diff(old, new)
        else:
            diff = _make_write_diff(tool_input.get("content", ""))
        return [AgentEvent(
            kind="file_change",
            path=path,
            diff=_truncate(diff, 1000),
        )]

    return []


def _make_edit_diff(old: str, new: str) -> str:
    """Build a minimal unified-style diff preview from Edit old/new strings."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    parts: list[str] = []
    for line in old_lines:
        parts.append(f"-{line.rstrip()}")
    for line in new_lines:
        parts.append(f"+{line.rstrip()}")
    return "\n".join(parts) if parts else "(edit applied)"


def _make_write_diff(content: str) -> str:
    """Build a diff preview for a Write (new file creation) from file content."""
    if not content:
        return "(empty file)"
    lines = content.splitlines()
    parts = [f"+{line}" for line in lines]
    return "\n".join(parts)


def _truncate(s: object, max_len: int) -> str:
    s = "" if s is None else str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
