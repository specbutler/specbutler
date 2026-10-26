"""Suite-wide isolation for process-supervision control state."""

from __future__ import annotations

import itertools
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

_CONTROL_ROOT_SEQUENCE = itertools.count()

_PS_SKIP_PREFIX = "ps unavailable"
_ps_unavailability_key = pytest.StashKey[str | None]()
_ps_skip_count_key = pytest.StashKey[int]()
_session_config: pytest.Config


def probe_ps(env: Mapping[str, str] | None = None) -> str | None:
    """Return why ``ps`` cannot inspect this process, or ``None`` if usable.

    Sandboxed agent sessions deny ``ps`` outright, and process-inspection tests
    then fail for reasons unrelated to the code under test.  ``ps`` counts as
    usable only when it exists, exits 0 for our own PID, and does not report
    "Operation not permitted".
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            errors="replace",
            env=None if env is None else dict(env),
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "ps not found"
    except subprocess.TimeoutExpired:
        return "ps timed out"
    except OSError as exc:
        return exc.strerror or str(exc)
    output = f"{result.stderr}\n{result.stdout}"
    if "operation not permitted" in output.lower():
        return "Operation not permitted"
    if result.returncode != 0:
        detail = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
        return f"ps exited {result.returncode}" + (f": {detail}" if detail else "")
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_ps`` tests, with the probe's error, when ``ps`` cannot run.

    The probe runs at most once per session, and only when a collected test
    actually needs ``ps``.
    """
    needs_ps = [item for item in items if item.get_closest_marker("requires_ps")]
    if not needs_ps:
        return
    reason = probe_ps()
    config.stash[_ps_unavailability_key] = reason
    if reason is None:
        return
    skip = pytest.mark.skip(reason=f"{_PS_SKIP_PREFIX} ({reason})")
    for item in needs_ps:
        item.add_marker(skip)


def pytest_configure(config: pytest.Config) -> None:
    global _session_config
    _session_config = config
    config.stash[_ps_skip_count_key] = 0


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not (report.skipped and isinstance(report.longrepr, tuple)):
        return
    if report.longrepr[2].startswith(f"Skipped: {_PS_SKIP_PREFIX} ("):
        _session_config.stash[_ps_skip_count_key] += 1


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """Make ps-driven skips visible as one line instead of silent ``s`` marks."""
    count = config.stash.get(_ps_skip_count_key, 0)
    reason = config.stash.get(_ps_unavailability_key, None)
    if count and reason:
        noun = "test" if count == 1 else "tests"
        terminalreporter.write_line(f"{count} {noun} skipped: {_PS_SKIP_PREFIX} ({reason})", yellow=True)


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
