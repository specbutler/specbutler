"""Portable ownership and identity boundary for host subprocesses.

Workflow code must not make platform-specific process-tree decisions.  This
module preserves POSIX sessions and uses kill-on-close Job Objects on Windows.
Persisted identities are deliberately reopenable and are checked before every
signal, preventing a recycled PID from becoming a termination target.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

_REAL_POPEN_TYPE = subprocess.Popen
_ADOPTED_TOKENS: set[str] = set()


def _kernel32() -> Any:
    """Return kernel32 with pointer-width-safe declarations."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "GlobalMemoryStatusEx": ([ctypes.c_void_p], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
    }
    for name, (argtypes, restype) in declarations.items():
        function = getattr(kernel32, name)
        function.argtypes = argtypes
        function.restype = restype
    return kernel32


def _resume_windows_process(process_handle: int) -> None:
    """Resume every thread in a process created with CREATE_SUSPENDED."""
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    if ntdll.NtResumeProcess(process_handle) != 0:
        raise OSError("NtResumeProcess failed")


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
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        executable = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            executable = buffer.value
        return ProcessIdentity(pid=pid, started_at=str(ticks), executable=executable, command=executable)
    finally:
        kernel32.CloseHandle(handle)


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
        return int(status.total_phys) if _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None


def available_memory_bytes() -> int | None:
    """Return memory available to new work, when the platform exposes it."""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended")
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return int(status.avail_phys) if _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        with open("/proc/meminfo") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_AVPHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None


def iter_processes() -> list[ProcessIdentity]:
    """Return inspectable processes through one platform-owned inventory."""
    if os.name == "nt":
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("pid", wintypes.DWORD),
                ("default_heap", ctypes.POINTER(ctypes.c_ulong)),
                ("module_id", wintypes.DWORD),
                ("threads", wintypes.DWORD),
                ("parent_pid", wintypes.DWORD),
                ("priority", ctypes.c_long),
                ("flags", wintypes.DWORD),
                ("exe_file", wintypes.WCHAR * 260),
            ]

        # Toolhelp is used only to enumerate PIDs; every result is reopened and
        # creation-time validated by inspect_process.
        kernel32 = _kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if not snapshot:
            return []
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            identities: list[ProcessIdentity] = []
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                identity = inspect_process(int(entry.pid))
                if identity is not None:
                    identities.append(identity)
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return identities
        finally:
            kernel32.CloseHandle(snapshot)
    try:
        result = subprocess.run(
            ["ps", "-ww", "-e", "-o", "pid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    identities = []
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        parts = line.strip().split(None, 6)
        if len(parts) == 7 and parts[0].isdigit():
            command = parts[6].strip()
            identities.append(ProcessIdentity(int(parts[0]), " ".join(parts[1:6]), command.split(None, 1)[0], command))
    return identities


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
    def __init__(self, name: str | None = None) -> None:
        self._kernel32 = _kernel32()
        self.handle = self._kernel32.CreateJobObjectW(None, name)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _ExtendedJobLimit()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self, code: int = 1) -> None:
        self._kernel32.TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class ManagedProcess:
    def __init__(self, process: subprocess.Popen[Any], token: SupervisionToken, job: _WindowsJob | None = None):
        self.process = process
        self.token = token
        self._job = job

    def terminate(self, grace_seconds: float = 5.0) -> None:
        terminate(self.token, grace_seconds=grace_seconds, job=self._job)

    def wait(self, timeout: float | None = None) -> int:
        returncode = int(self.process.wait(timeout=timeout))
        self.close()
        return returncode

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


class ManagedAsyncProcess:
    """Asyncio process facade that retains its run-owned Job handle."""

    def __init__(self, process: asyncio.subprocess.Process, token: SupervisionToken, job: _WindowsJob | None = None):
        self.process = process
        self.token = token
        self._job = job

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        if self._job is not None and identity_matches(self.token.identity):
            self._job.terminate()
        else:
            self.process.kill()

    async def wait(self) -> int:
        returncode = int(await self.process.wait())
        self.close()
        return returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes | None, bytes | None]:
        result = await self.process.communicate(input)
        self.close()
        return result

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


