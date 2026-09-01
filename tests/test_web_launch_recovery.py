"""Dependency-free tests for the web launch ownership handshake."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from spec_runtime.process_supervisor import LifetimeMode, ProcessIdentity, SupervisionToken
from spec_runtime.web.server import (
    _helper_metadata_path,
    _launch_path,
    _ready_path,
    _recover_launch,
    _write_launch_reservation,
    read_supervision_token,
    stop_server,
)


@pytest.fixture(autouse=True)
def _isolated_process_control_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(tmp_path / "process-controls"))


def _token(name: str) -> SupervisionToken:
    return SupervisionToken(
        LifetimeMode.DETACHED,
        ProcessIdentity(123, "helper", "python.exe"),
        1,
        "owner",
        name,
        payload_identity=ProcessIdentity(124, "payload", "python.exe"),
    )


def _reserve(tmp_path, token: SupervisionToken, nonce: str = "starting") -> None:
    helper_path = _helper_metadata_path(tmp_path, token.token)
    helper_path.parent.mkdir(parents=True, exist_ok=True)
    helper_path.write_text(json.dumps(token.to_dict()), encoding="utf-8")
    _write_launch_reservation(
        tmp_path,
        supervision_id=token.token,
        helper_path=helper_path,
        nonce=nonce,
        host="0.0.0.0",
        port=7700,
    )


def test_live_launch_is_occupied_before_ready(tmp_path):
    token = _token("launching-web")
    _reserve(tmp_path, token)
    with (
        patch("spec_runtime.process_supervisor.identity_matches", return_value=True),
        patch("spec_runtime.web.server._wait_for_ready_record", return_value=False),
    ):
        assert _recover_launch(tmp_path, readiness_timeout=0.01) == token
    assert _launch_path(tmp_path).exists()
    assert read_supervision_token(tmp_path) is None


def test_payload_does_not_recover_own_launch(tmp_path):
    token = _token("own-launch")
    _reserve(tmp_path, token, nonce="child-nonce")
    with patch.dict(os.environ, {"SPEC_WEB_READY_NONCE": "child-nonce"}):
        assert _recover_launch(tmp_path) is None


def test_dead_launch_is_cleared_before_retry(tmp_path):
    token = _token("dead-web")
    _reserve(tmp_path, token)
    _ready_path(tmp_path).write_text("stale", encoding="utf-8")
    with patch("spec_runtime.process_supervisor.identity_matches", return_value=False):
        assert _recover_launch(tmp_path) is None
    assert not _launch_path(tmp_path).exists()
    assert not _ready_path(tmp_path).exists()
    assert not _helper_metadata_path(tmp_path, token.token).exists()


def test_stop_uses_live_launch_token(tmp_path):
    token = _token("stopping-launch")
    _reserve(tmp_path, token)
    with (
        patch("spec_runtime.web.server.is_server_running", return_value=(True, 124)),
        patch("spec_runtime.web.server.read_supervision_token", return_value=None),
        patch("spec_runtime.web.server._recover_launch", return_value=token) as recover,
        patch("spec_runtime.process_supervisor.terminate", return_value=True) as terminate,
        patch("spec_runtime.web.server.remove_pid") as remove_pid,
    ):
        assert stop_server(tmp_path) == 0
    recover.assert_called_once_with(tmp_path, readiness_timeout=0.0)
    terminate.assert_called_once_with(token)
    remove_pid.assert_called_once_with(tmp_path)
