from __future__ import annotations

import errno
import json
import multiprocessing
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from spec_runtime.platform import is_unc_path
from spec_runtime.platform_fs import FileLock, atomic_write_text, remove_tree
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


def test_remove_tree_repairs_read_only_file(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    item = tree / "item"
    item.write_text("data")
    item.chmod(0o444)
    remove_tree(tree)
    assert not tree.exists()


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
    env["PYTHONPATH"] = str(target)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True)
    for args in (["--version"], ["--help"], ["init"], ["list"], ["show", "--spec", "missing"], ["status", "--spec", "missing"]):
        result = subprocess.run([sys.executable, "-m", "spec_runtime.cli", *args], cwd=repo, env=env, text=True, capture_output=True)
        assert "Traceback" not in result.stderr
