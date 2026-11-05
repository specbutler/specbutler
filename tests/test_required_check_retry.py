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
    checks = MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "") if passes else (False, ERROR)])
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
    assert sleep.call_count == 3
    # The same head cannot spend a second rerun, even after loading durable state.
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    forge.workflow_run.side_effect = None
    forge.workflow_run.return_value = observation()
    orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert forge.rerun_workflow.call_count == 1
    # A newly published head has an independent allowance.
    forge.workflow_run.side_effect = [observation("new"), observation("new", 2, "success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "")]))
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
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "") if passes else (False, ERROR)]))
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


@pytest.fixture
def retry_host(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch", attempts=2, review_changes=1)
    forge = MagicMock()
    forge.rerun_workflow.return_value = ""
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda _: "owner/repo")
    monkeypatch.setattr(orch, "_assert_publication_transition_safe", lambda *a: None)
    monkeypatch.setattr(orch, "_poll_sleep", lambda _: None)
    return run, forge


def reserve(run, status="requested", kind="workflow"):
    event = dict(kind=kind, head_sha="head", workflow_run_id="123", jobs=["browser"], attempt=1, status=status)
    run.required_check_reruns.append(event)
    return event


def test_waits_for_optional_jobs_before_submitting_whole_run(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    forge.workflow_run.side_effect = [observation(status="in_progress"), observation(status="in_progress"),
                                      observation(), observation(attempt=2, conclusion="success")]
    def submit(*a, **kw):
        assert forge.workflow_run.call_count == 3
        assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "submitting"
        return ""
    forge.rerun_workflow.side_effect = submit
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    forge.rerun_workflow.assert_called_once()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_aggregate_failure_cannot_override_green_required_job(retry_host, monkeypatch, tmp_path, conclusion):
    run, forge = retry_host
    after = observation(attempt=2, conclusion=conclusion)
    after["jobs"] = [dict(name="browser", conclusion="success"), dict(name="optional", conclusion=conclusion)]
    forge.workflow_run.side_effect = [observation(), after]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert run.required_check_reruns[0]["status"] == "flaky"


def test_superseded_rerun_waits_for_replacement(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="cancelled"),
                                      observation(attempt=2, conclusion="cancelled")]
    checks = MagicMock(side_effect=[(False, ERROR), (False, ERROR), (False, "Required checks still pending: superseded"), (True, "")])
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", checks)
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    forge.rerun_workflow.assert_called_once()


@pytest.mark.parametrize("stage", ["initial", "poll", "mutation"])
@pytest.mark.parametrize("exc", [RuntimeError("forge unavailable"), OSError("socket closed"),
                                  ValueError("invalid JSON"), subprocess.TimeoutExpired("gh", 1)])
def test_forge_errors_remain_pending_without_repeating_mutation(retry_host, monkeypatch, tmp_path, stage, exc):
    run, forge = retry_host
    forge.workflow_run.side_effect = [exc] if stage == "initial" else [observation(), exc]
    if stage == "mutation":
        forge.workflow_run.side_effect = [observation()]
        forge.rerun_workflow.side_effect = exc
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    # Poll error is observed after an accepted submission on resume.
    if stage == "poll":
        reserve(run, "waiting")
        forge.workflow_run.side_effect = [exc]
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert not ok and orch._required_checks_still_pending(error)
    assert run.attempts == 2 and run.review_changes == 1
    assert forge.rerun_workflow.call_count == int(stage == "mutation")
    if stage == "mutation":
        assert run.required_check_reruns[0]["status"] == "submitting"


