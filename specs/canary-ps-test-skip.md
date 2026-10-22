---
id: canary-ps-test-skip
area: backend
priority: 5
depends_on: []
description: Skip process-inspection tests with a clear reason when ps cannot run
---

# Canary: Skip ps-Dependent Tests When ps Is Unavailable

## Goal

Inside a sandboxed agent session, `ps` is blocked, and about 400–480
process-inspection tests fail, even on a clean `main`. Agents then can't get a
meaningful local test result and have to treat CI as the first full run. Make those
tests skip, with an explicit reason, when `ps` genuinely can't run, so local runs
inside a sandbox are trustworthy.

This is also a Phase 1 canary: a small, real change to run through `spec implement`
end to end, implemented by Claude.

## Acceptance Criteria

- [ ] **A session-scoped probe in `conftest.py`** runs `ps -p <own pid>` once. It
  decides whether `ps` is usable: it exists, exits 0, and doesn't fail with
  "Operation not permitted".
- [ ] **Tests that need a real `ps` are marked `requires_ps`** (register the marker in
  `pyproject.toml`). When `ps` is unusable they're skipped, and the skip reason
  includes the probe's error.
- [ ] **On a machine where `ps` works, nothing is skipped.** The test count and
  results match today's, so CI is unaffected.
- [ ] **When `ps` is unusable, the run prints a single summary line**, e.g.
  `N tests skipped: ps unavailable (Operation not permitted)`, so the skips are
  visible rather than silent.
- [ ] **A test of the probe itself** uses a fake `ps` on `PATH` to cover the usable,
  missing and permission-denied cases.

## Out of Scope

- Changing product code that calls `ps` (`process_supervisor.py`, `container.py`,
  `execution_backend.py`).
- Skipping tests for any other unavailable tool.

## Agent Notes

- Find affected tests by running the suite with `ps` made unavailable (for example,
  a `PATH` entry with a `ps` stub that exits 1 with "Operation not permitted"). Mark
  only tests that really fail for that reason. Don't skip whole files blindly.
- Keep the fake-binary pattern consistent with existing test helpers.
