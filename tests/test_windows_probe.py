"""Focused native-Windows product invariants.

These probes deliberately exercise production paths.  They are expected to expose
porting failures until the owning Windows-support specs land.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="native Windows probe; exercised by the non-blocking windows-latest job",
)


def _clean_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("SPEC_CONFIG", None)
    return env


def _wait_for_path(path: Path, process: subprocess.Popen[object], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert process.poll() is None, f"process exited before creating {path}"
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.05)


def _wait_for_identity_exit(identity: object, *, timeout: float = 10.0) -> None:
    from spec_runtime.process_supervisor import identity_matches

    deadline = time.monotonic() + timeout
    while identity_matches(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not identity_matches(identity), f"process identity remained live: {identity}"


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 30,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    expected_codes = {expected} if isinstance(expected, int) else expected
    assert completed.returncode in expected_codes, (
        f"command returned {completed.returncode}, expected {sorted(expected_codes)}: "
        f"{argv!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed


def _cli(
    repo: Path,
    *args: str,
    env: dict[str, str],
    timeout: float = 30,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    """Run the installed module in isolated mode, never checkout source."""
    return _run(
        [sys.executable, "-I", "-m", "spec_runtime.cli", *args],
        cwd=repo,
        env=env,
        timeout=timeout,
        expected=expected,
    )


def _git(
    repo: Path,
    *args: str,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo, expected=expected)


def _wait_until(predicate, *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {detail}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_fake_cli_tools(fake_bin: Path, python: Path) -> None:
    """Install deterministic model/forge doubles at the external CLI boundary."""
    from pip._vendor.distlib.scripts import ScriptMaker

    fake_bin.mkdir()
    (fake_bin / "fake-gh.py").write_text(
        """import json, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if args[:2] == ["auth", "token"]:
    print("fixture-token")
elif args[:2] == ["pr", "list"]:
    print("[]")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "example/windows-ci-fixture"}))
elif args and args[0] == "api":
    print("[]")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    (fake_bin / "gh.cmd").write_text(
        f'@echo off\r\n"{python}" -I "%~dp0fake-gh.py" %*\r\n',
        encoding="utf-8",
    )
    (fake_bin / "fixture_codex.py").write_text(
        """import os
import pathlib
import subprocess
import sys

def main():
    if sys.argv[1:3] == ["exec", "--help"]:
        print("codex exec --json --output-schema")
        return 0
    marker = pathlib.Path(".fixture-agent-needs-input")
    python = os.environ["SPEC_FIXTURE_PYTHON"]
    if not marker.exists():
        marker.write_text("waiting")
        return subprocess.run([
            python, "-I", "-m", "spec_runtime.cli", "report",
            "--status", "needs-input", "--summary", "Choose fixture behavior A or B",
        ]).returncode
    pathlib.Path("windows-resolution.txt").write_text("resolved")
    added = subprocess.run(["git", "add", str(marker), "windows-resolution.txt"])
    if added.returncode:
        return added.returncode
    committed = subprocess.run([
        "git", "-c", "user.name=Windows CI",
        "-c", "user.email=windows-ci@example.invalid",
        "commit", "-m", "Resolve Windows fixture input",
    ])
    if committed.returncode:
        return committed.returncode
    return subprocess.run([
        python, "-I", "-m", "spec_runtime.cli", "report",
        "--status", "ok", "--summary", "Selected fixture behavior A",
    ]).returncode
""",
        encoding="utf-8",
    )
    maker = ScriptMaker(None, str(fake_bin))
    maker.executable = str(python)
    maker.variants = {""}
    generated = maker.make("codex = fixture_codex:main")
    assert generated


