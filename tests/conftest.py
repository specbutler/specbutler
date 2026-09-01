"""Suite-wide isolation for process-supervision control state."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

_CONTROL_ROOT_SEQUENCE = itertools.count()


@pytest.fixture(autouse=True)
def _isolate_process_control_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Keep tests and their child processes out of the user's control root.

    A process-supervision test may launch a real helper in a child interpreter,
    so patching the Python helper alone is insufficient.  The environment
    variable is the public cross-process configuration seam and is inherited by
    every ordinary subprocess the test starts.
    """
    # getbasetemp() already belongs exclusively to this pytest worker. A lazy
    # counter path gives every test and child process a unique root without
    # creating thousands of empty directories in tests that never supervise.
    root = tmp_path_factory.getbasetemp() / f"pc{next(_CONTROL_ROOT_SEQUENCE)}"
    monkeypatch.setenv("SPEC_PROCESS_CONTROL_ROOT", str(root))
    return root
