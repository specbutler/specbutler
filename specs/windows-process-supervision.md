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

1. A `ProcessSupervisor` boundary owns every long-running or timeout-bounded child
   started by the orchestrator, autopilot, Git timeout wrapper, web API/server,
   Codex chat bridge, and TUI chat. Direct `ps`, `/proc`, `lsof`, `killpg`,
   `getpgid`, `setsid`, and `start_new_session` policy no longer leaks outside the
   platform implementation.
2. Persisted identity includes PID plus process creation time (and executable when
   available). Before signaling or reporting a process as live, identity is
   revalidated so PID reuse cannot target an unrelated process.
3. On POSIX, the implementation preserves current session/process-group behavior.
   On Windows, each owned payload is assigned before it can spawn descendants to a
   Job Object with kill-on-close semantics. PID-only kill and `taskkill /T` are not
   the ownership primitive.
4. Graceful cancellation is attempted when supported, followed by a bounded hard
   termination of the owned tree. Cancellation and timeout results retain the
   existing state-machine meaning and useful diagnostics.
5. A native Windows integration test launches a parent, child, and grandchild,
   then proves normal completion, timeout, explicit stop, parent crash/handle
   close, and stale-identity protection leave no owned descendants alive.
6. Process working-directory, command, memory, and liveness inspection used by
   autopilot/watch is implemented through portable APIs and degrades explicitly
   when a field is unavailable.
7. The web server's background mode uses a portable detached child handshake;
   startup reports success only after the child is listening, and stop targets the
   recorded identity safely. No `fork` or `waitpid` path is required on Windows.
8. Existing POSIX stop, timeout, web, autopilot, and recovery tests remain green,
   and new tests exercise real subprocess trees rather than mocks alone.

## Out of Scope

- Assuming ownership of arbitrary externally-created process trees
- Sending Unix signals with identical numbers on Windows
- Docker Desktop container ownership

## Design Notes

Windows Job Objects are the semantic equivalent of the current process-group
contract. A small supervisor helper is acceptable if needed to create the payload
suspended, assign it to the job, and only then resume it.
