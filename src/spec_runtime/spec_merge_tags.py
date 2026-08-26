#!/usr/bin/env python3
"""Shared helpers for spec/merged/* git tags."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

TAG_PREFIX = "spec/merged/"
MERGE_TAG_SCHEMA_VERSION = 1
_SPEC_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


@dataclass(frozen=True)
class MergeTagProvenance:
    spec_id: str
    merge_commit_sha: str
    pr_number: int | None
    source_branch: str
    actor: str
    timestamp: str
    schema_version: int = MERGE_TAG_SCHEMA_VERSION


def merge_tag_name(spec_id: str) -> str:
    return f"{TAG_PREFIX}{spec_id}"


def utc_timestamp_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_tag_message(provenance: MergeTagProvenance) -> str:
    return json.dumps(
        {
            "actor": provenance.actor,
            "merge_commit_sha": provenance.merge_commit_sha,
            "pr_number": provenance.pr_number,
            "schema_version": provenance.schema_version,
            "source_branch": provenance.source_branch,
            "spec_id": provenance.spec_id,
            "timestamp": provenance.timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_tag_message(message: str) -> tuple[MergeTagProvenance | None, str]:
    raw = message.strip()
    if not raw:
        return None, "tag message is empty"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"tag message is not valid JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "tag message JSON must be an object"

    schema_version = payload.get("schema_version")
    if schema_version != MERGE_TAG_SCHEMA_VERSION:
        return None, (f"unsupported schema_version {schema_version!r}; expected {MERGE_TAG_SCHEMA_VERSION}")

    spec_id = str(payload.get("spec_id", "") or "").strip()
    merge_commit_sha = str(payload.get("merge_commit_sha", "") or "").strip()
    source_branch = str(payload.get("source_branch", "") or "").strip()
    actor = str(payload.get("actor", "") or "").strip()
    timestamp = str(payload.get("timestamp", "") or "").strip()
    pr_number_raw = payload.get("pr_number")

    if not spec_id:
        return None, "spec_id is missing"
    if not merge_commit_sha:
        return None, "merge_commit_sha is missing"
    if not source_branch:
        return None, "source_branch is missing"
    if not actor:
        return None, "actor is missing"
    if not timestamp:
        return None, "timestamp is missing"

    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        return None, f"timestamp is not a valid ISO-8601 value: {exc}"

    if pr_number_raw in (None, ""):
        pr_number = None
    elif isinstance(pr_number_raw, int):
        pr_number = pr_number_raw
    elif isinstance(pr_number_raw, str) and pr_number_raw.isdigit():
        pr_number = int(pr_number_raw)
    else:
        return None, f"pr_number must be an integer or null, got {pr_number_raw!r}"

    return (
        MergeTagProvenance(
            spec_id=spec_id,
            merge_commit_sha=merge_commit_sha,
            pr_number=pr_number,
            source_branch=source_branch,
            actor=actor,
            timestamp=timestamp,
        ),
        "",
    )


def annotated_tag_command(
    tag_name: str,
    target_ref: str,
    message: str,
    *,
    force: bool = False,
) -> list[str]:
    command = ["git", "tag"]
    if force:
        command.append("--force")
    command.extend(["-a", tag_name, target_ref, "-m", message])
    return command


def push_tag_command(tag_name: str, *, force: bool = False) -> list[str]:
    ref = f"refs/tags/{tag_name}"
    if force:
        ref = f"+{ref}"
    return ["git", "push", "origin", ref]


def shellify(command: list[str]) -> str:
    return shlex.join(command)


def spec_id_referenced(text: str, spec_id: str) -> bool:
    if not text.strip() or not spec_id.strip():
        return False
    return any(token == spec_id for token in _SPEC_TOKEN_RE.findall(text))
