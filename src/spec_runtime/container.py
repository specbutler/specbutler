"""Container bootstrap, diagnostics, and smoke-test commands."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import BootstrapCacheConfig, SpecRuntimeConfig, load_repo_spec_runtime_config
from .execution_backend import (
    CommandRequest,
    CommandResult,
    ExecutionBackend,
    WorkspaceHandle,
    get_execution_backend,
    host_spec_runtime_source_id,
    host_spec_runtime_version,
)
from .process_supervisor import run as run_supervised
from .source_repository import runtime_repository_https_url

WORKER_DOCKERFILE_TEMPLATE = """\
# syntax=docker/dockerfile:1.7
FROM python:3.12-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        bash \\
        build-essential \\
        ca-certificates \\
        curl \\
        git \\
        gnupg \\
        jq \\
        openssh-client \\
        pkg-config \\
        python3-dev \\
        python3-pip \\
        python3-venv \\
    && mkdir -p /etc/apt/keyrings \\
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \\
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \\
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \\
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \\
    && chmod go+r /etc/apt/keyrings/nodesource.gpg \\
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
        > /etc/apt/sources.list.d/github-cli.list \\
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \\
        > /etc/apt/sources.list.d/nodesource.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends gh nodejs \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip

# The backend build wrapper may run [bootstrap.cache].command here after
# dependency inputs are copied. [bootstrap].install_command runs later in the
# full workspace.
WORKDIR /workspace/bootstrap/source

{spec_install_block}

{agent_install_block}

