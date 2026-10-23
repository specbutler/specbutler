"""``[execution].safety_mode`` native/isolated behaviour.

Hermetic: no provider CLI is started. Covers the generated Claude settings and
Codex config for each mode, the PreToolUse write guard, network modes, the
invariant that no unattended launch can wait on an approval, and the
``sandbox_denied`` failure classification.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.agent_adapter import ClaudeAgent, CodexAgent
from spec_runtime.claude_write_guard import check as guard_check
from spec_runtime.config import (
    DEFAULT_ALLOWED_NETWORK_DOMAINS,
    ExecutionConfig,
    ExecutionNetworkConfig,
    SpecConfigError,
    SpecRuntimeConfig,
    emit_safety_mode_deprecation_note,
    load_spec_runtime_config,
    safety_mode_deprecation_note,
)
from spec_runtime.execution_backend import get_execution_backend
from spec_runtime.safety_policy import (
    LaunchSafety,
    claude_native_unavailability_reason,
    claude_sandbox_network,
    claude_write_guard_script,
    codex_profile_network,
)
from spec_runtime.sandbox_denials import (
    SandboxDenial,
    claude_event_denials,
    codex_event_denials,
    sandbox_denial_summary,
)

NATIVE = LaunchSafety(claude_bypass=True)
FALLBACK = LaunchSafety(claude_bypass=False)
GUARD_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"


def _load(tmp_path: Path, body: str, *, local: str = "") -> SpecRuntimeConfig:
    config = tmp_path / ".spec.toml"
    config.write_text(textwrap.dedent(body), encoding="utf-8")
    if local:
        (tmp_path / ".spec.local.toml").write_text(textwrap.dedent(local), encoding="utf-8")
    return load_spec_runtime_config(config_path=config, env={})


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace.resolve()


def _claude_settings(workspace: Path, **kwargs) -> dict:
    orch._write_sandbox_config("claude", workspace, **kwargs)
    return json.loads(
        (workspace / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )


def _flag_value(cmd: list[str], flag: str) -> str | None:
    return next(
        (cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == flag),
        None,
    )


def _config_overrides(cmd: list[str]) -> list[str]:
    return [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "-c"]


def _profile_network(cmd: list[str], profile: str = "specbutler-implement") -> str:
    prefix = f"permissions.{profile}.network="
    return next(
        value.removeprefix(prefix)
        for value in _config_overrides(cmd)
        if value.startswith(prefix)
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestSafetyModeConfig:
    def test_default_is_native_with_open_network(self, tmp_path: Path) -> None:
        config = _load(tmp_path, '[execution]\nbackend = "clone"\n')
        assert config.execution.safety_mode == "native"
        assert config.execution.network == ExecutionNetworkConfig()
        assert config.execution.network.mode == "open"
        assert config.execution.codex.auto_review is False
        assert ExecutionConfig().safety_mode == "native"

    @pytest.mark.parametrize("legacy", ["safe", "full-auto", "trusted"])
    def test_legacy_values_map_to_native_with_a_one_time_note(
        self,
        tmp_path: Path,
        legacy: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from spec_runtime import config as config_module

        monkeypatch.setattr(config_module, "_EMITTED_SAFETY_MODE_NOTES", set())
        config = _load(
            tmp_path,
            f'[execution]\nbackend = "clone"\nsafety_mode = "{legacy}"\n',
        )
        assert config.execution.safety_mode == "native"
        assert config.execution.legacy_safety_mode == legacy
        note = safety_mode_deprecation_note(config)
        assert note and legacy in note and "native" in note

        emit_safety_mode_deprecation_note(config)
        emit_safety_mode_deprecation_note(config)
        assert capsys.readouterr().err.count("deprecated") == 1

    def test_native_has_no_deprecation_note(self, tmp_path: Path) -> None:
        config = _load(tmp_path, '[execution]\nsafety_mode = "native"\n')
        assert safety_mode_deprecation_note(config) is None

    def test_isolated_rejects_a_host_backend(self, tmp_path: Path) -> None:
        with pytest.raises(SpecConfigError, match="isolated.*requires backend = \"container\""):
            _load(tmp_path, '[execution]\nbackend = "clone"\nsafety_mode = "isolated"\n')

    def test_isolated_without_backend_selects_container(self, tmp_path: Path) -> None:
        config = _load(tmp_path, '[execution]\nsafety_mode = "isolated"\n')
        assert config.execution.backend == "container"
        assert config.execution.safety_mode == "isolated"

    @pytest.mark.parametrize("requested", ["", 'safety_mode = "safe"', 'safety_mode = "native"'])
    def test_container_backend_runs_isolated(self, tmp_path: Path, requested: str) -> None:
        config = _load(tmp_path, f'[execution]\nbackend = "container"\n{requested}\n')
        assert config.execution.safety_mode == "isolated"

    def test_unknown_mode_lists_the_new_values(self, tmp_path: Path) -> None:
        with pytest.raises(SpecConfigError, match="native, isolated"):
            _load(tmp_path, '[execution]\nsafety_mode = "yolo"\n')

    def test_backend_factory_rejects_isolated_host_backend(self) -> None:
        with pytest.raises(ValueError, match="isolated"):
            get_execution_backend(ExecutionConfig(backend="clone", safety_mode="isolated"))
        # Directly constructed legacy configs still work.
        get_execution_backend(ExecutionConfig(backend="clone", safety_mode="safe"))

    def test_network_allowlist_and_deny_list(self, tmp_path: Path) -> None:
        config = _load(
            tmp_path,
            """
            [execution.network]
            mode = "allowlist"
            allowed_domains = ["GitHub.com", "*.example.com", "github.com"]
            denied_domains = ["evil.example.com"]
            """,
        )
        network = config.execution.network
        assert network.mode == "allowlist"
        assert network.allowed_domains == ("github.com", "*.example.com")
        assert network.denied_domains == ("evil.example.com",)

    def test_allowlist_defaults_to_the_built_in_list(self, tmp_path: Path) -> None:
        config = _load(tmp_path, '[execution.network]\nmode = "allowlist"\n')
        assert config.execution.network.allowed_domains == DEFAULT_ALLOWED_NETWORK_DOMAINS

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ('mode = "closed"', "Unknown \\[execution.network\\].mode"),
            ('allowed_domains = "github.com"', "must be a list"),
            ('allowed_domains = ["https://github.com"]', "not a URL"),
            ('denied_domains = ["*"]', "too broad"),
            ('mode = "allowlist"\nallowed_domains = []', "at least one"),
        ],
    )
    def test_network_rejects_invalid_values(
        self, tmp_path: Path, body: str, message: str
    ) -> None:
        with pytest.raises(SpecConfigError, match=message):
            _load(tmp_path, f"[execution.network]\n{body}\n")

    def test_codex_auto_review_must_be_boolean(self, tmp_path: Path) -> None:
        config = _load(tmp_path, "[execution.codex]\nauto_review = true\n")
        assert config.execution.codex.auto_review is True
        with pytest.raises(SpecConfigError, match="auto_review"):
            _load(tmp_path, '[execution.codex]\nauto_review = "yes"\n')

    def test_local_overrides_merge_network_table(self, tmp_path: Path) -> None:
        config = _load(
            tmp_path,
            '[execution.network]\nmode = "allowlist"\nallowed_domains = ["github.com"]\n',
            local='[execution.network]\ndenied_domains = ["evil.example.com"]\n',
        )
        network = config.execution.network
        assert network.mode == "allowlist"
        assert network.allowed_domains == ("github.com",)
        assert network.denied_domains == ("evil.example.com",)


# ---------------------------------------------------------------------------
# Generated Claude settings
# ---------------------------------------------------------------------------


class TestClaudeSettings:
    def test_native_settings_drop_bypass_disable_and_add_write_guard(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        outbox = (tmp_path / "outbox").resolve()
        settings = _claude_settings(
            workspace,
            safety=NATIVE,
            extra_write_roots=(outbox,),
        )

        sandbox = settings["sandbox"]
        assert sandbox["enabled"] is True
        assert sandbox["failIfUnavailable"] is True
        assert sandbox["allowUnsandboxedCommands"] is False
        assert sandbox["autoAllowBashIfSandboxed"] is True
        assert sandbox["filesystem"]["denyRead"]
        assert sandbox["credentials"]["envVars"]
        assert "disableBypassPermissionsMode" not in settings["permissions"]
        deny = settings["permissions"]["deny"]
        assert "Bash(git push --force*)" in deny
        assert "Bash(git push -f*)" in deny
        assert "Bash(git reset --hard*)" in deny
        assert any(rule.startswith("Read(") for rule in deny)

        [entry] = settings["hooks"]["PreToolUse"]
        assert entry["matcher"] == GUARD_TOOLS
        [hook] = entry["hooks"]
        assert hook["type"] == "command"
        argv = shlex.split(hook["command"])
        assert argv[:3] == [sys.executable, "-I", str(claude_write_guard_script())]
        allowed = [argv[i + 1] for i, value in enumerate(argv) if value == "--allow"]
        denied = [argv[i + 1] for i, value in enumerate(argv) if value == "--deny"]
        assert str(workspace) in allowed
        assert str(outbox) in allowed
        assert str(workspace / ".claude") in denied

    def test_fallback_and_interactive_settings_keep_bypass_disabled(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        for kwargs in ({}, {"safety": FALLBACK}, {"safety": NATIVE, "interactive": True}):
            settings = _claude_settings(workspace, **kwargs)
            assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"
            assert "hooks" not in settings
            assert settings["sandbox"]["failIfUnavailable"] is True
            assert settings["sandbox"]["allowUnsandboxedCommands"] is False

    def test_native_open_network_has_no_allowlist(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        settings = _claude_settings(workspace, safety=NATIVE)
        assert settings["sandbox"]["network"] == {}

    def test_fallback_keeps_allowlist_as_pre_approved_hosts(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        settings = _claude_settings(workspace, safety=FALLBACK)
        network = settings["sandbox"]["network"]
        assert network["allowedDomains"] == list(DEFAULT_ALLOWED_NETWORK_DOMAINS)
        assert "strictAllowlist" not in network

    def test_allowlist_mode_is_strict_and_deny_list_always_applies(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        network = ExecutionNetworkConfig(
            mode="allowlist",
            allowed_domains=("github.com",),
            denied_domains=("evil.example.com",),
        )
        settings = _claude_settings(
            workspace,
            safety=LaunchSafety(claude_bypass=True, network=network),
        )
        assert settings["sandbox"]["network"] == {
            "allowedDomains": ["github.com"],
            "strictAllowlist": True,
            "deniedDomains": ["evil.example.com"],
        }

    def test_interactive_sessions_use_configured_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(
            orch,
            "SPEC_RUNTIME_CONFIG",
            SpecRuntimeConfig(
                execution=ExecutionConfig(
                    network=ExecutionNetworkConfig(denied_domains=("evil.example.com",))
                )
            ),
        )
        settings = _claude_settings(workspace, interactive=True)
        assert settings["sandbox"]["network"]["deniedDomains"] == ["evil.example.com"]
        assert settings["sandbox"]["network"]["allowedDomains"]

    @pytest.mark.parametrize(
        ("network", "bypass", "expected"),
        [
            (ExecutionNetworkConfig(), True, {}),
            (
                ExecutionNetworkConfig(denied_domains=("x.example",)),
                True,
                {"deniedDomains": ["x.example"]},
            ),
            (
                ExecutionNetworkConfig(mode="allowlist", allowed_domains=("a.example",)),
                False,
                {"allowedDomains": ["a.example"], "strictAllowlist": True},
            ),
        ],
    )
    def test_claude_network_rendering(self, network, bypass, expected) -> None:
        assert claude_sandbox_network(network, bypass=bypass) == expected


# ---------------------------------------------------------------------------
# Claude launch commands and capability probe
# ---------------------------------------------------------------------------

CLAUDE_HELP = """
  --add-dir <directories...>
  --allowedTools <tools...>
  --no-session-persistence
  --permission-mode <mode>  (choices: "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan")
  --restricted
  --safe-mode
  --setting-sources <sources>
  --settings <file-or-json>
  --strict-mcp-config
  --tools <tools...>
