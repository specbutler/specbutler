from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from spec_runtime import container
from spec_runtime.config import (
    ContainerExecutionConfig,
    ContainerPlaywrightMcpConfig,
    ExecutionConfig,
    SpecConfigError,
    SpecRuntimeConfig,
    _parse_container_execution_section,
)
from spec_runtime.container_sandbox import SANDBOX_PROBE_MARKER
from spec_runtime.container_security import require_same_sandbox_profile, worker_security_args


def test_nested_policy_is_explicit_and_cannot_inject_arbitrary_docker_flags():
    assert _parse_container_execution_section({}).sandbox_profile == "default"
    for name in ("unconfined", "--privileged", "", "custom.json"):
        with pytest.raises(SpecConfigError, match="sandbox_profile"):
            _parse_container_execution_section({"sandbox_profile": name})
    for extra in (
        {"engine": "podman"},
        {"compose_file": "services.yml"},
        {"playwright_mcp": {"topology": "sidecar"}},
    ):
        with pytest.raises(SpecConfigError, match="requires Docker"):
            _parse_container_execution_section({"sandbox_profile": "nested-v1", **extra})


@pytest.mark.parametrize("topology", ["disabled", "in-worker"])
def test_nested_policy_accepts_services_within_worker_only(monkeypatch, topology):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    config = _parse_container_execution_section({
        "sandbox_profile": "nested-v1", "playwright_mcp": {"topology": topology},
    })
    assert "--cap-drop=ALL" in worker_security_args(
        config, system_name="Linux", user_mapping="1000:1000",
    )


def test_nested_policy_rejects_sidecar_before_engine_contact(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("os.getuid", lambda: 1000, raising=False)
    monkeypatch.setattr("os.getgid", lambda: 1000, raising=False)
    config = SpecRuntimeConfig(execution=ExecutionConfig(
        backend="container", container=ContainerExecutionConfig(
            image="worker", sandbox_profile="nested-v1",
            playwright_mcp=ContainerPlaywrightMcpConfig(topology="sidecar"),
        ),
    ))
    runner = Mock()
    check = container._codex_worker_sandbox_check(tmp_path, config, runner, "Linux")
    assert not check.ok
    runner.run.assert_not_called()
    with pytest.raises(RuntimeError, match="in-worker services"):
        worker_security_args(
            config.execution.container, system_name="Linux", user_mapping="1000:1000",
        )


@pytest.mark.parametrize("user", ["", "root", "0:0", "0:1000", "1000:0", "1000", "foo:bar"])
def test_nested_policy_refuses_missing_or_root_identity(monkeypatch, user):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="non-root"):
        worker_security_args(
            ContainerExecutionConfig(sandbox_profile="nested-v1"),
            system_name="Linux", user_mapping=user,
        )


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Darwin", "arm64"), ("Linux", "s390x")])
def test_nested_policy_rejects_unvalidated_platforms(monkeypatch, system, machine):
    monkeypatch.setattr("platform.machine", lambda: machine)
    with pytest.raises(RuntimeError, match="requires a Linux"):
        worker_security_args(
            ContainerExecutionConfig(sandbox_profile="nested-v1"),
            system_name=system, user_mapping="1000:1000",
        )


def test_profile_keeps_default_seccomp_denials_and_mandatory_outer_controls(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert worker_security_args(
        ContainerExecutionConfig(), system_name="Linux", user_mapping="0:0",
    ) == []
    args = worker_security_args(
        ContainerExecutionConfig(sandbox_profile="nested-v1"),
        system_name="Linux", user_mapping="1000:1000",
    )
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges=true" in args
    assert "--security-opt=apparmor=specbutler-nested-v1" in args
    path = Path(next(arg.split("=", 2)[2] for arg in args if arg.startswith("--security-opt=seccomp=")))
    policy = json.loads(path.read_text())
    assert policy["defaultAction"] == "SCMP_ACT_ERRNO"
    unconditional_allow = {
        name for rule in policy["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("includes")
        for name in rule["names"]
    }
    assert not {"setns", "bpf", "init_module", "finit_module", "kexec_load", "clone3"} & unconditional_allow
    assert any("clone3" in rule["names"] and rule.get("errnoRet") == 38 for rule in policy["syscalls"])
    apparmor = path.with_name("specbutler-nested-v1.apparmor").read_text()
    # An optional-flags rule can accidentally admit a plain mount of any fs.
    # Bind remounts must always require both operations, not just allow them.
    assert "mount options in" not in apparmor
    for line in apparmor.splitlines():
        if "mount options=" in line and "remount" in line:
            assert "bind," in line and "nosuid" in line
    assert "deny /sys/kernel/security/** rwklx" in apparmor


@pytest.mark.parametrize("recorded", [None, {}, [], "unconfined"])
def test_malformed_recorded_policy_fails_closed(recorded):
    with pytest.raises(RuntimeError, match="invalid sandbox profile"):
        require_same_sandbox_profile(recorded, "default")


@pytest.mark.parametrize("old,new", [("default", "nested-v1"), ("nested-v1", "default")])
def test_existing_run_cannot_change_outer_policy(old, new):
    with pytest.raises(RuntimeError, match="cannot change sandbox profile"):
        require_same_sandbox_profile(old, new)


def test_doctor_uses_selected_policy_and_same_nonroot_identity(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("os.getuid", lambda: 1234, raising=False)
    monkeypatch.setattr("os.getgid", lambda: 1234, raising=False)
    runner = Mock()
    runner.run.return_value = subprocess.CompletedProcess([], 0, SANDBOX_PROBE_MARKER, "")
    config = SpecRuntimeConfig(execution=ExecutionConfig(
        backend="container", container=ContainerExecutionConfig(
            image="worker", sandbox_profile="nested-v1",
        ),
    ))
    check = container._codex_worker_sandbox_check(tmp_path, config, runner, "Linux")
    assert check.ok
    args = runner.run.call_args.args[0]
    assert args[args.index("--user") + 1] == "1234:1234"
    assert "--cap-drop=ALL" in args
    assert "--security-opt=apparmor=specbutler-nested-v1" in args


def test_doctor_root_identity_fails_before_engine_contact(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("os.getuid", lambda: 0, raising=False)
    monkeypatch.setattr("os.getgid", lambda: 0, raising=False)
    runner = Mock()
    config = SpecRuntimeConfig(execution=ExecutionConfig(
        backend="container", container=ContainerExecutionConfig(
            image="worker", sandbox_profile="nested-v1",
        ),
    ))
    check = container._codex_worker_sandbox_check(tmp_path, config, runner, "Linux")
    assert not check.ok
    runner.run.assert_not_called()
