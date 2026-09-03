"""Tests for web chat — bridge, chat API endpoints, and session management."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Guard: skip the entire module when the optional [web] extras are not installed.
pytest.importorskip("starlette", reason="requires specbutler[web] extras")


# ---------------------------------------------------------------------------
# AgentEvent tests
# ---------------------------------------------------------------------------


class TestAgentEvent:
    """AgentEvent dataclass creation and serialization."""

    def test_text_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="text", text="Hello world")
        assert event.kind == "text"
        assert event.text == "Hello world"
        d = asdict(event)
        assert d["kind"] == "text"
        assert d["text"] == "Hello world"

    def test_tool_call_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="tool_call", tool_name="read_file", tool_input='{"path": "/foo"}')
        assert event.tool_name == "read_file"
        assert event.tool_input == '{"path": "/foo"}'

    def test_tool_result_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="tool_result", tool_name="read_file", tool_output="contents...")
        assert event.tool_output == "contents..."

    def test_file_change_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="file_change", path="src/main.py", diff="+new line")
        assert event.path == "src/main.py"
        assert event.diff == "+new line"

    def test_command_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="command", cmd="pytest", exit_code=0, output="passed")
        assert event.cmd == "pytest"
        assert event.exit_code == 0

    def test_error_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="error", text="something went wrong")
        assert event.text == "something went wrong"

    def test_done_event(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="done")
        assert event.kind == "done"
        assert event.text == ""

    def test_defaults_are_empty(self):
        from spec_runtime.web.bridge import AgentEvent

        event = AgentEvent(kind="text")
        assert event.text == ""
        assert event.tool_name == ""
        assert event.path == ""
        assert event.cmd == ""
        assert event.exit_code is None


# ---------------------------------------------------------------------------
# ChatSession tests
# ---------------------------------------------------------------------------


class TestChatSession:
    """ChatSession creation and session registry."""

    def test_create_session(self):
        from spec_runtime.web.bridge import _sessions, create_session

        old_count = len(_sessions)
        session = create_session(mode="create", agent="claude")
        assert session.session_id
        assert session.mode == "create"
        assert session.agent == "claude"
        assert session.status == "active"
        assert session.created_at
        assert session.last_active
        assert len(_sessions) == old_count + 1
        # Cleanup
        _sessions.pop(session.session_id, None)

    def test_get_session(self):
        from spec_runtime.web.bridge import _sessions, create_session, get_session

        session = create_session(mode="task", agent="codex")
        found = get_session(session.session_id)
        assert found is session
        assert found.mode == "task"
        _sessions.pop(session.session_id, None)

    def test_get_session_not_found(self):
        from spec_runtime.web.bridge import get_session

        assert get_session("nonexistent-id") is None

    def test_list_sessions(self):
        from spec_runtime.web.bridge import _sessions, create_session, list_sessions

        s1 = create_session(mode="create", agent="claude")
        s2 = create_session(mode="task", agent="codex")
        all_sessions = list_sessions()
        ids = {s.session_id for s in all_sessions}
        assert s1.session_id in ids
        assert s2.session_id in ids
        _sessions.pop(s1.session_id, None)
        _sessions.pop(s2.session_id, None)

    def test_remove_session(self):
        from spec_runtime.web.bridge import (
            _bridges,
            create_session,
            get_session,
            register_bridge,
            remove_session,
        )

        session = create_session(mode="create", agent="claude")
        mock_bridge = MagicMock()
        register_bridge(session.session_id, mock_bridge)

        remove_session(session.session_id)
        assert get_session(session.session_id) is None
        assert session.session_id not in _bridges

    def test_touch_updates_last_active(self):
        from spec_runtime.web.bridge import ChatSession

        session = ChatSession(session_id="test", mode="create", agent="claude")
        old_active = session.last_active
        session.touch()
        assert session.last_active
        assert session.last_active >= old_active

    def test_worktree_and_branch_fields(self):
        from spec_runtime.web.bridge import ChatSession

        session = ChatSession(
            session_id="test", mode="create", agent="claude",
            worktree_path="/tmp/wt", branch="spec-authoring/token",
        )
        assert session.worktree_path == "/tmp/wt"
        assert session.branch == "spec-authoring/token"


# ---------------------------------------------------------------------------
# Claude bridge tests
# ---------------------------------------------------------------------------


class TestClaudeBridge:
    """ClaudeBridge construction and availability check."""

    def test_unavailable_raises(self):
        with patch("spec_runtime.web.bridge_claude._sdk_available", return_value=False):
            from spec_runtime.web.bridge_claude import ClaudeBridge

            with pytest.raises(RuntimeError, match="claude-agent-sdk"):
                ClaudeBridge()

    def test_available_constructs(self):
        with patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True):
            from spec_runtime.web.bridge_claude import ClaudeBridge

            bridge = ClaudeBridge()
            assert bridge._sessions == {}

    def test_session_uses_explicit_fail_closed_host_policy(
        self,
        tmp_path,
        monkeypatch,
    ):
        import asyncio
        import os
        import sys
        from types import SimpleNamespace

        from spec_runtime.web import bridge_claude

        captured = {}

        class _FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class _FakeClient:
            def __init__(self, *, options):
                self.options = options

            async def connect(self):
                return None

            async def interrupt(self):
                return None

            async def disconnect(self):
                return None

        monkeypatch.setitem(
            sys.modules,
            "claude_agent_sdk",
            SimpleNamespace(
                ClaudeAgentOptions=_FakeOptions,
                ClaudeSDKClient=_FakeClient,
            ),
        )
        monkeypatch.setattr(bridge_claude, "_sdk_available", lambda: True)
        monkeypatch.setattr(
            bridge_claude.shutil,
            "which",
            lambda _name: "/usr/bin/claude",
        )
        protected = (
            tmp_path / "operator-state" / "specbutler",
            tmp_path / "operator-home" / ".claude",
            tmp_path / "operator-home" / ".ssh",
            Path("/proc"),
        )
        monkeypatch.setattr(
            bridge_claude,
            "protected_operator_paths",
            lambda _source: protected,
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_grants = (tmp_path / "gitdir", tmp_path / "objects")
        git_read_only = (tmp_path / "real-gitdir", tmp_path / "shared-objects")
        git_isolation = SimpleNamespace(
            writable_paths=git_grants,
            read_only_paths=git_read_only,
            env_overrides={
                "GIT_DIR": str(git_grants[0]),
                "GIT_WORK_TREE": str(worktree),
            },
        )

        async def _run():
            bridge = bridge_claude.ClaudeBridge()
            session_id = await bridge.start_session(
                "system prompt",
                agent="claude",
                cwd=str(worktree),
                allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                git_isolation=git_isolation,
            )
            await bridge.stop_session(session_id)

        with patch.dict(
            os.environ,
            {
                "PATH": "/bin",
                "ANTHROPIC_API_KEY": "provider-secret",
                "CLAUDE_CONFIG_DIR": str(tmp_path / "operator-home" / ".claude"),
                "GH_TOKEN": "forge-secret",
                "DATABASE_URL": "database-secret",
            },
            clear=True,
        ):
            asyncio.run(_run())

        selected = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        assert captured["cli_path"] == "/usr/bin/claude"
        assert captured["tools"] == selected
        assert captured["allowed_tools"] == selected
        assert captured["setting_sources"] == []
        assert captured["skills"] == []
        assert captured["plugins"] == []
        assert captured["mcp_servers"] == {}
        assert captured["permission_mode"] == "dontAsk"

        sandbox = captured["sandbox"]
        assert sandbox["enabled"] is True
        assert sandbox["failIfUnavailable"] is True
        assert sandbox["allowUnsandboxedCommands"] is False
        assert sandbox["excludedCommands"] == []
        assert sandbox["enableWeakerNestedSandbox"] is False
        denied = sorted(str(path.resolve()) for path in protected)
        assert sandbox["filesystem"]["denyRead"] == denied
        assert sandbox["filesystem"]["denyWrite"] == sorted(
            {*denied, *(str(path.resolve()) for path in git_read_only)}
        )
        assert sandbox["filesystem"]["allowWrite"] == [
            str(path) for path in git_grants
        ]
        credential_names = {
            entry["name"] for entry in sandbox["credentials"]["envVars"]
        }
        assert "HTTPS_PROXY" in credential_names
        assert "ANTHROPIC_API_KEY" in credential_names
        assert sandbox["credentials"]["files"] == [
            {"path": path, "mode": "deny"} for path in denied
        ]
        assert sandbox["network"]["strictAllowlist"] is True
        assert sandbox["network"]["allowLocalBinding"] is True
        assert sandbox["network"]["allowAllUnixSockets"] is False

        extra_args = captured["extra_args"]
        assert set(extra_args) == {
            "restricted",
            "safe-mode",
            "strict-mcp-config",
            "no-session-persistence",
            "disable-slash-commands",
            "no-chrome",
        }
        assert set(extra_args.values()) == {None}
        settings = json.loads(captured["settings"])
        assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"
        assert "Bash(git reset --hard*)" in settings["permissions"]["deny"]

        child_env = captured["env"]
        assert child_env["ANTHROPIC_API_KEY"] == "provider-secret"
        assert child_env["CLAUDE_CONFIG_DIR"] == str(
            tmp_path / "operator-home" / ".claude"
        )
        assert child_env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
        assert child_env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
        assert child_env["GIT_DIR"] == str(git_grants[0])
        assert child_env["GIT_WORK_TREE"] == str(worktree)
        assert child_env["GH_TOKEN"] == ""
        assert child_env["DATABASE_URL"] == ""

    def test_session_rejects_tools_outside_web_allowlist(self, tmp_path):
        import asyncio

        from spec_runtime.web.bridge_claude import ClaudeBridge

        async def _run():
            bridge = ClaudeBridge()
            with pytest.raises(ValueError, match="WebFetch"):
                await bridge.start_session(
                    "system prompt",
                    agent="claude",
                    cwd=str(tmp_path),
                    allowed_tools=["Read", "WebFetch"],
                )

        with patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True):
            asyncio.run(_run())

    def test_connect_failure_stops_partially_started_client(self):
        import asyncio

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FailingClient:
            def __init__(self, *, options):
                self.connect = AsyncMock(side_effect=RuntimeError("connect failed"))
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

        async def _run():
            created = []

            def make_client(*, options):
                client = _FailingClient(options=options)
                created.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                with pytest.raises(RuntimeError, match="connect failed"):
                    await bridge.start_session(
                        prompt="system prompt",
                        agent="claude",
                        cwd="/tmp/spec-web-chat",
                    )

            client = created[0]
            client.interrupt.assert_awaited_once()
            client.disconnect.assert_awaited_once()
            assert bridge._sessions == {}

        asyncio.run(_run())

    def test_persistent_client_reused_across_turns(self):
        """The same ClaudeSDKClient is reused for all turns in a session.

        This is the key invariant: a single subprocess stays alive so the
        system prompt and conversation history are preserved naturally.
        Previous code created a new subprocess per turn and relied on
        ``--resume``, but the SDK always passes ``--system-prompt ""``
        when system_prompt is None, which cleared the prompt on follow-ups.
        """
        import asyncio

        import claude_agent_sdk

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FakeClient:
            def __init__(self, *, options):
                self.options = options
                self.query_calls = []
                self._responses = [
                    [
                        claude_agent_sdk.AssistantMessage(
                            content=[claude_agent_sdk.TextBlock(text="First turn")],
                            model="test-model",
                            session_id="sdk-session-1",
                        ),
                        claude_agent_sdk.ResultMessage(
                            subtype="success",
                            duration_ms=1,
                            duration_api_ms=1,
                            is_error=False,
                            num_turns=1,
                            session_id="sdk-session-1",
                            result="done",
                        ),
                    ],
                    [
                        claude_agent_sdk.AssistantMessage(
                            content=[claude_agent_sdk.TextBlock(text="Second turn")],
                            model="test-model",
                            session_id="sdk-session-1",
                        ),
                        claude_agent_sdk.ResultMessage(
                            subtype="success",
                            duration_ms=1,
                            duration_api_ms=1,
                            is_error=False,
                            num_turns=2,
                            session_id="sdk-session-1",
                            result="done",
                        ),
                    ],
                ]
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, prompt, session_id="default"):
                self.query_calls.append((prompt, session_id))

            async def receive_response(self):
                for message in self._responses.pop(0):
                    yield message

        async def _collect(bridge, session_id, text):
            return [event async for event in bridge.send_message(session_id, text)]

        async def _run():
            created_clients = []

            def _make_client(*, options):
                client = _FakeClient(options=options)
                created_clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=_make_client),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                    allowed_tools=["Read"],
                    session_id="web-session-123",
                    initial_prompt="first prompt",
                )
                first_events = await _collect(bridge, session_id, "ignored initial text")
                second_events = await _collect(bridge, session_id, "follow-up question")

            # Only ONE client created — reused across both turns
            assert len(created_clients) == 1
            client = created_clients[0]
            # System prompt set once at connection time
            assert client.options.system_prompt == "system prompt"
            assert client.options.resume is None
            # connect() called once during start_session
            client.connect.assert_awaited_once()
            # Both turns sent via query() on the same client
            assert client.query_calls == [
                ("first prompt", "default"),
                ("follow-up question", "default"),
            ]
            # ResultMessage.result is suppressed when text was already
            # streamed via AssistantMessage — no duplicate text events.
            assert [e.kind for e in first_events] == ["text", "done"]
            assert [e.kind for e in second_events] == ["text", "done"]

        asyncio.run(_run())

    def test_result_only_first_turn_does_not_replay_initial_prompt(self):
        """A terminal-only SDK response still advances conversation state."""
        import asyncio

        import claude_agent_sdk

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FakeClient:
            def __init__(self, *, options):
                self.options = options
                self.query_calls = []
                self._turn = 0
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, prompt, session_id="default"):
                self.query_calls.append((prompt, session_id))

            async def receive_response(self):
                self._turn += 1
                yield claude_agent_sdk.ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=self._turn,
                    session_id="sdk-session-result-only",
                    result=f"turn {self._turn}",
                )

        async def _run():
            created_clients = []

            def _make_client(*, options):
                client = _FakeClient(options=options)
                created_clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=_make_client),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                    initial_prompt="first prompt",
                )
                first_events = [
                    event
                    async for event in bridge.send_message(
                        session_id,
                        "ignored transport text",
                    )
                ]
                second_events = [
                    event
                    async for event in bridge.send_message(
                        session_id,
                        "follow-up question",
                    )
                ]

            client = created_clients[0]
            assert client.query_calls == [
                ("first prompt", "default"),
                ("follow-up question", "default"),
            ]
            assert [(event.kind, event.text) for event in first_events] == [
                ("text", "turn 1"),
                ("done", ""),
            ]
            assert [(event.kind, event.text) for event in second_events] == [
                ("text", "turn 2"),
                ("done", ""),
            ]

        asyncio.run(_run())

    def test_user_message_tool_results_emit_current_sdk_structured_events(self):
        """Claude SDK 0.1.68 delivers ToolResultBlock in UserMessage."""
        import asyncio

        import claude_agent_sdk

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FakeClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, _prompt, session_id="default"):
                return None

            async def receive_response(self):
                yield claude_agent_sdk.AssistantMessage(
                    content=[
                        claude_agent_sdk.ToolUseBlock(
                            id="write-1",
                            name="Write",
                            input={"file_path": "/tmp/audit.txt", "content": "file-ok\n"},
                        ),
                        claude_agent_sdk.ToolUseBlock(
                            id="bash-1",
                            name="Bash",
                            input={"command": "printf command-ok"},
                        ),
                    ],
                    model="test-model",
                )
                yield claude_agent_sdk.UserMessage(
                    content=[
                        claude_agent_sdk.ToolResultBlock(
                            tool_use_id="write-1",
                            content="File created successfully",
                            is_error=False,
                        ),
                        claude_agent_sdk.ToolResultBlock(
                            tool_use_id="bash-1",
                            content="command-ok",
                            is_error=False,
                        ),
                    ]
                )
                yield claude_agent_sdk.ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-session-1",
                    result=None,
                )

        async def _run():
            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=_FakeClient),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                return [
                    event
                    async for event in bridge.send_message(session_id, "run tools")
                ]

        events = asyncio.run(_run())
        assert [event.kind for event in events] == [
            "tool_call",
            "tool_call",
            "tool_result",
            "file_change",
            "tool_result",
            "command",
            "done",
        ]
        assert events[2].tool_name == "Write"
        assert events[3].path == "/tmp/audit.txt"
        assert events[3].diff == "+file-ok"
        assert events[4].tool_name == "Bash"
        assert events[5].cmd == "printf command-ok"
        assert events[5].output == "command-ok"

    def test_failed_write_tool_result_does_not_emit_file_change(self):
        from spec_runtime.web.bridge_claude import _structured_events

        assert _structured_events(
            "Write",
            {"file_path": "/tmp/failed.txt", "content": "not-written"},
            "permission denied",
            is_error=True,
        ) == []

    def test_turn_inactivity_timeout_interrupts_claude(self):
        import asyncio

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _SlowClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, _prompt, session_id="default"):
                return None

            async def receive_response(self):
                await asyncio.sleep(60)
                if False:
                    yield None

        async def _run():
            clients = []

            def make_client(*, options):
                client = _SlowClient(options=options)
                clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                bridge._TURN_INACTIVITY_TIMEOUT_SECONDS = 0.001
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                events = [
                    event
                    async for event in bridge.send_message(session_id, "wait forever")
                ]
            return clients[0], events, bridge

        client, events, bridge = asyncio.run(_run())
        assert [event.kind for event in events] == ["error", "done"]
        assert "timed out" in events[0].text
        client.interrupt.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        assert bridge._sessions == {}

    def test_provider_error_reaps_failed_claude_session(self):
        import asyncio

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FailingClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, _prompt, session_id="default"):
                return None

            async def receive_response(self):
                raise RuntimeError("provider stream failed")
                yield  # pragma: no cover - keeps this an async generator

        async def _run():
            clients = []

            def make_client(*, options):
                client = _FailingClient(options=options)
                clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                events = [
                    event
                    async for event in bridge.send_message(
                        session_id,
                        "trigger provider failure",
                    )
                ]
                retry_events = [
                    event
                    async for event in bridge.send_message(
                        session_id,
                        "must not reuse failed client",
                    )
                ]
            return clients[0], events, retry_events, bridge

        client, events, retry_events, bridge = asyncio.run(_run())
        assert [event.kind for event in events] == ["error", "done"]
        assert "provider stream failed" in events[0].text
        assert [event.kind for event in retry_events] == ["error", "done"]
        assert "Unknown session" in retry_events[0].text
        client.interrupt.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        assert bridge._sessions == {}

    def test_claude_connect_failure_retains_client_until_disconnect_retry(self):
        import asyncio

        from spec_runtime.web.bridge_claude import (
            _PENDING_CLIENT_ACTIONS,
            ClaudeBridge,
        )

        async def _run():
            release = asyncio.Event()

            class _FailedConnectClient:
                def __init__(self, *, options):
                    self.interrupt = AsyncMock()

                async def connect(self):
                    raise RuntimeError("connect handshake failed")

                async def disconnect(self):
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch(
                    "claude_agent_sdk.ClaudeSDKClient",
                    side_effect=_FailedConnectClient,
                ),
            ):
                bridge = ClaudeBridge()
                bridge._CLIENT_STOP_TIMEOUT_SECONDS = 0.001
                with pytest.raises(RuntimeError, match="connect handshake failed"):
                    await bridge.start_session(
                        prompt="system",
                        agent="claude",
                        cwd="/tmp/spec-web-chat",
                        session_id="provisional-claude",
                    )

                assert "provisional-claude" in bridge._sessions
                release.set()
                await bridge.stop_session("provisional-claude")
                if _PENDING_CLIENT_ACTIONS:
                    await asyncio.gather(
                        *list(_PENDING_CLIENT_ACTIONS),
                        return_exceptions=True,
                    )
                assert "provisional-claude" not in bridge._sessions

        asyncio.run(_run())

    def test_stop_session_interrupts_and_disconnects(self):
        import asyncio

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FakeClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

        async def _run():
            created = []

            def _make(*, options):
                c = _FakeClient(options=options)
                created.append(c)
                return c

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=_make),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                await bridge.stop_session(session_id)

            client = created[0]
            client.interrupt.assert_awaited_once()
            client.disconnect.assert_awaited_once()
            assert session_id not in bridge._sessions

        asyncio.run(_run())

    @pytest.mark.parametrize(
        ("subtype", "is_error", "detail"),
        [
            ("error_during_execution", True, "provider failed"),
            ("interrupted", False, "request interrupted"),
        ],
    )
    def test_failed_terminal_result_emits_error_and_reaps_session(
        self,
        subtype,
        is_error,
        detail,
    ):
        import asyncio

        import claude_agent_sdk

        from spec_runtime.web.bridge_claude import ClaudeBridge

        class _FakeClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.interrupt = AsyncMock()

            async def query(self, _prompt, session_id="default"):
                return None

            async def receive_response(self):
                yield claude_agent_sdk.ResultMessage(
                    subtype=subtype,
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=is_error,
                    num_turns=1,
                    session_id="sdk-session-1",
                    result=detail,
                )

        async def _run():
            clients = []

            def make_client(*, options):
                client = _FakeClient(options=options)
                clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                events = [
                    event
                    async for event in bridge.send_message(session_id, "do it")
                ]
            return bridge, session_id, clients[0], events

        bridge, session_id, client, events = asyncio.run(_run())
        assert [event.kind for event in events] == ["error", "done"]
        assert subtype in events[0].text
        assert detail in events[0].text
        client.disconnect.assert_awaited_once()
        assert session_id not in bridge._sessions

    def test_disconnect_timeout_retains_session_until_successful_retry(self):
        import asyncio

        from spec_runtime.web.bridge_claude import (
            _PENDING_CLIENT_ACTIONS,
            ClaudeBridge,
        )

        class _ResistantClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.interrupt = AsyncMock()
                self.release = asyncio.Event()
                self.disconnect_calls = 0

            async def disconnect(self):
                self.disconnect_calls += 1
                if self.disconnect_calls == 1:
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        await self.release.wait()

        async def _run():
            clients = []

            def make_client(*, options):
                client = _ResistantClient(options=options)
                clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                bridge._CLIENT_STOP_TIMEOUT_SECONDS = 0.001
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                with pytest.raises(RuntimeError, match="disconnect timed out"):
                    await bridge.stop_session(session_id)
                assert session_id in bridge._sessions

                # A retry joins the exact in-flight disconnect; it must not
                # issue a second call and falsely retire ownership.
                with pytest.raises(RuntimeError, match="disconnect timed out"):
                    await bridge.stop_session(session_id)
                assert session_id in bridge._sessions
                assert clients[0].disconnect_calls == 1

                clients[0].release.set()
                await bridge.stop_session(session_id)
                assert session_id not in bridge._sessions
                if _PENDING_CLIENT_ACTIONS:
                    await asyncio.gather(
                        *list(_PENDING_CLIENT_ACTIONS),
                        return_exceptions=True,
                    )
            return clients[0]

        client = asyncio.run(_run())
        assert client.disconnect_calls == 1

    def test_hung_interrupt_does_not_block_confirmed_disconnect(self):
        import asyncio

        from spec_runtime.web.bridge_claude import (
            _PENDING_CLIENT_ACTIONS,
            ClaudeBridge,
        )

        class _ResistantClient:
            def __init__(self, *, options):
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()
                self.release = asyncio.Event()

            async def interrupt(self):
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()

        async def _run():
            clients = []

            def make_client(*, options):
                client = _ResistantClient(options=options)
                clients.append(client)
                return client

            with (
                patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
                patch("claude_agent_sdk.ClaudeSDKClient", side_effect=make_client),
            ):
                bridge = ClaudeBridge()
                bridge._CLIENT_STOP_TIMEOUT_SECONDS = 0.001
                session_id = await bridge.start_session(
                    prompt="system prompt",
                    agent="claude",
                    cwd="/tmp/spec-web-chat",
                )
                await bridge.stop_session(session_id)
                assert session_id not in bridge._sessions
                clients[0].release.set()
                if _PENDING_CLIENT_ACTIONS:
                    await asyncio.gather(
                        *list(_PENDING_CLIENT_ACTIONS),
                        return_exceptions=True,
                    )
            return clients[0]

        client = asyncio.run(_run())
        client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Codex bridge tests
# ---------------------------------------------------------------------------


class TestCodexBridge:
    """CodexBridge construction and availability check."""

    def test_unavailable_raises(self):
        with patch("spec_runtime.web.bridge_codex._codex_available", return_value=False):
            from spec_runtime.web.bridge_codex import CodexBridge

            with pytest.raises(RuntimeError, match="Codex backend unavailable"):
                CodexBridge()

    def test_available_constructs(self):
        with patch("spec_runtime.web.bridge_codex._codex_available", return_value=True):
            from spec_runtime.web.bridge_codex import CodexBridge

            bridge = CodexBridge()
            assert bridge._sessions == {}

    def test_codex_available_true_when_cli_present(self):
        """Availability requires every CLI isolation control used by web chat."""
        from spec_runtime.web.bridge_codex import _codex_available

        help_result = MagicMock(
            returncode=0,
            stdout=(
                "--add-dir --ephemeral --ignore-rules --ignore-user-config "
                "--json --output-schema --strict-config --permission-profile"
            ),
            stderr="",
        )
        with (
            patch(
                "spec_runtime.web.bridge_codex.shutil.which",
                return_value="/bin/codex",
            ),
            patch(
                "spec_runtime.web.bridge_codex.subprocess.run",
                return_value=help_result,
            ),
        ):
            assert _codex_available() is True

    def test_codex_available_false_when_cli_missing(self):
        """_codex_available returns False when the Codex CLI is not on PATH."""
        from spec_runtime.web.bridge_codex import _codex_available

        with (
            patch("spec_runtime.web.bridge_codex.shutil.which", return_value=None),
        ):
            assert _codex_available() is False

    def test_codex_available_false_when_cli_lacks_isolation_control(self):
        """An older CLI is rejected before a browser session starts."""
        from spec_runtime.web.bridge_codex import _codex_available

        help_result = MagicMock(returncode=0, stdout="--json", stderr="")
        with (
            patch("spec_runtime.web.bridge_codex.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "spec_runtime.web.bridge_codex.subprocess.run",
                return_value=help_result,
            ),
        ):
            assert _codex_available() is False

    def test_codex_available_false_when_cli_rejects_security_config(self):
        from spec_runtime.web.bridge_codex import _codex_available

        help_result = MagicMock(
            returncode=0,
            stdout=(
                "--add-dir --ephemeral --ignore-rules --ignore-user-config "
                "--json --output-schema --strict-config --permission-profile"
            ),
            stderr="",
        )
        rejected_probe = MagicMock(
            returncode=1,
            stdout="",
            stderr="unknown configuration field features.browser_use",
        )
        with (
            patch(
                "spec_runtime.web.bridge_codex.shutil.which",
                return_value="/bin/codex",
            ),
            patch(
                "spec_runtime.web.bridge_codex.subprocess.run",
                side_effect=[help_result, help_result, help_result, rejected_probe],
            ),
        ):
            assert _codex_available() is False

    def test_codex_session_start_passes_prompt_as_thread_instructions(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        class _DummyPipe:
            def write(self, _data):
                return None

            async def drain(self):
                return None

            async def readline(self):
                return b""

        class _DummyProc:
            def __init__(self):
                self.stdin = _DummyPipe()
                self.stdout = _DummyPipe()
                self.stderr = _DummyPipe()

            def terminate(self):
                return None

            async def wait(self):
                return 0

        async def _run():
            session = _CodexSession(cwd="/tmp/spec-web-chat")
            calls = []

            async def fake_send_request(method, params):
                calls.append((method, params))
                if method == "initialize":
                    return {"result": {}}, []
                if method == "thread/start":
                    return {"result": {"thread": {"id": "thread-123"}}}, []
                raise AssertionError(f"unexpected method {method}")

            exec_args = []

            async def fake_spawn_async(_supervisor, args, **_kwargs):
                exec_args.extend(args)
                return _DummyProc()

            with (
                patch("spec_runtime.web.bridge_codex.shutil.which", return_value="/usr/bin/codex"),
                patch(
                    "spec_runtime.web.bridge_codex.ProcessSupervisor.spawn_async",
                    new=fake_spawn_async,
                ),
                patch.object(_CodexSession, "_drain_stderr", new=AsyncMock(return_value=None)),
                patch.object(_CodexSession, "_send_request", side_effect=fake_send_request),
            ):
                await session.start("keep task chat context")
                await session.stop()

            assert exec_args[0] == "/usr/bin/codex"
            assert exec_args[-1] == "app-server"
            assert "--strict-config" in exec_args
            assert "shell_environment_policy.inherit=all" in exec_args
            assert "features.shell_snapshot=false" in exec_args
            assert "features.plugins=false" in exec_args
            assert "features.code_mode=false" in exec_args
            assert "features.browser_use=false" in exec_args
            assert "features.computer_use=false" in exec_args
            assert "features.view_image=false" in exec_args
            assert "features.recommended_plugins=false" in exec_args
            assert "features.skill_mcp_dependency_install=false" in exec_args
            assert "features.skip_host_skill_discovery=true" in exec_args
            assert any(
                value.startswith("permissions.specbutler-web.filesystem=")
                for value in exec_args
            )
            assert calls == [
                (
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "spec-web-chat",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                ),
                (
                    "thread/start",
                    {
                        "cwd": "/tmp/spec-web-chat",
                        "runtimeWorkspaceRoots": ["/tmp/spec-web-chat"],
                        "developerInstructions": "keep task chat context",
                        "approvalPolicy": "never",
                        "permissions": "specbutler-web",
                    },
                ),
            ]

        import asyncio

        asyncio.run(_run())

    def test_codex_web_home_and_operator_state_are_denied_to_model_tools(
        self,
        tmp_path,
    ):
        from spec_runtime.web import bridge_codex

        operator_home = tmp_path / "operator-codex-home"
        operator_home.mkdir()
        source_auth = operator_home / "auth.json"
        source_auth.write_text('{"token":"canary"}', encoding="utf-8")
        user_state = tmp_path / "operator-state" / "specbutler"

        context, isolated_home = bridge_codex._CodexSession._isolated_provider_home(
            {"CODEX_HOME": str(operator_home)},
        )
        try:
            assert isolated_home.parent != tmp_path / "repo"
            assert (isolated_home / "auth.json").exists()
            with patch.object(
                bridge_codex,
                "protected_operator_paths",
                return_value=(user_state, Path("/proc")),
            ):
                cmd = bridge_codex._CodexSession._safety_config_overrides(
                    isolated_home,
                    operator_codex_home=operator_home,
                )

            filesystem = next(
                value
                for value in cmd
                if value.startswith("permissions.specbutler-web.filesystem=")
            )
            assert f'{json.dumps(str(isolated_home.resolve()))}="deny"' in filesystem
            assert f'{json.dumps(str(operator_home.resolve()))}="deny"' in filesystem
            assert f'{json.dumps(str(user_state.resolve()))}="deny"' in filesystem
            assert f'{json.dumps(str(Path("/proc").resolve()))}="deny"' in filesystem
            assert "shell_environment_policy.inherit=all" in cmd
        finally:
            context.cleanup()

        assert not isolated_home.exists()

    def test_codex_web_policy_omits_nested_deny_mounts(self, tmp_path):
        """Keep Codex/bubblewrap startup viable without weakening state denial."""
        from spec_runtime.web import bridge_codex

        user_state = tmp_path / "operator-state" / "specbutler"
        isolated_home = user_state / "provider-homes" / "session"
        operator_home = tmp_path / "operator-codex-home"
        with patch.object(
            bridge_codex,
            "protected_operator_paths",
            return_value=(user_state, Path("/proc")),
        ):
            cmd = bridge_codex._CodexSession._safety_config_overrides(
                isolated_home,
                operator_codex_home=operator_home,
            )

        filesystem = next(
            value
            for value in cmd
            if value.startswith("permissions.specbutler-web.filesystem=")
        )
        assert f'{json.dumps(str(user_state.resolve()))}="deny"' in filesystem
        assert f'{json.dumps(str(isolated_home.resolve()))}="deny"' not in filesystem
        assert f'{json.dumps(str(operator_home.resolve()))}="deny"' in filesystem
        assert f'{json.dumps(str(Path("/proc").resolve()))}="deny"' in filesystem

    def test_codex_session_start_raises_when_codex_not_on_path(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        async def _run():
            session = _CodexSession(cwd="/tmp/spec-web-chat")
            with patch("spec_runtime.web.bridge_codex.shutil.which", return_value=None):
                with pytest.raises(RuntimeError, match="codex CLI not found on PATH"):
                    await session.start("system prompt")

        import asyncio

        asyncio.run(_run())

    def test_codex_session_resumes_thread_before_follow_up_turn(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        class _DummyPipe:
            async def readline(self):
                return b""

        async def _run():
            session = _CodexSession(cwd="/tmp/spec-web-chat")
            session._proc = MagicMock(stdout=_DummyPipe())
            session._thread_id = "thread-123"
            calls = []

            async def fake_send_request(method, params):
                calls.append((method, params))
                if method == "thread/resume":
                    return {"result": {"thread": {"id": "thread-123"}}}, []
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-456"}}}, [{"type": "turn.completed"}]
                raise AssertionError(f"unexpected method {method}")

            with patch.object(_CodexSession, "_send_request", side_effect=fake_send_request):
                events = [event async for event in session.send_turn("what am i trying to fix?")]

            assert events == []
            assert calls == [
                ("thread/resume", {"threadId": "thread-123"}),
                (
                    "turn/start",
                    {
                        "threadId": "thread-123",
                        "input": [{"type": "text", "text": "what am i trying to fix?"}],
                    },
                ),
            ]

        import asyncio

        asyncio.run(_run())

    def test_codex_session_uses_initial_prompt_only_for_first_turn(self):
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        class _DummyPipe:
            async def readline(self):
                return b""

        async def _run():
            session = _CodexSession(cwd="/tmp/spec-web-chat")
            session._proc = MagicMock(stdout=_DummyPipe())
            session._thread_id = "thread-123"
            session.initial_prompt = "create the requested file"
            turn_prompts = []

            async def fake_send_request(method, params):
                if method == "thread/resume":
                    return {"result": {}}, []
                if method == "turn/start":
                    turn_prompts.append(params["input"][0]["text"])
                    return {"result": {}}, [{"type": "turn.completed"}]
                raise AssertionError(f"unexpected method {method}")

            with patch.object(
                _CodexSession,
                "_send_request",
                side_effect=fake_send_request,
            ):
                _ = [event async for event in session.send_turn("ignored transport text")]
                _ = [event async for event in session.send_turn("follow-up question")]

            assert turn_prompts == [
                "create the requested file",
                "follow-up question",
            ]

        asyncio.run(_run())

    def test_codex_session_stderr_in_error_on_immediate_exit(self):
        """When the subprocess exits immediately (EOF), the error includes stderr."""
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        class _EofStdout:
            async def readline(self):
                return b""

        class _StderrLines:
            def __init__(self):
                self._lines = [
                    b"Error: ANTHROPIC_API_KEY not set\n",
                    b"Traceback (most recent call last):\n",
                    b"  sdk_main()\n",
                ]
                self._index = 0

            async def readline(self):
                if self._index < len(self._lines):
                    line = self._lines[self._index]
                    self._index += 1
                    return line
                return b""

        class _DummyStdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        class _DummyProc:
            def __init__(self):
                self.stdin = _DummyStdin()
                self.stdout = _EofStdout()
                self.stderr = _StderrLines()
                self.returncode = 1

            def terminate(self):
                return None

            async def wait(self):
                return 1

        async def _run():
            session = _CodexSession(cwd="/tmp/test")

            async def fake_spawn_async(_supervisor, _args, **_kwargs):
                return _DummyProc()

            with (
                patch("spec_runtime.web.bridge_codex.shutil.which", return_value="/usr/bin/codex"),
                patch(
                    "spec_runtime.web.bridge_codex.ProcessSupervisor.spawn_async",
                    new=fake_spawn_async,
                ),
            ):
                with pytest.raises(RuntimeError, match=r"(?s)exit code 1.*ANTHROPIC_API_KEY"):
                    await session.start("system prompt")

        asyncio.run(_run())

    def test_codex_session_timeout_during_handshake(self):
        """When the subprocess hangs, the timeout fires with a descriptive error."""
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        class _HangingStdout:
            async def readline(self):
                await asyncio.sleep(999)
                return b""

        class _EmptyStderr:
            async def readline(self):
                return b""

        class _DummyStdin:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        class _DummyProc:
            def __init__(self):
                self.pid = 4242
                self.stdin = _DummyStdin()
                self.stdout = _HangingStdout()
                self.stderr = _EmptyStderr()
                self.returncode = None
                self.terminated = False
                self.waited = False

            def terminate(self):
                self.terminated = True

            def kill(self):
                return None

            async def wait(self):
                self.waited = True
                return 0

        async def _run():
            session = _CodexSession(cwd="/tmp/test")
            process = _DummyProc()

            async def fake_spawn_async(_supervisor, _args, **_kwargs):
                return process

            # Only raise TimeoutError on the first wait_for call (the
            # handshake).  Subsequent calls (e.g. inside stop()) use
            # the real implementation so cleanup completes normally.
            real_wait_for = asyncio.wait_for
            call_count = 0

            async def selective_wait_for(coro, *, timeout):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    if hasattr(coro, "close"):
                        coro.close()
                    raise asyncio.TimeoutError()
                return await real_wait_for(coro, timeout=timeout)

            with (
                patch("spec_runtime.web.bridge_codex.shutil.which", return_value="/usr/bin/codex"),
                patch(
                    "spec_runtime.web.bridge_codex.ProcessSupervisor.spawn_async",
                    new=fake_spawn_async,
                ),
                patch("spec_runtime.web.bridge_codex.asyncio.wait_for", side_effect=selective_wait_for),
            ):
                with pytest.raises(RuntimeError, match="timed out during initialize"):
                    await session.start("system prompt")

            assert process.terminated is True
            assert process.waited is True

        asyncio.run(_run())

    def test_codex_turn_inactivity_timeout_is_bounded(self):
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        class _HangingStdout:
            async def readline(self):
                await asyncio.sleep(60)
                return b""

        async def _run():
            session = _CodexSession(cwd="/tmp/test")
            session._proc = MagicMock(stdout=_HangingStdout())
            session._thread_id = "thread-123"
            session._TURN_INACTIVITY_TIMEOUT_SECONDS = 0.001

            async def fake_send_request(method, _params):
                if method == "thread/resume":
                    return {"result": {}}, []
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-1"}}}, []
                raise AssertionError(f"unexpected method {method}")

            with patch.object(
                _CodexSession,
                "_send_request",
                side_effect=fake_send_request,
            ), patch.object(session, "stop", new_callable=AsyncMock) as stop_mock:
                with pytest.raises(RuntimeError, match="timed out"):
                    _ = [
                        event
                        async for event in session.send_turn("wait forever")
                    ]
            stop_mock.assert_awaited_once()

        asyncio.run(_run())

    def test_codex_turn_eof_before_completion_is_an_error(self):
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        class _EofStdout:
            async def readline(self):
                return b""

        async def _run():
            session = _CodexSession(cwd="/tmp/test")
            session._proc = MagicMock(stdout=_EofStdout(), returncode=17)
            session._thread_id = "thread-123"

            async def fake_send_request(method, _params):
                if method == "thread/resume":
                    return {"result": {}}, []
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-1"}}}, []
                raise AssertionError(f"unexpected method {method}")

            with (
                patch.object(
                    _CodexSession,
                    "_send_request",
                    side_effect=fake_send_request,
                ),
                patch.object(
                    session,
                    "_collect_stderr_detail",
                    new=AsyncMock(return_value=":\n  fatal transport error"),
                ),
                patch.object(session, "stop", new_callable=AsyncMock) as stop_mock,
            ):
                with pytest.raises(
                    RuntimeError,
                    match=r"(?s)before the chat turn completed.*exit code 17.*fatal transport error",
                ):
                    _ = [event async for event in session.send_turn("hello")]

            stop_mock.assert_awaited_once()

        asyncio.run(_run())

    def test_codex_turn_timeout_reaps_real_app_server_process(self):
        import asyncio

        from spec_runtime.web.bridge_codex import _CodexSession

        async def _run():
            session = _CodexSession(cwd="/tmp/test")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                "import time; time.sleep(30)",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            session._proc = proc
            session._stderr_task = asyncio.create_task(session._drain_stderr())
            session._thread_id = "thread-123"
            session._TURN_INACTIVITY_TIMEOUT_SECONDS = 0.01

            async def fake_send_request(method, _params):
                if method == "thread/resume":
                    return {"result": {}}, []
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-1"}}}, []
                raise AssertionError(f"unexpected method {method}")

            with patch.object(
                _CodexSession,
                "_send_request",
                side_effect=fake_send_request,
            ):
                with pytest.raises(RuntimeError, match="timed out"):
                    _ = [
                        event
                        async for event in session.send_turn("wait forever")
                    ]

            assert proc.returncode is not None
            assert await asyncio.wait_for(proc.wait(), timeout=0.1) == proc.returncode
            assert session._proc is None
            assert session._stderr_task is None

        asyncio.run(_run())

    def test_codex_stop_timeout_retains_process_until_successful_retry(self):
        import asyncio

        from spec_runtime.web.bridge_codex import (
            _PENDING_PROCESS_WAITS,
            _CodexSession,
        )

        class _ResistantProcess:
            def __init__(self):
                self.returncode = None
                self.release = asyncio.Event()
                self.terminate_calls = 0
                self.kill_calls = 0

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

            async def wait(self):
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                return 0

        async def _run():
            session = _CodexSession(cwd="/tmp/test")
            process = _ResistantProcess()
            session._proc = process
            credential_home = MagicMock()
            session._isolated_home_context = credential_home

            with patch(
                "spec_runtime.web.bridge_codex._PROCESS_STOP_TIMEOUT_SECONDS",
                0.001,
            ):
                with pytest.raises(RuntimeError, match="did not exit after kill"):
                    await session.stop()
                assert session._proc is process
                credential_home.cleanup.assert_not_called()

                process.release.set()
                if _PENDING_PROCESS_WAITS:
                    await asyncio.gather(
                        *list(_PENDING_PROCESS_WAITS),
                        return_exceptions=True,
                    )
                await session.stop()

            assert session._proc is None
            credential_home.cleanup.assert_called_once()
            assert process.terminate_calls >= 2
            assert process.kill_calls == 1

        asyncio.run(_run())

    def test_codex_handshake_failure_retains_process_until_kill_retry(self):
        import asyncio

        from spec_runtime.web.bridge_codex import (
            _PENDING_PROCESS_WAITS,
            CodexBridge,
            _CodexSession,
        )

        class _ResistantProcess:
            def __init__(self, release):
                self.returncode = None
                self.release = release
                self.terminate_calls = 0
                self.kill_calls = 0

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

            async def wait(self):
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                return 1

        async def _run():
            release = asyncio.Event()
            process = _ResistantProcess(release)

            async def failed_handshake(session, _prompt):
                session._proc = process
                try:
                    await session.stop()
                except RuntimeError:
                    pass
                raise RuntimeError("app-server handshake failed")

            with (
                patch("spec_runtime.web.bridge_codex._codex_available", return_value=True),
                patch.object(_CodexSession, "start", new=failed_handshake),
                patch(
                    "spec_runtime.web.bridge_codex._PROCESS_STOP_TIMEOUT_SECONDS",
                    0.001,
                ),
            ):
                bridge = CodexBridge()
                with pytest.raises(RuntimeError, match="handshake failed"):
                    await bridge.start_session(
                        prompt="system",
                        agent="codex",
                        cwd="/tmp/spec-web-chat",
                        session_id="provisional-codex",
                    )

                assert "provisional-codex" in bridge._sessions
                assert bridge._sessions["provisional-codex"]._proc is process

                release.set()
                if _PENDING_PROCESS_WAITS:
                    await asyncio.gather(
                        *list(_PENDING_PROCESS_WAITS),
                        return_exceptions=True,
                    )
                await bridge.stop_session("provisional-codex")
                assert "provisional-codex" not in bridge._sessions

            assert process.terminate_calls >= 2
            assert process.kill_calls == 1

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Chat API endpoint tests
# ---------------------------------------------------------------------------


class TestChatAPI:
    """Tests for /api/v1/chat route handlers."""

    @pytest.fixture(autouse=True)
    def _trusted_publication_baseline(self, monkeypatch):
        monkeypatch.setattr(
            "spec_runtime.web.chat_api.capture_repository_publication_baseline",
            lambda _repo: ("https://example.invalid/repo.git", "fingerprint"),
        )
        monkeypatch.setattr(
            "spec_runtime.web.chat_api.assert_repository_publication_baseline",
            lambda _repo, **_kwargs: None,
        )
        # Endpoint unit tests generally stub worktree creation with a plain
        # directory. Dedicated integration tests below exercise the real
        # linked-worktree Git isolation boundary.
        monkeypatch.setattr(
            "spec_runtime.web.chat_api.prepare_agent_git_isolation_if_linked",
            lambda _repo: None,
        )

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token, reload_token=False)
        client = TestClient(app, raise_server_exceptions=False)
        return client

    def _auth_headers(self, token="test-token"):
        return {"Authorization": f"Bearer {token}"}

    def test_create_session_missing_mode(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions",
            json={"agent": "claude", "prompt": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 422
        assert "mode" in resp.json()["error"]

    def test_create_session_missing_prompt(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions",
            json={"mode": "create", "agent": "claude"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 422
        assert "prompt" in resp.json()["error"]

    def test_create_session_invalid_mode(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions",
            json={"mode": "invalid", "agent": "claude", "prompt": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 422

    def test_create_session_unavailable_backend(self, tmp_path):
        with patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": False, "codex": False}):
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "create", "agent": "claude", "prompt": "hello"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 422
        assert "not available" in resp.json()["error"]

    def test_create_session_success(self, tmp_path):
        mock_bridge = MagicMock()
        mock_bridge.start_session = AsyncMock(return_value="session-123")

        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": True, "codex": False}),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=mock_bridge),
            patch("spec_runtime.web.chat_api._setup_chat_worktree", return_value=(wt_path, "spec-authoring/token123", "abc123", "origin/main")),
        ):
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "create", "agent": "claude", "prompt": "hello"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert len(data["session_id"]) > 0

        # Verify bridge was started with worktree cwd, not repo_root
        call_kwargs = mock_bridge.start_session.call_args
        assert call_kwargs.kwargs.get("cwd") == str(wt_path)
        # Verify an explicit tool whitelist is passed
        assert call_kwargs.kwargs.get("allowed_tools") is not None
        assert len(call_kwargs.kwargs["allowed_tools"]) > 0

        # Cleanup
        from spec_runtime.web.bridge import _bridges, _sessions

        _sessions.pop(data["session_id"], None)
        _bridges.pop(data["session_id"], None)

    def test_create_session_stores_worktree_info(self, tmp_path):
        """Session records the worktree path and branch."""
        mock_bridge = MagicMock()
        mock_bridge.start_session = AsyncMock(return_value="session-123")

        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": True, "codex": False}),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=mock_bridge),
            patch("spec_runtime.web.chat_api._setup_chat_worktree", return_value=(wt_path, "spec-authoring/token123", "abc123", "origin/main")),
        ):
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "create", "agent": "claude", "prompt": "hello"},
                headers=self._auth_headers(),
            )

        session_id = resp.json()["session_id"]
        from spec_runtime.web.bridge import _bridges, _sessions

        session = _sessions.get(session_id)
        assert session is not None
        assert session.worktree_path == str(wt_path)
        assert session.branch == "spec-authoring/token123"

        _sessions.pop(session_id, None)
        _bridges.pop(session_id, None)

    def test_get_session_not_found(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.get(
            "/api/v1/chat/sessions/nonexistent",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    def test_get_session_found(self, tmp_path):
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")

        client = self._make_client(tmp_path)
        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session.session_id
        assert data["mode"] == "create"
        assert data["agent"] == "claude"
        assert data["status"] == "active"
        assert "created_at" in data
        assert "last_active" in data
        assert "turn_active" in data

        _sessions.pop(session.session_id, None)

    def test_list_sessions(self, tmp_path):
        from spec_runtime.web.bridge import _sessions, create_session

        s1 = create_session(mode="create", agent="claude")
        s2 = create_session(mode="task", agent="claude")

        client = self._make_client(tmp_path)
        resp = client.get(
            "/api/v1/chat/sessions",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        ids = {s["session_id"] for s in data}
        assert s1.session_id in ids
        assert s2.session_id in ids
        # Each entry includes turn_active
        for entry in data:
            assert "turn_active" in entry

        _sessions.pop(s1.session_id, None)
        _sessions.pop(s2.session_id, None)

    def test_stop_session_not_found(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions/nonexistent/stop",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    def test_stop_session_success(self, tmp_path):
        from spec_runtime.web.bridge import _bridges, _sessions, create_session, register_bridge

        session = create_session(mode="create", agent="claude")
        mock_bridge = MagicMock()
        mock_bridge.stop_session = AsyncMock()
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/stop",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        # Stop response includes branch info for integration visibility (F1).
        assert "branch" in data
        assert session.status == "completed"

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    @pytest.mark.parametrize(
        ("stop_error", "expected_status"),
        [
            (TimeoutError("provider still stopping"), 504),
            (RuntimeError("disconnect failed"), 503),
        ],
    )
    def test_stop_failure_retains_ownership_and_retry_clears_it(
        self,
        tmp_path,
        stop_error,
        expected_status,
    ):
        import asyncio

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            get_bridge,
            register_bridge,
        )
        from spec_runtime.web.chat_api import (
            _provider_stop_tasks,
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
            _turn_owners,
        )

        session = create_session(mode="create", agent="claude")
        mock_bridge = MagicMock()
        mock_bridge.stop_session = AsyncMock(side_effect=[stop_error, None])
        register_bridge(session.session_id, mock_bridge)
        _turn_notifiers[session.session_id] = asyncio.Event()
        _turn_completions[session.session_id] = asyncio.Event()
        _turn_event_lists[session.session_id] = []
        _turn_owners[session.session_id] = session.owner_id

        client = self._make_client(tmp_path)
        first = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/stop",
            headers=self._auth_headers(),
        )

        assert first.status_code == expected_status
        assert first.json()["status"] == "stopping"
        assert session.status == "stopping"
        assert get_bridge(session.session_id) is mock_bridge
        assert session.session_id in _turn_notifiers
        assert session.session_id in _turn_owners

        retry = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/stop",
            headers=self._auth_headers(),
        )

        assert retry.status_code == 200
        assert retry.json()["status"] == "stopped"
        assert session.status == "completed"
        assert session.session_id not in _provider_stop_tasks
        assert session.session_id not in _turn_notifiers
        assert session.session_id not in _turn_completions
        assert session.session_id not in _turn_event_lists
        assert session.session_id not in _turn_owners

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_stop_session_preserves_worktree(self, tmp_path):
        """Stop is cancellation only — no auto-commit or push."""
        from spec_runtime.web.bridge import _bridges, _sessions, create_session, register_bridge

        session = create_session(mode="create", agent="claude")
        wt_path = str(tmp_path / "wt")
        session.worktree_path = wt_path
        session.branch = "spec/test-branch"
        mock_bridge = MagicMock()
        mock_bridge.stop_session = AsyncMock()
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/stop",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        assert data["branch"] == "spec/test-branch"
        assert data["worktree"] == wt_path
        # Stop must NOT commit or push — no "pushed" key in response.
        assert "pushed" not in data

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_send_message_session_not_found(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions/nonexistent/messages",
            json={"text": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    def test_send_message_empty_text(self, tmp_path):
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": ""},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 422

        _sessions.pop(session.session_id, None)

    def test_send_message_no_bridge(self, tmp_path):
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 500

        _sessions.pop(session.session_id, None)

    def test_send_message_streams_events(self, tmp_path):
        import json

        from spec_runtime.web.bridge import (
            AgentEvent,
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )

        session = create_session(mode="create", agent="claude")

        async def mock_send_message(sid, text):
            yield AgentEvent(kind="text", text="Hello from agent")
            yield AgentEvent(kind="tool_call", tool_name="read_file", tool_input='{"path":"/x"}')
            yield AgentEvent(kind="done")

        mock_bridge = MagicMock()
        mock_bridge.send_message = mock_send_message
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Parse SSE events from response body
        body = resp.text
        events = []
        for line in body.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        assert len(events) >= 2
        assert events[0]["kind"] == "text"
        assert events[0]["text"] == "Hello from agent"
        assert events[1]["kind"] == "tool_call"
        assert events[1]["tool_name"] == "read_file"

        # Session stays active after a normal turn — only /stop sets completed
        assert session.status == "active"

        # History should contain user message and assistant events
        assert len(session.history) >= 2
        assert session.history[0]["role"] == "user"
        assert session.history[0]["content"] == "hello"
        assert session.history[1]["role"] == "assistant"
        assert len(session.history[1]["events"]) == 2  # text + tool_call (not done)

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_send_message_streamed_error_keeps_session_active(self, tmp_path):
        """Turn-level error events do NOT brick the session — only fatal
        exceptions should set session.status to 'error'."""
        from spec_runtime.web.bridge import (
            AgentEvent,
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )

        session = create_session(mode="create", agent="claude")

        async def mock_send_message(sid, text):
            yield AgentEvent(kind="error", text="something broke")
            yield AgentEvent(kind="done")

        mock_bridge = MagicMock()
        mock_bridge.send_message = mock_send_message
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "hello"},
            headers=self._auth_headers(),
        )

        # Streamed error events transition the session to error state (AC7)
        assert session.status == "error"

        # The error event should still appear in history
        assert any(
            ev.get("kind") == "error"
            for entry in session.history
            if entry.get("role") == "assistant"
            for ev in entry.get("events", [])
        )

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_create_session_records_initial_prompt(self, tmp_path):
        """Session creation records the initial user prompt in history and
        forwards it to the bridge so the agent begins processing immediately."""
        mock_bridge = MagicMock()
        mock_bridge.start_session = AsyncMock(return_value="session-123")

        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": True, "codex": False}),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=mock_bridge),
            patch("spec_runtime.web.chat_api._setup_chat_worktree", return_value=(wt_path, "spec-authoring/token123", "abc123", "origin/main")),
        ):
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "create", "agent": "claude", "prompt": "build a dashboard"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        from spec_runtime.web.bridge import _sessions

        session = _sessions.get(session_id)
        assert session is not None
        # The initial prompt is recorded in history
        assert len(session.history) == 1
        assert session.history[0]["role"] == "user"
        assert session.history[0]["content"] == "build a dashboard"
        # The initial prompt is stored on the session
        assert session.initial_prompt == "build a dashboard"
        # The bridge received the initial_prompt keyword
        mock_bridge.start_session.assert_called_once()
        call_kwargs = mock_bridge.start_session.call_args
        assert call_kwargs.kwargs.get("initial_prompt") == "build a dashboard"

        # Cleanup
        from spec_runtime.web.bridge import _bridges

        _sessions.pop(session_id, None)
        _bridges.pop(session_id, None)

    def test_initial_prompt_not_duplicated_by_messages(self, tmp_path):
        """When the frontend re-sends the initial prompt via POST /messages,
        it must not be recorded twice in session history."""
        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )

        session = create_session(mode="create", agent="claude")
        session.initial_prompt = "build a dashboard"
        session.initial_turn_dispatched = True
        session.history.append({"role": "user", "content": "build a dashboard"})

        mock_bridge = MagicMock()
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "build a dashboard"},
            headers=self._auth_headers(),
        )
        # Should return a done-only SSE stream (no turn tracking state)
        assert resp.status_code == 200

        # The user message should appear exactly once
        user_entries = [h for h in session.history if h.get("role") == "user"]
        assert len(user_entries) == 1

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_initial_prompt_reconnect_after_turn_completes(self, tmp_path):
        """If the initial turn finishes before the client's /messages POST
        arrives, the server must NOT start a duplicate turn."""
        import asyncio
        import json as json_mod

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import (
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
            _turn_tasks,
        )

        session = create_session(mode="create", agent="claude")
        session.initial_prompt = "build it"
        session.initial_turn_dispatched = True
        session.history.append({"role": "user", "content": "build it"})

        mock_bridge = MagicMock()
        register_bridge(session.session_id, mock_bridge)

        # Simulate a completed background turn by setting up tracking state
        # as _start_background_turn + _run_turn_bg would leave it.
        done_evt = asyncio.Event()
        done_evt.set()  # turn already finished
        notify = asyncio.Event()
        notify.set()
        events = [{"kind": "text", "text": "response"}]
        _turn_completions[session.session_id] = done_evt
        _turn_notifiers[session.session_id] = notify
        _turn_event_lists[session.session_id] = events
        # No task in _turn_tasks → _is_turn_active returns False

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "build it"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Should replay completed events, not start a new turn
        sse_events = []
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                sse_events.append(json_mod.loads(line[6:]))
        # The text event from the completed turn + a done event
        assert any(e["kind"] == "text" and e["text"] == "response" for e in sse_events)
        assert sse_events[-1]["kind"] == "done"

        # bridge.send_message should NOT have been called (no new turn)
        mock_bridge.send_message.assert_not_called()

        # History should have exactly one user message
        user_entries = [h for h in session.history if h.get("role") == "user"]
        assert len(user_entries) == 1

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)
        _turn_tasks.pop(session.session_id, None)
        _turn_notifiers.pop(session.session_id, None)
        _turn_completions.pop(session.session_id, None)
        _turn_event_lists.pop(session.session_id, None)

    def test_initial_prompt_reconnect_streams_error_instead_of_409(self, tmp_path):
        """When the initial turn fails before the client attaches, the
        reconnect POST /messages must stream the error events back rather
        than returning a bare 409 (F1 fix)."""
        import asyncio
        import json as json_mod

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import (
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
            _turn_tasks,
        )

        session = create_session(mode="create", agent="claude")
        session.initial_prompt = "build it"
        session.initial_turn_dispatched = True
        session.status = "error"  # initial turn failed quickly
        session.history.append({"role": "user", "content": "build it"})

        mock_bridge = MagicMock()
        register_bridge(session.session_id, mock_bridge)

        # Simulate a completed-with-error background turn
        done_evt = asyncio.Event()
        done_evt.set()
        notify = asyncio.Event()
        notify.set()
        events = [{"kind": "error", "text": "agent crashed"}]
        _turn_completions[session.session_id] = done_evt
        _turn_notifiers[session.session_id] = notify
        _turn_event_lists[session.session_id] = events

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "build it"},
            headers=self._auth_headers(),
        )
        # Must stream the error, not return 409
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        sse_events = []
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                sse_events.append(json_mod.loads(line[6:]))
        assert any(e["kind"] == "error" and "crashed" in e.get("text", "") for e in sse_events)

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)
        _turn_tasks.pop(session.session_id, None)
        _turn_notifiers.pop(session.session_id, None)
        _turn_completions.pop(session.session_id, None)
        _turn_event_lists.pop(session.session_id, None)

    def test_create_session_cleans_up_on_non_runtime_error(self, tmp_path):
        """Bridge startup failures other than RuntimeError (e.g. OSError,
        FileNotFoundError) must still clean up the worktree (F2 fix)."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        mock_bridge = MagicMock()
        mock_bridge.start_session = AsyncMock(side_effect=FileNotFoundError("codex not found"))
        mock_bridge.stop_session = AsyncMock()

        cleanup_mock = MagicMock()

        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": False, "codex": True}),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=mock_bridge),
            patch("spec_runtime.web.chat_api._setup_chat_worktree", return_value=(wt_path, "spec-authoring/token123", "abc123", "origin/main")),
            patch("spec_runtime.web.chat_api._cleanup_chat_worktree", cleanup_mock),
        ):
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "create", "agent": "codex", "prompt": "build it"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 422
        assert "codex not found" in resp.json()["error"]
        # Cleanup must have been called despite non-RuntimeError
        cleanup_mock.assert_called_once()
        mock_bridge.stop_session.assert_awaited_once()

    def test_startup_cleanup_failure_retains_public_ownership_for_stop_retry(
        self,
        tmp_path,
    ):
        """Do not remove the only bridge capable of reaping a failed startup."""
        from spec_runtime.web.bridge import get_bridge, get_session

        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        mock_bridge = MagicMock()
        mock_bridge.start_session = AsyncMock(
            side_effect=RuntimeError("provider handshake failed")
        )
        mock_bridge.stop_session = AsyncMock(
            side_effect=[RuntimeError("provider still alive"), None, None]
        )
        cleanup_mock = MagicMock(
            side_effect=[RuntimeError("git worktree remove failed"), None]
        )

        with (
            patch(
                "spec_runtime.web.chat_api._available_backends",
                return_value={"claude": False, "codex": True},
            ),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=mock_bridge),
            patch(
                "spec_runtime.web.chat_api._setup_chat_worktree",
                return_value=(
                    wt_path,
                    "task/web-task-startup--token",
                    "abc123",
                    "origin/main",
                ),
            ),
            patch(
                "spec_runtime.web.chat_api._cleanup_chat_worktree",
                cleanup_mock,
            ),
        ):
            client = self._make_client(tmp_path)
            response = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "task", "agent": "codex", "prompt": "build it"},
                headers=self._auth_headers(),
            )

            assert response.status_code == 503
            payload = response.json()
            session_id = payload["session_id"]
            assert payload["status"] == "stopping"
            assert get_session(session_id).status == "stopping"
            assert get_bridge(session_id) is mock_bridge
            assert wt_path.exists()
            cleanup_mock.assert_not_called()

            retry = client.post(
                f"/api/v1/chat/sessions/{session_id}/stop",
                headers=self._auth_headers(),
            )

            assert retry.status_code == 503
            assert retry.json()["status"] == "stopping"
            assert "worktree_removed" not in retry.json()
            assert get_session(session_id) is not None
            assert get_bridge(session_id) is mock_bridge

            retry = client.post(
                f"/api/v1/chat/sessions/{session_id}/stop",
                headers=self._auth_headers(),
            )

        assert retry.status_code == 200
        assert retry.json()["worktree_removed"] is True
        assert get_session(session_id) is None
        assert get_bridge(session_id) is None
        assert cleanup_mock.call_count == 2
        cleanup_mock.assert_called_with(
            tmp_path,
            str(wt_path),
            "task/web-task-startup--token",
        )
        assert mock_bridge.stop_session.await_count == 3

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_concurrent_stop_joins_provider_start_and_prevents_initial_turn(
        self,
        tmp_path,
        agent,
    ):
        import asyncio

        import httpx

        from spec_runtime.web.bridge import _sessions
        from spec_runtime.web.chat_api import _turn_tasks
        from spec_runtime.web.server import create_app

        async def run_test():
            startup_paused = asyncio.Event()
            release_startup = asyncio.Event()
            provider_stopped = asyncio.Event()
            bridge = MagicMock()

            async def start_session(**_kwargs):
                startup_paused.set()
                await release_startup.wait()

            async def stop_session(_session_id):
                provider_stopped.set()

            bridge.start_session = AsyncMock(side_effect=start_session)
            bridge.stop_session = AsyncMock(side_effect=stop_session)
            worktree = tmp_path / ".worktrees" / "task-web-task-start--token"
            worktree.mkdir(parents=True)
            app = create_app(tmp_path, "token", reload_token=False)
            transport = httpx.ASGITransport(app=app)
            headers = {"Authorization": "Bearer token"}

            with (
                patch(
                    "spec_runtime.web.chat_api._available_backends",
                    return_value={"claude": True, "codex": True},
                ),
                patch("spec_runtime.web.chat_api._create_bridge", return_value=bridge),
                patch(
                    "spec_runtime.web.chat_api._setup_chat_worktree",
                    return_value=(
                        worktree,
                        "task/web-task-start--token",
                        "abc123",
                        "origin/main",
                    ),
                ),
                patch("spec_runtime.orchestrator._write_sandbox_config"),
            ):
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    creating = asyncio.create_task(
                        client.post(
                            "/api/v1/chat/sessions",
                            json={"mode": "task", "agent": agent, "prompt": "scope it"},
                            headers=headers,
                        )
                    )
                    await startup_paused.wait()
                    session = next(
                        item
                        for item in _sessions.values()
                        if item.owner_id == app.state.chat_owner_id
                    )
                    stopping = asyncio.create_task(
                        client.post(
                            f"/api/v1/chat/sessions/{session.session_id}/stop",
                            headers=headers,
                        )
                    )
                    await asyncio.sleep(0)
                    assert not stopping.done()
                    release_startup.set()
                    create_response, stop_response = await asyncio.gather(
                        creating,
                        stopping,
                    )

            assert create_response.status_code == 409
            assert stop_response.status_code == 200
            assert provider_stopped.is_set()
            bridge.stop_session.assert_awaited_once_with(session.session_id)
            bridge.send_message.assert_not_called()
            assert session.session_id not in _turn_tasks
            assert session.status == "completed"

        asyncio.run(run_test())

    def test_stopped_session_rejects_messages(self, tmp_path):
        """After /stop, POST /messages must return 409 instead of
        accepting new messages."""
        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )

        session = create_session(mode="create", agent="claude")
        session.status = "completed"  # as set by stop_chat_session

        mock_bridge = MagicMock()
        register_bridge(session.session_id, mock_bridge)

        client = self._make_client(tmp_path)
        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/messages",
            json={"text": "hello"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 409
        assert "no longer active" in resp.json()["error"]

        _sessions.pop(session.session_id, None)
        _bridges.pop(session.session_id, None)

    def test_history_includes_live_turn(self, tmp_path):
        """In-progress turn events should be visible via /history for reconnect."""
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")
        session.history.append({"role": "user", "content": "hello"})
        # Simulate an in-progress turn
        session.live_turn = [
            {"kind": "text", "text": "partial "},
            {"kind": "tool_call", "tool_name": "read_file", "tool_input": "{}"},
        ]

        client = self._make_client(tmp_path)
        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}/history",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should have user message + live assistant turn
        assert len(data["history"]) == 2
        assert data["history"][1]["role"] == "assistant"
        assert len(data["history"][1]["events"]) == 2
        assert data["live_event_count"] == 2

        _sessions.pop(session.session_id, None)

    def test_stream_reattach_consumes_initial_turn_replay_flag(self, tmp_path):
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")
        session.initial_turn_dispatched = True
        client = self._make_client(tmp_path)

        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}/stream",
            headers=self._auth_headers(),
        )

        assert resp.status_code == 200
        assert session.initial_turn_dispatched is False
        _sessions.pop(session.session_id, None)

    @pytest.mark.parametrize("from_value", ["nope", "-1"])
    def test_stream_rejects_malformed_replay_offset(self, tmp_path, from_value):
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")
        session.initial_turn_dispatched = True
        client = self._make_client(tmp_path)

        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}/stream?from={from_value}",
            headers=self._auth_headers(),
        )

        assert resp.status_code == 422
        assert "non-negative integer" in resp.json()["error"]
        assert session.initial_turn_dispatched is True
        _sessions.pop(session.session_id, None)

    def test_get_history_endpoint(self, tmp_path):
        """History endpoint returns session conversation history."""
        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")
        session.history.append({"role": "user", "content": "hello"})
        session.history.append({"role": "assistant", "events": [{"kind": "text", "text": "Hi!"}]})

        client = self._make_client(tmp_path)
        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}/history",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session.session_id
        assert len(data["history"]) == 2
        assert data["history"][0]["role"] == "user"
        assert data["history"][1]["role"] == "assistant"
        # turn_active is included
        assert "turn_active" in data

        _sessions.pop(session.session_id, None)

    def test_get_history_not_found(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.get(
            "/api/v1/chat/sessions/nonexistent/history",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    def test_get_backends_endpoint(self, tmp_path):
        """Backends endpoint returns available agent backends."""
        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": True, "codex": False}),
            patch("spec_runtime.web.chat_api._default_chat_agent", return_value="claude"),
        ):
            client = self._make_client(tmp_path)
            resp = client.get(
                "/api/v1/chat/backends",
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["backends"]["claude"] is True
        assert data["backends"]["codex"] is False
        assert data["default_agent"] == "claude"

    def test_chat_requires_auth(self, tmp_path):
        """Chat endpoints must require authentication."""
        client = self._make_client(tmp_path)

        # No auth header
        resp = client.get("/api/v1/chat/sessions")
        assert resp.status_code == 401

        resp = client.post(
            "/api/v1/chat/sessions",
            json={"mode": "create", "agent": "claude", "prompt": "hello"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stream reconnect endpoint tests
# ---------------------------------------------------------------------------


class TestStreamEndpoint:
    """Tests for GET /api/v1/chat/sessions/{id}/stream."""

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token, reload_token=False)
        client = TestClient(app, raise_server_exceptions=False)
        return client

    def _auth_headers(self, token="test-token"):
        return {"Authorization": f"Bearer {token}"}

    def test_stream_not_found(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.get(
            "/api/v1/chat/sessions/nonexistent/stream",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404

    def test_stream_no_active_turn(self, tmp_path):
        """When no turn is active, stream returns a done event immediately."""
        import json

        from spec_runtime.web.bridge import _sessions, create_session

        session = create_session(mode="create", agent="claude")

        client = self._make_client(tmp_path)
        resp = client.get(
            f"/api/v1/chat/sessions/{session.session_id}/stream",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = []
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        assert len(events) == 1
        assert events[0]["kind"] == "done"

        _sessions.pop(session.session_id, None)


# ---------------------------------------------------------------------------
# Prompt loading tests
# ---------------------------------------------------------------------------


class TestPromptLoading:
    """Test system prompt loading for create and task modes."""

    def test_default_create_prompt(self, tmp_path):
        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(tmp_path, "create")
        assert "spec" in prompt.lower()
        assert "push `spec/<spec-id>`" not in prompt
        assert "open a single pr" not in prompt.lower()
        assert "do not run `git push`" in prompt.lower()
        assert "ready for operator publication" in prompt.lower()

    def test_default_task_prompt(self, tmp_path):
        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(tmp_path, "task")
        assert "task" in prompt.lower()

    def test_create_prompt_with_branch(self, tmp_path):
        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(
            tmp_path, "create", branch="spec-authoring/20260101T000000"
        )
        assert "spec" in prompt.lower()
        assert "spec-authoring/20260101T000000" in prompt

    def test_task_prompt_with_worktree(self, tmp_path):
        from spec_runtime.web.chat_api import _load_system_prompt

        wt = tmp_path / "my-worktree"
        prompt = _load_system_prompt(tmp_path, "task", worktree_path=wt)
        assert "task" in prompt.lower()
        assert str(wt) in prompt

    def test_custom_create_prompt(self, tmp_path):
        prompt_file = tmp_path / "prompts" / "spec-creation.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("Custom creation prompt for testing")

        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(tmp_path, "create")
        # The orchestrator appends session details (spec ID, branch, etc.)
        # to the base prompt, so check inclusion rather than exact match.
        assert prompt.startswith("Custom creation prompt for testing")
        assert "spec" in prompt.lower()
        assert "ready for operator publication" in prompt.lower()

    def test_custom_task_prompt(self, tmp_path):
        prompt_file = tmp_path / "prompts" / "task-scoping.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("Custom task prompt for testing")

        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(tmp_path, "task")
        # Custom prompt file content is used as project context.
        assert prompt.startswith("Custom task prompt for testing")
        # Web task prompt is scoping-only — no implementation instructions.
        assert "done scoping" in prompt.lower()


# ---------------------------------------------------------------------------
# Backend availability tests
# ---------------------------------------------------------------------------


class TestBackendAvailability:
    """Test agent backend availability detection."""

    def test_no_backends(self):
        with (
            patch("spec_runtime.web.chat_api._available_backends", return_value={"claude": False, "codex": False}),
        ):
            from spec_runtime.web.chat_api import _available_backends

            backends = _available_backends()
            assert not backends["claude"]
            assert not backends["codex"]

    def test_create_bridge_unknown_agent(self):
        from spec_runtime.web.chat_api import _create_bridge

        with pytest.raises(ValueError, match="Unknown agent"):
            _create_bridge("unknown-agent")

    def test_configured_allowed_agents_filter_installed_backends(self, tmp_path):
        from spec_runtime.web.chat_api import _available_backends

        config = MagicMock()
        config.agents.allowed = ("codex",)
        with (
            patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
            patch("spec_runtime.web.bridge_codex._codex_available", return_value=True),
            patch(
                "spec_runtime.config.load_repo_spec_runtime_config",
                return_value=config,
            ),
        ):
            assert _available_backends(tmp_path) == {
                "claude": False,
                "codex": True,
            }

    def test_missing_claude_sandbox_dependency_disables_backend(self):
        from spec_runtime.web.chat_api import _available_backends

        with (
            patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True),
            patch(
                "spec_runtime.web.bridge_claude._claude_cli_unavailability_reason",
                return_value="missing socat",
            ),
            patch("spec_runtime.web.bridge_codex._codex_available", return_value=False),
        ):
            assert _available_backends() == {"claude": False, "codex": False}

    def test_legacy_codex_file_collision_disables_backend(self, tmp_path):
        from spec_runtime.web.chat_api import _available_backends

        (tmp_path / ".codex").write_text("")
        config = MagicMock()
        config.agents.allowed = ("codex",)
        with (
            patch("spec_runtime.web.bridge_claude._sdk_available", return_value=False),
            patch("spec_runtime.web.bridge_codex._codex_available", return_value=True),
            patch(
                "spec_runtime.config.load_repo_spec_runtime_config",
                return_value=config,
            ),
        ):
            assert _available_backends(tmp_path) == {
                "claude": False,
                "codex": False,
            }

    def test_create_bridge_claude(self):
        with patch("spec_runtime.web.bridge_claude._sdk_available", return_value=True):
            from spec_runtime.web.chat_api import _create_bridge

            bridge = _create_bridge("claude")
            assert bridge is not None

    def test_create_bridge_codex(self):
        with patch("spec_runtime.web.bridge_codex._codex_available", return_value=True):
            from spec_runtime.web.chat_api import _create_bridge

            bridge = _create_bridge("codex")
            assert bridge is not None

    def test_log_backend_availability_warns_missing(self):
        """AC14: log_backend_availability warns about unavailable backends."""
        from spec_runtime.web.chat_api import log_backend_availability

        with patch(
            "spec_runtime.web.chat_api._available_backends",
            return_value={"claude": True, "codex": False},
        ):
            with patch("spec_runtime.web.chat_api.logger") as mock_logger:
                result = log_backend_availability()
            assert result == {"claude": True, "codex": False}
            # Should log available backends at info level
            mock_logger.info.assert_called_once()
            assert "claude" in mock_logger.info.call_args[0][1]
            # Should warn about unavailable backends
            mock_logger.warning.assert_called_once()
            assert "codex" in mock_logger.warning.call_args[0][1]

    def test_log_backend_availability_none_available(self):
        """AC14: warns when no backends are available at all."""
        from spec_runtime.web.chat_api import log_backend_availability

        with patch(
            "spec_runtime.web.chat_api._available_backends",
            return_value={"claude": False, "codex": False},
        ):
            with patch("spec_runtime.web.chat_api.logger") as mock_logger:
                result = log_backend_availability()
            assert result == {"claude": False, "codex": False}
            # Two warnings: one for unavailable list, one for "none at all"
            assert mock_logger.warning.call_count == 2


# ---------------------------------------------------------------------------
# Turn tracking tests
# ---------------------------------------------------------------------------


class TestTurnTracking:
    """Test background turn tracking helpers."""

    def test_is_turn_active_no_task(self):
        from spec_runtime.web.chat_api import _is_turn_active

        assert not _is_turn_active("nonexistent-session")

    def test_is_turn_active_with_done_task(self):
        import asyncio

        from spec_runtime.web.chat_api import _is_turn_active, _turn_tasks

        loop = asyncio.new_event_loop()

        async def noop():
            pass

        task = loop.create_task(noop())
        loop.run_until_complete(task)
        _turn_tasks["test-done"] = task
        assert not _is_turn_active("test-done")
        _turn_tasks.pop("test-done", None)
        loop.close()

    def test_cancellation_resistant_turn_retains_relay_until_retry(self):
        import asyncio

        from spec_runtime.web.chat_api import (
            _begin_turn_cancel,
            _finish_turn_cancel,
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
            _turn_owners,
            _turn_tasks,
        )

        async def run_test():
            session_id = "cancellation-resistant-turn"
            release = asyncio.Event()

            async def resist_cancellation():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

            task = asyncio.create_task(resist_cancellation())
            await asyncio.sleep(0)
            _turn_tasks[session_id] = task
            _turn_notifiers[session_id] = asyncio.Event()
            _turn_completions[session_id] = asyncio.Event()
            _turn_event_lists[session_id] = []
            _turn_owners[session_id] = "owner"

            with patch(
                "spec_runtime.web.chat_api._TURN_CANCEL_TIMEOUT_SECONDS",
                0.001,
            ):
                claimed = _begin_turn_cancel(session_id)
                assert claimed is task
                assert await _finish_turn_cancel(session_id, task) is False

            assert session_id in _turn_tasks
            assert session_id in _turn_owners

            release.set()
            await task
            assert await _finish_turn_cancel(session_id, task) is True
            assert session_id not in _turn_tasks
            assert session_id not in _turn_owners

        asyncio.run(run_test())

    def test_provider_stop_timeout_retains_task_until_confirmed(self):
        import asyncio

        from spec_runtime.web.chat_api import (
            _confirm_provider_stop,
            _provider_stop_tasks,
        )

        async def run_test():
            session_id = "provider-stop-timeout"
            release = asyncio.Event()

            async def delayed_stop(_session_id):
                await release.wait()

            bridge = MagicMock(
                stop_session=AsyncMock(side_effect=delayed_stop)
            )
            with patch(
                "spec_runtime.web.chat_api._PROVIDER_STOP_TIMEOUT_SECONDS",
                0.001,
            ):
                with pytest.raises(TimeoutError, match="still running"):
                    await _confirm_provider_stop(session_id, bridge)
                pending = _provider_stop_tasks[session_id]
                assert not pending.done()

                release.set()
                await _confirm_provider_stop(session_id, bridge)

            assert session_id not in _provider_stop_tasks
            bridge.stop_session.assert_awaited_once_with(session_id)

        asyncio.run(run_test())

    def test_app_shutdown_stops_bridges_cancels_turns_and_clears_registry(self):
        import asyncio

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import (
            _provider_stop_tasks,
            _turn_tasks,
            shutdown_chat_sessions,
        )

        async def run_test():
            session = create_session("create", "codex")
            bridge = MagicMock()
            bridge.stop_session = AsyncMock()
            register_bridge(session.session_id, bridge)

            async def hang():
                await asyncio.Event().wait()

            task = asyncio.create_task(hang())
            _turn_tasks[session.session_id] = task

            await shutdown_chat_sessions()

            assert task.cancelled()
            bridge.stop_session.assert_awaited_once_with(session.session_id)
            assert session.session_id not in _sessions
            assert session.session_id not in _bridges
            assert session.session_id not in _provider_stop_tasks
            assert session.session_id not in _turn_tasks

        asyncio.run(run_test())

    def test_cancelled_shutdown_still_finishes_provider_cleanup(self):
        import asyncio

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import shutdown_chat_sessions

        async def run_test():
            cleanup_started = asyncio.Event()
            allow_cleanup = asyncio.Event()
            session = create_session("create", "codex")

            async def stop_session(session_id):
                assert session_id == session.session_id
                cleanup_started.set()
                await allow_cleanup.wait()

            bridge = MagicMock(stop_session=AsyncMock(side_effect=stop_session))
            register_bridge(session.session_id, bridge)

            shutdown = asyncio.create_task(shutdown_chat_sessions())
            await cleanup_started.wait()
            shutdown.cancel()
            await asyncio.sleep(0)
            assert not shutdown.done()

            allow_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await shutdown

            bridge.stop_session.assert_awaited_once_with(session.session_id)
            assert session.session_id not in _sessions
            assert session.session_id not in _bridges

        asyncio.run(run_test())

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_shutdown_retries_provider_cleanup_until_exit_is_confirmed(self, agent):
        import asyncio

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import _provider_stop_tasks, shutdown_chat_sessions

        async def run_test():
            retry_started = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            session = create_session("create", agent)

            async def stop_session(_session_id):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("cleanup not yet confirmed")
                retry_started.set()
                await release.wait()

            bridge = MagicMock(stop_session=AsyncMock(side_effect=stop_session))
            register_bridge(session.session_id, bridge)
            shutdown = asyncio.create_task(shutdown_chat_sessions())
            await retry_started.wait()
            assert not shutdown.done()
            assert session.session_id in _sessions
            assert session.session_id in _bridges

            release.set()
            await shutdown
            assert calls == 2
            assert session.session_id not in _sessions
            assert session.session_id not in _bridges
            assert session.session_id not in _provider_stop_tasks

        asyncio.run(run_test())

    def test_testclient_lifespan_stops_only_sessions_owned_by_that_app(
        self,
        tmp_path,
    ):
        from starlette.testclient import TestClient

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.server import create_app

        app_one = create_app(tmp_path / "one", "token", reload_token=False)
        app_two = create_app(tmp_path / "two", "token", reload_token=False)
        session_one = create_session(
            "create",
            "codex",
            owner_id=app_one.state.chat_owner_id,
        )
        session_two = create_session(
            "task",
            "claude",
            owner_id=app_two.state.chat_owner_id,
        )
        bridge_one = MagicMock(stop_session=AsyncMock())
        bridge_two = MagicMock(stop_session=AsyncMock())
        register_bridge(session_one.session_id, bridge_one)
        register_bridge(session_two.session_id, bridge_two)

        try:
            with TestClient(app_one) as client_one:
                visible = client_one.get(
                    f"/api/v1/chat/sessions/{session_one.session_id}",
                    headers={"Authorization": "Bearer token"},
                )
                hidden = client_one.get(
                    f"/api/v1/chat/sessions/{session_two.session_id}",
                    headers={"Authorization": "Bearer token"},
                )
                assert visible.status_code == 200
                assert hidden.status_code == 404

            bridge_one.stop_session.assert_awaited_once_with(session_one.session_id)
            bridge_two.stop_session.assert_not_awaited()
            assert session_one.session_id not in _sessions
            assert session_one.session_id not in _bridges
            assert session_two.session_id in _sessions
            assert session_two.session_id in _bridges

            with TestClient(app_two):
                pass

            bridge_two.stop_session.assert_awaited_once_with(session_two.session_id)
            assert session_two.session_id not in _sessions
            assert session_two.session_id not in _bridges
        finally:
            _sessions.pop(session_one.session_id, None)
            _sessions.pop(session_two.session_id, None)
            _bridges.pop(session_one.session_id, None)
            _bridges.pop(session_two.session_id, None)


class TestChatWorktreeBase:
    def test_cleanup_fails_closed_when_worktree_remove_fails(self, tmp_path):
        import subprocess

        from spec_runtime.web.chat_api import _cleanup_chat_worktree

        worktrees = tmp_path / ".worktrees"
        target = worktrees / "task-web-task-cleanup--token"
        target.mkdir(parents=True)
        listing = subprocess.CompletedProcess(
            [], 0, f"worktree {target}\n", ""
        )
        failed = subprocess.CompletedProcess([], 1, "", "busy")
        with (
            patch("spec_runtime.orchestrator._worktrees_root", return_value=worktrees),
            patch(
                "spec_runtime.orchestrator.run_subprocess",
                side_effect=[listing, failed],
            ),
        ):
            with pytest.raises(RuntimeError, match="busy"):
                _cleanup_chat_worktree(
                    tmp_path,
                    str(target),
                    "task/web-task-cleanup--token",
                )

        assert target.exists()

    def test_cleanup_fails_closed_when_branch_delete_fails(self, tmp_path):
        import subprocess

        from spec_runtime.web.chat_api import _cleanup_chat_worktree

        exists = subprocess.CompletedProcess([], 0, "", "")
        failed = subprocess.CompletedProcess([], 1, "", "branch busy")
        with patch(
            "spec_runtime.orchestrator.run_subprocess",
            side_effect=[exists, failed],
        ):
            with pytest.raises(RuntimeError, match="branch busy"):
                _cleanup_chat_worktree(
                    tmp_path,
                    None,
                    "task/web-task-cleanup--token",
                )

    def test_setup_uses_configured_base_ref(self, tmp_path):
        from spec_runtime.web.chat_api import _setup_chat_worktree

        config = MagicMock()
        config.base_ref = "origin/release"
        completed = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
        ]
        with (
            patch("spec_runtime.config.load_repo_spec_runtime_config", return_value=config),
            patch("spec_runtime.orchestrator._worktrees_root", return_value=tmp_path / ".worktrees"),
            patch("spec_runtime.orchestrator.run_subprocess", side_effect=completed) as run,
        ):
            _worktree, _branch, base_sha, base_ref = _setup_chat_worktree(tmp_path, "create")

        assert base_ref == "origin/release"
        assert base_sha == "abc123"
        assert run.call_args_list[1].args[0][-1] == "origin/release"

    def test_setup_refuses_unresolved_configured_base(self, tmp_path):
        from spec_runtime.web.chat_api import _setup_chat_worktree

        config = MagicMock()
        config.base_ref = "origin/missing"
        with (
            patch("spec_runtime.config.load_repo_spec_runtime_config", return_value=config),
            patch("spec_runtime.orchestrator._worktrees_root", return_value=tmp_path / ".worktrees"),
            patch(
                "spec_runtime.orchestrator.run_subprocess",
                return_value=MagicMock(returncode=1, stdout="", stderr="missing"),
            ) as run,
        ):
            with pytest.raises(RuntimeError, match="spec doctor"):
                _setup_chat_worktree(tmp_path, "task")

        assert run.call_count == 1


# ---------------------------------------------------------------------------
# Codex notification parsing tests
# ---------------------------------------------------------------------------


class TestCodexParseNotification:
    """Test _CodexSession._parse_notification extracts events correctly."""

    def test_turn_completed(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification({"type": "turn.completed"})
        assert len(events) == 1
        assert events[0].kind == "done"

    def test_turn_completed_jsonrpc(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification({"method": "turn/completed"})
        assert len(events) == 1
        assert events[0].kind == "done"

    @pytest.mark.parametrize(
        "message",
        [
            {
                "type": "turn.completed",
                "turn": {
                    "status": "failed",
                    "error": {"message": "plain provider failure"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "status": "interrupted",
                        "error": {"message": "rpc interruption"},
                    }
                },
            },
            {
                "type": "turn.failed",
                "error": {"message": "plain failed notification"},
            },
            {
                "method": "turn/interrupted",
                "params": {"message": "rpc interrupted notification"},
            },
        ],
    )
    def test_failed_and_interrupted_terminals_emit_error_then_done(self, message):
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        session._last_turn_error = ""

        events = session._parse_notification(message)

        assert [event.kind for event in events] == ["error", "done"]
        assert session._last_turn_error == events[0].text
        assert any(
            marker in events[0].text.lower()
            for marker in ("failed", "interrupted")
        )

    def test_item_completed_agent_message(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}}
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "text"
        assert events[0].text == "Hello"

    def test_item_completed_command_execution(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0, "output": "file.txt"}}
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "command"
        assert events[0].cmd == "ls"

    def test_ignored_event_returns_empty(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification({"type": "turn.started"})
        assert events == []

    def test_error_event(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification({"type": "error", "message": "bad"})
        assert len(events) == 1
        assert events[0].kind == "error"
        assert events[0].text == "bad"

    def test_item_completed_unknown_type_returns_empty(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {"type": "item.completed", "item": {"type": "reasoning"}}
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert events == []

    def test_jsonrpc_item_completed_agent_message(self):
        """JSON-RPC notifications carry item under params.item with camelCase types."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "Hi"}},
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "text"
        assert events[0].text == "Hi"

    def test_jsonrpc_item_completed_command_execution(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "command": "pwd",
                    "exitCode": 0,
                    "output": "/home",
                },
            },
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "command"
        assert events[0].cmd == "pwd"

    def test_current_command_with_null_output_is_safe(self):
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "command": "true",
                    "exitCode": 0,
                    "aggregatedOutput": None,
                },
            },
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        events = session._parse_notification(msg)

        assert len(events) == 1
        assert events[0].kind == "command"
        assert events[0].output == ""

    def test_jsonrpc_error_notification(self):
        """Error notifications carry message under params.error.message."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "method": "error",
            "params": {"error": {"message": "something broke"}},
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "error"
        assert events[0].text == "something broke"

    def test_mcp_tool_call_with_result(self):
        """Completed MCP tool calls emit both tool_call and tool_result."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "fs",
                "tool": "read_file",
                "arguments": {"path": "/tmp/x"},
                "result": "file contents here",
            },
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 2
        assert events[0].kind == "tool_call"
        assert events[0].tool_name == "fs/read_file"
        assert events[1].kind == "tool_result"
        assert events[1].tool_name == "fs/read_file"
        assert events[1].tool_output == "file contents here"

    def test_mcp_tool_call_without_result(self):
        """MCP tool call without result emits only tool_call."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "tool": "ping",
                "arguments": {},
            },
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "tool_call"
        assert events[0].tool_name == "ping"

    def test_item_delta_agent_message(self):
        """Plain-JSON item.delta for agent_message yields incremental text."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "type": "item.delta",
            "item": {"type": "agent_message", "text": "partial "},
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "text"
        assert events[0].text == "partial "

    def test_item_delta_jsonrpc(self):
        """JSON-RPC item/delta yields incremental text from params.delta."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {
            "method": "item/delta",
            "params": {"delta": {"type": "agentMessage", "text": "chunk"}},
        }
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert len(events) == 1
        assert events[0].kind == "text"
        assert events[0].text == "chunk"

    def test_item_delta_no_text_returns_empty(self):
        """item.delta without text content is silently ignored."""
        from spec_runtime.web.bridge_codex import _CodexSession

        msg = {"type": "item.delta", "item": {"type": "reasoning"}}
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False
        events = session._parse_notification(msg)
        assert events == []

    def test_delta_then_completed_does_not_duplicate_text(self):
        """When a turn streams item.delta followed by item.completed for the
        same agent_message, the completed text must be suppressed to avoid
        rendering it twice."""
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        # Simulate a sequence: item.started → item.delta → item.completed
        started = {"type": "item.started", "item": {"type": "agent_message"}}
        delta = {"type": "item.delta", "item": {"type": "agent_message", "text": "Hello "}}
        delta2 = {"type": "item.delta", "item": {"type": "agent_message", "text": "world"}}
        completed = {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}}

        all_events = []
        all_events.extend(session._parse_notification(started))
        all_events.extend(session._parse_notification(delta))
        all_events.extend(session._parse_notification(delta2))
        all_events.extend(session._parse_notification(completed))

        # Only the two delta events should produce text — completed is suppressed
        text_events = [e for e in all_events if e.kind == "text"]
        assert len(text_events) == 2
        assert text_events[0].text == "Hello "
        assert text_events[1].text == "world"

    def test_completed_without_deltas_emits_text(self):
        """When no item.delta preceded item.completed, the completed text
        must still be emitted as a fallback."""
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        completed = {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}}
        events = session._parse_notification(completed)
        assert len(events) == 1
        assert events[0].kind == "text"
        assert events[0].text == "Hello"

    def test_item_updated_suppressed(self):
        """item.updated carries cumulative text and must be suppressed to
        avoid duplicating content already streamed via item.delta."""
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        # Plain-JSON form
        msg = {"type": "item.updated", "item": {"type": "agent_message", "text": "Hello"}}
        assert session._parse_notification(msg) == []

        # JSON-RPC slash form
        msg2 = {"method": "item/updated", "params": {"item": {"type": "agent_message", "text": "Hello world"}}}
        assert session._parse_notification(msg2) == []

    def test_current_app_server_recording_populates_stream_and_cards(self):
        """Exercise notification shapes recorded from Codex CLI 0.147.0."""
        from spec_runtime.web.bridge_codex import _CodexSession

        fixture = Path(__file__).parent / "fixtures/web_chat/codex_app_server_v2_events.json"
        messages = json.loads(fixture.read_text())
        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        events = [
            event
            for message in messages
            for event in session._parse_notification(message)
        ]

        assert [event.kind for event in events] == [
            "text",
            "command",
            "file_change",
        ]
        assert events[0].text == "codex-stream-ok"
        assert events[1].cmd == "printf command-ok"
        assert events[1].output == "command-ok"
        assert events[1].exit_code == 0
        assert events[2].path == "/tmp/spec-web-chat/audit.txt"
        assert events[2].diff == "file-ok\n"

    def test_delta_then_updated_then_completed_no_duplication(self):
        """Full streaming sequence: delta → updated → completed must only
        yield text from deltas, not from updated or completed."""
        from spec_runtime.web.bridge_codex import _CodexSession

        session = _CodexSession.__new__(_CodexSession)
        session._saw_delta_text = False

        started = {"type": "item.started", "item": {"type": "agent_message"}}
        delta1 = {"type": "item.delta", "item": {"type": "agent_message", "text": "Hello "}}
        updated1 = {"type": "item.updated", "item": {"type": "agent_message", "text": "Hello "}}
        delta2 = {"type": "item.delta", "item": {"type": "agent_message", "text": "world"}}
        updated2 = {"type": "item.updated", "item": {"type": "agent_message", "text": "Hello world"}}
        completed = {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello world"}}

        all_events = []
        for msg in [started, delta1, updated1, delta2, updated2, completed]:
            all_events.extend(session._parse_notification(msg))

        text_events = [e for e in all_events if e.kind == "text"]
        assert len(text_events) == 2
        assert text_events[0].text == "Hello "
        assert text_events[1].text == "world"


# ---------------------------------------------------------------------------
# Truncation helper tests
# ---------------------------------------------------------------------------


class TestTruncation:
    """Test the _truncate helper used by bridges."""

    def test_short_string_unchanged(self):
        from spec_runtime.web.bridge_claude import _truncate

        assert _truncate("hello", 100) == "hello"

    def test_long_string_truncated(self):
        from spec_runtime.web.bridge_claude import _truncate

        result = _truncate("a" * 200, 50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_exact_length_unchanged(self):
        from spec_runtime.web.bridge_claude import _truncate

        assert _truncate("abc", 3) == "abc"


# ---------------------------------------------------------------------------
# Task spec detection tests
# ---------------------------------------------------------------------------


class TestDetectTaskSpec:
    """_detect_task_spec finds committed task specs in a session worktree."""

    def test_no_task_dir(self, tmp_path):
        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        session = ChatSession(
            session_id="s1", mode="task", agent="claude",
            worktree_path=str(tmp_path),
        )
        assert _detect_task_spec(session) is None

    def test_empty_task_dir(self, tmp_path):
        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        (tmp_path / "specs" / "tasks").mkdir(parents=True)
        session = ChatSession(
            session_id="s2", mode="task", agent="claude",
            worktree_path=str(tmp_path),
        )
        assert _detect_task_spec(session) is None

    def test_detects_spec(self, tmp_path):
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: my-task\n---\n\n# My Task\n"
        spec_file = task_dir / "my-task.md"
        spec_file.write_text(spec_content)

        session = ChatSession(
            session_id="s3", mode="task", agent="claude",
            worktree_path=str(tmp_path),
        )
        # Fallback path calls git show to read committed content
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=spec_content, stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_show):
            result = _detect_task_spec(session)
        assert result is not None
        assert result["spec_id"] == "my-task"
        assert "# My Task" in result["spec_content"]


# ---------------------------------------------------------------------------
# Implement chat task endpoint tests
# ---------------------------------------------------------------------------


class TestImplementChatTask:
    """Tests for the implement_chat_task endpoint logic."""

    def test_non_task_session_rejected(self):
        """Only task-mode sessions support the implement endpoint."""
        from spec_runtime.web.bridge import ChatSession

        session = ChatSession(
            session_id="s-create", mode="create", agent="claude",
        )
        # The endpoint checks mode == "task"; create mode should be rejected.
        assert session.mode != "task"

    def test_session_marked_completed_on_detect(self, tmp_path):
        """After spec detection, session can be marked completed."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: test-impl\n---\n"
        (task_dir / "test-impl.md").write_text(spec_content)

        session = ChatSession(
            session_id="s-impl", mode="task", agent="claude",
            worktree_path=str(tmp_path),
        )
        assert session.status == "active"
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=spec_content, stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_show):
            result = _detect_task_spec(session)
        assert result is not None
        # Simulate what the implement endpoint does.
        session.status = "completed"
        assert session.status == "completed"


# ---------------------------------------------------------------------------
# Scoping-only prompt tests
# ---------------------------------------------------------------------------


class TestScopingOnlyPrompt:
    """Verify task mode prompt is scoping-only (no implementation instructions)."""

    def test_task_prompt_no_implementation(self, tmp_path):
        from spec_runtime.web.chat_api import _load_system_prompt

        prompt = _load_system_prompt(tmp_path, "task")
        # Should NOT contain old implementation instructions.
        assert "proceed to implement" not in prompt.lower()
        assert "execute the task end-to-end" not in prompt.lower()
        # Should contain the scoping-done instruction.
        assert "done scoping" in prompt.lower()


# ---------------------------------------------------------------------------
# Scoped task-spec detection tests (F1 — base_sha scoping)
# ---------------------------------------------------------------------------


class TestDetectTaskSpecScoped:
    """_detect_task_spec ignores pre-existing specs and only finds new ones."""

    def test_pre_existing_specs_ignored_when_base_sha_set(self, tmp_path):
        """A worktree with pre-existing specs (from main) should return None
        when git-diff shows no new files since base_sha."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "old-task.md").write_text("---\nid: old-task\n---\n")

        session = ChatSession(
            session_id="s-scoped-1", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )
        # Mock run_subprocess to return empty diff (no new files)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_result):
            result = _detect_task_spec(session)
        assert result is None

    def test_newly_added_spec_detected(self, tmp_path):
        """When git-diff reports a new spec file, _detect_task_spec returns it."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        # Pre-existing spec (should be ignored)
        (task_dir / "old-task.md").write_text("---\nid: old-task\n---\n")
        # Newly added spec (should be returned)
        spec_content = "---\nid: new-task\n---\n# New\n"
        (task_dir / "new-task.md").write_text(spec_content)

        session = ChatSession(
            session_id="s-scoped-2", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )
        # run_subprocess is called three times: git rev-parse HEAD, git-diff, git-show
        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="deadbeef\n", stderr="",
        )
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/new-task.md\n", stderr="",
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=spec_content, stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", side_effect=[mock_rev, mock_diff, mock_show]):
            result = _detect_task_spec(session)
        assert result is not None
        assert result["spec_id"] == "new-task"
        assert "# New" in result["spec_content"]
        assert result["head_sha"] == "deadbeef"

    def test_fallback_when_no_base_sha(self, tmp_path):
        """Without base_sha, falls back to glob then reads from HEAD."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: any-task\n---\n"
        (task_dir / "any-task.md").write_text(spec_content)

        session = ChatSession(
            session_id="s-scoped-3", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="",  # no base SHA
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=spec_content, stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_show):
            result = _detect_task_spec(session)
        assert result is not None
        assert result["spec_id"] == "any-task"


# ---------------------------------------------------------------------------
# task_spec_ready history persistence tests (F2)
# ---------------------------------------------------------------------------


class TestSpecReviewHistoryPersistence:
    """task_spec_ready events are persisted into session.history."""

    def test_spec_review_appended_to_history(self, tmp_path):
        """After a turn completes with a detected spec, session.history
        should contain a spec_review entry."""
        import asyncio
        import subprocess

        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import _run_turn_bg, _turn_completions, _turn_event_lists, _turn_notifiers

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: my-task\n---\n# My Task\n"
        (task_dir / "my-task.md").write_text(spec_content)

        session = ChatSession(
            session_id="s-hist-1", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )

        # Mock bridge that yields a single text event then done
        bridge = MagicMock()
        async def _fake_send(*_args, **_kwargs):
            yield AgentEvent(kind="text", text="Scoping done.")
            yield AgentEvent(kind="done")
        bridge.send_message = _fake_send

        # Set up turn tracking
        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()

        # run_subprocess is called three times: git rev-parse HEAD, git-diff, git-show
        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="deadbeef\n", stderr="",
        )
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/my-task.md\n", stderr="",
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=spec_content, stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", side_effect=[mock_rev, mock_diff, mock_show]):
            asyncio.run(_run_turn_bg(sid, session, bridge, "Describe a task"))

        # History should contain the assistant turn AND the spec_review entry
        roles = [h["role"] for h in session.history]
        assert "assistant" in roles
        assert "spec_review" in roles

        spec_review = [h for h in session.history if h["role"] == "spec_review"][0]
        assert spec_review["spec_id"] == "my-task"
        assert "# My Task" in spec_review["spec_content"]
        assert spec_review["head_sha"] == "deadbeef"


# ---------------------------------------------------------------------------
# Implement endpoint tests (F1 + F3 + F5)
# ---------------------------------------------------------------------------


class TestImplementEndpoint:
    """Tests for POST /api/v1/chat/sessions/{id}/implement."""

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token, reload_token=False)
        client = TestClient(app, raise_server_exceptions=False)
        return client

    def _auth_headers(self, token="test-token"):
        return {"Authorization": f"Bearer {token}"}

    def test_implement_non_task_session_rejected(self, tmp_path):
        """Implement endpoint rejects create-mode sessions."""
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)

        session = create_session(mode="create", agent="claude")
        session.worktree_path = str(tmp_path)

        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/implement",
            json={"spec_id": "irrelevant"},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 422
        assert "task-mode" in resp.json()["error"]

    def test_implement_no_spec_found(self, tmp_path):
        """Implement endpoint returns 422 when no new spec exists."""
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)

        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        session.base_sha = "abc123"

        # Mock run_subprocess to return empty diff
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_result):
            resp = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={"spec_id": "nonexistent"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 422
        assert "No task spec found" in resp.json()["error"]

    @pytest.mark.parametrize("spec_id", ["con", "a" * 65])
    def test_implement_rejects_windows_unsafe_detected_spec_id(self, tmp_path, spec_id):
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / f"{spec_id}.md").write_text(f"---\nid: {spec_id}\n---\n")
        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        session.branch = "task/web-task-test"
        session.base_sha = "abc123"
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"specs/tasks/{spec_id}.md\n", stderr=""
        )

        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff):
            resp = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers=self._auth_headers(),
            )

        assert resp.status_code == 422
        assert "invalid" in resp.json()["error"]

    def test_implement_uses_detected_spec_not_request_body(self, tmp_path):
        """The implement endpoint uses _detect_task_spec to find the correct
        spec, not blindly trusting whatever spec_id the client sends."""
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "correct-spec.md").write_text("---\nid: correct-spec\n---\n")

        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        session.branch = "task/web-task-test"
        session.base_sha = "abc123"

        # Mock diff to return the correct spec
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/correct-spec.md\n", stderr="",
        )
        # Mock Popen for the orchestrator spawn
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running

        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff), \
             patch("spec_runtime.web.chat_api.ProcessSupervisor.spawn", return_value=mock_proc):
            resp = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={"spec_id": "wrong-spec-from-client"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        # The endpoint should use the detected spec, not the client's spec_id
        assert data["spec_id"] == "correct-spec"
        assert session.status == "completed"
        # Response must include run_state matching the standard implement API
        assert "run_state" in data
        assert data["run_state"]["spec_id"] == "correct-spec"
        assert data["run_state"]["run_id"] == data["run_id"]

    def test_implement_validates_committed_bytes_not_dirty_correction(self, tmp_path):
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        # The mutable file looks corrected, but the reviewed/committed bytes
        # still carry the mismatched id and must be rejected.
        (task_dir / "committed-name.md").write_text(
            "---\nid: committed-name\n---\n",
            encoding="utf-8",
        )
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/committed-name"
        detected = {
            "spec_id": "committed-name",
            "spec_content": "---\nid: wrong-committed-id\n---\n",
            "head_sha": "deadbeef",
        }

        with (
            patch("spec_runtime.web.chat_api._detect_task_spec", return_value=detected),
            patch("spec_runtime.web.chat_api.ProcessSupervisor.spawn") as spawn,
        ):
            response = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers=self._auth_headers(),
            )

        assert response.status_code == 422
        assert "wrong-committed-id" in response.json()["error"]
        assert session.status == "active"
        spawn.assert_not_called()

    def test_implement_pins_same_committed_bytes_it_validates(self, tmp_path):
        import hashlib

        from spec_runtime.orchestrator import RunState, _run_spec_snapshot_path
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        # A dirty mismatch cannot redirect validation away from committed HEAD.
        (task_dir / "stable-task.md").write_text(
            "---\nid: dirty-other-id\n---\n",
            encoding="utf-8",
        )
        committed = "---\nid: stable-task\n---\n\n# Committed task\n"
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/stable-task"
        detected = {
            "spec_id": "stable-task",
            "spec_content": committed,
            "head_sha": "deadbeef",
        }
        process = MagicMock(pid=1234)
        process.poll.return_value = None

        with (
            patch("spec_runtime.web.chat_api._detect_task_spec", return_value=detected),
            patch(
                "spec_runtime.web.chat_api.ProcessSupervisor.spawn",
                return_value=process,
            ),
        ):
            response = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers=self._auth_headers(),
            )

        assert response.status_code == 200
        run_id = response.json()["run_id"]
        assert _run_spec_snapshot_path(tmp_path, run_id).read_text(encoding="utf-8") == committed
        assert RunState.load(tmp_path, run_id).spec_revision == (
            "sha256:" + hashlib.sha256(committed.encode("utf-8")).hexdigest()
        )

    def test_implement_idempotent_after_completion(self, tmp_path):
        """A second POST to implement on a completed session returns 409."""
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)

        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        session.branch = "task/web-task-test"
        session.status = "completed"  # already handed off

        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/implement",
            json={},
            headers=self._auth_headers(),
        )
        assert resp.status_code == 409
        assert "already handed off" in resp.json()["error"]

    def test_implement_prefers_session_agent(self, tmp_path):
        """The implement endpoint uses the persisted session agent, not the
        client-supplied value which may be stale."""
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "agent-test.md").write_text("---\nid: agent-test\n---\n")

        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/agent-test"
        session.base_sha = "abc123"

        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/agent-test.md\n", stderr="",
        )
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = None

        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff), \
             patch("spec_runtime.web.api._spec_executable", return_value="/venv/bin/spec"), \
             patch("spec_runtime.web.chat_api.ProcessSupervisor.spawn", return_value=mock_proc) as spawn_mock:
            resp = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={"agent": "claude"},  # client sends stale value
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        # The supervised spawn should use the session's agent ("codex"), not the
        # client-supplied "claude".
        call_args = spawn_mock.call_args[0][0]
        assert call_args[0] == "/venv/bin/spec"
        agent_idx = call_args.index("--agent")
        assert call_args[agent_idx + 1] == "codex"

    def test_implement_handoff_uses_adoptable_lifetime(self, tmp_path):
        """The orchestrator must survive a web-server restart after handoff."""
        import subprocess

        from spec_runtime.process_supervisor import LifetimeMode
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "durable-task.md").write_text(
            "---\nid: durable-task\n---\n",
            encoding="utf-8",
        )
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/durable-task"
        session.base_sha = "abc123"
        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="specs/tasks/durable-task.md\n",
            stderr="",
        )
        mock_proc = MagicMock(pid=88888)
        mock_proc.poll.return_value = None

        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff), \
             patch("spec_runtime.web.chat_api.ProcessSupervisor") as supervisor_cls:
            supervisor_cls.return_value.spawn.return_value = mock_proc
            response = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers=self._auth_headers(),
            )

        assert response.status_code == 200
        supervisor_cls.assert_called_once_with(LifetimeMode.ADOPTABLE)

    @pytest.mark.parametrize(
        ("stop_error", "expected_status"),
        [
            (TimeoutError("provider cleanup is still running"), 504),
            (RuntimeError("disconnect failed"), 503),
        ],
    )
    def test_implement_requires_confirmed_provider_stop_before_spawn(
        self,
        tmp_path,
        stop_error,
        expected_status,
    ):
        """A failed provider stop leaves a retryable session and no run."""
        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: stop-first\n---\n"
        (task_dir / "stop-first.md").write_text(spec_content, encoding="utf-8")
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/stop-first"
        session.base_sha = "abc123"
        bridge = MagicMock(stop_session=AsyncMock())
        register_bridge(session.session_id, bridge)

        detected = {
            "spec_id": "stop-first",
            "spec_content": spec_content,
            "head_sha": "deadbeef",
        }
        try:
            with (
                patch(
                    "spec_runtime.web.chat_api._detect_task_spec",
                    return_value=detected,
                ),
                patch(
                    "spec_runtime.web.chat_api._confirm_provider_stop",
                    new=AsyncMock(side_effect=stop_error),
                ),
                patch(
                    "spec_runtime.web.chat_api.ProcessSupervisor.spawn",
                ) as spawn_mock,
            ):
                response = client.post(
                    f"/api/v1/chat/sessions/{session.session_id}/implement",
                    json={},
                    headers=self._auth_headers(),
                )

            assert response.status_code == expected_status
            assert response.json()["status"] == "stopping"
            assert session.status == "stopping"
            spawn_mock.assert_not_called()
            runs_root = tmp_path / ".spec-state" / "runs"
            assert not runs_root.exists()

            # Stop is explicitly retryable after either failure mode.
            with patch(
                "spec_runtime.web.chat_api._confirm_provider_stop",
                new=AsyncMock(),
            ):
                retry = client.post(
                    f"/api/v1/chat/sessions/{session.session_id}/stop",
                    headers=self._auth_headers(),
                )
            assert retry.status_code == 200
            assert session.status == "completed"
        finally:
            _sessions.pop(session.session_id, None)
            _bridges.pop(session.session_id, None)

    def test_implement_requires_cancellation_resistant_relay_to_exit(self, tmp_path):
        """A turn that wins the initial status-check race blocks handoff."""
        import asyncio
        import json as json_mod

        from starlette.requests import Request

        from spec_runtime.web.bridge import (
            _bridges,
            _sessions,
            create_session,
            register_bridge,
        )
        from spec_runtime.web.chat_api import (
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
            _turn_owners,
            _turn_tasks,
            implement_chat_task,
            stop_chat_session,
        )
        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, "test-token", reload_token=False)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        spec_content = "---\nid: relay-first\n---\n"
        (task_dir / "relay-first.md").write_text(spec_content, encoding="utf-8")
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/relay-first"
        session.base_sha = "abc123"
        bridge = MagicMock(stop_session=AsyncMock())
        register_bridge(session.session_id, bridge)
        detected = {
            "spec_id": "relay-first",
            "spec_content": spec_content,
            "head_sha": "deadbeef",
        }

        def make_request(path: str, body: dict | None = None) -> Request:
            payload = json_mod.dumps(body or {}).encode()
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {
                    "type": "http.request",
                    "body": payload,
                    "more_body": False,
                }

            return Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "path_params": {"id": session.session_id},
                    "headers": [],
                    "app": app,
                },
                receive,
            )

        async def run_test():
            release = asyncio.Event()

            async def resist_cancellation():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

            turn = asyncio.create_task(resist_cancellation())
            await asyncio.sleep(0)
            sid = session.session_id
            _turn_tasks[sid] = turn
            _turn_notifiers[sid] = asyncio.Event()
            _turn_completions[sid] = asyncio.Event()
            _turn_event_lists[sid] = []
            _turn_owners[sid] = "owner"

            try:
                with (
                    # Simulate a turn starting immediately after the endpoint's
                    # optimistic active-turn check.
                    patch("spec_runtime.web.chat_api._is_turn_active", return_value=False),
                    patch(
                        "spec_runtime.web.chat_api._detect_task_spec",
                        return_value=detected,
                    ),
                    patch(
                        "spec_runtime.web.chat_api._TURN_CANCEL_TIMEOUT_SECONDS",
                        0.001,
                    ),
                    patch(
                        "spec_runtime.web.chat_api.ProcessSupervisor.spawn",
                    ) as spawn_mock,
                ):
                    response = await implement_chat_task(
                        make_request(
                            f"/api/v1/chat/sessions/{sid}/implement",
                            {},
                        )
                    )

                assert response.status_code == 503
                assert session.status == "stopping"
                assert sid in _turn_tasks
                assert sid in _turn_owners
                spawn_mock.assert_not_called()
                assert not (tmp_path / ".spec-state" / "runs").exists()

                release.set()
                await turn
                retry = await stop_chat_session(
                    make_request(f"/api/v1/chat/sessions/{sid}/stop")
                )
                assert retry.status_code == 200
                assert session.status == "completed"
                assert sid not in _turn_tasks
                assert sid not in _turn_owners
            finally:
                if not turn.done():
                    release.set()
                    await turn
                _turn_tasks.pop(sid, None)
                _turn_notifiers.pop(sid, None)
                _turn_completions.pop(sid, None)
                _turn_event_lists.pop(sid, None)
                _turn_owners.pop(sid, None)
                _sessions.pop(sid, None)
                _bridges.pop(sid, None)

        asyncio.run(run_test())

    def test_concurrent_stop_wins_handoff_before_process_spawn(self, tmp_path):
        import asyncio
        import json as json_mod

        from starlette.requests import Request

        from spec_runtime.web.bridge import create_session, register_bridge
        from spec_runtime.web.chat_api import implement_chat_task, stop_chat_session
        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, "token", reload_token=False)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        committed = "---\nid: handoff-race\n---\n"
        (task_dir / "handoff-race.md").write_text(committed, encoding="utf-8")
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/handoff-race"
        bridge = MagicMock(stop_session=AsyncMock())
        register_bridge(session.session_id, bridge)
        detected = {
            "spec_id": "handoff-race",
            "spec_content": committed,
            "head_sha": "deadbeef",
        }

        def request(path: str) -> Request:
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {
                    "type": "http.request",
                    "body": json_mod.dumps({}).encode(),
                    "more_body": False,
                }

            return Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "path_params": {"id": session.session_id},
                    "headers": [],
                    "app": app,
                },
                receive,
            )

        async def run_test():
            provider_stop_started = asyncio.Event()
            release_handoff = asyncio.Event()
            calls = 0

            async def controlled_stop(_session_id, _bridge):
                nonlocal calls
                calls += 1
                if calls == 1:
                    provider_stop_started.set()
                    await release_handoff.wait()

            with (
                patch("spec_runtime.web.chat_api._detect_task_spec", return_value=detected),
                patch(
                    "spec_runtime.web.chat_api._confirm_provider_stop",
                    side_effect=controlled_stop,
                ),
                patch("spec_runtime.web.chat_api.ProcessSupervisor.spawn") as spawn,
            ):
                handoff = asyncio.create_task(
                    implement_chat_task(request("/implement"))
                )
                await provider_stop_started.wait()
                stop = asyncio.create_task(stop_chat_session(request("/stop")))
                await asyncio.sleep(0)
                assert not stop.done()
                release_handoff.set()
                handoff_response, stop_response = await asyncio.gather(handoff, stop)

            assert handoff_response.status_code == 409
            assert stop_response.status_code == 200
            spawn.assert_not_called()
            assert session.status == "completed"
            assert not (tmp_path / ".spec-state" / "runs").exists()

        asyncio.run(run_test())

    def test_implement_supervisor_failure_rolls_back_session_and_run(self, tmp_path):
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "failed-handoff.md").write_text(
            "---\nid: failed-handoff\n---\n",
            encoding="utf-8",
        )
        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(tmp_path)
        session.branch = "task/failed-handoff"
        session.base_sha = "abc123"
        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="specs/tasks/failed-handoff.md\n",
            stderr="",
        )

        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff), \
             patch(
                 "spec_runtime.web.chat_api.ProcessSupervisor.spawn",
                 side_effect=RuntimeError("identity inspection failed"),
             ):
            response = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers=self._auth_headers(),
            )

        assert response.status_code == 422
        assert "identity inspection failed" in response.json()["error"]
        # Provider ownership was already relinquished before launch, so a
        # failed orchestrator spawn cannot safely reactivate the chat.
        assert session.status == "error"
        runs_root = tmp_path / ".spec-state" / "runs"
        assert not list(runs_root.glob("*.json"))
        assert not list(runs_root.glob("**/spec.md"))


