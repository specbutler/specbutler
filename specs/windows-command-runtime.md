---
id: windows-command-runtime
area: orchestrator
priority: 10
depends_on:
  - windows-platform-foundations
description: Define compatible command and shell semantics for native Windows
---

# Windows Command Runtime

## Goal

Execute bootstrap, hooks, gates, and internal commands predictably on Windows
without heuristically translating POSIX shell syntax or requiring Git Bash.
Existing POSIX repositories must continue to behave exactly as before.

## Acceptance Criteria

1. One typed command runner is shared by runtime execution and `spec doctor`. It
   distinguishes direct argument-vector commands from explicit shell scripts and
   records a redacted, shell-correct display form for diagnostics.
2. Existing command-string configuration retains its current behavior on POSIX.
   Additive Windows overrides and argument-vector forms are supported for
   bootstrap, setup/teardown hooks, and verify gates; their precedence and mutual
   exclusivity are validated with actionable errors.
3. Shell scripts declare their shell. Native Windows supports built-in Windows
   PowerShell and `cmd.exe`; PowerShell 7 may be selected when installed. The
   runtime never silently rewrites a POSIX script or assumes Git Bash.
4. Direct commands are launched without a shell and preserve arguments containing
   spaces, quotes, Unicode, backslashes, dollar signs, and shell metacharacters.
   Tests execute real helper programs and assert their received argument vectors.
5. `spec init` emits a Windows-valid bootstrap for `.venv\\Scripts\\python.exe`
   while preserving the existing POSIX template. A repository may keep both
   platform variants in one `.spec.toml`.
6. `spec doctor` validates the command variant that the current platform will
   execute, identifies a missing declared shell, and reports a targeted migration
   when only a clearly POSIX bootstrap is present on Windows.
7. Bootstrap, hook, gate, and review-bootstrap failures preserve exit codes and
   bounded output in the same state/log surfaces used on POSIX.
8. Unit and integration tests cover Windows and POSIX selection, PowerShell and
   `cmd.exe`, argv mode, path spaces, failure propagation, and secret redaction.
   Existing Linux behavior and configuration remain backward compatible.

## Out of Scope

- Translating arbitrary Bash to PowerShell
- Bundling a third-party shell
- Process-tree cancellation, which belongs to `windows-process-supervision`

## Design Notes

Favor explicit configuration over shell guessing. Platform-specific keys must be
additive so current `.spec.toml` files continue to work on Linux and macOS.
