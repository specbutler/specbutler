# Spec Butler restoration plan (October 2026)

**Goal:** make "well-formed spec in → tested, reviewed, merged change out" reliable
again, then use Spec Butler to build the rest of its own improvements, recording what
it does well and badly along the way.

Background: the October 2026 evaluation found that 83% of 958 runs merged, but only 28%
merged cleanly. Usage stopped because of sandbox and git friction (the last two runs
were killed by the private-git guard after the agent had finished its work), review
loops that didn't converge, failure storms with no backoff, and no live sessions, so
the operator had to babysit runs.

## Working constraints

- **Function over safety for git:** agents must be able to use git like a human would
  (log, add, commit, stash, branch, rebase). Keep the blast radius small where it's
  cheap, but don't give up working git to get it.
- **No mid-run approvals:** every unattended launch must finish without a human. A
  denial becomes a typed failure that is reported, not a hang and not a silent retry.
- **No nightly or scheduled tests.** Drift checks go in `spec doctor`, in launch
  preflights, or in PR gates.
- **Sessions can't do everything themselves:**
  - `gh` fails TLS inside the Claude Code sandbox, so the user either runs
    `gh pr create` or allows `gh` in sandbox settings. Merges go through the app's PR
    tools (bind, auto-merge).
  - Nested sandboxes fail inside a sandboxed session, so **`spec` runs and sandbox
    probes must be launched from the user's own terminal**, not from a session's shell.

## Phase 0: in flight

- [ ] specbutler/specbutler#38: back off infrastructure failures, circuit-break
  relaunch storms (auto-merge on)
- [ ] specbutler/specbutler#39: parse numbered acceptance criteria, validate the spec
  dependency graph (auto-merge on)

## Phase 1: restore minimum functionality (Claude Code sessions)

### 1A. Git-capable workspaces (one session, since 1A and 1B share files)

- **Worktree mode:** let the agent run `git add`/`commit`/`stash` in a linked worktree.
  Grant writes to the repo's git common directory instead of using the private
  `GIT_DIR` plus import. Keep `hooks/` and `config` read-only if that's cheap (Claude
  already does this natively), and remove the metadata guards that fail runs where the
  agent did nothing wrong. Fall back to wider writes if that's what makes it work.
- **Clone mode:** a per-run `git clone --local` (hardlinked, about 1s, under 1 MB
  extra), with a writable `.git` except `config`, `config.worktree`, `hooks/` and
  `info/`. Codex already supports this (#26); add the Claude side. Give the agent an
  identity through `GIT_AUTHOR_*`/`GIT_COMMITTER_*` and set `HUSKY=0`. The host
  fetches before cloning and publishes from the clone.
- **Make `clone` the default backend** for `spec init` and when `.spec.toml` doesn't
  set one.
- **Add a model-free `spec doctor` git probe** for each backend and provider: add,
  commit and stash succeed; writes to `.git/config`, `.git/hooks` and paths outside
  the workspace fail.

### 1B. Container mode → alpha (separate small session, in parallel)

- Label it alpha in README and docs (one banner line at the top of
  `docs/execution-backends.md`).
- Have `spec doctor` and run start print a one-line alpha warning when
  `backend = "container"`.
- No behaviour changes.

### 1C. Native safety mode (after 1A merges, since it touches the same settings code)

Make `[execution].safety_mode` actually change behaviour:

- **`native`, the default:**
  - **Claude:** bypass mode with the sandbox on, `failIfUnavailable`,
    `allowUnsandboxedCommands: false`, credential read-denies, deny rules for
    `git push` and `git reset --hard`, and a PreToolUse hook that keeps Edit/Write
    inside the workspace. Drop `--restricted` and `disableBypassPermissionsMode` for
    implement.
  - **Codex:** `exec` with a dedicated `CODEX_HOME` and a profile built on the
    workspace profile, with network on. Optionally use the auto-review approver.
- **`isolated`:** container mode (alpha).
- **Configurable network:** replace the hardcoded Claude allowlist with config
  (open with a deny list, or an allowlist).
- **No approval-dependent launches:** Codex operator intervention and authoring stop
  depending on `on-request` with network off whenever they're launched unattended.
- **Sandbox denials become a typed failure** that is reported and doesn't use up a
  retry.

### Phase 1 exit criteria ("minimum functionality restored")

1. The user runs `spec doctor` in their terminal, and the git probe passes for claude
   and codex in both clone and worktree modes.
2. Two small canary specs run end to end through `spec implement`, launched from the
   user's terminal, and merge with no sandbox or git failure:
   - `canary-ps-test-skip`, implemented by Claude and reviewed by Codex;
   - `canary-doctor-cli-versions`, implemented by Codex.
3. The installed `spec` (currently 0.5.0 via pyenv) runs the merged `main`.

## Phase 2: dogfood (author specs, let Spec Butler implement them)

Write these as specs in `specs/`, review and merge the spec PRs, then run them with
`spec implement` or `spec auto run --concurrency 2` from the user's terminal. Watch
`spec watch` and `spec status`.

| Spec id | What | Depends on |
|---|---|---|
| `review-convergence` | Give the reviewer its prior findings and their status, plus the incremental diff. Restore read-only repo inspection for the Codex reviewer. Block deterministically only on P0/P1, cap review at about 3 rounds and then escalate with a summary, and file new non-blocking findings as follow-up specs. Fix the retirementlab prompt that tells the reviewer it can run commands it can't. | Phase 1 |
| `agent-sessions` | Turn on session persistence with `--session-id`, resume the same session for verify and review retries, take the final result from `--json-schema` / Codex `--output-schema` instead of the `spec report` handshake, and add `spec attach` to take over interactively. | Phase 1 |
| `model-and-cost` | Model per role (implement, review, debugger, retry escalation). Save cost and tokens per run and per phase for both providers. Add `--max-budget-usd` as a stop-loss. Have `spec doctor` warn when `ANTHROPIC_API_KEY` would override subscription login. | `agent-sessions` |
| `spec-lint-and-authoring` | A `spec lint` command (building on #39), bundled authoring and scoping prompts (generalised from retirementlab's), a critique pass before implementing, an option to put the spec and its implementation in one PR, and detection of specs already landed by hand. | Phase 1 |
| `canary-ps-test-skip`, `canary-doctor-cli-versions` | The Phase 1 canaries (see exit criteria). | — |
| `generality-cleanup` | Remove hardcoded retirementlab leftovers: Postgres stop on cleanup, "origin/master" and "conflicts with master" in prompts, and the pytest-only diagnostic rerun. | Phase 1 |
| `harness-integration` | Spec Butler as a Claude Code plugin/skill/MCP server (dispatch, status, steer, take over), and the option to use the repo's CI review gates as the review phase. | `agent-sessions`, `review-convergence`, `spec-lint-and-authoring` |

### Dogfood protocol

- Keep `docs/plans/dogfood-log.md`. Every intervention, failure, retry storm, review
  loop and manual fix gets a dated entry with the run id, what happened, and how it
  was resolved.
- When Spec Butler itself breaks during a run, fix it in an ad-hoc session (a
  worktree with a PR), log the fix, and resume the run. Don't hand-implement a spec's
  own scope unless the run is hopeless; if that happens, log why.
- Each phase ends with a short summary in the log: runs, clean-merge rate, retries,
  review rounds, cost.

## Punted, not forgotten

Stacked PRs along the DAG, memory across runs, splitting `orchestrator.py`, remote and
cloud executors, deleting the private-git code (decide after 1A has been in use for a
while). Never: nightly real-CLI tests.
