from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from spec_runtime import cli, doctor
from spec_runtime.config import load_repo_spec_runtime_config


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def _config_text(
    *,
    default_agent: str = "codex",
    allowed_agents: tuple[str, ...] = ("codex",),
    base_ref: str = "HEAD",
    state_dir: str = ".spec-state",
    worktrees_dir: str = ".worktrees",
    workspace_root: str = ".spec-workspaces",
    bootstrap_command: str = "python -m pip --version",
    verify_command: str = "python -m pytest --version",
    backend: str = "worktree",
    safety_mode: str = "",
    container_block: str = "",
) -> str:
    allowed = ", ".join(f'"{agent}"' for agent in allowed_agents)
    return f"""
base_ref = "{base_ref}"

[paths]
state_dir = "{state_dir}"
worktrees_dir = "{worktrees_dir}"

[agents]
default = "{default_agent}"
allowed = [{allowed}]

[bootstrap]
install_command = "{bootstrap_command}"

[verify]
[[verify.gates]]
name = "test"
command = "{verify_command}"

[execution]
backend = "{backend}"
{f'safety_mode = "{safety_mode}"' if safety_mode else ''}
workspace_root = "{workspace_root}"
{container_block}
""".lstrip()


def _make_repo(tmp_path: Path, config_text: str, *, origin: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".spec.toml").write_text(config_text)
    (repo / ".gitignore").write_text(
        ".spec-state/\n.worktrees/\n.spec-workspaces/\n"
    )
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", ".spec.toml", ".gitignore", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Spec Doctor Tests",
        "-c",
        "user.email=doctor@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    if origin:
        _git(repo, "remote", "add", "origin", "https://github.com/example/project.git")
    return repo


def _resolver(
    *,
    claude: bool = False,
    claude_sandbox: bool = False,
    docker: bool = False,
):
    paths = {
        "codex": "/usr/bin/codex",
        "gh": "/usr/bin/gh",
        "python": sys.executable,
    }
    if claude:
        paths["claude"] = "/usr/bin/claude"
    if claude_sandbox:
        paths["bwrap"] = "/usr/bin/bwrap"
        paths["socat"] = "/usr/bin/socat"
    if docker:
        paths["docker"] = "/usr/bin/docker"
    return paths.get


class _Runner:
    def __init__(self, *, gh_auth_ok: bool = True, docker_info_ok: bool = True) -> None:
        self.gh_auth_ok = gh_auth_ok
        self.docker_info_ok = docker_info_ok
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if Path(argv[0]).name == "gh":
            if self.gh_auth_ok:
                return subprocess.CompletedProcess(argv, 0, "Logged in to github.com", "")
            return subprocess.CompletedProcess(argv, 1, "", "not logged in")
        if Path(argv[0]).name == "docker" and argv[1:] == ["info"]:
            if self.docker_info_ok:
                return subprocess.CompletedProcess(argv, 0, "daemon ready", "")
            return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
        return doctor._run_command(argv, cwd, timeout)


def _checks_by_name(report: doctor.DoctorReport) -> dict[str, doctor.DoctorCheck]:
    return {check.name: check for check in report.checks}


def test_windows_command_checks_enumerate_bootstrap_hooks_and_all_gates(
    tmp_path: Path,
) -> None:
    config_text = _config_text().replace(
        "[execution]",
        """[implement]
setup_command_windows = "Write-Output setup"
setup_shell_windows = "powershell"
teardown_command_windows = "Write-Output teardown"
teardown_shell_windows = "pwsh"

[execution]""",
    )
    repo = _make_repo(tmp_path, config_text)
    config = load_repo_spec_runtime_config(repo, require=True)

    checks = doctor._windows_command_checks(repo, config, _resolver())
    by_name = {check.name: check for check in checks}

    assert set(by_name) == {
        "bootstrap command",
        "implement setup command",
        "implement teardown command",
        "verify command (test)",
    }
    assert by_name["implement setup command"].status == "error"
    assert "powershell" in by_name["implement setup command"].detail
    assert by_name["implement teardown command"].status == "error"
    assert "pwsh" in by_name["implement teardown command"].detail


