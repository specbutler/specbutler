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


def test_incremental_diff_keeps_deleted_and_renamed_files_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(tmp_path, "init", "-q")
    body = "".join(f"line {i}\n" for i in range(1, 21))
    (tmp_path / "gone.py").write_text(body)
    (tmp_path / "old_name.py").write_text("".join(f"keep {i}\n" for i in range(1, 21)))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "rm", "-q", "gone.py", "blob.bin")
    _git(tmp_path, "mv", "old_name.py", "new_name.py")
    _git(tmp_path, "commit", "-qm", "second")
    second = _git(tmp_path, "rev-parse", "HEAD")
    # Operator git config must not change the parsed paths.
    _git(tmp_path, "config", "diff.noprefix", "true")

    _diff, ranges = orch._local_review_incremental_diff(
        tmp_path, last_reviewed_head_sha=first, head_sha=second
    )

    assert ranges is not None
    assert ranges["gone.py"] == [(1, 20)]
    assert ranges["blob.bin"] == [review_convergence.WHOLE_FILE_SPAN]
    assert ranges["new_name.py"] == [review_convergence.WHOLE_FILE_SPAN]
    assert ranges["old_name.py"] == [review_convergence.WHOLE_FILE_SPAN]
    computed = compute_review_decision(
        model_decision="approved",
        findings=[_finding("N1", "P1", file="gone.py", line=7)],
        prior_findings=[_finding("F1", "P1")],
        prior_classifications=[{"id": "F1", "status": "resolved"}],
        changed_ranges=ranges,
    )
    assert computed.decision == "request_changes" and computed.blocking_ids == ["N1"]


def test_changed_line_ranges_handles_deletion_rename_and_quoted_paths():
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-a\n-b\n-c\n"
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -4,2 +4 @@\n"
        "-x\n-y\n+z\n"
        'diff --git "a/sp\\303\\251c.py" "b/sp\\303\\251c.py"\n'
        '--- "a/sp\\303\\251c.py"\n'
        '+++ "b/sp\\303\\251c.py"\n'
        "@@ -2,0 +3,2 @@\n"
        "+n\n+m\n"
        "diff --git a/empty.txt b/empty.txt\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
    )

    ranges = review_convergence.changed_line_ranges(diff)

    assert ranges == {
        "gone.py": [(1, 3)],
        "new.py": [(4, 4)],
        "old.py": [(4, 5)],
        "spéc.py": [(3, 4)],
        "empty.txt": [review_convergence.WHOLE_FILE_SPAN],
    }


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


def test_new_same_file_finding_colliding_with_non_blocking_prior_id_still_blocks():
    # F1 is an unresolved, non-blocking P1 from an earlier round at line 10.
    # The reviewer reuses F1 for a distinct P1 on newly changed lines 40-42.
    prior = {**_finding("F1", "P1"), "blocking": False}
    new = {**_finding("F1", "P1", line=40), "end_line": 42, "title": "Different defect"}
    computed = compute_review_decision(
        model_decision="approved",
        findings=[new],
        prior_findings=[prior],
        prior_classifications=[{"id": "F1", "status": "unresolved"}],
        changed_ranges={"src/app.py": [(40, 42)]},
    )

    assert computed.decision == "request_changes"
    assert computed.blocking_ids == ["F1-new"]
    assert [(f["id"], f["blocking"]) for f in computed.outstanding_findings] == [
        ("F1", False),
        ("F1-new", True),
    ]


def test_same_id_finding_with_overlapping_lines_restates_prior():
    prior = {**_finding("F1", "P1"), "start_line": 8, "end_line": 12, "blocking": False}
    computed = compute_review_decision(
        model_decision="request_changes",
        findings=[{**_finding("F1", "P1", line=11), "title": "Reworded"}],
        prior_findings=[prior],
        prior_classifications=[{"id": "F1", "status": "unresolved"}],
        changed_ranges={"src/app.py": [(11, 11)]},
    )

    assert computed.decision == "approved"
    assert [f["id"] for f in computed.outstanding_findings] == ["F1"]


@pytest.mark.parametrize(
    ("title", "expected_ids"),
    [("Problem F1", ["F1"]), ("Another defect", ["F1", "F1-new"])],
)
def test_same_id_finding_without_lines_restates_prior_only_with_same_title(title, expected_ids):
    prior = {**_finding("F1", "P1"), "start_line": None, "end_line": None, "blocking": False}
    new = {**_finding("F1", "P1"), "start_line": None, "end_line": None, "title": title}
    computed = compute_review_decision(
        model_decision="approved",
        findings=[new],
        prior_findings=[prior],
        prior_classifications=[{"id": "F1", "status": "unresolved"}],
        changed_ranges=None,
    )

    assert [f["id"] for f in computed.outstanding_findings] == expected_ids


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


