---
id: agent-sessions
area: backend
priority: 20
depends_on: []
description: Persist and resume provider sessions across retries, and take completion from structured output
---

# Agent Sessions

## Goal

Every implement attempt today starts a fresh, unpersisted agent session. That loses
context between retries and makes taking over interactively impossible. Completion
also depends on the agent running `spec report` through the shell under fragile
quoting rules, and 566 historical failures were missing completion handshakes. Keep
one provider session per run, resume it for retries, take the final result from the
provider's structured output, and let the operator take over.

## Acceptance Criteria

- [ ] **Claude runs keep one session.** Implement launches pass `--session-id <uuid>`
  instead of `--no-session-persistence`, and the id is stored on the run state.
  Verify, review and merge-conflict retries in the same run use `--resume <id>` with
  a delta prompt (the failure package) instead of the full initial prompt. If the
  resume fails (session missing, CLI error), the run falls back to a fresh session
  and logs why.
- [ ] **Codex runs keep one thread.** Implement records the thread id from
  `codex exec --json` events and resumes it for retries (`codex exec resume <id>` or
  whatever the installed CLI supports). The same fresh-session fallback applies.
- [ ] **Completion comes from structured output.** Claude implement passes
  `--json-schema`, reading `structured_output` from the result event; Codex passes
  `--output-schema`. The schema covers `status` (ok, blocked, error, needs-input),
  `summary` and an optional `question`. That structured result is the primary
  completion signal. `spec report` still works as a fallback, and the no-handshake
  recovery path runs only when neither is present.
- [ ] **`spec attach --spec ID`** prints and, with `--exec`, runs the command to
  resume the latest run's session interactively in its workspace: `claude --resume
  <id>` or `codex resume <id>`. It refuses while that run's agent process is alive
  unless `--fork` is given, which uses `--fork-session` for Claude.
- [ ] **Steering reaches the live session.** Messages from `spec steer` are delivered
  through the resumed session at the next retry. The docs say steering takes effect
  at attempt boundaries; injecting mid-turn is out of scope.
- [ ] **`spec status` and `spec watch` show the provider session id** for active and
  recent runs.
- [ ] **Hermetic tests use fake Claude and Codex binaries** to cover: id capture,
  resume on retry, the fallback to a fresh session, structured completion parsing,
  the `spec report` fallback, and `spec attach` command construction and refusal.

## Out of Scope

- Moving the orchestrator to the Claude Agent SDK or the Codex app-server. Keep
  shelling out to the CLIs, which also keeps subscription authentication working.
- Live mid-turn steering.
- Changes to the review or debugger launches, which stay stateless.

## Design Notes

- The Claude implement builder is in `src/spec_runtime/agent_adapter.py` (~685–718,
  `--no-session-persistence` at ~704). The Codex implement builder is at ~996–1040.
- The completion outbox (`SPEC_COMPLETION_OUTBOX`) is in
  `src/spec_runtime/orchestrator.py` (~27770). No-handshake recovery is at ~17653.
  Stream event parsing is at ~18730–18870.
- Claude headless docs: <https://code.claude.com/docs/en/headless>. `--session-id`,
  `--resume`, and `--json-schema` returning `structured_output` are documented there.
  Sessions started with `-p` are resumable with `claude --resume <id>`, but don't
  drive one session headless and interactive at the same time.

## Agent Notes

- Check the installed CLIs' `--help` output in `doctor.py` capability probes before
  relying on a flag. When a flag is missing, degrade to today's behaviour with a
  `spec doctor` warning rather than failing the launch.
