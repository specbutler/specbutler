"""Fail-closed path checks for recursive worktree cleanup.

Worktree locations cross a trust boundary in two places: repository config
chooses their owning directory, and persisted run state can record a checkout
path.  Cleanup must prove both values before passing a path to either Git's
recursive worktree removal or a raw filesystem tree remover.
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from .spec_identity import (
    SPEC_ID_RE,
    authoring_branch_identity,
    implementation_branch_identity,
    parse_worktree_name,
    spec_run_worktree_name,
)


class UnsafeWorktreePathError(ValueError):
    """Raised when cleanup ownership cannot be proven for a path."""


def _has_parent_reference(raw: str) -> bool:
    """Recognize parent traversal in both native and portable path syntax."""
    return ".." in PurePosixPath(raw).parts or ".." in PureWindowsPath(raw).parts


def _portable_absolute(raw: str) -> bool:
    """Recognize absolute/drive paths even when tests run on another OS."""
    windows_path = PureWindowsPath(raw)
    return PurePosixPath(raw).is_absolute() or windows_path.is_absolute() or bool(windows_path.drive)


def paths_equal(
    left: str | Path,
    right: str | Path,
    *,
    windows: bool | None = None,
) -> bool:
    """Compare canonical path spellings with host-appropriate semantics.

    Git for Windows emits forward slashes and may preserve different casing
    than :class:`~pathlib.WindowsPath`.  ``ntpath`` normalizes both without
    requiring a Windows host, which also makes this behavior directly
    regression-testable on Linux.
    """
    use_windows = os.name == "nt" if windows is None else windows
    if use_windows:
        return ntpath.normcase(ntpath.normpath(os.fspath(left))) == ntpath.normcase(
            ntpath.normpath(os.fspath(right))
        )
    return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
        os.path.normpath(os.fspath(right))
    )


def configured_worktrees_root(repo_root: Path, configured: str) -> Path:
    """Return a canonical, repository-owned configured worktree root.

    Configuration is deliberately repository-relative.  Rejecting absolute,
    drive-relative, and parent-traversing values makes a copied ``.spec.toml``
    portable and prevents it from granting recursive deletion authority over
    an unrelated directory.
    """
    common_root = repo_root.expanduser().resolve(strict=False)
    raw = str(configured or "").strip()
    if not raw:
        raise UnsafeWorktreePathError("the configured worktree directory is empty")
    if _portable_absolute(raw):
        raise UnsafeWorktreePathError(
            f"the configured worktree directory must be repository-relative: {raw}"
        )
    if _has_parent_reference(raw):
        raise UnsafeWorktreePathError(
            f"the configured worktree directory must not contain parent traversal: {raw}"
        )

    root = (common_root / Path(raw)).resolve(strict=False)
    try:
        relative = root.relative_to(common_root)
    except ValueError as exc:
        raise UnsafeWorktreePathError(
            f"the configured worktree directory escapes the repository: {root}"
        ) from exc
    if not relative.parts:
        raise UnsafeWorktreePathError(
            "the configured worktree directory must not be the repository root"
        )
    return root


def expected_branch_worktree_names(branch: str) -> tuple[str, ...]:
    """Return the exact persistent checkout names authorized by a branch."""
    branch = branch.strip()
    implementation = implementation_branch_identity(branch)
    if implementation is not None:
        if not implementation.run_token:
            return ()
        if implementation.kind == "task":
            generated = f"task-{implementation.spec_id}--{implementation.run_token}"
            return (generated, implementation.spec_id)
        if implementation.kind == "specrun":
            generated = f"specrun-{implementation.spec_id}--{implementation.run_token}"
            return (generated, implementation.spec_id)
        return (
            spec_run_worktree_name(implementation.spec_id, implementation.run_token),
            implementation.spec_id,
        )

    authoring = authoring_branch_identity(branch)
    if authoring is not None:
        if authoring.kind == "spec-session" and authoring.run_token:
            return (f"spec-session-{authoring.run_token}",)
        # ``<id>`` is the pre-dedicated-authoring legacy layout and remains a
        # supported cleanup target for existing runs.
        return (f"spec-{authoring.spec_id}", authoring.spec_id)

    return ()


def expected_run_worktree_names(spec_id: str, branch: str) -> tuple[str, ...]:
    """Return exact checkout names only when branch and run identity agree."""
    spec_id = spec_id.strip()
    branch = branch.strip()
    if not SPEC_ID_RE.fullmatch(spec_id):
        return ()

    implementation = implementation_branch_identity(branch)
    if implementation is not None and implementation.spec_id != spec_id:
        return ()
    authoring = authoring_branch_identity(branch)
    if authoring is not None and authoring.kind != "spec-session" and authoring.spec_id != spec_id:
        return ()

    expected = expected_branch_worktree_names(branch)
    if expected:
        return expected

    # Very old persisted runs used the spec ID as both branch and directory.
    if branch == spec_id:
        return (spec_id,)
    return ()


def recognized_worktree_name(name: str) -> bool:
    """Return whether *name* is one of Spec Butler's persistent layouts."""
    if parse_worktree_name(name) is not None:
        return True
    if name.startswith("spec-"):
        return SPEC_ID_RE.fullmatch(name.removeprefix("spec-")) is not None
    if name.startswith("spec-session-"):
        token = name.removeprefix("spec-session-")
        return bool(token) and all(char.isalnum() or char == "-" for char in token)
    return False


