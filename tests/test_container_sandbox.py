from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from unittest.mock import Mock

import pytest

from spec_runtime import container
from spec_runtime import orchestrator as orch
from spec_runtime.config import ContainerExecutionConfig, ExecutionConfig, SpecRuntimeConfig
from spec_runtime.container_sandbox import (
    _CHILD_SCRIPT,
    SANDBOX_PROBE_MARKER,
    SANDBOX_PROBE_TIMEOUT,
    ContainerSandboxUnavailableError,
    codex_container_probe_command,
    sandbox_probe_failure,
)
from spec_runtime.execution_backend import CommandResult, ContainerExecutionBackend


def test_probe_uses_implementation_profile_without_provider_or_exec():
    argv = codex_container_probe_command()
    controls, workspace, outbox, _ = json.loads(argv[-1])
    policy = tomllib.loads("\n".join(controls[1::2]))
    fs = policy["permissions"]["specbutler-implement"]["filesystem"]
    assert fs[workspace] == fs[outbox] == "write"
    assert fs["/workspace/provider-homes/codex"] == "deny"
    assert fs["/__SPECBUTLER_PROBE_DENIED__"] == "deny"
    assert fs["/__SPECBUTLER_PROBE_HOME__"] == "deny"
    assert fs[":root"] == "read"
    assert "sandbox'" in argv[2]
    assert "'exec'" not in argv[2]
    assert "subprocess.DEVNULL" in argv[2]


@pytest.mark.parametrize("code,stdout,stderr", [
    (0, "", ""),
    (1, SANDBOX_PROBE_MARKER, "bwrap: No permissions to create new namespace"),
    (42, "sandbox allowed a protected read", ""),
    (127, "", "codex not found"),
])
def test_probe_rejects_missing_proof_and_explains_environment_blocker(code, stdout, stderr):
    reason = sandbox_probe_failure(code, stdout, stderr)
    assert "preflight failed" in reason
    assert "worktree backend" in reason
    run = orch.RunState(run_id="probe-123", spec_id="probe", branch="b", last_error=reason)
    classification = orch._classify_phase_result(run, "implement", "blocked")
    assert classification["failure_type"] == "environment"
    assert classification["failure_subtype"] == "container_sandbox_unavailable"
    assert classification["retryable"] is False
    assert not orch._is_retryable_implement_failure_message(reason)
    assert orch._workflow_failure_policy(run, phase="implement") is None


def test_probe_child_detects_missing_enforcement(tmp_path):
    workspace, outbox = tmp_path / "workspace", tmp_path / "outbox"
    workspace.mkdir()
    outbox.mkdir()
    denied, outside = tmp_path / "protected.txt", tmp_path / "readonly.txt"
    denied.write_text("synthetic secret")
    outside.write_text("original")
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(workspace), str(outbox), str(denied), str(outside)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "allowed a protected read" in result.stderr
    assert "synthetic secret" not in result.stdout + result.stderr
    assert outside.read_text() == "original"


@pytest.mark.parametrize("result", [
    CommandResult(returncode=1, stdout="", stderr="bwrap: namespace denied", argv=[]),
    subprocess.TimeoutExpired(["codex", "sandbox"], 25),
])
def test_worker_preflight_captures_failure_without_launching_agent(tmp_path, result):
    backend = ContainerExecutionBackend(ExecutionConfig(backend="container"))
    backend.run_command = Mock(
        **({"side_effect": result} if isinstance(result, Exception) else {"return_value": result})
    )
    with pytest.raises(ContainerSandboxUnavailableError, match="preflight failed"):
        backend.preflight_agent_sandbox("codex", tmp_path)
    request = backend.run_command.call_args.args[0]
    assert request.cwd == tmp_path
    assert request.timeout == SANDBOX_PROBE_TIMEOUT
    assert request.inherit_env is False


def test_worker_preflight_passes_only_with_positive_proof(tmp_path):
    backend = ContainerExecutionBackend(ExecutionConfig(backend="container"))
    backend.run_command = Mock(return_value=CommandResult(
        returncode=0, stdout=SANDBOX_PROBE_MARKER, stderr="", argv=[],
    ))
    backend.preflight_agent_sandbox("codex", tmp_path)
    backend.preflight_agent_sandbox("claude", tmp_path)
    assert backend.run_command.call_count == 1


def test_doctor_timeout_removes_only_its_disposable_container(tmp_path):
    runner = Mock()
    runner.run.side_effect = [subprocess.TimeoutExpired(["docker"], 25),
                              subprocess.CompletedProcess([], 0, "", "")]
    config = SpecRuntimeConfig(execution=ExecutionConfig(
        backend="container", container=ContainerExecutionConfig(image="test-worker"),
    ))
    check = container._codex_worker_sandbox_check(tmp_path, config, runner, "Linux")
    assert not check.ok
    argv = runner.run.call_args_list[0].args[0]
    assert "--network=none" in argv
    assert "--pull=never" in argv
    assert "--privileged" not in argv
    assert "--security-opt" not in argv
    name = argv[argv.index("--name") + 1]
    assert name.startswith("spec-sandbox-doctor-")
    assert runner.run.call_args_list[1].args[0] == ["docker", "rm", "-f", name]


def test_doctor_does_not_claim_unbuilt_dockerfile_enforces_sandbox(tmp_path):
    runner = Mock()
    check = container._codex_worker_sandbox_check(tmp_path, SpecRuntimeConfig(), runner, "Linux")
    assert not check.ok
    assert "not verified" in check.detail
    assert "spec container smoke" in check.remediation[0]
    runner.run.assert_not_called()
