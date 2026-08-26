#!/usr/bin/env python3
"""Shared spec frontmatter helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import resolve_specs_root

VALID_SPEC_AREAS = ("frontend", "backend", "fullstack", "orchestrator")
DEFAULT_SPEC_PRIORITY = 50

logger = logging.getLogger(__name__)
_WARNED_LEGACY_STATUS_PATHS: set[Path] = set()


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
    text = spec_path.read_text()
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
    text = spec_path.read_text()
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
        spec_id=str(frontmatter.get("id", "")).strip(),
        obsolete=obsolete_flag,
        depends_on=normalize_depends_on(frontmatter.get("depends_on")),
        description=str(frontmatter.get("description", "")).strip(),
        superseded_by=str(frontmatter.get("superseded_by", "")).strip(),
        area=str(frontmatter.get("area", "")).strip().lower(),
        priority=_to_int(frontmatter.get("priority"), default=DEFAULT_SPEC_PRIORITY),
        intake_required=intake_required_from_frontmatter(frontmatter),
        body=parse_spec_body(spec_path),
    )


def iter_spec_metadata(repo_root: Path) -> list[SpecMetadata]:
    specs: list[SpecMetadata] = []
    specs_root = resolve_specs_root(repo_root)
    for spec_path in sorted(specs_root.glob("*.md")):
        if spec_path.name == "TEMPLATE.md":
            continue
        metadata = parse_spec_metadata(spec_path)
        if not metadata.spec_id or metadata.spec_id == "<slug>":
            continue
        specs.append(metadata)
    return specs


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
