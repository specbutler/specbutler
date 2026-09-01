"""Portable ownership and identity boundary for host subprocesses.

Workflow code must not make platform-specific process-tree decisions.  This
module preserves POSIX sessions and uses kill-on-close Job Objects on Windows.
Persisted identities are deliberately reopenable and are checked before every
signal, preventing a recycled PID from becoming a termination target.
"""

from __future__ import annotations

import asyncio
import atexit
import bisect
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from spec_runtime.platform_fs import FileLock, atomic_write_text

_REAL_POPEN_TYPE = subprocess.Popen
_CONTROL_RECONCILE_LIMIT = 256
_ORPHAN_LOCK_MIN_AGE_SECONDS = 3600.0
_CONTROL_STATE_RECONCILE_LOCK = threading.Lock()
_CONTROL_RECONCILE_CURSOR_FILENAME = "reconcile-cursor.json"
_CONTROL_RECONCILE_CURSOR_LOCK_FILENAME = "reconcile-cursor.lock"
_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (?P<bytes>\d+) bytes", re.IGNORECASE)
_VM_STAT_RECLAIMABLE_KEYS = frozenset(
    {
        "pages free",
        "pages inactive",
        "pages speculative",
    }
)


def _platform_is_windows() -> bool:
    """Return whether native Windows process semantics apply."""
    return os.name == "nt"


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


def durable_metadata_path(supervision_id: str) -> Path:
    """Return the writable, launcher-independent helper handshake path."""
    if not supervision_id or Path(supervision_id).name != supervision_id:
        raise ValueError("invalid supervision id")
    return _control_root() / "metadata" / f"{supervision_id}.json"


def _durable_publication_ack_path(metadata_path: Path) -> Path:
    return metadata_path.with_suffix(metadata_path.suffix + ".ack")


def _kernel32() -> Any:
    """Return kernel32 with pointer-width-safe declarations."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
            wintypes.BOOL,
        ),
        "GenerateConsoleCtrlEvent": ([wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
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


def _control_record_identities(
    state: object,
    supervision_id: str,
) -> tuple[str, ProcessIdentity, ProcessIdentity] | None:
    """Return authenticated record fields needed for conservative cleanup."""
    if not isinstance(state, dict):
        return None
    nonce = state.get("nonce")
    keeper_value = state.get("keeper_identity")
    payload_value = state.get("payload_identity")
    if (
        state.get("schema") != 2
        or state.get("supervision_id") != supervision_id
        or state.get("job_name") != _windows_job_name(supervision_id)
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(keeper_value, dict)
        or not isinstance(payload_value, dict)
    ):
        return None
    try:
        keeper = ProcessIdentity.from_dict(keeper_value)
        payload = ProcessIdentity.from_dict(payload_value)
    except (TypeError, ValueError):
        return None
    if keeper.pid <= 0 or payload.pid <= 0 or not keeper.started_at or not payload.started_at:
        return None
    return nonce, keeper, payload


class ProcessGroupOwnershipError(RuntimeError):
    """A live group cannot be tied to its recorded leader identity."""


class ProcessGroupTerminationError(RuntimeError):
    """A proven owned process group survived bounded termination."""


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
        # Programmatic token creation is the trusted minting boundary. Parsing
        # below is intentionally strict and never repairs persisted V2 data.
        if self.version >= 2 and not self.job_name:
            object.__setattr__(self, "job_name", _windows_job_name(self.token))
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
        try:
            version = int(value.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid supervision token version") from exc
        if version not in {1, 2}:
            raise ValueError("unsupported supervision token version")
        if _platform_is_windows() and version < 2:
            raise ValueError("legacy Windows supervision tokens are not safe to use")
        if version >= 2:
            required = (
                "supervision_id",
                "job_name",
                "keeper_identity",
                "payload_identity",
                "control_relpath",
                "control_nonce",
            )
            missing = [key for key in required if not value.get(key)]
            if missing:
                raise ValueError(f"V2 supervision token is missing {', '.join(missing)}")
            supervision_id = str(value["supervision_id"])
            expected_job_name = _windows_job_name(supervision_id)
            legacy_posix_job_name = f"Local\\SpecButler-{supervision_id}"
            accepted_job_names = {expected_job_name}
            if sys.platform != "win32":
                # V2 tokens minted before native Windows support used the
                # session-local spelling on every platform.  POSIX never
                # opens the Job name, so retaining that persisted spelling is
                # state compatibility, not an ownership relaxation.  Windows
                # must remain strict: a Local Job cannot be reopened across
                # logon sessions and is not the canonical ownership boundary.
                accepted_job_names.add(legacy_posix_job_name)
            if value["job_name"] not in accepted_job_names:
                raise ValueError("V2 supervision token has a noncanonical Job name")
            expected_control_relpath = f"controls/{supervision_id}/control.json"
            if value["control_relpath"] != expected_control_relpath:
                raise ValueError("V2 supervision token has a noncanonical control path")
            _control_path(expected_control_relpath)
        keeper_value = value.get("keeper_identity") or value.get("supervisor_identity") or value.get("identity")
        if not isinstance(keeper_value, dict):
            raise ValueError("supervision token has no keeper identity")
        keeper = ProcessIdentity.from_dict(keeper_value)
        payload_value = value.get("payload_identity")
        payload = ProcessIdentity.from_dict(payload_value) if isinstance(payload_value, dict) else None
        if version == 2 and (keeper.pid <= 0 or payload is None or payload.pid <= 0):
            raise ValueError("V2 supervision token identities must have positive PIDs")
        return cls(
            mode=LifetimeMode(str(value["mode"])),
            identity=keeper,
            owner_pid=int(value.get("owner_pid", 0)),
            owner_started_at=str(value.get("owner_started_at", "")),
            token=str(value.get("supervision_id", value.get("token", ""))),
            pgid=int(value.get("pgid", 0)),
            payload_identity=payload,
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
            errors="replace",
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


def _darwin_framework_python_role(executable: str) -> tuple[Path, str] | None:
    """Identify the two executables in a versioned macOS Python framework.

    Python.org framework builds launch through ``Versions/X.Y/bin/python*``.
    That stub then execs the distinct executable inside ``Python.app`` without
    changing PID or process start time.  Resolve a venv symlink first so the
    same transition is recognized when Spec itself runs from a virtualenv.
    """
    path = Path(executable)
    candidates = [path]
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        pass
    else:
        if resolved != path:
            candidates.insert(0, resolved)

    app_suffix = ("Resources", "Python.app", "Contents", "MacOS", "Python")
    for candidate in candidates:
        parts = candidate.parts
        for index, part in enumerate(parts):
            if part != "Python.framework":
                continue
            if index + 2 >= len(parts) or parts[index + 1] != "Versions":
                continue
            version_root = Path(*parts[: index + 3])
            suffix = parts[index + 3 :]
            if len(suffix) == 2 and suffix[0] == "bin" and suffix[1].lower().startswith("python"):
                return version_root, "stub"
            if suffix == app_suffix:
                return version_root, "app"
    return None


def _darwin_framework_python_exec_transition_matches(
    expected_executable: str,
    live_executable: str,
) -> bool:
    if sys.platform != "darwin":
        return False
    expected = _darwin_framework_python_role(expected_executable)
    live = _darwin_framework_python_role(live_executable)
    if expected is None or live is None or {expected[1], live[1]} != {"stub", "app"}:
        return False
    try:
        return os.path.samefile(expected[0], live[0])
    except OSError:
        return expected[0].resolve(strict=False) == live[0].resolve(strict=False)


def identity_matches(expected: ProcessIdentity) -> bool:
    live = inspect_process(expected.pid)
    if live is None or not expected.started_at or live.started_at != expected.started_at:
        return False
    if not expected.executable or not live.executable:
        return True
    try:
        # Ordinary executable aliases resolve to the same file.  CPython's
        # Darwin framework stub and app runtime are distinct files joined by
        # an in-place exec, so that narrower transition is handled below.
        if os.path.samefile(live.executable, expected.executable):
            return True
    except OSError:
        if Path(live.executable).resolve(strict=False) == Path(
            expected.executable
        ).resolve(strict=False):
            return True
    return _darwin_framework_python_exec_transition_matches(
        expected.executable,
        live.executable,
    )


def list_live_process_group_members(pgid: int) -> list[int] | None:
    """Return non-zombie POSIX group members, or ``None`` if inventory fails."""
    if pgid <= 0 or os.name != "posix":
        return []
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    members: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            live_pid = int(parts[0])
            live_pgid = int(parts[1])
        except ValueError:
            continue
        stat = parts[2].strip().upper()
        if live_pgid == pgid and stat and not stat.startswith("Z"):
            members.append(live_pid)
    return members


def is_process_group_alive(pgid: int) -> bool:
    """Return complete POSIX group liveness without relying on its leader."""
    if pgid <= 0 or os.name != "posix":
        return False
    members = list_live_process_group_members(pgid)
    if members is not None:
        return bool(members)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def process_cwd(pid: int) -> Path | None:
    """Return process cwd where the platform exposes it, otherwise ``None``."""
    if os.name == "nt":
        return None  # Windows has no safe public API for another process cwd.
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            errors="replace",
        )
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


def _meminfo_available_bytes() -> int | None:
    """Return Linux MemAvailable, including reclaimable page cache."""
    try:
        with open("/proc/meminfo") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) * 1024
                    break
    except OSError:
        pass
    return None


def _sysconf_value(name: str) -> int | None:
    try:
        value = os.sysconf(name)
    except (AttributeError, OSError, ValueError):
        return None
    return value if isinstance(value, int) and value >= 0 else None


def _vm_stat_available_bytes() -> int | None:
    """Return macOS reclaimable memory from vm_stat output."""
    try:
        result = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    page_size_match = _VM_STAT_PAGE_SIZE_RE.search(result.stdout)
    if page_size_match is None:
        return None

    reclaimable_pages = 0
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        if key.strip().lower() not in _VM_STAT_RECLAIMABLE_KEYS:
            continue
        digits = "".join(character for character in raw_value if character.isdigit())
        if digits:
            reclaimable_pages += int(digits)
    return reclaimable_pages * int(page_size_match.group("bytes"))


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
    meminfo = _meminfo_available_bytes()
    if meminfo is not None:
        return meminfo
    pages = _sysconf_value("SC_AVPHYS_PAGES")
    page_size = _sysconf_value("SC_PAGE_SIZE") or _sysconf_value("SC_PAGESIZE")
    if pages is not None and page_size is not None:
        return pages * page_size
    return _vm_stat_available_bytes()


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
            errors="replace",
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
        # Query access is part of the ownership proof: a named Job is useful
        # only when the candidate payload can be shown to be one of its active
        # members.  Never fall back to trusting a PID from persisted state.
        handle = kernel32.OpenJobObjectW(0xC, False, name)  # QUERY | TERMINATE
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
        if not self.handle:
            return
        if not self._kernel32.TerminateJobObject(self.handle, code):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def active_process_ids(self) -> tuple[int, ...]:
        """Return the Job's active members from the kernel-owned membership list."""
        from ctypes import wintypes

        capacity = 16
        pointer_size = ctypes.sizeof(ctypes.c_size_t)
        while True:
            size = 8 + capacity * pointer_size
            buffer = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD()
            ok = self._kernel32.QueryInformationJobObject(
                self.handle,
                3,  # JobObjectBasicProcessIdList
                buffer,
                size,
                ctypes.byref(returned),
            )
            assigned = ctypes.c_uint32.from_buffer(buffer, 0).value
            included = ctypes.c_uint32.from_buffer(buffer, 4).value
            if ok and included >= assigned:
                if included == 0:
                    return ()
                array_type = ctypes.c_size_t * included
                members = array_type.from_buffer(buffer, 8)
                return tuple(int(pid) for pid in members)
            if assigned > capacity:
                capacity = assigned
                continue
            if not ok:
                raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
            # The Job grew between the query and the returned snapshot. Grow
            # geometrically rather than accepting an incomplete membership
            # list at a security boundary.
            capacity *= 2

    def active_identities(self) -> list[ProcessIdentity]:
        return [identity for pid in self.active_process_ids() if (identity := inspect_process(pid)) is not None]

    def contains(self, identity: ProcessIdentity) -> bool:
        return identity_matches(identity) and identity.pid in self.active_process_ids()

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not self.active_process_ids():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def close(self) -> None:
        if self.handle:
            handle = self.handle
            if not self._kernel32.CloseHandle(handle):
                raise OSError(ctypes.get_last_error(), "CloseHandle failed")
            self.handle = None


