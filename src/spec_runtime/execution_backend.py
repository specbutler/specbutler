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
import locale
import math
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
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
from .git_common import run_git, subprocess_text_kwargs
from .git_publish_guard import (
    capture_repository_publication_baseline,
    host_publication_git_environment,
)
from .platform_fs import (
    FileLock,
    atomic_write_text,
    read_bounded_regular_text,
    remove_tree,
)
from .process_supervisor import (
    LifetimeMode,
    ManagedProcess,
    ProcessSupervisor,
    SupervisionToken,
    hold_posix_setup_group,
    list_live_process_group_members,
)
from .process_supervisor import run as run_supervised
from .process_supervisor import (
    terminate as terminate_supervision_token,
)
from .provider_env import is_provider_process_startup_control_env_name
from .spec_identity import SPEC_ID_RE, implementation_branch_identity

_WORKSPACE_RUN_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,191}$")
_WORKSPACE_OWNER_FILENAME = ".specbutler-workspace-owner.json"
_CONTAINER_BACKEND_STATE_MAX_BYTES = 8 * 1024 * 1024


def validate_workspace_run_identity(run_id: str, spec_id: str) -> str:
    """Validate a backend run directory name against its owning spec.

    Run identifiers cross a destructive filesystem boundary. Treat values
    loaded from run/active state as untrusted even though normal producers use
    ``<spec-id>-<timestamp>``.
    """
    if not isinstance(run_id, str) or run_id != run_id.strip():
        raise ValueError(f"invalid non-canonical run_id {run_id!r}")
    if not isinstance(spec_id, str) or not SPEC_ID_RE.fullmatch(spec_id):
        raise ValueError(f"invalid spec_id {spec_id!r}")
    posix = PurePosixPath(run_id)
    windows = PureWindowsPath(run_id)
    if (
        not _WORKSPACE_RUN_COMPONENT_RE.fullmatch(run_id)
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or len(posix.parts) != 1
        or len(windows.parts) != 1
        or posix.name in {".", ".."}
        or windows.name in {".", ".."}
    ):
        raise ValueError(f"invalid run_id path component {run_id!r}")
    if not run_id.startswith(f"{spec_id}-") or run_id == f"{spec_id}-":
        raise ValueError(
            f"run_id {run_id!r} does not belong to spec {spec_id!r}"
        )
    return run_id


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized path without following links."""
    return Path(os.path.abspath(os.fspath(path)))


def path_is_link_or_junction(path: Path) -> bool:
    """Detect every Windows reparse-point boundary, including on Python 3.11.

    ``Path.is_junction`` was added after the oldest supported Python. Checking
    the native file attribute keeps destructive cleanup fail-closed for
    junctions and other directory reparse points on 3.11 as well.
    """
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _first_link_or_junction(path: Path, *, floor: Path) -> Path | None:
    target = _lexical_absolute(path)
    base = _lexical_absolute(floor)
    try:
        relative = target.relative_to(base)
    except ValueError:
        return target
    current = base
    for component in relative.parts:
        current /= component
        if path_is_link_or_junction(current):
            return current
    return None

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

_CONTAINER_GIT_METADATA_MAX_ENTRIES = 2_000_000
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
_CLAUDE_MCP_RUNTIME_ENV_RE = re.compile(r"SPEC_MCP_RUNTIME_[0-9A-F]{24}")
CONTAINER_COMPLETION_OUTBOX_ENV = "SPEC_COMPLETION_OUTBOX"
CONTAINER_COMPLETION_ARTIFACT = "completion-report.json"
CONTAINER_BOOTSTRAP_SOURCE = "/workspace/bootstrap/source"
CONTAINER_RUNTIME_SOURCE = "/workspace/source"
CONTAINER_CODEX_HOME = f"{CONTAINER_RUNTIME_SOURCE}/.spec-codex-home"
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
        raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
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
            match = re.search(
                r'^version\s*=\s*"([^"]+)"',
                pyproject.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
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
        # ``importlib.metadata.Distribution.read_text`` handles package
        # metadata as UTF-8 and accepts only the resource name.
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
            result = run_git(
                ["rev-parse", "HEAD"],
                cwd=source_root,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                # Editable installs can retain stale direct_url metadata after a pull.
                # The checkout containing the imported module is authoritative.
                commit_id = result.stdout.strip()
                status = run_git(
                    ["status", "--porcelain", "--untracked-files=normal"],
                    cwd=source_root,
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
    * ``preserve_descendants`` is reserved for setup hooks that intentionally
      hand declared background services to the orchestrator. It waits for the
      command leader without waiting on inherited output handles and returns a
      live Windows Job ownership token when descendants remain.
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str] | None = None
    inherit_env: bool = True
    timeout: float | None = None
    input_text: str | None = None
    stdin_devnull: bool = True
    redactions: Sequence[str] = ()
    preserve_descendants: bool = False


@dataclass(frozen=True)
class CommandResult:
    """Structured result of a backend-executed command."""

    returncode: int
    stdout: str
    stderr: str
    argv: list[str]
    ownership_token: SupervisionToken | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class AgentRequest:
    """Request to launch an agent in a workspace.

    ``popen_kwargs`` carries transport-neutral stream configuration the
    orchestrator needs the backend to forward (for example ``text=True`` or
    ``stdout=PIPE``). The backend owns process-group and platform launch policy
    so future backends can swap the transport (Docker exec, remote shell)
    without the orchestrator caring.
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str] | None = None
    capture_stdout: bool = False
    popen_kwargs: dict[str, Any] = field(default_factory=dict)
    redactions: Sequence[str] = ()
    # Names whose values came from an explicit repository setup manifest or
    # another declared launch input. Container backends use this provenance to
    # admit secret-shaped project variables without inheriting same-named
    # operator variables from the ambient environment.
    declared_env_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AgentResult:
    """Structured result of a backend-executed agent."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


AgentMonitor = Callable[["ManagedProcess | subprocess.Popen[Any]"], int]


def _run_agent_monitor(
    proc: ManagedProcess | subprocess.Popen[Any],
    monitor: AgentMonitor,
) -> int:
    """Run an agent monitor and release its retained ownership handle.

    Native Windows run-owned processes retain a Job Object until ``close``.
    Monitors commonly finish by polling the leader rather than calling
    ``wait``, so the backend owns this final release. Closing the Job also
    terminates any descendants that outlived the provider leader.
    """

    try:
        return monitor(proc)
    finally:
        close = getattr(proc, "close", None)
        if callable(close):
            close()


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
        final exit code. Built-in backends provide a ``ManagedProcess`` that
        owns its POSIX group or Windows Job; legacy custom backends may still
        provide a raw ``Popen`` only when they establish an equivalent owned
        boundary. When ``monitor`` is ``None`` the backend runs the command to
        completion and returns the captured result.
        """
        ...

    def collect_outbox_metadata(self, workspace: WorkspaceHandle) -> OutboxMetadata | None:
        """Return optional PR/MR metadata produced by the workspace.

        Returns ``None`` if no metadata artifact was produced, which is the
        normal behavior for the worktree backend until the agent starts
        writing one.
        """
        ...

    def prepare_host_access(self, workspace: WorkspaceHandle) -> None:
        """Establish a stable workspace boundary before host reads or Git."""
        ...

    def suspend_for_host_access(self, workspace: WorkspaceHandle) -> None:
        """Freeze backend writers while preserving resumable runtime state."""
        ...

    def resume_after_host_access(self, workspace: WorkspaceHandle) -> None:
        """Resume backend services after a host-only workspace transition."""
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
        ``allow_unpushed_work`` is set for an explicit operator discard such as
        ``spec clean``.
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


class ExecutionBackendQuiescenceError(RuntimeError):
    """Raised when a failed runtime transition cannot be made safely idle."""


class ExecutionBackendRuntimeResetError(RuntimeError):
    """Raised when safety recovery discarded a container runtime generation.

    The workspace is safely quiesced, but setup-created processes no longer
    exist.  Callers must retry from setup instead of silently continuing with
    a newly created runtime generation.
    """


class UnknownExecutionBackendError(ValueError):
    """Raised when an unknown backend value reaches the factory."""

    def __init__(self, backend: str):
        allowed = ", ".join(sorted(ALLOWED_EXECUTION_BACKENDS))
        super().__init__(f"Unknown execution backend {backend!r}. Allowed: {allowed}")


class WorkspaceHasUnpushedWorkError(OSError):
    """Raised when a workspace deletion is refused because the worktree still
    holds work that is not durable in ``origin``.

    "Unpushed work" spans four states, any of which blocks deletion:

    - commits on ``HEAD`` not reachable from any ``origin`` ref,
    - uncommitted modifications to tracked files, and
    - untracked, non-ignored files (excluding orchestrator secrets).
    - checked-out submodules, whose nested working trees cannot safely be
      inspected by host Git after an agent has controlled their Git config.

    Subclasses :class:`OSError` so existing ``cleanup`` callers that catch
    ``OSError`` degrade to a recorded warning rather than crashing. Deletion is
    only permitted with ``allow_unpushed_work=True`` (an explicit operator
    discard such as ``spec clean``).
    """

    def __init__(
        self,
        source: Path,
        unpushed: Sequence[str],
        *,
        dirty: bool = False,
        untracked: Sequence[str] = (),
        submodules: Sequence[str] = (),
    ):
        self.source = source
        self.unpushed = tuple(unpushed)
        self.dirty = bool(dirty)
        self.untracked = tuple(untracked)
        self.submodules = tuple(submodules)
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
        if self.submodules:
            preview = ", ".join(self.submodules[:5])
            reasons.append(
                f"{len(self.submodules)} checked-out submodule(s) whose nested "
                f"work was not inspected ({preview})"
            )
        detail = "; ".join(reasons) if reasons else "unpushed work"
        super().__init__(
            f"refusing to delete workspace {source}: worktree has {detail}. "
            "Push the branch or run `spec clean` to force removal."
        )


class WorkspaceInspectionFailedError(OSError):
    """Raised when Git cannot prove that a workspace is safe to replace.

    Destructive cleanup and retry restore must distinguish "clean" from
    "inspection failed". Treating a nonzero Git probe as an empty result can
    silently discard work when an index, ref, or protected configuration is
    malformed.
    """

    def __init__(self, source: Path, operation: str, detail: str):
        self.source = source
        self.operation = operation
        self.detail = detail
        super().__init__(
            f"refusing to replace workspace {source}: Git could not {operation}: "
            f"{detail or 'unknown error'}"
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
    try:
        payload = json.loads(
            read_bounded_regular_text(candidate, max_bytes=1024 * 1024)
        )
    except (ValueError, RecursionError, OSError, UnicodeError):
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


def _close_fd(control_fd: int) -> None:
    try:
        os.close(control_fd)
    except OSError:
        pass


def _wait_for_posix_setup_status(
    managed: ManagedProcess,
    status_path: Path,
    *,
    argv: Sequence[str],
    timeout: float | None,
) -> dict[str, object]:
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except (OSError, ValueError, RecursionError, TypeError) as exc:
            raise RuntimeError("POSIX setup keeper published invalid status") from exc
        if isinstance(payload, dict):
            if payload.get("schema") != 1:
                raise RuntimeError("POSIX setup keeper published an unknown status schema")
            return payload
        if managed.process.poll() is not None:
            raise RuntimeError("POSIX setup keeper exited before publishing command status")
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        time.sleep(0.02)


def _stop_posix_setup_keeper(managed: ManagedProcess, control_fd: int) -> bool:
    """Stop one live keeper boundary and close its parent-death descriptor."""
    try:
        stopped = terminate_supervision_token(managed.token, grace_seconds=0)
    finally:
        _close_fd(control_fd)
    try:
        managed.process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        stopped = False
    finally:
        managed.close()
    return stopped


def _release_empty_posix_setup_keeper(
    managed: ManagedProcess,
    control_fd: int,
) -> None:
    """Tell an empty retained boundary to exit without a group signal."""
    try:
        os.write(control_fd, b"R")
    except OSError as exc:
        _stop_posix_setup_keeper(managed, control_fd)
        raise RuntimeError("Could not release empty POSIX setup keeper") from exc
    _close_fd(control_fd)
    try:
        returncode = managed.process.wait(timeout=5.0)
    except subprocess.TimeoutExpired as exc:
        terminate_supervision_token(managed.token, grace_seconds=0)
        managed.process.wait(timeout=5.0)
        raise RuntimeError("POSIX setup keeper did not acknowledge release") from exc
    finally:
        managed.close()
    if returncode != 0:
        raise RuntimeError(
            f"POSIX setup keeper failed while releasing an empty boundary: {returncode}"
        )


def _read_command_output(
    stdout_file: Any,
    stderr_file: Any,
    *,
    encoding: str,
) -> tuple[str, str]:
    stdout_file.seek(0)
    stderr_file.seek(0)
    return (
        stdout_file.read().decode(encoding, errors="replace"),
        stderr_file.read().decode(encoding, errors="replace"),
    )


def _run_posix_command_preserving_descendants(
    request: CommandRequest,
    *,
    env: dict[str, str] | None,
) -> CommandResult:
    """Run setup beneath a live group keeper and retain its complete boundary."""
    encoding = str(
        subprocess_text_kwargs(request.argv).get("encoding")
        or locale.getpreferredencoding(False)
    )
    keeper_path = Path(__file__).with_name("setup_process_keeper.py")
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
        tempfile.TemporaryDirectory(prefix="spec-setup-keeper-") as keeper_dir,
    ):
        status_path = Path(keeper_dir) / "status.json"
        control_read_fd, control_write_fd = os.pipe()
        managed: ManagedProcess | None = None
        try:
            managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
                [
                    sys.executable,
                    str(keeper_path),
                    str(status_path),
                    str(control_read_fd),
                    "--",
                    *request.argv,
                ],
                cwd=request.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                pass_fds=(control_read_fd,),
            )
        except BaseException:
            _close_fd(control_write_fd)
            raise
        finally:
            _close_fd(control_read_fd)

        control_fd: int | None = control_write_fd
        try:
            status = _wait_for_posix_setup_status(
                managed,
                status_path,
                argv=request.argv,
                timeout=request.timeout,
            )
            stdout, stderr = _read_command_output(
                stdout_file,
                stderr_file,
                encoding=encoding,
            )
            launch_error = status.get("launch_error")
            if isinstance(launch_error, dict):
                release_fd = control_fd
                control_fd = None
                _release_empty_posix_setup_keeper(managed, release_fd)
                raw_errno = launch_error.get("errno")
                error_number = int(raw_errno) if isinstance(raw_errno, int) else None
                filename = launch_error.get("filename")
                raise OSError(
                    error_number,
                    str(launch_error.get("message") or "setup command launch failed"),
                    str(filename) if filename else None,
                )
            returncode = status.get("returncode")
            if not isinstance(returncode, int):
                raise RuntimeError("POSIX setup keeper omitted the command return code")

            members = list_live_process_group_members(managed.token.pgid)
            if members is None:
                raise RuntimeError("Could not inventory the retained POSIX setup group")
            descendants = tuple(
                pid for pid in members if pid != managed.token.identity.pid
            )
            ownership_token: SupervisionToken | None = None
            if descendants:
                ownership_token = hold_posix_setup_group(managed.token, control_fd)
                if ownership_token is None:
                    raise RuntimeError("Could not retain the POSIX setup-group keeper")
                # Ownership of this descriptor moved into process_supervisor's
                # held-group registry and its atexit parent-death backstop.
                control_fd = None
            else:
                release_fd = control_fd
                control_fd = None
                _release_empty_posix_setup_keeper(managed, release_fd)
            return CommandResult(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                argv=list(request.argv),
                ownership_token=ownership_token,
            )
        except subprocess.TimeoutExpired as exc:
            if control_fd is not None:
                _stop_posix_setup_keeper(managed, control_fd)
                control_fd = None
            stdout, stderr = _read_command_output(
                stdout_file,
                stderr_file,
                encoding=encoding,
            )
            exc.output = stdout
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        except BaseException:
            if control_fd is not None:
                _stop_posix_setup_keeper(managed, control_fd)
                control_fd = None
            raise


