"""Host reruns are independent of implementation and review budgets."""
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.forge import CheckRun, GitHubForge

URL = "https://github.com/owner/repo/actions/runs/123/job/456"
ERROR = f"Required checks failing: browser ({URL})"


def observation(head="head", attempt=1, conclusion="failure", status="completed"):
    return dict(headSha=head, attempt=attempt, conclusion=conclusion, status=status,
                jobs=[dict(name="browser", conclusion=conclusion)])


@pytest.mark.parametrize("passes", [True, False])
def test_rerun_waits_and_preserves_budgets(monkeypatch, tmp_path, passes):
    run = orch.RunState("run", "spec", "branch", attempts=2, review_changes=1)
    forge = MagicMock()
    forge.rerun_workflow.return_value = ""
    forge.workflow_run.side_effect = [observation(), observation(),
        observation(attempt=2, status="in_progress"),
        observation(attempt=2, conclusion="success" if passes else "failure")]
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    checks = MagicMock(side_effect=[(False, ERROR), (True, "")])
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", checks)
    sleep = MagicMock()
    monkeypatch.setattr(orch, "_poll_sleep", sleep)
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert ok == passes
    assert error == ("" if passes else ERROR)
    assert run.attempts == 2 and run.review_changes == 1
    assert run.required_check_reruns[0]["jobs"] == ["browser"]
    assert run.required_check_reruns[0]["status"] == ("flaky" if passes else "failed")
    forge.rerun_workflow.assert_called_once_with("123", cwd=tmp_path)
    assert sleep.call_count == 2
    # The same head cannot spend a second rerun, even after loading durable state.
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    forge.workflow_run.side_effect = None
    forge.workflow_run.return_value = observation()
    orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert forge.rerun_workflow.call_count == 1
    # A newly published head has an independent allowance.
    forge.workflow_run.side_effect = [observation("new"), observation("new", 2, "success")]
    orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert forge.rerun_workflow.call_count == 2


@pytest.mark.parametrize("reason,failing", [
    ("The job was not acquired by Runner", True),
    ("Canceling since a higher priority waiting request exists", False),
    ("Cancelled by concurrency", False),
])
def test_cancellation_classification(monkeypatch, tmp_path, reason, failing):
    forge = MagicMock()
    forge.get_required_checks.return_value = [CheckRun("macOS", "CANCELLED", URL, "cancel")]
    forge.check_cancellation_reason.return_value = reason
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    ok, error = orch._required_checks_green(tmp_path, 1)
    assert not ok
    assert orch._is_required_checks_failure(error) == failing
    assert orch._required_checks_still_pending(error) != failing
    forge.rerun_workflow.assert_not_called()


def test_whole_workflow_rerun_command(tmp_path):
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    assert GitHubForge(run_fn=runner).rerun_workflow("123", cwd=tmp_path) == ""
    assert runner.call_args.args[0] == ["gh", "run", "rerun", "123"]


def test_cancellation_reads_job_check_annotations(tmp_path):
    runner = MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, json.dumps({"check_run_url": "https://api.github.com/repos/o/r/check-runs/789"}), ""),
        subprocess.CompletedProcess([], 0, json.dumps([{"message": "not acquired by Runner"}]), ""),
    ])
    forge = GitHubForge(run_fn=runner, repo_slug="o/r")
    assert forge.check_cancellation_reason(CheckRun("mac", "CANCELLED", URL), cwd=tmp_path) == "not acquired by Runner"
    assert runner.call_args_list[0].args[0][-1] == "repos/o/r/actions/jobs/456"
    assert runner.call_args_list[1].args[0][-1].endswith("/789/annotations")


@pytest.mark.parametrize("passes", [True, False])
def test_manual_merge_after_rerun_or_reports_excerpts(monkeypatch, tmp_path, passes):
    from spec_runtime.forge import MergeResult

    run = orch.RunState("run", "spec", "branch", attempts=2, review_changes=1)
    forge = MagicMock()
    forge.rerun_workflow.return_value = ""
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success" if passes else "failure")]
    forge.merge_pr.return_value = MergeResult(True)
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda _: "owner/repo")
    monkeypatch.setattr(orch, "_assert_publication_transition_safe", lambda *a: None)
    monkeypatch.setattr(orch, "_revalidate_readiness_pr_head", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_wait_for_pr_merged", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(orch, "_poll_sleep", lambda _: None)
    pr = dict(headRefOid="head", mergeStateStatus="CLEAN")
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (pr, pr))
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (True, "")]))
    excerpts = MagicMock(return_value=ERROR + "\nBrowser timeout excerpt")
    monkeypatch.setattr(orch, "_augment_required_checks_error", excerpts)
    result = orch._manual_merge(run, tmp_path, tmp_path, "branch", 1, "head", pr)
    assert result == ("passed" if passes else "failed")
    assert run.attempts == 2 and run.review_changes == 1
    assert forge.merge_pr.call_count == int(passes)
    if not passes:
        assert "Browser timeout excerpt" in run.last_error
        excerpts.assert_called_once()
    loaded = orch.RunState.load(tmp_path, "run")
    assert loaded.required_check_reruns == run.required_check_reruns


def test_restart_closes_flaky_event_when_checks_already_green(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    run.required_check_reruns.append(dict(kind="workflow", head_sha="head", workflow_run_id="123",
                                         jobs=["browser"], attempt=1, status="waiting"))
    run.save(tmp_path)
    run = orch.RunState.load(tmp_path, "run")
    forge = MagicMock()
    forge.workflow_run.return_value = observation(attempt=2, conclusion="success")
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (True, ""))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "flaky"
    forge.rerun_workflow.assert_not_called()


def test_restart_resumes_wait_without_second_mutation(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    run.required_check_reruns.append(dict(kind="workflow", head_sha="head", workflow_run_id="123",
                                         jobs=["browser"], attempt=1, status="waiting"))
    forge = MagicMock()
    forge.workflow_run.return_value = observation(attempt=2, conclusion="success")
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert run.required_check_reruns[0]["status"] == "flaky"
    forge.rerun_workflow.assert_not_called()


def test_stale_workflow_head_waits_without_spending_rerun(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    forge = MagicMock()
    forge.workflow_run.return_value = observation("old")
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="new")
    assert not ok and orch._required_checks_still_pending(error)
    assert not run.required_check_reruns
    forge.rerun_workflow.assert_not_called()


def test_external_check_keeps_existing_failure_path(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    forge = MagicMock()
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    error = ERROR + ", external (https://example.org/build/9)"
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, error))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (False, error)
    forge.rerun_workflow.assert_not_called()


def test_rerun_wait_timeout_keeps_event_for_resume(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    forge = MagicMock()
    forge.workflow_run.return_value = observation()
    forge.rerun_workflow.return_value = ""
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert not ok and orch._required_checks_still_pending(error)
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "waiting"
    assert run.attempts == run.review_changes == 0