_LIVE_WINDOWS_JOBS: dict[tuple[int, str], _WindowsJob] = {}
_CURRENT_WINDOWS_JOBS: dict[str, _WindowsJob] = {}


def _windows_job_name(token: str) -> str:
    # A Local\\ object is scoped to one Windows logon session. Web and
    # autopilot processes commonly start in an interactive session but are
    # later inspected or stopped over SSH in session 0, so their ownership
    # capability must live in the cross-session namespace. Kernel objects
    # other than file mappings and symbolic links do not require
    # SeCreateGlobalPrivilege for this use.
    return f"Global\\SpecButler-{token}"


def _send_windows_break(process_group_id: int) -> bool:
    """Signal a Windows console group even after its leader shim has exited."""
    if process_group_id <= 0:
        return False
    return bool(_kernel32().GenerateConsoleCtrlEvent(1, process_group_id))  # CTRL_BREAK_EVENT


def _terminate_held_windows_job(token: SupervisionToken, job: _WindowsJob, grace_seconds: float) -> bool:
    """Gracefully, then forcibly, stop a Job held by this trusted process."""
    try:
        if not job.active_process_ids():
            return True
    except OSError:
        # The retained handle is still an ownership capability. A query
        # failure prevents graceful proof but must not prevent hard cleanup.
        pass
    try:
        _send_windows_break(token.pgid)
    except OSError:
        pass
    try:
        if job.wait_empty(grace_seconds):
            return True
    except OSError:
        pass
    try:
        identities = job.active_identities()
    except OSError:
        identities = []
    try:
        job.terminate()
    except OSError:
        return False
    return _wait_for_identities_exit(identities) if identities else True


def _abort_windows_job(job: _WindowsJob | None) -> None:
    """Best-effort ownership cleanup that never masks the launching exception."""
    if job is None:
        return
    try:
        job.terminate()
    except BaseException:
        pass
    try:
        job.close()
    except BaseException:
        pass


def _load_durable_publication(
    metadata_path: Path,
    *,
    supervision_id: str,
    control_nonce: str,
    mode: LifetimeMode,
) -> SupervisionToken | None:
    """Read only the token published by this exact helper launch."""
    try:
        token = SupervisionToken.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        token.token != supervision_id
        or token.control_nonce != control_nonce
        or token.mode is not mode
        or not identity_matches(token.identity)
    ):
        return None
    return token


def _same_durable_owner(candidate: SupervisionToken, token: SupervisionToken) -> bool:
    """Compare immutable ownership fields while allowing payload promotion."""
    return bool(
        candidate.token == token.token
        and candidate.mode is token.mode
        and candidate.identity == token.identity
        and candidate.job_name == token.job_name
        and candidate.control_relpath == token.control_relpath
        and candidate.control_nonce == token.control_nonce
    )


def _acknowledge_durable_publication(metadata_path: Path, token: SupervisionToken) -> None:
    """Tell a fast-exiting helper that the launcher retained its token."""
    payload = {
        "schema": 1,
        "supervision_id": token.token,
        "nonce": token.control_nonce,
        "keeper_identity": token.identity.to_dict(),
    }
    atomic_write_text(
        _durable_publication_ack_path(metadata_path),
        json.dumps(payload, sort_keys=True),
    )


def _durable_publication_acknowledged(metadata_path: Path, token: SupervisionToken) -> bool:
    try:
        payload = json.loads(
            _durable_publication_ack_path(metadata_path).read_text(encoding="utf-8")
        )
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema") == 1
        and payload.get("supervision_id") == token.token
        and payload.get("nonce") == token.control_nonce
        and payload.get("keeper_identity") == token.identity.to_dict()
    )


