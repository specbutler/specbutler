# SQLite Coordinator with Tailscale or SSH

This guide documents the recommended small-team setup for the SQLite-backed
coordinator. It is for teams that want several machines to run `spec implement`
or `spec auto run` against the same spec queue without opening a public,
unauthenticated service.

## Recommended Shape

Use one coordinator service and several independent worker machines:

- One trusted machine runs `spec coord serve` with a private SQLite database.
- Each worker machine has its own repo checkout, `.worktrees/`, `.spec-state/`,
  agent credentials, and GitHub credentials.
- Workers run normal commands such as `spec implement --spec <id>` or
  `spec auto run`; the coordinator only arbitrates leases.
- Operators can run `spec coord status` and inspect the coordinator with an
  operator token.

Expose the coordinator over a private network path such as Tailscale, or use an
SSH tunnel. Do not run the service as a public unauthenticated listener. The
coordinator does not execute remote code, does not move worktrees between
machines, and does not proxy agent sessions. It only records leases, machines,
heartbeats, and lease events.

## Start the Coordinator

On the coordinator host, choose a database path that is private to the
coordinator process:

```bash
TAILSCALE_IP="$(tailscale ip -4)"
mkdir -p ~/.local/state/spec
spec coord init --server --db ~/.local/state/spec/coord.sqlite --host "$TAILSCALE_IP" --port 8765
```

The init command creates the database schema, creates default worker and
operator tokens, prints each bearer token once, and prints the matching
`spec coord serve` command. Store printed secrets in a password manager,
environment variable, or uncommitted local config file. The database stores
only token hashes.

For a Tailscale-only listener, bind to the host's Tailscale IP:

```bash
TAILSCALE_IP="$(tailscale ip -4)"
spec coord serve --host "$TAILSCALE_IP" --port 8765 --db ~/.local/state/spec/coord.sqlite
```

For a local SSH tunnel, keep the service bound to loopback:

```bash
spec coord serve --host 127.0.0.1 --port 8765 --db ~/.local/state/spec/coord.sqlite
```

Then create a tunnel from each worker:

```bash
ssh -N -L 8765:127.0.0.1:8765 coordinator.example
```

Workers using the tunnel configure `http://127.0.0.1:8765`. Workers using
Tailscale replace `<coordinator-tailscale-ip>` below with the coordinator's
Tailscale IPv4 address.

## Configure Workers

Keep shared, non-secret defaults in committed `.spec.toml`:

```toml
[coordination]
backend = "http"
url = "http://<coordinator-tailscale-ip>:8765"
repo_id = "github.com/acme/widget"
```

Put machine-specific or secret values in `.spec.local.toml`. `spec init` adds
this file to `.gitignore`; do not commit it.

```toml
[coordination]
machine_id = "alice-mbp"
token = "spec_worker_token_printed_by_token_create"
```

You can also configure everything with environment variables:

```bash
export SPEC_COORDINATOR_BACKEND=http
export TAILSCALE_IP="<coordinator-tailscale-ip>"
export SPEC_COORDINATOR_URL="http://${TAILSCALE_IP}:8765"
export SPEC_COORDINATOR_REPO_ID=github.com/acme/widget
export SPEC_MACHINE_ID=alice-mbp
export SPEC_COORDINATOR_TOKEN=spec_worker_token_printed_by_token_create
```

Environment variables override `.spec.local.toml`, which overrides committed
`.spec.toml`.

To write safe local worker config, use the worker init command:

```bash
TAILSCALE_IP="<coordinator-tailscale-ip>"
spec coord init --worker \
  --url "http://${TAILSCALE_IP}:8765" \
  --repo-id github.com/acme/widget \
  --machine-id alice-mbp \
  --token spec_worker_token_printed_by_init
```

This writes `.spec.local.toml`, preserves unrelated local settings, refuses to
replace an existing token unless `--force` is passed, and prints equivalent
environment exports. Use `--env-only` to print exports without writing the
local file.

Verify the worker configuration before running specs:

```bash
spec coord doctor
```

