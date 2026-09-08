# Getting Started with Spec Butler

This guide walks you through adopting `spec` in an existing repository. By the
end you will have a working configuration, a spec file, and an understanding of
the full implementation lifecycle.

## Prerequisites

- **Linux, macOS, or the documented native Windows tier** -- Windows support is
  Windows 11, local fixed NTFS, the worktree backend, Codex, and PowerShell;
  review the [support matrix and limitations](windows.md) before setup
- **Python 3.11+**
- **pipx** -- installs the CLI in an isolated environment
- **git** (with a remote named `origin`)
- **At least one authenticated agent CLI on PATH** -- `claude` or `codex`
- **`gh` CLI** -- required for forge operations (push, PR creation, merge)

## 1. Install

Install the latest tagged GitHub Release:

```bash
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install \
  "specbutler @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"
```

Or clone and install in editable mode for development:

```bash
git clone https://github.com/specbutler/specbutler.git
cd specbutler
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,tui,web]"
```

The tagged release is the stable channel. An explicit `@main` VCS install is a
development channel; see [Releasing and install channels](../RELEASING.md).

Verify the install:

```bash
spec --version
gh auth status
claude --version  # or: codex --version; use Codex for native Windows
claude auth status  # or: codex login status
```

On native Windows, use the PowerShell installation commands in
[INSTALL.md](../INSTALL.md), keep the clone on a local NTFS drive, and run
`git config --system core.longpaths true` once from an elevated shell. Native
Claude is unavailable and fails closed; WSL2/Linux is the alternative when
Claude is required.

## 2. Initialize

Run `spec init` from the root of your repository:

```bash
cd /path/to/your-repo
spec init
```

This command:

- Creates **`.spec.toml`** -- the project configuration file.
- Creates **`specs/`** and **`specs/tasks/`** directories for spec and task
  files.
- Creates **`AGENTS.md`** -- a template with agent instructions and the
  implement-agent contract.
- Creates **`.github/prompts/review.md`** -- a review prompt template.
- Adds runtime workspaces, local configuration, and agent-local state paths to
  **`.gitignore`**.

### Auto-detection

`spec init` inspects your repo and auto-configures several settings:

| Setting | Detection logic |
|---------|----------------|
| `base_ref` | Uses local `origin/HEAD`, a conventional ref, or the only remote-tracking ref without contacting the remote; use `spec init --base REF` if ambiguous |
| `agents.default` | First of `claude`, `codex` found on `PATH` |
| `agents.allowed` | All of `claude`, `codex` found on `PATH` |
| Bootstrap | Prefers `make install`; otherwise detects Python or Node package setup |
| Verify gates | Scans Python, Make, Node, and Swift metadata for common build and verification commands |

If no agents are found on `PATH`, init fails with an actionable error. Install
at least one agent and run `spec init` again.

Run the read-only onboarding preflight before the first workflow:

```bash
spec doctor
```

It validates the configured base ref and origin, GitHub repository identity,
agent and GitHub CLI access, provider login state, bootstrap and verify command
executables, runtime path safety, and the selected execution backend. Blockers
exit nonzero and include an exact remediation; optional missing agents,
unverifiable provider login state, and unignored paths are reported as warnings.

## 3. Configure

### .spec.toml

Open `.spec.toml` and review the generated configuration. A typical starter
set of options is:

```toml
base_ref = "origin/main"

[paths]
specs_dir = "specs"
task_specs_dir = "specs/tasks"
state_dir = ".spec-state"       # gitignored -- runtime state
worktrees_dir = ".worktrees"    # gitignored -- git worktrees

[retry]
cap = 20                        # max retries across verify/review/merge failures
no_progress_retry_threshold = 2 # consecutive no-progress retries before giving up

[agents]
default = "claude"
review_default = "codex"
allowed = ["claude", "codex"]

[bootstrap]
install_command = "python -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'"

[verify]

[[verify.gates]]
name = "test"
command = ".venv/bin/python -m pytest"
parallel = true

[[verify.gates]]
name = "lint"
command = ".venv/bin/python -m ruff check ."
parallel = true
```