def _await_durable_publication(
    metadata_path: Path,
    token: SupervisionToken,
    publisher: ProcessIdentity,
) -> None:
    """Retain the handshake until its live publisher has consumed it."""
    try:
        published = SupervisionToken.from_dict(
            json.loads(metadata_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Publication never completed, so there is nothing for the launcher
        # to acknowledge and partial control state can be retired immediately.
        return
    if not _same_durable_owner(published, token):
        return
    while identity_matches(publisher):
        if _durable_publication_acknowledged(metadata_path, token):
            return
        time.sleep(0.02)


def _retire_durable_records(metadata_path: Path, token: SupervisionToken) -> bool:
    """Remove only this helper's authenticated records after its Job is empty."""
    try:
        if token.identity.pid != os.getpid() or not identity_matches(token.identity):
            return False
        if metadata_path.resolve() != durable_metadata_path(token.token).resolve():
            return False
        control_path = _control_path(token.control_relpath)
        lock_path = control_path.with_suffix(".lock")
        removed = False
        with FileLock(lock_path):
            try:
                state = json.loads(control_path.read_text(encoding="utf-8"))
                payload_value = state.get("payload_identity")
                if not isinstance(payload_value, dict):
                    return False
                ProcessIdentity.from_dict(payload_value)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False
            if (
                not isinstance(state, dict)
                or state.get("schema") != 2
                or state.get("supervision_id") != token.token
                or state.get("job_name") != token.job_name
                or state.get("nonce") != token.control_nonce
                or state.get("keeper_identity") != token.identity.to_dict()
            ):
                return False
            try:
                persisted = SupervisionToken.from_dict(
                    json.loads(metadata_path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                persisted = None
            metadata_owned = persisted is not None and _same_durable_owner(persisted, token)
            # Retire authorization before its discovery record. A crash
            # between these unlinks therefore leaves a fail-closed token.
            control_path.unlink(missing_ok=True)
            if metadata_owned:
                metadata_path.unlink(missing_ok=True)
            removed = True
        if removed:
            if _durable_publication_acknowledged(metadata_path, token):
                _durable_publication_ack_path(metadata_path).unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
            try:
                control_path.parent.rmdir()
            except OSError:
                pass
        return removed
    except BaseException:
        return False


def _windows_job_definitively_absent(job_name: str) -> bool:
    """Return true only when Windows proves that a named Job does not exist."""
    if os.name != "nt":
        return True
    job = _WindowsJob.open(job_name)
    if job is None:
        # Access denied and transient API failures are not absence proofs. The
        # Win32 object manager reports ERROR_FILE_NOT_FOUND when the final Job
        # handle has closed and the name no longer exists.
        return ctypes.get_last_error() == 2
    try:
        return False
    finally:
        job.close()


def _control_boundary_is_inactive(
    supervision_id: str,
    keeper: ProcessIdentity,
    payload: ProcessIdentity,
) -> bool:
    return bool(
        not identity_matches(keeper)
        and not identity_matches(payload)
        and _windows_job_definitively_absent(_windows_job_name(supervision_id))
    )


def _same_reconciled_boundary(
    token: SupervisionToken,
    *,
    supervision_id: str,
    nonce: str,
    keeper: ProcessIdentity,
    payload: ProcessIdentity,
) -> bool:
    return bool(
        token.version == 2
        and token.token == supervision_id
        and token.control_nonce == nonce
        and token.identity == keeper
        and token.payload == payload
        and token.job_name == _windows_job_name(supervision_id)
        and token.control_relpath == f"controls/{supervision_id}/control.json"
    )


def _read_durable_token(metadata_path: Path) -> SupervisionToken | None:
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        return SupervisionToken.from_dict(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _remove_empty_control_artifacts(control_path: Path, metadata_path: Path) -> None:
    """Best-effort cleanup after releasing the per-boundary file lock."""
    try:
        control_path.with_suffix(".lock").unlink(missing_ok=True)
    except OSError:
        pass
    # The per-ID control directory is unique to this boundary. ``metadata/``
    # is shared by every publisher, so removing it can race atomic_write_text
    # between its parent mkdir and temporary-file creation.
    try:
        control_path.parent.rmdir()
    except OSError:
        pass


def _reconcile_control_record(control_path: Path, supervision_id: str) -> bool:
    """Retire one dead control record without trusting its path or PID alone."""
    metadata_path = durable_metadata_path(supervision_id)
    lock_path = control_path.with_suffix(".lock")
    try:
        initial_state = json.loads(control_path.read_text(encoding="utf-8"))
        initial_identities = _control_record_identities(initial_state, supervision_id)
        if initial_identities is None or not _control_boundary_is_inactive(
            supervision_id,
            initial_identities[1],
            initial_identities[2],
        ):
            return False
    except (OSError, TypeError, json.JSONDecodeError):
        return False
    lock = FileLock(lock_path, blocking=False)
    try:
        if not lock.acquire():
            return False
        try:
            try:
                state = json.loads(control_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, json.JSONDecodeError):
                return False
            identities = _control_record_identities(state, supervision_id)
            if identities is None:
                return False
            nonce, keeper, payload = identities
            if not _control_boundary_is_inactive(supervision_id, keeper, payload):
                return False

            metadata_token = _read_durable_token(metadata_path)
            metadata_owned = metadata_token is not None and _same_reconciled_boundary(
                metadata_token,
                supervision_id=supervision_id,
                nonce=nonce,
                keeper=keeper,
                payload=payload,
            )
            ack_owned = bool(
                metadata_owned
                and metadata_token is not None
                and _durable_publication_acknowledged(metadata_path, metadata_token)
            )
            # Retire authorization before discovery. A crash between these
            # unlinks leaves a fail-closed metadata token.
            control_path.unlink()
            if metadata_owned:
                metadata_path.unlink(missing_ok=True)
            if ack_owned:
                _durable_publication_ack_path(metadata_path).unlink(missing_ok=True)
        finally:
            lock.release()
    except (OSError, ValueError):
        return False
    _remove_empty_control_artifacts(control_path, metadata_path)
    return True


def _reconcile_orphan_metadata(metadata_path: Path, supervision_id: str) -> bool:
    """Retire one dead discovery record after proving control is absent."""
    control_path = _control_path(f"controls/{supervision_id}/control.json")
    if control_path.exists():
        return False
    metadata_token = _read_durable_token(metadata_path)
    if metadata_token is None or metadata_token.token != supervision_id:
        return False
    if not _control_boundary_is_inactive(
        supervision_id,
        metadata_token.identity,
        metadata_token.payload,
    ):
        return False

    lock = FileLock(control_path.with_suffix(".lock"), blocking=False)
    try:
        if not lock.acquire():
            return False
        try:
            if control_path.exists():
                return False
            current = _read_durable_token(metadata_path)
            if current != metadata_token or not _control_boundary_is_inactive(
                supervision_id,
                current.identity,
                current.payload,
            ):
                return False
            ack_owned = _durable_publication_acknowledged(metadata_path, current)
            metadata_path.unlink()
            if ack_owned:
                _durable_publication_ack_path(metadata_path).unlink(missing_ok=True)
        finally:
            lock.release()
    except (OSError, ValueError):
        return False
    _remove_empty_control_artifacts(control_path, metadata_path)
    return True


def _reconcile_lock_only_directory(directory: Path, supervision_id: str) -> bool:
    """Remove an aged, unlocked artifact after proving no boundary exists."""
    control_path = directory / "control.json"
    metadata_path = durable_metadata_path(supervision_id)
    lock_path = control_path.with_suffix(".lock")
    try:
        if (
            control_path.exists()
            or metadata_path.exists()
            or lock_path.is_symlink()
            or not lock_path.is_file()
            or time.time() - lock_path.stat().st_mtime < _ORPHAN_LOCK_MIN_AGE_SECONDS
            or not _windows_job_definitively_absent(_windows_job_name(supervision_id))
        ):
            return False
    except OSError:
        return False

    lock = FileLock(lock_path, blocking=False)
    try:
        if not lock.acquire():
            return False
        try:
            # A same-ID launch creates its Job before acquiring this lock.
            # Rechecking both facts while serialized closes the scan/launch
            # race without inferring ownership from an old filename.
            if (
                control_path.exists()
                or metadata_path.exists()
                or not _windows_job_definitively_absent(_windows_job_name(supervision_id))
            ):
                return False
        finally:
            lock.release()
    except OSError:
        return False
    _remove_empty_control_artifacts(control_path, metadata_path)
    return not lock_path.exists()


def _read_control_reconcile_cursor(cursor_path: Path) -> str:
    try:
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return ""
    after = payload.get("after")
    return after if isinstance(after, str) else ""


def _write_control_reconcile_cursor(cursor_path: Path, after: str) -> None:
    atomic_write_text(
        cursor_path,
        json.dumps({"schema": 1, "after": after}, sort_keys=True) + "\n",
    )


def _control_reconcile_entries(
    controls_root: Path,
    metadata_root: Path,
) -> list[tuple[str, str, Path]]:
    """Return stable control/metadata entries for one circular scan.

    The entry key is persisted rather than an array offset so deletions do not
    reset progress.  A single ordered namespace also prevents a large control
    directory from consuming every batch before metadata is ever considered.
    """
    entries: list[tuple[str, str, Path]] = []
    try:
        if controls_root.is_symlink():
            raise OSError("control directory is a symlink")
        entries.extend(
            (f"{path.name}/1-control", "control", path)
            for path in controls_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    except OSError:
        pass
    try:
        if metadata_root.is_symlink():
            raise OSError("metadata directory is a symlink")
        entries.extend(
            (f"{path.stem}/0-metadata", "metadata", path)
            for path in metadata_root.glob("*.json")
            if path.is_file() and not path.is_symlink()
        )
    except OSError:
        pass
    return sorted(entries, key=lambda entry: entry[0])


def _control_reconcile_batch(
    entries: Sequence[tuple[str, str, Path]],
    *,
    after: str,
    limit: int,
) -> list[tuple[str, str, Path]]:
    """Select at most *limit* entries after *after*, wrapping once."""
    if not entries or limit <= 0:
        return []
    keys = [entry[0] for entry in entries]
    start = bisect.bisect_right(keys, after)
    ordered = [*entries[start:], *entries[:start]]
    return ordered[:limit]


def reconcile_stale_control_state(*, limit: int = _CONTROL_RECONCILE_LIMIT) -> int:
    """Bounded, fail-closed reconciliation of dead Windows control records.

    Records are removed only when both persisted process identities are dead,
    the canonical named Job is proven absent, and no writer owns the record's
    lock. Malformed or mismatched files are retained for diagnosis.
    """
    if limit <= 0:
        return 0
    root = _control_root()
    controls_root = root / "controls"
    metadata_root = root / "metadata"
    cursor_path = root / _CONTROL_RECONCILE_CURSOR_FILENAME
    cursor_lock_path = root / _CONTROL_RECONCILE_CURSOR_LOCK_FILENAME
    retired = 0
    cursor_lock = FileLock(cursor_lock_path, blocking=False)
    try:
        if root.is_symlink() or cursor_path.is_symlink() or cursor_lock_path.is_symlink():
            return 0
        if not cursor_lock.acquire():
            return 0
        entries = _control_reconcile_entries(controls_root, metadata_root)
        batch = _control_reconcile_batch(
            entries,
            after=_read_control_reconcile_cursor(cursor_path),
            limit=limit,
        )
        for _key, kind, path in batch:
            supervision_id = path.name if kind == "control" else path.stem
            try:
                if kind == "control":
                    control_path = path / "control.json"
                    if control_path.is_symlink() or not control_path.is_file():
                        if not (metadata_root / f"{supervision_id}.json").exists():
                            retired += int(_reconcile_lock_only_directory(path, supervision_id))
                        continue
                    if _control_path(f"controls/{supervision_id}/control.json") != control_path.resolve():
                        continue
                    retired += int(_reconcile_control_record(control_path, supervision_id))
                else:
                    if durable_metadata_path(supervision_id).resolve() != path.resolve():
                        continue
                    retired += int(_reconcile_orphan_metadata(path, supervision_id))
            except (OSError, ValueError):
                continue
        if batch:
            _write_control_reconcile_cursor(cursor_path, batch[-1][0])
    except OSError:
        return retired
    finally:
        cursor_lock.release()
    return retired


def _ensure_control_state_reconciled() -> None:
    """Run one bounded reconciliation batch before each Windows launch."""
    try:
        with _CONTROL_STATE_RECONCILE_LOCK:
            reconcile_stale_control_state()
    except BaseException:
        # Housekeeping must never make a process launch unavailable.
        pass


def _retire_current_process_control(token: SupervisionToken, control_path: Path) -> bool:
    """Remove an exact current-process claim during normal interpreter exit."""
    try:
        if token.identity.pid != os.getpid() or not identity_matches(token.identity):
            return False
        # Avoid creating a new lock file when a newer claim's atexit callback
        # already retired this shared supervision ID.
        if not control_path.is_file():
            return False
        lock_path = control_path.with_suffix(".lock")
        lock = FileLock(lock_path, blocking=False)
        if not lock.acquire():
            return False
        removed = False
        try:
            try:
                state = json.loads(control_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, json.JSONDecodeError):
                return False
            identities = _control_record_identities(state, token.token)
            if identities != (token.control_nonce, token.identity, token.payload):
                return False
            control_path.unlink()
            removed = True
        finally:
            lock.release()
        if removed:
            metadata_path = control_path.parents[2] / "metadata" / f"{token.token}.json"
            _remove_empty_control_artifacts(
                control_path,
                metadata_path,
            )
        return removed
    except BaseException:
        return False


def _establish_current_posix_process_group() -> int:
    """Make the caller a group leader while preserving interactive TTY use."""
    pid = os.getpid()
    current_pgid = os.getpgrp()
    if current_pgid == pid:
        return current_pgid

    os.setpgrp()
    current_pgid = os.getpgrp()
    # setpgrp() makes this a background group. Ignore SIGTTOU while reclaiming
    # the terminal foreground so interactive agent children can still use it.
    old_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(sys.stdin.fileno(), current_pgid)
    except (OSError, AttributeError):
        pass
    finally:
        signal.signal(signal.SIGTTOU, old_sigttou)
    return current_pgid


def claim_current_process(supervision_id: str) -> SupervisionToken:
    """Claim a portable ownership boundary around the current orchestrator."""
    if not supervision_id:
        raise ValueError("supervision_id is required")
    if os.name == "posix":
        pgid = _establish_current_posix_process_group()
        identity = inspect_process(os.getpid())
        if identity is None:
            raise RuntimeError("Could not inspect current process")
        return SupervisionToken(
            LifetimeMode.RUN_OWNED,
            identity,
            identity.pid,
            identity.started_at,
            supervision_id,
            pgid=pgid,
            payload_identity=identity,
            version=1,
        )
    if os.name != "nt":
        raise RuntimeError(f"current-process ownership is unsupported on {os.name}")
    _ensure_control_state_reconciled()
    identity = inspect_process(os.getpid())
    if identity is None:
        raise RuntimeError("Could not inspect current process")
    existing = _CURRENT_WINDOWS_JOBS.get(supervision_id)
    if existing is None:
        job = _WindowsJob(_windows_job_name(supervision_id))
        try:
            job.assign(int(_kernel32().GetCurrentProcess()))
        except Exception:
            job.close()
            raise
        _CURRENT_WINDOWS_JOBS[supervision_id] = job
    nonce = uuid.uuid4().hex
    relpath = f"controls/{supervision_id}/control.json"
    token = SupervisionToken(
        LifetimeMode.RUN_OWNED,
        identity,
        identity.pid,
        identity.started_at,
        supervision_id,
        payload_identity=identity,
        job_name=_windows_job_name(supervision_id),
        control_relpath=relpath,
        control_nonce=nonce,
    )
    control_path = _control_path(relpath)
    state = {
        "schema": 2,
        "supervision_id": supervision_id,
        "job_name": token.job_name,
        "nonce": nonce,
        "keeper_identity": identity.to_dict(),
        "payload_identity": identity.to_dict(),
        "request": None,
        "ack": None,
    }
    with FileLock(control_path.with_suffix(".lock")):
        atomic_write_text(control_path, json.dumps(state, sort_keys=True))

    def monitor() -> None:
        while identity_matches(identity):
            try:
                current = json.loads(control_path.read_text(encoding="utf-8"))
                request = current.get("request")
                if (
                    isinstance(request, dict)
                    and request.get("operation") == "stop"
                    and request.get("nonce") == nonce
                    and isinstance(request.get("id"), str)
                    and request.get("id")
                ):
                    # Acknowledge before asking Python's main thread to enter
                    # its normal SIGTERM cleanup path. The external caller owns
                    # the requested grace deadline and reopens the Job only as
                    # a bounded hard fallback if cleanup does not finish.
                    request_id = str(request["id"])
                    with FileLock(control_path.with_suffix(".lock")):
                        latest = json.loads(control_path.read_text(encoding="utf-8"))
                        if latest.get("request") == request:
                            latest["ack"] = request_id
                            latest["state"] = "stopping"
                            atomic_write_text(control_path, json.dumps(latest, sort_keys=True))
                    signal.raise_signal(signal.SIGTERM)
                    return
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            time.sleep(0.05)

    threading.Thread(target=monitor, name=f"spec-stop-{supervision_id}", daemon=True).start()
    # Install retirement after the monitor starts so it is the final lifecycle
    # hook registered by this claim. This ordering matters for short-lived
    # native Windows interpreters and is covered by a subprocess test.
    atexit.register(_retire_current_process_control, token, control_path)
    return token


class ManagedProcess:
    def __init__(self, process: subprocess.Popen[Any], token: SupervisionToken, job: _WindowsJob | None = None):
        self.process = process
        self.token = token
        self._job = job
        self._close_lock = threading.Lock()

    def terminate(self, grace_seconds: float = 5.0) -> None:
        if os.name == "nt" and self._job is not None:
            _terminate_held_windows_job(self.token, self._job, grace_seconds)
            return
        if os.name == "posix" and isinstance(self.process, _REAL_POPEN_TYPE):
            # The retained Popen plus start_new_session launch is the live
            # ownership capability.  Unlike persisted tokens, it does not
            # need to rediscover the leader through ps before signalling the
            # group.  That distinction matters on Darwin, where ps may report
            # a framework executable alias and getpgid rejects an exited
            # (unreaped) leader even while its descendants are still live.
            _terminate_verified_posix_group(
                self.token.pgid or self.process.pid,
                grace_seconds=grace_seconds,
            )
            return
        terminate(self.token, grace_seconds=grace_seconds)

    def kill(self) -> None:
        """Kill the complete owned tree, never just the Popen leader."""
        if self._job is not None:
            self._job.terminate()
        elif os.name == "posix" and isinstance(self.process, _REAL_POPEN_TYPE):
            _terminate_verified_posix_group(
                self.token.pgid or self.process.pid,
                grace_seconds=0,
            )
        else:
            terminate(self.token, grace_seconds=0)

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        """Mirror Popen.communicate while releasing ownership on completion.

        A timeout deliberately retains the handle so the caller can kill the
        tree and call communicate again, matching subprocess.run semantics.
        """
        # On Windows a descendant can inherit stdout/stderr and outlive the
        # Popen leader. Popen.communicate then waits for EOF forever even
        # though the leader has exited. Close the run-owned Job as soon as the
        # leader exits so inherited pipe handles cannot outlive ownership.
        if os.name == "nt" and isinstance(self.process, _REAL_POPEN_TYPE) and isinstance(self._job, _WindowsJob):
            threading.Thread(target=self._close_job_after_leader, daemon=True).start()
        try:
            result = self.process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        except BaseException:
            try:
                self.kill()
            except BaseException:
                pass
            try:
                self.close()
            except BaseException:
                pass
            raise
        self.close()
        return result

    def _close_job_after_leader(self) -> None:
        self.process.wait()
        self.close()

    def wait(self, timeout: float | None = None) -> int:
        tree = self._owned_tree_identities()
        returncode = int(self.process.wait(timeout=timeout))
        self.close()
        _wait_for_identities_exit(tree)
        return returncode

    def owned_tree_active(self) -> bool:
        """Whether the retained ownership boundary still has live members."""
        if os.name == "nt" and self._job is not None:
            query = getattr(self._job, "active_process_ids", None)
            if callable(query):
                return bool(query())
        return self.process.poll() is None

    def _owned_tree_identities(self) -> list[ProcessIdentity]:
        if os.name == "nt" and self._job is not None:
            try:
                query = getattr(self._job, "active_identities", None)
                if callable(query):
                    return query()
            except (OSError, AttributeError):
                pass
        return _windows_tree_identities(self.token.identity.pid)

    def close(self) -> None:
        with self._close_lock:
            if self._job is None:
                return
            tree = self._owned_tree_identities()
            self._job.close()
            _LIVE_WINDOWS_JOBS.pop((self.token.identity.pid, self.token.identity.started_at), None)
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
        try:
            managed.kill()
            stdout, stderr = managed.communicate(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            # A failed Job termination must not turn a bounded command into an
            # unbounded pipe drain. Closing the retained handle is the final
            # kill-on-close attempt; preserve the original timeout result.
            managed.close()
            stdout, stderr = exc.output, exc.stderr
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
        self._close_lock = threading.Lock()

    def terminate(self) -> None:
        # Match asyncio.subprocess.Process.terminate: initiate graceful
        # cancellation and return immediately so the event loop can enforce
        # its own timeout and escalation policy.
        if self.token.identity.started_at == "test-double":
            terminator = getattr(self.process, "terminate", None)
            if callable(terminator):
                terminator()
            return
        if os.name == "nt" and self._job is not None:
            try:
                _send_windows_break(self.token.pgid)
            except OSError:
                pass
            return
        if os.name == "posix" and isinstance(self.process, asyncio.subprocess.Process):
            try:
                os.killpg(self.token.pgid or self.token.identity.pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
            return
        terminate(self.token, grace_seconds=0)

    def kill(self) -> None:
        if self.token.identity.started_at == "test-double":
            killer = getattr(self.process, "kill", None)
            if callable(killer):
                killer()
            return
        if self._job is not None:
            self._job.terminate()
        elif os.name == "posix" and isinstance(self.process, asyncio.subprocess.Process):
            try:
                os.killpg(self.token.pgid or self.token.identity.pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass
        else:
            terminate(self.token, grace_seconds=0)

    async def wait(self) -> int:
        tree = self._owned_tree_identities()
        returncode = int(await self.process.wait())
        self.close()
        if tree:
            await asyncio.to_thread(_wait_for_identities_exit, tree)
        return returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes | None, bytes | None]:
        if os.name == "nt" and isinstance(self.process, asyncio.subprocess.Process) and isinstance(self._job, _WindowsJob):
            asyncio.create_task(self._close_job_after_leader())
        try:
            result = await self.process.communicate(input)
        except BaseException:
            try:
                self.kill()
            except BaseException:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except BaseException:
                pass
            try:
                self.close()
            except BaseException:
                pass
            raise
        self.close()
        return result

    async def _close_job_after_leader(self) -> None:
        await self.process.wait()
        try:
            self.close()
        except OSError:
            pass

    def _owned_tree_identities(self) -> list[ProcessIdentity]:
        if os.name == "nt" and self._job is not None:
            try:
                query = getattr(self._job, "active_identities", None)
                if callable(query):
                    return query()
            except (OSError, AttributeError):
                pass
        return _windows_tree_identities(self.token.identity.pid)

    def close(self) -> None:
        with self._close_lock:
            if self._job is None:
                return
            _LIVE_WINDOWS_JOBS.pop((self.token.identity.pid, self.token.identity.started_at), None)
            self._job.close()
            self._job = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


class ProcessSupervisor:
    """Launch children with an explicit ownership lifetime."""

    def __init__(
        self,
        mode: LifetimeMode = LifetimeMode.RUN_OWNED,
        *,
        supervision_id: str | None = None,
        publish_durable_token: bool = False,
    ):
        self.mode = LifetimeMode(mode)
        if publish_durable_token and self.mode is not LifetimeMode.DETACHED:
            raise ValueError("durable token publication requires detached lifetime")
        self._supervision_id = supervision_id
        self._publish_durable_token = publish_durable_token
        self._children: list[ManagedProcess | ManagedAsyncProcess] = []

    def spawn(self, argv: Sequence[str], **kwargs: Any) -> ManagedProcess:
        job = None
        metadata_path: Path | None = None
        publisher_identity: ProcessIdentity | None = None
        supervision_id = self._supervision_id or uuid.uuid4().hex
        control_relpath = f"controls/{supervision_id}/control.json"
        control_nonce = uuid.uuid4().hex
        if os.name == "nt":
            _ensure_control_state_reconciled()
            flags = int(kwargs.pop("creationflags", 0))
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                job = _WindowsJob(_windows_job_name(supervision_id))
            elif self.mode in {LifetimeMode.ADOPTABLE, LifetimeMode.DETACHED}:
                # A detached helper is the durable Job-handle owner.  The
                # dispatcher may exit or restart without closing the payload
                # Job; a replacement validates and adopts the helper token.
                metadata_path = durable_metadata_path(supervision_id)
                publisher_identity = inspect_process(os.getpid())
                if publisher_identity is None:
                    raise RuntimeError("Could not inspect durable token publisher identity")
                helper_argv = [
                    sys.executable,
                    "-m",
                    "spec_runtime.process_supervisor",
                    "--durable-helper",
                    str(metadata_path),
                    supervision_id,
                    self.mode.value,
                    control_relpath,
                    control_nonce,
                    json.dumps(publisher_identity.to_dict(), separators=(",", ":")),
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
        try:
            process = subprocess.Popen(list(argv), **kwargs)
        except BaseException:
            _abort_windows_job(job)
            raise
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
            owner = (
                ProcessIdentity(os.getpid(), "test-double")
                if is_test_double
                else publisher_identity or inspect_process(os.getpid())
            )
            owner = owner or ProcessIdentity(os.getpid(), "unavailable")
            pgid = (
                0
                if is_test_double
                # start_new_session=True makes the child the group/session
                # leader.  Record that launch invariant directly instead of
                # racing getpgid() against a short-lived command on Darwin.
                else process.pid
                if os.name == "posix"
                else process.pid
                if job is not None
                else 0
            )
            payload_identity = identity
            helper_token: SupervisionToken | None = None
            if metadata_path is not None and not is_test_double:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    helper_token = _load_durable_publication(
                        metadata_path,
                        supervision_id=supervision_id,
                        control_nonce=control_nonce,
                        mode=self.mode,
                    )
                    if helper_token is not None:
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Durable process supervisor exited before publishing payload identity")
                    time.sleep(0.02)
                if helper_token is None:
                    raise RuntimeError("Durable process supervisor did not publish payload identity")
                _acknowledge_durable_publication(metadata_path, helper_token)
                payload_identity = helper_token.payload
            job_name = _windows_job_name(supervision_id) if job is not None else ""
            if helper_token is not None:
                job_name = helper_token.job_name
                supervision_id = helper_token.token
                # Windows app-execution aliases and venv launchers may be
                # transient shims. The durable helper publishes the real
                # keeper identity after the shim has resolved; that identity,
                # not Popen.pid, is the reopenable ownership boundary.
                identity = helper_token.identity
            token = SupervisionToken(
                self.mode,
                identity,
                owner.pid,
                owner.started_at,
                supervision_id,
                pgid,
                payload_identity,
                job_name=job_name,
                control_relpath=helper_token.control_relpath if helper_token is not None else control_relpath,
                control_nonce=helper_token.control_nonce if helper_token is not None else control_nonce,
            )
            if is_test_double:
                setattr(process, "token", token)
                return process  # type: ignore[return-value]
            if self._publish_durable_token and os.name != "nt":
                # Windows DETACHED launches publish from their durable Job
                # helper before spawn() returns. POSIX has no keeper process,
                # so publish the exact session-leader token atomically here.
                # Callers opt in when crash recovery needs a discoverable token
                # before they publish their own domain-specific state.
                try:
                    atomic_write_text(
                        durable_metadata_path(token.token),
                        json.dumps(token.to_dict(), sort_keys=True) + "\n",
                    )
                except BaseException:
                    # Publication is the recovery handoff. If it fails, do not
                    # leave an undiscoverable detached process tree behind.
                    terminate(token, grace_seconds=0)
                    raise
            managed = ManagedProcess(process, token, job)
            if job is not None:
                _LIVE_WINDOWS_JOBS[(identity.pid, identity.started_at)] = job
            self._children.append(managed)
            return managed
        except BaseException:
            _abort_windows_job(job)
            try:
                killer = getattr(process, "kill", None)
                if callable(killer):
                    killer()
            except BaseException:
                pass
            raise

    async def spawn_async(self, argv: Sequence[str], **kwargs: Any) -> ManagedAsyncProcess:
        """Async counterpart with the same platform-owned launch policy."""
        job = None
        metadata_path: Path | None = None
        publisher_identity: ProcessIdentity | None = None
        supervision_id = self._supervision_id or uuid.uuid4().hex
        control_relpath = f"controls/{supervision_id}/control.json"
        control_nonce = uuid.uuid4().hex
        if os.name == "nt":
            _ensure_control_state_reconciled()
            flags = int(kwargs.pop("creationflags", 0))
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            if self.mode is LifetimeMode.RUN_OWNED:
                flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                job = _WindowsJob(_windows_job_name(supervision_id))
            elif self.mode in {LifetimeMode.ADOPTABLE, LifetimeMode.DETACHED}:
                metadata_path = durable_metadata_path(supervision_id)
                publisher_identity = inspect_process(os.getpid())
                if publisher_identity is None:
                    raise RuntimeError("Could not inspect durable token publisher identity")
                argv = [
                    sys.executable,
                    "-m",
                    "spec_runtime.process_supervisor",
                    "--durable-helper",
                    str(metadata_path),
                    supervision_id,
                    self.mode.value,
                    control_relpath,
                    control_nonce,
                    json.dumps(publisher_identity.to_dict(), separators=(",", ":")),
                    "--",
                    *argv,
                ]
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except BaseException:
            _abort_windows_job(job)
            raise
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
            identity = (
                ProcessIdentity(int(getattr(process, "pid", os.getpid())), "test-double")
                if is_test_double
                else inspect_process(process.pid)
            )
            if identity is None:
                raise RuntimeError(f"Could not inspect launched process pid={process.pid}")
            owner = (
                ProcessIdentity(os.getpid(), "test-double")
                if is_test_double
                else publisher_identity or inspect_process(os.getpid())
            )
            owner = owner or ProcessIdentity(os.getpid(), "unavailable")
            pgid = (
                0
                if is_test_double
                else process.pid
                if os.name == "posix"
                else process.pid
                if job is not None
                else 0
            )
            payload_identity = identity
            helper_token: SupervisionToken | None = None
            if metadata_path is not None and not is_test_double:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    helper_token = _load_durable_publication(
                        metadata_path,
                        supervision_id=supervision_id,
                        control_nonce=control_nonce,
                        mode=self.mode,
                    )
                    if helper_token is not None:
                        break
                    if process.returncode is not None:
                        raise RuntimeError("Durable process supervisor exited before publishing payload identity")
                    await asyncio.sleep(0.02)
                if helper_token is None:
                    raise RuntimeError("Durable process supervisor did not publish payload identity")
                _acknowledge_durable_publication(metadata_path, helper_token)
                payload_identity = helper_token.payload
            job_name = _windows_job_name(supervision_id) if job is not None else ""
            if helper_token is not None:
                job_name = helper_token.job_name
                supervision_id = helper_token.token
                identity = helper_token.identity
            token = SupervisionToken(
                self.mode,
                identity,
                owner.pid,
                owner.started_at,
                supervision_id,
                pgid,
                payload_identity,
                job_name=job_name,
                control_relpath=helper_token.control_relpath if helper_token is not None else control_relpath,
                control_nonce=helper_token.control_nonce if helper_token is not None else control_nonce,
            )
            if self._publish_durable_token and not is_test_double and os.name != "nt":
                try:
                    atomic_write_text(
                        durable_metadata_path(token.token),
                        json.dumps(token.to_dict(), sort_keys=True) + "\n",
                    )
                except BaseException:
                    terminate(token, grace_seconds=0)
                    raise
            managed = ManagedAsyncProcess(process, token, job)
            if job is not None:
                _LIVE_WINDOWS_JOBS[(identity.pid, identity.started_at)] = job
            self._children.append(managed)
            return managed
        except BaseException:
            _abort_windows_job(job)
            try:
                killer = getattr(process, "kill", None)
                if callable(killer):
                    killer()
                waiter = getattr(process, "wait", None)
                if callable(waiter):
                    await asyncio.wait_for(waiter(), timeout=5.0)
            except BaseException:
                pass
            raise

    def close(self) -> None:
        if self.mode is LifetimeMode.RUN_OWNED:
            for child in self._children:
                child.close()

    def __enter__(self) -> ProcessSupervisor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not is_process_group_alive(pgid):
            return True
        time.sleep(0.05)
    return not is_process_group_alive(pgid)


def _terminate_verified_posix_group(
    pgid: int,
    *,
    grace_seconds: float,
    hard_grace_seconds: float = 1.0,
) -> bool:
    """Terminate a group whose leader identity was verified by the caller."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return not is_process_group_alive(pgid)
    except (OSError, ValueError):
        return False
    if _wait_for_process_group_exit(pgid, grace_seconds):
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return not is_process_group_alive(pgid)
    except (OSError, ValueError):
        return False
    return _wait_for_process_group_exit(pgid, hard_grace_seconds)


def terminate(token: SupervisionToken, *, grace_seconds: float = 5.0, job: _WindowsJob | None = None) -> bool:
    """Revalidate identity, request cancellation, then kill the owned tree."""
    if os.name == "nt":
        held_job = job or _LIVE_WINDOWS_JOBS.get((token.identity.pid, token.identity.started_at))
        if held_job is not None:
            return _terminate_held_windows_job(token, held_job, grace_seconds)
        # Persisted tokens are not capabilities by themselves. Reopen only the
        # canonical Job, authenticate the locked helper record, and require its
        # current exact payload identity to be a live Job member before either
        # a graceful request or hard termination is permitted.
        if not identity_matches(token.identity):
            return False
        reopened_job = _WindowsJob.open(token.job_name or _windows_job_name(token.token))
        if reopened_job is None:
            return False
        request_id = uuid.uuid4().hex
        control_path = _control_path(token.control_relpath)
        authenticated_payload: ProcessIdentity | None = None
        try:
            with FileLock(control_path.with_suffix(".lock")):
                try:
                    state = json.loads(control_path.read_text(encoding="utf-8"))
                    payload_value = state.get("payload_identity")
                    if not isinstance(payload_value, dict):
                        raise ValueError("missing payload identity")
                    current_payload = ProcessIdentity.from_dict(payload_value)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    return False
                if (
                    state.get("schema") != 2
                    or state.get("supervision_id") != token.token
                    or state.get("job_name") != token.job_name
                    or state.get("nonce") != token.control_nonce
                    or state.get("keeper_identity") != token.identity.to_dict()
                    or not reopened_job.contains(current_payload)
                ):
                    return False
                authenticated_payload = current_payload
                state["request"] = {
                    "id": request_id,
                    "operation": "stop",
                    "nonce": token.control_nonce,
                    "grace_seconds": max(0.0, grace_seconds),
                }
                atomic_write_text(control_path, json.dumps(state, sort_keys=True))

            deadline = time.monotonic() + max(0.0, grace_seconds)
            while time.monotonic() < deadline:
                if not reopened_job.active_process_ids():
                    return True
                try:
                    state = json.loads(control_path.read_text(encoding="utf-8"))
                    if state.get("ack") == request_id and not reopened_job.active_process_ids():
                        return True
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.05, remaining))
            if not identity_matches(token.identity):
                # The open Job handle preserves the owned tree if the keeper
                # vanished, but a changed keeper identity invalidates the
                # persisted authorization rather than broadening it.
                return False
            if authenticated_payload is None or not reopened_job.contains(authenticated_payload):
                return False
            identities = reopened_job.active_identities()
            reopened_job.terminate()
            return _wait_for_identities_exit(identities) if identities else True
        except (OSError, ValueError):
            return False
        finally:
            try:
                reopened_job.close()
            except OSError:
                pass

    pgid = token.pgid or token.identity.pid
    if not identity_matches(token.identity):
        return False
    try:
        if os.getpgid(token.identity.pid) != pgid:
            return False
    except (OSError, ProcessLookupError):
        return False
    # Once the exact leader and group are proven, group liveness—not leader
    # liveness—controls escalation. The leader may exit before descendants.
    return _terminate_verified_posix_group(pgid, grace_seconds=grace_seconds)


def terminate_legacy_process_group(
    pgid: int,
    leader_pid: int,
    leader_started_at: str,
    *,
    grace_seconds: float = 5.0,
    hard_grace_seconds: float = 1.0,
) -> bool:
    """Stop an old pgid-only run record after exact leader revalidation.

    Returns whether a live owned group was stopped. A live group whose leader
    exited, changed creation identity, or no longer belongs to the group is
    refused because the pgid may now be orphaned or reused.
    """
    if os.name != "posix" or pgid <= 0 or leader_pid <= 0 or not leader_started_at:
        raise ProcessGroupOwnershipError("legacy process-group ownership is unavailable on this platform")

    group_was_alive = is_process_group_alive(pgid)
    expected = ProcessIdentity(leader_pid, leader_started_at)
    leader_matches = identity_matches(expected)
    leader_in_group = False
    if leader_matches:
        try:
            leader_in_group = os.getpgid(leader_pid) == pgid
        except (OSError, ProcessLookupError):
            leader_matches = False

    if not group_was_alive:
        return False
    if not leader_matches or not leader_in_group:
        raise ProcessGroupOwnershipError(
            f"Recorded leader {leader_pid} has exited or changed identity, but process group {pgid} "
            "still has live members; refusing to signal an orphaned or reused process group."
        )
    if not _terminate_verified_posix_group(
        pgid,
        grace_seconds=grace_seconds,
        hard_grace_seconds=hard_grace_seconds,
    ):
        raise ProcessGroupTerminationError(f"Failed to stop live process group {pgid}.")
    return True


def request_legacy_process_shutdown(identity: ProcessIdentity) -> bool:
    """Send SIGTERM to one exactly revalidated legacy POSIX process.

    Modern long-lived processes use a persisted supervision token or their
    control-plane shutdown tracker.  This compatibility seam exists for old
    pid files and discovery records that predate either capability.  Keeping
    the signal here prevents workflow code from reimplementing platform and
    PID-reuse policy.
    """
    if os.name != "posix":
        raise ProcessGroupOwnershipError(
            "legacy single-process shutdown is unavailable on this platform"
        )
    if identity.pid <= 0 or not identity.started_at or not identity_matches(identity):
        return False
    try:
        os.kill(identity.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except (OSError, ValueError) as exc:
        raise ProcessGroupTerminationError(
            f"Failed to request shutdown from legacy process {identity.pid}: {exc}"
        ) from exc
    return True


def stop_supervised_process(
    token: SupervisionToken,
    *,
    grace_seconds: float = 5.0,
    hard_grace_seconds: float = 1.0,
) -> bool:
    """Stop a persisted portable token, returning whether it was live."""
    if os.name == "nt":
        if not identity_matches(token.identity):
            return False
        if not terminate(token, grace_seconds=grace_seconds):
            raise ProcessGroupTerminationError(
                f"Failed to stop live supervised process {token.identity.pid}."
            )
        return True
    return terminate_legacy_process_group(
        token.pgid or token.identity.pid,
        token.identity.pid,
        token.identity.started_at,
        grace_seconds=grace_seconds,
        hard_grace_seconds=hard_grace_seconds,
    )


def terminate_legacy_popen_tree(process: subprocess.Popen[Any], *, grace_seconds: float = 5.0) -> bool:
    """Safely terminate a raw legacy ``Popen`` tree when ownership is provable.

    Custom execution backends historically received ``start_new_session=True``
    from workflow code and could hand their raw ``Popen`` back to the monitor.
    The supervisor now owns launch policy, but this bounded compatibility seam
    preserves those POSIX process-group semantics for a genuine group leader.
    Windows raw PIDs are not Job capabilities, and a POSIX process in somebody
    else's group is not an owned tree, so both cases fail closed.

    ``TypeError`` distinguishes lightweight test doubles from genuine Popen
    instances; callers may retain a narrow Popen-compatible mock fallback.
    """
    if not isinstance(process, _REAL_POPEN_TYPE):
        raise TypeError("legacy process is not a real subprocess.Popen")
    if process.poll() is not None:
        return True
    if os.name != "posix":
        return False

    identity = inspect_process(process.pid)
    if identity is None:
        return process.poll() is not None
    try:
        pgid = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        return process.poll() is not None
    if pgid != process.pid:
        return False

    owner = inspect_process(os.getpid())
    token = SupervisionToken(
        mode=LifetimeMode.RUN_OWNED,
        identity=identity,
        owner_pid=os.getpid(),
        owner_started_at=owner.started_at if owner is not None else "unavailable",
        token=uuid.uuid4().hex,
        pgid=pgid,
        version=1,
    )
    return terminate(token, grace_seconds=grace_seconds)


def legacy_pid_record_is_live(pid: int, started_at: str = "") -> bool:
    """Validate a tokenless pre-supervisor PID record without exposing signals.

    Windows PID records never carry an ownership capability and fail closed.
    On POSIX, a recorded start identity must match exactly. Older records that
    predate start-identity persistence remain readable for status, but are not
    sufficient authorization for later termination.
    """
    if os.name != "posix" or pid <= 0:
        return False
    live = inspect_process(pid)
    if live is None:
        return False
    return not started_at or live.started_at == started_at


def terminate_legacy_pid_record(
    pid: int,
    started_at: str = "",
    *,
    grace_seconds: float = 5.0,
) -> bool:
    """Stop an old tokenless POSIX session only while ownership still matches.

    The legacy web daemon used ``setsid()``, so its PID must still be its
    process-group ID. A persisted start identity is required. Routing the
    reconstructed token through :func:`terminate` closes the inspect/signal
    race with a second identity check and preserves bounded whole-group cleanup.
    """
    if os.name != "posix" or pid <= 0 or not started_at:
        return False
    identity = inspect_process(pid)
    if identity is None or identity.started_at != started_at:
        return False
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return False
    if pgid != pid:
        return False
    owner = inspect_process(os.getpid())
    token = SupervisionToken(
        LifetimeMode.DETACHED,
        identity,
        os.getpid(),
        owner.started_at if owner is not None else "unavailable",
        uuid.uuid4().hex,
        pgid=pgid,
        version=1,
    )
    return terminate(token, grace_seconds=grace_seconds)


def _same_durable_boundary(left: SupervisionToken, right: SupervisionToken) -> bool:
    return (
        left.version == right.version == 2
        and left.mode == right.mode
        and left.token == right.token
        and left.identity == right.identity
        and left.job_name == right.job_name
        and left.control_relpath == right.control_relpath
        and left.control_nonce == right.control_nonce
    )


def promote_payload_identity(token: SupervisionToken, candidate: ProcessIdentity) -> SupervisionToken:
    """Promote a durable payload only after proving canonical Job membership.

    Windows venv and app-execution launchers may exit after creating the real
    interpreter.  The ready child may identify that interpreter, but its PID is
    untrusted until the kernel confirms membership in the token's named Job and
    the locked helper state confirms the same immutable ownership boundary.
    """
    if token.version != 2 or token.mode not in {LifetimeMode.ADOPTABLE, LifetimeMode.DETACHED}:
        raise ValueError("payload promotion requires a durable V2 supervision token")
    expected_job_name = _windows_job_name(token.token)
    if token.job_name != expected_job_name:
        raise ValueError("payload promotion requires the canonical named Job")
    if not identity_matches(token.identity):
        raise ProcessLookupError(token.identity.pid)
    if not identity_matches(candidate):
        raise ProcessLookupError(candidate.pid)

    job = _WindowsJob.open(expected_job_name)
    if job is None:
        raise ValueError("payload promotion requires a live canonical named Job")
    try:
        if not job.contains(candidate):
            raise ValueError("candidate payload is not an active member of the supervised Job")
        control_path = _control_path(token.control_relpath)
        metadata_path = durable_metadata_path(token.token)
        with FileLock(control_path.with_suffix(".lock")):
            # Revalidate after acquiring the serialization lock. Keeping the
            # Job handle open also prevents kill-on-close from disappearing in
            # the middle of the two durable atomic replacements.
            if not identity_matches(token.identity) or not job.contains(candidate):
                raise ValueError("supervision identities changed during payload promotion")
            try:
                control_text = control_path.read_text(encoding="utf-8")
                state = json.loads(control_text)
                metadata_text = metadata_path.read_text(encoding="utf-8")
                metadata_token = SupervisionToken.from_dict(json.loads(metadata_text))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("payload promotion requires valid durable helper state") from exc

            expected_control = {
                "schema": 2,
                "supervision_id": token.token,
                "job_name": token.job_name,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
            }
            if any(state.get(key) != value for key, value in expected_control.items()):
                raise ValueError("payload promotion control record does not match")
            if state.get("request"):
                raise ValueError("payload promotion refused while stop is pending")
            if not _same_durable_boundary(token, metadata_token):
                raise ValueError("payload promotion metadata does not match")

            # Either file may already contain the candidate if a prior process
            # died between the two atomic replacements. Accept only the exact
            # old-or-new identities, then converge both files while locked.
            old_payload = token.payload.to_dict()
            new_payload = candidate.to_dict()
            if state.get("payload_identity") not in (old_payload, new_payload):
                raise ValueError("payload promotion control identity does not match")
            if metadata_token.payload.to_dict() not in (old_payload, new_payload):
                raise ValueError("payload promotion metadata identity does not match")

            promoted = SupervisionToken(
                token.mode,
                token.identity,
                token.owner_pid,
                token.owner_started_at,
                token.token,
                token.pgid,
                candidate,
                version=token.version,
                job_name=token.job_name,
                control_relpath=token.control_relpath,
                control_nonce=token.control_nonce,
            )
            promoted_metadata = SupervisionToken(
                metadata_token.mode,
                metadata_token.identity,
                metadata_token.owner_pid,
                metadata_token.owner_started_at,
                metadata_token.token,
                metadata_token.pgid,
                candidate,
                version=metadata_token.version,
                job_name=metadata_token.job_name,
                control_relpath=metadata_token.control_relpath,
                control_nonce=metadata_token.control_nonce,
            )
            promoted_state = dict(state)
            promoted_state["payload_identity"] = new_payload
            try:
                atomic_write_text(control_path, json.dumps(promoted_state, sort_keys=True))
                atomic_write_text(metadata_path, json.dumps(promoted_metadata.to_dict(), sort_keys=True))
            except Exception:
                # Best-effort rollback for ordinary I/O failures. The
                # old-or-new convergence rule above handles process death.
                try:
                    atomic_write_text(metadata_path, metadata_text)
                    atomic_write_text(control_path, control_text)
                except OSError:
                    pass
                raise
            return promoted
    finally:
        job.close()


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
            "job_name": token.job_name,
            "nonce": token.control_nonce,
            "keeper_identity": token.identity.to_dict(),
            "payload_identity": token.payload.to_dict(),
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("adoptable token control record does not match")
        prior_owner = state.get("adopted_by")
        if isinstance(prior_owner, dict):
            try:
                prior_identity = ProcessIdentity.from_dict(prior_owner)
            except (TypeError, ValueError):
                raise ValueError("adoptable token has invalid owner identity") from None
            if identity_matches(prior_identity):
                raise ValueError("adoptable token was already adopted")
        generation = int(state.get("adoption_generation", 0) or 0) + 1
        state["adopted_by"] = owner.to_dict()
        state["adoption_generation"] = generation
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


def _stabilize_windows_payload_identity(child: ManagedProcess, timeout: float = 0.2) -> ProcessIdentity:
    """Resolve a fast-exiting launcher shim to the oldest remaining Job member."""
    initial = child.token.identity
    if child._job is None:
        return initial
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not identity_matches(initial):
            try:
                members = child._job.active_identities()
            except OSError:
                return initial
            if not members:
                return initial

            def creation_key(identity: ProcessIdentity) -> tuple[int, str, int]:
                try:
                    return (0, f"{int(identity.started_at):020d}", identity.pid)
                except ValueError:
                    return (1, identity.started_at, identity.pid)

            return min(members, key=creation_key)
        time.sleep(0.01)
    return initial


def _durable_helper(
    metadata_path: Path,
    supervision_id: str,
    mode: LifetimeMode,
    control_relpath: str,
    control_nonce: str,
    publisher_identity: ProcessIdentity,
    argv: Sequence[str],
) -> int:
    """Own one Windows Job for the full lifetime of a durable payload."""
    token: SupervisionToken | None = None
    try:
        with ProcessSupervisor(LifetimeMode.RUN_OWNED, supervision_id=supervision_id) as supervisor:
            child = supervisor.spawn(argv)
            payload = _stabilize_windows_payload_identity(child)
            keeper = inspect_process(os.getpid())
            if keeper is None:
                raise RuntimeError("Could not inspect durable supervisor identity")
            token = SupervisionToken(
                mode,
                keeper,
                0,
                "",
                supervision_id,
                payload_identity=payload,
                job_name=child.token.job_name,
                control_relpath=control_relpath,
                control_nonce=control_nonce,
            )
            control_path = _control_path(token.control_relpath)
            state = {
                "schema": 2,
                "supervision_id": token.token,
                "job_name": token.job_name,
                "nonce": token.control_nonce,
                "keeper_identity": token.identity.to_dict(),
                "payload_identity": token.payload.to_dict(),
                "adopted_by": None,
                "adoption_generation": 0,
            }
            with FileLock(control_path.with_suffix(".lock")):
                atomic_write_text(control_path, json.dumps(state, sort_keys=True))
            atomic_write_text(metadata_path, json.dumps(token.to_dict()))
            # A Windows launcher shim may exit while its real interpreter remains
            # an active Job member. The helper owns the Job, not just Popen.pid, so
            # it must stay alive until the kernel says the entire Job is empty.
            while child.owned_tree_active():
                request_id = ""
                with FileLock(control_path.with_suffix(".lock")):
                    try:
                        current = json.loads(control_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, TypeError):
                        current = {}
                    request = current.get("request")
                    if (
                        isinstance(request, dict)
                        and request.get("operation") == "stop"
                        and request.get("nonce") == control_nonce
                        and isinstance(request.get("id"), str)
                        and request.get("id")
                    ):
                        request_id = str(request["id"])
                if request_id:
                    child.terminate(grace_seconds=float(request.get("grace_seconds", 0.0) or 0.0))
                    child.wait()
                    with FileLock(control_path.with_suffix(".lock")):
                        current["ack"] = request_id
                        current["request"] = None
                        atomic_write_text(control_path, json.dumps(current, sort_keys=True))
                    return int(child.returncode)
                time.sleep(0.05)
            return int(child.wait())
    finally:
        if token is not None:
            # The helper never removes publication before the exact launching
            # process has retained it (or that publisher is provably gone).
            # Cleanup is best effort so it cannot replace the payload result or
            # a BaseException already unwinding through this helper.
            try:
                _await_durable_publication(metadata_path, token, publisher_identity)
            except BaseException:
                pass
            try:
                _retire_durable_records(metadata_path, token)
            except BaseException:
                pass


if __name__ == "__main__":  # pragma: no cover - exercised by native Windows tests
    marker = "--durable-helper"
    if marker in sys.argv:
        separator = sys.argv.index("--")
        marker_index = sys.argv.index(marker)
        raise SystemExit(
            _durable_helper(
                Path(sys.argv[marker_index + 1]),
                sys.argv[marker_index + 2],
                LifetimeMode(sys.argv[marker_index + 3]),
                sys.argv[marker_index + 4],
                sys.argv[marker_index + 5],
                ProcessIdentity.from_dict(json.loads(sys.argv[marker_index + 6])),
                sys.argv[separator + 1 :],
            )
        )
