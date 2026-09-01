---
id: windows-container-backend
area: backend
priority: 60
depends_on:
  - windows-native-lifecycle
description: Support the Linux container backend from a Windows Docker Desktop host
---

# Windows Container Backend

## Goal

Allow Windows users to choose the existing Linux container isolation model through
Docker Desktop, including Claude where its Linux sandbox prerequisites are met.

## Acceptance Criteria

1. Container modules import on Windows without `grp` or other POSIX host APIs.
   User mapping, mounts, workspace paths, and Docker command construction are
   selected through a tested host-platform adapter.
2. `spec doctor` validates Docker Desktop, Linux-container mode, mount sharing,
   required image tools, provider credentials, and filesystem placement with
   actionable remediation.
3. Repository and worktree paths containing drive letters, spaces, and Unicode are
   mounted correctly. State written by the Linux worker remains usable and
   cleanable by the Windows host.
4. Real Docker Desktop integration tests cover image build/cache locking,
   bootstrap, Codex implementation, Claude implementation when authenticated,
   stop/timeout, resume, and cleanup without orphaned containers or worktrees.
5. Container onboarding supplies a maintained baseline image recipe and a doctor-
   driven customization workflow instead of requiring users to guess a just-right
   project image.
6. Native worktree and POSIX container behavior remain unchanged.

## Out of Scope

- Windows containers
- Requiring Docker Desktop for native Codex support
- Claiming native Windows Claude sandbox support

## Design Notes

This is an additional support tier, not a prerequisite for native Windows. Keep
Linux commands inside the worker POSIX-native while porting only host concerns.
