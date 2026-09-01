"""Bounded host-side ``git fetch`` invocations.

The host owns the timeout and classification of fetch failures so we can tell
a real network or auth failure apart from an unbounded hang. Existing fetch
behavior (returncode == 0) is unchanged; only the host-side wrapper is added.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor

DEFAULT_GIT_FETCH_TIMEOUT_SECONDS = 60.0


class GitFetchOutcomeKind(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    FAILURE = "failure"


class GitFetchTimeoutError(RuntimeError):
    """Raised when a host-bounded ``git fetch`` exceeds its timeout."""

    def __init__(self, command: Sequence[str], timeout_seconds: float, partial_output: str = ""):
        self.command = tuple(command)
        self.timeout_seconds = float(timeout_seconds)
        self.partial_output = partial_output
        super().__init__(
            f"git fetch exceeded {self.timeout_seconds:.1f}s timeout: {' '.join(self.command)}"
        )


@dataclass(frozen=True)
class GitFetchOutcome:
    kind: GitFetchOutcomeKind
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    command: tuple[str, ...]
    timeout_seconds: float

    @property
    def is_success(self) -> bool:
        return self.kind is GitFetchOutcomeKind.SUCCESS

    @property
    def is_timeout(self) -> bool:
        return self.kind is GitFetchOutcomeKind.TIMEOUT


def classify_git_fetch_failure(
    *,
    returncode: int,
    timed_out: bool,
    stderr: str = "",
) -> GitFetchOutcomeKind:
    """Classify a ``git fetch`` failure as a timeout or a generic failure.

    Timeouts are reported separately so the orchestrator can treat them as
    transient infrastructure issues without confusing them with a real fetch
    error such as auth, missing ref, or network rejection.
    """

    if timed_out:
        return GitFetchOutcomeKind.TIMEOUT

    lowered = (stderr or "").lower()
    if returncode != 0 and ("timed out" in lowered or "timeout" in lowered) and "stderr" not in lowered:
        # Some git error messages embed the word "timeout" in transport hints.
        # We classify these as TIMEOUT only when the wrapper itself observed it.
        return GitFetchOutcomeKind.FAILURE

    return GitFetchOutcomeKind.SUCCESS if returncode == 0 else GitFetchOutcomeKind.FAILURE


def _run_fetch_process_group(
    command: Sequence[str],
    *,
    cwd: str | None = None,
    capture_output: bool = True,
    text: bool = True,
    timeout: float = DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run the fetch in its own session and kill the whole group on timeout.

    Mirrors subprocess.run's contract for the arguments we use, but a timeout
    tears down ssh/git-upload-pack children too instead of orphaning them.
    """
    del capture_output, check  # always captured; caller inspects returncode
    supervisor = ProcessSupervisor(LifetimeMode.RUN_OWNED)
    managed = supervisor.spawn(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    proc = managed.process
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        managed.terminate(grace_seconds=0)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(list(command), timeout, output=stdout, stderr=stderr)
    finally:
        managed.close()
    return subprocess.CompletedProcess(list(command), proc.returncode, stdout, stderr)


def run_git_fetch_with_timeout(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
    runner: callable = subprocess.run,
    monotonic: callable | None = None,
) -> GitFetchOutcome:
    """Run ``git fetch <args...>`` with an explicit host-side timeout.

    On timeout this raises :class:`GitFetchTimeoutError` so the call site can
    decide whether to log a nonfatal warning or escalate. Successful fetches
    return a :class:`GitFetchOutcome` whose ``kind`` is ``SUCCESS``; non-timeout
    failures return ``FAILURE`` so the caller can preserve existing behavior.
    """

    import time as _time

    monotonic_fn = monotonic or _time.monotonic
    if runner is subprocess.run:
        # git fetch spawns ssh/git-upload-pack children; subprocess.run's
        # timeout only kills the direct child, leaking hung transports. Use the
        # process-group runner unless a test injected its own.
        runner = _run_fetch_process_group
    command = ("git", "fetch", *list(args))
    started = monotonic_fn()
    try:
        completed = runner(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = monotonic_fn() - started
        partial = ""
        if exc.stderr:
            partial = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace")
        raise GitFetchTimeoutError(command, float(timeout_seconds), partial) from exc

    elapsed = monotonic_fn() - started
    kind = classify_git_fetch_failure(
        returncode=completed.returncode,
        timed_out=False,
        stderr=completed.stderr or "",
    )
    return GitFetchOutcome(
        kind=kind,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=float(elapsed),
        timed_out=False,
        command=command,
        timeout_seconds=float(timeout_seconds),
    )
