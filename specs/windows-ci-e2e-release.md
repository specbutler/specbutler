---
id: windows-ci-e2e-release
area: backend
priority: 40
depends_on:
  - windows-web-autopilot
description: Continuously prove and document the supported Windows product
---

# Windows CI, End-to-End Proof, and Release

## Goal

Turn Windows support from a one-time port into a continuously verified support
promise, backed by both hosted CI and the persistent local Windows 11 lab.

## Acceptance Criteria

1. GitHub Actions installs the built wheel on a supported Windows runner and runs
   lint plus the full portable test suite on supported Python versions. Windows-
   specific integration tests are not skipped merely because CI is Windows Server.
2. CI smoke tests create a repository whose path contains spaces and Unicode, run
   `init`, `doctor`, `list`, `show`, `status`, lifecycle fixture flows, web
   foreground/background, autopilot dispatch/stop, and cleanup from the wheel.
3. A checked-in, secret-free Windows-lab harness documents and automates creation,
   reset, source sync, command execution, log/artifact retrieval, and snapshotting
   for the local KVM-backed Windows 11 evaluation VM. Machine-specific credentials
   and disk images stay outside Git.
4. The local Windows 11 proof run installs the release candidate wheel and executes
   a real Codex lifecycle, real multi-turn web chat, cancellation/timeout tree
   cleanup, autopilot dependency dispatch, restart recovery, and a real disposable
   GitHub PR/merge. Logs and machine-readable results are retained as release
   evidence with secrets redacted.
5. Repeating the local proof from a clean VM snapshot requires one documented
   command (apart from provider/Microsoft authentication when tokens expire) and
   does not require editing files inside the guest manually.
6. README, INSTALL, troubleshooting, and support-matrix documentation state exact
   supported Windows edition/filesystem/backend/agent/shell combinations, known
   limitations, and setup commands. Claims distinguish native Codex support from
   Claude and Docker/container alternatives.
7. Windows is added to package classifiers, release verification builds and
   installs both wheel and source distribution on Windows, and `spec doctor`
   produces no unexplained warnings in the documented supported configuration.
8. A final audit maps every Windows acceptance criterion to current CI or retained
   VM evidence; missing, skipped, flaky, or indirect evidence blocks the release.

## Out of Scope

- Bundling or licensing Windows, Microsoft Office, Codex, Claude, Git, or GitHub CLI
- Promising UNC/network workspaces
- Native Claude support before its host-sandbox prerequisite is available

## Design Notes

Hosted CI catches regressions; the Windows 11 VM proves behaviors that Windows
Server runners and mocks cannot. Keep the VM reusable but make the proof resettable.
