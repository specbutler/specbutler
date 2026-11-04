"""Hermetic merge-queue adapter, lifecycle and diagnostic contracts."""

import json
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
    forge.merge_group_failure_excerpt.return_value = "group workflow failure excerpt"
    forge.merge_queue_status.side_effect = [
        queue_status(),
        queue_status(event={"id": "new", "reason": reason, "beforeCommit": {"oid": "group"}}),
    ]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert expected in run.last_error
    if "check" in reason:
        assert "group workflow failure excerpt" in run.last_error
        forge.merge_group_failure_excerpt.assert_called_once_with("group", cwd=tmp_path)
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
    forge.cancel_merge_queue.return_value = "permission denied"
    forge.merge_queue_status.side_effect = [
        queue_status(),
        queue_status(event={"id": "new", "reason": "required checks failed"}),
    ]
    assert orch._try_queued_merge(run, tmp_path, 12, expected_head_sha="head") == "failed"
    assert run.last_error.startswith("Could not cancel merge queue request")
    assert orch._workflow_failure_policy(run, "merge") is None
