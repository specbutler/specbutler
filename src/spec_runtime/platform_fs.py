"""Cross-platform filesystem primitives for runtime state."""

from __future__ import annotations

import errno
import ntpath
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)
_WINDOWS = os.name == "nt"


def _windows_extended_path(path: Path) -> Path:
    """Return an absolute Windows path that is not limited by ``MAX_PATH``."""
    rendered = ntpath.abspath(str(path))
    if rendered.startswith("\\\\?\\"):
        return Path(rendered)
    if rendered.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + rendered.lstrip("\\"))
    return Path("\\\\?\\" + rendered)


def _transient(exc: OSError) -> bool:
    return _WINDOWS and (
        getattr(exc, "winerror", None) in {5, 32, 33}
        or exc.errno in {errno.EACCES, errno.EPERM}
    )


def _retry(operation) -> None:
    for delay in (*_DELAYS, None):
        try:
            operation()
            return
        except OSError as exc:
            if delay is None or not _transient(exc):
                raise
            time.sleep(delay)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace a file atomically using a short, unique sibling temp file.

    The temporary basename deliberately does not include the destination
    basename.  Appending a PID and UUID to a long run-state filename can push
    an otherwise valid path over the legacy Windows ``MAX_PATH`` boundary.
    ``mkstemp`` still gives concurrent writers a unique sibling, which keeps
    the final ``os.replace`` atomic without spending that path-length budget.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=".spec-",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_source = _windows_extended_path(temporary) if _WINDOWS else temporary
        replace_target = _windows_extended_path(path) if _WINDOWS else path
        _retry(lambda: os.replace(replace_source, replace_target))
    finally:
        temporary.unlink(missing_ok=True)


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Remove a tree while tolerating read-only and transient Windows entries."""
    def onerror(function, name, exc_info) -> None:
        exc = exc_info[1]
        if isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) in {2, 3}:
            # Another remover (or a junction/cache cleanup) won the race.
            return
        target = Path(name)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        _retry(lambda: function(name))

    removal_path = _windows_extended_path(path) if _WINDOWS else path
    try:
        _retry(lambda: shutil.rmtree(removal_path, onerror=onerror))
    except OSError:
        if not ignore_errors:
            raise


def _windows_lock_contended(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in {32, 33, 36} or exc.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }


class FileLock:
    """Exclusive inter-process file lock, optionally non-blocking."""

    def __init__(self, path: Path, *, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        self.file = os.fdopen(descriptor, "r+b")
        try:
            if _WINDOWS:
                import msvcrt

                self.file.seek(0)
                while True:
                    try:
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if not _windows_lock_contended(exc):
                            raise
                        if not self.blocking:
                            self.file.close()
                            self.file = None
                            return False
                        time.sleep(_DELAYS[0])
                # Never inspect byte zero before owning it: Windows denies even
                # reads of a byte locked by another process. Initialize a new
                # file, or migrate older POSIX metadata, only while holding the
                # byte-range lock.
                self.file.seek(0)
                content = self.file.read()
                if not content:
                    self.file.seek(0)
                    self.file.write(b"\0")
                    self.file.flush()
                elif not content.startswith(b"\0"):
                    self.file.seek(0)
                    self.file.write(b"\0" + content)
                    self.file.truncate()
                    self.file.flush()
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
                fcntl.flock(self.file.fileno(), flags)
        except OSError:
            self.file.close()
            self.file = None
            if _WINDOWS:
                raise
            return False
        return True

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if _WINDOWS:
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise BlockingIOError(f"lock is held: {self.path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def read_lock_metadata(path: Path) -> str:
    """Read metadata without touching byte zero, which Windows locks exclusively."""
    with path.open("rb") as stream:
        if _WINDOWS:
            stream.seek(1)
        return stream.read().decode("utf-8")


def lock_metadata_offset() -> int:
    """Return the first byte not covered by the platform lock region."""
    return 1 if _WINDOWS else 0
