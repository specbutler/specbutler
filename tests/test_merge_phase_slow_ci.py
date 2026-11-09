"""Merge-phase behaviour on slow CI, repos without auto-merge, and resolved conflicts.

Regression contracts for #95 (GitHub's "Auto merge is not allowed" refusal),
#96 (fixed 15-minute check wait; a pending-checks readiness timeout relaunched
implement) and #97 (readiness reused a conflict that had since been resolved).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.config import SpecConfigError, load_repo_spec_runtime_config
from spec_runtime.forge import MergeResult

GITHUB_AUTO_MERGE_NOT_ALLOWED = (
    "GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)"
)


# ---------------------------------------------------------------------------
# #95: "Auto merge is not allowed for this repository"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        GITHUB_AUTO_MERGE_NOT_ALLOWED,
        "Auto merge is not allowed for this repository",
        "auto-merge is not allowed for this repository",
    ],
)
def test_auto_merge_not_allowed_is_a_capability_error(stderr: str) -> None:
    assert orch._is_auto_merge_capability_error(stderr) is True
    # Not a transient GraphQL error: retrying --auto can never succeed.
    assert orch._is_auto_merge_graphql_retryable_error(stderr) is False


def test_merge_method_refusal_is_not_an_auto_merge_capability_error() -> None:
    # Dropping --auto cannot help when the repository forbids the merge method.
    assert orch._is_auto_merge_capability_error("Squash merges are not allowed on this repository") is False


def test_transient_auto_merge_graphql_error_stays_retryable() -> None:
    error = "GraphQL: Something went wrong while executing your query (enablePullRequestAutoMerge)"
    assert orch._is_auto_merge_capability_error(error) is False
    assert orch._is_auto_merge_graphql_retryable_error(error) is True


def test_try_auto_merge_drops_auto_when_repository_disallows_it(tmp_path: Path) -> None:
    run = orch.RunState(run_id="r", spec_id="s", branch="code/s--1")
    forge = MagicMock()
    forge.merge_pr.return_value = MergeResult(ok=False, message=GITHUB_AUTO_MERGE_NOT_ALLOWED)
    with (
        patch.object(orch, "resolve_worktree_path", return_value=tmp_path),
        patch.object(orch, "_assert_publication_transition_safe"),
        patch.object(orch, "_trusted_publication_repo_slug", return_value="acme/repo"),
        patch.object(orch, "_forge", return_value=forge),
        patch.object(orch, "_poll_sleep") as poll_sleep,
    ):
        result = orch._try_auto_merge(run, tmp_path, 12, expected_head_sha="a" * 40)

    assert result == "unavailable_after_auto_no_auto"
    poll_sleep.assert_not_called()


def test_manual_merge_retries_without_auto_when_repository_disallows_it(tmp_path: Path) -> None:
    run = orch.RunState(run_id="r", spec_id="s", branch="code/s--1")
    forge = MagicMock()
    forge.merge_pr.side_effect = [
        MergeResult(ok=False, message=GITHUB_AUTO_MERGE_NOT_ALLOWED),
        MergeResult(ok=True),
    ]
    head = "a" * 40
    with (
        patch.object(orch, "_trusted_publication_repo_slug", return_value="acme/repo"),
        patch.object(orch, "_merge_required_checks", return_value=(True, "")),
        patch.object(orch, "_find_open_pr_for_branch", return_value=({"headRefOid": head}, None)),
        patch.object(orch, "_assert_publication_transition_safe"),
        patch.object(orch, "_forge", return_value=forge),
        patch.object(orch, "_wait_for_pr_merged", return_value=(True, "")),
        patch.object(orch, "_poll_sleep"),
    ):
        result = orch._manual_merge(
            run, tmp_path, tmp_path, run.branch, 12, head, {"headRefOid": head}, use_auto_merge=True,
        )

    assert result == "passed", run.last_error
    assert [call.kwargs["auto"] for call in forge.merge_pr.call_args_list] == [True, False]


# ---------------------------------------------------------------------------
# #96: configurable check wait; pending-checks readiness timeout stops in merge
# ---------------------------------------------------------------------------


def test_checks_timeout_defaults_to_fifteen_minutes(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text("[readiness]\nrequire_forge_checks = true\n", encoding="utf-8")
    config = load_repo_spec_runtime_config(tmp_path)
    assert config.readiness.checks_timeout_seconds == 900
    assert config.readiness.require_forge_checks is True


def test_checks_timeout_is_configurable(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        "[readiness]\nrequire_forge_checks = false\nchecks_timeout_seconds = 2700\n", encoding="utf-8",
    )
    config = load_repo_spec_runtime_config(tmp_path)
    assert config.readiness.checks_timeout_seconds == 2700
    assert config.readiness.require_forge_checks is False


@pytest.mark.parametrize("value", ["0", "-5", '"2700"', "true", "12.5"])
def test_checks_timeout_rejects_invalid_values(tmp_path: Path, value: str) -> None:
    (tmp_path / ".spec.toml").write_text(f"[readiness]\nchecks_timeout_seconds = {value}\n", encoding="utf-8")
    with pytest.raises(SpecConfigError, match=r"\[readiness\]\.checks_timeout_seconds"):
        load_repo_spec_runtime_config(tmp_path)


def test_orchestrator_wait_comes_from_readiness_config() -> None:
    assert orch.MERGE_CHECKS_TIMEOUT_SECONDS == orch.SPEC_RUNTIME_CONFIG.readiness.checks_timeout_seconds


class _Clock:
    """Fake ``time`` module: only ``time()`` is virtual; the rest is real."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(time, name)


