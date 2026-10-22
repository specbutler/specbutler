---
id: generality-cleanup
area: backend
priority: 50
depends_on: []
description: Remove hardcoded leftovers from the project Spec Butler was first built for
---

# Generality Cleanup

## Goal

Spec Butler grew inside one production app, and some of that app's assumptions are
still hardcoded in the core: Postgres shutdown during cleanup, a base branch named
`master` in prompts and errors, and a pytest-only diagnostic rerun. Move these to
configuration so the tool behaves correctly in any repo.

## Acceptance Criteria

- [ ] **Postgres shutdown moves to repos.** The Postgres-specific cleanup
  (`SIM_LOCAL_PG_BIN`, stopping Homebrew `postgresql@16`) is removed from the core.
  Repos that need it use the existing `[implement] teardown_command` hook, and the
  docs show an example.
- [ ] **Prompts and errors name the configured base branch.** Agent prompts and
  operator-facing errors use the branch derived from `base_ref` instead of a literal
  `master`, including the merge-conflict prompt.
- [ ] **The diagnostic rerun works for any gate.** It's no longer hardcoded to pytest.
  A gate can set `diagnostic_command`; gates that don't set one and aren't
  pytest-like skip the rerun rather than running pytest.
- [ ] **A test fails if** a literal `master` reappears in prompt constants, or a
  Postgres-specific identifier reappears in the core package.
- [ ] **Hermetic tests cover:** base-branch substitution in prompts, gate diagnostic
  selection, and cleanup without the Postgres branch.

## Out of Scope

- Changing any downstream repo's configuration. Repos that relied on the built-in
  Postgres stop must add a teardown hook; call this out in the release notes.

## Design Notes

- Postgres cleanup: `src/spec_runtime/orchestrator.py` (~1080–1120).
- `master` literals: around ~482 (`MERGE_CONFLICT_PROMPT`), ~14437, ~19611 and
  ~23598. Search for others.
- Diagnostic rerun: ~398–409.