@pytest.mark.parametrize("status,advanced,submissions", [
    ("requested", False, 1), ("requested", True, 0), ("submitting", True, 0), ("waiting", True, 0),
])
def test_restart_reconciles_reserved_or_accepted_retry(retry_host, monkeypatch, tmp_path, status, advanced, submissions):
    run, forge = retry_host
    reserve(run, status)
    run.save(tmp_path)
    run = orch.RunState.load(tmp_path, "run")
    forge.workflow_run.side_effect = ([observation()] if not advanced else []) + [observation(attempt=2, conclusion="success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR)] * (2 if submissions else 1) + [(True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert forge.rerun_workflow.call_count == submissions
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "flaky"


def test_ambiguous_submission_is_observed_without_duplicate(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    reserve(run, "submitting")
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    forge.rerun_workflow.assert_not_called()


@pytest.mark.parametrize("finishes", [True, False])
def test_retry_refreshes_enclosing_deadline(retry_host, monkeypatch, tmp_path, finishes):
    run, _ = retry_host
    now = [9.9]
    monkeypatch.setattr(orch.time, "time", lambda: now[0])
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 10)
    def check(*a, **kw):
        reserve(run, "flaky" if finishes else "waiting")
        now[0] += 20
        return finishes, "" if finishes else "Required checks still pending: rerun"
    monkeypatch.setattr(orch, "_merge_required_checks", check)
    ok, _, deadline = orch._merge_required_checks_with_deadline(run, tmp_path, 1, repo_slug="owner/repo",
                                                              expected_head_sha="head", deadline=10)
    assert ok == finishes
    assert deadline == 39.9


def test_auto_merge_can_finish_after_original_deadline(retry_host, monkeypatch, tmp_path):
    from spec_runtime.forge import MergeResult

    run, forge = retry_host
    now = [0.0]
    monkeypatch.setattr(orch.time, "time", lambda: now[0])
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 10)
    forge.merge_pr.return_value = MergeResult(True)
    states = iter(["OPEN", "MERGED"])
    def lookup(*a, **kw):
        state = next(states)
        if state == "OPEN":
            now[0] = 9.9
        return dict(state=state, headRefOid="head")
    monkeypatch.setattr(orch, "_find_pr_for_branch", lookup)
    def submit(*a, **kw):
        now[0] += 20
        return ""
    forge.rerun_workflow.side_effect = submit
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (True, "")]))
    assert orch._try_auto_merge(run, tmp_path, 1, expected_head_sha="head") == "merged"
    assert run.attempts == 2 and run.review_changes == 1


def test_auto_merge_poll_error_returns_through_cleanup(retry_host, monkeypatch, tmp_path):
    from spec_runtime.forge import MergeResult

    run, forge = retry_host
    reserve(run, "waiting")
    forge.merge_pr.return_value = MergeResult(True)
    forge.workflow_run.side_effect = RuntimeError("forge unavailable")
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    monkeypatch.setattr(orch, "_restore_approved_local_review_status_if_pending", lambda *a, **kw: (False, ""))
    monkeypatch.setattr(orch, "_find_pr_for_branch", MagicMock(side_effect=[dict(state="OPEN"), dict(state="CLOSED")]))
    disable = MagicMock(return_value="")
    monkeypatch.setattr(orch, "_disable_pr_auto_merge", disable)
    assert orch._try_auto_merge(run, tmp_path, 1, expected_head_sha="head") == "failed"
    assert "closed" in run.last_error
    disable.assert_called_once()


@pytest.mark.parametrize("pending_first", [True, False])
def test_rerun_on_last_merge_race_does_not_spend_retry_cap(retry_host, monkeypatch, tmp_path, pending_first):
    from spec_runtime.forge import MergeResult

    run, forge = retry_host
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (dict(headRefOid="head"), None))
    monkeypatch.setattr(orch, "_revalidate_readiness_pr_head", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_wait_for_pr_merged", lambda *a, **kw: (True, ""))
    results = [MergeResult(False, "base branch policy")]
    if pending_first:
        results.append(MergeResult(False, "base branch policy"))
    results.append(MergeResult(True))
    forge.merge_pr.side_effect = results
    calls = [0]
    def checks(*a, **kw):
        calls[0] += 1
        if calls[0] == 2 and pending_first:
            reserve(run, "waiting")
            return False, "Required checks still pending: rerun"
        if calls[0] in ({4} if pending_first else {2}):
            if not pending_first:
                reserve(run, "waiting")
            run.required_check_reruns[0]["status"] = "flaky"
        return True, ""
    monkeypatch.setattr(orch, "_merge_required_checks", checks)
    assert orch._manual_merge(run, tmp_path, tmp_path, "branch", 1, "head", {}, prior_merge_attempts=2) == "passed"
    assert forge.merge_pr.call_count == len(results)
    assert run.attempts == 2 and run.review_changes == 1


@pytest.mark.parametrize("kind", ["queue", "workflow"])
@pytest.mark.parametrize("status", ["requested", "submitting", "waiting"])
def test_auto_merge_terminal_recovery_closes_events(retry_host, monkeypatch, tmp_path, kind, status):
    from spec_runtime.forge import MergeResult

    run, forge = retry_host
    reserve(run, status, kind)
    run.save(tmp_path)
    run = orch.RunState.load(tmp_path, "run")
    forge.merge_pr.return_value = MergeResult(True)
    monkeypatch.setattr(orch, "_find_pr_for_branch", lambda *a, **kw: dict(state="MERGED", headRefOid="head"))
    assert orch._try_auto_merge(run, tmp_path, 1, expected_head_sha="head") == "merged"
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == (
        "released" if kind == "workflow" and status == "requested" else "flaky"
    )
    forge.workflow_run.assert_not_called()
    forge.rerun_workflow.assert_not_called()


def test_green_pr_checks_do_not_finalize_queue_retry(retry_host, monkeypatch, tmp_path):
    run, _ = retry_host
    reserve(run, "waiting", "queue")
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (True, ""))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert run.required_check_reruns[0]["status"] == "waiting"