def validate_owned_worktree_path(
    *,
    owner_root: Path,
    target: str | Path,
    relative_to: Path | None = None,
    expected_names: tuple[str, ...] = (),
    expected_prefix: str = "",
) -> Path:
    """Canonicalize and prove that *target* is an authorized direct child.

    Resolving both the owning root and target detects existing symlink and
    Windows junction/reparse-point escapes.  Direct-child ownership avoids
    accidentally authorizing a nested arbitrary tree merely because its path
    starts with the configured root string.
    """
    canonical_root = owner_root.expanduser().resolve(strict=False)
    raw = os.fspath(target).strip()
    if not raw:
        raise UnsafeWorktreePathError("the worktree cleanup target is empty")
    if _has_parent_reference(raw):
        raise UnsafeWorktreePathError(
            f"the worktree cleanup target contains parent traversal: {raw}"
        )

    native_target = Path(raw).expanduser()
    if native_target.is_absolute():
        candidate = native_target
    else:
        # A foreign absolute spelling (for example C:\\repo on POSIX) cannot
        # be a valid local checkout and must not be reinterpreted as relative.
        if _portable_absolute(raw):
            raise UnsafeWorktreePathError(
                f"the worktree cleanup target is not a native local path: {raw}"
            )
        base = (relative_to or canonical_root).expanduser().resolve(strict=False)
        candidate = base / native_target
    canonical_target = candidate.resolve(strict=False)

    if not paths_equal(canonical_target.parent, canonical_root):
        raise UnsafeWorktreePathError(
            f"worktree cleanup target is not a direct child of its owned root: {canonical_target}"
        )

    name = canonical_target.name
    if expected_names and not any(paths_equal(name, expected, windows=os.name == "nt") for expected in expected_names):
        rendered = ", ".join(expected_names)
        raise UnsafeWorktreePathError(
            f"worktree cleanup target {canonical_target} does not match expected identity ({rendered})"
        )
    if expected_prefix and not (
        name.casefold().startswith(expected_prefix.casefold())
        if os.name == "nt"
        else name.startswith(expected_prefix)
    ):
        raise UnsafeWorktreePathError(
            f"worktree cleanup target {canonical_target} does not match expected prefix {expected_prefix}"
        )
    if not expected_names and not expected_prefix and not recognized_worktree_name(name):
        raise UnsafeWorktreePathError(
            f"worktree cleanup target has no recognized Spec Butler identity: {canonical_target}"
        )
    return canonical_target
