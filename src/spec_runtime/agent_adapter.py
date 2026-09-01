"""Agent adapter boundary.

Isolates agent-specific launch behavior (command construction and sandbox
setup) behind a clean interface so that Codex/Claude specifics are not
entangled with the generic public CLI surface.

Usage
-----
Consumers obtain an ``AgentAdapter`` through ``get_agent_adapter(name)``
which returns the appropriate implementation for the requested agent.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

_logger = logging.getLogger(__name__)

# TOML-spec bare key: ASCII letters, digits, underscores, and dashes.
# Codex's `-c` dotted-key parser accepts these unquoted but does not accept
# quoted segments (e.g. `mcp_servers."foo bar".command=...` registers the
# literal name `"foo bar"` rather than `foo bar`), so names outside this
# character set cannot be reliably delivered via `-c` overrides.
_BARE_TOML_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


class HostAgentUnavailableError(RuntimeError):
    """Raised before an agent whose host isolation is unavailable is launched."""


def claude_sandbox_unavailability_reason(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Return an actionable reason Claude's host sandbox cannot run.

    Claude Code falls back to unsandboxed command execution when its native
    sandbox cannot start unless ``sandbox.failIfUnavailable`` is enabled.
    Detect the documented static prerequisites before launching an agent; the
    fail-closed provider setting remains the runtime check for AppArmor,
    namespace, and other host-specific failures.
    """
    active_platform = platform or sys.platform
    resolve = which or shutil.which
    if active_platform.startswith("linux"):
        missing = [name for name in ("bwrap", "socat") if not resolve(name)]
        if missing:
            rendered = ", ".join(f"`{name}`" for name in missing)
            return (
                "Claude sandbox prerequisites are missing on Linux: "
                f"{rendered}. Install `bubblewrap` and `socat`, then rerun "
                "`spec doctor`."
            )
        return ""
    if active_platform == "darwin":
        return ""
    return (
        f"Claude's host sandbox is not supported on platform {active_platform!r}. "
        "Use Codex for native execution, run Claude under WSL2 or macOS, or "
        "use the Linux container execution backend for implementation."
    )