def test_post_sync_second_failure_includes_excerpts(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    pr = dict(headRefOid="old", mergeStateStatus="BEHIND")
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (pr, pr))
    monkeypatch.setattr(orch, "_merge_origin_master", lambda *a, **kw: orch.MergeOriginMasterResult(status="success"))
    monkeypatch.setattr(orch, "_resolve_git_ref_sha", lambda *a: "base")
    monkeypatch.setattr(orch, "_head_sha", lambda *a: "head")
    monkeypatch.setattr(orch, "_mark_local_review_for_rerun_after_sync", lambda *a, **kw: False)
    monkeypatch.setattr(orch, "host_publication_git_environment", lambda: {})
    monkeypatch.setattr(orch, "run_subprocess", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(True, ""), (False, ERROR), (False, ERROR), (False, ERROR)]))
    forge.workflow_run.side_effect = [observation(), observation(attempt=2)]
    excerpt = MagicMock(return_value=ERROR + "\nBrowser timeout excerpt")
    monkeypatch.setattr(orch, "_augment_required_checks_error", excerpt)
    assert orch._manual_merge(run, tmp_path, tmp_path, "branch", 1, "old", pr) == "failed"
    assert "Browser timeout excerpt" in run.last_error
    assert "Checks failed after syncing" in run.last_error
    assert run.required_check_reruns[0]["head_sha"] == "head"
    excerpt.assert_called_once()


@pytest.mark.parametrize("payload", [[], None, {}, {**observation(), "attempt": "2"},
                                    {**observation(), "jobs": [None]}])
def test_invalid_workflow_payload_is_a_handled_query_error(tmp_path, payload):
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    with pytest.raises(ValueError):
        GitHubForge(run_fn=runner).workflow_run("123", cwd=tmp_path)


