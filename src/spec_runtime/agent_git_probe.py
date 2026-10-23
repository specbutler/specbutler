"""Model-free probe that agents can use Git inside their provider sandbox.

The probe builds a throwaway repository and workspace for one execution
backend (a per-run ``git clone --local`` or a linked ``git worktree``),
generates the same sandbox policy an implement launch would use, and runs a
small script under the provider's own sandbox runner:

- Codex: ``codex sandbox`` with the implement permission profile.
- Claude: Anthropic's ``srt`` (sandbox-runtime, the library Claude Code's
  sandbox is built on) with the filesystem policy from the generated
  ``.claude/settings.local.json``. Claude Code itself has no model-free
  command runner, so without ``srt`` the sandbox half is reported as
  unverified.

Inside the sandbox, ``git add``, ``commit``, ``stash``, ``branch`` and
``log`` must succeed. Writes to the repository's Git config, its hooks
directory and a path outside the workspace must fail.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .agent_git_access import agent_git_access, apply_agent_git_identity
from .git_publish_guard import apply_host_owned_publication_guard

PROBE_BACKENDS = ("clone", "worktree")
PROBE_AGENTS = ("claude", "codex")
PROBE_GIT_STEPS = ("add", "commit", "stash", "branch", "log")
PROBE_DENIED_WRITES = ("git config", "git hooks", "outside workspace")
PROBE_DISABLE_ENV = "SPEC_AGENT_GIT_PROBE"
_RESULT_MARKER = "SPEC_AGENT_GIT_PROBE_RESULT="
_PROBE_TIMEOUT_SECONDS = 60.0
_NESTED_SANDBOX_MARKERS = (
    "sandbox_apply: Operation not permitted",
    "No permissions to create new namespace",
)

ProbeRunner = Callable[
    [list[str], Path, Mapping[str, str], float],
    subprocess.CompletedProcess[str],
]
ExecutableResolver = Callable[[str], str | None]

_PROBE_SCRIPT = f"""
import json, subprocess, sys
from pathlib import Path
workspace = Path(sys.argv[1])
targets = dict(zip({PROBE_DENIED_WRITES!r}, map(Path, sys.argv[2:5])))
steps = {{}}
def git(name, *args):
    done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)
    detail = (done.stderr or done.stdout).strip()
    steps[name] = "" if done.returncode == 0 else (detail[-300:] or f"exit {{done.returncode}}")
def edit(text):
    try:
        (workspace / "spec-git-probe.txt").write_text(text, encoding="utf-8")
    except OSError as exc:
        steps.setdefault("write workspace", str(exc))
edit("one\\n")
git("add", "add", "spec-git-probe.txt")
git("commit", "commit", "-q", "-m", "spec git probe")
edit("two\\n")
git("stash", "stash", "push", "-q", "-m", "spec git probe")
git("branch", "branch", "spec-git-probe-side")
git("log", "log", "--oneline", "-1")
leaks = []
for label, path in targets.items():
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        continue
    leaks.append(label)
print({_RESULT_MARKER!r} + json.dumps({{"steps": steps, "leaks": leaks}}))
"""


@dataclass(frozen=True)
class AgentGitProbeResult:
    """Outcome of one agent/backend probe."""

    agent: str
    backend: str
    runner: str = ""
    failed_steps: tuple[tuple[str, str], ...] = ()
    leaked_writes: tuple[str, ...] = ()
    unavailable_reason: str = ""
    error: str = ""

    @property
    def ran(self) -> bool:
        return not self.unavailable_reason and not self.error

    @property
    def git_works(self) -> bool:
        return self.ran and not self.failed_steps

    @property
    def passed(self) -> bool:
        return self.git_works and not self.leaked_writes

    def summary(self) -> str:
        if self.unavailable_reason:
            return self.unavailable_reason
        if self.error:
            return self.error
        parts: list[str] = []
        if self.failed_steps:
            parts.append(
                "git failed: "
                + "; ".join(f"{name} ({detail})" for name, detail in self.failed_steps)
            )
        if self.leaked_writes:
            parts.append("writes not blocked: " + ", ".join(self.leaked_writes))
        if not parts:
            parts.append(
                "add, commit, stash, branch and log work; writes to Git config, "
                "hooks and outside the workspace are blocked"
            )
        if self.backend == "worktree":
            parts.append(
                "worktree mode grants the shared .git directory (config, hooks "
                "and info stay read-only)"
            )
        return f"{'; '.join(parts)} (via {self.runner})"


def agent_git_probe_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether ``SPEC_AGENT_GIT_PROBE=0`` turned the probe off."""
    value = (os.environ if environ is None else environ).get(PROBE_DISABLE_ENV, "")
    return value.strip().lower() in {"0", "off", "false", "no"}


