---
id: spec-not-found-checkout-behind
area: orchestrator
priority: 40
depends_on: []
description: Tell the operator their checkout is behind when a requested spec exists only on the remote base branch
---

# Spec Not Found: Checkout Behind

## Goal

`spec implement --spec <id>` reports "Spec lint failed … spec not found" when the spec
file isn't in the local checkout. Twice during Phase 3 dogfooding, the spec had
already merged to `origin/main` and the checkout simply hadn't been pulled. The error
pointed at the spec when the actual fix was `git pull`. Both runs ended in under a
second, and the second one exited 0 with an empty outcome line, so an unattended lane
runner looked as if it had completed.

When a requested spec is missing locally but present on the remote base branch, say
so plainly and say how to fix it.

## Acceptance Criteria

- [ ] **Behind is named.** If `--spec <id>` isn't found locally but
  `specs/<id>.md` exists on the base branch's remote-tracking ref (for example
  `origin/main`), the error says the spec exists on `origin/main` but not in this
  checkout. It says how many commits the checkout is behind, and it suggests
  `git pull --ff-only` (or switching branch, when the checkout isn't on the base
  branch).
- [ ] **Fresh remote state.** Before concluding, the check refreshes the
  remote-tracking ref with a bounded fetch: a short timeout, and no credential
  prompt. If the fetch fails or times out, it falls back to the cached ref and says
  that the remote state may be stale.
- [ ] **Truly missing stays missing.** When the spec is on neither the checkout nor
  the remote base, the existing "spec not found" error is unchanged. It may add the
  closest existing spec IDs as suggestions.
- [ ] **Nothing is pulled for you.** Spec Butler only reports. It never pulls,
  resets or switches the operator's checkout.
- [ ] **Failure is a failure.** In every case above, `spec implement` exits non-zero
  and doesn't start a run, so wrapper scripts can't mistake it for a successful
  workflow.
- [ ] **Same guidance everywhere.** `spec lint --spec <id>` and `spec status --spec <id>`
  give the same "checkout is behind" guidance as `spec implement`.
- [ ] **Hermetic tests cover:** a spec only on the remote base (behind message with
  commit count); a checkout on a feature branch (switch-branch hint); a failed fetch
  (stale-cache note); a spec that's truly missing (unchanged error); and a non-zero
  exit with no run started.

## Out of Scope

- Pulling or updating the checkout automatically.
- Finding specs on arbitrary remote branches other than the base branch.

## Design Notes

- The error starts in `lint_specs` (`src/spec_runtime/spec_lint.py`, the
  `["spec not found"]` result), and `_block_reason_category` in `orchestrator.py`
  categorizes it as `spec-not-found`. The behind check belongs at the CLI / lookup
  layer, not inside the pure lint function.
- Phase 3 evidence: the lane runner's first restart (checkout 3 commits behind), and
  lane D for `codex-review-inspection-tools` on 2026-11-04 (spec merged in #73; the
  run ended instantly).
