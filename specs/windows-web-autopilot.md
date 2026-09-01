---
id: windows-web-autopilot
area: orchestrator
priority: 30
depends_on:
  - windows-native-lifecycle
description: Port web, chat, autopilot, and watch workflows to native Windows
---

# Windows Web, Chat, Autopilot, and Watch

## Goal

Make the interactive and dispatch surfaces usable on native Windows with the same
context, cancellation, and ownership guarantees as the core lifecycle.

## Acceptance Criteria

1. `spec web` starts in foreground and background modes on Windows, serves the
   authenticated application, reaches readiness deterministically, and stops
   without leaving server or action subprocesses alive.
2. Real Codex web chat is tested through the running HTTP/WebSocket interface for
   at least three turns. The second and third responses demonstrably depend on
   facts supplied only in earlier turns, refresh/reconnect preserves the intended
   session, separate chats do not leak context, and cancellation leaves no child
   process.
3. Claude chat remains available where the existing supported isolation/runtime is
   present. On native Windows it is hidden or disabled with a precise explanation
   rather than launched without its sandbox; Linux regression tests exercise a
   real multi-turn Claude session when credentials are available.
4. Web lifecycle actions invoke the same runtime and process-supervision paths as
   the CLI and report structured failures rather than POSIX tracebacks.
5. `spec auto run` dispatches multiple dependency-ready specs on Windows up to the
   configured concurrency, never dispatches blocked dependents, survives a
   dispatcher restart, and `spec auto stop` drains/terminates according to its
   documented contract.
6. `spec watch` and its chat surface run on Windows with portable process and
   memory inspection. Terminal limitations degrade explicitly without crashing
   the dispatcher.
7. Windows integration tests use real subprocesses and a real listening server;
   browser/API tests cover authentication, reconnect, concurrent chats, context,
   cancellation, background start/stop, and stale server identity.
8. Existing Linux web, chat, autopilot, and TUI tests remain green.

## Out of Scope

- Native Claude execution until its sandbox prerequisite exists
- Docker Desktop-specific behavior
- Browser UI redesign unrelated to portability

## Design Notes

Do not validate chat persistence by inspecting state alone. The proof must interact
with the running server and require the model response to recall prior-turn data.
