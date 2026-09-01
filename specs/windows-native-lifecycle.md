---
id: windows-native-lifecycle
area: orchestrator
priority: 20
depends_on:
  - windows-command-runtime
  - windows-process-supervision
description: Run the complete Codex worktree lifecycle natively on Windows
---

# Windows Native Lifecycle

## Goal

Make the core spec-to-merge workflow function in a local NTFS repository on
Windows using Codex and the worktree backend, while failing closed for provider
features whose isolation guarantees are unavailable.

## Acceptance Criteria

1. `spec create`, `spec task`, `spec implement`, `spec phase`, `spec report`,
   `spec input`, `spec status`, `spec stop`, `spec clean`, `spec gc`, and foreground
   `spec update` operate in a native Windows repository using the worktree backend
   and platform-valid Git paths.
2. A real Codex implementation run creates an isolated worktree, bootstraps it,
   edits and commits a fixture change, passes gates, performs review, opens and
   merges a pull request on a disposable GitHub repository, records provenance,
   and cleans up without manual repair.
3. Stop, timeout, failed gate, review retry, resume after orchestrator restart,
   stale state, merge-conflict recovery, and `needs-input` followed by `spec input`
   resolution are exercised on Windows. Owned subprocesses are gone afterward and
   unrelated processes are untouched.
4. Codex home/config/auth isolation works without requiring symlink privileges or
   Developer Mode. Secrets are copied or referenced with least privilege and are
   absent from logs, commits, review workspaces, and cleanup remnants.
5. Git and GitHub CLI discovery works from normal Windows installations. Commands
   tolerate drive-letter paths, backslashes, spaces, Unicode, CRLF output, and
   case-insensitive filesystem behavior.
6. Security-sensitive reviewer/block-debugger paths use an isolation mechanism
   that is proven on Windows or fail closed with an actionable explanation. POSIX
   `chmod` or read-only file attributes are not presented as a security boundary.
7. Native Claude execution remains explicitly unavailable while its required
   host sandbox is unsupported; selection fails before launch with documented
   alternatives and does not weaken sandbox policy.
8. Foreground update checks and applies through the documented safety gates. The
   background refresh mode uses the detached lifetime from
   `windows-process-supervision`, survives CLI exit, records durable identity, and
   leaves no stale process or state after completion.
9. Hermetic Windows integration tests cover the lifecycle without model, network,
   or real-forge dependencies, and a separately marked real-provider proof test
   covers the end-to-end Codex path.
10. Linux/macOS lifecycle behavior and state compatibility remain unchanged.

## Out of Scope

- Claiming native Claude support without an enforceable provider sandbox
- Docker Desktop/container execution
- Excel or other desktop-application automation
- UNC/network workspaces

## Design Notes

The first support tier is native Windows 11, local NTFS, worktree backend, and
Codex. Unsupported features must be named by `spec doctor` before a run starts.
