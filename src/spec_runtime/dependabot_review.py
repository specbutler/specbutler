"""Validate a Dependabot PR as a dependency-only update.

This policy deliberately accepts a narrow subset of Dependabot changes. GitHub
workflow updates may only replace a full SHA for the same allowlisted action.
Python package updates may only alter constraints for existing dependencies.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from .git_common import run_git

_ACTION_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:-\s+)?uses:\s+)"
    r"(?P<action>(?:actions/[A-Za-z0-9_.-]+|openai/codex-action))"
    r"@(?P<sha>[0-9a-f]{40})(?P<suffix>\s*(?:#.*)?)$"
)
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]+\])?\s*(?P<constraint>.*)$"
)
_SPECIFIER_SET_RE = re.compile(
    r"\s*(?:(?:~=|==|!=|<=|>=|<|>|===)\s*[A-Za-z0-9*_.+!-]+)"
    r"(?:\s*,\s*(?:~=|==|!=|<=|>=|<|>|===)\s*[A-Za-z0-9*_.+!-]+)*\s*"
)


class DependabotPolicyError(ValueError):
    """Raised when a proposed update exceeds the deterministic policy."""


def _git(repo: Path, *args: str) -> str:
    result = run_git(
        args,
        cwd=repo,
        check=True,
    )
    return result.stdout


def _blob(repo: Path, commit: str, path: str) -> str:
    return _git(repo, "show", f"{commit}:{path}")


def _validate_workflow(repo: Path, base_sha: str, head_sha: str, path: str) -> None:
    diff = _git(
        repo,
        "diff",
        "--unified=0",
        "--no-ext-diff",
        base_sha,
        head_sha,
        "--",
        path,
    )
    hunks: list[tuple[list[str], list[str]]] = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            if in_hunk:
                hunks.append((old_lines, new_lines))
            old_lines = []
            new_lines = []
            in_hunk = True
        elif in_hunk and line.startswith("-"):
            old_lines.append(line[1:])
        elif in_hunk and line.startswith("+"):
            new_lines.append(line[1:])
        elif in_hunk and not line.startswith("\\ No newline at end of file"):
            raise DependabotPolicyError(f"{path}: unexpected workflow diff context")
    if in_hunk:
        hunks.append((old_lines, new_lines))
    if not hunks:
        raise DependabotPolicyError(f"{path}: no action pin changed")

    for old_lines, new_lines in hunks:
        if len(old_lines) != len(new_lines) or not old_lines:
            raise DependabotPolicyError(f"{path}: only existing action pins may change")
        for old_line, new_line in zip(old_lines, new_lines, strict=True):
            old_match = _ACTION_LINE_RE.fullmatch(old_line)
            new_match = _ACTION_LINE_RE.fullmatch(new_line)
            if old_match is None or new_match is None:
                raise DependabotPolicyError(f"{path}: non-action workflow content changed")
            if old_match.group("prefix") != new_match.group("prefix"):
                raise DependabotPolicyError(f"{path}: action step structure changed")
            if old_match.group("action") != new_match.group("action"):
                raise DependabotPolicyError(f"{path}: action identity changed")
            if old_match.group("sha") == new_match.group("sha"):
                raise DependabotPolicyError(f"{path}: action SHA did not change")


def _requirement_identity(value: object, location: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise DependabotPolicyError(f"{location}: dependency must be a string")
    requirement, separator, marker = value.partition(";")
    match = _REQUIREMENT_RE.fullmatch(requirement)
    if match is None:
        raise DependabotPolicyError(f"{location}: unsupported dependency syntax")
    constraint = match.group("constraint")
    if constraint and _SPECIFIER_SET_RE.fullmatch(constraint) is None:
        raise DependabotPolicyError(
            f"{location}: only registry version constraints are allowed"
        )
    name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
    extras = (match.group("extras") or "").replace(" ", "").lower()
    normalized_marker = marker.strip() if separator else ""
    return name, extras, normalized_marker


def _dependency_list(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise DependabotPolicyError(f"{location}: dependency group must be an array")
    return value


def _dependency_lists(document: dict) -> dict[str, list[object]]:
    project = document.get("project", {})
    build_system = document.get("build-system", {})
    if not isinstance(project, dict) or not isinstance(build_system, dict):
        raise DependabotPolicyError("pyproject.toml: project/build-system must be tables")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise DependabotPolicyError("pyproject.toml: optional-dependencies must be a table")
    result = {
        "project.dependencies": _dependency_list(
            project.get("dependencies", []), "project.dependencies"
        ),
        "build-system.requires": _dependency_list(
            build_system.get("requires", []), "build-system.requires"
        ),
    }
    for extra, values in sorted(optional.items()):
        if not isinstance(extra, str):
            raise DependabotPolicyError("pyproject.toml: invalid optional dependency group")
        location = f"project.optional-dependencies.{extra}"
        result[location] = _dependency_list(values, location)
    return result


def _without_dependencies(document: dict) -> dict:
    clean = copy.deepcopy(document)
    project = clean.get("project")
    if isinstance(project, dict):
        project.pop("dependencies", None)
        project.pop("optional-dependencies", None)
    build_system = clean.get("build-system")
    if isinstance(build_system, dict):
        build_system.pop("requires", None)
    return clean


def _validate_pyproject(repo: Path, base_sha: str, head_sha: str) -> None:
    before = tomllib.loads(_blob(repo, base_sha, "pyproject.toml"))
    after = tomllib.loads(_blob(repo, head_sha, "pyproject.toml"))
    if _without_dependencies(before) != _without_dependencies(after):
        raise DependabotPolicyError("pyproject.toml: non-dependency metadata changed")
    before_lists = _dependency_lists(before)
    after_lists = _dependency_lists(after)
    if before_lists.keys() != after_lists.keys():
        raise DependabotPolicyError("pyproject.toml: dependency groups changed")
    changed = False
    for location in before_lists:
        old_values = before_lists[location]
        new_values = after_lists[location]
        old_ids = [_requirement_identity(value, location) for value in old_values]
        new_ids = [_requirement_identity(value, location) for value in new_values]
        if old_ids != new_ids:
            raise DependabotPolicyError(
                f"pyproject.toml: dependency identities changed in {location}"
            )
        changed = changed or old_values != new_values
    if not changed:
        raise DependabotPolicyError("pyproject.toml: no dependency constraint changed")


def validate_dependabot_update(repo: Path, base_sha: str, head_sha: str) -> tuple[str, ...]:
    """Validate and return the changed files in an accepted update."""

    repo = repo.resolve()
    changed_files = tuple(
        path
        for path in _git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=AM",
            base_sha,
            head_sha,
        ).splitlines()
        if path
    )
    all_changed_files = tuple(
        path
        for path in _git(repo, "diff", "--name-only", base_sha, head_sha).splitlines()
        if path
    )
    if not changed_files or changed_files != all_changed_files:
        raise DependabotPolicyError("empty, renamed, or deleted files are not allowed")

    for path in changed_files:
        pure_path = PurePosixPath(path)
        if (
            len(pure_path.parts) == 3
            and pure_path.parts[:2] == (".github", "workflows")
            and pure_path.suffix in {".yml", ".yaml"}
        ):
            _validate_workflow(repo, base_sha, head_sha, path)
        elif path == "pyproject.toml":
            _validate_pyproject(repo, base_sha, head_sha)
        else:
            raise DependabotPolicyError(f"changed file is not allowed: {path}")
    return changed_files


def build_review_payload(base_sha: str, head_sha: str) -> dict:
    """Build a schema-compatible approval for a validated dependency update."""

    return {
        "schema_version": "v1",
        "decision": "approved",
        "summary": (
            "Trusted Dependabot identity and dependency-only diff validated; "
            "required CI and human merge review remain authoritative."
        ),
        "reviewed_base_sha": base_sha,
        "reviewed_head_sha": head_sha,
        "findings": [],
        "reviewer_role": "dependency-update-policy",
        "reviewer_agent": "dependabot",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        changed_files = validate_dependabot_update(args.repo, args.base_sha, args.head_sha)
    except (DependabotPolicyError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        print(f"Dependabot policy rejected update: {exc}")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_review_payload(args.base_sha, args.head_sha), indent=2) + "\n"
    )
    print(f"Validated Dependabot dependency update: {', '.join(changed_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
