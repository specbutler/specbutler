# Nested container sandbox validation

The current worker fails its model-free Codex enforcement probe under Docker's
default security profiles. A disposable non-root worker also fails
`unshare --user --map-root-user true`. Allowing only that operation in seccomp
makes it succeed. Allowing Bubblewrap's additional mount calls then reaches
`bwrap: Failed to make / slave: Permission denied` under Docker's AppArmor
profile. Docker's default AppArmor policy explicitly denies mounts.

The candidate named AppArmor and seccomp profiles are in
`src/spec_runtime/profiles/`. They are derived from Moby profiles revision
`245180c51918481c0525424b3ee025d2b435d46c` (Apache-2.0):

- https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/apparmor/testdata/with-tunables.golden
- https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json

The change permits user-namespace creation and mount setup for nested sandboxing.
AppArmor retains Docker's network-family, procfs, sysfs, signal, and ptrace
restrictions. New mounts are limited to the listed virtual filesystem types and
bind/remount/propagation operations. Seccomp retains the remaining default
allowlist and clone3 fallback restriction. It does not grant setns access.

The required launch uses a non-root user, drops ALL outer capabilities, and
enables no-new-privileges. The disposable probe mounts neither a Docker socket nor host credentials.
These are mandatory parts of the candidate, not optional hardening. Namespace
and mount operations expose more kernel code than Docker's default policy;
this is an explicit per-worker tradeoff requiring operator approval.

## Evidence and remaining validation

- AppArmor profile compiles with `apparmor_parser --skip-kernel-load --skip-cache`.
- Non-root user namespace creation succeeds with the candidate seccomp policy,
  all capabilities dropped, and no-new-privileges set.
- The corrected profile was loaded on Ubuntu kernel 6.17.0-29-generic with
  Docker 29.5.2 and tested against Codex 0.154.0 on 2026-09-22. Bubblewrap
  requires the exact `silent` mount-flag combinations present in this profile.
- The outer worker checks passed: effective and bounding capabilities are zero,
  no-new-privileges is set, direct mounts fail, and joining an existing mount
  namespace fails.
- The real credential-free probe passes workspace/outbox writes, protected-file
  and synthetic credential-home read denial, and external write denial.
- Descendant user/mount namespace attempts cannot uncover a protected file or
  make the read-only root writable. All three enforcement markers pass.
- Codex helper aliases must be available on a root-owned system PATH: npm Codex
  otherwise creates them inside CODEX_HOME, which this boundary deliberately
  denies. The tested image links `codex-linux-sandbox` and `apply_patch` to the
  installed native Codex executable outside that home. A root-owned compatibility
  launcher additionally redirects startup filesystem reads that use the absolute
  hidden helper path; all Bubblewrap flags are preserved.
- A disposable bind workspace passes actual backend preparation, sandbox
  preflight, pause/resume with a host-written handoff, agent launch, runtime
  quiescence/recreation, command execution, and cleanup. Host and worker use
  the same candidate wheel. Deployment additionally checks exact Git source
  identity with the final installed runtime and worker image.

`scripts/probe_nested_container_sandbox.py --image IMAGE --profile default`
reproduces the failure; `--profile nested` tests the candidate. Run with this
checkout's `src` on PYTHONPATH and a Python environment containing Spec Butler's
dependencies. Both commands use disposable workers and leave defaults alone.

A real Codex 0.154.0 session also completed a shell write/read round trip under
the implementation permission profile with the provider home denied. The
preflight now catches the absolute-helper startup failure without a model call;
the helper-alias-only image fails this check and the complete image passes.
