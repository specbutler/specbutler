from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime import container
from spec_runtime.config import (
    AgentConfig,
    AutopilotConfig,
    BootstrapCacheConfig,
    ContainerExecutionConfig,
    ExecutionConfig,
    SpecRuntimeConfig,
)
from spec_runtime.execution_backend import (
    CommandRequest,
    WorkspaceHandle,
    host_spec_runtime_source_id,
    host_spec_runtime_version,
)


class FakeRunner:
    def __init__(self, *, permission_failure: bool = False, engine: str = "docker"):
        self.calls: list[list[str]] = []
        self.permission_failure = permission_failure
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
        del cwd, env, input_text, timeout
        self.calls.append(argv)
        if argv == [self.engine, "--version"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.engine} version 1\n", "")
        if argv == [self.engine, "info"] and self.permission_failure:
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "permission denied while trying to connect to the Docker daemon socket",
            )
        if argv == [self.engine, "info"]:
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        if argv[:2] == [self.engine, "run"]:
            return subprocess.CompletedProcess(argv, 0, "hello\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeBackend:
    def __init__(self, *, fail_cleanup: bool = False):
        self.identity = type("Identity", (), {"backend": "container"})()
        self.commands: list[CommandRequest] = []
        self.cleaned = False
        self.cleanup_allow_unpushed_work = False
        self.fail_cleanup = fail_cleanup
        self.workspace = WorkspaceHandle(
            path=Path("/tmp/spec-smoke/source"),
            outbox_path=Path("/tmp/spec-smoke/outbox"),
            branch="main",
            backend="container",
            metadata={"logs_path": "/tmp/spec-smoke/logs"},
        )

    def prepare_workspace(self, **kwargs) -> WorkspaceHandle:  # noqa: ANN003
        self.prepare_kwargs = kwargs
        return self.workspace

    def run_command(self, request: CommandRequest):
        self.commands.append(request)
        if request.argv == ["spec", "--version"]:
            output = host_spec_runtime_version()
        elif request.argv == ["spec", "--source-id"]:
            output = host_spec_runtime_source_id()
        else:
            output = "ok"
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": f"{output}\n", "stderr": "", "argv": request.argv},
        )()

    def cleanup(self, workspace: WorkspaceHandle, *, allow_unpushed_work: bool = False) -> None:
        assert workspace is self.workspace
        self.cleaned = True
        self.cleanup_allow_unpushed_work = allow_unpushed_work
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")


class FakeGcRunner:
    def __init__(self, inventory: dict[str, list[dict[str, str]]]):
        self.inventory = inventory
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: Path, **kwargs):  # noqa: ANN003
        del cwd, kwargs
        self.calls.append(argv)
        kind = "container" if argv[1] == "ps" else argv[1]
        if "ls" in argv or argv[1] == "ps":
            output = "\n".join(json.dumps(item) for item in self.inventory.get(kind, []))
            return subprocess.CompletedProcess(argv, 0, output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _config(**overrides) -> SpecRuntimeConfig:
    execution = overrides.pop(
        "execution",
        ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(image="example/spec-worker:latest"),
        ),
    )
    return replace(SpecRuntimeConfig(), execution=execution, **overrides)


def test_container_gc_protects_active_and_unrelated_resources(tmp_path: Path) -> None:
    runs = tmp_path / ".spec-state" / "runs"
    runs.mkdir(parents=True)
    (runs / "active.json").write_text(json.dumps({"run_id": "active-run", "status": "running", "phase": "implement"}))
    active = tmp_path / ".spec-state" / "autopilot"
    active.mkdir()
    (active / "active.json").write_text(json.dumps({"feature": {"run_id": "active-run"}}))
    runner = FakeGcRunner(
        {
            "container": [
                {
                    "ID": "active",
                    "Names": "active",
                    "State": "exited",
                    "Labels": "spec.owner=spec-runtime,spec.run_id=active-run",
                },
                {
                    "ID": "stale",
                    "Names": "stale",
                    "State": "exited",
                    "Labels": "spec.owner=spec-runtime,spec.run_id=done-run",
                },
                {"ID": "other", "Names": "database", "State": "exited", "Labels": "team=product"},
                {"ID": "legacy", "Names": "spec-0123456789abcdef-worker", "State": "exited", "Labels": ""},
            ],
            "volume": [{"Name": "spec-0123456789abcdef-source", "Labels": ""}],
            "network": [{"ID": "net", "Name": "unrelated", "Labels": ""}],
        }
    )

    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    assert [(item.kind, item.name, item.legacy) for item in resources] == [
        ("container", "spec-0123456789abcdef-worker", True),
        ("container", "stale", False),
        ("volume", "spec-0123456789abcdef-source", True),
    ]


