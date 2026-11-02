"""Suite-wide isolation for process-supervision control state."""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest

_CONTROL_ROOT_SEQUENCE = itertools.count()


@pytest.fixture(autouse=True)
def _pin_current_checkout_for_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make child interpreters exercise the same package pytest imported.

    Worktrees commonly share one editable virtualenv.  Pytest adds this
    checkout's ``src`` directory to its own import path, but a child launched
    from a temporary repository otherwise falls back to whichever checkout
    that virtualenv last installed.  Conversely, installed-wheel CI deliberately
    disables pytest's checkout import path.  Derive the boundary from the package
    already imported by this pytest process so both modes remain honest, while
    retaining any caller-supplied import paths.  Individual tests remain free to
    clear or replace ``PYTHONPATH`` when that is the behavior at issue.
    """
    import spec_runtime

    source_root = Path(spec_runtime.__file__).resolve().parent.parent
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(source_root), inherited) if part),
    )


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


@pytest.fixture(autouse=True)
def _scope_orchestrator_jobs_to_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixed test run ids from sharing a machine-global Windows Job.

    The orchestrator claims ``orchestrator-<run_id>`` as a ``Global\\`` named
    Job and joins it for life.  Tests reuse fixed run ids, so a second pytest
    process (an xdist worker) claiming the same id would open the first
    process's Job and be refused with ERROR_ACCESS_DENIED.  Suffixing this
    process's pid keeps the real claim while making the name ours alone.
    """
    if os.name != "nt":
        return
    from spec_runtime import orchestrator

    claim = orchestrator.claim_current_process
    suffix = f"pytest{os.getpid()}"
    monkeypatch.setattr(
        orchestrator,
        "claim_current_process",
        lambda supervision_id: claim(f"{supervision_id}-{suffix}"),
    )


@pytest.fixture(autouse=True)
def _stub_claude_native_launch_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep implement tests from shelling out to ``claude --help``.

    The native-launch capability probe is a seam: tests get the restricted
    acceptEdits fallback unless they stub the probe to report support (see
    tests/test_native_safety_mode.py).
    """
    from spec_runtime import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "_claude_native_launch_unavailability_reason",
        lambda: "native Claude launch probe is stubbed in tests",
    )
