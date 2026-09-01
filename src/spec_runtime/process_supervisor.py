"""Portable ownership and identity boundary for host subprocesses.

Workflow code must not make platform-specific process-tree decisions.  This
module preserves POSIX sessions and uses kill-on-close Job Objects on Windows.
Persisted identities are deliberately reopenable and are checked before every
signal, preventing a recycled PID from becoming a termination target.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from spec_runtime.platform_fs import FileLock, atomic_write_text

_REAL_POPEN_TYPE = subprocess.Popen


def _control_root() -> Path:
    """Return per-user common state shared by launcher, helper, and adopter."""
    configured = os.environ.get("SPEC_PROCESS_CONTROL_ROOT")
    if configured:
        return Path(configured)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SpecButler" / "process-controls"
    user_key = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    return Path(tempfile.gettempdir()) / f"specbutler-process-controls-{user_key}"


def _control_path(relpath: str) -> Path:
    relative = Path(relpath)
    if not relpath or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid supervision control path")
    root = _control_root().resolve()
    resolved = (root / relative).resolve()
    if root not in resolved.parents:
        raise ValueError("invalid supervision control path")
    return resolved

def _kernel32() -> Any:
    """Return kernel32 with pointer-width-safe declarations."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
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
    payload_identity: ProcessIdentity | None = None
    version: int = 2
    job_name: str = ""
    control_relpath: str = ""
    control_nonce: str = ""

    def __post_init__(self) -> None:
        if self.payload_identity is None:
            object.__setattr__(self, "payload_identity", self.identity)
        if self.version >= 2 and not self.control_relpath:
            object.__setattr__(self, "control_relpath", f"controls/{self.token}/control.json")
        if self.version >= 2 and not self.control_nonce:
            object.__setattr__(self, "control_nonce", uuid.uuid4().hex)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["mode"] = self.mode.value
        result["supervisor_identity"] = result["identity"]
        result["payload_identity"] = asdict(self.payload)
        result["supervision_id"] = self.token
        result["keeper_identity"] = result["identity"]
        return result

    @property
    def payload(self) -> ProcessIdentity:
        return self.payload_identity or self.identity

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SupervisionToken:
        version = int(value.get("version", 1))
        if os.name == "nt" and version < 2:
            raise ValueError("legacy Windows supervision tokens are not safe to use")
        keeper_value = value.get("keeper_identity") or value.get("supervisor_identity") or value.get("identity")
        if not isinstance(keeper_value, dict):
            raise ValueError("supervision token has no keeper identity")
        return cls(
            mode=LifetimeMode(str(value["mode"])),
            identity=ProcessIdentity.from_dict(keeper_value),
            owner_pid=int(value.get("owner_pid", 0)),
            owner_started_at=str(value.get("owner_started_at", "")),
            token=str(value.get("supervision_id", value.get("token", ""))),
            pgid=int(value.get("pgid", 0)),
            payload_identity=ProcessIdentity.from_dict(dict(value["payload_identity"]))
            if isinstance(value.get("payload_identity"), dict)
            else None,
            version=version,
            job_name=str(value.get("job_name", "")),
            control_relpath=str(value.get("control_relpath", "")),
            control_nonce=str(value.get("control_nonce", "")),
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
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        # A process object remains queryable while another process retains a
        # handle to it.  That does not make the process live: only
        # STILL_ACTIVE denotes a running process.
        if exit_code.value != 259:
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


def _windows_tree_identities(root_pid: int) -> list[ProcessIdentity]:
    """Snapshot live descendants before closing or terminating their Job."""
    if os.name != "nt":
        return []
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

    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot:
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(int(entry.parent_pid), []).append(int(entry.pid))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    pending = [root_pid]
    seen: set[int] = set()
    identities: list[ProcessIdentity] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(children.get(pid, ()))
        identity = inspect_process(pid)
        if identity is not None:
            identities.append(identity)
    return identities


def _wait_for_identities_exit(identities: Sequence[ProcessIdentity], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    pending = list(identities)
    while pending and time.monotonic() < deadline:
        pending = [identity for identity in pending if identity_matches(identity)]
        if pending:
            time.sleep(0.05)
    return not any(identity_matches(identity) for identity in pending)


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

    @classmethod
    def open(cls, name: str) -> _WindowsJob | None:
        kernel32 = _kernel32()
        handle = kernel32.OpenJobObjectW(0x8, False, name)  # JOB_OBJECT_TERMINATE
        if not handle:
            return None
        job = cls.__new__(cls)
        job._kernel32 = kernel32
        job.handle = handle
        return job

    def assign(self, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self, code: int = 1) -> None:
        self._kernel32.TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


_LIVE_WINDOWS_JOBS: dict[tuple[int, str], _WindowsJob] = {}


def _windows_job_name(token: str) -> str:
    return f"Local\\SpecButler-{token}"


class ManagedProcess:
    def __init__(self, process: subprocess.Popen[Any], token: SupervisionToken, job: _WindowsJob | None = None):
        self.process = process
        self.token = token
        self._job = job

    def terminate(self, grace_seconds: float = 5.0) -> None:
        terminate(self.token, grace_seconds=grace_seconds, job=self._job)

    def kill(self) -> None:
        """Kill the complete owned tree, never just the Popen leader."""
        if self._job is not None and identity_matches(self.token.identity):
            self._job.terminate()
        else:
            terminate(self.token, grace_seconds=0, job=self._job)

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        """Mirror Popen.communicate while releasing ownership on completion.

        A timeout deliberately retains the handle so the caller can kill the
        tree and call communicate again, matching subprocess.run semantics.
        """
        try:
            result = self.process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        self.close()
        return result

    def wait(self, timeout: float | None = None) -> int:
        tree = _windows_tree_identities(self.token.identity.pid)
        returncode = int(self.process.wait(timeout=timeout))
        self.close()
        _wait_for_identities_exit(tree)
        return returncode

    def close(self) -> None:
        if self._job is not None:
            tree = _windows_tree_identities(self.token.identity.pid)
            _LIVE_WINDOWS_JOBS.pop((self.token.identity.pid, self.token.identity.started_at), None)
            self._job.close()
            self._job = None
            _wait_for_identities_exit(tree)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


def run(
    argv: Sequence[str],
    *,
    input: Any = None,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """A tree-safe equivalent of subprocess.run for owned commands."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(argv, **kwargs)
    try:
        stdout, stderr = managed.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        managed.kill()
        stdout, stderr = managed.communicate()
        exc.stdout = stdout
        exc.stderr = stderr
        exc.output = stdout
        raise
    completed = subprocess.CompletedProcess(argv, managed.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


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
        tree = _windows_tree_identities(self.token.identity.pid)
        returncode = int(await self.process.wait())
        self.close()
        if tree:
            await asyncio.to_thread(_wait_for_identities_exit, tree)
        return returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes | None, bytes | None]:
        result = await self.process.communicate(input)
        self.close()
        return result

    def close(self) -> None:
        if self._job is not None:
            tree = _windows_tree_identities(self.token.identity.pid)
            _LIVE_WINDOWS_JOBS.pop((self.token.identity.pid, self.token.identity.started_at), None)
            self._job.close()
            self._job = None
            _wait_for_identities_exit(tree)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


class ProcessSupervisor:
    """Launch children with an explicit ownership lifetime."""

    def __init__(self, mode: LifetimeMode = LifetimeMode.RUN_OWNED, *, supervision_id: str | None = None):
        self.mode = LifetimeMode(mode)
        self._supervision_id = supervision_id
        self._children: list[ManagedProcess | ManagedAsyncProcess] = []

    def spawn(self, argv: Sequence[str], **kwargs: Any) -> ManagedProcess:
        job = None
        metadata_path: Path | None = None
        supervision_id = self._supervision_id or uuid.uuid4().hex
        if os.name == "nt":
            flags = int(kwargs.pop("creationflags", 0))
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                job = _WindowsJob(_windows_job_name(supervision_id))
            elif self.mode in {LifetimeMode.ADOPTABLE, LifetimeMode.DETACHED}:
                # A detached helper is the durable Job-handle owner.  The
                # dispatcher may exit or restart without closing the payload
                # Job; a replacement validates and adopts the helper token.
                metadata_path = Path(kwargs.get("cwd") or os.getcwd()) / f".spec-supervisor-{supervision_id}.json"
                helper_argv = [
                    sys.executable,
                    "-m",
                    "spec_runtime.process_supervisor",
                    "--durable-helper",
                    str(metadata_path),
                    supervision_id,
                    "--",
                    *argv,
                ]
                argv = helper_argv
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            else:
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
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
            identity = (
                ProcessIdentity(int(getattr(process, "pid", os.getpid())), "test-double")
                if is_test_double
                else inspect_process(process.pid)
            )
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = ProcessIdentity(os.getpid(), "test-double") if is_test_double else inspect_process(os.getpid())
            owner = owner or ProcessIdentity(os.getpid(), "unavailable")
            pgid = 0 if is_test_double else os.getpgid(process.pid) if os.name == "posix" else 0
            payload_identity = identity
            helper_token: SupervisionToken | None = None
            if metadata_path is not None and not is_test_double:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and not metadata_path.exists():
                    if process.poll() is not None:
                        raise RuntimeError("Durable process supervisor exited before publishing payload identity")
                    time.sleep(0.02)
                try:
                    helper_token = SupervisionToken.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
                    payload_identity = helper_token.payload
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Durable process supervisor did not publish payload identity") from exc
                finally:
                    metadata_path.unlink(missing_ok=True)
            job_name = _windows_job_name(supervision_id) if job is not None else ""
            if helper_token is not None:
                job_name = helper_token.job_name
                supervision_id = helper_token.token
            token = SupervisionToken(
                self.mode,
                identity,
                owner.pid,
                owner.started_at,
                supervision_id,
                pgid,
                payload_identity,
                job_name=job_name,
                control_relpath=helper_token.control_relpath if helper_token is not None else "",
                control_nonce=helper_token.control_nonce if helper_token is not None else "",
            )
            if is_test_double:
                setattr(process, "token", token)
                return process  # type: ignore[return-value]
            managed = ManagedProcess(process, token, job)
            if job is not None:
                _LIVE_WINDOWS_JOBS[(identity.pid, identity.started_at)] = job
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
        metadata_path: Path | None = None
        supervision_id = self._supervision_id or uuid.uuid4().hex
        if os.name == "nt":
            flags = int(kwargs.pop("creationflags", 0))
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                job = _WindowsJob(_windows_job_name(supervision_id))
            elif self.mode in {LifetimeMode.ADOPTABLE, LifetimeMode.DETACHED}:
                metadata_path = Path(kwargs.get("cwd") or os.getcwd()) / f".spec-supervisor-{supervision_id}.json"
                argv = [
                    sys.executable,
                    "-m",
                    "spec_runtime.process_supervisor",
                    "--durable-helper",
                    str(metadata_path),
                    supervision_id,
                    "--",
                    *argv,
                ]
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        try:
            if job is not None:
                if isinstance(process, asyncio.subprocess.Process):
                    transport_process = process._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
                    process_handle = int(transport_process._handle)
                    job.assign(process_handle)
                    _resume_windows_process(process_handle)
                else:
                    # Explicit compatibility seam for lightweight async test
                    # doubles. Real Windows asyncio processes must expose the
                    # transport handle so assignment occurs before resume.
                    job.close()
                    job = None
            is_test_double = not isinstance(process, asyncio.subprocess.Process)
            identity = ProcessIdentity(process.pid, "test-double") if is_test_double else inspect_process(process.pid)
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = ProcessIdentity(os.getpid(), "test-double") if is_test_double else inspect_process(os.getpid())
            owner = owner or ProcessIdentity(os.getpid(), "unavailable")
            pgid = 0 if is_test_double else os.getpgid(process.pid) if os.name == "posix" else 0
            payload_identity = identity
            helper_token: SupervisionToken | None = None
            if metadata_path is not None and not is_test_double:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and not metadata_path.exists():
                    if process.returncode is not None:
                        raise RuntimeError("Durable process supervisor exited before publishing payload identity")
                    await asyncio.sleep(0.02)
                try:
                    helper_token = SupervisionToken.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
                    payload_identity = helper_token.payload
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Durable process supervisor did not publish payload identity") from exc
                finally:
                    metadata_path.unlink(missing_ok=True)
            job_name = _windows_job_name(supervision_id) if job is not None else ""
            if helper_token is not None:
                job_name = helper_token.job_name
                supervision_id = helper_token.token
            token = SupervisionToken(
                self.mode,
                identity,
                owner.pid,
                owner.started_at,
                supervision_id,
                pgid,
                payload_identity,
                job_name=job_name,
                control_relpath=helper_token.control_relpath if helper_token is not None else "",
                control_nonce=helper_token.control_nonce if helper_token is not None else "",
            )
            managed = ManagedAsyncProcess(process, token, job)
            if job is not None:
                _LIVE_WINDOWS_JOBS[(identity.pid, identity.started_at)] = job
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
    if job is None and os.name == "nt":
        job = _LIVE_WINDOWS_JOBS.get((token.identity.pid, token.identity.started_at))
    tree = _windows_tree_identities(token.payload.pid)
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
            return _wait_for_identities_exit(tree) if tree else True
        time.sleep(0.05)
    if not identity_matches(token.identity):
        return _wait_for_identities_exit(tree) if tree else True
    reopened_job = None
    if os.name == "nt" and job is None:
        reopened_job = _WindowsJob.open(token.job_name or _windows_job_name(token.token))
        job = reopened_job
        if job is None:
            # A live payload with no reopenable ownership primitive must never
            # degrade to signaling a PID (including the helper PID).
            return False
    try:
        if os.name == "posix":
            os.killpg(token.pgid or token.identity.pid, signal.SIGKILL)
        elif job is not None:
            job.terminate()
        else:
            return False
    except (OSError, ValueError):
        if reopened_job is not None:
            reopened_job.close()
        return False
    result = _wait_for_identities_exit(tree) if tree else True
    if reopened_job is not None:
        reopened_job.close()
    return result


def adopt(token: SupervisionToken) -> SupervisionToken:
    """Validate a durable adoptable token and transfer logical ownership once."""
    if token.mode is not LifetimeMode.ADOPTABLE:
        raise ValueError("Only adoptable tokens may be adopted")
    if not identity_matches(token.identity):
        raise ProcessLookupError(token.identity.pid)
    owner = inspect_process(os.getpid()) or ProcessIdentity(os.getpid(), "unavailable")
    control_path = _control_path(token.control_relpath)
    with FileLock(control_path.with_suffix(".lock")):
        try:
            state = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("adoptable token has no valid control record") from exc
        expected = {
            "schema": 2,
            "supervision_id": token.token,
            "nonce": token.control_nonce,
            "keeper_identity": token.identity.to_dict(),
            "payload_identity": token.payload.to_dict(),
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("adoptable token control record does not match")
        if state.get("adopted_by") is not None:
            raise ValueError("adoptable token was already adopted")
        state["adopted_by"] = owner.to_dict()
        atomic_write_text(control_path, json.dumps(state, sort_keys=True))
    adopted = SupervisionToken(
        token.mode,
        token.identity,
        owner.pid,
        owner.started_at,
        token.token,
        token.pgid,
        token.payload_identity,
        job_name=token.job_name,
        control_relpath=token.control_relpath,
        control_nonce=token.control_nonce,
    )
    return adopted


def _durable_helper(metadata_path: Path, supervision_id: str, argv: Sequence[str]) -> int:
    """Own one Windows Job for the full lifetime of a durable payload."""
    with ProcessSupervisor(LifetimeMode.RUN_OWNED, supervision_id=supervision_id) as supervisor:
        child = supervisor.spawn(argv)
        keeper = inspect_process(os.getpid())
        if keeper is None:
            raise RuntimeError("Could not inspect durable supervisor identity")
        token = SupervisionToken(
            LifetimeMode.ADOPTABLE,
            keeper,
            0,
            "",
            supervision_id,
            payload_identity=child.token.identity,
            job_name=child.token.job_name,
        )
        control_path = _control_path(token.control_relpath)
        state = {
            "schema": 2,
            "supervision_id": token.token,
            "nonce": token.control_nonce,
            "keeper_identity": token.identity.to_dict(),
            "payload_identity": token.payload.to_dict(),
            "adopted_by": None,
        }
        with FileLock(control_path.with_suffix(".lock")):
            atomic_write_text(control_path, json.dumps(state, sort_keys=True))
        atomic_write_text(metadata_path, json.dumps(token.to_dict()))
        return int(child.wait())


if __name__ == "__main__":  # pragma: no cover - exercised by native Windows tests
    marker = "--durable-helper"
    if marker in sys.argv:
        separator = sys.argv.index("--")
        marker_index = sys.argv.index(marker)
        raise SystemExit(
            _durable_helper(Path(sys.argv[marker_index + 1]), sys.argv[marker_index + 2], sys.argv[separator + 1 :])
        )
