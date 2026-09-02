# Spec Butler – Agent Notes

## Purpose

Spec-driven development workflow CLI. The `spec` command manages the full
lifecycle from spec authoring through implementation, verification, review,
merge, and cleanup. The package is at `src/spec_runtime/` with clean adapter
boundaries (forge, agent) that make it repo-agnostic.

## Multi-Agent Project

This project supports multiple coding agents working on specs in parallel.

- **AGENTS.md is the single source of truth** for project knowledge and workflow.
  It must stay agent-neutral — no agent-specific behavior belongs here.
- **Agent-specific instructions** live in dedicated files: `CLAUDE.md`, `CODEX.md`.
  Each agent should self-identify and look for its own file on startup.
- Agent-specific files are **strictly additive** — they add tool tips only, never
  contradict or duplicate AGENTS.md content.

### Isolated-Workspace Editing Rule

**Never edit files in the main worktree.** The main worktree is for orchestration
only — running `spec implement`, `spec status`, etc. All file changes must happen
in a dedicated worktree or an orchestrator-provided isolated workspace.

- **Spec work** — use `spec implement --spec <id>`, which creates the configured
  isolated execution workspace automatically.
- **Ad-hoc fixes** — create a worktree manually:
  ```bash
  git worktree add .worktrees/<short-name> -b fix/<short-name> origin/main
  cd .worktrees/<short-name>
  # ... make changes, commit, push, open PR ...
  ```

### Default Delivery Contract

When a human asks for code changes directly (outside an orchestrated implement
session), treat delivery as end-to-end:

1. Commit and push the branch.
2. Open or update a PR targeting `main`.
3. Watch required CI checks. If checks fail, fix and push follow-up commits.
4. Merge the PR once checks pass (squash by default).

This does **not** override orchestrator-owned phases during spec runs.

## Architecture

```
src/spec_runtime/
├── cli.py                  # Public CLI entry point (spec command)
├── init.py                 # spec init — repo bootstrapping
├── orchestrator.py         # Lifecycle engine and phase state machine
├── config.py               # .spec.toml loader
├── forge.py                # ForgeAdapter protocol + GitHubForge
├── agent_adapter.py        # AgentAdapter protocol + Claude/Codex
├── spec_identity.py        # Branch/worktree naming conventions
├── spec_metadata.py        # Spec YAML frontmatter parsing
├── spec_status.py          # Status resolution (merged/in-progress/etc.)
├── spec_merge_tags.py      # Git merge tag provenance
├── review_feedback.py      # Review result lookup and parsing
├── review_gate.py          # In-process review gate evaluation
├── review_gate_sticky_comment.py  # PR sticky comment publishing
├── autopilot.py            # Dispatcher for parallel spec runs
├── autopilot_tui/          # Textual TUI for autopilot watch
├── backfill_merge_tags.py  # Merge tag audit/backfill/repair
├── spec_table.py           # Spec listing table renderer
└── templates/              # Bundled review prompt, schema, AGENTS.md
```

### Key adapter boundaries

- **ForgeAdapter** (`forge.py`) — isolates GitHub-specific operations behind a
  protocol. `GitHubForge` uses the `gh` CLI. To support another forge, implement
  the protocol and call `set_forge_adapter()`.
- **AgentAdapter** (`agent_adapter.py`) — isolates agent-specific launch behavior.
  Methods: `build_implement_command()`, `build_authoring_command()`,
  `build_review_command()`. Register custom agents with `register_agent_adapter()`.

### MCP isolation

Non-interactive agent sessions (implement, recovery, review, block-debugger)
only see MCP servers the orchestrator explicitly provides — they do **not**
inherit user-level Codex/Claude MCP registrations. Codex isolation uses a
per-worktree `CODEX_HOME` at `<worktree>/.spec-codex-home`; Claude isolation
uses `--mcp-config <path> --strict-mcp-config` against
`<worktree>/.claude/mcp-servers.json`. Interactive authoring (`spec create`,
`spec task` scoping) keeps the user's full MCP toolbox. To selectively allow a
user-registered server through into non-interactive sessions, list its name
under `[mcp] allow_from_user` in `.spec.toml`.

### Testing

```bash
pytest tests/           # full test suite
pytest tests/ -x -q     # stop on first failure
```

Tests are hermetic: they do not require network access, model providers, a real
forge remote, or Docker. Some fixtures create temporary Git repositories and
run bounded local command-parser/preflight checks.

