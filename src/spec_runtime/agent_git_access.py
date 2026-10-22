"""Git metadata an implementation agent may write in its own workspace.

Agents use Git the way a person would: add, commit, stash, branch, rebase,
log and fetch. That needs writes to the workspace's Git directory and, for a
linked worktree, to the repository's shared common directory, where objects,
refs and the stash live. Repository controls that would let the agent change
how host Git behaves stay read-only: ``config``, ``config.worktree``,
``hooks/`` and ``info/``. Publishing stays with the host, which pushes with
its own command-scoped config and hooks disabled.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .git_common import subprocess_text_kwargs

GIT_CONTROL_NAMES = ("config", "config.worktree", "hooks", "info")

_GIT_LAYOUT_ENV_KEYS = (
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
)


class UnsafeAgentGitLayoutError(ValueError):
    """Raised when a workspace's Git metadata cannot be granted safely."""


@dataclass(frozen=True)
class AgentGitAccess:
    """Writable Git directories and the read-only controls inside them."""

    worktree: Path
    git_dir: Path
    common_dir: Path
    linked: bool
    # Branches checked out in the repository's other worktrees, such as the
    # operator's own checkout. A linked-worktree agent can write shared refs,
    # but not these.
    protected_refs: tuple[str, ...] = ()

    @property
    def writable_paths(self) -> tuple[Path, ...]:
        if self.git_dir == self.common_dir:
            return (self.git_dir,)
        return (self.git_dir, self.common_dir)

    @property
    def read_only_paths(self) -> tuple[Path, ...]:
        paths = [self.common_dir / name for name in GIT_CONTROL_NAMES]
        if self.git_dir != self.common_dir:
            paths.append(self.git_dir / "config.worktree")
            # The main checkout's own HEAD and index live in the common dir.
            paths.extend((self.common_dir / "HEAD", self.common_dir / "index"))
            for ref in self.protected_refs:
                ref_path = self.common_dir.joinpath(*ref.split("/"))
                paths.extend((ref_path, ref_path.with_name(ref_path.name + ".lock")))
        return tuple(paths)