The doctor command prints the resolved backend, coordinator URL with
credentials redacted, repo id, machine id, whether a token is set, API
compatibility, and an end-to-end synthetic lease acquire, conflict, heartbeat,
release, and re-acquire smoke test. It never prints the token. `spec coord
status` remains available for a shorter configuration and connectivity check.

## Multi-Machine Preflight

Before dispatching real specs from multiple machines:

1. Start `spec coord serve` on one trusted coordinator host using the command
   printed by `spec coord init --server`.
2. Configure two worker checkouts with distinct `machine_id` values using
   `spec coord init --worker`.
3. Run `spec coord doctor` on both workers and confirm both finish with
   `Doctor status: ok`.
4. Run a dry autopilot dispatch from both workers using the operator token
   printed by `spec coord init --server`, because lease inspection requires
   operator scope:

   ```bash
   SPEC_COORDINATOR_TOKEN=spec_operator_token_printed_by_init spec auto run --dry-run
   ```

   Confirm remote active leases show as waiting elsewhere instead of starting
   duplicate work.

## Tokens

Coordinator requests use bearer tokens. There are two scopes:

- `worker`: acquire, heartbeat, and release leases.
- `operator`: inspect leases, machines, and event history.

Create separate tokens for each machine or role so you can rotate or revoke
them independently:

```bash
spec coord token create --db ~/.local/state/spec/coord.sqlite --name worker-alice --scope worker
spec coord token create --db ~/.local/state/spec/coord.sqlite --name worker-ci-1 --scope worker
spec coord token create --db ~/.local/state/spec/coord.sqlite --name operator-admin --scope operator
```

Rotate a token by creating it again with the same name, then update the worker's
local config or environment:

```bash
spec coord token create --db ~/.local/state/spec/coord.sqlite --name worker-alice --scope worker
```

Revoke a token by name:

```bash
spec coord token revoke --db ~/.local/state/spec/coord.sqlite --name worker-alice
```

Existing workers using a revoked token will fail closed the next time they need
to acquire or heartbeat a coordinator lease.

## Machine IDs

`machine_id` identifies the worker that owns a lease. Choose stable, readable
names such as:

- `alice-mbp`
- `buildbox-1`
- `contractor-sam`

If `machine_id` is not configured, `spec` falls back to the local hostname.
Explicit names are usually better for contributors, laptops that may be
renamed, containers, and machines where the hostname is not meaningful.

Each machine still needs its own local checkout, agent credentials, and GitHub
credentials. The coordinator cannot borrow credentials or worktrees from
another machine.

## Lease Behavior

When coordination is enabled, `spec implement` and `spec auto run` acquire a
lease before working on a spec. A second machine attempting the same repo/spec
while the lease is active receives a conflict that names the current owner,
run id, heartbeat age, and expiry time.

The default coordinator lease TTL is 900 seconds. Active runs heartbeat during
long phases so the lease expiry is extended while the owner is alive. You can
override the TTL for orchestrator clients with:

```bash
export SPEC_COORDINATOR_LEASE_TTL_SECONDS=900
```

If a machine crashes or loses network access, its heartbeat stops. Once the TTL
expires, another machine can acquire the spec. The old lease is marked
`expired`, and the new lease becomes active.

Normal lifecycle outcomes:

- Successful completion: the orchestrator releases the active lease after the
  run finishes.
- Blocked, failed, or waiting-for-input runs: the orchestrator also releases
  the active lease when the run exits with that final state.
- Resumed runs by the same owner: reacquiring the same run is idempotent and
  refreshes the lease.
- Resumed work after a crash: another machine can continue only after the old
  lease expires or an operator intentionally bypasses coordination.

Completion is still determined by annotated merge tags named
`spec/merged/<spec-id>`. The coordinator prevents duplicate active work; it is
not the source of truth for whether a spec is merged.

Use `--coordination-bypass` only for emergency local-only work when a configured
coordinator is unavailable and you accept the risk of duplicate cross-machine
work.

## Limitations

- Local run artifacts remain on the machine that produced them.
- The coordinator is not a remote execution service.
- `spec watch` may show remote ownership without remote logs unless a later
  artifact sharing feature is added.
- Existing `spec watch` and `spec status` reconciliation behavior is separate
  from distributed lease coordination.
- This guide intentionally does not cover public internet deployment patterns.