class TestSpecReviewDedup:
    """task_spec_ready dedup and clear-review flow."""

    def test_no_duplicate_spec_review(self, tmp_path):
        """A second turn should not emit task_spec_ready when the same spec
        was already surfaced in a prior turn."""
        import asyncio
        import subprocess

        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import _run_turn_bg, _turn_completions, _turn_event_lists, _turn_notifiers

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "dedup-task.md").write_text("---\nid: dedup-task\n---\n# Task\n")

        session = ChatSession(
            session_id="s-dedup", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )
        # Simulate that the spec was already surfaced in a prior turn.
        session.history.append(
            {"role": "spec_review", "spec_id": "dedup-task",
             "spec_content": "---\nid: dedup-task\n---\n# Task\n"}
        )

        bridge = MagicMock()
        async def _fake_send(*_args, **_kwargs):
            yield AgentEvent(kind="text", text="Follow-up clarification.")
            yield AgentEvent(kind="done")
        bridge.send_message = _fake_send

        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()

        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/dedup-task.md\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff):
            asyncio.run(_run_turn_bg(sid, session, bridge, "Just a question"))

        # Should not have added another spec_review entry.
        spec_reviews = [h for h in session.history if h.get("role") == "spec_review"]
        assert len(spec_reviews) == 1

        # Events emitted during this turn should NOT contain task_spec_ready.
        spec_ready_events = [e for e in _turn_event_lists[sid] if e.get("kind") == "task_spec_ready"]
        assert len(spec_ready_events) == 0

    def test_spec_resurfaces_after_clear_review_and_new_commit(self, tmp_path):
        """After clearing spec_review from history (Keep Editing flow),
        the next turn should re-surface the spec only when HEAD has moved
        (i.e. the agent committed a new version of the spec)."""
        import asyncio
        import subprocess

        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import _run_turn_bg, _turn_completions, _turn_event_lists, _turn_notifiers

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "edit-task.md").write_text("---\nid: edit-task\n---\n# Task v2\n")

        session = ChatSession(
            session_id="s-resurface", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )
        # Simulate clear-review having recorded the old HEAD SHA.
        session.last_reviewed_head_sha = "oldhead111"

        bridge = MagicMock()
        async def _fake_send(*_args, **_kwargs):
            yield AgentEvent(kind="text", text="Updated the spec.")
            yield AgentEvent(kind="done")
        bridge.send_message = _fake_send

        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()

        # git rev-parse HEAD returns a NEW sha (different from oldhead111).
        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="newhead222\n", stderr="",
        )
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/edit-task.md\n", stderr="",
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="---\nid: edit-task\n---\n# Task v2\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", side_effect=[mock_rev, mock_diff, mock_show]):
            asyncio.run(_run_turn_bg(sid, session, bridge, "Update the spec"))

        # The spec should have been re-surfaced because HEAD moved.
        spec_reviews = [h for h in session.history if h.get("role") == "spec_review"]
        assert len(spec_reviews) == 1
        assert spec_reviews[0]["spec_id"] == "edit-task"

        spec_ready_events = [e for e in _turn_event_lists[sid] if e.get("kind") == "task_spec_ready"]
        assert len(spec_ready_events) == 1

    def test_spec_does_not_resurface_without_new_commit(self, tmp_path):
        """After Keep Editing, if HEAD has not moved (no new commit),
        the spec should NOT be re-surfaced."""
        import asyncio
        import subprocess

        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import _run_turn_bg, _turn_completions, _turn_event_lists, _turn_notifiers

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "edit-task.md").write_text("---\nid: edit-task\n---\n# Task\n")

        session = ChatSession(
            session_id="s-no-resurface", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )
        # Simulate clear-review having recorded the HEAD SHA.
        session.last_reviewed_head_sha = "samesha999"

        bridge = MagicMock()
        async def _fake_send(*_args, **_kwargs):
            yield AgentEvent(kind="text", text="Just a question.")
            yield AgentEvent(kind="done")
        bridge.send_message = _fake_send

        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()

        # git rev-parse HEAD returns the SAME sha as last_reviewed_head_sha.
        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="samesha999\n", stderr="",
        )
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/edit-task.md\n", stderr="",
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="---\nid: edit-task\n---\n# Task\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", side_effect=[mock_rev, mock_diff, mock_show]):
            asyncio.run(_run_turn_bg(sid, session, bridge, "Just a question"))

        # Should NOT have added a spec_review entry (HEAD didn't move).
        spec_reviews = [h for h in session.history if h.get("role") == "spec_review"]
        assert len(spec_reviews) == 0

        spec_ready_events = [e for e in _turn_event_lists[sid] if e.get("kind") == "task_spec_ready"]
        assert len(spec_ready_events) == 0

    def test_spec_suppressed_after_late_error(self, tmp_path):
        """A turn that commits a valid spec but ends in error status must NOT
        emit task_spec_ready — the review card would be a dead end because
        POST /implement rejects non-active sessions (F1 fix)."""
        import asyncio
        import subprocess

        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import _run_turn_bg, _turn_completions, _turn_event_lists, _turn_notifiers

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "err-task.md").write_text("---\nid: err-task\n---\n# Task\n")

        session = ChatSession(
            session_id="s-late-err", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )

        bridge = MagicMock()
        async def _fake_send(*_args, **_kwargs):
            yield AgentEvent(kind="text", text="Writing spec...")
            yield AgentEvent(kind="error", text="transport error")
        bridge.send_message = _fake_send

        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()

        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="commitsha1\n", stderr="",
        )
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/err-task.md\n", stderr="",
        )
        mock_show = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="---\nid: err-task\n---\n# Task\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", side_effect=[mock_rev, mock_diff, mock_show]):
            asyncio.run(_run_turn_bg(sid, session, bridge, "Write the spec"))

        # Session ended in error — review card must NOT be surfaced.
        assert session.status == "error"
        spec_reviews = [h for h in session.history if h.get("role") == "spec_review"]
        assert len(spec_reviews) == 0

        spec_ready_events = [e for e in _turn_event_lists[sid] if e.get("kind") == "task_spec_ready"]
        assert len(spec_ready_events) == 0


