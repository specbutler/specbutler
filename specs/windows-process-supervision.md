---
id: windows-process-supervision
area: backend
priority: 10
depends_on:
  - windows-platform-foundations
description: Supervise and terminate complete owned process trees on Windows
---

# Windows Process Supervision

## Goal

Replace duplicated POSIX process handling with one ownership boundary that offers
the same safe timeout, stop, and stale-PID guarantees on Windows.

## Acceptance Criteria

1. A `ProcessSupervisor` boundary manages every long-running or timeout-bounded
   child started by the orchestrator, autopilot, updater, Git timeout wrapper, web
   API/server, Codex chat bridge, and TUI chat. Direct `ps`, `/proc`, `lsof`,
   `killpg`, `getpgid`, `setsid`, and `start_new_session` policy no longer leaks
   outside the platform implementation.
2. Persisted identity includes PID plus process creation time (and executable when
   available). Before signaling or reporting a process as live, identity is
   revalidated so PID reuse cannot target an unrelated process.
3. The API defines three explicit lifetime modes: run-owned trees die when their
   owning run supervisor dies; adoptable children survive a dispatcher launcher
   restart and transfer ownership exactly once; detached services/refreshes survive
   the short-lived CLI that launches them and are stopped by their durable recorded
   identity. Call sites select a mode deliberately, and persisted state records the
   owning supervisor/helper and reopenable supervision token.
4. On POSIX, the implementation preserves current session/process-group behavior.
   On Windows, run-owned payloads are assigned before they can spawn descendants to
   a Job Object with kill-on-close semantics. Adoptable and detached modes use a
   durable supervisor/helper or equivalent handle owner whose lifetime matches the
   contract; launcher exit is not confused with owner failure. PID-only kill and
   `taskkill /T` are not the ownership primitive.
5. Graceful cancellation is attempted when supported, followed by a bounded hard
   termination of the owned tree. Cancellation and timeout results retain the
   existing state-machine meaning and useful diagnostics.
6. A native Windows integration test launches a parent, child, and grandchild,
   then proves normal completion, timeout, explicit stop, parent crash/handle
   close, and stale-identity protection leave no run-owned descendants alive.
7. Windows integration tests prove an update refresh and background web service
   survive their short-lived launcher, remain controllable through durable identity,
   and stop cleanly; another test kills/restarts autopilot and proves each live
   implementation child is adopted exactly once without losing or duplicating it.
8. Process working-directory, command, memory, and liveness inspection used by
   autopilot/watch is implemented through portable APIs and degrades explicitly
   when a field is unavailable.
9. The web server's background mode uses a portable detached child handshake;
   startup reports success only after the child is listening, and stop targets the
   recorded identity safely. No `fork` or `waitpid` path is required on Windows.
10. Existing POSIX stop, timeout, update, web, autopilot, and recovery tests remain green,
   and new tests exercise real subprocess trees rather than mocks alone.

## Out of Scope

- Assuming ownership of arbitrary externally-created process trees
- Sending Unix signals with identical numbers on Windows
- Docker Desktop container ownership

## Design Notes

Windows Job Objects are the semantic equivalent of the current process-group
contract for run-owned trees, but Job-handle ownership must match the selected
lifetime. A small durable supervisor helper is acceptable if needed to create the
payload suspended, assign it to the job, hold/reopen the handle, and only then
resume it.
