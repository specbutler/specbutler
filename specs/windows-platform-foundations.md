---
id: windows-platform-foundations
area: backend
priority: 5
depends_on:
  - windows-ci-probe
description: Make the CLI, state, locking, paths, and packaging safe on native Windows
---

# Windows Platform Foundations

## Goal

Make Spec Butler importable and usable for read-only/local-state commands on native
Windows before porting lifecycle process execution. Establish narrow platform
boundaries that preserve current Linux and macOS behavior.

## Acceptance Criteria

1. Importing `spec_runtime.cli`, `spec_runtime.orchestrator`,
   `spec_runtime.execution_backend`, `spec_runtime.autopilot`, and
   `spec_runtime.container` succeeds on native Windows; POSIX-only modules such as
   `fcntl` and `grp` are not imported there.
2. All orchestrator and execution-backend file locking goes through one tested
   cross-platform lock abstraction. On Windows it provides exclusive blocking and
   non-blocking leases, preserves lock-owner metadata, releases locks after normal
   exit and exceptions, and prevents two processes from owning the same spec.
3. State writes use same-directory atomic replacement and bounded retry for
   transient Windows sharing violations. A concurrent multi-process test proves
   that state files remain parseable and no update is silently lost.
4. Temporary paths use the platform temporary-directory API. Filesystem cleanup
   tolerates Windows read-only files and bounded transient sharing violations but
   still reports permanent failures.
5. Spec IDs reject Windows reserved device basenames and invalid trailing dots or
   spaces. Workspace paths remain deterministic and are short enough for supported
   Windows tooling; tests cover repository roots containing spaces and non-ASCII
   characters.
6. Local native Windows support is explicitly limited to fixed local filesystems
   for this phase. `spec doctor` diagnoses UNC/network roots as unsupported instead
   of making unsafe locking or atomicity claims.
7. From an installed wheel on Windows, `spec --version`, `spec --help`, `spec init`,
   `spec list`, `spec show`, and read-only `spec status` complete without a traceback
   in a temporary Git repository on NTFS.
8. This phase does not add a Windows package classifier or make a general Windows
   support claim. Any experimental capability label names the exact read-only/local
   commands proven here. Dependencies are conditional where needed, with no
   unconditional Windows-only dependency on POSIX systems.
9. Existing Linux tests plus new platform-unit tests pass, including explicit
   tests of both platform branches rather than tests that merely mock away all
   Windows behavior.

## Out of Scope

- Launching agents, verification commands, or the complete implementation lifecycle
- Windows process-tree termination
- Docker Desktop and the container execution backend
- UNC/network workspaces

## Design Notes

Prefer small adapters for locks, atomic filesystem operations, and platform facts.
Do not scatter `sys.platform` conditionals through the orchestrator. Preserve the
meaning of existing state and lock-owner files so an upgraded repository remains
readable.
