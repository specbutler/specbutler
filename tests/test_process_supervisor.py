from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import spec_runtime.process_supervisor as process_supervisor
from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    ProcessSupervisor,
    SupervisionToken,
    adopt,
    identity_matches,
    inspect_process,
    promote_payload_identity,
    run,
    terminate,
)


def test_token_round_trip_preserves_reopenable_identity() -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "token", 9)
    assert SupervisionToken.from_dict(token.to_dict()) == token
    assert token.version == 2
    assert token.control_relpath.endswith("/control.json")
    assert token.control_nonce


@pytest.mark.parametrize(
    "missing",
    ["supervision_id", "job_name", "keeper_identity", "payload_identity", "control_relpath", "control_nonce"],
)
def test_v2_token_parser_does_not_mint_missing_security_fields(missing: str) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload.pop(missing)

    with pytest.raises(ValueError, match="V2 supervision token is missing"):
        SupervisionToken.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 3, "unsupported supervision token version"),
        ("job_name", r"Local\SpecButler-arbitrary", "noncanonical Job name"),
        ("control_relpath", "controls/other/control.json", "noncanonical control path"),
    ],
)
def test_v2_token_parser_rejects_noncanonical_ownership_metadata(
    field: str, value: object, message: str
) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        SupervisionToken.from_dict(payload)


@pytest.mark.parametrize("identity_field", ["keeper_identity", "payload_identity"])
def test_v2_token_parser_rejects_nonpositive_identity(identity_field: str) -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    payload = SupervisionToken(LifetimeMode.DETACHED, identity, 7, "owner", "strict-token").to_dict()
    payload[identity_field] = {**payload[identity_field], "pid": 0}

    with pytest.raises(ValueError, match="positive PIDs"):
        SupervisionToken.from_dict(payload)


def test_identity_rejects_stale_creation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ProcessIdentity(os.getpid(), "old", sys.executable)
    monkeypatch.setattr(
        "spec_runtime.process_supervisor.inspect_process",
        lambda pid: ProcessIdentity(pid, "new", sys.executable),
    )
    assert identity_matches(expected) is False


def test_adoptable_token_records_new_logical_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = ProcessIdentity(42, "created", "python.exe")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "unique-adoption-token")
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    control_path = tmp_path / token.control_relpath
    control_path.parent.mkdir(parents=True)
    control_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "supervision_id": token.token,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
                "payload_identity": token.payload.to_dict(),
                "adopted_by": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("spec_runtime.process_supervisor.identity_matches", lambda _identity: True)
    monkeypatch.setattr(
        "spec_runtime.process_supervisor.inspect_process",
        lambda pid: ProcessIdentity(pid, "new-owner", sys.executable),
    )
    assert adopt(token).owner_pid == os.getpid()
    state = json.loads(control_path.read_text(encoding="utf-8"))
    assert state["adoption_generation"] == 1
    assert state["adopted_by"]["pid"] == os.getpid()
    with pytest.raises(ValueError, match="already adopted"):
        adopt(token)


def test_token_distinguishes_supervisor_and_payload_identity() -> None:
    helper = ProcessIdentity(41, "helper")
    payload = ProcessIdentity(42, "payload")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, helper, 7, "owner", uuid.uuid4().hex, 0, payload)
    restored = SupervisionToken.from_dict(token.to_dict())
    assert restored.identity == helper
    assert restored.payload == payload


def test_token_persists_explicit_job_name() -> None:
    keeper = ProcessIdentity(41, "helper")
    payload = ProcessIdentity(42, "payload")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "supervision-id",
        payload_identity=payload,
        job_name=r"Local\SpecButler-supervision-id",
    )
    restored = SupervisionToken.from_dict(token.to_dict())
    assert restored.identity == keeper
    assert restored.payload == payload
    assert restored.job_name == token.job_name


def test_managed_process_keeps_live_job_registered_when_close_handle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "close-failure")

    class FailingJob:
        def close(self) -> None:
            raise OSError("CloseHandle failed")

    job = FailingJob()
    key = (identity.pid, identity.started_at)
    monkeypatch.setitem(process_supervisor._LIVE_WINDOWS_JOBS, key, job)
    managed = process_supervisor.ManagedProcess(object(), token, job)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="CloseHandle failed"):
        managed.close()

    assert process_supervisor._LIVE_WINDOWS_JOBS[key] is job
    assert managed._job is job


