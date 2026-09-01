---
id: windows-ci-probe
area: backend
priority: 1
depends_on: []
description: Expose native Windows failures continuously before claiming support
---

# Windows CI Probe

## Goal

Create an immediate, diagnostic Windows feedback loop while the port is in
progress. The probe must expose all current failures without weakening the existing
required Linux gate or prematurely claiming support.

## Acceptance Criteria

1. A `windows-latest` GitHub Actions job on Python 3.12 builds the wheel, installs
   the wheel with development, web, and TUI extras into a clean environment, runs
   `pip check`, and retains build/test logs as artifacts.
2. Independent probe steps attempt `spec --version`, `spec --help`, imports of all
   shipped `spec_runtime` modules, `pytest --collect-only`, a focused Windows probe
   test file, and the full test suite. An early import failure does not hide later
   diagnostic results.
3. Expected failures are visible through step summaries or uploaded logs. The job
   is initially non-blocking and is not included in the aggregate required CI gate.
   Individual diagnostic commands are not masked with shell constructs that erase
   their exit status.
4. A focused Windows probe test covers five product invariants: lifecycle-module
   import, cross-process lock contention, parent/child/grandchild termination,
   runnable `spec init` output accepted by `spec doctor`, and a real foreground web
   bind/auth request. Tests may fail until their owning specs land, but may not be
   replaced with unconditional Windows skips.
5. Checkout and test behavior is deterministic with documented line-ending policy,
   and the existing required Linux test/lint/package/security workflows remain
   unchanged.
6. The job contains a documented promotion condition: it becomes blocking and
   joins the aggregate CI result only after every probe step is green.

## Out of Scope

- Fixing the failures exposed by the probe
- Advertising Windows as supported
- Provider credentials or real GitHub mutations in pull-request CI
