---
id: required-check-retry
area: orchestrator
priority: 30
depends_on: [merge-queue-support]
description: Rerun a failing required check once before treating it as the PR's CI failure
---

# Required Check Retry

## Goal

During Phase 3, three PRs failed their merge phase on required checks that had
nothing to do with the change:
- a cold-start Chrome timeout;
- a broken pip download (`IncompleteRead`);
- a macOS job GitHub cancelled because no runner was available ("job was not
  acquired by Runner").

Each one was treated as the PR's CI failure. That sent the run through a full
implement → verify → review cycle, and in every case a plain rerun of the same
commit would have passed.

When a required check fails on a head, Spec Butler should rerun it once before
deciding the PR broke it.

## Acceptance Criteria

- [ ] **One rerun per head.** When the merge phase finds a required check failing
  on the PR head, the host reruns that check's GitHub Actions workflow run once
  and waits for the result, instead of returning a CI failure right away. A
  failure on the same head after that rerun takes the existing CI-failure path,
  with log excerpts. A new head SHA gets its own single rerun.
- [ ] **Whole-run reruns.** The rerun covers the whole workflow run
  (`gh run rerun <id>`), not only the failed jobs. CI's evidence job requires all
  job fragments to come from one run attempt.
- [ ] **Cancellations read correctly.** A job cancelled because no runner was
  acquired counts as a failure that gets the rerun. That's GitHub's "not acquired
  by Runner" annotation, even though GitHub marks the job cancelled. A run
  cancelled because a newer run superseded it ("higher priority waiting
  request", or concurrency cancellation) is not a failure: the phase waits for
  the newer run and spends no rerun.
- [ ] **Queue failures too.** On a base branch with a merge queue, a dequeue
  caused by a failing merge-group check gets one re-enqueue of the same head,
  following the same rule, before the existing merge-group CI-failure retry.
- [ ] **No attempt spent.** The rerun and its wait don't use up an implement
  attempt, a review round or the retry cap. A rerun that passes leads straight to
  the merge.
- [ ] **Visible.** Each rerun is recorded as a run event with the head SHA, the
  workflow run ID and the failing job names. `spec status` shows when a rerun is
  in progress. A run that passed only after a rerun is recorded as flaky with
  those job names, so recurring flakes can be counted.
- [ ] **Hermetic tests cover:** a failure that passes on rerun (merges with no
  attempt spent); a failure twice on the same head (existing CI-failure path
  with excerpts); a new head after a rerun (gets its own rerun); a runner-not-
  acquired cancellation (rerun); a superseded cancellation (wait, no rerun); and
  a merge-group dequeue (one re-enqueue).

## Out of Scope

- Retrying failing tests inside host verify. That's a separate gate with its own
  evidence.
- Fixing individual flaky tests. Those get their own PRs.
- Non-GitHub-Actions check providers. Report them as today.

## Design Notes

- Merge readiness: `_required_checks_green` in `src/spec_runtime/orchestrator.py`
  (the "Required checks failing" message). Its "cancel" bucket is currently
  treated as pending. CI excerpts: `_required_check_failure_excerpts`. Queue
  dequeue handling came with `merge-queue-support` (#88), in `forge.py` and the
  merge phase.
- Phase 3 evidence:
  - run 41730070119 (Chrome, PR 80);
  - run 41746283481 (pip, PR 81);
  - run 41892547304 (macOS runner not acquired, PR 88).
  All three passed on rerun, and all three had already sent their PR back to
  implement.
