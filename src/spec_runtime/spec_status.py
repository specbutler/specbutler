#!/usr/bin/env python3
"""Shared spec status helpers.

Status is resolved from stable merge tags plus run state:
- merged:           tag refs/tags/spec/merged/<spec-id> reachable from the configured base ref
- in-progress:      active run record for the spec or remote implementation branch/ref
- needs-attention:  spec requires intake but has no intake-complete run to resume
- obsolete:         spec frontmatter contains ``obsolete: true``

Stale local branches/worktrees without an active run record are surfaced as
diagnostics and do not change the primary status.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import SpecRuntimeConfig, load_repo_spec_runtime_config, load_spec_runtime_config, resolve_spec_path
from .git_common import resolve_common_root as _resolve_common_root
from .git_common import run_git
from .spec_identity import SPEC_ID_RE, implementation_branch_identity, parse_worktree_name
from .spec_metadata import SpecMetadata, parse_spec_metadata

VALID_STATUSES = ("not-started", "in-progress", "needs-attention", "merged", "obsolete")
ACTIVE_RUN_STATUSES = frozenset(("pending", "running", "failed", "blocked", "waiting-for-input"))
LIVE_ACTIVE_RUN_STATUSES = frozenset(("pending", "running"))
TERMINAL_RUN_STATUSES = frozenset(("passed", "abandoned", "superseded"))
SPEC_RUNTIME_CONFIG = load_spec_runtime_config(require=False)


def _runtime_config(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> SpecRuntimeConfig:
    return config or load_repo_spec_runtime_config(repo_root)


@dataclass(frozen=True)
class ActiveRunRecord:
    run_id: str
    spec_id: str
    branch: str
    worktree_path: str
    status: str
    phase: str = ""


@dataclass(frozen=True)
class StoredRunRecord:
    run_id: str
    spec_id: str
    branch: str
    worktree_path: str
    status: str
    phase: str
    superseded: bool
    created_at: str
    updated_at: str


@dataclass
class SpecGitState:
    """Batched git/run state used for fast status resolution."""

    merged_specs: frozenset[str]
    active_run_specs: frozenset[str]
    remote_in_progress_specs: frozenset[str]
    active_runs_by_spec: dict[str, tuple[ActiveRunRecord, ...]]
    orphaned_local_branches_by_spec: dict[str, tuple[str, ...]]
    orphaned_local_worktrees_by_spec: dict[str, tuple[str, ...]]

    def is_in_progress(self, spec_id: str) -> bool:
        return spec_id in self.active_run_specs or spec_id in self.remote_in_progress_specs

    def orphaned_artifacts(self, spec_id: str) -> tuple[str, ...]:
        return self.orphaned_local_branches_by_spec.get(spec_id, ()) + self.orphaned_local_worktrees_by_spec.get(
            spec_id, ()
        )


def _git(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_git(
        args,
        cwd=cwd,
    )


def _git_lines(*args: str, cwd: Path) -> list[str]:
    result = _git(*args, cwd=cwd)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _strip_ref_prefix(refs: list[str], prefix: str) -> set[str]:
    return {ref[len(prefix) :] for ref in refs if ref.startswith(prefix)}


def _default_worktree_path(
    repo_root: Path,
    spec_id: str,
    branch: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> str:
    common_root = _resolve_common_root(repo_root)
    runtime_config = _runtime_config(repo_root, config=config)
    identity = implementation_branch_identity(branch)
    if identity and identity.run_token:
        worktree_name = (
            f"specrun-{identity.spec_id}--{identity.run_token}"
            if identity.kind == "specrun"
            else f"code-{identity.spec_id}--{identity.run_token}"
        )
        return str(common_root / runtime_config.paths.worktrees_dir / worktree_name)
    return str(common_root / runtime_config.paths.worktrees_dir / spec_id)


def _load_active_runs(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> dict[str, tuple[ActiveRunRecord, ...]]:
    runtime_config = _runtime_config(repo_root, config=config)
    runs_dir = _state_runs_dir(repo_root, config=runtime_config)
    if not runs_dir.exists():
        return {}

    active: dict[str, list[ActiveRunRecord]] = {}
    for candidate in runs_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            continue

        status = str(data.get("status", "")).strip()
        spec_id = str(data.get("spec_id", "")).strip()
        if not spec_id or status not in ACTIVE_RUN_STATUSES:
            continue

        branch = str(data.get("branch", "")).strip()
        worktree_path = str(data.get("worktree_path", "")).strip() or _default_worktree_path(
            repo_root,
            spec_id,
            branch,
            config=runtime_config,
        )
        record = ActiveRunRecord(
            run_id=str(data.get("run_id", candidate.stem)).strip(),
            spec_id=spec_id,
            branch=branch,
            worktree_path=worktree_path,
            status=status,
            phase=str(data.get("phase", "")).strip(),
        )
        active.setdefault(spec_id, []).append(record)

    return {
        spec_id: tuple(sorted(records, key=lambda record: record.run_id, reverse=True))
        for spec_id, records in active.items()
    }


def _collect_remote_in_progress_specs(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> set[str]:
    refs = _git_lines(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/remotes/origin/code",
        "refs/remotes/origin/specrun",
        cwd=repo_root,
    )
    if not refs:
        return set()
    # A branch whose head is already reachable from the base ref carries zero
    # unique commits: bootstrap residue from a run that died before pushing
    # any work. Counting it as in-progress parks the spec forever: autopilot
    # never dispatches it as not-started and no run record exists to surface
    # the problem.
    base_ref = _runtime_config(repo_root, config=config).base_ref
    empty = set(
        _git_lines(
            "for-each-ref",
            "--merged",
            base_ref,
            "--format=%(refname:short)",
            "refs/remotes/origin/code",
            "refs/remotes/origin/specrun",
            cwd=repo_root,
        )
    )
    specs: set[str] = set()
    for ref in refs:
        if ref in empty:
            continue
        short = ref.removeprefix("origin/")
        identity = implementation_branch_identity(short)
        if identity:
            specs.add(identity.spec_id)
    return specs


def _state_runs_dir(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> Path:
    runtime_config = _runtime_config(repo_root, config=config)
    return _resolve_common_root(repo_root) / runtime_config.paths.state_dir / "runs"


def _load_runs_for_spec(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> tuple[StoredRunRecord, ...]:
    runtime_config = _runtime_config(repo_root, config=config)
    runs_dir = _state_runs_dir(repo_root, config=runtime_config)
    if not runs_dir.exists():
        return ()

    runs: list[StoredRunRecord] = []
    for candidate in runs_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            continue

        if str(data.get("spec_id", "")).strip() != spec_id:
            continue

        branch = str(data.get("branch", "")).strip()
        runs.append(
            StoredRunRecord(
                run_id=str(data.get("run_id", candidate.stem)).strip(),
                spec_id=spec_id,
                branch=branch,
                worktree_path=str(data.get("worktree_path", "")).strip()
                or _default_worktree_path(
                    repo_root,
                    spec_id,
                    branch,
                    config=runtime_config,
                ),
                status=str(data.get("status", "")).strip(),
                phase=str(data.get("phase", "")).strip(),
                superseded=(
                    str(data.get("status", "")).strip() == "superseded"
                    or bool(str(data.get("superseded_by", "")).strip())
                ),
                created_at=str(data.get("created_at", "")).strip(),
                updated_at=str(data.get("updated_at", "")).strip(),
            )
        )

    runs.sort(
        key=lambda record: (record.created_at, record.updated_at, record.run_id),
        reverse=True,
    )
    return tuple(runs)


def _latest_non_superseded_run(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> StoredRunRecord | None:
    return next(_iter_non_superseded_runs(repo_root, spec_id, config=config), None)


def _iter_non_superseded_runs(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
):
    for run in _load_runs_for_spec(repo_root, spec_id, config=config):
        if not run.superseded:
            yield run


def _load_run_payload(
    repo_root: Path,
    run_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> dict | None:
    payload = _state_runs_dir(repo_root, config=config) / f"{run_id}.json"
    if not payload.exists():
        return None
    try:
        data = json.loads(payload.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_run_spec_path(
    repo_root: Path,
    run_id: str,
    *,
    spec_id: str = "",
    spec_path: Path | None = None,
    config: SpecRuntimeConfig | None = None,
) -> tuple[str, Path | None]:
    runtime_config = _runtime_config(repo_root, config=config)
    resolved_spec_id = spec_id.strip()
    resolved_spec_path = spec_path
    run_payload = None

    if not resolved_spec_id or resolved_spec_path is None:
        run_payload = _load_run_payload(repo_root, run_id, config=runtime_config)

    if not resolved_spec_id and run_payload is not None:
        resolved_spec_id = str(run_payload.get("spec_id", "")).strip()

    if resolved_spec_path is None and run_payload is not None:
        raw_spec_path = str(run_payload.get("spec_path", "")).strip()
        if raw_spec_path:
            candidate = Path(raw_spec_path)
            resolved_spec_path = candidate if candidate.is_absolute() else repo_root / candidate

    if resolved_spec_path is None and resolved_spec_id:
        candidate = resolve_spec_path(repo_root, resolved_spec_id, config=runtime_config)
        if candidate.exists():
            resolved_spec_path = candidate

    return resolved_spec_id, resolved_spec_path


def _load_orchestrator_intake_helpers():
    from .orchestrator import (
        INTAKE_FILE_VERSION,
        IntakeResult,
        _validate_intake_answers,
        parse_intake_spec,
    )

    return (
        INTAKE_FILE_VERSION,
        IntakeResult,
        _validate_intake_answers,
        parse_intake_spec,
    )


def run_has_completed_intake(
    repo_root: Path,
    run_id: str,
    *,
    spec_id: str = "",
    spec_path: Path | None = None,
    config: SpecRuntimeConfig | None = None,
) -> bool:
    runtime_config = _runtime_config(repo_root, config=config)
    resolved_spec_id, resolved_spec_path = _resolve_run_spec_path(
        repo_root,
        run_id,
        spec_id=spec_id,
        spec_path=spec_path,
        config=runtime_config,
    )
    if not resolved_spec_id or resolved_spec_path is None or not resolved_spec_path.exists():
        return False

    (
        intake_file_version,
        intake_result_cls,
        validate_intake_answers,
        parse_intake_spec,
    ) = _load_orchestrator_intake_helpers()
    intake_result = intake_result_cls.load(repo_root, run_id)
    if intake_result is None or not str(intake_result.completed_at).strip():
        return False
    if intake_result.run_id and intake_result.run_id != run_id:
        return False
    if intake_result.spec_id and intake_result.spec_id != resolved_spec_id:
        return False

    try:
        intake_spec = parse_intake_spec(resolved_spec_path)
    except ValueError:
        return False

    if not intake_spec.required:
        return False
    if intake_result.version != intake_file_version:
        return False
    if intake_result.schema_version != intake_spec.schema_version:
        return False
    if intake_result.schema_hash != intake_spec.schema_hash():
        return False
    if validate_intake_answers(intake_spec, intake_result.answers):
        return False
    return True


def latest_run_has_completed_intake(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> bool:
    return any(
        run_has_completed_intake(repo_root, run.run_id, spec_id=spec_id, config=config)
        for run in _iter_non_superseded_runs(repo_root, spec_id, config=config)
    )


def find_intake_resume_run(
    repo_root: Path,
    spec_id: str,
    *,
    config: SpecRuntimeConfig | None = None,
) -> str | None:
    for run in _iter_non_superseded_runs(repo_root, spec_id, config=config):
        if run.status != "passed" or run.phase != "intake":
            continue
        if run_has_completed_intake(repo_root, run.run_id, spec_id=spec_id, config=config):
            return run.run_id
    return None


def _collect_orphaned_local_branches(
    repo_root: Path,
    active_runs_by_spec: dict[str, tuple[ActiveRunRecord, ...]],
) -> dict[str, tuple[str, ...]]:
    refs = _git_lines(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
        cwd=repo_root,
    )
    active_branches_by_spec = {
        spec_id: {record.branch for record in records} for spec_id, records in active_runs_by_spec.items()
    }
    orphaned: dict[str, list[str]] = {}
    for branch in refs:
        spec_id: str | None = None
        identity = implementation_branch_identity(branch)
        if identity:
            spec_id = identity.spec_id
        elif SPEC_ID_RE.fullmatch(branch):
            spec_id = branch

        if not spec_id:
            continue
        if branch in active_branches_by_spec.get(spec_id, set()):
            continue
        orphaned.setdefault(spec_id, []).append(f"orphan branch {branch}")

    return {spec_id: tuple(sorted(entries)) for spec_id, entries in orphaned.items()}


def _collect_orphaned_local_worktrees(
    repo_root: Path,
    active_runs_by_spec: dict[str, tuple[ActiveRunRecord, ...]],
    *,
    config: SpecRuntimeConfig | None = None,
) -> dict[str, tuple[str, ...]]:
    runtime_config = _runtime_config(repo_root, config=config)
    worktrees_root = _resolve_common_root(repo_root) / runtime_config.paths.worktrees_dir
    if not worktrees_root.is_dir():
        return {}

    active_worktrees_by_spec = {
        spec_id: {record.worktree_path for record in records} for spec_id, records in active_runs_by_spec.items()
    }
    orphaned: dict[str, list[str]] = {}

    for child in worktrees_root.iterdir():
        if not child.is_dir():
            continue
        identity = parse_worktree_name(child.name)
        if identity is None:
            continue
        spec_id = identity.spec_id
        if str(child) in active_worktrees_by_spec.get(spec_id, set()):
            continue
        orphaned.setdefault(spec_id, []).append(f"orphan worktree {child}")

    return {spec_id: tuple(sorted(entries)) for spec_id, entries in orphaned.items()}


def collect_git_spec_state(
    repo_root: Path,
    *,
    config: SpecRuntimeConfig | None = None,
) -> SpecGitState:
    runtime_config = _runtime_config(repo_root, config=config)
    merged_tags = _git_lines(
        "tag",
        "--merged",
        runtime_config.base_ref,
        "--list",
        "spec/merged/*",
        cwd=repo_root,
    )
    active_runs_by_spec = _load_active_runs(repo_root, config=runtime_config)
    remote_in_progress_specs = _collect_remote_in_progress_specs(repo_root, config=runtime_config)

    return SpecGitState(
        merged_specs=frozenset(_strip_ref_prefix(merged_tags, "spec/merged/")),
        active_run_specs=frozenset(active_runs_by_spec),
        remote_in_progress_specs=frozenset(remote_in_progress_specs),
        active_runs_by_spec=active_runs_by_spec,
        orphaned_local_branches_by_spec=_collect_orphaned_local_branches(
            repo_root,
            active_runs_by_spec,
        ),
        orphaned_local_worktrees_by_spec=_collect_orphaned_local_worktrees(
            repo_root,
            active_runs_by_spec,
            config=runtime_config,
        ),
    )


def is_spec_merged(
    repo_root: Path,
    spec_id: str,
    git_state: SpecGitState | None = None,
    *,
    config: SpecRuntimeConfig | None = None,
    base_ref: str = "",
) -> bool:
    if git_state is not None:
        return spec_id in git_state.merged_specs

    runtime_config = _runtime_config(repo_root, config=config)
    tag_ref = f"refs/tags/spec/merged/{spec_id}"
    ancestor = _git(
        "merge-base",
        "--is-ancestor",
        tag_ref,
        base_ref or runtime_config.base_ref,
        cwd=repo_root,
    )
    return ancestor.returncode == 0


MERGE_COMPLETION_FENCE_FETCH_TIMEOUT_SECONDS = 30


def fence_base_ref(ref: str) -> str:
    """Return the ref the merge-time completion fence should refresh and check.

    For unqualified branch names (e.g. ``main``), the fence must consult the
    remote-tracking ref ``refs/remotes/origin/<branch>`` rather than the local
    branch — otherwise the post-fetch ``is_spec_merged`` check would still see
    the stale local branch even after the remote has advanced. Refs that
    already include a remote (``origin/master``) or any other slash-qualified
    form are returned unchanged.
    """
    return ref if "/" in ref else f"refs/remotes/origin/{ref}"


def refresh_merge_completion_state(
    repo_root: Path,
    *,
    base_ref: str = "",
    config: SpecRuntimeConfig | None = None,
    timeout: float = MERGE_COMPLETION_FENCE_FETCH_TIMEOUT_SECONDS,
    remote_url: str = "",
    env: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Refresh the remote base ref and ``spec/merged/*`` tags from origin.

    The merge-time completion fence relies on ``is_spec_merged()`` reflecting
    the remote source of truth; this helper performs the single fetch required
    to make that check trustworthy before any mutating merge action runs.

    Uses an explicit destination refspec for the base branch so the local
    remote-tracking ref (``refs/remotes/<remote>/<branch>``) checked by
    ``is_spec_merged()`` is actually advanced — a bare ``git fetch <remote>
    <branch>`` would only update FETCH_HEAD and leave the stale view in place.

    Returns ``(action, error, fenced_ref)``. On success ``action`` and
    ``error`` are empty and ``fenced_ref`` names the ref that the caller must
    use for the subsequent ``is_spec_merged`` check (the same ref that was
    just refreshed). On failure ``action`` is the human-readable command that
    failed and ``error`` carries the captured detail; ``fenced_ref`` is still
    populated so callers can include it in diagnostics. A network hang is
    converted into a timeout error so the merge phase can fail closed instead
    of blocking forever.
    """
    runtime_config = _runtime_config(repo_root, config=config)
    ref = base_ref or runtime_config.base_ref
    if "/" in ref:
        remote_name, remote_branch = ref.split("/", 1)
    else:
        remote_name, remote_branch = "origin", ref
    fenced_ref = f"refs/remotes/{remote_name}/{remote_branch}"
    branch_refspec = f"+refs/heads/{remote_branch}:{fenced_ref}"
    tag_refspec = "+refs/tags/spec/merged/*:refs/tags/spec/merged/*"
    remote_source = remote_url or remote_name
    action = f"git fetch {remote_source} {branch_refspec} {tag_refspec}"
    try:
        result = run_git(
            ["fetch", remote_source, branch_refspec, tag_refspec],
            cwd=repo_root,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return action, f"timed out after {timeout:g}s", fenced_ref
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "git fetch failed"
        )
        return action, detail, fenced_ref
    return "", "", fenced_ref