def _readiness_ready_run(head: str) -> orch.RunState:
    return orch.RunState(
        run_id="r",
        spec_id="s",
        branch="code/s--1",
        verify_passed_once=True,
        verify_head_sha=head,
        review_decision_status="approved",
        review_expected_head_sha=head,
    )


def _evaluate_with_checks(run: orch.RunState, repo: Path, head: str, checks, *, clock: _Clock):
    with (
        patch.object(orch, "time", clock),
        patch.object(orch, "_poll_sleep", side_effect=clock.sleep),
        patch.object(orch, "_trusted_publication_repo_slug", return_value="acme/repo"),
        patch.object(orch, "_merge_required_checks", side_effect=checks),
        patch.object(orch, "_augment_required_checks_error", side_effect=lambda _r, e, **_k: e),
        patch.object(orch, "_find_pr_for_branch", return_value={"headRefOid": head}),
    ):
        return orch._evaluate_readiness_for_promotion(
            run, repo, pr_number=12, pr_data={"headRefOid": head}, current_head_sha=head,
            require_integration_checkpoint=False,
        )


def test_readiness_waits_for_the_configured_timeout(tmp_path: Path, monkeypatch) -> None:
    """A 25-minute browser suite passes readiness when the wait is 45 minutes."""
    head = "a" * 40
    clock = _Clock()
    start = clock.now
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 2700)

    def checks(*_args, **_kwargs):
        if clock.now - start < 25 * 60:
            return False, "Required checks still pending: browser (https://example.com/runs/1/job/2)"
        return True, ""

    decision = _evaluate_with_checks(_readiness_ready_run(head), tmp_path, head, checks, clock=clock)

    assert decision.ready, decision.blocker
    assert clock.now - start >= 25 * 60


def test_readiness_pending_timeout_stops_in_merge_instead_of_relaunching_implement(
    tmp_path: Path, monkeypatch,
) -> None:
    head = "a" * 40
    clock = _Clock()
    monkeypatch.setattr(orch, "MERGE_CHECKS_TIMEOUT_SECONDS", 900)
    run = _readiness_ready_run(head)

    decision = _evaluate_with_checks(
        run, tmp_path, head,
        lambda *_a, **_k: (False, "Required checks still pending: browser (https://example.com/runs/1/job/2)"),
        clock=clock,
    )
    assert not decision.ready
    run.last_error = f"Readiness promotion refused for {orch._short_sha(head)}: {decision.blocker}"

    assert orch._is_required_checks_pending_timeout(run.last_error)
    assert orch._workflow_failure_policy(run, "merge") is None
    classification = orch.classify_failure(run.last_error, phase="merge")
    assert classification.is_infrastructure
    assert classification.kind == "ci_wait_timeout"
    assert classification.transient


def test_readiness_failing_checks_still_go_back_to_the_agent(tmp_path: Path) -> None:
    head = "a" * 40
    run = _readiness_ready_run(head)
    decision = _evaluate_with_checks(
        run, tmp_path, head,
        lambda *_a, **_k: (False, "Required checks failing: browser (https://example.com/runs/1/job/2)"),
        clock=_Clock(),
    )
    run.last_error = f"Readiness promotion refused for {orch._short_sha(head)}: {decision.blocker}"

    policy = orch._workflow_failure_policy(run, "merge")
    assert policy is not None
    assert policy.next_phase == "implement"
    assert policy.retry_reason == "verify_failures"


def test_other_readiness_refusals_still_retry_implement() -> None:
    run = orch.RunState(run_id="r", spec_id="s", branch="code/s--1")
    run.last_error = "Readiness promotion refused for abc1234: independent verify has not passed"
    policy = orch._workflow_failure_policy(run, "merge")
    assert policy is not None
    assert policy.next_phase == "implement"


def test_merge_step_pending_timeout_is_a_ci_wait_timeout() -> None:
    error = (
        "Merge refused for abc1234; timed out waiting for required checks: "
        "Required checks still pending: browser (https://example.com/runs/1/job/2)"
    )
    run = orch.RunState(run_id="r", spec_id="s", branch="code/s--1", last_error=error)
    assert orch._workflow_failure_policy(run, "merge") is None
    assert orch.classify_failure(error, phase="merge").kind == "ci_wait_timeout"


