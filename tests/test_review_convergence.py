"""Hermetic tests for bounded local-review convergence."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import spec_runtime.orchestrator as orch
from spec_runtime import agent_adapter, review_convergence
from spec_runtime.agent_adapter import CodexAgent
from spec_runtime.config import SpecConfigError, _parse_review_section
from spec_runtime.review_gate import GateEvaluation, compute_review_decision

HEAD = "a" * 40
BASE = "b" * 40


def _finding(finding_id: str, severity: str, *, file: str = "src/app.py", line: int = 10) -> dict:
    return {
        "id": finding_id,
        "title": f"Problem {finding_id}",
        "severity": severity,
        "file": file,
        "start_line": line,
        "end_line": line,
        "body": "Details.",
        "confidence": 0.9,
    }


def _run() -> orch.RunState:
    return orch.RunState(
        run_id="my-feature-20260101T000000",
        spec_id="my-feature",
        branch="code/my-feature--run123",
    )


def _run_dir(repo: Path, run: orch.RunState) -> Path:
    return orch._state_root(repo) / "runs" / run.run_id


# ---------------------------------------------------------------------------
# Prior findings rendering
# ---------------------------------------------------------------------------


def test_prior_findings_section_lists_findings_and_implementer_responses():
    section = review_convergence.render_prior_findings_section(
        [_finding("F1", "P1", line=12), _finding("F2", "P2", file="docs/x.md")],
        round_number=2,
        implementer_summary="F1: fixed by validating input. Unrelated cleanup.",
    )

    assert "review round 2" in section
    assert "- id `F1` [P1] src/app.py:12: Problem F1: Details." in section
    assert "Implementer response: F1: fixed by validating input." in section
    assert "- id `F2` [P2] docs/x.md:10" in section
    assert "Implementer response: not addressed by id" in section
    assert "`resolved`" in section and "`unresolved`" in section and "`withdrawn`" in section


def test_rereview_rules_section_states_blocking_rule_and_includes_diff():
    section = review_convergence.render_rereview_rules_section(
        last_reviewed_head_sha="c" * 40,
        incremental_diff="diff --git a/x b/x\n",
    )

    assert "must be either P0, or a P1 introduced by the changes since the last review" in section
    assert "<BEGIN_INCREMENTAL_DIFF>\ndiff --git a/x b/x\n<END_INCREMENTAL_DIFF>" in section


# ---------------------------------------------------------------------------
# Incremental diff materialisation
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_incremental_diff_covers_only_changes_since_last_reviewed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(tmp_path, "init", "-q")
    (tmp_path / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 21)))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    lines = (tmp_path / "app.py").read_text().splitlines(keepends=True)
    lines[14] = "line 15 changed\n"
    (tmp_path / "app.py").write_text("".join(lines))
    _git(tmp_path, "commit", "-qam", "second")
    second = _git(tmp_path, "rev-parse", "HEAD")

    diff, ranges = orch._local_review_incremental_diff(
        tmp_path, last_reviewed_head_sha=first, head_sha=second
    )

    assert "+line 15 changed" in diff
    assert ranges == {"app.py": [(15, 15)]}
    unchanged_diff, unchanged_ranges = orch._local_review_incremental_diff(
        tmp_path, last_reviewed_head_sha=second, head_sha=second
    )
    assert unchanged_diff == "" and unchanged_ranges == {}
    missing_diff, missing_ranges = orch._local_review_incremental_diff(
        tmp_path, last_reviewed_head_sha="f" * 40, head_sha=second
    )
    assert "could not diff" in missing_diff and missing_ranges is None


# ---------------------------------------------------------------------------
# Decision rule
# ---------------------------------------------------------------------------


def test_first_review_blocks_on_any_new_p0_or_p1():
    computed = compute_review_decision(
        model_decision="approved", findings=[_finding("N1", "P1"), _finding("N2", "P3")]
    )

    assert computed.decision == "request_changes"
    assert computed.blocking_ids == ["N1"]
    assert computed.overrides_model


def test_rereview_unresolved_prior_p1_blocks_and_resolved_does_not():
    prior = [_finding("F1", "P1"), _finding("F2", "P0")]
    blocked = compute_review_decision(
        model_decision="approved",
        findings=[],
        prior_findings=prior,
        prior_classifications=[{"id": "F1", "status": "unresolved", "note": ""}, {"id": "F2", "status": "resolved"}],
        changed_ranges={},
    )
    approved = compute_review_decision(
        model_decision="request_changes",
        findings=[],
        prior_findings=prior,
        prior_classifications=[{"id": "F1", "status": "withdrawn"}, {"id": "F2", "status": "resolved"}],
        changed_ranges={},
    )

    assert blocked.decision == "request_changes" and blocked.blocking_ids == ["F1"]
    assert approved.decision == "approved" and approved.overrides_model


def test_rereview_unclassified_prior_blocking_finding_counts_as_unresolved():
    computed = compute_review_decision(
        model_decision="approved",
        findings=[],
        prior_findings=[_finding("F1", "P1")],
        prior_classifications=[],
        changed_ranges={},
    )

    assert computed.decision == "request_changes"


def test_rereview_new_p1_blocks_only_when_it_touches_changed_lines():
    ranges = {"src/app.py": [(40, 45)]}
    outside = compute_review_decision(
        model_decision="request_changes",
        findings=[_finding("N1", "P1", line=10), _finding("N2", "P2", line=41)],
        prior_findings=[],
        changed_ranges=ranges,
    )
    inside = compute_review_decision(
        model_decision="approved",
        findings=[_finding("N1", "P1", line=42)],
        prior_findings=[],
        changed_ranges=ranges,
    )
    new_p0 = compute_review_decision(
        model_decision="approved",
        findings=[_finding("N1", "P0", line=1)],
        prior_findings=[],
        changed_ranges=ranges,
    )

    assert outside.decision == "approved"
    assert {f["id"]: f["blocking"] for f in outside.outstanding_findings} == {"N1": False, "N2": False}
    assert inside.decision == "request_changes"
    assert new_p0.decision == "request_changes"


def test_non_blocking_rereview_p1_stays_non_blocking_in_later_rounds():
    # Round 2: N1 is a P1 outside the incremental diff (non-blocking); N2 is a
    # P0 that forces another round.
    round_two = compute_review_decision(
        model_decision="request_changes",
        findings=[_finding("N1", "P1", line=10), _finding("N2", "P0", line=42)],
        prior_findings=[],
        changed_ranges={"src/app.py": [(40, 45)]},
    )
    assert round_two.blocking_ids == ["N2"]

    # Round 3 carries round 2's outstanding findings forward; N2 is fixed but
    # N1 is still unresolved and must keep its non-blocking eligibility.
    round_three = compute_review_decision(
        model_decision="request_changes",
        findings=[],
        prior_findings=round_two.outstanding_findings,
        prior_classifications=[{"id": "N1", "status": "unresolved"}, {"id": "N2", "status": "resolved"}],
        changed_ranges={"src/app.py": [(10, 10)]},
    )

    assert round_three.decision == "approved"
    assert round_three.blocking_ids == []
    assert [(f["id"], f["blocking"]) for f in round_three.outstanding_findings] == [("N1", False)]

    # Round 4 restates N1 under its own id; it is still counted once, non-blocking.
    round_four = compute_review_decision(
        model_decision="request_changes",
        findings=[_finding("N1", "P1", line=10)],
        prior_findings=round_three.outstanding_findings,
        prior_classifications=[{"id": "N1", "status": "unresolved"}],
        changed_ranges={"src/app.py": [(10, 10)]},
    )
    assert round_four.decision == "approved"
    assert [(f["id"], f["blocking"]) for f in round_four.outstanding_findings] == [("N1", False)]


def test_legacy_prior_finding_without_blocking_flag_falls_back_to_severity():
    legacy_p1 = _finding("F1", "P1")
    legacy_p2 = _finding("F2", "P2")
    assert "blocking" not in legacy_p1 and "blocking" not in legacy_p2

    computed = compute_review_decision(
        model_decision="approved",
        findings=[],
        prior_findings=[legacy_p1, legacy_p2],
        prior_classifications=[],
        changed_ranges={},
    )

    assert computed.blocking_ids == ["F1"]


def test_new_finding_reusing_a_resolved_prior_id_still_blocks():
    computed = compute_review_decision(
        model_decision="approved",
        findings=[_finding("F1", "P0", file="src/other.py", line=3)],
        prior_findings=[_finding("F1", "P1")],
        prior_classifications=[{"id": "F1", "status": "resolved"}],
        changed_ranges={},
    )

    assert computed.decision == "request_changes"
    assert computed.blocking_ids == ["F1-new"]
    (new,) = computed.outstanding_findings
    assert new["id"] == "F1-new" and new["reported_id"] == "F1" and new["origin"] == "new"


def test_new_finding_colliding_with_unresolved_prior_id_is_kept_when_it_differs():
    prior = [_finding("F1", "P2")]
    computed = compute_review_decision(
        model_decision="approved",
        findings=[_finding("F1", "P0", file="src/other.py"), _finding("F1", "P1", line=40)],
        prior_findings=prior,
        prior_classifications=[{"id": "F1", "status": "unresolved"}],
        changed_ranges={"src/app.py": [(40, 40)]},
    )

    assert computed.decision == "request_changes"
    assert [f["id"] for f in computed.outstanding_findings] == ["F1", "F1-new", "F1-new2"]
    assert computed.blocking_ids == ["F1-new", "F1-new2"]


def test_restated_unresolved_prior_finding_is_not_double_counted():
    computed = compute_review_decision(
        model_decision="request_changes",
        findings=[_finding("F1", "P1")],
        prior_findings=[_finding("F1", "P1")],
        prior_classifications=[{"id": "F1", "status": "unresolved"}],
        changed_ranges={},
    )

    assert computed.blocking_ids == ["F1"]
    assert len(computed.outstanding_findings) == 1


def test_prior_findings_prompt_asks_for_ids_distinct_from_prior_ones():
    section = review_convergence.render_prior_findings_section(
        [_finding("F1", "P1")], round_number=2, implementer_summary=""
    )

    assert "differs from all prior finding ids" in section


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "./.github/workflows/ci.yml", ".spec.toml"])
def test_new_p1_on_dot_prefixed_path_touching_changed_lines_blocks(path: str):
    key = path.removeprefix("./")
    computed = compute_review_decision(
        model_decision="approved",
        findings=[_finding("N1", "P1", file=path, line=5)],
        prior_findings=[],
        changed_ranges={key: [(4, 6)]},
    )

    assert computed.decision == "request_changes"
    assert review_convergence.finding_touches_ranges({"file": path, "start_line": 5}, {key: [(5, 5)]})


def _gate_evaluation(tmp_path: Path, *, decision: str, findings: list[dict], **extra) -> GateEvaluation:
    from spec_runtime.review_gate import evaluate_review_gate

    schema = Path(orch.__file__).parent / "templates" / "review-schema.json"
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "decision": decision,
                "summary": "ok",
                "reviewed_base_sha": BASE,
                "reviewed_head_sha": HEAD,
                "findings": findings,
                "reviewer_role": "independent-review",
                "reviewer_agent": "codex",
                "reviewed_at": "2026-01-01T00:00:00Z",
                **extra,
            }
        )
    )
    return evaluate_review_gate(
        input_path=raw,
        schema_path=schema,
        expected_head_sha=HEAD,
        expected_base_sha=BASE,
        check_name="review",
    )


def test_model_decision_disagreement_is_logged_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    evaluation = _gate_evaluation(tmp_path, decision="request_changes", findings=[_finding("N1", "P3")])
    assert evaluation.result_payload["status"] == "request_changes", evaluation.human_summary

    with caplog.at_level(logging.WARNING, logger=orch.logger.name):
        result = orch._apply_computed_local_review_decision(
            run,
            evaluation,
            review_run_dir=run_dir,
            last_round=None,
            round_number=1,
            changed_ranges=None,
            expected_head_sha=HEAD,
            expected_base_sha=BASE,
        )

    assert result.result_payload["status"] == "approved"
    assert result.result_payload["model_decision"] == "request_changes"
    assert result.exit_code == 0
    assert "Ignoring reviewer decision request_changes" in caplog.text
    assert result.result_payload["review_round"] == 1
    assert result.result_payload["outstanding_findings"][0]["id"] == "N1"
    # The round is recorded by the phase once it reaches _apply_review_result.
    assert review_convergence.load_review_rounds(run_dir) == []


def test_older_review_output_without_prior_findings_still_parses(tmp_path: Path):
    evaluation = _gate_evaluation(tmp_path, decision="approved", findings=[])

    assert evaluation.result_payload["status"] == "approved", evaluation.human_summary
    classified = _gate_evaluation(
        tmp_path,
        decision="approved",
        findings=[],
        prior_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}],
    )
    assert classified.result_payload["prior_findings"][0]["status"] == "resolved"


# ---------------------------------------------------------------------------
# Round cap -> waiting-for-input, and operator resolution
# ---------------------------------------------------------------------------


def _patch_local_review_phase(repo: Path, review_result: orch.ReviewResult, result_path: Path):
    pr = {
        "number": 42,
        "url": "https://github.com/acme/repo/pull/42",
        "headRefOid": HEAD,
        "body": "## Summary\nChange.\n",
        "mergeStateStatus": "CLEAN",
    }
    return (
        patch.object(orch, "resolve_worktree_path", return_value=repo),
        patch.object(orch, "_assert_publication_transition_safe", return_value=None),
        patch.object(orch, "require_host_agent_available", return_value=None),
        patch.object(orch.shutil, "which", return_value="/usr/bin/codex"),
        patch.object(orch, "_trusted_publication_repo_slug", return_value="acme/repo"),
        patch.object(orch, "_disable_pr_auto_merge", return_value=""),
        patch.object(orch, "_repo_name_with_owner", return_value="acme/repo"),
        patch.object(orch, "_run_local_review", return_value=(review_result, result_path)),
        patch.object(orch, "_review_stall_fallback_agent", return_value=""),
        patch.object(orch, "_find_open_pr_for_branch", return_value=(pr, pr)),
        patch.object(orch, "_publish_review_gate_sticky_comment", return_value=""),
        patch.object(orch, "_publish_local_review_status", return_value=""),
        patch.object(orch, "_effective_review_agent", return_value="codex"),
    ), pr


def _run_phase(repo: Path, run: orch.RunState, review_result: orch.ReviewResult, gh_calls: list):
    result_path = repo / "review-result.json"
    patches, pr = _patch_local_review_phase(repo, review_result, result_path)

    def fake_run_subprocess(cmd, **_kwargs):
        gh_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
        patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch.object(orch, "run_subprocess", side_effect=fake_run_subprocess),
    ):
        return orch._phase_review_local(
            run, repo, pr_data=pr, expected_head_sha=HEAD, expected_base_sha=BASE
        )


def _request_changes_result() -> orch.ReviewResult:
    blocking = {**_finding("F1", "P1"), "origin": "new", "blocking": True}
    return orch.ReviewResult(
        status="request_changes",
        summary="Still broken.",
        findings=[orch.ReviewFinding(id="F1", title="Problem F1", severity="P1", file="src/app.py")],
        reviewed_head_sha=HEAD,
        reviewed_base_sha=BASE,
        outstanding_findings=[blocking],
        blocking_finding_ids=["F1"],
    )


def _record_rounds(run_dir: Path, count: int) -> None:
    for number in range(1, count + 1):
        review_convergence.append_review_round(
            run_dir,
            review_convergence.ReviewRound(
                round_number=number,
                reviewed_head_sha=HEAD,
                decision="request_changes",
                outstanding_findings=[{**_finding("F1", "P1"), "origin": "new", "blocking": True}],
            ),
        )


def test_request_changes_below_cap_retries_implement(tmp_path: Path):
    run = _run()
    _record_rounds(_run_dir(tmp_path, run), 1)

    result = _run_phase(tmp_path, run, _request_changes_result(), [])

    assert result == "failed"
    assert orch.OperatorRequest.load(tmp_path, run.run_id) is None


def test_round_cap_moves_run_to_waiting_for_input(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)

    result = _run_phase(tmp_path, run, _request_changes_result(), [])

    assert result == "waiting-for-input"
    assert run.status == "waiting-for-input"
    request = orch.OperatorRequest.load(tmp_path, run.run_id)
    assert request is not None
    assert request.kind == review_convergence.REVIEW_ROUND_CAP_REQUEST_KIND
    assert request.requested_by_phase == "review"
    assert "F1 [P1] src/app.py:10" in request.prompt
    assert {o["value"] for o in request.options} == {"accept", "another_round"}

    # One more round with guidance: the cap moves by one and implement resumes.
    with patch.object(orch, "cmd_run", return_value=0) as cmd_run:
        rc = orch._cmd_input_review_round_cap(
            tmp_path,
            argparse.Namespace(spec=run.spec_id),
            accept=False,
            message="Fix F1 by validating input.",
        )
    assert rc == 0 and cmd_run.called
    assert review_convergence.load_extra_rounds_granted(run_dir) == 1
    assert not review_convergence.round_cap_reached(
        run_dir, max_rounds=orch.SPEC_RUNTIME_CONFIG.review.max_rounds
    )
    resumed = orch.RunState.find_latest(tmp_path, run.spec_id)
    assert resumed is not None and resumed.phase == "implement"


def test_operator_accept_at_cap_approves_and_records_followups(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    _run_phase(tmp_path, run, _request_changes_result(), [])
    _request_changes_result().save(tmp_path, run.run_id)

    with patch.object(orch, "cmd_run", return_value=0):
        assert (
            orch._cmd_input_review_round_cap(
                tmp_path, argparse.Namespace(spec=run.spec_id), accept=True, message=""
            )
            == 0
        )
    gh_calls: list = []
    result = _run_phase(tmp_path, run, _request_changes_result(), gh_calls)

    assert result == "passed"
    assert run.review_decision_status == "approved"
    assert orch.OperatorRequest.load(tmp_path, run.run_id).status == "consumed"
    followups = (run_dir / review_convergence.REVIEW_FOLLOWUPS_FILENAME).read_text()
    assert "accepted this change at the review round cap" in followups
    assert "`F1`" in followups
    assert any(call[:3] == ["gh", "pr", "edit"] for call in gh_calls)


def test_completed_review_round_is_recorded_once_per_round_number(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    result = _request_changes_result()
    result.review_round = 1

    assert _run_phase(tmp_path, run, result, []) == "failed"
    assert _run_phase(tmp_path, run, result, []) == "failed"

    rounds = review_convergence.load_review_rounds(run_dir)
    assert [(r.round_number, r.decision, r.reviewed_head_sha) for r in rounds] == [
        (1, "request_changes", HEAD)
    ]


def test_review_round_is_not_recorded_when_phase_fails_after_review(tmp_path: Path):
    run = _run()
    result = _request_changes_result()
    result.review_round = 1
    moved_pr = {"number": 42, "url": "u", "headRefOid": "c" * 40, "mergeStateStatus": "CLEAN"}

    with (
        patch.object(orch, "_find_open_pr_for_branch", return_value=(moved_pr, moved_pr)),
        patch.object(orch, "_remote_branch_head_sha", return_value="c" * 40),
    ):
        patches, pr = _patch_local_review_phase(tmp_path, result, tmp_path / "review-result.json")
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patches[6], patches[7], patches[8],
        ):
            outcome = orch._phase_review_local(
                run, tmp_path, pr_data=pr, expected_head_sha=HEAD, expected_base_sha=BASE
            )

    assert outcome == "failed" and "PR head changed" in run.last_error
    assert review_convergence.load_review_rounds(_run_dir(tmp_path, run)) == []


def test_operator_accept_at_cap_reviews_again_when_head_moved(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    _run_phase(tmp_path, run, _request_changes_result(), [])
    stale = _request_changes_result()
    stale.reviewed_head_sha = "c" * 40
    stale.save(tmp_path, run.run_id)
    with patch.object(orch, "cmd_run", return_value=0):
        orch._cmd_input_review_round_cap(
            tmp_path, argparse.Namespace(spec=run.spec_id), accept=True, message=""
        )
    approved = orch.ReviewResult(
        status="approved", summary="Fine.", reviewed_head_sha=HEAD, reviewed_base_sha=BASE
    )

    result_path = tmp_path / "review-result.json"
    patches, pr = _patch_local_review_phase(tmp_path, approved, result_path)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
        patches[7] as run_review, patches[8], patches[9], patches[10], patches[11], patches[12],
        patch.object(orch, "run_subprocess", return_value=subprocess.CompletedProcess([], 0, "", "")),
    ):
        outcome = orch._phase_review_local(
            run, tmp_path, pr_data=pr, expected_head_sha=HEAD, expected_base_sha=BASE
        )

    assert run_review.called
    assert outcome == "passed"
    assert "Accepted by the operator" not in run.review_decision_summary
    assert orch.OperatorRequest.load(tmp_path, run.run_id).status == "consumed"


def test_operator_accept_at_cap_without_saved_review_runs_a_review(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    _run_phase(tmp_path, run, _request_changes_result(), [])
    with patch.object(orch, "cmd_run", return_value=0):
        orch._cmd_input_review_round_cap(
            tmp_path, argparse.Namespace(spec=run.spec_id), accept=True, message=""
        )
    (run_dir / "review-result.json").unlink()
    assert orch.ReviewResult.load(tmp_path, run.run_id) is None

    approved = orch.ReviewResult(status="approved", summary="Fine.", reviewed_head_sha=HEAD)
    assert _run_phase(tmp_path, run, approved, []) == "passed"

    assert "Accepted by the operator" not in run.review_decision_summary


def test_round_cap_input_rejects_runs_without_cap_request(tmp_path: Path, capsys):
    run = _run()
    run.save(tmp_path)

    rc = orch._cmd_input_review_round_cap(
        tmp_path, argparse.Namespace(spec=run.spec_id), accept=True, message=""
    )

    assert rc == 1
    assert "no pending review round-cap request" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Follow-ups in the PR body
# ---------------------------------------------------------------------------


def test_pr_body_followups_section_is_appended_and_replaced():
    section = review_convergence.render_pr_followups_section([_finding("N2", "P2", line=7)])
    body = review_convergence.pr_body_with_followups("## Summary\nChange.\n", section)

    assert body == "## Summary\nChange.\n\n## Follow-ups\n- [P2] `N2` src/app.py:7: Problem N2: Details.\n"
    replaced = review_convergence.pr_body_with_followups(
        body + "\n## Notes\nkeep\n",
        review_convergence.render_pr_followups_section([_finding("N3", "P3")]),
    )
    assert "N2" not in replaced and "`N3`" in replaced and "## Notes\nkeep" in replaced
    assert review_convergence.pr_body_with_followups(body, "") == "## Summary\nChange.\n"


def test_approval_writes_non_blocking_followups_and_updates_pr_body(tmp_path: Path):
    run = _run()
    approved = orch.ReviewResult(
        status="approved",
        summary="Fine.",
        reviewed_head_sha=HEAD,
        reviewed_base_sha=BASE,
        outstanding_findings=[{**_finding("N2", "P2"), "origin": "new", "blocking": False}],
    )
    gh_calls: list = []

    result = _run_phase(tmp_path, run, approved, gh_calls)

    assert result == "passed"
    followups = (_run_dir(tmp_path, run) / review_convergence.REVIEW_FOLLOWUPS_FILENAME).read_text()
    assert "[P2] `N2` src/app.py:10" in followups
    edit = next(call for call in gh_calls if call[:3] == ["gh", "pr", "edit"])
    body = Path(edit[edit.index("--body-file") + 1]).read_text()
    assert body.startswith("## Summary\nChange.\n\n## Follow-ups\n- [P2] `N2`")


# ---------------------------------------------------------------------------
# Codex review permission profile
# ---------------------------------------------------------------------------


def test_codex_review_profile_reads_checkout_and_denies_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "review-checkout"
    scratch = tmp_path / "scratch"
    provider_home = tmp_path / "auth" / ".spec-codex-home"
    operator_secret = tmp_path / "home" / ".ssh"
    for path in (checkout, scratch, provider_home, operator_secret):
        path.mkdir(parents=True)
    monkeypatch.setattr(agent_adapter, "protected_operator_paths", lambda: [operator_secret])

    cmd = CodexAgent().build_review_command(
        prompt="p",
        output_path=scratch / "out.json",
        schema_path=scratch / "schema.json",
        writable_temp_dir=scratch,
        review_checkout=checkout,
        provider_home=provider_home,
        reasoning_effort="high",
    )

    filesystem = next(c for c in cmd if c.startswith("permissions.specbutler-review.filesystem="))
    assert '":root"="read"' in filesystem
    assert f'"{checkout.resolve()}"="read"' in filesystem
    assert f'"{scratch.resolve()}"="write"' in filesystem
    assert f'"{provider_home.resolve()}"="deny"' in filesystem
    assert f'"{operator_secret.resolve()}"="deny"' in filesystem
    assert f'"{checkout.resolve()}"="deny"' not in filesystem
    assert 'default_permissions="specbutler-review"' in cmd
    assert "permissions.specbutler-review.network={enabled=false}" in cmd
    assert "features.shell_tool=false" not in cmd
    assert "-s" not in cmd
    assert 'model_reasoning_effort="high"' in cmd


def test_codex_review_without_checkout_keeps_no_tool_boundary(tmp_path: Path):
    cmd = CodexAgent().build_review_command(
        prompt="p", output_path=tmp_path / "out.json", writable_temp_dir=tmp_path
    )

    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert "features.shell_tool=false" in cmd
    assert not any(c.startswith("default_permissions=") for c in cmd)


# ---------------------------------------------------------------------------
# Configuration and doctor
# ---------------------------------------------------------------------------


def test_review_config_defaults_and_validation():
    assert _parse_review_section({}).max_rounds == 3
    assert _parse_review_section({}).rereview_reasoning_effort == ""
    parsed = _parse_review_section({"max_rounds": 5, "rereview_reasoning_effort": "Low"})
    assert (parsed.max_rounds, parsed.rereview_reasoning_effort) == (5, "low")
    with pytest.raises(SpecConfigError):
        _parse_review_section({"max_rounds": 0})
    with pytest.raises(SpecConfigError):
        _parse_review_section({"rereview_reasoning_effort": "extreme"})


def test_rereview_keeps_first_pass_reasoning_effort_by_default():
    assert orch.LOCAL_REVIEW_REASONING_EFFORT == orch.LOCAL_REVIEW_FIRST_PASS_REASONING_EFFORT


def test_review_prompt_source_label_reports_repo_level_or_bundled(tmp_path: Path):
    assert review_convergence.review_prompt_source_label(tmp_path) == review_convergence.BUNDLED_REVIEW_PROMPT_LABEL
    prompts = tmp_path / ".github" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "codex-review.md").write_text("x")
    assert review_convergence.review_prompt_source_label(tmp_path) == "repo-level .github/prompts/codex-review.md"
    (prompts / "review.md").write_text("x")
    assert review_convergence.review_prompt_source_label(tmp_path) == "repo-level .github/prompts/review.md"


def test_doctor_reports_review_prompt_in_effect(tmp_path: Path):
    from spec_runtime import doctor

    check = doctor._review_prompt_check(tmp_path)

    assert check.name == "review prompt"
    assert check.detail == review_convergence.BUNDLED_REVIEW_PROMPT_LABEL


def test_codex_review_prompt_text_describes_read_only_command_access(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    run_dir.mkdir(parents=True)
    (run_dir / "gate-status.json").write_text(
        json.dumps({"gates": {"test": {"last_status": "passed", "last_command": "make test"}}})
    )
    run.spec_path = "specs/my-feature.md"
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "my-feature.md").write_text("# Spec\n")

    codex_evidence = orch._format_gate_evidence_for_review(tmp_path, run, review_agent="codex")
    claude_evidence = orch._format_gate_evidence_for_review(tmp_path, run, review_agent="claude")
    codex_spec = orch._host_materialized_review_spec_note(run, tmp_path, review_agent="codex")
    claude_spec = orch._host_materialized_review_spec_note(run, tmp_path, review_agent="claude")

    assert "may run read-only commands" in codex_evidence
    assert "cannot run additional commands" not in codex_evidence
    assert "cannot run additional commands" in claude_evidence
    assert "model tools are unavailable" not in codex_spec
    assert "model tools are unavailable" in claude_spec
