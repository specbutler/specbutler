---
id: initial-attempt-framing
area: backend
priority: 30
depends_on: []
description: Frame an implement attempt as initial work when no earlier attempt produced a commit
---

# Initial Attempt Framing

## Goal

An implement attempt can carry `implement_reason="retry"` even though no earlier
attempt produced any code, for example after a bootstrap or dependency block, a
coordinator restart, or a killed launch. Agents then treat the attempt as "make
verification pass": they run the suite, see only sandbox noise, and stop without
writing the feature. In Phase 2 both `model-and-cost` and `harness-integration`
lost their first attempt this way. Frame such attempts as the initial
implementation, whatever the stored reason says.

## Acceptance Criteria

- [ ] **Initial work is detected from evidence, not the reason field.** An attempt
  counts as initial when the run has no commit beyond the base from any earlier
  implement attempt, and there is no prior review result for the run's branch.
- [ ] **Initial attempts get the initial prompt.** The implement context for such an
  attempt uses the initial-implementation framing and omits retry-only sections
  (failure package, "fix what failed", previous attempt result) except an
  explicit note of any non-code blocker that was cleared.
- [ ] **Retries with real prior work are unchanged.** When an earlier attempt
  committed code or a review result exists, the existing retry framing and failure
  package are used.
- [ ] **The decision is visible.** The implement context records why the attempt was
  framed as initial or retry (for example `framing_reason`), and the run's progress
  line says "initial implementation" or "retry".
- [ ] **Hermetic tests cover:** a run blocked at bootstrap and then resumed (initial);
  a run whose first launch was killed before committing (initial); a run with a
  committed earlier attempt (retry); and a review-triggered retry (retry).

## Out of Scope

- Changing what counts as a retry for the retry budget. This spec is about the
  prompt and context the agent sees.
- Clearing stale failure text from resumed runs. `resume-drops-stale-failure-context`
  covers that.

## Design Notes

- Implement context assembly: `_build_implement_attempt_context`,
  `_build_retry_failure_package` and `ImplementContext.implement_reason` in
  `src/spec_runtime/orchestrator.py`.
- Phase 2 evidence: both Linux runs' implement contexts had `implement_reason=retry`
  with an empty `failure_summary` on attempt 1 (see `docs/plans/dogfood-log.md`,
  2026-11-03 entries).

## Agent Notes

Find every place that branches on `implement_reason == "retry"` before changing
the framing; some feed the retry budget and should stay as they are.
