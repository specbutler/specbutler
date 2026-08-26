#!/usr/bin/env python3
"""Render specs/specs-history table using batched git status resolution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spec_runtime.config import load_repo_spec_runtime_config, resolve_spec_path
from spec_runtime.spec_metadata import iter_spec_metadata
from spec_runtime.spec_status import collect_git_spec_state, get_spec_status

SPEC_FMT = "%-22s %-14s %-24s %-15s %s\n"


def _render_dependencies(
    repo_root: Path,
    depends_on: tuple[str, ...],
    *,
    config,
    include_merged: bool,
    git_state,
) -> str:
    if include_merged:
        return f"[{', '.join(depends_on)}]"

    visible: list[str] = []
    for dep in depends_on:
        dep_status = get_spec_status(
            repo_root,
            dep,
            resolve_spec_path(repo_root, dep, config=config),
            git_state=git_state,
            config=config,
        )
        if dep_status != "merged":
            visible.append(dep)
    return f"[{', '.join(visible)}]"


def refresh_git_spec_refs(repo_root: Path) -> tuple[bool, str]:
    """Refresh and prune origin refs so table rendering uses current merge/run refs."""
    from spec_runtime.control_plane import (
        DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        GitFetchTimeoutError,
        run_git_fetch_with_timeout,
    )

    try:
        outcome = run_git_fetch_with_timeout(
            ["--quiet", "--tags", "--prune", "origin"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        )
    except GitFetchTimeoutError as exc:
        return False, f"git fetch timed out after {exc.timeout_seconds:.0f}s"

    if outcome.is_success:
        return True, ""

    output = (outcome.stderr or outcome.stdout).strip()
    if not output:
        output = "git fetch failed"
    return False, output


def render_specs_table(repo_root: Path, *, include_merged: bool) -> str:
    repo_config = load_repo_spec_runtime_config(repo_root)
    git_state = collect_git_spec_state(repo_root, config=repo_config)
    records = iter_spec_metadata(repo_root)

    lines = [
        SPEC_FMT % ("SPEC", "AREA", "DEPENDS ON", "STATUS", "DESCRIPTION"),
        SPEC_FMT % ("----", "----", "----------", "------", "-----------"),
    ]
    for record in records:
        if record.obsolete and not include_merged:
            continue
        if record.superseded_by:
            if not include_merged:
                continue
            status = "superseded"
        else:
            spec_path = resolve_spec_path(repo_root, record.spec_id, config=repo_config)
            status = get_spec_status(
                repo_root,
                record.spec_id,
                spec_path,
                git_state=git_state,
                config=repo_config,
            )
            if not include_merged and status in {"merged", "obsolete"}:
                continue

        lines.append(
            SPEC_FMT
            % (
                record.spec_id,
                record.area or "-",
                _render_dependencies(
                    repo_root,
                    record.depends_on,
                    config=repo_config,
                    include_merged=include_merged,
                    git_state=git_state,
                ),
                status,
                record.description,
            )
        )
    return "".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render spec status table.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--include-merged", type=int, choices=(0, 1), default=0)
    parser.add_argument("--refresh-refs", type=int, choices=(0, 1), default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    repo_root = Path(args.repo_root)
    if bool(args.refresh_refs):
        ok, error = refresh_git_spec_refs(repo_root)
        if not ok:
            print(f"warning: {error}", file=sys.stderr)
    table = render_specs_table(repo_root, include_merged=bool(args.include_merged))
    sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
