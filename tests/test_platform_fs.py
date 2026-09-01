from __future__ import annotations

import errno
import json
import multiprocessing
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from spec_runtime.platform import is_unc_path
from spec_runtime.platform_fs import FileLock, _windows_extended_path, atomic_write_text, remove_tree
from spec_runtime.spec_identity import SPEC_ID_RE


def _locked_increment(path_text: str, count: int) -> None:
    from spec_runtime.orchestrator import _locked_state_path, _read_json_dict, _write_json_file_atomically

    path = Path(path_text)
    for _ in range(count):
        with _locked_state_path(path):
            payload = _read_json_dict(path) or {"value": 0}
            payload["value"] += 1
            _write_json_file_atomically(path, payload)


def test_file_lock_blocks_second_owner_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    first = FileLock(path, blocking=False)
    second = FileLock(path, blocking=False)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_file_lock_releases_after_exception(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    with pytest.raises(RuntimeError), FileLock(path):
        raise RuntimeError("boom")
    with FileLock(path, blocking=False):
        pass


def test_atomic_write_text_replaces_with_parseable_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_text(path, '{"version": 1}\n')
    atomic_write_text(path, '{"version": 2}\n')
    assert json.loads(path.read_text()) == {"version": 2}
    assert not list(tmp_path.glob(".state.json.tmp-*"))
    assert not list(tmp_path.glob(".spec-*.tmp"))


def test_atomic_write_text_uses_bounded_temp_basename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A long destination name must not be repeated in the sibling temp path."""
    from spec_runtime import platform_fs

    replaced_from: list[Path] = []
    real_replace = platform_fs.os.replace

    def capture_replace(source: Path, target: Path) -> None:
        replaced_from.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(platform_fs.os, "replace", capture_replace)
    path = tmp_path / f"{('long-run-state-' * 10)}.json"

    atomic_write_text(path, '{"ok": true}\n')

    assert json.loads(path.read_text()) == {"ok": True}
    assert len(replaced_from) == 1
    temporary = replaced_from[0]
    assert temporary.parent == path.parent
    assert path.name not in temporary.name
    assert len(temporary.name) <= 32


def test_locked_state_updates_survive_multiple_processes(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    process_count = 4
    increments = 20
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_locked_increment, args=(str(path), increments)) for _ in range(process_count)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert json.loads(path.read_text()) == {"value": process_count * increments}


def test_windows_atomic_replace_retries_sharing_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from spec_runtime import platform_fs

    real_replace = platform_fs.os.replace
    attempts = 0

    def sharing_violation_then_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        real_replace(source, target)

    monkeypatch.setattr(platform_fs, "_WINDOWS", True)
    monkeypatch.setattr(platform_fs.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(platform_fs.os, "replace", sharing_violation_then_replace)
    path = tmp_path / "state.json"
    atomic_write_text(path, '{"ok": true}\n')
    assert json.loads(path.read_text()) == {"ok": True}
    assert attempts == 3


def test_windows_blocking_lock_waits_until_contention_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from spec_runtime import platform_fs

    calls = 0

    def locking(_fd: int, mode: int, _length: int) -> None:
        nonlocal calls
        if mode == 2:  # LK_NBLCK
            calls += 1
            if calls < 8:
                raise PermissionError(errno.EACCES, "locked")

    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_fs, "_WINDOWS", True)
    monkeypatch.setattr(platform_fs.time, "sleep", lambda _delay: None)
    lock = FileLock(tmp_path / "state.lock")
    assert lock.acquire()
    assert calls == 8
    lock.release()


def test_windows_contender_reads_owner_after_locked_byte(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from spec_runtime import orchestrator, platform_fs

    path = tmp_path / "spec.lock"
    path.write_bytes(b'\0{"pid": 4321, "command": "spec implement"}\n')
    monkeypatch.setattr(platform_fs, "_WINDOWS", True)
    monkeypatch.setattr(orchestrator.FileLock, "acquire", lambda _self: False)
    owner = orchestrator.read_spec_lock_owner_from_path(path)
    assert owner is not None
    assert owner.pid == 4321
    assert owner.command == "spec implement"


def test_windows_lock_migrates_legacy_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from spec_runtime import platform_fs

    path = tmp_path / "legacy.lock"
    legacy = b'{"pid": 4321, "command": "spec implement"}\n'
    path.write_bytes(legacy)
    fake_msvcrt = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *_args: None)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_fs, "_WINDOWS", True)

    lock = FileLock(path, blocking=False)
    assert lock.acquire()
    try:
        assert path.read_bytes() == b"\0" + legacy
        assert json.loads(platform_fs.read_lock_metadata(path)) == {
            "pid": 4321,
            "command": "spec implement",
        }
    finally:
        lock.release()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows byte-range locks")
def test_windows_file_lock_contends_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "native.lock"
    result_path = tmp_path / "result.txt"
    contender = (
        "from pathlib import Path\n"
        "import sys\n"
        "from spec_runtime.platform_fs import FileLock\n"
        "lock = FileLock(Path(sys.argv[1]), blocking=False)\n"
        "Path(sys.argv[2]).write_text(str(lock.acquire()))\n"
    )
    with FileLock(path):
        completed = subprocess.run(
            [sys.executable, "-c", contender, str(path), str(result_path)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert result_path.read_text() == "False"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows byte-range locks")
def test_windows_file_lock_blocks_across_processes_until_release(tmp_path: Path) -> None:
    path = tmp_path / "native.lock"
    ready_path = tmp_path / "ready.txt"
    result_path = tmp_path / "result.txt"
    contender = (
        "from pathlib import Path\n"
        "import sys\n"
        "from spec_runtime.platform_fs import FileLock\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "with FileLock(Path(sys.argv[1])):\n"
        "    Path(sys.argv[3]).write_text('acquired')\n"
    )
    lock = FileLock(path)
    assert lock.acquire()
    process = subprocess.Popen(
        [sys.executable, "-c", contender, str(path), str(ready_path), str(result_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(1000):
            if ready_path.exists() or process.poll() is not None:
                break
            time.sleep(0.01)
        assert ready_path.exists()
        assert process.poll() is None
        assert not result_path.exists()
    finally:
        lock.release()
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
    assert result_path.read_text() == "acquired"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows byte-range locks")
def test_windows_file_lock_migrates_legacy_metadata_natively(tmp_path: Path) -> None:
    from spec_runtime.platform_fs import read_lock_metadata

    path = tmp_path / "legacy-native.lock"
    legacy = b'{"pid": 4321, "command": "spec implement"}\n'
    path.write_bytes(legacy)
    with FileLock(path):
        assert json.loads(read_lock_metadata(path))["pid"] == 4321
    assert path.read_bytes() == b"\0" + legacy


def test_remove_tree_repairs_read_only_file(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    item = tree / "item"
    item.write_text("data")
    item.chmod(0o444)
    remove_tree(tree)
    assert not tree.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"C:\repo\deep", r"\\?\C:\repo\deep"),
        (r"\\server\share\repo", r"\\?\UNC\server\share\repo"),
        (r"\\?\C:\already", r"\\?\C:\already"),
    ],
)
def test_windows_extended_path_supports_drive_and_unc_roots(value: str, expected: str) -> None:
    assert str(_windows_extended_path(Path(value))) == expected


@pytest.mark.parametrize("value", [r"\\server\share\repo", "//server/share/repo"])
def test_unc_paths_are_recognized(value: str) -> None:
    assert is_unc_path(value)


def test_doctor_rejects_windows_network_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from spec_runtime import doctor

    monkeypatch.setattr(doctor, "is_windows", lambda: True)
    monkeypatch.setattr(doctor, "is_unc_path", lambda _path: True)
    report = doctor.run_doctor_checks(tmp_path)
    assert report.exit_code == 1
    assert report.checks[0].name == "local filesystem"
    assert "unsupported" in report.checks[0].detail


@pytest.mark.parametrize("value", ["con", "prn", "aux", "nul", "com1", "lpt9"])
def test_windows_device_names_are_not_spec_ids(value: str) -> None:
    assert SPEC_ID_RE.fullmatch(value) is None


def test_spec_ids_are_bounded_for_generated_windows_paths() -> None:
    assert SPEC_ID_RE.fullmatch("a" * 64)
    assert SPEC_ID_RE.fullmatch("a" * 65) is None


def test_workspace_identity_is_deterministic_under_unicode_root(tmp_path: Path) -> None:
    from spec_runtime.spec_identity import spec_run_worktree_name

    root = tmp_path / "repo with spaces 雪"
    root.mkdir()
    first = root / ".worktrees" / spec_run_worktree_name("sample", "20260901T010203")
    second = root / ".worktrees" / spec_run_worktree_name("sample", "20260901T010203")
    assert first == second
    assert len(first.name) <= 64


@pytest.mark.skipif(sys.platform != "win32", reason="installed-wheel smoke test requires native Windows")
def test_installed_wheel_read_only_cli_smoke(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", ".", "-w", str(wheel_dir)], check=True)
    wheel = next(wheel_dir.glob("*.whl"))
    target = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(target), str(wheel)], check=True)
    env = os.environ.copy()
    # The root conftest points SPEC_CONFIG at a synthetic POSIX path so unit
    # tests use defaults. An installed CLI subprocess must instead observe the
    # same environment a real Windows user gets outside a repository.
    env.pop("SPEC_CONFIG", None)
    env["PYTHONPATH"] = str(target)
    outside_repo = tmp_path / "outside"
    outside_repo.mkdir()
    for args in (["--version"], ["--help"]):
        result = subprocess.run(
            [sys.executable, "-m", "spec_runtime.cli", *args],
            cwd=outside_repo,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        assert "Traceback" not in result.stderr

    strict_result = subprocess.run(
        [sys.executable, "-m", "spec_runtime.cli", "list"],
        cwd=outside_repo,
        env=env,
        text=True,
        capture_output=True,
    )
    assert strict_result.returncode == 1
    assert str(outside_repo / ".spec.toml") in strict_result.stderr
    assert "site-packages" not in strict_result.stderr

    repo = tmp_path / "Spec Butler snow-雪"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True)
    result = subprocess.run(
        [sys.executable, "-m", "spec_runtime.cli", "init"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    assert (repo / ".spec.toml").is_file()
    mojibake_name = repo.name.encode("utf-8").decode("cp1252")
    assert not repo.with_name(mojibake_name).exists()

    for args in (["list"], ["show", "--spec", "missing"], ["status", "--spec", "missing"]):
        result = subprocess.run(
            [sys.executable, "-m", "spec_runtime.cli", *args],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
        )
        assert "Traceback" not in result.stderr
