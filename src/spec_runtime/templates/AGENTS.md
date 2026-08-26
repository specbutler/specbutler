# [Project Name] – Agent Notes

## Spec-Driven Development

New features and improvements are described in **spec files** under `specs/`.
Each spec is a self-contained task for an agent to implement autonomously.

- **Dispatchable specs** belong in `specs/`. These are the specs discovered by
  `spec list`, shown by `spec watch`, and eligible for `spec auto run`.
- **Task specs** under `specs/tasks/` are for the `spec task` flow and are not
  part of normal top-level spec discovery.

### The `spec` CLI

The `spec` command is the primary interface for humans and agents.

**Primary commands:**

| Command | What it does |
|---------|-------------|
| `spec create [--spec ID]` | Author a new spec interactively |
| `spec implement --spec ID [--agent claude\|codex] [--review-agent claude\|codex]` | Start/resume implementation workflow |
| `spec status --spec ID` | Show run state and gate status |
| `spec list [--all]` | List specs with status and dependencies |
| `spec show --spec ID` | Display a spec's content |
| `spec report --status ok\|blocked\|error\|needs-input` | Report implement-phase completion |
| `spec stop --spec ID` | Stop an active run without deleting its workspace |
| `spec clean --spec ID` | Remove execution workspaces and branches |
| `spec task [--agent claude\|codex] [--review-agent claude\|codex]` | Describe and execute a quick task |

### Isolated Workspace Editing Rule

**Never edit files in the main worktree.** The main worktree is for
orchestration only — running `spec implement`, `spec status`, etc. All file
changes must happen in the isolated workspace provided by the execution
backend.

### Implement Agent Contract

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

### Default Worktree-Backend Conventions

| Type | Branch | Worktree |
|------|--------|----------|
| Spec implementation | `code/<spec-id>--<run-token>` | `.worktrees/code-<spec-id>--<run-token>/` |
| Task | `task/<slug>--<token>` | `.worktrees/task-<slug>--<token>/` |
| Spec authoring | `spec/<spec-id>` | `.worktrees/spec-<spec-id>/` |
