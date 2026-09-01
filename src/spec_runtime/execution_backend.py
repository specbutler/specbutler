"""Execution backend boundary.

Defines the seam through which the orchestrator prepares workspaces, runs
commands, launches agents, and collects backend-owned artifacts.

:class:`WorktreeExecutionBackend` wraps the existing linked worktree behavior so
default user-visible flows are unchanged. :class:`CloneExecutionBackend`
materializes a backend-owned full checkout under ``.spec-workspaces``.

:class:`ContainerExecutionBackend` is an opt-in preview backend that runs
workspace commands through a Docker-compatible CLI while keeping host-owned
state, outbox, logs, and forge authority outside the worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import yaml

from .config import (
    ALLOWED_EXECUTION_BACKENDS,
    ALLOWED_EXECUTION_SAFETY_MODES,
    SUPPORTED_EXECUTION_BACKENDS,
    ExecutionConfig,
    SpecRuntimeConfig,
)
from .platform_fs import FileLock, remove_tree
from .process_supervisor import LifetimeMode, ProcessSupervisor

CONTAINER_WORKER_ENV_DENYLIST = frozenset(
    {
        "CDPATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GNUPGHOME",
        "HOME",
        "NODE_PATH",
        "OLDPWD",
        "PATH",
        "PWD",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELL",
        "SSH_AUTH_SOCK",
        # Host temp dirs (e.g. macOS TMPDIR=/var/folders/...) do not exist in
        # the container; a nonexistent TMPDIR hangs `claude -p` at startup.
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CREDENTIAL_HOME",
    }
)
CONTAINER_WORKER_ENV_SENSITIVE_MARKERS = (
    "AUTH",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
CONTAINER_WORKER_ENV_SECRET_ALLOWLIST = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)
CONTAINER_COMPLETION_OUTBOX_ENV = "SPEC_COMPLETION_OUTBOX"
CONTAINER_COMPLETION_ARTIFACT = "completion-report.json"
CONTAINER_BOOTSTRAP_SOURCE = "/workspace/bootstrap/source"
CONTAINER_RUNTIME_SOURCE = "/workspace/source"
CONTAINER_RUNTIME_STATE = f"{CONTAINER_RUNTIME_SOURCE}/.spec-state"
CONTAINER_RUNTIME_STATE_TMPFS = f"{CONTAINER_RUNTIME_STATE}:rw,noexec,nosuid,nodev,mode=1777"
CONTAINER_CODEX_SANDBOX_MODE = "danger-full-access"
CONTAINER_BOOTSTRAP_PATH = (
    f"{CONTAINER_BOOTSTRAP_SOURCE}/.venv/bin:"
    f"{CONTAINER_BOOTSTRAP_SOURCE}/node_modules/.bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
CONTAINER_RUNTIME_VENV_BIN = f"{CONTAINER_RUNTIME_SOURCE}/.venv/bin"
# Non-agent (gate/prep) commands additionally get the workspace venv on PATH.
# With the default cache-disabled bootstrap, ``[bootstrap].install_command``
# creates the venv at ``/workspace/source/.venv`` rather than baking it into the
# image at ``/workspace/bootstrap/source/.venv``, so bare ``pytest`` / ``ruff``
# gate commands would otherwise exit 127. The workspace venv is inserted *after*
# the baked bootstrap venv so cached-layer tools still win when both exist; a
# missing directory on PATH is harmless. Agent launches keep
# CONTAINER_BOOTSTRAP_PATH unchanged because their HOME/PATH contract is
# deliberate and must not shift.
CONTAINER_NON_AGENT_PATH = (
    f"{CONTAINER_BOOTSTRAP_SOURCE}/.venv/bin:"
    f"{CONTAINER_RUNTIME_VENV_BIN}:"
    f"{CONTAINER_BOOTSTRAP_SOURCE}/node_modules/.bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
CONTAINER_BOOTSTRAP_CACHE_FILENAMES = frozenset(
    {
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile",
        "Gemfile.lock",
        "Makefile",
        "go.mod",
        "go.sum",
        "gradle.lockfile",
        "justfile",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pom.xml",
        "pyproject.toml",
        "requirements-dev.txt",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
)
CONTAINER_SERVICE_POSTGRES_ENVS = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "SIM_DATABASE_URL",
    "SIM_TEST_DATABASE_URL",
)
CONTAINER_PLAYWRIGHT_ARTIFACT_PATHS = (
    "playwright-report",
    "test-results",
    "blob-report",
)


@dataclass(frozen=True)
class ContainerCapacityResult:
    """Fail-open capacity signal returned to schedulers."""

    available: bool
    endpoint_count: int | None = None
    threshold: int = 0
    warning: str = ""


def _is_adjacent_spec_runtime_checkout(source_root: Path) -> bool:
    """Return whether the imported module belongs to this source checkout.

    A wheel installed in ``<project>/.venv`` is nested inside the user's Git
    repository.  Merely running Git from the wheel path would therefore report
    the user's project as Spec Butler provenance.
    """
    pyproject = source_root / "pyproject.toml"
    source_module = source_root / "src" / "spec_runtime" / "execution_backend.py"
    try:
        if source_module.resolve() != Path(__file__).resolve():
            return False
        raw = tomllib.loads(pyproject.read_text())
        project = raw.get("project", {})
        return isinstance(project, dict) and project.get("name") == "specbutler"
    except (OSError, tomllib.TOMLDecodeError):
        return False


def host_spec_runtime_version() -> str:
    """Best-effort version of the spec_runtime running on the host.

    Editable installs carry stale pip metadata after a git pull, so prefer the
    pyproject.toml adjacent to the package source (it moves with the checkout)
    and fall back to installed distribution metadata for wheel installs.
    """
    try:
        source_root = Path(__file__).resolve().parents[2]
        pyproject = source_root / "pyproject.toml"
        if _is_adjacent_spec_runtime_checkout(source_root):
            match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
            if match:
                return match.group(1)
    except OSError:
        pass
    try:
        from importlib import metadata

        return metadata.version("specbutler")
    except Exception:
        return "unknown"


def host_spec_runtime_source_id() -> str:
    """Return version plus exact source provenance when it is available."""
    version = host_spec_runtime_version()
    commit_id = ""
    source_is_dirty = False
    try:
        from importlib import metadata

        dist = metadata.distribution("specbutler")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            vcs_info = direct_url.get("vcs_info", {})
            if isinstance(vcs_info, dict):
                commit_id = str(vcs_info.get("commit_id", "")).strip()
    except Exception:
        pass

    try:
        source_root = Path(__file__).resolve().parents[2]
        if _is_adjacent_spec_runtime_checkout(source_root):
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                # Editable installs can retain stale direct_url metadata after a pull.
                # The checkout containing the imported module is authoritative.
                commit_id = result.stdout.strip()
                status = subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=normal"],
                    cwd=source_root,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                source_is_dirty = status.returncode == 0 and bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    if not commit_id:
        return version
    dirty_suffix = "+dirty" if source_is_dirty else ""
    return f"{version}@{commit_id}{dirty_suffix}"


def inspect_container_capacity(
    config: SpecRuntimeConfig | ExecutionConfig,
    *,
    threshold: int,
    cwd: Path,
    runner: ContainerCliRunner | None = None,
) -> ContainerCapacityResult:
    """Inspect the default bridge without creating a container.

    Inspection errors deliberately fail open: inability to inspect capacity
    must not turn into a new global scheduling outage.
    """
    execution = config.execution if isinstance(config, SpecRuntimeConfig) else config
    engine = execution.container.engine
    cli = runner or ContainerCliRunner(engine)
    try:
        result = cli.run(
            [engine, "network", "inspect", "bridge"],
            cwd=cwd,
            timeout=10.0,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(detail)
        payload = json.loads(result.stdout)
        network = payload[0] if isinstance(payload, list) and payload else payload
        containers = network.get("Containers", {}) if isinstance(network, dict) else {}
        endpoint_count = len(containers) if isinstance(containers, dict) else 0
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return ContainerCapacityResult(
            available=True,
            threshold=threshold,
            warning=f"container capacity inspection failed open: {exc}",
        )
    if endpoint_count >= threshold:
        return ContainerCapacityResult(
            available=False,
            endpoint_count=endpoint_count,
            threshold=threshold,
            warning=(
                f"default bridge capacity is saturated ({endpoint_count} endpoints, "
                f"pause threshold {threshold}); container dispatch paused"
            ),
        )
    return ContainerCapacityResult(
        available=True,
        endpoint_count=endpoint_count,
        threshold=threshold,
    )


CONTAINER_PLAYWRIGHT_MCP_SIDECAR_PORT = 3001

# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendIdentity:
    """Resolved identity and safety profile of an execution backend."""

    backend: str
    safety_mode: str
    workspace_root: str
    backend_explicit: bool = False


@dataclass(frozen=True)
class WorkspaceHandle:
    """Workspace materialization returned by a backend.

    `path` is the directory the orchestrator should treat as the current
    working tree for the run. `outbox_path` is the directory the backend
    promises to keep available for host-mediated artifact collection (PR/MR
    metadata, logs).
    """

    path: Path
    outbox_path: Path
    branch: str = ""
    backend: str = "worktree"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandRequest:
    """Request to run a non-agent command in a workspace.

    Mirrors the semantics of the legacy ``run_subprocess`` helper so the
    worktree backend can replace it without behavior drift:

    * ``inherit_env`` controls whether ``env`` is layered on top of the
      orchestrator's process environment (matching ``run_subprocess``'s
      default-True behavior).
    * ``stdin_devnull`` ensures commands cannot accidentally inherit the
      orchestrator's stdin when no ``input_text`` is provided.
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str] | None = None
    inherit_env: bool = True
    timeout: float | None = None
    input_text: str | None = None
    stdin_devnull: bool = True
    redactions: Sequence[str] = ()


@dataclass(frozen=True)
class CommandResult:
    """Structured result of a backend-executed command."""

    returncode: int
    stdout: str
    stderr: str
    argv: list[str]


@dataclass(frozen=True)
class AgentRequest:
    """Request to launch an agent in a workspace.

    ``popen_kwargs`` carries any extra keyword arguments the orchestrator
    needs the backend to forward to ``subprocess.Popen`` (for example
    ``start_new_session=True``, ``text=True``, or ``stdout=PIPE``). The
    backend owns the actual process spawn so future backends can swap the
    transport (Docker exec, remote shell) without the orchestrator caring.
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str] | None = None
    capture_stdout: bool = False
    popen_kwargs: dict[str, Any] = field(default_factory=dict)
    redactions: Sequence[str] = ()


@dataclass(frozen=True)
class AgentResult:
    """Structured result of a backend-executed agent."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


AgentMonitor = Callable[["subprocess.Popen[Any]"], int]


@dataclass(frozen=True)
class OutboxMetadata:
    """Optional PR/MR metadata published by the backend.

    Absent metadata is valid: the host must fall back to its existing
    PR/MR generation behavior.
    """

    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    summary: str = ""
    head_sha: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotRef:
    """Backend snapshot reference."""

    label: str
    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutionBackend(Protocol):
    """Abstract execution boundary for a single run."""

    @property
    def identity(self) -> BackendIdentity:
        """Return the resolved identity and safety mode for this backend."""
        ...

    def prepare_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
    ) -> WorkspaceHandle:
        """Resolve or materialize a workspace for the run.

        Implementations must not mutate the workspace beyond what the prior
        worktree behavior already did. Backends that materialize fresh
        checkouts may add steps later, but the worktree backend simply
        resolves the existing path.
        """
        ...

    def run_command(self, request: CommandRequest) -> CommandResult:
        """Run a non-agent command in the workspace and return the result."""
        ...

    def launch_agent(
        self,
        request: AgentRequest,
        *,
        monitor: AgentMonitor | None = None,
    ) -> AgentResult:
        """Launch an agent process in the workspace.

        The agent command itself is built by :class:`AgentAdapter`; the
        backend owns the actual subprocess/exec semantics. When ``monitor``
        is provided the backend starts the process and hands the live
        ``Popen`` to the caller, who supervises completion (process
        registration, progress streaming, idle timeouts) and returns the
        final exit code. When ``monitor`` is ``None`` the backend runs the
        command to completion and returns the captured result.
        """
        ...

    def collect_outbox_metadata(self, workspace: WorkspaceHandle) -> OutboxMetadata | None:
        """Return optional PR/MR metadata produced by the workspace.

        Returns ``None`` if no metadata artifact was produced, which is the
        normal behavior for the worktree backend until the agent starts
        writing one.
        """
        ...

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        """Create a backend snapshot when supported."""
        ...

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        """Restore a backend snapshot when supported."""
        ...

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        """Clean up backend-owned workspace artifacts.

        For the worktree backend this is a no-op: the existing
        ``spec clean`` flow continues to manage the linked worktree.

        Backends that own the checkout must refuse deletion when the branch
        holds commits not reachable from any ``origin`` ref, unless
        ``allow_unpushed_work`` is set (post-merge cleanup / ``spec clean``).
        """
        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExecutionBackendNotImplementedError(RuntimeError):
    """Raised when a known but unimplemented backend is selected."""

    def __init__(self, backend: str):
        self.backend = backend
        super().__init__(
            f"Execution backend {backend!r} is not implemented yet in this "
            'spec slice. Set [execution].backend = "worktree" to opt out.'
        )


class ExecutionBackendImportError(RuntimeError):
    """Raised when backend-owned workspace output cannot be imported host-side."""

    def __init__(
        self,
        message: str,
        *,
        artifact_paths: Sequence[Path] = (),
    ):
        self.artifact_paths = tuple(Path(path) for path in artifact_paths)
        artifact_detail = ", ".join(str(path) for path in self.artifact_paths)
        if artifact_detail:
            message = f"{message} Artifacts: {artifact_detail}"
        super().__init__(message)


class UnknownExecutionBackendError(ValueError):
    """Raised when an unknown backend value reaches the factory."""

    def __init__(self, backend: str):
        allowed = ", ".join(sorted(ALLOWED_EXECUTION_BACKENDS))
        super().__init__(f"Unknown execution backend {backend!r}. Allowed: {allowed}")


class WorkspaceHasUnpushedWorkError(OSError):
    """Raised when a workspace deletion is refused because the worktree still
    holds work that is not durable in ``origin``.

    "Unpushed work" spans three states, any of which blocks deletion:

    - commits on ``HEAD`` not reachable from any ``origin`` ref,
    - uncommitted modifications to tracked files, and
    - untracked, non-ignored files (excluding orchestrator secrets).

    Subclasses :class:`OSError` so existing ``cleanup`` callers that catch
    ``OSError`` degrade to a recorded warning rather than crashing. Deletion is
    only permitted with ``allow_unpushed_work=True`` (the post-merge cleanup
    phase and ``spec clean``).
    """

    def __init__(
        self,
        source: Path,
        unpushed: Sequence[str],
        *,
        dirty: bool = False,
        untracked: Sequence[str] = (),
    ):
        self.source = source
        self.unpushed = tuple(unpushed)
        self.dirty = bool(dirty)
        self.untracked = tuple(untracked)
        reasons: list[str] = []
        if self.unpushed:
            preview = ", ".join(sha[:12] for sha in self.unpushed[:5])
            reasons.append(
                f"{len(self.unpushed)} commit(s) not present on any origin ref "
                f"({preview})"
            )
        if self.dirty:
            reasons.append("uncommitted changes to tracked files")
        if self.untracked:
            preview = ", ".join(self.untracked[:5])
            reasons.append(f"{len(self.untracked)} untracked file(s) ({preview})")
        detail = "; ".join(reasons) if reasons else "unpushed work"
        super().__init__(
            f"refusing to delete workspace {source}: worktree has {detail}. "
            "Push the branch or run `spec clean` to force removal."
        )


class WorkspaceRescueFailedError(OSError):
    """Raised when a restore detected work that must be preserved but failed to
    write a complete rescue artifact for it.

    ``restore`` replaces (and thereby destroys) the workspace tree, so it must
    only proceed once every category of non-durable work it detected — unpushed
    commits, uncommitted tracked edits, untracked non-secret files — has been
    successfully captured. If any required artifact write fails, raising this
    aborts the restore *before* the tree is replaced, leaving the agent's work
    in place. Subclasses :class:`OSError` so the retry-restore caller degrades
    to a recorded warning and returns the unmodified workspace.
    """

    def __init__(self, source: Path, categories: Sequence[str], manifest_path: str | None):
        self.source = source
        self.categories = tuple(categories)
        self.manifest_path = manifest_path
        detail = ", ".join(self.categories) if self.categories else "unpushed work"
        location = f" (partial rescue at {manifest_path})" if manifest_path else ""
        super().__init__(
            f"refusing to restore workspace {source}: failed to preserve "
            f"{detail} before replacing the tree{location}. Aborting so the "
            "work is not destroyed."
        )


# ---------------------------------------------------------------------------
# Worktree backend
# ---------------------------------------------------------------------------


_OUTBOX_METADATA_FILENAME = "pr-metadata.json"


