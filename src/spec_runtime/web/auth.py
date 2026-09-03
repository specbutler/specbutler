"""Token generation, cookie/bearer authentication middleware."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from spec_runtime.provider_env import specbutler_user_state_root

TOKEN_LENGTH = 32  # 32 bytes = 256 bits of entropy
MAX_TOKEN_FILE_BYTES = 1024
COOKIE_NAME_PREFIX = "spec_session"


class UnsafeTokenFileError(OSError):
    """Raised when an existing bearer file cannot be trusted."""


def cookie_name_for_port(port: int | None) -> str:
    """Return the per-instance cookie name.

    Browsers do not scope cookies by port, so two ``spec web`` instances on
    the same host but different ports would otherwise overwrite each other's
    session cookie. Suffixing the port keeps both jars distinct.
    """
    return f"{COOKIE_NAME_PREFIX}_{port}" if port else COOKIE_NAME_PREFIX


def _legacy_web_state_dir(repo_root: Path) -> Path:
    from spec_runtime.config import load_spec_runtime_config

    config = load_spec_runtime_config(require=False)
    return repo_root / config.paths.state_dir / "web"


def _user_state_root() -> Path:
    """Return a user-private, machine-local state root.

    The web bearer token is an operator credential, not repository state.  In
    particular, repository-local runtime state may be writable by project
    processes or retained execution workspaces, so keeping the token there
    would weaken the web operator boundary.
    """
    return specbutler_user_state_root()


def _repo_state_key(repo_root: Path) -> str:
    resolved = os.path.normcase(str(repo_root.expanduser().resolve(strict=False)))
    return hashlib.sha256(os.fsencode(resolved)).hexdigest()[:32]


def _token_path(repo_root: Path) -> Path:
    return _user_state_root() / "web" / _repo_state_key(repo_root) / "auth-token"


def _legacy_token_path(repo_root: Path) -> Path:
    return _legacy_web_state_dir(repo_root) / "auth-token"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"Refusing unsafe web credential directory: {path}")
    except TypeError:  # pragma: no cover - Python/platform compatibility guard
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"Refusing unsafe web credential directory: {path}")
        metadata = path.stat()
    if os.name != "nt" and metadata.st_uid != os.geteuid():
        raise OSError(f"Refusing web credential directory owned by another user: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        # Windows protects the user profile through ACL inheritance; chmod is
        # only a best-effort tightening there.
        if os.name != "nt":
            raise


def _read_token_file(path: Path) -> str | None:
    if not path.parent.exists():
        return None
    _ensure_private_directory(path.parent)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeTokenFileError(f"Could not inspect web token {path}: {exc}") from exc
    reparse = bool(
        getattr(before, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if stat.S_ISLNK(before.st_mode) or reparse or not stat.S_ISREG(before.st_mode):
        raise UnsafeTokenFileError(f"Refusing non-regular or link-shaped web token: {path}")
    if before.st_nlink != 1:
        raise UnsafeTokenFileError(f"Refusing multiply-linked web token: {path}")
    if before.st_size > MAX_TOKEN_FILE_BYTES:
        raise UnsafeTokenFileError(f"Web token file is unexpectedly large: {path}")
    if os.name != "nt":
        if before.st_uid != os.geteuid():
            raise UnsafeTokenFileError(f"Refusing web token owned by another user: {path}")
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise UnsafeTokenFileError(
                f"Refusing web token readable by group or other users: {path}; "
                f"run `chmod 600 {path}` or `spec web token --reset`"
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeTokenFileError(f"Could not securely open web token {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeTokenFileError(
                f"Refusing non-regular or multiply-linked web token: {path}"
            )
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise UnsafeTokenFileError(f"Web token changed while being opened: {path}")
        if os.name != "nt" and (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise UnsafeTokenFileError(f"Web token permissions changed while opening: {path}")
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise UnsafeTokenFileError(f"Web token changed while being read: {path}") from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino)
        or (
            os.name != "nt"
            and (
                after.st_uid != os.geteuid()
                or stat.S_IMODE(after.st_mode) & 0o077
            )
        )
    ):
        raise UnsafeTokenFileError(f"Web token changed while being read: {path}")
    if len(payload) > MAX_TOKEN_FILE_BYTES:
        raise UnsafeTokenFileError(f"Web token file is unexpectedly large: {path}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeTokenFileError(f"Web token is not valid UTF-8: {path}") from exc
    token = text.rstrip("\r\n")
    if (
        not token
        or "\n" in token
        or "\r" in token
        or token != token.strip()
        or len(token) < 20
        or not token.isascii()
        or not all(character.isalnum() or character in "-_" for character in token)
    ):
        raise UnsafeTokenFileError(
            f"Web token file must contain one URL-safe bearer token: {path}"
        )
    return token


def _create_token_file(path: Path, token: str) -> bool:
    """Create *path* exclusively, returning False if another process won."""
    _ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            if os.name != "nt":
                raise
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return True


def _replace_token_file(path: Path, token: str) -> None:
    """Atomically replace *path* without following a destination symlink."""
    from spec_runtime.platform_fs import atomic_write_text

    _ensure_private_directory(path.parent)
    # atomic_write_text uses a unique sibling and os.replace. Replacing the
    # directory entry itself does not follow an existing destination symlink,
    # and its Windows retry path handles transient sharing violations.
    atomic_write_text(path, token)
    try:
        path.chmod(0o600)
    except OSError:
        if os.name != "nt":
            raise


def _remove_legacy_token(repo_root: Path) -> None:
    """Remove the old agent-writable token after rotating it during upgrade."""
    try:
        _legacy_token_path(repo_root).unlink(missing_ok=True)
    except OSError:
        # A stale legacy token is no longer consulted and therefore cannot
        # authenticate. Failure to remove it is a cleanup issue, not a reason
        # to fall back to the unsafe location.
        pass


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_LENGTH)


def load_or_create_token(repo_root: Path) -> str:
    path = _token_path(repo_root)
    token = _read_token_file(path)
    if token:
        return token

    token = generate_token()
    if not _create_token_file(path, token):
        token = _read_token_file(path)
        if not token:
            raise OSError(f"Could not read securely-created web token: {path}")

    # Older releases stored this credential inside .spec-state, a directory
    # writable by implementation agents. Never preserve that possibly-known
    # value: rotate during migration, then remove the obsolete copy.
    _remove_legacy_token(repo_root)
    return token


def reset_token(repo_root: Path) -> str:
    path = _token_path(repo_root)
    token = generate_token()
    _replace_token_file(path, token)
    _remove_legacy_token(repo_root)
    return token


def read_token(repo_root: Path) -> str | None:
    return _read_token_file(_token_path(repo_root))


def parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies
