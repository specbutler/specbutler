#!/usr/local/bin/python3
"""Root-owned worker compatibility launcher for npm Codex 0.154.0.

Codex's startup filesystem reader re-executes an absolute helper alias below
CODEX_HOME, which must remain hidden. Redirect only that command to the same
native executable installed on the system PATH. All Bubblewrap policy arguments
and command arguments are preserved. Install this file as /usr/local/bin/bwrap;
the real, unmodified Bubblewrap executable must remain at /usr/bin/bwrap.
"""

from __future__ import annotations

import os
import re
import sys

_HELPER = re.compile(
    r"/workspace/(?:provider-homes/codex|outbox/specbutler-sandbox-[^/]+/codex)"
    r"/tmp/arg0/codex-arg0[^/]+/codex-linux-sandbox"
)


def bubblewrap_argv(arguments: list[str]) -> list[str]:
    args = list(arguments)
    if "--" in args:
        command = args.index("--") + 1
        if command < len(args) and _HELPER.fullmatch(args[command]):
            args[command] = "/usr/local/bin/codex-linux-sandbox"
    return ["/usr/bin/bwrap", *args]


if __name__ == "__main__":
    os.execv("/usr/bin/bwrap", bubblewrap_argv(sys.argv[1:]))
