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
- 2026-10-22 Phase 1 — Strict "branch must be up to date" rule: each merge (#40 ecb8711) left the other open PRs (#38, #41, #42) BEHIND, so auto-merge stalled. Resolution: coordinator ran `gh pr update-branch` and started a merge-train monitor that updates BEHIND auto-merge PRs every 2 minutes. Follow-up: Spec Butler's own merge phase already handles BEHIND (orchestrator ~23457); concurrent autopilot runs will hit the same serialization.
- 2026-10-22 Phase 1 — #43 (1A) went red on all test jobs + review gate and became DIRTY after #40 merged; its session had the CI monitor off (auto_fix=false), so nothing would have woken it. Resolution: coordinator messaged it to enable the monitor, sync, and fix from CI logs. Follow-up: briefs should tell sessions to enable auto_fix when they open a PR; the sandbox `ps` block makes local full-suite runs meaningless (canary-ps-test-skip addresses this).
- 2026-10-22 Phase 1 — Spec PR #41: the CI Codex review gate approved the specs at dbcb3df, then requested changes on the next head (only a merge of main), flagging that harness-integration could dispatch before spec-lint-and-authoring (its lint_spec tool needs spec lint). The finding was valid but newly raised — the same re-review drift seen in run history. Resolution: added the dependency (aa04bb3). Follow-up: review-convergence spec covers prior-findings context and incremental diffs.
- **2026-10-22, Phase 1, PR #41:** the `test (3.12)` job failed on an unrelated flaky test, `tests/test_web_service_linux.py::test_linux_chrome_bootstrap_keeps_credentials_out_of_url_history` ("Chrome did not load the authenticated clean URL ... timed out"); 3,516 other tests passed. **Resolution:** re-ran the failed job. **Follow-up:** quarantine the test or make it more robust; on autopilot runs this would have cost a verify retry.
- **2026-10-22, Phase 1, CI:** re-running only the failed jobs after a flake can never pass. `tools/ci_evidence.py aggregate` rejects fragments from different run attempts ("fragments came from different workflow runs"), so `ci` and `ci-evidence` stay red. **Resolution:** re-ran whole workflows for #41 and #38. **Follow-up:** have the aggregator accept fragments from the same run id across attempts, or document re-running the full workflow.
- **2026-10-23, Phase 1, PR #43:** at 74b67df the review gate finally approved, after three review rounds, each raising new P1s: packed refs, sibling worktree metadata, probe leaks. Windows 3.12 then failed only `tests/test_platform_fs.py::test_locked_state_updates_survive_multiple_processes` (`assert None == 0`), while 3.11 and 3.13 passed. **Resolution:** treated it as a timing flake and re-ran the full workflow. **Follow-up:** harden that multiprocess lock test on Windows.
- **2026-10-23, Phase 1, coordination:** under `/goal`, every turn after the user's last message is an automated event. The desktop app refuses to start a new session on such turns, so 1C could only be offered as a card. Cards can't carry a source branch, so 1C waited for #43 to merge. **Resolution:** the user starts the 1C card. **Follow-up:** this is a real constraint for unattended orchestration from a chat session, and another reason Spec Butler itself (not a coordinator chat) should drive Phase 2.
- **2026-10-23, Phase 1 milestones:** #39 → 8a4d1e2, #40 (1B) → ecb8711, #42 → 06505f5, #41 (specs) → 79714c4, #38 → 9dab779, #43 (1A) → 29985d7, all with green CI (#43: 20 passed, 2 skipped). The coordinator's merge-train monitor kept auto-merge PRs current with main and re-ran flaky workflows.

## Phase 2 (Spec Butler runs)

_No runs yet._

## Summaries

_Added at the end of each phase: runs, clean-merge rate, retries, review rounds,
cost._
