"""Bounded local-review convergence: round ledger, prior findings, follow-ups.

Each completed local review (approved or request_changes) is recorded as a
*round* in ``review-rounds.json`` under the run's state directory. A round
stores the findings still outstanding after it, so the next re-review can be
shown exactly what it asked for last time and how the implementer responded.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

REVIEW_ROUNDS_FILENAME = "review-rounds.json"
REVIEW_FOLLOWUPS_FILENAME = "review-followups.md"
PR_FOLLOWUPS_HEADING = "## Follow-ups"
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})
PRIOR_FINDING_STATUSES = ("resolved", "unresolved", "withdrawn")
REVIEW_ROUND_CAP_REQUEST_KIND = "review_round_cap"
REVIEW_ROUND_CAP_ACCEPT = "accept"
REVIEW_ROUND_CAP_ANOTHER_ROUND = "another_round"
_RESPONSE_EXCERPT_MAX_CHARS = 400
_SUMMARY_MAX_CHARS = 240


REPO_REVIEW_PROMPT_PATHS = (
    Path(".github") / "prompts" / "review.md",
    Path(".github") / "prompts" / "codex-review.md",
)
BUNDLED_REVIEW_PROMPT_LABEL = "bundled spec_runtime/templates/review.md"


def resolve_review_prompt_path(repo_root: Path) -> Path | None:
    """Return the repo-level review prompt in effect, or None for the bundled one."""
    for relative in REPO_REVIEW_PROMPT_PATHS:
        candidate = repo_root / relative
        if candidate.is_file():
            return candidate
    return None


def review_prompt_source_label(repo_root: Path) -> str:
    path = resolve_review_prompt_path(repo_root)
    if path is None:
        return BUNDLED_REVIEW_PROMPT_LABEL
    return f"repo-level {path.relative_to(repo_root).as_posix()}"


@dataclass
class ReviewRound:
    round_number: int
    reviewed_head_sha: str
    reviewed_base_sha: str = ""
    decision: str = ""
    model_decision: str = ""
    recorded_at: str = ""
    # Findings still open after this round: unresolved prior findings carried
    # forward plus this round's new findings (all severities).
    outstanding_findings: list[dict] = field(default_factory=list)


def review_rounds_path(run_dir: Path) -> Path:
    return run_dir / REVIEW_ROUNDS_FILENAME


def load_review_rounds(run_dir: Path) -> list[ReviewRound]:
    path = review_rounds_path(run_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = payload.get("rounds") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    rounds: list[ReviewRound] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            round_number = int(entry.get("round_number", len(rounds) + 1))
        except (TypeError, ValueError):
            round_number = len(rounds) + 1
        findings = entry.get("outstanding_findings", [])
        rounds.append(
            ReviewRound(
                round_number=round_number,
                reviewed_head_sha=str(entry.get("reviewed_head_sha", "") or ""),
                reviewed_base_sha=str(entry.get("reviewed_base_sha", "") or ""),
                decision=str(entry.get("decision", "") or ""),
                model_decision=str(entry.get("model_decision", "") or ""),
                recorded_at=str(entry.get("recorded_at", "") or ""),
                outstanding_findings=[f for f in findings if isinstance(f, dict)]
                if isinstance(findings, list)
                else [],
            )
        )
    return rounds


def load_extra_rounds_granted(run_dir: Path) -> int:
    """Return how many extra request_changes rounds the operator granted."""
    try:
        payload = json.loads(review_rounds_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("extra_rounds_granted", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _write_ledger(run_dir: Path, rounds: list[ReviewRound], *, extra_rounds_granted: int) -> Path:
    path = review_rounds_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "rounds": [asdict(r) for r in rounds],
        "extra_rounds_granted": extra_rounds_granted,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def append_review_round(run_dir: Path, review_round: ReviewRound) -> Path:
    # A re-run of the same round (same number) replaces the earlier record
    # so one review round is never counted twice toward the cap.
    rounds = [r for r in load_review_rounds(run_dir) if r.round_number != review_round.round_number]
    rounds.append(review_round)
    return _write_ledger(run_dir, rounds, extra_rounds_granted=load_extra_rounds_granted(run_dir))


def grant_extra_review_round(run_dir: Path) -> Path:
    return _write_ledger(
        run_dir,
        load_review_rounds(run_dir),
        extra_rounds_granted=load_extra_rounds_granted(run_dir) + 1,
    )


def request_changes_round_count(rounds: list[ReviewRound]) -> int:
    return sum(1 for r in rounds if r.decision == "request_changes")


def round_cap_reached(run_dir: Path, *, max_rounds: int) -> bool:
    used = request_changes_round_count(load_review_rounds(run_dir))
    return used >= max_rounds + load_extra_rounds_granted(run_dir)


def non_blocking_followups(outstanding: list[dict]) -> list[dict]:
    return [f for f in outstanding if not f.get("blocking")]


def finding_location(finding: dict) -> str:
    file_path = str(finding.get("file", "") or "").strip()
    if not file_path:
        return "location unavailable"
    start = finding.get("start_line") or 1
    end = finding.get("end_line") or start
    return f"{file_path}:{start}" if start == end else f"{file_path}:{start}-{end}"


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def finding_summary(finding: dict) -> str:
    title = str(finding.get("title", "") or "").strip()
    body = str(finding.get("body", "") or "").strip()
    if title and body:
        return _one_line(f"{title}: {body}", _SUMMARY_MAX_CHARS)
    return _one_line(title or body or "(no summary)", _SUMMARY_MAX_CHARS)


def response_for_finding(implementer_summary: str, finding_id: str) -> str:
    """Return the sentences of the completion summary that name *finding_id*."""
    summary = " ".join(str(implementer_summary or "").split())
    finding_id = str(finding_id or "").strip()
    if not summary or not finding_id:
        return ""
    pattern = re.compile(rf"(?<![\w-]){re.escape(finding_id)}(?![\w-])")
    sentences = re.split(r"(?<=[.;!?])\s+", summary)
    matches = [s for s in sentences if pattern.search(s)]
    return _one_line(" ".join(matches), _RESPONSE_EXCERPT_MAX_CHARS)


def render_prior_findings_section(
    prior_findings: list[dict],
    *,
    round_number: int,
    implementer_summary: str,
) -> str:
    """Render the previous round's findings plus the implementer's response."""
    lines = [
        "## Prior review findings (re-review)",
        "",
        f"This is review round {round_number} of this run. The previous round left "
        "these findings open. Classify EVERY one of them in `prior_findings` "
        "(by id) as `resolved` (the code now addresses it), `unresolved` (still "
        "present) or `withdrawn` (on reflection it was not a real defect). Do not "
        "repeat a prior finding in `findings`; classify it instead. Give every new "
        "finding an id that differs from all prior finding ids listed here.",
        "",
    ]
    if not prior_findings:
        lines.append("- (no findings were left open by the previous round)")
    for finding in prior_findings:
        finding_id = str(finding.get("id", "") or "").strip() or "(no id)"
        severity = str(finding.get("severity", "") or "P2").strip() or "P2"
        lines.append(
            f"- id `{finding_id}` [{severity}] {finding_location(finding)}: {finding_summary(finding)}"
        )
        response = response_for_finding(implementer_summary, finding_id)
        if response:
            lines.append(f"  Implementer response: {response}")
        elif implementer_summary.strip():
            lines.append("  Implementer response: not addressed by id; see completion summary below.")
        else:
            lines.append("  Implementer response: none recorded.")
    lines.append("")
    summary = _one_line(implementer_summary, 2000)
    lines.append(
        "Implementer completion summary: " + (summary or "(no completion summary was recorded)")
    )
    return "\n".join(lines)


def render_rereview_rules_section(*, last_reviewed_head_sha: str, incremental_diff: str) -> str:
    """Tell a re-reviewer what changed and which new findings may block."""
    diff_body = incremental_diff if incremental_diff.strip() else "(no changes since the last reviewed head)\n"
    return (
        "## Changes since the last review\n\n"
        f"The previous round reviewed head `{last_reviewed_head_sha}`. The incremental "
        "diff below shows only what changed since then; the full PR diff is still the "
        "source of truth for the overall change.\n\n"
        "Re-review blocking rule: a NEW blocking finding in a re-review must be either "
        "P0, or a P1 introduced by the changes since the last review (its lines appear "
        "in the incremental diff). Any other new concern must be reported as P2 or P3; "
        "it is recorded as a follow-up rather than blocking the merge. The orchestrator "
        "computes the final decision from your classifications and findings.\n\n"
        "<BEGIN_INCREMENTAL_DIFF>\n"
        f"{diff_body}"
        "<END_INCREMENTAL_DIFF>"
    )


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_line_ranges(unified_zero_diff: str) -> dict[str, list[tuple[int, int]]]:
    """Map new-side file paths to changed line ranges from a ``-U0`` diff.

    Pure deletions are recorded as a one-line range at the deletion point so a
    finding about removed behavior still counts as touching the change.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in unified_zero_diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith("b/") else target
            continue
        match = _HUNK_HEADER.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            end = start + count - 1 if count > 0 else start
            ranges.setdefault(current, []).append((max(1, start), max(1, end)))
    return ranges


