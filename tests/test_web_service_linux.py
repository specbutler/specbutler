"""Real Linux web-service lifecycle coverage."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
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

def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, dict[str, str]]:
    config_path = tmp_path / ".spec.toml"
    config_path.write_text('base_ref = "HEAD"\n', encoding="utf-8")
    control_root = tmp_path / "process-controls"
    user_state_root = tmp_path / "user-state"
    monkeypatch.setenv("SPEC_CONFIG", str(config_path))
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(control_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(user_state_root))
    from spec_runtime.web.auth import load_or_create_token

    token = load_or_create_token(tmp_path)
    env = os.environ.copy()
    env.pop("SPEC_WEB_READY_NONCE", None)
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


def test_linux_chrome_bootstrap_keeps_credentials_out_of_url_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome/Chromium is not installed")

    repo, token, env = _repo(tmp_path, monkeypatch)
    port = _free_port()
    stdout_path = tmp_path / "chrome-server.stdout.log"
    stderr_path = tmp_path / "chrome-server.stderr.log"
    profile = tmp_path / "chrome-profile"
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
            deadline = time.monotonic() + 5
            opening_url = ""
            while time.monotonic() < deadline:
                match = re.search(
                    r"One-time browser URL: (\S+)",
                    stderr_path.read_text(errors="replace"),
                )
                if match:
                    opening_url = match.group(1)
                    break
                time.sleep(0.05)
            assert opening_url
            assert token not in opening_url
            assert "bootstrap=" in opening_url

            browser = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    opening_url,
                ],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            assert browser.returncode == 0, browser.stderr[-1000:]
            assert 'class="summary-row"' in browser.stdout

            history_path = profile / "Default" / "History"
            assert history_path.is_file()
            database = sqlite3.connect(f"file:{history_path}?mode=ro", uri=True)
            try:
                urls = [row[0] for row in database.execute("select url from urls")]
            finally:
                database.close()
            assert urls
            assert all(token not in url for url in urls)
            assert all("spec-csrf" not in url for url in urls)

            with pytest.raises(urllib.error.HTTPError) as replay:
                urllib.request.urlopen(opening_url, timeout=2)
            assert replay.value.code == 401
        except Exception as exc:
            raise AssertionError(f"{exc}\n{_logs(stdout_path, stderr_path)}") from exc
        finally:
            if managed.poll() is None:
                managed.terminate(grace_seconds=0.1)


def test_linux_raw_foreground_launch_claims_group_and_stops_out_of_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, token, env = _repo(tmp_path, monkeypatch)
    port = _free_port()
    stdout_path = tmp_path / "raw-foreground.stdout.log"
    stderr_path = tmp_path / "raw-foreground.stderr.log"
    supervision: SupervisionToken | None = None
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        # Deliberately omit start_new_session: this reproduces a direct caller
        # whose child initially shares the caller's process group.
        process = subprocess.Popen(  # noqa: S603
            _start_command(port),
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            _wait_for_authenticated_http(port, token, process=process)
            supervision = SupervisionToken.from_dict(
                json.loads(
                    (repo / ".spec-state" / "web" / "server.supervision.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            assert supervision.mode is LifetimeMode.RUN_OWNED
            assert supervision.identity.pid == process.pid
            assert supervision.pgid == process.pid

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
            process.wait(timeout=10)
            _assert_port_closed(port)
            assert not (repo / ".spec-state" / "web" / "server.pid").exists()
            assert not (
                repo / ".spec-state" / "web" / "server.supervision.json"
            ).exists()
        except Exception as exc:
            raise AssertionError(f"{exc}\n{_logs(stdout_path, stderr_path)}") from exc
        finally:
            if supervision is not None and identity_matches(supervision.identity):
                terminate(supervision, grace_seconds=0.1)
            elif process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


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
