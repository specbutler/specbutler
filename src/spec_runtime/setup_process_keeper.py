"""POSIX setup-command keeper used by descendant-preserving execution.

This module is launched as a private subprocess. It stays alive as the exact
session/process-group leader after the repository setup command exits. The
parent retains the write end of a pipe; EOF means the parent disappeared and
the keeper must tear down the complete group rather than orphan services.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _publish_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ignore_sigterm(_signum: int, _frame: object) -> None:
    # The keeper must survive the graceful group signal long enough for the
    # parent (or its own EOF handler) to deliver the bounded hard escalation.
    return


def _terminate_own_group() -> None:
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    time.sleep(0.25)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except (OSError, ValueError):
        os._exit(1)


def _watch_parent(control_fd: int, released: threading.Event) -> None:
    """Kill the complete group on parent EOF, even while setup is running."""
    try:
        action = os.read(control_fd, 1)
    except OSError:
        action = b""
    finally:
        try:
            os.close(control_fd)
        except OSError:
            pass
    if action == b"R":
        released.set()
        return
    _terminate_own_group()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4 or arguments[2] != "--":
        return 64
    status_path = Path(arguments[0])
    try:
        control_fd = int(arguments[1])
    except ValueError:
        return 64
    command = arguments[3:]
    if not command:
        return 64

    signal.signal(signal.SIGTERM, _ignore_sigterm)
    released = threading.Event()
    parent_watcher = threading.Thread(
        target=_watch_parent,
        args=(control_fd, released),
        name="spec-setup-parent-watch",
        daemon=True,
    )
    parent_watcher.start()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        try:
            _publish_status(
                status_path,
                {
                    "schema": 1,
                    "launch_error": {
                        "errno": exc.errno,
                        "filename": exc.filename,
                        "message": str(exc),
                    },
                },
            )
        except OSError:
            _terminate_own_group()
    else:
        returncode = int(process.wait())
        try:
            _publish_status(
                status_path,
                {
                    "schema": 1,
                    "returncode": returncode,
                },
            )
        except OSError:
            _terminate_own_group()

    released.wait()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
