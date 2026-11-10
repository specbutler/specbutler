"""Shared helpers for ``spec/hold/*`` git tags.

A spec is *held* while the local tag ``refs/tags/spec/hold/<spec-id>`` exists.
``spec hold`` writes an annotated tag whose message is a JSON object
``{schema_version, spec_id, reason, actor, timestamp}`` and pushes it to
``origin``; ``spec unhold`` deletes it there and locally. Tags created by hand
(lightweight, or with a message that is not JSON) still hold the spec; their
reason reads ``no reason recorded``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .git_common import run_git

HOLD_TAG_PREFIX = "spec/hold/"
HOLD_TAG_REF_PREFIX = f"refs/tags/{HOLD_TAG_PREFIX}"
HOLD_TAG_SCHEMA_VERSION = 1
HOLD_TAG_REFSPEC = f"+{HOLD_TAG_REF_PREFIX}*:{HOLD_TAG_REF_PREFIX}*"
NO_REASON_RECORDED = "no reason recorded"


@dataclass(frozen=True)
class HoldTag:
    spec_id: str
    reason: str = ""
    actor: str = ""
    timestamp: str = ""

    @property
    def display_reason(self) -> str:
        return self.reason or NO_REASON_RECORDED

    def describe(self) -> str:
        """Return ``reason (by actor at timestamp)`` for human-facing output."""
        details = []
        if self.actor:
            details.append(f"by {self.actor}")
        if self.timestamp:
            details.append(f"at {self.timestamp}")
        suffix = f" ({' '.join(details)})" if details else ""
        return f"{self.display_reason}{suffix}"


def hold_tag_name(spec_id: str) -> str:
    return f"{HOLD_TAG_PREFIX}{spec_id}"


def hold_tag_ref(spec_id: str) -> str:
    return f"refs/tags/{hold_tag_name(spec_id)}"


def build_hold_message(*, spec_id: str, reason: str, actor: str, timestamp: str) -> str:
    return json.dumps(
        {
            "actor": actor,
            "reason": reason,
            "schema_version": HOLD_TAG_SCHEMA_VERSION,
            "spec_id": spec_id,
            "timestamp": timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_hold_message(spec_id: str, message: str) -> HoldTag:
    """Parse a hold tag message tolerantly.

    A message that is empty, not JSON, or not a JSON object yields a
    ``HoldTag`` with no reason rather than an error: the tag still holds.
    """
    try:
        payload = json.loads(message.strip())
    except (ValueError, RecursionError):
        return HoldTag(spec_id=spec_id)
    if not isinstance(payload, dict):
        return HoldTag(spec_id=spec_id)

    def _field(name: str) -> str:
        value = payload.get(name)
        return value.strip() if isinstance(value, str) else ""

    return HoldTag(
        spec_id=spec_id,
        reason=_field("reason"),
        actor=_field("actor"),
        timestamp=_field("timestamp"),
    )


def read_hold_tags(repo_root: Path) -> dict[str, HoldTag]:
    """Return every local hold tag, parsed, with a single ``for-each-ref`` call."""
    result = run_git(
        [
            "for-each-ref",
            "--format=%(refname)%00%(objecttype)%00%(contents)%00",
            HOLD_TAG_REF_PREFIX,
        ],
        cwd=repo_root,
    )
    if result.returncode != 0:
        return {}
    fields = result.stdout.split("\0")
    holds: dict[str, HoldTag] = {}
    for index in range(0, len(fields) - 2, 3):
        refname = fields[index].lstrip("\r\n")
        objecttype = fields[index + 1]
        contents = fields[index + 2]
        if not refname.startswith(HOLD_TAG_REF_PREFIX):
            continue
        spec_id = refname[len(HOLD_TAG_REF_PREFIX) :]
        if not spec_id:
            continue
        # A lightweight tag points straight at a commit; %(contents) would be
        # that commit's message, not a hold reason.
        message = contents if objecttype == "tag" else ""
        holds[spec_id] = parse_hold_message(spec_id, message)
    return holds


def read_hold_tag(repo_root: Path, spec_id: str) -> HoldTag | None:
    return read_hold_tags(repo_root).get(spec_id)


def refresh_hold_tags(
    repo_root: Path,
    *,
    remote: str = "origin",
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[bool, str]:
    """Mirror ``spec/hold/*`` tags from ``remote``, pruning ones deleted there.

    ``git fetch --tags --prune`` never prunes tags, so a hold removed on
    another checkout would linger. Fetching the hold namespace with an
    explicit refspec and ``--prune`` removes exactly the stale hold tags and
    leaves every other local tag alone. Returns ``(ok, error)``.
    """
    from .control_plane import (
        DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        GitFetchTimeoutError,
        run_git_fetch_with_timeout,
    )

    kwargs: dict[str, object] = {}
    if runner is not None:
        kwargs["runner"] = runner
    try:
        outcome = run_git_fetch_with_timeout(
            ["--quiet", "--prune", "--no-tags", remote, HOLD_TAG_REFSPEC],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
            **kwargs,
        )
    except GitFetchTimeoutError as exc:
        return False, f"hold tag fetch timed out after {exc.timeout_seconds:.0f}s"
    except (OSError, RuntimeError) as exc:
        return False, f"hold tags could not be fetched from {remote}: {exc}"
    if outcome.is_success:
        return True, ""
    detail = (outcome.stderr or outcome.stdout or "").strip() or "git fetch failed"
    return False, f"hold tags could not be fetched from {remote}: {detail}"


def create_hold_tag_command(tag_name: str, target_ref: str, message: str) -> list[str]:
    return ["git", "tag", "--force", "-a", tag_name, target_ref, "-m", message]


def push_hold_tag_command(tag_name: str, *, remote: str = "origin") -> list[str]:
    return ["git", "push", remote, f"+refs/tags/{tag_name}"]


def delete_remote_hold_tag_command(tag_name: str, *, remote: str = "origin") -> list[str]:
    return ["git", "push", remote, f":refs/tags/{tag_name}"]


def delete_local_hold_tag_command(tag_name: str) -> list[str]:
    return ["git", "tag", "-d", tag_name]
