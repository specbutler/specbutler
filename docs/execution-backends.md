> **Alpha: container mode has known reliability issues (image builds, in-worker services such as Postgres, credential refresh, nested provider sandboxing). Prefer the clone or worktree backend.**

# Execution backends

`spec` can run implementation work in a linked worktree, a full local clone,
or a container. The orchestrator remains on the host in all three modes: it
owns retry policy, run state, forge credentials, publishing, review, merge, and
cleanup.

## Codex container preflight

Codex container execution requires a worker whose outer isolation permits its
provider sandbox to run. An installed `bwrap`, a successful image build, or
`codex --version` does not establish that capability. Before implementation
setup and model launch, Spec Butler runs a bounded, credential-free
`codex sandbox` probe inside the prepared worker under its effective user. It
checks workspace and outbox writes, denial of a synthetic protected read, and
denial of a write outside the permitted roots. Failure is an environment
blocker (`container_sandbox_unavailable`), not a retryable missing handshake.
The captured diagnostic is retained in the command log and run error.

`spec container doctor` probes an explicitly configured worker image in a
disposable container without mounting the repository or credentials. For a
Dockerfile source, it reports sandbox enforcement as unverified and directs
you to `spec container smoke`, which builds/prepares the actual worker and runs
the same check. Neither command relaxes container privileges or disables the
provider sandbox. The doctor image check does not replace the per-launch check
against the actual workspace mounts.

If the worker reports `bwrap: No permissions to create new namespace`, use the
existing `worktree` backend with its normal provider sandbox via a scoped
`SPEC_CONFIG` override for a new run, or repair and validate namespace support
in the worker environment before resuming. Existing runs retain their backend
identity and cannot switch backends just by changing config. Preserve the
stalled branch and workspace before explicitly retiring/resetting an old run;
do not increase the retry cap to repeat an unchanged environment failure.

### Landlock and alternative sandbox runtimes