def host_agent_unavailability_reason(
    agent_name: str,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Return why *agent_name* cannot be launched directly on this host.

    This is the common policy boundary for every host-side provider launch.
    Container implementation workers deliberately do not call it because the
    provider process runs in their Linux environment; authoring, local review,
    block debugging, and local chat always run on the host.
    """
    if agent_name.strip().lower() == "claude":
        return claude_sandbox_unavailability_reason(platform=platform, which=which)
    return ""


def require_host_agent_available(
    agent_name: str,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> None:
    """Fail closed before starting an unavailable host-side provider."""
    reason = host_agent_unavailability_reason(
        agent_name,
        platform=platform,
        which=which,
    )
    if reason:
        raise HostAgentUnavailableError(reason)


def _toml_quote(s: str) -> str:
    """Return *s* wrapped in a TOML basic-string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_inline_key(key: str) -> str:
    """Return a TOML inline-table key: bare if possible, else quoted."""
    if _BARE_TOML_KEY_RE.fullmatch(key):
        return key
    return _toml_quote(key)


def _render_codex_mcp_toml(
    mcp_servers: dict[str, dict[str, object]] | None,
) -> str:
    """Render ``mcp_servers`` as a Codex ``config.toml`` body string.

    The output is the same set of servers that ``_codex_mcp_server_overrides``
    emits as ``-c`` argv overrides, but serialized as fully-formed TOML so it
    can be written into an isolated ``CODEX_HOME``.

    Server-name validation, transport selection (command vs url), and the
    ``default_tools_approval_mode``/``experimental_use_rmcp_client`` rules
    mirror the ``-c`` override helper exactly.
    """
    if not mcp_servers:
        return ""
    rendered_blocks: list[str] = []
    has_url_server = False
    for name, server in mcp_servers.items():
        name_str = str(name)
        if not _BARE_TOML_KEY_RE.fullmatch(name_str):
            _logger.warning(
                "Skipping Codex MCP server %r: name is not a TOML bare key "
                "(allowed chars: A-Z, a-z, 0-9, '_', '-'); Codex -c overrides "
                "cannot address it reliably.",
                name_str,
            )
            continue
        if not isinstance(server, dict):
            continue
        command = server.get("command")
        url = server.get("url")
        section_header = f"[mcp_servers.{name_str}]"
        lines: list[str] = [section_header]
        env_block: str | None = None
        if isinstance(command, str) and command:
            lines.append(f"command = {_toml_quote(command)}")
            args = server.get("args")
            if isinstance(args, list) and args:
                rendered = ", ".join(_toml_quote(str(a)) for a in args)
                lines.append(f"args = [{rendered}]")
            env = server.get("env")
            if isinstance(env, dict) and env:
                env_lines = [f"[mcp_servers.{name_str}.env]"]
                for k, v in env.items():
                    env_lines.append(
                        f"{_toml_inline_key(str(k))} = {_toml_quote(str(v))}"
                    )
                env_block = "\n".join(env_lines)
        elif isinstance(url, str) and url:
            lines.append(f"url = {_toml_quote(url)}")
            bearer = server.get("bearer_token_env_var")
            if isinstance(bearer, str) and bearer:
                lines.append(
                    f"bearer_token_env_var = {_toml_quote(bearer)}"
                )
            has_url_server = True
        else:
            _logger.warning(
                "Skipping Codex MCP server %r: missing both `command` and `url`; "
                "Codex requires one of them to launch a server.",
                name_str,
            )
            continue
        lines.append('default_tools_approval_mode = "approve"')
        block = "\n".join(lines)
        if env_block:
            block = f"{block}\n\n{env_block}"
        rendered_blocks.append(block)

    if not rendered_blocks:
        return ""

    body = "\n\n".join(rendered_blocks) + "\n"
    if has_url_server:
        body = "experimental_use_rmcp_client = true\n\n" + body
    return body


def _codex_mcp_server_overrides(
    mcp_servers: dict[str, dict[str, object]] | None,
) -> list[str]:
    """Render ``mcp_servers`` as Codex ``-c`` argv overrides.

    Each server becomes a group of ``-c mcp_servers.<name>.<field>=<toml>`` pairs
    so that ``codex exec`` loads them without touching ``~/.codex/config.toml``.
    ``default_tools_approval_mode="approve"`` is forced because Codex runs with
    ``-a never`` but MCP tool calls otherwise default to Auto/Prompt approval.

    Server names must be valid TOML bare keys (``[A-Za-z0-9_-]+``); any other
    name is skipped with a warning because Codex's ``-c`` parser does not honor
    quoted key segments in dotted paths.

    Both stdio (``command``+``args``) and streamable HTTP/SSE (``url``) shapes
    are supported. When at least one URL-based server is rendered Codex's
    ``experimental_use_rmcp_client`` flag is enabled, since the legacy MCP
    client does not connect to remote transports.
    """
    if not mcp_servers:
        return []
    overrides: list[str] = []
    has_url_server = False
    for name, server in mcp_servers.items():
        name_str = str(name)
        if not _BARE_TOML_KEY_RE.fullmatch(name_str):
            _logger.warning(
                "Skipping Codex MCP server %r: name is not a TOML bare key "
                "(allowed chars: A-Z, a-z, 0-9, '_', '-'); Codex -c overrides "
                "cannot address it reliably.",
                name_str,
            )
            continue
        if not isinstance(server, dict):
            continue
        command = server.get("command")
        url = server.get("url")
        prefix = f"mcp_servers.{name_str}"
        if isinstance(command, str) and command:
            overrides += ["-c", f"{prefix}.command={_toml_quote(command)}"]
            args = server.get("args")
            if isinstance(args, list) and args:
                rendered = ", ".join(_toml_quote(str(a)) for a in args)
                overrides += ["-c", f"{prefix}.args=[{rendered}]"]
            env = server.get("env")
            if isinstance(env, dict) and env:
                rendered_env = ", ".join(
                    f"{_toml_inline_key(str(k))} = {_toml_quote(str(v))}"
                    for k, v in env.items()
                )
                overrides += ["-c", f"{prefix}.env={{ {rendered_env} }}"]
        elif isinstance(url, str) and url:
            overrides += ["-c", f"{prefix}.url={_toml_quote(url)}"]
            bearer = server.get("bearer_token_env_var")
            if isinstance(bearer, str) and bearer:
                overrides += [
                    "-c",
                    f"{prefix}.bearer_token_env_var={_toml_quote(bearer)}",
                ]
            has_url_server = True
        else:
            _logger.warning(
                "Skipping Codex MCP server %r: missing both `command` and `url`; "
                "Codex requires one of them to launch a server.",
                name_str,
            )
            continue
        overrides += [
            "-c",
            f'{prefix}.default_tools_approval_mode="approve"',
        ]
    if has_url_server:
        overrides = ["-c", "experimental_use_rmcp_client=true"] + overrides
    return overrides


def codex_isolated_home(worktree_path: Path) -> Path:
    """Return the per-worktree isolated ``CODEX_HOME`` directory.

    Non-interactive Codex sessions point ``CODEX_HOME`` at this directory so
    they only see the MCP servers the orchestrator wrote into
    ``<dir>/config.toml``. The directory is created by the orchestrator, not
    by the adapter — adapters stay pure transport.
    """
    return worktree_path / ".spec-codex-home"


def claude_isolated_home(worktree_path: Path) -> Path:
    """Return the per-worktree isolated ``HOME`` for Claude Code.

    Containerized non-interactive Claude sessions point ``HOME`` at this
    directory so Claude can read a copied ``.claude.json`` through the
    workspace mount without exposing the operator's full home directory.
    """
    return worktree_path / ".spec-claude-home"


def _codex_linux_sandbox_overrides() -> list[str]:
    """Return Codex config overrides needed for Linux sandbox compatibility."""
    return []


def _read_gitdir_file(path: Path) -> Path | None:
    """Resolve a Git ``.git`` file or ``commondir`` file path."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        text = text[len("gitdir:") :].strip()
    if not text:
        return None
    resolved = Path(text)
    if not resolved.is_absolute():
        resolved = path.parent / resolved
    return resolved.resolve()


def _codex_git_metadata_dirs(worktree_path: Path) -> list[Path]:
    """Return Git metadata dirs Codex needs to run ``git add``/``git commit``.

    Linked worktrees keep a ``.git`` file in the checkout that points at
    ``<common-git-dir>/worktrees/<name>``. The index lives in that per-worktree
    gitdir, while objects and refs live in the common gitdir. Codex runs with a
    workspace sandbox rooted at the worktree, so both metadata locations must be
    added explicitly.
    """
    dot_git = worktree_path / ".git"
    metadata_dirs: list[Path] = []
    if dot_git.is_dir():
        gitdir = dot_git.resolve()
    elif dot_git.is_file():
        gitdir = _read_gitdir_file(dot_git)
    else:
        gitdir = None

    if gitdir is None:
        return []

    metadata_dirs.append(gitdir)
    commondir_file = gitdir / "commondir"
    common_dir = _read_gitdir_file(commondir_file) if commondir_file.is_file() else None
    if common_dir is not None:
        metadata_dirs.append(common_dir)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in metadata_dirs:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def _codex_add_dir_args(paths: list[Path]) -> list[str]:
    args: list[str] = []
    for path in paths:
        args += ["--add-dir", str(path)]
    return args


def _codex_writable_roots_override(paths: list[Path]) -> list[str]:
    """Return a Codex config override for workspace-write writable roots.

    Codex 0.130 records top-level ``--add-dir`` arguments in argv but does not
    project them into the active ``codex exec`` sandbox policy. Keep
    ``--add-dir`` for CLI compatibility, and also set the lower-level config
    field that appears in the persisted active sandbox policy.
    """
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    if not unique:
        return []
    rendered = ", ".join(_toml_quote(str(path)) for path in unique)
    return ["-c", f"sandbox_workspace_write.writable_roots=[{rendered}]"]


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCapabilities:
    """Describes what an agent runtime supports."""

    name: str
    supports_stream_json: bool = False
    supports_mcp: bool = False
    supports_add_dir: bool = True
    supports_dangerously_skip_permissions: bool = False
    supports_network_access: bool = False
    supports_json_output: bool = False
    review_output_on_stdout: bool = False


# ---------------------------------------------------------------------------
# Protocol (the boundary contract)
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentAdapter(Protocol):
    """Abstract agent operations required by the Spec Butler orchestrator.

    Each supported agent runtime (Claude Code, Codex CLI, etc.) must
    implement this protocol.
    """

    @property
    def name(self) -> str:
        """Agent identifier (e.g., 'claude', 'codex')."""
        ...

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the agent's capability profile."""
        ...

    def build_implement_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path,
        stream_json: bool = False,
        mcp_config_path: Path | None = None,
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        """Build the shell command to launch the agent for an implement phase."""
        ...

    def build_authoring_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path | None = None,
        mcp_config_path: Path | None = None,
        initial_prompt: str = "",
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        """Build the shell command to launch the agent for spec authoring."""
        ...

    def build_review_command(
        self,
        *,
        prompt: str,
        output_path: Path,
        schema_path: Path | None = None,
        mcp_config_path: Path | None = None,
        writable_temp_dir: Path | None = None,
    ) -> list[str]:
        """Build the shell command to run an independent code review.

        The agent should produce JSON matching the review schema.
        If ``capabilities.review_output_on_stdout`` is True, the orchestrator
        captures stdout and writes it to *output_path*.  Otherwise the agent
        is expected to write to *output_path* itself.

        When ``mcp_config_path`` is provided, MCP-capable agents that take a
        config file (Claude) constrain their MCP toolbox to the contents of
        that file via ``--strict-mcp-config``. Adapters that load MCP via
        other means may ignore this argument.
        """
        ...


# ---------------------------------------------------------------------------
# Claude Code implementation
# ---------------------------------------------------------------------------


class ClaudeAgent:
    """Agent adapter for Anthropic's Claude Code CLI."""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            name="claude",
            supports_stream_json=True,
            supports_mcp=True,
            supports_add_dir=True,
            supports_dangerously_skip_permissions=True,
            review_output_on_stdout=True,
        )

    def build_implement_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path,
        stream_json: bool = False,
        mcp_config_path: Path | None = None,
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        del mcp_servers  # Claude consumes MCP via the pre-written mcp_config_path file.
        cmd = ["claude", "-p"]
        if stream_json:
            cmd += ["--output-format", "stream-json", "--verbose"]
        cmd += ["--dangerously-skip-permissions"]
        cmd += ["--add-dir", str(state_dir)]
        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
        cmd += ["--", prompt]
        return cmd

    def build_authoring_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path | None = None,
        mcp_config_path: Path | None = None,
        initial_prompt: str = "",
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        del mcp_servers  # Claude consumes MCP via the pre-written mcp_config_path file.
        cmd = ["claude", "--dangerously-skip-permissions"]
        if state_dir:
            cmd += ["--add-dir", str(state_dir)]
        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
        cmd += ["--append-system-prompt", prompt]
        if initial_prompt:
            cmd.append(initial_prompt)
        return cmd

    def build_review_command(
        self,
        *,
        prompt: str,
        output_path: Path,
        schema_path: Path | None = None,
        mcp_config_path: Path | None = None,
        writable_temp_dir: Path | None = None,
    ) -> list[str]:
        del output_path, schema_path, writable_temp_dir  # Claude streams review JSON to stdout.
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
        cmd.append(prompt)
        return cmd