def test_stale_job_ids_wait_for_current_required_results(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    after = observation(attempt=2, conclusion="failure")
    after["jobs"] = [dict(name="browser", conclusion="failure", databaseId=999)]
    forge.workflow_run.side_effect = [observation(), after, after]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert run.required_check_reruns[0]["status"] == "flaky"


def test_external_failure_after_actions_retry_is_reported(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success")]
    external = "Required checks failing: external (https://example.org/build/9)"
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (False, ERROR), (False, external)]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (False, external)
    assert run.required_check_reruns[0]["status"] == "failed"


def test_new_head_finalizes_old_active_events_as_superseded(retry_host, tmp_path):
    run, _ = retry_host
    reserve(run, "waiting")
    orch._finish_required_check_reruns(run, tmp_path, "new-head")
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "superseded"


@pytest.mark.parametrize("via_fence", [True, False])
@pytest.mark.parametrize("kind", ["workflow", "queue"])
def test_phase_terminal_recovery_persists_flake(retry_host, monkeypatch, tmp_path, via_fence, kind):
    run, _ = retry_host
    reserve(run, "waiting", kind)
    run.save(tmp_path)
    run = orch.RunState.load(tmp_path, "run")
    monkeypatch.setattr(orch, "host_publication_git_environment", lambda: {})
    monkeypatch.setattr(orch, "_check_run_forge_auth", lambda *a: "")
    monkeypatch.setattr(orch, "refresh_merge_completion_state", lambda *a, **kw: ("fetch", "", "base"))
    monkeypatch.setattr(orch, "is_spec_merged", lambda *a, **kw: via_fence)
    monkeypatch.setattr(orch, "_find_pr_for_branch", lambda *a, **kw: dict(number=1, state="MERGED", headRefOid="head"))
    monkeypatch.setattr(orch, "_head_sha", lambda *a: "head")
    monkeypatch.setattr(orch, "run_subprocess", lambda *a, **kw: subprocess.CompletedProcess([], 0, orch.merge_tag_name(run.spec_id), ""))
    # Stop at unrelated post-merge work; recovery must already be durable.
    def post_merge(*a, **kw):
        assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "flaky"
        raise StopIteration("post-merge reached")
    monkeypatch.setattr(orch, "_best_effort_post_merge_local_sync", post_merge)
    if via_fence:
        assert orch.phase_merge(run, tmp_path) == "passed"
    else:
        with pytest.raises(StopIteration, match="post-merge reached"):
            orch.phase_merge(run, tmp_path)
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "flaky"


def test_completion_fence_without_pr_attribution_is_terminal_not_flaky(retry_host, monkeypatch, tmp_path):
    run, _ = retry_host
    reserve(run, "waiting")
    monkeypatch.setattr(orch, "host_publication_git_environment", lambda: {})
    monkeypatch.setattr(orch, "_check_run_forge_auth", lambda *a: "")
    monkeypatch.setattr(orch, "refresh_merge_completion_state", lambda *a, **kw: ("fetch", "", "base"))
    monkeypatch.setattr(orch, "is_spec_merged", lambda *a, **kw: True)
    monkeypatch.setattr(orch, "_find_pr_for_branch", MagicMock(side_effect=RuntimeError("forge unavailable")))
    assert orch.phase_merge(run, tmp_path) == "passed"
    event = orch.RunState.load(tmp_path, "run").required_check_reruns[0]
    assert event["status"] == "completed"
    assert "attribution unavailable" in event["completion_note"]


def test_workflow_only_link_does_not_match_an_optional_job(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    error = "Required checks failing: browser (https://github.com/owner/repo/actions/runs/123)"
    after = observation(attempt=2, conclusion="failure")
    after["jobs"] = [dict(name="optional", conclusion="failure"), dict(name="browser", conclusion="success")]
    forge.workflow_run.side_effect = [observation(), after, after]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, error), (False, error), (False, error), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    assert run.required_check_reruns[0]["status"] == "flaky"


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("current", ["replacement_pending", "replacement_failed", "cancelled", "stale_failure", "green"])
def test_unused_superseded_reservation_keeps_allowance(retry_host, monkeypatch, tmp_path, restart, current):
    run, forge = retry_host
    replacement_url = URL.replace("/123/", "/124/")
    replacement_error = f"Required checks failing: browser ({replacement_url})"
    if restart:
        reserve(run)
        run.save(tmp_path)
        run = orch.RunState.load(tmp_path, "run")
    cancelled = observation(conclusion="cancelled")
    cancelled["jobs"][0]["databaseId"] = 456
    forge.workflow_run.side_effect = ([] if restart else [observation(status="in_progress")]) + [cancelled]
    forge.check_cancellation_reason.return_value = "Cancelled since a higher priority waiting request exists"
    responses = {
        "replacement_pending": (False, f"Required checks still pending: browser ({replacement_url})"),
        "replacement_failed": (False, replacement_error),
        "cancelled": (False, f"Required checks still pending: browser ({URL})"),
        "stale_failure": (False, ERROR),
        "green": (True, ""),
    }
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), responses[current]]))
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head")
    assert ok == (current == "green")
    assert ok or orch._required_checks_still_pending(error)
    forge.rerun_workflow.assert_not_called()
    run = orch.RunState.load(tmp_path, "run")
    assert run.required_check_reruns[0]["status"] == "released"
    assert run.attempts == 2 and run.review_changes == 1
    # The replacement on the SAME head still owns its one whole-run rerun.
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[
        (False, replacement_error), (False, replacement_error), (True, ""),
    ]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head") == (True, "")
    forge.rerun_workflow.assert_called_once_with("124", cwd=tmp_path)
    assert [e["status"] for e in run.required_check_reruns] == ["released", "flaky"]