[Landlock](https://docs.kernel.org/userspace-api/landlock.html) can restrict
unprivileged processes without creating user namespaces. Its ABI and enabled
rights must be checked at runtime; availability alone does not establish the
required filesystem, network, credential, or process-lifecycle boundary.
Codex's current Linux sandbox still uses Bubblewrap. Installing a Landlock
wrapper does not change the sandbox Codex creates internally.

The [Vetto proposal on #16](https://github.com/specbutler/specbutler/issues/16#issuecomment-5745232187)
is a possible future integration, not a supported replacement. Vetto's
[platform boundary documentation](https://github.com/shleder/vetto/blob/main/docs/platform-backends.md)
also uses user/mount namespaces for secret masking and network/PID namespaces
for its full Linux boundary. Its current namespace-free FS-only path blocks
network access, so it is not equivalent to our network-enabled provider
session. A replacement must demonstrate the exact
Spec Butler write/deny-read contract, provider-credential separation, MCP
behavior, and descendant cleanup under ordinary worker restrictions before it
can replace the existing policy. The preflight does not silently switch runtimes.

## Choose a backend

| Backend | Use it when | Isolation and cost |
|---------|-------------|--------------------|
| `clone` | The default: a disposable full checkout without Docker | Own `.git` directory per run, so agent Git needs no shared metadata; about as fast as a worktree (`git clone --local` hardlinks objects); uses the host toolchain |
| `worktree` | You want runs inside the repository's `.worktrees/` | Shares the main repository's Git directory and host toolchain; the agent must be allowed to write that shared `.git` |
| `container` | You want the agent and project toolchain inside a worker image | Strongest process/toolchain boundary, but **alpha**; requires a Docker-compatible engine and image setup |

The default is `clone` (`worktree` on native Windows, its only qualified
backend). `spec init` writes the backend explicitly. A `.spec.toml` without a
backend picks up the default, so runs started under the old `worktree`
default must be resumed with `backend = "worktree"` set; the orchestrator
refuses to switch an existing run's backend.

## Agent Git

Implementation agents use Git as a person would: `add`, `commit`, `stash`,
`branch`, `rebase`, `log`, and `fetch` from origin. The host supplies a commit
identity through `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (your own `user.name` and
`user.email` when it can read them), sets `HUSKY=0`, and turns off
`branch.autoSetupMerge` for the session. Pushing and pull requests stay with
the host, which publishes the run branch with its own command-scoped config and
hooks disabled.

The provider sandbox grants writes to the workspace's Git directory and keeps
the repository controls read-only:

| Backend | Writable Git paths | Read-only |
|---------|--------------------|-----------|
| `clone` | `<clone>/.git` | `config`, `config.worktree`, `hooks/`, `info/` |
| `worktree` | `.git/worktrees/<name>/` and the shared `.git` common directory | the same four controls in the common directory, the worktree's `config.worktree`, the main checkout's `HEAD` and `index`, and the refs of branches checked out in other worktrees (such as your `main`) |

A symlinked `.git` is refused. Host clones use `git clone --shared`: they
borrow the orchestration checkout's objects through an alternate instead of
hardlinking them, so a write through the clone's `.git` can never reach the
host's object files. (Container workers cannot see host paths and keep a
hardlinked `--local` clone.) Agent sessions also run with `gc.auto=0`; the host
owns garbage collection. Claude receives these as `sandbox.filesystem`
`allowWrite`/`denyWrite` entries in the generated `.claude/settings.local.json`;
Codex receives them as writable roots and `read` entries in its permission
profile (Codex rules only name paths that exist).

Linked worktrees are the harder case because a worktree has no Git directory
of its own worth committing to: objects, refs, the stash, and the reflog live
in the repository's shared common directory next to your own checkout's
metadata. An agent that can commit can therefore write refs and objects that
your checkout also sees. Spec Butler keeps the branches checked out in other
worktrees, `packed-refs`, other runs' `.git/worktrees/<name>/` directories,
and the main checkout's `HEAD` and `index` read-only, but other branches and
the stash are shared. For Codex, whose rules can only name existing files, the
host first writes a packed protected branch back out as an equal loose ref. Because `packed-refs`
is read-only, deleting a branch that Git has packed fails inside the session. Spec Butler used to give each worktree launch a
private `GIT_DIR` and import its commits afterwards, but that import's
metadata guards failed runs whenever the shared repository changed during a
session. Worktree mode now grants the common directory directly. Set
`SPEC_PRIVATE_AGENT_GIT=1` to use the old private `GIT_DIR` import instead.
A clone avoids the question entirely, which is why it is the default.

### Git probe

`spec doctor` runs a model-free Git probe for each allowed provider and both
host backends. It builds a throwaway repository, generates the same sandbox
policy an implementation launch would use, and runs a small script under the
provider's own sandbox runner: `codex sandbox` for Codex, and Anthropic's
`srt` ([sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime),
the library Claude Code's sandbox is built on) for Claude. Claude Code has no
model-free way to run a command in its sandbox, so without `srt` the Claude
probe is reported as unverified. Install it with:

```bash
npm install -g @anthropic-ai/sandbox-runtime
```

Inside the sandbox, `add`, `commit`, `stash`, `branch`, and `log` must
succeed, and writes to `.git/config`, `.git/hooks/` and a path outside the
workspace must fail. A failure for the configured backend is a blocker; the
other backend is a warning.

Implementation launches on the host run the same probe once per process. A
launch is refused when the probe ran and Git itself failed, or the sandbox let
it write Git config, hooks or outside the workspace. A missing runner or a
probe that could not start is logged. Run `spec doctor` from a
normal terminal: a sandboxed agent session cannot start another sandbox
(`sandbox_apply: Operation not permitted`). Set `SPEC_AGENT_GIT_PROBE=0` to
skip the probe.

Host implementation and handshake-recovery sessions receive `TMPDIR`, `TMP`,
and `TEMP` pointing to a private `tmp` directory under their launch-scoped
completion outbox. This is outside the checkout and already covered by the
launch's writable grant. It is removed with that launch's outbox. Repository
setup values cannot redirect these variables. Container agent sessions use the
existing `/workspace/outbox` mount for all three variables, replacing host paths
after environment filtering. This keeps temporary files outside the checkout
even when the nested provider sandbox denies writes to `/tmp`; it does not add
any writable roots or change the sandbox policy.

On native Windows, the supported tier is limited to Windows 11, a local fixed
NTFS repository, Codex, PowerShell, and the `worktree` backend. The clone and
Docker Desktop container combinations are not release-qualified on Windows;
see the [Windows support matrix](windows.md). WSL2 is a Linux-mode alternative
when the repository and orchestrator live inside the WSL Linux filesystem.

```toml
[execution]
backend = "clone"                # clone, worktree, or container
safety_mode = "native"           # native, or isolated with backend = "container"
workspace_root = ".spec-workspaces"

[execution.network]
mode = "open"                    # open, or allowlist
# allowed_domains = [...]        # used in allowlist mode; defaults to the built-in list
denied_domains = []              # always applied

[execution.codex]
auto_review = false              # route Codex escalations to its model reviewer
```

## Safety modes

`safety_mode` decides how unattended agent sessions (implement, and the
automatic recovery launch after a missing completion handshake) are confined.
No unattended launch waits for an approval: nobody is there to give one.

### `native` (default)

The provider's own sandbox is the boundary, without approval prompts.

**Claude** runs `claude -p --permission-mode bypassPermissions` with only the
orchestrator-written settings file (`--setting-sources ""`). Those settings:

- keep the Bash sandbox on with `failIfUnavailable: true` and
  `allowUnsandboxedCommands: false`, so shell commands can write only the
  workspace, its Git directories (with `config`, `hooks/`, `info/` and protected
  refs read-only) and the launch outbox, and a session whose sandbox cannot
  start fails instead of running unconfined;
- deny reads of provider and operator credential stores, both through the
  sandbox and through the file tools;
- deny `git push --force`/`-f` and `git reset --hard`, both as permission
  deny rules and in the PreToolUse hook below, which runs in bypass mode too.
  That Bash check is a best-effort extra layer against mistakes, not a
  security boundary; a script or other shell indirection can get past it. The
  real limits are: the sandbox confines Bash writes to the workspace (so
  `git reset --hard` can only discard the agent's own uncommitted work there),
  protected branch refs are read-only, and a force push has no way out, since
  the agent environment carries no forge credentials (no `GH_TOKEN`; `gh`
  config, `~/.git-credentials`, `~/.ssh`, the SSH agent socket and global Git
  config are read-denied or blanked) and `pushURL` is `specbutler-no-push://`.
  One residual: a session that names an OS credential store explicitly
  (`git -c credential.helper=osxkeychain push`) may reach it if the provider
  sandbox allows that service;
- register a PreToolUse hook for Edit, Write, MultiEdit, NotebookEdit and Bash.
  Those tools run inside the Claude process, not under the OS sandbox, and
  bypass mode skips their protected-path prompts. The hook resolves symlinks
  and blocks (exit code 2, with the reason shown to the model) any target
  outside the workspace, the agent's Git directories and the launch outbox
  (whose `tmp/` is the session's `TMPDIR`), any protected path, and any
  hard-linked file.

Review and the blocked-run debugger keep `--restricted`, read-only tools and
`disableBypassPermissionsMode`. Interactive sessions (`spec create`,
`spec task` scoping, `spec input`) keep prompting the person at the terminal.

Before each launch Spec Butler checks `claude --help` for `bypassPermissions`
and the other flags it passes. If one is missing it falls back to the previous
restricted `acceptEdits` launch (file tools confined to the working
directories, anything that would prompt is denied, network limited to the
allowlist) and `spec doctor` warns.

**Codex** runs `codex exec` with a dedicated `CODEX_HOME`, `-a never`, and a
permission profile built on the workspace profile with network on. A command
that needs to escalate fails back to the model. With
`[execution.codex] auto_review = true` the launch uses `approval_policy =
"on-request"` and `approvals_reviewer = "auto_review"`, so Codex's model-based
reviewer decides escalations instead of a silent denial. `spec doctor` checks
that the installed Codex accepts the setting.

### `isolated`

Requires `backend = "container"` (alpha) and keeps its launch policy: the
container is the boundary, and Claude keeps the restricted `acceptEdits`
launch inside it. Combining `isolated` with another backend is a
configuration error; `isolated` without a backend selects `container`.
`[execution.network]` applies to native mode only.

### Legacy values

`safe`, `full-auto` and `trusted` date from when `safety_mode` was a label.
They now mean `native`, and the CLI prints a deprecation note once per command.

### Sandbox denials

When a failed session hit a sandbox or permission denial (a write outside the
workspace, a blocked host, a write-guard block, a Claude permission denial in
`stream-json`, or Codex's `approval is not supported in exec mode`), the
failure is classified `sandbox_denied`. `spec status` shows the denied path or
host. It is handled like an infrastructure failure: it does not spend a code
retry, it is backed off, and repeats trip autopilot's breaker. Fix the policy
(or the spec) rather than raising the retry cap.

## Network

Unattended sessions can reach the network by default (`mode = "open"`).
`mode = "allowlist"` limits them to `allowed_domains`, which defaults to the
built-in list (GitHub, the Anthropic API, npm, PyPI, Yarn, Azure blob storage,
and localhost). `denied_domains` always applies.

| | Claude `sandbox.network` | Codex permission profile `network` |
|---|---|---|
| `open` | no `allowedDomains`, no `strictAllowlist`; bypass mode reaches unlisted hosts without a prompt | `enabled = true` |
| `open` + `denied_domains` | `deniedDomains` | proxy `domains` with `deny` entries |
| `allowlist` | `allowedDomains` with `strictAllowlist = true` | proxy `domains` with `allow` entries |

Interactive Claude sessions and the acceptEdits fallback keep `allowedDomains`
as their pre-approved hosts in open mode, since they cannot (or must not)
bypass the prompt for other hosts. Codex's proxy has no "everything else"
wildcard and refuses hosts it is not told to allow, so `denied_domains` in open
mode may cost Codex its general network access; `spec doctor` warns. Prefer
allowlist mode for Codex runs that must exclude hosts.

What open network means:

- **Credential exfiltration.** Anything a session can read, it can send
  anywhere. The real defence is that it cannot read credentials: provider and
  operator credential stores (`~/.ssh`, cloud and `gh` configs, provider auth
  files) are read-denied, forge tokens stay with the host orchestrator, and
  agent processes start from a minimal environment. A deny list or allowlist
  only narrows where data could go; GitHub itself is allowed by default.
- **Prompt injection.** Web pages, issues, package READMEs and API responses
  can carry instructions. The sandbox bounds what a misled agent can touch, not
  what it can say or which allowed host it uses.
- **Package install scripts.** `npm install`, `pip install` and similar run
  third-party code inside the session with the same file and network access as
  the agent.
- **Local sockets.** Services on `localhost` (databases, dev servers, a Docker
  API on TCP) are reachable. The Claude sandbox blocks Unix sockets such as the
  Docker socket unless allowed; Codex's profile sets
  `allow_local_binding = true`. Do not run unattended sessions on a machine
  whose local services you would not hand to the agent.

## Worktree mode

Worktree mode uses `.worktrees/code-<spec-id>--<token>/` and the corresponding
`code/<spec-id>--<token>` branch. The main worktree is for orchestration only.
Do not edit it while an implementation run is active.

Select it with `backend = "worktree"`. The agent writes the linked
worktree's Git directory and the repository's shared common directory, with
config and hooks read-only; see [Agent Git](#agent-git).

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

Clone mode, the default, creates a disposable checkout at
`.spec-workspaces/<run-id>/source/` with `git clone --shared`, with durable logs
and outbox data beside it. The host refreshes the clone's refs from the
orchestration checkout before every launch, points `origin` at the forge for
host-owned publishing, and publishes the run branch from the clone. Configure
it explicitly with:

```toml
[execution]
backend = "clone"
safety_mode = "native"
workspace_root = ".spec-workspaces"
```

The repository must have a usable configured base ref. `spec` ignores the
workspace root before creating artifacts and refuses to use a tracked source
path.

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
safety_mode = "isolated"

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

## Explicit nested sandbox profile (Linux Docker)

Docker's default seccomp/AppArmor policies can prevent Codex from creating its
inner Bubblewrap sandbox. Installing Bubblewrap alone does not change those
outer policies. For Linux x86_64/aarch64 Docker coordinators using in-worker
services, an operator can explicitly select the versioned nested worker policy:

```toml
[execution.container]
sandbox_profile = "nested-v1"
```

The default remains `"default"`. The nested policy always uses a non-root numeric
worker uid/gid, drops all outer Linux capabilities, and enables
`no-new-privileges`. It permits the namespace and mount operations needed by the
inner sandbox while retaining the other Docker seccomp/AppArmor restrictions.
This exposes additional kernel operations and must be an explicit operator
choice. It does not use privileged containers, add SYS_ADMIN, or disable either
security subsystem. Compose services, Podman, and other coordinator platforms
are not supported by this policy.

An administrator must install and load the bundled
`spec_runtime/profiles/specbutler-nested-v1.apparmor` profile on the Docker daemon
host. The corresponding seccomp JSON is supplied by the installed CLI, not by
the model-writable checkout. Locate the AppArmor file in the installed Python
environment, review it, then install it with root ownership under
`/etc/apparmor.d/specbutler-nested-v1` and load it with
`sudo apparmor_parser --replace /etc/apparmor.d/specbutler-nested-v1`.
Simply loading it does not attach it to existing containers or change Docker's
default profile. A profile loaded from a checkout without installation under
`/etc/apparmor.d` is temporary and will not survive reboot.

Use a security-maintained Bubblewrap with `--argv0` support (validated here:
0.12.0). Codex needs that option to re-execute its native helper while keeping
its credential home denied. The preflight rejects older Bubblewrap before
launching Codex; installing helper aliases inside the hidden home is insufficient.
Pin provider and sandbox versions and rerun the probe after upgrades.

Codex 0.154.0 also recognizes an unavailable proc mount by its legacy
`/newroot/proc` error text. Bubblewrap 0.12.0 changed that diagnostic to `/proc`.
The validated worker build retains the legacy spelling for that one error;
mount operations, errno, and exit status remain unchanged. This permits Codex's
existing proc-mount fallback without widening the worker's procfs access.

Run `spec container doctor` with a configured image, then `spec container smoke`
to check the actual worker. Both use the selected policy; the Codex enforcement
probe must prove workspace/outbox writes, protected-file read denial, and
external write denial before implementation begins. Missing host profiles fail
closed. A run records its selected policy and cannot switch policies on resume;
create a new run when selecting a different policy.

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