def is_spec_in_progress(
    repo_root: Path,
    spec_id: str,
    git_state: SpecGitState | None = None,
    *,
    config: SpecRuntimeConfig | None = None,
) -> bool:
    if git_state is not None:
        return git_state.is_in_progress(spec_id)

    return collect_git_spec_state(repo_root, config=config).is_in_progress(spec_id)


def _active_runs_for_spec(
    repo_root: Path,
    spec_id: str,
    git_state: SpecGitState | None = None,
    *,
    config: SpecRuntimeConfig | None = None,
) -> tuple[ActiveRunRecord, ...]:
    if git_state is not None:
        return git_state.active_runs_by_spec.get(spec_id, ())
    return _load_active_runs(repo_root, config=config).get(spec_id, ())


def _remote_specs_in_progress(
    repo_root: Path,
    git_state: SpecGitState | None = None,
    *,
    config: SpecRuntimeConfig | None = None,
) -> frozenset[str]:
    if git_state is not None:
        return git_state.remote_in_progress_specs
    return collect_git_spec_state(repo_root, config=config).remote_in_progress_specs


def blocks_on_required_intake(
    repo_root: Path,
    spec_id: str,
    metadata: SpecMetadata | None,
    *,
    git_state: SpecGitState | None = None,
    config: SpecRuntimeConfig | None = None,
) -> bool:
    if metadata is None or not metadata.intake_required:
        return False
    if latest_run_has_completed_intake(repo_root, spec_id, config=config):
        return False

    active_runs = _active_runs_for_spec(repo_root, spec_id, git_state=git_state, config=config)
    if any(record.status in LIVE_ACTIVE_RUN_STATUSES for record in active_runs):
        return False

    return spec_id not in _remote_specs_in_progress(repo_root, git_state=git_state, config=config)


