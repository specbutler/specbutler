---
id: hermetic-suite-under-agent-sandbox
area: backend
priority: 30
depends_on: []
description: Make the test suite meaningful inside the agents' provider sandbox
---

# Hermetic Suite Under Agent Sandbox

## Goal

Implement agents run the full suite inside the provider sandbox and see 450 to 460
failures that are all sandbox denials: process inspection with `ps`, writes to the
user's Spec Butler state directory, provider-home creation, and network. Real
regressions hide in that noise. In Phase 2, agents reported "blocked" instead of
fixing real failures that host verify then found. Make a sandboxed full-suite run
report only real failures, and say which tests it skipped and why.

## Acceptance Criteria

- [ ] **User state is isolated.** An autouse fixture points `XDG_STATE_HOME`, and
  anything else `specbutler_user_state_root` or provider-home resolution reads, at a
  per-test temporary directory. No test writes under the real user state or
  Application Support directories.
- [ ] **Capability-dependent tests skip with a reason.** Tests that need process
  inspection, real provider homes, network, or nested sandboxes are marked (the
  existing `requires_ps` marker, plus markers for the other capabilities). They skip
  with a clear reason when the capability is missing, and run normally when it's
  present.
- [ ] **A sandboxed run is near-green.** Running `.venv/bin/python -m pytest` inside
  the Codex and Claude implement sandboxes on macOS and Linux yields no failures on
  a clean `main`, only capability skips. Record the counts in the PR.
- [ ] **Host and CI coverage don't shrink.** Outside the sandbox (host verify, CI)
  the same tests run as before: the passed and skipped counts on CI match `main`
  within the tests added here.
- [ ] **The skip summary is visible.** The pytest summary shows a one-line count per
  skipped capability, so an agent can tell sandbox skips from real results.

## Out of Scope

- Changing the provider sandboxes themselves.
- Windows-only behaviour.

## Design Notes

- Existing pieces: the `requires_ps` marker and `ps`-availability detection added by
  `canary-ps-test-skip`; `specbutler_user_state_root` honours `XDG_STATE_HOME`.
- Typical sandbox failures: "Could not inspect launched process", `PermissionError`
  under `~/Library/Application Support/SpecButler/provider-homes`, and "Operation not
  permitted" on provider homes.

## Agent Notes

You run inside the provider sandbox yourself, so your own local run is the
measurement. Compare it with the host-verify run in the gate output.
