from __future__ import annotations

import json
from pathlib import Path

import pytest

from spec_runtime.platform import is_unc_path
from spec_runtime.platform_fs import FileLock, atomic_write_text, remove_tree
from spec_runtime.spec_identity import SPEC_ID_RE


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


@pytest.mark.parametrize("value", ["con", "prn", "aux", "nul", "com1", "lpt9"])
def test_windows_device_names_are_not_spec_ids(value: str) -> None:
    assert SPEC_ID_RE.fullmatch(value) is None


def test_spec_ids_are_bounded_for_generated_windows_paths() -> None:
    assert SPEC_ID_RE.fullmatch("a" * 64)
    assert SPEC_ID_RE.fullmatch("a" * 65) is None