To observe or neutralize orchestrator waiting, patch `orch._poll_sleep`, never
`orch.time.sleep`. The latter is the process-global `time.sleep`, which
`subprocess.Popen.wait(timeout=...)` busy-waits on: any phase that shells out
with a timeout dumps tens to thousands of stdlib sleeps into the same counter,
and how many depends only on runner speed.

The test suite covers:
- `test_spec_orchestrator.py` — lifecycle phases, retries, state machine, review
- `test_spec_cli_surface.py` — forge/agent adapters, CLI dispatch
- `test_spec_identity.py` — branch naming, PR classification
- `test_spec_init.py` — init command, config enforcement, detection

### Verify gates

```bash
pytest tests/           # test gate
ruff check .            # lint gate
```

## The `spec` CLI

| Command | What it does |
|---------|-------------|
| `spec init` | Bootstrap a repo for spec-driven development |
| `spec create [--spec ID]` | Author a new spec interactively |
| `spec implement --spec ID [--agent A] [--review-agent R]` | Start/resume implementation workflow |
| `spec status --spec ID` | Show run state and gate status |
| `spec review --pr NUMBER` | Inspect full machine-readable PR review feedback |
| `spec list [--all]` | List specs with status and dependencies |
| `spec show --spec ID` | Display a spec's content |
| `spec report --status ok\|blocked\|error\|needs-input` | Report implement-phase completion |
| `spec stop --spec ID` | Stop an active run without deleting its workspace |
| `spec clean --spec ID` | Remove execution workspaces and branches |
| `spec task [--agent A] [--review-agent R]` | Describe and execute a quick task |
| `spec doctor` | Validate repository configuration and local dependencies without mutation |
| `spec phase --spec ID --phase NAME [--agent A] [--review-agent R]` | Run a single orchestrator phase |
| `spec watch [--interval N] [--agent A]` | Interactive TUI dashboard (requires the `tui` extra) |
| `spec gc [--apply]` | Reconcile stale run state (dry-run by default, `--apply` to mutate) |
| `spec auto run [--concurrency N] [--poll-interval N] [--notify ...] [--dry-run] [--agent A]` | Dispatch loop — runs multiple specs in parallel |
| `spec auto stop` | Graceful shutdown of running dispatcher |

## Spec Placement

- **Dispatchable specs** belong in `specs/`. These are the specs discovered by
  `spec list`, shown by `spec watch`, and eligible for `spec auto run`.
- **Task specs** under `specs/tasks/` are for the `spec task` flow. They are
  not part of normal top-level spec discovery and should not be used for work
  you expect autopilot to pick up as a regular spec.

### Other tools

| Command | What it does |
|---------|-------------|
| `spec-table` | Render specs table with status |
| `spec-backfill-merge-tags audit` | Check for missing/broken merge tags |
| `spec-backfill-merge-tags backfill` | Create missing merge tags |
| `spec-backfill-merge-tags repair` | Fix broken merge tags |

## Implement Agent Contract

When launched by the orchestrator for `implement`, follow this contract:

1. **Read context first** — read the spec file and `AGENTS.md` before editing.
2. **Stay in the provided workspace** — do not switch branches or start another run.
3. **Apply only this attempt's scope**:
   - Initial/retry: implement remaining spec work
   - Verify retry: fix the failing gate output provided by orchestrator
   - Review retry: address unresolved review findings only
   - Merge-conflict retry: resolve merge conflicts only
4. **Run verify gates locally** before reporting.
5. **Do not run orchestrator lifecycle commands** (`spec implement`,
   `spec phase`) or publish/merge/cleanup actions from inside the
   implementation session. `spec report` is the exception — it is required
   (see step 6).
6. **Report completion before exit**:
   ```bash
   spec report --status ok|blocked|error|needs-input --summary 'plain text summary'
   ```
   Keep the summary shell-safe: single-quote the complete value, avoid
   apostrophes, and do not include backticks or `$()`. Describe commands without
   Markdown code delimiters because the shell evaluates substitutions before
   `spec report` starts.
   Use `needs-input` when the spec is genuinely ambiguous and a wrong guess
   would waste retry cycles. Describe the ambiguity and include specific
   options when possible.
   Wait for `Completion recorded for <spec-id>:` before exiting.

## Default Worktree-Backend Conventions

| Type | Branch | Worktree |
|------|--------|----------|
| Spec implementation | `code/<id>--<token>` | `.worktrees/code-<id>--<token>/` |
| Task | `task/<slug>--<token>` | `.worktrees/task-<slug>--<token>/` |
| Spec authoring | `spec/<id>` | `.worktrees/spec-<id>/` |