def test_managed_process_wait_closes_job_after_leader_exit() -> None:
    events: list[str] = []
    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "normal-wait")

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            return 23

    class Job:
        def close(self) -> None:
            events.append("close")

    managed = process_supervisor.ManagedProcess(Process(), token, Job())  # type: ignore[arg-type]
    assert managed.wait(timeout=2.0) == 23
    assert events == ["wait:2.0", "close"]
    assert managed._job is None


def test_managed_process_communicate_preserves_baseexception_when_cleanup_fails() -> None:
    class Abort(BaseException):
        pass

    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "sync-abort")

    class Process:
        def communicate(self, **_kwargs: object) -> tuple[None, None]:
            raise Abort

    class Job:
        def terminate(self) -> None:
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            raise ValueError("close cleanup failed")

    managed = process_supervisor.ManagedProcess(Process(), token, Job())  # type: ignore[arg-type]
    with pytest.raises(Abort):
        managed.communicate()


def test_managed_async_communicate_preserves_baseexception_when_cleanup_fails() -> None:
    class Abort(BaseException):
        pass

    identity = ProcessIdentity(42, "created")
    token = SupervisionToken(LifetimeMode.RUN_OWNED, identity, 7, "owner", "async-abort")

    class Process:
        async def communicate(self, _input: bytes | None) -> tuple[None, None]:
            raise Abort

        async def wait(self) -> int:
            raise ValueError("wait cleanup failed")

    class Job:
        def terminate(self) -> None:
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            raise ValueError("close cleanup failed")

    async def exercise() -> None:
        managed = process_supervisor.ManagedAsyncProcess(Process(), token, Job())  # type: ignore[arg-type]
        with pytest.raises(Abort):
            await managed.communicate()

    asyncio.run(exercise())


def test_held_windows_job_uses_original_group_and_exact_grace_before_hard_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    identity = ProcessIdentity(42, "exited-shim")
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        identity,
        7,
        "owner",
        "held-job-grace",
        pgid=4242,
    )

    class Job:
        def active_process_ids(self) -> tuple[int, ...]:
            return (99,)

        def wait_empty(self, timeout: float) -> bool:
            events.append(("wait", timeout))
            return False

        def active_identities(self) -> list[ProcessIdentity]:
            return []

        def terminate(self) -> None:
            events.append("hard-kill")

    monkeypatch.setattr(process_supervisor, "_send_windows_break", lambda pgid: events.append(("break", pgid)) or True)

    assert process_supervisor._terminate_held_windows_job(token, Job(), 0.375) is True  # type: ignore[arg-type]
    assert events == [("break", 4242), ("wait", 0.375), "hard-kill"]


def test_durable_metadata_path_is_independent_of_payload_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control_root = tmp_path / "writable-controls"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(control_root))
    monkeypatch.chdir(tmp_path)

    path = process_supervisor.durable_metadata_path("stable-id")

    assert path == control_root / "metadata" / "stable-id.json"
    assert path.parent != tmp_path


def _write_promotion_state(tmp_path: Path, token: SupervisionToken) -> None:
    control_path = tmp_path / token.control_relpath
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "supervision_id": token.token,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
                "payload_identity": token.payload.to_dict(),
                "request": None,
            }
        ),
        encoding="utf-8",
    )
    metadata_path = process_supervisor.durable_metadata_path(token.token)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")


def test_payload_promotion_updates_locked_control_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    candidate = ProcessIdentity(43, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-positive",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            return identity == candidate

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda identity: identity != shim)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))

    promoted = promote_payload_identity(token, candidate)

    assert promoted.payload == candidate
    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    metadata = SupervisionToken.from_dict(
        json.loads(process_supervisor.durable_metadata_path(token.token).read_text(encoding="utf-8"))
    )
    assert control["payload_identity"] == candidate.to_dict()
    assert metadata.payload == candidate


