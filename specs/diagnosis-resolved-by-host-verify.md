---
id: diagnosis-resolved-by-host-verify
area: backend
priority: 30
depends_on: []
description: Clear a pending block-debugger diagnosis when its recommended action has succeeded
---

# Diagnosis Resolved By Host Verify

## Goal

When an implement attempt blocks, the block debugger writes a diagnosis and the run
keeps a `pending_block_debugger_signature`. If the diagnosis only asked for host
verification (category `environment_verification_handoff`), a passing host verify
on the diagnosed head should resolve it. Today it stays pending, and readiness later
refuses the merge ("blocked-run diagnosis remains unresolved"). In Phase 2 that sent
`harness-integration` back through a guided implement attempt after review had
already approved it, about 40 minutes of rework.

## Acceptance Criteria

- [ ] **Host verify resolves handoff diagnoses.** When verify passes on the exact
  head named by a pending diagnosis whose category is a verification handoff and
  `requires_human_attention` is false, the pending signature is cleared and the
  diagnosis is marked resolved with the verify evidence.
- [ ] **Other diagnoses are untouched.** Diagnoses that need a new commit, human
  attention, or a different head stay pending until they're handled as today.
- [ ] **Readiness explains what's pending.** If readiness still refuses for a
  pending diagnosis, its message names the diagnosis category and next action, not
  just the signature hash.
- [ ] **Hermetic tests cover:** handoff diagnosis plus host verify on the same head
  (cleared); host verify on a different head (pending); a needs-new-commit
  diagnosis (pending); and the readiness message.

## Out of Scope

- Detecting sandbox-only blocks. #70 routes those to host verify without a
  diagnosis.

## Design Notes

- `pending_block_debugger_signature`, `BlockDiagnosis` and the readiness check
  "blocked-run diagnosis remains unresolved" in `src/spec_runtime/orchestrator.py`.
- Phase 2 evidence: `harness-integration` diagnosis `c302e639…`, category
  `environment_verification_handoff`, next action "run host verify on 7c637bf",
  host verify passed, readiness refused anyway.
