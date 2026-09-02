#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path

MAX_ARCHIVE_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 1 << 30


class EvidenceArchiveError(RuntimeError):
    pass


def _validated_members(
    archive: zipfile.ZipFile,
    destination: Path,
) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members:
        raise EvidenceArchiveError("guest evidence archive is empty")
    if len(members) > MAX_ARCHIVE_FILES:
        raise EvidenceArchiveError(
            f"guest evidence archive exceeds {MAX_ARCHIVE_FILES} entries"
        )

    validated: list[zipfile.ZipInfo] = []
    names: set[str] = set()
    total_size = 0
    for member in members:
        name = member.filename
        if (
            member.is_dir()
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise EvidenceArchiveError(
                f"guest evidence archive must contain only flat files: {name!r}"
            )
        normalized = name.casefold()
        if normalized in names:
            raise EvidenceArchiveError(
                f"guest evidence archive contains a duplicate file name: {name!r}"
            )
        names.add(normalized)
        if member.flag_bits & 0x1:
            raise EvidenceArchiveError(
                f"guest evidence archive contains an encrypted file: {name!r}"
            )
        file_type = (member.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise EvidenceArchiveError(
                f"guest evidence archive contains a symbolic link: {name!r}"
            )
        total_size += member.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise EvidenceArchiveError(
                "guest evidence archive exceeds the uncompressed size limit"
            )
        if (destination / name).exists():
            raise EvidenceArchiveError(
                f"guest evidence would overwrite an existing artifact: {name!r}"
            )
        validated.append(member)
    return validated


def extract_flat_archive(archive_path: Path, destination: Path) -> list[str]:
    archive_path = archive_path.resolve()
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_members(archive, destination)
            for member in members:
                target = destination / member.filename
                with archive.open(member) as source, target.open("xb") as output:
                    created.append(target)
                    shutil.copyfileobj(source, output)
                if target.stat().st_size != member.file_size:
                    raise EvidenceArchiveError(
                        f"guest evidence size mismatch after extraction: {member.filename!r}"
                    )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise EvidenceArchiveError(f"cannot extract guest evidence archive: {exc}") from exc
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return [path.name for path in created]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely extract a flat Windows-lab evidence archive."
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        extract_flat_archive(args.archive, args.destination)
    except EvidenceArchiveError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