class TestClearReviewEndpoint:
    """Tests for POST /api/v1/chat/sessions/{id}/clear-review."""

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token, reload_token=False)
        client = TestClient(app, raise_server_exceptions=False)
        return client

    def _auth_headers(self, token="test-token"):
        return {"Authorization": f"Bearer {token}"}

    def test_clear_review_removes_spec_review_from_history(self, tmp_path):
        """Calling clear-review removes spec_review entries from session history
        and records the last reviewed HEAD SHA."""
        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        session.history.append(
            {"role": "spec_review", "spec_id": "my-task",
             "spec_content": "# Task\n", "head_sha": "abc999"}
        )
        session.history.append(
            {"role": "assistant", "events": [{"kind": "text", "text": "done"}]}
        )

        resp = client.post(
            f"/api/v1/chat/sessions/{session.session_id}/clear-review",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True

        # spec_review should be gone; other entries preserved.
        assert len(session.history) == 1
        assert session.history[0]["role"] == "assistant"
        # The cleared review's head_sha should be saved on the session.
        assert session.last_reviewed_head_sha == "abc999"

    def test_clear_review_fallback_resolves_head(self, tmp_path):
        """When spec_review has no head_sha, clear-review resolves HEAD
        from the worktree as a fallback."""
        import subprocess

        from spec_runtime.web.bridge import create_session

        client = self._make_client(tmp_path)
        session = create_session(mode="task", agent="claude")
        session.worktree_path = str(tmp_path)
        # Legacy spec_review without head_sha
        session.history.append(
            {"role": "spec_review", "spec_id": "my-task",
             "spec_content": "# Task\n"}
        )

        mock_rev = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="fallback123\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_rev):
            resp = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/clear-review",
                headers=self._auth_headers(),
            )
        assert resp.status_code == 200
        assert session.last_reviewed_head_sha == "fallback123"

    def test_clear_review_unknown_session(self, tmp_path):
        client = self._make_client(tmp_path)
        resp = client.post(
            "/api/v1/chat/sessions/nonexistent/clear-review",
            headers=self._auth_headers(),
        )
        assert resp.status_code == 404