def test_payload_promotion_rejects_candidate_from_another_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    foreign = ProcessIdentity(99, "foreign", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-cross-job",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, _identity: ProcessIdentity) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda _identity: True)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))

    with pytest.raises(ValueError, match="not an active member"):
        promote_payload_identity(token, foreign)

    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    assert control["payload_identity"] == shim.to_dict()


def test_payload_promotion_converges_after_death_between_atomic_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    keeper = ProcessIdentity(41, "keeper", "python.exe")
    shim = ProcessIdentity(42, "shim", "python.exe")
    candidate = ProcessIdentity(43, "payload", "python.exe")
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        keeper,
        7,
        "owner",
        "promotion-crash-convergence",
        payload_identity=shim,
    )
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path))
    _write_promotion_state(tmp_path, token)

    class Job:
        def contains(self, identity: ProcessIdentity) -> bool:
            return identity == candidate

        def close(self) -> None:
            pass

    monkeypatch.setattr(process_supervisor, "identity_matches", lambda identity: identity != shim)
    monkeypatch.setattr(process_supervisor._WindowsJob, "open", classmethod(lambda _cls, _name: Job()))
    real_atomic_write = process_supervisor.atomic_write_text
    write_count = 0

    def die_on_metadata(path: Path, content: str) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise SimulatedProcessDeath
        real_atomic_write(path, content)

    monkeypatch.setattr(process_supervisor, "atomic_write_text", die_on_metadata)
    with pytest.raises(SimulatedProcessDeath):
        promote_payload_identity(token, candidate)

    control = json.loads((tmp_path / token.control_relpath).read_text(encoding="utf-8"))
    metadata = SupervisionToken.from_dict(
        json.loads(process_supervisor.durable_metadata_path(token.token).read_text(encoding="utf-8"))
    )
    assert control["payload_identity"] == candidate.to_dict()
    assert metadata.payload == shim

    monkeypatch.setattr(process_supervisor, "atomic_write_text", real_atomic_write)
    assert promote_payload_identity(token, candidate).payload == candidate


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object integration")
@pytest.mark.parametrize("action", ["normal", "stop", "owner-close"])
def test_windows_run_owned_parent_child_grandchild_tree(tmp_path: Path, action: str) -> None:
    """Exercise real descendants; no process API is mocked in this test."""
    pid_file = tmp_path / "pids.json"
    script = tmp_path / "tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30 if level else 2)\n",
        encoding="utf-8",
    )
    supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
    managed = supervisor.spawn([sys.executable, str(script), "0", str(pid_file)])
    while not all(Path(f"{pid_file}-{level}").exists() for level in range(3)):
        time.sleep(0.05)
    if action == "normal":
        managed.wait(timeout=10)
    else:
        if action == "owner-close":
            supervisor.close()
        else:
            managed.terminate(grace_seconds=0.1)
        managed.wait(timeout=10)
    for level in range(3):
        pid = int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8"))
        assert inspect_process(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows timeout integration")
def test_windows_run_timeout_kills_parent_child_grandchild_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout-pids"
    script = tmp_path / "timeout-tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, str(script), "0", str(pid_file)], timeout=1, capture_output=True)
    for level in range(3):
        path = Path(f"{pid_file}-{level}")
        assert path.exists()
        assert inspect_process(int(path.read_text(encoding="utf-8"))) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows owner death integration")
def test_windows_external_owner_death_closes_job_and_kills_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "owner-death-pids"
    tree = tmp_path / "owner-death-tree.py"
    tree.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "owner.py"
    launcher.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn([sys.executable,sys.argv[1],'0',sys.argv[2]])\n"
        "paths=[Path(sys.argv[2]+'-'+str(i)) for i in range(3)]\n"
        "while not all(path.exists() for path in paths): time.sleep(.05)\n"
        "os._exit(17)\n",
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(launcher), str(tree), str(pid_file)], check=False, timeout=10)
    assert completed.returncode == 17
    deadline = time.monotonic() + 10
    identities = [int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8")) for level in range(3)]
    while any(inspect_process(pid) is not None for pid in identities) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(inspect_process(pid) is None for pid in identities)


@pytest.mark.skipif(os.name != "nt", reason="native Windows graceful cancellation integration")
def test_windows_stop_attempts_graceful_break_before_job_termination(tmp_path: Path) -> None:
    marker = tmp_path / "graceful"
    ready = tmp_path / "ready"
    code = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK,lambda *_:(Path(sys.argv[1]).write_text('graceful'),sys.exit(0))); "
        "Path(sys.argv[2]).write_text('ready'); "
        "time.sleep(30)"
    )
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", code, str(marker), str(ready)]
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    managed.terminate(grace_seconds=3)
    managed.wait(timeout=10)
    # GenerateConsoleCtrlEvent is explicitly best-effort: Windows may accept
    # the request yet suppress it in inherited/redirector console topologies.
    # When delivery is supported the handler proves it ran; either way the
    # bounded Job fallback must leave the complete owned tree dead.
    if marker.exists():
        assert marker.read_text(encoding="utf-8") == "graceful"
    assert inspect_process(managed.token.identity.pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows stale identity integration")
def test_windows_rejects_stale_identity_without_signaling_live_process() -> None:
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    stale = SupervisionToken(
        managed.token.mode,
        ProcessIdentity(managed.token.identity.pid, "recycled", managed.token.identity.executable),
        managed.token.owner_pid,
        managed.token.owner_started_at,
        managed.token.token,
        payload_identity=managed.token.payload,
    )
    try:
        assert terminate(stale, grace_seconds=0) is False
        assert identity_matches(managed.token.identity)
    finally:
        managed.kill()
        managed.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="native Windows inherited pipe integration")