WORKDIR /workspace/source
CMD ["bash"]
"""


AGENT_NPM_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    remediation: tuple[str, ...] = ()


@dataclass(frozen=True)
class GcResource:
    kind: str
    resource_id: str
    name: str
    reason: str
    legacy: bool = False


_LEGACY_RESOURCE_RE = re.compile(r"^spec-[0-9a-f]{16}(?:[-_].+)?$")


class _SubprocessContainerRunner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return run_supervised(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


def container_image_source(config: SpecRuntimeConfig, repo_root: Path) -> str:
    container = config.execution.container
    if container.image:
        return f"image:{container.image}"
    dockerfile = Path(container.dockerfile).expanduser()
    if not dockerfile.is_absolute():
        dockerfile = repo_root / dockerfile
    if dockerfile.is_file():
        return f"dockerfile:{container.dockerfile}"
    return f"missing-dockerfile:{container.dockerfile}"


def cmd_container(args: argparse.Namespace) -> int:
    command = getattr(args, "container_command", "")
    if command == "doctor":
        return cmd_doctor(args)
    if command == "init":
        return cmd_init(args)
    if command == "smoke":
        return cmd_smoke(args)
    if command == "gc":
        return cmd_gc(args)
    args._container_parser.print_help()
    return 1


def cmd_gc(args: argparse.Namespace) -> int:
    repo_root = Path(getattr(args, "repo_root", "") or Path.cwd()).resolve()
    config = load_repo_spec_runtime_config(repo_root)
    runner = _SubprocessContainerRunner()
    resources = discover_gc_resources(
        repo_root,
        config.execution.container.engine or "docker",
        runner=runner,
        state_dir=config.paths.state_dir,
    )
    if not resources:
        print("No stale spec container resources found.")
        return 0
    apply = bool(getattr(args, "apply", False))
    for resource in resources:
        marker = "legacy match; " if resource.legacy else ""
        action = "removing" if apply else "would remove"
        print(f"{action} {resource.kind} {resource.name}: {marker}{resource.reason}")
        if not apply:
            continue
        if resource.kind == "container":
            argv = [config.execution.container.engine or "docker", "rm", "-f", resource.resource_id]
        elif resource.kind == "volume":
            argv = [config.execution.container.engine or "docker", "volume", "rm", "-f", resource.name]
        else:
            argv = [config.execution.container.engine or "docker", "network", "rm", resource.name]
        result = runner.run(argv, cwd=repo_root)
        if result.returncode != 0:
            print(f"failed to remove {resource.kind} {resource.name}: {_one_line(result.stderr)}", file=sys.stderr)
            return 1
    if not apply:
        print("Re-run with --apply to remove these resources.")
    return 0


def discover_gc_resources(
    repo_root: Path,
    engine: str,
    *,
    runner: object,
    state_dir: str = ".spec-state",
) -> list[GcResource]:
    """Return stale owned resources while conservatively protecting live runs."""
    active_run_ids = _active_run_ids(repo_root, state_dir=state_dir)
    discovered: list[GcResource] = []
    commands = {
        "container": [engine, "ps", "-a", "--format", "{{json .}}"],
        "volume": [engine, "volume", "ls", "--format", "{{json .}}"],
        "network": [engine, "network", "ls", "--format", "{{json .}}"],
    }
    for kind, argv in commands.items():
        result = runner.run(argv, cwd=repo_root)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            name = str(item.get("Names") or item.get("Name") or "").lstrip("/")
            resource_id = str(item.get("ID") or item.get("Id") or name)
            labels = _parse_engine_labels(str(item.get("Labels") or item.get("Label") or ""))
            owned = labels.get("spec.owner") == "spec-runtime"
            legacy = not owned and bool(_LEGACY_RESOURCE_RE.fullmatch(name))
            if not owned and not legacy:
                continue
            run_id = labels.get("spec.run_id", "")
            if run_id and run_id in active_run_ids:
                continue
            if kind == "container":
                state = str(item.get("State") or item.get("Status") or "").lower()
                stopped = state.startswith(("created", "exited", "dead"))
                # Detect workers via the spec.phase=execution label rather than the
                # Command column, which Docker truncates (e.g. "sh -lc 'sleep infin…")
                # and so never contained the literal "sleep infinity" match.
                worker = owned and labels.get("spec.phase") == "execution"
                if not stopped and not worker:
                    continue
                reason = "container is stopped" if stopped else "running worker of finished run"
            else:
                reason = "owning run is finished or missing" if owned else "strict legacy spec name"
            discovered.append(GcResource(kind, resource_id, name, reason, legacy))
    order = {"container": 0, "volume": 1, "network": 2}
    return sorted(discovered, key=lambda item: (order[item.kind], item.name))


def _parse_engine_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part in raw.split(","):
        key, separator, value = part.partition("=")
        if separator:
            labels[key.strip()] = value.strip()
    return labels


def _active_run_ids(repo_root: Path, *, state_dir: str = ".spec-state") -> set[str]:
    from .control_plane import CanonicalRunStatus
    from .spec_status import project_run_record_status

    active: set[str] = set()
    state_root = repo_root / state_dir
    runs_dir = state_root / "runs"
    for path in runs_dir.glob("*.json") if runs_dir.is_dir() else ():
        try:
            run = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        projection = project_run_record_status(runs_dir, run)
        protected = projection is not None and projection.status not in {
            CanonicalRunStatus.MERGED,
            CanonicalRunStatus.NOT_STARTED,
            CanonicalRunStatus.PASSED,
            CanonicalRunStatus.STALE,
        }
        if protected and run.get("run_id"):
            active.add(str(run["run_id"]))
    active_path = state_root / "autopilot" / "active.json"
    try:
        payload = json.loads(active_path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    if isinstance(payload, dict):
        for entry in payload.values():
            if isinstance(entry, dict) and entry.get("run_id"):
                active.add(str(entry["run_id"]))
    return active


def cmd_doctor(args: argparse.Namespace) -> int:
    repo_root = Path(getattr(args, "repo_root", "") or Path.cwd()).resolve()
    config = load_repo_spec_runtime_config(repo_root)
    checks = run_doctor_checks(repo_root, config)
    for check in checks:
        prefix = "ok" if check.ok else "fail"
        print(f"[{prefix}] {check.name}: {check.detail}")
        for line in check.remediation:
            print(f"      {line}")
    return 0 if all(check.ok for check in checks) else 1


def run_doctor_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    *,
    runner: object | None = None,
    system_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    runner = runner or _SubprocessContainerRunner()
    system_name = system_name or platform.system()
    if env is None:
        env = os.environ
    container = config.execution.container
    engine = container.engine or "docker"
    checks: list[CheckResult] = []

    engine_path = shutil.which(engine)
    checks.append(
        CheckResult(
            "engine binary",
            bool(engine_path),
            f"{engine} at {engine_path}" if engine_path else f"{engine!r} was not found on PATH",
            (
                "Install Docker Engine/Desktop, Podman, Colima, OrbStack, or Rancher Desktop.",
                'Or set [execution.container].engine = "podman" in .spec.local.toml.',
            )
            if not engine_path
            else (),
        )
    )
    if not engine_path:
        checks.extend(_config_checks(repo_root, config, env=env))
        return checks

    version = runner.run([engine, "--version"], cwd=repo_root)
    checks.append(
        CheckResult(
            "engine version",
            version.returncode == 0,
            (version.stdout or version.stderr or "").strip() or f"{engine} --version exited {version.returncode}",
        )
    )

    info = runner.run([engine, "info"], cwd=repo_root)
    checks.append(
        CheckResult(
            "daemon/API",
            info.returncode == 0,
            "engine API is reachable" if info.returncode == 0 else _one_line(info.stderr or info.stdout),
            _docker_permission_remediation()
            if _looks_like_docker_permission_failure(engine, info, system_name)
            else (),
        )
    )

    if engine == "docker" and system_name == "Linux":
        checks.append(_docker_socket_check())

    tiny = runner.run([engine, "run", "--rm", "hello-world"], cwd=repo_root, timeout=60)
    checks.append(
        CheckResult(
            "disposable container",
            tiny.returncode == 0,
            "tiny container completed" if tiny.returncode == 0 else _one_line(tiny.stderr or tiny.stdout),
        )
    )
    checks.extend(_config_checks(repo_root, config, env=env))
    return checks


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = Path(getattr(args, "repo_root", "") or Path.cwd()).resolve()
    config = load_repo_spec_runtime_config(repo_root)
    force = bool(getattr(args, "force", False))
    install_agents = not bool(getattr(args, "no_agents", False))
    container = config.execution.container
    dockerfile = Path(container.dockerfile).expanduser()
    if not dockerfile.is_absolute():
        dockerfile = repo_root / dockerfile
    if container.image and not force:
        print(
            f"Configured worker image already exists: {container.image}. Use --force to create bootstrap files anyway."
        )
        return 0
    if dockerfile.exists() and not force:
        print(f"Configured worker Dockerfile already exists: {dockerfile}. Use --force to overwrite it.")
        return 0

    try:
        source_repository_url = runtime_repository_https_url(
            override=str(getattr(args, "source_repository", "") or "")
        )
    except ValueError as exc:
        print(f"Error: invalid --source-repository: {exc}", file=sys.stderr)
        return 1
    if not source_repository_url:
        print(
            "Error: could not resolve the Spec Butler source repository. "
            "Pass --source-repository https://HOST/OWNER/REPO.git.",
            file=sys.stderr,
        )
        return 1

    print("spec container init will create/update:")
    print(f"  {dockerfile.relative_to(repo_root) if dockerfile.is_relative_to(repo_root) else dockerfile}")
    print("  .spec.toml commented container defaults if absent")

    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    agent_versions = {
        agent: version
        for agent in config.agents.allowed
        if (version := _detect_agent_cli_version(agent))
    }
    dockerfile.write_text(
        render_worker_dockerfile(
            config.agents.allowed if install_agents else (),
            build_ssh=bool(config.execution.container.build_ssh),
            agent_versions=agent_versions,
            source_repository_url=source_repository_url,
        )
    )
    _append_config_snippet(repo_root / ".spec.toml")
    print("Container bootstrap files are ready.")
    return 0


def _detect_agent_cli_version(agent: str) -> str:
    """Return a package-compatible version from an installed agent CLI."""
    if agent not in AGENT_NPM_PACKAGES or not shutil.which(agent):
        return ""
    try:
        result = subprocess.run(
            [agent, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    match = re.search(
        r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
        result.stdout or result.stderr,
    )
    return match.group(1) if match else ""


def render_worker_dockerfile(
    allowed_agents: tuple[str, ...],
    *,
    build_ssh: bool = False,
    agent_versions: Mapping[str, str] | None = None,
    source_repository_url: str = "",
) -> str:
    repository_url = runtime_repository_https_url(override=source_repository_url)
    if not repository_url:
        raise ValueError("could not resolve the Spec Butler source repository")
    spec_install_block = _render_spec_install_block(
        build_ssh=build_ssh,
        source_repository_url=repository_url,
    )
    versions = agent_versions or {}
    packages = []
    for agent in dict.fromkeys(allowed_agents):
        package = AGENT_NPM_PACKAGES.get(agent)
        if package is None:
            continue
        version = str(versions.get(agent, "")).strip()
        packages.append(f"{package}@{version}" if version else package)
    if packages:
        package_args = " ".join(packages)
        agent_install_block = "\n".join(
            (
                "# Install configured agent CLIs without baking secrets into the image.",
                f"RUN npm install -g {package_args}",
            )
        )
    else:
        agent_install_block = "\n".join(
            (
                "# Extension point: install agent CLIs here without baking secrets into the image.",
                "# Examples:",
                "# RUN npm install -g @anthropic-ai/claude-code",
                "# RUN npm install -g @openai/codex",
            )
        )
    return WORKER_DOCKERFILE_TEMPLATE.format(
        spec_install_block=spec_install_block,
        agent_install_block=agent_install_block,
    )


def _render_spec_install_block(*, build_ssh: bool, source_repository_url: str) -> str:
    # Referencing the version arg inside the install RUN makes Docker's layer
    # cache key on it, so the layer rebuilds exactly when the host spec
    # version changes instead of serving an outdated snapshot indefinitely.
    # The build wrapper always passes the current host version.
    cache_bust = (
        "ARG SPEC_BUTLER_VERSION=unpinned",
        f"ARG SPEC_BUTLER_REPOSITORY_URL={source_repository_url}",
        "# The build passes the host spec version; referencing it below busts",
        "# this layer's cache on spec upgrades.",
    )
    if build_ssh:
        return "\n".join(
            (
                *cache_bust,
                "# Trust GitHub for private dependencies fetched by the optional cache step.",
                "RUN mkdir -p -m 0700 /root/.ssh \\",
                "    && curl --fail --silent --show-error --location https://api.github.com/meta \\",
                "    | jq -r '.ssh_keys[] | \"github.com \\(.)\"' > /root/.ssh/known_hosts \\",
                "    && test -s /root/.ssh/known_hosts",
                "# spec itself is public; SSH forwarding is reserved for project dependencies.",
                "RUN echo \"specbutler ${SPEC_BUTLER_VERSION}\" \\",
                "    && python -m pip install --no-cache-dir \\",
                '    "specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"',
            )
        )
    return "\n".join(
        (
            *cache_bust,
            "# Install the spec CLI used by container smoke checks and completion reporting.",
            "RUN echo \"specbutler ${SPEC_BUTLER_VERSION}\" \\",
            "    && python -m pip install --no-cache-dir \\",
            '    "specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"',
            "",
            "# For private project dependencies in [bootstrap.cache].command, set",
            "# [execution.container].build_ssh and rerun `spec container init --force`.",
        )
    )


def cmd_smoke(args: argparse.Namespace) -> int:
    repo_root = Path(getattr(args, "repo_root", "") or Path.cwd()).resolve()
    config = load_repo_spec_runtime_config(repo_root)
    from .autopilot import execution_config_for_autopilot

    run_bootstrap = not getattr(args, "no_bootstrap", False)
    smoke_config = replace(config, execution=execution_config_for_autopilot(config))
    if not run_bootstrap:
        smoke_config = replace(
            smoke_config,
            bootstrap_install_command="",
            bootstrap_cache=BootstrapCacheConfig(),
        )
    backend = get_execution_backend(smoke_config)
    if backend.identity.backend != "container":
        print(
            'Error: spec container smoke requires [execution].backend = "container" '
            "or an autopilot container rollout policy.",
            file=sys.stderr,
        )
        return 1
    return run_smoke(
        repo_root,
        smoke_config,
        backend=backend,
        run_bootstrap=run_bootstrap,
        run_verify=getattr(args, "verify_gates", False),
        timeout_seconds=float(getattr(args, "timeout", 300)),
    )


def run_smoke(
    repo_root: Path,
    config: SpecRuntimeConfig,
    *,
    backend: ExecutionBackend,
    run_bootstrap: bool = True,
    run_verify: bool = False,
    timeout_seconds: float = 300,
) -> int:
    timings: dict[str, float] = {}
    run_id = f"container-smoke-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    branch = _current_branch(repo_root)
    workspace: WorkspaceHandle | None = None
    overall_start = time.monotonic()
    try:
        start = time.monotonic()
        workspace = backend.prepare_workspace(
            run_id=run_id,
            spec_id="container-smoke",
            branch=branch,
            repo_root=repo_root,
            base_ref=config.base_ref,
        )
        timings["workspace_prepare"] = time.monotonic() - start
        print(f"Prepared disposable container backend workspace: {workspace.path}")
        if workspace.metadata.get("logs_path"):
            print(f"Logs: {workspace.metadata['logs_path']}")

        commands = [
            ("git", ["git", "--version"]),
            ("python", ["python", "--version"]),
            ("gh", ["gh", "--version"]),
        ]
        worker_spec_result = _run_smoke_command(
            backend,
            workspace,
            "spec",
            ["spec", "--version"],
            timeout_seconds,
            timings,
        )
        host_version = host_spec_runtime_version()
        worker_version = (worker_spec_result.stdout or worker_spec_result.stderr).strip().splitlines()[0]
        if host_version != "unknown" and worker_version != host_version:
            raise RuntimeError(
                "spec version mismatch: "
                f"host={host_version}, worker={worker_version}. "
                "Rebuild the worker image with the current generated Dockerfile or select a matching image."
            )
        worker_source_result = _run_smoke_command(
            backend,
            workspace,
            "spec-source",
            ["spec", "--source-id"],
            timeout_seconds,
            timings,
        )
        host_source_id = host_spec_runtime_source_id()
        worker_source_id = (
            worker_source_result.stdout or worker_source_result.stderr
        ).strip().splitlines()[0]
        if "@" in host_source_id and worker_source_id != host_source_id:
            raise RuntimeError(
                "spec source identity mismatch: "
                f"host={host_source_id}, worker={worker_source_id}. "
                "Install the host from a tagged release or build a worker from the exact host source."
            )
        for agent in config.agents.allowed:
            commands.append(
                (
                    f"agent:{agent}",
                    [
                        "sh",
                        "-lc",
                        f"if command -v {agent} >/dev/null 2>&1; then {agent} --version; else echo '{agent} not installed'; fi",
                    ],
                )
            )

        for name, argv in commands:
            _run_smoke_command(backend, workspace, name, argv, timeout_seconds, timings)

        if run_verify:
            for gate in config.verify_gates:
                _run_smoke_command(
                    backend,
                    workspace,
                    f"verify:{gate.name}",
                    ["sh", "-lc", gate.command],
                    timeout_seconds,
                    timings,
                )
        return 0
    except Exception as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if workspace is not None:
            start = time.monotonic()
            try:
                # Smoke workspaces are disposable by definition. Bootstrap and
                # verify commands commonly create normal untracked artifacts
                # such as egg-info and __pycache__, which must not trip the
                # implementation-work loss guard during smoke cleanup.
                backend.cleanup(workspace, allow_unpushed_work=True)
            finally:
                timings["cleanup"] = time.monotonic() - start
        timings["total"] = time.monotonic() - overall_start
        print("Timings:")
        for name, value in timings.items():
            print(f"  {name}: {value:.3f}s")


def _run_smoke_command(
    backend: ExecutionBackend,
    workspace: WorkspaceHandle,
    name: str,
    argv: list[str],
    timeout_seconds: float,
    timings: dict[str, float],
) -> CommandResult:
    start = time.monotonic()
    result = backend.run_command(CommandRequest(argv=argv, cwd=workspace.path, timeout=timeout_seconds))
    timings[f"command:{name}"] = time.monotonic() - start
    if result.returncode != 0:
        raise RuntimeError(
            f"{name} check failed with exit {result.returncode}: {_one_line(result.stderr or result.stdout)}"
        )
    print(f"[ok] {name}: {_one_line(result.stdout or result.stderr)}")
    return result


def _config_checks(
    repo_root: Path,
    config: SpecRuntimeConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    if env is None:
        env = os.environ
    checks = []
    backend = config.execution.backend
    if config.autopilot.container_default_enabled and not config.execution.backend_explicit:
        backend_detail = "container via [autopilot].container_default_enabled"
        backend_ok = True
    else:
        backend_detail = (
            f"{backend} via .spec.toml" if config.execution.backend_explicit else f"{backend} implicit default"
        )
        backend_ok = backend == "container"
    checks.append(
        CheckResult(
            "backend selection",
            backend_ok,
            backend_detail,
            ('Set [execution].backend = "container" or enable [autopilot].container_default_enabled.',)
            if not backend_ok
            else (),
        )
    )
    source = container_image_source(config, repo_root)
    ok = source.startswith(("image:", "dockerfile:"))
    checks.append(
        CheckResult(
            "worker image source",
            ok,
            source,
            ("Run: spec container init",) if not ok else (),
        )
    )
    build_ssh_check = _build_ssh_agent_check(config.execution.container.build_ssh, env)
    if build_ssh_check is not None:
        checks.append(build_ssh_check)
    return checks


def _build_ssh_agent_check(build_ssh: str, env: Mapping[str, str]) -> CheckResult | None:
    if not build_ssh:
        return None
    remediation = (
        'Start an SSH agent: eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519',
        "Or unset [execution.container].build_ssh to disable BuildKit SSH forwarding.",
    )
    sock = env.get("SSH_AUTH_SOCK", "")
    if not sock:
        return CheckResult(
            "build SSH agent",
            False,
            "SSH_AUTH_SOCK unset",
            remediation,
        )
    sock_path = Path(sock)
    try:
        st = sock_path.stat()
    except FileNotFoundError:
        return CheckResult(
            "build SSH agent",
            False,
            f"SSH_AUTH_SOCK={sock} not found",
            remediation,
        )
    except OSError as exc:
        return CheckResult(
            "build SSH agent",
            False,
            f"SSH_AUTH_SOCK={sock}: {exc.strerror or exc}",
            remediation,
        )
    if not stat.S_ISSOCK(st.st_mode):
        return CheckResult(
            "build SSH agent",
            False,
            f"{sock} is not a socket",
            remediation,
        )
    return CheckResult(
        "build SSH agent",
        True,
        f"SSH_AUTH_SOCK={sock}",
    )


def _append_config_snippet(path: Path) -> None:
    text = path.read_text() if path.exists() else ""
    if "[execution.container]" in text or "spec container init snippet" in text:
        return
    snippet = """

