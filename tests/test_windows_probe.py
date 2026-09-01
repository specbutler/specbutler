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
    import os

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
