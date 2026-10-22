---
id: canary-doctor-cli-versions
area: backend
priority: 6
depends_on: []
description: Report installed provider CLI versions in spec doctor
---

# Canary: Provider CLI Versions in spec doctor

## Goal

Provider CLI changes are the main source of Spec Butler breakage, yet `spec doctor`
doesn't report which Claude Code and Codex versions are installed. Report them, so
operators and bug reports always include the versions in play.

This is also a Phase 1 canary: a small, real change to run through `spec implement`
end to end, implemented by Codex.

## Acceptance Criteria

- [ ] **`spec doctor` prints the installed version** of each configured agent CLI
  (`claude --version`, `codex --version`) on its own line, with the resolved
  executable path.
- [ ] **A missing CLI, or a version command that fails or times out after 10
  seconds,** is reported as a warning with the error. It's an error only when that
  CLI is the configured default agent, which matches existing doctor semantics for a
  missing default agent.
- [ ] **`spec doctor --json`,** if JSON output exists, includes the versions as
  structured fields: name, path, version string, ok.
- [ ] **Output is parsed loosely.** The full first line of output is kept, and a
  semver-looking token is extracted when present (`2.1.296`, `codex-cli 0.156.1`).
- [ ] **Hermetic tests** use fake `claude` and `codex` binaries on `PATH` to cover
  the normal output, missing, timeout and unparseable cases.

## Out of Scope

- Minimum-version enforcement or upgrade advice.
- Checking the GitHub CLI version.

## Design Notes

- Doctor checks live in `src/spec_runtime/doctor.py`. Follow its existing check and
  result structure.