@pytest.mark.parametrize("timeout", [None, 2])
def test_windows_capture_closes_pipe_inherited_by_descendant(tmp_path: Path, timeout: float | None) -> None:
    script = tmp_path / "inherited-pipe.py"
    script.write_text(
        "import subprocess,sys\n"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        "print('leader-exited')\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    completed = run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout)
    assert completed.stdout.strip() == "leader-exited"
    assert time.monotonic() - started < 6


@pytest.mark.skipif(os.name != "nt", reason="native Windows async Job Object integration")
def test_windows_async_run_owned_owner_close_kills_complete_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "async-pids"
    script = tmp_path / "async-tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "open(path+'-'+str(level),'w').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
        managed = await supervisor.spawn_async([sys.executable, str(script), "0", str(pid_file)])
        while not all(Path(f"{pid_file}-{level}").exists() for level in range(3)):
            await asyncio.sleep(0.05)
        supervisor.close()
        await asyncio.wait_for(managed.wait(), timeout=10)

    asyncio.run(exercise())
    for level in range(3):
        pid = int(Path(f"{pid_file}-{level}").read_text(encoding="utf-8"))
        assert inspect_process(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows async cancellation integration")
def test_windows_async_terminate_is_nonblocking(tmp_path: Path) -> None:
    ready = tmp_path / "async-terminate-ready"
    code = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK,lambda *_:None); "
        "Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
    )

    async def exercise() -> None:
        managed = await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
            [sys.executable, "-c", code, str(ready)]
        )
        while not ready.exists():
            await asyncio.sleep(0.02)
        started = time.monotonic()
        managed.terminate()
        assert time.monotonic() - started < 0.5
        assert managed._job is not None and managed._job.active_process_ids()
        managed.kill()
        await asyncio.wait_for(managed.wait(), timeout=10)

    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_sync_launch_baseexception_closes_unassigned_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    events: list[str] = []

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def terminate(self) -> None:
            events.append("terminate")
            raise ValueError("terminate cleanup failed")

        def close(self) -> None:
            events.append("close")
            raise ValueError("close cleanup failed")

    def aborting_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[object]:
        raise LaunchAbort

    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor.subprocess, "Popen", aborting_popen)

    with pytest.raises(LaunchAbort):
        ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn([sys.executable, "-c", "pass"])

    assert events == ["open", "terminate", "close"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_async_launch_baseexception_closes_unassigned_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    events: list[str] = []

    class Job:
        def __init__(self, _name: str) -> None:
            events.append("open")

        def terminate(self) -> None:
            events.append("terminate")

        def close(self) -> None:
            events.append("close")

    async def aborting_create(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        raise LaunchAbort

    monkeypatch.setattr(process_supervisor, "_WindowsJob", Job)
    monkeypatch.setattr(process_supervisor.asyncio, "create_subprocess_exec", aborting_create)

    async def exercise() -> None:
        with pytest.raises(LaunchAbort):
            await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
                [sys.executable, "-c", "pass"]
            )

    asyncio.run(exercise())
    assert events == ["open", "terminate", "close"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_sync_spawn_baseexception_closes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    launched_pid = 0
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[object]:
        nonlocal launched_pid
        process = real_popen(*args, **kwargs)
        launched_pid = process.pid
        return process

    monkeypatch.setattr(process_supervisor.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process_supervisor, "_resume_windows_process", lambda _handle: (_ for _ in ()).throw(LaunchAbort()))
    with pytest.raises(LaunchAbort):
        ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
    deadline = time.monotonic() + 10
    while inspect_process(launched_pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launched_pid > 0
    assert inspect_process(launched_pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows BaseException cleanup integration")
def test_windows_async_spawn_baseexception_closes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LaunchAbort(BaseException):
        pass

    launched_pid = 0
    real_create = asyncio.create_subprocess_exec

    async def recording_create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal launched_pid
        process = await real_create(*args, **kwargs)
        launched_pid = process.pid
        return process

    monkeypatch.setattr(process_supervisor.asyncio, "create_subprocess_exec", recording_create)
    monkeypatch.setattr(process_supervisor, "_resume_windows_process", lambda _handle: (_ for _ in ()).throw(LaunchAbort()))

    async def exercise() -> None:
        with pytest.raises(LaunchAbort):
            await ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn_async(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )

    asyncio.run(exercise())
    deadline = time.monotonic() + 10
    while inspect_process(launched_pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launched_pid > 0
    assert inspect_process(launched_pid) is None


def _spawn_windows_durable_payload(
    tmp_path: Path,
    name: str,
) -> tuple[process_supervisor.ManagedProcess, ProcessIdentity]:
    marker = tmp_path / f"{name}.pid"
    code = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    managed = ProcessSupervisor(LifetimeMode.DETACHED, supervision_id=name).spawn(
        [sys.executable, "-c", code, str(marker)]
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    identity = inspect_process(int(marker.read_text(encoding="utf-8")))
    assert identity is not None
    return managed, identity


@pytest.mark.skipif(os.name != "nt", reason="native Windows payload promotion integration")
def test_windows_promotes_real_payload_with_authenticated_job_membership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    managed, candidate = _spawn_windows_durable_payload(tmp_path, f"promote-{uuid.uuid4().hex}")
    promoted = managed.token
    try:
        promoted = promote_payload_identity(managed.token, candidate)
        assert promoted.payload == candidate
        control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / promoted.control_relpath
        control = json.loads(control_path.read_text(encoding="utf-8"))
        metadata = SupervisionToken.from_dict(
            json.loads(process_supervisor.durable_metadata_path(promoted.token).read_text(encoding="utf-8"))
        )
        assert control["payload_identity"] == candidate.to_dict()
        assert metadata.payload == candidate
    finally:
        terminate(promoted, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows cross-Job rejection integration")
def test_windows_rejects_payload_promotion_from_another_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    first, _first_candidate = _spawn_windows_durable_payload(tmp_path, f"first-{uuid.uuid4().hex}")
    second, foreign_candidate = _spawn_windows_durable_payload(tmp_path, f"second-{uuid.uuid4().hex}")
    try:
        with pytest.raises(ValueError, match="not an active member"):
            promote_payload_identity(first.token, foreign_candidate)
        metadata = SupervisionToken.from_dict(
            json.loads(process_supervisor.durable_metadata_path(first.token.token).read_text(encoding="utf-8"))
        )
        assert metadata.payload == first.token.payload
    finally:
        terminate(first.token, grace_seconds=0.1)
        terminate(second.token, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows persisted-token rejection integration")
def test_windows_persisted_termination_rejects_cross_job_control_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "controls"))
    first, first_candidate = _spawn_windows_durable_payload(tmp_path, f"terminate-a-{uuid.uuid4().hex}")
    second, second_candidate = _spawn_windows_durable_payload(tmp_path, f"terminate-b-{uuid.uuid4().hex}")
    first_token = promote_payload_identity(first.token, first_candidate)
    second_token = promote_payload_identity(second.token, second_candidate)
    control_path = Path(os.environ["SPEC_PROCESS_CONTROL_ROOT"]) / first_token.control_relpath
    lock_path = control_path.with_suffix(".lock")
    try:
        with process_supervisor.FileLock(lock_path):
            state = json.loads(control_path.read_text(encoding="utf-8"))
            state["payload_identity"] = second_candidate.to_dict()
            process_supervisor.atomic_write_text(control_path, json.dumps(state, sort_keys=True))
        assert terminate(first_token, grace_seconds=0) is False
        assert identity_matches(first_token.identity)
        assert identity_matches(second_token.identity)
    finally:
        with process_supervisor.FileLock(lock_path):
            state = json.loads(control_path.read_text(encoding="utf-8"))
            state["payload_identity"] = first_candidate.to_dict()
            process_supervisor.atomic_write_text(control_path, json.dumps(state, sort_keys=True))
        terminate(first_token, grace_seconds=0.1)
        terminate(second_token, grace_seconds=0.1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows durable helper integration")
def test_windows_adoptable_helper_survives_launcher_and_stops(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    token_file = tmp_path / "adoptable.json"
    launcher = tmp_path / "adoptable-launcher.py"
    launcher.write_text(
        "import json,sys\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "payload=[sys.executable,'-c',"
        "'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"alive\"); time.sleep(30)',sys.argv[2]]\n"
        "managed=ProcessSupervisor(LifetimeMode.ADOPTABLE).spawn(payload)\n"
        "open(sys.argv[1],'w').write(json.dumps(managed.token.to_dict()))\n",
        encoding="utf-8",
    )
    __import__("subprocess").run(
        [sys.executable, str(launcher), str(token_file), str(marker)],
        check=True,
        timeout=10,
    )
    token = SupervisionToken.from_dict(json.loads(token_file.read_text(encoding="utf-8")))
    while not marker.exists():
        time.sleep(0.05)
    assert identity_matches(token.identity)
    adopted = adopt(token)
    with pytest.raises(ValueError, match="already adopted"):
        adopt(token)
    assert terminate(adopted, grace_seconds=0.1)
    deadline = time.monotonic() + 10
    while identity_matches(adopted.identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not identity_matches(adopted.identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows detached launcher integration")
@pytest.mark.parametrize("workflow", ["update-refresh", "background-web-service"])
def test_windows_detached_workflows_survive_launcher_and_stop_by_identity(tmp_path: Path, workflow: str) -> None:
    """Model both short-lived call sites with a real launcher and payload."""
    token_file = tmp_path / f"{workflow}.json"
    marker = tmp_path / f"{workflow}.alive"
    launcher = tmp_path / f"launch-{workflow}.py"
    launcher.write_text(
        "import json,sys\n"
        "from spec_runtime.process_supervisor import LifetimeMode,ProcessSupervisor\n"
        "payload=[sys.executable,'-c',"
        "'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"alive\"); time.sleep(30)',sys.argv[2]]\n"
        "managed=ProcessSupervisor(LifetimeMode.DETACHED).spawn(payload)\n"
        "open(sys.argv[1],'w').write(json.dumps(managed.token.to_dict()))\n",
        encoding="utf-8",
    )
    launcher_process = __import__("subprocess").run(
        [sys.executable, str(launcher), str(token_file), str(marker)],
        check=True,
        timeout=10,
    )
    assert launcher_process.returncode == 0
    token = SupervisionToken.from_dict(json.loads(token_file.read_text(encoding="utf-8")))
    while not marker.exists():
        time.sleep(0.05)
    assert identity_matches(token.identity)
    assert terminate(token, grace_seconds=0.1)
    deadline = time.monotonic() + 10
    while identity_matches(token.identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not identity_matches(token.identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows background web integration")
def test_windows_background_web_server_survives_launcher_and_stops_by_token(tmp_path: Path) -> None:
    """Exercise the real web start/status/stop lifecycle, including its persisted token."""
    pytest.importorskip("uvicorn")
    config_path = tmp_path / ".spec.toml"
    config_path.write_text('base_ref = "origin/main"\n', encoding="utf-8")
    launcher_env = os.environ.copy()
    launcher_env["SPEC_CONFIG"] = str(config_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    launch = (
        "import sys; from pathlib import Path; "
        "from spec_runtime.web.server import run_server; "
        "raise SystemExit(run_server(Path(sys.argv[1]),port=int(sys.argv[2]),background=True))"
    )
    subprocess.run(
        [sys.executable, "-c", launch, str(tmp_path), str(port)],
        check=True,
        timeout=20,
        env=launcher_env,
    )
    token_path = tmp_path / ".spec-state" / "web" / "server.supervision.json"
    token = SupervisionToken.from_dict(json.loads(token_path.read_text(encoding="utf-8")))
    try:
        assert identity_matches(token.identity)
        from spec_runtime.web.server import is_server_running, stop_server

        assert is_server_running(tmp_path) == (True, token.payload.pid)
        assert stop_server(tmp_path) == 0
        deadline = time.monotonic() + 10
        while identity_matches(token.identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not identity_matches(token.identity)
        assert not token_path.exists()
    finally:
        if identity_matches(token.identity):
            terminate(token, grace_seconds=0.1)
