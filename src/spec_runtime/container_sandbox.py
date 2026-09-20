"""Model-free enforcement checks for the Codex policy in Linux workers.

The script is sent to the worker rather than imported there: a cached image may
contain an older Spec Butler. No provider credentials or model call are needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agent_adapter import _codex_implement_permission_overrides

SANDBOX_PROBE_MARKER = "SPEC_CODEX_CONTAINER_SANDBOX_ENFORCED"
SANDBOX_PROBE_TIMEOUT = 25.0
_DENIED_PLACEHOLDER = "/__SPECBUTLER_PROBE_DENIED__"


class ContainerSandboxUnavailableError(RuntimeError):
    """An environment blocker, never an implementation retry."""

_CHILD_SCRIPT = """\
from pathlib import Path
import sys
workspace, outbox, denied, outside = map(Path, sys.argv[1:])
(workspace / 'write.ok').write_text('ok')
(outbox / 'write.ok').write_text('ok')
try:
    denied.read_bytes()
except (PermissionError, FileNotFoundError):
    pass
else:
    raise SystemExit('sandbox allowed a protected read')
try:
    outside.write_text('changed')
except (PermissionError, OSError):
    pass
else:
    raise SystemExit('sandbox allowed an external write')
print('SPEC_CODEX_CONTAINER_SANDBOX_ENFORCED')
"""

_SETUP_SCRIPT = """\
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

config_args, workspace, outbox, child_script = json.loads(sys.argv[1])
with contextlib.ExitStack() as stack:
    def scratch(parent=None):
        return Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix='specbutler-sandbox-', dir=parent)))
    writable = scratch(workspace)
    state = scratch(outbox)
    external = scratch()
    home = external / 'codex'
    home.mkdir()
    denied = writable / 'protected.txt'
    outside = external / 'readonly.txt'
    denied.write_text('synthetic preflight fixture')
    outside.write_text('original')
    # Prove denial cannot be explained by absent files or baseline OS permissions.
    assert denied.read_text() == 'synthetic preflight fixture'
    outside.write_text('original')
    config_args = [arg.replace('/__SPECBUTLER_PROBE_DENIED__', str(denied))
                   for arg in config_args]
    argv = ['codex', 'sandbox', '-C', workspace, '--include-managed-config',
            *config_args, '-P', 'specbutler-implement', sys.executable, '-c',
            child_script, str(writable), str(state), str(denied), str(outside)]
    env = {key: os.environ[key] for key in ('PATH', 'LANG') if key in os.environ}
    env.update(HOME=str(external), CODEX_HOME=str(home))
    try:
        result = subprocess.run(argv, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print('Codex sandbox enforcement probe failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
    if result.returncode:
        raise SystemExit(1)
    if (not (writable / 'write.ok').is_file() or not (state / 'write.ok').is_file()
            or outside.read_text() != 'original'
            or 'SPEC_CODEX_CONTAINER_SANDBOX_ENFORCED' not in result.stdout):
        raise SystemExit('Codex sandbox enforcement probe did not prove the boundary')
"""


def codex_container_probe_command() -> list[str]:
    """Use the same permission builder as implementation, plus a synthetic deny."""
    workspace = Path("/workspace/source")
    outbox = Path("/workspace/outbox")
    overrides = _codex_implement_permission_overrides(
        workspace,
        [outbox],
        provider_home=Path("/workspace/provider-homes/codex"),
        additional_protected_paths=(Path(_DENIED_PLACEHOLDER),),
    )
    config_args = [
        item
        for index, value in enumerate(overrides[:-1])
        if value == "-c"
        for item in ("-c", overrides[index + 1])
    ]
    # The command runs in Linux even when constructed on a Windows coordinator.
    for remote in ("/workspace/source", "/workspace/outbox",
                   "/workspace/provider-homes/codex", _DENIED_PLACEHOLDER):
        local = json.dumps(str(Path(remote).resolve()))
        config_args = [arg.replace(local, json.dumps(remote)) for arg in config_args]
    return [
        "python3", "-c", _SETUP_SCRIPT,
        json.dumps([config_args, "/workspace/source", "/workspace/outbox", _CHILD_SCRIPT]),
    ]


def sandbox_probe_failure(returncode: int, stdout: str, stderr: str) -> str:
    if returncode == 0 and SANDBOX_PROBE_MARKER in stdout:
        return ""
    detail = " ".join((stderr or stdout).split())[:1000] or f"exit status {returncode}"
    return (
        "Container Codex sandbox preflight failed: " + detail + ". "
        "The worker cannot enforce the required write and deny-read profile. "
        "Use the worktree backend with its normal provider sandbox via a scoped "
        "SPEC_CONFIG override for a new run, or repair the worker's namespace support and rerun "
        "spec container smoke. No implementation was launched; retrying the same "
        "worker or installing bubblewrap alone will not repair this failure."
    )
