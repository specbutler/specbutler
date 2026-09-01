#!/usr/bin/env python3
"""Safely bound retired Windows lab overlays for automated proof runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

RETIRED_NAME_RE = re.compile(
    r"(?P<timestamp>[0-9]{8}T[0-9]{6})-(?P<pid>[1-9][0-9]*)"
)
REQUIRED_ENTRIES = frozenset({"run.qcow2", "nvram.fd", "tpm"})


class TrashRetentionError(RuntimeError):
    """The trash root was not safe enough for automated deletion."""


def parse_keep(value: str) -> int:
    """Parse a positive retention count without accepting signs or whitespace."""
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("keep must be a positive integer")
    return int(value)


def _require_plain_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise TrashRetentionError(f"could not inspect retired entry: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise TrashRetentionError(f"retired entry is not a plain file: {path}")


def _require_plain_directory(path: Path, *, allow_mount: bool = False) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise TrashRetentionError(f"could not inspect retired directory: {path}") from exc
    if (
        stat.S_ISLNK(mode)
        or not stat.S_ISDIR(mode)
        or (not allow_mount and os.path.ismount(path))
    ):
        raise TrashRetentionError(f"retired entry is not a plain directory: {path}")


def _validate_tpm_tree(tpm: Path) -> None:
    pending = [tpm]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                descendants = list(entries)
        except OSError as exc:
            raise TrashRetentionError(
                f"could not enumerate retired TPM state: {directory}"
            ) from exc
        for entry in descendants:
            descendant = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise TrashRetentionError(
                    f"could not inspect retired TPM entry: {descendant}"
                ) from exc
            if stat.S_ISLNK(mode):
                raise TrashRetentionError(
                    f"retired TPM state contains a symlink: {descendant}"
                )
            if stat.S_ISDIR(mode):
                if os.path.ismount(descendant):
                    raise TrashRetentionError(
                        f"retired TPM state contains a mount: {descendant}"
                    )
                pending.append(descendant)
            elif not stat.S_ISREG(mode):
                raise TrashRetentionError(
                    f"retired TPM state contains an unknown entry type: {descendant}"
                )


def _validate_retired_directory(path: Path, trash_root: Path) -> tuple[str, int]:
    name_match = RETIRED_NAME_RE.fullmatch(path.name)
    if path.parent != trash_root or name_match is None:
        raise TrashRetentionError(f"unknown entry under trash root: {path}")
    timestamp = name_match.group("timestamp")
    try:
        datetime.strptime(timestamp, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise TrashRetentionError(f"unknown entry under trash root: {path}") from exc
    _require_plain_directory(path)
    try:
        children = {child.name: child for child in path.iterdir()}
    except OSError as exc:
        raise TrashRetentionError(f"could not enumerate retired directory: {path}") from exc
    if set(children) != REQUIRED_ENTRIES:
        unexpected = sorted(set(children) - REQUIRED_ENTRIES)
        missing = sorted(REQUIRED_ENTRIES - set(children))
        details = []
        if unexpected:
            details.append(f"unknown={','.join(unexpected)}")
        if missing:
            details.append(f"missing={','.join(missing)}")
        raise TrashRetentionError(
            f"retired directory has an unsafe layout: {path} ({'; '.join(details)})"
        )

    _require_plain_file(children["run.qcow2"])
    _require_plain_file(children["nvram.fd"])
    tpm = children["tpm"]
    _require_plain_directory(tpm)
    _validate_tpm_tree(tpm)
    # reset_lab owns this immutable timestamp/PID naming contract. Directory
    # mtimes are mutable metadata and must not influence recovery retention.
    return timestamp, int(name_match.group("pid"))


def apply_retention(*, trash_root: Path, keep: int, apply: bool) -> dict[str, Any]:
    """Validate every direct child, then optionally remove all but the newest."""
    if keep < 1:
        raise TrashRetentionError("keep must be a positive integer")
    if not trash_root.is_absolute():
        raise TrashRetentionError("trash root must be an absolute path")
    # The configured trash root may intentionally live on its own filesystem.
    # Mounted descendants remain forbidden so deletion cannot cross a boundary.
    _require_plain_directory(trash_root, allow_mount=True)
    try:
        children = list(trash_root.iterdir())
    except OSError as exc:
        raise TrashRetentionError(f"could not enumerate trash root: {trash_root}") from exc

    # Validate the complete set before deleting the first byte. An unknown or
    # partially written entry is operator-owned recovery state and must make
    # automated retention fail closed.
    ordered = [
        item[1]
        for item in sorted(
            ((_validate_retired_directory(path, trash_root), path) for path in children),
            key=lambda item: item[0],
            reverse=True,
        )
    ]
    retained = ordered[:keep]
    candidates = ordered[keep:]
    if apply:
        for path in candidates:
            # Narrow the inspection-to-delete window by validating the target
            # again immediately before removal.
            _validate_retired_directory(path, trash_root)
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise TrashRetentionError(
                    f"could not remove retired directory: {path}"
                ) from exc
    return {
        "status": "passed",
        "trash_root": str(trash_root),
        "keep": keep,
        "applied": apply,
        "retained": [path.name for path in retained],
        "removed": [path.name for path in candidates] if apply else [],
        "would_remove": [path.name for path in candidates],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trash-root", required=True, type=Path)
    parser.add_argument("--keep", default=1, type=parse_keep)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = apply_retention(
            trash_root=args.trash_root,
            keep=args.keep,
            apply=args.apply,
        )
    except TrashRetentionError as exc:
        print(f"Windows lab trash retention failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
