"""Typed, platform-aware commands shared by execution and diagnostics."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

CommandMode = Literal["argv", "script"]
ShellName = Literal["sh", "powershell", "pwsh", "cmd"]


class CommandConfigurationError(ValueError):
    """A configured command has conflicting or incomplete variants."""


@dataclass(frozen=True)
class CommandSpec:
    """One command after platform selection, before process launch."""

    mode: CommandMode
    value: tuple[str, ...] | str
    shell: ShellName | None = None
    source: str = ""

    def argv(self, *, which: Callable[[str], str | None] = shutil.which) -> list[str]:
        if self.mode == "argv":
            return list(self.value) if isinstance(self.value, tuple) else [self.value]
        script = str(self.value)
        shell = self.shell or "sh"
        if shell == "sh":
            return ["sh", "-c", script]
        if shell == "cmd":
            return ["cmd.exe", "/d", "/s", "/c", script]
        executable = "powershell.exe" if shell == "powershell" else "pwsh"
        resolved = which(executable)
        if resolved is None:
            raise FileNotFoundError(f"declared shell {shell!r} is not installed")
        return [resolved, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]

    def display(self, *, windows: bool | None = None) -> str:
        argv = [_redact(item) for item in self.argv(which=lambda name: name)]
        use_windows = os.name == "nt" if windows is None else windows
        return subprocess.list2cmdline(argv) if use_windows else shlex.join(argv)


@dataclass(frozen=True)
class CommandVariants:
    """Additive POSIX/Windows script and argument-vector configuration."""

    command: str = ""
    windows_command: str = ""
    argv_value: tuple[str, ...] = ()
    windows_argv: tuple[str, ...] = ()
    shell: ShellName | None = None
    windows_shell: ShellName | None = None
    source: str = ""

    def select(self, *, windows: bool | None = None) -> CommandSpec | None:
        use_windows = os.name == "nt" if windows is None else windows
        if use_windows:
            if self.windows_argv:
                return CommandSpec("argv", self.windows_argv, source=self.source)
            if self.windows_command:
                return CommandSpec(
                    "script", self.windows_command, self.windows_shell or self.shell,
                    self.source,
                )
            if self.argv_value:
                return CommandSpec("argv", self.argv_value, source=self.source)
            if self.command:
                return CommandSpec("script", self.command, self.shell or "sh", self.source)
            return None
        if self.argv_value:
            return CommandSpec("argv", self.argv_value, source=self.source)
        if self.command:
            return CommandSpec("script", self.command, self.shell or "sh", self.source)
        return None


def parse_command_variants(
    payload: Mapping[str, object], *, command_key: str = "command", source: str
) -> CommandVariants:
    """Parse and validate the common command variant vocabulary."""
    argv_key = "argv" if command_key == "command" else f"{command_key.removesuffix('_command')}_argv"
    command = _string(payload.get(command_key), f"{source}.{command_key}")
    windows_command = _string(payload.get(f"{command_key}_windows"), f"{source}.{command_key}_windows")
    argv_value = _argv(payload.get(argv_key), f"{source}.{argv_key}")
    windows_argv = _argv(payload.get(f"{argv_key}_windows"), f"{source}.{argv_key}_windows")
    shell = _shell(payload.get("shell"), f"{source}.shell")
    windows_shell = _shell(payload.get("shell_windows"), f"{source}.shell_windows")
    if command and argv_value:
        raise CommandConfigurationError(f"{source} cannot set both {command_key} and {argv_key}")
    if windows_command and windows_argv:
        raise CommandConfigurationError(
            f"{source} cannot set both {command_key}_windows and {argv_key}_windows"
        )
    if shell and not command:
        raise CommandConfigurationError(f"{source}.shell requires {command_key}")
    if windows_shell and not windows_command:
        raise CommandConfigurationError(
            f"{source}.shell_windows requires {command_key}_windows"
        )
    if windows_command and not windows_shell:
        raise CommandConfigurationError(
            f"{source}.{command_key}_windows requires shell_windows; "
            "select powershell, pwsh, or cmd"
        )
    if shell in ("powershell", "pwsh", "cmd"):
        raise CommandConfigurationError(f"{source}.shell={shell!r} is Windows-only; use shell_windows")
    if windows_shell == "sh":
        raise CommandConfigurationError(f"{source}.shell_windows cannot select POSIX sh")
    return CommandVariants(
        command, windows_command, argv_value, windows_argv, shell, windows_shell, source
    )


def run_command(
    command: CommandSpec, *, cwd: Path, env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Launch a typed command without interpreting argv-mode arguments."""
    try:
        return subprocess.run(
            command.argv(), cwd=cwd, env=None if env is None else dict(env),
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command.display(), 127, "", str(exc))
    except OSError as exc:
        return subprocess.CompletedProcess(command.display(), 126, "", str(exc))


def looks_posix_script(script: str) -> bool:
    return bool(re.search(r"(^|\s)(?:&&|\|\||\./|\.venv/bin/|/bin/|export\s)", script))


def _string(value: object, location: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommandConfigurationError(f"{location} must be a string")
    return value.strip()


def _argv(value: object, location: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise CommandConfigurationError(f"{location} must be a non-empty array of strings")
    return tuple(value)


def _shell(value: object, location: str) -> ShellName | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"sh", "powershell", "pwsh", "cmd"}:
        raise CommandConfigurationError(
            f"{location} must be one of sh, powershell, pwsh, or cmd"
        )
    return value  # type: ignore[return-value]


def _redact(value: str) -> str:
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key)=([^\s]+)", r"\1=***", value)
    return re.sub(r"(?i)^(--?(?:token|password|secret|api[_-]?key))(.*)$", r"\1***", value)
