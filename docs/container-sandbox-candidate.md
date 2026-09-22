# Nested container sandbox validation

Docker defaults prevent the provider sandbox from creating the user namespaces
and mounts it needs. Allowing user namespaces in seccomp exposes a second
failure: Docker's AppArmor profile explicitly denies mounts.

The versioned policies in `src/spec_runtime/profiles/` derive from Moby profiles
commit `245180c51918481c0525424b3ee025d2b435d46c` (Apache-2.0):

- https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/apparmor/testdata/with-tunables.golden
- https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json

The worker must use a non-root uid/gid, no outer capabilities, and
no-new-privileges. The policy permits nested namespace/mount setup while
retaining Docker's remaining restrictions. This exposes additional kernel
operations and requires explicit operator selection and host profile installation.

## Verified behavior

On Ubuntu kernel 6.17.0-29-generic and Docker 29.5.2:

- The AppArmor profile compiles and enforces its exact Bubblewrap mount rules.
- Effective and bounding capabilities are zero; direct mounts and joining the
  outer mount namespace fail.
- The credential-free probe proves workspace/outbox writes, protected-file and
  synthetic credential-home read denial, and external write denial.
- Descendant namespace attempts cannot uncover a protected file or make the
  inherited read-only root writable.
- Backend preparation, pause/resume, host handoff, agent launch, quiescence,
  recreation, commands, and cleanup pass in a disposable bind workspace.
- Real Codex shell and apply_patch operations pass with the credential home
  denied. A real Claude shell write/read round trip also passes.
- Retirementlab's complete test, lint, and browser gates pass inside the worker
  under these outer profiles. Test-only smoke must provision its database or
  clear placeholder database URLs so the repository can provision one.

The final Codex image uses Bubblewrap 0.12.0, including its upstream symlink
resolution security fix, and Codex 0.154.0. It preserves the legacy spelling of
one proc-mount error for Codex's existing fallback; see execution-backends.md.
No helper-path interception or extra credential-home permissions are needed.
The preflight requires --argv0 support and then tests actual enforcement.

Run `scripts/probe_nested_container_sandbox.py --image IMAGE --profile nested`
with this checkout's `src` on PYTHONPATH. `--profile default` reproduces the
outer-policy failure. Both use disposable credential-free workers. Deployment
also requires matching host/worker Git source identity via container smoke.
