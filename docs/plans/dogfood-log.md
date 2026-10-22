# Dogfood log

A record of every intervention, failure and manual fix while Spec Butler is restored
and then used to implement its own improvements. See
[2026-10-restoration.md](2026-10-restoration.md) for the plan.

Entry format: date, phase, run id or session, what happened, how it was resolved, and
the follow-up, if any.

## Phase 0–1 (Claude Code sessions, before Spec Butler runs)

These aren't Spec Butler runs. They're friction in the sessions that are repairing
it, logged because the same constraints will affect dogfood runs.

- **2026-10-21, Phase 0, fix sessions for #38/#39:** `gh` fails TLS verification
  inside the Claude Code sandbox (`x509: OSStatus -26276`), while `git push` works.
  Sessions couldn't open PRs. **Resolution:** the user ran `gh pr create` from their
  terminal for both PRs, and sessions bound them with the app's PR tools.
  **Follow-up:** allow `gh` in the user's sandbox settings, or keep handing PR
  creation to the user.
- **2026-10-21, Phase 0:** the sandbox blocks `ps`, so about 400–477
  process-inspection tests fail locally on clean `main`. Sessions couldn't get a
  green full suite. **Resolution:** CI was treated as the first full run, and both
  PRs passed it. **Follow-up:** consider skipping process-inspection tests when `ps`
  is unavailable, with a clear marker, so local runs inside a sandbox are meaningful.
- **2026-10-22, Phase 0, #38 session:** Claude Code's auto-mode classifier denied a
  `git fetch` of `origin/main`. A later retry worked. **Resolution:** none needed.
- **2026-10-22, Phase 0:** after #39 merged, #38 sat at `mergeStateStatus=BEHIND`.
  Auto-merge waits for an up-to-date branch. **Resolution:** the session was asked to
  sync with main. **Follow-up:** Spec Butler's own merge phase should handle "behind"
  by updating the branch, not by waiting.
- **2026-10-22, evaluation session:** nested sandboxes fail inside a sandboxed
  session (`sandbox_apply: Operation not permitted`), so neither `codex sandbox`
  probes nor `spec` runs can be launched from a session's shell. **Resolution:**
  probes and `spec` runs are launched from the user's terminal.

- **2026-10-22, Phase 1:** the user fixed the sandbox settings so `gh` passes TLS
  verification, and `gh api` and `gh pr create` now work from sessions. The
  container-alpha session's `gh pr create` was then still denied by the auto-mode
  classifier, because nothing in that session said the user had authorized it.
  **Resolution:** the user granted standing authorization ("feel free to make changes
  to the specbutler repo without further manual authorization from me"), and the
  coordinator relayed it verbatim to every linked session. **Follow-up:** briefs for
  unattended sessions should carry the user's authorization for publish and merge
  steps up front.

## Phase 2 (Spec Butler runs)

_No runs yet._

## Summaries

_Added at the end of each phase: runs, clean-merge rate, retries, review rounds,
cost._
