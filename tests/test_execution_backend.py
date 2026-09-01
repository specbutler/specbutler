"""Tests for the execution backend seam (spec: execution-backend-interface)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime import execution_backend as eb
from spec_runtime import orchestrator as orch
from spec_runtime.config import (
    BootstrapCacheConfig,
    ContainerExecutionConfig,
    ExecutionConfig,
    SpecConfigError,
    SpecRuntimeConfig,
    load_spec_runtime_config,
)

# ---------------------------------------------------------------------------
# Config defaults & validation
# ---------------------------------------------------------------------------


def test_container_path_translation_accepts_native_windows_separators() -> None:
    translated = eb.ContainerExecutionBackend._translate_container_paths(
        r"C:\src\project\frontend\node_modules\@playwright\mcp\cli.js",
        [("C:/src/project", "/workspace/source")],
    )

    assert translated == "/workspace/source/frontend/node_modules/@playwright/mcp/cli.js"


def test_container_path_translation_does_not_rewrite_prefix_collisions() -> None:
    untouched = eb.ContainerExecutionBackend._translate_container_paths(
        r"C:\src\project-other\tool.exe",
        [("C:/src/project", "/workspace/source")],
    )

    assert untouched == r"C:\src\project-other\tool.exe"


def test_container_path_translation_preserves_unrelated_composite_backslashes() -> None:
    composite = (
        r'--config={"cwd":"C:\src\project\tools\run.py",'
        r'"pattern":"\\d+","replacement":"\\1"}'
    )

    translated = eb.ContainerExecutionBackend._translate_container_paths(
        composite,
        [("C:/src/project", "/workspace/source")],
    )

    assert translated == (
        r'--config={"cwd":"/workspace/source/tools/run.py",'
        r'"pattern":"\\d+","replacement":"\\1"}'
    )


class TestExecutionConfigDefaults:
    def test_default_when_section_missing(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text('base_ref = "origin/main"\n')
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.backend == "worktree"
        assert config.execution.safety_mode == "safe"
        assert config.execution.workspace_root == ".spec-workspaces"
        # Selection origin: implicit default. Future rollout policy can flip
        # this without overriding an explicit escape hatch.
        assert config.execution.backend_explicit is False
        assert config.execution.safety_mode_explicit is False

    def test_omitted_section_is_backwards_compatible(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
base_ref = "origin/main"

[implement]
setup_command = "scripts/setup.sh"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        # Other fields parsed normally.
        assert config.implement.setup_command == "scripts/setup.sh"
        # Execution defaults applied.
        assert config.execution == ExecutionConfig()

    def test_bootstrap_cache_section_parsed(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[bootstrap]
install_command = "make install"

[bootstrap.cache]
enabled = true
inputs = ["Makefile", "frontend/package-lock.json"]
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.bootstrap_install_command == "make install"
        assert config.bootstrap_cache.enabled is True
        assert config.bootstrap_cache.command == "make install"
        assert config.bootstrap_cache.inputs == ("Makefile", "frontend/package-lock.json")

    def test_explicit_worktree_distinguishable_from_omitted(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "worktree"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.backend == "worktree"
        assert config.execution.backend_explicit is True

    def test_explicit_clone_preserved(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "clone"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.backend == "clone"
        assert config.execution.backend_explicit is True

    def test_container_section_parsed(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"

[execution.container]
engine = "podman"
image = "example/spec-worker:latest"
dockerfile = ".spec/Dockerfile"
workspace_mode = "volume"
compose_file = "compose.yaml"

[execution.container.playwright_mcp]
topology = "sidecar"
command = "node"
args = ["cli.js", "--headless"]
browser = "msedge"
app_url = "http://localhost:5173"
sidecar_endpoint = "http://app:5173"
expected_version = "1.52.0"
actual_version = "1.52.0"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.backend == "container"
        assert config.execution.container.engine == "podman"
        assert config.execution.container.image == "example/spec-worker:latest"
        assert config.execution.container.dockerfile == ".spec/Dockerfile"
        assert config.execution.container.workspace_mode == "volume"
        assert config.execution.container.compose_file == "compose.yaml"
        assert config.execution.container.playwright_mcp.topology == "sidecar"
        assert config.execution.container.playwright_mcp.command == "node"
        assert config.execution.container.playwright_mcp.args == ("cli.js", "--headless")
        assert config.execution.container.playwright_mcp.browser == "msedge"
        assert config.execution.container.playwright_mcp.app_url == "http://localhost:5173"
        assert config.execution.container.playwright_mcp.sidecar_endpoint == "http://app:5173"
        assert config.execution.container.playwright_mcp.expected_version == "1.52.0"
        assert config.execution.container.playwright_mcp.actual_version == "1.52.0"

    def test_playwright_mcp_browser_defaults_to_chromium(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"

[execution.container.playwright_mcp]
topology = "in-worker"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.container.playwright_mcp.browser == "chromium"

    def test_default_playwright_mcp_args_pin_chromium_browser(self, tmp_path: Path):
        # Package fallback (no local cli.js).
        args = eb.ContainerExecutionBackend._default_playwright_mcp_args(tmp_path)
        assert args == ("@playwright/mcp", "--headless", "--browser", "chromium")

        # Local frontend install is preferred and still pins the browser.
        local_cli = tmp_path / "frontend" / "node_modules" / "@playwright" / "mcp" / "cli.js"
        local_cli.parent.mkdir(parents=True)
        local_cli.write_text("// cli\n")
        args = eb.ContainerExecutionBackend._default_playwright_mcp_args(
            tmp_path, expected_version="1.52.0", browser="msedge"
        )
        assert args == (str(local_cli), "--headless", "--browser", "msedge")

        # Empty browser omits the flag entirely (MCP server default applies).
        args = eb.ContainerExecutionBackend._default_playwright_mcp_args(tmp_path, browser="")
        assert args == (str(local_cli), "--headless")

    def test_local_config_overrides_execution_container_settings(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "worktree"

[execution.container]
engine = "docker"
image = "committed-worker:latest"
dockerfile = ".spec/worker.Dockerfile"
workspace_mode = "volume"
"""
        )
        (tmp_path / ".spec.local.toml").write_text(
            """
[execution]
backend = "container"

[execution.container]
engine = "podman"
image = "local-worker:latest"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.backend == "container"
        assert config.execution.backend_explicit is True
        assert config.execution.container.engine == "podman"
        assert config.execution.container.image == "local-worker:latest"
        assert config.execution.container.dockerfile == ".spec/worker.Dockerfile"
        assert config.execution.container.workspace_mode == "volume"

    def test_unknown_playwright_mcp_topology_rejected(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"

[execution.container.playwright_mcp]
topology = "magic"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            with pytest.raises(SpecConfigError, match="playwright_mcp.*topology"):
                load_spec_runtime_config(require=True)

    def test_container_build_ssh_round_trips(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"

[execution.container]
build_ssh = "default"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.container.build_ssh == "default"

    def test_container_build_ssh_defaults_to_empty(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            config = load_spec_runtime_config(require=True)

        assert config.execution.container.build_ssh == ""

    def test_unknown_container_workspace_mode_rejected(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "container"

[execution.container]
workspace_mode = "teleport"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            with pytest.raises(SpecConfigError, match="workspace_mode"):
                load_spec_runtime_config(require=True)

    def test_unknown_backend_rejected(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
backend = "kubernetes"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            with pytest.raises(SpecConfigError, match="kubernetes"):
                load_spec_runtime_config(require=True)

    def test_unknown_safety_mode_rejected(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".spec.toml").write_text(
            """
[execution]
safety_mode = "yolo"
"""
        )
        with patch(
            "spec_runtime.config._config_path",
            return_value=tmp_path / ".spec.toml",
        ):
            with pytest.raises(SpecConfigError, match="safety_mode"):
                load_spec_runtime_config(require=True)


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


class TestBackendFactory:
    def test_default_returns_worktree_backend(self):
        config = SpecRuntimeConfig()
        backend = eb.get_execution_backend(config)
        assert isinstance(backend, eb.WorktreeExecutionBackend)
        assert backend.identity.backend == "worktree"
        assert backend.identity.safety_mode == "safe"

    def test_clone_backend_returns_clone_backend(self):
        config = SpecRuntimeConfig(
            execution=ExecutionConfig(backend="clone", backend_explicit=True),
        )
        backend = eb.get_execution_backend(config)
        assert isinstance(backend, eb.CloneExecutionBackend)
        assert backend.identity.backend == "clone"

    def test_container_backend_returns_container_backend(self):
        config = SpecRuntimeConfig(
            execution=ExecutionConfig(backend="container", backend_explicit=True),
        )
        backend = eb.get_execution_backend(config)
        assert isinstance(backend, eb.ContainerExecutionBackend)
        assert backend.identity.backend == "container"

    def test_unknown_backend_raises(self):
        config = SpecRuntimeConfig(
            execution=ExecutionConfig(backend="bogus", backend_explicit=True),
        )
        with pytest.raises(eb.UnknownExecutionBackendError):
            eb.get_execution_backend(config)

    def test_factory_accepts_execution_config_directly(self):
        backend = eb.get_execution_backend(ExecutionConfig())
        assert isinstance(backend, eb.WorktreeExecutionBackend)


# ---------------------------------------------------------------------------
# Worktree backend behavior
# ---------------------------------------------------------------------------


class TestWorktreeBackend:
    def _make(self) -> eb.WorktreeExecutionBackend:
        return eb.WorktreeExecutionBackend(ExecutionConfig())

    def test_prepare_workspace_returns_existing_path(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        handle = backend.prepare_workspace(
            run_id="run-1",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=tmp_path,
            worktree_path=worktree,
        )
        assert handle.path == worktree
        assert handle.outbox_path == worktree / ".spec-outbox"
        assert handle.branch == "code/my-feature--abc"
        assert handle.backend == "worktree"
        assert handle.metadata["run_id"] == "run-1"

    def test_prepare_workspace_requires_worktree_path(self, tmp_path: Path):
        backend = self._make()
        with pytest.raises(ValueError, match="requires worktree_path"):
            backend.prepare_workspace(
                run_id="run-1",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=tmp_path,
                worktree_path=None,
            )

    def test_collect_outbox_metadata_absent_returns_none(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        handle = backend.prepare_workspace(
            run_id="run-1",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=tmp_path,
            worktree_path=worktree,
        )
        assert backend.collect_outbox_metadata(handle) is None

    def test_collect_outbox_metadata_parses_present_artifact(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        handle = backend.prepare_workspace(
            run_id="run-1",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=tmp_path,
            worktree_path=worktree,
        )
        handle.outbox_path.mkdir(parents=True, exist_ok=True)
        (handle.outbox_path / "pr-metadata.json").write_text(
            json.dumps(
                {
                    "title": "Implements my-feature",
                    "body": "Closes #42",
                    "labels": ["spec", "needs-review"],
                    "summary": "Adds the foo seam",
                    "head_sha": "deadbeef",
                }
            )
        )
        metadata = backend.collect_outbox_metadata(handle)
        assert metadata is not None
        assert metadata.title == "Implements my-feature"
        assert metadata.body == "Closes #42"
        assert metadata.labels == ("spec", "needs-review")
        assert metadata.summary == "Adds the foo seam"
        assert metadata.head_sha == "deadbeef"

    def test_collect_outbox_metadata_invalid_json_returns_none(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        handle = backend.prepare_workspace(
            run_id="run-1",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=tmp_path,
            worktree_path=worktree,
        )
        handle.outbox_path.mkdir(parents=True, exist_ok=True)
        (handle.outbox_path / "pr-metadata.json").write_text("not json")
        assert backend.collect_outbox_metadata(handle) is None

    def test_run_command_executes_in_workspace(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "marker.txt").write_text("ok")
        result = backend.run_command(
            eb.CommandRequest(
                argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; print(Path('marker.txt').read_text())",
                ],
                cwd=worktree,
            )
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_cleanup_is_noop(self, tmp_path: Path):
        backend = self._make()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        handle = backend.prepare_workspace(
            run_id="run-1",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=tmp_path,
            worktree_path=worktree,
        )
        backend.cleanup(handle)
        assert worktree.exists()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(*args: str, cwd: Path) -> None:
    result = _git(*args, cwd=cwd)
    assert result.returncode == 0, result.stderr or result.stdout


def _init_clone_source(repo: Path) -> None:
    repo.mkdir()
    _git_ok("init", cwd=repo)
    _git_ok("config", "user.email", "test@test.com", cwd=repo)
    _git_ok("config", "user.name", "Test", cwd=repo)
    remote = repo.parent / "remote.git"
    _git_ok("init", "--bare", str(remote), cwd=repo.parent)
    _git_ok("remote", "add", "origin", str(remote), cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git_ok("add", ".", cwd=repo)
    _git_ok("commit", "-m", "initial", cwd=repo)
    _git_ok("branch", "-M", "master", cwd=repo)
    _git_ok("push", "-u", "origin", "master", cwd=repo)


class TestCloneBackend:
    def _make(self, workspace_root: str = ".spec-workspaces") -> eb.CloneExecutionBackend:
        return eb.CloneExecutionBackend(
            ExecutionConfig(
                backend="clone",
                workspace_root=workspace_root,
                backend_explicit=True,
            )
        )

    def test_prepare_workspace_creates_full_checkout_not_linked_worktree(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()

        handle = backend.prepare_workspace(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--20260101T000000",
            repo_root=repo,
            base_ref="master",
        )

        assert handle.path == repo / ".spec-workspaces" / "my-feature-20260101T000000" / "source"
        assert handle.outbox_path == repo / ".spec-workspaces" / "my-feature-20260101T000000" / "outbox"
        assert (handle.path / ".git").is_dir()
        assert handle.backend == "clone"
        git_file = handle.path / ".git"
        assert not git_file.is_file()
        assert (
            _git("remote", "get-url", "origin", cwd=handle.path).stdout.strip()
            == _git(
                "remote",
                "get-url",
                "origin",
                cwd=repo,
            ).stdout.strip()
        )
        assert _git("worktree", "list", "--porcelain", cwd=repo).stdout.count("worktree ") == 1
        status = _git("status", "--porcelain", cwd=repo)
        assert status.stdout.strip() == ""

    def test_prepare_workspace_retries_no_local_after_cross_device_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        original_run_git = eb.CloneExecutionBackend._run_git
        clone_commands: list[list[str]] = []

        def fake_run_git(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
            if argv[:3] == ["clone", "--local", "--no-checkout"]:
                clone_commands.append(argv)
                source = Path(argv[-1])
                source.mkdir(parents=True)
                (source / "partial-clone").write_text("left by failed local clone\n")
                return subprocess.CompletedProcess(
                    ["git", *argv],
                    128,
                    "",
                    "fatal: failed to create link '.git/objects/aa/bb': Invalid cross-device link\n",
                )
            if argv[:3] == ["clone", "--no-local", "--no-checkout"]:
                clone_commands.append(argv)
            return original_run_git(argv, cwd=cwd)

        monkeypatch.setattr(
            eb.CloneExecutionBackend,
            "_run_git",
            staticmethod(fake_run_git),
        )

        handle = backend.prepare_workspace(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--20260101T000000",
            repo_root=repo,
            base_ref="master",
        )

        assert [command[:3] for command in clone_commands] == [
            ["clone", "--local", "--no-checkout"],
            ["clone", "--no-local", "--no-checkout"],
        ]
        assert (handle.path / ".git").is_dir()
        assert not (handle.path / "partial-clone").exists()

    def test_prepare_workspace_uses_existing_local_branch(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        _git_ok("checkout", "-b", "code/my-feature--abc", cwd=repo)
        (repo / "feature.txt").write_text("branch content\n")
        _git_ok("add", "feature.txt", cwd=repo)
        _git_ok("commit", "-m", "branch work", cwd=repo)
        _git_ok("checkout", "master", cwd=repo)

        handle = self._make().prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )

        assert (handle.path / "feature.txt").read_text() == "branch content\n"
        assert _git("branch", "--show-current", cwd=handle.path).stdout.strip() == "code/my-feature--abc"

    def test_refuses_tracked_workspace_root(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("print('x')\n")
        _git_ok("add", "src/app.py", cwd=repo)
        _git_ok("commit", "-m", "add src", cwd=repo)

        with pytest.raises(RuntimeError, match="tracked source path"):
            self._make("src").prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

    def test_requires_origin_remote_for_publish(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        _git_ok("remote", "remove", "origin", cwd=repo)

        with pytest.raises(RuntimeError, match="requires an origin remote"):
            self._make().prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

    def test_run_command_and_agent_write_backend_logs(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )

        command_result = backend.run_command(
            eb.CommandRequest(
                argv=["git", "status", "--short"],
                cwd=handle.path,
            )
        )
        agent_result = backend.launch_agent(
            eb.AgentRequest(
                argv=["python", "-c", "print('agent ok')"],
                cwd=handle.path,
                capture_stdout=True,
            )
        )

        assert command_result.returncode == 0
        assert agent_result.returncode == 0
        logs = sorted((repo / ".spec-workspaces" / "my-feature-abc" / "logs").glob("*.log"))
        assert any("command-git" in path.name for path in logs)
        assert any("agent-python" in path.name for path in logs)
        agent_payload = json.loads(
            (repo / ".spec-workspaces" / "my-feature-abc" / "outbox" / "agent-result.json").read_text()
        )
        assert agent_payload["returncode"] == 0
        assert "agent ok" in agent_payload["stdout"]

    def test_agent_completion_writes_patch_and_commit_metadata(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )

        result = backend.launch_agent(
            eb.AgentRequest(
                argv=[
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "import subprocess; "
                        "Path('agent.txt').write_text('done\\n'); "
                        "subprocess.run(['git', 'add', 'agent.txt'], check=True)"
                    ),
                ],
                cwd=handle.path,
                capture_stdout=True,
            )
        )

        assert result.returncode == 0
        metadata = json.loads((handle.outbox_path / "commit-metadata.json").read_text())
        patch_text = (handle.outbox_path / "final.patch").read_text()
        assert metadata["branch"] == "code/my-feature--abc"
        assert metadata["head_sha"]
        assert "agent.txt" in metadata["status"]
        assert "agent.txt" in patch_text
        assert "+done" in patch_text

    def test_agent_completion_patch_includes_committed_work(self, tmp_path: Path):
        # Regression: when the agent *commits* its work,
        # ``git diff HEAD`` is empty, so final.patch must instead diff against the
        # recorded base ref to still capture the committed changes.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )

        result = backend.launch_agent(
            eb.AgentRequest(
                argv=[
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "import subprocess; "
                        "Path('agent.txt').write_text('done\\n'); "
                        "subprocess.run(['git', 'add', 'agent.txt'], check=True); "
                        "subprocess.run("
                        "['git', 'commit', '-m', 'agent work'], check=True)"
                    ),
                ],
                cwd=handle.path,
                capture_stdout=True,
            )
        )

        assert result.returncode == 0
        metadata = json.loads((handle.outbox_path / "commit-metadata.json").read_text())
        patch_text = (handle.outbox_path / "final.patch").read_text()
        # HEAD advanced past the base and the tree is clean...
        assert "agent work" in metadata["recent_commits"]
        assert metadata["base_sha"]
        # ...yet the committed change is still captured in the patch.
        assert "agent.txt" in patch_text
        assert "+done" in patch_text

    def test_cleanup_removes_run_workspace(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )

        backend.cleanup(handle)

        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()
        assert repo.exists()

    def test_cleanup_rejects_inconsistent_workspace_paths(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        unsafe = eb.WorkspaceHandle(
            path=tmp_path / "outside" / "source",
            outbox_path=handle.outbox_path,
            branch=handle.branch,
            backend=handle.backend,
        )

        with pytest.raises(OSError, match="refusing to clean"):
            backend.cleanup(unsafe)

        assert (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_reprepare_preserves_uncommitted_changes(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        kwargs = dict(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        handle = backend.prepare_workspace(**kwargs)
        # Agent leaves uncommitted work in the tree.
        (handle.path / "wip.txt").write_text("uncommitted agent work\n")
        (handle.path / "README.md").write_text("edited by agent\n")

        # Resume / retry re-prepares the very same workspace.
        reprepared = backend.prepare_workspace(**kwargs)

        assert reprepared.path == handle.path
        assert (reprepared.path / "wip.txt").read_text() == "uncommitted agent work\n"
        assert (reprepared.path / "README.md").read_text() == "edited by agent\n"

    def test_reprepare_preserves_local_commits_ahead_of_origin(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        kwargs = dict(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        handle = backend.prepare_workspace(**kwargs)
        (handle.path / "impl.txt").write_text("implementation\n")
        _git_ok("add", "impl.txt", cwd=handle.path)
        _git_ok("commit", "-m", "implement work", cwd=handle.path)
        committed_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()

        reprepared = backend.prepare_workspace(**kwargs)

        assert _git("rev-parse", "HEAD", cwd=reprepared.path).stdout.strip() == committed_head
        assert (reprepared.path / "impl.txt").read_text() == "implementation\n"
        assert backend._unpushed_commits(reprepared.path) == [committed_head]

    def test_reprepare_refreshes_base_ref_from_orchestration_checkout(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        kwargs = dict(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="origin/master",
        )
        handle = backend.prepare_workspace(**kwargs)
        branch_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()
        stale_base = _git("rev-parse", "origin/master", cwd=handle.path).stdout.strip()

        (repo / "base-update.txt").write_text("new base\n")
        _git_ok("add", "base-update.txt", cwd=repo)
        _git_ok("commit", "-m", "advance base", cwd=repo)
        _git_ok("push", "origin", "master", cwd=repo)
        refreshed_base = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        assert refreshed_base != stale_base
        assert _git("rev-parse", "origin/master", cwd=handle.path).stdout.strip() == stale_base

        reprepared = backend.prepare_workspace(**kwargs)

        assert _git("rev-parse", "origin/master", cwd=reprepared.path).stdout.strip() == refreshed_base
        assert _git("rev-parse", "HEAD", cwd=reprepared.path).stdout.strip() == branch_head

    def test_cleanup_refuses_when_branch_has_unpushed_commits(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        (handle.path / "impl.txt").write_text("implementation\n")
        _git_ok("add", "impl.txt", cwd=handle.path)
        _git_ok("commit", "-m", "implement work", cwd=handle.path)

        with pytest.raises(eb.WorkspaceHasUnpushedWorkError, match="not present on any origin ref"):
            backend.cleanup(handle)
        assert (repo / ".spec-workspaces" / "my-feature-abc").exists()

        # The explicit post-merge / spec-clean opt-out still deletes it.
        backend.cleanup(handle, allow_unpushed_work=True)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_cleanup_allowed_when_commits_are_pushed(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        (handle.path / "impl.txt").write_text("implementation\n")
        _git_ok("add", "impl.txt", cwd=handle.path)
        _git_ok("commit", "-m", "implement work", cwd=handle.path)
        # Push the branch to origin: the work is now durable.
        _git_ok("push", "origin", "code/my-feature--abc", cwd=handle.path)

        backend.cleanup(handle)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_forced_restore_rescues_unpushed_work_and_records_index(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        base_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()
        snapshot = backend.snapshot(handle, "pre-implement")
        # Agent commits work and leaves an additional uncommitted edit.
        (handle.path / "impl.txt").write_text("implementation\n")
        _git_ok("add", "impl.txt", cwd=handle.path)
        _git_ok("commit", "-m", "implement work", cwd=handle.path)
        rescued_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()
        (handle.path / "README.md").write_text("uncommitted edit\n")

        restored = backend.restore(handle, snapshot)

        # The forced restore rolled the tree back to the snapshot (base) state...
        assert _git("rev-parse", "HEAD", cwd=restored.path).stdout.strip() == base_head
        assert not (restored.path / "impl.txt").exists()
        # ...but first produced a rescue snapshot referenced in run state.
        rescue_root = restored.outbox_path.parent / "rescue"
        index = json.loads((rescue_root / "index.json").read_text())
        assert len(index) == 1
        manifest = index[0]
        assert manifest["unpushed_commits"] == [rescued_head]
        assert Path(manifest["artifacts"]["bundle"]).is_file()
        assert Path(manifest["artifacts"]["uncommitted_patch"]).is_file()
        # The rescued commit is recoverable from the bundle.
        verify = _git("bundle", "verify", manifest["artifacts"]["bundle"], cwd=restored.path)
        assert verify.returncode == 0, verify.stderr

    def test_rescue_excludes_untracked_orchestrator_secrets(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        # Orchestrator stages self-gitignored credentials in the worktree.
        secret_home = handle.path / ".spec-claude-home"
        secret_home.mkdir()
        (secret_home / ".gitignore").write_text("*\n")
        (secret_home / "credentials.json").write_text("SUPER SECRET\n")
        # Uncommitted tracked change that should be rescued.
        (handle.path / "README.md").write_text("real work\n")

        manifest = backend._rescue_unpushed_work(handle.path, handle.outbox_path.parent, reason="test")

        assert manifest is not None
        patch_text = Path(manifest["artifacts"]["uncommitted_patch"]).read_text()
        assert "real work" in patch_text
        assert "SUPER SECRET" not in patch_text
        assert "credentials.json" not in patch_text

    def test_forced_restore_rescues_untracked_agent_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        snapshot = backend.snapshot(handle, "pre-implement")
        # Agent creates a brand-new file but never ``git add``-s it.
        (handle.path / "new_module.py").write_text("print('agent work')\n")
        nested = handle.path / "pkg"
        nested.mkdir()
        (nested / "helper.py").write_text("HELPER = 1\n")
        # Orchestrator secrets must not be rescued even though untracked.
        secret_home = handle.path / ".spec-claude-home"
        secret_home.mkdir()
        (secret_home / ".gitignore").write_text("*\n")
        (secret_home / "credentials.json").write_text("SUPER SECRET\n")

        restored = backend.restore(handle, snapshot)

        # The restore rolled the tree back, dropping the untracked files...
        assert not (restored.path / "new_module.py").exists()
        # ...but they were captured in the rescue snapshot first.
        rescue_root = restored.outbox_path.parent / "rescue"
        index = json.loads((rescue_root / "index.json").read_text())
        assert len(index) == 1
        manifest = index[0]
        saved = manifest["untracked_files"]
        assert "new_module.py" in saved
        assert "pkg/helper.py" in saved
        assert not any(".spec-claude-home" in rel for rel in saved)
        untracked_dir = Path(manifest["artifacts"]["untracked_dir"])
        assert (untracked_dir / "new_module.py").read_text() == "print('agent work')\n"
        assert (untracked_dir / "pkg" / "helper.py").read_text() == "HELPER = 1\n"
        assert not (untracked_dir / ".spec-claude-home").exists()

    def test_forced_restore_aborts_when_required_rescue_artifact_fails(self, tmp_path: Path):
        # If a rescue artifact for detected work fails to write, restore must
        # abort *before* replacing the tree — otherwise the work it failed to
        # preserve would be destroyed anyway.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        snapshot = backend.snapshot(handle, "pre-implement")
        # Agent commits unpushed work and leaves an uncommitted edit.
        (handle.path / "impl.txt").write_text("implementation\n")
        _git_ok("add", "impl.txt", cwd=handle.path)
        _git_ok("commit", "-m", "implement work", cwd=handle.path)
        committed_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()
        (handle.path / "README.md").write_text("uncommitted edit\n")

        # Simulate the bundle write failing so the unpushed commits cannot be
        # preserved. Every other git invocation runs for real.
        original_run_git = backend._run_git

        def failing_run_git(argv, *, cwd):
            if argv[:1] == ["bundle"]:
                return subprocess.CompletedProcess(argv, 1, "", "simulated bundle failure")
            return original_run_git(argv, cwd=cwd)

        backend._run_git = failing_run_git
        try:
            with pytest.raises(eb.WorkspaceRescueFailedError, match="unpushed commits"):
                backend.restore(handle, snapshot)
        finally:
            del backend._run_git

        # The workspace tree was NOT replaced: the commit and the edit survive.
        assert _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip() == committed_head
        assert (handle.path / "impl.txt").read_text() == "implementation\n"
        assert (handle.path / "README.md").read_text() == "uncommitted edit\n"
        # A partial rescue manifest is still recorded for diagnostics, flagged
        # as incomplete so the failure package can surface it.
        rescue_root = handle.outbox_path.parent / "rescue"
        index = json.loads((rescue_root / "index.json").read_text())
        assert index[-1]["preserved"] is False
        assert "unpushed commits" in index[-1]["unpreserved"]

    @pytest.mark.skipif(os.name == "nt", reason="non-elevated Windows cannot create file symlinks")
    def test_rescue_preserves_untracked_symlink_without_following_it(self, tmp_path: Path):
        # An untracked symlink pointing outside the workspace must be captured
        # as a symlink, never dereferenced — otherwise the rescue artifact would
        # embed file content from outside the workspace (e.g. /etc/passwd).
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("CONTENT FROM OUTSIDE THE WORKSPACE\n")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        link = handle.path / "escape_link"
        link.symlink_to(outside)

        manifest = backend._rescue_unpushed_work(
            handle.path, handle.outbox_path.parent, reason="test"
        )

        assert manifest is not None
        assert "escape_link" in manifest["untracked_files"]
        untracked_dir = Path(manifest["artifacts"]["untracked_dir"])
        saved = untracked_dir / "escape_link"
        assert saved.is_symlink()
        assert os.path.samefile(saved, outside)
        # The outside content must not have been copied into the rescue tree.
        assert not any(
            p.is_file() and not p.is_symlink() and "CONTENT FROM OUTSIDE" in p.read_text()
            for p in untracked_dir.rglob("*")
        )

    def test_cleanup_refuses_when_tracked_files_are_dirty(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        # Uncommitted edit to a tracked file, no commits, no untracked files.
        (handle.path / "README.md").write_text("uncommitted edit\n")

        with pytest.raises(eb.WorkspaceHasUnpushedWorkError, match="uncommitted changes"):
            backend.cleanup(handle)
        assert (repo / ".spec-workspaces" / "my-feature-abc").exists()

        backend.cleanup(handle, allow_unpushed_work=True)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_cleanup_refuses_when_untracked_files_present(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        # Brand-new never-staged agent file: no commits, no tracked edits.
        (handle.path / "scratch.py").write_text("agent output\n")

        with pytest.raises(eb.WorkspaceHasUnpushedWorkError, match="untracked file"):
            backend.cleanup(handle)
        assert (repo / ".spec-workspaces" / "my-feature-abc").exists()

        backend.cleanup(handle, allow_unpushed_work=True)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_cleanup_allowed_when_only_orchestrator_secrets_are_untracked(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        backend = self._make()
        handle = backend.prepare_workspace(
            run_id="my-feature-abc",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            repo_root=repo,
            base_ref="master",
        )
        # Only self-gitignored orchestrator credentials are present: nothing the
        # agent produced, so deletion must not be blocked.
        secret_home = handle.path / ".spec-claude-home"
        secret_home.mkdir()
        (secret_home / ".gitignore").write_text("*\n")
        (secret_home / "credentials.json").write_text("SUPER SECRET\n")

        backend.cleanup(handle)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()


# A sample pid recorded on line 1 of a stale postmaster.pid file. Teardown does
# not (and must not) probe its host-side liveness — the pid is written by a
# postmaster inside the worker container's own PID namespace, so it is
# meaningless on the host. The value is arbitrary; it exists only to give the
# fixture a realistic pid line.
_RECORDED_PID = 2147483646


class _FakeContainerRunner:
    def __init__(
        self,
        *,
        fail_volume_import: bool = False,
        fail_compose_up: bool = False,
        fail_compose_stop: bool = False,
        fail_in_worker_run: bool = False,
        playwright_version: str = "",
        image_passwd: str | None = None,
        image_group: str | None = None,
        fail_cp: bool = False,
        ps_container_ids: Sequence[str] = (),
        fail_ps: bool = False,
        fail_rm_ids: Sequence[str] = (),
        inspect_present_ids: Sequence[str] = (),
        cache_built_images: bool = False,
        build_delay_seconds: float = 0.0,
    ):
        self.calls: list[list[str]] = []
        self.cwd_calls: list[Path] = []
        self.envs: list[dict[str, str] | None] = []
        self.fail_volume_import = fail_volume_import
        self.fail_compose_up = fail_compose_up
        self.fail_compose_stop = fail_compose_stop
        self.fail_in_worker_run = fail_in_worker_run
        self.playwright_version = playwright_version
        self.image_passwd = image_passwd
        self.image_group = image_group
        self.fail_cp = fail_cp
        self.ps_container_ids = tuple(ps_container_ids)
        self.fail_ps = fail_ps
        self.fail_rm_ids = set(fail_rm_ids)
        # Ids that ``docker inspect`` should report as still present (returncode
        # 0). Any other id inspects as not-found, modelling a confirmed removal.
        self.inspect_present_ids = set(inspect_present_ids)
        self.cache_built_images = cache_built_images
        self.build_delay_seconds = build_delay_seconds
        self.built_images: set[str] = set()
        self.image_state_lock = threading.Lock()

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, timeout
        self.calls.append([str(item) for item in argv])
        self.cwd_calls.append(cwd)
        self.envs.append(dict(env) if env is not None else None)
        if argv[:3] == ["docker", "image", "inspect"]:
            if self.cache_built_images:
                with self.image_state_lock:
                    if argv[3] in self.built_images:
                        return subprocess.CompletedProcess(argv, 0, "present\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "missing")
        if argv[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 0, "pulled\n", "")
        if argv[:2] == ["docker", "build"]:
            if self.build_delay_seconds:
                time.sleep(self.build_delay_seconds)
            if self.cache_built_images:
                with self.image_state_lock:
                    self.built_images.add(_value_after(argv, "-t"))
            return subprocess.CompletedProcess(argv, 0, "built\n", "")
        if argv[:3] == ["docker", "compose", "-p"]:
            if self.fail_compose_up and "up" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "postgres failed\n")
            if self.fail_compose_stop and "stop" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "postgres still running\n")
            return subprocess.CompletedProcess(argv, 0, "compose ok\n", "")
        if argv[:2] == ["docker", "volume"] and "ls" in argv:
            label = _value_after(argv, "--filter")
            project = label.rsplit("=", 1)[-1] if label else "spec-stale"
            return subprocess.CompletedProcess(argv, 0, f"{project}_postgres-data\n", "")
        if argv[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(argv, 0, "created\n", "")
        if argv[:2] == ["docker", "cp"]:
            if self.fail_cp:
                return subprocess.CompletedProcess(argv, 1, "", "no such file\n")
            if len(argv) >= 4:
                src = argv[2]
                dest = Path(argv[3])
                content: str | None = None
                if src.endswith(":/etc/passwd"):
                    content = self.image_passwd
                elif src.endswith(":/etc/group"):
                    content = self.image_group
                if content is not None:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 1, "", "no fixture configured\n")
            return subprocess.CompletedProcess(argv, 1, "", "bad cp args\n")
        if argv[:2] == ["docker", "ps"]:
            if self.fail_ps:
                return subprocess.CompletedProcess(argv, 1, "", "ps failed\n")
            return subprocess.CompletedProcess(argv, 0, "".join(f"{cid}\n" for cid in self.ps_container_ids), "")
        if argv[:3] == ["docker", "rm", "-f"]:
            target = argv[3] if len(argv) > 3 else ""
            if target in self.fail_rm_ids:
                return subprocess.CompletedProcess(argv, 1, "", f"cannot remove {target}\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["docker", "inspect"]:
            target = argv[-1]
            if target in self.inspect_present_ids:
                return subprocess.CompletedProcess(argv, 0, "[{}]\n", "")
            return subprocess.CompletedProcess(
                argv, 1, "", f"Error: No such object: {target}\n"
            )
        if argv[:2] == ["docker", "run"]:
            if argv[-2:] and "@playwright/test/package.json" in argv[-1]:
                stdout = f"{self.playwright_version}\n" if self.playwright_version else ""
                returncode = 0 if self.playwright_version else 2
                return subprocess.CompletedProcess(argv, returncode, stdout, "")
            cidfile = _value_after(argv, "--cidfile")
            if cidfile and Path(cidfile).exists():
                return subprocess.CompletedProcess(
                    argv,
                    125,
                    "",
                    f"container ID file found, delete {cidfile}\n",
                )
            if cidfile:
                Path(cidfile).write_text("container-123\n")
            if self.fail_in_worker_run and cidfile and "sleep infinity" in argv[-1]:
                return subprocess.CompletedProcess(argv, 1, "", "worker failed\n")
            snapshot_mount = next(
                (
                    item
                    for index, item in enumerate(argv)
                    if index > 0 and argv[index - 1] == "-v" and item.endswith(":/workspace/service-volume-snapshot")
                ),
                "",
            )
            if snapshot_mount:
                snapshot_dir = Path(
                    snapshot_mount.removesuffix(
                        ":/workspace/service-volume-snapshot"
                    )
                )
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                command = argv[-1]
                marker = "/workspace/service-volume-snapshot/"
                if marker in command:
                    archive_name = command.split(marker, 1)[1].split()[0]
                    (snapshot_dir / archive_name).write_text("volume archive\n")
            if self.fail_volume_import and any("/workspace/host" in item for item in argv):
                return subprocess.CompletedProcess(argv, 1, "", "copy failed\n")
            return subprocess.CompletedProcess(argv, 0, "container ok\n", "")
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, "exec ok\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _value_after(argv: list[str], flag: str) -> str:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


class TestContainerBackend:
    def _make(
        self,
        runner: _FakeContainerRunner,
        *,
        image: str = "example/spec-worker:latest",
        dockerfile: str = ".spec/worker.Dockerfile",
        workspace_mode: str = "bind",
        system_name: str = "Linux",
        bootstrap_install_command: str = "",
        bootstrap_cache_command: str = "",
        bootstrap_cache_inputs: tuple[str, ...] = (),
        compose_file: str = "",
        playwright_mcp: object | None = None,
        build_ssh: str = "",
    ) -> eb.ContainerExecutionBackend:
        container_config = ContainerExecutionConfig(
            image=image,
            dockerfile=dockerfile,
            workspace_mode=workspace_mode,
            compose_file=compose_file,
            build_ssh=build_ssh,
        )
        if playwright_mcp is not None:
            container_config = replace(container_config, playwright_mcp=playwright_mcp)
        return eb.ContainerExecutionBackend(
            ExecutionConfig(
                backend="container",
                workspace_root=".spec-workspaces",
                container=container_config,
                backend_explicit=True,
            ),
            bootstrap_install_command=bootstrap_install_command,
            bootstrap_cache_command=bootstrap_cache_command,
            bootstrap_cache_inputs=bootstrap_cache_inputs,
            runner=runner,
            system_name=system_name,
        )

    def test_prepare_workspace_requires_docker_compatible_cli(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)

        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Docker-compatible CLI"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

    def test_prepare_workspace_uses_configured_image_and_writes_state(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert handle.backend == "container"
        assert state["image"] == "example/spec-worker:latest"
        assert state["workspace_mode"] == "bind"
        assert state["service_topology"] == "in-worker"
        assert state["playwright_mcp"]["topology"] == "in-worker"
        assert state["playwright_mcp"]["target_app_url"] == "http://localhost:3000"
        assert state["playwright_mcp"]["command"] == "npx"
        assert state["compose_file"] == ""
        assert state["resource_labels"] == {
            "spec.owner": "spec-runtime",
            "spec.phase": "execution",
            "spec.run_id": "my-feature-abc",
            "spec.spec_id": "my-feature",
            "spec.workspace_root": str(handle.path),
        }
        worker_call = next(call for call in runner.calls if call[:3] == ["docker", "run", "-d"])
        assert "spec.owner=spec-runtime" in worker_call
        assert "spec.run_id=my-feature-abc" in worker_call
        assert state["service_data_dirs"] == [str(handle.path / ".local" / "postgres" / "data")]
        assert Path(handle.metadata["container_state_path"]).parent == handle.outbox_path.parent / "backend-state"
        assert not (handle.outbox_path / "container-backend-state.json").exists()
        assert any(call[:2] == ["docker", "pull"] for call in runner.calls)

    def test_empty_compose_file_defaults_to_in_worker_services(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["service_topology"] == "in-worker"
        assert state["worker_container"] == "container-123"
        assert state["service_processes"][0]["container_id"] == "container-123"
        assert not any(call[:2] == ["docker", "compose"] for call in runner.calls)

    def test_in_worker_commands_exec_in_persistent_worker_container(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        result = backend.run_command(eb.CommandRequest(argv=["scripts/local_postgres.sh"], cwd=handle.path))

        assert result.returncode == 0
        exec_call = next(call for call in runner.calls if call[:2] == ["docker", "exec"])
        assert "container-123" in exec_call
        assert "scripts/local_postgres.sh" in exec_call
        assert not any(call[:2] == ["docker", "run"] and "scripts/local_postgres.sh" in call for call in runner.calls)

    def test_previous_attempt_containers_removed_on_new_attempt(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        # Two leftover containers from earlier attempts of the same run, plus
        # the current attempt's own worker id (container-123, written by the
        # cidfile) which must be preserved.
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1", "old-sidecar-2"))
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        ps_call = next(call for call in runner.calls if call[:2] == ["docker", "ps"])
        assert "label=spec.owner=spec-runtime" in ps_call
        assert "label=spec.run_id=my-feature-abc" in ps_call
        rm_targets = [call[3] for call in runner.calls if call[:3] == ["docker", "rm", "-f"] and len(call) > 3]
        assert "old-worker-1" in rm_targets
        assert "old-sidecar-2" in rm_targets

    def test_current_attempt_worker_created_after_teardown(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        # The current attempt's own worker is preserved by ordering: teardown
        # discovers/removes prior containers *before* this attempt starts its
        # own worker, so the new worker id can never be a teardown target.
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1",))
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["worker_container"] == "container-123"
        ps_index = next(i for i, call in enumerate(runner.calls) if call[:2] == ["docker", "ps"])
        worker_index = next(
            i for i, call in enumerate(runner.calls) if call[:3] == ["docker", "run", "-d"] and "sleep infinity" in call[-1]
        )
        assert ps_index < worker_index
        # The new worker id is never a teardown target.
        assert not any(
            call[:3] == ["docker", "rm", "-f"] and len(call) > 3 and call[3] == "container-123"
            for call in runner.calls
        )

    def test_teardown_excludes_already_recorded_worker(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(ps_container_ids=("container-123", "old-worker-1"))
        backend = self._make(runner, compose_file="")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "containers": ["container-123"],
            "service_data_dirs": [],
        }

        backend._teardown_previous_attempt_containers(run_root, state)

        rm_targets = [call[3] for call in runner.calls if call[:3] == ["docker", "rm", "-f"] and len(call) > 3]
        assert "container-123" not in rm_targets
        assert "old-worker-1" in rm_targets

    def test_teardown_leaves_other_runs_untouched(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        # The label query scopes teardown to this run's owned containers only:
        # both labels are present, so other runs' and unlabeled containers can
        # never match.
        ps_call = next(call for call in runner.calls if call[:2] == ["docker", "ps"])
        assert "label=spec.owner=spec-runtime" in ps_call
        assert "label=spec.run_id=my-feature-abc" in ps_call
        # No stale ids returned -> teardown removes nothing (the only rm -f
        # calls are the unrelated one-shot passwd/group shim extractions).
        teardown_rm = [
            call
            for call in runner.calls
            if call[:3] == ["docker", "rm", "-f"] and len(call) > 3 and not call[3].startswith("spec-extract-")
        ]
        assert teardown_rm == []

    def test_teardown_clears_stale_postmaster_pid(self, tmp_path: Path):
        # Verified-gone (rm -f succeeds, docker inspect reports not-found) ->
        # the stale postmaster.pid is cleared. Container removal is the
        # authoritative "no postmaster owns it" signal; the recorded pid's
        # host-side liveness is irrelevant and is never probed.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1",))
        backend = self._make(runner, compose_file="")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        data_dir = run_root / "source" / ".local" / "postgres" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text(f"{_RECORDED_PID}\n/workspace/source/.local/postgres/data\n")
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "workspace_mode": "bind",
            "containers": [],
            "service_data_dirs": [str(data_dir)],
        }

        verified = backend._teardown_previous_attempt_containers(run_root, state)

        assert verified is True
        assert not pid_file.exists()
        cleanup_log = run_root / "logs" / "previous-attempt-postmaster-cleanup.log"
        assert cleanup_log.is_file()
        # A not-found inspect confirmed removal before the pid was cleared.
        assert any(call[:2] == ["docker", "inspect"] and "old-worker-1" in call for call in runner.calls)

    def test_teardown_leaves_pid_when_removal_not_verified(self, tmp_path: Path):
        # rm -f reports success but docker inspect still finds the container:
        # removal is NOT verified, so the postmaster.pid must be left untouched
        # (a live postmaster could still own the data dir) and the skip logged.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(
            ps_container_ids=("old-worker-1",),
            inspect_present_ids=("old-worker-1",),
        )
        backend = self._make(runner, compose_file="")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        data_dir = run_root / "source" / ".local" / "postgres" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text(f"{_RECORDED_PID}\n/workspace/source/.local/postgres/data\n")
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "workspace_mode": "bind",
            "containers": [],
            "service_data_dirs": [str(data_dir)],
        }

        verified = backend._teardown_previous_attempt_containers(run_root, state)

        assert verified is False
        assert pid_file.exists()
        skipped_log = run_root / "logs" / "previous-attempt-postmaster-cleanup-skipped.log"
        assert skipped_log.is_file()

    def test_teardown_leaves_pid_when_removal_fails(self, tmp_path: Path):
        # rm -f fails outright: removal unverified -> pid untouched, skip logged.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(
            ps_container_ids=("old-worker-1",),
            fail_rm_ids=("old-worker-1",),
        )
        backend = self._make(runner, compose_file="")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        data_dir = run_root / "source" / ".local" / "postgres" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text(f"{_RECORDED_PID}\n/workspace/source/.local/postgres/data\n")
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "workspace_mode": "bind",
            "containers": [],
            "service_data_dirs": [str(data_dir)],
        }

        verified = backend._teardown_previous_attempt_containers(run_root, state)

        assert verified is False
        assert pid_file.exists()
        skipped_log = run_root / "logs" / "previous-attempt-postmaster-cleanup-skipped.log"
        assert skipped_log.is_file()
        failure_log = run_root / "logs" / "previous-attempt-teardown-failures.log"
        assert failure_log.is_file()

    def test_teardown_clears_pid_recorded_as_live_host_process(self, tmp_path: Path):
        # Regression: postgres runs *inside* the worker container (its own PID
        # namespace), so the pid recorded in postmaster.pid is a
        # container-namespace pid that is meaningless on the host. Even when it
        # happens to collide with a live host process (this test process),
        # teardown must still clear the stale file once every prior-attempt
        # container is verified gone — probing host-side liveness would
        # misclassify the stale pid as "live" and leave it in place, causing the
        # exact `pg_ctl: another server might be running` failure this cleanup
        # exists to prevent.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1",))
        backend = self._make(runner, compose_file="")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        data_dir = run_root / "source" / ".local" / "postgres" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text(f"{os.getpid()}\n/workspace/source/.local/postgres/data\n")
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "workspace_mode": "bind",
            "containers": [],
            "service_data_dirs": [str(data_dir)],
        }

        verified = backend._teardown_previous_attempt_containers(run_root, state)

        assert verified is True
        assert not pid_file.exists()
        cleanup_log = run_root / "logs" / "previous-attempt-postmaster-cleanup.log"
        assert cleanup_log.is_file()

    def test_teardown_defers_volume_postmaster_clear(self, tmp_path: Path):
        # In volume mode the postgres data dir lives inside the workspace
        # volume, which is wiped and reseeded *after* teardown. Clearing the
        # stale postmaster.pid during teardown would be undone by the reseed,
        # so the container removal step must NOT spin the one-shot clear here —
        # it is deferred to prepare_workspace, after the reseed.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1",))
        backend = self._make(runner, compose_file="", workspace_mode="volume", system_name="Darwin")
        run_root = (repo / ".spec-workspaces" / "my-feature-abc").resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        state = {
            "resource_labels": {"spec.owner": "spec-runtime", "spec.run_id": "my-feature-abc"},
            "workspace_mode": "volume",
            "image": "example/spec-worker:latest",
            "workspace_volumes": ["spec-deadbeef-source"],
            "containers": [],
            "service_data_dirs": [str(run_root / "source" / ".local" / "postgres" / "data")],
        }

        backend._teardown_previous_attempt_containers(run_root, state)

        rm_pid_call = next(
            (
                call
                for call in runner.calls
                if call[:3] == ["docker", "run", "--rm"] and "postmaster.pid" in call[-1]
            ),
            None,
        )
        assert rm_pid_call is None

    def test_volume_postmaster_pid_cleared_after_reseed(self, tmp_path: Path):
        # End-to-end ordering guard for the finding: the volume-mode stale
        # postmaster.pid clear must run *after* the workspace volume is reseeded
        # (which wipes and repopulates it) and *before* the worker starts, so
        # the cleanup is not undone.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(ps_container_ids=("old-worker-1",))
        backend = self._make(runner, compose_file="", workspace_mode="volume", system_name="Darwin")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        seed_index = next(
            i for i, call in enumerate(runner.calls) if "/workspace/seed:ro" in " ".join(call)
        )
        clear_index = next(
            i
            for i, call in enumerate(runner.calls)
            if call[:3] == ["docker", "run", "--rm"] and "postmaster.pid" in call[-1]
        )
        worker_index = next(
            i
            for i, call in enumerate(runner.calls)
            if call[:3] == ["docker", "run", "-d"] and "sleep infinity" in call[-1]
        )
        # reseed -> clear stale pid -> start worker
        assert seed_index < clear_index < worker_index
        clear_call = runner.calls[clear_index]
        assert "/workspace/source/.local/postgres/data/postmaster.pid" in clear_call[-1]

    def test_volume_postmaster_pid_cleared_without_prior_container(self, tmp_path: Path):
        # A stale postmaster.pid can outlive its
        # container (pruned, or the run died with no orchestrator attached), so
        # discovery finds no prior-attempt container (empty ps) yet the reseed
        # can still re-introduce a stale pid from the host seed. The volume-mode
        # clear must therefore run regardless of whether a prior container was
        # found — gating it on "a prior container still exists" would skip the
        # cleanup and leave verify env prep failing with 'another server might
        # be running'. It is a cheap rm -f no-op when the data dir is clean.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="", workspace_mode="volume", system_name="Darwin")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        # The clear runs after the reseed and before the worker starts, even
        # though ps returned no prior-attempt container.
        seed_index = next(
            i for i, call in enumerate(runner.calls) if "/workspace/seed:ro" in " ".join(call)
        )
        clear_index = next(
            i
            for i, call in enumerate(runner.calls)
            if call[:3] == ["docker", "run", "--rm"] and "postmaster.pid" in call[-1]
        )
        worker_index = next(
            i
            for i, call in enumerate(runner.calls)
            if call[:3] == ["docker", "run", "-d"] and "sleep infinity" in call[-1]
        )
        assert seed_index < clear_index < worker_index

    def test_teardown_failure_is_non_fatal_and_logged(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(
            ps_container_ids=("old-worker-1",),
            fail_rm_ids=("old-worker-1",),
        )
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        # The attempt still prepares its own worker despite the teardown failure.
        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["worker_container"] == "container-123"
        failure_log = handle.outbox_path.parent / "logs" / "previous-attempt-teardown-failures.log"
        assert failure_log.is_file()
        assert "old-worker-1" in failure_log.read_text()

    def test_teardown_discovery_failure_is_non_fatal(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_ps=True)
        backend = self._make(runner, compose_file="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["worker_container"] == "container-123"
        # A failed discovery removes nothing (only unrelated shim extractions).
        teardown_rm = [
            call
            for call in runner.calls
            if call[:3] == ["docker", "rm", "-f"] and len(call) > 3 and not call[3].startswith("spec-extract-")
        ]
        assert teardown_rm == []

    def test_compose_file_opts_into_host_managed_sidecars(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text(
            "services:\n  postgres:\n    image: postgres:16\nvolumes:\n  postgres-data: {}\n"
        )
        _git_ok("add", "compose.yaml", cwd=repo)
        _git_ok("commit", "-m", "add compose services", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        compose_call = next(call for call in runner.calls if call[:3] == ["docker", "compose", "-p"])
        assert state["service_topology"] == "sidecar"
        assert state["compose_file"] == str((handle.path / "compose.yaml").resolve())
        assert state["compose_project"].startswith("spec-")
        assert state["service_networks"][0].startswith("spec-")
        assert state["service_volumes"] == [f"{state['compose_project']}_postgres-data"]
        assert "up" in compose_call
        assert "-d" in compose_call
        assert str((handle.path / "compose.yaml").resolve()) in compose_call
        assert "/var/run/docker.sock" not in " ".join(compose_call)
        override = json.loads((handle.outbox_path.parent / "container-compose-labels.json").read_text())
        assert override["services"]["postgres"]["labels"]["spec.owner"] == "spec-runtime"
        assert override["volumes"]["postgres-data"]["labels"]["spec.run_id"] == "my-feature-abc"
        assert override["networks"]["default"]["labels"]["spec.spec_id"] == "my-feature"

    def test_playwright_mcp_sidecar_requires_explicit_reachable_target(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        playwright_mcp = replace(
            ContainerExecutionConfig().playwright_mcp,
            topology="sidecar",
        )
        backend = self._make(runner, playwright_mcp=playwright_mcp)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="requires .*app_url or sidecar_endpoint"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

    def test_playwright_mcp_sidecar_records_mapping_and_cleans_up(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        playwright_mcp = replace(
            ContainerExecutionConfig().playwright_mcp,
            topology="sidecar",
            app_url="http://localhost:5173/dashboard",
            command="node",
            args=("cli.js", "--headless"),
        )
        backend = self._make(runner, playwright_mcp=playwright_mcp)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        mcp_state = state["playwright_mcp"]
        diagnostics = json.loads((handle.outbox_path.parent / "logs" / "playwright-mcp-diagnostics.json").read_text())
        assert mcp_state["topology"] == "sidecar"
        assert mcp_state["target_app_url"] == "http://host.docker.internal:5173/dashboard"
        assert mcp_state["sidecar_networks"][0] in state["networks"]
        assert mcp_state["sidecar_mcp_transport"] == "sse"
        assert mcp_state["sidecar_mcp_port"] == 3001
        assert mcp_state["sidecar_mcp_server"] == {
            "type": "sse",
            "url": f"http://{mcp_state['sidecar_container']}:3001/sse",
        }
        assert "SSE endpoint" in mcp_state["sidecar_mcp_note"]
        assert diagnostics["mcp_command"] == ["node", "cli.js", "--headless"]
        assert diagnostics["topology"] == "sidecar"
        assert diagnostics["target_app_url"] == mcp_state["target_app_url"]
        assert diagnostics["sidecar_mcp_transport"] == "sse"
        sidecar_run = next(
            call
            for call in runner.calls
            if call[:2] == ["docker", "run"] and "--name" in call and mcp_state["sidecar_container"] in call
        )
        assert "spec.owner=spec-runtime" in sidecar_run
        assert "spec.run_id=my-feature-abc" in sidecar_run
        assert sidecar_run[-7:] == [
            "node",
            "cli.js",
            "--headless",
            "--port",
            "3001",
            "--host",
            "0.0.0.0",
        ]
        assert mcp_state["sidecar_networks"][0] in sidecar_run
        network_create = next(call for call in runner.calls if call[:3] == ["docker", "network", "create"])
        assert "spec.owner=spec-runtime" in network_create
        assert "spec.run_id=my-feature-abc" in network_create

        backend.cleanup(handle)

        assert any(
            call[:3] == ["docker", "rm", "-f"] and mcp_state["sidecar_container"] in call for call in runner.calls
        )

    def test_playwright_mcp_sidecar_connects_to_service_network(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        playwright_mcp = replace(
            ContainerExecutionConfig().playwright_mcp,
            topology="sidecar",
            sidecar_endpoint="http://app:5173",
        )
        backend = self._make(
            runner,
            compose_file="compose.yaml",
            playwright_mcp=playwright_mcp,
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        mcp_state = state["playwright_mcp"]
        assert mcp_state["target_app_url"] == "http://app:5173"
        assert any(
            call[:3] == ["docker", "network", "connect"]
            and state["service_networks"][0] in call
            and mcp_state["sidecar_container"] in call
            for call in runner.calls
        )

    def test_playwright_mcp_version_mismatch_diagnostic(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        playwright_mcp = replace(
            ContainerExecutionConfig().playwright_mcp,
            expected_version="1.52.0",
            actual_version="1.51.1",
        )
        backend = self._make(runner, playwright_mcp=playwright_mcp)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="expected 1.52.0, actual 1.51.1"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        failure = json.loads((run_root / "logs" / "playwright-mcp-version-mismatch.json").read_text())
        assert failure["failure_type"] == "browser_runtime"
        assert failure["failure_subtype"] == "playwright_version_mismatch"
        assert failure["expected_version"] == "1.52.0"
        assert failure["actual_version"] == "1.51.1"
        assert "Install browser dependencies" in failure["remediation"]

    def test_playwright_mcp_version_mismatch_detects_worker_runtime(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "package.json").write_text(json.dumps({"devDependencies": {"@playwright/test": "1.52.0"}}))
        _git_ok("add", "package.json", cwd=repo)
        _git_ok("commit", "-m", "add package manifest", cwd=repo)
        _git_ok("push", cwd=repo)
        runner = _FakeContainerRunner(playwright_version="1.51.1")
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="expected 1.52.0, actual 1.51.1"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        failure = json.loads((run_root / "logs" / "playwright-mcp-version-mismatch.json").read_text())
        assert failure["expected_version"] == "1.52.0"
        assert failure["actual_version"] == "1.51.1"
        assert any(
            call[:2] == ["docker", "run"] and "@playwright/test/package.json" in call[-1] for call in runner.calls
        )

    def test_playwright_mcp_version_detection_normalizes_semver_range(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "package.json").write_text(json.dumps({"devDependencies": {"@playwright/test": "^1.52.0"}}))
        _git_ok("add", "package.json", cwd=repo)
        _git_ok("commit", "-m", "add package manifest", cwd=repo)
        _git_ok("push", cwd=repo)
        runner = _FakeContainerRunner(playwright_version="1.52.0")
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["playwright_mcp"]["expected_version"] == "1.52.0"
        assert state["playwright_mcp"]["actual_version"] == "1.52.0"
        assert state["playwright_mcp"]["command"] == "npx"
        assert state["playwright_mcp"]["args"] == [
            "@playwright/mcp@1.52.0",
            "--headless",
            "--browser",
            "chromium",
        ]

    def test_sidecar_startup_failure_writes_service_diagnostics(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner(fail_compose_up=True)
        backend = self._make(runner, compose_file="compose.yaml")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="sidecar service startup failed"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        failure = json.loads((run_root / "logs" / "service-startup-failure.json").read_text())
        assert failure["failure_type"] == "service_startup"
        assert failure["failure_subtype"] == "sidecar_compose_failed"
        assert failure["topology"] == "sidecar"

    def test_in_worker_startup_failure_removes_container_and_preserves_diagnostic(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_in_worker_run=True)
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="in-worker service startup failed"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        state_path = run_root / "backend-state" / "container-backend-state.json"
        state = json.loads(state_path.read_text())

        # The captured container is removed immediately before the startup
        # error propagates, and no tracked container remains in state.
        assert any(call[:3] == ["docker", "rm", "-f"] and "container-123" in call for call in runner.calls)
        assert state.get("worker_container") in ("", None)
        assert "container-123" not in state.get("containers", [])
        assert not any(
            isinstance(process, dict) and process.get("container_id") == "container-123"
            for process in state.get("service_processes", [])
        )

        # The diagnostic (and run_root logs generally) survive teardown so the
        # failure can still be debugged.
        diagnostic = run_root / "logs" / "service-startup-failure.json"
        assert diagnostic.exists()
        failure = json.loads(diagnostic.read_text())
        assert failure["failure_type"] == "service_startup"
        assert failure["failure_subtype"] == "in_worker_container_failed"
        assert failure["topology"] == "in-worker"
        assert failure["container_id"] == "container-123"
        assert run_root.exists()

    def test_prepare_workspace_failure_tears_down_resources_but_keeps_logs(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_in_worker_run=True)
        # Volume workspace mode allocates a docker volume during
        # prepare_workspace; a startup failure must tear it down rather than
        # leaking it.
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            with pytest.raises(RuntimeError, match="in-worker service startup failed"):
                backend.prepare_workspace(
                    run_id="my-feature-abc",
                    spec_id="my-feature",
                    branch="code/my-feature--abc",
                    repo_root=repo,
                    base_ref="master",
                )

        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        # The workspace volume created earlier in prepare_workspace is removed.
        assert any(call[:4] == ["docker", "volume", "rm", "-f"] for call in runner.calls), runner.calls
        # The worker container is removed too.
        assert any(call[:3] == ["docker", "rm", "-f"] for call in runner.calls)
        # But the run root / diagnostic survive (teardown is not a full rmtree).
        assert run_root.exists()
        assert (run_root / "logs" / "service-startup-failure.json").exists()

    def test_prepare_workspace_removes_worker_visible_spec_state(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        source = run_root / "source"
        source.mkdir(parents=True)
        _git_ok("clone", "--local", str(repo), str(source), cwd=repo)
        leaked_state = source / ".spec-state" / "runs" / "my-feature-abc"
        leaked_state.mkdir(parents=True)
        (leaked_state / "implement-result.json").write_text('{"status": "passed"}\n')

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        assert handle.path == source
        assert not (source / ".spec-state").exists()

    def test_prepare_workspace_builds_deterministic_image_from_dockerfile(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo / ".spec-state").mkdir()
        (repo / ".spec-state" / "secret.json").write_text('{"token": "host-secret"}\n')
        (repo / ".spec.local.toml").write_text('token = "host-secret"\n')
        _git_ok("add", ".spec/worker.Dockerfile", "pyproject.toml", cwd=repo)
        _git_ok("commit", "-m", "add worker image inputs", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            image="",
            bootstrap_cache_command="pip install -e .",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        build_call = next(call for call in runner.calls if call[:2] == ["docker", "build"])
        generated = handle.outbox_path.parent / "container-build" / "Dockerfile"
        assert state["image"].startswith("spec-worker:")
        assert "--build-arg" in build_call
        generated_text = generated.read_text()
        assert "COPY dependency-inputs/ /workspace/bootstrap/source/" in generated_text
        assert "COPY . /workspace/bootstrap/source/" in generated_text
        assert 'cd /workspace/bootstrap/source && sh -lc "$SPEC_BOOTSTRAP_CACHE_COMMAND"' in generated_text
        assert generated_text.index("COPY dependency-inputs/ /workspace/bootstrap/source/") < generated_text.index(
            'RUN if [ -n "$SPEC_BOOTSTRAP_CACHE_COMMAND" ]'
        )
        assert generated_text.index('RUN if [ -n "$SPEC_BOOTSTRAP_CACHE_COMMAND" ]') < generated_text.index(
            "COPY . /workspace/bootstrap/source/"
        )
        assert "WORKDIR /workspace/bootstrap/source" in generated_text
        assert "/tmp/spec-dependency-inputs" not in generated_text
        assert (handle.outbox_path.parent / "container-build" / "dependency-inputs" / "pyproject.toml").is_file()
        assert not (handle.outbox_path.parent / "container-build" / ".spec-state").exists()
        assert not (handle.outbox_path.parent / "container-build" / ".spec.local.toml").exists()

    def test_prepare_workspace_preserves_nested_cache_input_paths(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        (repo / "Makefile").write_text("install:\n\tcd frontend && npm ci\n")
        (repo / "frontend").mkdir()
        (repo / "frontend" / "package.json").write_text('{"scripts": {}}\n')
        (repo / "frontend" / "package-lock.json").write_text("{}\n")
        _git_ok(
            "add",
            ".spec/worker.Dockerfile",
            "Makefile",
            "frontend/package.json",
            "frontend/package-lock.json",
            cwd=repo,
        )
        _git_ok("commit", "-m", "add nested manifests", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            image="",
            bootstrap_cache_command="make install",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        dependency_inputs = handle.outbox_path.parent / "container-build" / "dependency-inputs"
        assert (dependency_inputs / "Makefile").is_file()
        assert (dependency_inputs / "frontend" / "package.json").is_file()
        assert (dependency_inputs / "frontend" / "package-lock.json").is_file()

    def test_prepare_workspace_keeps_full_bootstrap_command_out_of_image_build(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add worker image", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            image="",
            bootstrap_install_command="make install",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_call = next(call for call in runner.calls if call[:2] == ["docker", "build"])
        generated_text = (handle.outbox_path.parent / "container-build" / "Dockerfile").read_text()
        # The bootstrap command must never reach the image build; the version
        # cache-bust arg is always passed and is not a secret.
        assert not any(arg.startswith("SPEC_BOOTSTRAP_CACHE_COMMAND") for arg in build_call)
        assert any(arg.startswith("SPEC_BUTLER_VERSION=") for arg in build_call)
        assert "make install" not in generated_text
        assert "SPEC_BOOTSTRAP_CACHE_COMMAND" not in generated_text

    def test_legacy_dockerfile_without_version_arg_builds_no_cache(self, tmp_path: Path):
        """A Dockerfile that never references SPEC_BUTLER_VERSION cannot
        cache-bust its spec install layer; the build must use --no-cache or a
        stale layer survives under the fresh tag."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text(
            "FROM python:3.12-slim\nRUN pip install 'specbutler @ git+https://x/spec.git'\n"
        )
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "legacy worker image", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, image="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_call = next(call for call in runner.calls if call[:2] == ["docker", "build"])
        assert "--no-cache" in build_call

    def test_template_dockerfile_with_version_arg_keeps_layer_cache(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text(
            "FROM python:3.12-slim\nARG SPEC_BUTLER_VERSION=unpinned\n"
            'RUN echo "specbutler ${SPEC_BUTLER_VERSION}" '
            "&& pip install 'specbutler @ git+https://x/spec.git'\n"
        )
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "template worker image", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, image="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_call = next(call for call in runner.calls if call[:2] == ["docker", "build"])
        assert "--no-cache" not in build_call
        assert any(str(arg).startswith("SPEC_BUTLER_VERSION=") for arg in build_call)

    def test_concurrent_workspace_preparation_builds_deterministic_image_once(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        dockerfile = repo / ".spec" / "worker.Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\n")
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add worker image", cwd=repo)
        runner = _FakeContainerRunner(
            cache_built_images=True,
            build_delay_seconds=0.1,
        )
        backend = self._make(runner, image="")

        def resolve(run_id: str) -> str:
            run_root = repo / ".spec-workspaces" / run_id
            return backend._resolve_worker_image(
                repo_root=repo,
                run_root=run_root,
                logs=run_root / "logs",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            tags = list(executor.map(resolve, ("run-a", "run-b")))

        assert tags[0] == tags[1]
        build_calls = [call for call in runner.calls if call[:2] == ["docker", "build"]]
        inspect_calls = [call for call in runner.calls if call[:3] == ["docker", "image", "inspect"]]
        assert len(build_calls) == 1
        assert len(inspect_calls) == 2
        logs = [
            (repo / ".spec-workspaces" / run_id / "logs" / "image-build.log").read_text()
            for run_id in ("run-a", "run-b")
        ]
        assert sum("built" in log for log in logs) == 1
        assert sum("Reused existing deterministic worker image" in log for log in logs) == 1

    def test_deterministic_image_tag_changes_with_cache_input_content(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        dockerfile = repo / "worker.Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\n")
        makefile = repo / "Makefile"
        makefile.write_text("install:\n\ttrue\n")
        _git_ok("add", "Makefile", "worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add image inputs", cwd=repo)
        backend = self._make(
            _FakeContainerRunner(),
            image="",
            bootstrap_cache_inputs=("Makefile",),
        )

        tag_before = backend._deterministic_image_tag(repo, dockerfile)
        makefile.write_text("install:\n\tpython -m pip install -e .\n")
        tag_after = backend._deterministic_image_tag(repo, dockerfile)

        assert tag_before != tag_after

    def test_deterministic_image_tag_changes_with_host_spec_version(self, tmp_path: Path):
        """A spec upgrade must produce a new image tag, or the old
        image keeps serving stale spec_runtime behind a current-looking name."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        dockerfile = repo / "worker.Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\n")
        backend = self._make(_FakeContainerRunner(), image="")

        with patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.0.0"):
            tag_a = backend._deterministic_image_tag(repo, dockerfile)
            tag_a_again = backend._deterministic_image_tag(repo, dockerfile)
        with patch("spec_runtime.execution_backend.host_spec_runtime_version", return_value="1.0.1"):
            tag_b = backend._deterministic_image_tag(repo, dockerfile)

        assert tag_a == tag_a_again
        assert tag_a != tag_b

    def test_prepare_workspace_redacts_bootstrap_cache_command_from_build_log(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add worker image", cwd=repo)
        runner = _FakeContainerRunner()
        cache_command = "pip install https://token:secret@example.invalid/private.whl"
        backend = self._make(
            runner,
            image="",
            bootstrap_cache_command=cache_command,
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_log = (handle.outbox_path.parent / "logs" / "image-build.log").read_text()
        build_call = next(call for call in runner.calls if call[:2] == ["docker", "build"])
        assert cache_command in " ".join(build_call)
        assert cache_command not in build_log
        assert "SPEC_BOOTSTRAP_CACHE_COMMAND=<redacted>" in build_log

    def test_image_build_uses_buildkit_when_build_ssh_set(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add worker image", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            image="",
            build_ssh="default",
            bootstrap_cache_command="pip install git+ssh://git@example.invalid/private.git",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_idx = next(i for i, call in enumerate(runner.calls) if call[:2] == ["docker", "build"])
        build_call = runner.calls[build_idx]
        build_env = runner.envs[build_idx]
        assert "--ssh" in build_call
        assert build_call[build_call.index("--ssh") + 1] == "default"
        # --ssh must appear after `build` and before `-t`.
        assert build_call.index("--ssh") < build_call.index("-t")
        assert build_env is not None
        assert build_env.get("DOCKER_BUILDKIT") == "1"
        generated = handle.outbox_path.parent / "container-build" / "Dockerfile"
        assert 'RUN --mount=type=ssh if [ -n "$SPEC_BOOTSTRAP_CACHE_COMMAND" ]' in generated.read_text()

    def test_image_build_unchanged_when_build_ssh_empty(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / ".spec").mkdir()
        (repo / ".spec" / "worker.Dockerfile").write_text("FROM python:3.12-slim\n")
        _git_ok("add", ".spec/worker.Dockerfile", cwd=repo)
        _git_ok("commit", "-m", "add worker image", cwd=repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, image="")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        build_idx = next(i for i, call in enumerate(runner.calls) if call[:2] == ["docker", "build"])
        build_call = runner.calls[build_idx]
        build_env = runner.envs[build_idx]
        assert "--ssh" not in build_call
        assert build_env is None

    def test_factory_uses_bootstrap_cache_only_when_enabled(self, tmp_path: Path):
        config = SpecRuntimeConfig(
            bootstrap_install_command="make install",
            bootstrap_cache=BootstrapCacheConfig(
                enabled=True,
                command="make deps",
                inputs=("Makefile", "frontend/package-lock.json"),
            ),
            execution=ExecutionConfig(
                backend="container",
                container=ContainerExecutionConfig(image="example/spec-worker:latest"),
            ),
        )

        backend = eb.get_execution_backend(config)

        assert isinstance(backend, eb.ContainerExecutionBackend)
        assert backend._bootstrap_install_command == "make install"
        assert backend._bootstrap_cache_command == "make deps"
        assert backend._bootstrap_cache_inputs == ("Makefile", "frontend/package-lock.json")

    def test_prepare_workspace_runs_full_bootstrap_install_in_container(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            bootstrap_install_command="pip install https://token:secret@example.invalid/private.whl",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        install_command = "pip install https://token:secret@example.invalid/private.whl"
        assert any(
            call[:2] == ["docker", "exec"] and call[-3:] == ["sh", "-lc", install_command] for call in runner.calls
        )
        command_logs = sorted((handle.outbox_path.parent / "logs").glob("*-container-command-*.log"))
        assert command_logs
        log_text = "\n".join(path.read_text() for path in command_logs)
        assert install_command not in log_text
        assert "<redacted>" in log_text

    def test_prepare_workspace_runs_bootstrap_install_in_sidecar_worker(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            compose_file="compose.yaml",
            bootstrap_install_command="make install",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        install_call = next(
            call
            for call in runner.calls
            if call[:2] == ["docker", "exec"] and call[-3:] == ["sh", "-lc", "make install"]
        )
        worker_run_call = next(
            call for call in runner.calls if call[:2] == ["docker", "run"] and "sleep infinity" in call
        )
        assert "container-123" in install_call
        assert "--network" in worker_run_call

    def test_run_command_executes_through_container_without_host_sensitive_mounts(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        # Use an explicit host-home sentinel rather than the ambient
        # ``Path.home()`` so the "host home not mounted" assertion below is
        # decoupled from the runner's HOME (which, in some CI/worker
        # environments, coincidentally equals the container's internal
        # ``/workspace/source`` working dir and would trip the check).
        host_home = "/host/home/agent"
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(
                backend,
                "_container_user_mapping",
                return_value="1000:1000",
            ),
        ):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

            result = backend.run_command(
                eb.CommandRequest(
                    argv=["pytest", "-q"],
                    cwd=handle.path,
                    env={
                        "APP_FEATURE_FLAG": "enabled",
                        "DATABASE_URL": "postgres://worker-db",
                        "GH_TOKEN": "forge-secret",
                        "GITHUB_TOKEN": "forge-secret",
                        "HOME": host_home,
                        "OPENAI_API_KEY": "agent-secret",
                        "PATH": "/host/bin",
                        "SIM_TEST_DATABASE_URL": "postgres://worker-test",
                        "SPEC_SECRET_TOKEN": "spec-secret",
                        "SPEC_TEST": "1",
                        "TEST_DATABASE_URL": "postgres://worker-test-db",
                        "TERM": "xterm-256color",
                    },
                )
            )

        run_call = runner.calls[-1]
        start_call = next(call for call in runner.calls if call[:2] == ["docker", "run"] and "sleep infinity" in call)
        start_call_text = " ".join(start_call)
        assert result.returncode == 0
        assert run_call[:2] == ["docker", "exec"]
        assert f"{handle.path}:/workspace/source" in start_call
        assert f"{handle.outbox_path}:/workspace/outbox" in start_call
        assert f"{handle.outbox_path.parent / 'logs'}:/workspace/logs" in start_call
        assert "--tmpfs" in start_call
        assert "/workspace/source/.spec-state:rw,noexec,nosuid,nodev,mode=1777" in start_call
        assert "--user" in start_call
        assert start_call[start_call.index("--user") + 1] == "1000:1000"
        # Non-agent commands must not see the live completion outbox (fixture
        # tests exercising `spec report` would pollute it) and get HOME pinned
        # to the isolated home instead of the container default.
        assert "SPEC_COMPLETION_OUTBOX=" in run_call
        assert "SPEC_COMPLETION_OUTBOX=/workspace/outbox/completion-report.json" not in run_call
        assert (
            "PATH=/workspace/bootstrap/source/.venv/bin:"
            "/workspace/source/.venv/bin:"
            "/workspace/bootstrap/source/node_modules/.bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ) in run_call
        assert "NODE_PATH=/workspace/bootstrap/source/node_modules" in run_call
        assert "/var/run/docker.sock" not in start_call_text
        assert host_home not in start_call_text
        assert "GH_TOKEN=forge-secret" not in run_call
        assert "GITHUB_TOKEN=forge-secret" not in run_call
        assert f"HOME={host_home}" not in run_call
        assert "HOME=/workspace/source/.spec-claude-home" in run_call
        assert "OPENAI_API_KEY=agent-secret" not in run_call
        assert "PATH=/host/bin" not in run_call
        assert "SPEC_SECRET_TOKEN=spec-secret" not in run_call
        assert "APP_FEATURE_FLAG=enabled" in run_call
        assert "DATABASE_URL=postgres://worker-db" in run_call
        assert "SIM_TEST_DATABASE_URL=postgres://worker-test" in run_call
        assert "SPEC_TEST=1" in run_call
        assert "TEST_DATABASE_URL=postgres://worker-test-db" in run_call
        assert "TERM=xterm-256color" in run_call
        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert "container-123" in state["containers"]

    def test_agent_launch_keeps_live_completion_outbox(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        result = backend.launch_agent(
            eb.AgentRequest(
                argv=["claude", "-p", "implement"],
                cwd=handle.path,
            )
        )

        agent_call = runner.calls[-1]
        assert result.returncode == 0
        assert agent_call[:2] == ["docker", "exec"]
        # Agents report completion through the outbox, so they keep the real
        # path — and their HOME comes from the launch env, never the pin.
        assert "SPEC_COMPLETION_OUTBOX=/workspace/outbox/completion-report.json" in agent_call
        assert "HOME=/workspace/source/.spec-claude-home" not in agent_call

    def test_non_agent_path_includes_workspace_venv_bin(self, tmp_path: Path):
        # Gate/prep commands run with the default cache-disabled bootstrap,
        # which creates the venv at /workspace/source/.venv. That bin dir must
        # be on PATH so bare ``pytest`` / ``ruff check .`` gates resolve, while
        # the baked bootstrap venv stays first for cached-layer images.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(
            eb.CommandRequest(argv=["pytest", "-q"], cwd=handle.path)
        )

        run_call = runner.calls[-1]
        path_arg = run_call[run_call.index("PATH=/workspace/bootstrap/source/.venv/bin:"
            "/workspace/source/.venv/bin:"
            "/workspace/bootstrap/source/node_modules/.bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")]
        entries = path_arg[len("PATH="):].split(":")
        assert "/workspace/source/.venv/bin" in entries
        # Bootstrap venv keeps priority so cached-layer tools win when both exist.
        assert entries.index("/workspace/bootstrap/source/.venv/bin") < entries.index(
            "/workspace/source/.venv/bin"
        )

    def test_agent_launch_keeps_bootstrap_path_contract(self, tmp_path: Path):
        # Agent launches must NOT get the workspace venv implicitly injected —
        # their HOME/PATH contract is deliberate.
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.launch_agent(
            eb.AgentRequest(argv=["claude", "-p", "implement"], cwd=handle.path)
        )

        agent_call = runner.calls[-1]
        assert f"PATH={eb.CONTAINER_BOOTSTRAP_PATH}" in agent_call
        assert "/workspace/source/.venv/bin" not in " ".join(
            arg for arg in agent_call if arg.startswith("PATH=")
        )

    def test_codex_agent_uses_container_as_sandbox_boundary(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        result = backend.launch_agent(
            eb.AgentRequest(
                argv=[
                    "codex",
                    "-a",
                    "never",
                    "-s",
                    "workspace-write",
                    "exec",
                    "do the thing",
                ],
                cwd=handle.path,
            )
        )

        agent_call = runner.calls[-1]
        assert result.returncode == 0
        assert agent_call[:2] == ["docker", "exec"]
        sandbox_index = agent_call.index("-s")
        assert agent_call[sandbox_index + 1] == "danger-full-access"

    def test_run_command_allows_workspace_scoped_home_env(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(
            eb.CommandRequest(
                argv=["claude", "--version"],
                cwd=handle.path,
                env={"HOME": str(handle.path / ".spec-claude-home")},
            )
        )

        run_call = runner.calls[-1]
        assert "HOME=/workspace/source/.spec-claude-home" in run_call
        assert str(handle.path / ".spec-claude-home") not in run_call

    def test_run_command_allows_claude_secret_env_without_argv_values(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(
            eb.CommandRequest(
                argv=["claude", "-p", "Reply OK"],
                cwd=handle.path,
                env={
                    "ANTHROPIC_API_KEY": "anthropic-secret",
                    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
                },
            )
        )

        run_call = runner.calls[-1]
        assert "ANTHROPIC_API_KEY" in run_call
        assert "ANTHROPIC_API_KEY=anthropic-secret" not in run_call
        assert "CLAUDE_CODE_OAUTH_TOKEN" in run_call
        assert "CLAUDE_CODE_OAUTH_TOKEN=oauth-secret" not in run_call
        assert runner.envs[-1] is not None
        assert runner.envs[-1]["ANTHROPIC_API_KEY"] == "anthropic-secret"
        assert runner.envs[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"

        command_log = next((handle.outbox_path.parent / "logs").glob("*container-command-docker.log"))
        command_log_text = command_log.read_text()
        assert "anthropic-secret" not in command_log_text
        assert "oauth-secret" not in command_log_text
        assert "ANTHROPIC_API_KEY" in command_log_text
        assert "CLAUDE_CODE_OAUTH_TOKEN" in command_log_text

    def test_backend_injects_postgres_env_for_in_worker_topology(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(eb.CommandRequest(argv=["env"], cwd=handle.path))

        run_call = runner.calls[-1]
        assert "DATABASE_URL=postgresql://spec:spec@127.0.0.1:5432/spec" in run_call
        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["service_env_redactions"]["DATABASE_URL"] == "<redacted>"
        command_log = next((handle.outbox_path.parent / "logs").glob("*container-command-docker.log"))
        command_log_text = command_log.read_text()
        assert "postgresql://spec:spec@127.0.0.1:5432/spec" not in command_log_text
        assert "DATABASE_URL=<redacted>" in command_log_text

    def test_backend_injects_postgres_env_and_network_for_sidecar_topology(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(eb.CommandRequest(argv=["pytest"], cwd=handle.path))

        exec_call = runner.calls[-1]
        worker_run_call = next(
            call for call in runner.calls if call[:2] == ["docker", "run"] and "sleep infinity" in call
        )
        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert exec_call[:2] == ["docker", "exec"]
        assert "DATABASE_URL=postgresql://spec:spec@postgres:5432/spec" in exec_call
        assert "--network" in worker_run_call
        assert worker_run_call[worker_run_call.index("--network") + 1] == state["service_networks"][0]
        command_log = next((handle.outbox_path.parent / "logs").glob("*container-command-docker.log"))
        command_log_text = command_log.read_text()
        assert "postgresql://spec:spec@postgres:5432/spec" not in command_log_text
        assert "DATABASE_URL=<redacted>" in command_log_text

    def test_bind_mode_runs_with_host_user_mapping_for_writable_mounts(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="bind", system_name="Linux")
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(
                backend,
                "_container_user_mapping",
                return_value="1000:1000",
            ),
        ):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

            backend.run_command(
                eb.CommandRequest(
                    argv=["sh", "-lc", "touch out"],
                    cwd=handle.path,
                )
            )

        run_call = runner.calls[-1]
        start_call = next(call for call in runner.calls if call[:2] == ["docker", "run"] and "sleep infinity" in call)
        assert run_call[:2] == ["docker", "exec"]
        assert "--user" in start_call
        assert start_call[start_call.index("--user") + 1] == "1000:1000"
        assert f"{handle.path}:/workspace/source" in start_call
        assert f"{handle.outbox_path}:/workspace/outbox" in start_call
        assert f"{handle.outbox_path.parent / 'logs'}:/workspace/logs" in start_call

    def test_run_command_translates_workspace_paths_for_worker(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        source_script = handle.path / "scripts" / "setup.py"
        outbox_file = handle.outbox_path / "result.json"
        logs_dir = handle.outbox_path.parent / "logs"
        host_prompt = f"Work in {handle.path}; write {outbox_file}; logs are under {logs_dir}."
        result = backend.run_command(
            eb.CommandRequest(
                argv=[
                    "python",
                    str(source_script),
                    "--worktree",
                    str(handle.path),
                    "--outbox",
                    str(outbox_file),
                    host_prompt,
                ],
                cwd=handle.path,
                env={
                    "SPEC_WORKTREE": str(handle.path),
                    "SPEC_OUTBOX": str(outbox_file),
                    "SPEC_LOGS": str(logs_dir),
                },
            )
        )

        run_call = runner.calls[-1]
        worker_argv = run_call[run_call.index("container-123") + 1 :]
        worker_text = " ".join(worker_argv)
        assert result.returncode == 0
        assert "/workspace/source/scripts/setup.py" in worker_argv
        assert "/workspace/source" in worker_argv
        assert "/workspace/outbox/result.json" in worker_argv
        assert "/workspace/logs." in worker_text
        assert "SPEC_WORKTREE=/workspace/source" in run_call
        assert "SPEC_OUTBOX=/workspace/outbox/result.json" in run_call
        assert "SPEC_LOGS=/workspace/logs" in run_call
        assert str(handle.path) not in worker_text
        assert str(handle.outbox_path) not in worker_text
        assert str(logs_dir) not in worker_text

    def test_container_report_writes_completion_outbox_without_host_state(self, tmp_path: Path):
        outbox_result = tmp_path / "outbox" / "completion-report.json"
        args = argparse.Namespace(spec="", run="", status="ok", summary="done")

        with patch.dict(
            os.environ,
            {
                "SPEC_COMPLETION_OUTBOX": str(outbox_result),
                "SPEC_ID": "my-feature",
                "SPEC_RUN_ID": "my-feature-abc",
                "SPEC_ATTEMPT": "3",
            },
        ):
            rc = orch.cmd_report(args)

        payload = json.loads(outbox_result.read_text())
        result = payload["implement_result"]
        assert rc == 0
        assert payload["spec_id"] == "my-feature"
        assert payload["run_id"] == "my-feature-abc"
        assert result["status"] == "passed"
        assert result["summary"] == "done"
        assert result["attempt"] == 3
        assert result["result_source"] == "agent_report_outbox"

    def test_load_matching_result_imports_container_completion_outbox(self, tmp_path: Path):
        repo = tmp_path / "repo"
        source = repo / ".spec-workspaces" / "my-feature-abc" / "source"
        outbox = source.parent / "outbox"
        (source.parent / "logs").mkdir(parents=True)
        (source.parent / "backend-state").mkdir()
        (source.parent / "backend-state" / "container-backend-state.json").write_text("{}\n")
        outbox.mkdir()
        result = orch.ImplementResult(
            status="passed",
            summary="done in container",
            attempt=2,
            result_source="agent_report_outbox",
        )
        (outbox / "completion-report.json").write_text(
            json.dumps(
                {
                    "artifact": "spec-container-completion-report",
                    "version": 1,
                    "spec_id": "my-feature",
                    "run_id": "my-feature-abc",
                    "implement_result": {
                        **result.__dict__,
                        "ignored_envelope_key": "does not break loading",
                    },
                }
            )
        )

        loaded, loaded_from_local = orch._load_matching_implement_result(
            repo_root=repo,
            worktree_path=source,
            run_id="my-feature-abc",
            attempt=2,
        )

        assert loaded is not None
        assert loaded_from_local is False
        assert loaded.summary == "done in container"
        assert orch.ImplementResult.load(repo, "my-feature-abc") is not None

    def test_load_matching_result_ignores_container_source_local_state(self, tmp_path: Path):
        repo = tmp_path / "repo"
        source = repo / ".spec-workspaces" / "my-feature-abc" / "source"
        (source.parent / "logs").mkdir(parents=True)
        (source.parent / "backend-state").mkdir()
        (source.parent / "backend-state" / "container-backend-state.json").write_text("{}\n")
        orch.ImplementResult(
            status="passed",
            summary="forged local state",
            attempt=2,
        ).save_to_state_root(source / ".spec-state", "my-feature-abc")

        loaded, loaded_from_local = orch._load_matching_implement_result(
            repo_root=repo,
            worktree_path=source,
            run_id="my-feature-abc",
            attempt=2,
        )

        assert loaded is None
        assert loaded_from_local is False
        assert orch.ImplementResult.load(repo, "my-feature-abc") is None

    def test_load_matching_result_keeps_clone_source_local_state_without_container_marker(self, tmp_path: Path):
        repo = tmp_path / "repo"
        source = repo / ".spec-workspaces" / "my-feature-abc" / "source"
        (source.parent / "logs").mkdir(parents=True)
        orch.ImplementResult(
            status="passed",
            summary="clone local fallback",
            attempt=2,
        ).save_to_state_root(source / ".spec-state", "my-feature-abc")

        loaded, loaded_from_local = orch._load_matching_implement_result(
            repo_root=repo,
            worktree_path=source,
            run_id="my-feature-abc",
            attempt=2,
        )

        assert loaded is not None
        assert loaded.summary == "clone local fallback"
        assert loaded_from_local is True

    def test_load_matching_result_prefers_container_outbox_over_source_local_state(self, tmp_path: Path):
        repo = tmp_path / "repo"
        source = repo / ".spec-workspaces" / "my-feature-abc" / "source"
        outbox = source.parent / "outbox"
        (source.parent / "logs").mkdir(parents=True)
        (source.parent / "backend-state").mkdir()
        (source.parent / "backend-state" / "container-backend-state.json").write_text("{}\n")
        outbox.mkdir()
        orch.ImplementResult(
            status="passed",
            summary="forged local state",
            attempt=2,
        ).save_to_state_root(source / ".spec-state", "my-feature-abc")
        outbox_result = orch.ImplementResult(
            status="passed",
            summary="trusted outbox",
            attempt=2,
            result_source="agent_report_outbox",
        )
        (outbox / "completion-report.json").write_text(
            json.dumps(
                {
                    "artifact": "spec-container-completion-report",
                    "version": 1,
                    "spec_id": "my-feature",
                    "run_id": "my-feature-abc",
                    "implement_result": outbox_result.__dict__,
                }
            )
        )

        loaded, loaded_from_local = orch._load_matching_implement_result(
            repo_root=repo,
            worktree_path=source,
            run_id="my-feature-abc",
            attempt=2,
        )

        assert loaded is not None
        assert loaded.summary == "trusted outbox"
        assert loaded_from_local is False

    def test_auto_workspace_mode_uses_volume_on_macos(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            workspace_mode="auto",
            system_name="Darwin",
        )

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        assert state["workspace_mode"] == "volume"
        assert state["volumes"]
        volume_create = next(call for call in runner.calls if call[:3] == ["docker", "volume", "create"])
        assert "spec.owner=spec-runtime" in volume_create
        assert "spec.run_id=my-feature-abc" in volume_create
        seed_calls = [call for call in runner.calls if "/workspace/seed:ro" in " ".join(call)]
        assert seed_calls
        assert "spec.owner=spec-runtime" in seed_calls[-1]
        assert "spec.run_id=my-feature-abc" in seed_calls[-1]
        assert "find /workspace/source -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in " ".join(seed_calls[-1])

    def test_ignores_worker_writable_container_state_for_mounts_and_cleanup(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        host_state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        expected_volume = host_state["volumes"][0]
        (handle.outbox_path / "container-backend-state.json").write_text(
            json.dumps(
                host_state
                | {
                    "image": "attacker/image",
                    "volumes": ["/:/workspace/source"],
                    "networks": ["attacker-network"],
                }
            )
        )

        backend.run_command(eb.CommandRequest(argv=["true"], cwd=handle.path))
        start_call = next(call for call in runner.calls if call[:2] == ["docker", "run"] and "sleep infinity" in call)
        exec_call = next(call for call in runner.calls if call[:2] == ["docker", "exec"])
        assert f"{expected_volume}:/workspace/source" in start_call
        assert "/:/workspace/source" not in start_call
        assert "attacker/image" not in exec_call

        backend.cleanup(handle)
        cleanup_text = "\n".join(" ".join(call) for call in runner.calls)
        assert f"volume rm -f {expected_volume}" in cleanup_text
        assert "volume rm -f /:/workspace/source" not in cleanup_text
        assert "network rm attacker-network" not in cleanup_text

    def test_volume_mode_imports_worker_changes_back_to_host(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(eb.CommandRequest(argv=["sh", "-lc", "touch file"], cwd=handle.path))

        import_calls = [
            call
            for call in runner.calls
            if "/workspace/source:ro" in " ".join(call) and "/workspace/host" in " ".join(call)
        ]
        assert import_calls
        assert f"{handle.path}:/workspace/host" in import_calls[-1]

    def test_volume_mode_import_uses_bounded_find_cleanup(self, tmp_path: Path):
        """The import cleanup must bound find to the
        top-level entries (-maxdepth 1) so find never descends into a directory
        that rm -rf has already deleted (notably under .git/objects), which made
        find exit nonzero and skip the cp -a import."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.run_command(eb.CommandRequest(argv=["sh", "-lc", "touch file"], cwd=handle.path))

        import_calls = [
            call
            for call in runner.calls
            if "/workspace/source:ro" in " ".join(call) and "/workspace/host" in " ".join(call)
        ]
        assert import_calls
        joined = " ".join(import_calls[-1])
        assert "find /workspace/host -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in joined
        assert "find /workspace/host -mindepth 1 -exec rm -rf {} +" not in joined
        assert "find /workspace/source -mindepth 1 -maxdepth 1 -exec sh -c" in joined
        assert 'cp -a "$@" /workspace/host/' in joined
        assert "cp -a -t /workspace/host" not in joined
        assert "cp -a /workspace/source/. /workspace/host/" not in joined

    def test_volume_mode_import_failure_is_typed_with_artifacts(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_volume_import=True)
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        with pytest.raises(eb.ExecutionBackendImportError) as exc_info:
            backend.run_command(eb.CommandRequest(argv=["sh", "-lc", "touch file"], cwd=handle.path))

        log_path = handle.outbox_path.parent / "logs" / "volume-import.log"
        failure_path = handle.outbox_path.parent / "logs" / "volume-import-failure.json"
        failure = json.loads(failure_path.read_text())
        assert log_path in exc_info.value.artifact_paths
        assert failure_path in exc_info.value.artifact_paths
        assert failure["failure_type"] == "import"
        assert failure["failure_subtype"] == "volume_workspace_import_failed"
        assert failure["log_path"] == str(log_path)
        assert "copy failed" in log_path.read_text()

    def test_volume_cleanup_syncs_volume_before_deletability_guard(self, tmp_path: Path):
        """Regression: in volume mode the authoritative git state lives inside
        the Docker volume. If a crash (or a cleanup/resume before the post-run
        sync) leaves work only in the volume, the host mirror can read clean.
        ``cleanup`` must sync the volume back to the host and re-check
        deletability *before* removing any docker resources, so unsynced agent
        work is never destroyed by ``volume rm -f``."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        # Host mirror is clean — the never-synced work exists only in the volume.
        source = (handle.outbox_path.parent / "source").resolve()
        assert backend._untracked_files(source) == []

        def _fake_sync(run_root: Path, state: dict) -> None:
            # The volume→host import surfaces work that never reached the host.
            (Path(run_root) / "source" / "volume_only.py").write_text("agent work\n")

        with patch.object(
            backend, "_sync_volume_workspace_to_host", side_effect=_fake_sync
        ) as synced:
            with pytest.raises(eb.WorkspaceHasUnpushedWorkError, match="untracked file"):
                backend.cleanup(handle)

        synced.assert_called_once()
        # The guard fired before any docker resource was torn down.
        assert not any("volume rm -f" in " ".join(call) for call in runner.calls)
        assert source.exists()

        # The explicit post-merge / spec-clean opt-out skips the guard (and the
        # extra sync) and still deletes the workspace.
        with patch.object(
            backend, "_sync_volume_workspace_to_host", side_effect=_fake_sync
        ) as synced_allowed:
            backend.cleanup(handle, allow_unpushed_work=True)
        synced_allowed.assert_not_called()
        assert not source.exists()

    def test_volume_restore_syncs_volume_before_rescuing_unpushed_work(self, tmp_path: Path):
        """Regression: in volume mode the authoritative git state lives inside the
        Docker volume. ``restore`` rescues unpushed work from the host ``source``
        mirror and then reseeds the volume from the restored tree — so work that
        reached only the volume (a crash before the post-run sync) would be
        overwritten by the reseed before the rescue ever saw it. ``restore`` must
        sync the volume back to the host *before* the rescue so volume-only agent
        work is captured in the rescue snapshot."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        snapshot = backend.snapshot(handle, "pre-implement")

        # Host mirror is clean — the never-synced work exists only in the volume.
        source = (handle.outbox_path.parent / "source").resolve()
        assert backend._untracked_files(source) == []

        def _fake_sync(run_root: Path, state: dict) -> None:
            # The volume→host import surfaces work that never reached the host.
            (Path(run_root) / "source" / "volume_only.py").write_text("agent work\n")

        with patch.object(
            backend, "_sync_volume_workspace_to_host", side_effect=_fake_sync
        ) as synced:
            restored = backend.restore(handle, snapshot)

        synced.assert_called_once()
        # The rescue ran *after* the sync surfaced the volume-only file, so it was
        # captured before the reseed could discard it.
        rescue_index = json.loads((restored.outbox_path.parent / "rescue" / "index.json").read_text())
        assert rescue_index
        assert "volume_only.py" in rescue_index[-1].get("untracked_files", [])

    def test_sync_host_paths_into_workspace_copies_into_volume(self, tmp_path: Path):
        """Orchestrator-written paths (.spec-codex-home, .claude/mcp-servers.json)
        must be pushed into the container volume after seeding so the agent
        running inside the container sees them at /workspace/source."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        (handle.path / ".spec-codex-home").mkdir()
        (handle.path / ".spec-codex-home" / "config.toml").write_text("")
        runner.calls.clear()

        backend.sync_host_paths_into_workspace(handle.path, (".spec-codex-home",))

        sync_calls = [
            call
            for call in runner.calls
            if "/workspace/host:ro" in " ".join(call) and ".spec-codex-home" in " ".join(call)
        ]
        assert sync_calls, runner.calls
        last = sync_calls[-1]
        # Volume is mounted rw; host source is mounted ro under /workspace/host.
        host_state = json.loads(Path(handle.metadata["container_state_path"]).read_text())
        expected_volume = host_state["volumes"][0]
        assert f"{expected_volume}:/workspace/source" in last
        assert f"{handle.path}:/workspace/host:ro" in last
        joined = " ".join(last)
        assert "cp -a /workspace/host/.spec-codex-home /workspace/source/.spec-codex-home" in joined

    def test_sync_host_paths_into_workspace_skips_bind_mode(self, tmp_path: Path):
        """Bind mode binds the host worktree directly; the sync helper has
        nothing to do."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="bind")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        (handle.path / ".spec-codex-home").mkdir()
        runner.calls.clear()

        backend.sync_host_paths_into_workspace(handle.path, (".spec-codex-home",))

        assert runner.calls == []

    def test_sync_host_paths_into_workspace_skips_missing_paths(self, tmp_path: Path):
        """Relative paths that do not exist on the host are silently skipped."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        runner.calls.clear()

        backend.sync_host_paths_into_workspace(handle.path, (".does-not-exist",))

        sync_calls = [call for call in runner.calls if "/workspace/host:ro" in " ".join(call)]
        assert sync_calls == []

    @pytest.mark.skipif(os.name == "nt", reason="non-elevated Windows cannot create file symlinks")
    def test_snapshot_restore_and_cleanup_are_idempotent(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        (handle.path / "file.txt").write_text("before\n")
        (handle.path / "target.txt").write_text("target\n")
        os.symlink("target.txt", handle.path / "link.txt")
        _git_ok("add", "file.txt", cwd=handle.path)
        _git_ok("add", "target.txt", "link.txt", cwd=handle.path)
        _git_ok("commit", "-m", "snapshot base", cwd=handle.path)
        before_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()

        snapshot = backend.snapshot(handle, "pre-implement")
        (handle.path / "file.txt").write_text("after\n")
        (handle.path / "link.txt").unlink()
        (handle.path / "link.txt").write_text("dereferenced\n")
        _git_ok("add", "file.txt", cwd=handle.path)
        _git_ok("add", "link.txt", cwd=handle.path)
        _git_ok("commit", "-m", "failed attempt", cwd=handle.path)
        after_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()
        assert after_head != before_head
        restored = backend.restore(handle, snapshot)

        assert (restored.path / "file.txt").exists()
        assert (restored.path / "file.txt").read_text() == "before\n"
        assert (restored.path / "link.txt").is_symlink()
        assert os.readlink(restored.path / "link.txt") == "target.txt"
        assert (snapshot.path / "link.txt").is_symlink()
        assert snapshot.metadata["service_topology"] == "in-worker"
        assert snapshot.metadata["service_data_dirs"] == [str(handle.path / ".local" / "postgres" / "data")]
        assert _git("rev-parse", "HEAD", cwd=restored.path).stdout.strip() == before_head
        assert _git("status", "--short", cwd=restored.path).stdout.strip() == ""
        # The restore rolled the tree back over unpushed commits, so it first
        # rescued the discarded work into the run's rescue directory.
        rescue_index = json.loads((restored.outbox_path.parent / "rescue" / "index.json").read_text())
        assert rescue_index and rescue_index[-1]["unpushed_commits"]
        # The branch still carries unpushed commits, so cleanup is only allowed
        # with the explicit post-merge opt-out.
        backend.cleanup(restored, allow_unpushed_work=True)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_sidecar_snapshot_stops_services_and_records_volume_scope(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        snapshot = backend.snapshot(handle, "pre-implement")

        compose_calls = [call for call in runner.calls if call[:3] == ["docker", "compose", "-p"]]
        stop_index = next(index for index, call in enumerate(compose_calls) if "stop" in call)
        restart_index = next(
            index for index, call in enumerate(compose_calls[stop_index + 1 :], start=stop_index + 1) if "up" in call
        )
        assert stop_index < restart_index
        assert snapshot.metadata["service_topology"] == "sidecar"
        assert snapshot.metadata["service_volumes"]
        assert snapshot.metadata["service_volume_snapshots"]
        assert any(
            call[:2] == ["docker", "run"] and any("/workspace/service-volume:ro" in item for item in call)
            for call in runner.calls
        )

    def test_sidecar_snapshot_aborts_when_services_do_not_stop_cleanly(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner(fail_compose_stop=True)
        backend = self._make(runner, compose_file="compose.yaml")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        with pytest.raises(RuntimeError, match="could not cleanly stop sidecar services"):
            backend.snapshot(handle, "pre-implement")

        assert not any(
            call[:2] == ["docker", "run"] and any("/workspace/service-volume:ro" in item for item in call)
            for call in runner.calls
        )

    def test_sidecar_restore_stops_services_before_importing_volume_snapshot(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        snapshot = backend.snapshot(handle, "pre-implement")
        runner.calls.clear()

        backend.restore(handle, snapshot)

        compose_stop_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:3] == ["docker", "compose", "-p"] and "stop" in call
        )
        import_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "run"]
            and any(":/workspace/service-volume" in item for item in call)
            and any(":/workspace/service-volume-snapshot:ro" in item for item in call)
        )
        compose_up_index = next(
            index for index, call in enumerate(runner.calls) if call[:3] == ["docker", "compose", "-p"] and "up" in call
        )
        assert compose_stop_index < import_index
        assert import_index < compose_up_index

    def test_in_worker_restore_recreates_persistent_worker_container(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        (handle.path / ".local" / "postgres" / "data").mkdir(parents=True)
        (handle.path / ".local" / "postgres" / "data" / "seeded.txt").write_text("seeded\n")
        _git_ok("add", ".local/postgres/data/seeded.txt", cwd=handle.path)
        _git_ok("commit", "-m", "seed postgres data", cwd=handle.path)
        snapshot = backend.snapshot(handle, "pre-implement")
        (handle.path / ".local" / "postgres" / "data" / "seeded.txt").write_text("mutated\n")
        runner.calls.clear()

        restored = backend.restore(handle, snapshot)

        rm_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:3] == ["docker", "rm", "-f"] and "container-123" in call
        )
        restart_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "run"] and "sleep infinity" in call[-1]
        )
        state = json.loads((restored.outbox_path.parent / "backend-state" / "container-backend-state.json").read_text())
        assert rm_index < restart_index
        assert (restored.path / ".local" / "postgres" / "data" / "seeded.txt").read_text() == "seeded\n"
        assert state["worker_container"] == "container-123"
        assert state["service_processes"][0]["container_id"] == "container-123"

    def test_restore_reruns_bootstrap_install_after_worker_reset(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, bootstrap_install_command="make install")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        snapshot = backend.snapshot(handle, "pre-implement")
        runner.calls.clear()

        backend.restore(handle, snapshot)

        rm_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:3] == ["docker", "rm", "-f"] and "container-123" in call
        )
        restart_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "run"] and "sleep infinity" in call[-1]
        )
        install_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "exec"] and call[-3:] == ["sh", "-lc", "make install"]
        )
        assert rm_index < restart_index
        assert restart_index < install_index

    def test_reseed_workspace_volume_reinstalls_dependencies_after_seed(self, tmp_path: Path):
        """Re-seeding the worker volume after repositioning the
        host source wipes /workspace/source (including bootstrap-installed
        dependencies). The reseed must rerun the bootstrap install so the volume
        is not left dependency-less before the implement agent launches."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            workspace_mode="volume",
            bootstrap_install_command="make install",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        runner.calls.clear()

        backend.reseed_workspace_volume(handle)

        seed_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "run"]
            and any(item.endswith(":/workspace/seed:ro") for item in call)
            and "cp -a /workspace/seed/. /workspace/source/" in call[-1]
        )
        install_index = next(
            index
            for index, call in enumerate(runner.calls)
            if call[:2] == ["docker", "exec"] and call[-3:] == ["sh", "-lc", "make install"]
        )
        # Bootstrap install must run *after* the reseed, so the freshly copied
        # tree gets its dependencies reinstalled rather than being left bare.
        assert seed_index < install_index

    def test_reseed_workspace_volume_is_noop_in_bind_mode(self, tmp_path: Path):
        """The reseed step only applies to volume mode; a bind-mode workspace
        (where the host worktree is the workspace) must not seed or reinstall."""
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(
            runner,
            workspace_mode="bind",
            bootstrap_install_command="make install",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        runner.calls.clear()

        backend.reseed_workspace_volume(handle)

        assert not any(
            call[:2] == ["docker", "run"] and any(item.endswith(":/workspace/seed:ro") for item in call)
            for call in runner.calls
        )
        assert not any(call[-3:] == ["sh", "-lc", "make install"] for call in runner.calls)

    def test_cleanup_removes_sidecars_after_successful_startup(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        (repo / "compose.yaml").write_text("services: {}\n")
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        backend.cleanup(handle)

        compose_calls = [call for call in runner.calls if call[:3] == ["docker", "compose", "-p"]]
        assert any("down" in call and "--volumes" in call for call in compose_calls)
        assert not (repo / ".spec-workspaces" / "my-feature-abc").exists()

    def test_stale_service_cleanup_uses_persisted_state_without_running_worker(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, compose_file="compose.yaml")
        run_root = repo / ".spec-workspaces" / "my-feature-abc"
        source = run_root / "source"
        outbox = run_root / "outbox"
        state_path = run_root / "backend-state" / "container-backend-state.json"
        source.mkdir(parents=True)
        outbox.mkdir(parents=True)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "service_topology": "sidecar",
                    "compose_file": str(repo / "compose.yaml"),
                    "compose_project": "spec-stale",
                    "containers": ["container-123"],
                    "volumes": ["spec-stale-postgres-data"],
                    "networks": ["spec-stale-network"],
                    "service_processes": [],
                    "service_data_dirs": [],
                }
            )
        )

        backend.cleanup(
            eb.WorkspaceHandle(
                path=source,
                outbox_path=outbox,
                branch="code/my-feature--abc",
                backend="container",
            )
        )

        cleanup_text = "\n".join(" ".join(call) for call in runner.calls)
        assert "docker compose -p spec-stale" in cleanup_text
        assert "docker rm -f container-123" in cleanup_text
        assert "docker volume rm -f spec-stale-postgres-data" in cleanup_text
        assert "docker network rm spec-stale-network" in cleanup_text

    def test_missing_snapshot_restore_recreates_fresh_workspace(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )
        (handle.path / "dirty.txt").write_text("failed retry state\n")
        _git_ok("add", "dirty.txt", cwd=handle.path)
        _git_ok("commit", "-m", "failed retry", cwd=handle.path)
        dirty_head = _git("rev-parse", "HEAD", cwd=handle.path).stdout.strip()

        missing = eb.SnapshotRef(
            label="pre-implement",
            path=handle.outbox_path.parent / "snapshots" / "pre-implement",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"):
            restored = backend.restore(handle, missing)

        restored_head = _git("rev-parse", "HEAD", cwd=restored.path).stdout.strip()
        fallback_log = restored.outbox_path.parent / "logs" / "snapshot-restore-fallback.log"
        assert restored.path == handle.path
        assert not (restored.path / "dirty.txt").exists()
        assert restored_head != dirty_head
        assert _git("status", "--short", cwd=restored.path).stdout.strip() == ""
        assert "snapshot path is missing" in fallback_log.read_text()
        assert "prepared fresh workspace" in fallback_log.read_text()

    def test_passwd_shim_files_are_created_with_runtime_uid(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(
            image_passwd="root:x:0:0:root:/root:/bin/sh\ndaemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n",
            image_group="root:x:0:\ndaemon:x:1:\n",
        )
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        passwd_path = run_root / "passwd-shim" / "passwd"
        group_path = run_root / "passwd-shim" / "group"
        assert passwd_path.is_file()
        assert group_path.is_file()
        passwd_text = passwd_path.read_text()
        group_text = group_path.read_text()
        uid = os.getuid()
        gid = os.getgid()
        assert "root:x:0:0:root:/root:/bin/sh" in passwd_text
        assert f"spec:x:{uid}:{gid}:spec runtime user:/workspace/source:/bin/sh" in passwd_text
        assert "root:x:0:" in group_text
        assert f"spec:x:{gid}:" in group_text

    def test_passwd_shim_does_not_duplicate_existing_uid(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        uid = os.getuid()
        gid = os.getgid()
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(
            image_passwd=(f"root:x:0:0:root:/root:/bin/sh\nnode:x:{uid}:{gid}:node user:/home/node:/bin/sh\n"),
            image_group=f"root:x:0:\nnode:x:{gid}:\n",
        )
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        passwd_text = (run_root / "passwd-shim" / "passwd").read_text()
        group_text = (run_root / "passwd-shim" / "group").read_text()
        matching_passwd_lines = [
            line for line in passwd_text.splitlines() if line and line.split(":")[2:3] == [str(uid)]
        ]
        matching_group_lines = [line for line in group_text.splitlines() if line and line.split(":")[2:3] == [str(gid)]]
        assert len(matching_passwd_lines) == 1
        assert matching_passwd_lines[0].startswith("node:")
        assert "spec:x:" not in passwd_text
        assert len(matching_group_lines) == 1
        assert matching_group_lines[0].startswith("node:")

    def test_passwd_shim_falls_back_to_baseline_when_image_lacks_passwd(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_cp=True)
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        passwd_text = (run_root / "passwd-shim" / "passwd").read_text()
        group_text = (run_root / "passwd-shim" / "group").read_text()
        uid = os.getuid()
        gid = os.getgid()
        assert "root:x:0:0:root:/root:/bin/sh" in passwd_text
        assert "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin" in passwd_text
        assert f"spec:x:{uid}:{gid}:spec runtime user:/workspace/source:/bin/sh" in passwd_text
        assert "root:x:0:" in group_text
        assert "nogroup:x:65534:" in group_text
        assert f"spec:x:{gid}:" in group_text

    def test_in_worker_start_mounts_passwd_shim_when_user_mapping_set(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_cp=True)
        backend = self._make(runner, system_name="Linux")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        passwd_path = run_root / "passwd-shim" / "passwd"
        group_path = run_root / "passwd-shim" / "group"
        worker_call = next(
            call
            for call in runner.calls
            if call[:2] == ["docker", "run"] and call[-3:] == ["sh", "-lc", "sleep infinity"]
        )
        assert "--user" in worker_call
        user_idx = worker_call.index("--user")
        passwd_mount = f"{passwd_path}:/etc/passwd:ro"
        group_mount = f"{group_path}:/etc/group:ro"
        assert passwd_mount in worker_call
        assert group_mount in worker_call
        assert worker_call.index(passwd_mount) > user_idx
        assert worker_call.index(group_mount) > user_idx

    def test_run_command_run_branch_mounts_passwd_shim(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_cp=True)
        backend = self._make(runner)

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        # Force the no-worker-container fallback path.
        state_path = run_root / "backend-state" / "container-backend-state.json"
        state = json.loads(state_path.read_text())
        state["worker_container"] = ""
        state_path.write_text(json.dumps(state))

        runner.calls.clear()
        backend.run_command(
            eb.CommandRequest(
                argv=["echo", "hello"],
                cwd=handle.path,
            )
        )
        run_call = next(call for call in runner.calls if call[:3] == ["docker", "run", "--rm"])
        passwd_mount = f"{run_root / 'passwd-shim' / 'passwd'}:/etc/passwd:ro"
        group_mount = f"{run_root / 'passwd-shim' / 'group'}:/etc/group:ro"
        assert "--user" in run_call
        assert passwd_mount in run_call
        assert group_mount in run_call

    def test_passwd_shim_is_skipped_on_windows(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner()
        backend = self._make(runner, system_name="Windows")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        assert not (run_root / "passwd-shim").exists()
        worker_call = next(
            call
            for call in runner.calls
            if call[:2] == ["docker", "run"] and call[-3:] == ["sh", "-lc", "sleep infinity"]
        )
        assert "--user" not in worker_call
        assert not any(":/etc/passwd:ro" in item for item in worker_call)
        assert not any(":/etc/group:ro" in item for item in worker_call)

    def test_volume_import_mounts_passwd_shim(self, tmp_path: Path):
        if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
            pytest.skip("requires POSIX uid/gid")
        repo = tmp_path / "repo"
        _init_clone_source(repo)
        runner = _FakeContainerRunner(fail_cp=True)
        backend = self._make(runner, workspace_mode="volume")

        with patch("shutil.which", return_value="/usr/bin/docker"):
            handle = backend.prepare_workspace(
                run_id="my-feature-abc",
                spec_id="my-feature",
                branch="code/my-feature--abc",
                repo_root=repo,
                base_ref="master",
            )

        run_root = handle.outbox_path.parent
        runner.calls.clear()
        backend.run_command(
            eb.CommandRequest(
                argv=["echo", "hello"],
                cwd=handle.path,
            )
        )
        # Volume import path runs after each command; locate the import
        # docker run that mounts /workspace/host.
        import_call = next(
            call
            for call in runner.calls
            if call[:3] == ["docker", "run", "--rm"] and any("/workspace/host" in item for item in call)
        )
        passwd_mount = f"{run_root / 'passwd-shim' / 'passwd'}:/etc/passwd:ro"
        group_mount = f"{run_root / 'passwd-shim' / 'group'}:/etc/group:ro"
        assert "--user" in import_call
        assert passwd_mount in import_call
        assert group_mount in import_call


# ---------------------------------------------------------------------------
# Orchestrator seam
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Fake backend used to verify the orchestrator routes through the seam."""

    workspace_path: Path
    outbox_path: Path
    safety_mode: str = "safe"
    backend_name: str = "worktree"
    prepare_calls: list[dict] = field(default_factory=list)
    command_calls: list[eb.CommandRequest] = field(default_factory=list)
    agent_calls: list[eb.AgentRequest] = field(default_factory=list)
    snapshot_calls: list[tuple[eb.WorkspaceHandle, str]] = field(default_factory=list)
    cleanup_calls: list[tuple[eb.WorkspaceHandle, bool]] = field(default_factory=list)
    restore_calls: list[tuple[eb.WorkspaceHandle, eb.SnapshotRef]] = field(default_factory=list)

    @property
    def identity(self) -> eb.BackendIdentity:
        return eb.BackendIdentity(
            backend=self.backend_name,
            safety_mode=self.safety_mode,
            workspace_root=".spec-workspaces",
            backend_explicit=True,
        )

    def prepare_workspace(self, **kwargs) -> eb.WorkspaceHandle:
        self.prepare_calls.append(kwargs)
        return eb.WorkspaceHandle(
            path=self.workspace_path,
            outbox_path=self.outbox_path,
            branch=kwargs.get("branch", ""),
            backend=self.backend_name,
            metadata={"run_id": kwargs.get("run_id", "")},
        )

    def run_command(self, request: eb.CommandRequest) -> eb.CommandResult:
        self.command_calls.append(request)
        return eb.CommandResult(
            returncode=0,
            stdout="",
            stderr="",
            argv=list(request.argv),
        )

    def launch_agent(
        self,
        request: eb.AgentRequest,
        *,
        monitor: eb.AgentMonitor | None = None,
    ) -> eb.AgentResult:
        self.agent_calls.append(request)
        return eb.AgentResult(returncode=0)

    def collect_outbox_metadata(self, workspace):
        return None

    def snapshot(self, workspace, label):
        self.snapshot_calls.append((workspace, label))
        return eb.SnapshotRef(
            label=label,
            path=workspace.outbox_path.parent / "snapshots" / label,
            metadata={"backend": self.backend_name},
        )

    def restore(self, workspace, snapshot):
        self.restore_calls.append((workspace, snapshot))
        return workspace

    def cleanup(self, workspace, *, allow_unpushed_work=False):
        self.cleanup_calls.append((workspace, allow_unpushed_work))
        return None


@pytest.fixture
def reset_execution_backend():
    yield
    orch.set_execution_backend(None)


class TestOrchestratorBackendSeam:
    def test_resolve_workspace_handle_uses_factory_default(self, tmp_path: Path):
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            # Keep this backend-seam test independent of the intentional
            # native-Windows Claude fail-closed boundary.
            agent="codex",
        )
        worktree = tmp_path / ".worktrees" / "spec-my-feature"
        worktree.mkdir(parents=True)
        with patch("spec_runtime.orchestrator.resolve_worktree_path", return_value=worktree):
            handle = orch._resolve_workspace_handle(run, tmp_path)
        assert handle.path == worktree
        assert handle.outbox_path == worktree / ".spec-outbox"

    def test_set_execution_backend_overrides_factory(self, tmp_path: Path, reset_execution_backend):
        fake = _FakeBackend(
            workspace_path=tmp_path / "fake-ws",
            outbox_path=tmp_path / "fake-outbox",
            backend_name="fake",
        )
        orch.set_execution_backend(fake)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
        )
        with patch("spec_runtime.orchestrator.resolve_worktree_path", return_value=tmp_path / "wt"):
            handle = orch._resolve_workspace_handle(run, tmp_path)
        assert handle.path == tmp_path / "fake-ws"
        assert handle.outbox_path == tmp_path / "fake-outbox"
        assert handle.backend == "fake"
        # Backend received the orchestrator-resolved worktree path.
        assert fake.prepare_calls
        assert fake.prepare_calls[0]["worktree_path"] == tmp_path / "wt"
        assert fake.prepare_calls[0]["run_id"] == "my-feature-20260101T000000"
        assert fake.prepare_calls[0]["spec_id"] == "my-feature"
        assert fake.prepare_calls[0]["branch"] == "spec/my-feature"

    def test_publish_workspace_reuses_prepared_container_checkout(
        self,
        tmp_path: Path,
        reset_execution_backend,
    ):
        workspace_root = tmp_path / ".spec-workspaces"
        run_root = workspace_root / "my-feature-20260101T000000"
        source = run_root / "source"
        outbox = run_root / "outbox"
        source.mkdir(parents=True)
        outbox.mkdir()
        backend = _FakeBackend(
            workspace_path=source,
            outbox_path=outbox,
            backend_name="container",
        )
        orch.set_execution_backend(backend)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            backend="container",
            backend_workspace_root=str(workspace_root),
        )

        handle = orch._resolve_publish_workspace_handle(run, tmp_path)

        assert handle.path == source
        assert handle.outbox_path == outbox
        assert handle.backend == "container"
        assert backend.prepare_calls == []

    def test_phase_implement_uses_backend_workspace_path(self, tmp_path: Path, reset_execution_backend):
        # Backend reports a missing workspace; phase_implement should fail with
        # an error that names the backend-resolved path, proving the seam ran.
        fake_workspace = tmp_path / "absent-fake-workspace"
        fake_outbox = tmp_path / "absent-outbox"
        fake = _FakeBackend(
            workspace_path=fake_workspace,
            outbox_path=fake_outbox,
        )
        orch.set_execution_backend(fake)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            # This test exercises workspace routing, not Claude host-sandbox
            # preflight. Keep it independent of whether bwrap/socat happen to
            # be installed on the test runner.
            agent="codex",
        )
        run.save(tmp_path)
        with patch(
            "spec_runtime.orchestrator.resolve_worktree_path",
            return_value=tmp_path / "fallback-wt",
        ):
            status = orch.phase_implement(run, tmp_path)
        assert status == "failed"
        # The reported error must reference the backend-supplied path, not
        # the fallback worktree path the orchestrator passed in.
        assert str(fake_workspace) in run.last_error
        assert "fallback-wt" not in run.last_error
        assert fake.prepare_calls

    def test_phase_implement_persists_prepared_workspace_before_retry_context(
        self,
        tmp_path: Path,
        reset_execution_backend,
    ):
        workspace = tmp_path / ".spec-workspaces" / "my-feature-run" / "source"
        workspace.mkdir(parents=True)
        fake = _FakeBackend(
            workspace_path=workspace,
            outbox_path=workspace.parent / "outbox",
            backend_name="container",
        )
        orch.set_execution_backend(fake)
        stale_path = tmp_path / ".worktrees" / "code-my-feature--old"
        run = orch.RunState(
            run_id="my-feature-run",
            spec_id="my-feature",
            branch="code/my-feature--old",
            worktree_path=str(stale_path),
            backend="container",
        )
        run.save(tmp_path)

        with patch.object(orch, "_sync_reused_branch_before_implement", return_value="failed"):
            status = orch.phase_implement(run, tmp_path)

        assert status == "failed"
        assert run.worktree_path == str(workspace)
        reloaded = orch.RunState.load(tmp_path, run.run_id)
        assert reloaded is not None
        assert reloaded.worktree_path == str(workspace)

    def test_phase_verify_uses_backend_workspace_path(self, tmp_path: Path, reset_execution_backend):
        fake_workspace = tmp_path / "absent-fake-workspace"
        fake = _FakeBackend(
            workspace_path=fake_workspace,
            outbox_path=tmp_path / "absent-outbox",
        )
        orch.set_execution_backend(fake)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
        )
        run.save(tmp_path)
        with patch(
            "spec_runtime.orchestrator.resolve_worktree_path",
            return_value=tmp_path / "fallback-wt",
        ):
            status = orch.phase_verify(run, tmp_path)
        assert status == "failed"
        assert str(fake_workspace) in run.last_error
        assert "fallback-wt" not in run.last_error

    def test_phase_bootstrap_fails_early_for_unimplemented_backend(self, tmp_path: Path, reset_execution_backend):
        # Selecting a known-but-unimplemented backend must fail before any
        # worktree or branch mutation. resolve_worktree_path returns a path
        # the test treats as a tripwire: if bootstrap proceeds far enough to
        # call git worktree add, the directory's parent will exist.
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
        )
        run_mode_orig = run.run_mode
        run.run_mode = "task"
        worktree_parent = tmp_path / "should-not-be-created"
        worktree = worktree_parent / "wt"

        def _raise_clone():
            raise eb.ExecutionBackendNotImplementedError("clone")

        with (
            patch("spec_runtime.orchestrator.resolve_worktree_path", return_value=worktree),
            patch(
                "spec_runtime.orchestrator._resolve_execution_backend",
                side_effect=_raise_clone,
            ),
            patch("spec_runtime.orchestrator._worktree_is_registered") as mock_registered,
            patch("spec_runtime.orchestrator.run_subprocess") as mock_subproc,
        ):
            status = orch.phase_bootstrap(run, tmp_path)

        assert status == "failed"
        assert "clone" in (run.last_error or "")
        assert "not implemented yet" in (run.last_error or "")
        # Tripwires: nothing should have probed git or created worktree dirs.
        mock_registered.assert_not_called()
        mock_subproc.assert_not_called()
        assert not worktree_parent.exists()
        run.run_mode = run_mode_orig

    def test_phase_bootstrap_fails_early_for_unknown_backend(self, tmp_path: Path, reset_execution_backend):
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
        )
        run.run_mode = "task"
        worktree = tmp_path / "tripwire" / "wt"

        def _raise_unknown():
            raise eb.UnknownExecutionBackendError("kubernetes")

        with (
            patch("spec_runtime.orchestrator.resolve_worktree_path", return_value=worktree),
            patch(
                "spec_runtime.orchestrator._resolve_execution_backend",
                side_effect=_raise_unknown,
            ),
            patch("spec_runtime.orchestrator._worktree_is_registered") as mock_registered,
        ):
            status = orch.phase_bootstrap(run, tmp_path)

        assert status == "failed"
        assert "kubernetes" in (run.last_error or "")
        mock_registered.assert_not_called()
        assert not (tmp_path / "tripwire").exists()

    def test_collect_workspace_outbox_metadata_uses_active_backend(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        outbox = worktree / ".spec-outbox"
        outbox.mkdir()
        (outbox / "pr-metadata.json").write_text(
            json.dumps(
                {
                    "title": "Outbox-supplied title",
                    "body": "Outbox-supplied body",
                    "summary": "Outbox summary",
                }
            )
        )
        backend = eb.WorktreeExecutionBackend(ExecutionConfig())
        orch.set_execution_backend(backend)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
        )
        with patch("spec_runtime.orchestrator.resolve_worktree_path", return_value=worktree):
            handle = orch._resolve_workspace_handle(run, tmp_path)
            metadata = orch.collect_workspace_outbox_metadata(handle)
        assert metadata is not None
        assert metadata.title == "Outbox-supplied title"
        assert metadata.summary == "Outbox summary"

    def test_clone_cleanup_ignores_stale_persisted_worktree_path(self, tmp_path: Path, reset_execution_backend):
        fake = _FakeBackend(
            workspace_path=tmp_path / "unused",
            outbox_path=tmp_path / "unused-outbox",
            backend_name="clone",
        )
        orch.set_execution_backend(fake)
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            worktree_path=str(tmp_path / "outside" / "source"),
        )

        status = orch.phase_cleanup(run, tmp_path)

        expected_run_root = tmp_path / ".spec-workspaces" / run.run_id
        assert status == "passed"
        assert fake.cleanup_calls
        cleanup_workspace, allow_unpushed_work = fake.cleanup_calls[0]
        assert cleanup_workspace.path == expected_run_root / "source"
        assert cleanup_workspace.outbox_path == expected_run_root / "outbox"
        # The post-merge cleanup phase opts out of the resume-safety guard.
        assert allow_unpushed_work is True

    def test_container_bootstrap_skips_host_install_command(self, tmp_path: Path, monkeypatch, reset_execution_backend):
        repo = tmp_path / "repo"
        repo.mkdir()
        specs = repo / "specs"
        specs.mkdir()
        (specs / "my-feature.md").write_text(
            """---
id: my-feature
depends_on: []
---

# My Feature
"""
        )
        workspace = repo / ".spec-workspaces" / "my-feature-20260101T000000" / "source"
        outbox = workspace.parent / "outbox"
        workspace.mkdir(parents=True)
        outbox.mkdir()
        fake = _FakeBackend(
            workspace_path=workspace,
            outbox_path=outbox,
            backend_name="container",
        )
        orch.set_execution_backend(fake)
        monkeypatch.setattr(
            orch,
            "SPEC_RUNTIME_CONFIG",
            replace(orch.SPEC_RUNTIME_CONFIG, bootstrap_install_command="pip install -e ."),
        )
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--20260101T000000",
            agent="codex",
        )
        subprocess_calls: list[list[str]] = []

        def mock_run_subprocess(cmd, **kwargs):  # noqa: ARG001
            subprocess_calls.append(cmd)
            if cmd == ["git", "status", "--porcelain", "--", "specs/my-feature.md"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            patch.object(orch, "resolve_worktree_path", return_value=repo / ".worktrees" / "unused"),
            patch.object(orch, "run_subprocess", side_effect=mock_run_subprocess),
            patch.object(orch, "_write_sandbox_config") as write_sandbox_config,
        ):
            status = orch.phase_bootstrap(run, repo)

        assert status == "passed", run.last_error
        assert ["sh", "-c", "pip install -e ."] not in subprocess_calls
        write_sandbox_config.assert_not_called()
        assert not (workspace / ".spec-state").exists()
        assert run.worktree_path == str(workspace)
        assert fake.prepare_calls

    def test_container_retry_restores_pre_implement_snapshot(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / ".spec-workspaces" / "my-feature-20260101T000000" / "source"
        outbox = worktree.parent / "outbox"
        worktree.mkdir(parents=True)
        outbox.mkdir()
        fake = _FakeBackend(
            workspace_path=worktree,
            outbox_path=outbox,
            backend_name="container",
        )
        ctx = orch.ImplementContext(
            implement_reason="review_feedback",
            objective="Implement spec my-feature",
            run_id="my-feature-20260101T000000",
            attempt_number=2,
            run_state_dir=".spec-state/runs/my-feature-20260101T000000/",
            spec_path="specs/my-feature.md",
            triggering_phase="review",
        )

        restored = orch._restore_container_workspace_for_retry(
            eb.WorkspaceHandle(
                path=worktree,
                outbox_path=outbox,
                branch="code/my-feature--abc",
                backend="container",
            ),
            fake,
            ctx,
        )

        assert restored.path == worktree
        assert fake.restore_calls
        assert fake.restore_calls[0][1].label == "pre-implement"
        assert fake.restore_calls[0][1].path == worktree.parent / "snapshots" / "pre-implement"


# ---------------------------------------------------------------------------
# Seam routing for verify gate execution and implement agent launch
# ---------------------------------------------------------------------------


class TestVerifyGateRoutesThroughBackend:
    def test_run_verify_gate_invokes_backend_run_command(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        fake = _FakeBackend(
            workspace_path=worktree,
            outbox_path=worktree / ".spec-outbox",
        )
        orch.set_execution_backend(fake)

        result = orch._run_verify_gate(worktree, "lint", repo_root=tmp_path)

        assert fake.command_calls, "verify gate must route through backend.run_command"
        request = fake.command_calls[0]
        assert request.cwd == worktree
        # The command argv must come from the configured verify gate command,
        # not be silently rewritten by the backend layer.
        expected_argv = orch.shlex.split(orch.VERIFY_GATE_COMMANDS.get("lint", "make lint"))
        assert request.argv == expected_argv
        # Backend always sees the synthesized gate env so a future remote
        # backend can apply it inside the workspace, not the host process env.
        assert request.env is not None
        # The orchestrator must surface the backend's exit code as a
        # CompletedProcess, preserving downstream record/diagnostic behavior.
        assert result.completed_process.returncode == 0
        assert result.completed_process.args == expected_argv

    def test_run_verify_gate_failed_exit_propagates_through_backend(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / "wt"
        worktree.mkdir()

        @dataclass
        class _FailingBackend(_FakeBackend):
            def run_command(self, request: eb.CommandRequest) -> eb.CommandResult:
                self.command_calls.append(request)
                return eb.CommandResult(
                    returncode=2,
                    stdout="boom",
                    stderr="kaboom",
                    argv=list(request.argv),
                )

        fake = _FailingBackend(
            workspace_path=worktree,
            outbox_path=worktree / ".spec-outbox",
        )
        orch.set_execution_backend(fake)

        result = orch._run_verify_gate(worktree, "lint", repo_root=tmp_path)
        assert result.completed_process.returncode == 2
        assert result.completed_process.stdout == "boom"
        assert result.completed_process.stderr == "kaboom"


class TestImplementSetupRoutesThroughBackend:
    def test_setup_command_invokes_backend_run_command(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        fake = _FakeBackend(
            workspace_path=worktree,
            outbox_path=worktree / ".spec-outbox",
        )
        orch.set_execution_backend(fake)

        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            agent="claude",
        )

        from dataclasses import replace as _dc_replace

        patched_config = _dc_replace(
            orch.SPEC_RUNTIME_CONFIG,
            implement=_dc_replace(
                orch.SPEC_RUNTIME_CONFIG.implement,
                setup_command="scripts/setup.sh",
            ),
        )
        with patch.object(orch, "SPEC_RUNTIME_CONFIG", patched_config):
            manifest = orch._run_implement_setup_command(run, worktree)

        assert manifest.failure is None or manifest.failure.exit_code == 0
        assert fake.command_calls, "setup command must route through backend.run_command"
        request = fake.command_calls[0]
        assert request.cwd == worktree
        assert request.argv[0] == "scripts/setup.sh"

    def test_container_empty_setup_command_snapshots_pre_implement(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / ".spec-workspaces" / "run-1" / "source"
        worktree.mkdir(parents=True)
        fake = _FakeBackend(
            workspace_path=worktree,
            outbox_path=worktree.parent / "outbox",
            backend_name="container",
        )
        orch.set_execution_backend(fake)

        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            agent="claude",
        )

        from dataclasses import replace as _dc_replace

        patched_config = _dc_replace(
            orch.SPEC_RUNTIME_CONFIG,
            implement=_dc_replace(
                orch.SPEC_RUNTIME_CONFIG.implement,
                setup_command="",
            ),
        )
        with patch.object(orch, "SPEC_RUNTIME_CONFIG", patched_config):
            manifest = orch._run_implement_setup_command(run, worktree)

        assert manifest == orch.ImplementSetupManifest()
        assert fake.command_calls == []
        assert len(fake.snapshot_calls) == 1
        snapshot_workspace, label = fake.snapshot_calls[0]
        assert label == "pre-implement"
        assert snapshot_workspace.path == worktree
        assert snapshot_workspace.outbox_path == worktree.parent / "outbox"
        assert snapshot_workspace.branch == "spec/my-feature"

    def test_container_setup_preserves_existing_pre_implement_snapshot(self, tmp_path: Path, reset_execution_backend):
        worktree = tmp_path / ".spec-workspaces" / "run-1" / "source"
        worktree.mkdir(parents=True)
        existing_snapshot = worktree.parent / "snapshots" / "pre-implement"
        existing_snapshot.mkdir(parents=True)
        (existing_snapshot / "clean.txt").write_text("clean baseline\n")
        fake = _FakeBackend(
            workspace_path=worktree,
            outbox_path=worktree.parent / "outbox",
            backend_name="container",
        )
        orch.set_execution_backend(fake)

        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            agent="claude",
        )

        from dataclasses import replace as _dc_replace

        patched_config = _dc_replace(
            orch.SPEC_RUNTIME_CONFIG,
            implement=_dc_replace(
                orch.SPEC_RUNTIME_CONFIG.implement,
                setup_command="",
            ),
        )
        with patch.object(orch, "SPEC_RUNTIME_CONFIG", patched_config):
            manifest = orch._run_implement_setup_command(run, worktree)

        assert manifest == orch.ImplementSetupManifest()
        assert fake.snapshot_calls == []
        assert (existing_snapshot / "clean.txt").read_text() == "clean baseline\n"

    def test_container_import_failure_classifies_as_import(self):
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            last_error=(
                "Container backend import failed after worker execution: "
                "Container backend import failed: could not import source volume spec-123."
            ),
        )

        metadata = orch._classify_phase_result(run, "implement", "failed")

        assert metadata["failure_type"] == "import"
        assert metadata["failure_subtype"] == "container_workspace_import_failed"
        assert metadata["retryable"] is True


class TestImplementLaunchRoutesThroughBackend:
    def test_launch_implement_attempt_routes_through_backend_launch_agent(
        self, tmp_path: Path, reset_execution_backend
    ):
        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured: dict[str, object] = {}

        @dataclass
        class _RecordingBackend(_FakeBackend):
            def launch_agent(
                self,
                request: eb.AgentRequest,
                *,
                monitor: eb.AgentMonitor | None = None,
            ) -> eb.AgentResult:
                self.agent_calls.append(request)
                captured["request"] = request
                captured["monitor_provided"] = monitor is not None
                # Confirm the supervisor closure runs without spawning a real
                # process by feeding it a dummy "process" that pretends to
                # have already exited cleanly.
                if monitor is not None:
                    captured["monitor_invoked"] = True
                    captured["monitor_proc"] = "skipped"
                return eb.AgentResult(returncode=7)

        fake = _RecordingBackend(
            workspace_path=worktree,
            outbox_path=worktree / ".spec-outbox",
        )
        orch.set_execution_backend(fake)

        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="spec/my-feature",
            agent="claude",
        )

        plan = orch.ImplementLaunchPlan(
            use_stream_json=False,
            agent_env={"FOO": "bar"},
            agent_cmd=["echo", "implement"],
            popen_kwargs={
                "cwd": worktree,
                "env": {"FOO": "bar"},
                "text": True,
            },
            progress_tracker=None,
        )

        exit_code = orch._launch_implement_attempt(run, tmp_path, worktree, plan)

        assert exit_code == 7
        assert captured["monitor_provided"] is True
        request = captured["request"]
        assert isinstance(request, eb.AgentRequest)
        assert request.argv == ["echo", "implement"]
        assert request.cwd == worktree
        assert request.env == {"FOO": "bar"}
        # cwd/env are owned by the backend via dedicated AgentRequest fields,
        # so they must not also appear in popen_kwargs (otherwise the backend
        # would receive duplicate keyword arguments).
        assert "cwd" not in request.popen_kwargs
        assert "env" not in request.popen_kwargs
        # Process ownership is backend policy, not a caller-supplied Popen
        # option. Transport-neutral stream configuration is still forwarded.
        assert "start_new_session" not in request.popen_kwargs
        assert request.popen_kwargs.get("text") is True


# ---------------------------------------------------------------------------
# Publish honors backend outbox PR/MR metadata
# ---------------------------------------------------------------------------


class TestPublishHonorsOutboxMetadata:
    def _make_run(self) -> orch.RunState:
        run = orch.RunState(
            run_id="my-feature-20260101T000000",
            spec_id="my-feature",
            branch="code/my-feature--abc",
            agent="claude",
        )
        run.run_mode = "spec"
        run.publish_as_draft = False
        run.attempts = 1
        run.retry_cap = 5
        return run

    def _setup_workspace(self, tmp_path: Path) -> Path:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        spec_dir = worktree / "specs"
        spec_dir.mkdir()
        (spec_dir / "my-feature.md").write_text("# Spec\n- [ ] item\n")
        return worktree

    def _publish_with_metadata(
        self,
        tmp_path: Path,
        worktree: Path,
        outbox_payload: dict | None,
        head_sha: str = "deadbeef",
    ):
        from unittest.mock import MagicMock

        from spec_runtime.forge import PullRequest, PushResult

        if outbox_payload is not None:
            outbox_dir = worktree / ".spec-outbox"
            outbox_dir.mkdir(exist_ok=True)
            (outbox_dir / "pr-metadata.json").write_text(json.dumps(outbox_payload))

        run = self._make_run()
        captured: dict = {}

        mock_forge = MagicMock()
        mock_forge.find_pr_for_branch.return_value = None
        mock_forge.push_branch.return_value = PushResult(ok=True)

        def capture_create_pr(**kwargs):
            captured["create_pr_kwargs"] = kwargs
            return PullRequest(number=1, url="http://pr/1")

        mock_forge.create_pr.side_effect = capture_create_pr

        spec_file = worktree / "specs" / "my-feature.md"
        with (
            patch.object(orch, "resolve_worktree_path", return_value=worktree),
            patch.object(orch, "_worktree_branch_alignment_error", return_value=""),
            patch.object(orch, "_active_spec_path", return_value=spec_file),
            patch.object(orch, "_spec_path_in_tree", return_value=spec_file),
            patch.object(orch, "_ensure_run_spec_committed", return_value=True),
            patch.object(orch, "_extract_acceptance_checklist", return_value="- [ ] check"),
            patch.object(orch, "_forge", return_value=mock_forge),
            patch.object(orch, "_check_forge_auth", return_value=""),
            patch.object(orch, "_known_issues_markdown", return_value="None"),
            patch.object(orch, "_spec_path_for_run", return_value="specs/my-feature.md"),
            patch.object(orch, "format_pr_review_owner", return_value="@reviewer"),
            patch.object(orch, "_head_sha", return_value=head_sha),
            patch.object(orch, "_gate_status_path", return_value=tmp_path / "gate.json"),
            patch.object(orch, "_reset_local_review_gate_for_head"),
        ):
            result = orch.phase_publish(run, tmp_path)

        return result, captured

    def test_publish_uses_outbox_title_summary_and_labels(self, tmp_path: Path, reset_execution_backend):
        worktree = self._setup_workspace(tmp_path)
        result, captured = self._publish_with_metadata(
            tmp_path,
            worktree,
            {
                "title": "Outbox-supplied title",
                "summary": "Outbox-supplied summary",
                "labels": ["needs-review", "infra"],
                "head_sha": "deadbeef",
            },
        )
        assert result == "passed"
        kwargs = captured["create_pr_kwargs"]
        assert kwargs["title"] == "Outbox-supplied title"
        assert "Outbox-supplied summary" in kwargs["body"]
        assert kwargs["labels"] == ("needs-review", "infra")

    def test_publish_outbox_body_overrides_template(self, tmp_path: Path, reset_execution_backend):
        worktree = self._setup_workspace(tmp_path)
        result, captured = self._publish_with_metadata(
            tmp_path,
            worktree,
            {
                "body": "Wholly-custom body from backend",
                "head_sha": "deadbeef",
            },
        )
        assert result == "passed"
        kwargs = captured["create_pr_kwargs"]
        assert kwargs["body"] == "Wholly-custom body from backend"

    def test_publish_falls_back_when_outbox_metadata_absent(self, tmp_path: Path, reset_execution_backend):
        worktree = self._setup_workspace(tmp_path)
        result, captured = self._publish_with_metadata(
            tmp_path,
            worktree,
            outbox_payload=None,
        )
        assert result == "passed"
        kwargs = captured["create_pr_kwargs"]
        assert kwargs["title"] == "Spec: my-feature"
        assert kwargs["labels"] == ()
        assert kwargs["draft"] is True

    def test_publish_ignores_metadata_with_stale_head_sha(self, tmp_path: Path, reset_execution_backend):
        worktree = self._setup_workspace(tmp_path)
        result, captured = self._publish_with_metadata(
            tmp_path,
            worktree,
            {
                "title": "Stale title",
                "labels": ["stale"],
                "head_sha": "0" * 40,
            },
            head_sha="deadbeef",
        )
        assert result == "passed"
        kwargs = captured["create_pr_kwargs"]
        # Stale metadata is ignored; the host falls back to defaults.
        assert kwargs["title"] == "Spec: my-feature"
        assert kwargs["labels"] == ()


# ---------------------------------------------------------------------------
# Container worker env denylist (spec: container-agent-env-sanitization)
# ---------------------------------------------------------------------------


class TestContainerWorkerEnvHostPathDenylist:
    """Host temp-dir vars must never reach containerized execs.

    Regression for the July 2026 implement wedge: macOS launchd sets
    TMPDIR=/var/folders/... in every process; passed into the Linux worker
    container that path does not exist and `claude -p` hangs indefinitely at
    startup (its MCP child never spawns). Env delta-debugging against the
    live wedged process isolated `TMPDIR` as the exact minimal hanging set.
    """

    @pytest.mark.parametrize("key", ["TMPDIR", "TMP", "TEMP"])
    def test_host_temp_env_is_denied(self, key: str) -> None:
        assert not eb._is_container_worker_env_allowed(key)

    def test_filter_strips_host_temp_vars(self) -> None:
        env = {
            "TMPDIR": "/var/folders/xx/yyy/T/",
            "TMP": "/var/folders/xx/yyy/T/",
            "TEMP": "/var/folders/xx/yyy/T/",
            "SPEC_ID": "example",
        }
        filtered = eb.ContainerExecutionBackend._filter_container_worker_env(env)
        assert "TMPDIR" not in filtered
        assert "TMP" not in filtered
        assert "TEMP" not in filtered
        assert filtered["SPEC_ID"] == "example"
