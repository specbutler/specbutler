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

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    ProcessSupervisor,
    SupervisionToken,
    adopt,
    identity_matches,
    inspect_process,
    terminate,
)


def test_token_round_trip_preserves_reopenable_identity() -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "token", 9)
    assert SupervisionToken.from_dict(token.to_dict()) == token
    assert token.version == 2
    assert token.control_relpath.endswith("/control.json")
    assert token.control_nonce


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
        job_name=r"Local\SpecButler-payload-job",
    )
    restored = SupervisionToken.from_dict(token.to_dict())
    assert restored.identity == keeper
    assert restored.payload == payload
    assert restored.job_name == token.job_name


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object integration")
@pytest.mark.parametrize("action", ["normal", "timeout", "stop", "owner-close"])
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

        assert is_server_running(tmp_path) == (True, token.identity.pid)
        assert stop_server(tmp_path) == 0
        deadline = time.monotonic() + 10
        while identity_matches(token.identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not identity_matches(token.identity)
        assert not token_path.exists()
    finally:
        if identity_matches(token.identity):
            terminate(token, grace_seconds=0.1)