def _default_runner(
    argv: list[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:g}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 126, "", str(exc))


def _host_git_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "spec doctor",
            "GIT_AUTHOR_EMAIL": "spec-doctor@localhost",
            "GIT_COMMITTER_NAME": "spec doctor",
            "GIT_COMMITTER_EMAIL": "spec-doctor@localhost",
        }
    )
    return env


def _host_git(args: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_host_git_environment(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail[-300:]}")


def prepare_probe_workspace(root: Path, backend: str) -> Path:
    """Create a throwaway repository and a *backend*-shaped workspace."""
    repo = root / "repo"
    repo.mkdir()
    _host_git(["init", "-q", "-b", "main"], repo)
    (repo / "README.md").write_text("spec git probe\n", encoding="utf-8")
    _host_git(["add", "README.md"], repo)
    _host_git(["commit", "-q", "-m", "init"], repo)
    (repo / ".git" / "hooks").mkdir(exist_ok=True)
    workspace = root / "workspace"
    if backend == "worktree":
        _host_git(["worktree", "add", "-q", "-b", "spec-git-probe", str(workspace)], repo)
    elif backend == "clone":
        _host_git(["clone", "-q", "--local", str(repo), str(workspace)], root)
        _host_git(["checkout", "-q", "-B", "spec-git-probe"], workspace)
        (workspace / ".git" / "hooks").mkdir(exist_ok=True)
    else:
        raise ValueError(f"Unsupported probe backend {backend!r}")
    return workspace


def _probe_environment(workspace: Path, state_dir: Path) -> dict[str, str]:
    env = {
        key: value
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "USER")
        if (value := os.environ.get(key))
    }
    apply_host_owned_publication_guard(env, workspace)
    apply_agent_git_identity(env, workspace)
    scratch = state_dir / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(scratch)
    return env


def _probe_targets(workspace: Path, outside: Path) -> list[str]:
    access = agent_git_access(workspace)
    if access is None:
        raise RuntimeError(f"Probe workspace has no Git metadata: {workspace}")
    return [
        str(access.common_dir / "config"),
        str(access.common_dir / "hooks" / "spec-git-probe"),
        str(outside / "spec-git-probe"),
    ]


def codex_probe_command(
    workspace: Path,
    state_dir: Path,
    script_args: list[str],
    *,
    codex_path: str = "codex",
    python_path: str | None = None,
) -> list[str]:
    """Run the probe under ``codex sandbox`` with the implement profile."""
    from .agent_adapter import (
        _CODEX_IMPLEMENT_PERMISSION_PROFILE,
        _codex_git_metadata_dirs,
        _codex_implement_permission_overrides,
    )

    writable_roots = [state_dir, *_codex_git_metadata_dirs(workspace)]
    permission_args = _codex_implement_permission_overrides(workspace, writable_roots)
    config_args: list[str] = []
    for index, value in enumerate(permission_args[:-1]):
        if value == "-c":
            config_args.extend(("-c", permission_args[index + 1]))
    return [
        codex_path,
        "sandbox",
        "-C",
        str(workspace),
        *config_args,
        "-P",
        _CODEX_IMPLEMENT_PERMISSION_PROFILE,
        python_path or sys.executable,
        "-c",
        _PROBE_SCRIPT,
        *script_args,
    ]


