# Security Policy

## Supported versions

Security fixes are made on the latest release line and on `main`. Older
versions may not receive backports; users should upgrade to the newest release
before reporting a problem that is already fixed there.

## Trust model

`spec` assumes that the operator trusts the repository and the configuration
being executed. Repository-defined bootstrap, implement setup/teardown, and
verify commands run as code. Agent sessions can also act on spec text and other
repository content. Inspect untrusted pull requests before starting a run;
`safety_mode = "safe"` is currently a recorded compatibility label, not a
security boundary.

Built-in Claude and Codex reviewers do not install or execute code from the
change under review. The host materializes the canonical spec, exact diff, and
completed gate evidence before launch. Codex receives only that evidence with
model-controlled shell, code, browser, plugin, subagent, user-config, and MCP
capabilities disabled; Claude runs in restricted mode with read-only file tools
and no MCP. Review and blocked-run-debugger prompts travel on stdin rather than
in process arguments. Custom reviewer adapters may retain the detached-checkout
bootstrap contract; that install runs in a no-network sandbox or is skipped.

The host orchestrator owns forge authentication, publication, review, merge,
and run state. Non-interactive agent sessions receive an isolated MCP set, a
provider-specific allowlisted environment, and a per-launch completion outbox
instead of write access to shared control state. Built-in review and debugger
provider credentials are kept outside the attacker-controlled checkout. Claude
local review requires restricted read-only mode and fails closed when the
installed CLI cannot supply it. Container workers should not receive host
GitHub credentials, SSH keys, or a container-engine socket. These controls
reduce credential exposure but do not make a malicious repository safe to
execute.

For linked-worktree implementation, authoring, and web-chat sessions, the
provider writes a disposable private Git directory rather than the repository's
real administrative files. The real index, config, object database, reflogs,
and refs remain read-only to the provider. Only after the supervisor has
confirmed its operating-system ownership boundary stopped does the host
validate that the private history descends from the launch head and atomically
advance that session's exact branch. A changed layout, config, alternate object
path, or concurrent branch update fails the import closed. The ownership
boundary's platform-specific limits are described below.

Interactive authoring keeps the operator's trusted MCP registrations, but its
process environment contains only provider values and variables explicitly
referenced by those registrations. Claude shell commands require operator
approval; Codex workspace edits and commits remain available while network
commands require approval. Ordinary forge credentials are omitted and Git
publication is guarded, but explicitly trusted MCP servers retain their own
service authority and provider approval behavior. The prompt forbids publishing
through either route; inspect and publish local commits from an operator shell.

Before implementation, authoring, and operator-intervention launches, Spec
Butler refuses repository-local remote URL userinfo,
`Authorization`/`Proxy-Authorization` extraheaders, credential helpers, and
include directives. Linked-worktree and global Git config paths are
sandbox-denied. Host-side Git and forge operations revalidate their pre-agent
baseline and stay pinned to the captured remote and repository identity.

Implementation setup manifests may declare project environment values, but
they cannot replace provider authentication, API routing, process search paths,
TLS/proxy settings, Spec Butler control variables, or runtime loader/startup
controls. Container launches pass admitted values through the Docker client
environment rather than command arguments and redact them from command logs.

Run-owned provider and verification commands retain an operating-system
ownership boundary. A successful leader exit is not sufficient: Spec Butler
terminates and confirms that boundary before a command can release ownership or
allow a later publication step. Timeout, exception, and asynchronous
cancellation paths enforce the same postcondition; an unconfirmed cleanup fails
closed while retaining a retryable process handle. Windows uses a Job Object.
Linux uses a private cgroup v2 when the current service scope delegates one,
which covers ordinary daemonization, new sessions, and nested child cgroups;
otherwise POSIX falls back to a dedicated process group. A deliberately hostile
same-UID process can escape a writable ancestor cgroup, and a child can leave a
process-group fallback, so these controls do not change the trusted-repository
model. Provider sandboxes must deny writes to the host cgroup hierarchy.
Long-lived setup services use the separate, explicitly declared
service-lifecycle contract.

Choose `container` when you need a stronger process and toolchain boundary.
Use a disposable machine or VM—with separate, least-privilege credentials—for
genuinely untrusted repositories or build hooks. The web dashboard is a
single-operator control plane and should remain on loopback or behind an
authenticated private tunnel. Browser login exchanges the durable operator
token for a short-lived, server-memory session and an independent origin-held
request proof; the reusable bearer is never stored in a localhost cookie.

The optional coordinator database and its SQLite sidecars are created with
private POSIX permissions and symlink-shaped database targets are rejected.
The immediate database parent must be user-owned and not group/world-writable.
When configuring fixed server-side coordinator tokens, prefer the
`--worker-token-file` and `--operator-token-file` options with private files;
their immediate parent directories must also be user-owned and not
group/world-writable. The deprecated raw token arguments expose their values in
the process command line.

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub's private security
advisory flow: **Security → Advisories → Report a vulnerability** in this
repository.

Include the affected version or commit, impact, reproduction steps, and any
known mitigations. Do not include credentials, private repository contents, or
exploit details in a public issue. If private advisory reporting is unavailable,
email [thiago.hirai@gmail.com](mailto:thiago.hirai@gmail.com) with the subject
`Spec Butler security report`; do not open a public issue containing
vulnerability details.

You should receive an acknowledgement within seven days. We will validate the
report, coordinate a fix and disclosure timeline with you, and credit you in the
release notes unless you prefer to remain anonymous.

## Scope

Reports about command execution boundaries, credential exposure, unsafe process
signalling, path traversal, web authentication, and GitHub Actions trust
boundaries are especially useful. Reports that require a user to deliberately
run an untrusted spec or configuration with full host permissions may be treated
as hardening requests rather than vulnerabilities, depending on the impact.