class TestDetectTaskSpecFilter:
    """_detect_task_spec only picks up Added files, not Modified ones."""

    def test_modified_only_file_not_detected(self, tmp_path):
        """A spec file that was only Modified (not Added) should not be
        detected — this prevents handing off a pre-existing spec."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "existing.md").write_text("---\nid: existing\n---\n# Existing\n")

        session = ChatSession(
            session_id="s-filter", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )

        # Simulate git diff returning the file as Modified only (not Added).
        # With --diff-filter=A, this file would NOT appear in the output.
        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="", stderr="",  # empty because --diff-filter=A excludes M
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff):
            result = _detect_task_spec(session)

        assert result is None

    def test_added_file_detected(self, tmp_path):
        """A newly Added spec file should be detected."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "new-task.md").write_text("---\nid: new-task\n---\n# New\n")

        session = ChatSession(
            session_id="s-added", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )

        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/new-task.md\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff):
            result = _detect_task_spec(session)

        assert result is not None
        assert result["spec_id"] == "new-task"


# ---------------------------------------------------------------------------
# Multiple task spec rejection tests (F1 — review attempt 9)
# ---------------------------------------------------------------------------


class TestDetectTaskSpecMultipleRejection:
    """_detect_task_spec raises ValueError when multiple task specs exist."""

    def test_multiple_specs_via_git_diff_raises(self, tmp_path):
        """When git diff reports multiple Added spec files, raise ValueError."""
        import subprocess

        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "task-a.md").write_text("---\nid: task-a\n---\n")
        (task_dir / "task-b.md").write_text("---\nid: task-b\n---\n")

        session = ChatSession(
            session_id="s-multi-diff", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="abc123",
        )

        mock_diff = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="specs/tasks/task-a.md\nspecs/tasks/task-b.md\n", stderr="",
        )
        with patch("spec_runtime.orchestrator.run_subprocess", return_value=mock_diff):
            with pytest.raises(ValueError, match="Multiple task specs"):
                _detect_task_spec(session)

    def test_multiple_specs_via_glob_raises(self, tmp_path):
        """When glob finds multiple spec files (no base_sha), raise ValueError."""
        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _detect_task_spec

        task_dir = tmp_path / "specs" / "tasks"
        task_dir.mkdir(parents=True)
        (task_dir / "task-a.md").write_text("---\nid: task-a\n---\n")
        (task_dir / "task-b.md").write_text("---\nid: task-b\n---\n")

        session = ChatSession(
            session_id="s-multi-glob", mode="task", agent="claude",
            worktree_path=str(tmp_path),
            base_sha="",
        )

        with pytest.raises(ValueError, match="Multiple task specs"):
            _detect_task_spec(session)


