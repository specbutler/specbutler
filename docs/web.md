# Web dashboard and chat

The optional web interface provides a local dashboard, run controls, server-sent
event updates, and isolated Claude or Codex chat sessions for creating specs
and scoping tasks.

On the supported native Windows tier, chat uses Codex only. Native Claude is
unavailable and fails closed; run the web server and Claude CLI inside WSL2, or
use a supported Linux/macOS host, when Claude is required. See [Native Windows
support](windows.md) for the exact support matrix.

## Install and start

```bash
SPEC_RELEASE="$(gh release view --repo specbutler/specbutler --json tagName --jq .tagName)"
pipx install --force \
  "specbutler[web] @ git+https://github.com/specbutler/specbutler.git@${SPEC_RELEASE}"

cd /path/to/your-project
spec web start --open
```

The server binds to `127.0.0.1:7700` by default. `--open` launches a URL holding
a 60-second, single-use opening nonce—not the durable operator token. Consuming
it creates a short-lived, launch-local browser session; the HTTP-only cookie
never contains the reusable operator token. If the nonce expires, paste the
token into the login form instead; the form submits it in the request body.

For a persistent local process:

```bash
spec web start --background
spec web status
spec web stop
```

The PID, port, and logs live under the configured `.spec-state/web/` directory
and should remain gitignored. The authentication token is an operator
credential, so it is stored outside the repository in a private,
repository-specific user-state directory:

- Linux: `$XDG_STATE_HOME/specbutler/web/` or `~/.local/state/specbutler/web/`
- macOS: `~/Library/Application Support/SpecButler/web/`
- Windows: `%LOCALAPPDATA%\SpecButler\web\`

The repository path is represented by a hash below that directory. Upgrading
from a release that stored the token in `.spec-state` rotates the token and
removes the obsolete repository-local copy; existing browser sessions must log
in once with the newly printed token.

## Authentication

The generated operator token is the credential for login and non-browser API
clients. Print or rotate it with:

```bash
spec web token
spec web token --reset
```

Resetting the token invalidates existing browser sessions immediately. Do not
put the token in committed configuration, screenshots, issue reports, or shell
scripts shared with other users.

Browser login creates an in-memory session that expires after one hour and is
lost when the server restarts. Sensitive API reads, writes, and event streams
also require an independent proof kept in the browser origin's local storage.
That proof is delivered only in the no-store login response body, then the
bootstrap page replaces itself with the dashboard. Neither the durable token
nor the request proof enters browser URL history. This separation matters on
development machines because browsers scope localhost cookies by host, not by
port: a cookie observed by another local development server is neither the
operator bearer nor sufficient to call the Spec Butler API.

Keep the default loopback bind for normal use. If access from another machine
is required, use a TLS-terminating SSH or private-network tunnel and keep the
application server on loopback. Binding directly to another interface exposes
an operator control plane and is refused unless the risk is explicitly
acknowledged:

```bash
spec web start --host 0.0.0.0 --allow-remote
```

Spec Butler does not terminate TLS. Do not expose that direct bind to an
untrusted network. Forwarded proxy headers are ignored by default; when a TLS
reverse proxy is part of the deployment, trust only its exact source IP (the
option is repeatable):

```bash
spec web start --trusted-proxy 127.0.0.1
```

Wildcard and hostname proxy trust entries are rejected. Every browser API
request must carry its independent session proof, and browser action requests
must also carry a matching `Origin` or `Referer`. API clients using the durable
operator token in `Authorization: Bearer ...` are unaffected. Query-token
authentication is not accepted; URL-based automatic opening uses only the
expiring, single-use nonce.

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
branch for inspection; the UI displays both paths. Provider-created commits
are first validated and imported from the chat's disposable Git directory only
after provider shutdown is confirmed. Commit any remaining uncommitted work
and push work you want to keep from your operator shell before removing the
worktree. Use `git worktree remove` and delete the branch only when you have
deliberately discarded it.

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

### Credentialed Linux provider regressions

Maintainers can opt into a real-provider regression that creates a temporary
Git repository, starts the actual authenticated web server, and uses a real
Claude or Codex session for three context-dependent HTTP/SSE turns. Later turns
must recall random markers supplied only in earlier turns. The Codex regression
also creates and edits a file through chat. Both tests stop the session and
server and verify that their exact provider processes are gone.

Install the development and web dependencies, authenticate Claude Code, and
install the Linux sandbox prerequisites (`bubblewrap` and `socat`). Then run:

```bash
SPEC_LINUX_CLAUDE_REAL_PROVIDER=1 \
pytest -m linux_claude_real_provider \
  tests/test_linux_claude_real_provider.py -v
```

For Codex, authenticate the Codex CLI and run:

```bash
SPEC_LINUX_CODEX_REAL_PROVIDER=1 \
pytest -m linux_codex_real_provider \
  tests/test_linux_claude_real_provider.py -v
```

Each test is skipped unless its opt-in variable is exactly `1`. It inherits the
operator's existing provider authentication without copying credentials into
the fixture repository. It discards server/provider output rather than
recording the authenticated startup URL, prompts, model responses, or provider
output, and removes the temporary web token during cleanup.

For retained release evidence, use the checked-in runner from a completely
clean checkout and name the revision independently:

```bash
revision="$(git rev-parse HEAD)"
python tools/linux_claude_web_evidence.py \
  --expected-revision "$revision" \
  --output /path/to/evidence/linux-claude-web-result.json
```

The evidence runner currently targets Claude. It selects the marked test itself
and writes a private, single-run receipt only after all three dependent
HTTP/SSE turns prove their random context markers and both the Claude provider
and web-server processes are reaped. The runner binds that receipt to the clean
checkout's exact commit before atomically publishing the result. A failure,
skip, dirty checkout, revision mismatch, or incomplete receipt removes any
stale output and leaves no passing artifact.

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
