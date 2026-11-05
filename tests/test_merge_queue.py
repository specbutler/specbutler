"""Hermetic merge-queue adapter, lifecycle and diagnostic contracts."""

import json
import re
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from spec_runtime import doctor
from spec_runtime import orchestrator as orch
from spec_runtime.config import MergeConfig, SpecConfigError, SpecRuntimeConfig, load_spec_runtime_config
from spec_runtime.forge import GitHubForge, MergeResult


def response(payload, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, json.dumps(payload), stderr)


@pytest.mark.parametrize("queued", [True, False])
def test_detection_handles_classic_or_ruleset_queue(tmp_path, queued):
    runner = MagicMock(return_value=response({"data": {"repository": {"mergeQueue": {"id": "q"} if queued else None}}}))
    forge = GitHubForge(run_fn=runner, repo_slug="owner/repo")
    assert forge.merge_queue_required("release/next", cwd=tmp_path) == queued
    assert 'mergeQueue(branch:"release/next")' in runner.call_args.args[0][4]


def test_unknown_detection_fails_closed(tmp_path):
    forge = GitHubForge(run_fn=lambda *a, **kw: response({"errors": [{"message": "denied"}]}))
    with pytest.raises(RuntimeError, match="Could not query"):
        forge.merge_queue_required("main", cwd=tmp_path)


@pytest.mark.parametrize("mode,expected", [("on", True), ("off", False), ("auto", True)])
def test_override_and_run_cache(monkeypatch, tmp_path, mode, expected):
    run = orch.RunState("run", "spec", "branch")
    forge = MagicMock()
    forge.merge_queue_required.return_value = True
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda _: "owner/repo")
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, merge=MergeConfig(mode)))
    assert orch._run_uses_merge_queue(run, tmp_path) is expected
    assert orch._run_uses_merge_queue(run, tmp_path) is expected
    assert forge.merge_queue_required.call_count == (1 if mode == "auto" else 0)


def test_queue_config_validation(tmp_path):
    path = tmp_path / ".spec.toml"
    path.write_text('[merge]\nqueue = "invalid"\n', encoding="utf-8")
    with pytest.raises(SpecConfigError, match="merge.queue"):
        load_spec_runtime_config(config_path=path)


def queue_status(*, entry=None, event=None, state="OPEN", head="head"):
    return dict(id="PR", state=state, headRefOid=head, mergeQueueEntry=entry,
                timelineItems={"nodes": [event] if event else []})


