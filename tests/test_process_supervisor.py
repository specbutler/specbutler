from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    ProcessSupervisor,
    SupervisionToken,
    identity_matches,
    inspect_process,
    terminate,
)


def test_token_round_trip_preserves_reopenable_identity() -> None:
    identity = ProcessIdentity(42, "created", "python.exe", "python child.py")
    token = SupervisionToken(LifetimeMode.ADOPTABLE, identity, 7, "owner", "token", 9)
    assert SupervisionToken.from_dict(token.to_dict()) == token


def test_identity_rejects_stale_creation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ProcessIdentity(os.getpid(), "old", sys.executable)
    monkeypatch.setattr(
        "spec_runtime.process_supervisor.inspect_process",
        lambda pid: ProcessIdentity(pid, "new", sys.executable),
    )
    assert identity_matches(expected) is False


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object integration")
@pytest.mark.parametrize("action", ["normal", "timeout", "stop", "owner-close"])
def test_windows_run_owned_parent_child_grandchild_tree(tmp_path: Path, action: str) -> None:
    """Exercise real descendants; no process API is mocked in this test."""
    pid_file = tmp_path / "pids.json"
    script = tmp_path / "tree.py"
    script.write_text(
        "import json,os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); path=sys.argv[2]\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),path])\n"
        "if level == 0:\n"
        " time.sleep(.5); json.dump([os.getpid(),child.pid],open(path,'w'))\n"
        "time.sleep(30 if level else 2)\n",
        encoding="utf-8",
    )
    supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
    managed = supervisor.spawn([sys.executable, str(script), "0", str(pid_file)])
    if action == "normal":
        managed.wait(timeout=10)
    else:
        while not pid_file.exists():
            time.sleep(0.05)
        if action == "owner-close":
            supervisor.close()
        else:
            managed.terminate(grace_seconds=0.1)
        managed.wait(timeout=10)
    if action != "normal":
        for pid in json.loads(pid_file.read_text(encoding="utf-8")):
            assert inspect_process(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows durable helper integration")
def test_windows_adoptable_helper_survives_launcher_and_stops(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    managed = ProcessSupervisor(LifetimeMode.ADOPTABLE).spawn(
        [sys.executable, "-c", f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('ok'); time.sleep(30)"]
    )
    while not marker.exists():
        time.sleep(0.05)
    assert identity_matches(managed.token.identity)
    assert terminate(managed.token, grace_seconds=0.1)
    managed.wait(timeout=10)
