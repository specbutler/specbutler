"""Typed, platform-aware commands shared by execution and diagnostics."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, Mapping

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
    login_shell: bool = False

    def argv(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        windows: bool | None = None,
        arguments: tuple[str, ...] = (),
    ) -> list[str]:
        if self.mode == "argv":
            base = list(self.value) if isinstance(self.value, tuple) else [self.value]
            return [*base, *arguments]
        script = str(self.value)
        shell = self.shell or "sh"
        if shell == "sh":
            use_windows = os.name == "nt" if windows is None else windows
            if use_windows:
                raise FileNotFoundError(
                    "POSIX shell command cannot run on native Windows; configure an argv_windows "
                    "variant, or command_windows with shell_windows set to powershell, pwsh, or cmd"
                )
            # sh -c assigns the first word after the script to $0.  Reserve it
            # so caller-supplied hook metadata starts at $1 as documented.
            shell_option = "-lc" if self.login_shell else "-c"
            return (
                ["sh", shell_option, script, "spec-command", *arguments]
                if arguments
                else ["sh", shell_option, script]
            )
        if shell == "cmd":
            if arguments:
                raise CommandConfigurationError(
                    "cmd scripts do not support positional hook metadata safely; "
                    "read SPEC_ID, SPEC_RUN_ID, SPEC_PATH, and SPEC_WORKTREE from the "
                    "environment, or configure an argv_windows command"
                )
            return ["cmd.exe", "/d", "/s", "/c", script]
        executable = "powershell.exe" if shell == "powershell" else "pwsh"
        resolved = which(executable)
        if resolved is None:
            raise FileNotFoundError(f"declared shell {shell!r} is not installed")
        if arguments:
            # Windows PowerShell treats every native argv element following
            # -Command as more source text.  Invoke one scriptblock and encode
            # metadata as PowerShell literals so spaces and metacharacters
            # cannot change the command boundary.
            source = f"& {{ {script} }} " + " ".join(
                _powershell_literal(argument) for argument in arguments
            )
        else:
            source = script
        return [
            resolved, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source,
        ]

    def display(self, *, windows: bool | None = None) -> str:
        use_windows = os.name == "nt" if windows is None else windows
        argv = self.argv(which=lambda name: name, windows=use_windows)
        if self.mode == "script":
            # The script is one process argument, but contains its own tokens.
            # Redact it before argv quoting so split-form credentials cannot
            # escape the argv-level redactor.
            script_index = argv.index(str(self.value))
            argv[script_index] = _redact_script(argv[script_index])
        argv = _redact_argv(argv)
        return subprocess.list2cmdline(argv) if use_windows else shlex.join(argv)

    @contextmanager
    def launch_argv(
        self,
        *,
        cwd: Path,
        which: Callable[[str], str | None] = shutil.which,
        windows: bool | None = None,
        arguments: tuple[str, ...] = (),
        temp_dir: Path | None = None,
    ) -> Iterator[list[str]]:
        """Materialize any launch-only resources and yield the process argv.

        Python's Windows argv encoder cannot safely carry an arbitrary cmd.exe
        program as the single argument after ``/c``: quotes inside that program
        become backslash-quote text.  A batch file gives cmd its native parsing
        boundary and is removed regardless of the process result or timeout.
        """
        use_windows = os.name == "nt" if windows is None else windows
        if self.mode != "script" or self.shell != "cmd" or not use_windows:
            argv = self.argv(which=which, windows=windows, arguments=arguments)
            if use_windows and self.mode == "argv" and argv:
                executable = Path(argv[0])
                if not executable.is_absolute() and (
                    "/" in argv[0] or "\\" in argv[0]
                ):
                    # CreateProcess does not use Popen(cwd=...) when resolving
                    # an executable path. Anchor explicit relative paths to
                    # the command cwd; bare names still use PATH normally.
                    argv[0] = str((cwd / executable).resolve())
            yield argv
            return
        if arguments:
            # Keep the targeted configuration error from argv().
            self.argv(which=which, windows=windows, arguments=arguments)
        fd, raw_path = tempfile.mkstemp(
            prefix="spec-command-",
            suffix=".cmd",
            dir=temp_dir,
        )
        script_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(str(self.value))
                handle.write("\r\n")
            yield ["cmd.exe", "/d", "/c", str(script_path)]
        finally:
            script_path.unlink(missing_ok=True)


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
                if self.shell or looks_posix_script(self.command):
                    return CommandSpec("script", self.command, self.shell or "sh", self.source)
                # Preserve the established native-Windows behavior for simple
                # legacy command strings: launch their tokenized argv directly.
                # Shell syntax is never translated and is rejected by argv().
                return CommandSpec("argv", tuple(shlex.split(self.command)), source=self.source)
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
    shell_key = "shell" if command_key == "command" else f"{command_key.removesuffix('_command')}_shell"
    command = _string(payload.get(command_key), f"{source}.{command_key}")
    windows_command = _string(payload.get(f"{command_key}_windows"), f"{source}.{command_key}_windows")
    argv_value = _argv(payload.get(argv_key), f"{source}.{argv_key}")
    windows_argv = _argv(payload.get(f"{argv_key}_windows"), f"{source}.{argv_key}_windows")
    shell = _shell(payload.get(shell_key), f"{source}.{shell_key}")
    windows_shell = _shell(
        payload.get(f"{shell_key}_windows"), f"{source}.{shell_key}_windows"
    )
    if command and argv_value:
        raise CommandConfigurationError(f"{source} cannot set both {command_key} and {argv_key}")
    if windows_command and windows_argv:
        raise CommandConfigurationError(
            f"{source} cannot set both {command_key}_windows and {argv_key}_windows"
        )
    if shell and not command:
        raise CommandConfigurationError(f"{source}.{shell_key} requires {command_key}")
    if windows_shell and not windows_command:
        raise CommandConfigurationError(
            f"{source}.{shell_key}_windows requires {command_key}_windows"
        )
    if windows_command and not windows_shell:
        raise CommandConfigurationError(
            f"{source}.{command_key}_windows requires {shell_key}_windows; "
            "select powershell, pwsh, or cmd"
        )
    if shell in ("powershell", "pwsh", "cmd"):
        raise CommandConfigurationError(
            f"{source}.{shell_key}={shell!r} is Windows-only; use {shell_key}_windows"
        )
    if windows_shell == "sh":
        raise CommandConfigurationError(f"{source}.{shell_key}_windows cannot select POSIX sh")
    return CommandVariants(
        command, windows_command, argv_value, windows_argv, shell, windows_shell, source
    )


def run_command(
    command: CommandSpec, *, cwd: Path, env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Launch a typed command without interpreting argv-mode arguments."""
    from .process_supervisor import run as run_supervised

    try:
        with command.launch_argv(cwd=cwd) as argv:
            return run_supervised(
                argv, cwd=cwd, env=None if env is None else dict(env),
                capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
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


def _powershell_literal(value: str) -> str:
    """Encode a value as a non-interpolating PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


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


_SPLIT_SECRET = re.compile(
    r"(?i)(?P<option>--?[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?P<space>\s+)(?P<value>\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|]+)"
)


def _redact_script(script: str) -> str:
    """Redact credential values embedded in explicit shell script text."""
    script = re.sub(
        r"(?i)(token|password|passwd|secret|api[_-]?key)=([^\s;&|]+)",
        r"\1=***",
        script,
    )
    def replace_split(match: re.Match[str]) -> str:
        option = match.group("option")
        name = option.lstrip("-")
        return f"{option}{match.group('space')}***" if _sensitive_option(name) else match.group(0)

    return _SPLIT_SECRET.sub(replace_split, script)


_SENSITIVE_OPTION = re.compile(
    r"^--?([A-Za-z0-9][A-Za-z0-9_.-]*)$"
)


def _sensitive_option(name: str) -> bool:
    normalized = re.sub(r"[-_.]", "", name).lower()
    return any(
        marker in normalized
        for marker in ("token", "password", "passwd", "secret", "apikey")
    )


def _redact_argv(argv: list[str]) -> list[str]:
    """Redact both attached and split values of credential-bearing options."""
    redacted: list[str] = []
    hide_next = False
    for item in argv:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        match = _SENSITIVE_OPTION.fullmatch(item)
        if match and _sensitive_option(match.group(1)):
            redacted.append(item)
            hide_next = True
            continue
        redacted.append(_redact(item))
    return redacted