Key things to check:

- **`base_ref`** -- must match your default branch (e.g., `origin/main` or
  `origin/master`).
- **`verify.gates`** -- add or adjust commands so they match what you actually
  run. Gates with `parallel = true` run concurrently; set `parallel = false`
  for gates that need exclusive access (e.g., e2e tests).
- **`bootstrap.install_command`** -- keep dependencies isolated inside each
  generated worktree. For Python, make sure the verify commands use that same
  environment rather than an ambient `pytest` or `ruff` executable.
- **`agents.default`** -- the agent used when `--agent` is not passed to
  `spec implement`.

For multi-machine dispatch with the SQLite coordinator, keep secrets out of
committed `.spec.toml` and follow
[SQLite Coordinator with Tailscale or SSH](coordinator-tailscale.md), including
the `spec coord init --server`, `spec coord init --worker`, and
`spec coord doctor` preflight flow.

### AGENTS.md

Edit `AGENTS.md` to add project-specific context that agents need during
implementation. The template includes the spec CLI reference and the
implement-agent contract. Add sections for:

- Repository structure and key modules
- Testing conventions (how to run tests, where test files live)
- Code style and linting rules
- Common pitfalls specific to your codebase

This file is read by the agent at the start of every implementation session.

## 4. First spec

Create a spec interactively:

```bash
spec create --spec my-first-feature
```

This launches the configured agent in an authoring worktree to help you write
the spec. The resulting file is committed locally at
`specs/my-first-feature.md`. Ordinary forge credentials are omitted from the
agent process and Git publication is guarded. Explicitly trusted user MCP
servers retain their own service authority and provider approval behavior, so
review those integrations. Publish from your operator shell:

```bash
cd .worktrees/spec-my-first-feature
git status --short
git log -1 --stat
git push --set-upstream origin spec/my-first-feature
gh pr create --head spec/my-first-feature --base main
```

Review and merge that pull request, then update the orchestration checkout
before running `spec implement`:

```bash
cd ../..
git pull --ff-only
spec implement --spec my-first-feature
```

A spec file uses YAML frontmatter followed by markdown:

```markdown
---
id: my-first-feature
area: backend
priority: 50
depends_on: []
description: Short one-line description of the feature
---

# My First Feature

## Goal

Describe what this spec achieves and why it matters.

## Acceptance Criteria

1. First concrete, testable requirement
2. Second requirement
3. Third requirement

## Out of Scope

- Things explicitly not covered by this spec

## Design Notes

Implementation hints, architectural constraints, or references to code paths.

## Agent Notes

Tips for the implementing agent -- common pitfalls, files to read first.
```

You can also create spec files manually. Place them in `specs/` with the
filename matching the `id` field (e.g., `specs/my-first-feature.md`).

## 5. Implement

Start the implementation workflow:

```bash
spec implement --spec my-first-feature
```

Optional flags:

```bash
spec implement --spec my-first-feature --agent codex     # use a specific agent
spec implement --spec my-first-feature --retry-cap 5     # limit retries
spec implement --spec my-first-feature --run <run-id>    # resume a prior run
```

### Lifecycle phases

The orchestrator drives the spec through 9 phases:

| # | Phase | What happens |
|---|-------|-------------|
| 1 | **bootstrap** | Creates an isolated workspace and branch from `base_ref` |
| 2 | **scoping** | Conversational task scoping (task mode only; skipped for specs) |
| 3 | **intake** | Captures human decisions for interactive specs |
| 4 | **implement** | Launches the agent in the selected execution workspace |
| 5 | **verify** | Runs all configured verify gates (test, lint, etc.) |
| 6 | **publish** | Pushes the branch and creates or updates a PR via `gh` |
| 7 | **review** | Runs a local review using the configured agent (no CI setup needed) |
| 8 | **merge** | Merges the PR when review passes |
| 9 | **cleanup** | Removes the execution workspace and local branch |

### Workspace isolation

