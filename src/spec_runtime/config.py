from __future__ import annotations

import os
import socket
import subprocess
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path

_REPO_ROOT_MARKERS = (".git", ".spec.toml", "pyproject.toml")

_LOCAL_CONFIG_FILENAME = ".spec.local.toml"

_ENV_COORDINATION_URL = "SPEC_COORDINATOR_URL"
_ENV_COORDINATION_TOKEN = "SPEC_COORDINATOR_TOKEN"
_ENV_COORDINATION_MACHINE_ID = "SPEC_MACHINE_ID"
_ENV_COORDINATION_REPO_ID = "SPEC_COORDINATOR_REPO_ID"
_ENV_COORDINATION_BACKEND = "SPEC_COORDINATOR_BACKEND"


@dataclass(frozen=True)
class VerifyGateConfig:
    name: str
    command: str
    parallel: bool = False
    role: str = ""  # Semantic role: "test", "lint", "e2e". Defaults to name.

    @property
    def effective_role(self) -> str:
        return self.role or self.name


@dataclass(frozen=True)
class SpecPathConfig:
    specs_dir: str = "specs"
    task_specs_dir: str = "specs/tasks"
    state_dir: str = ".spec-state"
    worktrees_dir: str = ".worktrees"


@dataclass(frozen=True)
class AgentConfig:
    default: str = "claude"
    review_default: str = ""
    allowed: tuple[str, ...] = ("claude", "codex")


@dataclass(frozen=True)
class ImplementConfig:
    setup_command: str = ""
    teardown_command: str = ""


@dataclass(frozen=True)
class UpdateConfig:
    check_enabled: bool = True


@dataclass(frozen=True)
class ReadinessConfig:
    require_forge_checks: bool = True


@dataclass(frozen=True)
class AutopilotConfig:
    container_default_enabled: bool = False
    worktree_memory_mb: int = 1536
    clone_memory_mb: int = 2048
    container_memory_mb: int = 3072
    container_bridge_endpoint_threshold: int = 950
    container_capacity_recheck_seconds: float = 30.0
    # Dispatch discipline: on lock contention autopilot backs off exponentially
    # for the contended spec (base -> base*2 -> base*4 ...) capped at max, and
    # yields to non-autopilot actors (operator resume, steer, manual phase) for
    # a grace window after they touch a run.
    lock_backoff_base_seconds: float = 30.0
    lock_backoff_max_seconds: float = 1800.0
    operator_grace_seconds: float = 600.0


SUPPORTED_EXECUTION_BACKENDS: tuple[str, ...] = ("worktree", "clone", "container")
KNOWN_FUTURE_EXECUTION_BACKENDS: tuple[str, ...] = ()
ALLOWED_EXECUTION_BACKENDS: tuple[str, ...] = (
    SUPPORTED_EXECUTION_BACKENDS + KNOWN_FUTURE_EXECUTION_BACKENDS
)
ALLOWED_EXECUTION_SAFETY_MODES: tuple[str, ...] = ("safe", "full-auto", "trusted")
ALLOWED_CONTAINER_WORKSPACE_MODES: tuple[str, ...] = ("auto", "bind", "volume")
ALLOWED_CONTAINER_PLAYWRIGHT_MCP_TOPOLOGIES: tuple[str, ...] = (
    "disabled",
    "in-worker",
    "sidecar",
)


@dataclass(frozen=True)
class ContainerPlaywrightMcpConfig:
    topology: str = "in-worker"
    command: str = ""
    args: tuple[str, ...] = ()
    # Browser passed to the Playwright MCP server via --browser. The MCP
    # default is the chrome *channel*, which worker images typically lack and
    # whose install path escalates via `su root` (hangs forever in a container
    # without a tty). Bundled chromium installs into the user cache instead.
    # Set to "" to omit --browser and use the MCP server's own default.
    browser: str = "chromium"
    app_url: str = ""
    sidecar_endpoint: str = ""
    expected_version: str = ""
    actual_version: str = ""


