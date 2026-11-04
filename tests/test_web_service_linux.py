"""Real Linux web-service lifecycle coverage."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ManagedProcess,
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


def _assert_unauthorized(
    request: urllib.request.Request | str, *, deadline_seconds: float = 15.0,
) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            with urllib.request.urlopen(request, timeout=min(1.0, remaining)) as response:
                raise AssertionError(f"Expected HTTP 401, received {response.status}")
        except urllib.error.HTTPError as exc:
            with exc:
                assert exc.code == 401
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise AssertionError(f"HTTP 401 was not observed before the deadline: {last_error}")


def _assert_wrong_token_rejected(port: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        headers={"Authorization": "Bearer wrong-token"},
    )
    _assert_unauthorized(request)


def _close_chrome_target(
    browser: ManagedProcess, debug_port: int, target_id: str, *, deadline_seconds: float = 10.0,
) -> None:
    # Allow five seconds for the close request and five for confirmation.
    deadline = time.monotonic() + deadline_seconds
    close_request = urllib.request.Request(
        f"http://127.0.0.1:{debug_port}/json/close/{target_id}", method="PUT",
    )
    acknowledged = False
    last_error: Exception | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        if browser.poll() is not None:
            return
        if not acknowledged:
            try:
                with urllib.request.urlopen(close_request, timeout=min(5.0, remaining)) as response:
                    assert response.read().decode() == "Target is closing"
                acknowledged = True
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json/list", timeout=min(5.0, remaining),
            ) as response:
                targets = json.load(response)
            # A timed-out close may already have succeeded in Chrome. Verify
            # the target disappeared rather than depending on its HTTP reply.
            if all(target.get("id") != target_id for target in targets):
                return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise AssertionError(f"Chrome did not close the audited page before the deadline: {last_error}")


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


@pytest.mark.requires_network
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


@pytest.mark.requires_network
@pytest.mark.requires_browser
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
    browser_stdout_path = tmp_path / "chrome.stdout.log"
    browser_stderr_path = tmp_path / "chrome.stderr.log"
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

            with (
                browser_stdout_path.open("w", encoding="utf-8") as browser_stdout,
                browser_stderr_path.open("w", encoding="utf-8") as browser_stderr,
            ):
                browser = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
                    [
                        chrome,
                        "--headless=new",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--disable-background-networking",
                        "--no-first-run",
                        "--no-default-browser-check",
                        f"--user-data-dir={profile}",
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=0",
                        opening_url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=browser_stdout,
                    stderr=browser_stderr,
                )
                try:
                    # A cold Chrome on a shared runner may publish its port
                    # before DevTools responds. Allow startup time, but stay
                    # within the one-time browser URL's 60-second lifetime.
                    deadline = time.monotonic() + 45
                    targets: list[dict[str, object]] = []
                    page_target: dict[str, object] | None = None
                    last_browser_error: Exception | None = None
                    debug_port: int | None = None
                    while time.monotonic() < deadline:
                        try:
                            if debug_port is None:
                                active_port = profile / "DevToolsActivePort"
                                debug_port = int(
                                    active_port.read_text(encoding="utf-8")
                                    .splitlines()[0]
                                    .strip()
                                )
                            with urllib.request.urlopen(
                                f"http://127.0.0.1:{debug_port}/json/list",
                                timeout=5,
                            ) as response:
                                targets = json.load(response)
                            page_target = next(
                                (
                                    target
                                    for target in targets
                                    if target.get("type") == "page"
                                    and target.get("title") == "spec web"
                                    and str(target.get("url", "")).startswith(
                                        f"http://127.0.0.1:{port}/"
                                    )
                                    and "bootstrap="
                                    not in str(target.get("url", ""))
                                ),
                                None,
                            )
                            if page_target is not None:
                                break
                        except (
                            IndexError,
                            OSError,
                            ValueError,
                            urllib.error.URLError,
                        ) as exc:
                            last_browser_error = exc
                        if browser.poll() is not None:
                            break
                        time.sleep(0.05)
                    assert page_target is not None, (
                        "Chrome did not load the authenticated clean URL: "
                        f"debug_port={debug_port}, error={last_browser_error}, targets={targets}\n"
                        f"{browser_stderr_path.read_text(errors='replace')[-2000:]}"
                    )
                    current_url = str(page_target["url"])
                    assert token not in current_url
                    assert "bootstrap=" not in current_url
                    assert "spec-csrf" not in current_url
                    target_id = str(page_target["id"])
                    # Closing the target commits the navigation to history;
                    # transient DevTools I/O must not abort that observation.
                    _close_chrome_target(browser, debug_port, target_id)
                finally:
                    if browser.owned_tree_active():
                        browser.send_signal(signal.SIGINT)
                    try:
                        browser.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        assert browser.terminate(grace_seconds=1)
                        browser.wait(timeout=5)

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

            _assert_unauthorized(opening_url)
        except Exception as exc:
            raise AssertionError(f"{exc}\n{_logs(stdout_path, stderr_path)}") from exc
        finally:
            if managed.poll() is None:
                managed.terminate(grace_seconds=0.1)


@pytest.mark.requires_network
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


@pytest.mark.requires_network
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


class _HttpResponse(io.BytesIO):
    status = 200

    def __init__(self, data: bytes, *, timeout_read: bool = False):
        super().__init__(data)
        self.timeout_read = timeout_read

    def read(self, size: int = -1) -> bytes:
        if self.timeout_read:
            raise TimeoutError("response body timed out")
        return super().read(size)


@pytest.fixture
def http_poll_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    # Patch only this module, leaving the supervisor and subprocess clocks real.
    monkeypatch.setitem(globals(), "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


@pytest.mark.parametrize("stage", ["close_open", "close_read", "poll_open", "poll_read"])
def test_chrome_close_recovers_from_transient_io(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float], stage: str,
) -> None:
    calls = {"close": 0, "poll": 0}

    def open_response(request, *, timeout):
        action = "close" if isinstance(request, urllib.request.Request) else "poll"
        calls[action] += 1
        if stage == f"{action}_open" and calls[action] == 1:
            raise TimeoutError("DevTools request timed out")
        return _HttpResponse(
            b"Target is closing" if action == "close" else b"[]",
            timeout_read=stage == f"{action}_read" and calls[action] == 1,
        )

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    _close_chrome_target(SimpleNamespace(poll=lambda: None), 1234, "audited")
    assert calls["close"] == 1
    assert calls["poll"] == (2 if stage.startswith("poll") else 1)


def test_chrome_close_retries_when_timed_out_request_did_not_close_target(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float],
) -> None:
    closes = []

    def open_response(request, *, timeout):
        if isinstance(request, urllib.request.Request):
            closes.append(request)
            if len(closes) == 1:
                raise TimeoutError("close was not accepted")
            return _HttpResponse(b"Target is closing")
        return _HttpResponse(b'[{"id":"audited"}]' if len(closes) == 1 else b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    _close_chrome_target(SimpleNamespace(poll=lambda: None), 1234, "audited")
    assert len(closes) == 2


@pytest.mark.parametrize("unreachable", [False, True])
def test_chrome_close_requires_target_disappearance_before_deadline(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float], unreachable: bool,
) -> None:
    timeouts = []

    def open_response(request, *, timeout):
        timeouts.append(timeout)
        if unreachable:
            raise TimeoutError("DevTools unavailable")
        return _HttpResponse(b"Target is closing" if isinstance(request, urllib.request.Request) else b'[{"id":"audited"}]')

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    with pytest.raises(AssertionError, match="Chrome did not close the audited page before the deadline"):
        _close_chrome_target(SimpleNamespace(poll=lambda: None), 1234, "audited", deadline_seconds=0.15)
    assert http_poll_clock[0] == pytest.approx(0.15)
    assert timeouts and all(0 < timeout <= 0.15 for timeout in timeouts)


def test_unauthorized_check_retries_timeout_but_requires_401(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float],
) -> None:
    calls = []

    def open_response(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("replay request timed out")
        raise urllib.error.HTTPError(request, 401, "Unauthorized", None, io.BytesIO())

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    _assert_unauthorized("http://127.0.0.1:1234/?bootstrap=used")
    assert len(calls) == 2


@pytest.mark.parametrize("status", [200, 403])
def test_unauthorized_check_rejects_unexpected_status_immediately(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float], status: int,
) -> None:
    def open_response(request, *, timeout):
        if status == 200:
            return _HttpResponse(b"unexpected authentication success")
        raise urllib.error.HTTPError(request, status, "Forbidden", None, io.BytesIO())

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    with pytest.raises(AssertionError):
        _assert_unauthorized("http://127.0.0.1:1234/?bootstrap=used")
    assert http_poll_clock[0] == 0


def test_unauthorized_check_does_not_accept_a_server_that_stays_unreachable(
    monkeypatch: pytest.MonkeyPatch, http_poll_clock: list[float],
) -> None:
    def open_response(request, *, timeout):
        raise TimeoutError("server unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    with pytest.raises(AssertionError, match="HTTP 401 was not observed before the deadline: server unavailable"):
        _assert_unauthorized("http://127.0.0.1:1234/?bootstrap=used", deadline_seconds=0.15)
    assert http_poll_clock[0] == pytest.approx(0.15)