"""


class TestClaudeLaunch:
    def _implement(self, tmp_path: Path, safety: LaunchSafety | None) -> list[str]:
        return ClaudeAgent().build_implement_command(
            prompt="do it",
            worktree_path=tmp_path,
            state_dir=tmp_path / "outbox",
            stream_json=True,
            safety=safety,
        )

    def test_native_implement_runs_bypass_without_restricted(self, tmp_path: Path) -> None:
        cmd = self._implement(tmp_path, NATIVE)
        assert _flag_value(cmd, "--permission-mode") == "bypassPermissions"
        assert "--restricted" not in cmd
        assert _flag_value(cmd, "--setting-sources") == ""
        assert _flag_value(cmd, "--settings") == str(tmp_path / ".claude" / "settings.local.json")
        assert _flag_value(cmd, "--add-dir") == str(tmp_path / "outbox")

    @pytest.mark.parametrize("safety", [None, FALLBACK])
    def test_fallback_implement_keeps_restricted_accept_edits(
        self, tmp_path: Path, safety: LaunchSafety | None
    ) -> None:
        cmd = self._implement(tmp_path, safety)
        assert _flag_value(cmd, "--permission-mode") == "acceptEdits"
        assert "--restricted" in cmd

    def test_capability_probe_requires_bypass_choice(self) -> None:
        assert claude_native_unavailability_reason(CLAUDE_HELP) == ""
        older = CLAUDE_HELP.replace('"bypassPermissions", ', "")
        assert "bypassPermissions" in claude_native_unavailability_reason(older)
        assert "--setting-sources" in claude_native_unavailability_reason(
            CLAUDE_HELP.replace("--setting-sources", "")
        )
        assert claude_native_unavailability_reason("")

    def _launch_safety(self, monkeypatch, *, help_text: str, safety_mode: str = "native"):
        monkeypatch.setattr(
            orch,
            "SPEC_RUNTIME_CONFIG",
            SpecRuntimeConfig(execution=ExecutionConfig(backend="clone", safety_mode=safety_mode)),
        )
        monkeypatch.setattr(
            orch,
            "run_subprocess",
            lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, help_text, ""),
        )
        backend = get_execution_backend(ExecutionConfig(backend="clone"))
        return orch._unattended_launch_safety("claude", backend)

    def test_launch_uses_bypass_when_cli_supports_it(self, monkeypatch) -> None:
        assert self._launch_safety(monkeypatch, help_text=CLAUDE_HELP).claude_bypass is True

    def test_launch_falls_back_when_cli_lacks_bypass(self, monkeypatch) -> None:
        safety = self._launch_safety(
            monkeypatch, help_text=CLAUDE_HELP.replace("bypassPermissions", "")
        )
        assert safety.claude_bypass is False

    def test_container_backend_keeps_its_launch_policy(self, monkeypatch) -> None:
        monkeypatch.setattr(
            orch,
            "run_subprocess",
            lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, CLAUDE_HELP, ""),
        )

        class _Identity:
            backend = "container"

        class _ContainerBackend:
            identity = _Identity()

        assert orch._unattended_launch_safety("claude", _ContainerBackend()) == LaunchSafety()

    def test_doctor_warns_when_native_launch_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        from spec_runtime import doctor

        config = SpecRuntimeConfig()

        def run(cmd, cwd, timeout):
            text = CLAUDE_HELP.replace("bypassPermissions", "")
            return subprocess.CompletedProcess(cmd, 0, text, "")

        checks = doctor._agent_checks(
            tmp_path,
            config,
            lambda name: f"/usr/bin/{name}" if name == "claude" else None,
            run,
        )
        native = [check for check in checks if check.name == "agent native launch (claude)"]
        assert native and native[0].status == "warning"
        assert "acceptEdits" in native[0].detail


# ---------------------------------------------------------------------------
# Codex launch commands
# ---------------------------------------------------------------------------


class TestCodexLaunch:
    def _implement(self, tmp_path: Path, safety: LaunchSafety | None = None) -> list[str]:
        workspace = _workspace(tmp_path)
        return CodexAgent().build_implement_command(
            prompt="do it",
            worktree_path=workspace,
            state_dir=tmp_path / "outbox",
            safety=safety,
        )

    def test_default_never_asks_and_has_open_network(self, tmp_path: Path) -> None:
        cmd = self._implement(tmp_path)
        assert cmd[:3] == ["codex", "-a", "never"]
        assert "exec" in cmd
        assert not any("approvals_reviewer" in value for value in _config_overrides(cmd))
        assert _profile_network(cmd) == '{enabled=true,mode="full",allow_local_binding=true}'

    def test_auto_review_routes_escalations_to_the_reviewer(self, tmp_path: Path) -> None:
        cmd = self._implement(tmp_path, LaunchSafety(codex_auto_review=True))
        assert _flag_value(cmd, "-a") == "on-request"
        assert 'approvals_reviewer="auto_review"' in _config_overrides(cmd)
        assert cmd.index("exec") > cmd.index("-a")

    def test_allowlist_network_uses_proxy_domains(self, tmp_path: Path) -> None:
        network = ExecutionNetworkConfig(
            mode="allowlist",
            allowed_domains=("github.com", "*.npmjs.org"),
            denied_domains=("evil.example.com",),
        )
        cmd = self._implement(tmp_path, LaunchSafety(network=network))
        assert _profile_network(cmd) == (
            '{enabled=true,mode="full",allow_local_binding=true,'
            'domains={"github.com"="allow","*.npmjs.org"="allow","evil.example.com"="deny"}}'
        )

    def test_open_network_deny_list_uses_proxy_domains(self) -> None:
        network = ExecutionNetworkConfig(denied_domains=("evil.example.com",))
        assert codex_profile_network(network) == (
            '{enabled=true,mode="full",allow_local_binding=true,'
            'domains={"evil.example.com"="deny"}}'
        )
        assert codex_profile_network(network, enabled=False) == "{enabled=false}"

    def test_interactive_authoring_keeps_prompting_with_network_off(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        cmd = CodexAgent().build_authoring_command(prompt="p", worktree_path=workspace)
        assert cmd[:3] == ["codex", "-a", "on-request"]
        assert "exec" not in cmd
        assert _profile_network(cmd, "specbutler-authoring") == "{enabled=false}"

    def test_unattended_authoring_uses_the_implement_policy(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        cmd = CodexAgent().build_authoring_command(
            prompt="p",
            worktree_path=workspace,
            interactive=False,
        )
        assert cmd[:3] == ["codex", "-a", "never"]
        assert cmd[cmd.index("exec") + 1] == "--json"
        assert _profile_network(cmd, "specbutler-authoring").startswith("{enabled=true")


# ---------------------------------------------------------------------------
# No unattended launch may wait on an approval
# ---------------------------------------------------------------------------


def _cannot_block_on_approval(cmd: list[str]) -> bool:
    """Return True when *cmd* runs to completion with nobody to approve.

    Claude print mode never shows a prompt: anything that would prompt is
    denied. ``codex exec`` never waits for a person; an ``on-request`` policy
    is acceptable only with the model reviewer answering.
    """
    if cmd[0] == "claude":
        return "-p" in cmd and _flag_value(cmd, "--permission-mode") in (
            None,
            "bypassPermissions",
            "acceptEdits",
            "dontAsk",
        )
    if cmd[0] == "codex":
        if "exec" not in cmd:
            return False
        approval = _flag_value(cmd[: cmd.index("exec")], "-a")
        if approval in (None, "never"):
            return True
        return approval == "on-request" and 'approvals_reviewer="auto_review"' in _config_overrides(cmd)
    return False


def _unattended_commands(tmp_path: Path) -> dict[str, list[str]]:
    from spec_runtime import init

    workspace = _workspace(tmp_path)
    outbox = tmp_path / "outbox"
    commands: dict[str, list[str]] = {}
    for label, safety in (
        ("native", NATIVE),
        ("fallback", FALLBACK),
        ("auto-review", LaunchSafety(codex_auto_review=True)),
        ("unset", None),
    ):
        for stream_json in (False, True):
            commands[f"claude implement {label} stream={stream_json}"] = (
                ClaudeAgent().build_implement_command(
                    prompt="p",
                    worktree_path=workspace,
                    state_dir=outbox,
                    stream_json=stream_json,
                    safety=safety,
                )
            )
        commands[f"codex implement {label}"] = CodexAgent().build_implement_command(
            prompt="p", worktree_path=workspace, state_dir=outbox, safety=safety
        )
        for adapter in (ClaudeAgent(), CodexAgent()):
            commands[f"{adapter.name} unattended authoring {label}"] = (
                adapter.build_authoring_command(
                    prompt="p",
                    worktree_path=workspace,
                    state_dir=outbox,
                    initial_prompt="go",
                    interactive=False,
                    safety=safety,
                )
            )
    for adapter in (ClaudeAgent(), CodexAgent()):
        commands[f"{adapter.name} review"] = adapter.build_review_command(
            prompt="p",
            output_path=tmp_path / "review.json",
            schema_path=tmp_path / "schema.json",
            writable_temp_dir=tmp_path,
        )
        commands[f"{adapter.name} block debugger"] = orch._build_block_debugger_command(
            prompt="p",
            schema_path=tmp_path / "schema.json",
            output_path=tmp_path / "diagnosis.json",
            agent_name=adapter.name,
            writable_output_dir=tmp_path,
        )
        commands[f"{adapter.name} init merge"] = init._build_agent_merge_command(adapter.name)
    return commands


def test_every_unattended_launch_builder_cannot_block_on_approval(tmp_path: Path) -> None:
    blocking = {
        label: cmd
        for label, cmd in _unattended_commands(tmp_path).items()
        if not _cannot_block_on_approval(cmd)
    }
    assert blocking == {}


def test_interactive_authoring_builders_keep_prompting(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    for adapter in (ClaudeAgent(), CodexAgent()):
        cmd = adapter.build_authoring_command(prompt="p", worktree_path=workspace)
        assert not _cannot_block_on_approval(cmd), adapter.name
    claude = ClaudeAgent().build_authoring_command(prompt="p", worktree_path=workspace)
    assert "-p" not in claude
    assert _flag_value(claude, "--permission-mode") == "acceptEdits"


def test_unattended_claude_authoring_matches_the_implement_policy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    cmd = ClaudeAgent().build_authoring_command(
        prompt="system",
        worktree_path=workspace,
        initial_prompt="go",
        interactive=False,
        safety=NATIVE,
    )
    assert cmd[:2] == ["claude", "-p"]
    assert _flag_value(cmd, "--permission-mode") == "bypassPermissions"
    assert _flag_value(cmd, "--append-system-prompt") == "system"
    assert cmd[-2:] == ["--", "go"]


# ---------------------------------------------------------------------------
# PreToolUse write guard
# ---------------------------------------------------------------------------


def _run_guard(command: str, payload: object, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Claude host sandbox is POSIX-only")
class TestWriteGuardHook:
    @pytest.fixture
    def layout(self, tmp_path: Path) -> dict[str, Path]:
        workspace = _workspace(tmp_path)
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        settings = _claude_settings(
            workspace,
            safety=NATIVE,
            extra_write_roots=(outbox,),
        )
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        return {
            "workspace": workspace,
            "outbox": outbox.resolve(),
            "outside": outside.resolve(),
            "command": command,
        }

    def _call(self, layout, tool: str, path: str, key: str = "file_path"):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {key: path},
            "cwd": str(layout["workspace"]),
        }
        return _run_guard(layout["command"], payload, cwd=layout["workspace"])

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
    def test_allows_workspace_and_outbox(self, layout, tool: str) -> None:
        for target in (
            layout["workspace"] / "src" / "app.py",
            layout["outbox"] / "tmp" / "scratch.txt",
            Path("relative/inside.py"),
        ):
            result = self._call(layout, tool, str(target))
            assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
    def test_blocks_outside_workspace_with_exit_2(self, layout, tool: str) -> None:
        result = self._call(layout, tool, str(layout["outside"] / "notes.txt"))
        assert result.returncode == 2
        assert "outside the workspace" in result.stderr
        assert "Spec Butler write guard" in result.stderr

    def test_blocks_notebook_edit_outside(self, layout) -> None:
        result = self._call(
            layout,
            "NotebookEdit",
            str(layout["outside"] / "nb.ipynb"),
            key="notebook_path",
        )
        assert result.returncode == 2

    def test_blocks_parent_traversal_and_home(self, layout) -> None:
        result = self._call(layout, "Write", str(layout["workspace"] / ".." / "outside" / "x"))
        assert result.returncode == 2
        result = self._call(layout, "Write", "~/.bashrc")
        assert result.returncode == 2

    def test_blocks_symlink_escape(self, layout) -> None:
        link = layout["workspace"] / "link"
        link.symlink_to(layout["outside"], target_is_directory=True)
        result = self._call(layout, "Write", str(link / "escape.txt"))
        assert result.returncode == 2

    def test_blocks_protected_config_inside_workspace(self, layout) -> None:
        target = layout["workspace"] / ".claude" / "settings.local.json"
        result = self._call(layout, "Edit", str(target))
        assert result.returncode == 2
        assert "protected" in result.stderr

    def test_blocks_hard_links(self, layout) -> None:
        source = layout["outside"] / "shared.txt"
        source.write_text("x", encoding="utf-8")
        link = layout["workspace"] / "shared.txt"
        os.link(source, link)
        result = self._call(layout, "Edit", str(link))
        assert result.returncode == 2
        assert "hard link" in result.stderr

    def test_ignores_other_tools(self, layout) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
        assert _run_guard(layout["command"], payload, cwd=layout["workspace"]).returncode == 0

    def test_fails_closed_on_malformed_input(self, layout) -> None:
        for payload in ("not json", json.dumps({"tool_name": "Write", "tool_input": {}})):
            result = _run_guard(layout["command"], payload, cwd=layout["workspace"])
            assert result.returncode == 2

    def test_ignores_modules_planted_in_the_workspace(self, layout) -> None:
        (layout["workspace"] / "json.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        result = self._call(layout, "Write", str(layout["outside"] / "x"))
        assert result.returncode == 2

    def test_denied_paths_match_case_insensitively(self, tmp_path: Path) -> None:
        workspace = str(tmp_path / "ws")
        protected = str(tmp_path / "ws" / ".claude")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "ws" / ".CLAUDE" / "settings.json")},
            "cwd": workspace,
        }
        assert "protected" in guard_check(payload, [workspace], [protected])


# ---------------------------------------------------------------------------
# sandbox_denied classification
# ---------------------------------------------------------------------------


class TestSandboxDenied:
    @pytest.mark.parametrize(
        "message",
        [
            "Agent exited with code 1 (no_handshake). Sandbox denied write to /etc/hosts (sandbox_denied).",
            "touch: /Users/me/.zshrc: Operation not permitted",
            "exec_command failed: approval is not supported in exec mode",
            "curl: request blocked by sandbox network policy for evil.example.com",
        ],
    )
    def test_classified_as_sandbox_denied_infrastructure(self, message: str) -> None:
        classification = orch.classify_failure(message, phase="implement")
        assert classification.category is orch.FailureCategory.SANDBOX_DENIED
        assert classification.kind == "sandbox_denied"
        assert classification.is_infrastructure
        assert classification.transient
        assert classification.signature.startswith("sandbox_denied:")

    def test_gate_output_is_not_a_sandbox_denial(self) -> None:
        classification = orch.classify_failure(
            "gate 'test' failed: open /tmp/x: Operation not permitted",
            phase="verify",
        )
        assert classification.category is not orch.FailureCategory.SANDBOX_DENIED

    def test_does_not_spend_a_code_retry(self) -> None:
        run = orch.RunState(run_id="r", spec_id="s", branch="b", phase="implement")
        run.last_error = "Agent exited with code 1 (no_handshake)."
        assert orch._workflow_failure_policy(run, "implement") is not None
        run.last_error += " Sandbox denied write to /etc/hosts (sandbox_denied)."
        assert orch._workflow_failure_policy(run, "implement") is None

    def test_claude_stream_permission_denials_are_tracked(self) -> None:
        tracker = orch.AgentProgressTracker(agent="claude")
        orch._handle_claude_stream_line(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "is_error": True,
                                "content": (
                                    "Spec Butler write guard: Write may not write "
                                    "/Users/me/.bashrc: it is outside the workspace."
                                ),
                            }
                        ]
                    },
                }
            ),
            tracker,
        )
        assert tracker.latest_sandbox_denial() == SandboxDenial("write", "/Users/me/.bashrc")
        orch._handle_claude_stream_line(
            json.dumps(
                {
                    "type": "result",
                    "permission_denials": [
                        {"tool_name": "WebFetch", "tool_input": {"url": "https://evil.example.com"}}
                    ],
                }
            ),
            tracker,
        )
        denial = tracker.latest_sandbox_denial()
        assert denial == SandboxDenial("permission", "https://evil.example.com", "WebFetch")

    def test_successful_tool_results_are_not_denials(self) -> None:
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "Operation not permitted"}]},
        }
        assert claude_event_denials(event) == []

    def test_codex_events(self) -> None:
        failed = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "failed",
                "aggregated_output": "rm: /Users/me/project: Operation not permitted",
            },
        }
        assert codex_event_denials(failed) == [SandboxDenial("write", "/Users/me/project")]
        error = {"type": "error", "message": "approval is not supported in exec mode"}
        assert codex_event_denials(error) == [SandboxDenial("approval")]
        assert codex_event_denials({"type": "turn.completed"}) == []

    def test_failed_attempt_names_the_denial_and_status_reports_it(self) -> None:
        tracker = orch.AgentProgressTracker(agent="codex")
        tracker.record_sandbox_denials([SandboxDenial("network", "evil.example.com")])
        outcome = orch.ImplementAttemptOutcome(
            exit_code=1,
            head_before="a",
            head_after="a",
            has_new_commit=False,
            has_uncommitted_changes=False,
            progress_tracker=tracker,
        )
        run = orch.RunState(run_id="r", spec_id="s", branch="b")
        run.last_error = "Agent exited with code 1 (no_handshake)."
        orch._annotate_sandbox_denial(run, outcome)
        orch._annotate_sandbox_denial(run, outcome)
        assert run.last_error.count("sandbox_denied") == 1
        assert sandbox_denial_summary(run.last_error) == "network access to evil.example.com"
        assert orch.classify_failure(run.last_error, phase="implement").kind == "sandbox_denied"

    def test_summary_falls_back_to_the_raw_message(self) -> None:
        assert (
            sandbox_denial_summary("touch: /etc/x: Operation not permitted")
            == "write to /etc/x"
        )
        assert sandbox_denial_summary("all good") == ""
