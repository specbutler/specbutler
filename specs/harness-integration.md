---
id: harness-integration
area: backend
priority: 60
depends_on: [agent-sessions, review-convergence]
description: Drive Spec Butler from inside Claude Code and Codex sessions, and optionally use CI review gates
---

# Harness Integration

## Goal

The owner now works mostly in long-running Claude Code and Codex sessions and
already writes specs and hands them to subagents by hand. Spec Butler competes with
that as a separate CLI and dashboards. Make it a capability inside those sessions:
an MCP server and a Claude Code plugin to lint, dispatch, watch, steer and take over
spec runs. Also allow the repo's CI review gates to serve as the review phase.

## Acceptance Criteria

- [ ] **`spec mcp serve`** runs a stdio MCP server with these tools:
  - **Read-only:** `list_specs` (with a ready-only filter from the DAG),
    `spec_status`, `lint_spec`.
  - **Mutating:** `implement_spec` (starts a detached run and returns its run id),
    `steer_run`, `stop_run`.
  - **Takeover:** `attach_command`, which returns the command from `spec attach`.

  Tool descriptions state which tools change state.
- [ ] **Runs started by `implement_spec` are fully detached.** They don't depend on
  the MCP server process or the calling session, and they keep running after either
  exits. Starting a second run for a spec that's already running returns the
  existing run id.
- [ ] **A Claude Code plugin** under `integrations/claude-code/` provides:
  - a skill describing the workflow: author, lint, open the spec PR, dispatch, watch,
    and take over when stuck;
  - MCP wiring for `spec mcp serve`;
  - installation instructions.

  The docs also include a Codex `config.toml` snippet for the same server.
- [ ] **`[review] source = "ci"`** makes the review phase wait for the configured CI
  review check or checks on the PR head. The check's structured findings (the sticky
  review comment or the check output) are then handled exactly like local review
  results, including `review-convergence` rounds. The default stays `local`.
- [ ] **Hermetic tests cover:** MCP tools through an in-process client, the
  detached-run idempotency rule, plugin manifest validity, and CI-review result
  parsing from fixture check output.

## Out of Scope

- Remote or cloud executors.
- Push notifications to the calling session when a run finishes; callers poll
  `spec_status`.

## Design Notes

- In Claude Code, MCP servers run outside the Bash sandbox. A session can therefore
  start runs through `spec mcp serve` even when its own shell is sandboxed, which a
  sandboxed shell can't do today (nested sandboxes fail).
- The review gate and sticky comment code is in `src/spec_runtime/review_gate.py` and
  `review_gate_sticky_comment.py`. Forge check reads are in
  `src/spec_runtime/forge.py`.
- Takeover depends on `agent-sessions` (`spec attach`). CI review results must go
  through the convergence rules from `review-convergence`.
