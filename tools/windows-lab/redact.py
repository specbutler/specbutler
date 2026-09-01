#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s\"']+"),
)


def redact_text(payload: str) -> str:
    for pattern in PATTERNS:
        payload = pattern.sub(
            lambda match: match.group(1) + "[REDACTED]"
            if match.lastindex
            else "[REDACTED]",
            payload,
        )
    return payload


def redact(payload: bytes) -> bytes:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return redact_text(payload.decode("utf-16")).encode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return b"\xef\xbb\xbf" + redact_text(payload.decode("utf-8-sig")).encode("utf-8")
    try:
        return redact_text(payload.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        # The evidence archive is expanded before redaction. Preserve remaining
        # binary artifacts byte-for-byte instead of corrupting them.
        return payload


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: redact.py SOURCE DESTINATION")
    source = Path(sys.argv[1]).resolve()
    destination = Path(sys.argv[2]).resolve()
    if source == destination or source in destination.parents:
        raise SystemExit("destination must be outside the raw source tree")
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(redact(path.read_bytes()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
