from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor
from spec_runtime.review_bootstrap import (
    REVIEW_BOOTSTRAP_PERMISSION_PROFILE,
    ReviewBootstrapSandboxUnavailable,
    build_review_bootstrap_environment,
    isolated_review_bootstrap_sandbox,
)


def _override(argv: tuple[str, ...], prefix: str) -> str:
    for index, value in enumerate(argv):
        if value == "--config" and argv[index + 1].startswith(prefix):
            return argv[index + 1]
    raise AssertionError(f"missing config override {prefix!r}: {argv!r}")


def _inline_value(override: str) -> object:
    _, value = override.split("=", 1)
    return tomllib.loads(f"value = {value}\n")["value"]


def test_sandbox_profile_is_write_scoped_and_credential_blind(tmp_path: Path):
    review_worktree = tmp_path / "review"
    review_worktree.mkdir()
    operator_home = tmp_path / "operator-home"
    codex_home = operator_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("operator-secret")
    # Exercise the common nvm-style layout where the Codex distribution is
    # installed below the operator home. A broad parent deny would override
    # the narrow executable read grant and make the sandbox unable to start.
    fake_codex = operator_home / "tools" / "codex"
    fake_codex.parent.mkdir()
    fake_codex.write_text("fake")
    inherited = {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "CODEX_HOME": str(codex_home),
        "GH_TOKEN": "forge-secret",
        "HOME": str(operator_home),
        "HTTPS_PROXY": "https://user:password@example.invalid",
        "PATH": os.environ.get("PATH", os.defpath),
    }

    def which(name: str, **kwargs: object) -> str | None:  # noqa: ARG001
        return str(fake_codex) if name.startswith("codex") else None

    with isolated_review_bootstrap_sandbox(
        review_worktree,
        [sys.executable, "-c", "pass"],
        inherited_env=inherited,
        windows=False,
        which=which,
    ) as sandbox:
        runtime_root = sandbox.runtime_root
        assert runtime_root.is_relative_to(review_worktree)
        assert sandbox.env["HOME"].startswith(str(runtime_root))
        assert sandbox.env["USERPROFILE"].startswith(str(runtime_root))
        assert sandbox.env["TMPDIR"].startswith(str(runtime_root))
        assert sandbox.env["CODEX_HOME"] == str(codex_home)
        assert "ANTHROPIC_API_KEY" not in sandbox.env
        assert "AWS_SECRET_ACCESS_KEY" not in sandbox.env
        assert "GH_TOKEN" not in sandbox.env
        assert "HTTPS_PROXY" not in sandbox.env

        argv = sandbox.launcher_argv
        assert argv[:4] == (
            str(fake_codex),
            "sandbox",
            "--permission-profile",
            REVIEW_BOOTSTRAP_PERMISSION_PROFILE,
        )
        assert "--include-managed-config" in argv
        assert argv[argv.index("--cd") + 1] == str(review_worktree)
        assert argv[-1] == "--"

        profile = _inline_value(
            _override(argv, f"permissions.{REVIEW_BOOTSTRAP_PERMISSION_PROFILE}=")
        )
        assert isinstance(profile, dict)
        assert profile["network"] == {"enabled": False}
        filesystem = profile["filesystem"]
        assert filesystem[":minimal"] == "read"
        assert filesystem[":workspace_roots"] == {".": "write"}
        assert str(operator_home) not in filesystem
        assert filesystem[str(fake_codex.parent)] == "read"
        assert all(access != "write" for key, access in filesystem.items() if key != ":workspace_roots")

        filters = _inline_value(_override(argv, "shell_environment_policy.filters="))
        assert filters == {key: "include" for key in sorted(sandbox.env)}
        configured_env = _inline_value(_override(argv, "shell_environment_policy.set="))
        assert configured_env == sandbox.env
        assert sandbox.wrap(["python", "-c", "pass"])[-3:] == ["python", "-c", "pass"]

    assert not runtime_root.exists()
    assert (codex_home / "auth.json").read_text() == "operator-secret"


