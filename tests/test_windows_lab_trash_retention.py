from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tools" / "windows-lab" / "trash_retention.py"
LABCTL = REPO_ROOT / "tools" / "windows-lab" / "labctl"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_lab_trash_retention", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _retired(trash: Path, name: str, *, modified_ns: int) -> Path:
    retired = trash / name
    tpm = retired / "tpm"
    tpm.mkdir(parents=True)
    (retired / "run.qcow2").write_bytes(b"overlay")
    (retired / "nvram.fd").write_bytes(b"nvram")
    (tpm / "tpm2-00.permall").write_bytes(b"tpm")
    os.utime(retired, ns=(modified_ns, modified_ns))
    return retired


def _controller_retention(
    tmp_path: Path,
    docker_state: str,
    *,
    action: str = "retention",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    state_root = tmp_path / "state"
    trash = state_root / "trash"
    trash.mkdir(parents=True)
    older = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    newer = _retired(trash, "20260901T020202-202", modified_ns=2_000_000_000)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    config = tmp_path / "lab.env"
    config.write_text(
        "WINDOWS_ISO=/tmp/fake.iso\n"
        f"WINDOWS_ISO_SHA256={'0' * 64}\n"
        "WINDOWS_IMAGE_INDEX=1\n"
        "LAB_DOCKER_QUERY_TIMEOUT_SECONDS=1\n",
        encoding="utf-8",
    )
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
case "$FAKE_DOCKER_STATE" in
    query-error) exit 125 ;;
    absent) exit 0 ;;
    ambiguous)
        printf '%s\\n' 'specbutler-windows-lab|exited' 'specbutler-windows-lab|created'
        ;;
    *) printf '%s|%s\\n' 'specbutler-windows-lab' "$FAKE_DOCKER_STATE" ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    script = r"""
source <(awk '/^stage_source\(\)/ { exit } { print }' "$LABCTL_PATH")
LAB_ROOT="$LAB_ROOT_OVERRIDE"
HARNESS_ROOT="$LAB_ROOT"
STATE_ROOT="$STATE_ROOT_OVERRIDE"
CONFIG_FILE="$CONFIG_FILE_OVERRIDE"
case "$CONTROLLER_ACTION" in
    retention) proof_trash_retention 1 apply ;;
    vm-running)
        if vm_running; then
            printf '%s\n' RUNNING
        else
            printf '%s\n' INTERPRETED_AS_STOPPED
        fi
        ;;
    *) exit 97 ;;
esac
"""
    env = {
        **os.environ,
        "FAKE_DOCKER_STATE": docker_state,
        "LABCTL_PATH": str(LABCTL),
        "LAB_ROOT_OVERRIDE": str(LABCTL.parent),
        "STATE_ROOT_OVERRIDE": str(state_root),
        "CONFIG_FILE_OVERRIDE": str(config),
        "CONTROLLER_ACTION": action,
        "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
    }
    completed = subprocess.run(
        [bash, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return completed, older, newer


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Windows VM controller runs on its supported Linux host",
)
def test_vm_running_does_not_interpret_query_failure_as_stopped(
    tmp_path: Path,
) -> None:
    completed, older, newer = _controller_retention(
        tmp_path,
        "query-error",
        action="vm-running",
    )

    assert completed.returncode != 0
    assert "could not query Docker" in completed.stderr
    assert "INTERPRETED_AS_STOPPED" not in completed.stdout
    assert older.is_dir()
    assert newer.is_dir()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Windows VM controller runs on its supported Linux host",
)
@pytest.mark.parametrize(
    ("docker_state", "error"),
    [
        ("query-error", "could not query Docker"),
        ("ambiguous", "ambiguous Windows VM state"),
        ("unknown", "unknown Windows VM state"),
        ("running", "must be stopped"),
        ("paused", "must be stopped"),
        ("restarting", "must be stopped"),
        ("removing", "must be stopped"),
    ],
)
def test_controller_retention_fails_closed_without_mutation(
    tmp_path: Path,
    docker_state: str,
    error: str,
) -> None:
    completed, older, newer = _controller_retention(tmp_path, docker_state)

    assert completed.returncode != 0
    assert error in completed.stderr
    assert older.is_dir()
    assert newer.is_dir()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Windows VM controller runs on its supported Linux host",
)
@pytest.mark.parametrize("docker_state", ["absent", "created", "exited", "dead"])
def test_controller_retention_accepts_confirmed_inactive_state(
    tmp_path: Path,
    docker_state: str,
) -> None:
    completed, older, newer = _controller_retention(tmp_path, docker_state)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not older.exists()
    assert newer.is_dir()


