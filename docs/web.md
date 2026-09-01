# Web dashboard and chat

The optional web interface provides a local dashboard, run controls, server-sent
event updates, and isolated Claude or Codex chat sessions for creating specs
and scoping tasks.

On the supported native Windows tier, chat uses Codex only. Native Claude is
unavailable and fails closed; run Spec Butler in WSL2 or a supported Linux/macOS
container when Claude is required. See [Native Windows support](windows.md) for
the exact support matrix.

## Install and start

```bash
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install --force \
  "specbutler[web] @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"

cd /path/to/your-project
spec web start --open
```

The server binds to `127.0.0.1:7700` by default. `--open` launches an
authenticated URL in the default browser; after authentication, the token is
removed from the URL and stored in an HTTP-only cookie.

For a persistent local process:

```bash
spec web start --background
spec web status
spec web stop
```

The PID, port, and authentication token live under the configured
`.spec-state/web/` directory and should remain gitignored.

## Authentication

Every dashboard and API request requires the generated token. Print or rotate
it with:

```bash
spec web token
spec web token --reset
```

Resetting the token invalidates existing browser sessions immediately. Do not
put the token in committed configuration, screenshots, issue reports, or shell
scripts shared with other users.

Keep the default loopback bind for normal use. If access from another machine
is required, use a TLS-terminating SSH or private-network tunnel and keep the
application server on loopback. Binding directly to a public interface exposes
an operator control plane and is not recommended.

## Chat sessions

Choose Claude or Codex when starting a chat. The UI prefers the repository's
configured default agent when that provider is available. Each session receives
a dedicated Git worktree and branch, so chat-created specs and task scope do not
modify the main orchestration checkout. The selected provider CLI must already
be logged in and available on `PATH` to the web-server process.

The browser streams assistant text and tool activity while a turn is running.
Starting or stopping an implementation run, handing a reviewed task to the
orchestrator, and stopping a chat require confirmation. Stopping a chat cancels
its provider process but intentionally preserves the session worktree and
branch for inspection; the UI displays both paths. Commit and push work you want
to keep before removing the worktree. Use `git worktree remove` and delete the
branch only when you have deliberately discarded it.

Chat session metadata and transcript history are held in the web-server
process, not persisted. Restarting `spec web` loses the session listing even
though its worktrees and branches remain. Before manual cleanup, list registered
worktrees and inspect their changes:

```bash
git worktree list
git -C .worktrees/<chat-worktree> status --short
git worktree remove .worktrees/<chat-worktree>
git branch -d <chat-branch>       # use -D only after deliberately discarding work
```

There is not yet a Finish/Discard action in the UI. Treat chat worktrees as
operator-owned until they are committed or deliberately removed.

Use `--verbose` while diagnosing provider startup or event-protocol problems:

```bash
spec web start --verbose
```

### Credentialed Linux Claude regression

Maintainers can opt into a real-provider regression that creates a temporary
Git repository, starts the actual authenticated web server, and uses one real
Claude session for three context-dependent HTTP/SSE turns. The second turn must
recall a random marker supplied only in turn one; the third must recall that
marker and a second marker supplied only in turn two. The test also stops the
session and server and verifies that their exact provider processes are gone.

Install the development and web dependencies, authenticate Claude Code, and
install the Linux sandbox prerequisites (`bubblewrap` and `socat`). Then run:

```bash
SPEC_LINUX_CLAUDE_REAL_PROVIDER=1 \
pytest -m linux_claude_real_provider \
  tests/test_linux_claude_real_provider.py -v
```

The test is skipped unless the opt-in variable is exactly `1`. It inherits the
operator's existing Claude authentication without copying credentials into the
fixture repository. It discards server/provider output rather than recording
the authenticated startup URL, prompts, model responses, or provider output,
and removes the temporary web token during cleanup.

## Troubleshooting

- **The page shows the login form:** run `spec web token` and paste the token,
  or restart with `--open`.
- **A provider is unavailable:** verify `claude --version` or `codex --version`
  in the same environment that launches `spec web` and complete the provider's
  login flow. Native Windows supports Codex only. On Linux, Claude web chat
  also requires `bubblewrap` and `socat`; install both and rerun `spec doctor`.
  Spec configures Claude to fail closed when its sandbox cannot start instead
  of silently running commands without isolation.
- **Codex reports that `.codex/config.toml` is not a directory:** an older tool
  left a project-root `.codex` file. Inspect and rename or remove that file so
  current Codex can use `.codex/` as a directory, then rerun `spec doctor`.
- **Chat starts but produces no output:** stop the session, restart the server
  with `--verbose`, and inspect the provider error. Confirm that the installed
  `web` dependencies are current.
- **The background server is stale:** run `spec web status`, then `spec web
  stop` before starting it again. PID reuse is checked against process start
  identity.
- **The dashboard does not update:** verify the browser can keep an SSE request
  to `/api/v1/events` open through any tunnel or reverse proxy.

The web UI is an operator interface, not a multi-tenant service. It does not
replace host access controls, secret management, or a hardened remote gateway.