def test_windows_command_checks_target_posix_hook_migration(tmp_path: Path) -> None:
    config_text = _config_text().replace(
        "[execution]",
        """[implement]
setup_command = "./scripts/setup.sh && export READY=1"
setup_shell = "sh"

[execution]""",
    )
    repo = _make_repo(tmp_path, config_text)
    config = load_repo_spec_runtime_config(repo, require=True)

    checks = doctor._windows_command_checks(repo, config, _resolver())
    setup = next(check for check in checks if check.name == "implement setup command")

    assert setup.status == "error"
    assert "clearly POSIX" in setup.detail
    assert "setup" in " ".join(setup.remediation)


def test_doctor_happy_path_has_no_blockers_or_warnings(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, _config_text())

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(),
    )

    assert report.exit_code == 0
    assert report.blocker_count == 0
    assert report.warning_count == 0
    checks = _checks_by_name(report)
    assert checks["repository"].status == "ok"
    assert checks["configuration"].status == "ok"
    assert checks["base ref"].status == "ok"
    assert checks["origin remote"].status == "ok"
    assert checks["GitHub authentication"].status == "ok"
    assert checks["verify command (test)"].status == "ok"
    assert checks["runtime path separation"].status == "ok"


def test_missing_optional_allowed_agent_is_warning_only(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(allowed_agents=("codex", "claude")),
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(claude=False),
    )

    assert report.exit_code == 0
    assert report.blocker_count == 0
    assert _checks_by_name(report)["agent binary (claude)"].status == "warning"


def test_required_claude_without_sandbox_dependencies_is_blocked(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(default_agent="claude", allowed_agents=("claude",)),
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(claude=True, claude_sandbox=False),
    )

    check = _checks_by_name(report)["agent runtime (claude)"]
    assert check.status == "error"
    assert "bubblewrap" in " ".join(check.remediation)
    assert "socat" in check.detail
    assert report.exit_code == 1


def test_optional_claude_without_sandbox_dependencies_warns(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(allowed_agents=("codex", "claude")),
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(claude=True, claude_sandbox=False),
    )

    check = _checks_by_name(report)["agent runtime (claude)"]
    assert check.status == "warning"
    assert report.exit_code == 0


def test_legacy_codex_file_collision_is_blocked(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, _config_text())
    (repo / ".codex").write_text("")

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(),
    )

    check = _checks_by_name(report)["agent project config (codex)"]
    assert check.status == "error"
    assert "not a real directory" in check.detail
    assert report.exit_code == 1


def test_explicit_safety_mode_warns_that_it_is_not_an_enforcement_switch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, _config_text(safety_mode="trusted"))

    report = doctor.run_doctor_checks(repo, runner=_Runner(), which=_resolver())

    check = _checks_by_name(report)["execution safety label"]
    assert check.status == "warning"
    assert "metadata only" in check.detail
    assert report.exit_code == 0


def test_bootstrap_only_requires_the_initial_launcher_before_first_run(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(
            bootstrap_command=(
                "python -m venv .venv && "
                ".venv/bin/pip install -e ."
            ),
        ),
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(),
    )

    check = _checks_by_name(report)["bootstrap command"]
    assert check.status == "ok"
    assert "python" in check.detail
    assert report.exit_code == 0


def test_verify_executable_created_by_bootstrap_is_ready_before_first_run(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(
            bootstrap_command=(
                "python -m venv .venv && "
                ".venv/bin/python -m pip install -e . pytest"
            ),
            verify_command=".venv/bin/python -m pytest",
        ),
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(),
    )

    check = _checks_by_name(report)["verify command (test)"]
    assert check.status == "ok"
    assert "pending bootstrap: .venv/bin/python" in check.detail
    assert report.exit_code == 0