def finding_touches_ranges(finding: dict, ranges: dict[str, list[tuple[int, int]]]) -> bool:
    file_path = str(finding.get("file", "") or "").strip().removeprefix("./")
    spans = ranges.get(file_path)
    if not spans:
        return False
    try:
        start = int(finding.get("start_line") or 1)
        end = int(finding.get("end_line") or start)
    except (TypeError, ValueError):
        return True
    return any(start <= span_end and span_start <= end for span_start, span_end in spans)


def render_followups_markdown(findings: list[dict], *, spec_id: str, accepted_at_cap: bool = False) -> str:
    lines = [f"# Review follow-ups for {spec_id}", ""]
    if accepted_at_cap:
        lines.append(
            "The operator accepted this change at the review round cap. Findings below "
            "were still open, including any blocking ones."
        )
    else:
        lines.append("Non-blocking findings that remained open when review approved the change.")
    lines.append("")
    lines.extend(_followup_bullets(findings))
    return "\n".join(lines).rstrip() + "\n"


def _followup_bullets(findings: list[dict]) -> list[str]:
    bullets = []
    for finding in findings:
        severity = str(finding.get("severity", "") or "P2").strip() or "P2"
        finding_id = str(finding.get("id", "") or "").strip()
        id_part = f" `{finding_id}`" if finding_id else ""
        bullets.append(
            f"- [{severity}]{id_part} {finding_location(finding)}: {finding_summary(finding)}"
        )
    return bullets


