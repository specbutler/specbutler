---
id: merge-state-on-fresh-clones
area: backend
priority: 30
depends_on: []
description: Detect merged dependencies on a fresh clone without manually fetching merge tags
---

# Merge State On Fresh Clones

## Goal

Spec Butler decides whether a dependency is merged from `spec/merged/<id>` tags. A
plain `git clone` doesn't fetch them reliably, so on a fresh second machine every
dependency showed as "in-progress" and both runs blocked at bootstrap until the
operator ran `git fetch --tags`. Merge detection should work on a fresh clone.

## Acceptance Criteria

- [ ] **Merge tags are fetched when needed.** Before evaluating dependencies,
  bootstrap fetches `refs/tags/spec/merged/*` from the base remote if any dependency
  appears unmerged, using the existing fetch timeout.
- [ ] **The forge is the fallback.** If a dependency still has no merge tag, the
  forge is asked whether a PR for it merged into the base. If so, the dependency
  counts as merged and the missing tag is reported (and created by `spec
  backfill-merge-tags`, if that command exists).
- [ ] **The block message says how to fix it.** When a dependency really is unmerged,
  the message names it and its status; when tags couldn't be fetched, it says so.
- [ ] **`spec doctor` warns about missing merge tags.** When the local repo has fewer
  `spec/merged/*` tags than the remote, doctor reports it.
- [ ] **Hermetic tests cover:** a clone without tags whose dependency is merged
  upstream (passes after fetch); the forge fallback; a really unmerged dependency;
  and the doctor check.

## Out of Scope

- Changing the tag format or where tags are created.

## Design Notes

- `is_spec_merged`, `merge_tag_name` and `spec_merge_tags.py`; the dependency gate in
  `phase_bootstrap` ("Unmet dependencies").
- Phase 2 evidence: `model-and-cost` and `harness-integration` on thiago-linux, local
  tag count 9 before `git fetch --tags`.