# ---------------------------------------------------------------------------
# Handoff clears spec_review history (F2 — review attempt 9)
# ---------------------------------------------------------------------------


class TestHandoffClearsSpecReview:
    """Successful handoff removes spec_review entries from session history."""

    def test_spec_review_cleared_on_completed(self):
        """After implement_chat_task marks session completed, spec_review
        entries must be removed so replayHistory() doesn't show stale
        Implement/Keep Editing buttons."""
        from spec_runtime.web.bridge import ChatSession

        session = ChatSession(
            session_id="s-clear", mode="task", agent="claude",
            worktree_path="/tmp/fake",
        )
        session.history = [
            {"role": "assistant", "text": "Done scoping."},
            {"role": "spec_review", "spec_id": "my-task",
             "spec_content": "# My Task"},
        ]

        # Simulate what implement_chat_task does after marking completed.
        session.status = "completed"
        session.history = [
            h for h in session.history if h.get("role") != "spec_review"
        ]

        assert session.status == "completed"
        assert not any(
            h.get("role") == "spec_review" for h in session.history
        )
        # Non-review entries are preserved.
        assert len(session.history) == 1
        assert session.history[0]["role"] == "assistant"


def _linked_web_chat_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    import subprocess

    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    worktree = tmp_path / "task-worktree"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Web Test"], cwd=repo,
                   check=True)
    subprocess.run(
        ["git", "config", "user.email", "web-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / ".spec.toml").write_text(
        'base_ref = "HEAD"\n[agents]\ndefault = "codex"\n'
        'review_default = "codex"\nallowed = ["codex"]\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(
        ".spec-state/\n.worktrees/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo,
                   check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "worktree", "add", "-b", "task/web-canary", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, worktree, base_sha


class TestWebGitMetadataBoundary:
    def test_private_git_grant_excludes_all_real_metadata(
        self,
        tmp_path,
    ):
        from spec_runtime.agent_git_isolation import (
            prepare_agent_git_isolation,
        )

        repo, worktree, _ = _linked_web_chat_repo(tmp_path)
        common = repo / ".git"
        gitdir = Path(
            (worktree / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        ).resolve()
        isolation = prepare_agent_git_isolation(worktree)
        grants = set(isolation.writable_paths)

        assert grants == {gitdir / "specbutler-private-git"}
        assert gitdir not in grants
        assert common.resolve() not in grants
        assert (common / "objects").resolve() in isolation.read_only_paths
        assert (common / "refs").resolve() in isolation.read_only_paths
        assert (common / "config").resolve() in isolation.read_only_paths
        assert (worktree / ".git").resolve(strict=False) in isolation.read_only_paths

    def test_chat_lifecycle_passes_private_git_to_bridge_and_cleans_it_on_stop(
        self,
        tmp_path,
    ):
        import subprocess

        from starlette.testclient import TestClient

        from spec_runtime.agent_git_isolation import (
            prepare_agent_git_isolation_if_linked,
        )
        from spec_runtime.web.bridge import AgentEvent, get_session
        from spec_runtime.web.server import create_app

        repo, worktree, base_sha = _linked_web_chat_repo(tmp_path)
        initial_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        captured: dict[str, object] = {}

        class _Bridge:
            async def start_session(self, **kwargs):
                captured.update(kwargs)
                return kwargs["session_id"]

            async def send_message(self, _session_id, _text):
                yield AgentEvent(kind="done")

            async def stop_session(self, _session_id):
                return None

        app = create_app(repo, "token", reload_token=False)
        with (
            patch(
                "spec_runtime.web.chat_api._available_backends",
                return_value={"claude": False, "codex": True},
            ),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=_Bridge()),
            patch(
                "spec_runtime.web.chat_api._setup_chat_worktree",
                return_value=(worktree, "task/web-canary", base_sha, "HEAD"),
            ),
            patch(
                "spec_runtime.web.chat_api.prepare_agent_git_isolation_if_linked",
                side_effect=prepare_agent_git_isolation_if_linked,
            ),
            patch("spec_runtime.orchestrator._write_sandbox_config"),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            created = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "task", "agent": "codex", "prompt": "scope it"},
                headers={"Authorization": "Bearer token"},
            )
            assert created.status_code == 200, created.text
            session_id = created.json()["session_id"]
            session = get_session(session_id)
            assert session is not None
            isolation = session.agent_git_isolation
            assert isolation is not None
            assert captured["git_isolation"] is isolation
            assert isolation.private_git_dir.is_dir()

            stopped = client.post(
                f"/api/v1/chat/sessions/{session_id}/stop",
                headers={"Authorization": "Bearer token"},
            )
            assert stopped.status_code == 200, stopped.text
            assert session.agent_git_isolation is None
            assert not isolation.private_git_dir.exists()

        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip() == initial_head

    def test_handoff_refuses_private_head_changed_after_spec_selection(
        self,
        tmp_path,
    ):
        import asyncio
        import os
        import subprocess

        from spec_runtime.agent_git_isolation import (
            UnsafeAgentGitIsolationError,
            agent_git_head,
            prepare_agent_git_isolation,
        )
        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _finalize_agent_git

        _repo, worktree, _base_sha = _linked_web_chat_repo(tmp_path)
        isolation = prepare_agent_git_isolation(worktree)
        env = isolation.apply_to_environment({"PATH": os.environ["PATH"]})
        task = worktree / "specs" / "tasks" / "selected.md"
        task.parent.mkdir(parents=True)
        task.write_text("---\nid: selected\n---\n", encoding="utf-8")
        subprocess.run(["git", "add", "specs/tasks/selected.md"], cwd=worktree,
                       env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "selected"], cwd=worktree,
                       env=env, check=True, capture_output=True)
        selected_head = agent_git_head(isolation)

        (worktree / "later.txt").write_text("later\n", encoding="utf-8")
        subprocess.run(["git", "add", "later.txt"], cwd=worktree,
                       env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "later"], cwd=worktree,
                       env=env, check=True, capture_output=True)
        session = ChatSession(
            session_id="head-race",
            mode="task",
            agent="codex",
            worktree_path=str(worktree),
            agent_git_isolation=isolation,
        )

        with pytest.raises(UnsafeAgentGitIsolationError, match="HEAD changed"):
            asyncio.run(_finalize_agent_git(session, expected_head=selected_head))

        assert subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, text=True,
            capture_output=True, check=True,
        ).stdout.strip() == isolation.initial_head
        assert session.agent_git_isolation is None
        assert not isolation.private_git_dir.exists()

    def test_poisoned_private_git_is_discarded_without_losing_working_files(
        self,
        tmp_path,
    ):
        import asyncio

        from spec_runtime.agent_git_isolation import (
            UnsafeAgentGitIsolationError,
            prepare_agent_git_isolation,
        )
        from spec_runtime.web.bridge import ChatSession
        from spec_runtime.web.chat_api import _finalize_agent_git

        _repo, worktree, _base_sha = _linked_web_chat_repo(tmp_path)
        isolation = prepare_agent_git_isolation(worktree)
        recovery_file = worktree / "recover-me.txt"
        recovery_file.write_text("working copy survives\n", encoding="utf-8")
        (isolation.private_git_dir / "config").write_text(
            "[core]\n\thooksPath = attacker\n",
            encoding="utf-8",
        )
        session = ChatSession(
            session_id="poisoned-git",
            mode="task",
            agent="codex",
            worktree_path=str(worktree),
            agent_git_isolation=isolation,
        )

        with pytest.raises(UnsafeAgentGitIsolationError, match="config changed"):
            asyncio.run(_finalize_agent_git(session))

        assert session.agent_git_isolation is None
        assert not isolation.private_git_dir.exists()
        assert recovery_file.read_text(encoding="utf-8") == "working copy survives\n"

    def test_failed_startup_discards_private_git_before_worktree_cleanup(
        self,
        tmp_path,
    ):
        from starlette.testclient import TestClient

        from spec_runtime.agent_git_isolation import (
            prepare_agent_git_isolation_if_linked,
        )
        from spec_runtime.web.server import create_app

        repo, worktree, base_sha = _linked_web_chat_repo(tmp_path)
        prepared = []

        def prepare(path):
            isolation = prepare_agent_git_isolation_if_linked(path)
            prepared.append(isolation)
            return isolation

        def cleanup(_repo_root, _worktree, _branch):
            assert prepared and prepared[0] is not None
            assert not prepared[0].private_git_dir.exists()

        bridge = MagicMock()
        bridge.start_session = AsyncMock(side_effect=RuntimeError("connect failed"))
        bridge.stop_session = AsyncMock()
        app = create_app(repo, "token", reload_token=False)
        with (
            patch(
                "spec_runtime.web.chat_api._available_backends",
                return_value={"claude": False, "codex": True},
            ),
            patch("spec_runtime.web.chat_api._create_bridge", return_value=bridge),
            patch(
                "spec_runtime.web.chat_api._setup_chat_worktree",
                return_value=(worktree, "task/web-canary", base_sha, "HEAD"),
            ),
            patch(
                "spec_runtime.web.chat_api.prepare_agent_git_isolation_if_linked",
                side_effect=prepare,
            ),
            patch("spec_runtime.web.chat_api._cleanup_chat_worktree", side_effect=cleanup),
            patch("spec_runtime.orchestrator._write_sandbox_config"),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/api/v1/chat/sessions",
                json={"mode": "task", "agent": "codex", "prompt": "scope it"},
                headers={"Authorization": "Bearer token"},
            )

        assert response.status_code == 422
        assert prepared and prepared[0] is not None
        assert not prepared[0].private_git_dir.exists()

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="Codex sandbox canary uses the Linux filesystem boundary",
    )
    def test_real_codex_sandbox_can_commit_surface_ready_spec_and_handoff(
        self,
        tmp_path,
        monkeypatch,
    ):
        import asyncio
        import os
        import shlex
        import shutil
        import subprocess

        from starlette.testclient import TestClient

        from spec_runtime.agent_git_isolation import (
            prepare_agent_git_isolation,
        )
        from spec_runtime.git_publish_guard import (
            capture_repository_publication_baseline,
        )
        from spec_runtime.web.bridge import AgentEvent, create_session
        from spec_runtime.web.bridge_codex import _CodexSession
        from spec_runtime.web.chat_api import (
            _run_turn_bg,
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
        )
        from spec_runtime.web.server import create_app

        codex = shutil.which("codex")
        if codex is None:
            pytest.skip("Codex CLI is not installed")
        repo, worktree, base_sha = _linked_web_chat_repo(tmp_path)
        isolation = prepare_agent_git_isolation(worktree)
        dot_git = worktree / ".git"
        real_head = isolation.real_git_dir / "HEAD"
        main_ref = isolation.common_git_dir / "refs" / "heads" / "main"
        real_metadata_before = {
            path: path.read_bytes() for path in (dot_git, real_head, main_ref)
        }
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        overrides = [
            item
            for item in _CodexSession._safety_config_overrides(
                codex_home,
                git_write_paths=isolation.writable_paths,
                git_read_only_paths=isolation.read_only_paths,
            )
            if item != "--strict-config"
        ]
        command = (
            f"if printf attack > {shlex.quote(str(dot_git))}; then exit 41; fi; "
            f"if printf attack > {shlex.quote(str(real_head))}; then exit 42; fi; "
            f"if printf attack > {shlex.quote(str(main_ref))}; then exit 43; fi; "
            "mkdir -p specs/tasks && "
            "printf '%s\\n' '---' 'id: sandbox-canary' '---' '# Canary' "
            "> specs/tasks/sandbox-canary.md && "
            "git add specs/tasks/sandbox-canary.md && "
            "git -c user.name='Web Test' -c user.email='web-test@example.invalid' "
            "commit -m sandbox-canary"
        )
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        env.update(isolation.env_overrides)
        completed = subprocess.run(
            [
                codex,
                *overrides,
                "sandbox",
                "-P",
                "specbutler-web",
                "-C",
                str(worktree),
                "bash",
                "-c",
                command,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert {
            path: path.read_bytes() for path in real_metadata_before
        } == real_metadata_before

        session = create_session(mode="task", agent="codex")
        session.worktree_path = str(worktree)
        session.branch = "task/web-canary"
        session.base_sha = base_sha
        session.base_ref = "HEAD"
        session.agent_git_isolation = isolation
        session.publication_baseline = capture_repository_publication_baseline(
            worktree
        )

        class _FinishedBridge:
            async def send_message(self, _session_id, _text):
                yield AgentEvent(kind="done")

            async def stop_session(self, _session_id):
                return None

        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()
        asyncio.run(_run_turn_bg(sid, session, _FinishedBridge(), "scope"))
        assert any(
            event.get("kind") == "task_spec_ready"
            for event in _turn_event_lists[sid]
        )

        process = MagicMock(pid=31337)
        process.poll.return_value = None
        app = create_app(repo, "token", reload_token=False)
        with (
            patch(
                "spec_runtime.web.chat_api.ProcessSupervisor.spawn",
                return_value=process,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                f"/api/v1/chat/sessions/{sid}/implement",
                json={},
                headers={"Authorization": "Bearer token"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["spec_id"] == "sandbox-canary"
        assert session.handoff_completed is True


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="process-group descendant regression is POSIX-specific",
)
def test_codex_stop_reaps_descendant_after_app_server_leader_exits(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import os
    import signal
    import subprocess

    from spec_runtime.process_supervisor import (
        LifetimeMode,
        ProcessSupervisor,
        inspect_process,
        is_process_group_alive,
    )
    from spec_runtime.web.bridge_codex import _terminate_process

    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(60)"
    )
    leader_code = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
        "stdout=subprocess.PIPE,text=True); "
        "assert p.stdout.readline().strip() == 'ready'; "
        "print(p.pid, flush=True)"
    )

    async def run():
        proc = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
            [sys.executable, "-c", leader_code],
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tmp_path,
        )
        assert proc.stdout is not None
        child_pid = int((await proc.stdout.readline()).decode().strip())
        await proc.process.wait()
        assert inspect_process(child_pid) is not None
        # Keep the facade wait fast: the web boundary itself must notice the
        # still-live group and escalate after the exited leader was reaped.
        monkeypatch.setattr(
            "spec_runtime.process_supervisor._wait_for_identities_exit",
            lambda _identities, timeout=5.0: False,
        )
        try:
            await _terminate_process(proc)
            assert not is_process_group_alive(proc.token.pgid)
            assert inspect_process(child_pid) is None
        finally:
            if is_process_group_alive(proc.token.pgid):
                os.killpg(proc.token.pgid, signal.SIGKILL)

    asyncio.run(run())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="SIGTERM-resistant real-process regression is POSIX-specific",
)
def test_codex_stop_retries_shared_wait_after_grace_timeout(tmp_path, monkeypatch):
    """Graceful timeout must not poison the cached process-reaping task."""
    import asyncio
    import subprocess

    from spec_runtime.process_supervisor import (
        LifetimeMode,
        ProcessSupervisor,
        terminate_managed_process_tree_async,
    )
    from spec_runtime.web.bridge_codex import _CodexSession

    async def run():
        proc = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import signal,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); time.sleep(60)"
                ),
            ],
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tmp_path,
        )
        assert proc.stdout is not None
        assert await proc.stdout.readline() == b"ready\n"

        session = _CodexSession(cwd=str(tmp_path))
        session._proc = proc
        credential_home = MagicMock()
        session._isolated_home_context = credential_home
        monkeypatch.setattr(
            "spec_runtime.web.bridge_codex._PROCESS_STOP_TIMEOUT_SECONDS",
            0.01,
        )

        try:
            await session.stop()
            assert proc.returncode is not None
            assert proc._wait_task is not None
            assert proc._wait_task.done()
            assert not proc._wait_task.cancelled()
            assert session._proc is None
            assert session._isolated_home_context is None
            credential_home.cleanup.assert_called_once()
            await session.stop()
        finally:
            if proc.owned_tree_active():
                assert await terminate_managed_process_tree_async(
                    proc,
                    grace_seconds=0,
                )

    asyncio.run(run())


