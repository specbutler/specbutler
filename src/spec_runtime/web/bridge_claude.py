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
import json
import logging
import uuid
from typing import AsyncIterator

from .bridge import AgentEvent

logger = logging.getLogger(__name__)


def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401

        return True
    except ImportError:
        return False


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
    ) -> str:
        import claude_agent_sdk

        session_id = session_id or uuid.uuid4().hex

        opts = claude_agent_sdk.ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="dontAsk",
            system_prompt=prompt,
        )
        if allowed_tools:
            opts.allowed_tools = list(allowed_tools)

        client = claude_agent_sdk.ClaudeSDKClient(options=opts)
        try:
            await client.connect()
        except BaseException:
            await self._stop_client(client)
            raise

        self._sessions[session_id] = {
            "client": client,
            "initial_prompt": initial_prompt,
            "started": False,
            "cancelled": False,
        }
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
                    await self.stop_session(session_id)
                    yield AgentEvent(
                        kind="error",
                        text=(
                            "Claude chat turn timed out after "
                            f"{self._TURN_INACTIVITY_TIMEOUT_SECONDS:g} seconds "
                            "without output"
                        ),
                    )
                    break

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
                    # Only use ResultMessage.result as a fallback — the
                    # text was already streamed via AssistantMessage blocks.
                    if message.result and not had_text:
                        yield AgentEvent(kind="text", text=message.result)

        except Exception as exc:
            if not session.get("cancelled"):
                yield AgentEvent(kind="error", text=str(exc))

        yield AgentEvent(kind="done")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session["cancelled"] = True
            client = session.get("client")
            if client is not None:
                cleanup = asyncio.create_task(self._stop_client(client))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Web shutdown cancellation must not detach the SDK child.
                    await cleanup
                    raise

    async def _stop_client(self, client: object) -> None:
        for method_name in ("interrupt", "disconnect"):
            action = getattr(client, method_name, None)
            if action is None:
                continue
            try:
                await asyncio.wait_for(
                    action(),
                    timeout=self._CLIENT_STOP_TIMEOUT_SECONDS,
                )
            except Exception:
                pass


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
