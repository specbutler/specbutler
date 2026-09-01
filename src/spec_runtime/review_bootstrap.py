"""Fail-closed sandbox boundary for untrusted local-review bootstrap hooks.

The configured bootstrap command is trusted, but installing a pull request can
execute arbitrary code from that pull request (for example, a PEP 517 build
backend).  Environment scrubbing and a temporary ``HOME`` reduce accidental
credential exposure; they are not a security boundary.  This module runs the
command through Codex's direct, model-free sandbox runner with an explicit
least-privilege permission profile.

If the runner or requested profile is unavailable, callers must skip bootstrap
and continue with a diff-only review.  Running the command directly is never a
fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .platform_fs import remove_tree

REVIEW_BOOTSTRAP_PERMISSION_PROFILE = "spec_review_bootstrap"

# Start the trusted sandbox launcher with only portable process/runtime state.
# In particular, do not forward provider, forge, registry, cloud, or proxy
# variables merely because their exact names are not known here yet.
_INHERITED_ENV_KEYS = frozenset(
    {
        "COLORTERM",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TERM",
        "WINDIR",
    }
)


class ReviewBootstrapSandboxUnavailable(RuntimeError):
    """Raised when bootstrap cannot be executed inside an enforceable sandbox."""


@dataclass(frozen=True)
class PreparedReviewBootstrapSandbox:
    """One prepared bootstrap sandbox and its intentionally narrow environment."""

    launcher_argv: tuple[str, ...]
    env: dict[str, str]
    runtime_root: Path

    def wrap(self, command_argv: Sequence[str]) -> list[str]:
        """Return the sandbox-launch argv for *command_argv*.

        An empty prefix is useful only for hermetic unit-test doubles.  The
        production context manager always supplies the Codex sandbox prefix.
        """

        return [*self.launcher_argv, *command_argv]


def _toml_string(value: str) -> str:
    # A JSON string literal is also a valid TOML basic string, including for
    # Windows paths containing backslashes.
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_table(values: Mapping[str, str]) -> str:
    return "{" + ",".join(f"{_toml_string(key)}={_toml_string(value)}" for key, value in values.items()) + "}"


def _operator_codex_home(environ: Mapping[str, str]) -> Path:
    configured = str(environ.get("CODEX_HOME", "")).strip()
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def _resolve_executable(executable: str, *, path: str) -> Path | None:
    candidate = Path(executable)
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    resolved = shutil.which(executable, path=path)
    return Path(resolved).resolve() if resolved else None


def _read_root_for_executable(executable: Path) -> Path:
    """Return the narrow install root needed to execute *executable*.

    Interpreters installed below a conventional ``bin``/``Scripts`` directory
    need their adjacent libraries.  Standalone tools need only their containing
    directory.  These roots can reopen a toolchain nested below the otherwise
    denied operator home, without reopening the rest of that home.
    """

    parent = executable.parent
    if parent.name.casefold() in {"bin", "scripts"}:
        return parent.parent
    return parent


def _codex_distribution_read_roots(launcher: Path) -> tuple[Path, ...]:
    """Locate the files used by standalone and npm-installed Codex launchers."""

    lexical = launcher.absolute()
    resolved = launcher.resolve()
    roots = {_read_root_for_executable(resolved)}

    # On Windows npm puts codex.cmd and node_modules beside one another.  On
    # POSIX the global bin entry is commonly a symlink, so the resolved JS path
    # above identifies the package instead.
    roots.add(lexical.parent)
    if resolved.suffix.casefold() in {".js", ".mjs", ".cjs"} and resolved.parent.name == "bin":
        roots.add(resolved.parent.parent)
    return tuple(sorted((root.resolve() for root in roots if root.exists()), key=str))


def build_review_bootstrap_environment(
    *,
    inherited_env: Mapping[str, str],
    runtime_root: Path,
    codex_home: Path,
    windows: bool,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in inherited_env.items()
        if key.upper() in _INHERITED_ENV_KEYS
    }

    isolated_home = runtime_root / "home"
    isolated_tmp = runtime_root / "tmp"
    isolated_home.mkdir(parents=True, exist_ok=True)
    isolated_tmp.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_KEY_1": "remote.origin.pushurl",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_VALUE_1": "spec-review-disabled://origin",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(isolated_home),
            "TMP": str(isolated_tmp),
            "TEMP": str(isolated_tmp),
            "TMPDIR": str(isolated_tmp),
            "USERPROFILE": str(isolated_home),
            "XDG_CACHE_HOME": str(isolated_home / ".cache"),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            "XDG_DATA_HOME": str(isolated_home / ".local" / "share"),
        }
    )
    if windows:
        roaming = isolated_home / "AppData" / "Roaming"
        local = isolated_home / "AppData" / "Local"
        roaming.mkdir(parents=True, exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        env["APPDATA"] = str(roaming)
        env["LOCALAPPDATA"] = str(local)
        drive, tail = os.path.splitdrive(str(isolated_home))
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = tail or "\\"
    return env


def _permission_profile_override(
    *,
    operator_home: Path,
    codex_home: Path,
    read_roots: Sequence[Path],
) -> str:
    filesystem: dict[str, object] = {
        ":minimal": "read",
        ":workspace_roots": {".": "write"},
    }
    # Permission profiles are allowlists: an unlisted operator home remains
    # unreadable. Do not add an explicit parent deny here, because Codex is
    # commonly installed below that home (for example through nvm) and parent
    # denies take precedence over the narrow executable read roots below.
    if codex_home != operator_home and not codex_home.is_relative_to(operator_home):
        filesystem[str(codex_home)] = "deny"
    for root in read_roots:
        filesystem[str(root)] = "read"

    def render(value: object) -> str:
        if isinstance(value, str):
            return _toml_string(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, Mapping):
            return "{" + ",".join(f"{_toml_string(str(k))}={render(v)}" for k, v in value.items()) + "}"
        raise TypeError(f"unsupported TOML inline value: {value!r}")

    profile = {
        "filesystem": filesystem,
        "network": {"enabled": False},
    }
    return f"permissions.{REVIEW_BOOTSTRAP_PERMISSION_PROFILE}={render(profile)}"


def _shell_environment_overrides(env: Mapping[str, str]) -> tuple[str, ...]:
    # Codex normally merges the user's shell_environment_policy.  An operator
    # config can contain explicit `set` values, including secrets.  The exact
    # include filter below ensures the hostile child receives only this
    # allowlisted environment; the explicit set values win for allowed keys.
    includes = {key: "include" for key in sorted(env)}
    values = {key: env[key] for key in sorted(env)}
    return (
        "shell_environment_policy.inherit=\"all\"",
        "shell_environment_policy.experimental_use_profile=false",
        f"shell_environment_policy.filters={_toml_inline_table(includes)}",
        f"shell_environment_policy.set={_toml_inline_table(values)}",
    )


@contextmanager
def isolated_review_bootstrap_sandbox(
    review_worktree: Path,
    command_argv: Sequence[str],
    *,
    inherited_env: Mapping[str, str] | None = None,
    windows: bool | None = None,
    which: Callable[..., str | None] = shutil.which,
) -> Iterator[PreparedReviewBootstrapSandbox]:
    """Prepare a model-free Codex sandbox for one hostile bootstrap command.

    The active user Codex home is retained for trusted runner configuration and
    native-Windows elevated-sandbox setup.  The permission profile explicitly
    denies that home (and the rest of the operator home) to the untrusted child.
    No auth file is copied into the writable review worktree.
    """

    use_windows = os.name == "nt" if windows is None else windows
    source_env = dict(os.environ if inherited_env is None else inherited_env)
    path = source_env.get("PATH", os.defpath)
    codex_name = "codex.exe" if use_windows else "codex"
    resolved_codex = which(codex_name, path=path) or which("codex", path=path)
    if not resolved_codex:
        raise ReviewBootstrapSandboxUnavailable(
            "Codex CLI with the `codex sandbox` command is required for isolated review bootstrap"
        )

    codex_launcher = Path(resolved_codex)
    command_executable = _resolve_executable(str(command_argv[0]), path=path) if command_argv else None
    if command_executable is None:
        raise ReviewBootstrapSandboxUnavailable(
            f"bootstrap executable is unavailable: {command_argv[0] if command_argv else '(empty command)'}"
        )

    runtime_root = Path(tempfile.mkdtemp(prefix=".spec-review-bootstrap-", dir=review_worktree))
    try:
        configured_home = source_env.get("USERPROFILE" if use_windows else "HOME", "")
        operator_home = (
            Path(configured_home).expanduser().resolve()
            if configured_home
            else Path.home().resolve()
        )
        codex_home = _operator_codex_home(source_env)
        env = build_review_bootstrap_environment(
            inherited_env=source_env,
            runtime_root=runtime_root,
            codex_home=codex_home,
            windows=use_windows,
        )
        read_roots = {
            *_codex_distribution_read_roots(codex_launcher),
            _read_root_for_executable(command_executable).resolve(),
        }
        permission_override = _permission_profile_override(
            operator_home=operator_home,
            codex_home=codex_home,
            read_roots=tuple(sorted(read_roots, key=str)),
        )
        launcher = [
            str(codex_launcher),
            "sandbox",
            "--permission-profile",
            REVIEW_BOOTSTRAP_PERMISSION_PROFILE,
            "--include-managed-config",
            "--cd",
            str(review_worktree),
            "--config",
            permission_override,
        ]
        for override in _shell_environment_overrides(env):
            launcher.extend(("--config", override))
        launcher.append("--")
        yield PreparedReviewBootstrapSandbox(tuple(launcher), env, runtime_root)
    finally:
        remove_tree(runtime_root, ignore_errors=True)
