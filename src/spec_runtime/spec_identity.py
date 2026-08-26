#!/usr/bin/env python3
"""Helpers for spec branch/worktree identity parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

IMPLEMENTATION_BRANCH_PREFIX = "code/"
TASK_BRANCH_PREFIX = "task/"
LEGACY_SPEC_RUN_BRANCH_PREFIX = "specrun/"
SPEC_AUTHORING_BRANCH_PREFIX = "spec/"
SPEC_AUTHORING_SESSION_BRANCH_PREFIX = "spec-authoring/"
LEGACY_SPECDOC_BRANCH_PREFIX = "specdoc/"
IMPLEMENTATION_WORKTREE_PREFIX = "code-"
TASK_WORKTREE_PREFIX = "task-"
LEGACY_SPEC_RUN_WORKTREE_PREFIX = "specrun-"

IMPLEMENTATION_BRANCH_RE = re.compile(
    r"^code/(?P<spec_id>[a-z0-9][a-z0-9-]*)--(?P<run_token>[A-Za-z0-9][A-Za-z0-9-]*)$"
)
TASK_BRANCH_RE = re.compile(r"^task/(?P<spec_id>[a-z0-9][a-z0-9-]*)--(?P<run_token>[A-Za-z0-9][A-Za-z0-9-]*)$")
SPEC_RUN_BRANCH_RE = re.compile(r"^specrun/(?P<spec_id>[a-z0-9][a-z0-9-]*)--(?P<run_token>[A-Za-z0-9][A-Za-z0-9-]*)$")
SPEC_AUTHORING_BRANCH_RE = re.compile(r"^spec/(?P<spec_id>[a-z0-9][a-z0-9-]*)$")
SPEC_AUTHORING_SESSION_BRANCH_RE = re.compile(r"^spec-authoring/(?P<run_token>[A-Za-z0-9][A-Za-z0-9-]*)$")
SPECDOC_BRANCH_RE = re.compile(r"^specdoc/(?P<spec_id>[a-z0-9][a-z0-9-]*)$")
SPEC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PR_BODY_SPEC_ID_RE = re.compile(r"(?im)^Spec-ID:\s*(?P<spec_id>[a-z0-9][a-z0-9-]*)\s*$")
PR_BODY_REVIEW_OWNER_RE = re.compile(r"(?im)^Review-Owner:\s*(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)\s*$")
LOCAL_REVIEW_OWNER = "local"


@dataclass(frozen=True)
class BranchIdentity:
    kind: str
    spec_id: str
    run_token: str = ""


def spec_run_branch(spec_id: str, run_token: str) -> str:
    return f"{IMPLEMENTATION_BRANCH_PREFIX}{spec_id}--{run_token}"


def spec_run_worktree_name(spec_id: str, run_token: str) -> str:
    return f"{IMPLEMENTATION_WORKTREE_PREFIX}{spec_id}--{run_token}"


def specdoc_branch(spec_id: str) -> str:
    return f"{SPEC_AUTHORING_BRANCH_PREFIX}{spec_id}"


def classify_pr_head_ref(branch: str, body: str = "") -> str:
    """Classify a PR head ref as implementation, task, authoring, or other.

    ``spec/<id>`` is always spec-authoring. ``code/<id>--*`` and
    ``spec-authoring/<token>`` are authoring branches. ``code/<id>--*`` and
    ``specrun/<id>--*`` are implementation branches. ``task/<id>--*`` branches
    are task branches (no spec file required).
    """
    identity = implementation_branch_identity(branch)
    if identity is not None:
        if identity.kind == "task":
            return "task"
        return "implementation"

    if authoring_branch_identity(branch) is not None:
        return "authoring"

    return "other"


def implementation_branch_identity(branch: str) -> BranchIdentity | None:
    branch = branch.strip()
    code = IMPLEMENTATION_BRANCH_RE.match(branch)
    if code:
        return BranchIdentity(
            kind="code",
            spec_id=code.group("spec_id"),
            run_token=code.group("run_token"),
        )

    task = TASK_BRANCH_RE.match(branch)
    if task:
        return BranchIdentity(
            kind="task",
            spec_id=task.group("spec_id"),
            run_token=task.group("run_token"),
        )

    specrun = SPEC_RUN_BRANCH_RE.match(branch)
    if specrun:
        return BranchIdentity(
            kind="specrun",
            spec_id=specrun.group("spec_id"),
            run_token=specrun.group("run_token"),
        )

    return None


def authoring_branch_identity(branch: str) -> BranchIdentity | None:
    branch = branch.strip()
    match = SPEC_AUTHORING_BRANCH_RE.match(branch)
    if match:
        return BranchIdentity(kind="spec", spec_id=match.group("spec_id"))

    match = SPEC_AUTHORING_SESSION_BRANCH_RE.match(branch)
    if match:
        return BranchIdentity(
            kind="spec-session",
            spec_id="",
            run_token=match.group("run_token"),
        )

    match = SPECDOC_BRANCH_RE.match(branch)
    if not match:
        return None
    return BranchIdentity(kind="specdoc", spec_id=match.group("spec_id"))


def specdoc_branch_identity(branch: str) -> BranchIdentity | None:
    return authoring_branch_identity(branch)


def is_authoring_branch(branch: str) -> bool:
    return authoring_branch_identity(branch) is not None


def is_implementation_branch(branch: str) -> bool:
    return implementation_branch_identity(branch) is not None


def is_specdoc_branch(branch: str) -> bool:
    return is_authoring_branch(branch)


def spec_id_from_implementation_branch(branch: str) -> str | None:
    identity = implementation_branch_identity(branch)
    return identity.spec_id if identity else None


def parse_worktree_name(name: str) -> BranchIdentity | None:
    if name.startswith(IMPLEMENTATION_WORKTREE_PREFIX):
        branch = f"{IMPLEMENTATION_BRANCH_PREFIX}{name.removeprefix(IMPLEMENTATION_WORKTREE_PREFIX)}"
        return implementation_branch_identity(branch)
    if name.startswith(TASK_WORKTREE_PREFIX):
        branch = f"{TASK_BRANCH_PREFIX}{name.removeprefix(TASK_WORKTREE_PREFIX)}"
        return implementation_branch_identity(branch)
    if name.startswith(LEGACY_SPEC_RUN_WORKTREE_PREFIX):
        branch = f"{LEGACY_SPEC_RUN_BRANCH_PREFIX}{name.removeprefix(LEGACY_SPEC_RUN_WORKTREE_PREFIX)}"
        return implementation_branch_identity(branch)
    if SPEC_ID_RE.fullmatch(name):
        return BranchIdentity(kind="legacy-worktree", spec_id=name)
    return None


def extract_spec_id_from_pr_body(body: str) -> str | None:
    match = PR_BODY_SPEC_ID_RE.search(body or "")
    if not match:
        return None
    return match.group("spec_id")


def extract_review_owner_from_pr_body(body: str) -> str | None:
    match = PR_BODY_REVIEW_OWNER_RE.search(body or "")
    if not match:
        return None
    owner = match.group("owner").strip().lower()
    return owner or None


def pr_body_uses_local_review(body: str) -> bool:
    return extract_review_owner_from_pr_body(body) == LOCAL_REVIEW_OWNER


def format_pr_review_owner(owner: str = LOCAL_REVIEW_OWNER) -> str:
    normalized = str(owner or "").strip().lower()
    if not SPEC_ID_RE.fullmatch(normalized):
        raise ValueError(f"Invalid PR review owner: {owner!r}")
    return f"Review-Owner: {normalized}"


def resolve_spec_id_for_pr(head_ref: str, body: str) -> str | None:
    explicit = extract_spec_id_from_pr_body(body)
    if explicit:
        return explicit
    return spec_id_from_implementation_branch(head_ref)