def test_container_gc_apply_removes_only_discovered_stale_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeGcRunner(
        {
            "container": [
                {
                    "ID": "stale-id",
                    "Names": "stale-worker",
                    "State": "exited",
                    "Labels": "spec.owner=spec-runtime,spec.run_id=finished",
                },
                {
                    "ID": "other-id",
                    "Names": "product-db",
                    "State": "exited",
                    "Labels": "team=product",
                },
            ]
        }
    )
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    result = container.cmd_gc(argparse.Namespace(repo_root=str(tmp_path), apply=True))

    assert result == 0
    assert ["docker", "rm", "-f", "stale-id"] in runner.calls
    assert not any("other-id" in call for call in runner.calls)


def test_container_gc_dry_run_lists_reasons_and_flags_legacy_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeGcRunner(
        {
            "container": [
                {
                    "ID": "stale-id",
                    "Names": "stale-worker",
                    "State": "exited",
                    "Labels": "spec.owner=spec-runtime,spec.run_id=finished",
                },
                {
                    "ID": "legacy-id",
                    "Names": "spec-0123456789abcdef-worker",
                    "State": "created",
                    "Labels": "",
                },
            ],
            "network": [
                {
                    "ID": "network-id",
                    "Name": "stale-network",
                    "Labels": "spec.owner=spec-runtime,spec.run_id=finished",
                }
            ],
        }
    )
    monkeypatch.setattr(container, "_SubprocessContainerRunner", lambda: runner)

    result = container.cmd_gc(argparse.Namespace(repo_root=str(tmp_path), apply=False))

    assert result == 0
    assert capsys.readouterr().out.splitlines() == [
        "would remove container spec-0123456789abcdef-worker: legacy match; container is stopped",
        "would remove container stale-worker: container is stopped",
        "would remove network stale-network: owning run is finished or missing",
        "Re-run with --apply to remove these resources.",
    ]
    assert not any(call[1:3] in (["rm", "-f"], ["volume", "rm"], ["network", "rm"]) for call in runner.calls)


def test_container_gc_discovers_running_worker_despite_truncated_command(tmp_path: Path) -> None:
    # Docker truncates the Command column with a Unicode ellipsis, so the literal
    # "sleep infinity" is never present. Detection must rely on labels instead.
    runner = FakeGcRunner(
        {
            "container": [
                {
                    "ID": "worker-id",
                    "Names": "spec-worker",
                    "State": "running",
                    "Command": "sh -lc 'sleep infin…",
                    "Labels": (
                        "spec.owner=spec-runtime,spec.run_id=finished,spec.phase=execution"
                    ),
                },
            ]
        }
    )

    resources = container.discover_gc_resources(tmp_path, "docker", runner=runner)

    assert [(item.kind, item.name, item.reason) for item in resources] == [
        ("container", "spec-worker", "running worker of finished run"),
    ]


def test_doctor_reports_healthy_docker(tmp_path: Path) -> None:
    config = _config()
    runner = FakeRunner()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=runner,
            system_name="Darwin",
        )

    assert all(check.ok for check in checks)
    assert ["docker", "info"] in runner.calls
    assert any(check.name == "worker image source" and check.detail.startswith("image:") for check in checks)


def test_doctor_reports_healthy_podman(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(engine="podman", image="worker:latest"),
        )
    )
    runner = FakeRunner(engine="podman")

    with patch("shutil.which", return_value="/usr/bin/podman"):
        checks = container.run_doctor_checks(tmp_path, config, runner=runner, system_name="Linux")

    assert all(check.ok for check in checks)
    assert ["podman", "run", "--rm", "hello-world"] in runner.calls


def test_doctor_missing_engine_binary_is_actionable(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value=None):
        checks = container.run_doctor_checks(tmp_path, config, runner=FakeRunner())

    engine = checks[0]
    assert engine.name == "engine binary"
    assert engine.ok is False
    assert "not found" in engine.detail
    assert any(".spec.local.toml" in line for line in engine.remediation)


def test_doctor_docker_permission_failure_prints_ubuntu_next_steps(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(permission_failure=True),
            system_name="Linux",
        )

    api = next(check for check in checks if check.name == "daemon/API")
    assert api.ok is False
    assert any("usermod -aG docker" in line for line in api.remediation)
    assert any("root-equivalent" in line for line in api.remediation)