@pytest.mark.parametrize("reason,rerun", [
    ("A higher priority waiting request exists", False),
    ("Cancelled by concurrency", False),
    ("The job was not acquired by Runner", True),
])
def test_delayed_cancellation_uses_live_required_check_classification(retry_host, tmp_path, reason, rerun):
    run, forge = retry_host
    failed = CheckRun("browser", "FAILURE", URL, "fail")
    cancelled_check = CheckRun("browser", "CANCELLED", URL, "cancel")
    forge.get_required_checks.side_effect = [[failed], [cancelled_check]] + ([[CheckRun("browser", "SUCCESS", URL, "pass")]] if rerun else [])
    cancelled = observation(conclusion="cancelled")
    cancelled["jobs"][0]["databaseId"] = 456
    forge.workflow_run.side_effect = [observation(status="in_progress"), cancelled] + ([observation(attempt=2, conclusion="success")] if rerun else [])
    forge.check_cancellation_reason.return_value = reason
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head")
    assert ok == rerun
    assert ok or orch._required_checks_still_pending(error)
    assert forge.rerun_workflow.call_count == int(rerun)
    assert run.required_check_reruns[0]["status"] == ("flaky" if rerun else "released")


@pytest.mark.parametrize("response", [RuntimeError("forge unavailable"), (False, "Required checks still pending: unable to query required checks")])
def test_pre_submission_query_failure_preserves_unused_reservation(retry_host, monkeypatch, tmp_path, response):
    run, forge = retry_host
    forge.workflow_run.return_value = observation()
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), response]))
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert not ok and orch._required_checks_still_pending(error)
    forge.rerun_workflow.assert_not_called()
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "requested"


def test_later_workflow_supersession_does_not_release_submitted_retry(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    url2 = URL.replace("/123/", "/124/")
    initial = ERROR + f", other ({url2})"
    first = observation()
    second = observation(conclusion="cancelled")
    second["jobs"][0]["databaseId"] = 789
    forge.check_cancellation_reason.return_value = "Cancelled by concurrency"
    forge.workflow_run.side_effect = [first, second]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[
        (False, initial), (False, initial), (False, "Required checks still pending: replacement"),
    ]))
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert not ok and orch._required_checks_still_pending(error)
    forge.rerun_workflow.assert_called_once_with("123", cwd=tmp_path)
    assert [e["status"] for e in orch.RunState.load(tmp_path, "run").required_check_reruns] == ["waiting", "released"]


def test_pre_submission_annotation_outage_does_not_spend_allowance(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    cancelled = observation(conclusion="cancelled")
    cancelled["jobs"][0]["databaseId"] = 456
    forge.workflow_run.return_value = cancelled
    forge.check_cancellation_reason.side_effect = RuntimeError("forge unavailable")
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, ERROR))
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo")
    assert not ok and orch._required_checks_still_pending(error)
    forge.rerun_workflow.assert_not_called()
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "requested"


def test_pre_submission_green_result_needs_no_cancellation_annotation(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    cancelled = observation(conclusion="cancelled")
    cancelled["jobs"][0]["databaseId"] = 456
    forge.workflow_run.return_value = cancelled
    forge.check_cancellation_reason.side_effect = RuntimeError("forge unavailable")
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, ERROR), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo") == (True, "")
    forge.check_cancellation_reason.assert_not_called()
    forge.rerun_workflow.assert_not_called()
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "released"


