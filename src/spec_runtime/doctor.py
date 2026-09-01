"""Read-only onboarding diagnostics for a configured spec repository."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .command_runtime import CommandSpec, looks_posix_script
from .config import SpecRuntimeConfig, load_repo_spec_runtime_config
from .platform import is_unc_path, is_windows

DoctorStatus = Literal["ok", "warning", "error"]
CommandRunner = Callable[[list[str], Path, float], subprocess.CompletedProcess[str]]
ExecutableResolver = Callable[[str], str | None]
ConfigLoader = Callable[[Path], SpecRuntimeConfig]


@dataclass(frozen=True)
class DoctorCheck:
    """One deterministic preflight result."""

    name: str
    status: DoctorStatus
    detail: str
    remediation: tuple[str, ...] = ()

    @property
    def blocker(self) -> bool:
        return self.status == "error"


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def blocker_count(self) -> int:
        return sum(check.blocker for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status == "warning" for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocker_count else 0


def _ok(name: str, detail: str, *remediation: str) -> DoctorCheck:
    return DoctorCheck(name, "ok", detail, tuple(remediation))


def _warning(name: str, detail: str, *remediation: str) -> DoctorCheck:
    return DoctorCheck(name, "warning", detail, tuple(remediation))


def _error(name: str, detail: str, *remediation: str) -> DoctorCheck:
    return DoctorCheck(name, "error", detail, tuple(remediation))


def _run_command(
    argv: list[str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            124,
            "",
            f"timed out after {timeout:g}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 126, "", str(exc))


def _one_line(value: str, *, limit: int = 320) -> str:
    return " ".join((value or "").strip().split())[:limit] or "-"


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return _one_line(result.stderr or result.stdout or f"exit {result.returncode}")


def _redact_remote_url(url: str) -> str:
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", url.strip())


def _load_config(repo_root: Path) -> SpecRuntimeConfig:
    return load_repo_spec_runtime_config(repo_root, require=True)


def run_doctor_checks(
    requested_root: Path,
    *,
    runner: CommandRunner | None = None,
    which: ExecutableResolver | None = None,
    config_loader: ConfigLoader | None = None,
) -> DoctorReport:
    """Inspect project readiness without changing files, refs, or services."""
    run = runner or _run_command
    resolve_executable = which or shutil.which
    load_config = config_loader or _load_config
    requested_root = requested_root.expanduser().resolve()
    checks: list[DoctorCheck] = []

    if is_windows() and is_unc_path(requested_root):
        checks.append(
            _error(
                "local filesystem",
                f"UNC or network repository roots are unsupported: {requested_root}",
                "Move the checkout to a fixed local NTFS volume and rerun `spec doctor`.",
            )
        )
        return DoctorReport(tuple(checks))

    repo_result = run(
        ["git", "rev-parse", "--show-toplevel"],
        requested_root,
        10.0,
    )
    repo_ok = repo_result.returncode == 0 and bool(repo_result.stdout.strip())
    if repo_ok:
        repo_root = Path(repo_result.stdout.strip()).resolve()
        checks.append(_ok("repository", f"git checkout at {repo_root}"))
    else:
        repo_root = requested_root
        checks.append(
            _error(
                "repository",
                f"not a usable git checkout: {_result_detail(repo_result)}",
                "Run `spec doctor` from a Git checkout, or pass `--repo-root /path/to/repo`.",
                "Install Git if `git` is not available on PATH.",
            )
        )

    try:
        config = load_config(repo_root)
    except Exception as exc:
        checks.append(
            _error(
                "configuration",
                f"could not load {repo_root / '.spec.toml'}: {_one_line(str(exc))}",
                "Run `spec init` in the repository root to create `.spec.toml`.",
                "If the file exists, fix the reported TOML or configuration value and rerun `spec doctor`.",
            )
        )
        return DoctorReport(tuple(checks))

    checks.append(_ok("configuration", f"loaded {repo_root / '.spec.toml'}"))
    if not repo_ok:
        return DoctorReport(tuple(checks))

    checks.extend(_execution_policy_checks(config))
    checks.extend(_git_checks(repo_root, config, run))
    checks.extend(_agent_checks(repo_root, config, resolve_executable))
    checks.extend(_github_checks(repo_root, run, resolve_executable))
    checks.extend(_command_checks(repo_root, config, run, resolve_executable))
    path_checks, runtime_paths = _runtime_path_checks(repo_root, config, run)
    checks.extend(path_checks)
    checks.extend(_runtime_path_overlap_checks(runtime_paths))
    checks.extend(_container_checks(repo_root, config, run, resolve_executable))
    return DoctorReport(tuple(checks))


def _execution_policy_checks(config: SpecRuntimeConfig) -> list[DoctorCheck]:
    execution = config.execution
    if execution.safety_mode_explicit:
        return [
            _warning(
                "execution safety label",
                f"safety_mode={execution.safety_mode!r} is metadata only and does not change runtime enforcement",
                "Choose the execution backend based on the isolation you need; container is the strongest boundary.",
                "See docs/execution-backends.md before running untrusted repository code.",
            )
        ]
    return [
        _ok(
            "execution safety label",
            "implicit `safe` compatibility label; provider launch policy is fixed",
        )
    ]


def _git_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    run: CommandRunner,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    origin = run(["git", "remote", "get-url", "origin"], repo_root, 10.0)
    if origin.returncode == 0 and origin.stdout.strip():
        checks.append(
            _ok(
                "origin remote",
                _redact_remote_url(origin.stdout),
            )
        )
    else:
        checks.append(
            _error(
                "origin remote",
                f"origin is missing or unreadable: {_result_detail(origin)}",
                "Add the publish remote: `git remote add origin <repository-url>`.",
                "Then run `git fetch origin --prune`.",
            )
        )

    base_ref = config.base_ref.strip()
    base = run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
        repo_root,
        10.0,
    )
    if base.returncode == 0 and base.stdout.strip():
        checks.append(
            _ok(
                "base ref",
                f"{base_ref} -> {base.stdout.strip()[:12]}",
            )
        )
    else:
        checks.append(
            _error(
                "base ref",
                f"configured base_ref {base_ref!r} does not resolve to a commit",
                "Fetch remote refs: `git fetch origin --prune`.",
                "Or set `base_ref` in `.spec.toml` to an existing ref such as `origin/main`.",
            )
        )
    return checks


def _agent_install_remediation(agent: str) -> str:
    if agent == "claude":
        return "Install Claude Code: `npm install -g @anthropic-ai/claude-code`."
    if agent == "codex":
        return "Install Codex CLI: `npm install -g @openai/codex`."
    return f"Install the `{agent}` agent executable or remove it from `[agents].allowed`."


def _agent_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    which: ExecutableResolver,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    allowed = tuple(dict.fromkeys(config.agents.allowed))
    default = config.agents.default.strip()
    review_default = config.agents.review_default.strip()
    invalid_defaults = [
        name
        for name in (default, review_default)
        if name and name not in allowed
    ]
    if invalid_defaults:
        checks.append(
            _error(
                "agent defaults",
                "default agent values are not allowed: " + ", ".join(invalid_defaults),
                "Add each default to `[agents].allowed`, or choose a default from the allowed list.",
            )
        )
    else:
        review_detail = review_default or default
        checks.append(
            _ok(
                "agent defaults",
                f"implementation={default}; review={review_detail}; allowed={','.join(allowed)}",
            )
        )

    required = {default, review_default} - {""}
    for agent in allowed:
        path = which(agent)
        if path:
            checks.append(_ok(f"agent binary ({agent})", path))
        elif agent in required:
            checks.append(
                _error(
                    f"agent binary ({agent})",
                    f"required default agent executable `{agent}` was not found on PATH",
                    _agent_install_remediation(agent),
                    "Or change the corresponding default in `[agents]` to an installed allowed agent.",
                )
            )
        else:
            checks.append(
                _warning(
                    f"agent binary ({agent})",
                    f"optional allowed agent executable `{agent}` was not found on PATH",
                    _agent_install_remediation(agent),
                    f"Or remove `{agent}` from `[agents].allowed` until it is installed.",
                )
            )

        if not path:
            continue

        severity = _error if agent in required else _warning
        if agent == "claude":
            from .agent_adapter import claude_sandbox_unavailability_reason

            sandbox_reason = claude_sandbox_unavailability_reason(which=which)
            if sandbox_reason:
                checks.append(
                    severity(
                        "agent runtime (claude)",
                        sandbox_reason,
                        "On Debian/Ubuntu: `sudo apt-get install bubblewrap socat`.",
                        "Use the container backend when host sandboxing is unavailable.",
                    )
                )
            else:
                checks.append(
                    _ok(
                        "agent runtime (claude)",
                        "host sandbox prerequisites are installed",
                    )
                )
        elif agent == "codex":
            config_path = repo_root / ".codex"
            if config_path.is_symlink() or (
                config_path.exists() and not config_path.is_dir()
            ):
                checks.append(
                    severity(
                        "agent project config (codex)",
                        f"{config_path} is not a real directory; current Codex requires `.codex/` to be one",
                        "Inspect and rename or remove the legacy `.codex` path.",
                        "Rerun `spec doctor` before starting Codex web chat.",
                    )
                )
            else:
                checks.append(
                    _ok(
                        "agent project config (codex)",
                        "`.codex/` is absent or a directory",
                    )
                )
    return checks


def _github_checks(
    repo_root: Path,
    run: CommandRunner,
    which: ExecutableResolver,
) -> list[DoctorCheck]:
    gh_path = which("gh")
    if not gh_path:
        return [
            _error(
                "GitHub CLI",
                "`gh` was not found on PATH",
                "Install GitHub CLI: https://cli.github.com/",
                "Then authenticate with `gh auth login`.",
            )
        ]

    checks = [_ok("GitHub CLI", gh_path)]
    auth = run([gh_path, "auth", "status"], repo_root, 15.0)
    if auth.returncode == 0:
        checks.append(_ok("GitHub authentication", "`gh auth status` succeeded"))
    else:
        auth_lines = [
            line
            for line in (auth.stderr or auth.stdout).splitlines()
            if "token" not in line.lower()
        ]
        auth_detail = _one_line("\n".join(auth_lines))
        checks.append(
            _error(
                "GitHub authentication",
                auth_detail,
                "Run `gh auth login` and select the host used by the origin remote.",
                "In CI, provide a valid `GH_TOKEN` with repository access.",
            )
        )
    return checks


_COMMAND_BOUNDARIES = {"&&", "||", ";", "|", "&", "("}
_SHELL_KEYWORDS = {
    "!",
    "do",
    "elif",
    "else",
    "if",
    "then",
    "until",
    "while",
    "{",
}
_SHELL_BUILTINS = {
    ".",
    ":",
    "[",
    "alias",
    "break",
    "cd",
    "continue",
    "echo",
    "eval",
    "exit",
    "export",
    "false",
    "printf",
    "pwd",
    "read",
    "return",
    "set",
    "shift",
    "test",
    "true",
    "type",
    "ulimit",
    "umask",
    "unalias",
    "unset",
    "wait",
}
_SHELL_WRAPPERS = {"command", "env", "exec"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_executables(command: str) -> tuple[str, ...]:
    lexer = shlex.shlex(
        command,
        posix=True,
        punctuation_chars="|&;()",
    )
    lexer.whitespace_split = True
    lexer.commenters = "#"
    executables: list[str] = []
    expect_command = True
    wrapper = False
    for token in lexer:
        if token in _COMMAND_BOUNDARIES:
            expect_command = True
            wrapper = False
            continue
        if token in {")", "fi", "done", "esac", "}"}:
            expect_command = False
            wrapper = False
            continue
        if token in _SHELL_KEYWORDS:
            expect_command = True
            wrapper = False
            continue
        if not expect_command:
            continue
        if _ASSIGNMENT_RE.match(token):
            continue
        if wrapper and token.startswith("-"):
            continue
        if token in _SHELL_WRAPPERS:
            wrapper = True
            continue
        if token in _SHELL_BUILTINS:
            expect_command = False
            wrapper = False
            continue
        executables.append(token)
        expect_command = False
        wrapper = False
    return tuple(dict.fromkeys(executables))


def _resolve_command_executable(
    executable: str,
    repo_root: Path,
    which: ExecutableResolver,
) -> str | None:
    if "/" not in executable:
        resolved = which(executable)
        if resolved:
            return resolved
        for bin_dir in (repo_root / ".venv/bin", repo_root / "node_modules/.bin"):
            candidate = bin_dir / executable
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        return None
    path = Path(executable).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    return None


def _configured_commands(
    config: SpecRuntimeConfig,
) -> list[tuple[str, str, str, bool]]:
    commands: list[tuple[str, str, str, bool]] = []
    if config.bootstrap_install_command:
        commands.append(
            (
                "bootstrap command",
                config.bootstrap_install_command,
                "[bootstrap].install_command",
                False,
            )
        )
    if config.bootstrap_cache.enabled and config.bootstrap_cache.command:
        commands.append(
            (
                "bootstrap cache command",
                config.bootstrap_cache.command,
                "[bootstrap.cache].command",
                False,
            )
        )
    for gate in config.verify_gates:
        commands.append(
            (
                f"verify command ({gate.name})",
                gate.command,
                f"[[verify.gates]] name={gate.name!r}",
                True,
            )
        )
    return commands


def _command_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    run: CommandRunner,
    which: ExecutableResolver,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if is_windows():
        return _windows_command_checks(repo_root, config, which)
    commands = _configured_commands(config)
    bootstrap_deferred_executables: set[str] = set()
    if config.bootstrap_install_command:
        try:
            bootstrap_executables = _command_executables(config.bootstrap_install_command)
        except ValueError:
            bootstrap_executables = ()
        # Only the first bootstrap launcher must exist before a fresh run. A
        # later command may use an environment or tool created by an earlier
        # bootstrap step (for example, `python -m venv .venv &&
        # .venv/bin/python ...`). Treat the same executable as pending rather
        # than broken when a verify gate refers to it before bootstrap runs.
        bootstrap_deferred_executables.update(bootstrap_executables[1:])
    if not config.bootstrap_install_command:
        checks.append(_ok("bootstrap command", "not configured (optional)"))
    for name, command, config_location, validate_all_executables in commands:
        syntax = run(["/bin/sh", "-n", "-c", command], repo_root, 10.0)
        if syntax.returncode != 0:
            checks.append(
                _error(
                    name,
                    f"invalid shell syntax: {_result_detail(syntax)}",
                    f"Fix `{config_location}` in `.spec.toml`.",
                )
            )
            continue
        try:
            executables = _command_executables(command)
        except ValueError as exc:
            checks.append(
                _error(
                    name,
                    f"could not parse command: {_one_line(str(exc))}",
                    f"Fix `{config_location}` in `.spec.toml`.",
                )
            )
            continue
        executables_to_check = (
            executables
            if validate_all_executables
            else executables[:1]
        )
        missing = [
            executable
            for executable in executables_to_check
            if _resolve_command_executable(executable, repo_root, which) is None
        ]
        pending_bootstrap = [
            executable
            for executable in missing
            if validate_all_executables and executable in bootstrap_deferred_executables
        ]
        unresolved = [executable for executable in missing if executable not in pending_bootstrap]
        if unresolved:
            checks.append(
                _error(
                    name,
                    "missing executable(s): " + ", ".join(f"`{item}`" for item in unresolved),
                    f"Install the missing executable(s), or update `{config_location}` in `.spec.toml`.",
                )
            )
        else:
            detail = (
                ", ".join(executables_to_check)
                if executables_to_check
                else "shell builtins only"
            )
            if pending_bootstrap:
                detail += "; pending bootstrap: " + ", ".join(pending_bootstrap)
            checks.append(_ok(name, f"syntax valid; launcher(s): {detail}"))
    return checks


def _windows_command_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    which: ExecutableResolver,
) -> list[DoctorCheck]:
    """Validate exactly the typed command variants native Windows will run."""
    configured: list[tuple[str, CommandSpec | None, str, str]] = [
        (
            "bootstrap command", config.bootstrap_install.select(windows=True),
            "[bootstrap]", "install_",
        ),
        (
            "implement setup command", config.implement.setup.select(windows=True),
            "[implement]", "setup_",
        ),
        (
            "implement teardown command", config.implement.teardown.select(windows=True),
            "[implement]", "teardown_",
        ),
    ]
    configured.extend(
        (
            f"verify command ({gate.name})",
            gate.command_variants.select(windows=True),
            f"[[verify.gates]] name={gate.name!r}",
            "",
        )
        for gate in config.verify_gates
    )
    checks: list[DoctorCheck] = []
    for name, command, location, key_prefix in configured:
        if command is None:
            if name == "bootstrap command":
                checks.append(_ok(name, "not configured (optional)"))
            continue
        if command.mode == "script" and command.shell == "sh":
            detail = (
                "only a clearly POSIX shell command is configured, which native Windows will not rewrite"
                if looks_posix_script(str(command.value))
                else "a POSIX shell command is selected, which native Windows will not execute"
            )
            checks.append(
                _error(
                    name,
                    detail,
                    f"Add `{key_prefix}argv_windows` to `{location}`, or add "
                    f"`{key_prefix}command_windows` with `{key_prefix}shell_windows` "
                    "set to powershell, pwsh, or cmd.",
                )
            )
            continue
        try:
            argv = command.argv(which=which, windows=True)
        except FileNotFoundError as exc:
            checks.append(_error(name, str(exc), f"Install the declared shell or update `{location}`."))
            continue
        executable = argv[0]
        if _resolve_command_executable(executable, repo_root, which) is None:
            checks.append(
                _error(name, f"missing executable: `{executable}`", f"Install it or update `{location}`.")
            )
        else:
            checks.append(_ok(name, f"selected command: {command.display(windows=True)}"))
    return checks


def _runtime_path_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    run: CommandRunner,
) -> tuple[list[DoctorCheck], dict[str, Path]]:
    checks: list[DoctorCheck] = []
    resolved_paths: dict[str, Path] = {}
    configured_paths = (
        ("state path", config.paths.state_dir, "[paths].state_dir"),
        ("worktree path", config.paths.worktrees_dir, "[paths].worktrees_dir"),
        ("workspace path", config.execution.workspace_root, "[execution].workspace_root"),
    )
    repo_root = repo_root.resolve()
    git_dir = (repo_root / ".git").resolve()
    for name, raw_path, config_location in configured_paths:
        configured = Path(raw_path).expanduser()
        candidate = configured if configured.is_absolute() else repo_root / configured
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(repo_root)
        except ValueError:
            checks.append(
                _error(
                    name,
                    f"{config_location} resolves outside the repository: {resolved}",
                    f"Set `{config_location}` to a dedicated ignored directory inside {repo_root}.",
                )
            )
            continue
        if relative == Path("."):
            checks.append(
                _error(
                    name,
                    f"{config_location} resolves to the repository root",
                    f"Set `{config_location}` to a dedicated directory such as `.{name.split()[0]}`.",
                )
            )
            continue
        if resolved == git_dir or git_dir in resolved.parents:
            checks.append(
                _error(
                    name,
                    f"{config_location} resolves inside Git metadata: {resolved}",
                    f"Move `{config_location}` outside `.git` to a dedicated ignored directory.",
                )
            )
            continue
        if candidate.exists() and not candidate.is_dir():
            checks.append(
                _error(
                    name,
                    f"configured runtime directory is an existing non-directory: {candidate}",
                    f"Choose a directory path for `{config_location}`.",
                )
            )
            continue

        relative_text = relative.as_posix()
        tracked = run(
            ["git", "ls-files", "--", relative_text],
            repo_root,
            10.0,
        )
        if tracked.returncode != 0:
            checks.append(
                _error(
                    name,
                    f"could not verify tracked files under {relative_text}: {_result_detail(tracked)}",
                    "Repair the Git checkout and rerun `spec doctor`.",
                )
            )
            continue
        if tracked.stdout.strip():
            checks.append(
                _error(
                    name,
                    f"runtime path contains tracked files: {_one_line(tracked.stdout)}",
                    f"Add `/{relative_text}/` to `.gitignore`.",
                    f"Remove generated files from the index: `git rm -r --cached -- {shlex.quote(relative_text)}`.",
                )
            )
            continue

        ignored = run(
            ["git", "check-ignore", "--quiet", "--no-index", "--", f"{relative_text}/"],
            repo_root,
            10.0,
        )
        if ignored.returncode == 0:
            checks.append(_ok(name, f"{relative_text}/ is inside the repo, untracked, and ignored"))
        elif ignored.returncode == 1:
            checks.append(
                _warning(
                    name,
                    f"{relative_text}/ is untracked but not ignored",
                    f"Add `/{relative_text}/` to `.gitignore` before running workflows.",
                )
            )
        else:
            checks.append(
                _error(
                    name,
                    f"could not verify ignore rules for {relative_text}: {_result_detail(ignored)}",
                    "Repair the Git checkout and rerun `spec doctor`.",
                )
            )
        resolved_paths[name] = resolved
    return checks, resolved_paths


def _runtime_path_overlap_checks(paths: dict[str, Path]) -> list[DoctorCheck]:
    overlaps: list[str] = []
    items = list(paths.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if (
                left_path == right_path
                or left_path in right_path.parents
                or right_path in left_path.parents
            ):
                overlaps.append(f"{left_name}={left_path} overlaps {right_name}={right_path}")
    if overlaps:
        return [
            _error(
                "runtime path separation",
                "; ".join(overlaps),
                "Configure state, worktree, and workspace roots as distinct non-nested directories.",
            )
        ]
    return [_ok("runtime path separation", "runtime directories are distinct")]


def _container_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    run: CommandRunner,
    which: ExecutableResolver,
) -> list[DoctorCheck]:
    container_selected = config.execution.backend == "container" or (
        config.autopilot.container_default_enabled
        and not config.execution.backend_explicit
    )
    if not container_selected:
        return []

    from .container import container_image_source

    checks: list[DoctorCheck] = []
    engine = config.execution.container.engine or "docker"
    engine_path = which(engine)
    if not engine_path:
        checks.append(
            _error(
                "container engine",
                f"configured engine `{engine}` was not found on PATH",
                "Install Docker, Podman, or another compatible engine.",
                "Or update `[execution.container].engine` in `.spec.local.toml`.",
            )
        )
    else:
        info = run([engine_path, "info"], repo_root, 15.0)
        if info.returncode == 0:
            checks.append(_ok("container engine", f"{engine_path}; daemon/API reachable"))
        else:
            checks.append(
                _error(
                    "container engine",
                    f"{engine} daemon/API is unavailable: {_result_detail(info)}",
                    f"Start `{engine}` and verify `{engine} info` succeeds.",
                )
            )

    source = container_image_source(config, repo_root)
    if source.startswith(("image:", "dockerfile:")):
        checks.append(_ok("container worker source", source))
    else:
        checks.append(
            _error(
                "container worker source",
                source,
                "Run `spec container init` to create the configured worker Dockerfile.",
                "Or configure `[execution.container].image` with a prepared worker image.",
            )
        )
    checks.append(
        _ok(
            "container diagnostics",
            "read-only checks complete; run `spec container doctor` for the disposable-container smoke check",
        )
    )
    return checks


def print_doctor_report(report: DoctorReport) -> None:
    for check in report.checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
        for remediation in check.remediation:
            print(f"        Fix: {remediation}")
    print(
        "Summary: "
        f"{report.blocker_count} blocker(s), "
        f"{report.warning_count} warning(s), "
        f"{len(report.checks)} check(s)."
    )


def cmd_doctor(args: object) -> int:
    repo_root = Path(getattr(args, "repo_root", "") or Path.cwd())
    report = run_doctor_checks(repo_root)
    print_doctor_report(report)
    return report.exit_code