def test_doctor_reports_missing_worker_dockerfile(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(image="", dockerfile=".spec/worker.Dockerfile"),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
        )

    source = next(check for check in checks if check.name == "worker image source")
    assert source.ok is False
    assert source.detail == "missing-dockerfile:.spec/worker.Dockerfile"
    assert source.remediation == ("Run: spec container init",)


def test_doctor_warns_when_build_ssh_set_without_agent(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is False
    assert ssh_check.detail == "SSH_AUTH_SOCK unset"
    assert any('eval "$(ssh-agent -s)"' in line for line in ssh_check.remediation)
    assert any("build_ssh" in line for line in ssh_check.remediation)


def test_doctor_passes_when_build_ssh_set_with_agent(tmp_path: Path) -> None:
    sock_path = tmp_path / "agent.sock"
    sock_path.touch()
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.object(container.stat, "S_ISSOCK", return_value=True),
    ):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={"SSH_AUTH_SOCK": str(sock_path)},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is True
    assert str(sock_path) in ssh_check.detail


def test_doctor_omits_build_ssh_check_when_unset(tmp_path: Path) -> None:
    config = _config()

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={},
        )

    assert not any(check.name == "build SSH agent" for check in checks)


def test_doctor_warns_when_build_ssh_socket_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sock"
    config = _config(
        execution=ExecutionConfig(
            backend="container",
            backend_explicit=True,
            container=ContainerExecutionConfig(
                image="example/spec-worker:latest",
                build_ssh="default",
            ),
        )
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        checks = container.run_doctor_checks(
            tmp_path,
            config,
            runner=FakeRunner(),
            system_name="Darwin",
            env={"SSH_AUTH_SOCK": str(missing)},
        )

    ssh_check = next(check for check in checks if check.name == "build SSH agent")
    assert ssh_check.ok is False
    assert "not found" in ssh_check.detail


def test_container_init_creates_files_and_preserves_existing_without_force(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="https://github.com/acme/spec.git",
    )

    versions = {"claude": "2.1.233", "codex": "0.147.0"}
    with patch(
        "spec_runtime.container._detect_agent_cli_version",
        side_effect=lambda agent: versions.get(agent, ""),
    ):
        assert container.cmd_init(args) == 0
    dockerfile = tmp_path / ".spec" / "worker.Dockerfile"
    dockerfile_text = dockerfile.read_text()
    assert "FROM python:3.12-bookworm" in dockerfile_text
    assert "nodejs" in dockerfile_text
    assert "npm" in dockerfile_text
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec.git" in dockerfile_text
    assert "@anthropic-ai/claude-code@2.1.233" in dockerfile_text
    assert "@openai/codex@0.147.0" in dockerfile_text
    assert 'sh -lc "$SPEC_BOOTSTRAP_INSTALL_COMMAND"' not in dockerfile_text
    assert not (tmp_path / ".spec" / ".dockerignore").exists()

    dockerfile.write_text("custom\n")
    assert container.cmd_init(args) == 0
    assert dockerfile.read_text() == "custom\n"


def test_container_init_installs_only_configured_agents(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[agents]
allowed = ["codex"]

[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "RUN npm install -g @openai/codex" in dockerfile_text
    assert "@anthropic-ai/claude-code" not in dockerfile_text


def test_container_init_reserves_build_ssh_for_project_dependencies(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
build_ssh = "default"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "python -m pip install --no-cache-dir" in dockerfile_text
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "https://api.github.com/meta" in dockerfile_text
    assert "test -s /root/.ssh/known_hosts" in dockerfile_text
    assert "git+ssh://" not in dockerfile_text


def test_container_init_can_skip_agent_installs(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False, no_agents=True)

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert (
        '"specbutler @ git+${SPEC_BUTLER_REPOSITORY_URL}@v${SPEC_BUTLER_VERSION}"'
        in dockerfile_text
    )
    assert "RUN npm install -g @anthropic-ai/claude-code @openai/codex" not in dockerfile_text
    assert "# RUN npm install -g @anthropic-ai/claude-code" in dockerfile_text
    assert "# RUN npm install -g @openai/codex" in dockerfile_text


def test_container_init_can_override_public_source_repository(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="git@github.com:acme/spec-fork.git",
    )

    assert container.cmd_init(args) == 0

    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec-fork.git" in dockerfile_text


def test_container_init_strips_source_repository_credentials_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="https://oauth2:secret@github.com/acme/spec.git",
    )

    assert container.cmd_init(args) == 0
    dockerfile_text = (tmp_path / ".spec" / "worker.Dockerfile").read_text()
    assert "oauth2:secret" not in dockerfile_text
    assert "ARG SPEC_BUTLER_REPOSITORY_URL=https://github.com/acme/spec.git" in dockerfile_text
    assert capsys.readouterr().err == ""


def test_container_init_rejects_invalid_source_repository_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        force=False,
        source_repository="file:///private/source",
    )

    assert container.cmd_init(args) == 1
    assert not (tmp_path / ".spec").exists()
    assert "invalid --source-repository" in capsys.readouterr().err


def test_container_init_fails_closed_when_source_repository_is_unknown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
dockerfile = ".spec/worker.Dockerfile"
"""
    )
    args = argparse.Namespace(repo_root=str(tmp_path), force=False)

    with patch("spec_runtime.container.runtime_repository_https_url", return_value=""):
        assert container.cmd_init(args) == 1

    assert not (tmp_path / ".spec").exists()
    assert "--source-repository" in capsys.readouterr().err


def test_smoke_uses_autopilot_container_rollout_policy(tmp_path: Path) -> None:
    config = _config(
        execution=ExecutionConfig(
            backend="worktree",
            backend_explicit=False,
            container=ContainerExecutionConfig(image="example/spec-worker:latest"),
        ),
        autopilot=AutopilotConfig(container_default_enabled=True),
    )
    backend = FakeBackend()
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        no_bootstrap=False,
        verify_gates=False,
        timeout=300,
    )

    with (
        patch("spec_runtime.container.load_repo_spec_runtime_config", return_value=config),
        patch("spec_runtime.container.get_execution_backend", return_value=backend) as get_backend,
        patch("spec_runtime.container.run_smoke", return_value=0) as run_smoke,
    ):
        code = container.cmd_smoke(args)

    assert code == 0
    smoke_config = get_backend.call_args.args[0]
    assert smoke_config.execution.backend == "container"
    assert smoke_config.execution.backend_explicit is False
    run_smoke.assert_called_once()


def test_smoke_no_bootstrap_clears_backend_bootstrap_commands(tmp_path: Path) -> None:
    config = _config(
        bootstrap_install_command="python -m pip install -e .",
        bootstrap_cache=BootstrapCacheConfig(enabled=True, command="python -m pip install -e ."),
    )
    backend = FakeBackend()
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        no_bootstrap=True,
        verify_gates=False,
        timeout=300,
    )

    with (
        patch("spec_runtime.container.load_repo_spec_runtime_config", return_value=config),
        patch("spec_runtime.container.get_execution_backend", return_value=backend) as get_backend,
        patch("spec_runtime.container.run_smoke", return_value=0) as run_smoke,
    ):
        code = container.cmd_smoke(args)

    assert code == 0
    smoke_config = get_backend.call_args.args[0]
    assert smoke_config.bootstrap_install_command == ""
    assert smoke_config.bootstrap_cache.enabled is False
    assert smoke_config.bootstrap_cache.command == ""
    assert run_smoke.call_args.kwargs["run_bootstrap"] is False


def test_smoke_invokes_backend_and_cleans_up() -> None:
    backend = FakeBackend()
    config = _config(
        agents=AgentConfig(default="codex", allowed=("codex",)),
        bootstrap_install_command="python -m pip install -e .",
    )

    code = container.run_smoke(
        Path("/tmp/repo"),
        config,
        backend=backend,  # type: ignore[arg-type]
        run_bootstrap=True,
        run_verify=False,
    )

    assert code == 0
    assert backend.cleaned is True
    assert backend.cleanup_allow_unpushed_work is True
    argv = [request.argv for request in backend.commands]
    assert ["git", "--version"] in argv
    assert any(
        request.argv[0:2] == ["sh", "-lc"] and "codex --version" in request.argv[2] for request in backend.commands
    )


def test_smoke_cleans_up_after_command_failure() -> None:
    class FailingBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            self.commands.append(request)
            return type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": "boom", "argv": request.argv},
            )()

    backend = FailingBackend()

    code = container.run_smoke(
        Path("/tmp/repo"),
        _config(),
        backend=backend,  # type: ignore[arg-type]
    )

    assert code == 1
    assert backend.cleaned is True
    assert backend.cleanup_allow_unpushed_work is True


def test_smoke_rejects_worker_spec_version_drift(capsys: pytest.CaptureFixture[str]) -> None:
    class StaleSpecBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            result = super().run_command(request)
            if request.argv == ["spec", "--version"]:
                result.stdout = "0.0.1\n"
            return result

    backend = StaleSpecBackend()

    code = container.run_smoke(
        Path("/tmp/repo"),
        _config(),
        backend=backend,  # type: ignore[arg-type]
    )

    assert code == 1
    assert backend.cleaned is True
    assert "spec version mismatch" in capsys.readouterr().err


def test_smoke_rejects_same_version_from_different_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class StaleSourceBackend(FakeBackend):
        def run_command(self, request: CommandRequest):
            result = super().run_command(request)
            if request.argv == ["spec", "--source-id"]:
                result.stdout = f"{host_spec_runtime_version()}@{'0' * 40}\n"
            return result

    backend = StaleSourceBackend()

    code = container.run_smoke(
        Path("/tmp/repo"),
        _config(),
        backend=backend,  # type: ignore[arg-type]
    )

    assert code == 1
    assert backend.cleaned is True
    assert "spec source identity mismatch" in capsys.readouterr().err


def test_host_source_identity_marks_dirty_editable_checkout() -> None:
    completed = [
        subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, "abc123\n", ""),
        subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, " M file.py\n", ""),
    ]

    with (
        patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.2.3"),
        patch("importlib.metadata.distribution", side_effect=RuntimeError("no metadata")),
        patch("spec_runtime.execution_backend.subprocess.run", side_effect=completed),
    ):
        source_id = host_spec_runtime_source_id()

    assert source_id == "1.2.3@abc123+dirty"


def test_host_source_identity_does_not_use_project_around_installed_wheel(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("user project\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    wheel_module = (
        tmp_path
        / ".venv"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "spec_runtime"
        / "execution_backend.py"
    )
    wheel_module.parent.mkdir(parents=True)
    wheel_module.touch()

    with (
        patch("spec_runtime.execution_backend.__file__", str(wheel_module)),
        patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.2.3"),
        patch("importlib.metadata.distribution", side_effect=RuntimeError("no metadata")),
    ):
        source_id = host_spec_runtime_source_id()

    assert source_id == "1.2.3"


def test_autopilot_container_backend_error_points_to_doctor() -> None:
    from spec_runtime.autopilot import AutopilotBackendPolicy, validate_autopilot_backend

    config = _config(
        autopilot=AutopilotConfig(container_default_enabled=True),
    )
    policy = AutopilotBackendPolicy(
        backend="container",
        safety_mode="safe",
        source="rollout-policy",
        backend_explicit=False,
    )

    with patch("shutil.which", return_value=None):
        message = validate_autopilot_backend(policy, config)

    assert "spec container doctor" in message


def test_worker_dockerfile_spec_install_layer_cache_busts_on_version() -> None:
    """The pip layer must reference SPEC_BUTLER_VERSION so
    Docker's layer cache keys on the host spec version."""
    for build_ssh in (False, True):
        rendered = container.render_worker_dockerfile(("claude",), build_ssh=build_ssh)
        assert "ARG SPEC_BUTLER_VERSION=unpinned" in rendered
        arg_pos = rendered.index("ARG SPEC_BUTLER_VERSION")
        use_pos = rendered.index('echo "specbutler ${SPEC_BUTLER_VERSION}"')
        install_pos = rendered.index("specbutler @ git+")
        assert arg_pos < use_pos < install_pos
        # The version reference must live in the same RUN as the install, or
        # the cache bust would not invalidate the pip layer.
        run_start = rendered.rindex("RUN", 0, use_pos)
        assert rendered.index("specbutler @ git+", run_start) == install_pos


def test_worker_dockerfile_can_pin_detected_agent_cli_versions() -> None:
    rendered = container.render_worker_dockerfile(
        ("claude", "codex"),
        agent_versions={"claude": "2.1.233", "codex": "0.147.0"},
    )

    assert "RUN npm install -g @anthropic-ai/claude-code@2.1.233 @openai/codex@0.147.0" in rendered


@pytest.mark.parametrize(
    ("agent", "output", "expected"),
    [
        ("claude", "2.1.233 (Claude Code)\n", "2.1.233"),
        ("codex", "codex-cli 0.147.0\n", "0.147.0"),
    ],
)
def test_detect_agent_cli_version(agent: str, output: str, expected: str) -> None:
    with (
        patch("spec_runtime.container.shutil.which", return_value=f"/usr/bin/{agent}"),
        patch(
            "spec_runtime.container.subprocess.run",
            return_value=subprocess.CompletedProcess([agent, "--version"], 0, output, ""),
        ),
    ):
        assert container._detect_agent_cli_version(agent) == expected


def test_host_spec_runtime_version_reads_pyproject() -> None:
    from spec_runtime.execution_backend import host_spec_runtime_version

    version = host_spec_runtime_version()
    assert version and version != "unknown"
    assert version[0].isdigit()
