# Execution backends

`spec` can run implementation work in a linked worktree, a full local clone,
or a container. The orchestrator remains on the host in all three modes: it
owns retry policy, run state, forge credentials, publishing, review, merge, and
cleanup.

## Choose a backend

| Backend | Use it when | Isolation and cost |
|---------|-------------|--------------------|
| `worktree` | You want the simplest local setup | Fastest; shares the main repository's Git object database and host toolchain |
| `clone` | You want a disposable full checkout without Docker | Separate `.git` directory and workspace; still uses the host toolchain |
| `container` | You want the agent and project toolchain inside a worker image | Strongest process/toolchain boundary; requires a Docker-compatible engine and image setup |

The default is `worktree`. Existing projects do not need an `[execution]`
section unless they want a different backend or an explicit opt-out from
future rollout policy.

```toml
[execution]
backend = "worktree"             # worktree, clone, or container
safety_mode = "safe"             # compatibility label; see warning below
workspace_root = ".spec-workspaces"
```

> **Important:** `safety_mode` is currently recorded and displayed as policy
> metadata; it does not change runtime enforcement. The accepted `safe`,
> `full-auto`, and `trusted` values are reserved for compatibility and must not
> be treated as security boundaries. Actual isolation comes from the selected
> backend, the fixed provider launch policy, credential handling, and the host's
> own controls. `container` provides the strongest process/toolchain boundary.

## Worktree mode

Worktree mode uses `.worktrees/code-<spec-id>--<token>/` and the corresponding
`code/<spec-id>--<token>` branch. The main worktree is for orchestration only.
Do not edit it while an implementation run is active.

Before the first run, confirm that a clean worktree can execute the commands in
`[bootstrap]` and `[verify]`:

```bash
git status --short
spec doctor
spec implement --spec <id>
```

Use the gitignored `.spec.local.toml` for supported machine-local
`[execution]` and `[coordination]` overrides and secrets. Other configuration
belongs in `.spec.toml`.

## Clone mode

Clone mode creates a disposable checkout at
`.spec-workspaces/<run-id>/source/`, with durable logs and outbox data beside
it. Enable it with:

```toml
[execution]
backend = "clone"
safety_mode = "safe"
workspace_root = ".spec-workspaces"
```

The repository must have a usable configured base ref. `spec` ignores the
workspace root before creating artifacts and refuses to use a tracked source
path. Clone mode is a good diagnostic step when worktree-specific Git behavior
is suspected but a container is unnecessary.

## Container mode

Container mode supports Docker and Docker-compatible engines. Start with the
generated repo-local baseline, then adapt it to the project's actual build:

```bash
spec container init
```

`spec container init` creates `.spec/worker.Dockerfile` and a commented
configuration snippet. Review and commit the generated Dockerfile and config.
The generated worker installs the tagged `spec` release from the repository
that supplied the running CLI, normalized to credential-free HTTPS. Forks are
detected from VCS install metadata or the source checkout's `origin`. The
repository must be anonymously cloneable over HTTPS and contain the
`vX.Y.Z` tag matching the running CLI. Embedded credentials are discarded; they
cannot make a private source repository usable during the build.

During a repository migration, select the destination explicitly. When a
generated Dockerfile already exists, `--force` is required and overwrites it,
so reapply any local Dockerfile customizations afterward:

```bash
spec container init --source-repository https://HOST/OWNER/REPO.git
spec container init --force --source-repository https://HOST/OWNER/REPO.git
```
The backend builds from a generated context containing tracked repository files
instead of relying on a project `.dockerignore`. The worker must contain
Python, Git, the selected agent CLI, the `spec` CLI, and every native dependency
required by bootstrap and verification. The generated image installs the
release tag matching the host `spec` version and pins agent npm packages to the
installed host CLI versions when they can be detected. `spec container smoke`
rejects host/worker `spec` version drift and, for VCS/editable host installs,
exact source-commit drift. Never bake agent tokens, SSH keys, `gh` credentials,
or a Docker socket into the image.

Uncomment or add the backend configuration before running container-specific
diagnostics; `spec container smoke` intentionally refuses when the container
backend is not selected:

```toml
[execution]
backend = "container"
safety_mode = "safe"

[execution.container]
engine = "docker"
dockerfile = ".spec/worker.Dockerfile"
workspace_mode = "auto"
```

Then validate the host and disposable worker:

```bash
spec doctor
spec container doctor
spec container smoke --verify-gates
```

If the smoke test fails, set `backend = "worktree"` again while you adjust the
Dockerfile. Use `spec container gc` to inspect any crash leftovers.

The generated worker is release-oriented: it installs the tag matching the
host package version. A moving `@main` or editable host install can have the
same version but a different commit, which smoke correctly rejects. Use a
tagged host release for the baseline flow or provide a custom worker built from
the exact development source.

If the project publishes a maintained worker image, set `image` instead of
`dockerfile`. When both are configured, `image` wins.

### Workspace modes

- `auto` uses a Docker volume on macOS and a bind-backed workspace on Linux.
- `bind` keeps the source checkout visible on the host and is easy to inspect.
- `volume` avoids slow source bind mounts on macOS; `spec` synchronizes durable
  logs and outbox data back to the host.

Choose an explicit mode only after the `auto` smoke test demonstrates a reason
to override it.

Before enabling container mode for unattended autopilot runs, use the
[container dogfood checklist](autopilot-container-dogfood.md) to capture startup,
retry, cleanup, and capacity evidence.

### Private dependencies

The generated worker installs `spec` from its public HTTPS release URL. For a
private project dependency needed by the optional build-time bootstrap cache,
enable the cache and configure BuildKit SSH-agent forwarding rather than
copying a key into the image:

```toml
[execution.container]
build_ssh = "default"

[bootstrap.cache]
enabled = true
command = "python -m pip install -r requirements.txt"
inputs = ["requirements.txt"]
```

Start an SSH agent, add only the required key, and rerun `spec container
doctor`. `build_ssh` is mounted only into `[bootstrap.cache].command`; it does
not apply to the normal `[bootstrap].install_command` executed later in the
prepared workspace. Treat Docker-group membership and build-time access as
privileged, and never bake credentials into the worker image.

### Services and browsers

Use `[execution.container].compose_file` when verify gates need service
sidecars. Playwright MCP defaults to `in-worker`, which keeps an app served on
worker `localhost` reachable by the browser. Configure the `sidecar` topology
only when the project supplies the necessary network and endpoint mapping.

## Operations and recovery

Inspect the backend and recorded safety label with `spec status` or `spec
watch`. Stop active work and verify its status before cleanup:

```bash
spec stop --spec <id>             # only when a run is active
spec status --spec <id>
spec clean --spec <id>
spec container gc                 # dry run
spec container gc --apply         # remove discovered stale spec resources
```

`spec clean` refuses to remove a live run. It is destructive for unpublished
worktrees and local branches, so inspect or commit anything you need first. For
container runs, use `spec container gc` after cleanup to discover engine
resources left by a crash. Do not remove `.spec-workspaces` or container
volumes broadly. If a run fails, preserve its record and logs until the failure
has been diagnosed.