@pytest.fixture
def queued(monkeypatch, tmp_path):
    run = orch.RunState("run", "spec", "branch")
    forge = MagicMock()
    forge.merge_pr.return_value = MergeResult(True)
    forge.cancel_merge_queue.return_value = ""
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda _: "owner/repo")
    monkeypatch.setattr(orch, "_assert_publication_transition_safe", lambda *a: None)
    monkeypatch.setattr(orch, "_revalidate_readiness_pr_head", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_poll_sleep", lambda _: None)
    return run, forge


def test_queue_waits_through_behind_and_merges(queued, tmp_path, monkeypatch):
    run, forge = queued
    forge.merge_queue_status.side_effect = [
        queue_status(),
        dict(queue_status(entry={"id": "q", "headCommit": {"oid": "group"}}), mergeStateStatus="BEHIND"),
        queue_status(state="MERGED"),
    ]
    manual = MagicMock(side_effect=AssertionError("must not synchronize base"))
    monkeypatch.setattr(orch, "_manual_merge", manual)
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "merged"
    forge.merge_pr.assert_called_once_with(12, method="squash", auto=True, expected_head_sha="head", cwd=tmp_path)
    forge.cancel_merge_queue.assert_not_called()
    manual.assert_not_called()


@pytest.mark.parametrize("reason,expected", [
    ("merge conflict with base", "Merge conflict"),
    ("required check failed", "required checks failing:"),
])
def test_removal_routes_to_existing_retry_policy(queued, tmp_path, reason, expected):
    run, forge = queued
    run.required_check_reruns.append(dict(kind="queue", head_sha="head", status="failed"))
    forge.merge_group_failure_excerpt.return_value = "group workflow failure excerpt"
    forge.merge_queue_status.side_effect = [
        queue_status(),
        queue_status(entry={"id": "q", "headCommit": {"oid": "group"}}),
        queue_status(event={"id": "new", "reason": reason}),
    ]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert expected in run.last_error
    if "check" in reason:
        assert "group workflow failure excerpt" in run.last_error
        args, kwargs = forge.merge_group_failure_excerpt.call_args
        assert args == ("group",)
        assert kwargs["pr_number"] == 12
        assert kwargs["base_branch"] == orch.PR_BASE_BRANCH
        assert kwargs["queued_at"]
        assert kwargs["cwd"] == tmp_path
        assert orch._is_required_checks_failure(run.last_error)
    else:
        assert orch._is_merge_conflict(run.last_error)
    forge.cancel_merge_queue.assert_called_once()


@pytest.mark.parametrize("reason", ["status check timeout", "removed by operator"])
def test_transient_removals_are_bounded_per_run(queued, tmp_path, reason):
    run, forge = queued
    statuses = []
    for index in range(3):
        statuses.extend([
            queue_status(event={"id": str(index)}),
            queue_status(event={"id": str(index + 1), "reason": reason}),
        ])
    forge.merge_queue_status.side_effect = statuses
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert "exhausted 3 attempts" in run.last_error
    assert run.merge_queue_retries == 3
    assert forge.merge_pr.call_count == 3
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert forge.merge_pr.call_count == 3


def test_queue_poll_timeout_is_bounded(queued, tmp_path, monkeypatch):
    run, forge = queued
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 0)
    forge.merge_queue_status.return_value = queue_status(entry={"id": "q"})
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert run.merge_queue_retries == 3


def test_stale_removal_event_is_ignored(queued, tmp_path):
    run, forge = queued
    status = queue_status(event={"id": "old", "reason": "checks failed"})
    forge.merge_queue_status.side_effect = [status, status, queue_status(state="MERGED")]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "merged"


def test_failed_logs_are_from_exact_group_commit(tmp_path):
    runner = MagicMock(side_effect=[
        response([{"databaseId": 8, "conclusion": "failure", "url": "run-url"}]),
        subprocess.CompletedProcess([], 0, "failed test details", ""),
    ])
    excerpt = GitHubForge(run_fn=runner).merge_group_failure_excerpt("group-sha", cwd=tmp_path)
    assert "failed test details" in excerpt
    assert "--event" in runner.call_args_list[0].args[0]
    assert "merge_group" in runner.call_args_list[0].args[0]
    assert "group-sha" in runner.call_args_list[0].args[0]


def test_cancel_dequeues_before_disabling_auto(tmp_path, monkeypatch):
    runner = MagicMock(side_effect=[response({"data": {"dequeuePullRequest": {}}}), response({})])
    forge = GitHubForge(run_fn=runner)
    monkeypatch.setattr(forge, "merge_queue_status", lambda *a, **kw: dict(
        queue_status(entry={"id": "q"}), autoMergeRequest={"enabledAt": "now"}))
    assert forge.cancel_merge_queue(12, cwd=tmp_path) == ""
    assert "dequeuePullRequest" in runner.call_args_list[0].args[0][-1]
    assert "--disable-auto" in runner.call_args_list[1].args[0]


@pytest.mark.parametrize("queued,contexts,strict,expected", [
    (True, ["ci"], False, "not required on PR heads"),
    (True, ["ci", "review-decision-gate"], False, "also applies inside"),
    (False, ["ci"], True, "Up-to-date PRs"),
])
def test_doctor_advises(tmp_path, monkeypatch, queued, contexts, strict, expected):
    from spec_runtime import autopilot

    monkeypatch.setattr(autopilot, "compute_autopilot_concurrency", lambda *a, **kw: SimpleNamespace(cap=3))
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\non:\n  pull_request:\njobs:\n  ci:\n    runs-on: ubuntu-latest\n", encoding="utf-8")
    forge = MagicMock()
    forge.branch_merge_policy.return_value = dict(queued=queued, contexts=contexts, strict=strict, workflows=[])
    checks = doctor._merge_queue_checks(tmp_path, SpecRuntimeConfig(), forge)
    assert any(expected in check.detail and check.status == "warning" for check in checks)
    assert not any(check.blocker for check in checks)
    if queued:
        assert any("merge_group missing" in check.detail for check in checks)