def claude_srt_settings(workspace: Path, state_dir: Path) -> dict[str, object]:
    """Translate generated Claude sandbox settings into an ``srt`` policy.

    Claude Code's sandbox always lets Bash write the working directory; plain
    ``srt`` writes only what ``allowWrite`` lists, so the workspace and the
    launch scratch directory are added explicitly.
    """
    from .orchestrator import _write_sandbox_config

    _write_sandbox_config("claude", workspace)
    settings = json.loads(
        (workspace / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    filesystem = settings["sandbox"]["filesystem"]
    return {
        "network": {"allowedDomains": [], "deniedDomains": []},
        "filesystem": {
            "denyRead": list(filesystem.get("denyRead", [])),
            "allowWrite": [
                str(workspace),
                str(state_dir),
                *filesystem.get("allowWrite", []),
            ],
            "denyWrite": list(filesystem.get("denyWrite", [])),
        },
    }


def claude_probe_command(
    workspace: Path,
    state_dir: Path,
    script_args: list[str],
    *,
    srt_path: str = "srt",
    python_path: str | None = None,
) -> list[str]:
    """Run the probe under ``srt`` with the generated Claude policy."""
    settings_path = state_dir / "srt-settings.json"
    settings_path.write_text(
        json.dumps(claude_srt_settings(workspace, state_dir), indent=2) + "\n",
        encoding="utf-8",
    )
    inner = [python_path or sys.executable, "-c", _PROBE_SCRIPT, *script_args]
    # srt treats bare positional arguments as an argv to exec, so a single
    # joined string becomes one over-long executable name. ``-c`` hands it
    # to the shell, matching how the quoting was built.
    return [srt_path, "--settings", str(settings_path), "-c", shlex.join(inner)]


def parse_probe_output(
    agent: str,
    backend: str,
    runner_name: str,
    completed: subprocess.CompletedProcess[str],
) -> AgentGitProbeResult:
    """Interpret the probe script's marker line."""
    payload: dict[str, object] | None = None
    for line in reversed((completed.stdout or "").splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                loaded = json.loads(line[len(_RESULT_MARKER):])
            except json.JSONDecodeError:
                break
            payload = loaded if isinstance(loaded, dict) else None
            break
    if payload is None:
        output = " ".join(f"{completed.stdout}\n{completed.stderr}".split())
        detail = output[-300:] or f"exit status {completed.returncode}"
        if any(marker in output for marker in _NESTED_SANDBOX_MARKERS):
            detail += (
                " (the probe cannot start a sandbox from inside another sandbox; "
                "run it from a normal terminal)"
            )
        return AgentGitProbeResult(
            agent=agent,
            backend=backend,
            runner=runner_name,
            error=f"{runner_name} did not run the probe: {detail}",
        )
    steps = payload.get("steps")
    leaks = payload.get("leaks")
    failed = tuple(
        (str(name), str(detail))
        for name, detail in (steps.items() if isinstance(steps, dict) else ())
        if detail
    )
    leaked = tuple(str(item) for item in leaks) if isinstance(leaks, list) else ()
    return AgentGitProbeResult(
        agent=agent,
        backend=backend,
        runner=runner_name,
        failed_steps=failed,
        leaked_writes=leaked,
    )


def run_agent_git_probe(
    agent: str,
    backend: str,
    *,
    runner: ProbeRunner | None = None,
    which: ExecutableResolver | None = None,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> AgentGitProbeResult:
    """Probe Git inside *agent*'s sandbox for one *backend* workspace layout."""
    run = runner or _default_runner
    resolve = which or shutil.which
    if agent == "codex":
        runner_path = resolve("codex")
        runner_name = "codex sandbox"
        missing = "Codex CLI is not installed; nothing to probe"
    elif agent == "claude" and sys.platform == "win32":
        return AgentGitProbeResult(
            agent=agent,
            backend=backend,
            unavailable_reason=(
                "Anthropic's sandbox-runtime (`srt`) supports macOS and Linux "
                "only, so Git inside the Claude sandbox is unverified on Windows"
            ),
        )
    elif agent == "claude":
        runner_path = resolve("srt")
        runner_name = "srt"
        missing = (
            "Claude Code has no model-free sandbox runner and Anthropic's "
            "sandbox-runtime (`srt`) is not installed, so Git inside the Claude "
            "sandbox is unverified. Install it with `npm install -g "
            "@anthropic-ai/sandbox-runtime` and rerun `spec doctor`"
        )
    else:
        return AgentGitProbeResult(
            agent=agent,
            backend=backend,
            unavailable_reason=f"no sandbox probe for provider {agent!r}",
        )
    if not runner_path:
        return AgentGitProbeResult(agent=agent, backend=backend, unavailable_reason=missing)

    with tempfile.TemporaryDirectory(prefix="spec-git-probe-") as raw_root:
        root = Path(raw_root).resolve()
        try:
            workspace = prepare_probe_workspace(root, backend)
            state_dir = root / "state"
            outside = root / "outside"
            state_dir.mkdir()
            outside.mkdir()
            script_args = [str(workspace), *_probe_targets(workspace, outside)]
            env = _probe_environment(workspace, state_dir)
            if agent == "codex":
                provider_home = root / "codex-home"
                provider_home.mkdir()
                env["CODEX_HOME"] = str(provider_home)
                argv = codex_probe_command(
                    workspace, state_dir, script_args, codex_path=runner_path
                )
            else:
                argv = claude_probe_command(
                    workspace, state_dir, script_args, srt_path=runner_path
                )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            return AgentGitProbeResult(
                agent=agent,
                backend=backend,
                runner=runner_name,
                error=f"could not prepare the probe workspace: {exc}",
            )
        completed = run(argv, workspace, env, timeout)
        return parse_probe_output(agent, backend, runner_name, completed)
