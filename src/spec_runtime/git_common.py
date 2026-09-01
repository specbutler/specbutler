"""Shared subprocess text boundaries and Git repository helpers.

Git and GitHub CLI share an explicit UTF-8 decoding boundary. The common Git
root is the parent directory of the ``.git`` common dir — in a worktree layout
this is the main checkout, not the worktree itself.

All modules that need to resolve the common root should import from here
instead of duplicating the logic.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from .process_supervisor import run as run_supervised


def _command_executable(command: Sequence[str]) -> str:
    """Return a case-normalized executable basename for *command*."""
    if not command:
        return ""
    return str(command[0]).replace("\\", "/").rsplit("/", 1)[-1].casefold()


def is_git_command(command: Sequence[str]) -> bool:
    """Return whether *command* launches Git, including a resolved git.exe."""
    return _command_executable(command) in {"git", "git.exe"}


def is_github_cli_command(command: Sequence[str]) -> bool:
    """Return whether *command* launches GitHub CLI, including a resolved gh.exe."""
    return _command_executable(command) in {"gh", "gh.exe"}


def subprocess_text_kwargs(command: Sequence[str]) -> dict[str, object]:
    """Return safe text-mode kwargs for command-line tool output.

    Git for Windows and GitHub CLI emit UTF-8 independently of the active
    Windows ANSI code page. Letting :mod:`subprocess` choose the locale decoder
    can therefore corrupt paths, JSON, and user-authored text. Replacement on
    malformed bytes keeps diagnostics available instead of turning an
    otherwise recoverable command failure into ``UnicodeDecodeError``.

    Other commands retain Python's normal locale decoder, with replacement for
    bytes that decoder cannot represent.
    """
    # Arbitrary project commands may emit bytes that do not match the host's
    # active locale. Diagnostics should degrade visibly instead of raising from
    # ``subprocess`` and masking the command's real exit status.
    kwargs: dict[str, object] = {"text": True, "errors": "replace"}
    if is_git_command(command) or is_github_cli_command(command):
        kwargs["encoding"] = "utf-8"
    return kwargs


def git_text_kwargs(command: Sequence[str]) -> dict[str, object]:
    """Backward-compatible alias for :func:`subprocess_text_kwargs`."""
    return subprocess_text_kwargs(command)


def run_git(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    check: bool = False,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git through the shared UTF-8 stdout/stderr decoding boundary."""
    command = ["git", *args]
    runner = run_supervised if timeout is not None else subprocess.run
    return runner(
        command,
        cwd=cwd,
        check=check,
        timeout=timeout,
        env=env,
        capture_output=capture_output,
        **subprocess_text_kwargs(command),
    )


def _resolve_common_root_fallback(repo_root: Path) -> Path:
    """Walk worktree metadata on disk to find the common root."""
    # Walk up from repo_root to find the nearest .git entry.  This handles
    # the case where repo_root is a subdirectory (e.g. derived from cwd
    # when the caller is inside repo/subdir/).
    root = repo_root
    git_entry = root / ".git"
    if not git_entry.exists():
        for parent in root.parents:
            candidate = parent / ".git"
            if candidate.exists():
                root = parent
                git_entry = candidate
                break
        else:
            return repo_root  # No .git found — not in a git repo

    if git_entry.is_dir():
        return root
    if not git_entry.is_file():
        return root

    try:
        first_line = git_entry.read_text(encoding="utf-8").splitlines()[0].strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return root
    if not first_line.lower().startswith("gitdir:"):
        return root

    git_dir = Path(first_line.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()

    commondir_path = git_dir / "commondir"
    if commondir_path.is_file():
        try:
            commondir = commondir_path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (IndexError, OSError, UnicodeDecodeError):
            return root
        if not commondir:
            return root
        common_dir = Path(commondir)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        return common_dir.parent

    if git_dir.name == ".git":
        return git_dir.parent
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent.parent
    return root


def resolve_common_root(repo_root: Path | None = None) -> Path:
    """Return the common root for the git repo (parent of .git common dir).

    When *repo_root* is ``None`` the current working directory is used.

    On git < 2.31 the ``--path-format=absolute`` flag is silently ignored and
    the command returns a relative path.  We detect that and fall through to
    the filesystem-based fallback so the function works on older git versions.
    """
    fallback_root = repo_root.resolve() if repo_root is not None else Path.cwd().resolve()
    try:
        result = run_git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo_root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _resolve_common_root_fallback(fallback_root)
    if result.returncode != 0 or not result.stdout.strip():
        return _resolve_common_root_fallback(fallback_root)
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        return _resolve_common_root_fallback(fallback_root)
    return common_dir.parent
