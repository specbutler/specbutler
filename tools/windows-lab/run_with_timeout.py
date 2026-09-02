#!/usr/bin/env python3
"""Run one host command with a portable, process-group-wide timeout."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence

KILL_GRACE_SECONDS = 5
TIMEOUT_EXIT_CODE = 124


class _ForwardedSignal(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(process: subprocess.Popen[bytes], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        process.poll()
        if not _group_exists(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_group(process: subprocess.Popen[bytes], signum: int) -> bool:
    _signal_group(process, signum)
    if _wait_for_group_exit(process, KILL_GRACE_SECONDS):
        return True
    _signal_group(process, signal.SIGKILL)
    return _wait_for_group_exit(process, KILL_GRACE_SECONDS)


def run(seconds: int, command: Sequence[str]) -> int:
    if os.name != "posix":
        raise RuntimeError("the Windows lab host timeout helper requires POSIX")
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except FileNotFoundError:
        print(f"command is unavailable: {command[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"could not start command: {exc}", file=sys.stderr)
        return 126

    previous_handlers: dict[int, signal.Handlers] = {}

    def forward(signum: int, _frame: object) -> None:
        raise _ForwardedSignal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, forward)
    try:
        try:
            return process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            if not _stop_group(process, signal.SIGTERM):
                print("command process group survived SIGKILL", file=sys.stderr)
                return 125
            return TIMEOUT_EXIT_CODE
        except _ForwardedSignal as exc:
            if not _stop_group(process, exc.signum):
                print("command process group survived SIGKILL", file=sys.stderr)
                return 125
            return 128 + exc.signum
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("usage: run_with_timeout.py SECONDS -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        seconds = int(sys.argv[1])
    except ValueError:
        print("timeout must be a positive integer", file=sys.stderr)
        return 2
    if seconds < 1:
        print("timeout must be a positive integer", file=sys.stderr)
        return 2
    return run(seconds, sys.argv[3:])


if __name__ == "__main__":
    raise SystemExit(main())
