"""Portable ownership and identity boundary for host subprocesses.

Workflow code must not make platform-specific process-tree decisions.  This
module preserves POSIX sessions and uses kill-on-close Job Objects on Windows.
Persisted identities are deliberately reopenable and are checked before every
signal, preventing a recycled PID from becoming a termination target.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence


class LifetimeMode(str, Enum):
    RUN_OWNED = "run-owned"
    ADOPTABLE = "adoptable"
    DETACHED = "detached"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str
    executable: str = ""
    command: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ProcessIdentity:
        return cls(
            pid=int(value.get("pid", 0)),
            started_at=str(value.get("started_at", "")),
            executable=str(value.get("executable", "")),
            command=str(value.get("command", "")),
        )


@dataclass(frozen=True)
class SupervisionToken:
    mode: LifetimeMode
    identity: ProcessIdentity
    owner_pid: int
    owner_started_at: str
    token: str
    pgid: int = 0

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["mode"] = self.mode.value
        return result

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SupervisionToken:
        return cls(
            mode=LifetimeMode(str(value["mode"])),
            identity=ProcessIdentity.from_dict(dict(value["identity"])),  # type: ignore[arg-type]
            owner_pid=int(value.get("owner_pid", 0)),
            owner_started_at=str(value.get("owner_started_at", "")),
            token=str(value.get("token", "")),
            pgid=int(value.get("pgid", 0)),
        )


def _windows_identity(pid: int) -> ProcessIdentity | None:
    from ctypes import wintypes

    process_query = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        executable = ""
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            executable = buffer.value
        return ProcessIdentity(pid=pid, started_at=str(ticks), executable=executable, command=executable)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _posix_identity(pid: int) -> ProcessIdentity | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "pid=", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    parts = line.split(None, 6)
    if result.returncode or len(parts) != 7:
        return None
    try:
        live_pid = int(parts[0])
    except ValueError:
        return None
    command = parts[6].strip()
    executable = command.split(None, 1)[0] if command else ""
    return ProcessIdentity(live_pid, " ".join(parts[1:6]), executable, command)


def inspect_process(pid: int) -> ProcessIdentity | None:
    """Return portable identity information, or ``None`` when unavailable."""
    if pid <= 0:
        return None
    return _windows_identity(pid) if os.name == "nt" else _posix_identity(pid)


def identity_matches(expected: ProcessIdentity) -> bool:
    live = inspect_process(expected.pid)
    if live is None or not expected.started_at or live.started_at != expected.started_at:
        return False
    return not expected.executable or not live.executable or Path(live.executable) == Path(expected.executable)


def process_cwd(pid: int) -> Path | None:
    """Return process cwd where the platform exposes it, otherwise ``None``."""
    if os.name == "nt":
        return None  # Windows has no safe public API for another process cwd.
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError:
        pass
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True)
    except OSError:
        return None
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        if line.startswith("n"):
            try:
                return Path(line[1:]).resolve()
            except OSError:
                return None
    return None


def system_memory_bytes() -> int | None:
    """Return total physical memory without parsing platform command output."""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended")
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return int(status.total_phys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None


class _BasicJobLimit(ctypes.Structure):
    _fields_ = [
        ("per_process", ctypes.c_longlong),
        ("per_job", ctypes.c_longlong),
        ("flags", ctypes.c_uint32),
        ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t),
        ("active", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")
    ]


class _ExtendedJobLimit(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicJobLimit),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _WindowsJob:
    def __init__(self) -> None:
        self.handle = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _ExtendedJobLimit()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not ctypes.windll.kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, process_handle: int) -> None:
        if not ctypes.windll.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self, code: int = 1) -> None:
        ctypes.windll.kernel32.TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class ManagedProcess:
    def __init__(self, process: subprocess.Popen[Any], token: SupervisionToken, job: _WindowsJob | None = None):
        self.process = process
        self.token = token
        self._job = job

    def terminate(self, grace_seconds: float = 5.0) -> None:
        terminate(self.token, grace_seconds=grace_seconds, job=self._job)

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


class ProcessSupervisor:
    """Launch children with an explicit ownership lifetime."""

    def __init__(self, mode: LifetimeMode = LifetimeMode.RUN_OWNED):
        self.mode = LifetimeMode(mode)
        self._children: list[ManagedProcess] = []

    def spawn(self, argv: Sequence[str], **kwargs: Any) -> ManagedProcess:
        job = None
        if os.name == "nt":
            flags = int(kwargs.pop("creationflags", 0))
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                job = _WindowsJob()
            else:
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                if self.mode is LifetimeMode.DETACHED:
                    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(list(argv), **kwargs)
        try:
            if job is not None:
                job.assign(int(process._handle))  # type: ignore[attr-defined]
                if ctypes.windll.ntdll.NtResumeProcess(int(process._handle)) != 0:  # type: ignore[attr-defined]
                    raise OSError("NtResumeProcess failed")
            identity = inspect_process(process.pid)
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = inspect_process(os.getpid()) or ProcessIdentity(os.getpid(), "unavailable")
            pgid = os.getpgid(process.pid) if os.name == "posix" else 0
            token = SupervisionToken(self.mode, identity, owner.pid, owner.started_at, uuid.uuid4().hex, pgid)
            managed = ManagedProcess(process, token, job)
            self._children.append(managed)
            return managed
        except Exception:
            if job is not None:
                job.close()
            process.kill()
            raise

    def close(self) -> None:
        if self.mode is LifetimeMode.RUN_OWNED:
            for child in self._children:
                child.close()

    def __enter__(self) -> ProcessSupervisor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def terminate(token: SupervisionToken, *, grace_seconds: float = 5.0, job: _WindowsJob | None = None) -> bool:
    """Revalidate identity, request cancellation, then kill the owned tree."""
    if not identity_matches(token.identity):
        return False
    try:
        if os.name == "posix":
            os.killpg(token.pgid or token.identity.pid, signal.SIGTERM)
        else:
            os.kill(token.identity.pid, signal.CTRL_BREAK_EVENT)
    except (OSError, ValueError):
        pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not identity_matches(token.identity):
            return True
        time.sleep(0.05)
    if not identity_matches(token.identity):
        return True
    try:
        if os.name == "posix":
            os.killpg(token.pgid or token.identity.pid, signal.SIGKILL)
        elif job is not None:
            job.terminate()
        else:
            # Detached services have no live Job handle; identity validation
            # still makes direct termination safe, but descendants are outside scope.
            os.kill(token.identity.pid, signal.SIGTERM)
    except (OSError, ValueError):
        return False
    return True
