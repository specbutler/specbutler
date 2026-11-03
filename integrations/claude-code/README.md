# Spec Butler session integration

Install Spec Butler in the environment used by your session (`pip install specbutler`).
Run from a repository initialized with `spec init`. Load this local Claude Code plugin:

```sh
claude --plugin-dir /absolute/path/to/specbutler/integrations/claude-code
```

The plugin supplies the spec-workflow skill and a stdio MCP server. Ensure `spec` is
on PATH. The server inherits the session working directory; start Claude in the target
repository. Dispatch changes state and starts an independent, adoptable process with
file-backed logs. Poll spec_status for completion; closing the client or MCP server
does not stop a dispatched run. Repeated dispatch returns the active run id.

For Codex, add the same server to config.toml:

```toml
[mcp_servers.specbutler]
command = "spec"
args = ["mcp", "serve"]
cwd = "/absolute/path/to/target/repository"
```

To use CI review instead of the default local reviewer, configure the target repo:

```toml
[review]
source = "ci"
checks = ["review-decision-gate"]
max_rounds = 3
```

All configured checks must finish on the current PR head. Structured check output
uses the normal review JSON payload with reviewed_head_sha, status, summary and
findings. Findings use the same computed decisions and review round cap as local
reviews. Every completed check must supply structured results, even when its
findings array is empty. Results come from check output, the run artifact, or a
GitHub Actions sticky comment bound to that check execution.

The bundled workflow publishes a separate decision status for CI-owned PRs.
A completed review job stays successful when it requests changes; its decision
status blocks merging until the host computes approval or the operator accepts
at the round cap. The host updates only configured review gates. Custom checks
that fail their check run must permit the host to update that run with a
checks:write credential from the publishing GitHub App; otherwise acceptance
fails closed. Keep publication credentials outside spec content.

Before pushing a retry, the host includes prior findings, the implementer
response, and the incremental diff in the PR body. CI reviewers must classify
prior IDs exactly, including their check prefixes. The bundled workflow also
inlines the canonical spec, full diff, and current check evidence into its prompt.
