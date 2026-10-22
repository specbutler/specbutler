---
id: review-convergence
area: backend
priority: 10
depends_on: []
description: Make local review loops converge within a few rounds without losing real findings
---

# Review Convergence

## Goal

Review is the biggest source of retries. Across 958 historical runs, 534 of 791
merged runs failed review at least once and 120 failed five or more times. In rounds
4 and later, 367 of 374 rounds still raised a P1, and one run took 41 rounds. The
reviewer sees no record of its own previous findings, so it moves the goalposts every
round. Make review converge within a bounded number of rounds while still blocking
real P0/P1 defects.

## Acceptance Criteria

- [ ] **Re-reviews see prior findings.** On every review after the first in a run,
  the prompt includes the previous round's findings (id, severity, file/line,
  summary) and the implementer's response to each, taken from its completion summary.
  The structured output classifies each prior finding as `resolved`, `unresolved` or
  `withdrawn`. Extend `templates/review-schema.json` backward-compatibly: older
  outputs without the field still parse.
- [ ] **Re-reviews see what changed.** They receive the incremental diff since the
  last reviewed head, as well as the full diff. The prompt says a new *blocking*
  finding in a re-review must be P0, or a P1 introduced by changes since the last
  review.
- [ ] **The decision is computed, not trusted.** The orchestrator decides
  `request_changes` if and only if there's an unresolved P0/P1 prior finding or an
  eligible new P0/P1 finding. If the model's own `decision` field disagrees, it's
  logged and ignored.
- [ ] **Review rounds are capped.** A new `[review] max_rounds` setting (default 3)
  limits how many times one run can request changes. Hitting the cap moves the run to
  `waiting-for-input` with a summary of the outstanding findings, instead of starting
  another implement retry. The operator can resolve it with `spec input`, either
  accepting as-is or sending one more round with guidance.
- [ ] **Non-blocking findings aren't lost.** P2/P3 findings that remain at approval are
  written to `review-followups.md` in the run artifacts and listed in a "Follow-ups"
  section of the PR body.
- [ ] **The Codex reviewer can inspect the repo again.** It can run read-only commands
  against the pinned review checkout. The provider home (`CODEX_HOME`) and the
  operator credential paths in `provider_env.protected_operator_paths` are denied for
  reading in its permission profile. Tests show those paths are denied and the
  checkout is readable.
- [ ] **Re-reviews keep the first pass's reasoning effort** by default. A
  `[review] rereview_reasoning_effort` setting is available.
- [ ] **The bundled `templates/review.md` describes the reviewer's real
  capabilities.** `spec doctor` reports which review prompt file is in effect: a
  repo-level `.github/prompts/review.md` or `codex-review.md`, or the bundled one.
- [ ] **Hermetic tests cover:** prior-findings rendering, incremental diff
  materialisation, the decision rule, the round cap leading to waiting-for-input, the
  PR body follow-ups section, and the Codex review profile's read and deny rules.

## Out of Scope

- Replacing local review with CI review gates (see `harness-integration`).
- Changing the P0–P3 rubric itself.
- Automatically creating follow-up spec files from P2/P3 findings.

## Design Notes

- Review prompt rendering is `_render_local_review_prompt` in
  `src/spec_runtime/orchestrator.py` (~line 12873). It currently takes no prior
  findings. Prior findings already reach the *implementer* through
  `_format_prior_attempt_findings_for_prompt` (~line 19664); reuse that data.
- The diff is materialised on the host with `git diff --unified=80` (~22519–22590).
  The reasoning-effort constants are near line 304, and the effort is selected near
  22645.
- The Codex review command builder is in `src/spec_runtime/agent_adapter.py`
  (~1088–1125). Today it disables `features.shell_tool` and `unified_exec` (this
  started in #10 on 2026-09-08). Use a permission profile with a read-only filesystem
  plus explicit deny entries rather than removing the shell entirely.
- Decision validation lives in `src/spec_runtime/review_gate.py`.

## Agent Notes

- Keep the review prompt within the existing stdin path. Prompts can exceed argv
  limits.
- Don't weaken the "built-in reviewers do not execute pull-request code" boundary:
  read-only inspection (cat, grep, git log/show) is fine, while running tests or
  installs is not.
