from __future__ import annotations

import json
from pathlib import Path

from spec_runtime.interactive_authoring import (
    interactive_provider_environment,
    user_mcp_environment_keys,
)


def test_codex_interactive_env_keeps_only_explicit_user_mcp_values(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
[mcp_servers.docs]
command = "docs-server"
env_vars = ["DOCS_TOKEN"]

[mcp_servers.remote]
url = "https://mcp.example.test/${MCP_TENANT}"
bearer_token_env_var = "REMOTE_BEARER"
""",
        encoding="utf-8",
    )
    source = {
        "PATH": "/bin",
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "provider-secret",
        "DOCS_TOKEN": "docs-secret",
        "MCP_TENANT": "tenant-a",
        "REMOTE_BEARER": "remote-secret",
        "AWS_SECRET_ACCESS_KEY": "unrelated-secret",
        "GH_TOKEN": "forge-secret",
    }

    environment, protected = interactive_provider_environment("codex", source)

    assert protected == frozenset({"DOCS_TOKEN", "MCP_TENANT", "REMOTE_BEARER"})
    assert environment == {
        "PATH": "/bin",
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "provider-secret",
        "DOCS_TOKEN": "docs-secret",
        "MCP_TENANT": "tenant-a",
        "REMOTE_BEARER": "remote-secret",
    }


def test_claude_interactive_env_reads_user_settings_mcp_values(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "command": "local-server",
                        "env": {
                            "TOKEN": "${LOCAL_MCP_TOKEN}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source = {
        "PATH": "/bin",
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "ANTHROPIC_API_KEY": "provider-secret",
        "LOCAL_MCP_TOKEN": "mcp-secret",
        "DATABASE_URL": "unrelated-secret",
    }

    environment, protected = interactive_provider_environment("claude", source)

    assert protected == frozenset({"LOCAL_MCP_TOKEN"})
    assert environment == {
        "PATH": "/bin",
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "ANTHROPIC_API_KEY": "provider-secret",
        "LOCAL_MCP_TOKEN": "mcp-secret",
    }


def test_invalid_user_mcp_config_does_not_expand_the_environment(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("not = [valid", encoding="utf-8")
    source = {
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "provider-secret",
        "AMBIENT_SECRET": "must-not-pass",
    }

    assert user_mcp_environment_keys("codex", source) == frozenset()
    environment, protected = interactive_provider_environment("codex", source)
    assert protected == frozenset()
    assert environment == {
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "provider-secret",
    }


def test_custom_provider_environment_is_explicit_and_shell_protected() -> None:
    source = {
        "PATH": "/bin",
        "CUSTOM_PROVIDER_TOKEN": "provider-secret",
        "AMBIENT_SECRET": "must-not-pass",
    }

    environment, protected = interactive_provider_environment(
        "custom",
        source,
        provider_environment_keys={"CUSTOM_PROVIDER_TOKEN"},
    )

    assert environment == {
        "PATH": "/bin",
        "CUSTOM_PROVIDER_TOKEN": "provider-secret",
    }
    assert protected == frozenset({"CUSTOM_PROVIDER_TOKEN"})