def _write_fake_spec_launcher(fake_bin: Path, python: Path) -> None:
    from pip._vendor.distlib.scripts import ScriptMaker

    (fake_bin / "fixture_spec.py").write_text(
        """import json
import os
import pathlib
import time

def main():
    marker = pathlib.Path(os.environ["SPEC_FIXTURE_AUTO_MARKER"])
    release = pathlib.Path(os.environ["SPEC_FIXTURE_AUTO_RELEASE"])
    count = 0
    if marker.exists():
        count = int(json.loads(marker.read_text()).get("launch_count", 0))
    marker.write_text(json.dumps({"pid": os.getpid(), "launch_count": count + 1}))
    while not release.exists():
        time.sleep(0.2)
    return 0
""",
        encoding="utf-8",
    )
    maker = ScriptMaker(None, str(fake_bin))
    maker.executable = str(python)
    maker.variants = {""}
    generated = maker.make("spec = fixture_spec:main")
    assert generated


def _wait_for_http(url: str, token: str, process: subprocess.Popen[str] | None = None) -> None:
    def reachable() -> bool:
        if process is not None and process.poll() is not None:
            return False
        try:
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    _wait_until(reachable, timeout=30, detail=f"HTTP listener at {url}")


def test_lifecycle_module_imports() -> None:
    importlib.import_module("spec_runtime.orchestrator")


def test_cross_process_spec_lock_contention(tmp_path: Path) -> None:
    from spec_runtime.orchestrator import SpecLock

    code = (
        "import sys,time; from pathlib import Path; "
        "from spec_runtime.orchestrator import SpecLock; "
        "lock=SpecLock(Path(sys.argv[1]),'windows-probe'); lock.__enter__(); "
        "print('locked',flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        try:
            with SpecLock(tmp_path, "windows-probe"):
                pass
        except RuntimeError as exc:
            assert "Lock contention" in str(exc)
        else:
            raise AssertionError("a second process acquired an already-held spec lock")
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_parent_child_grandchild_termination(tmp_path: Path) -> None:
    from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor
    from spec_runtime.worktree_process_registry import (
        is_process_alive,
        read_process_identity,
        reap_registered_processes,
        register_process,
    )

    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    grandchild_code = "import time; time.sleep(30)"
    child_code = (
        "import os,subprocess,sys,time; "
        "grandchild=subprocess.Popen([sys.executable,'-c',sys.argv[3]]); "
        "open(sys.argv[1],'w').write(str(os.getpid())); "
        "open(sys.argv[2],'w').write(str(grandchild.pid)); "
        "time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[1],sys.argv[2],sys.argv[4]]); "
        "time.sleep(30)"
    )
    parent = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [
            sys.executable,
            "-c",
            parent_code,
            str(child_pid_path),
            str(grandchild_pid_path),
            child_code,
            grandchild_code,
        ],
    )
    pids = [parent.pid]
    identities = []
    try:
        deadline = time.monotonic() + 10
        while not (child_pid_path.exists() and grandchild_pid_path.exists()):
            assert parent.poll() is None, "parent exited before creating its descendants"
            assert time.monotonic() < deadline, "timed out waiting for descendant PIDs"
            time.sleep(0.1)

        pids = [parent.pid, int(child_pid_path.read_text()), int(grandchild_pid_path.read_text())]
        identities = [read_process_identity(pid) for pid in pids]
        assert all(identity is not None for identity in identities)
        parent_identity = identities[0]
        assert parent_identity is not None
        register_process(
            tmp_path,
            tmp_path / "worktree",
            name="process-tree",
            kind="probe",
            pid=parent.pid,
            started_at=parent_identity.started_at,
            termination_scope="pgid",
            pgid=parent.pid,
            supervision_token=parent.token,
        )
        report = reap_registered_processes(tmp_path, tmp_path / "worktree")
        assert not report.surviving
        parent.wait(timeout=10)
        survivors = [
            identity.pid
            for identity in identities
            if identity is not None and is_process_alive(identity.pid, identity.started_at)
        ]
        assert not survivors, f"process-tree descendants survived reap: {survivors}"
    finally:
        identities_by_pid = {
            identity.pid: identity for identity in identities if identity is not None
        }
        for pid in reversed(pids):
            identity = identities_by_pid.get(pid)
            if identity is not None and not is_process_alive(pid, identity.started_at):
                continue
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                subprocess.run(["kill", "-KILL", str(pid)], check=False)
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=10)


