"""Tests for the spec web server — auth, API, CLI dispatch, and server lifecycle."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Guard: skip the entire module when the optional [web] extras are not installed.
pytest.importorskip("starlette", reason="requires specbutler[web] extras")

# ---------------------------------------------------------------------------
# Auth module tests
# ---------------------------------------------------------------------------


class TestAuth:
    """Token generation, storage, and request extraction."""

    def test_generate_token_is_url_safe(self):
        from spec_runtime.web.auth import generate_token

        token = generate_token()
        assert len(token) > 20
        import re

        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)

    def test_generate_token_is_unique(self):
        from spec_runtime.web.auth import generate_token

        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50

    def test_load_or_create_token_creates_file(self, tmp_path):
        with patch("spec_runtime.web.auth._web_state_dir", return_value=tmp_path / "web"):
            with patch("spec_runtime.web.auth._token_path", return_value=tmp_path / "web" / "auth-token"):
                from spec_runtime.web.auth import load_or_create_token

                token = load_or_create_token(tmp_path)
                assert len(token) > 20
                assert (tmp_path / "web" / "auth-token").exists()

    def test_load_or_create_token_reuses_existing(self, tmp_path):
        token_path = tmp_path / "web" / "auth-token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("existing-token-value")

        with patch("spec_runtime.web.auth._token_path", return_value=token_path):
            from spec_runtime.web.auth import load_or_create_token

            token = load_or_create_token(tmp_path)
            assert token == "existing-token-value"

    def test_reset_token_generates_new(self, tmp_path):
        token_path = tmp_path / "web" / "auth-token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("old-token")

        with patch("spec_runtime.web.auth._token_path", return_value=token_path):
            from spec_runtime.web.auth import reset_token

            new_token = reset_token(tmp_path)
            assert new_token != "old-token"
            assert token_path.read_text() == new_token

    def test_read_token_returns_none_when_missing(self, tmp_path):
        with patch("spec_runtime.web.auth._token_path", return_value=tmp_path / "nonexistent"):
            from spec_runtime.web.auth import read_token

            assert read_token(tmp_path) is None

    def test_extract_token_from_query_string(self):
        from spec_runtime.web.auth import extract_token_from_request

        scope = {"query_string": b"token=abc123"}
        token = extract_token_from_request(scope, {}, {})
        assert token == "abc123"

    def test_extract_token_from_bearer_header(self):
        from spec_runtime.web.auth import extract_token_from_request

        scope = {"query_string": b""}
        headers = {"authorization": "Bearer mytoken"}
        token = extract_token_from_request(scope, headers, {})
        assert token == "mytoken"

    def test_extract_token_from_cookie(self):
        from spec_runtime.web.auth import COOKIE_NAME_PREFIX, extract_token_from_request

        scope = {"query_string": b""}
        cookies = {COOKIE_NAME_PREFIX: "cookie-token"}
        token = extract_token_from_request(scope, {}, cookies)
        assert token == "cookie-token"

    def test_extract_token_priority_query_over_cookie(self):
        from spec_runtime.web.auth import COOKIE_NAME_PREFIX, extract_token_from_request

        scope = {"query_string": b"token=qs-token"}
        cookies = {COOKIE_NAME_PREFIX: "cookie-token"}
        token = extract_token_from_request(scope, {}, cookies)
        assert token == "qs-token"

    def test_extract_token_uses_per_instance_cookie_name(self):
        from spec_runtime.web.auth import cookie_name_for_port, extract_token_from_request

        scope = {"query_string": b""}
        name = cookie_name_for_port(7701)
        cookies = {name: "instance-token"}
        token = extract_token_from_request(scope, {}, cookies, name)
        assert token == "instance-token"

    def test_extract_token_ignores_other_instance_cookie(self):
        from spec_runtime.web.auth import cookie_name_for_port, extract_token_from_request

        scope = {"query_string": b""}
        # Cookie set by the 7700 instance must not satisfy a 7701 middleware.
        cookies = {cookie_name_for_port(7700): "other-token"}
        token = extract_token_from_request(
            scope, {}, cookies, cookie_name_for_port(7701)
        )
        assert token is None

    def test_cookie_name_for_port(self):
        from spec_runtime.web.auth import COOKIE_NAME_PREFIX, cookie_name_for_port

        assert cookie_name_for_port(7700) == f"{COOKIE_NAME_PREFIX}_7700"
        assert cookie_name_for_port(None) == COOKIE_NAME_PREFIX
        # Two ports must produce two distinct names.
        assert cookie_name_for_port(7700) != cookie_name_for_port(7701)

    def test_parse_cookies(self):
        from spec_runtime.web.auth import parse_cookies

        result = parse_cookies("foo=bar; baz=qux; spec_session=tok123")
        assert result == {"foo": "bar", "baz": "qux", "spec_session": "tok123"}


# ---------------------------------------------------------------------------
# Server lifecycle tests
# ---------------------------------------------------------------------------


class TestServerLifecycle:
    """PID file management and server state."""

    def test_write_and_read_pid(self, tmp_path):
        with patch("spec_runtime.web.server._web_state_dir", return_value=tmp_path / "web"):
            with patch("spec_runtime.web.server._pid_path", return_value=tmp_path / "web" / "server.pid"):
                from spec_runtime.web.server import read_pid, write_pid

                write_pid(tmp_path)
                pid, started_at = read_pid(tmp_path)
                assert pid == os.getpid()
                # started_at may be empty if ps is unavailable (e.g. sandbox)
                assert isinstance(started_at, str)

    def test_read_pid_returns_none_when_missing(self, tmp_path):
        with patch("spec_runtime.web.server._pid_path", return_value=tmp_path / "nonexistent"):
            from spec_runtime.web.server import read_pid

            pid, started_at = read_pid(tmp_path)
            assert pid is None

    def test_remove_pid(self, tmp_path):
        pid_path = tmp_path / "web" / "server.pid"
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("12345")

        with patch("spec_runtime.web.server._pid_path", return_value=pid_path):
            from spec_runtime.web.server import remove_pid

            remove_pid(tmp_path)
            assert not pid_path.exists()

    def test_windows_background_persists_token_and_cleans_failed_start(self, tmp_path, capsys):
        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessIdentity,
            ProcessSupervisor,
            SupervisionToken,
        )

        token = SupervisionToken(
            LifetimeMode.DETACHED, ProcessIdentity(123, "created"), 1, "owner", "web-token"
        )
        managed = MagicMock(token=token)
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="auth"),
            patch("socket.create_connection", side_effect=OSError("free")),
            patch.object(ProcessSupervisor, "spawn", return_value=managed),
            patch("spec_runtime.web.server.write_supervision_token") as write_token,
            patch("spec_runtime.web.server._wait_for_ready_record", return_value=False),
            patch("spec_runtime.process_supervisor.identity_matches", return_value=False),
            patch("spec_runtime.web.server.remove_pid") as remove_pid,
        ):
            from spec_runtime.web.server import run_server

            assert run_server(tmp_path, background=True) == 1
        write_token.assert_not_called()
        managed.terminate.assert_called_once_with(grace_seconds=0.5)
        remove_pid.assert_called_once_with(tmp_path)

    def test_windows_recovers_interrupted_background_launch(self, tmp_path, monkeypatch):
        from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
        from spec_runtime.web.server import (
            _helper_metadata_path,
            _launch_path,
            _ready_path,
            _recover_launch,
            _write_launch_reservation,
            read_supervision_token,
        )

        monkeypatch.setenv(
            "SPEC_PROCESS_CONTROL_ROOT",
            str(tmp_path / "process-controls"),
        )
        supervision_id = "recover-web"
        nonce = "child-authenticated"
        identity = ProcessIdentity(123, "created", "python.exe")
        token = SupervisionToken(
            LifetimeMode.DETACHED,
            identity,
            1,
            "owner",
            supervision_id,
            payload_identity=ProcessIdentity(124, "payload", "python.exe"),
        )
        helper_path = _helper_metadata_path(tmp_path, supervision_id)
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        helper_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")
        _write_launch_reservation(
            tmp_path,
            supervision_id=supervision_id,
            helper_path=helper_path,
            nonce=nonce,
            host="127.0.0.1",
            port=7700,
        )
        ready_path = _ready_path(tmp_path)
        ready_path.write_text(
            json.dumps(
                {
                    "nonce": nonce,
                    "payload_identity": token.payload.to_dict(),
                    "host": "127.0.0.1",
                    "port": 7700,
                    "listener": "127.0.0.1:7700",
                }
            ),
            encoding="utf-8",
        )
        with (
            patch("spec_runtime.process_supervisor.identity_matches", return_value=True),
            patch(
                "spec_runtime.process_supervisor.promote_payload_identity",
                return_value=token,
            ) as promote_payload,
            patch("spec_runtime.web.server._wait_for_port", return_value=True),
        ):
            assert _recover_launch(tmp_path) == token
        if os.name == "nt":
            promote_payload.assert_called_once_with(token, token.payload)
        else:
            promote_payload.assert_not_called()
        assert read_supervision_token(tmp_path) == token
        assert not _launch_path(tmp_path).exists()
        assert not helper_path.exists()

    @pytest.mark.parametrize("corruption", ["malformed", "wrong-listener", "stale-identity"])
    def test_windows_recovery_rejects_untrusted_launch_state(
        self,
        tmp_path,
        corruption,
        monkeypatch,
    ):
        from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
        from spec_runtime.web.server import (
            _helper_metadata_path,
            _launch_path,
            _ready_path,
            _recover_launch,
            _write_launch_reservation,
        )

        monkeypatch.setenv(
            "SPEC_PROCESS_CONTROL_ROOT",
            str(tmp_path / "process-controls"),
        )
        supervision_id = "reject-web"
        identity = ProcessIdentity(123, "created", "python.exe")
        token = SupervisionToken(LifetimeMode.DETACHED, identity, 1, "owner", supervision_id)
        helper_path = _helper_metadata_path(tmp_path, supervision_id)
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        helper_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")
        _write_launch_reservation(
            tmp_path,
            supervision_id=supervision_id,
            helper_path=helper_path,
            nonce="nonce",
            host="127.0.0.1",
            port=7700,
        )
        ready = {
            "nonce": "nonce",
            "payload_identity": token.payload.to_dict(),
            "host": "127.0.0.1",
            "port": 7700,
            "listener": "127.0.0.1:7700",
        }
        _ready_path(tmp_path).write_text(json.dumps(ready), encoding="utf-8")
        if corruption == "malformed":
            _launch_path(tmp_path).write_text("not json", encoding="utf-8")
        elif corruption == "wrong-listener":
            ready["listener"] = "127.0.0.1:9999"
            _ready_path(tmp_path).write_text(json.dumps(ready), encoding="utf-8")
        with (
            patch(
                "spec_runtime.process_supervisor.identity_matches",
                return_value=corruption != "stale-identity",
            ),
            patch("spec_runtime.web.server._wait_for_port", return_value=True),
        ):
            recovered = _recover_launch(tmp_path)
        if corruption == "wrong-listener":
            assert recovered == token
            assert _launch_path(tmp_path).exists()
        else:
            assert recovered is None
            assert _launch_path(tmp_path).exists() == (corruption == "malformed")
        assert helper_path.exists() == (corruption != "stale-identity")

    def test_is_server_running_false_when_no_pid(self, tmp_path):
        with patch("spec_runtime.web.server._pid_path", return_value=tmp_path / "nonexistent"):
            from spec_runtime.web.server import is_server_running

            running, pid = is_server_running(tmp_path)
            assert not running
            assert pid is None

    def test_is_server_running_does_not_downgrade_malformed_supervision_to_pid(
        self,
        tmp_path,
    ):
        state_dir = tmp_path / "web"
        state_dir.mkdir()
        supervision_path = state_dir / "server.supervision.json"
        supervision_path.write_text("not json", encoding="utf-8")
        pid_path = state_dir / "server.pid"
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        with (
            patch("spec_runtime.web.server._supervision_path", return_value=supervision_path),
            patch("spec_runtime.web.server._pid_path", return_value=pid_path),
            patch("spec_runtime.process_supervisor.legacy_pid_record_is_live") as legacy_live,
        ):
            from spec_runtime.web.server import ServerOwnershipStateError, is_server_running

            with pytest.raises(ServerOwnershipStateError, match="supervision state is malformed"):
                is_server_running(tmp_path)
        legacy_live.assert_not_called()
        assert supervision_path.read_text(encoding="utf-8") == "not json"

    def test_is_server_running_true_for_own_process(self, tmp_path):
        from spec_runtime.web.server import _read_process_started_at

        pid_path = tmp_path / "web" / "server.pid"
        pid_path.parent.mkdir(parents=True)
        started_at = _read_process_started_at(os.getpid())
        pid_path.write_text(f"{os.getpid()}\n{started_at}")

        with patch("spec_runtime.web.server._pid_path", return_value=pid_path):
            from spec_runtime.web.server import is_server_running

            running, pid = is_server_running(tmp_path)
            if os.name == "nt":
                # PID files are diagnostic-only on Windows. Without a durable
                # Job token, trusting one would permit PID-reuse ownership bugs.
                assert not running
                assert pid is None
            else:
                assert running
                assert pid == os.getpid()

    def test_stop_server_not_running(self, tmp_path, capsys):
        with patch("spec_runtime.web.server._pid_path", return_value=tmp_path / "nonexistent"):
            from spec_runtime.web.server import stop_server

            rc = stop_server(tmp_path)
            assert rc == 1

    def test_stop_server_routes_legacy_pid_through_supervision_boundary(
        self,
        tmp_path,
    ):
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(True, 123)),
            patch("spec_runtime.web.server.read_supervision_token", return_value=None),
            patch("spec_runtime.web.server.read_pid", return_value=(123, "created")),
            patch(
                "spec_runtime.process_supervisor.terminate_legacy_pid_record",
                return_value=True,
            ) as terminate_legacy,
            patch("spec_runtime.web.server.remove_pid") as remove_pid,
        ):
            from spec_runtime.web.server import stop_server

            assert stop_server(tmp_path) == 0
        terminate_legacy.assert_called_once_with(123, "created")
        remove_pid.assert_called_once_with(tmp_path)

    def test_windows_failed_stop_retains_all_recovery_state(self, tmp_path):
        from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
        from spec_runtime.web.server import stop_server

        identity = ProcessIdentity(123, "created", "python.exe")
        token = SupervisionToken(LifetimeMode.DETACHED, identity, 1, "owner", "failed-stop")
        state_dir = tmp_path / ".spec-state" / "web"
        state_dir.mkdir(parents=True)
        paths = [
            state_dir / "server.supervision.json",
            state_dir / "server.launch.json",
            state_dir / "server.ready.json",
        ]
        for path in paths:
            path.write_text("retained", encoding="utf-8")
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(True, token.payload.pid)),
            patch("spec_runtime.web.server.read_supervision_token", return_value=token),
            patch("spec_runtime.process_supervisor.terminate", return_value=False),
        ):
            assert stop_server(tmp_path) == 1
        assert all(path.read_text(encoding="utf-8") == "retained" for path in paths)

    def test_windows_successful_stop_retires_exact_control_state(self, tmp_path):
        from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
        from spec_runtime.web.server import stop_server

        identity = ProcessIdentity(123, "created", "python.exe")
        token = SupervisionToken(
            LifetimeMode.RUN_OWNED,
            identity,
            1,
            "owner",
            "foreground-stop",
            payload_identity=identity,
        )
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(True, 123)),
            patch("spec_runtime.web.server.read_supervision_token", return_value=token),
            patch("spec_runtime.web.server._native_windows_host", return_value=True),
            patch("spec_runtime.process_supervisor.terminate", return_value=True),
            patch(
                "spec_runtime.process_supervisor.retire_inactive_control_state",
                return_value=True,
            ) as retire,
            patch("spec_runtime.web.server.remove_pid") as remove_pid,
        ):
            assert stop_server(tmp_path) == 0

        retire.assert_called_once_with(token)
        remove_pid.assert_called_once_with(tmp_path)

    def test_windows_control_retirement_failure_retains_recovery_state(
        self,
        tmp_path,
        capsys,
    ):
        from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
        from spec_runtime.web.server import stop_server

        identity = ProcessIdentity(123, "created", "python.exe")
        token = SupervisionToken(
            LifetimeMode.RUN_OWNED,
            identity,
            1,
            "owner",
            "foreground-stop-failed-retirement",
            payload_identity=identity,
        )
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(True, 123)),
            patch("spec_runtime.web.server.read_supervision_token", return_value=token),
            patch("spec_runtime.web.server._native_windows_host", return_value=True),
            patch("spec_runtime.process_supervisor.terminate", return_value=True),
            patch(
                "spec_runtime.process_supervisor.retire_inactive_control_state",
                return_value=False,
            ),
            patch("spec_runtime.web.server.remove_pid") as remove_pid,
        ):
            assert stop_server(tmp_path) == 1

        remove_pid.assert_not_called()
        assert "recovery state retained" in capsys.readouterr().err

    def test_server_status_not_running(self, tmp_path, capsys):
        with patch("spec_runtime.web.server._pid_path", return_value=tmp_path / "nonexistent"):
            from spec_runtime.web.server import server_status

            rc = server_status(tmp_path)
            assert rc == 0
            captured = capsys.readouterr()
            assert "not running" in captured.out

    def test_server_status_running_with_port(self, tmp_path, capsys):
        from spec_runtime.web.server import _read_process_started_at

        pid_path = tmp_path / "web" / "server.pid"
        pid_path.parent.mkdir(parents=True)
        started_at = _read_process_started_at(os.getpid())
        pid_path.write_text(f"{os.getpid()}\n{started_at}")
        port_path = tmp_path / "web" / "server.port"
        port_path.write_text("7700")

        with (
            patch("spec_runtime.web.server._pid_path", return_value=pid_path),
            patch("spec_runtime.web.server._port_path", return_value=port_path),
        ):
            from spec_runtime.web.server import server_status

            rc = server_status(tmp_path)
            assert rc == 0
            captured = capsys.readouterr()
            if os.name == "nt":
                assert "not running" in captured.out
            else:
                assert "port 7700" in captured.out

    def test_is_server_running_rejects_stale_pid(self, tmp_path):
        """A PID file with a mismatched started_at must not match the live process."""
        pid_path = tmp_path / "web" / "server.pid"
        pid_path.parent.mkdir(parents=True)
        # Write current PID but a bogus start time to simulate PID reuse
        pid_path.write_text(f"{os.getpid()}\nFri Jan  1 00:00:00 1999")

        with patch("spec_runtime.web.server._pid_path", return_value=pid_path):
            from spec_runtime.web.server import _read_process_started_at, is_server_running

            live_started_at = _read_process_started_at(os.getpid())
            # Only meaningful when ps is available (started_at != "")
            if live_started_at:
                running, pid = is_server_running(tmp_path)
                assert not running
                assert pid is None

    def test_print_token_creates_and_prints(self, tmp_path, capsys):
        with (
            patch("spec_runtime.web.server.read_token", return_value=None),
            patch("spec_runtime.web.server.load_or_create_token", return_value="new-tok"),
        ):
            from spec_runtime.web.server import print_token

            rc = print_token(tmp_path)
            assert rc == 0
            captured = capsys.readouterr()
            assert "new-tok" in captured.out

    def test_print_token_reset(self, tmp_path, capsys):
        with patch("spec_runtime.web.auth.reset_token", return_value="reset-tok") as mock_reset:
            from spec_runtime.web.server import print_token

            rc = print_token(tmp_path, reset=True)
            assert rc == 0
            captured = capsys.readouterr()
            assert "reset-tok" in captured.out
            mock_reset.assert_called_once_with(tmp_path)

    def test_run_server_refuses_when_already_running(self, tmp_path, capsys):
        """A second start must refuse rather than orphan the existing server."""
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(True, 999)),
            patch("spec_runtime.web.server.read_port", return_value=7700),
        ):
            from spec_runtime.web.server import run_server

            rc = run_server(tmp_path)
            assert rc == 1
            captured = capsys.readouterr()
            assert "already running" in captured.err

    @pytest.mark.parametrize(
        "state_name",
        ["server.supervision.json", "server.launch.json", "server.pid"],
    )
    def test_run_server_refuses_malformed_ownership_state(
        self,
        tmp_path,
        capsys,
        state_name,
    ):
        state_dir = tmp_path / ".spec-state" / "web"
        state_dir.mkdir(parents=True)
        (state_dir / state_name).write_text("not valid ownership state", encoding="utf-8")

        with patch("spec_runtime.web.server.load_or_create_token") as load_token:
            from spec_runtime.web.server import run_server

            assert run_server(tmp_path) == 1

        load_token.assert_not_called()
        assert "cannot start safely" in capsys.readouterr().err

    def test_payload_nonce_does_not_bypass_malformed_launch_reservation(
        self,
        tmp_path,
        capsys,
        monkeypatch,
    ):
        from spec_runtime.web.server import _launch_path, _write_launch_reservation, run_server

        monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "process-controls"))
        monkeypatch.setenv("SPEC_WEB_READY_NONCE", "payload-nonce")
        helper_path = tmp_path / "wrong-helper.json"
        _write_launch_reservation(
            tmp_path,
            supervision_id="payload-owner",
            helper_path=helper_path,
            nonce="payload-nonce",
            host="127.0.0.1",
            port=7700,
        )
        reservation = json.loads(_launch_path(tmp_path).read_text(encoding="utf-8"))
        reservation["listener"] = "127.0.0.1:9999"
        _launch_path(tmp_path).write_text(json.dumps(reservation), encoding="utf-8")

        with patch("spec_runtime.web.server.load_or_create_token") as load_token:
            assert run_server(tmp_path) == 1

        load_token.assert_not_called()
        assert "cannot start safely" in capsys.readouterr().err

    @pytest.mark.parametrize("operation", ["stop", "status"])
    def test_management_refuses_malformed_ownership_state(
        self,
        tmp_path,
        capsys,
        operation,
    ):
        state_dir = tmp_path / ".spec-state" / "web"
        state_dir.mkdir(parents=True)
        (state_dir / "server.supervision.json").write_text("not json", encoding="utf-8")

        from spec_runtime.web.server import server_status, stop_server

        command = stop_server if operation == "stop" else server_status
        assert command(tmp_path) == 1
        assert "state is malformed" in capsys.readouterr().err

    def test_wait_for_port_returns_true_on_connection(self):
        """_wait_for_port returns True when a connection succeeds."""
        from spec_runtime.web.server import _wait_for_port

        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            assert _wait_for_port("127.0.0.1", 7700, timeout=1.0)

    def test_wait_for_port_returns_false_on_timeout(self):
        """_wait_for_port returns False when connections keep failing."""
        from spec_runtime.web.server import _wait_for_port

        with patch("socket.create_connection", side_effect=OSError("refused")):
            assert not _wait_for_port("127.0.0.1", 7700, timeout=0.3)

    def test_background_start_fails_when_port_unavailable(self, tmp_path, capsys):
        """Background mode must return 1 when the child fails to bind."""
        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessIdentity,
            ProcessSupervisor,
            SupervisionToken,
        )

        token = SupervisionToken(
            LifetimeMode.DETACHED,
            ProcessIdentity(123, "created"),
            1,
            "owner",
            "failed-bind",
        )
        managed = MagicMock(token=token)
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("socket.create_connection", side_effect=OSError("refused")),
            patch.object(ProcessSupervisor, "spawn", return_value=managed),
            patch("spec_runtime.web.server._wait_for_ready_record", return_value=None),
            patch("spec_runtime.process_supervisor.identity_matches", return_value=False),
        ):
            from spec_runtime.web.server import run_server

            rc = run_server(tmp_path, background=True)
            assert rc == 1
            captured = capsys.readouterr()
            assert "failed" in captured.err.lower()
        managed.terminate.assert_called_once_with(grace_seconds=0.5)

    def test_foreground_banner_prints_after_bind(self, tmp_path, capsys):
        """Foreground banner must fire after Server.startup() (post-bind)."""
        import asyncio

        class _FakeServer:
            """Minimal uvicorn.Server stand-in that runs the startup/serve cycle."""

            started = False
            should_exit = False

            def __init__(self, config: object) -> None:
                pass

            async def startup(self, **kw: object) -> None:
                # Simulate a successful socket bind.
                self.started = True

            async def main_loop(self) -> None:
                pass

            async def shutdown(self, **kw: object) -> None:
                pass

            def run(self, sockets: object = None) -> None:
                asyncio.run(self._serve())

            async def _serve(self) -> None:
                await self.startup()
                await self.main_loop()
                await self.shutdown()

        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=False),
            patch("spec_runtime.web.server.write_pid"),
            patch("spec_runtime.web.server.read_pid", return_value=(os.getpid(), None)),
            patch("spec_runtime.web.server.remove_pid"),
            patch("spec_runtime.web.server.create_app", return_value=MagicMock()),
            patch("uvicorn.Config"),
            patch("uvicorn.Server", side_effect=_FakeServer),
        ):
            from spec_runtime.web.server import run_server

            rc = run_server(tmp_path, background=False)

        assert rc == 0
        captured = capsys.readouterr()
        assert "spec web running on" in captured.err
        assert "Authenticated URL:" in captured.err

    def test_foreground_no_banner_when_bind_fails(self, tmp_path, capsys):
        """Foreground suppresses banner when Server.startup() fails to bind."""
        import asyncio

        class _FakeServer:
            started = False  # stays False — bind failed
            should_exit = False

            def __init__(self, config: object) -> None:
                pass

            async def startup(self, **kw: object) -> None:
                pass  # started stays False

            async def main_loop(self) -> None:
                pass

            async def shutdown(self, **kw: object) -> None:
                pass

            def run(self, sockets: object = None) -> None:
                asyncio.run(self._serve())

            async def _serve(self) -> None:
                await self.startup()
                await self.main_loop()
                await self.shutdown()

        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=False),
            patch("spec_runtime.web.server.write_pid"),
            patch("spec_runtime.web.server.read_pid", return_value=(os.getpid(), None)),
            patch("spec_runtime.web.server.remove_pid"),
            patch("spec_runtime.web.server.create_app", return_value=MagicMock()),
            patch("uvicorn.Config"),
            patch("uvicorn.Server", side_effect=_FakeServer),
        ):
            from spec_runtime.web.server import run_server

            rc = run_server(tmp_path, background=False)

        assert rc == 0
        captured = capsys.readouterr()
        assert "spec web running on" not in captured.err

    def test_foreground_ctrl_c_exits_cleanly(self, tmp_path):
        """A normal foreground Ctrl-C must return success without a traceback."""
        fake_server = MagicMock()
        fake_server.startup = AsyncMock()
        fake_server.run.side_effect = KeyboardInterrupt

        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=False),
            patch("spec_runtime.web.server.write_pid"),
            patch("spec_runtime.web.server.read_pid", return_value=(os.getpid(), None)),
            patch("spec_runtime.web.server.remove_pid") as remove_pid,
            patch("spec_runtime.web.server.create_app", return_value=MagicMock()),
            patch("uvicorn.Config"),
            patch("uvicorn.Server", return_value=fake_server),
        ):
            from spec_runtime.web.server import run_server

            rc = run_server(tmp_path, background=False)

        assert rc == 0
        remove_pid.assert_called_once_with(tmp_path)

    @pytest.mark.parametrize("stale_nonce", [False, True])
    def test_direct_windows_foreground_publishes_current_process_claim(
        self,
        tmp_path,
        monkeypatch,
        stale_nonce,
    ):
        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessIdentity,
            SupervisionToken,
        )

        if stale_nonce:
            monkeypatch.setenv("SPEC_WEB_READY_NONCE", "stale-inherited-nonce")
        else:
            monkeypatch.delenv("SPEC_WEB_READY_NONCE", raising=False)
        identity = ProcessIdentity(os.getpid(), "created", "python.exe")
        claimed = SupervisionToken(
            LifetimeMode.RUN_OWNED,
            identity,
            identity.pid,
            identity.started_at,
            "web-foreground-test",
        )
        call_order: list[str] = []
        fake_server = MagicMock()
        fake_server.startup = AsyncMock()
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=True),
            patch(
                "spec_runtime.process_supervisor.claim_current_process",
                side_effect=lambda _name: (call_order.append("claim"), claimed)[1],
            ) as claim,
            patch(
                "spec_runtime.web.server.write_supervision_token",
                side_effect=lambda *_args: call_order.append("supervision"),
            ) as write_supervision,
            patch(
                "spec_runtime.web.server.write_pid",
                side_effect=lambda *_args, **_kwargs: call_order.append("pid"),
            ),
            patch("spec_runtime.web.server.read_pid", return_value=(os.getpid(), "created")),
            patch("spec_runtime.web.server.remove_pid"),
            patch("spec_runtime.web.server.create_app", return_value=MagicMock()),
            patch("uvicorn.Config"),
            patch("uvicorn.Server", return_value=fake_server),
        ):
            from spec_runtime.web.server import run_server

            assert run_server(tmp_path, background=False) == 0

        claim.assert_called_once()
        write_supervision.assert_called_once_with(tmp_path, claimed)
        assert call_order == ["claim", "supervision", "pid"]

    def test_matching_background_payload_reservation_skips_current_process_claim(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("SPEC_WEB_READY_NONCE", "matching-payload-nonce")
        fake_server = MagicMock()
        fake_server.startup = AsyncMock()
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=True),
            patch(
                "spec_runtime.web.server._launch_reservation_belongs_to_current_payload",
                return_value=True,
            ),
            patch("spec_runtime.process_supervisor.claim_current_process") as claim,
            patch("spec_runtime.web.server.write_supervision_token") as write_supervision,
            patch("spec_runtime.web.server.write_pid"),
            patch("spec_runtime.web.server.read_pid", return_value=(os.getpid(), "created")),
            patch("spec_runtime.web.server.remove_pid"),
            patch("spec_runtime.web.server.create_app", return_value=MagicMock()),
            patch("uvicorn.Config"),
            patch("uvicorn.Server", return_value=fake_server),
        ):
            from spec_runtime.web.server import run_server

            assert run_server(tmp_path, background=False) == 0

        claim.assert_not_called()
        write_supervision.assert_not_called()

    def test_background_windows_parent_does_not_claim_its_current_process(
        self,
        tmp_path,
    ):
        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessIdentity,
            ProcessSupervisor,
            SupervisionToken,
        )

        token = SupervisionToken(
            LifetimeMode.DETACHED,
            ProcessIdentity(123, "created"),
            1,
            "owner",
            "background-no-self-claim",
        )
        managed = MagicMock(token=token)
        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("spec_runtime.web.server._native_windows_host", return_value=True),
            patch("spec_runtime.process_supervisor.claim_current_process") as claim,
            patch("socket.create_connection", side_effect=OSError("free")),
            patch.object(ProcessSupervisor, "spawn", return_value=managed),
            patch("spec_runtime.web.server._wait_for_ready_record", return_value=token),
            patch("spec_runtime.web.server._ready_record_matches", return_value=True),
            patch("spec_runtime.web.server._wait_for_port", return_value=True),
            patch("spec_runtime.web.server.write_supervision_token"),
        ):
            from spec_runtime.web.server import run_server

            assert run_server(tmp_path, background=True) == 0

        claim.assert_not_called()

    def test_logging_configured_before_backend_availability_check(self, tmp_path):
        """Regression: logging.basicConfig must run before log_backend_availability
        so that INFO-level startup diagnostics are visible."""
        call_order: list[str] = []

        def fake_basic_config(**kwargs):
            call_order.append("basicConfig")

        def fake_log_backend(_repo_root=None):
            call_order.append("log_backend_availability")

        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessIdentity,
            ProcessSupervisor,
            SupervisionToken,
        )

        token = SupervisionToken(
            LifetimeMode.DETACHED,
            ProcessIdentity(123, "created"),
            1,
            "owner",
            "logging-order",
        )
        managed = MagicMock(token=token)
        supervisors = []

        def fake_spawn(supervisor, _command, **_kwargs):
            supervisors.append(supervisor)
            return managed

        with (
            patch("spec_runtime.web.server.is_server_running", return_value=(False, None)),
            patch("spec_runtime.web.server.load_or_create_token", return_value="tok"),
            patch("logging.basicConfig", side_effect=fake_basic_config),
            patch("spec_runtime.web.chat_api.log_backend_availability", fake_log_backend),
            patch("socket.create_connection", side_effect=OSError("free")),
            patch.object(ProcessSupervisor, "spawn", new=fake_spawn),
            patch("spec_runtime.web.server._wait_for_ready_record", return_value=token),
            patch("spec_runtime.web.server._ready_record_matches", return_value=True),
            patch("spec_runtime.web.server._wait_for_port", return_value=True),
            patch("spec_runtime.web.server.write_supervision_token"),
        ):
            from spec_runtime.web.server import run_server

            run_server(tmp_path, background=True)

        assert len(supervisors) == 1
        assert supervisors[0].mode is LifetimeMode.DETACHED
        assert supervisors[0]._supervision_id
        assert supervisors[0]._publish_durable_token is True
        assert "basicConfig" in call_order, "logging.basicConfig was not called"
        assert "log_backend_availability" in call_order, "log_backend_availability was not called"
        assert call_order.index("basicConfig") < call_order.index("log_backend_availability"), (
            f"logging.basicConfig must be called before log_backend_availability, "
            f"but call order was: {call_order}"
        )


# ---------------------------------------------------------------------------
# Starlette app + auth middleware tests
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    """Auth middleware enforces token on every request."""

    def _create_test_app(self, repo_root, token):
        from spec_runtime.web.server import create_app

        return create_app(repo_root, token)

    def test_unauthenticated_returns_401(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/dashboard", follow_redirects=False)
        assert resp.status_code == 401
        # API routes must return JSON, not HTML
        data = resp.json()
        assert "error" in data

    def test_unauthenticated_non_api_returns_html_form(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 401
        assert "Token" in resp.text

    def test_bearer_auth_allows_access(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/dashboard",
            headers={"Authorization": "Bearer secret-token"},
        )
        # Should get past auth (not 401) — actual handler may fail without real data
        assert resp.status_code != 401

    def test_query_token_sets_cookie_and_redirects(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "spec_session" in cookie
        assert "samesite=lax" in cookie.lower()

    def test_api_get_with_query_token_redirects(self, tmp_path):
        """GET /api/...?token= must also redirect to strip the token from the URL."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/dashboard?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "spec_session" in cookie
        assert "samesite=lax" in cookie.lower()
        # Redirect target must not contain the token
        location = resp.headers.get("location", "")
        assert "token=" not in location

    def test_post_with_query_token_does_not_redirect(self, tmp_path):
        """POST with ?token= must not 302 (which would lose the POST body)."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/specs/fake-id/implement?token=secret-token",
            follow_redirects=False,
        )
        # Must NOT be a redirect — the action should execute (or 404/422, not 302)
        assert resp.status_code not in (301, 302, 303, 307, 308)
        # Cookie must still be set so subsequent requests are authenticated
        cookie = resp.headers.get("set-cookie", "")
        assert "spec_session" in cookie
        assert "samesite=lax" in cookie.lower()

    def test_https_request_sets_secure_flag_on_redirect(self, tmp_path):
        """Cookie must include Secure when the request scheme is https."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False, base_url="https://testserver")
        resp = client.get("/?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in cookie

    def test_http_request_omits_secure_flag_on_redirect(self, tmp_path):
        """Cookie must NOT include Secure when the request scheme is http."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
        resp = client.get("/?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" not in cookie

    def test_https_post_sets_secure_flag(self, tmp_path):
        """POST cookie via raw header must include Secure on HTTPS."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False, base_url="https://testserver")
        resp = client.post(
            "/api/v1/specs/fake-id/implement?token=secret-token",
            follow_redirects=False,
        )
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in cookie

    def test_http_post_omits_secure_flag(self, tmp_path):
        """POST cookie via raw header must NOT include Secure on HTTP."""
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
        resp = client.post(
            "/api/v1/specs/fake-id/implement?token=secret-token",
            follow_redirects=False,
        )
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" not in cookie

    def test_proxy_header_https_sets_secure_flag_on_redirect(self, tmp_path):
        """X-Forwarded-Proto: https through ProxyHeadersMiddleware sets Secure flag."""
        from starlette.testclient import TestClient
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app = self._create_test_app(tmp_path, "secret-token")
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="*")
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get(
            "/?token=secret-token",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in cookie

    def test_proxy_header_https_sets_secure_flag_on_post(self, tmp_path):
        """X-Forwarded-Proto: https through ProxyHeadersMiddleware sets Secure on POST path."""
        from starlette.testclient import TestClient
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app = self._create_test_app(tmp_path, "secret-token")
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="*")
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/specs/fake-id/implement?token=secret-token",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in cookie

    def test_proxy_header_http_omits_secure_flag(self, tmp_path):
        """X-Forwarded-Proto: http through ProxyHeadersMiddleware omits Secure."""
        from starlette.testclient import TestClient
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app = self._create_test_app(tmp_path, "secret-token")
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="*")
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get(
            "/?token=secret-token",
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        assert "Secure" not in cookie

    def test_wrong_token_returns_401(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/dashboard",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_cookie_auth_allows_access(self, tmp_path):
        from starlette.testclient import TestClient

        app = self._create_test_app(tmp_path, "secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("spec_session", "secret-token")
        resp = client.get("/api/v1/dashboard")
        # Cookie auth passes — should not be 401
        assert resp.status_code != 401

    def test_token_reset_rotates_auth_for_running_app(self, tmp_path):
        """After token reset the middleware must reject the old token and accept the new one."""
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        current_token = ["original-token"]

        with patch(
            "spec_runtime.web.server.read_token",
            side_effect=lambda _root: current_token[0],
        ):
            app = create_app(tmp_path, "original-token")
            client = TestClient(app, raise_server_exceptions=False)

            # Original token works
            resp = client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer original-token"},
            )
            assert resp.status_code != 401

            # Simulate token rotation (e.g. `spec web token --reset`)
            current_token[0] = "new-rotated-token"

            # Old token must now be rejected
            resp = client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer original-token"},
            )
            assert resp.status_code == 401

            # New token must be accepted
            resp = client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer new-rotated-token"},
            )
            assert resp.status_code != 401

    def test_two_instances_do_not_share_session(self, tmp_path):
        """A cookie set by one instance must not authenticate another."""
        from starlette.testclient import TestClient

        from spec_runtime.web.auth import cookie_name_for_port
        from spec_runtime.web.server import create_app

        app_a = create_app(tmp_path / "a", "token-a", port=7700)
        app_b = create_app(tmp_path / "b", "token-b", port=7701)
        client_a = TestClient(app_a, raise_server_exceptions=False)
        client_b = TestClient(app_b, raise_server_exceptions=False)

        # Sending instance A's cookie to instance B must not authenticate.
        client_b.cookies.set(cookie_name_for_port(7700), "token-a")
        resp = client_b.get("/api/v1/dashboard", follow_redirects=False)
        assert resp.status_code == 401

        # And vice versa: instance B's cookie does not work on instance A.
        client_a.cookies.set(cookie_name_for_port(7701), "token-b")
        resp = client_a.get("/api/v1/dashboard", follow_redirects=False)
        assert resp.status_code == 401

    def test_set_cookie_name_matches_read_back(self, tmp_path):
        """The cookie set on login must be the same name middleware reads."""
        from starlette.testclient import TestClient

        from spec_runtime.web.auth import cookie_name_for_port
        from spec_runtime.web.server import create_app

        port = 7702
        app = create_app(tmp_path, "secret-token", port=port)
        client = TestClient(app, raise_server_exceptions=False)

        # Login via query string sets the per-instance cookie.
        resp = client.get("/?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        cookie = resp.headers.get("set-cookie", "")
        expected_name = cookie_name_for_port(port)
        assert expected_name in cookie
        # The bare prefix must NOT appear standalone — confirms it is suffixed.
        assert f"{expected_name}=secret-token" in cookie

        # Subsequent request authenticates via that cookie.
        client.cookies.clear()
        client.cookies.set(expected_name, "secret-token")
        resp = client.get("/api/v1/dashboard")
        assert resp.status_code != 401

    def test_query_string_login_sets_per_instance_cookie(self, tmp_path):
        """The ?token=… login flow must set the per-instance cookie name."""
        from starlette.testclient import TestClient

        from spec_runtime.web.auth import cookie_name_for_port
        from spec_runtime.web.server import create_app

        port = 7703
        app = create_app(tmp_path, "secret-token", port=port)
        client = TestClient(app, raise_server_exceptions=False)

        # GET path
        resp = client.get("/?token=secret-token", follow_redirects=False)
        assert resp.status_code == 302
        assert cookie_name_for_port(port) in resp.headers.get("set-cookie", "")

        # POST path
        resp = client.post(
            "/api/v1/specs/fake-id/implement?token=secret-token",
            follow_redirects=False,
        )
        assert cookie_name_for_port(port) in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# API route tests — patch at source module level for lazy imports
# ---------------------------------------------------------------------------


class TestAPIRoutes:
    """Tests for /api/v1 route handlers using mocked data."""

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token)
        client = TestClient(app, raise_server_exceptions=False)
        return client

    def _auth_headers(self, token="test-token"):
        return {"Authorization": f"Bearer {token}"}

    def test_list_specs(self, tmp_path):
        mock_record = MagicMock(
            spec_id="test-spec",
            area="backend",
            priority=10,
            depends_on=(),
            description="A test spec",
            obsolete=False,
            superseded_by="",
        )
        mock_git_state = MagicMock()

        with (
            patch("spec_runtime.spec_metadata.iter_spec_metadata", return_value=[mock_record]),
            patch("spec_runtime.spec_status.collect_git_spec_state", return_value=mock_git_state),
            patch("spec_runtime.spec_status.get_spec_status", return_value="not-started"),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/specs", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["spec_id"] == "test-spec"

    def test_get_spec_returns_body_with_rendered_html(self, tmp_path):
        """API returns raw body and rendered body_html per spec contract."""
        spec_file = tmp_path / "specs" / "my-spec.md"
        spec_file.parent.mkdir(parents=True)
        spec_file.write_text("---\nid: my-spec\nstatus: not-started\n---\n# Hello\n")

        mock_record = MagicMock(
            spec_id="my-spec",
            area="backend",
            priority=10,
            depends_on=(),
            description="A test spec",
            body="# Hello\n\nSome **bold** text.",
            obsolete=False,
            superseded_by="",
        )
        mock_git_state = MagicMock()
        mock_index = MagicMock(
            latest_by_spec={},
            records=[],
        )

        with (
            patch("spec_runtime.spec_metadata.iter_spec_metadata", return_value=[mock_record]),
            patch("spec_runtime.spec_status.collect_git_spec_state", return_value=mock_git_state),
            patch("spec_runtime.spec_status.get_spec_status", return_value="not-started"),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/specs/my-spec", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec_id"] == "my-spec"
        assert data["body"] == "# Hello\n\nSome **bold** text."
        # body_html contains rendered markdown per spec criterion 5
        assert "body_html" in data
        assert "<strong>bold</strong>" in data["body_html"]
        assert "<h1>" in data["body_html"] or "<h1" in data["body_html"]

    def test_render_markdown_strips_javascript_links(self):
        """Markdown links with javascript: scheme must be neutralized."""
        from spec_runtime.web.api import _render_markdown

        html = _render_markdown("[click me](javascript:alert(1))")
        assert "javascript:" not in html
        assert "click me" in html

    def test_render_markdown_strips_data_links(self):
        from spec_runtime.web.api import _render_markdown

        html = _render_markdown("[x](data:text/html,<script>alert(1)</script>)")
        assert "data:" not in html

    def test_get_spec_not_found(self, tmp_path):
        with (
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/specs/nonexistent", headers=self._auth_headers())

        assert resp.status_code == 404

    def test_list_runs(self, tmp_path):
        mock_index = MagicMock(
            records=[
                {
                    "run_id": "r1",
                    "spec_id": "s1",
                    "phase": "implement",
                    "status": "running",
                    "agent": "claude",
                    "attempts": 1,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:01:00",
                }
            ]
        )

        with patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["run_id"] == "r1"
        assert "elapsed" in data[0]

    def test_get_run_not_found(self, tmp_path):
        mock_index = MagicMock(by_run_id={})

        with patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/nonexistent", headers=self._auth_headers())

        assert resp.status_code == 404

    def test_get_run_found(self, tmp_path):
        run_data = {"run_id": "r1", "spec_id": "s1", "status": "running"}
        mock_index = MagicMock(by_run_id={"r1": run_data})

        with (
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.review_feedback.ReviewResult.load", return_value=None),
            patch("spec_runtime.orchestrator.BlockDiagnosis.load", return_value=None),
        ):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/r1", headers=self._auth_headers())

        assert resp.status_code == 200
        assert resp.json()["run_id"] == "r1"

    def test_get_run_includes_review_findings(self, tmp_path):
        from spec_runtime.review_feedback import ReviewFinding

        run_data = {"run_id": "r1", "spec_id": "s1", "status": "review"}
        mock_index = MagicMock(by_run_id={"r1": run_data})
        # Use real ReviewFinding dataclass instances to verify asdict() serialization
        mock_review = MagicMock(
            findings=[ReviewFinding(id="F1", title="Bug found", severity="P1")],
            status="request_changes",
            summary="Found a bug",
        )

        with (
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.review_feedback.ReviewResult.load", return_value=mock_review),
            patch("spec_runtime.orchestrator.BlockDiagnosis.load", return_value=None),
        ):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/r1", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["review_status"] == "request_changes"
        assert data["review_summary"] == "Found a bug"
        assert len(data["review_findings"]) == 1
        assert data["review_findings"][0]["id"] == "F1"

    def test_get_run_includes_block_diagnosis(self, tmp_path):
        from dataclasses import dataclass

        @dataclass
        class FakeBlock:
            summary: str = "CI is red"
            root_cause: str = "Flaky test"
            confidence: float = 0.9
            category: str = "ci"

        run_data = {"run_id": "r1", "spec_id": "s1", "status": "blocked"}
        mock_index = MagicMock(by_run_id={"r1": run_data})

        with (
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.review_feedback.ReviewResult.load", return_value=None),
            patch("spec_runtime.orchestrator.BlockDiagnosis.load", return_value=FakeBlock()),
        ):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/r1", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["block_diagnosis"]["summary"] == "CI is red"
        assert data["block_diagnosis"]["root_cause"] == "Flaky test"

    def test_get_run_log_no_file(self, tmp_path):
        mock_index = MagicMock(by_run_id={"r1": {"spec_id": "s1"}})
        with (
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.autopilot_tui.dashboard.resolve_log_path", return_value=None),
        ):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/r1/log", headers=self._auth_headers())

        assert resp.status_code == 200
        assert resp.json()["lines"] == []

    def test_get_run_log_with_file(self, tmp_path):
        log_file = tmp_path / "s1--20260101T000000Z.log"
        log_file.write_text("line1\nline2\nline3\n")

        mock_index = MagicMock(by_run_id={"r1": {"spec_id": "s1"}})
        with (
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.autopilot_tui.dashboard.resolve_log_path", return_value=log_file),
        ):
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/runs/r1/log?lines=2", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == ["line2", "line3"]

    def test_dashboard(self, tmp_path):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeRow:
            spec_id: str = "s1"
            display_spec_id: str = "s1"
            agent: str = "claude"
            phase: str = "implement"
            retries: str = "1/5"
            elapsed: str = "5m"
            status: str = "running"
            branch: str = "code/s1--abc"
            run_id: str = "r1"
            run_mode: str = "spec"
            created_at: str = "2026-01-01"

        @dataclass(frozen=True)
        class FakeCandidate:
            spec_id: str = "s2"
            agent: str = "claude"
            area: str = "backend"
            priority: int = 10
            unlock_count: int = 0
            status: str = "not-started"
            run_id: str = ""
            reason: str = "new"

        from spec_runtime.autopilot_tui.dashboard import DashboardSnapshot

        snapshot = DashboardSnapshot(
            rows=(FakeRow(),),
            queue=(FakeCandidate(),),
            merged_count=5,
            passed_count=2,
        )

        mock_record = MagicMock(
            spec_id="s1",
            area="backend",
            priority=10,
            depends_on=(),
            description="A test spec",
            obsolete=False,
            superseded_by="",
        )
        mock_git_state = MagicMock()

        with (
            patch(
                "spec_runtime.autopilot_tui.dashboard.load_dashboard_snapshot",
                return_value=snapshot,
            ),
            patch("spec_runtime.spec_metadata.iter_spec_metadata", return_value=[mock_record]),
            patch("spec_runtime.spec_status.collect_git_spec_state", return_value=mock_git_state),
            patch("spec_runtime.spec_status.get_spec_status", return_value="in-progress"),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/dashboard", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["merged_count"] == 5
        assert data["active_count"] == 1
        assert data["passed_count"] == 2
        assert "specs" in data
        assert len(data["specs"]) == 1
        assert data["specs"][0]["spec_id"] == "s1"

    def test_implement_spec(self, tmp_path):
        spec_file = tmp_path / "specs" / "my-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text("---\nid: my-spec\nstatus: not-started\n---\n")
        mock_index = MagicMock(latest_by_spec={})

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            mock_proc = MagicMock(pid=42)
            mock_proc.poll.return_value = None  # process still running
            mock_popen.return_value = mock_proc
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/specs/my-spec/implement", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec_id"] == "my-spec"
        assert data["status"] == "started"
        assert data["pid"] == 42
        assert "run_id" in data
        # Fresh starts get a synthesized run_state with spec_id and status
        assert data["run_state"] is not None
        assert data["run_state"]["spec_id"] == "my-spec"
        assert data["run_state"]["status"] == "starting"

    def test_implement_spec_not_found(self, tmp_path):
        """POST /implement for a nonexistent spec returns 404."""
        with patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config:
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/specs/nonexistent/implement", headers=self._auth_headers())

        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    def test_implement_spec_returns_existing_run(self, tmp_path):
        """When a new run record appears after spawning, return it."""
        spec_file = tmp_path / "specs" / "my-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text("---\nid: my-spec\nstatus: not-started\n---\n")
        old_run = {"run_id": "my-spec-20260331T000000", "spec_id": "my-spec", "status": "completed"}
        new_run = {"run_id": "my-spec-20260401T000000", "spec_id": "my-spec", "status": "running"}
        pre_index = MagicMock(latest_by_spec={"my-spec": old_run})
        post_index = MagicMock(latest_by_spec={"my-spec": new_run})

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("spec_runtime.autopilot.load_run_record_index", side_effect=[pre_index, post_index]),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            mock_proc = MagicMock(pid=42)
            mock_proc.poll.return_value = None  # process still running
            mock_popen.return_value = mock_proc
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/specs/my-spec/implement", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "my-spec-20260401T000000"
        assert data["run_state"]["status"] == "running"

    def test_implement_spec_stale_run_returns_starting(self, tmp_path):
        """When the run record hasn't changed after spawning, return synthesized starting state."""
        spec_file = tmp_path / "specs" / "my-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text("---\nid: my-spec\nstatus: not-started\n---\n")
        stale_run = {"run_id": "my-spec-20260331T000000", "spec_id": "my-spec", "status": "completed"}
        mock_index = MagicMock(latest_by_spec={"my-spec": stale_run})

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            mock_proc = MagicMock(pid=42)
            mock_proc.poll.return_value = None  # process still running
            mock_popen.return_value = mock_proc
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/specs/my-spec/implement", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == ""
        assert data["run_state"]["status"] == "starting"
        assert data["run_state"]["spec_id"] == "my-spec"

    def test_stop_spec_no_active_run(self, tmp_path):
        with (
            patch(
                "spec_runtime.autopilot_tui.dashboard._resolve_live_process_group",
                return_value=None,
            ),
            patch("spec_runtime.orchestrator.stop_run") as stop_run,
        ):
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/specs/my-spec/stop", headers=self._auth_headers())

        assert resp.status_code == 404
        stop_run.assert_not_called()

    def test_stop_spec_uses_managed_process_when_run_metadata_exists(self, tmp_path):
        """A web-started run retains its portable tree-termination boundary."""
        spec_file = tmp_path / "specs" / "my-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text("---\nid: my-spec\nstatus: not-started\n---\n")
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        run = {"run_id": "my-spec-run", "spec_id": "my-spec", "status": "running"}
        run_index = MagicMock(latest_by_spec={"my-spec": run})

        with (
            patch("subprocess.Popen", return_value=proc),
            patch(
                "spec_runtime.autopilot_tui.dashboard._resolve_live_process_group",
                return_value=(42, "creation-time"),
            ),
            patch("spec_runtime.autopilot.load_run_record_index", return_value=run_index),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            start_resp = client.post(
                "/api/v1/specs/my-spec/implement", headers=self._auth_headers()
            )
            resp = client.post("/api/v1/specs/my-spec/stop", headers=self._auth_headers())

        assert start_resp.status_code == 200
        assert resp.status_code == 200
        proc.terminate.assert_called_once_with(grace_seconds=3)
        proc.wait.assert_called_once_with(timeout=3)
        assert "my-spec" not in client.app.state.web_started_procs

    def test_process_registry_is_scoped_to_app_and_cleared_on_shutdown(self, tmp_path):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        first_app = create_app(tmp_path / "first", "token")
        second_app = create_app(tmp_path / "second", "token")
        first_app.state.web_started_procs["my-spec"] = MagicMock(pid=42)

        assert second_app.state.web_started_procs == {}
        with TestClient(first_app):
            assert "my-spec" in first_app.state.web_started_procs
        assert first_app.state.web_started_procs == {}

    def test_dispatch_start(self, tmp_path):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock(pid=99)
            mock_proc.poll.return_value = None  # process still running
            mock_popen.return_value = mock_proc
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/dispatch/start", headers=self._auth_headers())

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_dispatch_start_immediate_exit(self, tmp_path):
        """dispatch/start returns 422 when the subprocess exits immediately."""
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock(pid=99)
            mock_proc.poll.return_value = 1  # exited with error
            mock_popen.return_value = mock_proc
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/dispatch/start", headers=self._auth_headers())

        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_dispatch_start_dry_run(self, tmp_path):
        """dry_run runs synchronously and returns the dispatch preview output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Queue:\n  my-spec agent=claude\n", stderr=""
            )
            client = self._make_client(tmp_path)
            resp = client.post(
                "/api/v1/dispatch/start",
                headers=self._auth_headers(),
                json={"dry_run": True},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "dry_run"
        assert "Queue:" in body["stdout"]
        # The --dry-run flag must be present in the spawned command.
        assert "--dry-run" in mock_run.call_args[0][0]
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"
        assert mock_run.call_args.kwargs["errors"] == "replace"

    def test_dispatch_stop(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="stopped\n", stderr="")
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/dispatch/stop", headers=self._auth_headers())

        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"
        assert mock_run.call_args.kwargs["errors"] == "replace"

    def test_dispatch_stop_not_running(self, tmp_path):
        # `spec auto stop` reports "not running" (and may exit non-zero) when
        # no dispatcher is active — the endpoint must classify this as a
        # benign no-op rather than an error.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Autopilot is not running."
            )
            client = self._make_client(tmp_path)
            resp = client.post("/api/v1/dispatch/stop", headers=self._auth_headers())

        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"


# ---------------------------------------------------------------------------
# CLI dispatch tests
# ---------------------------------------------------------------------------


class TestCLIWebCommand:
    """Tests that `spec web` subcommands dispatch correctly."""

    def _mock_config(self):
        return MagicMock(
            agents=MagicMock(default="claude", review_default=""),
            base_ref="master",
            retry_cap=5,
            paths=MagicMock(specs_dir="specs", task_specs_dir="specs/tasks"),
        )

    def _import_cli(self):
        sys.modules.pop("spec_runtime.cli", None)
        from spec_runtime import cli

        return cli

    def test_web_no_subcommand_exits_nonzero(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            rc = cli.main(["web"])
        assert rc != 0

    def test_web_start_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["web", "start", "--help"])

    def test_web_stop_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["web", "stop", "--help"])

    def test_web_status_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["web", "status", "--help"])

    def test_web_token_help_exits_0(self):
        cli = self._import_cli()
        with patch.object(cli, "_lazy_config", return_value=self._mock_config()):
            with pytest.raises(SystemExit, match="0"):
                cli.main(["web", "token", "--help"])

    def test_web_start_dispatches(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.web.server.run_server", return_value=0) as mock_run,
        ):
            rc = cli.main(["web", "start"])
        assert rc == 0
        mock_run.assert_called_once()

    def test_web_start_warns_for_non_loopback_bind(self, capsys):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.web.server.run_server", return_value=0) as mock_run,
        ):
            rc = cli.main(["web", "start", "--host", "0.0.0.0"])

        assert rc == 0
        assert "does not terminate TLS" in capsys.readouterr().err
        assert mock_run.call_args.kwargs["host"] == "0.0.0.0"

    def test_web_stop_dispatches(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.web.server.stop_server", return_value=0) as mock_stop,
        ):
            rc = cli.main(["web", "stop"])
        assert rc == 0
        mock_stop.assert_called_once()

    def test_web_status_dispatches(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.web.server.server_status", return_value=0) as mock_status,
        ):
            rc = cli.main(["web", "status"])
        assert rc == 0
        mock_status.assert_called_once()

    def test_web_token_dispatches(self):
        cli = self._import_cli()
        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("spec_runtime.web.server.print_token", return_value=0) as mock_token,
        ):
            rc = cli.main(["web", "token"])
        assert rc == 0
        mock_token.assert_called_once()

    def test_web_start_missing_deps_exits_1(self):
        cli = self._import_cli()

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def mock_import(name, *args, **kwargs):
            if name in ("starlette", "uvicorn"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with (
            patch.object(cli, "_lazy_config", return_value=self._mock_config()),
            patch("builtins.__import__", side_effect=mock_import),
        ):
            rc = cli.main(["web", "start"])
        assert rc == 1


# ---------------------------------------------------------------------------
# resolve_log_path tests
# ---------------------------------------------------------------------------


class TestResolveLogPath:
    """Tests for log path resolution, especially same-day run disambiguation."""

    def test_same_day_runs_resolved_by_full_timestamp(self, tmp_path):
        """Two runs on the same day must resolve to different log files."""
        from spec_runtime.autopilot_tui.dashboard import resolve_log_path

        runs_root = tmp_path / ".spec-state" / "autopilot" / "runs"
        runs_root.mkdir(parents=True)

        log1 = runs_root / "my-spec--20260401T010000000000.log"
        log2 = runs_root / "my-spec--20260401T050000000000.log"
        log1.write_text("log from run 1\n")
        log2.write_text("log from run 2\n")

        with (
            patch("spec_runtime.autopilot_tui.dashboard._read_active_data", return_value={}),
            patch("spec_runtime.autopilot.autopilot_runs_root", return_value=runs_root),
        ):
            # Request run 1 — should get log1, not log2
            path1 = resolve_log_path(
                tmp_path, "my-spec", run_id="my-spec-20260401T010000000000"
            )
            assert path1 is not None
            assert path1.name == log1.name

            # Request run 2 — should get log2
            path2 = resolve_log_path(
                tmp_path, "my-spec", run_id="my-spec-20260401T050000000000"
            )
            assert path2 is not None
            assert path2.name == log2.name

    def test_single_candidate_returns_it_regardless(self, tmp_path):
        """With only one log file, it should be returned for any run_id."""
        from spec_runtime.autopilot_tui.dashboard import resolve_log_path

        runs_root = tmp_path / ".spec-state" / "autopilot" / "runs"
        runs_root.mkdir(parents=True)

        log = runs_root / "my-spec--20260401T010000000000.log"
        log.write_text("only log\n")

        with (
            patch("spec_runtime.autopilot_tui.dashboard._read_active_data", return_value={}),
            patch("spec_runtime.autopilot.autopilot_runs_root", return_value=runs_root),
        ):
            path = resolve_log_path(
                tmp_path, "my-spec", run_id="my-spec-20260401T010000000000"
            )
            assert path is not None
            assert path.name == log.name

    def test_multiple_candidates_choose_nearest_run_log_not_newest_probe(self, tmp_path):
        """A requested historical run should not fall through to a newer probe log."""
        from spec_runtime.autopilot_tui.dashboard import resolve_log_path

        runs_root = tmp_path / ".spec-state" / "autopilot" / "runs"
        runs_root.mkdir(parents=True)

        run_log = runs_root / "web-chat--20260402T231753Z.log"
        probe_log = runs_root / "web-chat--20260403T030343Z.log"
        run_log.write_text("real run log\n")
        probe_log.write_text("Error: Lock contention: another process holds the lock for web-chat\n")
        os.utime(run_log, (0, datetime(2026, 4, 3, 3, 3, 44, tzinfo=UTC).timestamp()))
        os.utime(probe_log, (0, datetime(2026, 4, 3, 3, 3, 43, tzinfo=UTC).timestamp()))
        mock_index = MagicMock(
            by_run_id={
                "web-chat-20260402T231754167397": {
                    "run_id": "web-chat-20260402T231754167397",
                    "updated_at": "2026-04-03T03:03:44.775916+00:00",
                }
            }
        )

        with (
            patch("spec_runtime.autopilot_tui.dashboard._read_active_data", return_value={}),
            patch("spec_runtime.autopilot.autopilot_runs_root", return_value=runs_root),
            patch("spec_runtime.autopilot.load_run_record_index", return_value=mock_index),
        ):
            path = resolve_log_path(
                tmp_path, "web-chat", run_id="web-chat-20260402T231754167397"
            )
            assert path is not None
            assert path.name == run_log.name

    def test_run_log_alias_path_wins_when_present(self, tmp_path):
        """Run-specific aliases should bypass generic spec log guessing."""
        from spec_runtime.autopilot import run_log_alias_path
        from spec_runtime.autopilot_tui.dashboard import resolve_log_path

        runs_root = tmp_path / ".spec-state" / "autopilot" / "runs"
        runs_root.mkdir(parents=True)

        aliased_log = runs_root / "web-chat--20260403T090000Z.log"
        unrelated_log = runs_root / "web-chat--20260403T030343Z.log"
        aliased_log.write_text("aliased log\n")
        unrelated_log.write_text("new unrelated log\n")
        run_id = "web-chat-20260402T231754167397"

        with (
            patch("spec_runtime.autopilot_tui.dashboard._read_active_data", return_value={}),
            patch("spec_runtime.autopilot.autopilot_runs_root", return_value=runs_root),
        ):
            run_log_alias_path(tmp_path, run_id).write_text(str(aliased_log) + "\n")
            path = resolve_log_path(tmp_path, "web-chat", run_id=run_id)
            assert path is not None
            assert path.name == aliased_log.name


# ---------------------------------------------------------------------------
# _serialize_snapshot unit test
# ---------------------------------------------------------------------------


class TestSerializeSnapshot:
    """Test that _serialize_snapshot produces the expected dict shape."""

    def test_basic_serialization(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeRow:
            spec_id: str = "s1"
            display_spec_id: str = "s1"
            agent: str = "claude"
            phase: str = "implement"
            retries: str = "1/5"
            elapsed: str = "5m"
            status: str = "running"
            branch: str = "code/s1--abc"
            run_id: str = "r1"
            run_mode: str = "spec"
            created_at: str = "2026-01-01"

        @dataclass(frozen=True)
        class FakeCandidate:
            spec_id: str = "s2"
            agent: str = "claude"
            area: str = "backend"
            priority: int = 10
            unlock_count: int = 0
            status: str = "not-started"
            run_id: str = ""
            reason: str = "new"

        from spec_runtime.autopilot_tui.dashboard import DashboardSnapshot

        snapshot = DashboardSnapshot(
            rows=(FakeRow(),),
            queue=(FakeCandidate(),),
            merged_count=5,
            passed_count=2,
        )

        from spec_runtime.web.api import _serialize_snapshot

        result = _serialize_snapshot(snapshot)
        assert result["merged_count"] == 5
        assert result["passed_count"] == 2
        assert result["active_count"] == 1
        assert len(result["rows"]) == 1
        assert result["rows"][0]["spec_id"] == "s1"


# ---------------------------------------------------------------------------
# Client-side logic regression tests
# ---------------------------------------------------------------------------


def _increment_elapsed(text: str) -> str:
    """Python port of the JS incrementElapsed function in app.js.

    Mirrors the regex ``/^(?:(\\d+)h)?(\\d+)m(\\d{2})s$/`` and the
    rollover logic so we can unit-test it without a JS runtime.
    """
    import re

    m = re.match(r"^(?:(\d+)h)?(\d+)m(\d{2})s$", text)
    if not m:
        return text
    h = int(m.group(1) or "0")
    mn = int(m.group(2))
    sec = int(m.group(3))
    sec += 1
    if sec >= 60:
        sec = 0
        mn += 1
    if mn >= 60:
        mn = 0
        h += 1
    ss = f"{sec:02d}"
    if h:
        return f"{h}h{mn:02d}m{ss}s"
    return f"{mn}m{ss}s"


class TestIncrementElapsed:
    """Regression tests for the elapsed-timer increment logic (app.js mirror)."""

    def test_simple_increment(self):
        assert _increment_elapsed("5m30s") == "5m31s"

    def test_seconds_rollover(self):
        assert _increment_elapsed("5m59s") == "6m00s"

    def test_minutes_rollover(self):
        assert _increment_elapsed("59m59s") == "1h00m00s"

    def test_hours_increment(self):
        assert _increment_elapsed("1h02m30s") == "1h02m31s"

    def test_hours_minutes_rollover(self):
        assert _increment_elapsed("1h59m59s") == "2h00m00s"

    def test_zero_value(self):
        assert _increment_elapsed("0m00s") == "0m01s"

    def test_non_matching_returns_unchanged(self):
        assert _increment_elapsed("—") == "—"
        assert _increment_elapsed("") == ""
        assert _increment_elapsed("invalid") == "invalid"

    def test_format_elapsed_output_is_parseable(self):
        """Verify _format_elapsed output matches the pattern incrementElapsed expects."""
        from spec_runtime.autopilot import _format_elapsed

        # Simulate a run started 90 seconds ago
        ts = (datetime.now(UTC).replace(microsecond=0)
              - __import__("datetime").timedelta(seconds=90)).isoformat()
        result = _format_elapsed(ts)
        # Should be parseable: "1m30s" (approximately)
        assert result == _increment_elapsed(result) or result != "—"
        # Must match the expected pattern
        import re
        assert re.match(r"^(?:\d+h)?\d+m\d{2}s$", result), (
            f"_format_elapsed returned '{result}' which doesn't match the elapsed pattern"
        )

    def test_hour_format_consistency(self):
        """Verify _format_elapsed with hours matches incrementElapsed pattern."""
        from spec_runtime.autopilot import _format_elapsed

        ts = (datetime.now(UTC).replace(microsecond=0)
              - __import__("datetime").timedelta(hours=2, minutes=5, seconds=10)).isoformat()
        result = _format_elapsed(ts)
        assert result == "2h05m10s" or result == "2h05m11s"  # timing tolerance
        incremented = _increment_elapsed(result)
        assert incremented != result  # must have changed


class TestDashboardMergedFilterBehavior:
    """Regression tests: dashboard API + app.js must support hiding merged specs."""

    def _make_client(self, tmp_path, token="test-token"):
        from starlette.testclient import TestClient

        from spec_runtime.web.server import create_app

        app = create_app(tmp_path, token)
        return TestClient(app, raise_server_exceptions=False)

    def _auth_headers(self):
        return {"Authorization": "Bearer test-token"}

    def test_dashboard_api_returns_merged_specs_with_status(self, tmp_path):
        """API must return per-spec status including 'merged' so the frontend can filter."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeRow:
            spec_id: str = "s1"
            display_spec_id: str = "s1"
            agent: str = "claude"
            phase: str = "implement"
            retries: str = "1/5"
            elapsed: str = "5m30s"
            status: str = "running"
            branch: str = "code/s1--abc"
            run_id: str = "r1"
            run_mode: str = "spec"
            created_at: str = "2026-01-01"

        from spec_runtime.autopilot_tui.dashboard import DashboardSnapshot

        snapshot = DashboardSnapshot(
            rows=(FakeRow(),),
            queue=(),
            merged_count=2,
            passed_count=1,
        )

        # Create mock spec records: one merged, one in-progress, one not-started
        merged_record = MagicMock(
            spec_id="done-spec", area="backend", priority=10,
            depends_on=(), description="Already merged", obsolete=False, superseded_by="",
        )
        active_record = MagicMock(
            spec_id="s1", area="backend", priority=20,
            depends_on=(), description="Active spec", obsolete=False, superseded_by="",
        )
        waiting_record = MagicMock(
            spec_id="todo-spec", area="frontend", priority=30,
            depends_on=(), description="Not started", obsolete=False, superseded_by="",
        )
        mock_git_state = MagicMock()

        # Return different statuses per spec_id
        def fake_status(_root, sid, _path, git_state=None):
            return {"done-spec": "merged", "s1": "in-progress", "todo-spec": "not-started"}[sid]

        with (
            patch(
                "spec_runtime.autopilot_tui.dashboard.load_dashboard_snapshot",
                return_value=snapshot,
            ),
            patch(
                "spec_runtime.spec_metadata.iter_spec_metadata",
                return_value=[merged_record, active_record, waiting_record],
            ),
            patch("spec_runtime.spec_status.collect_git_spec_state", return_value=mock_git_state),
            patch("spec_runtime.spec_status.get_spec_status", side_effect=fake_status),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            client = self._make_client(tmp_path)
            resp = client.get("/api/v1/dashboard", headers=self._auth_headers())

        assert resp.status_code == 200
        data = resp.json()

        # Verify every spec has a status field
        statuses = {s["spec_id"]: s["status"] for s in data["specs"]}
        assert statuses["done-spec"] == "merged"
        assert statuses["s1"] == "in-progress"
        assert statuses["todo-spec"] == "not-started"
        # merged_count must be present for the toggle button label
        assert data["merged_count"] == 2


class TestAppJsRenderDashboardLogic:
    """Structural regression tests: renderDashboard must filter merged specs.

    These tests extract the function body from app.js and verify the conditional
    filtering logic is present — not just string tokens, but the actual control
    flow that hides merged rows when showMerged is false.
    """

    @pytest.fixture()
    def app_js_source(self):
        from pathlib import Path

        js_path = (
            Path(__file__).resolve().parent.parent
            / "src" / "spec_runtime" / "web" / "static" / "app.js"
        )
        return js_path.read_text()

    @staticmethod
    def _extract_function_body(source, func_name):
        """Extract the body of a named JS function from source."""
        import re

        # Match 'function funcName(...) {' and extract the balanced body
        pattern = re.compile(r'function\s+' + re.escape(func_name) + r'\s*\([^)]*\)\s*\{')
        m = pattern.search(source)
        assert m, f"Function {func_name} not found in app.js"
        start = m.end()
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            if source[i] == '{':
                depth += 1
            elif source[i] == '}':
                depth -= 1
            i += 1
        return source[start:i - 1]

    def test_renderdashboard_filters_merged_from_visible_specs(self, app_js_source):
        """renderDashboard must check each spec's status for 'merged' and
        conditionally exclude it from visibleSpecs when showMerged is false.

        A regression that drops this filter would remove the status check
        and cause this test to fail.
        """
        body = self._extract_function_body(app_js_source, "renderDashboard")

        # Must have a conditional that checks for merged status
        assert '"merged"' in body, (
            "renderDashboard must check spec status for 'merged'"
        )
        # Must build a filtered list (visibleSpecs) rather than rendering all specs
        assert "visibleSpecs" in body, (
            "renderDashboard must use a visibleSpecs array to filter merged rows"
        )
        # The merged check must guard whether specs are added to visibleSpecs
        # Pattern: status === "merged" ... showMerged ... visibleSpecs.push
        # (these must appear in order within the specs loop)
        merged_check_idx = body.find('"merged"')
        show_merged_idx = body.find("showMerged", merged_check_idx)
        push_idx = body.find("visibleSpecs.push", merged_check_idx)
        assert merged_check_idx < show_merged_idx < push_idx, (
            "renderDashboard must check 'merged' status, then showMerged flag, "
            "then conditionally push to visibleSpecs — in that order"
        )

    def test_renderdashboard_renders_toggle_button_with_count(self, app_js_source):
        """The toggle button must display the merged count and switch label text."""
        body = self._extract_function_body(app_js_source, "renderDashboard")

        assert "toggle-merged" in body, (
            "renderDashboard must render a toggle-merged action button"
        )
        assert "mergedCount" in body, (
            "renderDashboard must track mergedCount for the toggle label"
        )
        # Toggle label must change based on showMerged state
        assert "Hide merged" in body, "toggle must show 'Hide merged' when visible"
        assert "Show merged" in body, "toggle must show 'Show merged' when hidden"

    def test_renderdashboard_iterates_visible_specs_not_all(self, app_js_source):
        """The spec table rows must iterate visibleSpecs, not data.specs directly."""
        body = self._extract_function_body(app_js_source, "renderDashboard")

        import re
        # After building visibleSpecs, the table row loop must use visibleSpecs
        # Find the table body generation loop
        table_loop = re.search(
            r'for\s*\(\s*var\s+\w+\s*=\s*0\s*;\s*\w+\s*<\s*visibleSpecs\.length',
            body,
        )
        assert table_loop, (
            "renderDashboard must loop over visibleSpecs.length for the spec table, "
            "not data.specs.length — otherwise merged filtering has no effect"
        )


class TestAppJsElapsedTickerLogic:
    """Structural regression tests: elapsed ticker must only advance running rows.

    Extracts the startElapsedTicker function body and verifies the control flow
    that guards against ticking non-running rows.
    """

    @pytest.fixture()
    def app_js_source(self):
        from pathlib import Path

        js_path = (
            Path(__file__).resolve().parent.parent
            / "src" / "spec_runtime" / "web" / "static" / "app.js"
        )
        return js_path.read_text()

    @staticmethod
    def _extract_function_body(source, func_name):
        """Extract the body of a named JS function from source."""
        import re

        pattern = re.compile(r'function\s+' + re.escape(func_name) + r'\s*\([^)]*\)\s*\{')
        m = pattern.search(source)
        assert m, f"Function {func_name} not found in app.js"
        start = m.end()
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            if source[i] == '{':
                depth += 1
            elif source[i] == '}':
                depth -= 1
            i += 1
        return source[start:i - 1]

    def test_ticker_checks_running_status_before_increment(self, app_js_source):
        """startElapsedTicker must check for 'running' badge text and skip
        non-running rows. Removing this guard would tick all rows.
        """
        body = self._extract_function_body(app_js_source, "startElapsedTicker")

        # Must read the status badge text
        assert ".badge" in body, (
            "ticker must query the .badge element to read row status"
        )
        # Must check for "running" string
        assert '"running"' in body, (
            "ticker must compare badge text against 'running'"
        )
        # Must have a continue statement to skip non-running rows
        assert "continue" in body, (
            "ticker must 'continue' past non-running rows"
        )
        # The running check must appear BEFORE incrementElapsed call
        running_idx = body.find('"running"')
        increment_idx = body.find("incrementElapsed")
        assert 0 <= running_idx < increment_idx, (
            "ticker must check for 'running' before calling incrementElapsed"
        )

    def test_ticker_syncs_snapshot_after_increment(self, app_js_source):
        """The elapsed ticker must write back to latestSnapshot.rows to avoid
        stale values when toggleMerged re-renders."""
        body = self._extract_function_body(app_js_source, "startElapsedTicker")

        assert "latestSnapshot" in body, (
            "ticker must reference latestSnapshot to keep it in sync"
        )
        # Must assign the updated value back to the snapshot
        assert ".elapsed" in body and "updated" in body, (
            "ticker must write updated elapsed value back to snapshot"
        )

    def test_ticker_lifecycle_on_view_change(self, app_js_source):
        """Ticker must be started on dashboard render and stopped on view change."""
        assert "startElapsedTicker" in app_js_source
        assert "stopElapsedTicker" in app_js_source
        # stopElapsedTicker must be called inside showSpecDetail to prevent leaks
        body = self._extract_function_body(app_js_source, "showSpecDetail")
        assert "stopElapsedTicker" in body, (
            "showSpecDetail must call stopElapsedTicker to prevent timer leaks"
        )

    def test_dashboard_api_returns_row_status_for_ticker(self, tmp_path):
        """API active-run rows must include status so the ticker can filter by running."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class RunningRow:
            spec_id: str = "s1"
            display_spec_id: str = "s1"
            agent: str = "claude"
            phase: str = "implement"
            retries: str = "1/5"
            elapsed: str = "5m30s"
            status: str = "running"
            branch: str = "code/s1--abc"
            run_id: str = "r1"
            run_mode: str = "spec"
            created_at: str = "2026-01-01"

        @dataclass(frozen=True)
        class FailedRow:
            spec_id: str = "s2"
            display_spec_id: str = "s2"
            agent: str = "codex"
            phase: str = "verify"
            retries: str = "3/5"
            elapsed: str = "12m05s"
            status: str = "failed"
            branch: str = "code/s2--def"
            run_id: str = "r2"
            run_mode: str = "spec"
            created_at: str = "2026-01-01"

        from spec_runtime.autopilot_tui.dashboard import DashboardSnapshot

        snapshot = DashboardSnapshot(
            rows=(RunningRow(), FailedRow()),
            queue=(),
            merged_count=0,
        )

        mock_record = MagicMock(
            spec_id="s1", area="backend", priority=10,
            depends_on=(), description="Test", obsolete=False, superseded_by="",
        )
        mock_git_state = MagicMock()

        with (
            patch(
                "spec_runtime.autopilot_tui.dashboard.load_dashboard_snapshot",
                return_value=snapshot,
            ),
            patch("spec_runtime.spec_metadata.iter_spec_metadata", return_value=[mock_record]),
            patch("spec_runtime.spec_status.collect_git_spec_state", return_value=mock_git_state),
            patch("spec_runtime.spec_status.get_spec_status", return_value="in-progress"),
            patch("spec_runtime.config.load_repo_spec_runtime_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(paths=MagicMock(specs_dir="specs"))
            from starlette.testclient import TestClient

            from spec_runtime.web.server import create_app

            client = TestClient(
                create_app(tmp_path, "test-token"), raise_server_exceptions=False,
            )
            resp = client.get(
                "/api/v1/dashboard",
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code == 200
        rows = resp.json()["rows"]
        # Both rows must have status — the ticker uses this to decide which to tick
        assert rows[0]["status"] == "running"
        assert rows[1]["status"] == "failed"
        # Elapsed must be in the parseable format
        assert rows[0]["elapsed"] == "5m30s"
        assert rows[1]["elapsed"] == "12m05s"
