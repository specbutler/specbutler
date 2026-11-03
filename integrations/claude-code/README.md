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
reviews. A failed check without structured findings fails closed. A successful check
may omit findings. Keep CI publication credentials in CI, outside spec content.
