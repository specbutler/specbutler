#!/usr/bin/env python3
"""Disposable, credential-free experiment for the nested worker profile.

This does not install profiles, change Docker defaults, or launch a model.
Run with the checkout's Python environment so spec_runtime is importable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path

from spec_runtime.container_sandbox import codex_container_probe_command

_OUTER_BOUNDARY_CHECK = """\
import ctypes, errno, os
from pathlib import Path
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert os.getuid() != 0 and os.getgid() != 0
assert int(status['CapEff'], 16) == int(status['CapBnd'], 16) == 0
assert int(status['NoNewPrivs']) == 1
assert Path('/proc/self/attr/current').read_text().strip() == 'specbutler-nested-v1 (enforce)'
libc = ctypes.CDLL(None, use_errno=True)
assert libc.mount(b'tmpfs', b'/workspace/source', b'tmpfs', 0, None) == -1
assert ctypes.get_errno() in (errno.EPERM, errno.EACCES)
with open('/proc/1/ns/mnt') as namespace:
    assert libc.setns(namespace.fileno(), 0) == -1
    assert ctypes.get_errno() in (errno.EPERM, errno.EACCES)
print('SPEC_OUTER_WORKER_BOUNDARY_ENFORCED', flush=True)
"""

_INNER_ESCAPE_CHECK = """\
import subprocess
# A descendant namespace must not uncover the protected file or make an
# inherited read-only mount writable. Use only synthetic fixtures.
attempts = [
    ['unshare', '--user', '--map-root-user', '--mount', 'sh', '-c',
     'umount "$1"; cat "$1"', 'probe', str(denied)],
    ['unshare', '--user', '--map-root-user', '--mount', 'sh', '-c',
     'mount -o remount,rw /; printf changed > "$1"', 'probe', str(outside)],
]
for argv in attempts:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0, 'descendant namespace escaped the inner boundary'
    assert 'synthetic preflight fixture' not in result.stdout
print('SPEC_DESCENDANT_BOUNDARY_ENFORCED', flush=True)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--profile", choices=("default", "nested"), default="default")
    args = parser.parse_args()
    profiles = Path(__file__).resolve().parents[1] / "src/spec_runtime/profiles"
    name = "spec-nested-probe-" + uuid.uuid4().hex[:12]
    argv = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--network=none", "--user=1000:1000", "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--tmpfs=/workspace/source:uid=1000,gid=1000,mode=0700",
        "--tmpfs=/workspace/outbox:uid=1000,gid=1000,mode=0700",
        "--workdir=/workspace/source",
    ]
    if args.profile == "nested":
        argv.extend([
            "--security-opt=apparmor=specbutler-nested-v1",
            "--security-opt=seccomp=" + str(profiles / "specbutler-nested-v1.seccomp.json"),
        ])
    probe = codex_container_probe_command()
    if args.profile == "nested":
        # Execute these checks in the outer worker, before Codex hides procfs.
        probe[2] = _OUTER_BOUNDARY_CHECK + "\n" + probe[2]
        payload = json.loads(probe[3])
        payload[3] += "\n" + _INNER_ESCAPE_CHECK
        probe[3] = json.dumps(payload)
    argv.extend(["--entrypoint", probe[0], args.image, *probe[1:]])
    try:
        return subprocess.run(argv, timeout=30, check=False).returncode
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    raise SystemExit(main())
