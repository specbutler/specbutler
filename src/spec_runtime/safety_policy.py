"""Provider launch policy for ``[execution].safety_mode``.

``native`` (the default) runs unattended implement and recovery sessions with
no approval prompts under the provider's own sandbox:

* Claude runs in ``bypassPermissions`` mode with its Bash sandbox on,
  ``failIfUnavailable`` and ``allowUnsandboxedCommands: false``. The file
  tools are not covered by the OS sandbox, so a PreToolUse hook keeps
  Edit/Write/MultiEdit/NotebookEdit inside the workspace and the launch's
  outbox.
* Codex runs ``codex exec`` with ``-a never`` (or ``on-request`` routed to
  the model reviewer when ``[execution.codex].auto_review`` is on).

``isolated`` is the container backend, whose launches are unchanged.

This module stays free of orchestrator imports so adapters and doctor can use
it.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExecutionNetworkConfig

CLAUDE_WRITE_GUARD_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
CLAUDE_BYPASS_PERMISSION_MODE = "bypassPermissions"
CLAUDE_FALLBACK_PERMISSION_MODE = "acceptEdits"
CODEX_AUTO_REVIEWER = "auto_review"

# Flags an unattended native Claude launch passes. ``bypassPermissions`` must
# be one of the advertised ``--permission-mode`` choices.
_CLAUDE_NATIVE_REQUIRED_FLAGS = (
    "--permission-mode",
    "--settings",
    "--setting-sources",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--allowedTools",
    "--tools",
    "--add-dir",
)


@dataclass(frozen=True)
class LaunchSafety:
    """Resolved safety controls for one unattended provider launch."""

    # True when Claude may run in bypassPermissions mode. False keeps the
    # restricted acceptEdits launch (isolated mode, or an older CLI).
    claude_bypass: bool = False
    codex_auto_review: bool = False
    network: ExecutionNetworkConfig = field(default_factory=ExecutionNetworkConfig)


def claude_native_unavailability_reason(help_text: str) -> str:
    """Return why the installed Claude CLI cannot run a native launch."""
    missing = [flag for flag in _CLAUDE_NATIVE_REQUIRED_FLAGS if flag not in help_text]
    if CLAUDE_BYPASS_PERMISSION_MODE not in help_text:
        missing.append(f"--permission-mode {CLAUDE_BYPASS_PERMISSION_MODE}")
    if not missing:
        return ""
    return (
        "The installed Claude CLI does not advertise "
        f"{', '.join(missing)}, so unattended sessions fall back to the "
        "restricted acceptEdits launch (no network beyond the built-in "
        "allowlist, and file tools confined to the working directories). "
        "Upgrade Claude Code with `npm install -g @anthropic-ai/claude-code`."
    )


def claude_sandbox_network(
    network: ExecutionNetworkConfig,
    *,
    bypass: bool,
) -> dict[str, object]:
    """Render ``[execution.network]`` as Claude ``sandbox.network`` settings.

    In ``open`` mode a bypassPermissions session reaches unlisted hosts
    without a prompt. A session that can prompt (interactive authoring) or
    must not (the acceptEdits fallback, where an unanswered prompt is a
    denial) keeps the allowlist as its pre-approved set.
    """
    rendered: dict[str, object] = {}
    if network.mode == "allowlist":
        rendered["allowedDomains"] = list(network.allowed_domains)
        rendered["strictAllowlist"] = True
    elif not bypass:
        rendered["allowedDomains"] = list(network.allowed_domains)
    if network.denied_domains:
        rendered["deniedDomains"] = list(network.denied_domains)
    return rendered


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_profile_network(
    network: ExecutionNetworkConfig | None,
    *,
    enabled: bool = True,
) -> str:
    """Render ``[execution.network]`` as a Codex permission-profile table.

    Open mode without a deny list is plain network access. An allowlist or a
    deny list routes traffic through Codex's network proxy with per-domain
    ``allow``/``deny`` entries.
    """
    if not enabled:
        return "{enabled=false}"
    entries = ['enabled=true', 'mode="full"', "allow_local_binding=true"]
    policy = network or ExecutionNetworkConfig()
    domains: dict[str, str] = {}
    if policy.mode == "allowlist":
        domains.update({domain: "allow" for domain in policy.allowed_domains})
    for domain in policy.denied_domains:
        domains[domain] = "deny"
    if domains:
        rendered = ",".join(
            f"{_toml_string(domain)}={_toml_string(action)}"
            for domain, action in domains.items()
        )
        entries.append("domains={" + rendered + "}")
    return "{" + ",".join(entries) + "}"


def codex_unattended_approval_args(auto_review: bool) -> list[str]:
    """Return Codex approval flags for a launch nobody can answer."""
    if auto_review:
        return [
            "-a",
            "on-request",
            "-c",
            f'approvals_reviewer="{CODEX_AUTO_REVIEWER}"',
        ]
    return ["-a", "never"]


def claude_write_guard_script() -> Path:
    """Return the stdlib-only hook script shipped with this package."""
    return Path(__file__).resolve().with_name("claude_write_guard.py")


def claude_write_guard_command(
    allowed_roots: list[Path] | tuple[Path, ...],
    denied_paths: list[Path] | tuple[Path, ...] = (),
    *,
    python: str | None = None,
) -> str:
    """Build the shell command Claude runs for the PreToolUse write guard.

    ``-I`` keeps the interpreter from importing anything from the agent's
    working directory or environment.
    """
    argv = [python or sys.executable, "-I", str(claude_write_guard_script())]
    for root in sorted({str(Path(root)) for root in allowed_roots}):
        argv += ["--allow", root]
    for path in sorted({str(Path(path)) for path in denied_paths}):
        argv += ["--deny", path]
    return shlex.join(argv)


def claude_write_guard_hooks(
    allowed_roots: list[Path] | tuple[Path, ...],
    denied_paths: list[Path] | tuple[Path, ...] = (),
    *,
    python: str | None = None,
) -> dict[str, object]:
    """Return the ``hooks`` settings block for the write guard."""
    return {
        "PreToolUse": [
            {
                "matcher": "|".join(CLAUDE_WRITE_GUARD_TOOLS),
                "hooks": [
                    {
                        "type": "command",
                        "command": claude_write_guard_command(
                            allowed_roots,
                            denied_paths,
                            python=python,
                        ),
                        "timeout": 30,
                    }
                ],
            }
        ]
    }
