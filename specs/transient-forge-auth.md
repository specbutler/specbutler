---
id: transient-forge-auth
area: backend
priority: 30
depends_on: []
description: Retry forge auth failures that coincide with network outages instead of failing the workflow
---

# Transient Forge Auth

## Goal

During a GitHub outage, the merge phase correctly classified "error connecting to
api.github.com" as `forge_network` and retried. The next preflight, `gh auth
status`, then failed in the same outage and was reported as "GitHub CLI not
authenticated". That's classified `forge_auth`, which is terminal, so the workflow
failed. A minute later `gh auth status` was fine. One auth failure during or right
after a network failure should be retried as a network problem.

## Acceptance Criteria

- [ ] **Auth checks retry before failing.** A failed forge auth preflight is retried
  with backoff (for example 3 tries over about a minute) before it's reported as
  `forge_auth`.
- [ ] **Network context wins.** If the auth failure's output shows a network error,
  or the run's previous failure in the same phase was `forge_network`, the failure
  is classified `forge_network` and retried without consuming an attempt.
- [ ] **Real auth failures still stop the run.** A consistent "not logged in" result
  across the retries, with the network reachable, still fails with the existing
  `gh auth login` guidance.
- [ ] **Hermetic tests cover:** one auth failure then success (no failure recorded);
  auth failure after `forge_network` (network classification); and persistent auth
  failure (terminal).

## Out of Scope

- Locked-keychain detection on macOS. The existing keychain checks stay as they
  are.

## Design Notes

- Classification: `classify_failure`, `_infrastructure_failure_kind` and the
  `forge_auth` path in `src/spec_runtime/orchestrator.py`.
- Phase 2 evidence: the `agent-sessions` merge phase on 2026-11-02, "Could not reach
  the forge…" followed by "GitHub CLI not authenticated", then workflow failed.
