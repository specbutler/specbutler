"""Least-privilege environment helpers for interactive authoring sessions."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from .provider_env import minimal_provider_environment

_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MAX_USER_AGENT_CONFIG_BYTES = 4 * 1024 * 1024


def _mcp_environment_keys(value: object) -> set[str]:
    """Collect explicit parent-env references from a trusted MCP config."""
    keys: set[str] = set()
    if isinstance(value, dict):
        bearer = value.get("bearer_token_env_var")
        if isinstance(bearer, str) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", bearer
        ):
            keys.add(bearer)
        env_vars = value.get("env_vars")
        if isinstance(env_vars, list):
            keys.update(
                item
                for item in env_vars
                if isinstance(item, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item)
            )
        for nested in value.values():
            keys.update(_mcp_environment_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_mcp_environment_keys(nested))
    elif isinstance(value, str):
        keys.update(_ENV_REFERENCE_RE.findall(value))
    return keys


def _bounded_config_bytes(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_USER_AGENT_CONFIG_BYTES:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    return payload if len(payload) <= _MAX_USER_AGENT_CONFIG_BYTES else None


def user_mcp_environment_keys(
    provider: str,
    source: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return env names explicitly referenced by trusted user MCP config.

    Repository configuration is deliberately excluded. This preserves the
    operator's registered interactive MCP toolbox without forwarding every
    unrelated ambient secret into the model-controlled process.
    """
    values = os.environ if source is None else source
    normalized = provider.strip().lower()
    sections: list[object] = []
    if normalized == "codex":
        home = Path(values.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        payload = _bounded_config_bytes(home / "config.toml")
        if payload is not None:
            try:
                parsed = tomllib.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                parsed = {}
            sections.append(parsed.get("mcp_servers", {}))
    elif normalized == "claude":
        config_root = Path(
            values.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        ).expanduser()
        for path in (Path.home() / ".claude.json", config_root / "settings.json"):
            payload = _bounded_config_bytes(path)
            if payload is None:
                continue
            try:
                parsed = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                sections.append(parsed.get("mcpServers", {}))
                projects = parsed.get("projects", {})
                if isinstance(projects, dict):
                    sections.extend(
                        project.get("mcpServers", {})
                        for project in projects.values()
                        if isinstance(project, dict)
                    )
    return frozenset(
        key
        for section in sections
        for key in _mcp_environment_keys(section)
    )


def interactive_provider_environment(
    provider: str,
    source: Mapping[str, str] | None = None,
    *,
    provider_environment_keys: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, str], frozenset[str]]:
    """Build a minimal provider env plus explicitly declared MCP values."""
    values = os.environ if source is None else source
    mcp_keys = user_mcp_environment_keys(provider, values)
    extra_keys = {*mcp_keys, *provider_environment_keys}
    if provider.strip().lower() == "claude" and values.get("CLAUDE_CONFIG_DIR"):
        # Interactive Claude is intentionally allowed to load the operator's
        # trusted user settings/MCP registrations. The sandbox separately
        # denies model-selected commands access to this path.
        extra_keys.add("CLAUDE_CONFIG_DIR")
    return (
        minimal_provider_environment(provider, values, extra_keys=extra_keys),
        frozenset({*mcp_keys, *provider_environment_keys}),
    )