def test_repo_workflows_are_queue_ready():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("ci", "spec-pr-policy", "codex-review"):
        data = yaml.safe_load((root / ".github/workflows" / (name + ".yml")).read_text(encoding="utf-8"))
        assert "merge_group" in data.get("on", data.get(True))
    assert "merge_group" in data["jobs"]["skip-check"]["if"]


def test_changed_head_requires_review_before_requeue(queued, tmp_path, monkeypatch):
    run, forge = queued
    forge.merge_queue_status.side_effect = [queue_status(), queue_status(head="new-head")]

    def invalidate(*args, **kwargs):
        run.last_error = orch.LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX + ": changed head"
        return True

    monkeypatch.setattr(orch, "_mark_local_review_for_rerun_after_sync", invalidate)
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert run.last_error.startswith(orch.LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX)
    policy = orch._workflow_failure_policy(run, "merge")
    assert policy.next_phase == "verify"
    forge.merge_pr.assert_called_once()
    forge.cancel_merge_queue.assert_called_once()


def test_queue_policy_combines_classic_protection_and_active_rules(tmp_path):
    runner = MagicMock(side_effect=[
        response({"data": {"repository": {"mergeQueue": {"id": "q"}}}}),
        response([[{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": "ci"}],
        }}]]),
        response({"required_status_checks": {"contexts": ["review-decision-gate"], "strict": False}}),
    ])
    policy = GitHubForge(run_fn=runner).branch_merge_policy("main", cwd=tmp_path)
    assert policy["queued"]
    assert policy["strict"]
    assert policy["contexts"] == ["ci", "review-decision-gate"]


