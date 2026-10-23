"""Recognize provider sandbox and permission denials.

A denied write or connection is not a code failure: another attempt with the
same policy meets the same wall. The orchestrator spots denials in the agent's
event stream, names the denied path or host in the run's ``last_error`` with
the ``sandbox_denied`` marker, and ``classify_failure`` treats the failure as
infrastructure (backoff and breaker, no code retry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SANDBOX_DENIED_MARKER = "sandbox_denied"

_OPERATION_NOT_PERMITTED = "operation not permitted"
_TEXT_MARKERS = (
    _OPERATION_NOT_PERMITTED,
    "blocked by sandbox",
    "sandbox denied",
    "<sandbox_violations>",
    "approval is not supported in exec mode",
    "spec butler write guard:",
    "network access was blocked by policy",
    "request is blocked by network policy",
    "domain denied by the sandbox policy",
    "domain is explicitly denied by policy",
    "domain not in allowlist",
    "domain is not on the allowlist",
)
# A path right before ": Operation not permitted" (``touch: /etc/x: ...``,
# ``open '/etc/x': ...``), or after the write guard's "may not write".
_PATH_BEFORE_EPERM_RE = re.compile(
    r"""['"]?((?:~|/)[^\s'"`:]*)['"]?\s*:\s*operation not permitted""",
    re.IGNORECASE,
)
_GUARD_PATH_RE = re.compile(r"may not (?:write|modify) (\S+?)(?::|\s|$)")
_HOST_RE = re.compile(
    r"(?:https?://)?((?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)(?::\d+)?",
    re.IGNORECASE,
)
_NETWORK_HINTS = ("network", "domain", "allowlist", "connection", "host")
_DENIED_SUMMARY_RE = re.compile(r"sandbox denied ([^()]+?) \(" + SANDBOX_DENIED_MARKER + r"\)")


@dataclass(frozen=True)
class SandboxDenial:
    # "write", "network", "permission" (a tool the policy refused) or
    # "approval" (an escalation nobody could approve).
    kind: str
    target: str = ""
    tool: str = ""

    def describe(self) -> str:
        label = {
            "write": "write to",
            "network": "network access to",
            "permission": f"{self.tool or 'tool'} call on",
            "approval": "escalation for",
        }.get(self.kind, self.kind)
        return f"{label} {self.target}" if self.target else label


def is_sandbox_denial_text(text: object) -> bool:
    """Return True when *text* carries a sandbox or permission denial."""
    lowered = str(text or "").lower()
    return SANDBOX_DENIED_MARKER in lowered or any(
        marker in lowered for marker in _TEXT_MARKERS
    )


def denial_from_text(text: object, *, tool: str = "") -> SandboxDenial | None:
    """Return the denial described in a tool result or error message."""
    raw = str(text or "")
    lowered = raw.lower()
    if not any(marker in lowered for marker in _TEXT_MARKERS):
        return None
    guard = _GUARD_PATH_RE.search(raw)
    if "spec butler write guard:" in lowered and guard:
        return SandboxDenial("write", guard.group(1), tool)
    eperm = _PATH_BEFORE_EPERM_RE.search(raw)
    if eperm:
        return SandboxDenial("write", eperm.group(1), tool)
    if "approval is not supported in exec mode" in lowered:
        return SandboxDenial("approval", "", tool)
    if any(hint in lowered for hint in _NETWORK_HINTS):
        host = _HOST_RE.search(raw)
        return SandboxDenial("network", host.group(1).lower() if host else "", tool)
    return SandboxDenial("write" if _OPERATION_NOT_PERMITTED in lowered else "permission", "", tool)


def _tool_input_target(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "notebook_path", "path", "url", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return ""


def claude_event_denials(event: object) -> list[SandboxDenial]:
    """Return denials in one Claude ``stream-json`` event.

    The final ``result`` event lists refused tool calls in
    ``permission_denials``; sandbox and write-guard refusals arrive as error
    ``tool_result`` blocks.
    """
    if not isinstance(event, dict):
        return []
    denials: list[SandboxDenial] = []
    if event.get("type") == "result":
        for entry in event.get("permission_denials") or ():
            if not isinstance(entry, dict):
                continue
            tool = str(entry.get("tool_name", "") or "")
            denials.append(
                SandboxDenial(
                    "permission",
                    _tool_input_target(entry.get("tool_input")),
                    tool,
                )
            )
        return denials
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        if not item.get("is_error"):
            text = _result_text(item.get("content"))
            # Bash output can report a sandbox refusal with exit 0 noise.
            if "<sandbox_violations>" not in text.lower():
                continue
        denial = denial_from_text(_result_text(item.get("content")))
        if denial is not None:
            denials.append(denial)
    return denials


def codex_event_denials(event: object) -> list[SandboxDenial]:
    """Return denials in one ``codex exec --json`` event."""
    if not isinstance(event, dict):
        return []
    texts: list[str] = []
    if event.get("type") == "error":
        texts.append(str(event.get("message", "")))
    item = event.get("item")
    if isinstance(item, dict) and event.get("type") in ("item.completed", "item.failed"):
        if item.get("type") == "command_execution" and item.get("status") != "completed":
            texts.append(str(item.get("aggregated_output", "")))
        elif item.get("type") == "error":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "command_execution":
            output = str(item.get("aggregated_output", ""))
            if _OPERATION_NOT_PERMITTED in output.lower() or "blocked by" in output.lower():
                texts.append(output)
    denials = []
    for text in texts:
        denial = denial_from_text(text)
        if denial is not None:
            denials.append(denial)
    return denials


def sandbox_denial_summary(message: object) -> str:
    """Return the denied path or host recorded in a failure message."""
    raw = str(message or "")
    match = _DENIED_SUMMARY_RE.search(raw.lower())
    if match:
        start = match.start(1)
        return raw[start : start + len(match.group(1))].strip()
    denial = denial_from_text(raw)
    return denial.describe() if denial is not None else ""
