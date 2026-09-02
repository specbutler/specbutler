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

The dependency-install step performed for a detached local-review checkout is
handled differently because package build hooks come from the change under
review. That one command runs in a no-network sandbox with writes scoped to the
disposable review checkout and operator-home reads denied. If Spec Butler cannot
establish that boundary, it skips the install and continues with a diff-only
review. This narrow control does not extend to implementation bootstrap, verify
commands, or other lifecycle hooks.

The host orchestrator owns forge authentication, publication, review, merge,
and run state. Non-interactive agent sessions receive an isolated MCP set, and
container workers should not receive host GitHub credentials, SSH keys, or a
container-engine socket. These controls reduce credential exposure but do not
make a malicious repository safe to execute.

Choose `container` when you need a stronger process and toolchain boundary.
Use a disposable machine or VM—with separate, least-privilege credentials—for
genuinely untrusted repositories or build hooks. The web dashboard is a
single-operator control plane and should remain on loopback or behind an
authenticated private tunnel.

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
