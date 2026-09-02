from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tools" / "windows-lab" / "memory_config.py"


def _resolve(guest_memory: str, container_limit: str = "") -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(HELPER), "--guest-memory", guest_memory]
    if container_limit:
        command.extend(("--container-limit", container_limit))
    return subprocess.run(command, check=False, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("guest_memory", "expected_limit"),
    [
        ("16G", "20G"),
        ("16384", "20G"),
        ("32G", "40G"),
        ("65536M", "80G"),
    ],
)
def test_legacy_config_derives_limit_from_custom_guest_memory(
    guest_memory: str,
    expected_limit: str,
) -> None:
    result = _resolve(guest_memory)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_limit


def test_explicit_safe_limit_is_normalized() -> None:
    result = _resolve("16G", "24576M")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "24G"


@pytest.mark.parametrize(
    ("guest_memory", "container_limit", "minimum"),
    [
        ("16G", "19G", "20G"),
        ("32G", "20G", "40G"),
        ("65536M", "64G", "80G"),
    ],
)
def test_explicit_undersized_limit_fails_before_compose(
    guest_memory: str,
    container_limit: str,
    minimum: str,
) -> None:
    result = _resolve(guest_memory, container_limit)

    assert result.returncode == 2
    assert result.stdout == ""
    assert f"must be at least {minimum}" in result.stderr