def test_doctor_accepts_merge_group_workflow(tmp_path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: ci\non: [pull_request, merge_group]\njobs:\n  ci:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    forge = MagicMock()
    forge.branch_merge_policy.return_value = dict(queued=True, strict=False, contexts=["ci"], workflows=[])
    checks = doctor._merge_queue_checks(tmp_path, SpecRuntimeConfig(), forge)
    assert any("merge_group enabled" in check.detail and check.status == "ok" for check in checks)


def test_failed_dequeue_prevents_implementation_retry(queued, tmp_path):
    run, forge = queued
    run.required_check_reruns.append(dict(kind="queue", head_sha="head", status="failed"))
    forge.cancel_merge_queue.return_value = "permission denied"
    forge.merge_queue_status.side_effect = [
        queue_status(),
        queue_status(event={"id": "new", "reason": "required checks failed"}),
    ]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert run.last_error.startswith("Could not cancel merge queue request")
    assert orch._workflow_failure_policy(run, "merge") is None


@pytest.mark.parametrize("entry,event", [
    (None, None),
    ({"id": "q", "headCommit": {"oid": "group"}}, None),
    (None, {"id": "removed", "reason": "checks failed"}),
])
def test_queue_status_graphql_contract(tmp_path, entry, event):
    # Exercise the real adapter with the portable response contract: removal
    # events supply identity/reason, never the workflow commit. Reject extra
    # selections here, even when the response has no timeline events.
    payload = queue_status(entry=entry, event=event)
    payload["autoMergeRequest"] = None

    def runner(cmd, **kwargs):
        query = cmd[4].removeprefix("query=")
        selection = re.search(r"on RemovedFromMergeQueueEvent\{([^{}]*)\}", query)
        assert selection is not None
        assert set(selection[1].split()) == {"id", "reason"}
        return response({"data": {"repository": {"pullRequest": payload}}})

    status = GitHubForge(run_fn=runner).merge_queue_status(12, cwd=tmp_path)
    assert status == payload


def test_removal_between_polls_uses_workflow_lookup(queued, tmp_path):
    run, forge = queued
    run.required_check_reruns.append(dict(kind="queue", head_sha="head", status="failed"))
    forge.merge_queue_status.side_effect = [
        queue_status(), queue_status(event={"id": "removed", "reason": "checks failed"}),
    ]
    forge.merge_group_failure_excerpt.return_value = "synthetic workflow failure"
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    args, kwargs = forge.merge_group_failure_excerpt.call_args
    assert args == ("",)
    assert kwargs["pr_number"] == 12
    assert kwargs["queued_at"]
    assert "synthetic workflow failure" in run.last_error


def test_workflow_lookup_excludes_other_prs_bases_and_admissions(tmp_path):
    now = "2026-11-04T18:00:00Z"

    def workflow(number, branch, created=now):
        return dict(databaseId=number, conclusion="failure", url=f"run-{number}",
                    headBranch=branch, headSha=f"synthetic-{number}", createdAt=created)

    runner = MagicMock(side_effect=[
        response([
            workflow(1, "gh-readonly-queue/main/pr-12-abcd", "2026-11-03T18:00:00Z"),
            workflow(2, "gh-readonly-queue/main/pr-123-abcd"),
            workflow(3, "gh-readonly-queue/release/pr-12-abcd"),
            workflow(4, "code/feature"),
            workflow(5, "gh-readonly-queue/main/pr-12-abcd"),
        ]),
        subprocess.CompletedProcess([], 0, "failed synthetic test", ""),
    ])
    excerpt = GitHubForge(run_fn=runner).merge_group_failure_excerpt(
        "", cwd=tmp_path, pr_number=12, base_branch="main", queued_at=now,
    )
    assert "failed synthetic test" in excerpt
    assert "synthetic-5" in excerpt
    assert runner.call_args_list[1].args[0] == ["gh", "run", "view", "5", "--log-failed"]
    command = runner.call_args_list[0].args[0]
    assert command[command.index("--event") + 1] == "merge_group"
    assert command[command.index("--created") + 1] == ">=" + now


@pytest.mark.parametrize("failure", ["unavailable", "invalid-json", "queue-query-unavailable", "confirmation-timeout", "confirmation-oserror", "timeout"])
@pytest.mark.parametrize("cancel_error", [False, True])
def test_successful_admission_with_uncertain_confirmation_is_cancelled(
    queued, tmp_path, monkeypatch, failure, cancel_error,
):
    run, _ = queued
    armed = False
    calls = []

    def runner(cmd, **kwargs):
        nonlocal armed
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "merge"]:
            if "--disable-auto" in cmd:
                armed = False
                return response({})
            armed = True
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            if failure == "confirmation-timeout":
                raise subprocess.TimeoutExpired(cmd, 30)
            if failure == "confirmation-oserror":
                raise OSError("confirmation connection lost")
            if failure == "invalid-json":
                return subprocess.CompletedProcess(cmd, 0, "{", "")
            if failure == "queue-query-unavailable":
                return response(dict(state="OPEN", headRefOid="head", autoMergeRequest=None))
            return response({}, code=1, stderr="confirmation unavailable")
        query = cmd[4].removeprefix("query=")
        if "dequeuePullRequest" in query:
            if cancel_error:
                return response({"errors": [{"message": "permission denied"}]})
            armed = False
            return response({"data": {"dequeuePullRequest": {}}})
        if failure == "queue-query-unavailable" and armed:
            # Both confirmation and cleanup must report uncertainty without
            # permitting an implementation retry while admission may remain.
            return response({"errors": [{"message": "queue query unavailable"}]})
        payload = queue_status(entry={"id": "q"} if armed else None)
        payload["autoMergeRequest"] = {"enabledAt": "now"} if armed else None
        return response({"data": {"repository": {"pullRequest": payload}}})

    forge = GitHubForge(run_fn=runner)
    monkeypatch.setattr(orch, "_forge", lambda *_: forge)
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    if cancel_error or failure == "queue-query-unavailable":
        assert run.last_error.startswith("Could not cancel merge queue request")
        assert orch._workflow_failure_policy(run, "merge") is None
    else:
        assert not armed
        assert any("dequeuePullRequest" in " ".join(command) for command in calls)
        assert "admission failed" in run.last_error


