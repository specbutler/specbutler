#!/usr/bin/env python3
"""Resolve a safe Docker memory limit for the Windows lab's QEMU process."""

from __future__ import annotations

import argparse
import re
import sys

MIB = 1024**2
GIB = 1024**3
MINIMUM_HEADROOM = 4 * GIB


class MemoryConfigError(ValueError):
    """Raised when guest and container memory settings are incompatible."""


def _parse_size(value: str, *, default_unit: str, setting: str) -> int:
    normalized = str(value).strip()
    match = re.fullmatch(r"([1-9][0-9]*)([bBkKmMgGtT]?)", normalized)
    if match is None:
        raise MemoryConfigError(
            f"{setting} must be a positive integer with an optional B, K, M, G, or T suffix"
        )
    amount = int(match.group(1))
    unit = (match.group(2) or default_unit).upper()
    multipliers = {"B": 1, "K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}
    return amount * multipliers[unit]


def _format_size(size_bytes: int) -> str:
    for suffix, multiplier in (("T", 1024 * GIB), ("G", GIB), ("M", MIB)):
        if size_bytes % multiplier == 0:
            return f"{size_bytes // multiplier}{suffix}"
    return str(size_bytes)


def minimum_container_limit(guest_memory: str) -> int:
    # QEMU interprets an unsuffixed -m value as MiB. Reserve at least 4 GiB,
    # growing to 25% for larger guests, then give Compose a whole-GiB limit.
    guest_bytes = _parse_size(
        guest_memory,
        default_unit="M",
        setting="LAB_MEMORY",
    )
    headroom = max(MINIMUM_HEADROOM, (guest_bytes + 3) // 4)
    required = guest_bytes + headroom
    return ((required + GIB - 1) // GIB) * GIB


def resolve_container_limit(guest_memory: str, configured_limit: str = "") -> str:
    minimum = minimum_container_limit(guest_memory)
    if not configured_limit.strip():
        return _format_size(minimum)
    limit = _parse_size(
        configured_limit,
        default_unit="B",
        setting="LAB_CONTAINER_MEMORY_LIMIT",
    )
    if limit < minimum:
        raise MemoryConfigError(
            "LAB_CONTAINER_MEMORY_LIMIT must be at least "
            f"{_format_size(minimum)} for LAB_MEMORY={guest_memory}"
        )
    return _format_size(limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest-memory", required=True)
    parser.add_argument("--container-limit", default="")
    args = parser.parse_args(argv)
    try:
        resolved = resolve_container_limit(args.guest_memory, args.container_limit)
    except MemoryConfigError as exc:
        print(f"windows-lab memory configuration: {exc}", file=sys.stderr)
        return 2
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
