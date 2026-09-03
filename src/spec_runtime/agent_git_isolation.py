"""Private Git metadata for provider-owned worktree sessions.

An implementation agent needs to stage and commit files, but it does not need
write access to the linked worktree's real Git metadata.  This module prepares
a complete, disposable ``GIT_DIR`` beneath the worktree's unique administrative
directory.  Its object database reads the real object database through an
alternate; every ref, lock, index, config, and newly-created object is private.

Reconciliation is deliberately a trusted host operation.  It revalidates the
original linked-worktree layout, imports commits through a Git bundle while
repository configuration and hooks are disabled, then advances exactly the
original branch with compare-and-swap semantics.  The checkout is never reset,
so provider edits that were not committed remain in place.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_PRIVATE_GIT_DIR_NAME = "specbutler-private-git"
_MAX_POINTER_BYTES = 16 * 1024
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SCP_REMOTE_RE = re.compile(
    r"(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[^/\\:@\s]+):(?P<path>[^\s]+)\Z"
)
_SCP_SECRET_RE = re.compile(r"[^/@\s]*:[^/@\s]+@[^/:\s]+:")
_UNSAFE_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PROXY_COMMAND",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
    }
)
class UnsafeAgentGitIsolationError(RuntimeError):
    """Raised when private Git metadata cannot be prepared or reconciled safely."""


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    exists: bool
    size: int
    digest: str


@dataclass(frozen=True)
class _LinkedWorktreeSnapshot:
    worktree: Path
    dot_git: Path
    git_dir: Path
    common_dir: Path
    branch_ref: str
    head: str
    object_format: str
    protected_files: tuple[_FileSnapshot, ...]


@dataclass(frozen=True)
class AgentGitIsolation:
    """Prepared launch boundary and immutable reconciliation baseline."""

    worktree: Path
    real_git_dir: Path
    common_git_dir: Path
    private_git_dir: Path
    branch_ref: str
    initial_head: str
    origin_url: str | None
    writable_paths: tuple[Path, ...]
    read_only_paths: tuple[Path, ...]
    private_config_fingerprint: str
    private_alternates_fingerprint: str
    _baseline: _LinkedWorktreeSnapshot

    @property
    def env_overrides(self) -> dict[str, str]:
        """Return the layout variables that must be set in the provider process."""
        return {
            "GIT_DIR": str(self.private_git_dir),
            "GIT_WORK_TREE": str(self.worktree),
        }

    def apply_to_environment(
        self,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return a copy of *environment* pointed only at the private layout.

        Layout redirects inherited from the operator shell are removed.  Git
        config overrides are intentionally retained: callers may use them for
        the no-push policy that is independent of this filesystem boundary.
        """
        result = dict(os.environ if environment is None else environment)
        for name in _UNSAFE_GIT_ENV:
            result.pop(name, None)
        result.update(self.env_overrides)
        return result

    @property
    def info_exclude_path(self) -> Path:
        """Return the private per-repository exclude file for recovery seeding."""
        return self.private_git_dir / "info" / "exclude"


