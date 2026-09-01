#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"(?i)(\btoken\s*[:=]\s*[\"']?)[A-Za-z0-9._~-]{16,}"),
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
    files_processed = 0
    text_files = 0
    changed_files = 0
    residual_matches: list[str] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        sanitized = redact(payload)
        target.write_bytes(sanitized)
        files_processed += 1
        changed_files += int(payload != sanitized)
        try:
            encoding = "utf-16" if sanitized.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            rendered = sanitized.decode(encoding)
        except UnicodeDecodeError:
            continue
        text_files += 1
        if any(
            "[REDACTED]" not in match.group(0)
            for pattern in PATTERNS
            for match in pattern.finditer(rendered)
        ):
            residual_matches.append(relative.as_posix())
    report = {
        "status": "passed" if not residual_matches else "failed",
        "files_processed": files_processed,
        "text_files_scanned": text_files,
        "files_with_replacements": changed_files,
        "recognized_secret_shapes_remaining": residual_matches,
    }
    (destination / "_redaction-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if residual_matches:
        raise SystemExit("recognized secret shape remained after redaction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
