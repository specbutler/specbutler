from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime import execution_backend as eb
from spec_runtime import orchestrator as orch
from spec_runtime.agent_adapter import ClaudeAgent


def _credential_env_names(settings: dict[str, object]) -> set[str]:
    sandbox = settings["sandbox"]
    assert isinstance(sandbox, dict)
    credentials = sandbox["credentials"]
    assert isinstance(credentials, dict)
    env_vars = credentials["envVars"]
    assert isinstance(env_vars, list)
    return {str(entry["name"]) for entry in env_vars if isinstance(entry, dict)}


def test_claude_mcp_literals_and_references_are_launch_only(tmp_path: Path) -> None:
    secrets = {
        "literal-env-secret",
        "referenced-env-secret",
        "argv-secret",
        "opaque-positional-sentinel",
        "Bearer header-secret",
        "https://example.test/mcp/url-path-secret-sentinel",
    }
    materialization = orch._write_claude_mcp_config(
        tmp_path,
        extra_mcp_servers={
            "stdio": {
                "command": "python",
                "args": [
                    "server.py",
                    "--token",
                    "argv-secret",
                    "opaque-positional-sentinel",
                ],
                "env": {
                    "MCP_LITERAL": "literal-env-secret",
                    "MCP_REFERENCE": "${HOST_MCP_SECRET}",
                },
            },
            "remote": {
                "url": "https://example.test/mcp/url-path-secret-sentinel",
                "headers": {"Authorization": "Bearer header-secret"},
            },
        },
        protect_secrets=True,
        secret_source={"HOST_MCP_SECRET": "referenced-env-secret"},
    )

    config_path = tmp_path / ".claude" / "mcp-servers.json"
    config_text = config_path.read_text(encoding="utf-8")
    assert all(secret not in config_text for secret in secrets)
    assert secrets <= set(materialization.environment.values())
    assert secrets <= set(materialization.redactions)
    assert materialization.referenced_environment_keys == frozenset(
        materialization.environment
    )
    assert all(
        name.startswith("SPEC_MCP_RUNTIME_")
        and eb._is_container_worker_env_allowed(name)
        for name in materialization.environment
    )
    if os.name == "posix":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_claude_sandbox_guards_full_user_passthrough_mcp_set(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    operator_secret = "user-passthrough-secret"
    (operator_home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "private-remote": {
                        "url": "https://mcp.example.test",
                        "headers": {"X-Private-Token": operator_secret},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = replace(
        orch.SPEC_RUNTIME_CONFIG,
        mcp=replace(
            orch.SPEC_RUNTIME_CONFIG.mcp,
            allow_from_user=("private-remote",),
        ),
    )

    with (
        patch.object(orch, "SPEC_RUNTIME_CONFIG", config),
        patch.object(orch.Path, "home", return_value=operator_home),
    ):
        materialization = orch._write_sandbox_config(
            "claude",
            tmp_path,
            protect_claude_mcp_secrets=True,
            claude_mcp_secret_source={},
        )

    assert materialization is not None
    config_text = (tmp_path / ".claude" / "mcp-servers.json").read_text()
    assert operator_secret not in config_text
    assert operator_secret in materialization.environment.values()
    settings = json.loads(
        (tmp_path / ".claude" / "settings.local.json").read_text()
    )
    assert materialization.referenced_environment_keys <= _credential_env_names(
        settings
    )
    credentials = settings["sandbox"]["credentials"]
    assert "environmentVariables" not in credentials
    assert all(entry["mode"] == "deny" for entry in credentials["envVars"])
    assert all(entry["mode"] == "deny" for entry in credentials["files"])


def test_external_sandbox_settings_are_credential_only(tmp_path: Path) -> None:
    materialization = orch._write_claude_mcp_config(
        tmp_path,
        extra_mcp_servers={
            "stdio": {
                "command": "python",
                "env": {"MCP_TOKEN": "container-secret"},
            }
        },
        protect_secrets=True,
        secret_source={},
    )
    orch._write_external_sandbox_claude_settings(tmp_path, materialization)

    settings = json.loads(
        (tmp_path / ".claude" / "settings.local.json").read_text()
    )
    assert "enabled" not in settings["sandbox"]
    assert "filesystem" not in settings["sandbox"]
    assert materialization.referenced_environment_keys <= _credential_env_names(
        settings
    )
    assert any(
        ".claude" in entry["path"] and entry["mode"] == "deny"
        for entry in settings["sandbox"]["credentials"]["files"]
    )


def test_claude_implementation_allows_only_explicit_mcp_servers(
    tmp_path: Path,
) -> None:
    cmd = ClaudeAgent().build_implement_command(
        prompt="implement",
        worktree_path=tmp_path,
        state_dir=tmp_path / "outbox",
        mcp_config_path=tmp_path / ".claude" / "mcp-servers.json",
        mcp_servers={"declared-server": {"command": "helper"}},
        externally_sandboxed=True,
    )

    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__declared-server__*" in allowed.split(",")
    assert "mcp__*" not in allowed.split(",")
    assert "--safe-mode" not in cmd
    assert cmd[cmd.index("--settings") + 1] == str(
        tmp_path / ".claude" / "settings.local.json"
    )


def test_container_mcp_runtime_secret_uses_key_only_export_without_smuggling() -> None:
    runtime_name = "SPEC_MCP_RUNTIME_0123456789ABCDEF01234567"
    secret = "docker-argv-secret"

    assert eb.ContainerExecutionBackend._container_worker_env_arg(
        runtime_name
    ) == runtime_name
    with pytest.raises(ValueError, match="Invalid container environment variable name"):
        eb.ContainerExecutionBackend._container_worker_env_arg(
            "SPEC_MCP_RUNTIME_0123456789ABCDEF01234567=SMUGGLED"
        )
    client_env = eb.ContainerExecutionBackend._container_client_env(
        {runtime_name: secret},
        inherit_env=False,
    )
    assert client_env is not None
    assert client_env[runtime_name] == secret
    assert eb.ContainerExecutionBackend._request_env_log_redactions(
        {runtime_name: secret}
    ) == [secret]


def test_private_host_home_uses_canonical_refresh_store_and_cleans_up(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator-home"
    canonical_config = operator_home / ".claude"
    canonical_config.mkdir(parents=True)
    source_config = operator_home / ".claude.json"
    source_config.write_text('{"oauthAccount":{"uuid":"operator"}}')
    credentials = canonical_config / ".credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-access",
                    "refreshToken": "rotatable-refresh",
                    "expiresAt": 1,
                }
            }
        )
    )

    state_root = tmp_path / "operator-state"
    with (
        patch.object(orch.Path, "home", return_value=operator_home),
        patch.dict(os.environ, {"XDG_STATE_HOME": str(state_root)}, clear=False),
    ):
        context, home, resolved_config = orch._write_claude_private_host_home(
            source_config=source_config
        )
        assert home.is_relative_to(state_root)
        assert resolved_config == canonical_config.resolve()
        assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
            "uuid": "operator"
        }
        assert not (home / ".claude" / ".credentials.json").exists()

        # Claude refreshes the canonical file directly through
        # CLAUDE_CONFIG_DIR, so a rotated single-use token is never stranded
        # in a disposable copy.
        rotated = json.loads(credentials.read_text())
        rotated["claudeAiOauth"]["refreshToken"] = "rotated-refresh"
        credentials.write_text(json.dumps(rotated))

        orch._remove_claude_private_host_home(context, home)
        assert not home.exists()
        assert "rotated-refresh" in credentials.read_text()
