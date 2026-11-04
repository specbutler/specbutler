#!/usr/bin/env python3
"""Retry complete CI installs when pip loses a download or build subprocess."""

from __future__ import annotations

import subprocess
import sys
import time

MAX_ATTEMPTS = 3


def main(args: list[str] | None = None) -> int:
    command = [sys.executable, "-m", "pip", "install", *(sys.argv[1:] if args is None else args)]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Stream pip diagnostics directly into the CI log, including build
        # subprocess output. Each retry starts a fresh pip process.
        result = subprocess.run(command, check=False)
        if result.returncode <= 0:
            # Propagate termination signals instead of restarting cancelled jobs.
            return 128 - result.returncode if result.returncode < 0 else 0
        if attempt == MAX_ATTEMPTS:
            return result.returncode
        delay = attempt * 5
        print(
            f"pip install failed (exit {result.returncode}); retrying in {delay}s "
            f"(attempt {attempt + 1}/{MAX_ATTEMPTS})",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    raise AssertionError("No pip install attempt was made")


if __name__ == "__main__":
    raise SystemExit(main())
