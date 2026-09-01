"""Cross-platform filesystem primitives for runtime state."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import time
import uuid
from pathlib import Path

_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)


def _transient(exc: OSError) -> bool:
    return os.name == "nt" and (
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
    """Replace a file atomically using a unique sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _retry(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)


def remove_tree(path: Path) -> None:
    """Remove a tree while tolerating read-only and transient Windows entries."""
    def onerror(function, name, _exc_info) -> None:
        target = Path(name)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        _retry(lambda: function(name))

    _retry(lambda: shutil.rmtree(path, onerror=onerror))


class FileLock:
    """Exclusive inter-process file lock, optionally non-blocking."""

    def __init__(self, path: Path, *, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(self.file.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
                fcntl.flock(self.file.fileno(), flags)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
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