@pytest.mark.parametrize("restart", [False, True])
def test_two_workflows_each_rerun_while_first_is_pending(retry_host, monkeypatch, tmp_path, restart):
    from spec_runtime.forge import MergeResult

    run, forge = retry_host
    urls = {"123": URL, "124": URL.replace("/123/", "/124/").replace("/456", "/789")}
    names = {"123": "browser", "124": "other"}
    submitted = set()
    finished = [False]
    if restart:
        for rid in urls:
            event = reserve(run)
            event.update(workflow_run_id=rid, jobs=[names[rid]])
        run.save(tmp_path)
        run = orch.RunState.load(tmp_path, "run")
    def checks(*a, **kw):
        return [CheckRun(names[rid], "SUCCESS" if finished[0] else "PENDING" if rid in submitted else "FAILURE",
                         url, "pass" if finished[0] else "pending" if rid in submitted else "fail")
                for rid, url in urls.items()]
    def workflow(rid, **kw):
        info = observation(attempt=2 if rid in submitted else 1,
                           conclusion="success" if finished[0] else "failure",
                           status="completed" if finished[0] or rid not in submitted else "in_progress")
        info["jobs"][0]["name"] = names[rid]
        return info
    def rerun(rid, **kw):
        assert rid not in submitted
        if rid == "124":
            # The real aggregate helper now reports pending, hiding the second
            # workflow's failure; its individual required check still says fail.
            assert checks()[0].bucket == "pending"
            assert checks()[1].bucket == "fail"
        submitted.add(rid)
        return ""
    def poll(_):
        assert submitted == {"123", "124"}
        finished[0] = True
    forge.get_required_checks.side_effect = checks
    forge.workflow_run.side_effect = workflow
    forge.rerun_workflow.side_effect = rerun
    forge.merge_pr.return_value = MergeResult(True)
    monkeypatch.setattr(orch, "_poll_sleep", poll)
    pr = dict(headRefOid="head", mergeStateStatus="CLEAN")
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (pr, pr))
    monkeypatch.setattr(orch, "_revalidate_readiness_pr_head", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_wait_for_pr_merged", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(orch, "_augment_required_checks_error", MagicMock(side_effect=AssertionError("CI failure escaped")))
    assert orch._manual_merge(run, tmp_path, tmp_path, "branch", 1, "head", pr) == "passed"
    assert [call.args[0] for call in forge.rerun_workflow.call_args_list] == ["123", "124"]
    assert run.attempts == 2 and run.review_changes == 1 and not run.last_error
    assert [(e["workflow_run_id"], e["status"]) for e in run.required_check_reruns] == [("123", "flaky"), ("124", "flaky")]
    forge.merge_pr.assert_called_once()


def test_another_workflow_keeps_its_allowance_after_first_finished(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    reserve(run, "flaky")
    error = ERROR.replace("/123/", "/124/")
    forge.workflow_run.side_effect = [observation(), observation(attempt=2, conclusion="success")]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[(False, error), (False, error), (True, "")]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head") == (True, "")
    forge.rerun_workflow.assert_called_once_with("124", cwd=tmp_path)
    assert [e["status"] for e in run.required_check_reruns] == ["flaky", "flaky"]


def test_new_failure_during_another_retry_gets_its_own_reservation(retry_host, monkeypatch, tmp_path):
    run, forge = retry_host
    other_error = ERROR.replace("/123/", "/124/")
    submitted = set()
    def info(rid, **kw):
        return observation(attempt=2 if rid in submitted else 1,
                           conclusion="success" if rid in submitted else "failure")
    def rerun(rid, **kw):
        assert rid not in submitted
        submitted.add(rid)
        return ""
    forge.workflow_run.side_effect = info
    forge.rerun_workflow.side_effect = rerun
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", MagicMock(side_effect=[
        (False, ERROR), (False, ERROR), (False, other_error), (False, other_error), (True, ""),
    ]))
    assert orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head") == (True, "")
    assert submitted == {"123", "124"} and forge.rerun_workflow.call_count == 2
    assert [e["status"] for e in run.required_check_reruns] == ["flaky", "flaky"]


@pytest.mark.parametrize("outcome", ["passed", "replacement", "unrelated_pending", "own_pending"])
def test_reservation_release_requires_individual_workflow_evidence(retry_host, monkeypatch, tmp_path, outcome):
    run, forge = retry_host
    reserve(run)
    own = observation()
    own["workflowDatabaseId"] = 10
    other_url = URL.replace("/123/", "/124/")
    other = observation(status="in_progress")
    other["workflowDatabaseId"] = 10 if outcome == "replacement" else 20
    forge.workflow_run.side_effect = lambda rid, **kw: own if rid == "123" else other
    own_checks = [CheckRun("browser", "SUCCESS" if outcome == "passed" else "PENDING", URL,
                           "pass" if outcome == "passed" else "pending")]
    forge.get_required_checks.return_value = (own_checks if outcome in {"passed", "own_pending"} else []) + [
        CheckRun("other", "PENDING", other_url, "pending"),
    ]
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (False, "Required checks still pending: other"))
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    ok, error = orch._merge_required_checks(run, tmp_path, 1, repo_slug="owner/repo", expected_head_sha="head")
    assert not ok and orch._required_checks_still_pending(error)
    assert run.required_check_reruns[0]["status"] == ("released" if outcome in {"passed", "replacement"} else "requested")
    forge.rerun_workflow.assert_not_called()