def get_spec_status(
    repo_root: Path,
    spec_id: str,
    spec_path: Path | None = None,
    git_state: SpecGitState | None = None,
    *,
    config: SpecRuntimeConfig | None = None,
) -> str:
    metadata = _load_spec_metadata(repo_root, spec_id, spec_path, config=config)
    if metadata is not None and metadata.obsolete:
        return "obsolete"
    if is_spec_merged(repo_root, spec_id, git_state=git_state, config=config):
        return "merged"
    if blocks_on_required_intake(
        repo_root,
        spec_id,
        metadata,
        git_state=git_state,
        config=config,
    ):
        return "needs-attention"
    if is_spec_in_progress(repo_root, spec_id, git_state=git_state, config=config):
        # Refine "in-progress" with the canonical control-plane projection so
        # callers see "stale", "blocked", "retryable", or "needs-input" when
        # the lease/run state is more precise than the legacy presence check.
        # We only override the label when a durable lease exists so we never
        # mislabel runs that simply pre-date the lease infrastructure.
        canonical = project_canonical_spec_status(
            repo_root,
            spec_id,
            git_state=git_state,
            config=config,
            require_lease=True,
        )
        if canonical is not None:
            from .control_plane import CanonicalRunStatus as _Canonical

            if canonical.status is _Canonical.STALE:
                return "stale"
            if canonical.status is _Canonical.BLOCKED:
                return "blocked"
            if canonical.status is _Canonical.RETRYABLE:
                return "retryable"
            if canonical.status is _Canonical.NEEDS_INPUT:
                return "needs-input"
            if canonical.status is _Canonical.NEEDS_ATTENTION:
                return "needs-attention"
        # A deterministic, non-retryable failure must surface as
        # needs-attention even without a durable lease so autopilot stops
        # re-dispatching it. Unlike the stale/blocked/retryable refinements
        # above, this classification comes from the run record itself, so it is
        # trustworthy regardless of lease presence.
        unleased = project_canonical_spec_status(
            repo_root,
            spec_id,
            git_state=git_state,
            config=config,
            require_lease=False,
        )
        if unleased is not None:
            from .control_plane import CanonicalRunStatus as _Canonical

            if unleased.status is _Canonical.NEEDS_ATTENTION:
                return "needs-attention"
        return "in-progress"
    return "not-started"


