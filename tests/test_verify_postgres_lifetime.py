from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from spec_runtime import orchestrator as orch


@pytest.mark.skipif(os.name != "posix", reason="POSIX daemon handoff")
@pytest.mark.parametrize("failure", [None, "gate", "start"])
def test_verify_database_daemon_lives_until_gate_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    worktree = tmp_path / "worktree"
    scripts = worktree / "scripts"
    scripts.mkdir(parents=True)
    port_file = worktree / "port"
    pid_file = worktree / "pid"
    daemon = (
        "import os,socket,time; from pathlib import Path; "
        "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"Path({str(port_file)!r}).write_text(str(s.getsockname()[1])); "
        "time.sleep(60)"
    )
    script = scripts / "local_postgres.sh"
    script.write_text(f"""#!{sys.executable}
import os, signal, subprocess, sys, time
from pathlib import Path
port_file = Path({str(port_file)!r})
pid_file = Path({str(pid_file)!r})
action = sys.argv[1]
if action == 'status':
    print('Local Postgres is not running.')
elif action == 'start':
    subprocess.Popen([sys.executable, '-c', {daemon!r}],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    for _ in range(200):
        if port_file.exists():
            break
        time.sleep(0.01)
    else:
        raise SystemExit('daemon did not start')
    print('Started local Postgres')
    raise SystemExit({1 if failure == 'start' else 0})
elif action == 'url':
    print('export SIM_TEST_DATABASE_URL=postgresql://localhost:' + port_file.read_text() + '/test')
elif action == 'stop':
    try:
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
    except ProcessLookupError:
        pass
""")
    script.chmod(0o755)
    monkeypatch.delenv("SIM_DATABASE_URL", raising=False)
    monkeypatch.delenv("SIM_TEST_DATABASE_URL", raising=False)
    # Reproduce the actual orchestrated path, including RUN_OWNED supervision.
    monkeypatch.setattr(orch, "_ACTIVE_PHASE_LEASE_FAILURE", orch.LeaseHeartbeatFailure())

    def exercise() -> None:
        with orch._with_verify_test_environment(worktree) as env:
            assert port_file.read_text() in env["SIM_TEST_DATABASE_URL"]
            with socket.create_connection(("127.0.0.1", int(port_file.read_text())), timeout=1):
                pass
            if failure == "gate":
                raise RuntimeError("gate failed")

    try:
        if failure:
            with pytest.raises(RuntimeError, match="gate failed|start failed"):
                exercise()
        else:
            exercise()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", int(port_file.read_text())), timeout=1)
    finally:
        # Ensure a failed regression cannot leave its synthetic service alive.
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