# ---------------------------------------------------------------------------
# #97: a resolved conflict must not keep blocking readiness
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _conflicting_branch(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return (origin, clone, branch) where the branch conflicts with the base in package.json."""
    base_branch = orch.BASE_REF.split("/", 1)[1]
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", base_branch, cwd=origin)
    _git("config", "user.email", "t@t.com", cwd=origin)
    _git("config", "user.name", "T", cwd=origin)
    (origin / "package.json").write_text('{"version": "1.0.0"}\n', encoding="utf-8")
    _git("add", "package.json", cwd=origin)
    _git("commit", "-q", "-m", "base", cwd=origin)

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "t@t.com", cwd=clone)
    _git("config", "user.name", "T", cwd=clone)
    branch = "code/s--1"
    _git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "package.json").write_text('{"version": "1.1.0-branch"}\n', encoding="utf-8")
    _git("commit", "-q", "-am", "branch bump", cwd=clone)

    (origin / "package.json").write_text('{"version": "2.0.0-base"}\n', encoding="utf-8")
    _git("commit", "-q", "-am", "base bump", cwd=origin)
    # The clone backend (the test default) checks against the workspace's
    # tracked base without refetching it, so bring that base up to date here.
    _git("fetch", "-q", "origin", cwd=clone)
    return origin, clone, branch


def _readiness_on(run: orch.RunState, clone: Path) -> orch.ReadinessDecision:
    head = _git("rev-parse", "HEAD", cwd=clone)
    run.verify_passed_once = True
    run.verify_head_sha = head
    run.review_decision_status = "approved"
    run.review_expected_head_sha = head
    with patch.object(orch, "resolve_worktree_path", return_value=clone):
        return orch._evaluate_readiness_for_promotion(
            run, clone, pr_number=12, pr_data={"headRefOid": head}, current_head_sha=head,
            require_forge_checks=False, require_integration_checkpoint=False,
        )


@pytest.mark.requires_ps
def test_readiness_rechecks_a_saved_conflict_after_it_is_resolved(tmp_path: Path) -> None:
    _origin, clone, branch = _conflicting_branch(tmp_path)
    saved = (
        f"Confirmed local merge conflict with {orch.BASE_REF} during auto-merge wait: "
        "Auto-merging package.json CONFLICT (content): Merge conflict in package.json"
    )
    run = orch.RunState(
        run_id="r", spec_id="s", branch=branch, merge_conflict_error=saved, mergeability_issue=saved,
    )

    # Still conflicting: readiness stays blocked, on a fresh recheck.
    blocked = _readiness_on(run, clone)
    assert not blocked.ready
    assert blocked.status == "merge-blocked"
    assert "during readiness promotion" in blocked.blocker
    assert "package.json" in blocked.blocker

    # The operator merges the base, resolves package.json and commits.
    subprocess.run(["git", "merge", "-q", orch.BASE_REF], cwd=clone, capture_output=True)
    (clone / "package.json").write_text('{"version": "2.1.0"}\n', encoding="utf-8")
    _git("commit", "-q", "-am", "resolve package.json", cwd=clone)

    decision = _readiness_on(run, clone)

    assert decision.ready, decision.blocker
    assert decision.status == "merge-eligible"
    assert run.merge_conflict_error == ""
    assert run.mergeability_issue == ""


def test_readiness_keeps_the_saved_conflict_when_the_recheck_cannot_run(tmp_path: Path) -> None:
    saved = "Confirmed local merge conflict with origin/main during auto-merge wait: CONFLICT in package.json"
    run = orch.RunState(run_id="r", spec_id="s", branch="code/s--1", merge_conflict_error=saved)
    head = "a" * 40
    run.verify_passed_once = True
    run.verify_head_sha = head
    run.review_decision_status = "approved"
    with (
        patch.object(orch, "resolve_worktree_path", return_value=tmp_path / "missing"),
    ):
        decision = orch._evaluate_readiness_for_promotion(
            run, tmp_path, pr_number=12, pr_data={"headRefOid": head}, current_head_sha=head,
            require_forge_checks=False, require_integration_checkpoint=False,
        )

    assert not decision.ready
    assert decision.status == "merge-blocked"
    assert "could not recheck the saved base-branch conflict" in decision.blocker
    assert run.merge_conflict_error == saved
    # Not a confirmed conflict: the run stops in merge instead of spending an
    # implement retry on a conflict that may already be resolved.
    run.last_error = f"Readiness promotion refused for {orch._short_sha(head)}: {decision.blocker}"
    assert not orch._is_merge_conflict(run.last_error)
    assert orch._workflow_failure_policy(run, "merge") is None