def render_pr_followups_section(findings: list[dict]) -> str:
    if not findings:
        return ""
    return "\n".join([PR_FOLLOWUPS_HEADING, *_followup_bullets(findings)]) + "\n"


def pr_body_with_followups(body: str, followups_section: str) -> str:
    """Replace (or append) the PR body's Follow-ups section."""
    text = str(body or "")
    pattern = re.compile(
        rf"^{re.escape(PR_FOLLOWUPS_HEADING)}[ \t]*\n.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    text = pattern.sub("", text).rstrip()
    if not followups_section.strip():
        return text + "\n" if text else ""
    if not text:
        return followups_section
    return f"{text}\n\n{followups_section}"


def render_round_cap_summary(outstanding: list[dict], *, rounds_used: int, max_rounds: int) -> str:
    blocking = [f for f in outstanding if f.get("blocking")]
    lines = [
        f"Local review requested changes in {rounds_used} rounds (review max_rounds={max_rounds}).",
        "Outstanding findings:",
    ]
    for finding in blocking or outstanding:
        severity = str(finding.get("severity", "") or "P2").strip() or "P2"
        finding_id = str(finding.get("id", "") or "").strip() or "(no id)"
        lines.append(
            f"- {finding_id} [{severity}] {finding_location(finding)}: {finding_summary(finding)}"
        )
    if not (blocking or outstanding):
        lines.append("- (none recorded)")
    lines.append(
        "Resolve with spec input: accept the change as-is (remaining findings become "
        "follow-ups), or send one more implement round with guidance."
    )
    return "\n".join(lines)


def parse_round_cap_response(response: str) -> str:
    """Classify an operator response to a review round-cap request."""
    normalized = " ".join(str(response or "").split()).lower()
    if re.match(r"^(accept|approve)(ed)?\b", normalized):
        return REVIEW_ROUND_CAP_ACCEPT
    return REVIEW_ROUND_CAP_ANOTHER_ROUND
