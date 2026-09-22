# Nested worker profiles

These files are derived from Moby profiles, Apache-2.0, commit
`245180c51918481c0525424b3ee025d2b435d46c`:

- `apparmor/testdata/with-tunables.golden`
- `seccomp/default.json`

Upstream: https://github.com/moby/profiles
The upstream license is included as `LICENSE`.

Spec Butler changes the named AppArmor policy to allow user namespaces,
Bubblewrap's virtual filesystem and bind mounts, remount/propagation operations,
and pivot_root. Remaining Docker AppArmor restrictions are retained.

The seccomp policy adds constrained namespace creation, mount, umount2, and
pivot_root calls. clone3 retains its ENOSYS rule; setns and other privileged
operations retain Docker's restrictions. Kernel capabilities are still checked.

The profiles MUST be paired with non-root numeric uid/gid, `--cap-drop=ALL`, and
`--security-opt=no-new-privileges=true`. They require a Linux x86_64/aarch64 Docker
coordinator and the named AppArmor profile loaded on the daemon host.

These policies deliberately expose more namespace/mount kernel operations than
Docker defaults. They are explicitly selected by `sandbox_profile = "nested-v1"`,
never applied as an automatic recovery or silently selected by a sandbox probe.
Do not change an active run's policy or edit v1 incompatibly after release.
