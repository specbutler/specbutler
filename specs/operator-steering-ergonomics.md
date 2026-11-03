---
id: operator-steering-ergonomics
area: backend
priority: 30
depends_on: []
description: Make steering additive, cheap mid-attempt, and usable without a TTY or while a question is pending
---

# Operator Steering Ergonomics

## Goal

Steering a run is the operator's main tool, and in Phase 2 it lost guidance and
attempts four ways:

- a second `spec steer` silently replaced the first;
- steering that arrived during an attempt forced a whole extra attempt, even when
  that attempt had already done the work;
- steering was refused while an operator request was pending;
- the only way to answer a request, `spec input`, needs a TTY and opens a full agent
  session.

Fix all four so a coordinator can steer unattended runs from scripts.

## Acceptance Criteria

- [ ] **Steering accumulates.** `spec steer` appends to the run's active steering
  instead of replacing it. The next attempt sees every active note in order, and
  `spec steer --replace` keeps the old behaviour. The command prints how many notes
  are active.
- [ ] **Mid-attempt steering doesn't always cost an attempt.** When steering arrives
  while an implement attempt is running, the next launch shows the agent the
  steering together with that attempt's result. If the agent reports the steering is
  already satisfied and makes no new change, the run continues to verify without
  consuming another attempt.
- [ ] **Steering works while a request is pending.** `spec steer` on a run with a
  pending operator request either attaches the note to the request's eventual
  answer or resolves the request with it (`spec steer --answer`). It no longer
  refuses.
- [ ] **Requests can be answered non-interactively.** `spec input --spec <id>
  --message <text>` resolves a pending operator request without a TTY or an agent
  session, using the same continuation rules as the interactive path.
- [ ] **Hermetic tests cover:** two steers then a launch (both notes present);
  `--replace`; mid-attempt steering with a satisfied no-op; steer during a pending
  request; and non-interactive `spec input`.

## Out of Scope

- A web UI for steering.

## Design Notes

- `OperatorSteering` and `cmd_steer` (the "Replaced proactive steering" path).
- Mid-flight rerun: `test_midflight_operator_steering_reruns_implement_before_verify`
  shows the current behaviour.
- The pending-request refusal message: "This run is already waiting on a reactive
  operator request".
- `resolve_operator_request` is the API the coordinator called directly in Phase 2.
  `spec input --message` should use it.