def test_configured_failures_are_blockers_with_remediation(tmp_path: Path) -> None:
    config = _config_text(
        default_agent="claude",
        allowed_agents=("codex",),
        state_dir="tracked-state",
        workspace_root="../outside",
        verify_command="missing-check-tool --version",
    )
    repo = _make_repo(tmp_path, config)
    tracked_state = repo / "tracked-state"
    tracked_state.mkdir()
    (tracked_state / "run.json").write_text("{}\n")
    _git(repo, "add", "tracked-state/run.json")
    _git(
        repo,
        "-c",
        "user.name=Spec Doctor Tests",
        "-c",
        "user.email=doctor@example.invalid",
        "commit",
        "-m",
        "track unsafe state",
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(),
        which=_resolver(),
    )
    checks = _checks_by_name(report)

    assert report.exit_code == 1
    assert checks["agent defaults"].status == "error"
    assert checks["verify command (test)"].status == "error"
    assert checks["state path"].status == "error"
    assert checks["workspace path"].status == "error"
    assert all(
        check.remediation
        for check in checks.values()
        if check.status == "error"
    )


def test_missing_config_is_reported_without_cli_config_bootstrap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")

    report = doctor.run_doctor_checks(repo, runner=_Runner(), which=_resolver())

    assert report.exit_code == 1
    checks = _checks_by_name(report)
    assert checks["repository"].status == "ok"
    assert checks["configuration"].status == "error"
    assert "spec init" in " ".join(checks["configuration"].remediation)


def test_base_origin_and_gh_auth_failures_are_blockers(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(base_ref="origin/main"),
        origin=False,
    )

    report = doctor.run_doctor_checks(
        repo,
        runner=_Runner(gh_auth_ok=False),
        which=_resolver(),
    )
    checks = _checks_by_name(report)

    assert checks["origin remote"].status == "error"
    assert checks["base ref"].status == "error"
    assert checks["GitHub authentication"].status == "error"
    assert report.exit_code == 1


def test_container_checks_are_read_only_and_direct_to_deep_doctor(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        _config_text(
            backend="container",
            container_block='\n[execution.container]\nengine = "docker"\nimage = "example/worker:latest"',
        ),
    )
    runner = _Runner()

    report = doctor.run_doctor_checks(
        repo,
        runner=runner,
        which=_resolver(docker=True),
    )
    checks = _checks_by_name(report)

    assert report.exit_code == 0
    assert checks["container engine"].status == "ok"
    assert checks["container worker source"].status == "ok"
    assert "spec container doctor" in checks["container diagnostics"].detail
    assert not any(Path(call[0]).name == "docker" and "run" in call for call in runner.calls)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd frontend && npm test", ("npm",)),
        ("FOO=bar env BAZ=qux python -m pytest", ("python",)),
        ("printf ok", ()),
    ],
)
def test_command_executable_extraction(command: str, expected: tuple[str, ...]) -> None:
    assert doctor._command_executables(command) == expected


def test_report_exit_code_ignores_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    report = doctor.DoctorReport(
        (
            doctor.DoctorCheck("healthy", "ok", "ready"),
            doctor.DoctorCheck(
                "optional",
                "warning",
                "not installed",
                ("Install it if needed.",),
            ),
        )
    )

    doctor.print_doctor_report(report)

    assert report.exit_code == 0
    output = capsys.readouterr().out
    assert "[warning] optional" in output
    assert "0 blocker(s), 1 warning(s)" in output


def test_cli_doctor_bypasses_normal_config_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_cmd(args: object) -> int:
        captured["repo_root"] = getattr(args, "repo_root")
        return 7

    monkeypatch.setattr(doctor, "cmd_doctor", fake_cmd)
    monkeypatch.setattr(
        cli,
        "_lazy_config",
        lambda: (_ for _ in ()).throw(AssertionError("normal config loading must be bypassed")),
    )

    result = cli.main(["doctor", "--repo-root", str(tmp_path)])

    assert result == 7
    assert captured["repo_root"] == str(tmp_path)