# One incremental -U0 diff exercising every file-level change shape.
_ELIGIBILITY_DIFF = (
    "diff --git a/gone.py b/gone.py\n"
    "deleted file mode 100644\n"
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1,30 +0,0 @@\n"
    "-x\n"
    "diff --git a/old.py b/new.py\n"
    "similarity index 90%\n"
    "rename from old.py\n"
    "rename to new.py\n"
    "--- a/old.py\n"
    "+++ b/new.py\n"
    "@@ -20,2 +20 @@\n"
    "-x\n-y\n+z\n"
    "diff --git a/before.py b/after.py\n"
    "similarity index 100%\n"
    "rename from before.py\n"
    "rename to after.py\n"
    "diff --git a/added.py b/added.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/added.py\n"
    "@@ -0,0 +1,5 @@\n"
    "+a\n"
    'diff --git "a/sp ac\\303\\251.txt" "b/sp ac\\303\\251.txt"\n'
    "new file mode 100644\n"
    "index 0000000..e69de29\n"
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -40,3 +40,3 @@\n"
    "-a\n+b\n"
    "@@ -61,2 +60,0 @@\n"
    "-gone\n"
)


def _f(finding_id: str, severity: str, file: str = "src/app.py", line: int | None = 10, **extra) -> dict:
    return {**_finding(finding_id, severity, file=file, line=line or 0), "start_line": line, "end_line": line, **extra}


