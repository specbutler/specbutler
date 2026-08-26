# Autopilot Container Dogfood Checklist

Use this checklist before setting `[autopilot].container_default_enabled = true`
in a repo that does not explicitly configure `[execution].backend`.

## Setup

- If the repo does not already configure a worker image or Dockerfile, create
  the repo-local baseline:
  `spec container init`
  By default this generated Dockerfile installs `spec` plus the agent CLIs
  listed in the repo's `[agents].allowed`; use `spec container init --no-agents`
  when a custom image or later Dockerfile edit will provide the agent CLIs.
  The generated `spec` install uses the auto-detected source repository,
  normalized to credential-free HTTPS. It must be anonymously cloneable and
  contain the tag matching the running CLI. Forks or repository migrations can
  override it with `--source-repository https://HOST/OWNER/REPO.git`; add
  `--force` to refresh an existing generated Dockerfile, then reapply local
  Dockerfile customizations.
- Temporarily set `[execution].backend = "container"`. Container smoke refuses
  to run against an unselected backend, and an explicit selection keeps this
  bounded test independent from the unattended rollout default.
- Run host diagnostics:
  `spec container doctor`
- Keep `[bootstrap].install_command` as the full-workspace setup command. Use
  `[bootstrap.cache]` only as an optional image-layer optimization for commands
  that can run from copied dependency inputs such as `Makefile`,
  `requirements.txt`, or nested package manifests. If that cache command needs
  a private Git dependency, `[execution.container].build_ssh` mounts the SSH
  agent into that build step only; it does not apply to the normal
  `[bootstrap].install_command` run.
- Smoke test the configured backend before dispatching real work:
  `spec container smoke`
- Run a bounded dispatcher dry run:
  `spec auto run --dry-run --concurrency 2`
- Run the dogfood with explicit bounded concurrency:
  `spec auto run --concurrency 2`

## Capture

Record the following for each dogfood run:

- Backend and safety mode reported by autopilot startup output.
- Effective concurrency cap and whether it was computed or operator-set.
- Cold setup time from process launch to builder-ready.
- Pre-implement snapshot restore time to builder-ready.
- Verify duration.
- Cleanup result for worker containers, sidecars, networks, volumes, clones,
  and worktrees.
- Forge readiness state, including exact head SHA used for draft promotion.
- Final outcome: merged, intentionally stopped dry run, blocked, or failed.
- Failure class when applicable: backend, setup, verify, review, forge, or
  cleanup.

## Budgets

Do not enable the autopilot container default unless all applicable budgets pass
or the budget is explicitly renegotiated in the rollout notes:

- Linux cold start with cached image reaches builder-ready in 15 seconds or
  less.
- macOS volume-mode cold start with cached image reaches builder-ready in 30
  seconds or less.
- Linux pre-implement snapshot restore reaches builder-ready in 5 seconds or
  less.
- macOS pre-implement snapshot restore reaches builder-ready in 10 seconds or
  less.
- Post-snapshot retry cycle is within 1.5x the worktree backend retry cycle on
  this repo's test suite.
- Cold full image build finishes in 5 minutes or less on the documented
  baseline machine.

## Rollout

After a successful dogfood run, remove the temporary explicit
`[execution].backend` selection and enable the unattended default with:

```toml
[autopilot]
container_default_enabled = true
```

Repos can opt out explicitly:

```toml
[execution]
backend = "worktree" # or "clone"
```