def project_run_record_status(
    runs_dir: Path,
    record: dict | None,
    *,
    is_merged: bool = False,
    require_lease: bool = False,
):
    """Project a single run record through the canonical control-plane view.

    Loads the lease, gate records, and process liveness for ``record`` and
    returns the resulting :class:`RunStatusProjection`. Returns ``None`` when
    ``record`` is missing and the spec is not merged, or when ``require_lease``
    is set but no durable lease exists.

    Callers that need the latest run for a spec should use
    :func:`project_canonical_spec_status`. Surfaces like ``spec watch`` that
    already iterate run records can pass each record through here so every
    surface derives its view from the same projection logic.
    """
    from .control_plane import (
        GateRecordStore,
        load_run_lease,
        project_run_status,
    )

    if record is None and not is_merged:
        return None

    run_id = ""
    run_status = ""
    retryable_hint: bool | None = None
    retryable_detail = ""
    if record is not None:
        run_id = str(record.get("run_id", "")).strip()
        run_status = str(record.get("status", "")).strip()
        # Only a persisted boolean overrides the default retryable behavior;
        # pre-existing records without the field keep today's behavior.
        raw_hint = record.get("last_failure_retryable", None)
        if isinstance(raw_hint, bool):
            retryable_hint = raw_hint
        retryable_detail = str(record.get("last_error", "")).strip()

    lease = load_run_lease(runs_dir, run_id) if run_id else None
    if require_lease and lease is None:
        return None

    state_run_dir = runs_dir / run_id if run_id else None
    gate_records: tuple = ()
    if state_run_dir is not None and state_run_dir.exists():
        try:
            gate_records = tuple(GateRecordStore(state_run_dir).load())
        except OSError:
            gate_records = ()

    process_alive: bool | None = None
    if lease is not None and lease.process_pid:
        try:
            from .orchestrator import is_pid_alive

            process_alive = is_pid_alive(lease.process_pid, lease.process_started_at)
        except Exception:
            process_alive = None

    # A run persisted as waiting-for-input whose operator request was already
    # consumed/resolved (e.g. the process crashed mid-transition) has nothing
    # left to answer; projecting NEEDS_INPUT forever strands it.
    operator_request_state = ""
    if state_run_dir is not None and run_status.strip().lower() == "waiting-for-input":
        request_path = state_run_dir / "operator-request.json"
        try:
            operator_request_state = str(
                json.loads(request_path.read_text(encoding="utf-8")).get("status", "")
            ).strip().lower()
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            operator_request_state = ""

    return project_run_status(
        run_status=run_status,
        lease=lease,
        process_alive=process_alive,
        gate_records=gate_records,
        is_merged=is_merged,
        retryable_hint=retryable_hint,
        retryable_detail=retryable_detail,
        operator_request_state=operator_request_state,
    )


