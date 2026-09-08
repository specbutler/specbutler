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

On native Windows, the supported tier is limited to Windows 11, a local fixed
NTFS repository, Codex, PowerShell, and the `worktree` backend. The clone and
Docker Desktop container combinations are not release-qualified on Windows;
see the [Windows support matrix](windows.md). WSL2 is a Linux-mode alternative
when the repository and orchestrator live inside the WSL Linux filesystem.

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

Agent-side Git writes do not go directly to the linked worktree's shared
administrative directory. Spec Butler creates a disposable Git directory for
each provider launch, backed by a read-only object alternate. Once the provider
ownership boundary is confirmed stopped, the host validates and imports only a
fast-forward commit chain for that worktree's exact branch. This keeps sibling
refs, the shared object database, hooks, config, and real worktree index outside
the agent's write boundary while preserving normal `git add` and `git commit`
inside the session.

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

This section describes supported Linux/macOS hosts. A working Docker Desktop
installation on Windows does not extend the native Windows support claim to the
container backend.

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

Container resource names include a digest of the canonical workspace path, so
two checkouts cannot collide when they happen to reuse a run ID. Treat those
names as implementation details: automation should discover resources from the
run state and `spec.*` labels, not reproduce the naming formula. A retry may
retain a development-era resource name only when the saved run has every
current safety marker and the engine proves exact ownership.

Keep the same container engine and daemon endpoint/context for the lifetime of
a run, including cleanup. Spec Butler rejects a changed engine command before
contacting either engine, but a changed `DOCKER_HOST`, `DOCKER_CONTEXT`, Podman
connection, or replaced daemon may be indistinguishable when the command name
is unchanged. Restore the original selection before `spec implement`, `spec
clean`, or `spec container gc`; otherwise an old writer can remain live on the
now-hidden daemon.

Container runs created before Spec Butler 0.5 do not meet that resume contract:
volume-mode runs lack the crash-safe seed marker, while sidecar runs lack the
operator-controlled protected Compose baseline. Version 0.5 therefore refuses
to auto-resume either kind rather than guessing whether workspace data is
complete or consulting an agent-editable Compose file.

Finish valuable container runs before upgrading when practical, and do not run
the 0.4 container backend against a shared daemon. If already upgraded,
preserve the run directory and engine resources and manually inspect or export
anything valuable. To permanently discard an inspected pre-0.5 run, use 0.5's
`spec clean --spec ID`; its cleanup path verifies exact ownership. Do not use
0.4 to recover a volume-mode run: that version can reseed from the host and
overwrite newer volume-only work. Never delete a volume based on its name alone.

Before enabling container mode for unattended autopilot runs, use the
[container dogfood checklist](autopilot-container-dogfood.md) to capture startup,
retry, cleanup, and capacity evidence.

Codex OAuth credentials must be copied into an isolated provider home for
container launches. Because the provider may rotate its refresh token, Spec
Butler permits only one copy-backed OAuth session per canonical Codex auth file
at a time; another launch fails promptly with an actionable busy error instead of
waiting behind a potentially hours-long agent turn. Use `OPENAI_API_KEY` /
`CODEX_API_KEY` authentication when concurrent Codex container runs are
required, or run OAuth-backed specs sequentially.

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

Managed Compose services must let Compose scope their names to the run. Do not
set `container_name`, or `name` on a non-external volume or network. If a
non-default network or volume is intentionally shared and independently
administered, declare it with the literal `external: true`; Spec Butler will
neither label nor remove that resource. The default network must remain managed
so the worker can join the run-scoped service network. Environment-interpolated
`external` values are intentionally treated as managed and fail closed if
Compose later resolves them as external.
Compose `include` and service `extends` are not supported because inherited
resources and global names cannot yet be validated consistently across Docker
and Podman providers; inline those services in the configured Compose file.
When cleaning up a run created by an older Spec Butler release, cleanup also
refuses an unexpected unlabeled resource in the same Compose project so the run
metadata remains available for manual recovery.

## Operations and recovery

Inspect the backend and recorded safety label with `spec status` or `spec
watch`. Stop active work and verify its status before cleanup:

```bash
spec stop --spec <id>             # only when a run is active
spec status --spec <id>
spec clean --spec <id>
spec container gc                 # dry run
spec container gc --apply         # revalidate and remove this checkout's resources
```

`spec clean` refuses to remove a live run. It is destructive for unpublished
worktrees and local branches, so inspect or commit anything you need first. For
container runs, use `spec container gc` after cleanup to discover engine
resources left by a crash. The engine inventory is host-global, but GC acts
only on resources whose structured labels prove the exact current-checkout
layout `<configured workspace root>/<run-id>/source`. It rechecks ownership,
run liveness, and per-spec locks immediately before `--apply`. Resources from
another checkout, running workers without a positively terminal local run, and
ambiguous name-only or unlabeled legacy resources are left untouched. Inspect
and remove those manually with the container engine only after establishing
their owner. Do not remove `.spec-workspaces` or container volumes broadly. If
a run fails, preserve its record and logs until the failure has been diagnosed.

Clone and container workspaces also fail closed during automatic cleanup when a
submodule has been checked out. The nested Git configuration is agent-controlled,
so Spec Butler cannot safely prove that the child worktree contains no
unpublished edits. It also rejects publication when the superproject changes a
submodule pointer, because the child commit may exist only in the disposable
workspace. Inspect, commit, and publish submodule work from an operator shell;
after confirming nothing must be preserved, `spec clean` is the explicit
discard action. Empty, uninitialized submodule paths do not block cleanup.