# ---------------------------------------------------------------------------
# Codex implementation
# ---------------------------------------------------------------------------


class CodexAgent:
    """Agent adapter for OpenAI's Codex CLI."""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            name="codex",
            supports_stream_json=False,
            supports_mcp=True,
            supports_add_dir=True,
            supports_network_access=True,
            supports_json_output=True,
            review_output_on_stdout=False,
        )

    def build_implement_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path,
        stream_json: bool = False,
        mcp_config_path: Path | None = None,
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        del mcp_config_path  # Codex loads MCP via `-c mcp_servers.*` overrides, not a config file.
        writable_roots = [state_dir, *_codex_git_metadata_dirs(worktree_path)]
        cmd = [
            "codex",
            "-a",
            "never",
            "-s",
            "workspace-write",
            *_codex_add_dir_args(writable_roots),
            "exec",
            "--json",
            "-c",
            "sandbox_workspace_write.network_access=true",
            *_codex_writable_roots_override(writable_roots),
        ]
        cmd += _codex_linux_sandbox_overrides()
        cmd += _codex_mcp_server_overrides(mcp_servers)
        cmd.append(prompt)
        return cmd

    def build_authoring_command(
        self,
        *,
        prompt: str,
        worktree_path: Path,
        state_dir: Path | None = None,
        mcp_config_path: Path | None = None,
        initial_prompt: str = "",
        mcp_servers: dict[str, dict[str, object]] | None = None,
    ) -> list[str]:
        del mcp_config_path  # Codex loads MCP via `-c mcp_servers.*` overrides, not a config file.
        # Authoring/operator-input sessions are interactive by design: unlike
        # unattended implement runs, the agent may ask for approval when a
        # resolution needs writes outside the pre-registered roots.
        cmd = [
            "codex",
            "-a",
            "on-request",
            "-s",
            "workspace-write",
        ]
        add_dirs = []
        if state_dir:
            add_dirs.append(state_dir)
        add_dirs.extend(_codex_git_metadata_dirs(worktree_path))
        cmd += _codex_add_dir_args(add_dirs)
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += _codex_writable_roots_override(add_dirs)
        cmd += _codex_linux_sandbox_overrides()
        cmd += _codex_mcp_server_overrides(mcp_servers)
        combined_prompt = f"{prompt}\n\n{initial_prompt}" if initial_prompt else prompt
        cmd.append(combined_prompt)
        return cmd

    def build_review_command(
        self,
        *,
        prompt: str,
        output_path: Path,
        schema_path: Path | None = None,
        mcp_config_path: Path | None = None,
        writable_temp_dir: Path | None = None,
    ) -> list[str]:
        del mcp_config_path  # Codex non-interactive isolation uses CODEX_HOME, not a config file path.
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "-s",
            "read-only",
        ]
        cmd += _codex_linux_sandbox_overrides()
        if writable_temp_dir is not None:
            # Keep the checkout read-only while allowing test runners to use
            # one disposable scratch root for temporary files.
            cmd += ["--add-dir", str(writable_temp_dir)]
        if schema_path:
            cmd += ["--output-schema", str(schema_path)]
        cmd += ["-o", str(output_path), prompt]
        return cmd


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------

_AGENT_REGISTRY: dict[str, AgentAdapter] = {}


def _default_registry() -> dict[str, AgentAdapter]:
    return {
        "claude": ClaudeAgent(),
        "codex": CodexAgent(),
    }


def get_agent_adapter(agent_name: str) -> AgentAdapter:
    """Return the agent adapter for the given agent name.

    Raises ``ValueError`` if the agent is not registered.
    """
    if not _AGENT_REGISTRY:
        _AGENT_REGISTRY.update(_default_registry())
    adapter = _AGENT_REGISTRY.get(agent_name)
    if adapter is None:
        available = ", ".join(sorted(_AGENT_REGISTRY))
        raise ValueError(f"Unknown agent: {agent_name!r}. Available: {available}")
    return adapter


def register_agent_adapter(name: str, adapter: AgentAdapter) -> None:
    """Register a custom agent adapter (useful for testing or extensions)."""
    if not _AGENT_REGISTRY:
        _AGENT_REGISTRY.update(_default_registry())
    _AGENT_REGISTRY[name] = adapter
