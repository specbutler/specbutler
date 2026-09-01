# Spec Butler

**Give it a spec. Get back a merge.**

Spec Butler is a local spec-to-merge development system built on a simple
premise: humans should define requirements, not edit code.

In this workflow, specs are the only input. Humans collaborate with agents to
author Markdown specs, and `spec` takes it from there: it launches isolated
implementation runs, has agents execute specs autonomously, runs verification
and review, opens pull requests, merges passing work, and cleans up afterward.
Multiple specs can move independently and in parallel, including unattended
while you sleep.

This is still a work in progress. It has been used on production projects and
to develop itself. The goal is to make the system steadily more robust,
autonomous, and trustworthy over time.

## Install

```bash
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install "specbutler @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"
```

Requires Linux or macOS, Python 3.11+, [pipx](https://pipx.pypa.io/), at least
one AI agent CLI (`claude` or `codex`) on PATH, and an authenticated `gh`
(GitHub CLI). Native Windows is not supported; WSL provides a Linux environment
but is not currently covered by this project's CI matrix. The command resolves
the latest tagged GitHub Release; `spec update` advances a tagged install to
newer non-prerelease GitHub Releases.

Install optional interfaces when you need them:

```bash
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install --force \
  "specbutler[web,tui] @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"
```

Development installs may explicitly track `@main`; they are not the stable
channel. See [Releasing and install channels](RELEASING.md).

## Getting started

```bash
cd your-repo
spec init
spec doctor
```

`spec init` bootstraps your repo for spec-driven development. It:
- Auto-detects your default branch, available agents, and verify gates
  (from `pyproject.toml`, `Makefile`, or `package.json`)
- Writes `.spec.toml` with detected settings
- Creates `specs/` and `specs/tasks/` directories
- Adds `.spec-state/` and `.worktrees/` to `.gitignore`
- Copies starter `AGENTS.md`, additive notes for detected agents, and the
  review prompt template. When a destination already exists, `spec init`
  offers to merge the bundled guidance into it with the detected default
  agent.

Pass `--yolo` to use agent-assisted detection instead of heuristics:

```bash
spec init --yolo
```

This invokes the configured agent (Claude or Codex) to analyze your repo and
determine install commands, verify gates, and setup/teardown hooks — useful
for non-standard build systems, monorepos, or projects where the heuristic
detection falls short. Only applies on first init (ignored if `.spec.toml`
already exists).

### Planned work: `spec create` → `spec implement`

`spec create` launches an interactive agent session that produces one or more
committed spec files under `specs/`. Multiple specs can be authored in a single
session.

When ready, run the full lifecycle — implement, verify, review, merge:

```bash
spec implement --spec my-feature
```

Best for complex, multi-step changes where upfront planning pays off.

### Ad-hoc work: `spec task`

`spec task` combines scoping and execution in one command: it gathers
requirements interactively, writes a task spec, and immediately runs the
lifecycle.

```bash
spec task
```

Best for smaller, self-contained changes where a separate planning phase
would be overhead.

For the full walkthrough, see [docs/getting-started.md](docs/getting-started.md).

## Commands

```bash
# Core workflow
spec init                              # bootstrap repo for spec development
spec create --spec ID                  # author a new spec
spec implement --spec ID               # start/resume implementation
spec status --spec ID                  # show run state
spec list                              # list specs with status
spec show --spec ID                    # display spec content
spec report --status ok                # report completion (from inside implement)
spec stop --spec ID                    # stop an active run; preserve its workspace
spec clean --spec ID                   # remove execution workspaces/branches
spec task                              # quick task without a full spec

# Advanced
spec phase --spec ID --phase NAME      # run a single phase
spec input --spec ID [--agent A]       # resolve ambiguity for a waiting-for-input run
spec analytics                         # summarize history
spec doctor                            # read-only onboarding preflight
spec coord status                      # show coordinator config/connectivity
spec coord serve --db PATH             # run SQLite coordinator service
```

### Running multiple specs

Runs for different specs are independent. You can start several at once in
separate terminals; autopilot is not required for parallel execution:

```bash
# Run each command in a separate terminal.
spec implement --spec api
spec implement --spec frontend
```

### Autopilot: automatic dispatch

`spec auto run` watches the dispatchable specs in `specs/`, determines which
ones are ready because all of their dependencies are satisfied, and starts
ready work until it reaches its concurrency limit. As specs merge and unblock
their dependents, the dispatcher picks up the newly ready specs automatically.

```bash
spec auto run                          # dispatch ready specs as capacity permits
spec auto run --concurrency 4          # cap the number of concurrent runs
spec auto stop                         # gracefully stop the dispatcher
```

Without an explicit limit, safe concurrency is computed from the backend, CPU,
and available memory: worktree mode can use up to 8 workers, while clone and
container modes use lower caps.

### Monitoring and maintenance

These commands work for manually started and automatically dispatched runs:

```bash
spec watch                             # interactive TUI dashboard
spec web start --open                  # local browser dashboard and chat
spec gc                                # preview stale-state reconciliation
spec gc --apply                        # apply the proposed cleanup
```

The interactive TUI requires the `tui` extra. The browser dashboard requires
the `web` extra; see [Web dashboard and chat](docs/web.md).

### Container backend

Container execution is an optional isolation backend, independent of how runs
are dispatched or monitored:

```bash
spec container init                    # generate a baseline worker image
# Then enable backend = "container" in .spec.toml.
spec container doctor                  # validate container backend readiness
spec container smoke --verify-gates    # exercise the configured worker
```

### Coordinator Service

Spec's normal locks coordinate runs that share one checkout, but those locks
are invisible to another checkout or machine. The optional coordinator closes
that gap: it arbitrates per-spec leases so two workers do not implement the
same spec at the same time, and records ownership and heartbeats so operators
can see where work is running.

`spec coord serve` runs that small authenticated HTTP service, backed by a
SQLite database owned by the coordinator process. It is not a scheduler or a
remote execution service; workers still run `spec implement` or
`spec auto run` normally with their own checkout, agents, and credentials.

```bash
spec coord token create --db ~/.local/state/spec/coord.sqlite --name worker-main --scope worker
spec coord token create --db ~/.local/state/spec/coord.sqlite --name operator-main --scope operator
spec coord serve --host 127.0.0.1 --port 8765 --db ~/.local/state/spec/coord.sqlite
```

Token commands print the bearer token once and store only its hash in SQLite.
Run `token create` again with the same name to rotate it, or revoke it:

```bash
spec coord token revoke --db ~/.local/state/spec/coord.sqlite --name worker-main
```

Worker tokens can acquire, heartbeat, and release leases. Operator tokens can
also inspect leases, machines, and event history. Keep client tokens in local
environment variables such as `SPEC_COORDINATOR_TOKEN` or an uncommitted
`.spec.local.toml`; do not place secrets in committed repo files.

For a small-team Tailscale or SSH-tunnel setup, including worker config,
machine IDs, token rotation, lease TTL behavior, and operational limits, see
[docs/coordinator-tailscale.md](docs/coordinator-tailscale.md).

### Merge tag tools

```bash
spec-backfill-merge-tags audit         # check for missing merge tags
spec-backfill-merge-tags backfill      # create missing tags
spec-backfill-merge-tags repair        # fix broken tags
```

## Configuration

`spec init` creates `.spec.toml` automatically. Edit it to customize:

```toml
base_ref = "origin/main"

[paths]
specs_dir = "specs"
task_specs_dir = "specs/tasks"
state_dir = ".spec-state"
worktrees_dir = ".worktrees"

[retry]
cap = 20
no_progress_retry_threshold = 2

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

See [`examples/spec.toml`](examples/spec.toml) for a commented starter
configuration. Optional subsystems are documented in the guides linked below;
unrecognized keys are currently ignored for compatibility, so review spelling
carefully.

Repository-defined implement setup/teardown hooks and their structured handoff
format are documented in [Implement setup manifests](docs/setup-manifest.md).

## Architecture

`spec` is organized around a single orchestrator runtime with thin adapter
boundaries around the coding agent and the forge.

- **Public CLI** (`src/spec_runtime/cli.py`) exposes intent-level commands such
  as `spec create`, `spec implement`, `spec status`, and `spec auto run`.
- **Orchestrator runtime** (`src/spec_runtime/orchestrator.py`) owns the
  lifecycle state machine, retry policy, execution workspace management, sandbox config,
  and handoff between phases. Each run is represented by a persisted
  `RunState`.
- **Spec contracts** are plain Markdown files with YAML frontmatter.
  Dispatchable specs live in `specs/*.md`; `spec_metadata.py` discovers those
  for `spec list`, `spec watch`, and `spec auto run`. `specs/tasks/*.md` is
  reserved for the `spec task` flow rather than normal top-level dispatch.
- **Agent adapter boundary** (`src/spec_runtime/agent_adapter.py`) translates
  generic implement/author/review operations into concrete Claude or Codex CLI
  commands. This is where agent-specific launch behavior and sandbox settings
  live.
- **Forge adapter boundary** (`src/spec_runtime/forge.py`) isolates push, pull
  request, check, and merge behavior. The built-in `GitHubForge` implementation
  uses the `gh` CLI.
- **Status and provenance helpers** (`spec_status.py`, `spec_identity.py`,
  `spec_merge_tags.py`) answer "what state is this spec in?" from run records,
  branch/worktree naming conventions, remote refs, and annotated merge tags.
- **Review pipeline** (`review_feedback.py`, `review_gate.py`,
  `review_gate_sticky_comment.py`) turns local agent review output into
  structured findings and a review decision that the orchestrator can act on.
- **Autopilot** (`src/spec_runtime/autopilot.py`, `autopilot_tui/`) dispatches
  multiple specs in parallel on top of the same run records and status helpers.

### Persistent State

- `.spec.toml` configures paths, verify gates, agents, retry limits, and
  optional setup/teardown hooks.
- `specs/` holds normal dispatchable specs. `specs/tasks/` holds task-flow
  specs created by `spec task`.
- `.worktrees/` contains implementation and task worktrees; branch and worktree
  names encode the spec id and run token.
- `.spec-state/runs/*.json` stores per-run `RunState`. `.spec-state/orchestrator/`
  stores per-phase audit entries, and review artifacts are written under
  `.spec-state/runs/<run-id>/`.

## How it works

The orchestrator runs each spec through 9 phases:

1. **bootstrap** — create an isolated workspace and branch from the base ref
2. **scoping** — conversational task scoping (task mode only)
3. **intake** — capture human decisions for interactive specs
4. **implement** — agent codes the implementation
5. **verify** — run configured test/lint gates
6. **publish** — push branch and create/update PR
7. **review** — local code review using the configured agent
8. **merge** — merge PR when CI checks pass
9. **cleanup** — remove execution workspace artifacts

On verify failures, review change requests, or merge conflicts, the
orchestrator loops back to implement with failure context until passing or
the retry cap is reached.

All implementation happens in an isolated workspace selected by the configured
backend: a git worktree, a separate clone, or a container-backed workspace. The
review phase runs locally using the configured agent — no CI review workflow
setup required.

## GitHub Enforcement

For non-spec PRs, a repository can enforce the same three-way merge gate used
by the orchestrator:

- `ci` aggregates lint, test, package, and security jobs into one required
  status check.
- `review-decision-gate` runs blocking cloud Codex review unless the PR body
  declares `Review-Owner: local`.
- `spec-pr-policy` validates spec/task PR structure while passing
  deterministically for ad-hoc branches.

The workflow files live under [`.github/workflows/`](.github/workflows/).
To apply the matching GitHub ruleset and workflow permissions after merging
these files, run:

```bash
python3 scripts/setup_github_integration.py
```

Use `--dry-run` first if you want to inspect the exact API mutations.

### Sandboxing

`spec` configures a fixed launch policy for the agent sessions it starts. The
`[execution].safety_mode` setting is currently a recorded compatibility label,
not an enforcement switch. Choose the
`worktree`, `clone`, or `container` execution backend based on the isolation
your project needs; see [Execution backends](docs/execution-backends.md).

- With the default worktree backend, implementation and authoring run in a
  per-worktree sandbox. The agent can modify its workspace and the shared
  `.spec-state/` directory that `spec` uses for run state.
- Codex implementation and authoring sessions run with workspace-write access
  and network access enabled. Codex local review runs are stricter and use a
  read-only sandbox.
- Before a local review, `spec` may install the pull-request checkout so the
  reviewer can run tests. Package build hooks are untrusted, so this review
  bootstrap runs through Codex's model-free sandbox command with no network,
  an allowlisted environment, operator-home reads denied, and writes limited
  to the disposable review worktree. If that sandbox or permission profile is
  unavailable, `spec` skips installation, records a review-environment warning,
  and continues with a diff-only review; it never falls back to a direct host
  install. This boundary requires a current Codex CLI even when Claude is the
  selected reviewer.
- Claude sessions run with the Claude sandbox enabled, network access limited
  to a built-in allowlist, and a small denylist for dangerous git commands such
  as force-push and `git reset --hard`.
- Additional behavior may still come from the user's own Codex or Claude
  configuration, depending on how the underlying CLI merges local config with
  the settings that `spec` provides.

### Security and trust model

`spec` is an automation tool for repositories you trust, not a general sandbox
for hostile code. Specs, agent output, and repository-defined implementation
bootstrap, setup, teardown, and verify commands can execute code. The isolated
local-review bootstrap described above is a narrow exception; it does not make
the rest of a run safe for an untrusted repository. Review changes from
untrusted contributors before launching a run against them, and do not expose
the web operator interface as a public service.

Forge credentials and merge operations stay in the host orchestrator; they are
not intentionally copied into worker images or non-interactive agent MCP
configurations. That separation does not make repository commands safe. Use the
container backend for a stronger process/toolchain boundary and a disposable
machine or VM when evaluating genuinely untrusted repositories. See the
[Security Policy](SECURITY.md#trust-model) and [Execution
backends](docs/execution-backends.md).

### Container Playwright MCP

When `[execution].backend = "container"`, Playwright MCP defaults to the
`in-worker` topology. The MCP server is launched by the agent inside the worker
container, so apps served by repo-local commands remain reachable through
`localhost` and the worker does not need `/var/run/docker.sock`.

Repos that need a host-managed Playwright MCP sidecar must opt in explicitly:

```toml
[execution.container.playwright_mcp]
topology = "sidecar"
app_url = "http://localhost:5173"
# or, when localhost is not valid from the sidecar:
sidecar_endpoint = "http://app:5173"
```

The container backend records the MCP command, topology, mapped app URL,
sanitized env metadata, expected artifacts, and browser/runtime version
diagnostics under the run `logs/` directory. On macOS, Docker-compatible
engines run Linux browsers inside a VM; on Linux, the worker image must include
the distro libraries required by the repo's configured Playwright version.
Use headless mode in both cases.

### Adapter model

- **Forge adapter** (`ForgeAdapter` protocol) — isolates GitHub operations.
  `GitHubForge` uses `gh` CLI. Implement the protocol for other forges.
- **Agent adapter** (`AgentAdapter` protocol) — isolates agent launch behavior.
  Built-in: `ClaudeAgent`, `CodexAgent`. Register custom agents with
  `register_agent_adapter()`.

## Updating

```bash
spec update
```

Detects your installation method and upgrades to the latest applicable version
tag.
If bundled templates have changed, you will be prompted to run
`spec init --force` to refresh them.

For an explicit `@main` development install, rerun the development-channel
`pipx install --force` command from [INSTALL.md](INSTALL.md). Editable installs
are updated with normal Git and package-development commands.

## Command name

The CLI command is `spec`. The distribution name is `specbutler` to
avoid collision with the existing PyPI `spec` package. Stable releases are
distributed through tagged GitHub source rather than PyPI.

## Contributing

This project accepts GitHub issues rather than unsolicited code pull requests;
accepted work is implemented through the orchestrator. To report a bug or
propose a change, start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