class ProcessSupervisor:
    """Launch children with an explicit ownership lifetime."""

    def __init__(self, mode: LifetimeMode = LifetimeMode.RUN_OWNED):
        self.mode = LifetimeMode(mode)
        self._children: list[ManagedProcess | ManagedAsyncProcess] = []

    def spawn(self, argv: Sequence[str], **kwargs: Any) -> ManagedProcess:
        job = None
        if os.name == "nt":
            flags = int(kwargs.pop("creationflags", 0))
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                job = _WindowsJob()
            elif self.mode is LifetimeMode.ADOPTABLE:
                # A detached helper is the durable Job-handle owner.  The
                # dispatcher may exit or restart without closing the payload
                # Job; a replacement validates and adopts the helper token.
                helper_argv = [sys.executable, "-m", "spec_runtime.process_supervisor", "--adoptable-helper", "--", *argv]
                argv = helper_argv
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
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
                _resume_windows_process(int(process._handle))  # type: ignore[attr-defined]
            # Minimal Popen doubles used by callers do not represent a live OS
            # process. Keep that compatibility seam out of production paths.
            is_test_double = not isinstance(process, _REAL_POPEN_TYPE)
            identity = ProcessIdentity(process.pid, "test-double") if is_test_double else inspect_process(process.pid)
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = ProcessIdentity(os.getpid(), "test-double") if is_test_double else inspect_process(os.getpid())
            owner = owner or ProcessIdentity(os.getpid(), "unavailable")
            pgid = 0 if is_test_double else os.getpgid(process.pid) if os.name == "posix" else 0
            token = SupervisionToken(self.mode, identity, owner.pid, owner.started_at, uuid.uuid4().hex, pgid)
            if is_test_double:
                setattr(process, "token", token)
                return process  # type: ignore[return-value]
            managed = ManagedProcess(process, token, job)
            self._children.append(managed)
            return managed
        except Exception:
            if job is not None:
                job.close()
            if hasattr(process, "kill"):
                process.kill()
            raise

    async def spawn_async(self, argv: Sequence[str], **kwargs: Any) -> ManagedAsyncProcess:
        """Async counterpart with the same platform-owned launch policy."""
        job = None
        if os.name == "nt":
            flags = int(kwargs.pop("creationflags", 0))
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                job = _WindowsJob()
            elif self.mode is LifetimeMode.ADOPTABLE:
                argv = [sys.executable, "-m", "spec_runtime.process_supervisor", "--adoptable-helper", "--", *argv]
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            elif self.mode is LifetimeMode.DETACHED:
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        try:
            if job is not None:
                transport_process = process._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
                process_handle = int(transport_process._handle)
                job.assign(process_handle)
                _resume_windows_process(process_handle)
            identity = inspect_process(process.pid)
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = inspect_process(os.getpid()) or ProcessIdentity(os.getpid(), "unavailable")
            pgid = os.getpgid(process.pid) if os.name == "posix" else 0
            token = SupervisionToken(self.mode, identity, owner.pid, owner.started_at, uuid.uuid4().hex, pgid)
            managed = ManagedAsyncProcess(process, token, job)
            self._children.append(managed)
            return managed
        except Exception:
            if job is not None:
                job.close()
            process.kill()
            await process.wait()
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


def adopt(token: SupervisionToken) -> SupervisionToken:
    """Validate a durable adoptable token and transfer logical ownership once."""
    if token.mode is not LifetimeMode.ADOPTABLE:
        raise ValueError("Only adoptable tokens may be adopted")
    if token.token in _ADOPTED_TOKENS:
        raise ValueError("Supervision token was already adopted by this owner")
    if not identity_matches(token.identity):
        raise ProcessLookupError(token.identity.pid)
    owner = inspect_process(os.getpid()) or ProcessIdentity(os.getpid(), "unavailable")
    adopted = SupervisionToken(token.mode, token.identity, owner.pid, owner.started_at, token.token, token.pgid)
    _ADOPTED_TOKENS.add(token.token)
    return adopted


def _adoptable_helper(argv: Sequence[str]) -> int:
    """Own one Windows Job for the full lifetime of an adoptable payload."""
    with ProcessSupervisor(LifetimeMode.RUN_OWNED) as supervisor:
        child = supervisor.spawn(argv)
        return int(child.wait())


if __name__ == "__main__":  # pragma: no cover - exercised by native Windows tests
    marker = "--adoptable-helper"
    if marker in sys.argv:
        separator = sys.argv.index("--")
        raise SystemExit(_adoptable_helper(sys.argv[separator + 1 :]))