@dataclass(frozen=True)
class ContainerExecutionConfig:
    engine: str = "docker"
    image: str = ""
    dockerfile: str = ".spec/worker.Dockerfile"
    workspace_mode: str = "auto"
    compose_file: str = ""
    build_ssh: str = ""
    playwright_mcp: ContainerPlaywrightMcpConfig = field(
        default_factory=ContainerPlaywrightMcpConfig
    )


@dataclass(frozen=True)
class ExecutionConfig:
    backend: str = "worktree"
    safety_mode: str = "safe"
    workspace_root: str = ".spec-workspaces"
    container: ContainerExecutionConfig = field(default_factory=ContainerExecutionConfig)
    backend_explicit: bool = False
    safety_mode_explicit: bool = False


@dataclass(frozen=True)
class CoordinationConfig:
    """Configuration for cross-machine spec coordination.

    The default state is disabled/local-only when no coordinator URL is
    configured. Token values may come from env or a gitignored local file
    and must never be required in committed ``.spec.toml``.
    """

    backend: str = ""
    url: str = ""
    repo_id: str = ""
    machine_id: str = ""
    token: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())

    def redacted_url(self) -> str:
        """Return ``url`` with any embedded credentials stripped."""
        return _redact_url_credentials(self.url)


@dataclass(frozen=True)
class BootstrapCacheConfig:
    enabled: bool = False
    command: str = ""
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpConfig:
    """Configuration for MCP server passthrough into non-interactive sessions."""

    allow_from_user: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecRuntimeConfig:
    paths: SpecPathConfig = SpecPathConfig()
    base_ref: str = "origin/master"
    retry_cap: int = 20
    no_progress_retry_threshold: int = 2
    verify_gates: tuple[VerifyGateConfig, ...] = (
        VerifyGateConfig(name="test", command="make test", parallel=True),
        VerifyGateConfig(name="lint", command="make lint", parallel=True),
        VerifyGateConfig(name="e2e", command="make e2e", parallel=False),
    )
    agents: AgentConfig = AgentConfig()
    bootstrap_install_command: str = ""
    bootstrap_cache: BootstrapCacheConfig = BootstrapCacheConfig()
    implement: ImplementConfig = ImplementConfig()
    update: UpdateConfig = UpdateConfig()
    readiness: ReadinessConfig = ReadinessConfig()
    autopilot: AutopilotConfig = AutopilotConfig()
    execution: ExecutionConfig = ExecutionConfig()
    coordination: CoordinationConfig = CoordinationConfig()
    mcp: McpConfig = McpConfig()

    @property
    def pr_base_branch(self) -> str:
        tail = self.base_ref.rsplit("/", 1)[-1].strip()
        return tail or self.base_ref


def _repo_root_from_here() -> Path:
    module_dir = Path(__file__).resolve().parent
    package_root = module_dir.parent
    boundary_root = package_root.parent if package_root.name == "src" else package_root

    for candidate in (module_dir, *module_dir.parents):
        if candidate == boundary_root.parent:
            break
        if any((candidate / marker).exists() for marker in _REPO_ROOT_MARKERS):
            return candidate
    return boundary_root


def _discover_repo_root() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists() or (candidate / ".spec.toml").is_file():
            return candidate

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return _repo_root_from_here()
    return Path(result.stdout.strip())