def _git_output(worktree: Path, args: list[str]) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_LAYOUT_ENV_KEYS
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    argv = ["git", *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=worktree,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
            **subprocess_text_kwargs(argv),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeAgentGitLayoutError(
            f"Could not inspect Git metadata for {worktree}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UnsafeAgentGitLayoutError(
            f"Could not inspect Git metadata for {worktree}: {detail}"
        )
    return completed.stdout


def _rev_parse_paths(worktree: Path) -> tuple[Path, Path]:
    output = _git_output(
        worktree,
        ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
    )
    lines = output.splitlines()
    if len(lines) != 2:
        raise UnsafeAgentGitLayoutError(
            f"Could not inspect Git metadata for {worktree}: {output.strip()}"
        )
    return Path(lines[0]), Path(lines[1])


def _other_worktree_branches(worktree: Path) -> tuple[str, ...]:
    """Return refs checked out in every worktree except *worktree*."""
    refs: list[str] = []
    for block in _git_output(worktree, ["worktree", "list", "--porcelain"]).split("\n\n"):
        path = ""
        branch = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch "):]
        if not branch or not path:
            continue
        try:
            if Path(path).resolve() == worktree:
                continue
        except OSError:
            pass
        refs.append(branch)
    return tuple(dict.fromkeys(refs))


def _real_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise UnsafeAgentGitLayoutError(f"Refusing {label} that is not a real directory: {path}")
    return path.resolve(strict=True)


def _assert_registered_linked_worktree(
    worktree: Path,
    git_dir: Path,
    common_dir: Path,
) -> None:
    """Refuse a ``.git`` pointer that does not lead back to this worktree.

    The ``.git`` file sits in the agent-writable checkout, so a previous
    session could repoint it at another repository's metadata. Git records
    each linked worktree as ``<common>/worktrees/<name>/gitdir`` naming the
    worktree's ``.git`` file; that back-pointer lives outside the checkout.
    """
    if git_dir.parent != common_dir / "worktrees":
        raise UnsafeAgentGitLayoutError(
            f"Refusing linked worktree Git directory outside {common_dir / 'worktrees'}: {git_dir}"
        )
    back_pointer = git_dir / "gitdir"
    try:
        if back_pointer.is_symlink():
            raise OSError("symlinked gitdir back-pointer")
        recorded = back_pointer.read_text(encoding="utf-8").strip()
        recorded_path = Path(recorded)
        if not recorded_path.is_absolute():
            recorded_path = git_dir / recorded_path
        matches = recorded_path.resolve() == (worktree / ".git").resolve()
    except OSError as exc:
        raise UnsafeAgentGitLayoutError(
            f"Could not verify linked worktree registration for {worktree}: {exc}"
        ) from exc
    if not matches:
        raise UnsafeAgentGitLayoutError(
            f"Refusing .git pointer in {worktree}: {git_dir} is registered to another worktree"
        )


def agent_git_access(worktree_path: Path) -> AgentGitAccess | None:
    """Describe the Git metadata an agent in *worktree_path* may write.

    Returns ``None`` when the workspace has no ``.git`` entry. A symlinked
    ``.git`` is rejected, as is a full clone whose Git directory resolves
    outside the workspace.
    """
    if not os.path.lexists(worktree_path / ".git"):
        return None
    worktree = worktree_path.resolve(strict=True)
    dot_git = worktree / ".git"
    if dot_git.is_symlink():
        raise UnsafeAgentGitLayoutError(f"Refusing symlinked Git metadata {dot_git}")
    if dot_git.is_dir():
        git_dir = _real_directory(dot_git, label="Git directory")
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            raise UnsafeAgentGitLayoutError(
                f"Refusing checkout-local Git directory with a shared common directory: {git_dir}"
            )
        return AgentGitAccess(
            worktree=worktree,
            git_dir=git_dir,
            common_dir=common_dir,
            linked=False,
        )
    if not dot_git.is_file():
        raise UnsafeAgentGitLayoutError(f"Refusing unsafe Git metadata entry {dot_git}")
    raw_git_dir, raw_common_dir = _rev_parse_paths(worktree)
    git_dir = _real_directory(raw_git_dir, label="linked worktree Git directory")
    common_dir = _real_directory(raw_common_dir, label="Git common directory")
    if git_dir == common_dir:
        raise UnsafeAgentGitLayoutError(
            f"Linked worktree {worktree} does not have its own Git directory"
        )
    _assert_registered_linked_worktree(worktree, git_dir, common_dir)
    return AgentGitAccess(
        worktree=worktree,
        git_dir=git_dir,
        common_dir=common_dir,
        linked=True,
        protected_refs=_other_worktree_branches(worktree),
    )


_FALLBACK_NAME = "Spec Butler"
_FALLBACK_EMAIL = "spec-butler@localhost"
# Command-scoped config for agent Git. Repository config stays read-only, so
# creating a branch from a remote-tracking ref must not try to record its
# upstream there. Auto-gc is left to the host: in a linked worktree it would
# repack the shared store and try to prune protected loose refs.
_AGENT_GIT_CONFIG_ENTRIES = (
    ("branch.autoSetupMerge", "false"),
    ("gc.auto", "0"),
)


def _operator_git_identity(worktree_path: Path) -> tuple[str, str]:
    """Return the operator's Git identity as the host sees it, with fallbacks."""
    values: dict[str, str] = {}
    for key in ("user.name", "user.email"):
        try:
            argv = ["git", "config", "--get", key]
            completed = subprocess.run(
                argv,
                cwd=worktree_path,
                capture_output=True,
                timeout=5,
                check=False,
                **subprocess_text_kwargs(argv),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        value = completed.stdout.strip() if completed.returncode == 0 else ""
        if value and "\n" not in value:
            values[key] = value
    return (
        values.get("user.name", _FALLBACK_NAME),
        values.get("user.email", _FALLBACK_EMAIL),
    )


def apply_agent_git_identity(env: dict[str, str], worktree_path: Path) -> None:
    """Give agent commits an identity and keep repo hook managers quiet.

    Agent sessions ignore global Git config, so ``git commit`` would otherwise
    have no author. The operator's own identity is used when the host can
    read it; Husky-style hook installers are disabled.
    """
    name, email = _operator_git_identity(worktree_path)
    for prefix in ("GIT_AUTHOR", "GIT_COMMITTER"):
        env[f"{prefix}_NAME"] = os.environ.get(f"{prefix}_NAME") or name
        env[f"{prefix}_EMAIL"] = os.environ.get(f"{prefix}_EMAIL") or email
    env["HUSKY"] = "0"
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for key, value in _AGENT_GIT_CONFIG_ENTRIES:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