@dataclass(frozen=True)
class AgentGitReconciliation:
    """Result of importing a private commit chain into the real branch."""

    initial_head: str
    final_head: str
    imported_commit_count: int


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _lstat_kind(path: Path, *, required: bool, directory: bool) -> bool:
    """Validate a path without following its final component."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if required:
            raise UnsafeAgentGitIsolationError(
                f"Required Git metadata is missing: {path}"
            ) from None
        return False
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(
            f"Unable to inspect Git metadata: {path}"
        ) from exc
    wanted = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if stat.S_ISLNK(mode) or not wanted:
        expected = "directory" if directory else "regular file"
        raise UnsafeAgentGitIsolationError(
            f"Git metadata must be a real {expected}: {path}"
        )
    return True


def _read_small_regular(path: Path, *, label: str) -> bytes:
    _lstat_kind(path, required=True, directory=False)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(f"Unable to read {label}") from exc
    if len(payload) > _MAX_POINTER_BYTES or b"\0" in payload:
        raise UnsafeAgentGitIsolationError(f"Invalid {label}")
    return payload


def _read_one_line(path: Path, *, label: str) -> str:
    payload = _read_small_regular(path, label=label)
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise UnsafeAgentGitIsolationError(f"Invalid {label}") from exc
    if len(lines) != 1 or not lines[0].strip():
        raise UnsafeAgentGitIsolationError(f"Invalid {label}")
    return lines[0].strip()


def _resolve_directory_pointer(owner: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = owner.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(f"{label} does not resolve") from exc
    _lstat_kind(resolved, required=True, directory=True)
    return resolved


def _parse_branch_ref(value: str) -> str:
    if not value.startswith("ref: "):
        raise UnsafeAgentGitIsolationError(
            "The linked worktree must be attached to a local branch"
        )
    ref_text = value.removeprefix("ref: ")
    ref = PurePosixPath(ref_text)
    if (
        ref.is_absolute()
        or not ref_text.startswith("refs/heads/")
        or "\\" in ref_text
        or any(part in {"", ".", ".."} for part in ref.parts)
    ):
        raise UnsafeAgentGitIsolationError("The worktree branch ref is unsafe")
    return ref.as_posix()


def _validate_oid(value: str, *, object_format: str | None = None) -> str:
    normalized = value.strip().lower()
    if not _OID_RE.fullmatch(normalized):
        raise UnsafeAgentGitIsolationError("Git ref contains an invalid object id")
    if object_format == "sha1" and len(normalized) != 40:
        raise UnsafeAgentGitIsolationError("Git ref uses an unexpected object format")
    if object_format == "sha256" and len(normalized) != 64:
        raise UnsafeAgentGitIsolationError("Git ref uses an unexpected object format")
    return normalized


def _read_packed_ref(common_dir: Path, branch_ref: str) -> str:
    packed_refs = common_dir / "packed-refs"
    payload = _read_small_regular(packed_refs, label="packed refs")
    try:
        lines = payload.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise UnsafeAgentGitIsolationError("Invalid packed refs") from exc
    matches: list[str] = []
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        oid, separator, ref = line.partition(" ")
        if not separator or not ref:
            raise UnsafeAgentGitIsolationError("Invalid packed refs")
        if ref == branch_ref:
            matches.append(_validate_oid(oid))
    if len(matches) != 1:
        raise UnsafeAgentGitIsolationError(
            "The worktree branch is missing or ambiguous in packed refs"
        )
    return matches[0]


def _read_branch_head(common_dir: Path, branch_ref: str) -> str:
    branch_path = common_dir.joinpath(*PurePosixPath(branch_ref).parts)
    refs_heads = _absolute(common_dir / "refs" / "heads")
    try:
        _absolute(branch_path).relative_to(refs_heads)
    except ValueError as exc:
        raise UnsafeAgentGitIsolationError("The worktree branch ref escapes refs/heads") from exc
    _assert_real_directory_chain(refs_heads, branch_path.parent)
    if _lstat_kind(branch_path, required=False, directory=False):
        return _validate_oid(_read_one_line(branch_path, label="branch ref"))
    return _read_packed_ref(common_dir, branch_ref)


def _assert_real_directory_chain(root: Path, descendant: Path) -> None:
    """Reject symlinked directory components below an already-validated root."""
    root = _absolute(root)
    descendant = _absolute(descendant)
    try:
        relative = descendant.relative_to(root)
    except ValueError as exc:
        raise UnsafeAgentGitIsolationError("Git metadata directory escaped its root") from exc
    current = root
    _lstat_kind(current, required=True, directory=True)
    for component in relative.parts:
        current /= component
        _lstat_kind(current, required=True, directory=True)


def _assert_private_tree_is_plain(root: Path) -> None:
    """Ensure trusted Git will not follow provider-created special files."""
    _lstat_kind(root, required=True, directory=True)
    pending = [root]
    entries_seen = 0
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise UnsafeAgentGitIsolationError(
                "Unable to inspect private Git metadata"
            ) from exc
        for entry in entries:
            entries_seen += 1
            if entries_seen > 200_000:
                raise UnsafeAgentGitIsolationError(
                    "Private Git metadata contains too many filesystem entries"
                )
            try:
                if entry.is_symlink():
                    raise UnsafeAgentGitIsolationError(
                        f"Private Git metadata contains a symlink: {entry.path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_nlink != 1:
                        raise UnsafeAgentGitIsolationError(
                            f"Private Git metadata contains a hardlink: {entry.path}"
                        )
                else:
                    raise UnsafeAgentGitIsolationError(
                        f"Private Git metadata contains a special file: {entry.path}"
                    )
            except OSError as exc:
                raise UnsafeAgentGitIsolationError(
                    "Unable to inspect private Git metadata"
                ) from exc


def _file_snapshot(path: Path, *, required: bool = False) -> _FileSnapshot:
    if not _lstat_kind(path, required=required, directory=False):
        return _FileSnapshot(path=_absolute(path), exists=False, size=0, digest="")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(
            f"Unable to fingerprint Git metadata: {path}"
        ) from exc
    return _FileSnapshot(
        path=_absolute(path),
        exists=True,
        size=size,
        digest=digest.hexdigest(),
    )


def _inspect_linked_worktree(worktree_path: Path) -> _LinkedWorktreeSnapshot:
    candidate = _absolute(worktree_path)
    _lstat_kind(candidate, required=True, directory=True)
    try:
        worktree = candidate.resolve(strict=True)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Worktree path does not resolve") from exc
    if worktree != candidate:
        raise UnsafeAgentGitIsolationError("Worktree path must not contain symlinks")

    dot_git = worktree / ".git"
    pointer = _read_one_line(dot_git, label="worktree .git pointer")
    if not pointer.startswith("gitdir: "):
        raise UnsafeAgentGitIsolationError("Worktree .git is not a linked-worktree pointer")
    git_dir = _resolve_directory_pointer(
        dot_git,
        pointer.removeprefix("gitdir: ").strip(),
        label="Worktree gitdir",
    )
    commondir_file = git_dir / "commondir"
    common_dir = _resolve_directory_pointer(
        commondir_file,
        _read_one_line(commondir_file, label="commondir pointer"),
        label="Common gitdir",
    )
    if git_dir.parent.name != "worktrees" or git_dir.parent.parent != common_dir:
        raise UnsafeAgentGitIsolationError(
            "Linked gitdir is not owned by its declared common gitdir"
        )

    registered = _read_one_line(git_dir / "gitdir", label="worktree back-pointer")
    registered_path = Path(registered)
    if not registered_path.is_absolute():
        registered_path = git_dir / registered_path
    try:
        registered_path = registered_path.resolve(strict=True)
        dot_git_resolved = dot_git.resolve(strict=True)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(
            "Worktree registration does not resolve"
        ) from exc
    if registered_path != dot_git_resolved:
        raise UnsafeAgentGitIsolationError(
            "Worktree registration does not match its checkout"
        )

    branch_ref = _parse_branch_ref(_read_one_line(git_dir / "HEAD", label="HEAD"))
    head = _read_branch_head(common_dir, branch_ref)
    object_format = "sha1" if len(head) == 40 else "sha256"

    objects = common_dir / "objects"
    _lstat_kind(objects, required=True, directory=True)
    objects_info = objects / "info"
    _lstat_kind(objects_info, required=True, directory=True)
    _lstat_kind(objects / "pack", required=True, directory=True)
    _lstat_kind(common_dir / "refs", required=True, directory=True)
    _lstat_kind(common_dir / "refs" / "heads", required=True, directory=True)
    _lstat_kind(common_dir / "hooks", required=True, directory=True)
    _lstat_kind(common_dir / "info", required=True, directory=True)

    protected_files = (
        _file_snapshot(dot_git, required=True),
        _file_snapshot(git_dir / "HEAD", required=True),
        _file_snapshot(git_dir / "commondir", required=True),
        _file_snapshot(git_dir / "gitdir", required=True),
        _file_snapshot(git_dir / "index", required=True),
        _file_snapshot(git_dir / "config.worktree"),
        _file_snapshot(common_dir / "config", required=True),
        _file_snapshot(common_dir / "info" / "exclude"),
        _file_snapshot(objects_info / "alternates"),
        _file_snapshot(common_dir / "packed-refs"),
    )
    return _LinkedWorktreeSnapshot(
        worktree=worktree,
        dot_git=dot_git,
        git_dir=git_dir,
        common_dir=common_dir,
        branch_ref=branch_ref,
        head=_validate_oid(head, object_format=object_format),
        object_format=object_format,
        protected_files=protected_files,
    )


def _assert_same_baseline(
    expected: _LinkedWorktreeSnapshot,
    current: _LinkedWorktreeSnapshot,
) -> None:
    stable_fields = (
        "worktree",
        "dot_git",
        "git_dir",
        "common_dir",
        "branch_ref",
        "head",
        "object_format",
    )
    protected_files_match = (
        len(expected.protected_files) == len(current.protected_files)
        and all(
            expected_file == current_file
            or _is_safe_empty_config_worktree_creation(
                expected,
                expected_file,
                current_file,
            )
            for expected_file, current_file in zip(
                expected.protected_files,
                current.protected_files,
                strict=True,
            )
        )
    )
    if (
        any(getattr(expected, name) != getattr(current, name) for name in stable_fields)
        or not protected_files_match
    ):
        raise UnsafeAgentGitIsolationError(
            "Real linked-worktree metadata changed after private Git preparation"
        )


def _is_safe_empty_config_worktree_creation(
    baseline: _LinkedWorktreeSnapshot,
    expected: _FileSnapshot,
    current: _FileSnapshot,
) -> bool:
    """Accept only Claude's content-free worktree-config startup artifact.

    Claude Code 2.1.257 probes worktree-local configuration during SDK connect
    and creates ``config.worktree`` even when the provider environment points
    Git at the private GIT_DIR.  An absent-to-empty transition cannot alter Git
    behavior.  Keep every other metadata transition fail-closed, including a
    symlink, hardlink, non-empty file, replacement race, or wrong-owner file.
    """
    path = baseline.git_dir / "config.worktree"
    if (
        expected.path != _absolute(path)
        or current.path != expected.path
        or expected.exists
        or not current.exists
        or current.size != 0
        or current.digest != hashlib.sha256(b"").hexdigest()
    ):
        return False
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != 0
            or before.st_nlink != 1
            or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        ):
            return False
        after_snapshot = _file_snapshot(path, required=True)
        after = os.lstat(path)
    except OSError:
        return False
    return (
        (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        and after.st_size == 0
        and after.st_nlink == 1
        and after_snapshot == current
    )


def _write_new_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(
            f"Unable to create private Git metadata: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _quoted_git_config_value(value: str) -> str:
    if "\0" in value or "\n" in value or "\r" in value:
        raise UnsafeAgentGitIsolationError(
            "Worktree path contains characters unsafe for Git config"
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _trusted_git_environment(
    *,
    git_dir: Path,
    worktree: Path,
    object_format: str,
) -> dict[str, str]:
    """Build a minimal Git environment that cannot consume repository config."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(worktree),
        "GIT_CONFIG": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    entries = [("core.hooksPath", os.devnull), ("core.fsmonitor", "false")]
    if object_format == "sha256":
        entries.extend(
            [
                ("core.repositoryFormatVersion", "1"),
                ("extensions.objectFormat", "sha256"),
            ]
        )
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if value := os.environ.get(name):
                env[name] = value
    return env