def test_spec_stop_terminates_owned_tree_without_touching_unrelated_process(tmp_path: Path) -> None:
    """Exercise the persisted ``spec stop`` boundary in separate real processes."""
    from spec_runtime.orchestrator import RunState, stop_run
    from spec_runtime.process_supervisor import identity_matches, inspect_process

    repo = tmp_path / "Repo stop snow-雪"
    repo.mkdir()
    ready_path = tmp_path / "stop-ready.json"
    target_script = tmp_path / "stop-target.py"
    target_script.write_text(
        "import json,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "from spec_runtime.orchestrator import (OrchestratorTerminationRequested,RunState,"
        "_ensure_orchestrator_process_group,_orchestrator_sigterm_guard)\n"
        "repo,ready=Path(sys.argv[1]),Path(sys.argv[2])\n"
        "run=RunState(run_id='windows-stop-run',spec_id='windows-stop',"
        "branch='code/windows-stop--native',phase='implement',status='running')\n"
        "with _orchestrator_sigterm_guard(run,repo):\n"
        " _ensure_orchestrator_process_group(run,repo)\n"
        " child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        " ready.write_text(json.dumps({'pid':__import__('os').getpid(),'child':child.pid}),encoding='utf-8')\n"
        " try:\n"
        "  while True: time.sleep(0.05)\n"
        " except OrchestratorTerminationRequested:\n"
        "  pass\n",
        encoding="utf-8",
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    target = subprocess.Popen(
        [sys.executable, str(target_script), str(repo), str(ready_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    unrelated_identity = inspect_process(unrelated.pid)
    assert unrelated_identity is not None
    child_identity = None
    target_identity = None
    try:
        _wait_for_path(ready_path, target)
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        target_identity = inspect_process(int(ready["pid"]))
        child_identity = inspect_process(int(ready["child"]))
        assert target_identity is not None
        assert child_identity is not None

        stopped = stop_run("windows-stop", repo_root=repo)

        assert stopped.status == "failed"
        assert stopped.last_error == "stopped by user"
        target.wait(timeout=10)
        _wait_for_identity_exit(target_identity)
        _wait_for_identity_exit(child_identity)
        assert identity_matches(unrelated_identity)
        assert RunState.load(repo, "windows-stop-run").status == "failed"
    finally:
        if target.poll() is None:
            target.kill()
        try:
            target.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=10)


def test_local_review_timeout_reaps_tree_without_touching_unrelated_process(tmp_path: Path) -> None:
    """The reviewer timeout path must terminate its real Windows Job only."""
    from spec_runtime import orchestrator as orch
    from spec_runtime.process_supervisor import identity_matches, inspect_process

    repo = tmp_path / "review-state"
    review_worktree = tmp_path / "review checkout"
    review_worktree.mkdir()
    ready_path = tmp_path / "review-ready.json"
    script = tmp_path / "review-tree.py"
    script.write_text(
        "import json,os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child':child.pid}),encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    unrelated_identity = inspect_process(unrelated.pid)
    assert unrelated_identity is not None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            orch._run_local_review_subprocess(
                repo,
                [sys.executable, str(script), str(ready_path)],
                cwd=review_worktree,
                env=_clean_subprocess_env(),
                timeout=2,
            )

        assert ready_path.exists()
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        reviewer_identity = inspect_process(int(ready["pid"]))
        child_identity = inspect_process(int(ready["child"]))
        # Both processes should already be gone. Reconstructing exact creation
        # identities is impossible after exit, so their absence is authoritative.
        assert reviewer_identity is None
        assert child_identity is None
        assert identity_matches(unrelated_identity)
    finally:
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=10)


def test_cleanup_reaps_registered_helper_and_preserves_unrelated_process(tmp_path: Path) -> None:
    """Exercise cleanup against a real Unicode worktree and real Job token."""
    from spec_runtime import orchestrator as orch
    from spec_runtime import worktree_process_registry as registry
    from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor, identity_matches, inspect_process

    repo = tmp_path / "Repo cleanup snow-雪"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Windows Probe",
            "-c",
            "user.email=probe@example.invalid",
            "commit",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    branch = "code/windows-cleanup--native"
    worktree = repo / ".worktrees" / "cleanup space-雪"
    worktree.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    helper = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    unrelated_identity = inspect_process(unrelated.pid)
    assert unrelated_identity is not None
    registry.register_process(
        repo / ".spec-state",
        worktree,
        name="native-helper",
        kind="probe",
        pid=helper.token.payload.pid,
        started_at=helper.token.payload.started_at,
        termination_scope="supervision",
        supervision_token=helper.token,
    )
    run = orch.RunState(
        run_id="windows-cleanup-run",
        spec_id="windows-cleanup",
        branch=branch,
        worktree_path=str(worktree),
    )
    try:
        assert orch.phase_cleanup(run, repo) == "passed"
        helper.wait(timeout=10)
        assert not identity_matches(helper.token.payload)
        assert identity_matches(unrelated_identity)
        assert not worktree.exists()
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            check=False,
        )
        assert branch_check.returncode == 1
        assert registry.list_registered_worktrees(repo / ".spec-state") == []
    finally:
        if helper.poll() is None:
            helper.kill()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=10)


def test_spec_init_output_is_accepted_by_doctor(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "codex.cmd").write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    subprocess_env = _clean_subprocess_env()
    subprocess_env["PATH"] = os.pathsep.join(
        [str(fake_bin), subprocess_env.get("PATH", "")]
    )

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("probe\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Windows Probe", "-c", "user.email=probe@example.invalid", "commit", "-m", "probe"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/spec-windows-probe.git"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=tmp_path,
        check=True,
    )
    initialized = subprocess.run(
        [sys.executable, "-m", "spec_runtime.cli", "init"],
        cwd=tmp_path,
        env=subprocess_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    config_text = (tmp_path / ".spec.toml").read_text(encoding="utf-8")
    assert 'default = "codex"' in config_text
    assert 'allowed = ["codex"]' in config_text
    doctor = subprocess.run(
        [sys.executable, "-m", "spec_runtime.cli", "doctor"],
        cwd=tmp_path,
        env=subprocess_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr


def test_foreground_web_bind_and_authenticated_request(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = "windows-probe-token"
    token_path = tmp_path / ".spec-state" / "web" / "auth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(token)
    (tmp_path / ".spec.toml").write_text('[project]\nbase_ref = "main"\n')
    stdout_path = tmp_path / "web-probe.stdout.log"
    stderr_path = tmp_path / "web-probe.stderr.log"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_log,
        stderr_path.open("w", encoding="utf-8") as stderr_log,
    ):
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "spec_runtime.cli",
                "web",
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=_clean_subprocess_env(),
            stdout=stdout_log,
            stderr=stderr_log,
            text=True,
        )

        def stop_server() -> None:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

        def diagnostics() -> str:
            stdout_log.flush()
            stderr_log.flush()
            return stdout_path.read_text(errors="replace") + stderr_path.read_text(
                errors="replace"
            )

        try:
            # Importing the full web stack can take noticeably longer on a cold
            # or memory-constrained Windows runner. Keep the probe bounded while
            # allowing enough time for the production server to bind.
            startup_timeout = 30.0
            deadline = time.monotonic() + startup_timeout
            last_error: Exception | None = None
            url = f"http://127.0.0.1:{port}/"
            while True:
                try:
                    request = urllib.request.Request(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    with urllib.request.urlopen(request, timeout=1) as response:
                        assert response.status == 200
                    break
                except (OSError, urllib.error.URLError) as exc:
                    last_error = exc
                    if server.poll() is not None:
                        raise AssertionError(
                            "web server exited before accepting an authenticated "
                            f"request: {last_error}\n{diagnostics()}"
                        )
                    if time.monotonic() >= deadline:
                        stop_server()
                        raise AssertionError(
                            "web server did not accept an authenticated request "
                            f"within {startup_timeout:g} seconds: {last_error}\n"
                            f"{diagnostics()}"
                        )
                    time.sleep(0.1)

            wrong_token_request = urllib.request.Request(
                url,
                headers={"Authorization": "Bearer wrong-token"},
            )
            with pytest.raises(urllib.error.HTTPError) as auth_error:
                urllib.request.urlopen(wrong_token_request, timeout=2)
            assert auth_error.value.code == 401
        finally:
            stop_server()


@pytest.mark.skipif(
    os.environ.get("SPEC_WINDOWS_INSTALLED_CLI_MATRIX") != "1",
    reason="run once in the Python 3.12 wheel job",
)
def test_installed_artifact_cli_matrix(tmp_path: Path) -> None:
    """Exercise AC2 through the installed wheel with only external-boundary fakes.

    Git, CLI dispatch, worktrees, state files, HTTP listeners, detached web
    supervision, update workers, and autopilot processes are real.  Only GitHub,
    model turns, interactive-tty presentation, and the update network/installer
    provider are replaced with deterministic local fixtures.
    """
    import spec_runtime
    from spec_runtime.orchestrator import RunState

    checkout = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    imported = Path(spec_runtime.__file__).resolve()
    with pytest.raises(ValueError):
        imported.relative_to(checkout)

    # The product repository itself deliberately contains both a space and a
    # non-ASCII character.  Every command below operates from this path.
    repo = tmp_path / "Spec Butler snow-\u96ea"
    origin = tmp_path / "Origin snow-\u96ea.git"
    fake_bin = tmp_path / "fixture tools"
    operator_codex_home = tmp_path / "operator codex home"
    operator_codex_home.mkdir()
    (operator_codex_home / "auth.json").write_text(
        '{"OPENAI_API_KEY":"fixture-only-not-a-secret"}\n',
        encoding="utf-8",
    )
    _write_fake_cli_tools(fake_bin, Path(sys.executable))

    env = _clean_subprocess_env()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "CODEX_HOME": str(operator_codex_home),
            "PATH": os.pathsep.join([str(fake_bin), env.get("PATH", "")]),
            # Used only by generated native fake-provider launchers. Product
            # CLI subprocesses use -I and cannot import from this directory.
            "PYTHONPATH": str(fake_bin),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "SPEC_FIXTURE_PYTHON": sys.executable,
            "SPEC_NO_UPDATE_CHECK": "1",
        }
    )

    _run(["git", "init", "--bare", "--initial-branch=main", str(origin)], cwd=tmp_path)
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("Windows installed CLI fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Windows CI",
        "-c",
        "user.email=windows-ci@example.invalid",
        "commit",
        "-m",
        "Initialize Unicode fixture",
    )
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")

    initialized = _cli(repo, "init", env=env)
    assert "Created .spec.toml" in initialized.stdout
    assert (repo / ".spec.toml").is_file()
    doctor = _cli(repo, "doctor", env=env)
    assert "0 blocker(s)" in doctor.stdout.lower()
    assert "0 warning(s)" in doctor.stdout.lower()

    lifecycle_id = "windows-cli-flow"
    lifecycle_spec = repo / "specs" / f"{lifecycle_id}.md"
    lifecycle_spec.write_text(
        """---
id: windows-cli-flow
area: backend
priority: 10
depends_on: []
description: Exercise the installed Windows lifecycle fixture
---

# Installed Windows lifecycle fixture

## Acceptance Criteria

- [ ] Resolve the deterministic operator question and commit the answer.
""",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Windows CI",
        "-c",
        "user.email=windows-ci@example.invalid",
        "commit",
        "-m",
        "Add lifecycle fixture",
    )
    _git(repo, "push", "origin", "main")

    listed = _cli(repo, "list", env=env)
    assert lifecycle_id in listed.stdout
    shown = _cli(repo, "show", "--spec", lifecycle_id, env=env)
    assert "Installed Windows lifecycle fixture" in shown.stdout
    queued_status = _cli(repo, "status", "--spec", lifecycle_id, env=env)
    assert "No runs found" in queued_status.stdout

    # Foreground update dispatch runs in an isolated installed interpreter. The
    # release lookup and installer command are local fakes; the upgrade child is
    # still a real subprocess and the public CLI path is unchanged.
    foreground_update = """
import sys
from unittest.mock import patch
from spec_runtime.cli import main
from spec_runtime.update import InstallInfo
info = InstallInfo(
    method="pip",
    current_version="0.0.0",
    upgrade_command=(sys.executable, "-I", "-c", "print('fixture upgrade child')"),
)
with patch("spec_runtime.update.detect_installation", return_value=info), patch(
    "spec_runtime.update.resolve_repo_slug", return_value=None
):
    raise SystemExit(main(["update"]))
"""
    updated = _run(
        [sys.executable, "-I", "-c", foreground_update],
        cwd=repo,
        env=env,
    )
    assert "fixture upgrade child" in updated.stdout
    assert "Updated Spec Butler" in updated.stdout

    # Exercise the background refresh entry in its own process while replacing
    # only the HTTP release provider. This writes the production cache and lock
    # artifacts without relying on public network availability.
    background_update = """
import sys
from pathlib import Path
from unittest.mock import patch
from spec_runtime.update import _background_refresh_entry
repo, cache, lock = map(Path, sys.argv[1:4])
with patch("spec_runtime.update.resolve_repo_slug", return_value="fixture/spec"), patch(
    "spec_runtime.update.fetch_latest_version", return_value="9.9.9"
):
    _background_refresh_entry(repo, cache, lock)
"""
    update_cache = repo / ".spec-state" / "update-check.json"
    update_lock = repo / ".spec-state" / "update-check.lock"
    update_lock.parent.mkdir(parents=True, exist_ok=True)
    update_lock.write_text('{"started_at":"2026-09-01T00:00:00Z"}\n', encoding="utf-8")
    _run(
        [
            sys.executable,
            "-I",
            "-c",
            background_update,
            str(repo),
            str(update_cache),
            str(update_lock),
        ],
        cwd=tmp_path,
        env=env,
    )
    assert json.loads(update_cache.read_text(encoding="utf-8"))["latest_version"] == "9.9.9"
    assert not update_lock.exists()

    # Build a genuine linked worktree and invoke the real implement phase. The
    # fake Codex binary reports needs-input through the public `spec report`
    # handshake, exactly as a model process would.
    run_id = f"{lifecycle_id}-ci-matrix"
    branch = f"code/{lifecycle_id}--ci-matrix"
    worktree = repo / ".worktrees" / f"code-{lifecycle_id}--ci-matrix"
    worktree.parent.mkdir(exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "origin/main")
    head = _git(repo, "rev-parse", "origin/main").stdout.strip()
    RunState(
        run_id=run_id,
        spec_id=lifecycle_id,
        branch=branch,
        worktree_path=str(worktree),
        spec_path=f"specs/{lifecycle_id}.md",
        spec_revision=head,
        phase="implement",
        status="pending",
        agent="codex",
        review_agent="codex",
        base_ref="origin/main",
        backend="worktree",
        safety_mode="safe",
        backend_source="repo-config",
        backend_workspace_root=".worktrees",
    ).save(repo)
    pinned_spec = repo / ".spec-state" / "runs" / run_id / "spec.md"
    pinned_spec.parent.mkdir(parents=True, exist_ok=True)
    pinned_spec.write_text(lifecycle_spec.read_text(encoding="utf-8"), encoding="utf-8")
    first_implement = _cli(
        repo,
        "phase",
        "--spec",
        lifecycle_id,
        "--phase",
        "implement",
        "--agent",
        "codex",
        "--review-agent",
        "codex",
        "--run",
        run_id,
        env=env,
        timeout=60,
        expected={1, 2},
    )
    waiting_payload = json.loads(
        (repo / ".spec-state" / "runs" / f"{run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert waiting_payload["status"] == "waiting-for-input"
    assert waiting_payload["input_question"] == "Choose fixture behavior A or B"
    waiting_status = _cli(repo, "status", "--spec", lifecycle_id, env=env)
    assert "waiting-for-input" in waiting_status.stdout
    assert "Choose fixture behavior A or B" in waiting_status.stdout
    assert first_implement.returncode != 0

    # `input` is intentionally interactive. Hosted pytest has no console, so
    # this shim supplies only the isatty boundary; the actual command launches
    # the fake model subprocess, consumes its fresh report, and resolves the
    # persisted operator request. Continuing late forge phases is outside this
    # smoke and is replaced with a no-op return.
    input_runner = f"""
import sys
from unittest.mock import patch
import spec_runtime.orchestrator as orchestrator
from spec_runtime.cli import main
class Tty:
    def __init__(self, wrapped): self.wrapped = wrapped
    def isatty(self): return True
    def __getattr__(self, name): return getattr(self.wrapped, name)
sys.stdin = Tty(sys.stdin)
with patch.object(orchestrator, "cmd_run", return_value=0):
    raise SystemExit(main(["input", "--spec", {lifecycle_id!r}, "--agent", "codex"]))
"""
    resolved = _run(
        [sys.executable, "-I", "-c", input_runner],
        cwd=repo,
        env=env,
        timeout=60,
    )
    assert "Operator intervention resolved" in resolved.stdout
    run_payload = json.loads(
        (repo / ".spec-state" / "runs" / f"{run_id}.json").read_text(encoding="utf-8")
    )
    assert run_payload["status"] == "passed"
    assert run_payload["input_response"] == "Selected fixture behavior A"
    assert (worktree / "windows-resolution.txt").read_text().strip() == "resolved"

    # Foreground and background web modes both bind real sockets and serve an
    # authenticated request. Background status/stop traverse durable Windows
    # supervision rather than terminating a pytest-owned process directly.
    token = "windows-installed-matrix-token"
    token_path = repo / ".spec-state" / "web" / "auth-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    foreground_port = _free_port()
    foreground_log = repo / ".spec-state" / "web" / "foreground-test.log"
    with foreground_log.open("w", encoding="utf-8") as web_log:
        foreground = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "spec_runtime.cli",
                "web",
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                str(foreground_port),
            ],
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=web_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    try:
        _wait_for_http(f"http://127.0.0.1:{foreground_port}/", token, foreground)
    finally:
        if foreground.poll() is None:
            foreground.terminate()
        try:
            foreground.wait(timeout=10)
        except subprocess.TimeoutExpired:
            foreground.kill()
            foreground.wait(timeout=5)

    background_port = _free_port()
    background_started = _cli(
        repo,
        "web",
        "start",
        "--background",
        "--host",
        "127.0.0.1",
        "--port",
        str(background_port),
        env=env,
        timeout=45,
    )
    assert "spec web running" in (background_started.stdout + background_started.stderr)
    web_status = _cli(repo, "web", "status", env=env)
    assert str(background_port) in web_status.stdout
    _wait_for_http(f"http://127.0.0.1:{background_port}/", token)
    _cli(repo, "web", "stop", env=env, timeout=30)
    stopped_status = _cli(repo, "web", "status", env=env)
    assert "not running" in (stopped_status.stdout + stopped_status.stderr).lower()

    cleaned = _cli(repo, "clean", "--spec", lifecycle_id, env=env)
    assert "Removed worktree" in cleaned.stdout
    assert not worktree.exists()
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            expected=1,
        ).returncode
        == 1
    )

    # Keep the resolved lifecycle record out of the autopilot queue and add a
    # fresh dispatch target. The child command deliberately stays alive so a
    # replacement dispatcher must adopt it instead of starting a duplicate.
    _git(repo, "rm", f"specs/{lifecycle_id}.md")
    auto_id = "windows-auto-ready"
    (repo / "specs" / f"{auto_id}.md").write_text(
        """---
id: windows-auto-ready
area: backend
priority: 1
depends_on: []
description: Exercise installed Windows autopilot supervision
---

# Windows autopilot fixture
""",
        encoding="utf-8",
    )
    _git(repo, "add", "specs")
    _git(
        repo,
        "-c",
        "user.name=Windows CI",
        "-c",
        "user.email=windows-ci@example.invalid",
        "commit",
        "-m",
        "Prepare autopilot fixture",
    )
    _git(repo, "push", "origin", "main")

    auto_marker = tmp_path / "autopilot-child.json"
    auto_release = tmp_path / "autopilot-child.release"
    _write_fake_spec_launcher(fake_bin, Path(sys.executable))
    auto_env = env.copy()
    auto_env["PYTHONUNBUFFERED"] = "1"
    auto_env["SPEC_FIXTURE_AUTO_MARKER"] = str(auto_marker)
    auto_env["SPEC_FIXTURE_AUTO_RELEASE"] = str(auto_release)
    auto_command = [
        sys.executable,
        "-I",
        "-m",
        "spec_runtime.cli",
        "auto",
        "run",
        "--repo-root",
        str(repo),
        "--concurrency",
        "1",
        "--poll-interval",
        "1",
        "--agent",
        "codex",
    ]
    first_log_path = tmp_path / "autopilot-first.log"
    first_log = first_log_path.open("w", encoding="utf-8")
    first_dispatcher = subprocess.Popen(
        auto_command,
        cwd=repo,
        env=auto_env,
        stdin=subprocess.DEVNULL,
        stdout=first_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until(auto_marker.exists, timeout=30, detail="autopilot dispatch")
        first_payload = json.loads(auto_marker.read_text(encoding="utf-8"))
        assert first_payload["launch_count"] == 1
        _wait_until(
            lambda: (repo / ".spec-state" / "autopilot" / "active.json").exists(),
            timeout=15,
            detail="autopilot active state",
        )
        first_dispatcher.terminate()
        first_dispatcher.wait(timeout=10)
    finally:
        if first_dispatcher.poll() is None:
            first_dispatcher.kill()
            first_dispatcher.wait(timeout=5)
        first_log.close()

    second_log_path = tmp_path / "autopilot-second.log"
    second_log = second_log_path.open("w", encoding="utf-8")
    second_dispatcher = subprocess.Popen(
        auto_command,
        cwd=repo,
        env=auto_env,
        stdin=subprocess.DEVNULL,
        stdout=second_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        def child_was_adopted() -> bool:
            try:
                active_payload = json.loads(
                    (repo / ".spec-state" / "autopilot" / "active.json").read_text(
                        encoding="utf-8"
                    )
                )
                active_item = active_payload[auto_id]
            except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
                return False
            return (
                active_item.get("adoption_generation", 0) >= 1
                and int(active_item.get("adopted_by", {}).get("pid", 0)) > 0
            )

        _wait_until(
            child_was_adopted,
            timeout=30,
            detail="autopilot child adoption",
        )
        assert json.loads(auto_marker.read_text(encoding="utf-8"))["launch_count"] == 1
        stop_command = [
            sys.executable,
            "-I",
            "-m",
            "spec_runtime.cli",
            "auto",
            "stop",
            "--repo-root",
            str(repo),
        ]
        stopper = subprocess.Popen(
            stop_command,
            cwd=repo,
            env=auto_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        shutdown_path = repo / ".spec-state" / "autopilot" / "shutdown.json"

        def graceful_stop_was_requested() -> bool:
            try:
                return json.loads(shutdown_path.read_text(encoding="utf-8"))["phase"] == "graceful"
            except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
                return False

        _wait_until(
            graceful_stop_was_requested,
            timeout=10,
            detail="autopilot graceful shutdown request",
        )
        auto_release.touch()
        stopped_stdout, stopped_stderr = stopper.communicate(timeout=15)
        assert stopper.returncode == 0, stopped_stdout + stopped_stderr
        assert "acknowledged shutdown" in (stopped_stdout + stopped_stderr)
        second_dispatcher.wait(timeout=15)
    finally:
        if second_dispatcher.poll() is None:
            second_dispatcher.kill()
            second_dispatcher.wait(timeout=5)
        second_log.close()

    active_path = repo / ".spec-state" / "autopilot" / "active.json"
    if active_path.exists():
        assert json.loads(active_path.read_text(encoding="utf-8")) == {}