def test_windows_environment_redirects_all_standard_profile_roots(tmp_path: Path):
    runtime_root = tmp_path / "review" / ".spec-review-bootstrap"
    inherited = {
        "APPDATA": r"C:\Users\operator\AppData\Roaming",
        "HOME": r"C:\Users\operator",
        "LOCALAPPDATA": r"C:\Users\operator\AppData\Local",
        "PATH": r"C:\Windows\System32",
        "SECRET_TOKEN": "secret",
        "SYSTEMROOT": r"C:\Windows",
        "USERPROFILE": r"C:\Users\operator",
    }

    env = build_review_bootstrap_environment(
        inherited_env=inherited,
        runtime_root=runtime_root,
        codex_home=tmp_path / "codex-home",
        windows=True,
    )

    assert env["USERPROFILE"].startswith(str(runtime_root))
    assert env["HOME"].startswith(str(runtime_root))
    assert env["APPDATA"].startswith(str(runtime_root))
    assert env["LOCALAPPDATA"].startswith(str(runtime_root))
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert "SECRET_TOKEN" not in env


def test_missing_sandbox_runner_fails_before_command_launch(tmp_path: Path):
    review_worktree = tmp_path / "review"
    review_worktree.mkdir()

    with pytest.raises(ReviewBootstrapSandboxUnavailable, match="Codex CLI"):
        with isolated_review_bootstrap_sandbox(
            review_worktree,
            [sys.executable, "-c", "pass"],
            inherited_env={"HOME": str(tmp_path), "PATH": os.defpath},
            windows=False,
            which=lambda *args, **kwargs: None,
        ):
            raise AssertionError("unreachable")

    assert list(review_worktree.iterdir()) == []


def test_missing_bootstrap_executable_fails_closed(tmp_path: Path):
    review_worktree = tmp_path / "review"
    review_worktree.mkdir()
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("fake")

    def which(name: str, **kwargs: object) -> str | None:  # noqa: ARG001
        return str(fake_codex) if name.startswith("codex") else None

    with pytest.raises(ReviewBootstrapSandboxUnavailable, match="bootstrap executable"):
        with isolated_review_bootstrap_sandbox(
            review_worktree,
            ["definitely-not-a-real-bootstrap-executable"],
            inherited_env={"HOME": str(tmp_path), "PATH": os.defpath},
            windows=False,
            which=which,
        ):
            raise AssertionError("unreachable")

    assert list(review_worktree.iterdir()) == []


@pytest.mark.skipif(
    os.environ.get("SPEC_TEST_REVIEW_BOOTSTRAP_SANDBOX") != "1",
    reason="set SPEC_TEST_REVIEW_BOOTSTRAP_SANDBOX=1 to exercise the installed native sandbox",
)
def test_native_sandbox_denies_operator_secret_and_sibling_write(tmp_path: Path):
    """Exercise the real model-free sandbox, including its platform backend."""

    review_worktree = tmp_path / "review"
    sibling_worktree = tmp_path / "sibling"
    operator_home = tmp_path / "operator-home"
    review_worktree.mkdir()
    sibling_worktree.mkdir()
    operator_home.mkdir()
    secret = operator_home / "credential.txt"
    secret.write_text("operator-secret")
    escaped = sibling_worktree / "escaped.txt"
    script = """
import json
import pathlib
import sys

secret, escaped = map(pathlib.Path, sys.argv[1:])
outcome = {}
try:
    outcome["credential"] = secret.read_text()
except OSError:
    outcome["credential"] = "denied"
try:
    escaped.write_text("sandbox escape")
    outcome["sibling_write"] = "allowed"
except OSError:
    outcome["sibling_write"] = "denied"
marker = pathlib.Path("workspace-marker.txt")
marker.write_text("sandbox-write")
outcome["workspace_round_trip"] = marker.read_text()
print(json.dumps(outcome))
"""
    # Resolve virtual-environment launcher symlinks: the native sandbox must
    # read the interpreter itself, but should not need access to the outer
    # repository that owns this test process's venv.
    interpreter = str(Path(sys.executable).resolve())
    command = [interpreter, "-c", script, str(secret), str(escaped)]
    inherited = dict(os.environ)
    inherited["HOME"] = str(operator_home)
    inherited["USERPROFILE"] = str(operator_home)

    with isolated_review_bootstrap_sandbox(
        review_worktree,
        command,
        inherited_env=inherited,
    ) as sandbox:
        process = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
            sandbox.wrap(command),
            cwd=review_worktree,
            env=sandbox.env,
            stdout=-1,
            stderr=-1,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr or stdout
    assert json.loads(stdout) == {
        "credential": "denied",
        "sibling_write": "denied",
        "workspace_round_trip": "sandbox-write",
    }
    assert secret.read_text() == "operator-secret"
    assert not escaped.exists()