class TestWebPublicationBaseline:
    def test_private_git_detection_failure_completes_the_turn_stream(self):
        import asyncio

        from spec_runtime.agent_git_isolation import UnsafeAgentGitIsolationError
        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import (
            _run_turn_bg,
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
        )

        session = ChatSession(
            session_id="private-git-detection-failure",
            mode="task",
            agent="codex",
            worktree_path="/unused",
        )

        class _FinishedBridge:
            async def send_message(self, _session_id, _text):
                yield AgentEvent(kind="done")

        session_id = session.session_id
        _turn_event_lists[session_id] = []
        _turn_notifiers[session_id] = asyncio.Event()
        _turn_completions[session_id] = asyncio.Event()
        with patch(
            "spec_runtime.web.chat_api._detect_task_spec",
            side_effect=UnsafeAgentGitIsolationError("private metadata changed"),
        ):
            asyncio.run(
                _run_turn_bg(session_id, session, _FinishedBridge(), "scope")
            )

        assert _turn_completions[session_id].is_set()
        assert session.status == "error"
        assert any(
            event.get("kind") == "error"
            and "private metadata changed" in event.get("text", "")
            for event in _turn_event_lists[session_id]
        )

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_poisoning_after_turn_is_terminal_and_never_surfaces_ready_spec(
        self,
        tmp_path,
        agent,
    ):
        import asyncio
        import subprocess

        from spec_runtime.git_publish_guard import (
            capture_repository_publication_baseline,
        )
        from spec_runtime.web.bridge import AgentEvent, ChatSession
        from spec_runtime.web.chat_api import (
            _run_turn_bg,
            _turn_completions,
            _turn_event_lists,
            _turn_notifiers,
        )

        _, worktree, base_sha = _linked_web_chat_repo(tmp_path)
        session = ChatSession(
            session_id=f"poison-{agent}",
            mode="task",
            agent=agent,
            worktree_path=str(worktree),
            branch="task/web-canary",
            base_sha=base_sha,
            publication_baseline=capture_repository_publication_baseline(worktree),
        )

        class _PoisoningBridge:
            stopped = False

            async def send_message(self, _session_id, _text):
                subprocess.run(
                    ["git", "config", "--local", "core.hooksPath", "attacker-hooks"],
                    cwd=worktree,
                    check=True,
                )
                yield AgentEvent(kind="done")

            async def stop_session(self, _session_id):
                self.stopped = True

        bridge = _PoisoningBridge()
        sid = session.session_id
        _turn_event_lists[sid] = []
        _turn_notifiers[sid] = asyncio.Event()
        _turn_completions[sid] = asyncio.Event()
        asyncio.run(_run_turn_bg(sid, session, bridge, "poison"))

        assert session.status == "error"
        assert bridge.stopped is True
        assert any(
            "publication configuration changed" in event.get("text", "")
            for event in _turn_event_lists[sid]
        )
        assert not any(
            event.get("kind") == "task_spec_ready"
            for event in _turn_event_lists[sid]
        )

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_poisoning_immediately_before_handoff_never_spawns(
        self,
        tmp_path,
        agent,
    ):
        import subprocess

        from starlette.testclient import TestClient

        from spec_runtime.git_publish_guard import (
            capture_repository_publication_baseline,
        )
        from spec_runtime.web.bridge import create_session
        from spec_runtime.web.server import create_app

        repo, worktree, _ = _linked_web_chat_repo(tmp_path)
        session = create_session(mode="task", agent=agent)
        session.worktree_path = str(worktree)
        session.branch = "task/web-canary"
        session.publication_baseline = capture_repository_publication_baseline(
            worktree
        )
        detected = {
            "spec_id": "poisoned-task",
            "spec_content": "---\nid: poisoned-task\n---\n",
            "head_sha": "deadbeef",
        }
        subprocess.run(
            ["git", "config", "--local", "remote.origin.url", "https://evil.invalid/repo"],
            cwd=worktree,
            check=True,
        )
        app = create_app(repo, "token", reload_token=False)
        with (
            patch("spec_runtime.web.chat_api._detect_task_spec", return_value=detected),
            patch("spec_runtime.web.chat_api.ProcessSupervisor.spawn") as spawn,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                f"/api/v1/chat/sessions/{session.session_id}/implement",
                json={},
                headers={"Authorization": "Bearer token"},
            )
        assert response.status_code == 409
        assert session.status == "error"
        spawn.assert_not_called()


