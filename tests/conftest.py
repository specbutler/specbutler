"""Suite-wide state isolation and capability gates for restricted environments."""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

_REAL_PROVIDER_HOMES = {
    "codex": Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
    "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")),
}

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


def ps_unavailability_reason(os_name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Return why ``requires_ps`` tests must skip on ``os_name``, or ``None``.

    Only POSIX inspects processes through ``ps``; Windows uses kernel32, so a
    missing ``ps`` there says nothing about whether those tests can run.
    """
    if os_name != "posix":
        return None
    return probe_ps(env)


def probe_nested_sandbox() -> str | None:
    if shutil.which("codex") is None:
        return "Codex CLI is not installed"
    with tempfile.TemporaryDirectory() as home:
        try:
            result = subprocess.run(
                ["codex", "sandbox", "--", "true"],
                env={**os.environ, "CODEX_HOME": home},
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
    if result.returncode:
        return result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "sandbox launch failed"
    return None


def probe_browser() -> str | None:
    browser = shutil.which("google-chrome") or shutil.which("chromium")
    if browser is None:
        return "Chrome/Chromium is not installed"
    with tempfile.TemporaryDirectory() as home:
        try:
            result = subprocess.run(
                [browser, "--headless=new", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", "--disable-background-networking",
                 f"--user-data-dir={home}/profile", "--dump-dom", "about:blank"],
                env={**os.environ, "HOME": home, "XDG_CONFIG_HOME": home,
                     "XDG_CACHE_HOME": home},
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
    if result.returncode or "<html" not in result.stdout.lower():
        return f"headless browser probe failed (exit {result.returncode}): {result.stderr.strip()[-300:]}"
    return None


def probe_provider_home(provider: str) -> str | None:
    try:
        list(_REAL_PROVIDER_HOMES[provider].iterdir())
    except OSError as exc:
        return str(exc)
    return None


def probe_network() -> str | None:
    """Probe local networking without depending on an external service."""
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
    except OSError as exc:
        return str(exc)
    return None


_capability_reasons: dict[str, str] = {}
_capability_counts: dict[str, int] = {}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_ps`` tests, with the probe's error, when ``ps`` cannot run.

    The probe runs at most once per session, only on POSIX, and only when a
    collected test actually needs ``ps``.
    """
    for item in items:
        marker = item.get_closest_marker("requires_provider_home")
        if marker:
            provider = marker.args[0]
            if reason := probe_provider_home(provider):
                _capability_reasons["provider_home"] = reason
                item.add_marker(pytest.mark.skip(reason=f"provider_home unavailable ({reason})"))
    for capability, probe in {
        "nested_sandbox": probe_nested_sandbox,
        "browser": probe_browser,
        "network": probe_network,
    }.items():
        marked = [item for item in items if item.get_closest_marker(f"requires_{capability}")]
        if marked and (reason := probe()) is not None:
            _capability_reasons[capability] = reason
            for item in marked:
                item.add_marker(pytest.mark.skip(reason=f"{capability} unavailable ({reason})"))
    needs_ps = [item for item in items if item.get_closest_marker("requires_ps")]
    if not needs_ps:
        return
    reason = ps_unavailability_reason(os.name)
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
    _capability_reasons.clear()
    _capability_counts.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not (report.skipped and isinstance(report.longrepr, tuple)):
        return
    for capability in ("network", "nested_sandbox", "provider_home", "browser"):
        prefix = f"Skipped: {capability} unavailable ("
        if report.longrepr[2].startswith(prefix):
            _capability_reasons[capability] = report.longrepr[2][len(prefix):-1]
            _capability_counts[capability] = _capability_counts.get(capability, 0) + 1
    if report.longrepr[2].startswith(f"Skipped: {_PS_SKIP_PREFIX} ("):
        _session_config.stash[_ps_skip_count_key] += 1
        prefix = f"Skipped: {_PS_SKIP_PREFIX} ("
        _session_config.stash[_ps_unavailability_key] = report.longrepr[2][len(prefix):-1]


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """Show one count per unavailable capability, including xdist worker reports."""
    for capability, count in sorted(_capability_counts.items()):
        terminalreporter.write_line(
            f"{count} tests skipped: {capability} unavailable ({_capability_reasons[capability]})",
            yellow=True,
        )
    count = config.stash.get(_ps_skip_count_key, 0)
    reason = config.stash.get(_ps_unavailability_key, None)
    if count and reason:
        noun = "test" if count == 1 else "tests"
        terminalreporter.write_line(f"{count} {noun} skipped: {_PS_SKIP_PREFIX} ({reason})", yellow=True)


@pytest.fixture(autouse=True)
def _isolate_user_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    """Isolate state and provider homes, including resolvers that ignore XDG."""
    root = tmp_path_factory.getbasetemp() / f"user{next(_CONTROL_ROOT_SEQUENCE)}"
    for name, relative in {
        "HOME": "home", "USERPROFILE": "home", "LOCALAPPDATA": "local",
        "XDG_STATE_HOME": "state", "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
    }.items():
        (root / relative).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(root / relative))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    marker = request.node.get_closest_marker("requires_provider_home")
    if marker:
        provider = marker.args[0]
        name = "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"
        monkeypatch.setenv(name, str(_REAL_PROVIDER_HOMES[provider]))


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


@pytest.fixture(autouse=True)
def _stub_provider_session_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep implement tests from shelling out to provider ``--help`` probes.

    Tests get stateless launches (no persisted session, no structured
    output) unless they stub the probe to report support (see
    tests/test_agent_sessions.py).
    """
    from spec_runtime import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "_provider_session_support",
        lambda _agent, *_probe_target: orchestrator.ProviderSessionSupport(),
    )
