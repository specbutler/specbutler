#!/usr/bin/env python3
"""Shared spec frontmatter helpers."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import SpecRuntimeConfig, load_repo_spec_runtime_config, resolve_specs_root
from .spec_identity import SPEC_ID_RE

VALID_SPEC_AREAS = ("frontend", "backend", "fullstack", "orchestrator")
DEFAULT_SPEC_PRIORITY = 50

logger = logging.getLogger(__name__)
_WARNED_LEGACY_STATUS_PATHS: set[Path] = set()


class InvalidSpecIdError(ValueError):
    """Raised when spec frontmatter contains a non-canonical identifier."""


@dataclass(frozen=True)
class SpecMetadata:
    spec_id: str
    obsolete: bool
    depends_on: tuple[str, ...]
    description: str
    superseded_by: str
    area: str
    priority: int
    intake_required: bool
    body: str


def parse_spec_frontmatter(spec_path: Path) -> dict:
    """Parse YAML frontmatter from a spec file."""
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}

    fm_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_spec_body(spec_path: Path) -> str:
    """Return the spec body without YAML frontmatter."""
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text

    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[idx + 1 :]).lstrip("\n")
    return text


def normalize_depends_on(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    value = str(raw).strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        return tuple(entry.strip() for entry in inner.split(",") if entry.strip())
    return (value,) if value else ()


def intake_required_from_frontmatter(frontmatter: dict) -> bool:
    intake_meta = frontmatter.get("intake")
    if isinstance(intake_meta, dict):
        if "required" in intake_meta:
            return _to_bool(intake_meta.get("required"))
        mode = str(intake_meta.get("mode", "")).strip().lower()
        if mode:
            return mode in ("required", "true", "yes", "interactive")
        return True
    if isinstance(intake_meta, str):
        return intake_meta.strip().lower() in ("required", "true", "yes", "interactive")
    if isinstance(intake_meta, bool):
        return intake_meta
    return False


def parse_spec_metadata(spec_path: Path) -> SpecMetadata:
    frontmatter = parse_spec_frontmatter(spec_path)
    spec_id = str(frontmatter.get("id", "")).strip()
    if spec_id and not SPEC_ID_RE.fullmatch(spec_id):
        raise InvalidSpecIdError(
            f"Spec {spec_path} has invalid frontmatter id {spec_id!r}; "
            "use lowercase letters, digits, and hyphens, starting with a letter or digit"
        )

    # Backwards compatibility: if legacy ``status`` is ``obsolete``, treat as obsolete.
    raw_status = str(frontmatter.get("status", "")).strip().lower()
    if raw_status and raw_status != "obsolete":
        try:
            warning_key = spec_path.resolve()
        except OSError:
            warning_key = spec_path
        if warning_key not in _WARNED_LEGACY_STATUS_PATHS:
            _WARNED_LEGACY_STATUS_PATHS.add(warning_key)
            logger.warning(
                "Spec %s has legacy frontmatter `status: %s`; status is derived from run state, so remove this field",
                spec_path,
                raw_status,
            )
    obsolete_flag = _to_bool(frontmatter.get("obsolete", False)) or raw_status == "obsolete"

    return SpecMetadata(
        spec_id=spec_id,
        obsolete=obsolete_flag,
        depends_on=normalize_depends_on(frontmatter.get("depends_on")),
        description=str(frontmatter.get("description", "")).strip(),
        superseded_by=str(frontmatter.get("superseded_by", "")).strip(),
        area=str(frontmatter.get("area", "")).strip().lower(),
        priority=_to_int(frontmatter.get("priority"), default=DEFAULT_SPEC_PRIORITY),
        intake_required=intake_required_from_frontmatter(frontmatter),
        body=parse_spec_body(spec_path),
    )


def iter_spec_metadata(repo_root: Path, *, config: SpecRuntimeConfig | None = None) -> list[SpecMetadata]:
    specs: list[SpecMetadata] = []
    specs_root = resolve_specs_root(repo_root, config=config)
    for spec_path in sorted(specs_root.glob("*.md")):
        if spec_path.name == "TEMPLATE.md":
            continue
        try:
            metadata = parse_spec_metadata(spec_path)
        except InvalidSpecIdError:
            # A malformed committed spec must not become a path, branch, or DOM
            # identifier. Keep discovery usable for the remaining specs while
            # making the rejected file visible to operators.
            logger.warning(
                "Ignoring spec %s because its frontmatter id is not a canonical slug",
                spec_path,
            )
            continue
        if not metadata.spec_id or metadata.spec_id == "<slug>":
            continue
        specs.append(metadata)
    return specs


@dataclass(frozen=True)
class SpecGraphProblem:
    """A ``depends_on`` defect that prevents a spec from ever being dispatched."""

    spec_id: str
    kind: str  # "unknown-dependency" | "dependency-cycle"
    detail: str


def task_spec_ids(repo_root: Path, *, config: SpecRuntimeConfig | None = None) -> set[str]:
    """Ids of task specs, which ``depends_on`` may reference but discovery skips."""
    runtime_config = config or load_repo_spec_runtime_config(repo_root)
    task_root = repo_root / runtime_config.paths.task_specs_dir
    return {path.stem for path in task_root.glob("*.md") if path.name != "TEMPLATE.md"}


def validate_spec_graph(
    specs: Iterable[SpecMetadata],
    *,
    merged_spec_ids: Iterable[str] = (),
    external_spec_ids: Iterable[str] = (),
) -> dict[str, tuple[SpecGraphProblem, ...]]:
    """Find unknown ``depends_on`` ids and dependency cycles among discovered specs.

    ``merged_spec_ids`` are treated as satisfied dependencies even when their spec
    file is no longer discovered, and merged specs are left out of cycle detection
    because their own dependencies no longer gate anything. ``external_spec_ids``
    (task specs) are valid dependency targets outside the discovered catalog.
    """
    specs = list(specs)
    merged = set(merged_spec_ids)
    known = {spec.spec_id for spec in specs}
    external = set(external_spec_ids)
    problems: dict[str, list[SpecGraphProblem]] = {}

    for spec in specs:
        for dep in spec.depends_on:
            if dep not in known and dep not in merged and dep not in external:
                problems.setdefault(spec.spec_id, []).append(
                    SpecGraphProblem(
                        spec.spec_id,
                        "unknown-dependency",
                        f"depends on unknown spec {dep!r}",
                    )
                )

    pending = known - merged
    graph = {
        spec.spec_id: [dep for dep in dict.fromkeys(spec.depends_on) if dep in pending]
        for spec in specs
        if spec.spec_id in pending
    }

    for spec_id in graph:
        cycle = _shortest_cycle_through(graph, spec_id)
        if cycle:
            problems.setdefault(spec_id, []).append(
                SpecGraphProblem(
                    spec_id,
                    "dependency-cycle",
                    "dependency cycle " + " -> ".join(cycle),
                )
            )
    return {spec_id: tuple(items) for spec_id, items in problems.items()}


def format_spec_graph_problems(problems: Iterable[SpecGraphProblem]) -> str:
    return "; ".join(problem.detail for problem in problems)


def _shortest_cycle_through(graph: dict[str, list[str]], start: str) -> list[str]:
    """Return ``[start, ..., start]`` for the shortest dependency cycle through ``start``."""
    parents: dict[str, str] = {}
    queue: deque[str] = deque()
    for dep in graph.get(start, ()):
        if dep == start:
            return [start, start]
        if dep not in parents:
            parents[dep] = start
            queue.append(dep)
    while queue:
        node = queue.popleft()
        for dep in graph.get(node, ()):
            if dep == start:
                path = [node]
                while path[-1] != start:
                    path.append(parents[path[-1]])
                return [*reversed(path), start]
            if dep not in parents:
                parents[dep] = node
                queue.append(dep)
    return []


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "required")
    return bool(value)


def _to_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
