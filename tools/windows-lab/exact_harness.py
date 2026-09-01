#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


class HarnessMismatch(RuntimeError):
    pass


def _git(repo_root: Path, *arguments: str, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() if text else completed.stderr.decode(errors="replace").strip()
        raise HarnessMismatch(f"git {' '.join(arguments)} failed: {stderr}")
    return completed.stdout


def verify_exact_harness(
    repo_root: Path,
    revision: str,
    output_path: Path,
    snapshot_root: Path,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    snapshot_root = snapshot_root.resolve()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise HarnessMismatch("proof revision must be an exact lowercase commit SHA")
    if not snapshot_root.is_dir():
        raise HarnessMismatch(f"snapshot root is unavailable: {snapshot_root}")
    if any(snapshot_root.iterdir()):
        raise HarnessMismatch(f"snapshot root must be empty: {snapshot_root}")
    head = str(_git(repo_root, "rev-parse", "HEAD", text=True)).strip().lower()
    if head != revision:
        raise HarnessMismatch(
            f"proof revision {revision} is not the controller checkout HEAD {head}"
        )
    dirty = str(
        _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "tools/windows-lab",
            text=True,
        )
    ).strip()
    if dirty:
        raise HarnessMismatch(
            "tools/windows-lab differs from the exact proof revision; commit or remove: "
            + ", ".join(line[3:] for line in dirty.splitlines())
        )
    tree_raw = _git(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        revision,
        "--",
        "tools/windows-lab",
    )
    assert isinstance(tree_raw, bytes)
    entries: list[tuple[str, str, str]] = []
    for raw_entry in tree_raw.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise HarnessMismatch("exact revision contains an invalid harness tree entry") from exc
        posix_path = PurePosixPath(relative)
        if (
            object_type != "blob"
            or mode not in {"100644", "100755"}
            or posix_path.is_absolute()
            or ".." in posix_path.parts
            or posix_path.parts[:2] != ("tools", "windows-lab")
        ):
            raise HarnessMismatch(f"unsupported harness tree entry: {relative}")
        entries.append((mode, object_id, relative))
    if not entries:
        raise HarnessMismatch("exact revision contains no Windows lab harness")
    hashes: dict[str, str] = {}
    for mode, object_id, relative in entries:
        committed = _git(repo_root, "cat-file", "blob", object_id)
        assert isinstance(committed, bytes)
        worktree_path = repo_root / relative
        try:
            worktree = worktree_path.read_bytes()
        except OSError as exc:
            raise HarnessMismatch(f"harness file is unavailable: {relative}: {exc}") from exc
        if worktree != committed:
            raise HarnessMismatch(
                f"harness file differs from exact revision: {relative}"
            )
        digest = hashlib.sha256(committed).hexdigest()
        hashes[relative] = digest
        snapshot_path = snapshot_root.joinpath(*PurePosixPath(relative).parts)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(committed)
        os.chmod(snapshot_path, 0o755 if mode == "100755" else 0o644)
        if hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != digest:
            raise HarnessMismatch(f"materialized harness snapshot differs: {relative}")
    result: dict[str, object] = {
        "status": "passed",
        "source_revision": revision,
        "controller_head": head,
        "files_verified": len(hashes),
        "snapshot_materialized": True,
        "snapshot_files_verified": len(hashes),
        "sha256": hashes,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_exact_harness(
            args.repo_root,
            args.revision,
            args.output,
            args.snapshot_root,
        )
    except HarnessMismatch as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