def _run_trusted_git(
    isolation: AgentGitIsolation,
    *arguments: str,
    private: bool,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    git_dir = isolation.private_git_dir if private else isolation.real_git_dir
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=isolation.worktree,
            env=_trusted_git_environment(
                git_dir=git_dir,
                worktree=isolation.worktree,
                object_format=isolation._baseline.object_format,
            ),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeAgentGitIsolationError(
            f"Trusted Git operation failed to execute: git {arguments[0]}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if len(detail) > 500:
            detail = f"{detail[:500]}..."
        suffix = f": {detail}" if detail else ""
        raise UnsafeAgentGitIsolationError(
            f"Trusted Git operation failed: git {arguments[0]}{suffix}"
        )
    return completed


def _sanitize_origin_url(value: str) -> str:
    """Accept ordinary credential-free fetch transports, never remote helpers."""
    if (
        not value
        or value != value.strip()
        or any(
            ord(character) < 32
            or (character.isspace() and character != " ")
            for character in value
        )
    ):
        raise UnsafeAgentGitIsolationError("remote.origin.url is unsafe")
    if value.startswith(("-", "~")) or "::" in value:
        raise UnsafeAgentGitIsolationError("remote.origin.url uses an unsafe transport")
    if _SCP_SECRET_RE.search(value):
        raise UnsafeAgentGitIsolationError("remote.origin.url contains credentials")
    if " " in value and ":" in value:
        raise UnsafeAgentGitIsolationError("remote.origin.url is malformed")

    if "://" not in value:
        # A literal local filesystem path is passed to Git as a quoted config
        # value and never through a shell, so internal ASCII spaces are data,
        # not an injection boundary. URL and SCP-like syntaxes remain stricter
        # because their parsers give whitespace transport-specific meaning.
        if ":" in value and not _SCP_REMOTE_RE.fullmatch(value):
            raise UnsafeAgentGitIsolationError("remote.origin.url is malformed")
        return value

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeAgentGitIsolationError("remote.origin.url is malformed") from exc
    if parsed.scheme.casefold() not in {"file", "git", "http", "https", "ssh"}:
        raise UnsafeAgentGitIsolationError("remote.origin.url uses an unsafe transport")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise UnsafeAgentGitIsolationError("remote.origin.url contains credentials")
    if parsed.scheme.casefold() in {"http", "https"} and parsed.username is not None:
        raise UnsafeAgentGitIsolationError("remote.origin.url contains credentials")
    if parsed.scheme.casefold() == "file" and parsed.netloc not in {"", "localhost"}:
        raise UnsafeAgentGitIsolationError("remote.origin.url uses a remote file host")
    if parsed.scheme.casefold() == "file" and not parsed.path:
        raise UnsafeAgentGitIsolationError("remote.origin.url is malformed")
    if parsed.scheme.casefold() != "file" and (not parsed.hostname or not parsed.path):
        raise UnsafeAgentGitIsolationError("remote.origin.url is malformed")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeAgentGitIsolationError("remote.origin.url has an invalid port")
    return value


def _origin_url_from_common_config(baseline: _LinkedWorktreeSnapshot) -> str | None:
    """Read one literal origin URL without following includes or redirects."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if inherited := os.environ.get(name):
                env[name] = inherited
    try:
        completed = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(baseline.common_dir / "config"),
                "--no-includes",
                "--null",
                "--get-all",
                "remote.origin.url",
            ],
            cwd=baseline.worktree,
            env=env,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeAgentGitIsolationError("Unable to inspect remote.origin.url") from exc
    if completed.returncode == 1 and not completed.stdout:
        return None
    if completed.returncode != 0:
        raise UnsafeAgentGitIsolationError("Unable to inspect remote.origin.url")
    values = [entry for entry in completed.stdout.split(b"\0") if entry]
    if len(values) != 1:
        raise UnsafeAgentGitIsolationError(
            "Repository must have at most one remote.origin.url"
        )
    try:
        value = values[0].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise UnsafeAgentGitIsolationError("remote.origin.url is not UTF-8") from exc
    return _sanitize_origin_url(value)


def _private_config(
    baseline: _LinkedWorktreeSnapshot,
    *,
    origin_url: str | None,
) -> bytes:
    format_lines = ""
    if baseline.object_format == "sha256":
        format_lines = "\n[extensions]\n\tobjectFormat = sha256"
    remote_lines = ""
    if origin_url is not None:
        remote_lines = (
            '\n[remote "origin"]\n'
            f"\turl = {_quoted_git_config_value(origin_url)}\n"
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            "\tpushURL = specbutler-no-push://origin\n"
        )
    rendered = (
        "[core]\n"
        f"\trepositoryFormatVersion = {'1' if baseline.object_format == 'sha256' else '0'}\n"
        f"\tfileMode = {'false' if os.name == 'nt' else 'true'}\n"
        "\tbare = false\n"
        "\tlogAllRefUpdates = true\n"
        f"\tworktree = {_quoted_git_config_value(str(baseline.worktree))}\n"
        "[user]\n"
        "\tname = Spec Butler Agent\n"
        "\temail = specbutler-agent@localhost\n"
        f"{remote_lines}"
        f"{format_lines}\n"
    )
    return rendered.encode("utf-8")


def _copy_optional_info_exclude(baseline: _LinkedWorktreeSnapshot, target: Path) -> None:
    source = baseline.common_dir / "info" / "exclude"
    if not _lstat_kind(source, required=False, directory=False):
        return
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Unable to copy Git info/exclude") from exc
    if len(payload) > 4 * 1024 * 1024 or b"\0" in payload:
        raise UnsafeAgentGitIsolationError("Git info/exclude is unsafe to copy")
    _write_new_file(target, payload)


def _validate_context_ref(refname: str) -> str:
    if not refname.startswith(("refs/remotes/origin/", "refs/tags/")):
        raise UnsafeAgentGitIsolationError("Git context ref escaped its namespace")
    ref = PurePosixPath(refname)
    if (
        ref.is_absolute()
        or "\\" in refname
        or any(part in {"", ".", ".."} for part in ref.parts)
        or any(character.isspace() or ord(character) < 32 for character in refname)
        or any(part.endswith(".lock") for part in ref.parts)
    ):
        raise UnsafeAgentGitIsolationError("Git context ref is unsafe")
    return ref.as_posix()


def _copy_reference_context(isolation: AgentGitIsolation) -> None:
    """Copy remote-tracking refs and tags as private loose refs."""
    completed = _run_trusted_git(
        isolation,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/remotes/origin",
        "refs/tags",
        private=False,
    )
    for line in completed.stdout.splitlines():
        refname, separator, oid = line.partition(" ")
        if not separator:
            raise UnsafeAgentGitIsolationError("Git returned an invalid context ref")
        safe_ref = _validate_context_ref(refname)
        safe_oid = _validate_oid(
            oid,
            object_format=isolation._baseline.object_format,
        )
        _run_trusted_git(
            isolation,
            "update-ref",
            safe_ref,
            safe_oid,
            private=True,
        )


def prepare_agent_git_isolation(worktree_path: Path) -> AgentGitIsolation:
    """Create a new private ``GIT_DIR`` for one provider launch.

    Existing private metadata is never overwritten.  Call
    :func:`reset_agent_git_isolation` explicitly when beginning a new attempt.
    """
    baseline = _inspect_linked_worktree(worktree_path)
    origin_url = _origin_url_from_common_config(baseline)
    private_git_dir = baseline.git_dir / _PRIVATE_GIT_DIR_NAME
    try:
        private_git_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise UnsafeAgentGitIsolationError(
            "Private Git metadata already exists; reconcile or reset it first"
        ) from exc
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Unable to create private Git metadata") from exc

    try:
        for relative in (
            "objects/info",
            "objects/pack",
            "refs/heads",
            "refs/tags",
            "logs/refs/heads",
            "info",
        ):
            (private_git_dir / relative).mkdir(parents=True, exist_ok=True, mode=0o700)

        private_branch = private_git_dir.joinpath(
            *PurePosixPath(baseline.branch_ref).parts
        )
        private_log = private_git_dir / "logs" / baseline.branch_ref
        private_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_new_file(private_git_dir / "HEAD", f"ref: {baseline.branch_ref}\n".encode())
        _write_new_file(private_branch, f"{baseline.head}\n".encode())
        _write_new_file(
            private_git_dir / "config",
            _private_config(baseline, origin_url=origin_url),
        )
        _copy_optional_info_exclude(
            baseline,
            private_git_dir / "info" / "exclude",
        )
        alternates = private_git_dir / "objects" / "info" / "alternates"
        actual_objects = str(baseline.common_dir / "objects")
        if "\n" in actual_objects or "\r" in actual_objects or "\0" in actual_objects:
            raise UnsafeAgentGitIsolationError("Object database path is unsafe")
        _write_new_file(alternates, f"{actual_objects}\n".encode())

        provisional = AgentGitIsolation(
            worktree=baseline.worktree,
            real_git_dir=baseline.git_dir,
            common_git_dir=baseline.common_dir,
            private_git_dir=private_git_dir,
            branch_ref=baseline.branch_ref,
            initial_head=baseline.head,
            origin_url=origin_url,
            writable_paths=(_absolute(private_git_dir),),
            read_only_paths=(),
            private_config_fingerprint="",
            private_alternates_fingerprint="",
            _baseline=baseline,
        )
        _copy_reference_context(provisional)
        _run_trusted_git(provisional, "read-tree", baseline.head, private=True)
        _file_snapshot(private_git_dir / "index", required=True)

        read_only_paths = tuple(
            _absolute(path)
            for path in (
                baseline.dot_git,
                baseline.git_dir / "HEAD",
                baseline.git_dir / "commondir",
                baseline.git_dir / "gitdir",
                baseline.git_dir / "index",
                baseline.git_dir / "config.worktree",
                baseline.common_dir / "config",
                baseline.common_dir / "hooks",
                baseline.common_dir / "info",
                baseline.common_dir / "objects",
                baseline.common_dir / "refs",
                baseline.common_dir / "packed-refs",
                baseline.common_dir / "logs",
            )
        )
        return AgentGitIsolation(
            worktree=baseline.worktree,
            real_git_dir=baseline.git_dir,
            common_git_dir=baseline.common_dir,
            private_git_dir=private_git_dir,
            branch_ref=baseline.branch_ref,
            initial_head=baseline.head,
            origin_url=origin_url,
            writable_paths=(_absolute(private_git_dir),),
            read_only_paths=read_only_paths,
            private_config_fingerprint=_file_snapshot(
                private_git_dir / "config", required=True
            ).digest,
            private_alternates_fingerprint=_file_snapshot(
                alternates, required=True
            ).digest,
            _baseline=baseline,
        )
    except BaseException:
        if private_git_dir.exists() and not private_git_dir.is_symlink():
            shutil.rmtree(private_git_dir, ignore_errors=True)
        raise


def prepare_agent_git_isolation_if_linked(
    worktree_path: Path,
) -> AgentGitIsolation | None:
    """Prepare isolation for a linked worktree, or accept a full local clone.

    Container backends commonly use a complete clone whose ``.git`` directory
    is already inside the isolated workspace.  Those need no external metadata
    grant and return ``None``.  Symlinks and malformed ``.git`` entries are
    rejected instead of being mistaken for either supported layout.
    """
    candidate = _absolute(worktree_path)
    _lstat_kind(candidate, required=True, directory=True)
    try:
        worktree = candidate.resolve(strict=True)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Worktree path does not resolve") from exc
    if worktree != candidate:
        raise UnsafeAgentGitIsolationError("Worktree path must not contain symlinks")

    dot_git = worktree / ".git"
    try:
        mode = dot_git.lstat().st_mode
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Worktree .git entry is unavailable") from exc
    if stat.S_ISLNK(mode):
        raise UnsafeAgentGitIsolationError("Worktree .git entry must not be a symlink")
    if stat.S_ISREG(mode):
        return prepare_agent_git_isolation(worktree)
    if not stat.S_ISDIR(mode):
        raise UnsafeAgentGitIsolationError(
            "Worktree .git entry is neither a linked pointer nor a directory"
        )

    # A full clone needs no writable path outside the checkout, but still
    # validate the minimum topology relied on by that classification.
    for relative, directory in (
        ("HEAD", False),
        ("config", False),
        ("objects", True),
        ("objects/info", True),
        ("objects/pack", True),
        ("refs", True),
        ("refs/heads", True),
        ("hooks", True),
    ):
        _lstat_kind(dot_git / relative, required=True, directory=directory)
    return None


def append_agent_git_exclude_patterns(
    isolation: AgentGitIsolation,
    patterns: Iterable[str],
) -> None:
    """Append trusted recovery-only patterns to the private ``info/exclude``.

    Call this only while preparing a launch, before the provider starts.  The
    real repository's exclude file is never changed.
    """
    _validate_real_baseline(isolation)
    _validate_private_layout(isolation)
    encoded_patterns: list[bytes] = []
    for pattern in patterns:
        if not pattern or "\0" in pattern or "\n" in pattern or "\r" in pattern:
            raise UnsafeAgentGitIsolationError("Git exclude pattern is unsafe")
        encoded_patterns.append(pattern.encode("utf-8"))
    if not encoded_patterns:
        return

    target = isolation.info_exclude_path
    if _lstat_kind(target, required=False, directory=False):
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise UnsafeAgentGitIsolationError(
                "Unable to read private Git info/exclude"
            ) from exc
    else:
        existing = b""
    if len(existing) > 4 * 1024 * 1024 or b"\0" in existing:
        raise UnsafeAgentGitIsolationError("Private Git info/exclude is unsafe")
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    payload = existing + separator + b"\n".join(encoded_patterns) + b"\n"

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix="exclude-", dir=target.parent)
        temporary_path = Path(raw_path)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError as exc:
        raise UnsafeAgentGitIsolationError(
            "Unable to update private Git info/exclude"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _validate_private_layout(isolation: AgentGitIsolation) -> str:
    expected_private = isolation.real_git_dir / _PRIVATE_GIT_DIR_NAME
    if isolation.private_git_dir != expected_private:
        raise UnsafeAgentGitIsolationError("Private Git directory escaped its worktree")
    for relative in (
        ".",
        "objects",
        "objects/info",
        "objects/pack",
        "refs",
        "refs/heads",
        "logs",
        "logs/refs",
        "logs/refs/heads",
    ):
        _lstat_kind(isolation.private_git_dir / relative, required=True, directory=True)
    _assert_private_tree_is_plain(isolation.private_git_dir)
    for forbidden in ("shallow", "info/grafts"):
        path = isolation.private_git_dir / forbidden
        if path.exists() or path.is_symlink():
            raise UnsafeAgentGitIsolationError(
                f"Private Git history override is not allowed: {forbidden}"
            )

    config = _file_snapshot(isolation.private_git_dir / "config", required=True)
    if config.digest != isolation.private_config_fingerprint:
        raise UnsafeAgentGitIsolationError("Private Git config changed during provider execution")
    alternates = _file_snapshot(
        isolation.private_git_dir / "objects" / "info" / "alternates",
        required=True,
    )
    if alternates.digest != isolation.private_alternates_fingerprint:
        raise UnsafeAgentGitIsolationError(
            "Private Git object alternates changed during provider execution"
        )
    expected_alternates = f"{isolation.common_git_dir / 'objects'}\n".encode()
    if _read_small_regular(alternates.path, label="private object alternates") != expected_alternates:
        raise UnsafeAgentGitIsolationError("Private Git object alternates are invalid")

    private_head_ref = _parse_branch_ref(
        _read_one_line(isolation.private_git_dir / "HEAD", label="private HEAD")
    )
    if private_head_ref != isolation.branch_ref:
        raise UnsafeAgentGitIsolationError("Private Git HEAD changed branches")
    private_branch = isolation.private_git_dir.joinpath(
        *PurePosixPath(isolation.branch_ref).parts
    )
    _assert_real_directory_chain(
        isolation.private_git_dir / "refs" / "heads",
        private_branch.parent,
    )
    head = _validate_oid(
        _read_one_line(private_branch, label="private branch ref"),
        object_format=isolation._baseline.object_format,
    )
    _file_snapshot(isolation.private_git_dir / "index", required=True)
    return head


def _safe_repo_relative_path(value: str, *, allow_glob: bool = False) -> str:
    """Validate a repository-relative POSIX path without normalizing it."""
    if not value or "\0" in value or "\\" in value or "\n" in value or "\r" in value:
        raise UnsafeAgentGitIsolationError("Private Git path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeAgentGitIsolationError("Private Git path is unsafe")
    if not allow_glob and any(character in value for character in "*?["):
        raise UnsafeAgentGitIsolationError("Private Git file path contains a glob")
    return path.as_posix()


def agent_git_head(isolation: AgentGitIsolation) -> str:
    """Return the validated private HEAD without consulting provider config."""
    _validate_real_baseline(isolation)
    return _validate_private_layout(isolation)


def agent_git_added_paths(
    isolation: AgentGitIsolation,
    base_sha: str,
    pathspec: str,
) -> tuple[str, ...]:
    """Return files added between *base_sha* and the validated private HEAD."""
    base = _validate_oid(base_sha, object_format=isolation._baseline.object_format)
    safe_pathspec = _safe_repo_relative_path(pathspec, allow_glob=True)
    head = agent_git_head(isolation)
    completed = _run_trusted_git(
        isolation,
        "diff",
        "--name-only",
        "--diff-filter=A",
        base,
        head,
        "--",
        safe_pathspec,
        private=True,
    )
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if line:
            paths.append(_safe_repo_relative_path(line))
    return tuple(paths)


def agent_git_show_file(
    isolation: AgentGitIsolation,
    revision: str,
    path: str,
) -> str:
    """Read one file from a validated private commit as UTF-8 text."""
    commit = _validate_oid(
        revision,
        object_format=isolation._baseline.object_format,
    )
    safe_path = _safe_repo_relative_path(path)
    head = agent_git_head(isolation)
    _run_trusted_git(
        isolation,
        "merge-base",
        "--is-ancestor",
        commit,
        head,
        private=True,
    )
    completed = _run_trusted_git(
        isolation,
        "show",
        f"{commit}:{safe_path}",
        private=True,
    )
    return completed.stdout


def _validate_real_baseline(isolation: AgentGitIsolation) -> None:
    current = _inspect_linked_worktree(isolation.worktree)
    _assert_same_baseline(isolation._baseline, current)


def reconcile_agent_git_isolation(
    isolation: AgentGitIsolation,
) -> AgentGitReconciliation:
    """Import the private HEAD and atomically advance the original real branch.

    The caller must have confirmed that the provider supervisor's ownership
    boundary is stopped before this function is called. On success, the real
    index is reset to the imported tree without touching checkout files,
    preserving uncommitted edits and untracked files.
    """
    _validate_real_baseline(isolation)
    private_head = _validate_private_layout(isolation)
    _run_trusted_git(
        isolation,
        "cat-file",
        "-e",
        f"{private_head}^{{commit}}",
        private=True,
    )
    ancestor = _run_trusted_git(
        isolation,
        "merge-base",
        "--is-ancestor",
        isolation.initial_head,
        private_head,
        private=True,
    )
    # ``_run_trusted_git`` has already converted a non-zero result into a
    # fail-closed exception. Keep this assertion as a guard if its contract is
    # ever loosened.
    if ancestor.returncode != 0:  # pragma: no cover - defensive
        raise UnsafeAgentGitIsolationError("Private Git history rewrote the initial HEAD")

    if private_head == isolation.initial_head:
        return AgentGitReconciliation(
            initial_head=isolation.initial_head,
            final_head=private_head,
            imported_commit_count=0,
        )

    count_output = _run_trusted_git(
        isolation,
        "rev-list",
        "--count",
        f"{isolation.initial_head}..{private_head}",
        private=True,
    ).stdout.strip()
    try:
        commit_count = int(count_output)
    except ValueError as exc:
        raise UnsafeAgentGitIsolationError("Unable to count private commits") from exc
    if commit_count <= 0:
        raise UnsafeAgentGitIsolationError("Private HEAD has no importable commits")

    with tempfile.TemporaryDirectory(prefix="specbutler-git-reconcile-") as temp_dir:
        bundle_path = Path(temp_dir) / "provider.bundle"
        _run_trusted_git(
            isolation,
            "bundle",
            "create",
            str(bundle_path),
            "HEAD",
            private=True,
            timeout=120.0,
        )
        _lstat_kind(bundle_path, required=True, directory=False)

        # Re-check immediately before importing into any real metadata.  Git is
        # deliberately invoked with the real GIT_DIR but no repository config.
        _validate_real_baseline(isolation)
        _run_trusted_git(
            isolation,
            "bundle",
            "verify",
            str(bundle_path),
            private=False,
            timeout=120.0,
        )
        _run_trusted_git(
            isolation,
            "bundle",
            "unbundle",
            str(bundle_path),
            private=False,
            timeout=120.0,
        )

    _run_trusted_git(
        isolation,
        "cat-file",
        "-e",
        f"{private_head}^{{commit}}",
        private=False,
    )
    _validate_real_baseline(isolation)
    _run_trusted_git(
        isolation,
        "update-ref",
        isolation.branch_ref,
        private_head,
        isolation.initial_head,
        private=False,
    )
    try:
        _run_trusted_git(
            isolation,
            "read-tree",
            "--reset",
            private_head,
            private=False,
        )
    except BaseException:
        try:
            _run_trusted_git(
                isolation,
                "update-ref",
                isolation.branch_ref,
                isolation.initial_head,
                private_head,
                private=False,
            )
        except UnsafeAgentGitIsolationError as rollback_error:
            raise UnsafeAgentGitIsolationError(
                "Real index reset failed and the branch rollback also failed"
            ) from rollback_error
        raise

    return AgentGitReconciliation(
        initial_head=isolation.initial_head,
        final_head=private_head,
        imported_commit_count=commit_count,
    )


def cleanup_agent_git_isolation(isolation: AgentGitIsolation) -> None:
    """Remove this attempt's disposable Git metadata without following symlinks."""
    expected = isolation.real_git_dir / _PRIVATE_GIT_DIR_NAME
    if isolation.private_git_dir != expected:
        raise UnsafeAgentGitIsolationError("Private Git directory escaped its worktree")
    if not isolation.private_git_dir.exists() and not isolation.private_git_dir.is_symlink():
        return
    _lstat_kind(isolation.real_git_dir, required=True, directory=True)
    _lstat_kind(isolation.private_git_dir, required=True, directory=True)
    try:
        shutil.rmtree(isolation.private_git_dir)
    except OSError as exc:
        raise UnsafeAgentGitIsolationError("Unable to remove private Git metadata") from exc


def reset_agent_git_isolation(worktree_path: Path) -> AgentGitIsolation:
    """Discard any prior private attempt and prepare a fresh isolation boundary."""
    baseline = _inspect_linked_worktree(worktree_path)
    private_git_dir = baseline.git_dir / _PRIVATE_GIT_DIR_NAME
    if private_git_dir.exists() or private_git_dir.is_symlink():
        _lstat_kind(private_git_dir, required=True, directory=True)
        try:
            shutil.rmtree(private_git_dir)
        except OSError as exc:
            raise UnsafeAgentGitIsolationError(
                "Unable to reset private Git metadata"
            ) from exc
    return prepare_agent_git_isolation(baseline.worktree)
