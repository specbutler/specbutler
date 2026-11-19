"""Project-configured commands run on the host at run lifecycle events.

A repository lists commands under ``[hooks]`` in ``.spec.toml``. The
orchestrator runs them from the main checkout with the operator's environment,
never inside an agent workspace, so they can reach an issue tracker, a chat
channel or any other system the project coordinates through.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# In lifecycle order. ``start`` is the only event whose failure stops the run.
EVENTS = ("start", "resumed", "needs_input", "blocked", "finished", "abandoned")
DEFAULT_TIMEOUT_SECONDS = 120.0
_MARKER_FILENAME = "lifecycle-event"
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_FRONTMATTER_REFERENCE_RE = re.compile(r"(?:\{frontmatter_|SPEC_HOOK_FRONTMATTER_)([A-Za-z0-9_]+)")
_OUTPUT_TAIL_CHARS = 1000


def _frontmatter_name(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


class HookConfigError(ValueError):
    """Raised when the ``[hooks]`` table is malformed."""


@dataclass(frozen=True)
class RunHooksConfig:
    commands: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.commands)

    @property
    def frontmatter_keys(self) -> frozenset[str]:
        """Frontmatter keys the commands read, as normalized placeholder names."""
        return frozenset(
            match.group(1).lower()
            for argv in self.commands.values()
            for arg in argv
            for match in _FRONTMATTER_REFERENCE_RE.finditer(arg)
        )


@dataclass(frozen=True)
class HookResult:
    ok: bool
    detail: str = ""


def parse_hooks_section(payload: object) -> RunHooksConfig:
    """Parse ``[hooks]``: ``run_<event>`` commands plus ``timeout_seconds``."""
    if payload is None:
        return RunHooksConfig()
    if not isinstance(payload, dict):
        raise HookConfigError("hooks must be a table")
    known = {f"run_{event}": event for event in EVENTS}
    commands: dict[str, tuple[str, ...]] = {}
    timeout = DEFAULT_TIMEOUT_SECONDS
    for key, value in payload.items():
        if key == "timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise HookConfigError("hooks.timeout_seconds must be a positive number")
            timeout = float(value)
            continue
        if key not in known:
            raise HookConfigError(f"hooks.{key} is not a hook; use one of {', '.join(sorted(known))}")
        if isinstance(value, str):
            try:
                argv = tuple(shlex.split(value))
            except ValueError as exc:
                raise HookConfigError(f"hooks.{key} could not be parsed: {exc}") from exc
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            argv = tuple(value)
        else:
            raise HookConfigError(f"hooks.{key} must be a command string or an array of strings")
        if argv:
            commands[known[key]] = argv
    return RunHooksConfig(commands=commands, timeout_seconds=timeout)


def reads_frontmatter_key(config: RunHooksConfig, key: object) -> bool:
    """Whether a hook command reads this spec frontmatter key."""
    return _frontmatter_name(key) in config.frontmatter_keys


def last_event(run_dir: Path) -> str:
    """The last lifecycle event recorded for a run, or ``""``."""
    try:
        return (run_dir / _MARKER_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def record_event(run_dir: Path, event: str) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / _MARKER_FILENAME).write_text(event + "\n", encoding="utf-8")
    except OSError:
        pass


def hook_values(
    *,
    event: str,
    spec_id: str,
    run_id: str,
    branch: str,
    state: str,
    phase: str,
    run_mode: str,
    frontmatter: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Values a hook receives, keyed by placeholder name."""
    values = {
        "event": event,
        "spec_id": spec_id,
        "run_id": run_id,
        "branch": branch,
        "state": state,
        "phase": phase,
        "run_mode": run_mode,
    }
    for key, value in (frontmatter or {}).items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        name = _frontmatter_name(key)
        if name:
            values[f"frontmatter_{name}"] = str(value)
    return values


def run_hook(
    config: RunHooksConfig,
    event: str,
    values: Mapping[str, str],
    *,
    cwd: Path,
    message: str = "",
    redact: Callable[[str], str] = lambda text: text,
) -> HookResult | None:
    """Run the hook configured for ``event``; ``None`` when there is none.

    ``{name}`` placeholders in each argument are replaced by ``values``; the
    same values are exported as ``SPEC_HOOK_<NAME>``. ``message`` is written to
    a temporary file passed as ``{message_file}`` / ``SPEC_HOOK_MESSAGE_FILE``
    so free text never becomes part of a command line.
    """
    argv = config.commands.get(event)
    if not argv:
        return None
    name = f"run_{event}"
    handle = tempfile.NamedTemporaryFile(
        "w", prefix=f"spec-hook-{event}-", suffix=".md", encoding="utf-8", delete=False
    )
    message_file = Path(handle.name)
    try:
        with handle:
            handle.write(redact(message).strip() + "\n")
        resolved = {**values, "message_file": str(message_file)}
        missing: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in resolved:
                return resolved[key]
            if key.startswith("frontmatter_"):
                missing.append(key)
            # Any other brace text (shell or awk syntax) is left as written.
            return match.group(0)

        command = [_PLACEHOLDER_RE.sub(substitute, arg) for arg in argv]
        if missing:
            keys = ", ".join(sorted({key.removeprefix("frontmatter_") for key in missing}))
            return HookResult(False, f"{name}: the spec's frontmatter does not set {keys}")
        env = {**os.environ, **{f"SPEC_HOOK_{key.upper()}": value for key, value in resolved.items()}}
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return HookResult(False, f"{name} timed out after {config.timeout_seconds:g}s")
        except OSError as exc:
            return HookResult(False, f"{name} could not start: {exc}")
        output = redact((completed.stderr or "").strip() or (completed.stdout or "").strip())
        output = output[-_OUTPUT_TAIL_CHARS:]
        if completed.returncode != 0:
            return HookResult(False, f"{name} exited {completed.returncode}" + (f": {output}" if output else ""))
        return HookResult(True, output)
    finally:
        try:
            message_file.unlink()
        except OSError:
            pass