All implementation happens in an isolated workspace. The default backend uses
a git worktree under `.worktrees/`; clone and container backends instead use
`.spec-workspaces/`. The main checkout is reserved for orchestrator commands.
Implementation branch naming follows `code/<spec-id>--<run-token>` regardless
of backend.

### Retry loop

When a phase fails, the orchestrator loops back to **implement** with failure
context:

- **Verify failure** -- gate output is passed to the agent so it can fix the
  failing tests or lint errors.
- **Review change request** -- unresolved review findings are passed to the
  agent.
- **Merge conflict** -- the agent is asked to resolve conflicts only.

Retries continue until the spec passes all phases or the retry cap is reached
(default: 20). If the cap is exceeded, a **draft PR** is created so you can
inspect the state manually.

The `no_progress_retry_threshold` (default: 2) stops retries early if the agent
makes no new commits on consecutive attempts.

## Execution backends

The default `worktree` backend is the shortest path for most repositories. Use
`clone` when a separate checkout is important, or `container` when the
toolchain and agent should run in a worker image. Follow [Execution
backends](execution-backends.md) before switching from the default; the
container guide includes image setup, workspace modes, diagnostics, and a
smoke test.

Native Windows support currently covers only the worktree backend. Docker
Desktop container execution and UNC/network workspaces are not claimed. See
[Native Windows support](windows.md) for the exact tier and troubleshooting.

## Browser dashboard

Install the `web` extra to monitor runs and chat with Claude or Codex from a
browser. The server binds to loopback by default and requires its generated
token. See [Web dashboard and chat](web.md) for startup, authentication, remote
access, and lifecycle commands.

On native Windows, web chat supports Codex only. Claude fails closed there;
run the web server and Claude CLI inside WSL2, or use a supported Linux/macOS
host, when Claude is required.

## 6. Monitor

Check the status of a specific spec run:

```bash
spec status --spec my-first-feature
```

This shows the current phase, gate results, retry count, and any errors.

List all specs with their status:

```bash
spec list
```

Output includes the spec ID, area, dependencies, status, and description.
To include merged and obsolete specs:

```bash
spec list --all
```

View the contents of a spec:

```bash
spec show --spec my-first-feature
```

## 7. Quick tasks

For small changes that do not warrant a full spec (one-off fixes, minor
refactors), use the `task` command:

```bash
spec task
```

This starts a conversational scoping session where you describe the change.
The orchestrator then creates a lightweight task spec under `specs/tasks/`,
provisions a worktree, and runs the same lifecycle phases (implement, verify,
publish, review, merge, cleanup).

You can specify the agent:

```bash
spec task --agent codex
```

## 8. Cleanup

After a spec is merged, remove its execution workspaces and local branches. For an
in-progress or failed run, first stop any live process and inspect the status
and workspace so you do not discard unpublished changes:

```bash
spec stop --spec my-first-feature      # omit when no run is active
spec status --spec my-first-feature
spec clean --spec my-first-feature
spec container gc                      # container backend: inspect stale resources
```

This removes:

- Execution workspaces owned by the run (under `.worktrees/` for the default
  worktree backend or `.spec-workspaces/` for clone/container backends)
- All local branches associated with the spec (`code/<id>--*`,
  `spec/<id>`, etc.)

Cleanup is also run automatically as the final phase of a successful
implementation lifecycle. `spec clean` refuses to remove a live run, but it is
otherwise destructive: commit or copy any work you need before running it. If
container GC reports crash leftovers, review its ownership labels before using
`spec container gc --apply`.

## Quick reference

```bash
spec init                                  # bootstrap repo
spec doctor                                # validate local readiness
spec create --spec <id>                    # author a spec
spec implement --spec <id>                 # run full lifecycle
spec stop --spec <id>                      # stop a live run, preserving work
spec status --spec <id>                    # check run state
spec list                                  # list all specs
spec show --spec <id>                      # display spec content
spec task                                  # quick task (no spec file needed)
spec clean --spec <id>                     # remove execution workspaces and branches
spec analytics                             # summarize run history
```