# spec container init snippet: committed non-secret defaults.
# Put machine-specific engine/image overrides in .spec.local.toml.
#[execution]
#backend = "container"
#
#[execution.container]
#engine = "docker"
#dockerfile = ".spec/worker.Dockerfile"
# Forward the host SSH agent into image builds (see Dockerfile RUN --mount=type=ssh example).
#build_ssh = "default"
#
#[bootstrap.cache]
#enabled = false
# Optional cache-layer optimization. If enabled without a command, spec uses
# [bootstrap].install_command against copied dependency inputs.
#command = "make install"
#inputs = ["Makefile", "requirements.txt", "frontend/package.json", "frontend/package-lock.json"]
"""
    path.write_text(text.rstrip() + snippet + "\n")


def _docker_socket_check() -> CheckResult:
    if os.name == "nt":
        return CheckResult("docker socket", False, "Unix docker socket is not available on native Windows")
    import grp

    socket_path = Path("/var/run/docker.sock")
    if not socket_path.exists():
        return CheckResult("docker socket", False, "missing /var/run/docker.sock")
    stat = socket_path.stat()
    group_name = str(stat.st_gid)
    try:
        group_name = grp.getgrgid(stat.st_gid).gr_name
    except KeyError:
        pass
    groups = {group.gr_name for group in grp.getgrall() if getpass.getuser() in group.gr_mem}
    try:
        primary = grp.getgrgid(os.getgid()).gr_name
        groups.add(primary)
    except KeyError:
        pass
    ok = group_name in groups or os.access(socket_path, os.R_OK | os.W_OK)
    return CheckResult(
        "docker socket",
        ok,
        f"{socket_path} group={group_name}; user groups={','.join(sorted(groups)) or '-'}",
        _docker_permission_remediation() if not ok and group_name == "docker" else (),
    )


def _docker_permission_remediation() -> tuple[str, ...]:
    user = getpass.getuser()
    return (
        "Docker group membership is root-equivalent; only add trusted users.",
        f"sudo usermod -aG docker {user}",
        "newgrp docker",
        "sudo systemctl enable --now docker",
        "Then start a fresh shell and rerun: spec container doctor",
    )


def _looks_like_docker_permission_failure(
    engine: str,
    result: subprocess.CompletedProcess[str],
    system_name: str,
) -> bool:
    if engine != "docker" or system_name != "Linux" or result.returncode == 0:
        return False
    text = f"{result.stdout}\n{result.stderr}".lower()
    return "permission denied" in text and "docker" in text


def _one_line(text: str) -> str:
    return " ".join((text or "").strip().split())[:240] or "-"


def _current_branch(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return f"container-smoke/{socket.gethostname()}"
    branch = result.stdout.strip()
    if result.returncode == 0 and branch and branch != "HEAD":
        return branch
    return f"container-smoke/{socket.gethostname()}"
