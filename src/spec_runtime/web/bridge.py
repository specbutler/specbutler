"""AgentBridge protocol and AgentEvent types for interactive chat sessions.

The bridge abstracts over pluggable agent backends (Claude Agent SDK, Codex
app-server) so the web server stays a thin relay rather than reimplementing
either agent.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Literal, Protocol, runtime_checkable

from spec_runtime.agent_git_isolation import AgentGitIsolation

# ---------------------------------------------------------------------------
# AgentEvent — union type covering all streamed event kinds
# ---------------------------------------------------------------------------

EventKind = Literal[
    "text",
    "tool_call",
    "tool_result",
    "file_change",
    "command",
    "error",
    "done",
]


@dataclass(frozen=True)
class AgentEvent:
    """A single event emitted by an agent during a chat turn.

    The *kind* field determines which other fields are populated:
    - ``text``: *text* contains the assistant message fragment.
    - ``tool_call``: *tool_name* and *tool_input* are set.
    - ``tool_result``: *tool_name* and *tool_output* are set.
    - ``file_change``: *path* and *diff* are set.
    - ``command``: *cmd*, *exit_code*, and *output* are set.
    - ``error``: *text* contains the error message.
    - ``done``: signals the turn is complete.
    """

    kind: EventKind

    # text / error
    text: str = ""

    # tool_call / tool_result
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""

    # file_change
    path: str = ""
    diff: str = ""

    # command
    cmd: str = ""
    exit_code: int | None = None
    output: str = ""


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------


@dataclass
class ChatSession:
    """Server-side state for an active chat session."""

    session_id: str
    mode: str  # "create" or "task"
    agent: str  # "claude" or "codex"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = ""
    status: str = "active"  # active / stopping / completed / error
    history: list = field(default_factory=list)
    initial_prompt: str = ""
    initial_turn_dispatched: bool = False  # True once the create-time turn starts
    live_turn: list = field(default_factory=list)
    worktree_path: str = ""  # dedicated worktree for the session
    branch: str = ""  # branch name for the session
    base_sha: str = ""  # commit SHA the worktree branched from
    base_ref: str = ""  # configured ref the worktree branched from
    owner_id: str = ""  # web-app instance that owns provider lifecycle
    cleanup_worktree_on_stop: bool = False  # deferred failed-start rollback
    stop_requested: bool = False
    handoff_completed: bool = False
    publication_baseline: tuple[str, str] | None = field(
        default=None,
        repr=False,
    )
    agent_git_isolation: AgentGitIsolation | None = field(default=None, repr=False)
    agent_git_reconciled_head: str = field(default="", repr=False)
    startup_done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    terminal_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.last_active = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# AgentBridge protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentBridge(Protocol):
    """Abstract interface for pluggable agent backends."""

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
        """Start a new interactive session. Returns a session_id."""
        ...

    def send_message(
        self,
        session_id: str,
        text: str,
    ) -> AsyncIterator[AgentEvent]:
        """Send a user message and stream agent events back."""
        ...

    async def stop_session(self, session_id: str) -> None:
        """Stop a running session."""
        ...


# ---------------------------------------------------------------------------
# Session registry (in-memory, ephemeral)
# ---------------------------------------------------------------------------

_sessions: dict[str, ChatSession] = {}
_bridges: dict[str, AgentBridge] = {}


def create_session(
    mode: str,
    agent: str,
    *,
    owner_id: str = "",
) -> ChatSession:
    """Create and register a new chat session."""
    session_id = uuid.uuid4().hex[:16]
    session = ChatSession(
        session_id=session_id,
        mode=mode,
        agent=agent,
        owner_id=owner_id,
    )
    session.touch()
    _sessions[session_id] = session
    return session


def get_session(session_id: str) -> ChatSession | None:
    return _sessions.get(session_id)


def list_sessions() -> list[ChatSession]:
    return list(_sessions.values())


def register_bridge(session_id: str, bridge: AgentBridge) -> None:
    _bridges[session_id] = bridge


def get_bridge(session_id: str) -> AgentBridge | None:
    return _bridges.get(session_id)


def remove_session(session_id: str) -> None:
    _sessions.pop(session_id, None)
    _bridges.pop(session_id, None)