def project_canonical_spec_status(
    repo_root: Path,
    spec_id: str,
    *,
    git_state: SpecGitState | None = None,
    config: SpecRuntimeConfig | None = None,
    require_lease: bool = False,
):
    """Return the canonical :class:`RunStatusProjection` for ``spec_id``'s latest run.

    Returns ``None`` when no run record exists. Used by ``spec list``,
    ``spec_table``, autopilot dispatch, ``spec status``, and ``spec watch`` so
    all surfaces derive their canonical view from a single projection function.
    """
    runtime_config = _runtime_config(repo_root, config=config)
    runs_dir = _state_runs_dir(repo_root, config=runtime_config)
    if not runs_dir.exists():
        return None

    latest_record = None
    latest_run_id = ""
    is_merged = is_spec_merged(repo_root, spec_id, git_state=git_state, config=runtime_config)
    for candidate in runs_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        if str(data.get("spec_id", "")).strip() != spec_id:
            continue
        run_id = str(data.get("run_id", candidate.stem)).strip()
        if run_id < latest_run_id:
            continue
        latest_run_id = run_id
        latest_record = data

    return project_run_record_status(
        runs_dir,
        latest_record,
        is_merged=is_merged,
        require_lease=require_lease,
    )


def _load_spec_metadata(
    repo_root: Path,
    spec_id: str,
    spec_path: Path | None,
    *,
    config: SpecRuntimeConfig | None = None,
) -> SpecMetadata | None:
    resolved = spec_path
    if resolved is None:
        candidate = resolve_spec_path(repo_root, spec_id, config=_runtime_config(repo_root, config=config))
        if candidate.exists():
            resolved = candidate
    if resolved is None or not resolved.exists():
        return None
    return parse_spec_metadata(resolved)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read spec status from run/git state.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get")
    p_get.add_argument("--repo-root", required=True)
    p_get.add_argument("--spec-id", required=True)
    p_get.add_argument("--spec-path")

    sub.add_parser(
        "verify-no-json",
        help="Historical compatibility no-op. Status now uses run state intentionally.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    if args.cmd == "get":
        repo_root = Path(args.repo_root)
        spec_path = Path(args.spec_path) if args.spec_path else None
        print(get_spec_status(repo_root, args.spec_id, spec_path))
        return 0

    if args.cmd == "verify-no-json":
        print("ok: spec status intentionally uses active run state")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
