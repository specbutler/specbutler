# Implement Setup Manifest

The implement phase can run repo-defined setup and teardown hooks through the
`[implement]` section in `.spec.toml`:

```toml
[implement]
setup_command = "scripts/implement-setup.sh"
teardown_command = "scripts/implement-teardown.sh"
```

When configured, the orchestrator runs the commands with:

```text
--worktree <path> --spec-id <id> --run-id <id> --attempt <n>
```

It also exports:

```text
SPEC_ID=<id>
SPEC_RUN_ID=<id>
SPEC_PATH=<worktree-relative spec path>
```

`setup_command` may print progress logs before a trailing JSON manifest. The
orchestrator ignores non-JSON prefix output and parses the final JSON object.

## Schema

All fields are optional:

```json
{
  "env": {
    "DATABASE_URL": "postgres://..."
  },
  "prompt": "A dev server is running at http://127.0.0.1:43123/app/.",
  "mcp_prompt": "Use Playwright MCP to inspect the UI at the above URL.",
  "mcp_servers": {
    "playwright": {
      "command": "node",
      "args": ["/path/to/mcp/cli.js", "--headless"]
    }
  },
  "managed_processes": [
    {
      "name": "dev-server",
      "kind": "server",
      "pid": 12345,
      "started_at": "process-identity-from-the-operating-system",
      "command": "npm run dev",
      "termination_scope": "pid"
    }
  ]
}
```

The orchestrator applies the manifest like this:

- `env`: merged into the implement agent environment.
- `prompt`: appended to the implement prompt for all agents.
- `mcp_prompt`: appended only for agents that support MCP.
- `mcp_servers`: merged into the generated MCP config only for agents that
  support MCP.
- `managed_processes`: registered for identity-checked teardown. Each entry
  requires a positive `pid` and the process start identity reported by the
  operating system. `termination_scope` may be `pid` or `pgid`; a `pgid` scope
  may also supply the process-group ID. Tokenless `pid` teardown requires a
  stable kernel process handle (currently Linux pidfd); other platforms fail
  closed and preserve the worktree. Use a teardown command or a dedicated
  `pgid` on those platforms rather than relying on a raw PID.

Setup hooks may start background services and then exit. The orchestrator waits
only for the setup leader, so a child that inherits its output handles does not
block setup completion. On native Windows, the setup command and its descendants
are placed in one run-owned Job Object before execution begins. On POSIX, they
run beneath a small retained session/process-group leader. A parent-death pipe
makes that leader terminate its group if the orchestrator disappears. In either
case, a declared `managed_processes` entry is accepted as the boundary teardown
handoff only when its exact process identity is still a live member and the
cleanup registration is persisted successfully. At least one authenticated,
persisted entry retains the complete boundary, including workers that outlive
the declared service, for whole-tree cleanup. If live in-boundary descendants
remain but no entry completes that handoff, Spec Butler terminates the retained
boundary before launching the agent.

For that transactional protection, a service must not daemonize, create a new
session/process group, request Windows Job breakaway, or otherwise escape the
setup boundary. An explicitly declared escaped POSIX process may still use the
older identity-checked `pid`/`pgid` registration where the platform supports
it, but it is not contained by the keeper and cannot be recovered if setup exits
before printing the declaration. Registry persistence failure blocks agent
launch and triggers best-effort exact cleanup; do not rely on this compatibility
path for new setup hooks.

## Examples

Database bootstrap:

```json
{
  "env": {
    "DATABASE_URL": "postgresql://app:app@127.0.0.1:55433/app_dev",
    "TEST_DATABASE_URL": "postgresql://app:app@127.0.0.1:55433/app_test"
  }
}
```

Dev server handoff:

```json
{
  "prompt": "The app is running at http://127.0.0.1:3000/."
}
```

Playwright MCP:

```json
{
  "prompt": "The app is running at http://127.0.0.1:3000/.",
  "mcp_prompt": "Use Playwright MCP to inspect the rendered UI before you commit.",
  "mcp_servers": {
    "playwright": {
      "command": "node",
      "args": ["frontend/node_modules/@playwright/mcp/cli.js", "--headless"]
    }
  }
}
```

Teardown output is ignored. A non-zero `teardown_command` exit is logged but
does not fail the implement phase.

## Failure semantics

`setup_command` is a **best-effort prewarm step**, not an admission gate. If the
command exits non-zero or cannot be launched at all, the orchestrator still
launches the implement agent and hands it a structured diagnostic block in the
setup prompt: the command, exit code (or "launch failed"), trimmed + redacted
stderr/stdout tails, and a list of environment keys, MCP server names, and
managed process names that the partial manifest populated before crashing.

Because partial manifests are still consumed, setup scripts should:

- **Be safe to rerun across attempts.** The orchestrator retries implement
  phases and runs teardown between attempts; setup may run multiple times on
  the same worktree.
- **Emit a partial JSON manifest before crashing when possible.** If a
  `managed_processes` entry is printed before the script errors out, the
  orchestrator registers the process so teardown can clean it up.
- **Keep Windows services inside the setup Job.** Do not request Job breakaway
  when launching a service. Print the operating-system start identity for the
  service itself; Spec Butler verifies that identity against kernel Job
  membership before persisting teardown ownership.
- **Not rely on prepare to be a gate.** Repo-specific failures surface as
  diagnostics for the agent to investigate as part of the spec work. Verify
  gates remain strict.

A setup failure is recorded as a non-fatal warning on the run
(`failure_type="setup"`, `failure_subtype="prepare_failed"`) so TUIs and audits
can surface it.

The following remain hard failures that short-circuit the phase before the
agent is launched:

- Missing worktree.
- Branch sync failure.
- Agent binary missing (`FileNotFoundError` from `Popen`).
- Unusable workspace (e.g., required intake missing).