def run_local_command_preserving_descendants(
    request: CommandRequest,
    *,
    env: dict[str, str] | None,
) -> CommandResult:
    """Run a setup leader while retaining descendants for an agent handoff.

    Pipes are deliberately not used: a background service can inherit them and
    keep ``communicate()`` waiting forever. Temporary files let us wait for only
    the setup leader. On Windows the retained Job handle remains the cleanup
    capability for descendants; on POSIX a dedicated live group leader plus a
    parent-death pipe retain the setup boundary until the manifest handoff is
    accepted or rejected.
    """
    if request.input_text is not None:
        raise ValueError("descendant-preserving commands do not accept input_text")
    if os.name == "posix":
        return _run_posix_command_preserving_descendants(request, env=env)

    encoding = str(
        subprocess_text_kwargs(request.argv).get("encoding")
        or locale.getpreferredencoding(False)
    )
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
            request.argv,
            cwd=request.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        try:
            returncode = int(managed.process.wait(timeout=request.timeout))
        except subprocess.TimeoutExpired as exc:
            try:
                managed.kill()
                managed.process.wait(timeout=5.0)
            finally:
                managed.close()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode(encoding, errors="replace")
            stderr = stderr_file.read().decode(encoding, errors="replace")
            exc.output = stdout
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        except BaseException:
            try:
                managed.kill()
            except BaseException:
                pass
            try:
                managed.close()
            except BaseException:
                pass
            raise

        try:
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode(encoding, errors="replace")
            stderr = stderr_file.read().decode(encoding, errors="replace")
            ownership_token: SupervisionToken | None = None
            if managed.owned_tree_active():
                if os.name == "nt":
                    # The Job is already retained in process_supervisor's live
                    # Job registry. Do not close it until manifest registration
                    # gives the orchestrator an identity-checked teardown path.
                    ownership_token = managed.token
                else:
                    managed.close()
            else:
                managed.close()
        except BaseException:
            # Once the setup leader has exited, a Job inventory or output-read
            # failure must still close the kill-on-close ownership boundary.
            # Otherwise a retained setup service could become an undiscoverable
            # side effect of a command whose manifest was never consumed.
            try:
                managed.kill()
            except BaseException:
                pass
            try:
                managed.close()
            except BaseException:
                pass
            raise
        return CommandResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            argv=list(request.argv),
            ownership_token=ownership_token,
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
        if request.preserve_descendants:
            kwargs["preserve_descendants"] = True
        completed = orchestrator.run_subprocess(list(request.argv), **kwargs)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=list(request.argv),
            ownership_token=getattr(completed, "ownership_token", None),
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
            popen_kwargs.setdefault("errors", "replace")
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
        returncode = _run_agent_monitor(proc, monitor)
        return AgentResult(returncode=returncode)

    def collect_outbox_metadata(self, workspace: WorkspaceHandle) -> OutboxMetadata | None:
        return _read_outbox_metadata(workspace.outbox_path)

    def prepare_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

    def suspend_for_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

    def resume_after_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

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
        try:
            run_id = validate_workspace_run_identity(run_id, spec_id)
        except ValueError as exc:
            raise RuntimeError(
                f"Clone backend refuses unsafe run identity: {exc}"
            ) from exc
        repo_root = repo_root.resolve()
        workspace_root = self._resolve_workspace_root(repo_root)
        self._ensure_safe_workspace_root(repo_root, workspace_root)
        self._ensure_workspace_root_ignored(repo_root, workspace_root)
        self._ensure_workspace_root_owner(repo_root, workspace_root)
        publish_remote_url = self._resolve_publish_remote_url(repo_root)

        run_root = workspace_root / run_id
        source = run_root / "source"
        outbox = run_root / "outbox"
        logs = run_root / "logs"
        self._assert_run_layout_has_no_links(
            repo_root=repo_root,
            workspace_root=workspace_root,
            run_root=run_root,
            source=source,
            outbox=outbox,
        )
        outbox.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        source_created = not source.exists()
        if source_created:
            source.parent.mkdir(parents=True, exist_ok=True)
            self._clone_source_checkout(repo_root, source)
            self._copy_user_git_config(repo_root, source)
        elif not (source / ".git").is_dir():
            raise RuntimeError(
                f"Clone backend source path exists but is not a full checkout: {source}. "
                "Remove the workspace directory and retry."
            )

        self._prepare_workspace_git_boundary(
            repo_root=repo_root,
            run_root=run_root,
            source=source,
            run_id=run_id,
            spec_id=spec_id,
            source_created=source_created,
        )
        if source_created:
            self._write_git_state(source, logs, "clone")

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
        if request.preserve_descendants:
            return run_local_command_preserving_descendants(request, env=env)
        stdin = subprocess.DEVNULL if request.stdin_devnull and request.input_text is None else None
        completed = run_supervised(
            request.argv,
            cwd=request.cwd,
            env=env,
            input=request.input_text,
            text=True,
            errors="replace",
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
            popen_kwargs.setdefault("errors", "replace")
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
        returncode = _run_agent_monitor(proc, monitor)
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

    def prepare_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

    def suspend_for_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

    def resume_after_host_access(self, workspace: WorkspaceHandle) -> None:
        del workspace

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        run_root = workspace.outbox_path.parent.resolve()
        snapshots = run_root / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        target = snapshots / _safe_artifact_name(label)
        existing = self._completed_snapshot(target, label)
        if existing is not None:
            return existing
        manifest_path = self._snapshot_manifest_path(target)
        if path_is_link_or_junction(target):
            target.unlink()
        elif target.exists():
            remove_tree(target)
        manifest_path.unlink(missing_ok=True)

        staging_parent = Path(
            tempfile.mkdtemp(
                dir=snapshots,
                prefix=f".{target.name}.staging-",
            )
        )
        staging_tree = staging_parent / "tree"
        published = False
        try:
            shutil.copytree(workspace.path, staging_tree, symlinks=True)
            # The final snapshot name appears only after copytree completed.
            # A crash or OSError during the copy leaves a private staging path,
            # never a directory the retry path can mistake for a recovery point.
            os.replace(staging_tree, target)
            published = True
        except BaseException:
            if published and target.exists():
                remove_tree(target, ignore_errors=True)
            raise
        finally:
            if staging_parent.exists():
                remove_tree(staging_parent, ignore_errors=True)
        ref = SnapshotRef(
            label=label,
            path=target,
            metadata={
                "backend": self.identity.backend,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "complete": True,
            },
        )
        atomic_write_text(
            manifest_path,
            json.dumps(
                ref.metadata | {"label": label, "path": str(target)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return ref

    @staticmethod
    def _snapshot_manifest_path(target: Path) -> Path:
        return target.parent / f"{target.name}.json"

    def _completed_snapshot(self, target: Path, label: str) -> SnapshotRef | None:
        manifest_path = self._snapshot_manifest_path(target)
        if (
            not target.is_dir()
            or path_is_link_or_junction(target)
            or not manifest_path.is_file()
            or path_is_link_or_junction(manifest_path)
        ):
            return None
        try:
            payload = json.loads(
                read_bounded_regular_text(manifest_path, max_bytes=1024 * 1024)
            )
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("label") != label
            or payload.get("path") != str(target)
        ):
            return None
        # Older snapshots wrote their manifest only after copytree returned,
        # so a well-formed legacy manifest is also a valid completion marker.
        metadata = {
            str(key): value
            for key, value in payload.items()
            if key not in {"label", "path"}
        }
        return SnapshotRef(label=label, path=target, metadata=metadata)

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        self._validated_cleanup_layout(
            workspace,
            migrate_legacy_owner=False,
        )
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
        completed_snapshot = self._completed_snapshot(snapshot.path, snapshot.label)
        if completed_snapshot is None:
            return self._restore_fresh_workspace_fallback(
                workspace,
                snapshot,
                reason="snapshot path is missing or completion manifest is invalid",
            )
        try:
            # Recheck the link-sensitive boundary immediately before deleting
            # any child. A replaced source directory must never redirect this
            # restore into an external tree.
            self._validated_cleanup_layout(
                workspace,
                migrate_legacy_owner=False,
            )
            self._replace_workspace_tree(workspace.path, completed_snapshot.path)
        except OSError as exc:
            return self._restore_fresh_workspace_fallback(
                workspace,
                snapshot,
                reason=f"snapshot restore failed: {exc}",
            )
        return workspace

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        run_root, source, _outbox = self._validated_cleanup_layout(workspace)
        self._assert_workspace_deletable(source, allow_unpushed_work=allow_unpushed_work)
        # Recheck the link-sensitive boundary immediately before deletion. A
        # corrupt run directory must never redirect rmtree outside the
        # backend-owned workspace root.
        self._assert_run_layout_has_no_links(
            repo_root=self._workspace_owner_repo_root(run_root.parent),
            workspace_root=run_root.parent,
            run_root=run_root,
            source=source,
            outbox=run_root / "outbox",
        )
        remove_tree(run_root)

    def _validated_cleanup_layout(
        self,
        workspace: WorkspaceHandle,
        *,
        migrate_legacy_owner: bool = True,
    ) -> tuple[Path, Path, Path]:
        """Resolve an owned cleanup target without trusting handle paths."""
        outbox = _lexical_absolute(workspace.outbox_path)
        run_root = outbox.parent
        source = _lexical_absolute(workspace.path)
        recorded_run_id = workspace.metadata.get("run_id")
        recorded_spec_id = workspace.metadata.get("spec_id")
        run_id = str(recorded_run_id or run_root.name)
        spec_id = str(recorded_spec_id or "")
        branch_identity = implementation_branch_identity(workspace.branch)
        if not spec_id:
            if branch_identity is not None:
                spec_id = branch_identity.spec_id
            elif workspace.branch.startswith("task/"):
                legacy_task_spec_id = f"task-{workspace.branch.removeprefix('task/')}"
                if SPEC_ID_RE.fullmatch(legacy_task_spec_id) and run_id.startswith(
                    f"{legacy_task_spec_id}-"
                ):
                    spec_id = legacy_task_spec_id
        try:
            validate_workspace_run_identity(run_id, spec_id)
        except ValueError as exc:
            raise OSError(f"refusing to clean clone backend workspace: {exc}") from exc
        if branch_identity is not None and branch_identity.spec_id != spec_id:
            raise OSError(
                "refusing to clean clone backend workspace with mismatched branch/spec identity"
            )

        expected_run_root = run_root.parent / run_id
        expected_source = expected_run_root / "source"
        expected_outbox = expected_run_root / "outbox"
        if (
            run_root != expected_run_root
            or source != expected_source
            or outbox != expected_outbox
        ):
            raise OSError(
                "refusing to clean clone backend workspace with inconsistent paths: "
                f"source={source}, outbox={outbox}, run_root={run_root}"
            )

        workspace_root = run_root.parent
        for candidate in (workspace_root, run_root, source, outbox):
            if path_is_link_or_junction(candidate):
                raise OSError(
                    "refusing to clean clone backend workspace through symlink "
                    f"or junction: {candidate}"
                )
        owner_marker = workspace_root / _WORKSPACE_OWNER_FILENAME
        if not owner_marker.is_file():
            required_metadata = {
                "run_id": run_id,
                "spec_id": spec_id,
                "repo_root": str(workspace.metadata.get("repo_root") or "").strip(),
                "workspace_root": str(
                    workspace.metadata.get("workspace_root") or ""
                ).strip(),
            }
            if (
                not isinstance(recorded_run_id, str)
                or recorded_run_id != run_id
                or not isinstance(recorded_spec_id, str)
                or recorded_spec_id != spec_id
                or not required_metadata["repo_root"]
                or not required_metadata["workspace_root"]
            ):
                raise OSError(
                    "refusing legacy workspace migration without complete canonical metadata"
                )
        repo_root = self._workspace_owner_repo_root(
            workspace_root,
            workspace=workspace,
            migrate_legacy_owner=migrate_legacy_owner,
        )
        configured_root = self._resolve_workspace_root(repo_root)
        if configured_root != workspace_root:
            raise OSError(
                "refusing to clean clone backend workspace outside its configured root: "
                f"run_root={run_root}, configured_root={configured_root}"
            )
        metadata_repo_root = str(workspace.metadata.get("repo_root") or "").strip()
        if metadata_repo_root and Path(metadata_repo_root).resolve() != repo_root:
            raise OSError(
                "refusing to clean clone backend workspace with mismatched repo_root metadata"
            )
        metadata_workspace_root = str(
            workspace.metadata.get("workspace_root") or ""
        ).strip()
        if (
            metadata_workspace_root
            and Path(metadata_workspace_root).resolve() != workspace_root
        ):
            raise OSError(
                "refusing to clean clone backend workspace with mismatched workspace_root metadata"
            )
        self._assert_run_layout_has_no_links(
            repo_root=repo_root,
            workspace_root=workspace_root,
            run_root=run_root,
            source=source,
            outbox=outbox,
        )
        return run_root, source, outbox

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
        # Container workspaces expose logs to the worker. Atomic replacement
        # cannot follow an attacker-planted leaf symlink; preserving older
        # fallback entries is less important than retaining that boundary.
        atomic_write_text(path, entry, encoding="utf-8")

    def _restore_fresh_workspace_fallback(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
        *,
        reason: str,
    ) -> WorkspaceHandle:
        repo_root_raw = workspace.metadata.get("repo_root")
        if not repo_root_raw or not workspace.branch:
            raise RuntimeError(
                "Snapshot restore fallback requires workspace metadata with "
                "repo_root and branch; refusing to continue in a dirty workspace."
            )
        repo_root = Path(str(repo_root_raw)).expanduser()
        if not repo_root.is_absolute():
            repo_root = repo_root.resolve()
        self._validated_cleanup_layout(
            workspace,
            migrate_legacy_owner=False,
        )
        run_root = workspace.outbox_path.parent.resolve()
        logs = run_root / "logs"
        base_ref = str(workspace.metadata.get("base_ref") or "") or "origin/master"
        publish_remote_url = self._resolve_publish_remote_url(repo_root)
        staging_parent = Path(
            tempfile.mkdtemp(dir=run_root, prefix=".source.fresh-staging-")
        )
        staging_tree = staging_parent / "tree"
        try:
            # Build and validate the complete replacement beside the current
            # checkout. A clone/fetch/checkout failure leaves the only known
            # good source tree untouched.
            self._clone_source_checkout(repo_root, staging_tree)
            self._copy_user_git_config(repo_root, staging_tree)
            self._prepare_restored_checkout_git_boundary(
                run_root=run_root,
                source=staging_tree,
            )
            self._copy_local_refs(repo_root, staging_tree)
            self._configure_publish_remote(staging_tree, publish_remote_url)
            self._ensure_ref_available(staging_tree, base_ref)
            self._checkout_branch(staging_tree, workspace.branch, base_ref)
            self._write_git_state(staging_tree, logs, "prepared")
            self._swap_workspace_tree(workspace.path, staging_tree, staging_parent)
        finally:
            if staging_parent.exists():
                remove_tree(staging_parent, ignore_errors=True)
        self._persist_base_ref(run_root, workspace.path, base_ref)
        refreshed = workspace
        self._record_snapshot_fallback(
            refreshed,
            snapshot,
            f"{reason}; prepared fresh workspace after snapshot restore fallback",
        )
        return refreshed

    def _replace_workspace_tree(self, workspace_path: Path, snapshot_path: Path) -> None:
        staging_parent = Path(
            tempfile.mkdtemp(
                dir=workspace_path.parent,
                prefix=".source.snapshot-staging-",
            )
        )
        staging_tree = staging_parent / "tree"
        try:
            shutil.copytree(snapshot_path, staging_tree, symlinks=True)
            self._prepare_restored_checkout_git_boundary(
                run_root=workspace_path.parent,
                source=staging_tree,
            )
            self._swap_workspace_tree(workspace_path, staging_tree, staging_parent)
        finally:
            if staging_parent.exists():
                remove_tree(staging_parent, ignore_errors=True)

    @staticmethod
    def _prepare_restored_checkout_git_boundary(
        *,
        run_root: Path,
        source: Path,
    ) -> None:
        del run_root
        git_dir = source / ".git"
        if not git_dir.is_dir() or path_is_link_or_junction(git_dir):
            raise OSError(
                "replacement workspace is not a regular full Git checkout"
            )

    @staticmethod
    def _swap_workspace_tree(
        workspace_path: Path,
        staging_tree: Path,
        staging_parent: Path,
    ) -> None:
        """Install a complete sibling tree while retaining rollback authority."""
        backup = staging_parent / "previous"
        os.replace(workspace_path, backup)
        try:
            os.replace(staging_tree, workspace_path)
        except BaseException:
            try:
                os.replace(backup, workspace_path)
            except BaseException as rollback_error:
                raise OSError(
                    "workspace replacement failed and the original checkout "
                    f"could not be restored from {backup}"
                ) from rollback_error
            raise
        # The replacement is now installed atomically. Cleanup failure may
        # leave a private backup for manual recovery, but must not trigger a
        # second fallback that would replace the successful tree again.
        remove_tree(backup, ignore_errors=True)

    def _resolve_workspace_root(self, repo_root: Path) -> Path:
        configured = Path(self._identity.workspace_root).expanduser()
        if not configured.is_absolute():
            configured = repo_root / configured
        configured = _lexical_absolute(configured)
        linked = _first_link_or_junction(configured, floor=repo_root)
        if linked is not None:
            raise RuntimeError(
                "Clone backend refuses a workspace_root containing a symlink or "
                f"junction (or outside the checkout): {linked}"
            )
        return configured.resolve()

    def _ensure_workspace_root_owner(
        self,
        repo_root: Path,
        workspace_root: Path,
    ) -> None:
        """Persist the host-owned root identity used by destructive cleanup."""
        workspace_root.mkdir(parents=True, exist_ok=True)
        linked = _first_link_or_junction(workspace_root, floor=repo_root)
        if linked is not None:
            raise RuntimeError(
                f"Clone backend refuses linked workspace root component: {linked}"
            )
        marker = workspace_root / _WORKSPACE_OWNER_FILENAME
        if path_is_link_or_junction(marker):
            raise RuntimeError(
                f"Clone backend refuses linked workspace ownership marker: {marker}"
            )
        expected = {
            "format": 1,
            "repo_root": str(repo_root.resolve()),
            "workspace_root": str(workspace_root.resolve()),
        }
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                raise RuntimeError(
                    f"Clone backend workspace ownership marker is unreadable: {marker}"
                ) from exc
            if payload != expected:
                raise RuntimeError(
                    "Clone backend workspace ownership marker does not match this "
                    f"checkout: {marker}"
                )
            return
        atomic_write_text(
            marker,
            json.dumps(expected, indent=2, sort_keys=True) + "\n",
        )

    def _workspace_owner_repo_root(
        self,
        workspace_root: Path,
        *,
        workspace: WorkspaceHandle | None = None,
        migrate_legacy_owner: bool = True,
    ) -> Path:
        marker = workspace_root / _WORKSPACE_OWNER_FILENAME
        if path_is_link_or_junction(marker):
            raise OSError(
                f"refusing to clean clone backend workspace through linked ownership marker: {marker}"
            )
        if not marker.is_file():
            repo_path = self._legacy_workspace_owner_repo_root(
                workspace_root,
                workspace=workspace,
            )
            # Released workspaces predate the marker. Migrate only after the
            # caller paths, run identity, branch/spec relationship, root
            # containment, and link checks have all passed.
            if migrate_legacy_owner:
                self._ensure_workspace_root_owner(repo_path, workspace_root)
            return repo_path
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            repo_raw = payload["repo_root"]
            recorded_root_raw = payload["workspace_root"]
            marker_format = payload["format"]
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            raise OSError(
                f"refusing to clean clone backend workspace with invalid ownership marker: {marker}"
            ) from exc
        repo_path = Path(str(repo_raw)).expanduser()
        recorded_root = Path(str(recorded_root_raw)).expanduser()
        if (
            marker_format != 1
            or not repo_path.is_absolute()
            or not recorded_root.is_absolute()
            or repo_path.resolve() != repo_path
            or recorded_root.resolve() != workspace_root
        ):
            raise OSError(
                f"refusing to clean clone backend workspace with mismatched ownership marker: {marker}"
            )
        try:
            workspace_root.relative_to(repo_path)
        except ValueError as exc:
            raise OSError(
                f"refusing to clean clone backend workspace outside its owner checkout: {workspace_root}"
            ) from exc
        if workspace_root == repo_path:
            raise OSError(
                f"refusing to clean clone backend workspace at repository root: {workspace_root}"
            )
        linked = _first_link_or_junction(workspace_root, floor=repo_path)
        if linked is not None:
            raise OSError(
                f"refusing to clean clone backend workspace through symlink or junction: {linked}"
            )
        return repo_path

    def _legacy_workspace_owner_repo_root(
        self,
        workspace_root: Path,
        *,
        workspace: WorkspaceHandle | None,
    ) -> Path:
        configured = Path(self._identity.workspace_root).expanduser()
        metadata_repo = (
            str(workspace.metadata.get("repo_root") or "").strip()
            if workspace is not None
            else ""
        )
        if metadata_repo:
            candidate_repo = Path(metadata_repo).expanduser()
            if not candidate_repo.is_absolute():
                raise OSError(
                    "refusing legacy workspace migration with relative repo_root metadata"
                )
            candidate_repo = candidate_repo.resolve()
        else:
            if configured.is_absolute():
                raise OSError(
                    "refusing legacy absolute workspace-root migration without repo_root metadata"
                )
            if any(part in {"", ".", ".."} for part in configured.parts):
                raise OSError(
                    "refusing legacy workspace migration for non-canonical workspace_root"
                )
            candidate_repo = workspace_root
            for _component in configured.parts:
                candidate_repo = candidate_repo.parent
            candidate_repo = candidate_repo.resolve()
        if not ((candidate_repo / ".git").is_dir() or (candidate_repo / ".git").is_file()):
            raise OSError(
                f"refusing legacy workspace migration outside a Git checkout: {candidate_repo}"
            )
        expected_root = configured
        if not expected_root.is_absolute():
            expected_root = candidate_repo / expected_root
        expected_root = _lexical_absolute(expected_root)
        if expected_root != workspace_root or expected_root.resolve() != workspace_root:
            raise OSError(
                "refusing legacy workspace migration outside the configured root: "
                f"{workspace_root}"
            )
        linked = _first_link_or_junction(workspace_root, floor=candidate_repo)
        if linked is not None:
            raise OSError(
                f"refusing legacy workspace migration through symlink or junction: {linked}"
            )
        if workspace is not None:
            metadata_root = str(
                workspace.metadata.get("workspace_root") or ""
            ).strip()
            if metadata_root and Path(metadata_root).resolve() != workspace_root:
                raise OSError(
                    "refusing legacy workspace migration with mismatched workspace_root metadata"
                )
        return candidate_repo

    def _assert_run_layout_has_no_links(
        self,
        *,
        repo_root: Path,
        workspace_root: Path,
        run_root: Path,
        source: Path,
        outbox: Path,
    ) -> None:
        try:
            if run_root.parent != workspace_root:
                raise ValueError("run root is not a direct workspace-root child")
            run_root.resolve(strict=False).relative_to(workspace_root.resolve())
            for candidate in (workspace_root, run_root, source, outbox):
                linked = _first_link_or_junction(candidate, floor=repo_root)
                if linked is not None:
                    raise ValueError(f"symlink or junction at {linked}")
        except (OSError, ValueError) as exc:
            raise OSError(
                "refusing backend workspace operation outside the owned, "
                f"link-free run layout: {run_root} ({exc})"
            ) from exc

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
        existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if rel not in existing:
            with exclude.open("a", encoding="utf-8") as handle:
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

    def _prepare_workspace_git_boundary(
        self,
        *,
        repo_root: Path,
        run_root: Path,
        source: Path,
        run_id: str,
        spec_id: str,
        source_created: bool,
    ) -> None:
        """Hook for backends with a stronger Git trust boundary."""
        del repo_root, run_root, source, run_id, spec_id, source_created

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
        status = self._run_git(
            ["status", "--short", "--branch", "--ignore-submodules=all"],
            cwd=source,
        )
        rev = self._run_git(["rev-parse", "HEAD"], cwd=source)
        atomic_write_text(
            logs / f"git-state-{label}.txt",
            "\n".join(
                [
                    f"$ git status --short --branch\n{status.stdout}{status.stderr}",
                    f"$ git rev-parse HEAD\n{rev.stdout}{rev.stderr}",
                ]
            ),
            encoding="utf-8",
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
        atomic_write_text(path, "\n".join(payload), encoding="utf-8")

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
        atomic_write_text(
            outbox / "agent-result.json",
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._write_completion_artifacts(source=run_root / "source", outbox=outbox)

    def _write_completion_artifacts(self, *, source: Path, outbox: Path) -> None:
        if not (source / ".git").exists():
            return
        branch = self._run_git(["branch", "--show-current"], cwd=source)
        head = self._run_git(["rev-parse", "HEAD"], cwd=source)
        status = self._run_git(
            ["status", "--short", "--branch", "--ignore-submodules=all"],
            cwd=source,
        )
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
        diff = self._run_git(
            ["diff", diff_target, "--binary", "--ignore-submodules=all"],
            cwd=source,
        )
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
        atomic_write_text(
            outbox / "commit-metadata.json",
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if diff.returncode == 0:
            atomic_write_text(outbox / "final.patch", diff.stdout, encoding="utf-8")

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
            (run_root / self._BASE_REF_FILENAME).write_text(f"{sha}\n", encoding="utf-8")
        except OSError:
            pass

    def _read_persisted_base_sha(self, run_root: Path, source: Path) -> str:
        """Return the recorded base SHA if it still resolves in ``source``."""
        try:
            sha = (run_root / self._BASE_REF_FILENAME).read_text(encoding="utf-8").strip()
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

    @staticmethod
    def _has_inspectable_git_boundary(source: Path) -> bool:
        """Require Git metadata whenever an existing workspace may be erased."""
        if not source.exists():
            return False
        git_dir = source / ".git"
        if not git_dir.is_dir() or path_is_link_or_junction(git_dir):
            raise WorkspaceInspectionFailedError(
                source,
                "locate repository metadata",
                ".git is missing, linked, or not a directory",
            )
        return True

    def _untracked_files(self, source: Path) -> list[str]:
        """Return untracked, non-ignored file paths (repo-relative) worth saving.

        ``--exclude-standard`` honors ``.gitignore`` (so the self-gitignored
        ``.spec-claude-home`` is already skipped), and the secret prefix is then
        filtered again explicitly. These are files the agent created but never
        ``git add``-ed: they are lost by a tree-replacing restore and by
        deletion just as surely as committed work, so both the rescue snapshot
        and the deletion guard must account for them.
        """
        if not self._has_inspectable_git_boundary(source):
            return []
        result = self._run_git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=source
        )
        if result.returncode != 0:
            raise WorkspaceInspectionFailedError(
                source,
                "inspect untracked files",
                self._git_detail(result),
            )
        files = [entry for entry in result.stdout.split("\0") if entry]
        return [rel for rel in files if not self._is_secret_path(rel)]

    def _unpushed_commits(self, source: Path) -> list[str]:
        """Return SHAs on ``HEAD`` not reachable from any ``origin`` ref.

        An empty list means the branch tip is fully published (or the source
        has no git checkout). Used both to gate destructive deletion and to
        decide whether a rescue snapshot is worth taking.
        """
        if not self._has_inspectable_git_boundary(source):
            return []
        result = self._run_git(["rev-list", "HEAD", "--not", "--remotes=origin"], cwd=source)
        if result.returncode != 0:
            raise WorkspaceInspectionFailedError(
                source,
                "inspect unpublished commits",
                self._git_detail(result),
            )
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _has_uncommitted_changes(self, source: Path) -> bool:
        """Report whether tracked files carry uncommitted modifications.

        Only the *tracked* dirty signal lives here; untracked agent work is
        reported separately by :meth:`_untracked_files` (which filters secrets).
        ``--untracked-files=no`` keeps orchestrator-staged, self-gitignored
        credentials under ``.spec-claude-home`` out of this signal.
        """
        if not self._has_inspectable_git_boundary(source):
            return False
        # ``--untracked-files=no`` keeps orchestrator-staged, self-gitignored
        # secrets out of the "dirty" signal and out of any rescue artifact.
        result = self._run_git(
            [
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--ignore-submodules=all",
            ],
            cwd=source,
        )
        if result.returncode != 0:
            raise WorkspaceInspectionFailedError(
                source,
                "inspect tracked changes",
                self._git_detail(result),
            )
        return bool(result.stdout.strip())

    def _checked_out_submodules(self, source: Path) -> list[str]:
        """Return checked-out gitlinks without entering their repositories.

        A container agent controls each nested ``.git/modules/*/config``. Host
        Git therefore ignores submodule dirtiness to avoid executing a nested
        fsmonitor or blocking on an included FIFO. At a destructive boundary,
        the safe conservative choice is to preserve every initialized
        submodule checkout and require an explicit operator discard instead
        of pretending its uncommitted state is known to be clean.
        """
        if not self._has_inspectable_git_boundary(source):
            return []
        result = self._run_git(["ls-files", "--stage", "-z"], cwd=source)
        if result.returncode != 0:
            raise WorkspaceInspectionFailedError(
                source,
                "inventory submodules",
                self._git_detail(result),
            )
        checked_out: list[str] = []
        for record in result.stdout.split("\0"):
            if not record:
                continue
            metadata, separator, rel = record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise WorkspaceInspectionFailedError(
                    source,
                    "inventory submodules",
                    "Git returned malformed index data",
                )
            mode, _object_id, stage = fields
            if mode != "160000" or stage != "0":
                continue
            relative = PurePosixPath(rel)
            if (
                relative.is_absolute()
                or not rel
                or ".." in relative.parts
                or "\\" in rel
            ):
                raise WorkspaceInspectionFailedError(
                    source,
                    "inventory submodules",
                    "Git returned an unsafe submodule path",
                )
            target = source.joinpath(*relative.parts)
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorkspaceInspectionFailedError(
                    source,
                    "inventory submodules",
                    f"could not inspect {rel}: {exc}",
                ) from exc
            if stat.S_ISDIR(target_stat.st_mode) and not path_is_link_or_junction(
                target
            ):
                try:
                    if next(target.iterdir(), None) is None:
                        # Non-recursive clones materialize an empty directory
                        # for an uninitialized gitlink. It contains no nested
                        # work and is safe to recreate from the snapshot.
                        continue
                except OSError as exc:
                    raise WorkspaceInspectionFailedError(
                        source,
                        "inventory submodules",
                        f"could not inspect {rel}: {exc}",
                    ) from exc
            checked_out.append(rel)
        return checked_out

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
        submodules = self._checked_out_submodules(source)
        if submodules:
            # Inspect gitlinks before any command that asks Git about worktree
            # state. Newer Git versions may enter a checked-out submodule while
            # answering those queries, which would let agent-controlled nested
            # config (for example an included FIFO or fsmonitor) block the host
            # orchestrator. Nested work cannot be captured safely without
            # crossing that trust boundary, so record it as unpreserved and let
            # restore abort with the original tree intact.
            unpushed: list[str] = []
            dirty = False
            untracked: list[str] = []
        else:
            unpushed = self._unpushed_commits(source)
            dirty = self._has_uncommitted_changes(source)
            untracked = self._untracked_files(source)
        if not unpushed and not dirty and not untracked and not submodules:
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
            "checked_out_submodules": list(submodules),
            "artifacts": {},
        }

        # Categories of detected work whose rescue artifact failed to write.
        # Any entry here means the restore must abort rather than replace (and
        # destroy) the tree, since that work is not durable anywhere else.
        unpreserved: list[str] = []
        if submodules:
            # Nested work is deliberately not traversed: agent-controlled
            # submodule Git metadata is outside the trusted host-Git boundary.
            unpreserved.append("checked-out submodule work")

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
            diff = self._run_git(
                ["diff", "HEAD", "--binary", "--ignore-submodules=all"],
                cwd=source,
            )
            if diff.returncode == 0:
                try:
                    patch_path.write_text(diff.stdout, encoding="utf-8")
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
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest["manifest_path"] = str(manifest_path)

        index_path = rescue_root / self.RESCUE_INDEX_FILENAME
        try:
            existing = (
                json.loads(index_path.read_text(encoding="utf-8"))
                if index_path.exists()
                else []
            )
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []
        existing.append(manifest)
        index_path.write_text(
            json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8"
        )
        return manifest

    def _assert_workspace_deletable(self, source: Path, *, allow_unpushed_work: bool) -> None:
        """Refuse to delete a workspace that still holds non-durable agent work.

        Deletion is blocked when the worktree has unpushed commits, uncommitted
        edits to tracked files, or untracked non-ignored files — any of which
        would be permanently lost. Only explicit operator discard paths such
        as ``spec clean`` pass ``allow_unpushed_work=True``; automatic
        post-merge cleanup keeps the guard so omitted nested work survives.
        """
        if allow_unpushed_work:
            return
        # Check gitlinks first. Some Git versions enter checked-out submodules
        # while answering broader worktree-status queries; nested config is
        # agent-controlled and may contain a blocking include or executable
        # helper. A populated submodule alone is enough to make automatic
        # deletion unsafe, so fail before running any such query.
        submodules = self._checked_out_submodules(source)
        if submodules:
            raise WorkspaceHasUnpushedWorkError(
                source,
                [],
                dirty=False,
                untracked=[],
                submodules=submodules,
            )
        unpushed = self._unpushed_commits(source)
        dirty = self._has_uncommitted_changes(source)
        untracked = self._untracked_files(source)
        if unpushed or dirty or untracked:
            raise WorkspaceHasUnpushedWorkError(
                source,
                unpushed,
                dirty=dirty,
                untracked=untracked,
                submodules=[],
            )

    @staticmethod
    def _run_git(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return run_git(
            argv,
            cwd=cwd,
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
    if not _is_valid_container_env_name(key):
        return False
    normalized = key.upper()
    if normalized in CONTAINER_WORKER_ENV_DENYLIST:
        return False
    if is_provider_process_startup_control_env_name(key):
        return False
    if any(marker in normalized for marker in CONTAINER_WORKER_ENV_SENSITIVE_MARKERS):
        return False
    return True


def _is_valid_container_env_name(key: object) -> bool:
    return isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is not None


def _is_claude_mcp_runtime_env_key(key: str) -> bool:
    """Accept only orchestrator-generated MCP alias names for key-only export."""
    return _CLAUDE_MCP_RUNTIME_ENV_RE.fullmatch(key) is not None


def _trusted_container_git_guard_environment(env: dict[str, str]) -> dict[str, str]:
    """Validate and retain the host-generated no-push Git config as one unit."""
    raw_count = env.get("GIT_CONFIG_COUNT")
    if raw_count is None:
        return {}
    try:
        count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Container Git publication guard count is invalid.") from exc
    if count < 1 or count > 256 or str(count) != raw_count:
        raise RuntimeError("Container Git publication guard count is invalid.")

    result = {"GIT_CONFIG_COUNT": raw_count}
    allowed_push_prefixes = {"https://", "http://", "ssh://", "git://", "git@", "file://"}
    for index in range(count):
        key_name = f"GIT_CONFIG_KEY_{index}"
        value_name = f"GIT_CONFIG_VALUE_{index}"
        key = env.get(key_name)
        value = env.get(value_name)
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError("Container Git publication guard is incomplete.")
        valid = (
            (key == "credential.helper" and value == "")
            or (key == "credential.interactive" and value == "false")
            or (
                key.startswith("url.specbutler-no-push://")
                and key.endswith("/.pushInsteadOf")
                and value in allowed_push_prefixes
            )
            or (
                re.fullmatch(r"remote\.[^\s\x00-\x1f]+\.pushurl", key) is not None
                and value.startswith("specbutler-no-push://remote/")
                and "\0" not in value
                and "\n" not in value
                and "\r" not in value
            )
        )
        if not valid:
            raise RuntimeError(
                "Container Git publication guard contains an unexpected entry."
            )
        result[key_name] = key
        result[value_name] = value

    indexed_names = {
        key
        for key in env
        if re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", key)
    }
    if indexed_names != set(result) - {"GIT_CONFIG_COUNT"}:
        raise RuntimeError("Container Git publication guard has stray entries.")
    if env.get("GIT_CONFIG_NOSYSTEM") != "1" or env.get("GIT_CONFIG_GLOBAL") != os.devnull:
        raise RuntimeError("Container Git publication guard isolation is incomplete.")
    result["GIT_CONFIG_NOSYSTEM"] = "1"
    # The worker image is Linux even when the host is Windows. Forwarding the
    # host spelling ``nul`` would make Git read a relative workspace file named
    # ``nul`` as global config inside the container.
    result["GIT_CONFIG_GLOBAL"] = "/dev/null"
    if env.get("GIT_TERMINAL_PROMPT") == "0":
        result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def _replace_host_path_reference(value: str, *, host_path: str, container_path: str) -> str:
    if not host_path:
        return value
    path_boundary = r"A-Za-z0-9_~\\/-"
    # A mapped path may be embedded in an argv value such as a JSON document
    # or path-list environment variable.  Normalize separators only through
    # the end of that path reference; replacing every backslash in ``value``
    # would also rewrite unrelated regexes, JSON escapes, or later values.
    reference_terminators = "\"'`,;{}[]\r\n"
    # Whitespace can be part of a Windows path, so it is not an unconditional
    # terminator.  It does end the reference when the next token is visibly a
    # command option or environment-style assignment.  Without this boundary,
    # a composite value such as ``C:\repo\a.txt --regex=\d+`` treats the
    # regex as part of the path and rewrites its backslash.
    token_start = r"(?:-{1,2}[A-Za-z0-9]|[A-Za-z_][A-Za-z0-9_]*=)"
    suffix_stop = rf"(?:[{re.escape(reference_terminators)}]|\s+(?={token_start}))"
    suffix_pattern = rf"(?P<suffix>(?:[\\/](?:(?!{suffix_stop}).)*)?)"
    variants = sorted(
        {
            host_path,
            host_path.replace("\\", "/"),
            host_path.replace("/", "\\"),
        },
        key=len,
        reverse=True,
    )
    translated = value
    for variant in variants:
        pattern = re.compile(
            rf"(?<![{path_boundary}]){re.escape(variant)}"
            rf"(?=$|[\\/]|[^{path_boundary}]){suffix_pattern}"
        )

        def replace_match(match: re.Match[str]) -> str:
            suffix = match.group("suffix") or ""
            return container_path + suffix.replace("\\", "/")

        translated = pattern.sub(replace_match, translated)
    return translated


def _redact_log_text(text: str, redactions: Sequence[str]) -> str:
    """Redact values while avoiding destructive one-character replacement.

    Short environment values are common (for example ``CI=1``). Replacing
    every occurrence of such a value would make a command log unreadable, so
    values shorter than four characters are redacted only when they appear as
    a complete non-identifier token. Longer values use literal replacement.
    """
    redacted = text
    secrets = sorted({secret for secret in redactions if secret}, key=len, reverse=True)
    for secret in secrets:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "<redacted>")
        else:
            redacted = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(secret)}(?![A-Za-z0-9_])",
                "<redacted>",
                redacted,
            )
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
            capture_output=True,
            check=False,
            **subprocess_text_kwargs(argv),
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

    @staticmethod
    def _container_safe_git_config_path(run_root: Path) -> Path:
        return run_root / "backend-state" / "host-git-config"

    @staticmethod
    def _pinned_compose_file_path(run_root: Path) -> Path:
        return run_root / "backend-state" / "operator-compose.yaml"

    @staticmethod
    def _pinned_compose_project_directory(run_root: Path) -> Path:
        return run_root / "backend-state" / "compose-project"

    @staticmethod
    def _quote_git_config_value(value: str) -> str:
        if "\0" in value or "\r" in value or "\n" in value:
            raise RuntimeError("Container Git configuration contains an unsafe value.")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _create_container_safe_git_config(
        self,
        *,
        repo_root: Path,
        run_root: Path,
    ) -> None:
        def trusted_value(*arguments: str) -> str:
            result = run_git(arguments, cwd=repo_root, check=False, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else ""

        # This file is mounted read-only into the worker, so even its fetch URL
        # must be safe for an untrusted process to read. Reuse the publication
        # boundary's local-config parser: it disables includes/global config,
        # rejects credential helpers and URL userinfo, and returns exactly one
        # credential-free origin URL.
        remote_url, _ = capture_repository_publication_baseline(repo_root)
        object_format = trusted_value("rev-parse", "--show-object-format") or "sha1"
        if object_format not in {"sha1", "sha256"}:
            raise RuntimeError(
                f"Container backend does not support Git object format {object_format!r}."
            )
        hooks = run_root / "backend-state" / "disabled-git-hooks"
        if path_is_link_or_junction(hooks) or (hooks.exists() and not hooks.is_dir()):
            raise RuntimeError(
                f"Container backend refuses unsafe disabled-hooks path: {hooks}"
            )
        hooks.mkdir(parents=True, exist_ok=True, mode=0o700)

        lines = [
            "[core]",
            f"\trepositoryFormatVersion = {'1' if object_format == 'sha256' else '0'}",
            f"\tfileMode = {trusted_value('config', '--bool', 'core.filemode') or ('false' if os.name == 'nt' else 'true')}",
            "\tbare = false",
            "\tlogAllRefUpdates = true",
            "\thooksPath = /dev/null",
            "\tfsmonitor = false",
            "[diff]",
            "\tignoreSubmodules = all",
            "[status]",
            "\tsubmoduleSummary = false",
            "[submodule]",
            "\trecurse = false",
            "[fetch]",
            "\trecurseSubmodules = false",
            "[push]",
            "\trecurseSubmodules = no",
        ]
        for key, rendered in (
            ("core.ignorecase", "ignoreCase"),
            ("core.symlinks", "symlinks"),
            ("core.precomposeunicode", "precomposeUnicode"),
        ):
            value = trusted_value("config", "--bool", key)
            if value in {"true", "false"}:
                lines.append(f"\t{rendered} = {value}")
        if object_format == "sha256":
            lines.extend(["[extensions]", "\tobjectFormat = sha256"])
        lines.extend(
            [
                '[remote "origin"]',
                f"\turl = {self._quote_git_config_value(remote_url)}",
                "\tfetch = +refs/heads/*:refs/remotes/origin/*",
            ]
        )
        user_name = trusted_value("config", "--get", "user.name")
        user_email = trusted_value("config", "--get", "user.email")
        if user_name or user_email:
            lines.append("[user]")
            if user_name:
                lines.append(f"\tname = {self._quote_git_config_value(user_name)}")
            if user_email:
                lines.append(f"\temail = {self._quote_git_config_value(user_email)}")
        safe_path = self._container_safe_git_config_path(run_root)
        atomic_write_text(safe_path, "\n".join(lines) + "\n")
        if os.name != "nt":
            safe_path.chmod(0o600)

    def _restore_container_safe_git_config(
        self,
        *,
        run_root: Path,
        source: Path,
    ) -> None:
        safe_path = self._container_safe_git_config_path(run_root)
        git_dir = source / ".git"
        if (
            not safe_path.is_file()
            or path_is_link_or_junction(safe_path)
            or safe_path.stat().st_size > 1024 * 1024
            or not git_dir.is_dir()
            or path_is_link_or_junction(git_dir)
        ):
            raise RuntimeError(
                "Container backend cannot establish a safe host Git configuration."
            )
        payload = safe_path.read_text(encoding="utf-8")
        if "\0" in payload:
            raise RuntimeError(
                "Container backend safe host Git configuration is invalid."
            )
        atomic_write_text(git_dir / "config", payload)
        self._validate_container_git_metadata_for_host(source)

    @staticmethod
    def _validate_container_git_metadata_for_host(source: Path) -> None:
        """Reject blocking or redirecting Git metadata before host Git runs.

        Agent output is repository data, but filesystem topology is not a
        trusted Git transport. A FIFO in HEAD/packed-refs or a symlink under
        objects can hang Git or redirect it outside the isolated checkout.
        Validate with lstat/scandir only, without following links or opening
        arbitrary agent-selected files. The bounded HEAD read happens after
        the runtime is quiesced (or in a private completed import staging dir).
        """
        git_dir = source / ".git"
        try:
            git_stat = git_dir.lstat()
        except OSError as exc:
            raise RuntimeError(
                "Container backend workspace Git metadata is unavailable."
            ) from exc
        if not stat.S_ISDIR(git_stat.st_mode) or path_is_link_or_junction(git_dir):
            raise RuntimeError(
                "Container backend refuses linked or non-directory .git metadata."
            )

        # Container workspaces are standalone clones.  Linked-worktree control
        # files are therefore never legitimate here, and Git would honor them
        # before consulting the metadata tree we validate below.  In
        # particular, ``commondir`` can redirect refs, objects, and packed-refs
        # outside ``.git`` and reintroduce both path escapes and blocking
        # special files at the host Git boundary.
        for control_name in ("commondir", "gitdir"):
            control_path = git_dir / control_name
            try:
                control_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(
                    "Container backend could not structurally inspect Git metadata."
                ) from exc
            raise RuntimeError(
                "Container backend refuses linked-worktree control files inside "
                f".git metadata: {control_path}"
            )

        entries_seen = 0
        stack = [git_dir]
        while stack:
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError as exc:
                raise RuntimeError(
                    "Container backend could not structurally inspect Git metadata."
                ) from exc
            with entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > _CONTAINER_GIT_METADATA_MAX_ENTRIES:
                        raise RuntimeError(
                            "Container backend Git metadata exceeds the structural safety limit."
                        )
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise RuntimeError(
                            "Container backend could not structurally inspect Git metadata."
                        ) from exc
                    is_reparse_point = bool(
                        getattr(entry_stat, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    )
                    if stat.S_ISLNK(entry_stat.st_mode) or is_reparse_point:
                        raise RuntimeError(
                            "Container backend refuses symbolic links or reparse "
                            "points inside .git metadata: "
                            f"{entry.path}"
                        )
                    if stat.S_ISDIR(entry_stat.st_mode):
                        stack.append(Path(entry.path))
                    elif not stat.S_ISREG(entry_stat.st_mode):
                        raise RuntimeError(
                            "Container backend refuses special files inside .git metadata: "
                            f"{entry.path}"
                        )

        for relative in ("objects/info/alternates", "objects/info/http-alternates"):
            if (git_dir / relative).exists():
                raise RuntimeError(
                    "Container backend refuses Git object alternates in agent output."
                )
        for required_directory in (git_dir / "objects", git_dir / "refs"):
            try:
                required_stat = required_directory.lstat()
            except OSError as exc:
                raise RuntimeError(
                    "Container backend workspace Git metadata is incomplete."
                ) from exc
            if not stat.S_ISDIR(required_stat.st_mode):
                raise RuntimeError(
                    "Container backend workspace Git metadata is incomplete."
                )

        head_path = git_dir / "HEAD"
        try:
            head_stat = head_path.lstat()
        except OSError as exc:
            raise RuntimeError(
                "Container backend workspace Git HEAD is unavailable."
            ) from exc
        if not stat.S_ISREG(head_stat.st_mode) or head_stat.st_size > 4096:
            raise RuntimeError(
                "Container backend workspace Git HEAD is not a bounded regular file."
            )
        try:
            head = read_bounded_regular_text(
                head_path,
                max_bytes=4096,
                encoding="ascii",
            ).strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                "Container backend workspace Git HEAD is invalid."
            ) from exc
        direct_head = re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head)
        symbolic = head.removeprefix("ref: ") if head.startswith("ref: ") else ""
        safe_symbolic = (
            re.fullmatch(r"refs/[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}", symbolic)
            is not None
            and ".." not in symbolic.split("/")
            and not symbolic.endswith(("/", ".lock"))
        )
        if direct_head is None and not safe_symbolic:
            raise RuntimeError(
                "Container backend workspace Git HEAD has an unsafe reference."
            )

    @staticmethod
    def _run_git(
        argv: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        # Structural validation removes known blocking filesystem primitives;
        # the timeout is the final fail-closed bound for malformed Git data.
        return run_git(
            argv,
            cwd=cwd,
            check=False,
            timeout=30,
            env=host_publication_git_environment(),
        )

    def _prepare_workspace_git_boundary(
        self,
        *,
        repo_root: Path,
        run_root: Path,
        source: Path,
        run_id: str,
        spec_id: str,
        source_created: bool,
    ) -> None:
        previous = self._read_container_state(run_root, missing_ok=True)
        retrying_existing_run = bool(previous) or not source_created
        if retrying_existing_run and self._service_topology() == "sidecar":
            # A pre-0.5 run has no operator-controlled Compose baseline. Check
            # this migration boundary before removing any existing runtime;
            # failure must leave the old generation available for inspection.
            self._validated_pinned_compose_file(run_root)
        if retrying_existing_run:
            labels = self._resource_labels(
                run_id=run_id,
                spec_id=spec_id,
                workspace_root=source,
            )
            quiesce_state = dict(previous)
            quiesce_state["resource_labels"] = labels
            quiesce_state["containers"] = []
            # This state can be stale or corrupt. Quiescing needs only the
            # host-derived labels; never let persisted mode/path fields trigger
            # host-path mutation before the safe Git boundary is restored.
            quiesce_state["workspace_mode"] = "volume"
            quiesce_state["service_data_dirs"] = []
            self._remove_exact_owned_runtime_containers(run_root, quiesce_state)
        self._create_container_safe_git_config(
            repo_root=repo_root,
            run_root=run_root,
        )
        self._restore_container_safe_git_config(
            run_root=run_root,
            source=source,
        )
        if self._service_topology() == "sidecar":
            self._pin_or_validate_operator_compose(
                repo_root=repo_root,
                run_root=run_root,
                source_created=not retrying_existing_run,
            )

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
        return self._prepare_workspace(
            run_id=run_id,
            spec_id=spec_id,
            branch=branch,
            repo_root=repo_root,
            worktree_path=worktree_path,
            base_ref=base_ref,
            start_runtime=True,
        )

    def materialize_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
    ) -> WorkspaceHandle:
        """Prepare a host-safe checkout without starting project processes."""
        return self._prepare_workspace(
            run_id=run_id,
            spec_id=spec_id,
            branch=branch,
            repo_root=repo_root,
            worktree_path=worktree_path,
            base_ref=base_ref,
            start_runtime=False,
        )

    def _prepare_workspace(
        self,
        *,
        run_id: str,
        spec_id: str,
        branch: str,
        repo_root: Path,
        worktree_path: Path | None = None,
        base_ref: str = "",
        start_runtime: bool,
    ) -> WorkspaceHandle:
        # Retry compatibility is a filesystem-only preflight. In particular,
        # do not contact or mutate the container engine for a pre-0.5 sidecar
        # run that cannot be resumed without a protected Compose baseline.
        checked_run_id = validate_workspace_run_identity(run_id, spec_id)
        checked_repo_root = repo_root.resolve()
        candidate_root = self._resolve_workspace_root(checked_repo_root)
        candidate_run_root = candidate_root / checked_run_id
        candidate_source = candidate_run_root / "source"
        prior_state = self._read_container_state(
            candidate_run_root,
            missing_ok=True,
        )
        prior_topology = prior_state.get("service_topology")
        current_topology = self._service_topology()
        if prior_state:
            if prior_state.get("backend") != "container":
                raise RuntimeError(
                    "Container backend state has an invalid backend identity; "
                    "refusing to contact the container engine."
                )
            prior_engine = prior_state.get("engine")
            if not isinstance(prior_engine, str) or not prior_engine:
                raise RuntimeError(
                    "Container backend state has an invalid engine identity; "
                    "refusing to contact the container engine."
                )
            if prior_engine != self._container.engine:
                raise RuntimeError(
                    "Container backend cannot change container engines while "
                    "resuming an existing run; restore the original engine before "
                    "retrying or cleaning that run."
                )
            if (
                not isinstance(prior_topology, str)
                or prior_topology not in {"in-worker", "sidecar"}
            ):
                raise RuntimeError(
                    "Container backend state has an invalid service topology; "
                    "refusing to contact the container engine."
                )
            if prior_topology != current_topology:
                raise RuntimeError(
                    "Container backend cannot change service topology while resuming "
                    "an existing run; clean or finish that run before changing config."
                )
            prior_mode = prior_state.get("workspace_mode")
            current_mode = self._effective_workspace_mode()
            if (
                not isinstance(prior_mode, str)
                or prior_mode not in {"bind", "volume"}
            ):
                raise RuntimeError(
                    "Container backend state has an invalid workspace mode; "
                    "refusing to contact the container engine."
                )
            if prior_mode != current_mode:
                raise RuntimeError(
                    "Container backend cannot change workspace mode while resuming "
                    "an existing run; clean or finish that run before changing config."
                )
            prior_playwright = prior_state.get("playwright_mcp")
            prior_playwright_topology = (
                prior_playwright.get("topology")
                if isinstance(prior_playwright, dict)
                else None
            )
            current_playwright_topology = self._container.playwright_mcp.topology
            if not isinstance(prior_playwright_topology, str) or (
                prior_playwright_topology
                not in {
                    "disabled",
                    "in-worker",
                    "sidecar",
                }
            ):
                raise RuntimeError(
                    "Container backend state has an invalid Playwright topology; "
                    "refusing to contact the container engine."
                )
            if prior_playwright_topology != current_playwright_topology:
                raise RuntimeError(
                    "Container backend cannot change Playwright topology while "
                    "resuming an existing run; clean or finish that run before "
                    "changing config."
                )
            if self._canonical_state_resource_labels(
                candidate_run_root,
                prior_state,
            ) is None:
                raise RuntimeError(
                    "Container backend state has non-canonical resource labels; "
                    "refusing to contact the container engine."
                )
            seed_state = prior_state.get("workspace_volume_seed_state")
            if prior_mode == "volume" and (
                not isinstance(seed_state, str)
                or seed_state not in {"unseeded", "seeding", "ready"}
            ):
                raise RuntimeError(
                    "Container backend found a legacy workspace volume without a "
                    "valid crash-safe seed marker. It will not contact the container "
                    "engine or guess whether newer work exists only in the volume."
                )
        if prior_topology == "sidecar" or (
            current_topology == "sidecar"
            and (candidate_source.exists() or bool(prior_state))
        ):
            self._validated_pinned_compose_file(candidate_run_root)
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
        previous_state = self._read_container_state(run_root, missing_ok=True)
        logs = run_root / "logs"
        resource_labels = self._resource_labels(
            run_id=run_id,
            spec_id=spec_id,
            workspace_root=handle.path,
        )
        previous_labels = self._canonical_state_resource_labels(
            run_root,
            previous_state,
        )
        preexisting_resources: dict[str, set[str]] | None = None
        try:
            preexisting_resources = self._discover_owned_cleanup_resources(
                run_root,
                resource_labels,
            )
        except OSError:
            # A prior state may point at data-bearing resources. Without an
            # authoritative inventory we cannot decide whether to retain its
            # legacy names or allocate checkout-scoped replacements.
            if previous_state:
                raise RuntimeError(
                    "Container backend could not inventory resources from the "
                    "previous attempt; refusing to guess whether existing "
                    "workspace or service data must be resumed."
                )
        else:
            # Treat engine label filters as candidate discovery only. Reinspect
            # every result before migration/startup preservation so an
            # unlabeled or foreign same-name resource cannot masquerade as an
            # exact-owned legacy generation.
            verified_resources: dict[str, set[str]] = {
                "container": set(),
                "volume": set(),
                "network": set(),
            }
            for kind, references in preexisting_resources.items():
                for reference in references:
                    actual_labels = self._inspect_resource_labels(
                        run_root,
                        kind,
                        reference,
                    )
                    if all(
                        actual_labels.get(key) == value
                        for key, value in resource_labels.items()
                    ):
                        verified_resources[kind].add(reference)
            preexisting_resources = verified_resources
            persistent_resources = (
                preexisting_resources["volume"]
                | preexisting_resources["network"]
            )
            if persistent_resources and previous_labels != resource_labels:
                preview = ", ".join(sorted(persistent_resources)[:5])
                raise RuntimeError(
                    "Container backend found exact-labeled persistent resources "
                    "without trustworthy matching state; refusing to orphan or "
                    f"overwrite ambiguous data ({preview})."
                )
        codex_provider_home = self.codex_provider_home_root(handle.path) / ".spec-codex-home"
        for candidate in (codex_provider_home.parent, codex_provider_home):
            if path_is_link_or_junction(candidate) or (
                candidate.exists() and not candidate.is_dir()
            ):
                raise RuntimeError(
                    f"Container backend refuses unsafe Codex provider-home path: {candidate}"
                )
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                candidate.chmod(0o700)
        image = self._resolve_worker_image(repo_root=repo_root, run_root=run_root, logs=logs)
        self._ensure_container_passwd_shim(
            run_root=run_root,
            image=image,
            resource_labels=resource_labels,
        )
        mode = self._effective_workspace_mode()
        service_topology = self._service_topology()
        service_env = self._service_env(service_topology)
        service_redactions = self._service_env_redactions(service_env)
        compose_project = (
            self._compose_project_name(run_id, handle.path)
            if service_topology == "sidecar"
            else ""
        )
        compose_file = (
            self._validated_pinned_compose_file(run_root)
            if service_topology == "sidecar"
            else None
        )
        playwright_mcp = self._playwright_mcp_state(
            run_id=run_id,
            source=handle.path,
            logs=logs,
            image=image,
            service_topology=service_topology,
            resource_labels=resource_labels,
        )
        workspace_volumes = self._volume_names(run_id, mode, handle.path)
        service_volumes = self._service_volume_names(run_id, service_topology)
        service_networks = self._service_network_names(
            run_id,
            service_topology,
            handle.path,
        )
        service_volume_snapshots: dict[str, Any] = {}
        if previous_labels == resource_labels and preexisting_resources is not None:
            (
                workspace_volumes,
                compose_project,
                service_volumes,
                service_networks,
                playwright_mcp,
                service_volume_snapshots,
            ) = self._adopt_previous_resource_names(
                run_id=run_id,
                run_root=run_root,
                workspace_root=handle.path,
                mode=mode,
                service_topology=service_topology,
                previous_state=previous_state,
                preexisting_resources=preexisting_resources,
                expected_labels=resource_labels,
                workspace_volumes=workspace_volumes,
                compose_project=compose_project,
                service_volumes=service_volumes,
                service_networks=service_networks,
                playwright_mcp=playwright_mcp,
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
            "runtime_generation_containers": [],
            "runtime_generation_status": "quiesced",
            "volumes": workspace_volumes,
            "workspace_volumes": workspace_volumes,
            "service_volumes": service_volumes,
            "service_volume_snapshots": service_volume_snapshots,
            "networks": [],
            "service_networks": service_networks,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resource_labels": resource_labels,
            "workspace_volume_seed_state": (
                "unseeded" if mode == "volume" else "not-applicable"
            ),
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
        if preexisting_resources is None:
            # Preparation can still proceed, but a later startup failure must
            # leak rather than guess which resources predated this attempt.
            state["startup_cleanup_safe"] = False
            state["startup_preserved_resources"] = {}
        else:
            state["startup_cleanup_safe"] = True
            state["startup_preserved_resources"] = {
                kind: sorted(resources)
                for kind, resources in preexisting_resources.items()
            }
        workspace_volume = next(iter(state["workspace_volumes"]), "")
        workspace_volume_preexisting = bool(
            workspace_volume
            and preexisting_resources is not None
            and workspace_volume in preexisting_resources.get("volume", set())
        )
        previous_seed_state = str(
            previous_state.get("workspace_volume_seed_state") or ""
        )
        previous_volumes = previous_state.get("workspace_volumes") or previous_state.get(
            "volumes", []
        )
        previous_volume = (
            str(previous_volumes[0])
            if isinstance(previous_volumes, list) and previous_volumes
            else ""
        )
        interrupted_seed = (
            workspace_volume_preexisting
            and previous_labels == resource_labels
            and previous_volume == workspace_volume
            and previous_seed_state in {"unseeded", "seeding"}
        )
        legacy_seed_state_ambiguous = (
            workspace_volume_preexisting
            and previous_labels == resource_labels
            and previous_volume == workspace_volume
            and "workspace_volume_seed_state" not in previous_state
        )
        if legacy_seed_state_ambiguous:
            raise RuntimeError(
                "Container backend found a legacy workspace volume without a "
                "crash-safe seed marker. It may contain either newer agent work "
                "or a partial interrupted seed, so Spec Butler will not replace "
                "the host checkout or overwrite the volume automatically. Preserve "
                f"and inspect volume {workspace_volume!r}, or explicitly clean this "
                "run before retrying."
            )
        if workspace_volume_preexisting:
            # Preserve "ready" across a transient import failure so the next
            # retry tries the non-destructive import again. Missing legacy
            # markers were rejected above because structural Git validation
            # cannot prove their working tree was completely seeded.
            state["workspace_volume_seed_state"] = (
                "seeding" if interrupted_seed else "ready"
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
        self._remove_exact_owned_runtime_containers(run_root, state)
        containers_verified_gone = True
        self._clear_stale_postmaster_pids(run_root, state)
        if mode == "volume":
            if preexisting_resources is None:
                raise RuntimeError(
                    "Container backend could not determine whether this run's "
                    "workspace volume already exists; refusing to seed it."
                )
            if (
                workspace_volume_preexisting
                and state["workspace_volume_seed_state"] == "ready"
            ):
                # A timed-out or interrupted provider can leave its newest git
                # state only in the authoritative workspace volume. Import it
                # after every prior worker is positively gone and before the
                # destructive seed below. The import itself revalidates volume
                # ownership and every attached consumer.
                self._sync_volume_workspace_to_host(run_root, state)
                # Never bring the completion/control state exposed to the old
                # worker back into the host mirror used for the next attempt.
                self._remove_worker_visible_state(handle.path)
                self._restore_container_safe_git_config(
                    run_root=run_root,
                    source=handle.path,
                )
        # Preparation intentionally starts no project-controlled process. The
        # checkout is now safe for bootstrap-phase host writes and Git. The
        # first backend command (or an explicit resume transition) seeds the
        # final host tree, starts services/worker, and runs bootstrap exactly
        # once for that runtime generation.
        state["prior_containers_verified_gone"] = containers_verified_gone
        self._write_container_state(run_root, state)
        prepared = WorkspaceHandle(
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
        if start_runtime:
            self._ensure_container_runtime_started(prepared.path)
        return prepared

    def _run_container_bootstrap_install(self, handle: WorkspaceHandle) -> None:
        if not self._bootstrap_install_command:
            return
        result = self._run_command_in_started_runtime(
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

    def _workspace_handle_from_run_root(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> WorkspaceHandle:
        return WorkspaceHandle(
            path=run_root / "source",
            outbox_path=run_root / "outbox",
            branch=str(state.get("branch") or ""),
            backend="container",
            metadata={
                "run_id": str(state.get("resource_labels", {}).get("spec.run_id") or run_root.name),
                "spec_id": str(state.get("resource_labels", {}).get("spec.spec_id") or ""),
                "workspace_root": str(run_root.parent),
            },
        )

    def _ensure_container_runtime_started(self, workspace_cwd: Path) -> None:
        run_root = self._workspace_run_root(workspace_cwd)
        if run_root is None:
            raise RuntimeError(
                f"Container backend command cwd is not inside a prepared workspace: {workspace_cwd}"
            )
        state = self._read_container_state(run_root)
        paused = [
            str(item)
            for item in state.get("host_access_paused_containers", [])
            if str(item)
        ]
        if paused:
            raise RuntimeError(
                "Container backend refuses to run a command while host access owns "
                "the suspended workspace boundary."
            )
        if self._recorded_runtime_is_healthy(run_root, state):
            return
        # The state can lag a crash or manual edit, and a recorded worker is
        # not proof that its sidecars are still alive.  Before creating a new
        # runtime generation, remove every exact-owned container discovered
        # from the engine so no partial or unrecorded generation can race it.
        cleanly_quiesced = (
            state.get("runtime_generation_status") == "quiesced"
            and not state.get("worker_container")
            and not state.get("containers")
            and not state.get("runtime_generation_containers")
        )
        if not cleanly_quiesced:
            self._quiesce_runtime_for_host_access(
                run_root=run_root,
                source=workspace_cwd,
                purpose="runtime health recovery",
            )
            state = self._read_container_state(run_root)
        # Arm recovery before the first resource is started. A crash anywhere
        # below leaves "starting", so the next command quiesces every
        # exact-owned partial resource instead of trusting an empty id list.
        state["runtime_generation_status"] = "starting"
        self._write_container_state(run_root, state)
        handle = self._workspace_handle_from_run_root(run_root, state)
        try:
            if state.get("workspace_mode") == "volume":
                if state.pop("workspace_volume_preseeded_for_runtime", False):
                    # A host reposition explicitly seeded this exact tree while
                    # quiesced. Consume the one-start token durably before
                    # starting any writer; a crash after this point safely
                    # falls back to a fresh seed on retry.
                    self._write_container_state(run_root, state)
                else:
                    self._seed_volume_workspace(handle, state)
                self._clear_stale_volume_postmaster_pids(
                    run_root,
                    state,
                    bool(state.get("prior_containers_verified_gone", True)),
                )
            if state.get("service_topology") == "sidecar":
                self._start_sidecar_services(
                    run_root=run_root,
                    logs=run_root / "logs",
                    compose_file=self._validated_pinned_compose_file(run_root),
                    compose_project=str(state.get("compose_project") or ""),
                )
                self._refresh_sidecar_service_volumes(run_root, state)
            playwright = state.get("playwright_mcp", {})
            if isinstance(playwright, dict) and playwright.get("topology") == "sidecar":
                self._start_playwright_mcp_sidecar(run_root, run_root / "logs", state)
            self._start_in_worker_container(run_root, state)
            self._run_container_bootstrap_install(handle)
            state = self._read_container_state(run_root)
            self._record_runtime_generation(run_root, state)
            if not self._recorded_runtime_is_healthy(run_root, state):
                raise RuntimeError(
                    "Container backend could not verify the complete runtime generation."
                )
        except BaseException as startup_error:
            teardown_state = self._read_container_state(run_root, missing_ok=True) or state
            try:
                self._remove_exact_owned_runtime_containers(run_root, teardown_state)
            except BaseException as quiesce_error:
                raise RuntimeError(
                    "Container runtime startup failed and its writers could not "
                    "be positively quiesced. Manual scoped cleanup is required."
                ) from ExceptionGroup(
                    "runtime startup and container quiescence both failed",
                    [startup_error, quiesce_error],
                )
            teardown_state["worker_container"] = ""
            teardown_state["containers"] = []
            teardown_state["service_processes"] = []
            teardown_state["host_access_paused_containers"] = []
            teardown_state["runtime_generation_containers"] = []
            teardown_state["runtime_generation_status"] = "quiesced"
            self._write_container_state(run_root, teardown_state)
            # Containers are now positively gone. Remaining volume/network
            # cleanup is best-effort and must not mask the startup failure.
            try:
                self._teardown_container_resources(run_root, teardown_state)
            except BaseException:
                pass
            raise

    def run_command(self, request: CommandRequest) -> CommandResult:
        self._ensure_container_runtime_started(request.cwd)
        return self._run_command_in_started_runtime(request)

    def _run_command_in_started_runtime(self, request: CommandRequest) -> CommandResult:
        run_root = self._workspace_run_root(request.cwd)
        if run_root is None:
            raise RuntimeError(f"Container backend command cwd is not inside a prepared workspace: {request.cwd}")
        state = self._read_container_state(run_root)
        self._require_workspace_volume_safe(run_root, state)
        worker_env = self._container_worker_environment(
            run_root=run_root,
            env=request.env or {},
            state=state,
        )
        argv = self._container_run_argv(
            run_root=run_root,
            cwd=request.cwd,
            command=request.argv,
            worker_env=worker_env,
            state=state,
        )
        env = self._container_client_env(
            worker_env,
            inherit_env=request.inherit_env,
        )
        completed = self._runner.run(
            argv,
            cwd=run_root,
            env=env,
            input_text=request.input_text,
            timeout=request.timeout,
        )
        self._write_command_log(
            kind="container-command",
            cwd=request.cwd,
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            redactions=(
                *self._service_log_redactions(state),
                *self._request_env_log_redactions(worker_env),
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
        self._ensure_container_runtime_started(request.cwd)
        state = self._read_container_state(run_root)
        self._require_workspace_volume_safe(run_root, state)
        worker_env = self._container_worker_environment(
            run_root=run_root,
            env=request.env or {},
            state=state,
            declared_env_keys=request.declared_env_keys,
        )
        argv = self._container_run_argv(
            run_root=run_root,
            cwd=request.cwd,
            command=request.argv,
            worker_env=worker_env,
            state=state,
            agent=True,
        )
        client_env = self._container_client_env(worker_env)
        completed: subprocess.CompletedProcess[str] | None = None
        returncode = 1
        try:
            if monitor is None:
                completed = self._runner.run(argv, cwd=run_root, env=client_env)
                returncode = completed.returncode
            else:
                popen_kwargs = dict(request.popen_kwargs)
                proc = self._runner.popen(
                    argv,
                    cwd=run_root,
                    env=client_env,
                    popen_kwargs=popen_kwargs,
                )
                returncode = _run_agent_monitor(proc, monitor)
        except BaseException as agent_error:
            try:
                self._quiesce_runtime_for_host_access(
                    run_root=run_root,
                    source=request.cwd,
                    purpose="failed agent return",
                )
            except BaseException as quiesce_error:
                raise RuntimeError(
                    "Container agent execution failed and its runtime could not "
                    "be positively quiesced. Manual scoped cleanup is required."
                ) from ExceptionGroup(
                    "agent execution and container quiescence both failed",
                    [agent_error, quiesce_error],
                )
            raise
        else:
            # This is the liveness boundary, not a diagnostic side effect. It
            # must run before touching worker-writable logs/outbox paths: an
            # agent can make those paths unwritable, and a logging exception
            # must never strand the persistent worker or its descendants.
            self._quiesce_runtime_for_host_access(
                run_root=run_root,
                source=request.cwd,
                purpose="agent return",
            )

        stdout = completed.stdout or "" if completed is not None else ""
        stderr = completed.stderr or "" if completed is not None else ""
        self._write_command_log(
            kind="container-agent",
            cwd=request.cwd,
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            redactions=(
                *self._service_log_redactions(state),
                *self._request_env_log_redactions(worker_env),
                *request.redactions,
            ),
        )
        self._write_agent_result(
            cwd=request.cwd,
            argv=request.argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        return AgentResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _write_completion_artifacts(self, *, source: Path, outbox: Path) -> None:
        """Collect evidence after :meth:`launch_agent` closes the runtime."""
        super()._write_completion_artifacts(source=source, outbox=outbox)

    def prepare_host_access(self, workspace: WorkspaceHandle) -> None:
        run_root = workspace.outbox_path.parent.resolve()
        if workspace.path.resolve() != (run_root / "source").resolve():
            raise RuntimeError(
                "Container backend refuses host access for a mismatched workspace path."
            )
        self._quiesce_runtime_for_host_access(
            run_root=run_root,
            source=workspace.path,
            purpose="host phase transition",
        )

    def suspend_for_host_access(self, workspace: WorkspaceHandle) -> None:
        """Pause all exactly-owned containers and stabilize the host mirror."""
        run_root = workspace.outbox_path.parent.resolve()
        if workspace.path.resolve() != (run_root / "source").resolve():
            raise RuntimeError(
                "Container backend refuses host suspension for a mismatched workspace."
            )
        state = self._read_container_state(run_root)
        existing = [
            str(item)
            for item in state.get("host_access_paused_containers", [])
            if str(item)
        ]
        if existing:
            for container_id in existing:
                if not self._container_pause_state(run_root, container_id):
                    raise RuntimeError(
                        "Container backend lost its suspended runtime boundary."
                    )
        else:
            labels = self._canonical_state_resource_labels(run_root, state)
            if labels is None:
                raise RuntimeError(
                    "Container backend refuses host suspension with non-canonical labels."
                )
            owned = self._discover_owned_cleanup_resources(run_root, labels)
            paused: list[str] = []
            try:
                for container_id in sorted(owned["container"]):
                    status = self._container_runtime_status(
                        run_root,
                        state,
                        container_id,
                    )
                    if status in {"created", "exited", "dead"}:
                        # A completed one-shot Compose job or crashed sidecar is
                        # not a writer and cannot be paused. It remains covered
                        # by exact ownership and later cleanup/quiescence.
                        continue
                    if status != "running":
                        raise RuntimeError(
                            "Container backend cannot establish a stable host "
                            f"boundary while exact-owned container {container_id} "
                            f"is in unexpected state {status!r}."
                        )
                    self._pause_owned_container(run_root, state, container_id)
                    paused.append(container_id)
            except Exception:
                for container_id in reversed(paused):
                    try:
                        self._unpause_owned_container(run_root, state, container_id)
                    except Exception:
                        pass
                raise
            state["host_access_paused_containers"] = paused
            self._write_container_state(run_root, state)
        if state.get("workspace_mode") == "volume":
            self._sync_volume_workspace_to_host(run_root, state)
        self._restore_container_safe_git_config(
            run_root=run_root,
            source=workspace.path,
        )

    def resume_after_host_access(self, workspace: WorkspaceHandle) -> None:
        """Resume a paused generation or recreate a fully quiesced runtime."""
        run_root = workspace.outbox_path.parent.resolve()
        if workspace.path.resolve() != (run_root / "source").resolve():
            raise RuntimeError(
                "Container backend refuses runtime resume for a mismatched workspace."
            )
        state = self._read_container_state(run_root)
        paused = [
            str(item)
            for item in state.get("host_access_paused_containers", [])
            if str(item)
        ]
        if paused:
            try:
                healthy = self._recorded_runtime_is_healthy(
                    run_root,
                    state,
                    allow_paused=True,
                )
            except Exception as health_error:
                self._discard_unusable_runtime_generation(
                    run_root=run_root,
                    source=workspace.path,
                    reason="could not validate the paused runtime generation",
                    cause=health_error,
                )
            if not healthy:
                self._discard_unusable_runtime_generation(
                    run_root=run_root,
                    source=workspace.path,
                    reason="the paused runtime generation changed or exited",
                )
            try:
                remaining = list(paused)
                for container_id in reversed(paused):
                    self._unpause_owned_container(run_root, state, container_id)
                    remaining.remove(container_id)
                    state["host_access_paused_containers"] = remaining
                    self._write_container_state(run_root, state)
                state = self._read_container_state(run_root)
                if not self._recorded_runtime_is_healthy(run_root, state):
                    raise RuntimeError(
                        "the resumed runtime generation could not be verified"
                    )
            except Exception as resume_error:
                self._discard_unusable_runtime_generation(
                    run_root=run_root,
                    source=workspace.path,
                    reason="could not safely resume the paused runtime generation",
                    cause=resume_error,
                )
            return
        self._ensure_container_runtime_started(workspace.path)

    def _discard_unusable_runtime_generation(
        self,
        *,
        run_root: Path,
        source: Path,
        reason: str,
        cause: BaseException | None = None,
    ) -> None:
        """Quiesce an invalid generation and force the caller to rerun setup."""
        try:
            self._quiesce_runtime_for_host_access(
                run_root=run_root,
                source=source,
                purpose="runtime generation reset",
            )
        except Exception as quiesce_error:
            errors: list[BaseException] = [quiesce_error]
            if cause is not None:
                errors.insert(0, cause)
            raise ExecutionBackendQuiescenceError(
                "Container backend could not quiesce an unusable runtime generation."
            ) from ExceptionGroup("runtime validation and quiescence failed", errors)
        error = ExecutionBackendRuntimeResetError(
            f"Container backend discarded its runtime because {reason}; "
            "the implementation setup phase must be retried."
        )
        if cause is None:
            raise error
        raise error from cause

    def _quiesce_runtime_for_host_access(
        self,
        *,
        run_root: Path,
        source: Path,
        purpose: str,
    ) -> None:
        state = self._read_container_state(run_root)
        self._remove_exact_owned_runtime_containers(run_root, state)
        state["worker_container"] = ""
        state["containers"] = []
        state["service_processes"] = []
        state["host_access_paused_containers"] = []
        state["runtime_generation_containers"] = []
        state["runtime_generation_status"] = "quiesced"
        self._write_container_state(run_root, state)
        if (
            state.get("workspace_mode") == "volume"
            and state.get("workspace_volume_seed_state") == "ready"
        ):
            # The volume is authoritative. Import only after every owned
            # writer is positively gone so the host receives one consistent,
            # final image rather than a raced mid-copy view.
            self._sync_volume_workspace_to_host(run_root, state)
        self._restore_container_safe_git_config(
            run_root=run_root,
            source=source,
        )

    def snapshot(self, workspace: WorkspaceHandle, label: str) -> SnapshotRef:
        run_root = workspace.outbox_path.parent.resolve()
        target = run_root / "snapshots" / _safe_artifact_name(label)
        existing = self._completed_snapshot(target, label)
        if existing is not None:
            # A recovery point is immutable.  In particular, do not pair an
            # existing source snapshot with a freshly captured sidecar database
            # on a resumed setup attempt.  Validate that the original sidecar
            # archive set is still complete, then return it unchanged.
            state = self._read_container_state(run_root)
            if state.get("service_topology") == "sidecar":
                self._validated_sidecar_service_volume_restore(
                    run_root,
                    state,
                    label,
                )
            return existing
        self._ensure_container_runtime_started(workspace.path)
        state = self._read_container_state(run_root, missing_ok=True)
        sidecars_stopped = False
        paused_containers: list[str] = []
        try:
            # setup_command may deliberately leave services running in the
            # persistent worker. Freeze (rather than remove) those exact-owned
            # containers so the snapshot is consistent without losing setup
            # descendants that the implementation agent still needs.
            for container_id in self._snapshot_pause_container_ids(state):
                self._pause_owned_container(run_root, state, container_id)
                paused_containers.append(container_id)
            if state.get("service_topology") == "sidecar":
                # Once the protected Compose input is known-good, any stop
                # attempt may have changed service state even if its later
                # verification fails. Arm the finally restart first.
                self._validated_pinned_compose_file(run_root)
                sidecars_stopped = True
                self._stop_sidecar_services(run_root, state)
                self._refresh_sidecar_service_volumes(run_root, state)
                self._snapshot_sidecar_service_volumes(run_root, state, label)
            self._sync_volume_workspace_to_host(run_root, state)
            self._restore_container_safe_git_config(
                run_root=run_root,
                source=workspace.path,
            )
            ref = super().snapshot(workspace, label)
            metadata = ref.metadata | {
                "backend": "container",
                "snapshot_kind": "source-copy",
                "service_topology": state.get("service_topology", "in-worker"),
                "service_data_dirs": state.get("service_data_dirs", []),
                "service_volumes": state.get("service_volumes", []),
                "service_volume_snapshots": state.get("service_volume_snapshots", {}).get(label, {}),
            }
            atomic_write_text(
                ref.path.parent / f"{ref.path.name}.json",
                json.dumps(
                    metadata | {"label": label, "path": str(ref.path)},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            return SnapshotRef(label=ref.label, path=ref.path, metadata=metadata)
        finally:
            restart_error: Exception | None = None
            try:
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
            except Exception as exc:
                restart_error = exc
            for container_id in reversed(paused_containers):
                try:
                    self._unpause_owned_container(run_root, state, container_id)
                except Exception as exc:
                    if restart_error is None:
                        restart_error = exc
            if restart_error is None:
                try:
                    state = self._read_container_state(run_root)
                    self._record_runtime_generation(run_root, state)
                except Exception as exc:
                    restart_error = exc
            if restart_error is not None:
                try:
                    # Never leave a live worker paired with missing/dead
                    # sidecars. Even successful quiescence invalidates any
                    # setup-created descendants, so the orchestrator must retry
                    # setup rather than silently starting an empty generation.
                    self._quiesce_runtime_for_host_access(
                        run_root=run_root,
                        source=workspace.path,
                        purpose="snapshot recovery",
                    )
                except Exception as quiesce_error:
                    raise ExecutionBackendQuiescenceError(
                        "Container backend could not quiesce the runtime after "
                        "snapshot restart failed."
                    ) from quiesce_error
                raise ExecutionBackendRuntimeResetError(
                    "Container backend discarded its runtime after snapshot "
                    "restart failed; the implementation setup phase must be retried."
                ) from restart_error

    def restore(
        self,
        workspace: WorkspaceHandle,
        snapshot: SnapshotRef,
    ) -> WorkspaceHandle:
        self._validated_cleanup_layout(
            workspace,
            migrate_legacy_owner=False,
        )
        run_root = workspace.outbox_path.parent.resolve()
        try:
            state = self._read_container_state(run_root, missing_ok=True)
        except RuntimeError as exc:
            raise OSError(str(exc)) from exc
        # Restore is a host-owned tree mutation. Stop *every* exactly-owned
        # writer first, including the Playwright sidecar, rather than managing
        # worker/Compose subsets independently. The returned workspace remains
        # quiesced; its next command performs one explicit seed/start/bootstrap.
        self._quiesce_runtime_for_host_access(
            run_root=run_root,
            source=workspace.path,
            purpose="workspace restore",
        )
        prepared_service_volumes: list[tuple[str, Path]] | None = None
        if state.get("service_topology") == "sidecar":
            # Validate the complete database/service recovery set before the
            # source tree is replaced. A missing later archive must not leave
            # source rolled back while service data remains advanced.
            prepared_service_volumes = self._validated_sidecar_service_volume_restore(
                run_root,
                state,
                snapshot.label,
            )
        restored = super().restore(workspace, snapshot)
        run_root = restored.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True) or state
        self._restore_container_safe_git_config(
            run_root=run_root,
            source=restored.path,
        )
        if state.get("service_topology") == "sidecar":
            self._restore_sidecar_service_volumes(
                run_root,
                state,
                snapshot.label,
                prepared=prepared_service_volumes,
            )
        return restored

    def _prepare_restored_checkout_git_boundary(
        self,
        *,
        run_root: Path,
        source: Path,
    ) -> None:
        # Restore the operator-generated config and structurally validate the
        # private staging checkout before it can replace the host-visible tree.
        self._restore_container_safe_git_config(
            run_root=run_root,
            source=source,
        )

    def _teardown_container_resources(self, run_root: Path, state: dict[str, Any]) -> None:
        """Remove docker resources recorded in ``state`` (sidecars, service
        processes, containers, volumes, networks) *without* deleting the
        ``run_root`` filesystem, so captured logs/diagnostics survive.

        Used on the ``prepare_workspace`` failure path where a full
        :meth:`cleanup` would ``rmtree`` the run root and destroy the
        service-startup-failure diagnostic. Best-effort: every step swallows
        errors so teardown never masks the original startup failure.
        """
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None or state.get("startup_cleanup_safe") is not True:
            return
        try:
            owned = self._discover_owned_cleanup_resources(run_root, labels)
        except Exception:
            # Failure cleanup must never fall back to mutable state names. If
            # ownership cannot be proven from the engine, leak the resource
            # for scoped GC rather than risk deleting another checkout's data.
            return
        commands = (
            (
                "container",
                [self._container.engine, "rm", "-f"],
                "startup-failure-container-cleanup",
            ),
            (
                "volume",
                [self._container.engine, "volume", "rm"],
                "startup-failure-volume-cleanup",
            ),
            (
                "network",
                [self._container.engine, "network", "rm"],
                "startup-failure-network-cleanup",
            ),
        )
        for kind, prefix, log_prefix in commands:
            preserved_raw = state.get("startup_preserved_resources", {})
            preserved = (
                {
                    str(item)
                    for item in preserved_raw.get(kind, [])
                    if str(item)
                }
                if isinstance(preserved_raw, dict)
                and isinstance(preserved_raw.get(kind, []), list)
                else set()
            )
            for reference in sorted(owned[kind] - preserved):
                try:
                    self._require_resource_owned(
                        run_root,
                        kind,
                        reference,
                        labels,
                        allow_additional_labels=True,
                    )
                    result = self._runner.run(
                        [*prefix, reference],
                        cwd=run_root,
                    )
                except Exception:
                    continue
                self._write_image_log(
                    run_root / "logs",
                    f"{log_prefix}-{_safe_artifact_name(reference)}.log",
                    result,
                )

    def _canonical_state_resource_labels(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> dict[str, str] | None:
        labels = state.get("resource_labels")
        if not isinstance(labels, dict):
            return None
        normalized = {str(key): str(value) for key, value in labels.items()}
        run_id = normalized.get("spec.run_id", "")
        spec_id = normalized.get("spec.spec_id", "")
        try:
            validate_workspace_run_identity(run_id, spec_id)
        except ValueError:
            return None
        expected = self._resource_labels(
            run_id=run_id,
            spec_id=spec_id,
            workspace_root=run_root / "source",
        )
        return expected if normalized == expected else None

    def _remove_exact_owned_runtime_containers(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> None:
        """Remove and verify all run-owned containers without diagnostic I/O.

        This is the fail-closed liveness primitive used before host access.
        Worker-visible log directories are attacker-controlled and may be
        unwritable; no logging operation is allowed to precede or interrupt
        resource removal and positive engine verification.
        """
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses runtime quiescence with non-canonical labels."
            )
        try:
            owned = self._discover_owned_cleanup_resources(run_root, labels)
        except OSError as exc:
            raise RuntimeError(
                "Container backend could not discover its exact-owned runtime resources."
            ) from exc
        for container_id in sorted(owned["container"]):
            self._require_resource_owned(
                run_root,
                "container",
                container_id,
                labels,
                allow_additional_labels=True,
            )
            removal = self._runner.run(
                [self._container.engine, "rm", "-f", container_id],
                cwd=run_root,
            )
            if removal.returncode != 0:
                detail = (
                    removal.stderr or removal.stdout or "unknown engine error"
                ).strip()
                raise RuntimeError(
                    "Container backend could not remove an exact-owned runtime "
                    f"container {container_id}: {detail}"
                )
            inspection = self._runner.run(
                [
                    self._container.engine,
                    "inspect",
                    "--type",
                    "container",
                    container_id,
                ],
                cwd=run_root,
            )
            missing_detail = f"{inspection.stdout}\n{inspection.stderr}".lower()
            if inspection.returncode == 0 or "no such" not in missing_detail:
                raise RuntimeError(
                    "Container backend could not positively verify removal of "
                    f"exact-owned container {container_id}."
                )

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
        ``state['containers']`` are excluded defensively. A teardown failure is
        loudly logged and causes workspace preparation to abort before any
        replacement worker or workspace-volume mutation; the label filter
        guarantees containers from other runs — and unlabeled containers — are
        never touched.

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
        spec_id = str(labels.get("spec.spec_id") or "")
        workspace_root = str(labels.get("spec.workspace_root") or "").strip()
        try:
            validate_workspace_run_identity(run_id, spec_id)
        except ValueError:
            return False
        expected_workspace = (run_root / "source").resolve()
        recorded_workspace = Path(workspace_root).expanduser()
        if (
            not workspace_root
            or not recorded_workspace.is_absolute()
            or _lexical_absolute(recorded_workspace) != recorded_workspace
            or recorded_workspace != expected_workspace
            or recorded_workspace.resolve(strict=False) != expected_workspace
        ):
            # A run id is not globally unique across checkouts. Without a
            # canonical exact workspace root we cannot safely identify prior
            # attempts. Leave every container and pid file untouched.
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
                "--filter",
                f"label=spec.spec_id={spec_id}",
                "--filter",
                f"label=spec.workspace_root={workspace_root}",
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
        # Docker and Podman both report a missing container as "no such ...".
        # A generic "not found" can instead describe a missing daemon context,
        # plugin, or endpoint and is not positive proof that the container is
        # absent.
        return "no such" in text

    def _write_pid_cleanup_skipped_log(self, run_root: Path, reason: str) -> None:
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            logs / "previous-attempt-postmaster-cleanup-skipped.log",
            "\n".join(
                [
                    f"logged_at: {datetime.now(timezone.utc).isoformat()}",
                    reason,
                ]
            ),
            encoding="utf-8",
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
        # Logs are worker-visible. Replacing the leaf is safe even if the
        # worker planted a symlink there; appending/opening would follow it.
        atomic_write_text(path, entry, encoding="utf-8")

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
        try:
            validated_dirs = self._validated_cleanup_service_data_dirs(
                run_root / "source",
                state.get("service_data_dirs", []),
                topology=str(state.get("service_topology") or ""),
            )
        except OSError as exc:
            self._write_pid_cleanup_skipped_log(
                run_root,
                f"unsafe service data path; leaving postmaster.pid untouched: {exc}",
            )
            return
        data_dirs = [str(item) for item in validated_dirs]
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
            atomic_write_text(
                logs / "previous-attempt-postmaster-cleanup.log",
                "\n".join(
                    [
                        f"completed_at: {datetime.now(timezone.utc).isoformat()}",
                        "removed stale postmaster.pid (no postmaster process owns it "
                        "after teardown):",
                        *removed,
                    ]
                ),
                encoding="utf-8",
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
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses stale-pid cleanup with "
                "non-canonical resource labels."
            )
        self._require_volume_safe_to_use(run_root, volume, labels)
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
        # Validate host path ownership before reading attacker-influenced state
        # or tearing down any external container resources. Clone cleanup
        # repeats this check immediately before deleting the run directory.
        run_root, source, _outbox = self._validated_cleanup_layout(workspace)
        try:
            state = self._read_container_state(run_root, missing_ok=True)
        except RuntimeError as exc:
            raise OSError(str(exc)) from exc
        expected_labels = self._validated_container_cleanup_state(
            workspace,
            run_root=run_root,
            source=source,
            state=state,
        )
        service_data_dirs = self._validated_cleanup_service_data_dirs(
            source,
            state.get("service_data_dirs", []),
            topology=str(state.get("service_topology") or ""),
        )
        if state.get("service_topology") == "sidecar":
            compose_project = str(state.get("compose_project") or "").strip()
            if not compose_project:
                raise OSError(
                    "refusing container cleanup with missing compose project ownership"
                )
            try:
                self._assert_compose_project_owned_or_unused(
                    run_root,
                    compose_project,
                    expected_labels,
                )
            except RuntimeError as exc:
                raise OSError(str(exc)) from exc
        owned_resources = self._discover_owned_cleanup_resources(
            run_root,
            expected_labels,
        )
        recorded_workspace_volumes = set(state["workspace_volumes"])
        if (
            state["workspace_mode"] == "volume"
            and not recorded_workspace_volumes.issubset(owned_resources["volume"])
        ):
            raise OSError(
                "refusing container cleanup because the recorded workspace "
                "volume is not present on the selected engine"
            )
        for volume in sorted(owned_resources["volume"]):
            try:
                self._require_volume_safe_to_use(
                    run_root,
                    volume,
                    expected_labels,
                )
            except RuntimeError as exc:
                raise OSError(str(exc)) from exc
        # Stop every runtime writer before importing volume data or invoking
        # host Git. A guarded cleanup may leave volumes/state for recovery, but
        # it must never let a container race host-side validation.
        for container_id in sorted(owned_resources["container"]):
            try:
                self._require_resource_owned(
                    run_root,
                    "container",
                    container_id,
                    expected_labels,
                    allow_additional_labels=True,
                )
            except RuntimeError as exc:
                raise OSError(str(exc)) from exc
            result = self._runner.run(
                [self._container.engine, "rm", "-f", container_id],
                cwd=run_root,
            )
            self._require_owned_cleanup_success("container", container_id, result)
            if not self._verify_container_removed(run_root, container_id):
                raise OSError(
                    "refusing container cleanup because removal could not be "
                    f"positively verified: {container_id}"
                )
        # For volume-mode workspaces the authoritative git state lives inside the
        # Docker volume, not the host ``source`` mirror. The post-run sync
        # (``_sync_volume_workspace_to_host``) normally keeps them in step, but a
        # crash mid-session — or a cleanup/resume that fires before that sync —
        # can leave commits or edits only in the volume while the host mirror
        # reads clean. Re-sync the volume back to the host before the guard so
        # the deletability check reasons about the same content ``volume rm``
        # would destroy. Skipped when deletion is already authorized
        # (an explicit operator ``spec clean``), where data loss is accepted.
        if not allow_unpushed_work and state.get("workspace_mode") == "volume":
            owned_workspace_volumes = (
                recorded_workspace_volumes & owned_resources["volume"]
            )
            if owned_workspace_volumes:
                safe_state = dict(state)
                safe_state["workspace_volumes"] = sorted(owned_workspace_volumes)
                safe_state["volumes"] = sorted(owned_resources["volume"])
                self._sync_volume_workspace_to_host(run_root, safe_state)
        if not allow_unpushed_work:
            # Guarded cleanup invokes host Git below, so reinstall and validate
            # the protected config first. Explicit operator discard mode does
            # not invoke host Git and must remain able to remove exact-owned
            # legacy workspaces created before the protected baseline existed.
            self._restore_container_safe_git_config(
                run_root=run_root,
                source=source,
            )
        # Refuse before tearing down workspace data or remaining Docker
        # resources so a guarded deletion leaves data and metadata recoverable.
        # Owned containers are already stopped above to close the host-Git race.
        self._assert_workspace_deletable(
            source,
            allow_unpushed_work=allow_unpushed_work,
        )
        # Re-resolve the destructive host paths only after every runtime writer
        # is positively gone. A worker could have replaced an ancestor with a
        # symlink between the initial state validation and container removal.
        service_data_dirs = self._validated_cleanup_service_data_dirs(
            source,
            state.get("service_data_dirs", []),
            topology=str(state.get("service_topology") or ""),
        )

        # Never trust raw ids/names in persisted state. Discover resources from
        # the engine using the complete host-generated label set and remove
        # only those matches. This also collects resources from older attempts
        # that a single latest-id state record may not mention.
        # Only remove host service data after every owned container is stopped;
        # otherwise a still-running database could race the deletion or keep
        # writing through its bind mount.
        for path in service_data_dirs:
            if path.exists():
                remove_tree(path)
        for volume in sorted(owned_resources["volume"]):
            try:
                self._require_volume_safe_to_use(
                    run_root,
                    volume,
                    expected_labels,
                )
            except RuntimeError as exc:
                raise OSError(str(exc)) from exc
            result = self._runner.run(
                [self._container.engine, "volume", "rm", volume],
                cwd=run_root,
            )
            self._require_owned_cleanup_success("volume", volume, result)
        for network in sorted(owned_resources["network"]):
            try:
                self._require_network_safe_to_use(
                    run_root,
                    network,
                    expected_labels,
                )
            except RuntimeError as exc:
                raise OSError(str(exc)) from exc
            result = self._runner.run(
                [self._container.engine, "network", "rm", network],
                cwd=run_root,
            )
            self._require_owned_cleanup_success("network", network, result)
        super().cleanup(workspace, allow_unpushed_work=allow_unpushed_work)

    @staticmethod
    def _require_owned_cleanup_success(
        kind: str,
        resource_id: str,
        result: subprocess.CompletedProcess[str],
    ) -> None:
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise OSError(
            f"owned {kind} cleanup failed for {resource_id}: {detail}"
        )

    def _validated_container_cleanup_state(
        self,
        workspace: WorkspaceHandle,
        *,
        run_root: Path,
        source: Path,
        state: dict[str, Any],
    ) -> dict[str, str]:
        if state.get("backend") != "container":
            raise OSError(
                "refusing container cleanup with an invalid backend identity"
            )
        saved_engine = state.get("engine")
        if not isinstance(saved_engine, str) or not saved_engine:
            raise OSError(
                "refusing container cleanup with an invalid saved engine identity"
            )
        if saved_engine != self._container.engine:
            raise OSError(
                "refusing container cleanup through a different engine; restore "
                "the run's original engine configuration before cleaning"
            )
        workspace_mode = state.get("workspace_mode")
        if not isinstance(workspace_mode, str) or workspace_mode not in {
            "bind",
            "volume",
        }:
            raise OSError(
                "refusing container cleanup with an invalid workspace mode"
            )
        service_topology = state.get("service_topology")
        if not isinstance(service_topology, str) or service_topology not in {
            "in-worker",
            "sidecar",
        }:
            raise OSError(
                "refusing container cleanup with an invalid service topology"
            )
        workspace_volumes = state.get("workspace_volumes")
        if workspace_mode == "volume":
            if (
                not isinstance(workspace_volumes, list)
                or len(workspace_volumes) != 1
                or not isinstance(workspace_volumes[0], str)
                or not workspace_volumes[0].strip()
            ):
                raise OSError(
                    "refusing container cleanup with invalid workspace_volumes "
                    "for volume mode"
                )
        elif workspace_volumes != []:
            raise OSError(
                "refusing container cleanup with invalid workspace_volumes "
                "for bind mode"
            )
        service_processes = state.get("service_processes")
        if not isinstance(service_processes, list):
            raise OSError(
                "refusing container cleanup with malformed service_processes"
            )
        run_id = str(workspace.metadata.get("run_id") or run_root.name)
        spec_id = str(workspace.metadata.get("spec_id") or "")
        if not spec_id:
            branch_identity = implementation_branch_identity(workspace.branch)
            if branch_identity is not None:
                spec_id = branch_identity.spec_id
            elif workspace.branch.startswith("task/"):
                legacy_task_spec_id = f"task-{workspace.branch.removeprefix('task/')}"
                if SPEC_ID_RE.fullmatch(legacy_task_spec_id) and run_id.startswith(
                    f"{legacy_task_spec_id}-"
                ):
                    spec_id = legacy_task_spec_id
        try:
            validate_workspace_run_identity(run_id, spec_id)
        except ValueError as exc:
            raise OSError(
                f"refusing container cleanup for invalid run ownership: {exc}"
            ) from exc
        expected = self._resource_labels(
            run_id=run_id,
            spec_id=spec_id,
            workspace_root=source,
        )
        labels = state.get("resource_labels")
        if labels != expected:
            raise OSError(
                "refusing container cleanup because persisted resource labels "
                "do not exactly match the canonical run identity"
            )
        for key, expected_path in (
            ("source_path", source),
            ("outbox_path", run_root / "outbox"),
        ):
            recorded = str(state.get(key) or "").strip()
            if recorded and _lexical_absolute(Path(recorded)) != expected_path:
                raise OSError(
                    f"refusing container cleanup with mismatched {key}: {recorded}"
                )
        if any(
            isinstance(process, dict) and process.get("pid")
            for process in service_processes
        ):
            # Current container service records use container_id, never a host
            # PID. A raw PID in mutable state has no authenticated supervision
            # token and must not reach `docker kill` or host signalling.
            raise OSError(
                "refusing container cleanup for unauthenticated service process PID"
            )
        return expected

    def _validated_cleanup_service_data_dirs(
        self,
        source: Path,
        raw_paths: object,
        *,
        topology: str,
    ) -> list[Path]:
        if not isinstance(raw_paths, list):
            raise OSError("refusing container cleanup with malformed service_data_dirs")
        if topology not in {"", "in-worker", "sidecar"}:
            raise OSError(
                f"refusing container cleanup with invalid service topology: {topology!r}"
            )
        validated: list[Path] = []
        for item in raw_paths:
            raw_path = str(item)
            if ".." in PurePosixPath(raw_path).parts or ".." in PureWindowsPath(
                raw_path
            ).parts:
                raise OSError(
                    f"refusing traversing container service data directory: {raw_path}"
                )
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise OSError(
                    f"refusing relative container service data directory: {path}"
                )
            lexical = _lexical_absolute(path)
            try:
                relative = lexical.relative_to(source)
            except ValueError as exc:
                raise OSError(
                    f"refusing container service data directory outside source: {path}"
                ) from exc
            if not relative.parts:
                raise OSError(
                    f"refusing to treat the entire workspace source as service data: {path}"
                )
            linked = _first_link_or_junction(lexical, floor=source)
            if linked is not None:
                raise OSError(
                    f"refusing linked container service data directory: {path}"
                )
            try:
                lexical.resolve(strict=False).relative_to(source.resolve())
            except ValueError as exc:
                raise OSError(
                    f"refusing container service data directory escape: {path}"
                ) from exc
            validated.append(lexical)
        expected = (
            [_lexical_absolute(Path(item)) for item in self._service_data_dirs(source, topology)]
            if topology
            else []
        )
        if validated != expected:
            raise OSError(
                "refusing container cleanup because service_data_dirs do not "
                "exactly match the canonical service layout"
            )
        return validated

    def _discover_owned_cleanup_resources(
        self,
        run_root: Path,
        labels: dict[str, str],
    ) -> dict[str, set[str]]:
        filters = [
            argument
            for key, value in sorted(labels.items())
            for argument in ("--filter", f"label={key}={value}")
        ]
        commands = {
            "container": [
                self._container.engine,
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                *filters,
            ],
            "volume": [
                self._container.engine,
                "volume",
                "ls",
                "-q",
                *filters,
            ],
            "network": [
                self._container.engine,
                "network",
                "ls",
                "-q",
                *filters,
            ],
        }
        discovered: dict[str, set[str]] = {}
        for kind, argv in commands.items():
            result = self._runner.run(argv, cwd=run_root)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                raise OSError(
                    f"refusing container cleanup because owned {kind} discovery failed: {detail}"
                )
            discovered[kind] = {
                line.strip() for line in result.stdout.splitlines() if line.strip()
            }
        return discovered

    def _discover_owned_container_ids(
        self,
        run_root: Path,
        labels: dict[str, str],
    ) -> set[str]:
        """Discover only exact-owned containers for runtime health checks."""
        filters = [
            argument
            for key, value in sorted(labels.items())
            for argument in ("--filter", f"label={key}={value}")
        ]
        result = self._runner.run(
            [
                self._container.engine,
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                *filters,
            ],
            cwd=run_root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise OSError(
                "container runtime health discovery failed: " + detail
            )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _inspect_resource_labels(
        self,
        run_root: Path,
        kind: str,
        reference: str,
    ) -> dict[str, str]:
        if kind == "container":
            argv = [
                self._container.engine,
                "inspect",
                "--type",
                "container",
                reference,
            ]
        elif kind == "volume":
            argv = [self._container.engine, "volume", "inspect", reference]
        elif kind == "network":
            argv = [self._container.engine, "network", "inspect", reference]
        else:
            raise RuntimeError(f"Unknown container resource kind: {kind}")
        result = self._runner.run(argv, cwd=run_root)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(
                f"Container backend could not inspect {kind} {reference}: {detail}"
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Container backend received malformed inspection data for {kind} {reference}."
            ) from exc
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
        ):
            raise RuntimeError(
                f"Container backend received unexpected inspection data for {kind} {reference}."
            )
        item = payload[0]
        if kind == "container":
            config = item.get("Config") or item.get("config")
            raw_labels = (
                config.get("Labels") or config.get("labels")
                if isinstance(config, dict)
                else None
            )
        else:
            raw_labels = item.get("Labels") or item.get("labels")
        if not isinstance(raw_labels, dict):
            return {}
        return {str(key): str(value) for key, value in raw_labels.items()}

    def _require_resource_owned(
        self,
        run_root: Path,
        kind: str,
        reference: str,
        expected_labels: dict[str, str],
        *,
        allow_additional_labels: bool = False,
    ) -> None:
        actual_labels = self._inspect_resource_labels(run_root, kind, reference)
        matches = (
            all(actual_labels.get(key) == value for key, value in expected_labels.items())
            if allow_additional_labels
            else actual_labels == expected_labels
        )
        if not matches:
            raise RuntimeError(
                "Container backend refuses to use a pre-existing or replaced "
                f"{kind} not owned by this checkout and run: {reference}"
            )

    def _require_volume_safe_to_use(
        self,
        run_root: Path,
        volume: str,
        expected_labels: dict[str, str],
    ) -> None:
        """Require both volume ownership and exclusively owned consumers.

        Older releases named volumes from ``run_id`` alone. Two checkouts with
        the same run id could therefore share one volume while its immutable
        labels described only the checkout that created it. Inspect every
        attached container before mounting the volume so labels cannot provide
        a false proof of exclusive ownership.
        """
        self._require_resource_owned(
            run_root,
            "volume",
            volume,
            expected_labels,
            allow_additional_labels=True,
        )
        result = self._runner.run(
            [
                self._container.engine,
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                "--filter",
                f"volume={volume}",
            ],
            cwd=run_root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(
                "Container backend could not verify consumers of volume "
                f"{volume}: {detail}"
            )
        for container_id in (line.strip() for line in result.stdout.splitlines()):
            if container_id:
                self._require_resource_owned(
                    run_root,
                    "container",
                    container_id,
                    expected_labels,
                    allow_additional_labels=True,
                )

    def _require_network_safe_to_use(
        self,
        run_root: Path,
        network: str,
        expected_labels: dict[str, str],
        *,
        endpoint_labels: dict[str, str] | None = None,
    ) -> None:
        """Require network ownership and exclusively owned endpoints."""
        self._require_resource_owned(
            run_root,
            "network",
            network,
            expected_labels,
            allow_additional_labels=True,
        )
        result = self._runner.run(
            [self._container.engine, "network", "inspect", network],
            cwd=run_root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(
                f"Container backend could not verify endpoints of network {network}: {detail}"
            )
        try:
            payload = json.loads(result.stdout)
            item = payload[0]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "Container backend received malformed endpoint data for "
                f"network {network}."
            ) from exc
        if not isinstance(item, dict):
            raise RuntimeError(
                "Container backend received malformed endpoint data for "
                f"network {network}."
            )
        raw_endpoints = item.get("Containers", item.get("containers", {}))
        if raw_endpoints is None:
            raw_endpoints = {}
        if isinstance(raw_endpoints, dict):
            container_ids = [str(value) for value in raw_endpoints]
        elif isinstance(raw_endpoints, list):
            container_ids = []
            for endpoint in raw_endpoints:
                if not isinstance(endpoint, dict):
                    raise RuntimeError(
                        "Container backend received malformed endpoint data for "
                        f"network {network}."
                    )
                container_id = endpoint.get("Id", endpoint.get("id", ""))
                if container_id:
                    container_ids.append(str(container_id))
        else:
            raise RuntimeError(
                "Container backend received malformed endpoint data for "
                f"network {network}."
            )
        for container_id in container_ids:
            self._require_resource_owned(
                run_root,
                "container",
                container_id,
                endpoint_labels or expected_labels,
                allow_additional_labels=True,
            )

    def _require_workspace_volume_safe(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> None:
        if state.get("workspace_mode") != "volume":
            return
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if not volumes:
            raise RuntimeError(
                "Container backend refuses volume-mode execution without a "
                "workspace volume."
            )
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses volume-mode execution with "
                "non-canonical resource labels."
            )
        self._require_volume_safe_to_use(run_root, str(volumes[0]), labels)

    def _assert_compose_project_owned_or_unused(
        self,
        run_root: Path,
        compose_project: str,
        expected_labels: dict[str, str],
    ) -> None:
        project_filter = f"label=com.docker.compose.project={compose_project}"
        commands = {
            "container": [
                self._container.engine,
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                "--filter",
                project_filter,
            ],
            "volume": [
                self._container.engine,
                "volume",
                "ls",
                "-q",
                "--filter",
                project_filter,
            ],
            "network": [
                self._container.engine,
                "network",
                "ls",
                "-q",
                "--filter",
                project_filter,
            ],
        }
        for kind, argv in commands.items():
            result = self._runner.run(argv, cwd=run_root)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                raise RuntimeError(
                    "Container backend could not safely preflight compose project "
                    f"{compose_project}: {kind} discovery failed: {detail}"
                )
            for reference in (line.strip() for line in result.stdout.splitlines()):
                if reference:
                    if kind == "volume":
                        self._require_volume_safe_to_use(
                            run_root,
                            reference,
                            expected_labels,
                        )
                    elif kind == "network":
                        self._require_network_safe_to_use(
                            run_root,
                            reference,
                            expected_labels,
                        )
                    else:
                        self._require_resource_owned(
                            run_root,
                            kind,
                            reference,
                            expected_labels,
                            allow_additional_labels=True,
                        )

    def _assert_compose_managed_names_owned_or_unused(
        self,
        run_root: Path,
        compose_file: Path,
        compose_project: str,
        expected_labels: dict[str, str],
    ) -> None:
        """Reject deterministic Compose-name collisions before ``compose up``.

        Compose will attach a newly started service to a pre-existing volume
        named ``<project>_<key>`` even when that volume is unlabeled and foreign.
        Project-label inventory cannot see that collision, so enumerate names
        and inspect every managed deterministic target directly first.
        """
        try:
            compose = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(
                "Container backend could not parse its protected Compose baseline."
            ) from exc
        if not isinstance(compose, dict):
            raise RuntimeError("Container backend protected Compose baseline is invalid.")

        managed: dict[str, set[str]] = {"volume": set(), "network": set()}
        for kind, section_name in (("volume", "volumes"), ("network", "networks")):
            section = compose.get(section_name, {})
            if not isinstance(section, dict):
                raise RuntimeError(
                    f"Container backend Compose {section_name} must be a mapping."
                )
            for raw_name, value in section.items():
                name = str(raw_name)
                external = isinstance(value, dict) and value.get("external") is True
                if not external:
                    configured_name = (
                        str(value.get("name") or "").strip()
                        if isinstance(value, dict)
                        else ""
                    )
                    managed[kind].add(
                        configured_name or f"{compose_project}_{name}"
                    )
        default_network = compose.get("networks", {}).get("default")
        default_external = (
            isinstance(default_network, dict)
            and default_network.get("external") is True
        )
        if not default_external:
            default_name = (
                str(default_network.get("name") or "").strip()
                if isinstance(default_network, dict)
                else ""
            )
            managed["network"].add(
                default_name or f"{compose_project}_default"
            )

        inventories: dict[str, set[str]] = {}
        for kind in ("volume", "network"):
            result = self._runner.run(
                [self._container.engine, kind, "ls", "-q"],
                cwd=run_root,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                raise RuntimeError(
                    "Container backend could not safely inventory deterministic "
                    f"Compose {kind} names: {detail}"
                )
            inventories[kind] = {
                line.strip() for line in result.stdout.splitlines() if line.strip()
            }

        compose_labels = expected_labels | {
            "com.docker.compose.project": compose_project,
        }
        for volume in sorted(managed["volume"] & inventories["volume"]):
            self._require_volume_safe_to_use(run_root, volume, compose_labels)
        for network in sorted(managed["network"] & inventories["network"]):
            self._require_network_safe_to_use(
                run_root,
                network,
                compose_labels,
                endpoint_labels=expected_labels,
            )

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
            if "SPEC_BUTLER_VERSION" not in dockerfile.read_text(
                encoding="utf-8", errors="replace"
            ):
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
        source_dockerfile = dockerfile.read_text(encoding="utf-8")
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
        (context / "Dockerfile").write_text("\n".join(wrapper) + "\n", encoding="utf-8")
        (context / "README.txt").write_text(
            "Generated by spec container backend. Dependency manifests are copied "
            "with their repo-relative paths before the optional bootstrap cache "
            "layer, and full source is copied after that layer so dependency "
            "installs can be cached across ordinary source edits. Runtime source "
            "is mounted at /workspace/source so it does not hide the cached "
            "bootstrap layer under /workspace/bootstrap.\n",
            encoding="utf-8",
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

    @staticmethod
    def _resource_scope_digest(
        run_id: str,
        workspace_root: Path,
        *,
        purpose: str = "runtime",
    ) -> str:
        """Bind deterministic engine names to one physical checkout.

        Run IDs are only repository-local. Including the canonical workspace
        path prevents two clones that resume the same persisted run ID from
        racing into one Docker/Compose namespace.
        """
        payload = f"{run_id}\0{workspace_root.resolve()}\0{purpose}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _legacy_resource_digest(run_id: str, *, purpose: str = "runtime") -> str:
        payload = run_id if purpose == "runtime" else f"{run_id}:{purpose}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _adopt_previous_resource_names(
        self,
        *,
        run_id: str,
        run_root: Path,
        workspace_root: Path,
        mode: str,
        service_topology: str,
        previous_state: dict[str, Any],
        preexisting_resources: dict[str, set[str]],
        expected_labels: dict[str, str],
        workspace_volumes: list[str],
        compose_project: str,
        service_volumes: list[str],
        service_networks: list[str],
        playwright_mcp: dict[str, Any],
    ) -> tuple[
        list[str],
        str,
        list[str],
        list[str],
        dict[str, Any],
        dict[str, Any],
    ]:
        """Retain exact-owned names from protected development-era state.

        Released pre-0.5 runs fail earlier because they lack the seed marker or
        protected Compose baseline. New runs use checkout-scoped names; a retry
        may retain an earlier development name only when canonical prior state
        names that exact formula and the engine proves the resource belongs to
        this checkout. Anything persistent that cannot be accounted for fails
        closed instead of being silently orphaned behind a new namespace.
        """
        owned_volumes = set(preexisting_resources.get("volume", set()))
        owned_networks = set(preexisting_resources.get("network", set()))
        accounted_volumes: set[str] = set()
        accounted_networks: set[str] = set()

        scoped_digest = self._resource_scope_digest(run_id, workspace_root)
        legacy_digest = self._legacy_resource_digest(run_id)
        allowed_workspace_volumes = (
            {
                f"spec-{legacy_digest}-source",
                f"spec-{scoped_digest}-source",
            }
            if mode == "volume"
            else set()
        )
        existing_workspace_volumes = owned_volumes & allowed_workspace_volumes
        if existing_workspace_volumes:
            previous_workspace_raw = previous_state.get(
                "workspace_volumes",
                previous_state.get("volumes", []),
            )
            previous_workspace = (
                [str(item) for item in previous_workspace_raw if str(item)]
                if isinstance(previous_workspace_raw, list)
                else []
            )
            if (
                len(existing_workspace_volumes) != 1
                or previous_workspace[:1] != sorted(existing_workspace_volumes)
            ):
                raise RuntimeError(
                    "Container backend found an ambiguous existing workspace "
                    "volume generation; refusing to abandon or overwrite it."
                )
            selected_workspace = next(iter(existing_workspace_volumes))
            self._require_volume_safe_to_use(
                run_root,
                selected_workspace,
                expected_labels,
            )
            workspace_volumes = [selected_workspace]
            accounted_volumes.add(selected_workspace)

        allowed_projects = (
            {f"spec-{legacy_digest}", f"spec-{scoped_digest}"}
            if service_topology == "sidecar"
            else set()
        )
        compose_resources: dict[str, dict[str, set[str]]] = {
            project: {"volume": set(), "network": set()}
            for project in allowed_projects
        }
        for kind, references in (
            ("volume", owned_volumes),
            ("network", owned_networks),
        ):
            for reference in references:
                labels = self._inspect_resource_labels(run_root, kind, reference)
                project = labels.get("com.docker.compose.project", "")
                if project in compose_resources:
                    compose_resources[project][kind].add(reference)
        existing_projects = {
            project
            for project, resources in compose_resources.items()
            if resources["volume"] or resources["network"]
        }
        service_volume_snapshots: dict[str, Any] = {}
        if existing_projects:
            previous_project = str(previous_state.get("compose_project") or "")
            if len(existing_projects) != 1 or previous_project not in existing_projects:
                raise RuntimeError(
                    "Container backend found an ambiguous existing Compose "
                    "project generation; refusing to abandon its service data."
                )
            compose_project = previous_project
            selected = compose_resources[compose_project]
            for volume in sorted(selected["volume"]):
                self._require_volume_safe_to_use(
                    run_root,
                    volume,
                    expected_labels,
                )
            service_volumes = sorted(selected["volume"])
            service_networks = sorted(selected["network"])
            if not service_networks:
                service_networks = [f"{compose_project}_default"]
            accounted_volumes.update(selected["volume"])
            accounted_networks.update(selected["network"])
            snapshots = previous_state.get("service_volume_snapshots", {})
            if not isinstance(snapshots, dict):
                raise RuntimeError(
                    "Container backend previous service snapshot state is invalid."
                )
            service_volume_snapshots = dict(snapshots)

        legacy_playwright_digest = self._legacy_resource_digest(
            run_id,
            purpose="playwright-mcp",
        )
        scoped_playwright_digest = self._resource_scope_digest(
            run_id,
            workspace_root,
            purpose="playwright-mcp",
        )
        playwright_generations = {
            f"spec-{legacy_playwright_digest}-playwright-mcp": (
                f"spec-{legacy_playwright_digest}-playwright-mcp"
            ),
            f"spec-{scoped_playwright_digest}-playwright-mcp": (
                f"spec-{scoped_playwright_digest}-playwright-mcp"
            ),
        }
        existing_playwright_networks = owned_networks & set(playwright_generations)
        if existing_playwright_networks:
            previous_playwright = previous_state.get("playwright_mcp", {})
            previous_networks_raw = (
                previous_playwright.get("sidecar_networks", [])
                if isinstance(previous_playwright, dict)
                else []
            )
            previous_networks = (
                [str(item) for item in previous_networks_raw if str(item)]
                if isinstance(previous_networks_raw, list)
                else []
            )
            previous_container = (
                str(previous_playwright.get("sidecar_container") or "")
                if isinstance(previous_playwright, dict)
                else ""
            )
            if (
                playwright_mcp.get("topology") != "sidecar"
                or len(existing_playwright_networks) != 1
                or previous_networks != sorted(existing_playwright_networks)
                or previous_container
                != playwright_generations[next(iter(existing_playwright_networks))]
            ):
                raise RuntimeError(
                    "Container backend found an ambiguous existing Playwright "
                    "sidecar generation; refusing to reuse it."
                )
            selected_network = next(iter(existing_playwright_networks))
            self._require_network_safe_to_use(
                run_root,
                selected_network,
                expected_labels,
            )
            playwright_mcp = dict(playwright_mcp)
            playwright_mcp["sidecar_networks"] = [selected_network]
            playwright_mcp["sidecar_container"] = previous_container
            server = dict(playwright_mcp.get("sidecar_mcp_server", {}))
            if server:
                port = int(
                    playwright_mcp.get("sidecar_mcp_port")
                    or CONTAINER_PLAYWRIGHT_MCP_SIDECAR_PORT
                )
                server["url"] = f"http://{previous_container}:{port}/sse"
                playwright_mcp["sidecar_mcp_server"] = server
            accounted_networks.add(selected_network)

        unaccounted_volumes = owned_volumes - accounted_volumes
        unaccounted_networks = owned_networks - accounted_networks
        if unaccounted_volumes or unaccounted_networks:
            preview = ", ".join(
                sorted(unaccounted_volumes | unaccounted_networks)[:5]
            )
            raise RuntimeError(
                "Container backend found persistent exact-owned resources that "
                "do not match a trusted resource generation; refusing to leave "
                f"their data behind ({preview})."
            )
        return (
            workspace_volumes,
            compose_project,
            service_volumes,
            service_networks,
            playwright_mcp,
            service_volume_snapshots,
        )

    def _volume_names(
        self,
        run_id: str,
        mode: str,
        workspace_root: Path,
    ) -> list[str]:
        if mode != "volume":
            return []
        digest = self._resource_scope_digest(run_id, workspace_root)
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
        # Worker values are intentionally absent from Docker argv, but the
        # command may echo them. Treat every forwarded non-empty value as
        # sensitive rather than trying to infer secrecy from its variable name.
        return list(dict.fromkeys(value for value in env.values() if value))

    @staticmethod
    def _container_client_env(
        worker_env: dict[str, str],
        *,
        inherit_env: bool = True,
    ) -> dict[str, str]:
        # Docker's ``-e NAME`` form reads NAME from the client environment.
        # Overlay every value, including empty strings, so a same-named
        # operator variable can never replace the value admitted by the
        # orchestrator. ``inherit_env=False`` must not silently recover the
        # ambient process environment merely because worker values exist.
        client_env = os.environ.copy() if inherit_env else {}
        client_env.update(worker_env)
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

    def _service_network_names(
        self,
        run_id: str,
        topology: str,
        workspace_root: Path,
    ) -> list[str]:
        if topology != "sidecar":
            return []
        digest = self._resource_scope_digest(run_id, workspace_root)
        return [f"spec-{digest}_default"]

    def _playwright_sidecar_names(
        self,
        run_id: str,
        workspace_root: Path,
    ) -> tuple[str, list[str]]:
        digest = self._resource_scope_digest(
            run_id,
            workspace_root,
            purpose="playwright-mcp",
        )
        name = f"spec-{digest}-playwright-mcp"
        return name, [name]

    def _playwright_mcp_state(
        self,
        *,
        run_id: str,
        source: Path,
        logs: Path,
        image: str,
        service_topology: str,
        resource_labels: dict[str, str],
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
            self._detect_worker_playwright_version(
                image=image,
                cwd=source,
                resource_labels=resource_labels,
            )
            if expected
            else ""
        )
        if expected and actual and expected != actual:
            failure_path = logs / "playwright-mcp-version-mismatch.json"
            remediation = (
                "Install browser dependencies through the repo setup path or use "
                "a worker image whose Playwright browsers match the configured "
                f"Playwright package version {expected}."
            )
            atomic_write_text(
                failure_path,
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
                ),
                encoding="utf-8",
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
            sidecar_container, sidecar_networks = self._playwright_sidecar_names(
                run_id,
                source,
            )
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
                payload = json.loads(package_path.read_text(encoding="utf-8"))
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

    def _detect_worker_playwright_version(
        self,
        *,
        image: str,
        cwd: Path,
        resource_labels: dict[str, str],
    ) -> str:
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
            [
                self._container.engine,
                "run",
                "--rm",
                *self._label_argv({"resource_labels": resource_labels}),
                image,
                "sh",
                "-lc",
                script,
            ],
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
        atomic_write_text(
            logs / "playwright-mcp-diagnostics.json",
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
            ),
            encoding="utf-8",
        )

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
            try:
                self._require_network_safe_to_use(
                    run_root,
                    network,
                    state["resource_labels"],
                )
            except RuntimeError as exc:
                if result.returncode != 0:
                    raise RuntimeError(
                        "Container backend could not create or safely reuse "
                        f"Playwright MCP sidecar network {network}. See "
                        f"{logs / 'playwright-mcp-network-create.log'}"
                    ) from exc
                raise
        argv = [
            self._container.engine,
            "run",
            "-d",
            "--name",
            container,
            *self._label_argv(state),
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
        workspace_volumes = state.get("workspace_volumes") or state.get(
            "volumes", []
        )
        if state.get("workspace_mode") == "volume" and workspace_volumes:
            volume = str(workspace_volumes[0])
            labels = self._canonical_state_resource_labels(run_root, state)
            if labels is None:
                raise RuntimeError(
                    "Container backend refuses Playwright sidecar start with "
                    "non-canonical resource labels."
                )
            self._require_volume_safe_to_use(run_root, volume, labels)
            argv.extend(["-v", f"{volume}:/workspace/source"])
        else:
            argv.extend(["-v", f"{run_root / 'source'}:/workspace/source"])
        safe_git_config = self._container_safe_git_config_path(run_root)
        if not safe_git_config.is_file() or path_is_link_or_junction(safe_git_config):
            raise RuntimeError(
                "Container backend safe Git configuration is unavailable."
            )
        argv.extend(
            [
                "-v",
                f"{safe_git_config}:{CONTAINER_RUNTIME_SOURCE}/.git/config:ro",
            ]
        )
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

    def _compose_project_name(self, run_id: str, workspace_root: Path) -> str:
        digest = self._resource_scope_digest(run_id, workspace_root)
        return f"spec-{digest}"

    def _resolve_compose_file(self, repo_root: Path) -> Path:
        compose_file = Path(self._container.compose_file).expanduser()
        if not compose_file.is_absolute():
            compose_file = repo_root / compose_file
        return compose_file.resolve()

    def _pin_or_validate_operator_compose(
        self,
        *,
        repo_root: Path,
        run_root: Path,
        source_created: bool,
    ) -> Path:
        """Freeze Compose authority outside the agent-writable checkout.

        The first prepare copies the operator's file from the orchestration
        checkout. Retries must reuse that exact protected copy: consulting the
        implementation checkout again would let an agent turn a later retry
        into an arbitrary host-daemon Compose launch.
        """
        pinned = self._pinned_compose_file_path(run_root)
        if not source_created:
            return self._validated_pinned_compose_file(run_root)

        configured = self._resolve_compose_file(repo_root)
        if (
            not configured.is_file()
            or path_is_link_or_junction(configured)
            or configured.stat().st_size > 4 * 1024 * 1024
        ):
            raise RuntimeError(
                "Container backend requires a regular operator-controlled "
                f"Compose file no larger than 4 MiB: {configured}"
            )
        try:
            payload = configured.read_text(encoding="utf-8")
            parsed = yaml.safe_load(payload) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeError(
                "Container backend could not safely parse the Compose file "
                "before pinning: "
                f"{configured}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                "Container backend requires the Compose file root to be a mapping."
            )
        services = parsed.get("services", {})
        if not isinstance(services, dict):
            raise RuntimeError("Container backend Compose services must be a mapping.")
        declared_volumes = parsed.get("volumes", {})
        if not isinstance(declared_volumes, dict):
            raise RuntimeError("Container backend Compose volumes must be a mapping.")
        deferred_inputs: list[str] = []
        for service_name, service in services.items():
            if not isinstance(service, dict):
                raise RuntimeError(
                    f"Container backend Compose service {service_name!r} must be a mapping."
                )
            for key in ("env_file", "label_file"):
                if key in service:
                    deferred_inputs.append(f"services.{service_name}.{key}")
            for key in ("build", "develop", "provider", "volumes_from"):
                if key in service:
                    deferred_inputs.append(f"services.{service_name}.{key}")
            for index, mount in enumerate(service.get("volumes", []) or []):
                location = f"services.{service_name}.volumes[{index}]"
                if isinstance(mount, str):
                    source_name, separator, _target = mount.partition(":")
                    # A path-only mount (``/var/lib/data``) and an empty
                    # source (``:/var/lib/data``) both ask Compose to create
                    # an anonymous volume.  It has no stable declared name,
                    # cannot receive our top-level volume labels, and would
                    # therefore escape snapshot/restore and GC reconciliation.
                    if (
                        not separator
                        or not source_name
                        or source_name not in declared_volumes
                    ):
                        deferred_inputs.append(location)
                elif isinstance(mount, dict):
                    mount_type = str(mount.get("type") or "volume")
                    source_name = str(mount.get("source") or "")
                    if mount_type == "volume":
                        if not source_name or source_name not in declared_volumes:
                            deferred_inputs.append(location)
                    elif mount_type != "tmpfs":
                        deferred_inputs.append(location)
                else:
                    deferred_inputs.append(location)
        for section_name in ("configs", "secrets"):
            section = parsed.get(section_name, {})
            if not isinstance(section, dict):
                raise RuntimeError(
                    f"Container backend Compose {section_name} must be a mapping."
                )
            for name, value in section.items():
                if isinstance(value, dict) and any(
                    key in value for key in ("file", "environment", "content")
                ):
                    deferred_inputs.append(f"{section_name}.{name}")

        interpolation = re.compile(r"(?<!\$)\$(?:[A-Za-z_]|\{)")

        def contains_interpolation(value: object) -> bool:
            if isinstance(value, str):
                return interpolation.search(value) is not None
            if isinstance(value, list):
                return any(contains_interpolation(item) for item in value)
            if isinstance(value, dict):
                return any(
                    contains_interpolation(key) or contains_interpolation(item)
                    for key, item in value.items()
                )
            return False

        if contains_interpolation(parsed):
            deferred_inputs.append("environment interpolation")
        if deferred_inputs:
            raise RuntimeError(
                "Container backend Compose files must be self-contained before "
                "agent launch; inline deferred inputs: "
                + ", ".join(sorted(deferred_inputs))
            )
        atomic_write_text(pinned, payload)
        if os.name != "nt":
            pinned.chmod(0o600)
        project_directory = self._pinned_compose_project_directory(run_root)
        if path_is_link_or_junction(project_directory) or (
            project_directory.exists() and not project_directory.is_dir()
        ):
            raise RuntimeError(
                "Container backend Compose project directory is unsafe."
            )
        project_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._validated_pinned_compose_file(run_root)

    def _validated_pinned_compose_file(self, run_root: Path) -> Path:
        pinned = self._pinned_compose_file_path(run_root)
        if (
            not pinned.is_file()
            or path_is_link_or_junction(pinned)
            or pinned.stat().st_size > 4 * 1024 * 1024
        ):
            raise RuntimeError(
                "Container backend operator Compose baseline is missing or unsafe; "
                "refusing to consult the agent workspace."
            )
        project_directory = self._pinned_compose_project_directory(run_root)
        if not project_directory.is_dir() or path_is_link_or_junction(
            project_directory
        ):
            raise RuntimeError(
                "Container backend protected Compose project directory is unsafe."
            )
        return pinned

    def _compose_argv(
        self,
        run_root: Path,
        compose_file: Path,
        compose_project: str,
        *args: str,
    ) -> list[str]:
        return [
            self._container.engine,
            "compose",
            "-p",
            compose_project,
            "--project-directory",
            str(self._pinned_compose_project_directory(run_root)),
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
        compose_file = self._validated_pinned_compose_file(run_root)
        state = self._read_container_state(run_root)
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses compose startup with non-canonical resource labels."
            )
        self._assert_compose_managed_names_owned_or_unused(
            run_root,
            compose_file,
            compose_project,
            labels,
        )
        self._assert_compose_project_owned_or_unused(
            run_root,
            compose_project,
            labels,
        )
        override_file = self._write_compose_label_override(run_root, compose_file, compose_project, state)
        state["compose_label_override"] = str(override_file)
        self._write_container_state(run_root, state)
        result = self._runner.run(
            self._compose_argv(
                run_root,
                compose_file,
                compose_project,
                "-f",
                str(override_file),
                "up",
                "-d",
                "--remove-orphans",
            ),
            cwd=run_root,
        )
        self._write_image_log(logs, "compose-services.log", result)
        if result.returncode != 0:
            failure_path = logs / "service-startup-failure.json"
            atomic_write_text(
                failure_path,
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
                ),
                encoding="utf-8",
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
            compose = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(
                "Container backend could not safely parse the Compose file; "
                f"refusing startup: {compose_file}: {exc}"
            ) from exc
        if not isinstance(compose, dict):
            raise RuntimeError(
                "Container backend requires the Compose file root to be a mapping."
            )
        if compose.get("include"):
            # Includes can introduce services and globally named resources that
            # are absent from this file. Until the resolved Compose model is
            # available portably across Docker and Podman providers, reject the
            # construct instead of starting resources we cannot label or own.
            raise RuntimeError(
                "Container backend does not support Compose include; inline the "
                "included services so every managed resource can be safety-labeled."
            )
        services = compose.get("services", {})
        volumes = compose.get("volumes", {})
        networks = compose.get("networks", {})
        default_network = networks.get("default") if isinstance(networks, dict) else None
        if (
            isinstance(default_network, dict)
            and default_network.get("external") is True
        ):
            raise RuntimeError(
                "Container backend requires the Compose default network to be "
                "managed so the worker can join its run-scoped service network."
            )
        if isinstance(services, dict):
            extended_services = [
                str(name)
                for name, value in services.items()
                if isinstance(value, dict) and value.get("extends")
            ]
            if extended_services:
                raise RuntimeError(
                    "Container backend does not support Compose extends; inline "
                    "the inherited service configuration so global names can be "
                    f"validated: {', '.join(sorted(extended_services))}"
                )
            named_services = [
                str(name)
                for name, value in services.items()
                if isinstance(value, dict) and value.get("container_name")
            ]
            if named_services:
                raise RuntimeError(
                    "Container backend compose files must not set container_name "
                    "for managed services; remove it so Compose can scope names "
                    f"to this run: {', '.join(sorted(named_services))}"
                )
        explicitly_named = [
            f"{kind}:{name}"
            for kind, resources in (("volume", volumes), ("network", networks))
            if isinstance(resources, dict)
            for name, value in resources.items()
            if isinstance(value, dict)
            and value.get("name")
            and value.get("external") is not True
        ]
        if explicitly_named:
            raise RuntimeError(
                "Container backend compose files must not assign global names "
                "to managed volumes or networks; remove name or declare the "
                "resource external: "
                + ", ".join(sorted(explicitly_named))
            )
        network_names = (
            {
                name: value
                for name, value in networks.items()
                if not isinstance(value, dict) or value.get("external") is not True
            }
            if isinstance(networks, dict)
            else {}
        )
        if not isinstance(networks, dict) or "default" not in networks:
            network_names.setdefault("default", {})

        payload: dict[str, Any] = {
            "services": {str(name): {"labels": labels} for name in services},
            "networks": {str(name): {"labels": labels} for name in network_names},
        }
        if volumes:
            payload["volumes"] = {
                str(name): {"labels": labels}
                for name, value in volumes.items()
                if not isinstance(value, dict) or value.get("external") is not True
            }
        path = run_root / "container-compose-labels.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def _stop_sidecar_services(self, run_root: Path, state: dict[str, Any]) -> None:
        compose_file = self._validated_pinned_compose_file(run_root)
        compose_project = str(state.get("compose_project") or "")
        if str(state.get("compose_file") or "") != str(compose_file) or not compose_project:
            raise RuntimeError(
                "Container backend cannot stop sidecar services because its "
                "Compose state is incomplete."
            )
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses compose stop with non-canonical resource labels."
            )
        self._assert_compose_project_owned_or_unused(
            run_root,
            compose_project,
            labels,
        )
        extra = ["-f", str(state["compose_label_override"])] if state.get("compose_label_override") else []
        result = self._runner.run(
            self._compose_argv(
                run_root,
                compose_file,
                compose_project,
                *extra,
                "stop",
            ),
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "compose-stop-before-snapshot.log", result)
        if result.returncode != 0:
            raise RuntimeError(
                "Container backend could not cleanly stop sidecar services before snapshot "
                f"for compose project {compose_project}. "
                f"See {run_root / 'logs' / 'compose-stop-before-snapshot.log'}"
            )
        filters = [
            argument
            for key, value in sorted(labels.items())
            for argument in ("--filter", f"label={key}={value}")
        ]
        running = self._runner.run(
            [
                self._container.engine,
                "ps",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                *filters,
                "--format",
                "{{.ID}}",
            ],
            cwd=run_root,
        )
        if running.returncode != 0 or running.stdout.strip():
            raise RuntimeError(
                "Container backend could not positively verify every sidecar "
                "service stopped before snapshot."
            )

    @staticmethod
    def _snapshot_pause_container_ids(state: dict[str, Any]) -> list[str]:
        candidates = [str(state.get("worker_container") or "")]
        playwright = state.get("playwright_mcp", {})
        if isinstance(playwright, dict) and playwright.get("topology") == "sidecar":
            candidates.append(str(playwright.get("sidecar_container") or ""))
        return list(dict.fromkeys(item for item in candidates if item))

    def _pause_owned_container(
        self,
        run_root: Path,
        state: dict[str, Any],
        container_id: str,
    ) -> None:
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses snapshot pause with non-canonical labels."
            )
        self._require_resource_owned(
            run_root,
            "container",
            container_id,
            labels,
            allow_additional_labels=True,
        )
        paused = self._runner.run(
            [self._container.engine, "pause", container_id],
            cwd=run_root,
        )
        if paused.returncode != 0:
            raise RuntimeError(
                "Container backend could not positively pause its worker before snapshot."
            )
        try:
            positively_paused = self._container_pause_state(run_root, container_id)
        except RuntimeError:
            # The mutation succeeded but verification failed. Roll it back
            # immediately because the caller cannot yet know it must resume
            # this container in its outer finally block.
            self._runner.run(
                [self._container.engine, "unpause", container_id],
                cwd=run_root,
            )
            raise
        if not positively_paused:
            self._runner.run(
                [self._container.engine, "unpause", container_id],
                cwd=run_root,
            )
            raise RuntimeError(
                "Container backend could not positively pause its worker before snapshot."
            )

    def _unpause_owned_container(
        self,
        run_root: Path,
        state: dict[str, Any],
        container_id: str,
    ) -> None:
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses snapshot unpause with non-canonical labels."
            )
        self._require_resource_owned(
            run_root,
            "container",
            container_id,
            labels,
            allow_additional_labels=True,
        )
        unpaused = self._runner.run(
            [self._container.engine, "unpause", container_id],
            cwd=run_root,
        )
        if unpaused.returncode != 0 or self._container_pause_state(
            run_root, container_id
        ):
            raise RuntimeError(
                "Container backend could not positively resume its worker after snapshot."
            )

    def _container_pause_state(self, run_root: Path, container_id: str) -> bool:
        inspected = self._runner.run(
            [
                self._container.engine,
                "inspect",
                "--format",
                "{{.State.Paused}}",
                container_id,
            ],
            cwd=run_root,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                "Container backend could not inspect worker pause state."
            )
        rendered = inspected.stdout.strip().casefold()
        if rendered not in {"true", "false"}:
            raise RuntimeError(
                "Container backend received an invalid worker pause state."
            )
        return rendered == "true"

    def _container_runtime_status(
        self,
        run_root: Path,
        state: dict[str, Any],
        container_id: str,
    ) -> str:
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses container-state inspection with "
                "non-canonical resource labels."
            )
        self._require_resource_owned(
            run_root,
            "container",
            container_id,
            labels,
            allow_additional_labels=True,
        )
        inspected = self._runner.run(
            [
                self._container.engine,
                "inspect",
                "--format",
                "{{.State.Status}}",
                container_id,
            ],
            cwd=run_root,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                "Container backend could not inspect exact-owned container state."
            )
        rendered = inspected.stdout.strip().casefold()
        if rendered not in {
            "created",
            "running",
            "paused",
            "restarting",
            "removing",
            "exited",
            "dead",
        }:
            raise RuntimeError(
                "Container backend received an invalid exact-owned container state."
            )
        return rendered

    def _container_exit_code(
        self,
        run_root: Path,
        state: dict[str, Any],
        container_id: str,
    ) -> int:
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses container exit-code inspection with "
                "non-canonical resource labels."
            )
        self._require_resource_owned(
            run_root,
            "container",
            container_id,
            labels,
            allow_additional_labels=True,
        )
        inspected = self._runner.run(
            [
                self._container.engine,
                "inspect",
                "--format",
                "{{.State.ExitCode}}",
                container_id,
            ],
            cwd=run_root,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                "Container backend could not inspect exact-owned container exit code."
            )
        rendered = inspected.stdout.strip()
        try:
            exit_code = int(rendered, 10)
        except ValueError as exc:
            raise RuntimeError(
                "Container backend received an invalid exact-owned container exit code."
            ) from exc
        if exit_code < 0:
            raise RuntimeError(
                "Container backend received an invalid exact-owned container exit code."
            )
        return exit_code

    def _record_runtime_generation(
        self,
        run_root: Path,
        state: dict[str, Any],
    ) -> None:
        """Persist the complete set of live containers for one generation."""
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses to record a runtime with non-canonical labels."
            )
        owned = self._discover_owned_container_ids(run_root, labels)
        running: list[str] = []
        for container_id in sorted(owned):
            status = self._container_runtime_status(run_root, state, container_id)
            if status == "running":
                if self._container_pause_state(run_root, container_id):
                    raise RuntimeError(
                        "Container backend found a paused container while recording "
                        "a live runtime generation."
                    )
                running.append(container_id)
            elif status == "exited" and self._container_exit_code(
                run_root,
                state,
                container_id,
            ) == 0:
                # Successful one-shot Compose jobs are part of a healthy
                # generation but are not live writers to record/pause.
                continue
            else:
                raise RuntimeError(
                    "Container backend found an unhealthy exact-owned container "
                    f"while recording its runtime generation: {container_id} ({status})."
                )
        worker = str(state.get("worker_container") or "")
        if not worker or worker not in running:
            raise RuntimeError(
                "Container backend could not verify its worker in the runtime generation."
            )
        playwright = state.get("playwright_mcp", {})
        if isinstance(playwright, dict) and playwright.get("topology") == "sidecar":
            sidecar = str(playwright.get("sidecar_container") or "")
            if not sidecar or sidecar not in running:
                raise RuntimeError(
                    "Container backend could not verify its Playwright sidecar in "
                    "the runtime generation."
                )
        state["runtime_generation_containers"] = running
        state["runtime_generation_status"] = "running"
        self._write_container_state(run_root, state)

    def _recorded_runtime_is_healthy(
        self,
        run_root: Path,
        state: dict[str, Any],
        *,
        allow_paused: bool = False,
    ) -> bool:
        """Check that durable state still describes one complete live runtime."""
        if state.get("runtime_generation_status") != "running":
            return False
        raw_expected = state.get("runtime_generation_containers")
        if not isinstance(raw_expected, list) or not raw_expected:
            return False
        expected_items = [str(item) for item in raw_expected if str(item)]
        expected = set(expected_items)
        if len(expected) != len(raw_expected) or len(expected) != len(expected_items):
            return False
        worker = str(state.get("worker_container") or "")
        if not worker or worker not in expected:
            return False
        playwright = state.get("playwright_mcp", {})
        if isinstance(playwright, dict) and playwright.get("topology") == "sidecar":
            sidecar = str(playwright.get("sidecar_container") or "")
            if not sidecar or sidecar not in expected:
                return False
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses runtime health checks with non-canonical labels."
            )
        owned = self._discover_owned_container_ids(run_root, labels)
        if not expected.issubset(owned):
            return False
        paused_raw = state.get("host_access_paused_containers", [])
        if not isinstance(paused_raw, list):
            return False
        paused = {str(item) for item in paused_raw if str(item)}
        if allow_paused:
            if paused != expected:
                return False
        elif paused:
            return False
        for container_id in sorted(owned):
            status = self._container_runtime_status(run_root, state, container_id)
            if container_id in expected:
                if status != "running":
                    return False
                is_paused = self._container_pause_state(run_root, container_id)
                if allow_paused != is_paused:
                    return False
            elif status == "exited":
                if self._container_exit_code(run_root, state, container_id) != 0:
                    return False
            else:
                # A live exact-owned container omitted from the persisted set
                # could be an unrecorded writer from a partial generation;
                # created/dead/unstable members are incomplete generations.
                return False
        return True

    def _refresh_sidecar_service_volumes(self, run_root: Path, state: dict[str, Any]) -> None:
        compose_project = str(state.get("compose_project") or "")
        if not compose_project:
            return
        label_filters = [
            argument
            for key, value in sorted(state.get("resource_labels", {}).items())
            for argument in ("--filter", f"label={key}={value}")
        ]
        result = self._runner.run(
            [
                self._container.engine,
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                *label_filters,
                "--format",
                "{{.Name}}",
            ],
            cwd=run_root,
        )
        self._write_image_log(run_root / "logs", "compose-volume-discovery.log", result)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown engine error").strip()
            raise RuntimeError(
                "Container backend could not inventory sidecar service volumes "
                f"for a consistent snapshot: {detail}"
            )
        volumes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
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
        snapshot_root.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(
                dir=snapshot_root.parent,
                prefix=f".{snapshot_root.name}.staging-",
            )
        )
        captured: dict[str, dict[str, Any]] = {}
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses service-volume snapshot with "
                "non-canonical resource labels."
            )
        try:
            # Validate every source volume before capturing any archive, then
            # publish the complete archive set with one directory rename.
            for volume in volumes:
                self._require_volume_safe_to_use(run_root, volume, labels)
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
                        f"{staging_root}:/workspace/service-volume-snapshot",
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
                archive_path = staging_root / archive_name
                if result.returncode != 0 or not archive_path.is_file():
                    raise RuntimeError(
                        f"Container backend could not snapshot service volume {volume}. "
                        f"See {run_root / 'logs' / ('service-volume-snapshot-' + _safe_artifact_name(volume) + '.log')}"
                    )
                archive_size, archive_sha256 = self._regular_file_sha256(
                    archive_path
                )
                captured[volume] = {
                    "path": str(snapshot_root / archive_name),
                    "size": archive_size,
                    "sha256": archive_sha256,
                }
            if path_is_link_or_junction(snapshot_root):
                snapshot_root.unlink()
            elif snapshot_root.exists():
                remove_tree(snapshot_root)
            os.replace(staging_root, snapshot_root)
        finally:
            if staging_root.exists():
                remove_tree(staging_root, ignore_errors=True)
        snapshots = dict(state.get("service_volume_snapshots", {}))
        snapshots[label] = captured
        state["service_volume_snapshots"] = snapshots
        self._write_container_state(run_root, state)

    @staticmethod
    def _regular_file_sha256(path: Path) -> tuple[int, str]:
        """Hash one regular file without following a last-component link."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError(f"Container backend could not safely read {path}.") from exc
        digest = hashlib.sha256()
        try:
            metadata = os.fstat(descriptor)
            reparse = bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if not stat.S_ISREG(metadata.st_mode) or reparse:
                raise RuntimeError(
                    f"Container backend refuses non-regular snapshot archive {path}."
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            return metadata.st_size, digest.hexdigest()
        finally:
            os.close(descriptor)

    def _validated_sidecar_service_volume_restore(
        self,
        run_root: Path,
        state: dict[str, Any],
        label: str,
    ) -> list[tuple[str, Path]]:
        snapshots = state.get("service_volume_snapshots", {})
        volume_archives = snapshots.get(label, {}) if isinstance(snapshots, dict) else {}
        if not isinstance(volume_archives, dict):
            raise RuntimeError(
                "Container backend service-volume snapshot metadata is invalid."
            )
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses service-volume restore with "
                "non-canonical resource labels."
            )
        expected_volumes = {
            str(volume) for volume in state.get("service_volumes", []) if str(volume)
        }
        archived_volumes = {str(volume) for volume in volume_archives}
        if archived_volumes != expected_volumes:
            missing = sorted(expected_volumes - archived_volumes)
            unexpected = sorted(archived_volumes - expected_volumes)
            detail: list[str] = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if unexpected:
                detail.append("unexpected " + ", ".join(unexpected))
            raise RuntimeError(
                "Container backend service-volume snapshot is incomplete or "
                "does not match the protected volume inventory: " + "; ".join(detail)
            )
        snapshot_root = (
            run_root / "snapshots" / f"{_safe_artifact_name(label)}.service-volumes"
        )
        if expected_volumes and (
            not snapshot_root.is_dir() or path_is_link_or_junction(snapshot_root)
        ):
            raise RuntimeError(
                "Container backend service-volume snapshot directory is missing or unsafe."
            )
        prepared: list[tuple[str, Path]] = []
        for volume in sorted(expected_volumes):
            archive_record = volume_archives[volume]
            expected_size: int | None = None
            expected_sha256 = ""
            if isinstance(archive_record, str):
                # v0.4.0 recovery points stored only a path. They remain
                # usable, but still receive the read-only tar validation below.
                archive_path = Path(archive_record)
            elif isinstance(archive_record, dict):
                archive_path = Path(str(archive_record.get("path") or ""))
                size_raw = archive_record.get("size")
                sha256_raw = archive_record.get("sha256")
                if (
                    not isinstance(size_raw, int)
                    or size_raw < 0
                    or not isinstance(sha256_raw, str)
                    or re.fullmatch(r"[0-9a-f]{64}", sha256_raw) is None
                ):
                    raise RuntimeError(
                        "Container backend service-volume archive integrity "
                        f"metadata is invalid: {volume}"
                    )
                expected_size = size_raw
                expected_sha256 = sha256_raw
            else:
                raise RuntimeError(
                    "Container backend service-volume archive metadata is "
                    f"invalid: {volume}"
                )
            expected_archive = snapshot_root / f"{_safe_artifact_name(volume)}.tar"
            try:
                metadata = archive_path.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f"Container backend service-volume archive is missing: {volume}"
                ) from exc
            reparse = bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if (
                not archive_path.is_absolute()
                or Path(os.path.abspath(archive_path)) != expected_archive
                or not stat.S_ISREG(metadata.st_mode)
                or reparse
                or path_is_link_or_junction(archive_path)
            ):
                raise RuntimeError(
                    f"Container backend service-volume archive is unsafe: {volume}"
                )
            actual_size, actual_sha256 = self._regular_file_sha256(archive_path)
            if (
                expected_size is not None
                and (
                    actual_size != expected_size
                    or actual_sha256 != expected_sha256
                )
            ):
                raise RuntimeError(
                    "Container backend service-volume archive failed its "
                    f"integrity check: {volume}"
                )
            archive_check = self._runner.run(
                [
                    self._container.engine,
                    "run",
                    "--rm",
                    *self._label_argv(state),
                    "-v",
                    f"{archive_path.parent}:/workspace/service-volume-snapshot:ro",
                    state["image"],
                    "sh",
                    "-lc",
                    "tar -tf "
                    f"/workspace/service-volume-snapshot/{archive_path.name} "
                    ">/dev/null",
                ],
                cwd=run_root,
            )
            self._write_image_log(
                run_root / "logs",
                f"service-volume-validate-{_safe_artifact_name(volume)}.log",
                archive_check,
            )
            if archive_check.returncode != 0:
                raise RuntimeError(
                    "Container backend service-volume archive is unreadable: "
                    f"{volume}. See "
                    f"{run_root / 'logs' / ('service-volume-validate-' + _safe_artifact_name(volume) + '.log')}"
                )
            self._require_volume_safe_to_use(run_root, volume, labels)
            prepared.append((volume, archive_path))

        return prepared

    def _restore_sidecar_service_volumes(
        self,
        run_root: Path,
        state: dict[str, Any],
        label: str,
        *,
        prepared: list[tuple[str, Path]] | None = None,
    ) -> None:
        # All archives, ownership labels, and foreign consumers are validated
        # before the first destructive volume wipe. This prevents a bad later
        # entry from leaving a partially restored service set.
        prepared = (
            prepared
            if prepared is not None
            else self._validated_sidecar_service_volume_restore(run_root, state, label)
        )
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses service-volume restore with "
                "non-canonical resource labels."
            )
        for volume, archive_path in prepared:
            # Source rescue/restore can take arbitrarily long after the bulk
            # archive preflight. Close that avoidable race by rechecking exact
            # ownership and every attached consumer immediately before the
            # destructive volume wipe helper is launched.
            self._require_volume_safe_to_use(run_root, volume, labels)
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
            raise RuntimeError(
                "Container backend refuses to start a second worker in a "
                "partially recorded runtime generation."
            )
        cidfile = run_root / "backend-state" / "in-worker-services.cid"
        cidfile.parent.mkdir(parents=True, exist_ok=True)
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
            volume = str(workspace_volumes[0])
            labels = self._canonical_state_resource_labels(run_root, state)
            if labels is None:
                raise RuntimeError(
                    "Container backend refuses worker start with non-canonical "
                    "resource labels."
                )
            self._require_volume_safe_to_use(run_root, volume, labels)
            argv.extend(["-v", f"{volume}:/workspace/source"])
        else:
            argv.extend(["-v", f"{run_root / 'source'}:/workspace/source"])
        safe_git_config = self._container_safe_git_config_path(run_root)
        if not safe_git_config.is_file() or path_is_link_or_junction(safe_git_config):
            raise RuntimeError(
                "Container backend safe Git configuration is unavailable."
            )
        argv.extend(
            [
                "-v",
                f"{safe_git_config}:{CONTAINER_RUNTIME_SOURCE}/.git/config:ro",
            ]
        )
        argv.extend(
            [
                "-v",
                f"{run_root / 'provider-homes' / 'codex' / '.spec-codex-home'}:{CONTAINER_CODEX_HOME}",
            ]
        )
        attached_networks = list(
            dict.fromkeys(
                [
                    *(str(network) for network in state.get("service_networks", [])),
                    *self._playwright_sidecar_networks(state),
                ]
            )
        )
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses worker network attachment with "
                "non-canonical resource labels."
            )
        for network in attached_networks:
            self._require_network_safe_to_use(run_root, network, labels)
            argv.extend(["--network", network])
        argv.extend([state["image"], "sh", "-lc", "sleep infinity"])
        result = self._runner.run(argv, cwd=run_root)
        self._write_image_log(run_root / "logs", "in-worker-services.log", result)
        container_id = (
            cidfile.read_text(encoding="utf-8").strip()
            if cidfile.is_file()
            else result.stdout.strip()
        )
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
            atomic_write_text(
                failure_path,
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
                ),
                encoding="utf-8",
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
            if not self._verify_container_removed(run_root, container_id):
                labels = self._canonical_state_resource_labels(run_root, state)
                if labels is None:
                    raise RuntimeError(
                        "Container backend refuses worker reset with "
                        "non-canonical resource labels."
                    )
                self._require_resource_owned(
                    run_root,
                    "container",
                    container_id,
                    labels,
                    allow_additional_labels=True,
                )
                result = self._runner.run(
                    [self._container.engine, "rm", "-f", container_id],
                    cwd=run_root,
                )
                self._write_image_log(
                    run_root / "logs",
                    "in-worker-services-reset.log",
                    result,
                )
                if result.returncode != 0 or not self._verify_container_removed(
                    run_root,
                    container_id,
                ):
                    detail = (result.stderr or result.stdout or "unknown error").strip()
                    raise RuntimeError(
                        "Container backend could not positively remove the worker "
                        f"before restore: {detail}"
                    )
        cidfile = run_root / "backend-state" / "in-worker-services.cid"
        if cidfile.exists():
            cidfile.unlink()
        containers = [str(item) for item in state.get("containers", []) if str(item) != container_id]
        state["containers"] = containers
        state["worker_container"] = ""
        state["runtime_generation_containers"] = []
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

        The workspace is quiesced here. Bootstrap is deliberately deferred to
        the next runtime start, after sidecar services and the persistent worker
        exist, so it runs exactly once in the environment the agent will use.
        """
        run_root = workspace.outbox_path.parent.resolve()
        state = self._read_container_state(run_root, missing_ok=True)
        if state.get("workspace_mode") != "volume":
            return
        self._seed_volume_workspace(workspace, state)
        state["workspace_volume_preseeded_for_runtime"] = True
        self._write_container_state(run_root, state)

    def _seed_volume_workspace(self, workspace: WorkspaceHandle, state: dict[str, Any]) -> None:
        volumes = state.get("workspace_volumes") or state.get("volumes", [])
        if not volumes:
            return
        volume = str(volumes[0])
        # This write is the crash boundary for a destructive reseed. A retry
        # seeing ``seeding`` knows the volume may be empty or partial and keeps
        # the existing host mirror authoritative instead of importing it.
        state["workspace_volume_seed_state"] = "seeding"
        self._write_container_state(workspace.outbox_path.parent, state)
        create = self._runner.run(
            [self._container.engine, "volume", "create", *self._label_argv(state), volume],
            cwd=workspace.outbox_path.parent,
        )
        if create.returncode != 0:
            raise RuntimeError(f"Container backend could not create source volume {volume}.")
        run_root = workspace.outbox_path.parent
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses source-volume seed with "
                "non-canonical resource labels."
            )
        self._require_volume_safe_to_use(run_root, volume, labels)
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
        state["workspace_volume_seed_state"] = "ready"
        self._write_container_state(workspace.outbox_path.parent, state)

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
        in. Absence is synchronized too, so removing launch-scoped credentials
        on the host also scrubs the volume. This method runs a one-shot
        container that mounts both the host worktree and the workspace volume.
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
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise RuntimeError(
                "Container backend refuses source-volume sync with "
                "non-canonical resource labels."
            )
        self._require_volume_safe_to_use(run_root, volume, labels)

        host_source = run_root / "source"
        normalized: list[str] = []
        for entry in relative_paths:
            rel = entry.strip()
            if not rel or rel.startswith("/"):
                continue
            posix_path = PurePosixPath(rel.replace("\\", "/"))
            if ".." in posix_path.parts or posix_path.is_absolute():
                continue
            posix = posix_path.as_posix().rstrip("/")
            if posix and posix not in normalized:
                normalized.append(posix)
        if not normalized:
            return

        copy_cmds: list[str] = []
        for rel in normalized:
            quoted = shlex.quote(rel)
            parent = "/".join(rel.split("/")[:-1])
            remove_clause = f"rm -rf /workspace/source/{quoted}"
            host_path = host_source / rel
            if host_path.exists() or host_path.is_symlink():
                parent_clause = (
                    f"mkdir -p /workspace/source/{shlex.quote(parent)} && "
                    if parent
                    else ""
                )
                copy_cmds.append(
                    f"{parent_clause}{remove_clause} && "
                    f"cp -a /workspace/host/{quoted} /workspace/source/{quoted}"
                )
            else:
                copy_cmds.append(remove_clause)
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
        labels = self._canonical_state_resource_labels(run_root, state)
        if labels is None:
            raise ExecutionBackendImportError(
                "Container backend import refused non-canonical resource labels."
            )
        try:
            self._require_volume_safe_to_use(run_root, volume, labels)
        except RuntimeError as exc:
            raise ExecutionBackendImportError(
                f"Container backend import refused unsafe source volume {volume}: {exc}"
            ) from exc
        logs = run_root / "logs"
        log_path = logs / "volume-import.log"
        failure_path = logs / "volume-import-failure.json"
        try:
            staging = Path(
                tempfile.mkdtemp(prefix=".spec-volume-import-", dir=run_root)
            )
        except OSError as exc:
            raise ExecutionBackendImportError(
                "Container backend import failed: could not create a staging directory."
            ) from exc
        try:
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
                    f"{staging}:/workspace/host",
                    state["image"],
                    "sh",
                    "-lc",
                    "find /workspace/source -mindepth 1 -maxdepth 1 "
                    "-exec sh -c 'cp -a \"$@\" /workspace/host/' sh {} +",
                ]
            )
            sync = self._runner.run(
                argv,
                cwd=run_root,
            )
            self._write_image_log(logs, "volume-import.log", sync)
            if sync.returncode != 0:
                atomic_write_text(
                    failure_path,
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
                    ),
                    encoding="utf-8",
                )
                raise ExecutionBackendImportError(
                    f"Container backend import failed: could not import source volume {volume}.",
                    artifact_paths=(log_path, failure_path),
                )

            host_source = run_root / "source"
            git_dir = staging / ".git"
            if (
                not host_source.is_dir()
                or path_is_link_or_junction(host_source)
                or path_is_link_or_junction(staging)
                or not git_dir.is_dir()
                or path_is_link_or_junction(git_dir)
            ):
                raise OSError(
                    "refusing to replace the host source with an incomplete or "
                    "linked container workspace checkout"
                )
            # Replace the worker's local config inside the private staging
            # tree, then reject every linked/special metadata entry before the
            # host checkout is swapped. No Git process reads this untrusted
            # staging tree.
            self._restore_container_safe_git_config(
                run_root=run_root,
                source=staging,
            )
            backup = run_root / f"{staging.name}-previous"
            host_source.rename(backup)
            try:
                staging.rename(host_source)
            except OSError:
                backup.rename(host_source)
                raise
            remove_tree(backup, ignore_errors=True)
        except ExecutionBackendImportError:
            raise
        except (OSError, RuntimeError) as exc:
            failure_path = logs / "volume-import-failure.json"
            atomic_write_text(
                failure_path,
                json.dumps(
                    {
                        "backend": "container",
                        "failure_type": "import",
                        "failure_subtype": "volume_workspace_import_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "volume": volume,
                        "returncode": None,
                        "detail": str(exc),
                        "log_path": str(log_path),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            raise ExecutionBackendImportError(
                f"Container backend import failed: could not install source volume {volume}.",
                artifact_paths=(log_path, failure_path),
            ) from exc
        finally:
            if staging.exists():
                remove_tree(staging, ignore_errors=True)

    def _container_run_argv(
        self,
        *,
        run_root: Path,
        cwd: Path,
        command: list[str],
        worker_env: dict[str, str],
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
        translated_command = [self._translate_container_paths(value, path_mappings) for value in command]
        translated_command = self._relax_codex_sandbox_for_container(translated_command)
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
        for key in sorted(worker_env):
            argv.extend(["-e", self._container_worker_env_arg(key)])
        if not agent and "HOME" not in worker_env:
            argv.extend(["-e", f"HOME={CONTAINER_RUNTIME_SOURCE}/.spec-claude-home"])
        argv.extend([container_id, *translated_command])
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
    def _container_worker_env_arg(key: str) -> str:
        if not _is_valid_container_env_name(key):
            raise ValueError(f"Invalid container environment variable name: {key!r}")
        return key

    def _container_worker_environment(
        self,
        *,
        run_root: Path,
        env: dict[str, str],
        state: dict[str, Any],
        declared_env_keys: frozenset[str] = frozenset(),
    ) -> dict[str, str]:
        """Return the sole validated environment exported into the worker."""
        path_mappings = self._container_path_mappings(run_root)
        worker_env = {
            key: self._translate_container_paths(value, path_mappings)
            for key, value in self._filter_container_worker_env(
                env,
                declared_env_keys=declared_env_keys,
            ).items()
        }
        home_value = env.get("HOME")
        if isinstance(home_value, str) and home_value:
            translated_home = self._translate_container_paths(home_value, path_mappings)
            if translated_home == CONTAINER_RUNTIME_SOURCE or translated_home.startswith(
                f"{CONTAINER_RUNTIME_SOURCE}/"
            ):
                worker_env["HOME"] = translated_home

        service_env = state.get("service_env", {})
        if not isinstance(service_env, dict):
            raise RuntimeError("Container backend service environment is invalid.")
        for raw_key, raw_value in service_env.items():
            if not _is_valid_container_env_name(raw_key) or not isinstance(raw_value, str):
                raise RuntimeError("Container backend service environment is invalid.")
            worker_env.setdefault(raw_key, raw_value)
        return worker_env

    @staticmethod
    def _playwright_sidecar_networks(state: dict[str, Any]) -> list[str]:
        playwright_mcp = state.get("playwright_mcp", {})
        if not isinstance(playwright_mcp, dict):
            return []
        return [str(network) for network in playwright_mcp.get("sidecar_networks", [])]

    @staticmethod
    def _filter_container_worker_env(
        env: dict[str, str],
        *,
        declared_env_keys: frozenset[str] = frozenset(),
    ) -> dict[str, str]:
        filtered = {
            key: value
            for key, value in env.items()
            if isinstance(value, str)
            and _is_valid_container_env_name(key)
            and (
                _is_container_worker_env_allowed(key)
                or key in CONTAINER_WORKER_ENV_SECRET_ALLOWLIST
                or _is_claude_mcp_runtime_env_key(key)
                or (
                    key in declared_env_keys
                    and key.upper() not in CONTAINER_WORKER_ENV_DENYLIST
                    and not is_provider_process_startup_control_env_name(key)
                )
            )
        }
        # Generic secret filtering deliberately rejects GIT_CONFIG_KEY_n, but
        # the publication guard is a host-generated all-or-nothing bundle.
        # Validate it structurally and restore only that exact safe subset.
        filtered.update(_trusted_container_git_guard_environment(env))
        return filtered

    @staticmethod
    def _container_cwd(run_root: Path, cwd: Path) -> str:
        rel = cwd.resolve().relative_to((run_root / "source").resolve())
        rel_text = rel.as_posix()
        return CONTAINER_RUNTIME_SOURCE if rel_text == "." else f"{CONTAINER_RUNTIME_SOURCE}/{rel_text}"

    @staticmethod
    def _container_path_mappings(run_root: Path) -> list[tuple[str, str]]:
        mappings: list[tuple[str, str]] = []
        for host_path, container_path in (
            (
                run_root / "provider-homes" / "codex" / ".spec-codex-home",
                CONTAINER_CODEX_HOME,
            ),
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

    def codex_provider_home_root(self, workspace_cwd: Path) -> Path:
        """Return the host-only root bind-mounted as the worker's CODEX_HOME."""
        run_root = self._workspace_run_root(workspace_cwd)
        if run_root is None:
            raise RuntimeError(
                f"Container backend cwd is not inside a prepared workspace: {workspace_cwd}"
            )
        return run_root / "provider-homes" / "codex"

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
        resource_labels: dict[str, str],
    ) -> str | None:
        shim_dir = run_root / "passwd-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        dest_path = shim_dir / f"{Path(container_path).name}.image"
        engine = self._container.engine
        logs = run_root / "logs"
        create = self._runner.run(
            [
                engine,
                "create",
                *self._label_argv({"resource_labels": resource_labels}),
                image,
            ],
            cwd=run_root,
        )
        container_id = (create.stdout or "").strip().splitlines()[:1]
        if create.returncode != 0 or not container_id:
            skipped = subprocess.CompletedProcess(
                [engine, "cp"],
                1,
                "",
                "skipped because image extraction container creation failed",
            )
            self._write_passwd_shim_extract_log(
                logs,
                log_name,
                create=create,
                cp=skipped,
                rm=skipped,
            )
            return None
        owned_id = container_id[0]
        try:
            cp = self._runner.run(
                [engine, "cp", f"{owned_id}:{container_path}", str(dest_path)],
                cwd=run_root,
            )
        finally:
            rm = self._runner.run(
                [engine, "rm", "-f", owned_id],
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
            return dest_path.read_text(encoding="utf-8")
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
        atomic_write_text(logs / name, "\n".join(sections), encoding="utf-8")

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
        resource_labels: dict[str, str],
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
            resource_labels=resource_labels,
        )
        if passwd_text is None:
            passwd_text = baseline_passwd
        group_text = self._extract_image_text_file(
            image=image,
            container_path="/etc/group",
            run_root=run_root,
            log_name="image-group-extract.log",
            resource_labels=resource_labels,
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
        passwd_path.write_text("\n".join(passwd_lines) + "\n", encoding="utf-8")
        group_path.write_text("\n".join(group_lines) + "\n", encoding="utf-8")
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

    def _container_state_path(self, run_root: Path) -> Path:
        return run_root / "backend-state" / "container-backend-state.json"

    def _read_container_state(
        self,
        run_root: Path,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        path = self._container_state_path(run_root)
        try:
            path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return {}
            raise RuntimeError(f"Container backend state is missing: {path}")
        except OSError as exc:
            raise RuntimeError(f"Container backend state is invalid: {path}") from exc
        try:
            def reject_nonfinite_constant(value: str) -> None:
                raise ValueError(f"non-finite JSON number: {value}")

            def parse_finite_float(value: str) -> float:
                parsed = float(value)
                if not math.isfinite(parsed):
                    raise ValueError(f"non-finite JSON number: {value}")
                return parsed

            payload = json.loads(
                read_bounded_regular_text(
                    path,
                    max_bytes=_CONTAINER_BACKEND_STATE_MAX_BYTES,
                ),
                parse_constant=reject_nonfinite_constant,
                parse_float=parse_finite_float,
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise RuntimeError(f"Container backend state is invalid: {path}") from exc
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError(f"Container backend state is invalid: {path}")
        if payload.get("backend") != "container":
            raise RuntimeError(
                f"Container backend state has an invalid backend identity: {path}"
            )
        saved_engine = payload.get("engine")
        if not isinstance(saved_engine, str) or not saved_engine:
            raise RuntimeError(
                f"Container backend state has an invalid engine identity: {path}"
            )
        if saved_engine != self._container.engine:
            raise RuntimeError(
                "Container backend cannot change container engines while using "
                "an existing run; restore the original engine before retrying, "
                f"running phases, or cleaning that run: {path}"
            )
        return payload

    def _write_container_state(self, run_root: Path, state: dict[str, Any]) -> None:
        path = self._container_state_path(run_root)
        if not isinstance(state, dict) or not state:
            raise RuntimeError(
                f"Container backend state must be a non-empty JSON object: {path}"
            )
        try:
            payload = json.dumps(
                state,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError(
                f"Container backend state is not JSON-serializable: {path}"
            ) from exc
        if len(payload.encode("utf-8")) > _CONTAINER_BACKEND_STATE_MAX_BYTES:
            raise RuntimeError(
                f"Container backend state exceeds the size limit: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            payload,
            encoding="utf-8",
        )

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
        atomic_write_text(
            logs / name,
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
            ),
            encoding="utf-8",
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