@pytest.mark.parametrize("path", ["auto", "manual"])
def test_direct_auto_merge_paths_disarm_uncertain_admission(queued, tmp_path, monkeypatch, path):
    run, forge = queued
    forge.merge_pr.return_value = MergeResult(
        False, "successful CLI mutation with failed confirmation", auto_merge_may_be_armed=True,
    )
    disable = MagicMock(return_value="")
    monkeypatch.setattr(orch, "_disable_pr_auto_merge", disable)
    monkeypatch.setattr(orch, "_required_checks_green_for_repo", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (
        {"headRefOid": "head", "mergeStateStatus": "CLEAN"}, "",
    ))
    if path == "auto":
        result = orch._try_auto_merge(run, tmp_path, 12, expected_head_sha="head")
    else:
        result = orch._manual_merge(run, tmp_path, tmp_path, run.branch, 12, "head", {})
    assert result == "failed"
    assert "could not be confirmed" in run.last_error
    disable.assert_called_once_with(tmp_path, 12, repo_slug="owner/repo")
    forge.merge_pr.assert_called_once()


def test_removal_after_resuming_queue_uses_existing_group(queued, tmp_path):
    run, forge = queued
    run.required_check_reruns.append(dict(kind="queue", head_sha="head", status="failed"))
    forge.merge_queue_status.side_effect = [
        queue_status(entry={"id": "q", "headCommit": {"oid": "existing-group"},
                            "enqueuedAt": "2026-11-04T17:00:00Z"}),
        queue_status(event={"id": "removed", "reason": "checks failed"}),
    ]
    forge.merge_group_failure_excerpt.return_value = "existing group failed"
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    args, kwargs = forge.merge_group_failure_excerpt.call_args
    assert args == ("existing-group",)
    assert kwargs["queued_at"] == "2026-11-04T17:00:00Z"


@pytest.mark.parametrize("passes", [True, False])
def test_queue_check_gets_one_free_reenqueue(queued, tmp_path, passes):
    run, forge = queued
    removed = queue_status(event={"id": "removed", "reason": "checks failed"})
    forge.merge_group_failure_excerpt.return_value = "https://github.com/o/r/actions/runs/123 workflow failure"
    forge.workflow_run.return_value = dict(jobs=[dict(name="queue browser", conclusion="failure")])
    forge.merge_queue_status.side_effect = [
        queue_status(), removed, removed,
        queue_status(state="MERGED") if passes else queue_status(event={"id": "again", "reason": "checks failed"}),
    ]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == ("merged" if passes else "failed")
    assert forge.merge_pr.call_count == 2
    assert run.merge_queue_retries == 0
    assert run.attempts == run.review_changes == 0
    assert run.required_check_reruns[0]["status"] == ("flaky" if passes else "failed")
    assert run.required_check_reruns[0]["workflow_run_id"] == "123"
    assert run.required_check_reruns[0]["jobs"] == ["queue browser"]


@pytest.mark.parametrize("merged_on_admission", [True, False])
def test_queue_terminal_recovery_closes_waiting_retry(queued, tmp_path, merged_on_admission):
    run, forge = queued
    run.required_check_reruns.append(dict(kind="queue", head_sha="head", workflow_run_id="123",
                                         jobs=["browser"], status="waiting"))
    run.save(tmp_path)
    run = orch.RunState.load(tmp_path, "run")
    forge.merge_queue_status.side_effect = ([queue_status()] if not merged_on_admission else []) + [queue_status(state="MERGED")]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "merged"
    assert orch.RunState.load(tmp_path, "run").required_check_reruns[0]["status"] == "flaky"
    assert run.merge_queue_retries == 0