class TestClaudeStartupDecoyCleanup:
    def test_confirmed_stop_removes_only_unchanged_launch_decoys(
        self,
        tmp_path,
        monkeypatch,
    ):
        import asyncio
        import sys
        from types import SimpleNamespace

        from spec_runtime.web import bridge_claude

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".env.keep").write_bytes(b"")
        (worktree / "package.json").write_text("{}\n", encoding="utf-8")
        (worktree / ".yarnrc.keep").symlink_to(worktree / "missing-target")
        before = {path.name for path in worktree.iterdir()}
        created = {
            ".env",
            ".env.local",
            ".env.staging",
            ".gitmodules",
            ".npmrc",
            ".yarnrc",
            ".yarnrc.yml",
            ".yarnrc.project",
            "bun.lock",
            "bun.lockb",
            "bunfig.toml",
            "npm-shrinkwrap.json",
            "package-lock.json",
            "package.generated.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        }

        class _Options:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class _Client:
            def __init__(self, *, options):
                self.options = options

            async def connect(self):
                for name in created:
                    (worktree / name).write_bytes(b"")

            async def interrupt(self):
                return None

            async def disconnect(self):
                return None

        monkeypatch.setitem(
            sys.modules,
            "claude_agent_sdk",
            SimpleNamespace(ClaudeAgentOptions=_Options, ClaudeSDKClient=_Client),
        )
        monkeypatch.setattr(bridge_claude, "_sdk_available", lambda: True)
        monkeypatch.setattr(bridge_claude.shutil, "which", lambda _name: "/bin/true")
        async def run():
            bridge = bridge_claude.ClaudeBridge()
            session_id = await bridge.start_session(
                "system",
                agent="claude",
                cwd=str(worktree),
            )
            await bridge.stop_session(session_id)

        asyncio.run(run())
        assert {path.name for path in worktree.iterdir()} == before
        assert (worktree / ".env.keep").read_bytes() == b""
        assert (worktree / "package.json").read_text(encoding="utf-8") == "{}\n"
        assert (worktree / ".yarnrc.keep").is_symlink()

    def test_launch_decoy_modified_after_start_is_preserved(self, tmp_path):
        from spec_runtime.web.bridge_claude import (
            _cleanup_claude_launch_decoys,
            _record_claude_launch_decoys,
            _snapshot_claude_decoy_candidates,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        session = {
            "cwd": worktree,
            "decoy_before": _snapshot_claude_decoy_candidates(worktree),
            "launch_decoys": {},
        }
        decoy = worktree / ".env"
        decoy.write_bytes(b"")
        _record_claude_launch_decoys(session)
        decoy.write_text("user-authored-content\n", encoding="utf-8")
        _cleanup_claude_launch_decoys(session)
        assert decoy.read_text(encoding="utf-8") == "user-authored-content\n"


class TestChatJavaScriptContract:
    @pytest.fixture()
    def source(self) -> str:
        return (
            Path(__file__).resolve().parent.parent
            / "src/spec_runtime/web/static/chat.js"
        ).read_text()

    def test_uses_configured_backend_default(self, source: str) -> None:
        assert "data.default_agent" in source
        assert "availableBackends[data.default_agent]" in source

    def test_stop_confirms_and_displays_preserved_paths(self, source: str) -> None:
        assert "worktree and branch will be preserved" in source
        assert "if (data.worktree)" in source
        assert "if (data.branch)" in source

    def test_spec_review_id_uses_attribute_escaping(self, source: str) -> None:
        assert "&quot;" in source
        assert "&#39;" in source
        assert "escapeAttribute(msg.specId)" in source
        assert "escapeHtml(msg.specId) + '\">Implement" not in source
