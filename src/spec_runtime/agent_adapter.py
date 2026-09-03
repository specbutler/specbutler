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

from .agent_git_isolation import AgentGitIsolation
from .provider_env import PROXY_ENV_KEYS, protected_operator_paths

_logger = logging.getLogger(__name__)

# TOML-spec bare key: ASCII letters, digits, underscores, and dashes.
# Codex's `-c` dotted-key parser accepts these unquoted but does not accept
# quoted segments (e.g. `mcp_servers."foo bar".command=...` registers the
# literal name `"foo bar"` rather than `foo bar`), so names outside this
# character set cannot be reliably delivered via `-c` overrides.
_BARE_TOML_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_CODEX_IMPLEMENT_PERMISSION_PROFILE = "specbutler-implement"
_CODEX_AUTHORING_PERMISSION_PROFILE = "specbutler-authoring"
_CODEX_PROVIDER_AUTH_ENV_KEYS = (
    "CODEX_API_KEY",
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT_ID",
)


class HostAgentUnavailableError(RuntimeError):
    """Raised before an agent whose host isolation is unavailable is launched."""


def claude_restricted_mode_unavailability_reason(help_text: str) -> str:
    """Return an upgrade error when Claude lacks required isolation flags."""
    required = (
        "--restricted",
        "--safe-mode",
        "--permission-mode",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--setting-sources",
        "--settings",
    )
    missing = [flag for flag in required if flag not in help_text]
    if not missing:
        return ""
    return (
        "The installed Claude CLI lacks isolation controls Spec Butler requires "
        f"({', '.join(missing)}). Upgrade Claude Code with `npm install -g "
        "@anthropic-ai/claude-code`, then rerun `spec doctor`."
    )


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


