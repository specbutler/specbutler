---
id: merge-queue-support
area: backend
priority: 30
depends_on: [base-sync-keeps-attempt-budget]
description: Merge through a GitHub merge queue when the base branch uses one, and keep the direct path otherwise
---

# Merge Queue Support

## Goal

With a strict "up to date" rule and ~15-minute CI, every merge forces every other
open PR to sync, re-review and re-run CI. In Phase 2 that serialized the whole
fleet, and the coordinator had to hold PRs back by hand. A GitHub merge queue
removes that: PRs are tested against `main` plus the PRs ahead of them and merged
in order. Spec Butler should merge through the queue when the base branch has one,
and keep today's direct path (base sync, merge) for repos without one. A queue is
recommended, not required: personal-account and many private repos can't enable it.

## Acceptance Criteria

- [ ] **The queue is detected automatically.** The merge phase finds out from the
  forge (rulesets or branch protection for the base) whether the base branch requires
  a merge queue, and caches that per run. There's no new required config; an optional
  `[merge] queue = "auto" | "on" | "off"` override exists for testing.
- [ ] **Queued merges wait for the queue's result.** With a queue, the merge phase
  enqueues the PR (`gh pr merge --auto` or the equivalent forge call) and waits for
  MERGED, without running the BEHIND base sync. Without a queue, behaviour is
  unchanged.
- [ ] **Removal from the queue is handled.** When the PR leaves the queue unmerged,
  the run reads the reason:
  - a merge conflict goes to the existing conflict path (local merge, implement
    resolves, republish, re-review, re-queue);
  - failing checks go to the existing CI-failure retry, with excerpts from the
    merge-group run rather than the PR head;
  - a timeout or a removal by someone else is retried a bounded number of times, then
    fails with a clear message.
- [ ] **The review gate guards entry to the queue.** `review-decision-gate` stays a
  required check on the PR head, so a PR can join the queue only after independent
  review approved that exact head, like a required human review. It isn't a required
  check inside the queue, so nothing needs to post review status on merge-group
  commits. If the queue's base sync changes the head (a conflict resolution, or a
  push after removal from the queue), local review reruns before the PR rejoins. `spec
  doctor` warns when a queued base branch requires the review gate inside the queue,
  or doesn't require it on PRs at all.
- [ ] **`spec doctor` advises.** It reports whether the base branch has a queue. When
  the branch requires up-to-date PRs, has no queue, and the repo's autopilot
  concurrency is above 1, it recommends enabling one as a warning, not a blocker. When
  a queue is configured, it checks that the required workflows trigger on
  `merge_group`.
- [ ] **This repo's workflows are queue-ready.** The `ci` and `spec-pr-policy`
  workflows run on `merge_group` events, `ci-evidence` accepts merge-group runs, and
  the review-gate workflow skips cleanly on `merge_group`. The workflow changes work with and without a queue, so nothing
  breaks before the repo enables one.
- [ ] **Hermetic tests cover:** queue detection on and off; a queued merge that
  succeeds; removal for a conflict, for failing checks and for a timeout; the
  no-queue path unchanged; and the doctor checks.

## Out of Scope

- Turning the queue on for this repository. That's a separate operator change, made
  after this spec merges.
- GitLab merge trains and other forges, beyond keeping the forge interface general
  enough to add them.

## Design Notes

- Merge phase: `phase_merge`, `_try_auto_merge` (returns `merged`, `stale_base`,
  `unavailable_*` or `failed`), `_manual_merge` and `_wait_for_pr_merged` in
  `src/spec_runtime/orchestrator.py`; the forge wrapper is in `forge.py`.
- GitHub specifics: queue membership and removal reasons are available from the PR's
  GraphQL fields and timeline events. The merge-group commit gets
  `gh-readonly-queue/<base>/pr-<n>-<sha>` refs and `merge_group` workflow runs.
- Local-review status: `_publish_review_gate_status` posts on the PR head SHA.
- This repo's ruleset currently requires `ci`, `review-decision-gate` and
  `spec-pr-policy` with `strict_required_status_checks_policy: true`.

## Agent Notes

This depends on `base-sync-keeps-attempt-budget` because both change merge-phase
retry accounting; build on its attempt-budget rules for queue removals.