_ELIGIBILITY_CASES = [
    # (case, prior findings, classifications, new findings, decision, [(id, blocking)])
    ("new P1 on a deleted file", [], [], [_f("N1", "P1", "gone.py", 7)], "request_changes", [("N1", True)]),
    ("new P1 on a renamed file's new side", [], [], [_f("N1", "P1", "new.py", 20)], "request_changes", [("N1", True)]),
    ("new P1 on a renamed file's removed lines", [], [], [_f("N1", "P1", "old.py", 21)], "request_changes", [("N1", True)]),
    ("new P1 on a pure rename", [], [], [_f("N1", "P1", "after.py", 3)], "request_changes", [("N1", True)]),
    ("new P1 on the old name of a pure rename", [], [], [_f("N1", "P1", "before.py", 3)], "request_changes", [("N1", True)]),
    ("new P1 on a new file", [], [], [_f("N1", "P1", "added.py", 2)], "request_changes", [("N1", True)]),
    ("new P1 on an empty new file with a quoted path", [], [], [_f("N1", "P1", "sp acé.txt", None)], "request_changes", [("N1", True)]),
    ("new P1 next to a pure deletion", [], [], [_f("N1", "P1", line=61)], "request_changes", [("N1", True)]),
    ("new file-level P1 on a changed file", [], [], [_f("N1", "P1", line=None)], "request_changes", [("N1", True)]),
    ("new P1 with an absolute checkout path", [], [], [_f("N1", "P1", "/tmp/checkout/src/app.py", 41)], "request_changes", [("N1", True)]),
    ("new P1 with a diff-side prefix", [], [], [_f("N1", "P1", "b/src/app.py", 41)], "request_changes", [("N1", True)]),
    ("new P1 on unchanged lines", [], [], [_f("N1", "P1", line=10)], "approved", [("N1", False)]),
    ("new P1 on an unchanged file", [], [], [_f("N1", "P1", "src/other.py", 41)], "approved", [("N1", False)]),
    ("new P1 without a file", [], [], [_f("N1", "P1", "", None)], "approved", [("N1", False)]),
    ("new P0 anywhere", [], [], [_f("N1", "P0", "src/other.py", 1)], "request_changes", [("N1", True)]),
    (
        "restated non-blocking prior whose lines moved into the change is treated as new",
        [_f("F1", "P1", blocking=False)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1", line=41)],
        "request_changes",
        [("F1", False), ("F1-new", True)],
    ),
    (
        "restated non-blocking prior at the same lines stays non-blocking",
        [_f("F1", "P1", blocking=False)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1", line=10)],
        "approved",
        [("F1", False)],
    ),
    (
        "id collision at a different location in the change blocks",
        [_f("F1", "P2")],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1", "new.py", 20, title="Other")],
        "request_changes",
        [("F1", False), ("F1-new", True)],
    ),
    (
        "id collision with a resolved prior blocks",
        [_f("F1", "P1")],
        [{"id": "F1", "status": "resolved"}],
        [_f("F1", "P1", line=41)],
        "request_changes",
        [("F1-new", True)],
    ),
    (
        "unresolved prior P1 restated as P2 keeps blocking",
        [_f("F1", "P1", blocking=True)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P2")],
        "request_changes",
        [("F1", True)],
    ),
    (
        "unresolved prior P0 restated as P3 keeps blocking",
        [_f("F1", "P0", blocking=True)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P3")],
        "request_changes",
        [("F1", True)],
    ),
    (
        "unclassified legacy prior P1 restated as P2 keeps blocking",
        [_f("F1", "P1")],
        [],
        [_f("F1", "P2")],
        "request_changes",
        [("F1", True)],
    ),
    (
        "prior P1 restated as P2 and resolved stops blocking",
        [_f("F1", "P1", blocking=True)],
        [{"id": "F1", "status": "resolved"}],
        [_f("F1", "P2")],
        "approved",
        [("F1-new", False)],
    ),
    (
        "prior P1 restated as P2 and withdrawn stops blocking",
        [_f("F1", "P1", blocking=True)],
        [{"id": "F1", "status": "withdrawn"}],
        [_f("F1", "P2")],
        "approved",
        [("F1-new", False)],
    ),
    (
        "prior P0 downgraded to P1 at the same lines keeps blocking",
        [_f("F1", "P0", blocking=True)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1")],
        "request_changes",
        [("F1", True)],
    ),
    (
        "restatement without a severity keeps the prior eligibility",
        [_f("F1", "P1", blocking=True)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "")],
        "request_changes",
        [("F1", True)],
    ),
    (
        "prior P2 raised to P1 in the change supersedes the prior",
        [_f("F1", "P2", line=41, blocking=False)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1", line=41)],
        "request_changes",
        [("F1-new", True)],
    ),
    (
        "prior P2 raised to P1 outside the change stays non-blocking",
        [_f("F1", "P2", blocking=False)],
        [{"id": "F1", "status": "unresolved"}],
        [_f("F1", "P1")],
        "approved",
        [("F1-new", False)],
    ),
    (
        "legacy prior P1 without a blocking flag blocks",
        [_f("F1", "P1")],
        [],
        [],
        "request_changes",
        [("F1", True)],
    ),
    (
        "legacy prior without severity or location does not block",
        [{"id": "F1", "title": "Old"}],
        [],
        [],
        "approved",
        [("F1", False)],
    ),
    (
        "legacy priors sharing an id are both kept",
        [_f("F1", "P2"), _f("F1", "P1", line=80)],
        [],
        [],
        "request_changes",
        [("F1", False), ("F1", True)],
    ),
    (
        "new findings without or with duplicate ids get unique ids",
        [],
        [],
        [_f("", "P0", line=41), _f("N2", "P3"), _f("N2", "P3", line=12)],
        "request_changes",
        [("N1", True), ("N2", False), ("N2-new", False)],
    ),
]


@pytest.mark.parametrize(
    ("prior", "classifications", "findings", "decision", "expected"),
    [case[1:] for case in _ELIGIBILITY_CASES],
    ids=[case[0] for case in _ELIGIBILITY_CASES],
)
def test_rereview_eligibility_and_carry_forward_table(prior, classifications, findings, decision, expected):
    computed = compute_review_decision(
        model_decision="approved" if decision == "request_changes" else "request_changes",
        findings=findings,
        prior_findings=prior,
        prior_classifications=classifications,
        changed_ranges=review_convergence.changed_line_ranges(_ELIGIBILITY_DIFF),
    )

    assert computed.decision == decision
    assert [(f["id"], f["blocking"]) for f in computed.outstanding_findings] == expected
    assert computed.blocking_ids == [i for i, blocking in expected if blocking]
    # Blocking eligibility survives a later round that only restates the findings.
    carried = compute_review_decision(
        model_decision="approved",
        findings=[],
        prior_findings=computed.outstanding_findings,
        prior_classifications=[],
        changed_ranges={},
    )
    assert [f["blocking"] for f in carried.outstanding_findings] == [b for _, b in expected]


def test_unresolved_prior_blocker_restated_lower_keeps_severity_across_rounds():
    prior = [_f("F1", "P1", blocking=True)]
    for _ in range(2):
        computed = compute_review_decision(
            model_decision="approved",
            findings=[_f("F1", "P2")],
            prior_findings=prior,
            prior_classifications=[{"id": "F1", "status": "unresolved"}],
            changed_ranges={},
        )
        assert computed.decision == "request_changes"
        assert computed.blocking_ids == ["F1"]
        [carried] = computed.outstanding_findings
        assert (carried["severity"], carried["reported_severity"], carried["blocking"]) == ("P1", "P2", True)
        assert review_convergence.non_blocking_followups(computed.outstanding_findings) == []
        prior = computed.outstanding_findings


def test_legacy_ledger_findings_without_or_with_duplicate_ids_get_classifiable_ids(tmp_path: Path):
    path = review_convergence.review_rounds_path(tmp_path)
    path.write_text(
        json.dumps(
            {
                "rounds": [
                    {
                        "round_number": 1,
                        "reviewed_head_sha": HEAD,
                        "decision": "request_changes",
                        "outstanding_findings": [
                            {"title": "No id", "severity": "P1"},
                            {"id": "F1", "title": "First", "severity": "P2"},
                            {"id": "F1", "title": "Second", "severity": "P1"},
                        ],
                    }
                ]
            }
        )
    )

    (round_one,) = review_convergence.load_review_rounds(tmp_path)
    ids = [f["id"] for f in round_one.outstanding_findings]
    section = review_convergence.render_prior_findings_section(
        round_one.outstanding_findings, round_number=2, implementer_summary=""
    )
    computed = compute_review_decision(
        model_decision="request_changes",
        findings=[],
        prior_findings=round_one.outstanding_findings,
        prior_classifications=[{"id": i, "status": "resolved"} for i in ids],
        changed_ranges={},
    )

    assert ids == ["R1-1", "F1", "R1-3"]
    assert all(f"id `{i}`" in section for i in ids)
    assert computed.decision == "approved"


def _gate_evaluation(
    tmp_path: Path,
    *,
    decision: str,
    findings: list[dict],
    schema: Path | None = None,
    **extra,
) -> GateEvaluation:
    from spec_runtime.review_gate import evaluate_review_gate

    schema = schema or Path(orch.__file__).parent / "templates" / "review-schema.json"
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


def _legacy_bundled_review_schema() -> dict:
    """The bundled review schema as shipped before prior_findings existed."""
    schema = json.loads((Path(orch.__file__).parent / "templates" / "review-schema.json").read_text())
    del schema["properties"]["prior_findings"]
    schema["required"].remove("prior_findings")
    assert schema["additionalProperties"] is False
    return schema


def _write_legacy_repo_schema(repo: Path) -> Path:
    path = repo / ".github" / "schemas" / "codex-review.schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_legacy_bundled_review_schema(), indent=2) + "\n")
    return path


def test_legacy_repo_schema_is_upgraded_for_launch_without_touching_the_repo(tmp_path: Path):
    repo_schema = _write_legacy_repo_schema(tmp_path)
    original = repo_schema.read_text()
    out_dir = tmp_path / "review-out"

    resolved = orch._resolve_local_review_schema_path(tmp_path, out_dir)

    assert resolved.parent == out_dir
    upgraded = json.loads(resolved.read_text())
    assert "prior_findings" in upgraded["properties"]
    assert "prior_findings" in upgraded["required"]
    assert upgraded["additionalProperties"] is False
    assert repo_schema.read_text() == original


def test_current_or_missing_repo_schema_is_used_as_is(tmp_path: Path):
    bundled = Path(orch.__file__).parent / "templates" / "review-schema.json"
    assert orch._resolve_local_review_schema_path(tmp_path, tmp_path / "out").resolve() == bundled.resolve()
    repo_schema = tmp_path / ".github" / "schemas" / "codex-review.schema.json"
    repo_schema.parent.mkdir(parents=True)
    repo_schema.write_text(bundled.read_text())
    assert orch._resolve_local_review_schema_path(tmp_path, tmp_path / "out") == repo_schema
    assert not (tmp_path / "out").exists()


def test_rereview_under_previous_bundled_schema_accepts_classifications(tmp_path: Path):
    legacy_path = _write_legacy_repo_schema(tmp_path)
    resolved = orch._resolve_local_review_schema_path(tmp_path, tmp_path / "out")
    prior = [{**_finding("F1", "P1"), "blocking": True}]
    last_round = review_convergence.ReviewRound(
        round_number=1,
        reviewed_head_sha=BASE,
        decision="request_changes",
        outstanding_findings=prior,
    )
    run = _run()
    run_dir = _run_dir(tmp_path, run)

    # Both the run-local upgraded schema and the legacy file itself (as used
    # by direct gate callers) accept the classification.
    for schema in (resolved, legacy_path):
        evaluation = _gate_evaluation(
            tmp_path,
            decision="request_changes",
            findings=[],
            schema=schema,
            prior_findings=[{"id": "F1", "status": "resolved", "note": "fixed"}],
        )
        assert evaluation.result_payload["status"] == "request_changes", evaluation.human_summary
        result = orch._apply_computed_local_review_decision(
            run,
            evaluation,
            review_run_dir=run_dir,
            last_round=last_round,
            round_number=2,
            changed_ranges={},
            expected_head_sha=HEAD,
            expected_base_sha=BASE,
        )
        assert result.result_payload["status"] == "approved", result.human_summary


def test_claude_review_launch_with_legacy_repo_schema_embeds_and_validates_classifications(
    tmp_path: Path,
):
    from contextlib import contextmanager

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_legacy_repo_schema(repo)
    review_worktree = tmp_path / "review-worktree"
    review_worktree.mkdir()
    run = orch.RunState(
        run_id="review-run",
        spec_id="feature",
        branch="code/feature--run",
        review_agent="claude",
    )
    raw_review = json.dumps(
        {
            "schema_version": "1",
            "decision": "approved",
            "summary": "Fixed.",
            "reviewed_head_sha": HEAD,
            "reviewed_base_sha": BASE,
            "findings": [],
            "reviewer_role": "independent-review",
            "reviewer_agent": "claude",
            "reviewed_at": "2026-01-01T00:00:00Z",
            "prior_findings": [{"id": "F1", "status": "resolved", "note": "fixed"}],
        }
    )
    prompts: list[str] = []

    @contextmanager
    def fake_review_worktree(*_args: object, **_kwargs: object):
        yield review_worktree

    def fake_review_exec(_repo_root: Path, cmd: list[str], **kwargs: object):
        prompts.append(str(kwargs["input_text"]))
        return subprocess.CompletedProcess(cmd, 0, raw_review, "")

    with (
        patch.object(orch, "_temporary_review_worktree", side_effect=fake_review_worktree),
        patch.object(orch, "_render_local_review_prompt", return_value="review"),
        patch.object(orch, "_host_materialized_review_spec_note", return_value=""),
        patch.object(orch, "_claude_review_evidence_prompt_note", return_value=""),
        patch.object(orch, "require_host_agent_available"),
        patch.object(orch, "_run_local_review_subprocess", side_effect=fake_review_exec),
    ):
        result, _path = orch._run_local_review(
            run,
            repo,
            repo_name="owner/repo",
            pr_number=1,
            pr_body="",
            expected_head_sha=HEAD,
            expected_base_sha=BASE,
        )

    assert result.status == "approved", result.summary
    assert '"prior_findings"' in prompts[0]


# ---------------------------------------------------------------------------
# Legacy inputs written before review convergence
# ---------------------------------------------------------------------------


def test_custom_legacy_repo_schema_keeps_its_fields_and_accepts_classifications(tmp_path: Path):
    custom = {
        "type": "object",
        "required": ["decision", "summary", "reviewed_head_sha", "reviewed_base_sha", "findings"],
        "properties": {
            "schema_version": {"type": "string"},
            "decision": {"type": "string", "enum": ["approved", "request_changes"]},
            "summary": {"type": "string"},
            "reviewed_head_sha": {"type": "string"},
            "reviewed_base_sha": {"type": "string"},
            "reviewer_role": {"type": "string"},
            "reviewer_agent": {"type": "string"},
            "reviewed_at": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "object"}},
            "risk_level": {"type": "string"},
        },
        "additionalProperties": False,
    }
    path = tmp_path / ".github" / "schemas" / "codex-review.schema.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(custom))

    upgraded = json.loads(orch._resolve_local_review_schema_path(tmp_path, tmp_path / "out").read_text())
    assert upgraded["properties"]["risk_level"] == {"type": "string"}
    assert "prior_findings" in upgraded["properties"]
    assert upgraded["required"][-1] == "prior_findings"

    classification = [{"id": "F1", "status": "resolved", "note": "fixed"}]
    for extra in ({"prior_findings": classification}, {}):
        evaluation = _gate_evaluation(
            tmp_path, decision="approved", findings=[], schema=path, risk_level="low", **extra
        )
        assert evaluation.result_payload["status"] == "approved", evaluation.human_summary


def test_legacy_review_result_json_without_convergence_fields_loads(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    run_dir.mkdir(parents=True)
    legacy = {
        "status": "request_changes",
        "summary": "Fix it.",
        "findings": [{**_finding("F1", "P1"), "legacy_extra": True}],
        "attempt_number": 2,
        "reviewed_head_sha": HEAD,
        "reviewed_base_sha": BASE,
        "reviewer_role": "independent-review",
        "reviewer_agent": "codex",
        "source_check_name": "review",
        "source_check_url": "",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    (run_dir / "review-result.json").write_text(json.dumps(legacy))
    gate_output = tmp_path / "review-decision.json"
    gate_output.write_text(json.dumps({k: v for k, v in legacy.items() if k != "attempt_number"}))

    for loaded in (
        orch.ReviewResult.load(tmp_path, run.run_id),
        orch._load_review_result_from_gate_output(gate_output),
    ):
        assert loaded is not None
        assert loaded.status == "request_changes"
        assert [f.id for f in loaded.findings] == ["F1"]
        assert loaded.review_round == 0
        assert loaded.model_decision == ""
        assert loaded.outstanding_findings == []
        assert loaded.prior_findings == []


def test_legacy_ledger_entries_missing_fields_load_with_defaults(tmp_path: Path):
    review_convergence.review_rounds_path(tmp_path).write_text(
        json.dumps(
            {
                "rounds": [
                    {"reviewed_head_sha": BASE, "decision": "request_changes"},
                    {"reviewed_head_sha": HEAD, "outstanding_findings": [_finding("F1", "P2")]},
                ]
            }
        )
    )

    rounds = review_convergence.load_review_rounds(tmp_path)

    assert [r.round_number for r in rounds] == [1, 2]
    assert rounds[1].decision == ""
    assert review_convergence.load_extra_rounds_granted(tmp_path) == 0
    assert review_convergence.request_changes_round_count(rounds) == 1
    assert not review_convergence.round_cap_reached(tmp_path, max_rounds=3)
    # Missing ``blocking`` falls back to severity; a P2 stays a follow-up.
    computed = compute_review_decision(
        model_decision="approved",
        findings=[],
        prior_findings=rounds[-1].outstanding_findings,
        prior_classifications=[],
        changed_ranges={},
    )
    assert computed.decision == "approved"
    assert review_convergence.non_blocking_followups(computed.outstanding_findings)[0]["id"] == "F1"


def test_old_format_sticky_comment_payload_still_parses(tmp_path: Path):
    from spec_runtime.review_feedback import extract_review_result_from_commit_status
    from spec_runtime.review_gate_sticky_comment import render_sticky_comment

    old_payload = {
        "status": "request_changes",
        "decision": "request_changes",
        "summary": "Fix it.",
        "reviewed_head_sha": HEAD,
        "reviewed_base_sha": BASE,
        "findings": [_finding("F1", "P1")],
        "reviewer_role": "independent-review",
        "reviewer_agent": "codex",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    comments = [{"body": render_sticky_comment(old_payload, pr_number=7)}]

    def fake_run_subprocess(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(comments), "")

    result = extract_review_result_from_commit_status(
        {"state": "failure", "context": "review"},
        repo_root=tmp_path,
        pr_number=7,
        expected_head_sha=HEAD,
        expected_base_sha=BASE,
        require_payload=True,
        run_subprocess=fake_run_subprocess,
    )

    assert result.status == "request_changes"
    assert [f.id for f in result.findings] == ["F1"]
    assert result.reviewed_head_sha == HEAD


def _write_legacy_review_result(repo: Path, run: orch.RunState, *, review_round: int | None = None) -> None:
    run_dir = _run_dir(repo, run)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "request_changes",
        "summary": "Fix it.",
        "findings": [_finding("F1", "P1")],
        "reviewed_head_sha": BASE,
        "reviewed_base_sha": BASE,
        "reviewer_role": "independent-review",
        "reviewer_agent": "codex",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    if review_round is not None:
        payload["review_round"] = review_round
    (run_dir / "review-result.json").write_text(json.dumps(payload))


def test_run_resumed_after_upgrade_seeds_round_ledger_from_legacy_review_result(tmp_path: Path):
    run = _run()
    run.review_changes = 1
    run_dir = _run_dir(tmp_path, run)
    _write_legacy_review_result(tmp_path, run)

    (seeded,) = orch._seed_review_rounds_from_legacy_result(tmp_path, run, run_dir)

    assert seeded.round_number == 1
    assert seeded.reviewed_head_sha == BASE
    assert seeded.decision == "request_changes"
    assert [f["id"] for f in seeded.outstanding_findings] == ["F1"]
    assert review_convergence.load_review_rounds(run_dir) == [seeded]
    # Seeded once: an existing ledger is never re-seeded.
    assert orch._seed_review_rounds_from_legacy_result(tmp_path, run, run_dir) == []


def test_legacy_seed_skips_fresh_runs_and_post_upgrade_results(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _write_legacy_review_result(tmp_path, run)
    assert orch._seed_review_rounds_from_legacy_result(tmp_path, run, run_dir) == []

    run.review_changes = 1
    _write_legacy_review_result(tmp_path, run, review_round=1)
    assert orch._seed_review_rounds_from_legacy_result(tmp_path, run, run_dir) == []
    assert not review_convergence.review_rounds_path(run_dir).exists()


def test_resumed_legacy_run_rereview_shows_and_resolves_legacy_findings(tmp_path: Path):
    from contextlib import contextmanager

    repo = tmp_path / "repo"
    repo.mkdir()
    review_worktree = tmp_path / "review-worktree"
    review_worktree.mkdir()
    run = orch.RunState(
        run_id="review-run",
        spec_id="feature",
        branch="code/feature--run",
        review_agent="claude",
        review_changes=1,
    )
    _write_legacy_review_result(repo, run)
    raw_review = json.dumps(
        {
            "schema_version": "1",
            "decision": "approved",
            "summary": "Fixed.",
            "reviewed_head_sha": HEAD,
            "reviewed_base_sha": BASE,
            "findings": [],
            "reviewer_role": "independent-review",
            "reviewer_agent": "claude",
            "reviewed_at": "2026-01-01T00:00:00Z",
            "prior_findings": [{"id": "F1", "status": "resolved", "note": "fixed"}],
        }
    )
    prompts: list[str] = []

    @contextmanager
    def fake_review_worktree(*_args: object, **_kwargs: object):
        yield review_worktree

    def fake_review_exec(_repo_root: Path, cmd: list[str], **kwargs: object):
        prompts.append(str(kwargs["input_text"]))
        return subprocess.CompletedProcess(cmd, 0, raw_review, "")

    with (
        patch.object(orch, "_temporary_review_worktree", side_effect=fake_review_worktree),
        patch.object(orch, "_render_local_review_prompt", return_value="review"),
        patch.object(orch, "_host_materialized_review_spec_note", return_value=""),
        patch.object(orch, "_claude_review_evidence_prompt_note", return_value=""),
        patch.object(orch, "require_host_agent_available"),
        patch.object(orch, "_local_review_incremental_diff", return_value=("", {})),
        patch.object(orch, "_run_local_review_subprocess", side_effect=fake_review_exec),
    ):
        result, _path = orch._run_local_review(
            run,
            repo,
            repo_name="owner/repo",
            pr_number=1,
            pr_body="",
            expected_head_sha=HEAD,
            expected_base_sha=BASE,
        )

    assert "Prior review findings" in prompts[0]
    assert "id `F1` [P1]" in prompts[0]
    assert result.review_round == 2
    assert result.status == "approved", result.summary


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


def _review_phase_with_reviewer(repo: Path, run: orch.RunState, review_result: orch.ReviewResult):
    """Run the local review phase and return (outcome, reviewer mock)."""
    patches, pr = _patch_local_review_phase(repo, review_result, repo / "review-result.json")
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
        patches[7] as run_review, patches[8], patches[9], patches[10], patches[11], patches[12],
        patch.object(orch, "run_subprocess", return_value=subprocess.CompletedProcess([], 0, "", "")),
    ):
        outcome = orch._phase_review_local(
            run, repo, pr_data=pr, expected_head_sha=HEAD, expected_base_sha=BASE
        )
    return outcome, run_review


def _approved_result(head: str = HEAD) -> orch.ReviewResult:
    return orch.ReviewResult(
        status="approved", summary="Looks fine.", reviewed_head_sha=head, reviewed_base_sha=BASE
    )


def test_resumed_review_on_unchanged_head_keeps_request_changes(tmp_path: Path):
    """Regression for #99: a fresh reviewer roll cannot clear a finding on unchanged code."""
    run = _run()
    first = _request_changes_result()
    first.review_round = 1
    assert _run_phase(tmp_path, run, first, []) == "failed"
    assert run.review_unaddressed_head_sha == HEAD

    # The run resumes straight into review on the same head; this roll would approve.
    outcome, run_review = _review_phase_with_reviewer(tmp_path, run, _approved_result())

    assert outcome == "failed"
    run_review.assert_not_called()
    assert run.review_decision_status == "request_changes"
    assert "Independent review requested changes" in run.last_error
    assert "head unchanged" in run.last_error
    kept = orch.ReviewResult.load(tmp_path, run.run_id)
    assert kept is not None and kept.status == "request_changes"
    assert [f.id for f in kept.findings] == ["F1"]
    # Keeping the result is not another review round.
    assert len(review_convergence.load_review_rounds(_run_dir(tmp_path, run))) == 1
    # The workflow classifies it like any requested change, so implement retries.
    classification = orch.classify_failure(
        run.last_error, phase="review", review_decision_status=run.review_decision_status
    )
    assert not classification.is_infrastructure


def test_resumed_review_at_round_cap_keeps_waiting_for_operator(tmp_path: Path):
    """A kept result at the cap must not grant another round without the operator."""
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "waiting-for-input"
    assert run.review_unaddressed_head_sha == HEAD
    rounds_before = len(review_convergence.load_review_rounds(run_dir))
    run.status = "running"  # `spec implement` resumes the waiting run at review

    outcome, run_review = _review_phase_with_reviewer(tmp_path, run, _approved_result())

    assert outcome == "waiting-for-input"
    run_review.assert_not_called()
    assert run.status == "waiting-for-input"
    request = orch.OperatorRequest.load(tmp_path, run.run_id)
    assert request is not None and request.status == "pending"
    assert request.kind == review_convergence.REVIEW_ROUND_CAP_REQUEST_KIND
    assert len(review_convergence.load_review_rounds(run_dir)) == rounds_before


def test_interactive_session_completing_implement_clears_unaddressed_review(tmp_path: Path):
    run = _run()
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "failed"
    run.phase = "implement"
    run.status = "waiting-for-input"
    run.save(tmp_path)
    orch.OperatorRequest(
        kind="agent_question",
        prompt="Is F1 a real defect?",
        requested_by_phase="implement",
        status="pending",
        request_attempt_number=orch._current_attempt_number(run),
    ).save(tmp_path, run.run_id)

    orch.resolve_operator_request(
        tmp_path, run, "F1 is a false positive; see the session notes.",
        source="spec input", session_completed_implement=True,
    )

    assert run.review_unaddressed_head_sha == ""


def test_resumed_review_after_implement_rereviews_unchanged_head(tmp_path: Path):
    """An implement attempt in between may rebut a finding; review convergence decides."""
    run = _run()
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "failed"

    with patch.dict(orch.PHASE_HANDLERS, {"implement": lambda _run, _repo: "passed"}):
        assert orch.run_single_phase(run, "implement", tmp_path) == "passed"
    assert run.review_unaddressed_head_sha == ""

    outcome, run_review = _review_phase_with_reviewer(tmp_path, run, _approved_result())

    run_review.assert_called_once()
    assert outcome == "passed"
    assert run.review_decision_status == "approved"


def test_resumed_review_on_moved_head_rereviews(tmp_path: Path):
    run = _run()
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "failed"
    run.review_unaddressed_head_sha = "c" * 40  # the blocked head, since replaced by HEAD

    outcome, run_review = _review_phase_with_reviewer(tmp_path, run, _approved_result())

    run_review.assert_called_once()
    assert outcome == "passed"


def test_resumed_workflow_returns_to_implement_when_review_head_unchanged(tmp_path: Path):
    """End to end through the workflow loop: review keeps its result, implement runs next."""
    run = _run()
    run.save(tmp_path)
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "failed"
    run.phase = "review"
    run.status = "failed"
    run.save(tmp_path)
    phases: list[str] = []

    def review_handler(review_run: orch.RunState, repo: Path) -> str:
        phases.append("review")
        outcome, run_review = _review_phase_with_reviewer(repo, review_run, _approved_result())
        assert run_review.call_count == (0 if phases.count("review") == 1 else 1)
        return outcome

    def recording(name: str):
        def handler(_run: orch.RunState, _repo: Path) -> str:
            phases.append(name)
            return "passed"
        return handler

    handlers = {phase: recording(phase) for phase in orch.PHASES}
    handlers["review"] = review_handler
    with (
        patch.dict(orch.PHASE_HANDLERS, handlers),
        patch.object(orch, "_ensure_orchestrator_process_group", return_value=None),
    ):
        result = orch.run_full_workflow(run, tmp_path, retry_cap=5)

    assert phases[:5] == ["review", "implement", "verify", "publish", "review"]
    assert phases[5:] == ["merge", "cleanup"]
    assert result == "passed"


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
    assert rc == 0
    cmd_run.assert_not_called()
    assert review_convergence.load_extra_rounds_granted(run_dir) == 1
    assert not review_convergence.round_cap_reached(
        run_dir, max_rounds=orch.SPEC_RUNTIME_CONFIG.review.max_rounds
    )
    resumed = orch.RunState.find_latest(tmp_path, run.spec_id)
    assert resumed is not None and resumed.phase == "implement"


@pytest.mark.parametrize("command", ["input", "steer", "resolver"])
def test_round_cap_answers_grant_one_round_without_launching(tmp_path: Path, command: str):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "waiting-for-input"
    with (
        patch.object(orch, "resolve_repo_root", return_value=tmp_path),
        patch.object(orch.sys.stdin, "isatty", return_value=False),
        patch.object(orch, "cmd_run") as launch,
    ):
        if command == "resolver":
            orch.resolve_operator_request(tmp_path, run, "Fix F1", source="tui-chat")
        else:
            handler = orch.cmd_input if command == "input" else orch.cmd_steer
            assert handler(argparse.Namespace(spec=run.spec_id, message="Fix F1", answer=True)) == 0
    launch.assert_not_called()
    saved = orch.RunState.find_latest(tmp_path, run.spec_id)
    request = orch.OperatorRequest.load(tmp_path, run.run_id)
    assert saved.status == "pending" and saved.phase == "implement"
    assert request.status == "resolved" and request.response == "Fix F1"
    assert request.continuation == orch.OPERATOR_CONTINUATION_RESUME_IMPLEMENT
    assert review_convergence.load_extra_rounds_granted(run_dir) == 1
    assert not review_convergence.round_cap_reached(
        run_dir, max_rounds=orch.SPEC_RUNTIME_CONFIG.review.max_rounds,
    )
    # Revising an answer on the same resolved request is not a new round grant.
    orch.resolve_operator_request(tmp_path, saved, "Clarified F1 guidance", source="test")
    assert review_convergence.load_extra_rounds_granted(run_dir) == 1


def test_invalid_round_cap_answer_does_not_grant_a_round(tmp_path: Path):
    run = _run()
    run_dir = _run_dir(tmp_path, run)
    _record_rounds(run_dir, orch.SPEC_RUNTIME_CONFIG.review.max_rounds)
    assert _run_phase(tmp_path, run, _request_changes_result(), []) == "waiting-for-input"
    with pytest.raises(RuntimeError, match="cannot be empty"):
        orch.resolve_operator_request(tmp_path, run, " ", source="test")
    assert review_convergence.load_extra_rounds_granted(run_dir) == 0
    assert orch.OperatorRequest.load(tmp_path, run.run_id).status == "pending"


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
    # Simulate an implement attempt in between so the second call re-reviews.
    run.review_unaddressed_head_sha = ""
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
        # The helper's own PR lookup (index 9) is skipped so the moved head above stays in effect.
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patches[6], patches[7], patches[8], patches[10], patches[11], patches[12],
            patch.object(orch, "run_subprocess", return_value=subprocess.CompletedProcess([], 0, "", "")),
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
    # The checkout shell is off by default (see the no-shell test below); this
    # pins the profile the shell path would use once Codex can bound it.
    monkeypatch.setattr(agent_adapter, "CODEX_REVIEW_CHECKOUT_SHELL", True)

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
    assert agent_adapter._toml_quote(str(checkout.resolve())) + '="read"' in filesystem
    assert agent_adapter._toml_quote(str(scratch.resolve())) + '="write"' in filesystem
    assert agent_adapter._toml_quote(str(provider_home.resolve())) + '="deny"' in filesystem
    assert agent_adapter._toml_quote(str(operator_secret.resolve())) + '="deny"' in filesystem
    assert agent_adapter._toml_quote(str(checkout.resolve())) + '="deny"' not in filesystem
    assert 'default_permissions="specbutler-review"' in cmd
    assert "permissions.specbutler-review.network={enabled=false}" in cmd
    assert "features.shell_tool=false" not in cmd
    assert "-s" not in cmd
    assert 'model_reasoning_effort="high"' in cmd


def test_codex_review_with_checkout_has_no_shell_by_default(tmp_path: Path):
    """codex-cli 0.164 cannot limit the shell to inspection, so review keeps none."""
    checkout = tmp_path / "review-checkout"
    scratch = tmp_path / "scratch"
    checkout.mkdir()
    scratch.mkdir()

    cmd = CodexAgent().build_review_command(
        prompt="p",
        output_path=scratch / "out.json",
        writable_temp_dir=scratch,
        review_checkout=checkout,
    )

    assert "features.shell_tool=false" in cmd
    assert "features.unified_exec=false" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert not any(c.startswith("permissions.specbutler-review") for c in cmd)
    assert not any(c.startswith("approval_policy=") for c in cmd)
    assert str(checkout) not in cmd
    assert cmd[cmd.index("-C") + 1] == str(scratch)


def test_codex_review_checkout_shell_path_pins_never_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the checkout shell is enabled, nothing may escalate past the profile."""
    monkeypatch.setattr(agent_adapter, "CODEX_REVIEW_CHECKOUT_SHELL", True)
    checkout = tmp_path / "review-checkout"
    scratch = tmp_path / "scratch"
    checkout.mkdir()
    scratch.mkdir()

    cmd = CodexAgent().build_review_command(
        prompt="p",
        output_path=scratch / "out.json",
        writable_temp_dir=scratch,
        review_checkout=checkout,
    )

    # Nothing may escalate past the read-only, network-off review profile.
    assert 'approval_policy="never"' in cmd
    # No model reviewer and no execpolicy rule can pre-approve an escalation.
    assert 'approvals_reviewer="user"' in cmd
    assert "--ignore-rules" in cmd
    assert "--strict-config" in cmd
    # A later override must not loosen the policy.
    approvals = [c for c in cmd if c.startswith("approval_policy=")]
    reviewers = [c for c in cmd if c.startswith("approvals_reviewer=")]
    assert approvals == ['approval_policy="never"']
    assert reviewers == ['approvals_reviewer="user"']
    for loosening in (
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
        "--yolo",
        "-a",
        "--ask-for-approval",
    ):
        assert loosening not in cmd
    # The checkout is never writable and never the working root.
    filesystem = next(c for c in cmd if c.startswith("permissions.specbutler-review.filesystem="))
    assert agent_adapter._toml_quote(str(checkout.resolve())) + '="write"' not in filesystem
    assert cmd[cmd.index("-C") + 1] == str(scratch)
    # Codex 0.164 routes shell calls through the code-mode host; disabling
    # it would silently remove the inspection commands this path exists for.
    assert "features.code_mode_host=false" not in cmd
    assert "features.code_mode=false" in cmd


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