def codex_isolation_unavailability_reason(
    exec_help_text: str,
    sandbox_help_text: str,
) -> str:
    """Return an upgrade error when Codex lacks enforced launch controls."""
    required_exec = (
        "--add-dir",
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--output-schema",
        "--strict-config",
    )
    required_sandbox = ("--permission-profile",)
    missing = [flag for flag in required_exec if flag not in exec_help_text]
    missing.extend(
        flag for flag in required_sandbox if flag not in sandbox_help_text
    )
    if not missing:
        return ""
    return (
        "The installed Codex CLI lacks isolation controls Spec Butler requires "
        f"({', '.join(missing)}). Upgrade Codex with `npm install -g "
        "@openai/codex`, then rerun `spec doctor`."
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

    The output is the same set of servers supported by the interactive
    ``_codex_mcp_server_overrides`` helper, but serialized as fully-formed TOML
    so non-interactive sessions can load it from an isolated ``CODEX_HOME``
    without exposing server credentials in process arguments.

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
    """Return the isolated ``CODEX_HOME`` below a caller-supplied root.

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


def _codex_git_metadata_dirs(
    worktree_path: Path,
    git_isolation: AgentGitIsolation | None = None,
) -> list[Path]:
    """Return only private external Git paths needed for local commits."""
    dot_git = worktree_path / ".git"
    # A full clone's metadata is already below the workspace root. No external
    # writable root is needed (notably for clone/container execution).
    if dot_git.is_dir():
        return []
    if not dot_git.exists():
        return []
    if git_isolation is None:
        raise RuntimeError(
            "Refusing linked-worktree Git metadata access without a prepared "
            "private Git directory"
        )
    if git_isolation.worktree != worktree_path.resolve(strict=True):
        raise RuntimeError("Private Git metadata belongs to a different worktree")
    return list(git_isolation.writable_paths)


def _codex_add_dir_args(paths: list[Path]) -> list[str]:
    args: list[str] = []
    for path in paths:
        args += ["--add-dir", str(path)]
    return args


def _codex_implement_permission_overrides(
    worktree_path: Path,
    writable_roots: list[Path],
    *,
    excluded_env_keys: set[str] | None = None,
    provider_home: Path | None = None,
    profile_name: str = _CODEX_IMPLEMENT_PERMISSION_PROFILE,
    network_enabled: bool = True,
    ignore_rules: bool = True,
    strict_config: bool = True,
    additional_protected_paths: tuple[Path, ...] = (),
) -> list[str]:
    """Build the host implementation policy around explicit path classes.

    The model may read system/project dependencies and write its checkout,
    Git metadata, and completion outbox. Provider/operator credential stores
    and Linux procfs stay inaccessible even though the Codex parent itself
    needs provider authentication.
    """
    protected = {
        *(path.resolve(strict=False) for path in protected_operator_paths()),
        (provider_home or codex_isolated_home(worktree_path)).resolve(strict=False),
        *(path.resolve(strict=False) for path in additional_protected_paths),
    }
    writable = {
        worktree_path.resolve(strict=False),
        *(path.resolve(strict=False) for path in writable_roots),
    }
    filesystem_entries = [
        '":root"="read"',
        '":workspace_roots"="write"',
        *(
            f"{_toml_quote(str(path))}=\"write\""
            for path in sorted(writable, key=str)
        ),
        *(
            f"{_toml_quote(str(path))}=\"deny\""
            for path in sorted(protected, key=str)
        ),
    ]
    all_excluded = {
        *_CODEX_PROVIDER_AUTH_ENV_KEYS,
        *PROXY_ENV_KEYS,
        *(excluded_env_keys or set()),
    }
    excluded = ", ".join(_toml_quote(key) for key in sorted(all_excluded))
    prefix: list[str] = []
    if strict_config:
        prefix.append("--strict-config")
    if ignore_rules:
        prefix.append("--ignore-rules")
    return [
        *prefix,
        "-c",
        f'default_permissions="{profile_name}"',
        "-c",
        (
            f"permissions.{profile_name}.filesystem="
            "{" + ",".join(filesystem_entries) + "}"
        ),
        "-c",
        (
            f"permissions.{profile_name}.network="
            + (
                '{enabled=true,mode="full",allow_local_binding=true}'
                if network_enabled
                else "{enabled=false}"
            )
        ),
        "-c",
        f"shell_environment_policy.exclude=[{excluded}]",
        "-c",
        "allow_login_shell=false",
    ]


def _codex_mcp_secret_env_keys(
    mcp_servers: dict[str, dict[str, object]] | None,
) -> set[str]:
    """Find parent-env keys referenced by explicitly allowed MCP servers."""
    keys: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            bearer = value.get("bearer_token_env_var")
            if isinstance(bearer, str) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", bearer
            ):
                keys.add(bearer)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str):
            keys.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value))

    visit(mcp_servers or {})
    return keys


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
    supports_network_access: bool = False
    supports_json_output: bool = False
    review_output_on_stdout: bool = False
    # Custom providers must opt individual parent-environment names into the
    # otherwise minimal child environment. Built-in Claude/Codex authentication
    # remains covered by their provider-specific allowlists.
    provider_environment_keys: frozenset[str] = frozenset()


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
        externally_sandboxed: bool = False,
        provider_home: Path | None = None,
        git_isolation: AgentGitIsolation | None = None,
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
        protected_env_keys: set[str] | frozenset[str] | None = None,
        git_isolation: AgentGitIsolation | None = None,
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
        externally_sandboxed: bool = False,
        provider_home: Path | None = None,
        git_isolation: AgentGitIsolation | None = None,
    ) -> list[str]:
        # Claude consumes MCP via the pre-written mcp_config_path file. The
        # explicit settings guard applies with either host or outer-container
        # OS isolation, so command construction is intentionally identical.
        del externally_sandboxed, provider_home, git_isolation
        cmd = ["claude", "-p"]
        if stream_json:
            cmd += ["--output-format", "stream-json", "--verbose"]
        tools = "Read,Write,Edit,Bash,Glob,Grep"
        mcp_tools = [
            f"mcp__{server_name}__*"
            for server_name in sorted(mcp_servers or {})
        ]
        allowed_tools = ",".join((tools, *mcp_tools))
        cmd += [
            "--restricted",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            tools,
            "--allowedTools",
            allowed_tools,
            "--setting-sources",
            "",
            "--no-session-persistence",
        ]
        # Even when the container supplies the OS sandbox, Claude still owns
        # the provider-aware credential filtering in this orchestrator-written
        # file. Restricted/safe mode ignore repository and user settings while
        # continuing to honor this explicit settings source.
        cmd += [
            "--settings",
            str(worktree_path / ".claude" / "settings.local.json"),
        ]
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
        protected_env_keys: set[str] | frozenset[str] | None = None,
        git_isolation: AgentGitIsolation | None = None,
    ) -> list[str]:
        del mcp_servers, protected_env_keys, git_isolation
        # Authoring is interactive: edits remain frictionless, while shell and
        # other side effects retain Claude's normal operator approval prompt.
        # The explicit settings file keeps the repository sandbox/credential
        # policy at highest precedence without suppressing the operator's
        # trusted user configuration.
        cmd = [
            "claude",
            "--permission-mode",
            "acceptEdits",
            "--settings",
            str(worktree_path / ".claude" / "settings.local.json"),
        ]
        if state_dir:
            cmd += ["--add-dir", str(state_dir)]
        if mcp_config_path:
            # Interactive authoring intentionally keeps user-registered MCP
            # servers in addition to the orchestrator-provided set.
            cmd += ["--mcp-config", str(mcp_config_path)]
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
        # Review runs against an untrusted PR checkout. Restricted mode is a
        # CLI-enforced boundary: it ignores repository/user settings and
        # hooks, refuses bypassPermissions, and removes command/code-running
        # tools. Gate results and the exact diff are supplied by the
        # orchestrator, so the reviewer only needs read-only file inspection.
        del prompt, output_path, schema_path, mcp_config_path
        cmd = [
            "claude",
            "-p",
            "--restricted",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Glob,Grep",
            "--allowedTools",
            "Read,Glob,Grep",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if writable_temp_dir is not None:
            # Restricted mode treats add-dir as an additional file-tool root;
            # no write-capable tool is available, so this only exposes the
            # host-materialized review evidence in the scratch directory.
            cmd += ["--add-dir", str(writable_temp_dir)]
        return cmd


# Capabilities supplied by user configuration or evolving Codex defaults must
# not silently enter non-interactive sessions. These overrides retain the core
# shell/edit surface used by implementation and web chat; stricter read-only
# review/TUI callers additionally disable shell_tool and unified_exec.
CODEX_AMBIENT_CAPABILITY_OVERRIDES = (
    "features.apps=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.browser_use_full_cdp_access=false",
    "features.code_mode=false",
    "features.computer_use=false",
    "features.enable_mcp_apps=false",
    "features.hooks=false",
    "features.image_generation=false",
    "features.in_app_browser=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.plugins=false",
    "features.plugin_sharing=false",
    "features.recommended_plugins=false",
    "features.remote_plugin=false",
    "features.shell_snapshot=false",
    "features.shell_snapshot_v2=false",
    "features.skill_mcp_dependency_install=false",
    "features.skip_host_skill_discovery=true",
    "features.view_image=false",
)

_CODEX_CAPABILITY_PROBE_OVERRIDES = (
    'default_permissions="specbutler-preflight"',
    (
        "permissions.specbutler-preflight.filesystem="
        '{":root"="read",":workspace_roots"="write"}'
    ),
    (
        "permissions.specbutler-preflight.network="
        '{enabled=true,mode="full",allow_local_binding=true}'
    ),
    'shell_environment_policy.exclude=["OPENAI_API_KEY"]',
    "shell_environment_policy.inherit=none",
    "allow_login_shell=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.code_mode_host=false",
    *CODEX_AMBIENT_CAPABILITY_OVERRIDES,
)


def codex_capability_probe_command(codex_path: str = "codex") -> list[str]:
    """Build a provider-free strict-config capability probe.

    ``app-server --listen off`` parses the same configuration surface used by
    implementation, review, TUI chat, and browser chat, then exits before any
    model request. Current CLIs report that no transport is configured after
    successfully parsing the controls; a future CLI may instead exit zero.
    """
    return [
        codex_path,
        "app-server",
        "--strict-config",
        "--listen",
        "off",
        *(
            item
            for override in _CODEX_CAPABILITY_PROBE_OVERRIDES
            for item in ("-c", override)
        ),
    ]


def codex_capability_probe_unavailability_reason(
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    """Interpret the model-free strict-config probe result."""
    output = f"{stdout}\n{stderr}".strip()
    if returncode == 0 or "no transport configured" in output.lower():
        return ""
    detail = " ".join(output.split())[:240] or f"exit status {returncode}"
    return (
        "The installed Codex CLI rejected security controls Spec Butler "
        f"requires under strict config ({detail}). Upgrade Codex with `npm "
        "install -g @openai/codex`, then rerun `spec doctor`."
    )


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
        externally_sandboxed: bool = False,
        provider_home: Path | None = None,
        git_isolation: AgentGitIsolation | None = None,
    ) -> list[str]:
        del mcp_config_path, externally_sandboxed
        writable_roots = [
            state_dir,
            *_codex_git_metadata_dirs(worktree_path, git_isolation),
        ]
        cmd = [
            "codex",
            "-a",
            "never",
            *_codex_add_dir_args(writable_roots),
            "exec",
            "--json",
            *_codex_implement_permission_overrides(
                worktree_path,
                writable_roots,
                excluded_env_keys=_codex_mcp_secret_env_keys(mcp_servers),
                provider_home=provider_home,
                additional_protected_paths=(
                    git_isolation.read_only_paths if git_isolation is not None else ()
                ),
            ),
        ]
        for override in CODEX_AMBIENT_CAPABILITY_OVERRIDES:
            cmd += ["-c", override]
        cmd += _codex_linux_sandbox_overrides()
        # The orchestrator writes the complete non-interactive MCP set into
        # the isolated CODEX_HOME/config.toml.  Never duplicate that data in
        # argv: setup manifests may contain literal MCP environment secrets,
        # and command lines are observable through process listings and
        # container inspection/logging.
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
        protected_env_keys: set[str] | frozenset[str] | None = None,
        git_isolation: AgentGitIsolation | None = None,
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
        add_dirs.extend(_codex_git_metadata_dirs(worktree_path, git_isolation))
        cmd += _codex_add_dir_args(add_dirs)
        cmd += _codex_implement_permission_overrides(
            worktree_path,
            add_dirs,
            excluded_env_keys=set(protected_env_keys or ()),
            profile_name=_CODEX_AUTHORING_PERMISSION_PROFILE,
            network_enabled=False,
            ignore_rules=False,
            strict_config=False,
            additional_protected_paths=(
                git_isolation.read_only_paths if git_isolation is not None else ()
            ),
        )
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
        del prompt, mcp_config_path  # Prompt arrives on stdin; review is MCP-free.
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "-s",
            "read-only",
        ]
        if writable_temp_dir is not None:
            cmd += ["-C", str(writable_temp_dir), "--skip-git-repo-check"]
        # Provider credentials are required by the Codex parent process, but
        # must be unreachable from model-controlled tools. Disable every
        # local-execution path and make a future accidental shell re-enable
        # inherit no environment. Strict config turns an older CLI that does
        # not recognize a boundary control into a fail-closed launch error.
        for override in (
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.code_mode_host=false",
            *CODEX_AMBIENT_CAPABILITY_OVERRIDES,
            "shell_environment_policy.inherit=none",
        ):
            cmd += ["-c", override]
        cmd += _codex_linux_sandbox_overrides()
        if schema_path:
            cmd += ["--output-schema", str(schema_path)]
        # Prompt content can exceed Linux MAX_ARG_STRLEN and Windows' command
        # line limit once the host-materialized diff/spec are included. A
        # literal ``-`` makes Codex read the prompt from stdin.
        cmd += ["-o", str(output_path), "-"]
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