def _config_path() -> Path:
    override = os.getenv("SPEC_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return _discover_repo_root() / ".spec.toml"


class SpecConfigNotFoundError(FileNotFoundError):
    """Raised when .spec.toml is missing and require=True."""


class SpecConfigError(ValueError):
    """Raised when .spec.toml contains invalid values."""


def _parse_execution_section(payload: object) -> ExecutionConfig:
    if payload in (None, {}):
        return ExecutionConfig()
    if not isinstance(payload, dict):
        raise SpecConfigError("[execution] must be a TOML table")

    backend_raw = payload.get("backend", None)
    safety_raw = payload.get("safety_mode", None)
    workspace_raw = payload.get("workspace_root", None)
    container = _parse_container_execution_section(payload.get("container", {}))

    backend_explicit = backend_raw is not None
    safety_explicit = safety_raw is not None

    backend = (str(backend_raw).strip() if backend_explicit else "worktree") or "worktree"
    safety_mode = (str(safety_raw).strip() if safety_explicit else "safe") or "safe"
    workspace_root = (
        str(workspace_raw).strip() if workspace_raw is not None else ".spec-workspaces"
    ) or ".spec-workspaces"

    if backend not in ALLOWED_EXECUTION_BACKENDS:
        allowed = ", ".join(sorted(ALLOWED_EXECUTION_BACKENDS))
        raise SpecConfigError(
            f"Unknown [execution].backend {backend!r}. Allowed: {allowed}"
        )
    if safety_mode not in ALLOWED_EXECUTION_SAFETY_MODES:
        allowed = ", ".join(sorted(ALLOWED_EXECUTION_SAFETY_MODES))
        raise SpecConfigError(
            f"Unknown [execution].safety_mode {safety_mode!r}. Allowed: {allowed}"
        )

    return ExecutionConfig(
        backend=backend,
        safety_mode=safety_mode,
        workspace_root=workspace_root,
        container=container,
        backend_explicit=backend_explicit,
        safety_mode_explicit=safety_explicit,
    )


def _parse_container_execution_section(payload: object) -> ContainerExecutionConfig:
    if payload in (None, {}):
        return ContainerExecutionConfig()
    if not isinstance(payload, dict):
        raise SpecConfigError("[execution.container] must be a TOML table")

    engine = str(payload.get("engine", "docker")).strip() or "docker"
    image = str(payload.get("image", "")).strip()
    dockerfile = str(payload.get("dockerfile", ".spec/worker.Dockerfile")).strip()
    workspace_mode = str(payload.get("workspace_mode", "auto")).strip() or "auto"
    compose_file = str(payload.get("compose_file", "")).strip()
    build_ssh = str(payload.get("build_ssh", "")).strip()
    playwright_mcp = _parse_container_playwright_mcp_section(
        payload.get("playwright_mcp", {})
    )

    if workspace_mode not in ALLOWED_CONTAINER_WORKSPACE_MODES:
        allowed = ", ".join(ALLOWED_CONTAINER_WORKSPACE_MODES)
        raise SpecConfigError(
            f"Unknown [execution.container].workspace_mode {workspace_mode!r}. Allowed: {allowed}"
        )

    return ContainerExecutionConfig(
        engine=engine,
        image=image,
        dockerfile=dockerfile,
        workspace_mode=workspace_mode,
        compose_file=compose_file,
        build_ssh=build_ssh,
        playwright_mcp=playwright_mcp,
    )


def _parse_container_playwright_mcp_section(
    payload: object,
) -> ContainerPlaywrightMcpConfig:
    if payload in (None, {}):
        return ContainerPlaywrightMcpConfig()
    if not isinstance(payload, dict):
        raise SpecConfigError("[execution.container.playwright_mcp] must be a TOML table")

    topology = str(payload.get("topology", "in-worker")).strip() or "in-worker"
    if topology not in ALLOWED_CONTAINER_PLAYWRIGHT_MCP_TOPOLOGIES:
        allowed = ", ".join(ALLOWED_CONTAINER_PLAYWRIGHT_MCP_TOPOLOGIES)
        raise SpecConfigError(
            f"Unknown [execution.container.playwright_mcp].topology {topology!r}. "
            f"Allowed: {allowed}"
        )

    args_raw = payload.get("args", ())
    if args_raw in (None, "") or args_raw == ():
        args: tuple[str, ...] = ()
    elif isinstance(args_raw, list):
        args = tuple(str(item) for item in args_raw)
    else:
        raise SpecConfigError("[execution.container.playwright_mcp].args must be a list")

    return ContainerPlaywrightMcpConfig(
        topology=topology,
        command=str(payload.get("command", "")).strip(),
        args=args,
        browser=str(payload.get("browser", "chromium")).strip(),
        app_url=str(payload.get("app_url", "")).strip(),
        sidecar_endpoint=str(payload.get("sidecar_endpoint", "")).strip(),
        expected_version=str(payload.get("expected_version", "")).strip(),
        actual_version=str(payload.get("actual_version", "")).strip(),
    )


def _parse_mcp_section(payload: object) -> McpConfig:
    if payload in (None, {}):
        return McpConfig()
    if not isinstance(payload, dict):
        raise SpecConfigError("[mcp] must be a TOML table")
    allow_raw = payload.get("allow_from_user", ())
    if isinstance(allow_raw, tuple):
        allow_list: list[object] = list(allow_raw)
    elif isinstance(allow_raw, list):
        allow_list = list(allow_raw)
    else:
        raise SpecConfigError("[mcp].allow_from_user must be a list of strings")
    cleaned: list[str] = []
    for item in allow_list:
        if not isinstance(item, str):
            raise SpecConfigError("[mcp].allow_from_user must be a list of strings")
        stripped = item.strip()
        if stripped:
            cleaned.append(stripped)
    return McpConfig(allow_from_user=tuple(cleaned))


def _parse_autopilot_section(payload: object) -> AutopilotConfig:
    if not isinstance(payload, dict):
        return AutopilotConfig()
    return AutopilotConfig(
        container_default_enabled=bool(payload.get("container_default_enabled", False)),
        worktree_memory_mb=max(256, int(payload.get("worktree_memory_mb", 1536))),
        clone_memory_mb=max(256, int(payload.get("clone_memory_mb", 2048))),
        container_memory_mb=max(256, int(payload.get("container_memory_mb", 3072))),
        container_bridge_endpoint_threshold=max(
            1, int(payload.get("container_bridge_endpoint_threshold", 950))
        ),
        container_capacity_recheck_seconds=max(
            1.0, float(payload.get("container_capacity_recheck_seconds", 30.0))
        ),
        lock_backoff_base_seconds=max(1.0, float(payload.get("lock_backoff_base_seconds", 30.0))),
        lock_backoff_max_seconds=max(1.0, float(payload.get("lock_backoff_max_seconds", 1800.0))),
        operator_grace_seconds=max(0.0, float(payload.get("operator_grace_seconds", 600.0))),
    )


def _redact_url_credentials(url: str) -> str:
    """Return ``url`` with any user:password component stripped.

    Coordinator URLs are typically plain ``https://host[:port]`` but operators
    may embed credentials. We never want to surface those in diagnostics.
    """
    if not url:
        return ""
    # Find scheme separator
    scheme_idx = url.find("://")
    if scheme_idx == -1:
        return url
    scheme = url[: scheme_idx + 3]
    rest = url[scheme_idx + 3 :]
    at_idx = rest.find("@")
    slash_idx = rest.find("/")
    # Only strip an "@" that lives in the authority section (before any path).
    if at_idx != -1 and (slash_idx == -1 or at_idx < slash_idx):
        rest = rest[at_idx + 1 :]
    return scheme + rest


def _default_machine_id() -> str:
    """Hostname-derived stable default identifier for this machine."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    host = (host or "").strip()
    return host or "unknown-host"


def _parse_coordination_section(payload: object) -> CoordinationConfig:
    if not isinstance(payload, dict):
        return CoordinationConfig()
    return CoordinationConfig(
        backend=str(payload.get("backend", "")).strip(),
        url=str(payload.get("url", "")).strip(),
        repo_id=str(payload.get("repo_id", "")).strip(),
        machine_id=str(payload.get("machine_id", "")).strip(),
        token=str(payload.get("token", "")),
    )


def _merge_coordination(
    base: CoordinationConfig,
    overlay: CoordinationConfig,
) -> CoordinationConfig:
    """Layer ``overlay`` on top of ``base``; non-empty overlay fields win."""
    return CoordinationConfig(
        backend=overlay.backend or base.backend,
        url=overlay.url or base.url,
        repo_id=overlay.repo_id or base.repo_id,
        machine_id=overlay.machine_id or base.machine_id,
        token=overlay.token or base.token,
    )


def _merge_local_execution_payload(base: object, overlay: object) -> object:
    """Overlay .spec.local.toml [execution] values onto committed config."""
    if not isinstance(overlay, dict):
        return base
    if not isinstance(base, dict):
        return deepcopy(overlay)

    merged = deepcopy(base)
    for key, value in overlay.items():
        if key == "container" and isinstance(value, dict):
            base_container = merged.get("container", {})
            if isinstance(base_container, dict):
                container = deepcopy(base_container)
                container.update(deepcopy(value))
                merged["container"] = container
            else:
                merged["container"] = deepcopy(value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _coordination_from_env(env: dict[str, str] | None = None) -> CoordinationConfig:
    src = env if env is not None else os.environ
    return CoordinationConfig(
        backend=str(src.get(_ENV_COORDINATION_BACKEND, "")).strip(),
        url=str(src.get(_ENV_COORDINATION_URL, "")).strip(),
        repo_id=str(src.get(_ENV_COORDINATION_REPO_ID, "")).strip(),
        machine_id=str(src.get(_ENV_COORDINATION_MACHINE_ID, "")).strip(),
        token=str(src.get(_ENV_COORDINATION_TOKEN, "")),
    )


def _finalize_coordination(coordination: CoordinationConfig) -> CoordinationConfig:
    """Apply defaults that depend on the resolved configuration."""
    if not coordination.machine_id:
        return replace(coordination, machine_id=_default_machine_id())
    return coordination


def load_spec_runtime_config(
    *,
    require: bool = True,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> SpecRuntimeConfig:
    path = config_path or _config_path()
    if not path.is_file():
        if require:
            raise SpecConfigNotFoundError(
                f"This repo is not configured for spec. Run 'spec init' to get started.\n(Expected config file: {path})"
            )
        return replace(
            SpecRuntimeConfig(),
            coordination=_finalize_coordination(
                _merge_coordination(
                    CoordinationConfig(),
                    _coordination_from_env(env),
                )
            ),
        )

    raw = tomllib.loads(path.read_text())
    local_path = path.parent / _LOCAL_CONFIG_FILENAME
    local_raw: dict[str, object] = {}
    if local_path.is_file():
        try:
            local_raw = tomllib.loads(local_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            local_raw = {}

    paths_payload = raw.get("paths", {})
    retry_payload = raw.get("retry", {})
    agents_payload = raw.get("agents", {})
    verify_payload = raw.get("verify", {})
    implement_payload = raw.get("implement", {})
    update_payload = raw.get("update", {})
    readiness_payload = raw.get("readiness", {})
    autopilot_payload = raw.get("autopilot", {})
    execution_payload = _merge_local_execution_payload(
        raw.get("execution", None),
        local_raw.get("execution", None),
    )
    mcp_payload = raw.get("mcp", None)
    gate_payloads = verify_payload.get("gates", [])

    execution = _parse_execution_section(execution_payload)
    autopilot = _parse_autopilot_section(autopilot_payload)
    env_source = env if env is not None else os.environ
    if (
        str(env_source.get("SPEC_ACTOR", "")).strip() == "autopilot"
        and autopilot.container_default_enabled
        and not execution.backend_explicit
    ):
        execution = replace(execution, backend="container")

    paths = SpecPathConfig(
        specs_dir=str(paths_payload.get("specs_dir", "specs")).strip() or "specs",
        task_specs_dir=(str(paths_payload.get("task_specs_dir", "specs/tasks")).strip() or "specs/tasks"),
        state_dir=str(paths_payload.get("state_dir", ".spec-state")).strip() or ".spec-state",
        worktrees_dir=(str(paths_payload.get("worktrees_dir", ".worktrees")).strip() or ".worktrees"),
    )
    gates: list[VerifyGateConfig] = []
    for gate in gate_payloads:
        if not isinstance(gate, dict):
            continue
        name = str(gate.get("name", "")).strip()
        command = str(gate.get("command", "")).strip()
        if not name or not command:
            continue
        gates.append(
            VerifyGateConfig(
                name=name,
                command=command,
                parallel=bool(gate.get("parallel", False)),
                role=str(gate.get("role", "")).strip(),
            )
        )
    # Only fall back to hardcoded defaults when there is NO [verify] section
    # at all. An explicit [verify] section with no [[verify.gates]] means the
    # user intentionally has no gates — do not backfill.
    if not gates and "verify" not in raw:
        gates = list(SpecRuntimeConfig.verify_gates)

    allowed_agents = tuple(
        str(item).strip() for item in agents_payload.get("allowed", ("claude", "codex")) if str(item).strip()
    ) or ("claude", "codex")

    bootstrap_payload = raw.get("bootstrap", {})
    bootstrap_install_command = str(bootstrap_payload.get("install_command", "")).strip()
    bootstrap_cache_payload = bootstrap_payload.get("cache", {})
    bootstrap_cache = BootstrapCacheConfig()
    if isinstance(bootstrap_cache_payload, dict):
        cache_inputs = tuple(
            str(item).strip()
            for item in bootstrap_cache_payload.get("inputs", ())
            if str(item).strip()
        )
        bootstrap_cache = BootstrapCacheConfig(
            enabled=bool(bootstrap_cache_payload.get("enabled", False)),
            command=str(bootstrap_cache_payload.get("command", "")).strip() or bootstrap_install_command,
            inputs=cache_inputs,
        )

    base_coordination = _parse_coordination_section(raw.get("coordination", {}))

    local_coordination = CoordinationConfig()
    if local_raw:
        local_coordination = _parse_coordination_section(local_raw.get("coordination", {}))

    env_coordination = _coordination_from_env(env)
    coordination = _merge_coordination(
        _merge_coordination(base_coordination, local_coordination),
        env_coordination,
    )
    coordination = _finalize_coordination(coordination)

    return SpecRuntimeConfig(
        paths=paths,
        base_ref=str(raw.get("base_ref", "origin/master")).strip() or "origin/master",
        retry_cap=max(1, int(retry_payload.get("cap", 20))),
        no_progress_retry_threshold=max(
            1,
            int(retry_payload.get("no_progress_retry_threshold", 2)),
        ),
        verify_gates=tuple(gates),
        agents=AgentConfig(
            default=str(agents_payload.get("default", "claude")).strip() or "claude",
            review_default=str(agents_payload.get("review_default", "")).strip(),
            allowed=allowed_agents,
        ),
        bootstrap_install_command=bootstrap_install_command,
        bootstrap_cache=bootstrap_cache,
        implement=ImplementConfig(
            setup_command=str(implement_payload.get("setup_command", "")).strip(),
            teardown_command=str(implement_payload.get("teardown_command", "")).strip(),
        ),
        update=UpdateConfig(
            check_enabled=bool(update_payload.get("check_enabled", True)),
        ),
        readiness=ReadinessConfig(
            require_forge_checks=bool(readiness_payload.get("require_forge_checks", True)),
        ),
        autopilot=autopilot,
        execution=execution,
        coordination=coordination,
        mcp=_parse_mcp_section(mcp_payload),
    )


def load_repo_spec_runtime_config(repo_root: Path, *, require: bool = False) -> SpecRuntimeConfig:
    return load_spec_runtime_config(require=require, config_path=repo_root / ".spec.toml")


def resolve_specs_root(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> Path:
    runtime_config = config or load_repo_spec_runtime_config(repo_root)
    return repo_root / runtime_config.paths.specs_dir


def resolve_spec_path(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> Path:
    return resolve_specs_root(repo_root, config=config) / f"{spec_id}.md"