def test_retention_dry_run_then_apply_keeps_only_newest_and_never_leaves_root(
    tmp_path: Path,
) -> None:
    module = _load_helper()
    state = tmp_path / "state"
    trash = state / "trash"
    trash.mkdir(parents=True)
    # Deliberately make mtimes oppose the controller-owned timestamp names.
    oldest = _retired(trash, "20260901T010101-101", modified_ns=3_000_000_000)
    middle = _retired(trash, "20260901T020202-202", modified_ns=2_000_000_000)
    newest = _retired(trash, "20260901T030303-303", modified_ns=1_000_000_000)
    active = state / "disk" / "run.qcow2"
    baseline = state / "baselines" / "toolchain" / "disk.qcow2"
    active.parent.mkdir(parents=True)
    baseline.parent.mkdir(parents=True)
    active.write_bytes(b"active")
    baseline.write_bytes(b"baseline")

    dry_run = module.apply_retention(trash_root=trash, keep=1, apply=False)

    assert dry_run["retained"] == [newest.name]
    assert dry_run["would_remove"] == [middle.name, oldest.name]
    assert dry_run["removed"] == []
    assert all(path.is_dir() for path in (oldest, middle, newest))

    applied = module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert applied["removed"] == [middle.name, oldest.name]
    assert newest.is_dir()
    assert not oldest.exists()
    assert not middle.exists()
    assert active.read_bytes() == b"active"
    assert baseline.read_bytes() == b"baseline"


def test_retention_uses_numeric_pid_as_same_second_tiebreaker(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    lower_pid = _retired(trash, "20260901T010101-9", modified_ns=2_000_000_000)
    higher_pid = _retired(trash, "20260901T010101-10", modified_ns=1_000_000_000)

    result = module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert result["retained"] == [higher_pid.name]
    assert result["removed"] == [lower_pid.name]


def test_retention_is_noop_when_valid_entries_do_not_exceed_keep(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    retained = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)

    result = module.apply_retention(trash_root=trash, keep=2, apply=True)

    assert result["retained"] == [retained.name]
    assert result["removed"] == []
    assert retained.is_dir()


@pytest.mark.parametrize("keep", [0, -1])
def test_retention_rejects_nonpositive_keep_without_deleting(
    tmp_path: Path,
    keep: int,
) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    retired = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)

    with pytest.raises(module.TrashRetentionError, match="positive integer"):
        module.apply_retention(trash_root=trash, keep=keep, apply=True)

    assert retired.is_dir()


@pytest.mark.parametrize("invalid", ["", "0", "-1", "+1", " 1", "1 ", "1.0"])
def test_keep_parser_rejects_noncanonical_values(invalid: str) -> None:
    module = _load_helper()

    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        module.parse_keep(invalid)


def test_unknown_or_partial_child_fails_closed_before_any_deletion(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    oldest = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    newest = _retired(trash, "20260901T020202-202", modified_ns=2_000_000_000)
    (newest / "operator-note.txt").write_text("retain", encoding="utf-8")

    with pytest.raises(module.TrashRetentionError, match="unsafe layout"):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert oldest.is_dir()
    assert newest.is_dir()


def test_unknown_direct_child_fails_closed_before_any_deletion(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    retired = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    (trash / "README").write_text("operator state", encoding="utf-8")

    with pytest.raises(module.TrashRetentionError, match="unknown entry"):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert retired.is_dir()


def test_invalid_controller_timestamp_fails_closed_before_any_deletion(
    tmp_path: Path,
) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    complete = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    invalid = _retired(trash, "20261340T256199-202", modified_ns=2_000_000_000)

    with pytest.raises(module.TrashRetentionError, match="unknown entry"):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert complete.is_dir()
    assert invalid.is_dir()


def test_partial_retired_directory_fails_closed_before_any_deletion(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    complete = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    partial = trash / "20260901T020202-202"
    partial.mkdir()
    (partial / "run.qcow2").write_bytes(b"overlay")

    with pytest.raises(module.TrashRetentionError, match="missing="):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert complete.is_dir()
    assert partial.is_dir()


def test_symlinked_retired_directory_fails_closed_before_any_deletion(
    tmp_path: Path,
) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    complete = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    target = tmp_path / "outside"
    target.mkdir()
    linked = trash / "20260901T020202-202"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(module.TrashRetentionError, match="plain directory"):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert complete.is_dir()
    assert target.is_dir()


def test_symlinked_trash_root_is_refused(tmp_path: Path) -> None:
    module = _load_helper()
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "trash"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(module.TrashRetentionError, match="plain directory"):
        module.apply_retention(trash_root=linked, keep=1, apply=True)


def test_nested_tpm_symlink_fails_closed_before_any_deletion(tmp_path: Path) -> None:
    module = _load_helper()
    trash = tmp_path / "trash"
    trash.mkdir()
    oldest = _retired(trash, "20260901T010101-101", modified_ns=1_000_000_000)
    newest = _retired(trash, "20260901T020202-202", modified_ns=2_000_000_000)
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    try:
        (newest / "tpm" / "escape").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(module.TrashRetentionError, match="contains a symlink"):
        module.apply_retention(trash_root=trash, keep=1, apply=True)

    assert oldest.is_dir()
    assert newest.is_dir()
    assert target.read_text(encoding="utf-8") == "outside"
