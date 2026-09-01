"""Real Linux web-service lifecycle coverage."""

from __future__ import annotations

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

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessSupervisor,
    SupervisionToken,
    durable_metadata_path,
    identity_matches,
    terminate,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="real Linux web service integration",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, dict[str, str]]:
    token = "linux-web-service-token"
    (tmp_path / ".spec-state" / "web").mkdir(parents=True)
    (tmp_path / ".spec-state" / "web" / "auth-token").write_text(
        token,
        encoding="utf-8",
    )
    config_path = tmp_path / ".spec.toml"
    config_path.write_text('base_ref = "HEAD"\n', encoding="utf-8")
    control_root = tmp_path / "process-controls"
    monkeypatch.setenv("SPEC_CONFIG", str(config_path))
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(control_root))
    env = os.environ.copy()
    env.pop("SPEC_WEB_READY_NONCE", None)
    # The service starts from a temporary repository, outside pytest's import
    # path.  Point that child at this checkout explicitly: developers commonly
    # run the suite from a worktree while their shared virtualenv is editable-
    # installed from a different checkout.  Without this boundary the test can
    # exercise stale installed code and, on assertion failure, leak its daemon.
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(REPO_ROOT / "src"), env.get("PYTHONPATH", ""))
        if part
    )
    env["SPEC_NO_UPDATE_CHECK"] = "1"
    return tmp_path, token, env


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_command(port: int, *, background: bool = False) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "spec_runtime.cli",
        "web",
        "start",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if background:
        command.append("--background")
    return command


def _cli_command(action: str) -> list[str]:
    return [sys.executable, "-m", "spec_runtime.cli", "web", action]


def _logs(stdout_path: Path, stderr_path: Path) -> str:
    return stdout_path.read_text(errors="replace") + stderr_path.read_text(
        errors="replace"
    )


def _wait_for_authenticated_http(
    port: int,
    token: str,
    *,
    deadline_seconds: float = 15.0,
    process: object | None = None,
) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                assert response.status == 200
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                break
            time.sleep(0.05)
    raise AssertionError(f"web service did not become ready: {last_error}")


def _assert_wrong_token_rejected(port: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        headers={"Authorization": "Bearer wrong-token"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=2)
    assert error.value.code == 401


def _assert_port_closed(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            return
        time.sleep(0.05)
    raise AssertionError(f"web service still accepts connections on port {port}")


def test_linux_foreground_bind_auth_status_and_legacy_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, token, env = _repo(tmp_path, monkeypatch)
    port = _free_port()
    stdout_path = tmp_path / "foreground.stdout.log"
    stderr_path = tmp_path / "foreground.stderr.log"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
            _start_command(port),
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            _wait_for_authenticated_http(port, token, process=managed)
            _assert_wrong_token_rejected(port)
            status = subprocess.run(
                _cli_command("status"),
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert status.returncode == 0
            assert f"port {port}" in status.stdout

            stopped = subprocess.run(
                _cli_command("stop"),
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr
            managed.wait(timeout=10)
            _assert_port_closed(port)
            assert not (repo / ".spec-state" / "web" / "server.pid").exists()
        except Exception as exc:
            raise AssertionError(f"{exc}\n{_logs(stdout_path, stderr_path)}") from exc
        finally:
            if managed.poll() is None:
                managed.terminate(grace_seconds=0.1)


def test_linux_background_durable_start_auth_status_stop_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, token, env = _repo(tmp_path, monkeypatch)
    port = _free_port()
    started = subprocess.run(
        _start_command(port, background=True),
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert started.returncode == 0, started.stdout + started.stderr
    state_dir = repo / ".spec-state" / "web"
    token_path = state_dir / "server.supervision.json"
    supervision: SupervisionToken | None = None
    try:
        supervision = SupervisionToken.from_dict(
            json.loads(token_path.read_text(encoding="utf-8"))
        )
        assert supervision.mode is LifetimeMode.DETACHED
        assert identity_matches(supervision.identity)
        _wait_for_authenticated_http(port, token)
        _assert_wrong_token_rejected(port)

        status = subprocess.run(
            _cli_command("status"),
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert status.returncode == 0
        assert f"port {port}" in status.stdout

        stopped = subprocess.run(
            _cli_command("stop"),
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        deadline = time.monotonic() + 10
        while identity_matches(supervision.identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not identity_matches(supervision.identity)
        _assert_port_closed(port)
        for name in (
            "server.pid",
            "server.port",
            "server.supervision.json",
            "server.launch.json",
            "server.ready.json",
        ):
            assert not (state_dir / name).exists()
        assert not durable_metadata_path(supervision.token).exists()
    finally:
        if supervision is not None and identity_matches(supervision.identity):
            terminate(supervision, grace_seconds=0.1)
        elif supervision is None:
            # Keep a failed ownership-publication assertion from leaking the
            # test daemon.  The product stop path still requires a matching
            # launch token or PID/start-time identity before it signals.
            subprocess.run(
                _cli_command("stop"),
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
