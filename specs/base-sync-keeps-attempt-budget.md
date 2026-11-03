---
id: base-sync-keeps-attempt-budget
area: backend
priority: 30
depends_on: []
description: Re-verify and re-review after a mechanical base sync without consuming an implement attempt
---

# Base Sync Keeps Attempt Budget

## Goal

When `main` moves while a run waits to merge, the merge phase merges the base,
pushes, and sends the run back through verify and local review. Each of those round
trips starts a new attempt, although no agent wrote any code. `review-convergence`
went from attempt 21 to 24 purely from `main` moving under it, and needed its retry
cap raised twice. A mechanical base sync should re-verify and re-review the synced
head within the same attempt budget.

## Acceptance Criteria

- [ ] **A clean base sync doesn't consume an attempt.** After the merge phase merges
  the base without conflicts and pushes, the re-verify and re-review of the synced
  head run without incrementing the attempt counter or the convergence-attempt
  count.
- [ ] **Real retries still count.** A verify failure on the synced head, a review
  that requests changes, and a merge conflict that needs the agent each still
  consume an attempt as today.
- [ ] **Unbounded loops stay bounded.** Base syncs have their own cap per run (for
  example `base_sync_cap`, default 10). Exceeding it fails the run with a clear
  message instead of syncing forever.
- [ ] **Resume goes to the right phase.** Resuming a run that is waiting on a
  post-sync re-review enters verify or review directly, not merge first. The
  readiness message no longer says "covered X, but the branch now points to X" with
  the same SHA on both sides.
- [ ] **Hermetic tests cover:** one and several clean syncs (attempt unchanged); a
  sync followed by a verify failure (attempt consumed); the sync cap; and the resume
  routing.

## Out of Scope

- Serializing merges across runs (a merge queue). This spec only stops syncs from
  spending the retry budget.

## Design Notes

- Sync and rerun: `_manual_merge` (`force_base_sync`), `_mark_local_review_for_rerun_after_sync`,
  and `LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX` in `src/spec_runtime/orchestrator.py`.
- Attempt accounting: `_consume_retry_budget`, `_convergence_attempts` and the
  phase-transition code in `run_full_workflow`.
