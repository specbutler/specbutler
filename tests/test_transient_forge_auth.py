"""Hermetic coverage for forge auth preflight retries and outage context."""

import subprocess
from unittest.mock import call, patch

import pytest

from spec_runtime import orchestrator as orch


def result(code=1, output="not logged in"):
    return subprocess.CompletedProcess(["gh", "auth", "status"], code, "", output)


def test_auth_recovers_without_recording_failure():
    run = orch.RunState(run_id="auth", spec_id="auth", branch="code/auth")
    with (
        patch.object(orch, "run_subprocess", side_effect=[result(), result(0)]) as command,
        patch.object(orch, "_poll_sleep") as sleep,
    ):
        assert orch._check_run_forge_auth(run, "merge") == ""
    assert command.call_count == 2
    sleep.assert_called_once_with(20.0)
    assert run.last_error == run.last_failure_signature == ""


@pytest.mark.parametrize("previous_phase, expected", [("merge", "forge_network"), ("publish", "forge_auth")])
def test_auth_after_network_failure_is_scoped_to_phase(previous_phase, expected):
    run = orch.RunState(
        run_id="auth", spec_id="auth", branch="code/auth",
        last_failure_phase=previous_phase, last_failure_signature="forge_network:previous",
    )
    with (
        patch.object(orch, "run_subprocess", return_value=result()) as command,
        patch.object(orch, "_poll_sleep") as sleep,
    ):
        error = orch._check_run_forge_auth(run, "merge")
    classification = orch.classify_failure(error, phase="merge")
    assert classification.kind == expected
    assert classification.transient == (expected == "forge_network")
    assert command.call_count == 3
    assert sleep.call_args_list == [call(20.0), call(40.0)]


def test_persistent_auth_failure_is_terminal():
    with patch.object(orch, "run_subprocess", return_value=result()), patch.object(orch, "_poll_sleep"):
        error = orch._check_forge_auth()
    assert "gh auth login" in error
    classification = orch.classify_failure(error)
    assert classification.kind == "forge_auth"
    assert not classification.transient


def test_network_output_wins_even_if_later_checks_only_report_auth():
    with (
        patch.object(orch, "run_subprocess", side_effect=[
            result(output="GitHub CLI not authenticated: error connecting to api.github.com"),
            result(), result(),
        ]),
        patch.object(orch, "_poll_sleep"),
    ):
        error = orch._check_forge_auth()
    assert orch.classify_failure(error).kind == "forge_network"
    assert orch.classify_failure(
        "GitHub CLI not authenticated: error connecting to api.github.com"
    ).kind == "forge_network"
