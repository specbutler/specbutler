---
id: resume-drops-stale-failure-context
area: backend
priority: 30
depends_on: []
description: Stop resumed runs from feeding an already-resolved failure to the next attempt
---

# Resume Drops Stale Failure Context

## Goal

When a blocked or failed run is resumed after its cause was fixed outside the run,
the next implement attempt still receives the old failure as retry evidence. In
Phase 2 a bootstrap "Unmet dependencies" block, which was resolved by fetching merge
tags, was handed to implement attempt 1 of two runs. Both agents read it, concluded
the dependencies were still unmet, and stopped with no changes. A resumed run should
re-check a failure's cause and drop the failure when the check now passes.

## Acceptance Criteria

- [ ] **Bootstrap and dependency blocks are re-checked on resume.** Resuming a run
  whose last failure was a bootstrap-phase block re-runs bootstrap. If bootstrap now
  passes, its earlier failure is not included in any implement context.
- [ ] **Resolved infrastructure failures are not retry evidence.** A failure
  classified as infrastructure (`forge_network`, `provider_network`,
  `provider_capacity`, `sandbox_denied` and similar) is never presented to the agent
  as something to fix in code once the phase that hit it has since passed.
- [ ] **Superseded failures are archived, not lost.** The dropped failure stays in
  the run's history (an attempt artifact or a `superseded_failures` list), so
  `spec show` can still explain what happened.
- [ ] **Hermetic tests cover:** a dependency block resolved and then resumed (no
  dependency text in the next context); a code verify failure followed by a resume
  (still included); and an infrastructure failure whose phase later passes
  (excluded).

## Out of Scope

- The initial-versus-retry framing. `initial-attempt-framing` covers that.
- Pending block-debugger diagnoses. `diagnosis-resolved-by-host-verify` covers those.

## Design Notes

- Bootstrap dependency gate: the "Unmet dependencies" path in `phase_bootstrap`.
- Retry evidence comes from `run.last_error` and the gate status read by
  `_build_retry_failure_package`.
- Failure classification: `classify_failure` and `_infrastructure_failure_kind`.