def _read_outbox_metadata(outbox_path: Path) -> OutboxMetadata | None:
    candidate = outbox_path / _OUTBOX_METADATA_FILENAME
    if not candidate.is_file():
        return None
    try:
        payload = json.loads(candidate.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    labels_raw = payload.get("labels", [])
    if isinstance(labels_raw, list):
        labels = tuple(str(item) for item in labels_raw if str(item).strip())
    else:
        labels = ()
    return OutboxMetadata(
        title=str(payload.get("title", "") or "").strip(),
        body=str(payload.get("body", "") or ""),
        labels=labels,
        summary=str(payload.get("summary", "") or "").strip(),
        head_sha=str(payload.get("head_sha", "") or "").strip(),
        raw=payload,
    )


class WorktreeExecutionBackend:
    """Backend that wraps the existing linked-worktree behavior."""

    def __init__(self, config: ExecutionConfig):
        self._identity = BackendIdentity(
            backend=config.backend,
            safety_mode=config.safety_mode,
            workspace_root=config.workspace_root,
            backend_explicit=config.backend_explicit,
        )

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def prepare_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
    ) -> WorkspaceHandle:
        del base_ref
        if worktree_path is None:
            raise ValueError(
                "WorktreeExecutionBackend requires worktree_path; the linked "
                "worktree is created by the orchestrator before backend "
                "preparation in this slice."
            )
        return WorkspaceHandle(
            path=worktree_path,
            outbox_path=self._resolve_outbox_path(worktree_path),
            branch=branch,
            backend=self._identity.backend,
            metadata={
                "run_id": run_id,
                "spec_id": spec_id,
                "repo_root": str(repo_root),
            },
        )

    def run_command(self, request: CommandRequest) -> CommandResult:
        # The worktree backend delegates to the orchestrator's existing
        # subprocess helper so output, env handling, timeout, and stdin
        # behavior remain bit-for-bit identical to the pre-seam path. Future
        # backends (clone, container) replace this method end-to-end.
        from . import orchestrator  # lazy import: orchestrator imports us

        kwargs: dict[str, Any] = {"cwd": request.cwd, "env": request.env}
        if request.timeout is not None:
            kwargs["timeout"] = request.timeout
        if not request.inherit_env:
            kwargs["inherit_env"] = False
        if request.input_text is not None:
            kwargs["input_text"] = request.input_text
        completed = orchestrator.run_subprocess(list(request.argv), **kwargs)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=list(request.argv),
        )

    def launch_agent(
        self,
        request: AgentRequest,
        *,
        monitor: AgentMonitor | None = None,
    ) -> AgentResult:
        popen_kwargs: dict[str, Any] = dict(request.popen_kwargs)
        popen_kwargs.setdefault("cwd", str(request.cwd))
        if request.env is not None:
            popen_kwargs.setdefault("env", request.env)
        if monitor is None:
            popen_kwargs.setdefault("text", True)
            if request.capture_stdout:
                popen_kwargs.setdefault("stdout", subprocess.PIPE)
                popen_kwargs.setdefault("stderr", subprocess.PIPE)
            proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(request.argv, **popen_kwargs)
            stdout, stderr = proc.communicate()
            return AgentResult(
                returncode=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(request.argv, **popen_kwargs)
        returncode = monitor(proc)
        return AgentResult(returncode=returncode)

    def collect_outbox_metadata(self, workspace: WorkspaceHandle) -> OutboxMetadata | None:
        return _read_outbox_metadata(workspace.outbox_path)

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        raise NotImplementedError("worktree backend snapshots are not supported")

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        del snapshot
        raise NotImplementedError("worktree backend snapshots are not supported")

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        # The worktree backend defers cleanup to `spec clean`, which already
        # owns linked-worktree teardown. Nothing to do here.
        del allow_unpushed_work
        return None

    @staticmethod
    def _resolve_outbox_path(worktree_path: Path) -> Path:
        return worktree_path / ".spec-outbox"


class CloneExecutionBackend:
    """Backend that prepares a full disposable checkout for a run."""

    def __init__(self, config: ExecutionConfig):
        self._identity = BackendIdentity(
            backend=config.backend,
            safety_mode=config.safety_mode,
            workspace_root=config.workspace_root,
            backend_explicit=config.backend_explicit,
        )
        self._log_sequence = 0

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def prepare_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
    ) -> WorkspaceHandle:
        del worktree_path
        repo_root = repo_root.resolve()
        workspace_root = self._resolve_workspace_root(repo_root)
        self._ensure_safe_workspace_root(repo_root, workspace_root)
        self._ensure_workspace_root_ignored(repo_root, workspace_root)
        publish_remote_url = self._resolve_publish_remote_url(repo_root)

        run_root = workspace_root / run_id
        source = run_root / "source"
        outbox = run_root / "outbox"
        logs = run_root / "logs"
        outbox.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            self._clone_source_checkout(repo_root, source)
            self._copy_user_git_config(repo_root, source)
            self._write_git_state(source, logs, "clone")
        elif not (source / ".git").is_dir():
            raise RuntimeError(
                f"Clone backend source path exists but is not a full checkout: {source}. "
                "Remove the workspace directory and retry."
            )

        base_ref = base_ref or "origin/master"
        # The disposable clone's ``origin`` is rewritten to the forge URL for
        # host-owned publishing. On retry that clone may not have credentials
        # to refresh the forge, while the orchestration checkout already has
        # the refs fetched by merge/readiness checks. Copy those host-local
        # refs on every prepare so agents see the current base without needing
        # forge credentials inside the isolated workspace.
        self._copy_local_refs(repo_root, source)
        self._configure_publish_remote(source, publish_remote_url)
        self._ensure_ref_available(source, base_ref)
        self._checkout_branch(source, branch, base_ref)
        self._write_git_state(source, logs, "prepared")
        self._persist_base_ref(run_root, source, base_ref)
        return WorkspaceHandle(
            path=source,
            outbox_path=outbox,
            branch=branch,
            backend=self._identity.backend,
            metadata={
                "run_id": run_id,
                "spec_id": spec_id,
                "repo_root": str(repo_root),
                "workspace_root": str(workspace_root),
                "logs_path": str(logs),
                "base_ref": base_ref,
            },
        )

    def run_command(self, request: CommandRequest) -> CommandResult:
        env = None
        if request.inherit_env:
            env = os.environ.copy()
            if request.env:
                env.update(request.env)
        elif request.env is not None:
            env = dict(request.env)
        stdin = subprocess.DEVNULL if request.stdin_devnull and request.input_text is None else None
        completed = subprocess.run(
            request.argv,
            cwd=request.cwd,
            env=env,
            input=request.input_text,
            text=True,
            capture_output=True,
            timeout=request.timeout,
            stdin=stdin,
            check=False,
        )
        self._write_command_log(
            kind="command",
            cwd=request.cwd,
            argv=request.argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            redactions=request.redactions,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=list(request.argv),
        )

    def launch_agent(
        self,
        request: AgentRequest,
        *,
        monitor: AgentMonitor | None = None,
    ) -> AgentResult:
        popen_kwargs: dict[str, Any] = dict(request.popen_kwargs)
        popen_kwargs.setdefault("cwd", str(request.cwd))
        if request.env is not None:
            popen_kwargs.setdefault("env", request.env)
        if monitor is None:
            popen_kwargs.setdefault("text", True)
            if request.capture_stdout:
                popen_kwargs.setdefault("stdout", subprocess.PIPE)
                popen_kwargs.setdefault("stderr", subprocess.PIPE)
            proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(request.argv, **popen_kwargs)
            stdout, stderr = proc.communicate()
            self._write_command_log(
                kind="agent",
                cwd=request.cwd,
                argv=request.argv,
                returncode=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
            self._write_agent_result(
                cwd=request.cwd,
                argv=request.argv,
                returncode=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
            return AgentResult(
                returncode=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(request.argv, **popen_kwargs)
        returncode = monitor(proc)
        self._write_command_log(
            kind="agent",
            cwd=request.cwd,
            argv=request.argv,
            returncode=returncode,
            stdout="",
            stderr="",
        )
        self._write_agent_result(
            cwd=request.cwd,
            argv=request.argv,
            returncode=returncode,
            stdout="",
            stderr="",
        )
        return AgentResult(returncode=returncode)

    def collect_outbox_metadata(self, workspace: WorkspaceHandle) -> OutboxMetadata | None:
        return _read_outbox_metadata(workspace.outbox_path)

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        run_root = workspace.outbox_path.parent.resolve()
        snapshots = run_root / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        target = snapshots / _safe_artifact_name(label)
        if target.exists():
            remove_tree(target)
        shutil.copytree(workspace.path, target, symlinks=True)
        ref = SnapshotRef(
            label=label,
            path=target,
            metadata={
                "backend": self.identity.backend,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        (snapshots / f"{target.name}.json").write_text(
            json.dumps(ref.metadata | {"label": label, "path": str(target)}, indent=2, sort_keys=True)
        )
        return ref

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        # A restore replaces the workspace tree (including ``.git``) with the
        # snapshot, so any commits or uncommitted changes the agent produced
        # since the snapshot are about to be discarded. Preserve them first.
        run_root = workspace.outbox_path.parent.resolve()
        rescue = self._rescue_unpushed_work(
            workspace.path,
            run_root,
            reason=f"restore snapshot {snapshot.label!r}",
        )
        if rescue is not None:
            if not rescue.get("preserved", True):
                # Detected work we could not fully capture. Aborting here — before
                # ``_replace_workspace_tree`` (or the fresh-workspace fallback,
                # which also rmtrees) — leaves the agent's commits/edits/untracked
                # files intact in the workspace. The caller records the failure and
                # returns the unmodified workspace.
                self._record_snapshot_fallback(
                    workspace,
                    snapshot,
                    "aborting restore: incomplete rescue of "
                    f"{', '.join(rescue.get('unpreserved', []))} "
                    f"(manifest: {rescue.get('manifest_path')})",
                )
                raise WorkspaceRescueFailedError(
                    workspace.path,
                    rescue.get("unpreserved", []),
                    rescue.get("manifest_path"),
                )
            self._record_snapshot_fallback(
                workspace,
                snapshot,
                f"rescued unpushed work before restore: {rescue.get('manifest_path')}",
            )
        if not snapshot.path.is_dir():
            self._record_snapshot_fallback(workspace, snapshot, "snapshot path is missing")
            return self._restore_fresh_workspace_fallback(workspace, snapshot)
        try:
            self._replace_workspace_tree(workspace.path, snapshot.path)
        except OSError as exc:
            self._record_snapshot_fallback(
                workspace,
                snapshot,
                f"snapshot restore failed: {exc}",
            )
            return self._restore_fresh_workspace_fallback(workspace, snapshot)
        return workspace

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        run_root = workspace.outbox_path.parent.resolve()
        source = workspace.path.resolve()
        expected_source = run_root / "source"
        expected_outbox = run_root / "outbox"
        if source != expected_source or workspace.outbox_path.resolve() != expected_outbox:
            raise OSError(
                "refusing to clean clone backend workspace with inconsistent paths: "
                f"source={source}, outbox={workspace.outbox_path.resolve()}, run_root={run_root}"
            )
        self._assert_workspace_deletable(source, allow_unpushed_work=allow_unpushed_work)
        if run_root.name and run_root.parent.name:
            remove_tree(run_root)

    def _record_snapshot_fallback(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
        reason: str,
    ) -> None:
        logs = workspace.outbox_path.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "snapshot-restore-fallback.log"
        entry = "\n".join(
            [
                f"created_at: {datetime.now(timezone.utc).isoformat()}",
                f"snapshot: {snapshot.label}",
                f"path: {snapshot.path}",
                f"reason: {reason}",
            ]
        )
        with path.open("a") as handle:
            if path.stat().st_size:
                handle.write("\n---\n")
            handle.write(entry)

    def _restore_fresh_workspace_fallback(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        repo_root_raw = workspace.metadata.get("repo_root")
        run_id = str(workspace.metadata.get("run_id") or workspace.outbox_path.parent.name)
        spec_id = str(workspace.metadata.get("spec_id") or "")
        if not repo_root_raw or not workspace.branch:
            raise RuntimeError(
                "Snapshot restore fallback requires workspace metadata with "
                "repo_root and branch; refusing to continue in a dirty workspace."
            )
        repo_root = Path(str(repo_root_raw)).expanduser()
        if not repo_root.is_absolute():
            repo_root = repo_root.resolve()
        if workspace.path.exists():
            remove_tree(workspace.path)
        refreshed = self.prepare_workspace(
            run_id=run_id,
            spec_id=spec_id,
            branch=workspace.branch,
            repo_root=repo_root,
            base_ref=str(workspace.metadata.get("base_ref") or ""),
        )
        self._record_snapshot_fallback(
            refreshed,
            snapshot,
            "prepared fresh workspace after snapshot restore fallback",
        )
        return refreshed

    @staticmethod
    def _replace_workspace_tree(workspace_path: Path, snapshot_path: Path) -> None:
        workspace_path.mkdir(parents=True, exist_ok=True)
        for child in workspace_path.iterdir():
            if child.is_dir() and not child.is_symlink():
                remove_tree(child)
            else:
                child.unlink()
        for child in snapshot_path.iterdir():
            target = workspace_path / child.name
            if child.is_symlink():
                os.symlink(os.readlink(child), target)
            elif child.is_dir():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target, follow_symlinks=False)

    def _resolve_workspace_root(self, repo_root: Path) -> Path:
        configured = Path(self._identity.workspace_root).expanduser()
        if not configured.is_absolute():
            configured = repo_root / configured
        return configured.resolve()

    def _ensure_safe_workspace_root(self, repo_root: Path, workspace_root: Path) -> None:
        try:
            workspace_root.relative_to(repo_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Clone backend workspace_root must be inside the orchestration checkout: {workspace_root}"
            ) from exc
        if workspace_root == repo_root:
            raise RuntimeError(
                f"Clone backend refuses to use tracked source path as workspace_root: {workspace_root}. "
                "Choose an ignored directory such as .spec-workspaces."
            )
        rel = workspace_root.relative_to(repo_root).as_posix()
        tracked = self._run_git(["ls-files", "--", rel], cwd=repo_root)
        if tracked.returncode != 0:
            raise RuntimeError(
                f"Could not validate clone backend workspace_root {workspace_root}: {self._git_detail(tracked)}"
            )
        if tracked.stdout.strip():
            raise RuntimeError(
                f"Clone backend refuses to use tracked source path as workspace_root: {workspace_root}. "
                "Choose an ignored directory such as .spec-workspaces."
            )

    def _ensure_workspace_root_ignored(self, repo_root: Path, workspace_root: Path) -> None:
        rel = workspace_root.relative_to(repo_root).as_posix().rstrip("/") + "/"
        common_dir_result = self._run_git(["rev-parse", "--git-common-dir"], cwd=repo_root)
        common_dir_raw = common_dir_result.stdout.strip() if common_dir_result.returncode == 0 else ".git"
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = repo_root / common_dir
        exclude = common_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        if rel not in existing:
            with exclude.open("a") as handle:
                if existing and existing[-1].strip():
                    handle.write("\n")
                handle.write(f"{rel}\n")

    def _resolve_publish_remote_url(self, repo_root: Path) -> str:
        remote = self._run_git(["remote", "get-url", "origin"], cwd=repo_root)
        if remote.returncode != 0 or not remote.stdout.strip():
            detail = self._git_detail(remote)
            raise RuntimeError(
                "Clone backend requires an origin remote in the orchestration checkout "
                f"so host-owned publish can push to the forge remote: {detail}"
            )
        return remote.stdout.strip()

    def _clone_source_checkout(self, repo_root: Path, source: Path) -> None:
        clone = self._run_git(
            ["clone", "--local", "--no-checkout", str(repo_root), str(source)],
            cwd=repo_root,
        )
        if clone.returncode == 0:
            return
        if self._is_cross_device_link_clone_failure(clone):
            remove_tree(source, ignore_errors=True)
            retry = self._run_git(
                ["clone", "--no-local", "--no-checkout", str(repo_root), str(source)],
                cwd=repo_root,
            )
            if retry.returncode == 0:
                return
            raise RuntimeError(
                "git clone --local failed with a cross-device link error and "
                "git clone --no-local also failed while preparing clone backend "
                f"workspace at {source}: {self._git_detail(retry)}"
            )
        raise RuntimeError(
            f"git clone --local failed while preparing clone backend workspace at {source}: {self._git_detail(clone)}"
        )

    def _configure_publish_remote(self, source: Path, remote_url: str) -> None:
        current = self._run_git(["remote", "get-url", "origin"], cwd=source)
        action = "set-url" if current.returncode == 0 else "add"
        result = self._run_git(["remote", action, "origin", remote_url], cwd=source)
        if result.returncode != 0:
            raise RuntimeError(
                "Clone backend could not configure origin remote for host-owned "
                f"publish in {source}: {self._git_detail(result)}"
            )

    def _ensure_ref_available(self, source: Path, ref: str) -> None:
        local = self._run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=source)
        if local.returncode == 0:
            return
        remote_name, branch_name = ref.split("/", 1) if "/" in ref else ("origin", ref)
        remote = self._run_git(["remote", "get-url", remote_name], cwd=source)
        if remote.returncode != 0:
            raise RuntimeError(
                f"Base ref '{ref}' is not available locally and remote '{remote_name}' "
                "is not configured. Fetch the base ref in the orchestration checkout or "
                "configure a supported git remote."
            )
        fetch = self._run_git(["fetch", remote_name, branch_name], cwd=source)
        if fetch.returncode != 0:
            raise RuntimeError(
                f"Base ref '{ref}' is not available locally and could not be fetched "
                f"from remote '{remote_name}': {self._git_detail(fetch)}"
            )

    def _checkout_branch(self, source: Path, branch: str, base_ref: str) -> None:
        local_branch = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=source,
        )
        if local_branch.returncode == 0:
            checkout = self._run_git(["checkout", branch], cwd=source)
        elif (
            self._run_git(
                ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
                cwd=source,
            ).returncode
            == 0
        ):
            checkout = self._run_git(["checkout", "-B", branch, f"origin/{branch}"], cwd=source)
        else:
            checkout = self._run_git(["checkout", "-B", branch, base_ref], cwd=source)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"Could not check out implementation branch '{branch}' in clone backend "
                f"workspace {source}: {self._git_detail(checkout)}"
            )

    def _copy_user_git_config(self, repo_root: Path, source: Path) -> None:
        for key in ("user.name", "user.email"):
            value = self._run_git(["config", "--get", key], cwd=repo_root)
            if value.returncode == 0 and value.stdout.strip():
                self._run_git(["config", key, value.stdout.strip()], cwd=source)

    def _copy_local_refs(self, repo_root: Path, source: Path) -> None:
        for refspec in (
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/remotes/*:refs/remotes/*",
        ):
            copied = self._run_git(
                ["fetch", "--no-tags", str(repo_root), refspec],
                cwd=source,
            )
            if copied.returncode != 0:
                raise RuntimeError(
                    "Clone backend could not refresh host-local refs from "
                    f"{repo_root}: {self._git_detail(copied)}"
                )

    def _write_git_state(self, source: Path, logs: Path, label: str) -> None:
        status = self._run_git(["status", "--short", "--branch"], cwd=source)
        rev = self._run_git(["rev-parse", "HEAD"], cwd=source)
        (logs / f"git-state-{label}.txt").write_text(
            "\n".join(
                [
                    f"$ git status --short --branch\n{status.stdout}{status.stderr}",
                    f"$ git rev-parse HEAD\n{rev.stdout}{rev.stderr}",
                ]
            )
        )

    def _next_log_path(self, logs: Path, kind: str, argv: list[str]) -> Path:
        self._log_sequence += 1
        executable = Path(argv[0]).name if argv else "command"
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in executable)
        return logs / f"{self._log_sequence:04d}-{kind}-{safe_name}.log"

    def _workspace_run_root(self, cwd: Path) -> Path | None:
        current = cwd.resolve()
        candidates = [current, *current.parents]
        for candidate in candidates:
            if candidate.name == "source" and (candidate.parent / "logs").is_dir():
                return candidate.parent
        return None

    def _write_command_log(
        self,
        *,
        kind: str,
        cwd: Path,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
        redactions: Sequence[str] = (),
    ) -> None:
        run_root = self._workspace_run_root(cwd)
        if run_root is None:
            return
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = self._next_log_path(logs, kind, argv)
        payload = [
            f"started_at: {datetime.now(timezone.utc).isoformat()}",
            f"kind: {kind}",
            f"cwd: {cwd}",
            f"argv: {json.dumps([_redact_log_text(item, redactions) for item in argv])}",
            f"returncode: {returncode}",
            "",
            "stdout:",
            _redact_log_text(stdout, redactions),
            "",
            "stderr:",
            _redact_log_text(stderr, redactions),
        ]
        path.write_text("\n".join(payload))

    def _write_agent_result(
        self,
        *,
        cwd: Path,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        run_root = self._workspace_run_root(cwd)
        if run_root is None:
            return
        outbox = run_root / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        payload = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "cwd": str(cwd),
            "argv": list(argv),
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        (outbox / "agent-result.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        self._write_completion_artifacts(source=run_root / "source", outbox=outbox)

    def _write_completion_artifacts(self, *, source: Path, outbox: Path) -> None:
        if not (source / ".git").exists():
            return
        branch = self._run_git(["branch", "--show-current"], cwd=source)
        head = self._run_git(["rev-parse", "HEAD"], cwd=source)
        status = self._run_git(["status", "--short", "--branch"], cwd=source)
        recent = self._run_git(
            ["log", "--oneline", "--decorate", "-20"],
            cwd=source,
        )
        # Diff from the pre-attempt base rather than ``git diff HEAD``. A plain
        # ``git diff HEAD`` only reports the *uncommitted* working tree, so when
        # the agent commits its work (the normal flow) the patch is empty even
        # though real changes exist. Diffing against the recorded base captures
        # both committed and uncommitted changes made during the run. See
        # Regression: committed work must not be lost during container export.
        base_sha = self._read_persisted_base_sha(outbox.parent, source)
        diff_target = base_sha or "HEAD"
        diff = self._run_git(["diff", diff_target, "--binary"], cwd=source)
        metadata = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "branch": branch.stdout.strip() if branch.returncode == 0 else "",
            "head_sha": head.stdout.strip() if head.returncode == 0 else "",
            "base_sha": base_sha,
            "patch_base": diff_target,
            "status": status.stdout,
            "recent_commits": recent.stdout,
            "git_errors": {
                "branch": branch.stderr if branch.returncode != 0 else "",
                "head": head.stderr if head.returncode != 0 else "",
                "status": status.stderr if status.returncode != 0 else "",
                "recent_commits": recent.stderr if recent.returncode != 0 else "",
                "final_patch": diff.stderr if diff.returncode != 0 else "",
            },
        }
        (outbox / "commit-metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
        if diff.returncode == 0:
            (outbox / "final.patch").write_text(diff.stdout)

    _BASE_REF_FILENAME = "base-ref"

    def _persist_base_ref(self, run_root: Path, source: Path, base_ref: str) -> None:
        """Record the resolved base commit for later patch extraction.

        The base ref (e.g. ``origin/master``) is captured as a concrete SHA at
        prepare time so completion-artifact collection can diff committed work
        against it even after the working branch has advanced.
        """
        resolved = self._run_git(
            ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
            cwd=source,
        )
        sha = resolved.stdout.strip() if resolved.returncode == 0 else ""
        if not sha:
            return
        try:
            (run_root / self._BASE_REF_FILENAME).write_text(f"{sha}\n")
        except OSError:
            pass

    def _read_persisted_base_sha(self, run_root: Path, source: Path) -> str:
        """Return the recorded base SHA if it still resolves in ``source``."""
        try:
            sha = (run_root / self._BASE_REF_FILENAME).read_text().strip()
        except OSError:
            return ""
        if not sha:
            return ""
        check = self._run_git(
            ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=source,
        )
        if check.returncode != 0 or not check.stdout.strip():
            return ""
        return sha

    # ------------------------------------------------------------------
    # Resume-safety: never destroy agent work on retry/resume/cleanup.
    # ------------------------------------------------------------------

    RESCUE_DIRNAME = "rescue"
    RESCUE_INDEX_FILENAME = "index.json"
    # Orchestrator-staged Claude credentials live here. It is self-gitignored,
    # but the prefix is also filtered explicitly (defense in depth) so no rescue
    # snapshot or dirty-tree signal can ever capture staged secrets.
    SECRET_HOME_DIRNAME = ".spec-claude-home"

    @classmethod
    def _is_secret_path(cls, rel_path: str) -> bool:
        """Whether a repo-relative path lives under the orchestrator secret home."""
        normalized = rel_path.replace("\\", "/").strip("/")
        return normalized == cls.SECRET_HOME_DIRNAME or normalized.startswith(
            f"{cls.SECRET_HOME_DIRNAME}/"
        )

    def _untracked_files(self, source: Path) -> list[str]:
        """Return untracked, non-ignored file paths (repo-relative) worth saving.

        ``--exclude-standard`` honors ``.gitignore`` (so the self-gitignored
        ``.spec-claude-home`` is already skipped), and the secret prefix is then
        filtered again explicitly. These are files the agent created but never
        ``git add``-ed: they are lost by a tree-replacing restore and by
        deletion just as surely as committed work, so both the rescue snapshot
        and the deletion guard must account for them.
        """
        if not (source / ".git").exists():
            return []
        result = self._run_git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=source
        )
        if result.returncode != 0:
            return []
        files = [entry for entry in result.stdout.split("\0") if entry]
        return [rel for rel in files if not self._is_secret_path(rel)]

    def _unpushed_commits(self, source: Path) -> list[str]:
        """Return SHAs on ``HEAD`` not reachable from any ``origin`` ref.

        An empty list means the branch tip is fully published (or the source
        has no git checkout). Used both to gate destructive deletion and to
        decide whether a rescue snapshot is worth taking.
        """
        if not (source / ".git").exists():
            return []
        result = self._run_git(["rev-list", "HEAD", "--not", "--remotes=origin"], cwd=source)
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _has_uncommitted_changes(self, source: Path) -> bool:
        """Report whether tracked files carry uncommitted modifications.

        Only the *tracked* dirty signal lives here; untracked agent work is
        reported separately by :meth:`_untracked_files` (which filters secrets).
        ``--untracked-files=no`` keeps orchestrator-staged, self-gitignored
        credentials under ``.spec-claude-home`` out of this signal.
        """
        if not (source / ".git").exists():
            return False
        # ``--untracked-files=no`` keeps orchestrator-staged, self-gitignored
        # secrets out of the "dirty" signal and out of any rescue artifact.
        result = self._run_git(["status", "--porcelain", "--untracked-files=no"], cwd=source)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _rescue_unpushed_work(self, source: Path, run_root: Path, *, reason: str) -> dict[str, Any] | None:
        """Preserve unpushed commits and uncommitted tracked changes before a
        genuinely-required reset/restore discards them.

        Writes a git bundle of the unpushed commits and a binary patch of the
        uncommitted tracked changes under ``<run_root>/rescue/<stamp>/`` and
        appends an entry to ``<run_root>/rescue/index.json`` so the failure
        package for the next attempt can point at the snapshot. Returns the
        manifest dict, or ``None`` when there is nothing to preserve.

        Committed history (via ``git bundle``), tracked-file diffs (via
        ``git diff HEAD``), and untracked non-ignored files (copied verbatim)
        are captured. Self-gitignored orchestrator secrets under
        ``.spec-claude-home`` are excluded from every artifact.
        """
        if not (source / ".git").exists():
            return None
        unpushed = self._unpushed_commits(source)
        dirty = self._has_uncommitted_changes(source)
        untracked = self._untracked_files(source)
        if not unpushed and not dirty and not untracked:
            return None

        rescue_root = run_root / self.RESCUE_DIRNAME
        rescue_root.mkdir(parents=True, exist_ok=True)
        stamp = _safe_artifact_name(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        rescue_dir = rescue_root / stamp
        suffix = 0
        while rescue_dir.exists():
            suffix += 1
            rescue_dir = rescue_root / f"{stamp}-{suffix}"
        rescue_dir.mkdir(parents=True)

        head = self._run_git(["rev-parse", "HEAD"], cwd=source)
        manifest: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "source": str(source),
            "branch": self._run_git(["branch", "--show-current"], cwd=source).stdout.strip(),
            "head_sha": head.stdout.strip() if head.returncode == 0 else "",
            "unpushed_commits": list(unpushed),
            "artifacts": {},
        }

        # Categories of detected work whose rescue artifact failed to write.
        # Any entry here means the restore must abort rather than replace (and
        # destroy) the tree, since that work is not durable anywhere else.
        unpreserved: list[str] = []

        if unpushed:
            bundle_path = rescue_dir / "unpushed.bundle"
            bundle = self._run_git(
                ["bundle", "create", str(bundle_path), "HEAD", "--not", "--remotes=origin"],
                cwd=source,
            )
            if bundle.returncode == 0 and bundle_path.exists():
                manifest["artifacts"]["bundle"] = str(bundle_path)
            else:
                manifest.setdefault("errors", {})["bundle"] = self._git_detail(bundle)
                unpreserved.append("unpushed commits")

        if dirty:
            patch_path = rescue_dir / "uncommitted.patch"
            diff = self._run_git(["diff", "HEAD", "--binary"], cwd=source)
            if diff.returncode == 0:
                try:
                    patch_path.write_text(diff.stdout)
                except OSError as exc:
                    manifest.setdefault("errors", {})["uncommitted_patch"] = str(exc)
                    unpreserved.append("uncommitted tracked changes")
                else:
                    manifest["artifacts"]["uncommitted_patch"] = str(patch_path)
            else:
                manifest.setdefault("errors", {})["uncommitted_patch"] = self._git_detail(diff)
                unpreserved.append("uncommitted tracked changes")

        if untracked:
            untracked_dir = rescue_dir / "untracked"
            saved: list[str] = []
            for rel in untracked:
                src_file = source / rel
                dest = untracked_dir / rel
                if src_file.is_symlink():
                    # Preserve the agent's symlink verbatim rather than
                    # following it: copying the target would pull file content
                    # from *outside* the workspace (e.g. a link to /etc/passwd
                    # or a staged secret) into the rescue artifact.
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.symlink(os.readlink(src_file), dest)
                    except OSError as exc:
                        manifest.setdefault("errors", {}).setdefault("untracked", {})[rel] = str(exc)
                        continue
                    saved.append(rel)
                    continue
                if not src_file.is_file():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # ``follow_symlinks=False`` is defensive: the symlink branch
                    # above already handles links, so this only sees real files.
                    shutil.copy2(src_file, dest, follow_symlinks=False)
                except OSError as exc:
                    manifest.setdefault("errors", {}).setdefault("untracked", {})[rel] = str(exc)
                    continue
                saved.append(rel)
            if saved:
                manifest["artifacts"]["untracked_dir"] = str(untracked_dir)
                manifest["untracked_files"] = saved
            # Any untracked file that raised while being copied/symlinked is a
            # required-work loss: git tracks it nowhere else.
            if manifest.get("errors", {}).get("untracked"):
                unpreserved.append("untracked files")

        manifest["preserved"] = not unpreserved
        if unpreserved:
            manifest["unpreserved"] = unpreserved

        manifest_path = rescue_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        manifest["manifest_path"] = str(manifest_path)

        index_path = rescue_root / self.RESCUE_INDEX_FILENAME
        try:
            existing = json.loads(index_path.read_text()) if index_path.exists() else []
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []
        existing.append(manifest)
        index_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
        return manifest

    def _assert_workspace_deletable(self, source: Path, *, allow_unpushed_work: bool) -> None:
        """Refuse to delete a workspace that still holds non-durable agent work.

        Deletion is blocked when the worktree has unpushed commits, uncommitted
        edits to tracked files, or untracked non-ignored files — any of which
        would be permanently lost. The post-merge ``cleanup`` phase and
        ``spec clean`` pass ``allow_unpushed_work=True`` because the work is
        merged (or the operator explicitly asked to discard it); every other
        caller gets the guard.
        """
        if allow_unpushed_work:
            return
        unpushed = self._unpushed_commits(source)
        dirty = self._has_uncommitted_changes(source)
        untracked = self._untracked_files(source)
        if unpushed or dirty or untracked:
            raise WorkspaceHasUnpushedWorkError(
                source, unpushed, dirty=dirty, untracked=untracked
            )

    @staticmethod
    def _run_git(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _git_detail(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr.strip() or result.stdout.strip() or "unknown error")[-500:]

    @staticmethod
    def _is_cross_device_link_clone_failure(result: subprocess.CompletedProcess[str]) -> bool:
        detail = f"{result.stderr}\n{result.stdout}".lower()
        return "invalid cross-device link" in detail


def _safe_artifact_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in value)
    return cleaned.strip(".-") or "snapshot"


def _is_container_worker_env_allowed(key: str) -> bool:
    normalized = key.upper()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return False
    if normalized in CONTAINER_WORKER_ENV_DENYLIST:
        return False
    if any(marker in normalized for marker in CONTAINER_WORKER_ENV_SENSITIVE_MARKERS):
        return False
    return True


def _replace_host_path_reference(value: str, *, host_path: str, container_path: str) -> str:
    if not host_path:
        return value
    path_boundary = r"A-Za-z0-9_~/-"
    pattern = re.compile(rf"(?<![{path_boundary}]){re.escape(host_path)}(?=$|/|[^{path_boundary}])")
    return pattern.sub(container_path, value)


def _redact_log_text(text: str, redactions: Sequence[str]) -> str:
    redacted = text
    for secret in redactions:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


class ContainerCliRunner:
    """Small wrapper around a Docker-compatible command-line engine."""

    def __init__(self, engine: str):
        self.engine = engine

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            timeout=timeout,
            text=True,
            capture_output=True,
            check=False,
        )

    def popen(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        popen_kwargs: dict[str, Any] | None = None,
    ) -> subprocess.Popen[Any]:
        kwargs = dict(popen_kwargs or {})
        kwargs.setdefault("cwd", str(cwd))
        if env is not None:
            kwargs.setdefault("env", env)
        return ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(argv, **kwargs)


class ContainerExecutionBackend(CloneExecutionBackend):
    """Preview backend that runs workspace commands in a container worker."""

    def __init__(
        self,
        config: ExecutionConfig,
        *,
        bootstrap_install_command: str = "",
        bootstrap_cache_command: str = "",
        bootstrap_cache_inputs: Sequence[str] = (),
        runner: ContainerCliRunner | None = None,
        system_name: str | None = None,
    ):
        super().__init__(config)
        self._identity = BackendIdentity(
            backend=config.backend,
            safety_mode=config.safety_mode,
            workspace_root=config.workspace_root,
            backend_explicit=config.backend_explicit,
        )
        self._container = config.container
        self._bootstrap_install_command = bootstrap_install_command
        self._bootstrap_cache_command = bootstrap_cache_command
        self._bootstrap_cache_inputs = tuple(bootstrap_cache_inputs)
        self._runner = runner or ContainerCliRunner(self._container.engine)
        self._system_name = system_name or platform.system()

    def prepare_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
    ) -> WorkspaceHandle:
        self._ensure_engine_available()
        handle = super().prepare_workspace(
            run_id=run_id,
            spec_id=spec_id,
            branch=branch,
            repo_root=repo_root,
            worktree_path=worktree_path,
            base_ref=base_ref,
        )
        run_root = handle.outbox_path.parent
        logs = run_root / "logs"
        image = self._resolve_worker_image(repo_root=repo_root, run_root=run_root, logs=logs)
        self._ensure_container_passwd_shim(run_root=run_root, image=image)
        mode = self._effective_workspace_mode()
        service_topology = self._service_topology()
        service_env = self._service_env(service_topology)
        service_redactions = self._service_env_redactions(service_env)
        compose_project = self._compose_project_name(run_id) if service_topology == "sidecar" else ""
        compose_file = self._resolve_compose_file(handle.path) if service_topology == "sidecar" else None
        playwright_mcp = self._playwright_mcp_state(
            run_id=run_id,
            source=handle.path,
            logs=logs,
            image=image,
            service_topology=service_topology,
        )
        state = {
            "backend": "container",
            "engine": self._container.engine,
            "image": image,
            "workspace_mode": mode,
            "requested_workspace_mode": self._container.workspace_mode,
            "service_topology": service_topology,
            "service_env": service_env,
            "service_env_redactions": service_redactions,
            "service_processes": [],
            "service_ports": self._service_ports(service_topology),
            "service_data_dirs": self._service_data_dirs(handle.path, service_topology),
            "service_log_paths": self._service_log_paths(logs, service_topology),
            "playwright_mcp": playwright_mcp,
            "compose_file": str(compose_file) if compose_file is not None else "",
            "compose_project": compose_project,
            "source_path": str(handle.path),
            "outbox_path": str(handle.outbox_path),
            "logs_path": str(logs),
            "containers": [],
            "volumes": self._volume_names(run_id, mode),
            "workspace_volumes": self._volume_names(run_id, mode),
            "service_volumes": self._service_volume_names(run_id, service_topology),
            "networks": [],
            "service_networks": self._service_network_names(run_id, service_topology),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resource_labels": self._resource_labels(run_id=run_id, spec_id=spec_id, workspace_root=handle.path),
        }
        state["volumes"] = list(dict.fromkeys([*state["workspace_volumes"], *state["service_volumes"]]))
        state["networks"] = list(
            dict.fromkeys(
                [
                    *state["networks"],
                    *state["service_networks"],
                    *playwright_mcp.get("sidecar_networks", []),
                ]
            )
        )
        self._write_container_state(run_root, state)
        self._write_playwright_mcp_diagnostics(logs, playwright_mcp, service_env)
        self._remove_worker_visible_state(handle.path)
        # Tear the previous attempt's containers down before starting this
        # attempt's own, so retries run in a clean environment and containers
        # do not accumulate across a long run.
        # The returned flag records whether *every* prior-attempt container was
        # verified removed (docker inspect not-found — not merely rm exit 0);
        # postmaster.pid cleanup is gated on it so a live postmaster's lock is
        # never deleted out from under it.
        containers_verified_gone = self._teardown_previous_attempt_containers(run_root, state)
        try:
            if service_topology == "sidecar":
                assert compose_file is not None
                self._start_sidecar_services(
                    run_root=run_root,
                    logs=logs,
                    compose_file=compose_file,
                    compose_project=compose_project,
                )
                self._refresh_sidecar_service_volumes(run_root, state)
            if playwright_mcp.get("topology") == "sidecar":
                self._start_playwright_mcp_sidecar(run_root, logs, state)
            if mode == "volume":
                self._seed_volume_workspace(handle, state)
                # Seeding wipes and repopulates the workspace volume from the
                # host worktree, which can re-introduce a stale postmaster.pid
                # into the postgres data dir. Clear it *after* the reseed (and
                # before the worker starts) so the cleanup is not undone. Gate
                # it on every prior-attempt container being *verified* removed:
                # in volume mode postgres only ever runs inside a spec-runtime
                # container, so once they are all confirmed gone no live
                # postmaster can own the data dir. If teardown could not confirm
                # that, leave the pid untouched and let env prep fail loudly
                # rather than risk deleting a live postmaster's lock.
                self._clear_stale_volume_postmaster_pids(
                    run_root, state, containers_verified_gone
                )
            self._start_in_worker_container(run_root, state)
            self._run_container_bootstrap_install(handle)
        except Exception:
            # Service/container startup failed partway through. Tear down the
            # docker resources created so far (sidecars, worker container,
            # networks, volumes) so a startup failure does not leak them and
            # saturate the docker bridge. Preserve run_root/logs (including the
            # service-startup-failure.json diagnostic) for debugging. Best-
            # effort — never mask the original startup error being re-raised.
            teardown_state = self._read_container_state(run_root, missing_ok=True) or state
            self._teardown_container_resources(run_root, teardown_state)
            raise
        return WorkspaceHandle(
            path=handle.path,
            outbox_path=handle.outbox_path,
            branch=handle.branch,
            backend="container",
            metadata=handle.metadata
            | {
                "engine": self._container.engine,
                "image": image,
                "workspace_mode": mode,
                "service_topology": service_topology,
                "logs_path": str(logs),
                "container_state_path": str(self._container_state_path(run_root)),
            },
        )

    def _run_container_bootstrap_install(self, handle: WorkspaceHandle) -> None:
        if not self._bootstrap_install_command:
            return
        result = self.run_command(
            CommandRequest(
                argv=["sh", "-lc", self._bootstrap_install_command],
                cwd=handle.path,
                redactions=(self._bootstrap_install_command,),
            )
        )
        if result.returncode != 0:
            logs = handle.outbox_path.parent / "logs"
            raise RuntimeError(
                f"Container backend bootstrap install command failed. See container-command logs in {logs}"
            )

    def service_database_reachable(self, workspace_cwd: Path) -> bool:
        """Probe whether the backend-managed Postgres service answers from inside the worker.

        Verify gates use this to decide between letting the service env flow
        into the gate (reachable service DB — sidecar topology, or an in-worker
        Postgres the image actually starts) and forcing the repo's test recipe
        to self-provision (dead defaults would otherwise make skip-on-
        unavailable-DB suites pass vacuously).
        """
        run_root = self._workspace_run_root(workspace_cwd)
        if run_root is None:
            return False
        state = self._read_container_state(run_root)
        topology = str(state.get("service_topology") or self._service_topology())
        host = "postgres" if topology == "sidecar" else "127.0.0.1"
        try:
            result = self.run_command(
                CommandRequest(
                    argv=["bash", "-c", f'timeout 3 bash -c "</dev/tcp/{host}/5432"'],
                    cwd=workspace_cwd,
                    env={},
                    inherit_env=True,
                    timeout=30,
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def run_command(self, request: CommandRequest) -> CommandResult:
        run_root = self._workspace_run_root(request.cwd)
        if run_root is None:
            raise RuntimeError(f"Container backend command cwd is not inside a prepared workspace: {request.cwd}")
        state = self._read_container_state(run_root)
        argv = self._container_run_argv(
            run_root=run_root,
            cwd=request.cwd,
            command=request.argv,
            env=request.env or {},
            state=state,
        )
        env = self._container_client_env(
            request.env or {},
            inherit_env=request.inherit_env,
        )
        completed = self._runner.run(
            argv,
            cwd=run_root,
            env=env,
            input_text=request.input_text,
            timeout=request.timeout,
        )
        self._remember_container_id(run_root, state)
        self._sync_volume_workspace_to_host(run_root, state)
        self._write_command_log(
            kind="container-command",
            cwd=request.cwd,
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            redactions=(
                *self._service_log_redactions(state),
                *self._request_env_log_redactions(request.env or {}),
                *request.redactions,
            ),
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=list(request.argv),
        )

    def launch_agent(
        self,
        request: AgentRequest,
        *,
        monitor: AgentMonitor | None = None,
    ) -> AgentResult:
        run_root = self._workspace_run_root(request.cwd)
        if run_root is None:
            raise RuntimeError(f"Container backend agent cwd is not inside a prepared workspace: {request.cwd}")
        state = self._read_container_state(run_root)
        argv = self._container_run_argv(
            run_root=run_root,
            cwd=request.cwd,
            command=request.argv,
            env=request.env or {},
            state=state,
            agent=True,
        )
        client_env = self._container_client_env(request.env or {})
        if monitor is None:
            completed = self._runner.run(argv, cwd=run_root, env=client_env)
            self._remember_container_id(run_root, state)
            self._sync_volume_workspace_to_host(run_root, state)
            self._write_command_log(
                kind="container-agent",
                cwd=request.cwd,
                argv=argv,
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                redactions=(
                    *self._service_log_redactions(state),
                    *self._request_env_log_redactions(request.env or {}),
                    *request.redactions,
                ),
            )
            self._write_agent_result(
                cwd=request.cwd,
                argv=request.argv,
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
            return AgentResult(
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )

        popen_kwargs = dict(request.popen_kwargs)
        proc = self._runner.popen(
            argv,
            cwd=run_root,
            env=client_env,
            popen_kwargs=popen_kwargs,
        )
        returncode = monitor(proc)
        self._remember_container_id(run_root, state)
        self._sync_volume_workspace_to_host(run_root, state)
        self._write_command_log(
            kind="container-agent",
            cwd=request.cwd,
            argv=argv,
            returncode=returncode,
            stdout="",
            stderr="",
            redactions=(
                *self._service_log_redactions(state),
                *self._request_env_log_redactions(request.env or {}),
                *request.redactions,
            ),
        )
        self._write_agent_result(
            cwd=request.cwd,
            argv=request.argv,
            returncode=returncode,
            stdout="",
            stderr="",
        )
        return AgentResult(returncode=returncode)

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        run_root = workspace.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        sidecars_stopped = False
        try:
            if state.get("service_topology") == "sidecar":
                self._stop_sidecar_services(run_root, state)
                sidecars_stopped = True
                self._refresh_sidecar_service_volumes(run_root, state)
                self._snapshot_sidecar_service_volumes(run_root, state, label)
            self._sync_volume_workspace_to_host(run_root, state)
            ref = super().snapshot(workspace, label)
            metadata = ref.metadata | {
                "backend": "container",
                "snapshot_kind": "source-copy",
                "service_topology": state.get("service_topology", "in-worker"),
                "service_data_dirs": state.get("service_data_dirs", []),
                "service_volumes": state.get("service_volumes", []),
                "service_volume_snapshots": state.get("service_volume_snapshots", {}).get(label, {}),
            }
            (ref.path.parent / f"{ref.path.name}.json").write_text(
                json.dumps(metadata | {"label": label, "path": str(ref.path)}, indent=2, sort_keys=True)
            )
            return SnapshotRef(label=ref.label, path=ref.path, metadata=metadata)
        finally:
            if sidecars_stopped:
                compose_file = str(state.get("compose_file") or "")
                compose_project = str(state.get("compose_project") or "")
                if compose_file and compose_project:
                    self._start_sidecar_services(
                        run_root=run_root,
                        logs=run_root / "logs",
                        compose_file=Path(compose_file),
                        compose_project=compose_project,
                    )

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        run_root = workspace.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        reset_worker = bool(state.get("worker_container")) or state.get("service_topology") == "in-worker"
        if reset_worker:
            self._reset_in_worker_container(run_root, state)
        # For volume-mode workspaces the authoritative git state lives inside the
        # Docker volume, not the host ``source`` mirror. ``super().restore``
        # rescues unpushed work from the host mirror and then reseeds the volume
        # from the restored tree — so any commits or edits that reached only the
        # volume (a crash before the post-run sync) would be overwritten before
        # the rescue ever sees them. Re-sync the volume back to the host first so
        # the rescue snapshot captures the same content the reseed is about to
        # discard. Mirrors the deletability-guard sync in :meth:`cleanup`.
        if state.get("workspace_mode") == "volume":
            self._sync_volume_workspace_to_host(run_root, state)
        restored = super().restore(workspace, snapshot)
        run_root = restored.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        if state.get("workspace_mode") == "volume":
            self._seed_volume_workspace(restored, state)
        if reset_worker:
            self._start_in_worker_container(run_root, state)
            self._run_container_bootstrap_install(restored)
        if state.get("service_topology") == "sidecar":
            compose_file = str(state.get("compose_file") or "")
            compose_project = str(state.get("compose_project") or "")
            sidecars_stopped = False
            try:
                self._stop_sidecar_services(run_root, state)
                sidecars_stopped = True
                self._restore_sidecar_service_volumes(run_root, state, snapshot.label)
            finally:
                if sidecars_stopped and compose_file and compose_project:
                    self._start_sidecar_services(
                        run_root=run_root,
                        logs=run_root / "logs",
                        compose_file=Path(compose_file),
                        compose_project=compose_project,
                    )
        return restored

    def _teardown_container_resources(self, run_root: Path, state: dict[str, Any]) -> None:
        """Remove docker resources recorded in ``state`` (sidecars, service
        processes, containers, volumes, networks) *without* deleting the
        ``run_root`` filesystem, so captured logs/diagnostics survive.

        Used on the ``prepare_workspace`` failure path where a full
        :meth:`cleanup` would ``rmtree`` the run root and destroy the
        service-startup-failure diagnostic. Best-effort: every step swallows
        errors so teardown never masks the original startup failure.
        """
        if state.get("service_topology") == "sidecar":
            try:
                self._remove_sidecar_services(run_root, state)
            except Exception:
                pass
        try:
            self._remove_playwright_mcp_sidecar(run_root, state)
        except Exception:
            pass
        for process in state.get("service_processes", []):
            pid = process.get("pid") if isinstance(process, dict) else None
            if pid:
                try:
                    self._runner.run(
                        [self._container.engine, "kill", str(pid)],
                        cwd=run_root,
                    )
                except Exception:
                    pass
        for container_id in state.get("containers", []):
            if container_id:
                try:
                    self._runner.run(
                        [self._container.engine, "rm", "-f", str(container_id)],
                        cwd=run_root,
                    )
                except Exception:
                    pass
        for volume in state.get("volumes", []):
            if volume:
                try:
                    self._runner.run(
                        [self._container.engine, "volume", "rm", "-f", str(volume)],
                        cwd=run_root,
                    )
                except Exception:
                    pass
        for network in state.get("networks", []):
            if network:
                try:
                    self._runner.run(
                        [self._container.engine, "network", "rm", str(network)],
                        cwd=run_root,
                    )
                except Exception:
                    pass

    def _teardown_previous_attempt_containers(self, run_root: Path, state: dict[str, Any]) -> bool:
        """Remove containers left behind by earlier attempts of this run.

        Each implement/verify attempt of a container-backed run creates a
        fresh worker container, but backend state records only the latest id
        and ``spec container gc`` (correctly) protects everything labeled with
        an active run. Without this, the previous attempt's worker — and any
        sidecars it started — keep running, and their in-worker services
        corrupt later attempts through the shared workspace volume. Discover
        prior containers by label (backend state loses earlier ids on retry)
        and force-remove them before this attempt starts its own.

        Called before any container for this attempt is created, so every
        match belongs to a previous attempt; ids already recorded in
        ``state['containers']`` are excluded defensively. Best-effort and
        loudly logged: a teardown failure is recorded but never aborts the
        attempt (an operator can still clean up manually), and the label
        filter guarantees containers from other runs — and unlabeled
        containers — are never touched.

        Ordering: enumerate -> ``rm -f`` -> *verify gone* (``docker inspect``
        must report each removed id not-found — ``rm -f`` exit 0 alone is not
        proof) -> only then the conditional postmaster.pid cleanup. Returns
        ``True`` iff every enumerated prior-attempt container was verified
        removed (vacuously true when none exist). Postmaster.pid cleanup — both
        the bind-mode clear here and the deferred volume-mode clear in
        ``prepare_workspace`` — is gated on that flag: a stale pid may be
        removed ONLY when no prior-attempt container survived that could still
        host a live postmaster owning the data dir.
        """
        labels = state.get("resource_labels", {})
        run_id = str(labels.get("spec.run_id") or "")
        if not run_id:
            # Without a run_id we cannot scope teardown at all, so we cannot
            # assert any prior container is gone: treat as unverified and skip
            # the pid cleanup rather than risk deleting a live lock.
            return False
        engine = self._container.engine
        logs = run_root / "logs"
        own_ids = {str(cid) for cid in state.get("containers", []) if cid}
        # Match on labels only and return bare ids (``-q --no-trunc``): the
        # ``ps`` Command column is truncated by the CLI and must never be used
        # to identify our containers.
        discovery = self._runner.run(
            [
                engine,
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                "--filter",
                "label=spec.owner=spec-runtime",
                "--filter",
                f"label=spec.run_id={run_id}",
            ],
            cwd=run_root,
        )
        self._write_image_log(logs, "previous-attempt-teardown-discovery.log", discovery)
        if discovery.returncode != 0:
            # Discovery failed -> we do not know what prior containers exist,
            # so we cannot claim they are gone. Leave the pid untouched.
            return False
        stale_ids = [
            line.strip()
            for line in discovery.stdout.splitlines()
            if line.strip() and line.strip() not in own_ids
        ]
        all_verified_gone = True
        for container_id in stale_ids:
            try:
                removal = self._runner.run(
                    [engine, "rm", "-f", container_id],
                    cwd=run_root,
                )
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                self._write_teardown_failure_log(logs, container_id, str(exc))
                all_verified_gone = False
                continue
            self._write_image_log(
                logs,
                f"previous-attempt-teardown-{_safe_artifact_name(container_id)}.log",
                removal,
            )
            if removal.returncode != 0:
                self._write_teardown_failure_log(
                    logs,
                    container_id,
                    (removal.stderr or removal.stdout or "").strip(),
                )
                all_verified_gone = False
                continue
            # ``rm -f`` exit 0 is not proof the container is gone (engine
            # races/quirks). The invariant requires positive confirmation
            # before any postmaster.pid is cleared, so inspect it.
            if not self._verify_container_removed(run_root, container_id):
                self._write_teardown_failure_log(
                    logs,
                    container_id,
                    "docker inspect still reports the container present after rm -f",
                )
                all_verified_gone = False
        if all_verified_gone:
            # Bind-mode postmaster.pid cleanup can run now (the host worktree
            # *is* the workspace, so there is no reseed to undo it). Volume mode
            # is deferred to prepare_workspace, after the volume is reseeded.
            self._clear_stale_postmaster_pids(run_root, state)
        else:
            self._write_pid_cleanup_skipped_log(
                run_root,
                "prior-attempt container teardown not verified complete; "
                "leaving postmaster.pid untouched",
            )
        return all_verified_gone

    def _verify_container_removed(self, run_root: Path, container_id: str) -> bool:
        """True iff ``docker inspect`` confirms the container is not-found.

        ``rm -f`` reporting success is not sufficient evidence for the
        postmaster.pid invariant: only a not-found inspect proves the container
        (and any postmaster inside it) is really gone. Anything else — the
        container still present, or an inconclusive/errored inspect — is
        treated as NOT verified so the caller leaves the pid file untouched.
        """
        logs = run_root / "logs"
        try:
            result = self._runner.run(
                [self._container.engine, "inspect", "--type", "container", container_id],
                cwd=run_root,
            )
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            self._write_teardown_failure_log(logs, container_id, f"inspect failed: {exc}")
            return False
        self._write_image_log(
            logs,
            f"previous-attempt-teardown-verify-{_safe_artifact_name(container_id)}.log",
            result,
        )
        if result.returncode == 0:
            return False
        text = f"{result.stdout}\n{result.stderr}".lower()
        return "no such" in text or "not found" in text

    def _write_pid_cleanup_skipped_log(self, run_root: Path, reason: str) -> None:
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "previous-attempt-postmaster-cleanup-skipped.log").write_text(
            "\n".join(
                [
                    f"logged_at: {datetime.now(timezone.utc).isoformat()}",
                    reason,
                ]
            )
        )

    def _write_teardown_failure_log(self, logs: Path, container_id: str, detail: str) -> None:
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "previous-attempt-teardown-failures.log"
        entry = "\n".join(
            [
                f"logged_at: {datetime.now(timezone.utc).isoformat()}",
                f"container: {container_id}",
                f"detail: {detail or 'unknown error'}",
            ]
        )
        has_content = path.exists() and path.stat().st_size > 0
        with path.open("a") as handle:
            if has_content:
                handle.write("\n---\n")
            handle.write(entry)

    def _clear_stale_postmaster_pids(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> None:
        """Remove a stale bind-mode ``postmaster.pid`` from the host data dir.

        A previous attempt killed mid-flight leaves its postgres data dir
        holding a ``postmaster.pid`` whose owning process died with the
        removed container. Because the workspace is shared across a run's
        attempts, the next verify env prep otherwise fails with
        ``pg_ctl: another server might be running``. This runs only after
        :meth:`_teardown_previous_attempt_containers` has *verified* the run's
        containers removed, so no in-container postmaster can still own the
        file — which is the authoritative, and sufficient, "no postmaster owns
        it" signal: with an in-worker topology postgres only ever runs *inside*
        a spec-runtime worker container (the only topology that populates
        ``service_data_dirs``; see :meth:`_service_data_dirs`), and the worker
        container has its own PID namespace (no ``--pid=host``). The pid
        recorded in the file is therefore a *container-namespace* pid that is
        meaningless on the host, so we deliberately do NOT probe its liveness
        with a host-side ``os.kill``: a low container pid routinely collides
        with an unrelated live host process, which would misclassify a stale
        pid as "live" and leave it in place — re-introducing the exact
        ``pg_ctl: another server might be running`` failure this method exists
        to prevent. Once every prior-attempt container is verified gone the pid
        is unconditionally stale, mirroring the volume-mode path
        (:meth:`_clear_stale_postmaster_pids_in_volume`).

        Volume mode is handled separately by
        :meth:`_clear_stale_volume_postmaster_pids`, which must run *after*
        the workspace volume is reseeded (seeding wipes and repopulates the
        volume from the host worktree and would otherwise re-introduce the
        stale file, undoing a pre-seed cleanup).
        """
        if state.get("workspace_mode") == "volume":
            return
        data_dirs = [str(item) for item in state.get("service_data_dirs", []) if item]
        if not data_dirs:
            return
        removed: list[str] = []
        for data_dir in data_dirs:
            pid_file = Path(data_dir) / "postmaster.pid"
            if not pid_file.is_file():
                continue
            try:
                pid_file.unlink()
            except OSError:
                continue
            removed.append(str(pid_file))
        logs = run_root / "logs"
        if removed:
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "previous-attempt-postmaster-cleanup.log").write_text(
                "\n".join(
                    [
                        f"completed_at: {datetime.now(timezone.utc).isoformat()}",
                        "removed stale postmaster.pid (no postmaster process owns it "
                        "after teardown):",
                        *removed,
                    ]
                )
            )

    def _clear_stale_volume_postmaster_pids(
        self,
        run_root: Path,
        state: dict[str, Any],
        containers_verified_gone: bool,
    ) -> None:
        """Volume-mode stale postmaster.pid cleanup, run after the reseed.

        In volume mode the postgres data dir lives inside the shared workspace
        volume, not on the host mirror. This must be invoked *after*
        :meth:`_seed_volume_workspace` has repopulated the volume — otherwise
        the reseed re-introduces the stale ``postmaster.pid`` and undoes the
        cleanup before the new worker starts.

        Gated on ``containers_verified_gone``: in volume mode postgres only ever
        runs inside a spec-runtime container, so once every prior-attempt
        container is *verified* removed no live postmaster can own the data dir
        and the stale pid is safe to clear. If teardown could not confirm that
        (rm -f failed, inspect still found a container, discovery errored), the
        pid is left untouched so a live postmaster's lock is never deleted.
        """
        if not containers_verified_gone:
            self._write_pid_cleanup_skipped_log(
                run_root,
                "prior-attempt container teardown not verified complete; "
                "leaving volume-mode postmaster.pid untouched",
            )
            return
        data_dirs = [str(item) for item in state.get("service_data_dirs", []) if item]
        if not data_dirs:
            return
        self._clear_stale_postmaster_pids_in_volume(run_root, state, data_dirs)

    def _clear_stale_postmaster_pids_in_volume(
        self,
        run_root: Path,
        state: dict[str, Any],
        data_dirs: list[str],
    ) -> None:
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        image = str(state.get("image") or "")
        if not volumes or not image:
            return
        volume = str(volumes[0])
        host_source = (run_root / "source").resolve()
        targets: list[str] = []
        for data_dir in data_dirs:
            try:
                rel = Path(data_dir).resolve().relative_to(host_source)
            except ValueError:
                continue
            targets.append(f"{CONTAINER_RUNTIME_SOURCE}/{rel.as_posix()}/postmaster.pid")
        if not targets:
            return
        script = " ; ".join(f"rm -f {shlex.quote(target)}" for target in targets)
        result = self._runner.run(
            [
                self._container.engine,
                "run",
                "--rm",
                *self._label_argv(state),
                "-v",
                f"{volume}:/workspace/source",
                image,
                "sh",
                "-lc",
                script,
            ],
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "previous-attempt-postmaster-cleanup.log", result)

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        run_root = workspace.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        # For volume-mode workspaces the authoritative git state lives inside the
        # Docker volume, not the host ``source`` mirror. The post-run sync
        # (``_sync_volume_workspace_to_host``) normally keeps them in step, but a
        # crash mid-session — or a cleanup/resume that fires before that sync —
        # can leave commits or edits only in the volume while the host mirror
        # reads clean. Re-sync the volume back to the host before the guard so
        # the deletability check reasons about the same content ``volume rm -f``
        # would destroy. Skipped when deletion is already authorized
        # (post-merge cleanup / ``spec clean``), where the work is durable.
        if not allow_unpushed_work and state.get("workspace_mode") == "volume":
            self._sync_volume_workspace_to_host(run_root, state)
        # Refuse before tearing down any docker resources so a guarded deletion
        # leaves the run fully recoverable.
        self._assert_workspace_deletable(
            (run_root / "source").resolve(),
            allow_unpushed_work=allow_unpushed_work,
        )
        if state.get("service_topology") == "sidecar":
            self._remove_sidecar_services(run_root, state)
        self._remove_playwright_mcp_sidecar(run_root, state)
        for process in state.get("service_processes", []):
            pid = process.get("pid") if isinstance(process, dict) else None
            if pid:
                self._runner.run(
                    [self._container.engine, "kill", str(pid)],
                    cwd=run_root,
                )
        for data_dir in state.get("service_data_dirs", []):
            path = Path(str(data_dir))
            try:
                path.relative_to((run_root / "source").resolve())
            except ValueError:
                continue
            if path.exists():
                remove_tree(path, ignore_errors=True)
        for container_id in state.get("containers", []):
            if container_id:
                self._runner.run(
                    [self._container.engine, "rm", "-f", str(container_id)],
                    cwd=run_root,
                )
        for volume in state.get("volumes", []):
            if volume:
                self._runner.run(
                    [self._container.engine, "volume", "rm", "-f", str(volume)],
                    cwd=run_root,
                )
        for network in state.get("networks", []):
            if network:
                self._runner.run(
                    [self._container.engine, "network", "rm", str(network)],
                    cwd=run_root,
                )
        super().cleanup(workspace, allow_unpushed_work=allow_unpushed_work)

    def _ensure_engine_available(self) -> None:
        if shutil.which(self._container.engine):
            return
        raise RuntimeError(
            "Container execution backend requires a Docker-compatible CLI, but "
            f"{self._container.engine!r} was not found on PATH. Install Docker Desktop, "
            "OrbStack, Colima, Rancher Desktop, Docker Engine, or configure a "
            "Docker-compatible Podman CLI."
        )

    def _resolve_worker_image(self, *, repo_root: Path, run_root: Path, logs: Path) -> str:
        if self._container.image:
            inspect = self._runner.run(
                [self._container.engine, "image", "inspect", self._container.image],
                cwd=repo_root,
            )
            if inspect.returncode != 0:
                pull = self._runner.run(
                    [self._container.engine, "pull", self._container.image],
                    cwd=repo_root,
                )
                self._write_image_log(logs, "image-pull.log", pull)
                if pull.returncode != 0:
                    raise RuntimeError(
                        "Container backend could not pull configured image "
                        f"{self._container.image!r}. See {logs / 'image-pull.log'}"
                    )
            return self._container.image

        dockerfile = Path(self._container.dockerfile).expanduser()
        if not dockerfile.is_absolute():
            dockerfile = repo_root / dockerfile
        if not dockerfile.is_file():
            raise RuntimeError(
                f"Container backend requires [execution.container].image or an existing dockerfile at {dockerfile}"
            )
        tag = self._deterministic_image_tag(repo_root, dockerfile)
        lock_dir = run_root.parent / ".image-build-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_digest = hashlib.sha256(f"{self._container.engine}\0{tag}".encode()).hexdigest()
        with FileLock(lock_dir / f"{lock_digest}.lock"):
            # Concurrent autopilot runs commonly resolve the same cold image.
            # Serialize the inspect/build decision so the first process builds
            # it and every waiter reuses that completed tag.
            inspect = self._runner.run(
                [self._container.engine, "image", "inspect", tag],
                cwd=repo_root,
            )
            if inspect.returncode == 0:
                reuse = subprocess.CompletedProcess(
                    inspect.args,
                    0,
                    f"Reused existing deterministic worker image {tag}.\n",
                    "",
                )
                self._write_image_log(logs, "image-build.log", reuse)
                return tag

            build_context = self._prepare_build_context(
                repo_root=repo_root,
                run_root=run_root,
                dockerfile=dockerfile,
            )
            build_argv: list[str] = [self._container.engine, "build"]
            # Docker's layer cache is independent of the image tag, so a legacy
            # Dockerfile that never references SPEC_BUTLER_VERSION would
            # still reuse a stale spec install layer under the new tag. Builds
            # only run when the tag is new (one per spec upgrade), so forcing
            # --no-cache for those Dockerfiles buys correctness at bounded cost;
            # template-generated Dockerfiles cache normally via the ARG reference.
            if "SPEC_BUTLER_VERSION" not in dockerfile.read_text(errors="replace"):
                build_argv.append("--no-cache")
            if self._container.build_ssh:
                build_argv.extend(["--ssh", self._container.build_ssh])
            build_argv.extend(
                [
                    "-t",
                    tag,
                    "-f",
                    str(build_context / "Dockerfile"),
                ]
            )
            if self._bootstrap_cache_command:
                build_argv.extend(
                    [
                        "--build-arg",
                        f"SPEC_BOOTSTRAP_CACHE_COMMAND={self._bootstrap_cache_command}",
                    ]
                )
            # Cache-bust the spec install layer on spec upgrades. Dockerfiles that
            # reference ARG SPEC_BUTLER_VERSION in their install RUN rebuild
            # exactly when the host version changes; older Dockerfiles ignore the
            # unused arg, so older custom Dockerfiles need ``--no-cache`` to
            # avoid serving an outdated pip layer under a fresh-looking tag.
            build_argv.extend(
                [
                    "--build-arg",
                    f"SPEC_BUTLER_VERSION={host_spec_runtime_version()}",
                ]
            )
            build_argv.append(".")
            build_env: dict[str, str] | None = None
            if self._container.build_ssh:
                build_env = {**os.environ, "DOCKER_BUILDKIT": "1"}
            build = self._runner.run(
                build_argv,
                cwd=build_context,
                env=build_env,
            )
            self._write_image_log(
                logs,
                "image-build.log",
                build,
                redactions=[self._bootstrap_cache_command, self._container.build_ssh],
            )
            if build.returncode != 0:
                raise RuntimeError(
                    f"Container backend image build failed for {tag}. See {logs / 'image-build.log'}"
                )
            return tag

    def _prepare_build_context(
        self,
        *,
        repo_root: Path,
        run_root: Path,
        dockerfile: Path,
    ) -> Path:
        context = run_root / "container-build"
        if context.exists():
            remove_tree(context)
        context.mkdir(parents=True)
        manifest_dir = context / "dependency-inputs"
        manifest_dir.mkdir()
        for rel in self._bootstrap_cache_input_paths(repo_root):
            src = repo_root / rel
            if src.is_file():
                target = manifest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        self._copy_build_source(repo_root=repo_root, context=context)
        source_dockerfile = dockerfile.read_text()
        wrapper = [
            source_dockerfile.rstrip(),
            "",
            f"WORKDIR {CONTAINER_BOOTSTRAP_SOURCE}",
            f"COPY dependency-inputs/ {CONTAINER_BOOTSTRAP_SOURCE}/",
        ]
        if self._bootstrap_cache_command:
            ssh_mount = "--mount=type=ssh " if self._container.build_ssh else ""
            wrapper.extend(
                [
                    "ARG SPEC_BOOTSTRAP_CACHE_COMMAND",
                    f'RUN {ssh_mount}if [ -n "$SPEC_BOOTSTRAP_CACHE_COMMAND" ]; then '
                    f"cd {CONTAINER_BOOTSTRAP_SOURCE} && "
                    'sh -lc "$SPEC_BOOTSTRAP_CACHE_COMMAND"; fi',
                ]
            )
        wrapper.append(f"COPY . {CONTAINER_BOOTSTRAP_SOURCE}/")
        (context / "Dockerfile").write_text("\n".join(wrapper) + "\n")
        (context / "README.txt").write_text(
            "Generated by spec container backend. Dependency manifests are copied "
            "with their repo-relative paths before the optional bootstrap cache "
            "layer, and full source is copied after that layer so dependency "
            "installs can be cached across ordinary source edits. Runtime source "
            "is mounted at /workspace/source so it does not hide the cached "
            "bootstrap layer under /workspace/bootstrap.\n"
        )
        return context

    def _bootstrap_cache_input_paths(self, repo_root: Path) -> list[Path]:
        if self._bootstrap_cache_inputs:
            candidates = [Path(item) for item in self._bootstrap_cache_inputs]
        else:
            tracked = self._run_git(["ls-files", "-z"], cwd=repo_root)
            if tracked.returncode != 0:
                return []
            candidates = [
                Path(rel_text)
                for rel_text in tracked.stdout.split("\0")
                if rel_text and Path(rel_text).name in CONTAINER_BOOTSTRAP_CACHE_FILENAMES
            ]
        paths: list[Path] = []
        seen: set[str] = set()
        for rel in candidates:
            if rel.is_absolute() or ".." in rel.parts:
                continue
            key = rel.as_posix()
            if key in seen:
                continue
            seen.add(key)
            if (repo_root / rel).is_file():
                paths.append(rel)
        return sorted(paths, key=lambda path: path.as_posix())

    def _copy_build_source(self, *, repo_root: Path, context: Path) -> None:
        tracked = self._run_git(["ls-files", "-z"], cwd=repo_root)
        if tracked.returncode != 0:
            raise RuntimeError(
                f"Container backend could not list tracked files for image build context: {self._git_detail(tracked)}"
            )
        for rel_text in tracked.stdout.split("\0"):
            if not rel_text:
                continue
            rel = Path(rel_text)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            src = repo_root / rel
            if not src.exists() and not src.is_symlink():
                continue
            target = context / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                target.symlink_to(os.readlink(src))
            elif src.is_file():
                shutil.copy2(src, target)

    def _deterministic_image_tag(self, repo_root: Path, dockerfile: Path) -> str:
        remote = self._run_git(["remote", "get-url", "origin"], cwd=repo_root)
        identity = remote.stdout.strip() if remote.returncode == 0 else str(repo_root.resolve())
        digest = hashlib.sha256()
        digest.update(identity.encode())
        digest.update(b"\0")
        digest.update(dockerfile.read_bytes())
        digest.update(b"\0")
        digest.update(self._bootstrap_cache_command.encode())
        digest.update(b"\0")
        for rel in self._bootstrap_cache_input_paths(repo_root):
            digest.update(rel.as_posix().encode())
            digest.update(b"\0")
            digest.update((repo_root / rel).read_bytes())
            digest.update(b"\0")
        # Include the host spec version so upgrading spec produces a new tag
        # and a rebuild. Without it, images can pin an outdated spec_runtime
        # behind a "current" tag because nothing in the digest changes.
        digest.update(host_spec_runtime_version().encode())
        return f"spec-worker:{digest.hexdigest()[:24]}"

    def _effective_workspace_mode(self) -> str:
        mode = self._container.workspace_mode
        if mode != "auto":
            return mode
        return "volume" if self._system_name == "Darwin" else "bind"

    @staticmethod
    def _resource_labels(*, run_id: str, spec_id: str, workspace_root: Path) -> dict[str, str]:
        return {
            "spec.owner": "spec-runtime",
            "spec.run_id": run_id,
            "spec.spec_id": spec_id,
            "spec.phase": "execution",
            "spec.workspace_root": str(workspace_root.resolve()),
        }

    @staticmethod
    def _label_argv(state: dict[str, Any]) -> list[str]:
        argv: list[str] = []
        for key, value in sorted(state.get("resource_labels", {}).items()):
            argv.extend(["--label", f"{key}={value}"])
        return argv

    def _volume_names(self, run_id: str, mode: str) -> list[str]:
        if mode != "volume":
            return []
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        return [f"spec-{digest}-source"]

    def _service_topology(self) -> str:
        return "sidecar" if self._container.compose_file.strip() else "in-worker"

    def _service_env(self, topology: str) -> dict[str, str]:
        host = "postgres" if topology == "sidecar" else "127.0.0.1"
        value = f"postgresql://spec:spec@{host}:5432/spec"
        return {key: value for key in CONTAINER_SERVICE_POSTGRES_ENVS}

    @staticmethod
    def _service_env_redactions(service_env: dict[str, str]) -> dict[str, str]:
        return {key: "<redacted>" for key in service_env}

    @staticmethod
    def _service_log_redactions(state: dict[str, Any]) -> list[str]:
        return [str(value) for value in state.get("service_env", {}).values()]

    @staticmethod
    def _request_env_log_redactions(env: dict[str, str]) -> list[str]:
        return [value for key, value in env.items() if key in CONTAINER_WORKER_ENV_SECRET_ALLOWLIST and value]

    @staticmethod
    def _container_client_env(
        env: dict[str, str],
        *,
        inherit_env: bool = True,
    ) -> dict[str, str] | None:
        client_env = os.environ.copy() if inherit_env else None
        secret_env = {
            key: value for key, value in env.items() if key in CONTAINER_WORKER_ENV_SECRET_ALLOWLIST and value
        }
        if not secret_env:
            return client_env
        if client_env is None:
            client_env = os.environ.copy()
        client_env.update(secret_env)
        return client_env

    @staticmethod
    def _service_ports(topology: str) -> list[dict[str, Any]]:
        host = "postgres" if topology == "sidecar" else "127.0.0.1"
        return [{"name": "postgres", "host": host, "port": 5432, "protocol": "tcp"}]

    @staticmethod
    def _service_data_dirs(source: Path, topology: str) -> list[str]:
        if topology != "in-worker":
            return []
        return [str((source / ".local" / "postgres" / "data").resolve())]

    @staticmethod
    def _service_log_paths(logs: Path, topology: str) -> list[str]:
        name = "compose-services.log" if topology == "sidecar" else "in-worker-services.log"
        return [str(logs / name)]

    def _service_volume_names(self, run_id: str, topology: str) -> list[str]:
        if topology != "sidecar":
            return []
        return []

    def _service_network_names(self, run_id: str, topology: str) -> list[str]:
        if topology != "sidecar":
            return []
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        return [f"spec-{digest}_default"]

    def _playwright_mcp_state(
        self,
        *,
        run_id: str,
        source: Path,
        logs: Path,
        image: str,
        service_topology: str,
    ) -> dict[str, Any]:
        config = self._container.playwright_mcp
        topology = config.topology
        if topology == "sidecar" and not (config.app_url or config.sidecar_endpoint):
            raise RuntimeError(
                "Container Playwright MCP sidecar topology requires "
                "[execution.container.playwright_mcp].app_url or sidecar_endpoint "
                "so the target app is explicitly reachable from the sidecar."
            )
        expected = config.expected_version or self._detect_playwright_version(source)
        command = config.command or self._default_playwright_mcp_command(source)
        args = list(
            config.args
            or self._default_playwright_mcp_args(
                source, expected_version=expected, browser=config.browser
            )
        )
        actual = config.actual_version or (
            self._detect_worker_playwright_version(image=image, cwd=source) if expected else ""
        )
        if expected and actual and expected != actual:
            failure_path = logs / "playwright-mcp-version-mismatch.json"
            remediation = (
                "Install browser dependencies through the repo setup path or use "
                "a worker image whose Playwright browsers match the configured "
                f"Playwright package version {expected}."
            )
            failure_path.write_text(
                json.dumps(
                    {
                        "backend": "container",
                        "failure_type": "browser_runtime",
                        "failure_subtype": "playwright_version_mismatch",
                        "expected_version": expected,
                        "actual_version": actual,
                        "remediation": remediation,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            raise RuntimeError(
                "Playwright MCP browser/runtime version mismatch: "
                f"expected {expected}, actual {actual}. {remediation} "
                f"See {failure_path}"
            )
        target_app_url = self._playwright_target_app_url(
            topology=topology,
            app_url=config.app_url,
            sidecar_endpoint=config.sidecar_endpoint,
            service_topology=service_topology,
        )
        sidecar_networks: list[str] = []
        sidecar_container = ""
        sidecar_mcp_server: dict[str, Any] = {}
        sidecar_mcp_transport = ""
        sidecar_mcp_note = ""
        sidecar_mcp_port = 0
        if topology == "sidecar":
            digest = hashlib.sha256(f"{run_id}:playwright-mcp".encode()).hexdigest()[:16]
            sidecar_networks = [f"spec-{digest}-playwright-mcp"]
            sidecar_container = f"spec-{digest}-playwright-mcp"
            sidecar_mcp_port = CONTAINER_PLAYWRIGHT_MCP_SIDECAR_PORT
            sidecar_mcp_transport = "sse"
            sidecar_mcp_note = (
                "Playwright MCP runs in a host-managed sidecar and exposes an "
                "SSE endpoint on the sidecar's docker network. The worker is "
                "attached to that network so containerized agents can reach the "
                "MCP server without a host container engine socket."
            )
            sidecar_mcp_server = {
                "type": "sse",
                "url": f"http://{sidecar_container}:{sidecar_mcp_port}/sse",
            }
        return {
            "enabled": topology != "disabled",
            "topology": topology,
            "command": command,
            "args": args,
            "target_app_url": target_app_url,
            "expected_version": expected,
            "actual_version": actual,
            "artifact_paths": [str(source / name) for name in CONTAINER_PLAYWRIGHT_ARTIFACT_PATHS],
            "sidecar_container": sidecar_container,
            "sidecar_endpoint": config.sidecar_endpoint,
            "sidecar_networks": sidecar_networks,
            "sidecar_mcp_port": sidecar_mcp_port,
            "sidecar_mcp_server": sidecar_mcp_server,
            "sidecar_mcp_transport": sidecar_mcp_transport,
            "sidecar_mcp_note": sidecar_mcp_note,
            "headless_notes": [
                "macOS Docker-compatible engines run Linux browsers inside a VM; "
                "use headless mode and install matching browser dependencies in the worker image.",
                "Linux containers need the distro libraries required by the configured Playwright version.",
            ],
        }

    @staticmethod
    def _default_playwright_mcp_command(source: Path) -> str:
        local_cli = source / "frontend" / "node_modules" / "@playwright" / "mcp" / "cli.js"
        if local_cli.is_file():
            return "node"
        return "npx"

    @staticmethod
    def _default_playwright_mcp_args(
        source: Path, *, expected_version: str = "", browser: str = "chromium"
    ) -> tuple[str, ...]:
        # Without --browser, @playwright/mcp defaults to the chrome *channel*.
        # Worker images typically ship only chromium deps, so the agent's first
        # browser call fails, browser_install runs Chrome's reinstall script via
        # `su root`, and su blocks forever on a password prompt.
        browser_args = ("--browser", browser) if browser else ()
        local_cli = source / "frontend" / "node_modules" / "@playwright" / "mcp" / "cli.js"
        if local_cli.is_file():
            return (str(local_cli), "--headless", *browser_args)
        package = f"@playwright/mcp@{expected_version}" if expected_version else "@playwright/mcp"
        return (package, "--headless", *browser_args)

    @staticmethod
    def _detect_playwright_version(source: Path) -> str:
        for package_path in (source / "package.json", source / "frontend" / "package.json"):
            if not package_path.is_file():
                continue
            try:
                payload = json.loads(package_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            for section_name in ("devDependencies", "dependencies"):
                section = payload.get(section_name, {})
                if not isinstance(section, dict):
                    continue
                for package_name in ("@playwright/test", "playwright"):
                    version = section.get(package_name)
                    if isinstance(version, str) and version.strip():
                        return ContainerExecutionBackend._normalize_playwright_version_spec(version)
        return ""

    @staticmethod
    def _normalize_playwright_version_spec(version: str) -> str:
        version = version.strip()
        match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)
        if match:
            return match.group(0)
        return version

    def _detect_worker_playwright_version(self, *, image: str, cwd: Path) -> str:
        script = (
            "node - <<'NODE'\n"
            "const candidates = [\n"
            "  '/workspace/bootstrap/source/node_modules/playwright/package.json',\n"
            "  '/workspace/bootstrap/source/node_modules/@playwright/test/package.json',\n"
            "  'playwright/package.json',\n"
            "  '@playwright/test/package.json',\n"
            "];\n"
            "for (const candidate of candidates) {\n"
            "  try {\n"
            "    const pkg = require(candidate);\n"
            "    if (pkg && pkg.version) { console.log(pkg.version); process.exit(0); }\n"
            "  } catch (_) {}\n"
            "}\n"
            "process.exit(2);\n"
            "NODE"
        )
        result = self._runner.run(
            [self._container.engine, "run", "--rm", image, "sh", "-lc", script],
            cwd=cwd,
        )
        if result.returncode != 0:
            return ""
        actual = (result.stdout or "").strip().splitlines()[-1:] or [""]
        version = actual[0].strip()
        return version if re.search(r"\d", version) else ""

    @staticmethod
    def _playwright_target_app_url(
        *,
        topology: str,
        app_url: str,
        sidecar_endpoint: str,
        service_topology: str,
    ) -> str:
        if topology == "disabled":
            return ""
        if topology == "sidecar":
            return sidecar_endpoint or ContainerExecutionBackend._map_localhost_for_sidecar(app_url)
        if app_url:
            return app_url
        return "http://localhost:3000" if service_topology == "in-worker" else "http://127.0.0.1:3000"

    @staticmethod
    def _map_localhost_for_sidecar(app_url: str) -> str:
        if not app_url:
            return ""
        parsed = urlsplit(app_url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return app_url
        host = "host.docker.internal"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))

    def _write_playwright_mcp_diagnostics(
        self,
        logs: Path,
        playwright_mcp: dict[str, Any],
        service_env: dict[str, str],
    ) -> None:
        logs.mkdir(parents=True, exist_ok=True)
        sanitized_env = {
            key: "<redacted>" if not _is_container_worker_env_allowed(key) else "<set>" for key in service_env
        }
        (logs / "playwright-mcp-diagnostics.json").write_text(
            json.dumps(
                {
                    "backend": "container",
                    "failure_classes": {
                        "browser_launch": "browser_runtime",
                        "mcp_startup": "mcp_startup",
                        "target_reachability": "target_app_reachability",
                    },
                    "mcp_command": [
                        playwright_mcp.get("command", ""),
                        *playwright_mcp.get("args", []),
                    ],
                    "topology": playwright_mcp.get("topology", ""),
                    "target_app_url": playwright_mcp.get("target_app_url", ""),
                    "sanitized_env": sanitized_env,
                    "artifact_paths": playwright_mcp.get("artifact_paths", []),
                    "headless_notes": playwright_mcp.get("headless_notes", []),
                    "sidecar_mcp_transport": playwright_mcp.get("sidecar_mcp_transport", ""),
                    "sidecar_mcp_note": playwright_mcp.get("sidecar_mcp_note", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
        )

    def _remove_playwright_mcp_sidecar(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> None:
        playwright_mcp = state.get("playwright_mcp", {})
        if not isinstance(playwright_mcp, dict) or playwright_mcp.get("topology") != "sidecar":
            return
        container_id = str(playwright_mcp.get("sidecar_container") or "")
        if container_id:
            result = self._runner.run(
                [self._container.engine, "rm", "-f", container_id],
                cwd=run_root,
            )
            self._write_image_log(run_root / "logs", "playwright-mcp-sidecar-cleanup.log", result)
        for network in playwright_mcp.get("sidecar_networks", []):
            result = self._runner.run(
                [self._container.engine, "network", "rm", str(network)],
                cwd=run_root,
            )
            self._write_image_log(run_root / "logs", "playwright-mcp-network-cleanup.log", result)

    def _start_playwright_mcp_sidecar(
        self,
        run_root: Path,
        logs: Path,
        state: dict[str, Any],
    ) -> None:
        playwright_mcp = state.get("playwright_mcp", {})
        if not isinstance(playwright_mcp, dict) or playwright_mcp.get("topology") != "sidecar":
            return
        container = str(playwright_mcp.get("sidecar_container") or "")
        if not container:
            return
        sidecar_networks = [str(network) for network in playwright_mcp.get("sidecar_networks", [])]
        for network in sidecar_networks:
            result = self._runner.run(
                [self._container.engine, "network", "create", *self._label_argv(state), network],
                cwd=run_root,
            )
            self._write_image_log(logs, "playwright-mcp-network-create.log", result)
            if result.returncode != 0:
                raise RuntimeError(
                    "Container backend could not create Playwright MCP sidecar network "
                    f"{network}. See {logs / 'playwright-mcp-network-create.log'}"
                )
        argv = [
            self._container.engine,
            "run",
            "-d",
            "--name",
            container,
            *self._label_argv(state),
            "-v",
            f"{run_root / 'source'}:/workspace/source",
            "-v",
            f"{run_root / 'logs'}:/workspace/logs",
            "-w",
            CONTAINER_RUNTIME_SOURCE,
            "-e",
            f"PATH={CONTAINER_BOOTSTRAP_PATH}",
            "-e",
            f"NODE_PATH={CONTAINER_BOOTSTRAP_SOURCE}/node_modules",
            "--tmpfs",
            CONTAINER_RUNTIME_STATE_TMPFS,
        ]
        user_mapping = self._container_user_mapping()
        if user_mapping:
            argv.extend(["--user", user_mapping])
            argv.extend(self._container_passwd_shim_argv(run_root))
        attach_networks = list(dict.fromkeys([*sidecar_networks, *state.get("service_networks", [])]))
        if attach_networks:
            argv.extend(["--network", attach_networks[0]])
        sidecar_port = int(playwright_mcp.get("sidecar_mcp_port") or CONTAINER_PLAYWRIGHT_MCP_SIDECAR_PORT)
        sidecar_command = str(playwright_mcp.get("command") or "")
        sidecar_args = [str(item) for item in playwright_mcp.get("args", [])]
        if not sidecar_command:
            raise RuntimeError(
                "Container backend Playwright MCP sidecar startup is missing a command. "
                f"See {logs / 'playwright-mcp-sidecar.log'}"
            )
        argv.extend(
            [
                str(state["image"]),
                sidecar_command,
                *sidecar_args,
                "--port",
                str(sidecar_port),
                "--host",
                "0.0.0.0",
            ]
        )
        result = self._runner.run(argv, cwd=run_root)
        self._write_image_log(logs, "playwright-mcp-sidecar.log", result)
        if result.returncode != 0:
            raise RuntimeError(
                "Container backend Playwright MCP sidecar startup failed "
                f"for {container}. See {logs / 'playwright-mcp-sidecar.log'}"
            )
        for network in attach_networks[1:]:
            result = self._runner.run(
                [self._container.engine, "network", "connect", network, container],
                cwd=run_root,
            )
            self._write_image_log(logs, "playwright-mcp-network-connect.log", result)
            if result.returncode != 0:
                raise RuntimeError(
                    "Container backend could not connect Playwright MCP sidecar "
                    f"{container} to network {network}. "
                    f"See {logs / 'playwright-mcp-network-connect.log'}"
                )
        containers = list(state.get("containers", []))
        if container not in containers:
            containers.append(container)
            state["containers"] = containers
        self._write_container_state(run_root, state)

    @staticmethod
    def _compose_project_name(run_id: str) -> str:
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        return f"spec-{digest}"

    def _resolve_compose_file(self, repo_root: Path) -> Path:
        compose_file = Path(self._container.compose_file).expanduser()
        if not compose_file.is_absolute():
            compose_file = repo_root / compose_file
        return compose_file.resolve()

    def _compose_argv(self, compose_file: Path, compose_project: str, *args: str) -> list[str]:
        return [
            self._container.engine,
            "compose",
            "-p",
            compose_project,
            "-f",
            str(compose_file),
            *args,
        ]

    def _start_sidecar_services(
        self,
        *,
        run_root: Path,
        logs: Path,
        compose_file: Path,
        compose_project: str,
    ) -> None:
        state = self._read_container_state(run_root)
        override_file = self._write_compose_label_override(run_root, compose_file, compose_project, state)
        state["compose_label_override"] = str(override_file)
        self._write_container_state(run_root, state)
        result = self._runner.run(
            self._compose_argv(compose_file, compose_project, "-f", str(override_file), "up", "-d", "--remove-orphans"),
            cwd=run_root,
        )
        self._write_image_log(logs, "compose-services.log", result)
        if result.returncode != 0:
            failure_path = logs / "service-startup-failure.json"
            failure_path.write_text(
                json.dumps(
                    {
                        "backend": "container",
                        "failure_type": "service_startup",
                        "failure_subtype": "sidecar_compose_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "compose_file": str(compose_file),
                        "compose_project": compose_project,
                        "log_path": str(logs / "compose-services.log"),
                        "topology": "sidecar",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            raise RuntimeError(
                "Container backend sidecar service startup failed "
                f"for compose project {compose_project}. See {logs / 'compose-services.log'}"
            )

    def _write_compose_label_override(
        self,
        run_root: Path,
        compose_file: Path,
        compose_project: str,
        state: dict[str, Any],
    ) -> Path:
        labels = state.get("resource_labels", {})

        try:
            compose = yaml.safe_load(compose_file.read_text()) or {}
        except (OSError, yaml.YAMLError):
            compose = {}
        services = compose.get("services", {}) if isinstance(compose, dict) else {}
        volumes = compose.get("volumes", {}) if isinstance(compose, dict) else {}
        networks = compose.get("networks", {}) if isinstance(compose, dict) else {}
        network_names = (
            {
                name: value
                for name, value in networks.items()
                if not isinstance(value, dict) or not value.get("external")
            }
            if isinstance(networks, dict)
            else {}
        )
        network_names.setdefault("default", {})

        payload: dict[str, Any] = {
            "services": {str(name): {"labels": labels} for name in services},
            "networks": {str(name): {"labels": labels} for name in network_names},
        }
        if volumes:
            payload["volumes"] = {
                str(name): {"labels": labels}
                for name, value in volumes.items()
                if not isinstance(value, dict) or not value.get("external")
            }
        path = run_root / "container-compose-labels.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def _stop_sidecar_services(self, run_root: Path, state: dict[str, Any]) -> None:
        compose_file = str(state.get("compose_file") or "")
        compose_project = str(state.get("compose_project") or "")
        if not compose_file or not compose_project:
            return
        extra = ["-f", str(state["compose_label_override"])] if state.get("compose_label_override") else []
        result = self._runner.run(
            self._compose_argv(Path(compose_file), compose_project, *extra, "stop"),
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "compose-stop-before-snapshot.log", result)
        if result.returncode != 0:
            raise RuntimeError(
                "Container backend could not cleanly stop sidecar services before snapshot "
                f"for compose project {compose_project}. "
                f"See {run_root / 'logs' / 'compose-stop-before-snapshot.log'}"
            )

    def _remove_sidecar_services(self, run_root: Path, state: dict[str, Any]) -> None:
        compose_file = str(state.get("compose_file") or "")
        compose_project = str(state.get("compose_project") or "")
        if not compose_file or not compose_project:
            return
        extra = ["-f", str(state["compose_label_override"])] if state.get("compose_label_override") else []
        result = self._runner.run(
            self._compose_argv(Path(compose_file), compose_project, *extra, "down", "--volumes", "--remove-orphans"),
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "compose-cleanup.log", result)

    def _refresh_sidecar_service_volumes(self, run_root: Path, state: dict[str, Any]) -> None:
        compose_project = str(state.get("compose_project") or "")
        if not compose_project:
            return
        result = self._runner.run(
            [
                self._container.engine,
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                "--format",
                "{{.Name}}",
            ],
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "compose-volume-discovery.log", result)
        if result.returncode != 0:
            return
        volumes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not volumes:
            return
        state["service_volumes"] = list(dict.fromkeys(volumes))
        state["volumes"] = list(dict.fromkeys([*state.get("workspace_volumes", []), *state["service_volumes"]]))
        self._write_container_state(run_root, state)

    def _snapshot_sidecar_service_volumes(
        self,
        run_root: Path,
        state: dict[str, Any],
        label: str,
    ) -> None:
        volumes = [str(volume) for volume in state.get("service_volumes", []) if volume]
        if not volumes:
            return
        snapshot_root = run_root / "snapshots" / f"{_safe_artifact_name(label)}.service-volumes"
        if snapshot_root.exists():
            remove_tree(snapshot_root)
        snapshot_root.mkdir(parents=True)
        captured: dict[str, str] = {}
        for volume in volumes:
            archive_name = f"{_safe_artifact_name(volume)}.tar"
            result = self._runner.run(
                [
                    self._container.engine,
                    "run",
                    "--rm",
                    *self._label_argv(state),
                    "-v",
                    f"{volume}:/workspace/service-volume:ro",
                    "-v",
                    f"{snapshot_root}:/workspace/service-volume-snapshot",
                    state["image"],
                    "sh",
                    "-lc",
                    f"tar -C /workspace/service-volume -cf /workspace/service-volume-snapshot/{archive_name} .",
                ],
                cwd=run_root,
            )
            self._write_image_log(
                run_root / "logs",
                f"service-volume-snapshot-{_safe_artifact_name(volume)}.log",
                result,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Container backend could not snapshot service volume {volume}. "
                    f"See {run_root / 'logs' / ('service-volume-snapshot-' + _safe_artifact_name(volume) + '.log')}"
                )
            captured[volume] = str(snapshot_root / archive_name)
        snapshots = dict(state.get("service_volume_snapshots", {}))
        snapshots[label] = captured
        state["service_volume_snapshots"] = snapshots
        self._write_container_state(run_root, state)

    def _restore_sidecar_service_volumes(
        self,
        run_root: Path,
        state: dict[str, Any],
        label: str,
    ) -> None:
        snapshots = state.get("service_volume_snapshots", {})
        volume_archives = snapshots.get(label, {}) if isinstance(snapshots, dict) else {}
        if not isinstance(volume_archives, dict):
            return
        for volume, archive in volume_archives.items():
            archive_path = Path(str(archive))
            if not archive_path.is_file():
                continue
            result = self._runner.run(
                [
                    self._container.engine,
                    "run",
                    "--rm",
                    *self._label_argv(state),
                    "-v",
                    f"{volume}:/workspace/service-volume",
                    "-v",
                    f"{archive_path.parent}:/workspace/service-volume-snapshot:ro",
                    state["image"],
                    "sh",
                    "-lc",
                    "find /workspace/service-volume -mindepth 1 -maxdepth 1 -exec rm -rf {} + && "
                    f"tar -C /workspace/service-volume -xf /workspace/service-volume-snapshot/{archive_path.name}",
                ],
                cwd=run_root,
            )
            self._write_image_log(
                run_root / "logs",
                f"service-volume-restore-{_safe_artifact_name(str(volume))}.log",
                result,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Container backend could not restore service volume {volume}. "
                    f"See {run_root / 'logs' / ('service-volume-restore-' + _safe_artifact_name(str(volume)) + '.log')}"
                )

    def _start_in_worker_container(self, run_root: Path, state: dict[str, Any]) -> None:
        if state.get("worker_container"):
            return
        cidfile = run_root / "logs" / "in-worker-services.cid"
        if cidfile.exists():
            cidfile.unlink()
        argv = [
            self._container.engine,
            "run",
            "-d",
            *self._label_argv(state),
            "--cidfile",
            str(cidfile),
            "-v",
            f"{run_root / 'outbox'}:/workspace/outbox",
            "-v",
            f"{run_root / 'logs'}:/workspace/logs",
            "-w",
            CONTAINER_RUNTIME_SOURCE,
            "-e",
            f"{CONTAINER_COMPLETION_OUTBOX_ENV}=/workspace/outbox/{CONTAINER_COMPLETION_ARTIFACT}",
            "-e",
            f"PATH={CONTAINER_BOOTSTRAP_PATH}",
            "-e",
            f"NODE_PATH={CONTAINER_BOOTSTRAP_SOURCE}/node_modules",
            "--tmpfs",
            CONTAINER_RUNTIME_STATE_TMPFS,
        ]
        user_mapping = self._container_user_mapping()
        if user_mapping:
            argv.extend(["--user", user_mapping])
            argv.extend(self._container_passwd_shim_argv(run_root))
        workspace_volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if state.get("workspace_mode") == "volume" and workspace_volumes:
            argv.extend(["-v", f"{workspace_volumes[0]}:/workspace/source"])
        else:
            argv.extend(["-v", f"{run_root / 'source'}:/workspace/source"])
        attached_networks = list(
            dict.fromkeys(
                [
                    *(str(network) for network in state.get("service_networks", [])),
                    *self._playwright_sidecar_networks(state),
                ]
            )
        )
        for network in attached_networks:
            argv.extend(["--network", network])
        argv.extend([state["image"], "sh", "-lc", "sleep infinity"])
        result = self._runner.run(argv, cwd=run_root)
        self._write_image_log(run_root / "logs", "in-worker-services.log", result)
        container_id = cidfile.read_text().strip() if cidfile.is_file() else result.stdout.strip()
        if container_id:
            state["worker_container"] = container_id
            state["containers"] = list(dict.fromkeys([*state.get("containers", []), container_id]))
            state["service_processes"] = [
                {
                    "name": "in-worker-services",
                    "container_id": container_id,
                    "topology": "in-worker",
                    "log_path": str(run_root / "logs" / "in-worker-services.log"),
                }
            ]
            self._write_container_state(run_root, state)
        if result.returncode != 0 or not container_id:
            failure_path = run_root / "logs" / "service-startup-failure.json"
            failure_path.write_text(
                json.dumps(
                    {
                        "backend": "container",
                        "failure_type": "service_startup",
                        "failure_subtype": "in_worker_container_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "container_id": container_id,
                        "log_path": str(run_root / "logs" / "in-worker-services.log"),
                        "topology": "in-worker",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            if container_id:
                # ``docker run -d`` failed but a container id was captured
                # (the container exists in a broken/exited state). Remove it
                # immediately so a failed startup does not leak a container and
                # eventually saturate the docker bridge. Best-effort: swallow
                # errors so cleanup never masks the startup failure below, and
                # drop it from tracked state so no tracked container remains.
                try:
                    self._runner.run(
                        [self._container.engine, "rm", "-f", container_id],
                        cwd=run_root,
                    )
                except Exception:
                    pass
                state["worker_container"] = ""
                state["containers"] = [c for c in state.get("containers", []) if c != container_id]
                state["service_processes"] = [
                    process
                    for process in state.get("service_processes", [])
                    if not (isinstance(process, dict) and process.get("container_id") == container_id)
                ]
                self._write_container_state(run_root, state)
            raise RuntimeError(
                f"Container backend in-worker service startup failed. "
                f"See {run_root / 'logs' / 'in-worker-services.log'}"
            )
        self._write_container_state(run_root, state)

    def _reset_in_worker_container(self, run_root: Path, state: dict[str, Any]) -> None:
        container_id = str(state.get("worker_container") or "")
        if container_id:
            result = self._runner.run(
                [self._container.engine, "rm", "-f", container_id],
                cwd=run_root,
            )
            self._write_image_log(run_root / "logs", "in-worker-services-reset.log", result)
        cidfile = run_root / "logs" / "in-worker-services.cid"
        if cidfile.exists():
            cidfile.unlink()
        containers = [str(item) for item in state.get("containers", []) if str(item) != container_id]
        state["containers"] = containers
        state["worker_container"] = ""
        state["service_processes"] = [
            process
            for process in state.get("service_processes", [])
            if not (
                isinstance(process, dict) and container_id and str(process.get("container_id") or "") == container_id
            )
        ]
        self._write_container_state(run_root, state)

    def reseed_workspace_volume(self, workspace: WorkspaceHandle) -> None:
        """Re-seed the worker source volume from the host workspace source.

        In ``volume`` workspace mode the agent runs against a Docker volume seeded
        from the host source tree. When the orchestrator repositions the host
        source after a restore (e.g. moving a review retry to the reviewed head),
        the stale volume must be re-seeded so the agent runs against the updated
        tree rather than the original base seed. No-op outside ``volume`` mode.

        Re-seeding wipes ``/workspace/source`` before copying the (tracked) host
        tree, which discards the gitignored dependencies/build state that the
        preceding :func:`restore` installed into the volume via bootstrap. Mirror
        ``restore``'s own volume sequence (seed → bootstrap install) so the freshly
        repositioned volume has its dependencies reinstalled rather than being left
        bare — otherwise the implement agent would run against a dependency-less
        tree.
        """
        run_root = workspace.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        if state.get("workspace_mode") != "volume":
            return
        self._seed_volume_workspace(workspace, state)
        self._run_container_bootstrap_install(workspace)

    def _seed_volume_workspace(self, workspace: WorkspaceHandle, state: dict[str, Any]) -> None:
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if not volumes:
            return
        volume = str(volumes[0])
        create = self._runner.run(
            [self._container.engine, "volume", "create", *self._label_argv(state), volume],
            cwd=workspace.outbox_path.parent,
        )
        if create.returncode != 0:
            raise RuntimeError(f"Container backend could not create source volume {volume}.")
        user_mapping = self._container_user_mapping()
        seed_script = (
            "find /workspace/source -mindepth 1 -maxdepth 1 -exec rm -rf {} + && "
            "cp -a /workspace/seed/. /workspace/source/"
        )
        if user_mapping:
            seed_script = f"{seed_script} && chown -R {user_mapping} /workspace/source"
        seed = self._runner.run(
            [
                self._container.engine,
                "run",
                "--rm",
                *self._label_argv(state),
                "-v",
                f"{volume}:/workspace/source",
                "-v",
                f"{workspace.path}:/workspace/seed:ro",
                state["image"],
                "sh",
                "-lc",
                seed_script,
            ],
            cwd=workspace.outbox_path.parent,
        )
        self._write_image_log(workspace.outbox_path.parent / "logs", "volume-seed.log", seed)
        if seed.returncode != 0:
            raise RuntimeError(
                f"Container backend could not seed source volume {volume}. "
                f"See {workspace.outbox_path.parent / 'logs' / 'volume-seed.log'}"
            )

    def sync_host_paths_into_workspace(
        self,
        workspace_path: Path,
        relative_paths: Sequence[str],
    ) -> None:
        """Push host-worktree-relative paths into the workspace.

        In bind mode the host worktree *is* the workspace, so this is a no-op.
        In volume mode the workspace is a Docker volume seeded at
        ``prepare_workspace`` time — files written to the host worktree after
        seeding (e.g. ``.spec-codex-home/``, ``.claude/mcp-servers.json``)
        are not visible inside ``/workspace/source`` until they are copied
        in. This method runs a one-shot container that mounts both the host
        worktree and the workspace volume and copies each requested path.
        """
        if not relative_paths:
            return
        run_root = self._workspace_run_root(workspace_path)
        if run_root is None:
            return
        state = self._read_container_state(run_root, missing_ok=True)
        if state.get("workspace_mode") != "volume":
            return
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if not volumes:
            return
        volume = str(volumes[0])

        host_source = run_root / "source"
        normalized: list[str] = []
        for entry in relative_paths:
            rel = entry.strip()
            if not rel or rel.startswith("/"):
                continue
            if not (host_source / rel).exists():
                continue
            posix = rel.replace("\\", "/").rstrip("/")
            if posix and posix not in normalized:
                normalized.append(posix)
        if not normalized:
            return

        copy_cmds: list[str] = []
        for rel in normalized:
            quoted = shlex.quote(rel)
            parent = "/".join(rel.split("/")[:-1])
            parent_clause = f"mkdir -p /workspace/source/{shlex.quote(parent)} && " if parent else ""
            copy_cmds.append(
                f"{parent_clause}"
                f"rm -rf /workspace/source/{quoted} && "
                f"cp -a /workspace/host/{quoted} /workspace/source/{quoted}"
            )
        script = " && ".join(copy_cmds)

        argv = [
            self._container.engine,
            "run",
            "--rm",
            *self._label_argv(state),
        ]
        user_mapping = self._container_user_mapping()
        if user_mapping:
            argv.extend(["--user", user_mapping])
            argv.extend(self._container_passwd_shim_argv(run_root))
        argv.extend(
            [
                "-v",
                f"{volume}:/workspace/source",
                "-v",
                f"{host_source}:/workspace/host:ro",
                state["image"],
                "sh",
                "-lc",
                script,
            ]
        )
        sync = self._runner.run(argv, cwd=run_root)
        logs = run_root / "logs"
        self._write_image_log(logs, "volume-host-sync.log", sync)
        if sync.returncode != 0:
            raise RuntimeError(
                "Container backend could not sync host paths into source volume "
                f"{volume}. See {logs / 'volume-host-sync.log'}"
            )

    def _sync_volume_workspace_to_host(self, run_root: Path, state: dict[str, Any]) -> None:
        if state.get("workspace_mode") != "volume":
            return
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if not volumes:
            return
        volume = str(volumes[0])
        argv = [
            self._container.engine,
            "run",
            "--rm",
            *self._label_argv(state),
        ]
        user_mapping = self._container_user_mapping()
        if user_mapping:
            argv.extend(["--user", user_mapping])
            argv.extend(self._container_passwd_shim_argv(run_root))
        argv.extend(
            [
                "-v",
                f"{volume}:/workspace/source:ro",
                "-v",
                f"{run_root / 'source'}:/workspace/host",
                state["image"],
                "sh",
                "-lc",
                "find /workspace/host -mindepth 1 -maxdepth 1 -exec rm -rf {} + && "
                "find /workspace/source -mindepth 1 -maxdepth 1 "
                "-exec sh -c 'cp -a \"$@\" /workspace/host/' sh {} +",
            ]
        )
        sync = self._runner.run(
            argv,
            cwd=run_root,
        )
        logs = run_root / "logs"
        log_path = logs / "volume-import.log"
        self._write_image_log(logs, "volume-import.log", sync)
        if sync.returncode != 0:
            failure_path = logs / "volume-import-failure.json"
            failure_path.write_text(
                json.dumps(
                    {
                        "backend": "container",
                        "failure_type": "import",
                        "failure_subtype": "volume_workspace_import_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "volume": volume,
                        "returncode": sync.returncode,
                        "log_path": str(log_path),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            raise ExecutionBackendImportError(
                f"Container backend import failed: could not import source volume {volume}.",
                artifact_paths=(log_path, failure_path),
            )

    def _container_run_argv(
        self,
        *,
        run_root: Path,
        cwd: Path,
        command: list[str],
        env: dict[str, str],
        state: dict[str, Any],
        agent: bool = False,
    ) -> list[str]:
        # Only agent sessions may see the live completion outbox. Gate and prep
        # commands run pytest, and tests that exercise `spec report` honor an
        # inherited SPEC_COMPLETION_OUTBOX — writing fixture completion reports
        # into the live outbox that the orchestrator would consume as the
        # agent's handshake. The variable is exported explicitly-empty (rather
        # than omitted) because the worker container's base environment also
        # carries it. Non-agent commands additionally pin HOME to the isolated
        # home: the container default (HOME=/workspace/source) breaks
        # HOME-sensitive tests even on a pristine tree.
        outbox_value = f"/workspace/outbox/{CONTAINER_COMPLETION_ARTIFACT}" if agent else ""
        path_value = CONTAINER_BOOTSTRAP_PATH if agent else CONTAINER_NON_AGENT_PATH
        container_cwd = self._container_cwd(run_root, cwd)
        path_mappings = self._container_path_mappings(run_root)
        worker_env = {
            key: self._translate_container_paths(value, path_mappings)
            for key, value in self._filter_container_worker_env(env).items()
        }
        home_value = env.get("HOME")
        if home_value:
            translated_home = self._translate_container_paths(home_value, path_mappings)
            if translated_home == CONTAINER_RUNTIME_SOURCE or translated_home.startswith(
                f"{CONTAINER_RUNTIME_SOURCE}/"
            ):
                worker_env["HOME"] = translated_home
        for key, value in state.get("service_env", {}).items():
            worker_env.setdefault(str(key), str(value))
        translated_command = [self._translate_container_paths(value, path_mappings) for value in command]
        translated_command = self._relax_codex_sandbox_for_container(translated_command)
        if state.get("worker_container"):
            container_id = str(state.get("worker_container") or "")
            if not container_id:
                raise RuntimeError("Container backend worker container is not running.")
            argv = [
                self._container.engine,
                "exec",
                "-w",
                container_cwd,
                "-e",
                f"{CONTAINER_COMPLETION_OUTBOX_ENV}={outbox_value}",
                "-e",
                f"PATH={path_value}",
                "-e",
                f"NODE_PATH={CONTAINER_BOOTSTRAP_SOURCE}/node_modules",
            ]
            for key, value in sorted(worker_env.items()):
                argv.extend(["-e", self._container_worker_env_arg(key, value)])
            if not agent and "HOME" not in worker_env:
                argv.extend(["-e", f"HOME={CONTAINER_RUNTIME_SOURCE}/.spec-claude-home"])
            argv.extend([container_id, *translated_command])
            return argv
        cidfile = run_root / "logs" / f"container-{datetime.now(timezone.utc).timestamp():.6f}.cid"
        argv = [
            self._container.engine,
            "run",
            "--rm",
            *self._label_argv(state),
            "--cidfile",
            str(cidfile),
            "-v",
            f"{run_root / 'outbox'}:/workspace/outbox",
            "-v",
            f"{run_root / 'logs'}:/workspace/logs",
            "-w",
            container_cwd,
            "-e",
            f"{CONTAINER_COMPLETION_OUTBOX_ENV}={outbox_value}",
            "-e",
            f"PATH={path_value}",
            "-e",
            f"NODE_PATH={CONTAINER_BOOTSTRAP_SOURCE}/node_modules",
            "--tmpfs",
            CONTAINER_RUNTIME_STATE_TMPFS,
        ]
        user_mapping = self._container_user_mapping()
        if user_mapping:
            argv.extend(["--user", user_mapping])
            argv.extend(self._container_passwd_shim_argv(run_root))
        volumes = state.get("volumes", [])
        workspace_volumes = state.get("workspace_volumes") or volumes
        if state.get("workspace_mode") == "volume" and workspace_volumes:
            argv.extend(["-v", f"{workspace_volumes[0]}:/workspace/source"])
        else:
            argv.extend(["-v", f"{run_root / 'source'}:/workspace/source"])
        attached_networks = list(
            dict.fromkeys(
                [
                    *(str(network) for network in state.get("service_networks", [])),
                    *self._playwright_sidecar_networks(state),
                ]
            )
        )
        for network in attached_networks:
            argv.extend(["--network", network])
        for key, value in sorted(worker_env.items()):
            argv.extend(["-e", self._container_worker_env_arg(key, value)])
        if not agent and "HOME" not in worker_env:
            argv.extend(["-e", f"HOME={CONTAINER_RUNTIME_SOURCE}/.spec-claude-home"])
        argv.extend([state["image"], *translated_command])
        return argv

    @staticmethod
    def _relax_codex_sandbox_for_container(command: list[str]) -> list[str]:
        if not command or Path(command[0]).name != "codex":
            return command
        relaxed = list(command)
        for index, value in enumerate(relaxed):
            if value in {"-s", "--sandbox"} and index + 1 < len(relaxed):
                if relaxed[index + 1] == "workspace-write":
                    relaxed[index + 1] = CONTAINER_CODEX_SANDBOX_MODE
                return relaxed
            if value.startswith("--sandbox="):
                mode = value.split("=", 1)[1]
                if mode == "workspace-write":
                    relaxed[index] = f"--sandbox={CONTAINER_CODEX_SANDBOX_MODE}"
                return relaxed
        return relaxed

    @staticmethod
    def _container_worker_env_arg(key: str, value: str) -> str:
        if key in CONTAINER_WORKER_ENV_SECRET_ALLOWLIST:
            return key
        return f"{key}={value}"

    @staticmethod
    def _playwright_sidecar_networks(state: dict[str, Any]) -> list[str]:
        playwright_mcp = state.get("playwright_mcp", {})
        if not isinstance(playwright_mcp, dict):
            return []
        return [str(network) for network in playwright_mcp.get("sidecar_networks", [])]

    @staticmethod
    def _filter_container_worker_env(env: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in env.items()
            if _is_container_worker_env_allowed(key) or key in CONTAINER_WORKER_ENV_SECRET_ALLOWLIST
        }

    @staticmethod
    def _container_cwd(run_root: Path, cwd: Path) -> str:
        rel = cwd.resolve().relative_to((run_root / "source").resolve())
        rel_text = rel.as_posix()
        return CONTAINER_RUNTIME_SOURCE if rel_text == "." else f"{CONTAINER_RUNTIME_SOURCE}/{rel_text}"

    @staticmethod
    def _container_path_mappings(run_root: Path) -> list[tuple[str, str]]:
        mappings: list[tuple[str, str]] = []
        for host_path, container_path in (
            (run_root / "source", CONTAINER_RUNTIME_SOURCE),
            (run_root / "outbox", "/workspace/outbox"),
            (run_root / "logs", "/workspace/logs"),
        ):
            for candidate in (host_path, host_path.resolve()):
                host_text = candidate.as_posix()
                if (host_text, container_path) not in mappings:
                    mappings.append((host_text, container_path))
        mappings.sort(key=lambda item: len(item[0]), reverse=True)
        return mappings

    def _container_user_mapping(self) -> str:
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            return ""
        if self._system_name == "Windows":
            return ""
        return f"{os.getuid()}:{os.getgid()}"

    def _extract_image_text_file(
        self,
        *,
        image: str,
        container_path: str,
        run_root: Path,
        log_name: str,
    ) -> str | None:
        shim_dir = run_root / "passwd-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        digest_input = f"{run_root.name}:{image}:{container_path}".encode()
        tmp_name = f"spec-extract-{hashlib.sha256(digest_input).hexdigest()[:16]}"
        dest_path = shim_dir / f"{Path(container_path).name}.image"
        engine = self._container.engine
        logs = run_root / "logs"
        try:
            create = self._runner.run(
                [engine, "create", "--name", tmp_name, image],
                cwd=run_root,
            )
            cp = self._runner.run(
                [engine, "cp", f"{tmp_name}:{container_path}", str(dest_path)],
                cwd=run_root,
            )
        finally:
            rm = self._runner.run(
                [engine, "rm", "-f", tmp_name],
                cwd=run_root,
            )
        self._write_passwd_shim_extract_log(
            logs,
            log_name,
            create=create,
            cp=cp,
            rm=rm,
        )
        if cp.returncode != 0 or not dest_path.is_file():
            return None
        try:
            return dest_path.read_text()
        except OSError:
            return None

    @staticmethod
    def _write_passwd_shim_extract_log(
        logs: Path,
        name: str,
        *,
        create: subprocess.CompletedProcess[str],
        cp: subprocess.CompletedProcess[str],
        rm: subprocess.CompletedProcess[str],
    ) -> None:
        logs.mkdir(parents=True, exist_ok=True)
        sections: list[str] = [f"completed_at: {datetime.now(timezone.utc).isoformat()}"]
        for label, result in (("create", create), ("cp", cp), ("rm", rm)):
            args = result.args
            logged_args = [str(item) for item in args] if isinstance(args, list) else str(args)
            sections.extend(
                [
                    "",
                    f"--- {label} ---",
                    f"argv: {json.dumps(logged_args)}",
                    f"returncode: {result.returncode}",
                    "stdout:",
                    result.stdout or "",
                    "stderr:",
                    result.stderr or "",
                ]
            )
        (logs / name).write_text("\n".join(sections))

    @staticmethod
    def _passwd_line_has_id(line: str, target_id: int) -> bool:
        fields = line.split(":")
        if len(fields) < 3:
            return False
        try:
            return int(fields[2]) == target_id
        except ValueError:
            return False

    def _ensure_container_passwd_shim(
        self,
        *,
        run_root: Path,
        image: str,
    ) -> tuple[Path, Path] | None:
        user_mapping = self._container_user_mapping()
        if not user_mapping:
            return None
        try:
            uid_text, gid_text = user_mapping.split(":", 1)
            uid = int(uid_text)
            gid = int(gid_text)
        except ValueError:
            return None
        baseline_passwd = "root:x:0:0:root:/root:/bin/sh\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        baseline_group = "root:x:0:\nnogroup:x:65534:\n"
        passwd_text = self._extract_image_text_file(
            image=image,
            container_path="/etc/passwd",
            run_root=run_root,
            log_name="image-passwd-extract.log",
        )
        if passwd_text is None:
            passwd_text = baseline_passwd
        group_text = self._extract_image_text_file(
            image=image,
            container_path="/etc/group",
            run_root=run_root,
            log_name="image-group-extract.log",
        )
        if group_text is None:
            group_text = baseline_group

        passwd_lines = [line for line in passwd_text.splitlines() if line]
        if not any(self._passwd_line_has_id(line, uid) for line in passwd_lines):
            passwd_lines.append(f"spec:x:{uid}:{gid}:spec runtime user:/workspace/source:/bin/sh")
        group_lines = [line for line in group_text.splitlines() if line]
        if not any(self._passwd_line_has_id(line, gid) for line in group_lines):
            group_lines.append(f"spec:x:{gid}:")

        shim_dir = run_root / "passwd-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        passwd_path = shim_dir / "passwd"
        group_path = shim_dir / "group"
        passwd_path.write_text("\n".join(passwd_lines) + "\n")
        group_path.write_text("\n".join(group_lines) + "\n")
        os.chmod(passwd_path, 0o644)
        os.chmod(group_path, 0o644)
        return passwd_path, group_path

    def _container_passwd_shim_argv(self, run_root: Path) -> list[str]:
        if not self._container_user_mapping():
            return []
        passwd_path = run_root / "passwd-shim" / "passwd"
        group_path = run_root / "passwd-shim" / "group"
        if not passwd_path.is_file() or not group_path.is_file():
            return []
        return [
            "-v",
            f"{passwd_path}:/etc/passwd:ro",
            "-v",
            f"{group_path}:/etc/group:ro",
        ]

    @staticmethod
    def _remove_worker_visible_state(source: Path) -> None:
        state_dir = source / ".spec-state"
        if state_dir.is_symlink() or state_dir.is_file():
            state_dir.unlink()
        elif state_dir.is_dir():
            remove_tree(state_dir)

    @staticmethod
    def _translate_container_paths(value: str, mappings: list[tuple[str, str]]) -> str:
        translated = str(value)
        for host_path, container_path in mappings:
            translated = _replace_host_path_reference(
                translated,
                host_path=host_path,
                container_path=container_path,
            )
        return translated

    def _remember_container_id(self, run_root: Path, state: dict[str, Any]) -> None:
        logs = run_root / "logs"
        containers = list(state.get("containers", []))
        for cidfile in logs.glob("container-*.cid"):
            try:
                container_id = cidfile.read_text().strip()
            except OSError:
                continue
            if container_id and container_id not in containers:
                containers.append(container_id)
        if containers != state.get("containers", []):
            state["containers"] = containers
            self._write_container_state(run_root, state)

    def _container_state_path(self, run_root: Path) -> Path:
        return run_root / "backend-state" / "container-backend-state.json"

    def _read_container_state(
        self,
        run_root: Path,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        path = self._container_state_path(run_root)
        if not path.is_file():
            if missing_ok:
                return {}
            raise RuntimeError(f"Container backend state is missing: {path}")
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Container backend state is invalid: {path}") from exc
        return payload if isinstance(payload, dict) else {}

    def _write_container_state(self, run_root: Path, state: dict[str, Any]) -> None:
        path = self._container_state_path(run_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))

    @staticmethod
    def _write_image_log(
        logs: Path,
        name: str,
        result: subprocess.CompletedProcess[str],
        *,
        redactions: Sequence[str] = (),
    ) -> None:
        logs.mkdir(parents=True, exist_ok=True)
        logged_args: object
        if isinstance(result.args, list):
            logged_args = [ContainerExecutionBackend._redact_log_text(str(item), redactions) for item in result.args]
        else:
            logged_args = ContainerExecutionBackend._redact_log_text(str(result.args), redactions)
        (logs / name).write_text(
            "\n".join(
                [
                    f"completed_at: {datetime.now(timezone.utc).isoformat()}",
                    f"argv: {json.dumps(logged_args)}",
                    f"returncode: {result.returncode}",
                    "",
                    "stdout:",
                    ContainerExecutionBackend._redact_log_text(result.stdout or "", redactions),
                    "",
                    "stderr:",
                    ContainerExecutionBackend._redact_log_text(result.stderr or "", redactions),
                ]
            )
        )

    @staticmethod
    def _redact_log_text(text: str, redactions: Sequence[str]) -> str:
        redacted = text
        for secret in redactions:
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return redacted


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_execution_backend(
    config: SpecRuntimeConfig | ExecutionConfig,
) -> ExecutionBackend:
    """Return the configured execution backend.

    Selecting an unimplemented but known backend raises
    :class:`ExecutionBackendNotImplementedError`. Unknown values raise
    :class:`UnknownExecutionBackendError`.
    """
    execution = config.execution if isinstance(config, SpecRuntimeConfig) else config
    backend = execution.backend
    if backend not in ALLOWED_EXECUTION_BACKENDS:
        raise UnknownExecutionBackendError(backend)
    if execution.safety_mode not in ALLOWED_EXECUTION_SAFETY_MODES:
        allowed = ", ".join(sorted(ALLOWED_EXECUTION_SAFETY_MODES))
        raise ValueError(f"Unknown safety_mode {execution.safety_mode!r}. Allowed: {allowed}")
    if backend not in SUPPORTED_EXECUTION_BACKENDS:
        raise ExecutionBackendNotImplementedError(backend)
    if backend == "container":
        bootstrap_cache_command = ""
        bootstrap_cache_inputs: Sequence[str] = ()
        if isinstance(config, SpecRuntimeConfig) and config.bootstrap_cache.enabled:
            bootstrap_cache_command = config.bootstrap_cache.command
            bootstrap_cache_inputs = config.bootstrap_cache.inputs
        return ContainerExecutionBackend(
            execution,
            bootstrap_install_command=config.bootstrap_install_command if isinstance(config, SpecRuntimeConfig) else "",
            bootstrap_cache_command=bootstrap_cache_command,
            bootstrap_cache_inputs=bootstrap_cache_inputs,
        )
    if backend == "clone":
        return CloneExecutionBackend(execution)
    return WorktreeExecutionBackend(execution)
