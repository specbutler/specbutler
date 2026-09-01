#!/usr/bin/env python3
"""Spec Butler orchestrator — owns the spec lifecycle.

Phases: bootstrap -> scoping -> intake -> implement -> verify -> publish -> review -> merge -> cleanup

The canonical CLI interface is the ``spec`` command (see ``spec_runtime.cli``).
This module provides the internal orchestrator runtime with low-level
subcommands that the CLI delegates to.

Canonical CLI usage (primary interface)::

    spec create [--spec <id>] [--agent claude|codex]
    spec implement --spec <id> [--agent claude|codex] [--review-agent claude|codex]
    spec input --spec <id> [--agent claude|codex]
    spec status --spec <id>
    spec list [--all]
    spec show --spec <id>
    spec report --status ok|blocked|error [--summary ...]
    spec clean --spec <id>
    spec task [--agent claude|codex] [--review-agent claude|codex]
    spec phase --spec <id> --phase <phase> [--agent claude|codex] [--review-agent claude|codex]

Internal orchestrator subcommands (available via spec_runtime.orchestrator:main)::

    spec implement --spec <id> --agent claude|codex [--review-agent claude|codex]
    spec create [--spec <id>] [--agent claude|codex]
    spec input --spec <id> [--agent claude|codex]
    spec task [--agent claude|codex] [--review-agent claude|codex]
    spec phase --spec <id> --phase <phase> [--agent claude|codex] [--review-agent claude|codex]
    spec status --spec <id>
    spec analytics [--spec <id>] [--run <run-id>] [--since <iso-date>]
    spec report --status ok|blocked|error [--summary ...]
    spec report --spec <id> --status passed|blocked|failed [--summary ...]
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib import parse as urllib_parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import review_feedback, worktree_process_registry
from .agent_adapter import (
    AgentAdapter,
    _codex_linux_sandbox_overrides,
    _render_codex_mcp_toml,
    claude_isolated_home,
    codex_isolated_home,
    get_agent_adapter,
)
from .command_runtime import CommandSpec, CommandVariants
from .config import load_spec_runtime_config
from .control_plane import (
    DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
    GateRecordStore,
    GitFetchTimeoutError,
    load_run_lease,
    project_run_status,
    record_gate_completed,
    record_gate_started,
    record_gate_timeout,
    run_git_fetch_with_timeout,
    save_run_lease,
)
from .control_plane.lease import build_lease
from .coordination import (
    CoordinatorError,
    CoordinatorLeaseConflictError,
    lease_age_seconds,
)
from .coordination import (
    build_client as build_coordinator_client,
)
from .execution_backend import (
    CONTAINER_SERVICE_POSTGRES_ENVS,
    AgentRequest,
    CommandRequest,
    ContainerExecutionBackend,
    ExecutionBackend,
    ExecutionBackendImportError,
    ExecutionBackendNotImplementedError,
    SnapshotRef,
    UnknownExecutionBackendError,
    WorkspaceHandle,
)
from .execution_backend import get_execution_backend as _factory_get_execution_backend
from .forge import GitHubForge, PushResult
from .git_common import resolve_common_root
from .platform_fs import FileLock, atomic_write_text, lock_metadata_offset, read_lock_metadata, remove_tree
from .process_supervisor import (
    LifetimeMode,
    ManagedProcess,
    ProcessSupervisor,
    SupervisionToken,
    claim_current_process,
    identity_matches,
    inspect_process,
    terminate_legacy_popen_tree,
)
from .process_supervisor import run as run_supervised
from .process_supervisor import terminate as terminate_supervised
from .spec_identity import (
    SPEC_ID_RE,
    authoring_branch_identity,
    format_pr_review_owner,
    implementation_branch_identity,
    is_authoring_branch,
    pr_body_uses_local_review,
    resolve_spec_id_for_pr,
    spec_run_branch,
    spec_run_worktree_name,
)
from .spec_merge_tags import (
    MergeTagProvenance,
    annotated_tag_command,
    build_tag_message,
    merge_tag_name,
    spec_id_referenced,
    utc_timestamp_now,
)
from .spec_metadata import parse_spec_frontmatter as load_spec_frontmatter
from .spec_status import collect_git_spec_state, is_spec_merged, refresh_merge_completion_state
from .spec_status import get_spec_status as read_spec_status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORCHESTRATOR_RUNTIME = "package"
SPEC_RUNTIME_CONFIG = load_spec_runtime_config(require=False)
BASE_REF = SPEC_RUNTIME_CONFIG.base_ref
PR_BASE_BRANCH = SPEC_RUNTIME_CONFIG.pr_base_branch

PHASES = (
    "bootstrap",
    "scoping",
    "intake",
    "implement",
    "verify",
    "publish",
    "review",
    "merge",
    "cleanup",
)
VALID_ACTIONS = ("run", "spec", "task", "step", "status", "input", "steer", "analytics", "report", "complete")
VALID_AGENTS = SPEC_RUNTIME_CONFIG.agents.allowed
RETRY_REASON_LABELS = {
    "implement_failures": "Implement failure",
    "verify_failures": "Verify failure",
    "merge_conflicts": "Merge conflict",
    "review_changes": "Review changes",
}
# Shared retry limit across verify, review, and merge retry paths.
RETRY_CAP = SPEC_RUNTIME_CONFIG.retry_cap
# Merge conflicts draw on their own budget instead of the shared cap.
#
# The shared cap is a circuit breaker for a run that cannot converge. Conflict
# frequency measures something else entirely: how often anything *else* lands on
# the base branch. A spec that sits in review while its neighbours merge can
# exhaust the shared cap without a single failure of its own, and raising
# autopilot concurrency makes that strictly more likely. Still bounded, so a
# genuinely unmergeable branch stops rather than retrying forever.
MERGE_CONFLICT_RETRY_CAP = int(os.environ.get("SPEC_MERGE_CONFLICT_RETRY_CAP", "5"))
NO_PROGRESS_RETRY_THRESHOLD = SPEC_RUNTIME_CONFIG.no_progress_retry_threshold
REQUIRED_GATES = tuple(gate.name for gate in SPEC_RUNTIME_CONFIG.verify_gates)
PARALLEL_VERIFY_GATES = frozenset(gate.name for gate in SPEC_RUNTIME_CONFIG.verify_gates if gate.parallel)
VERIFY_GATE_COMMANDS = {gate.name: gate.command for gate in SPEC_RUNTIME_CONFIG.verify_gates}
# Host-bounded verify gate timeout. Picked as a generous upper bound so a hung
# gate cannot block a run indefinitely while still leaving normal gates room to
# complete. Overridable via SPEC_VERIFY_GATE_TIMEOUT_SECONDS for operators that
# need to tune long-running suites.
DEFAULT_VERIFY_GATE_TIMEOUT_SECONDS = float(
    os.environ.get("SPEC_VERIFY_GATE_TIMEOUT_SECONDS", "1800")
)
# Gates whose effective role is "test" get pytest-specific behavior
# (test environment, diagnostics, fingerprinting).
TEST_ROLE_GATES = frozenset(gate.name for gate in SPEC_RUNTIME_CONFIG.verify_gates if gate.effective_role == "test")


def _is_test_gate(gate: str) -> bool:
    """Return True if *gate* should receive pytest-style execution semantics."""
    return gate in TEST_ROLE_GATES


def _non_e2e_verify_commands() -> tuple[str, ...]:
    return tuple(gate.command for gate in SPEC_RUNTIME_CONFIG.verify_gates if gate.effective_role != "e2e")


def _e2e_verify_commands() -> tuple[str, ...]:
    return tuple(gate.command for gate in SPEC_RUNTIME_CONFIG.verify_gates if gate.effective_role == "e2e")


def _format_verify_commands(commands: tuple[str, ...]) -> str:
    if not commands:
        return ""
    if len(commands) == 1:
        return f"`{commands[0]}`"
    return " and ".join(f"`{command}`" for command in commands)


SHM_PREFLIGHT_CLEANUP_THRESHOLD = 24
REVIEW_GATE_CHECK_NAME = review_feedback.REVIEW_GATE_CHECK_NAME
REVIEW_GATE_ARTIFACT_NAME = review_feedback.REVIEW_GATE_ARTIFACT_NAME
REVIEW_GATE_ARTIFACT_FILE = review_feedback.REVIEW_GATE_ARTIFACT_FILE
REVIEW_POLL_INTERVAL_SECONDS = 10
REVIEW_TIMEOUT_SECONDS = 900
LOCAL_REVIEW_FIRST_PASS_REASONING_EFFORT = "high"
LOCAL_REVIEW_REASONING_EFFORT = "low"
LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES = 40
LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS = 4000
LOCAL_REVIEW_TIMEOUT_RAW_REVIEW_MAX_CHARS = 4000
BLOCK_DEBUGGER_TIMEOUT_SECONDS = 600
REVIEW_BOOTSTRAP_TIMEOUT_SECONDS = 900
LOCAL_REVIEW_FIRST_PASS_EXHAUSTIVE_INSTRUCTION = (
    "First-pass review requirement: find ALL issues in a single pass rather than "
    "stopping at the first few. A missed finding costs a full retry cycle."
)
LOCAL_REVIEW_TIMEOUT_PENDING_DESCRIPTION = (
    "Local review timed out; review remains pending until the orchestrator retries."
)
# The counterpart to the exhaustive-review instruction above, aimed at the fixer
# rather than the reviewer. A reviewer reads a diff and reports the instances it
# happened to see; an agent that fixes exactly those hands the next round the
# rest of the same defect, one per round. Operators have been breaking those
# loops by hand with `spec steer` messages that all say this same thing, so say
# it up front instead.
REVIEW_FINDING_CLASS_INSTRUCTION = (
    "Treat each finding as one instance of a class, not as the whole defect. Before fixing, "
    "ask what else has the same shape — other call sites of a contract enforced at only one, "
    "other input forms a validator does not recognize, other acceptance-checklist items with "
    "no test. Derive the full set from a source of truth (a grep, a registry, the spec's "
    "acceptance checklist) rather than by hand, fix the class in one pass, and state in your "
    "completion summary how you derived completeness — the exact command or list you used and "
    "what it returned — so the next review can check coverage instead of finding the next "
    "instance. If you deliberately leave part of the class alone, say which part and why."
)
LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX = "Local review must rerun before merge"
LOCAL_REVIEW_RERUN_AFTER_SYNC_LEGACY_PREFIX = "Local review must rerun after syncing with origin/main"
LOCAL_REVIEW_WORKTREE_PREFIX = "spec-review-"
LOCAL_MERGEABILITY_WORKTREE_PREFIX = "spec-mergeability-"
LOCAL_BLOCK_DEBUGGER_WORKTREE_PREFIX = "spec-block-debugger-"
BLOCK_DEBUGGER_PRIVATE_CLONE_MARKER = ".spec-block-debugger-owner.json"
BLOCK_DEBUGGER_AUTO_RESUME_LIMIT = 1
LOCAL_REVIEW_WORKTREE_ROOT = Path(tempfile.gettempdir()).resolve()
GITHUB_API_VERSION = "2022-11-28"
LOCAL_REVIEW_DISABLED_CREDENTIAL_ENV_VARS = (
    "GH_ENTERPRISE_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
    "OPENAI_API_KEY",
    "SSH_AGENT_PID",
    "SSH_AUTH_SOCK",
)
MERGE_CHECKS_POLL_INTERVAL_SECONDS = 10
MERGE_CHECKS_TIMEOUT_SECONDS = 900
REVIEW_DECISION_VALUES = ("approved", "request_changes", "blocked", "failed")
INTAKE_FILE_VERSION = 1
INTAKE_INPUT_TYPES = ("string", "int", "float", "bool", "choice")
TASK_SCOPING_PROMPT_FILE = "prompts/task-scoping.md"
TASK_BRANCH_PREFIX = "task/"
TASK_SPEC_DIR = SPEC_RUNTIME_CONFIG.paths.task_specs_dir
RUN_MODES = ("spec", "task")
RESUMABLE_RUN_STATUSES = ("pending", "running", "failed", "blocked", "waiting-for-input")
STEP_SELECTABLE_RUN_STATUSES = (*RESUMABLE_RUN_STATUSES, "passed")
# "verify" is included so a run left passed/verify by a manual
# `spec phase --phase verify` recovery can resume into publish instead of
# stranding as "not resumable".
AUTO_RESUME_PHASES = ("verify", "publish", "review", "merge")
# Deliberately excludes "verify": failed verify runs re-enter through the
# normal retry policy, not the late-phase resume path.
FAILED_LATE_PHASE_RESUME_PHASES = ("publish", "review", "merge", "cleanup")
AGENT_COMPLETION_POLL_SECONDS = 0.5
AGENT_EXIT_GRACE_SECONDS = 5.0
AGENT_TERMINATE_TIMEOUT_SECONDS = 5.0
CODEX_IDLE_TIMEOUT_SECONDS = 600.0
# Claude streams an event per message/tool transition. This is its ordinary
# between-events ceiling; a tracked tool_use raises it to the command ceiling
# below. Keep it conservative for older/malformed streams whose tool IDs cannot
# be paired, but far below the infinite hangs it exists to catch.
CLAUDE_IDLE_TIMEOUT_SECONDS = 1800.0
# Claude and Codex are silent for the whole duration of many shell/MCP/tool
# calls, so their plain idle timeout would measure "how long this command
# takes", not "is the agent stuck". Foreground verification gates are mandatory
# (the exit checklist forbids backgrounding them) and full suites legitimately
# run past the between-events ceiling. While an item is in flight, fall back to
# this larger ceiling, which still bounds genuine hangs.
AGENT_COMMAND_IDLE_TIMEOUT_SECONDS = 3600.0
# Codex item types that block the stream until they finish.
CODEX_LONG_RUNNING_ITEM_TYPES = frozenset({"command_execution", "mcp_tool_call"})
IMPLEMENT_DEV_SERVER_READY_TIMEOUT_SECONDS = 20
IMPLEMENT_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 5
GATE_STDOUT_MAX_LINES = 60
GATE_STDOUT_MAX_CHARS = 4000
GATE_STDERR_MAX_LINES = 20
GATE_STDERR_MAX_CHARS = 2000
GATE_DIAGNOSTIC_MAX_LINES = 25
GATE_DIAGNOSTIC_MAX_CHARS = 2500
GATE_HISTORY_LIMIT = 10
RETRY_CAP_ESCALATION_COMMENT_MARKER = "<!-- retry-cap-escalation-summary -->"
RETRY_CAP_ESCALATION_HISTORY_CHAR_LIMIT = 50000
CARRIED_FORWARD_REVIEW_FINDINGS_LIMIT = 10
CARRIED_FORWARD_REVIEW_BODY_MAX_CHARS = 240
TEST_GATE_DIAGNOSTIC_TIMEOUT_SECONDS = 120
RUN_STOP_GRACE_SECONDS = 5.0
TEST_GATE_DIAGNOSTIC_ARGS = [
    "-m",
    "pytest",
    "--tb=short",
    "--no-header",
    "-q",
    "--maxfail=20",
    "tests",
]
TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX = "[diagnostic unavailable]"
TARGETED_TEST_DIAGNOSTIC_MAX_FAILURES = 3
TARGETED_TEST_DIAGNOSTIC_TIMEOUT_SECONDS = 20
TEST_FAILURE_FINGERPRINT_HEX_LENGTH = 16
BLOCK_DIAGNOSIS_FILENAME = "block-diagnosis.json"
OPERATOR_REQUEST_FILENAME = "operator-request.json"
OPERATOR_STEERING_FILENAME = "operator-steering.json"
BLOCK_DEBUGGER_CONTEXT_FILENAME = "block-debugger-context.json"
BLOCK_DEBUGGER_PROMPT_FILENAME = "block-debugger-prompt.md"
BLOCK_DEBUGGER_RAW_OUTPUT_FILENAME = "block-diagnosis.raw.json"
OPERATOR_REQUEST_FULL_SESSION_HINTS = (
    "api version",
    "code change",
    "edit the code",
    "implementation",
    "implement",
    "refactor",
    "migration",
    "schema",
    "endpoint",
    "backend",
    "frontend",
    "database",
    "test",
    "file",
)
PINNED_SPEC_FILENAME = "spec.md"
SPEC_CREATION_PROMPT_FILE = "prompts/spec-creation.md"
SPEC_AUTHORING_BRANCH_PREFIX = "spec/"
SPEC_AUTHORING_SESSION_BRANCH_PREFIX = "spec-authoring/"
SPEC_AUTHORING_SESSION_WORKTREE_PREFIX = "spec-session-"
SPEC_PROMPT = (
    "You are running the IMPLEMENT phase for spec <spec-id> in an already-prepared worktree.\n"
    "Do the following in order:\n"
    "1. Read <spec-path> and AGENTS.md.\n"
    "   This run is pinned to spec revision <spec-revision>.\n"
    "2. Implement only what this attempt requires.\n"
    "3. Commit any completed code changes.\n"
    f"4. Run {_format_verify_commands(_non_e2e_verify_commands())} to verify your changes. "
    f"Do NOT run {_format_verify_commands(_e2e_verify_commands())} — "
    "the orchestrator verify phase handles e2e outside the sandbox.\n"
    "5. Do NOT run orchestrator lifecycle commands (`spec implement`, `spec phase`) "
    "and do NOT run publish/merge/cleanup actions. The only orchestrator command "
    "you should run is `spec report` (see completion step below).\n"
    "6. If local environment limits block test infrastructure (for example Playwright "
    "MachPort/sandbox launch failures), do not report STATUS=blocked for that alone; "
    "report STATUS=ok with the environment note and let the orchestrator verify phase run gates.\n"
    "7. If you face genuine ambiguity where the spec can be interpreted multiple ways "
    "and a wrong guess would waste retry cycles, report STATUS=needs-input with "
    "SUMMARY describing the ambiguity. Include specific options when possible. "
    "The user will be launched into an interactive session to resolve it. "
    "Do NOT use needs-input for: test failures, clear review findings, merge "
    "conflicts, or anything resolvable by reading the code more carefully."
)
COMPLETE_HANDSHAKE_INSTRUCTION = (
    "Before exiting, report implement-phase status with "
    "`spec report --status "
    "ok|blocked|error|needs-input --summary 'plain text summary'` "
    "(explicit fallback: "
    "`spec report --spec <spec-id> --status "
    "ok|blocked|error|needs-input --summary 'plain text summary'`). "
    "Keep the summary shell-safe: single-quote the complete value, avoid apostrophes, "
    "and do not include backticks or `$()`; describe commands without Markdown code "
    "delimiters because the shell evaluates substitutions before `spec report` starts. "
    "Wait for `Completion recorded for <spec-id>:` before exiting."
)
BLOCK_DEBUGGER_PROMPT = (
    "You are running the BLOCKED-RUN DEBUGGER phase for a Spec Butler run.\n"
    "This is a read-only diagnosis task. Do NOT edit files, create commits, push, merge, "
    "or run lifecycle commands such as `spec implement`, `spec phase`, or `spec report`.\n"
    "Inspect the blocked-run evidence package, explain why the loop got stuck, and identify "
    "the smallest credible next move. Your job is not to implement the fix or review the code.\n"
    "Return a single JSON object matching the required schema."
)
MERGE_CONFLICT_PROMPT = (
    "The PR could not be merged due to conflicts with master. "
    "Resolve the conflicts by running: "
    f"git fetch origin && git merge {BASE_REF}. "
    f"Fix any merge conflicts, run {_format_verify_commands(_non_e2e_verify_commands())} "
    "to verify, "
    "then commit the conflict resolution. "
    "Do NOT push — the orchestrator pushes during the publish phase. "
    "Once conflicts are resolved and tests/lint pass, report STATUS=ok "
    "(not blocked). Having unpushed local commits is expected and correct. "
    "Read AGENTS.md for project conventions. "
    "Do NOT re-implement from scratch — only resolve the merge conflicts."
)

# ``--flag=value`` / ``--flag:value`` form: group(1) is the prefix up to and
# including the ``=``/``:`` separator, group(2) is just the flag name (used
# for sensitivity classification via :func:`_is_sensitive_log_key`), group(3)
# is the value tail. Name classification is deferred to the same normalizer
# used for structured log keys so every compound name handled there
# (``access-token``, ``refresh_token``, ``client-secret`` …) is also scrubbed
# here when it appears as an argv flag.
_ARGV_FLAG_WITH_VALUE = re.compile(
    r"^(--?([A-Za-z0-9][A-Za-z0-9_.-]*)[=:])(.*)$",
    re.DOTALL,
)
_ARGV_BARE_FLAG = re.compile(r"^--?([A-Za-z0-9][A-Za-z0-9_.-]*)$")

# Environment-variable assignment prefix (``PGPASSWORD=secret psql``).
# Argv elements that parse as ``NAME=VALUE`` and whose name is a
# sensitive env var must have their value scrubbed. Names use the POSIX
# env-var shape (letter/underscore followed by alnum/underscore).
_ARGV_ENV_ASSIGNMENT = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$",
    re.DOTALL,
)

# Single-letter short-option flags that commonly carry credentials:
# ``-p`` (password in mysql/psql/pg_dump/ssh-ish tools), ``-P`` (password
# in several MariaDB/MySQL dumpers), ``-w`` (password in pg_dump-style),
# ``-u``/``-U`` (userinfo pair in curl/psql/mysql — ``curl -u alice:secret``
# is the canonical credential-carrying form).
# These have legitimate non-secret uses in other tools (``-p`` is port
# in psql, ``-U`` is bare username in pg_dump, etc.), but defensive
# redaction is preferred to leaking — the command is only ever echoed
# into failure diagnostics and warning logs, not re-executed, so a
# redacted value still identifies the failure.
_SENSITIVE_SHORT_FLAGS = frozenset({"p", "P", "w", "W", "u", "U"})

# Long-option flag names whose value is treated as a credential by
# ``_redact_argv`` even though the name itself does not look like a
# password/token/secret suffix to :func:`_is_sensitive_log_field`.
# ``curl --user alice:secret`` / ``mysql --login-path=prod`` / etc. carry
# userinfo strings that almost always embed a password. Matched
# case-insensitively on the normalized name (``-``/``_`` stripped) so
# ``--user`` / ``--User`` / ``--login-path`` / ``--loginpath`` all
# classify uniformly. Same defensive-over-leak tradeoff as
# ``_SENSITIVE_SHORT_FLAGS``.
_ARGV_SENSITIVE_LONG_FLAGS = frozenset(
    {
        "user",
        "username",
        "login",
        "loginpath",
        "netrc",
        "netrcfile",
    }
)


def _is_argv_credential_flag(name: object) -> bool:
    """True when a long-option flag name should have its value redacted.

    Combines the structured-field credential matcher
    (:func:`_is_sensitive_log_field`) with a small argv-only set
    (``_ARGV_SENSITIVE_LONG_FLAGS``) for flags like ``--user`` whose name
    does not carry a ``password``/``token``/``secret`` suffix but whose
    value is conventionally a userinfo/credential pair.
    """
    if not isinstance(name, str):
        return False
    if _is_sensitive_log_field(name):
        return True
    normalized = name.replace("-", "").replace("_", "").lower()
    return normalized in _ARGV_SENSITIVE_LONG_FLAGS

SENSITIVE_PATTERNS = [
    re.compile(r"(ghp_[A-Za-z0-9_]{36,})", re.ASCII),
    re.compile(r"(gho_[A-Za-z0-9_]{36,})", re.ASCII),
    re.compile(r"(github_pat_[A-Za-z0-9_]{22,})", re.ASCII),
    re.compile(r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
    re.compile(r"(token\s*[=:]\s*\S+)", re.IGNORECASE),
    re.compile(r"(cookie\s*[=:]\s*\S+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[=:]\s*\S+)", re.IGNORECASE),
    # URI with inline credentials: ``scheme://user:password@host/…``. Setup
    # commands frequently carry DSNs (``postgres://u:p@db/app``) as argv and
    # those must not leak into the failure prompt or warning log.
    re.compile(r"[A-Za-z][A-Za-z0-9+\-.]*://[^\s:@/]+:[^\s@/]+@\S*"),
    # Common secret-style CLI flags: --password=X, --passwd=X, --pwd=X,
    # --secret=X, --api-key=X, --database-url=X, --db-url=X,
    # --connection-string=X (hyphen or underscore; ``=``, ``:`` or whitespace).
    # The value is consumed to end-of-line rather than ``\S+`` so connection
    # strings that embed whitespace (SQL Server style
    # ``--connection-string Server=db;User Id=foo;Password=super secret;``)
    # are scrubbed in full. Otherwise the tail after the first space would
    # reach failure diagnostics and orchestrator logs.
    re.compile(
        r"(--?(?:password|passwd|pwd|secret|api[-_]?key|database[-_]?url|"
        r"db[-_]?url|connection[-_]?string)[=:\s]+)[^\n]*",
        re.IGNORECASE,
    ),
]

logger = logging.getLogger(__name__)
_ACTIVE_AGENT_PROCESS_LOCK = threading.Lock()
_ACTIVE_AGENT_PROCESS: subprocess.Popen[str] | None = None
_ACTIVE_PHASE_LEASE_FAILURE_LOCK = threading.Lock()
_ACTIVE_PHASE_LEASE_FAILURE: "LeaseHeartbeatFailure | None" = None


@dataclass
class LeaseHeartbeatFailure:
    event: threading.Event = field(default_factory=threading.Event)
    message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            if not self.message:
                self.message = str(exc) or exc.__class__.__name__
            self.event.set()

    def failure_message(self) -> str:
        with self._lock:
            return self.message


def _set_active_phase_lease_failure(signal: LeaseHeartbeatFailure | None) -> None:
    global _ACTIVE_PHASE_LEASE_FAILURE
    with _ACTIVE_PHASE_LEASE_FAILURE_LOCK:
        _ACTIVE_PHASE_LEASE_FAILURE = signal


def _active_phase_lease_failure_message() -> str:
    with _ACTIVE_PHASE_LEASE_FAILURE_LOCK:
        signal = _ACTIVE_PHASE_LEASE_FAILURE
    if signal is None or not signal.event.is_set():
        return ""
    return signal.failure_message() or "Coordinator lease heartbeat failed closed"


def _raise_if_active_phase_lease_lost() -> None:
    message = _active_phase_lease_failure_message()
    if message:
        raise RuntimeError(message)


def _emit_user_progress(message: str) -> None:
    """Print short user-visible progress lines for long-running workflow steps."""
    print(message, file=sys.stderr, flush=True)


def format_attempt_progress(consumed_retries: int, retry_cap: int) -> str:
    """Render user-facing attempt progress from internal retry counters."""
    current_attempt = max(1, int(consumed_retries or 0) + 1)
    total_attempts = max(current_attempt, int(retry_cap or RETRY_CAP or 1) + 1)
    return f"{current_attempt}/{total_attempts}"


def _phase_attempt_label(run: RunState) -> str:
    return format_attempt_progress(_convergence_attempts(run), run.retry_cap)


def _input_requires_full_session(question: str) -> bool:
    normalized = question.lower()
    return any(hint in normalized for hint in OPERATOR_REQUEST_FULL_SESSION_HINTS)


def _emit_phase_progress(run: RunState, phase: str, status: str) -> None:
    message = f"[spec] {run.spec_id}: phase {phase} {status} (attempt {_phase_attempt_label(run)})"
    if status in {"failed", "blocked"} and run.last_error:
        detail = run.last_error.strip().splitlines()[0]
        if detail:
            message = f"{message}: {detail}"
    _emit_user_progress(message)


def _normalize_logger_state() -> None:
    """Keep the named orchestrator logger observable across long test sessions."""
    logger.disabled = False
    logger.propagate = True


def _forge():
    """Return the forge adapter, wired to use the orchestrator's ``run_subprocess``.

    A fresh ``GitHubForge`` is created each call so that test mocks applied to
    ``run_subprocess`` are picked up automatically.
    """
    return GitHubForge(run_fn=run_subprocess)


def _check_forge_auth() -> str:
    """Verify forge authentication. Returns empty string on success, error message on failure."""
    gh_check = run_subprocess(["gh", "auth", "status"])
    if gh_check.returncode != 0:
        return "GitHub CLI not authenticated. Run 'gh auth login'."
    return ""


def _mcp_config_path(worktree_path: Path) -> Path:
    """Return the standard MCP config path for a worktree."""
    return worktree_path / ".claude" / "mcp-servers.json"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str
    command: str


class OrchestratorTerminationRequested(RuntimeError):
    """Raised when the orchestrator receives SIGTERM while a workflow is active."""


class BlockDebuggerAutoResumeExhausted(RuntimeError):
    """Raised when autopilot tries to spend an already-used debugger resume grant."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_repo_root() -> Path:
    """Return the repository common root (main checkout for linked worktrees)."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return resolve_common_root(Path(result.stdout.strip()))


def _specs_root(repo_root: Path) -> Path:
    return repo_root / SPEC_RUNTIME_CONFIG.paths.specs_dir


def _task_specs_root(repo_root: Path) -> Path:
    return repo_root / SPEC_RUNTIME_CONFIG.paths.task_specs_dir


def _catalog_spec_relpath(spec_id: str) -> str:
    return str(PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / f"{spec_id}.md")


def _task_spec_relpath(spec_id: str) -> str:
    return str(PurePosixPath(SPEC_RUNTIME_CONFIG.paths.task_specs_dir) / f"{spec_id}.md")


def _configured_spec_roots() -> tuple[PurePosixPath, ...]:
    roots = {
        PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir),
        PurePosixPath(SPEC_RUNTIME_CONFIG.paths.task_specs_dir),
    }
    return tuple(sorted(roots, key=lambda root: root.as_posix()))


def _is_relative_to(parent: PurePosixPath, child: PurePosixPath) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_task_spec_path(spec_path: Path, repo_root: Path) -> bool:
    task_specs_root = _task_specs_root(repo_root)
    try:
        spec_path.relative_to(task_specs_root)
    except ValueError:
        return False
    return True


def _worktrees_root(repo_root: Path | None = None) -> Path:
    return resolve_common_root(repo_root) / SPEC_RUNTIME_CONFIG.paths.worktrees_dir


def _common_state_root(repo_root: Path | None = None) -> Path:
    return resolve_common_root(repo_root) / SPEC_RUNTIME_CONFIG.paths.state_dir


def resolve_worktree_path(spec_or_run: object, repo_root: Path | None = None) -> Path:
    """Return the canonical worktree path for a spec or run."""
    worktrees_root = _worktrees_root(repo_root)
    if hasattr(spec_or_run, "branch"):
        worktree_path = str(getattr(spec_or_run, "worktree_path", "") or "").strip()
        if worktree_path:
            return Path(worktree_path)

        branch = str(getattr(spec_or_run, "branch", "") or "").strip()
        spec_id = str(getattr(spec_or_run, "spec_id", "") or "").strip()
        identity = implementation_branch_identity(branch)
        if identity and identity.run_token:
            if identity.kind == "specrun":
                worktree_name = f"specrun-{identity.spec_id}--{identity.run_token}"
            elif identity.kind == "task":
                worktree_name = f"task-{identity.spec_id}--{identity.run_token}"
            else:
                worktree_name = spec_run_worktree_name(identity.spec_id, identity.run_token)
            return worktrees_root / worktree_name
        return worktrees_root / spec_id

    return worktrees_root / str(spec_or_run)


# ---------------------------------------------------------------------------
# Execution backend seam
# ---------------------------------------------------------------------------

_EXECUTION_BACKEND_OVERRIDE: ExecutionBackend | None = None


def set_execution_backend(backend: ExecutionBackend | None) -> None:
    """Inject an execution backend for tests.

    Pass ``None`` to restore the configured factory-resolved backend.
    """
    global _EXECUTION_BACKEND_OVERRIDE
    _EXECUTION_BACKEND_OVERRIDE = backend


def _resolve_execution_backend() -> ExecutionBackend:
    """Return the active execution backend (override > config)."""
    if _EXECUTION_BACKEND_OVERRIDE is not None:
        return _EXECUTION_BACKEND_OVERRIDE
    return _factory_get_execution_backend(SPEC_RUNTIME_CONFIG)


def _resolve_workspace_handle(
    run: "RunState",
    repo_root: Path,
) -> WorkspaceHandle:
    """Resolve the workspace handle for *run* via the execution backend."""
    backend = _resolve_execution_backend()
    worktree_path = resolve_worktree_path(run, repo_root)
    return backend.prepare_workspace(
        run_id=run.run_id,
        spec_id=run.spec_id,
        branch=run.branch,
        repo_root=repo_root,
        worktree_path=worktree_path,
        base_ref=run.base_ref or BASE_REF,
    )


def _resolve_existing_workspace_path(run: "RunState", repo_root: Path) -> Path:
    """Return an already-materialized local workspace without rebuilding it.

    Clone and container backends keep their source checkout under the configured
    workspace root instead of at ``run.worktree_path``. Interactive operator
    input happens between attempts, when that backend checkout must be reused
    without starting a fresh worker merely to locate its files.
    """
    worktree_path = resolve_worktree_path(run, repo_root)
    current_identity = _resolve_execution_backend().identity
    backend_name = (run.backend or current_identity.backend).strip()
    if backend_name not in {"clone", "container"}:
        return worktree_path

    recorded_workspace_root = str(run.backend_workspace_root or "").strip()
    workspace_root = Path(
        recorded_workspace_root
        or SPEC_RUNTIME_CONFIG.execution.workspace_root
        or current_identity.workspace_root
    ).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = repo_root / workspace_root
    workspace_root = workspace_root.resolve()
    backend_source = (workspace_root / run.run_id / "source").resolve()
    try:
        backend_source.relative_to(workspace_root)
    except ValueError:
        return worktree_path
    if backend_source.is_dir():
        return backend_source
    return worktree_path


def _resolve_publish_workspace_handle(
    run: "RunState",
    repo_root: Path,
) -> WorkspaceHandle:
    """Reuse an already-prepared clone/container checkout for host publishing.

    Publish only reads the checkout and outbox before performing host-owned Git
    and forge operations. Calling ``prepare_workspace`` here would rebuild and
    bootstrap a fresh container after verify even though no worker command runs
    during this phase.

    Custom backends retain the normal preparation seam because their workspace
    and outbox layouts are not part of the built-in clone/container contract.
    """
    backend = _resolve_execution_backend()
    backend_name = (run.backend or backend.identity.backend).strip()
    if backend_name not in {"clone", "container"}:
        return _resolve_workspace_handle(run, repo_root)

    workspace_path = _resolve_existing_workspace_path(run, repo_root)
    return WorkspaceHandle(
        path=workspace_path,
        outbox_path=workspace_path.parent / "outbox",
        branch=run.branch,
        backend=backend_name,
        metadata={"run_id": run.run_id, "spec_id": run.spec_id},
    )


def collect_workspace_outbox_metadata(workspace: WorkspaceHandle):
    """Collect optional PR/MR metadata from the backend outbox."""
    return _resolve_execution_backend().collect_outbox_metadata(workspace)


def _parse_repo_from_remote_url(url: str) -> str | None:
    cleaned = url.strip()
    if not cleaned:
        return None
    if cleaned.startswith("git@github.com:"):
        tail = cleaned.removeprefix("git@github.com:")
        return tail[:-4] if tail.endswith(".git") else tail
    if cleaned.startswith("https://github.com/"):
        tail = cleaned.removeprefix("https://github.com/")
        return tail[:-4] if tail.endswith(".git") else tail
    return None


def _repo_name_with_owner(repo_root: Path) -> str:
    repo_name = _forge().get_repo_slug(cwd=repo_root)
    if repo_name:
        return repo_name

    remote = run_subprocess(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=repo_root,
    )
    parsed = _parse_repo_from_remote_url(remote.stdout.strip())
    if remote.returncode == 0 and parsed:
        return parsed

    detail = remote.stderr.strip() or remote.stdout.strip() or "unknown error"
    raise ValueError(
        "Could not determine repository nameWithOwner via `gh repo view` or "
        f"`git config --get remote.origin.url`: {detail}"
    )


def _worktree_is_registered(repo_root: Path, worktree_path: Path) -> tuple[bool, str]:
    """Return whether *worktree_path* is still registered with git."""
    result = run_subprocess(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        detail = redact_sensitive(tail_lines(result.stderr or result.stdout))
        return False, f"git worktree list failed: {detail[-240:]}"
    resolved = str(worktree_path.resolve())
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered_path = line[len("worktree ") :]
        if registered_path == str(worktree_path) or registered_path == resolved:
            return True, ""
    return False, ""


def _worktree_branch_alignment_error(
    worktree_path: Path,
    expected_branch: str,
) -> str:
    """Return an actionable error when a worktree is not on *expected_branch*."""
    branch_result = run_subprocess(["git", "branch", "--show-current"], cwd=worktree_path)
    if branch_result.returncode != 0:
        detail = redact_sensitive(tail_lines(branch_result.stderr or branch_result.stdout))
        return f"Could not determine checked-out branch for {worktree_path}: {detail[-240:]}"

    actual_branch = branch_result.stdout.strip()
    if actual_branch == expected_branch:
        return ""

    if not actual_branch:
        return (
            f"Worktree {worktree_path} is in detached HEAD state, expected "
            f"branch '{expected_branch}'. Check out the expected branch or recreate "
            "the worktree and retry."
        )

    return (
        f"Worktree {worktree_path} is on branch '{actual_branch}', expected "
        f"'{expected_branch}'. Rename the branch (for example "
        f"`git -C {worktree_path} branch -m {expected_branch}`) or recreate the "
        "worktree and retry."
    )


def _ensure_local_branch_available(repo_root: Path, branch: str) -> str:
    """Ensure *branch* exists locally, fetching it from origin if needed."""
    branch_check = run_subprocess(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
    )
    if branch_check.returncode == 0:
        return ""

    try:
        fetch_outcome = run_git_fetch_with_timeout(
            ["origin", f"{branch}:refs/heads/{branch}"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
            runner=_orchestrator_fetch_runner,
        )
    except GitFetchTimeoutError as exc:
        return (
            f"Branch '{branch}' is not available locally and could not be fetched from origin: "
            f"git fetch timed out after {exc.timeout_seconds:.0f}s"
        )
    if fetch_outcome.is_success:
        return ""

    detail = fetch_outcome.stderr.strip() or fetch_outcome.stdout.strip() or "unknown error"
    return f"Branch '{branch}' is not available locally and could not be fetched from origin: {detail}"


def _resolve_pg_ctl() -> str | None:
    """Return a trusted pg_ctl binary path when available."""
    pg_bin = os.environ.get("SIM_LOCAL_PG_BIN", "").rstrip("/")
    if pg_bin:
        candidate = Path(pg_bin) / "pg_ctl"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    homebrew_pg = Path("/opt/homebrew/opt/postgresql@16/bin/pg_ctl")
    if homebrew_pg.is_file() and os.access(homebrew_pg, os.X_OK):
        return str(homebrew_pg)

    return shutil.which("pg_ctl")


def _stop_worktree_postgres_if_present(worktree_path: Path) -> None:
    """Best-effort stop for a worktree-local Postgres cluster before removal."""
    data_dir = worktree_path / ".local" / "postgres" / "data"
    if not (data_dir / "postmaster.pid").is_file():
        return

    pg_ctl = _resolve_pg_ctl()
    if not pg_ctl:
        logger.warning(
            "Cleanup could not stop local Postgres for %s because pg_ctl was not found",
            worktree_path,
        )
        return

    status_result = run_subprocess([pg_ctl, "-D", str(data_dir), "status"])
    if status_result.returncode != 0:
        return

    stop_result = run_subprocess([pg_ctl, "-D", str(data_dir), "stop", "-m", "fast"])
    if stop_result.returncode != 0:
        detail = redact_sensitive(tail_lines(stop_result.stderr or stop_result.stdout))
        logger.warning(
            "Cleanup could not stop local Postgres for %s (non-blocking): %s",
            worktree_path,
            detail[-240:],
        )


def _cleanup_worktree_checkout(
    repo_root: Path,
    worktree_path: Path,
    *,
    branch: str | None = None,
    delete_branch: bool,
) -> str:
    """Remove a spec worktree and optionally its branch, failing on leftovers."""
    _reap_registered_worktree_processes(
        repo_root,
        worktree_path,
        reason="worktree cleanup",
    )
    _stop_worktree_postgres_if_present(worktree_path)

    registered, error = _worktree_is_registered(repo_root, worktree_path)
    if error:
        return error

    if registered:
        rm_result = run_subprocess(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=repo_root,
        )
        if rm_result.returncode != 0:
            detail = redact_sensitive(tail_lines(rm_result.stderr or rm_result.stdout))
            logger.warning("worktree remove reported failure: %s", detail[-240:])
    elif worktree_path.is_dir():
        try:
            remove_tree(worktree_path)
        except OSError as exc:
            return f"Could not remove worktree directory {worktree_path}: {exc}"

    prune_result = run_subprocess(["git", "worktree", "prune"], cwd=repo_root)
    if prune_result.returncode != 0:
        detail = redact_sensitive(tail_lines(prune_result.stderr or prune_result.stdout))
        return f"git worktree prune failed: {detail[-240:]}"

    registered, error = _worktree_is_registered(repo_root, worktree_path)
    if error:
        return error
    if registered:
        return f"Cleanup left stale worktree metadata for {worktree_path}. Run 'git worktree prune' and retry."
    if worktree_path.exists():
        return f"Cleanup left worktree directory in place: {worktree_path}"

    if not delete_branch or not branch:
        return ""

    branch_check = run_subprocess(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
    )
    if branch_check.returncode == 0:
        branch_delete = run_subprocess(["git", "branch", "-D", branch], cwd=repo_root)
        if branch_delete.returncode != 0:
            detail = redact_sensitive(tail_lines(branch_delete.stderr or branch_delete.stdout))
            return f"git branch -D {branch} failed: {detail[-240:]}"
        branch_check = run_subprocess(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_root,
        )
    if branch_check.returncode == 0:
        return f"Cleanup left local branch in place: {branch}"
    if branch_check.returncode not in (0, 1):
        detail = redact_sensitive(tail_lines(branch_check.stderr or branch_check.stdout))
        return f"git show-ref failed while checking {branch}: {detail[-240:]}"
    return ""


def tail_lines(text: str, n: int = 50) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _tail_chars(text: str, max_chars: int = 1200) -> str:
    """Return the trailing portion of *text* capped at *max_chars*."""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _format_subprocess_failure(result: subprocess.CompletedProcess) -> str:
    """Format stdout/stderr for actionable subprocess failure messages."""
    sections = []
    for stream_name, stream_text in (
        ("stdout", result.stdout or ""),
        ("stderr", result.stderr or ""),
    ):
        cleaned = redact_sensitive(_tail_chars(tail_lines(stream_text)).strip())
        if cleaned:
            sections.append(f"--- {stream_name} ---\n{cleaned}")
    if sections:
        return "\n".join(sections)
    return f"exit code {result.returncode}"


_PROGRESS_BAR_CHARS = "■□▪▫█▉▊▋▌▍▎▏=#>─░▒▓"
_PROGRESS_FRAME_RE = re.compile(
    r"^[\s|\[\(]*[" + re.escape(_PROGRESS_BAR_CHARS) + r"]*[\s|\]\)]*"
    r"\d{1,3}(\.\d+)?\s*%.*$"
)


def _is_progress_frame(line: str) -> bool:
    """True for a line shaped like one frame of an in-place progress bar."""
    stripped = line.strip()
    return bool(stripped) and bool(_PROGRESS_FRAME_RE.match(stripped))


def _drop_progress_noise(text: str) -> str:
    """Collapse each run of redrawn progress frames down to its final frame.

    Tools that render progress in place separate frames with carriage returns,
    and ``str.splitlines`` treats ``\\r`` as a line break — so a single download
    becomes hundreds of "lines" that crowd the real error out of the
    fixed-size tail an agent is shown on retry. A Playwright browser download,
    for example, can dominate a failing e2e gate's stored output with
    ``|■■■■...| N% of 110.9 MiB``.

    Only *consecutive* frames are collapsed, so an isolated line that happens to
    carry a percentage (``Coverage: 87% of statements``) is a run of one and is
    always kept.
    """
    lines = text.splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        if _is_progress_frame(line) and index + 1 < len(lines) and _is_progress_frame(lines[index + 1]):
            continue  # a later frame in the same run supersedes this one
        kept.append(line)
    return "\n".join(kept)


def _sanitize_gate_stream(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
) -> str:
    """Trim, tail, and redact gate output before storing it."""
    if not text:
        return ""
    cleaned = _drop_progress_noise(text)
    return redact_sensitive(_tail_chars(tail_lines(cleaned, n=max_lines), max_chars).strip())


def _first_nonempty_pytest_summary_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if _parse_pytest_summary_counts(stripped) is not None:
            return stripped
    return ""


def _pytest_failure_header_pattern() -> re.Pattern[str]:
    return re.compile(r"^_{3,}\s+.+\s+_{3,}$")


def _extract_first_pytest_traceback_header(text: str) -> str:
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if re.match(r"^(tests/\S+|[\w./-]+\.py):\d+:", stripped):
            return stripped
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("E   "):
            return stripped
    failure_header = _pytest_failure_header_pattern()
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if failure_header.match(stripped):
            return re.sub(r"\s+", " ", stripped)
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if re.match(r"^FAILED\s+(tests/\S+)", stripped):
            return stripped
    return ""


def _extract_failed_test_node_ids(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(1)
            for line in text.splitlines()
            if (
                match := re.match(
                    r"^(?:FAILED|ERROR)\s+(tests/\S+)",
                    line.strip(),
                )
            )
        )
    )


def _first_failed_test_nodeid(*texts: str) -> str:
    for text in texts:
        failed_tests = _extract_failed_test_node_ids(text)
        if failed_tests:
            return failed_tests[0]
    return ""


def _diagnosis_first_failed_test_nodeid(repo_root: Path, run: RunState) -> str:
    """Extract the first failed test nodeid from gate status for a diagnosis."""
    _, gate_data = _read_gate_status(repo_root, run)
    if not isinstance(gate_data, dict):
        return ""
    gate_entry = gate_data.get("gates", {}).get("test")
    if not isinstance(gate_entry, dict):
        return ""
    nodeid = str(gate_entry.get("first_failed_test_nodeid", "") or "").strip()
    if nodeid:
        return nodeid
    return _first_failed_test_nodeid(
        str(gate_entry.get("last_diagnostic", "") or ""),
        str(gate_entry.get("last_stdout", "") or ""),
    )


def _latest_first_failed_test_nodeid_from_gate(
    repo_root: Path,
    run: RunState,
    gate_name: str,
) -> str:
    """Extract the first failed test nodeid from the latest gate failure."""
    _, gate_data = _read_gate_status(repo_root, run)
    if not isinstance(gate_data, dict):
        return ""
    gate_entry = gate_data.get("gates", {}).get(gate_name)
    if not isinstance(gate_entry, dict):
        return ""
    nodeid = str(gate_entry.get("first_failed_test_nodeid", "") or "").strip()
    if nodeid:
        return nodeid
    return _first_failed_test_nodeid(
        str(gate_entry.get("last_diagnostic", "") or ""),
        str(gate_entry.get("last_stdout", "") or ""),
    )


def _extract_pytest_summary_problem_lines(text: str) -> list[str]:
    summary_lines: list[str] = []
    in_summary = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if "short test summary info" in stripped.lower():
            in_summary = True
            continue
        if not in_summary:
            continue
        if _parse_pytest_summary_counts(stripped) is not None:
            break
        if re.match(r"^(?:FAILED|ERROR)\s+(tests/\S+)", stripped):
            summary_lines.append(stripped)
    return summary_lines


def _extract_test_failure_shape(text: str, *, first_failed_nodeid: str = "") -> str:
    if not text:
        return ""

    summary_problem_lines = _extract_pytest_summary_problem_lines(text)
    if summary_problem_lines:
        for line in reversed(summary_problem_lines):
            if first_failed_nodeid and line.startswith(f"FAILED {first_failed_nodeid}"):
                continue
            return redact_sensitive(line)
        return redact_sensitive(summary_problem_lines[-1])

    skip_prefixes = tuple(
        prefix for prefix in (f"FAILED {first_failed_nodeid}", f"ERROR {first_failed_nodeid}") if first_failed_nodeid
    )
    for raw_line in reversed(text.splitlines()):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _parse_pytest_summary_counts(stripped) is not None:
            continue
        if stripped.startswith(skip_prefixes):
            continue
        if _is_generic_failure_wrapper_line(stripped):
            continue
        if _looks_like_failure_detail(stripped):
            return redact_sensitive(stripped)
    return ""


def _stored_test_failure_shape(stdout: str, diagnostic: str) -> str:
    first_failed_nodeid = _first_failed_test_nodeid(stdout, diagnostic)
    failure_shape = _extract_test_failure_shape(diagnostic, first_failed_nodeid=first_failed_nodeid)
    if not failure_shape:
        failure_shape = _extract_test_failure_shape(stdout, first_failed_nodeid=first_failed_nodeid)
    return failure_shape


def _extract_first_pytest_failure_block(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines()]
    if not lines:
        return []

    failure_banner_idx = next(
        (idx for idx, line in enumerate(lines) if re.match(r"^=+\s+(?:FAILURES|ERRORS)\s+=+$", line.strip())),
        None,
    )
    if failure_banner_idx is None:
        return []

    summary_idx = next(
        (idx for idx, line in enumerate(lines) if "short test summary info" in line.lower()),
        len(lines),
    )
    header_pattern = _pytest_failure_header_pattern()
    start_idx = None
    for idx in range(failure_banner_idx + 1, summary_idx):
        stripped = lines[idx].strip()
        if (
            header_pattern.match(stripped)
            or re.match(r"^(tests/\S+|[\w./-]+\.py):\d+:", stripped)
            or stripped.startswith("E   ")
        ):
            start_idx = idx
            break
    if start_idx is None:
        return []

    end_idx = summary_idx
    for idx in range(start_idx + 1, summary_idx):
        stripped = lines[idx].strip()
        if header_pattern.match(stripped):
            end_idx = idx
            break

    block = lines[start_idx:end_idx]
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
    return block


def _stored_test_diagnostic(text: str) -> str:
    if not text:
        return ""

    stripped = text.strip()
    if stripped.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX):
        return redact_sensitive(stripped[:GATE_DIAGNOSTIC_MAX_CHARS].strip())

    summary_line = _first_nonempty_pytest_summary_line(text)
    failed_tests = _extract_failed_test_node_ids(text)
    first_failed_node = failed_tests[0] if failed_tests else ""
    failure_block = _extract_first_pytest_failure_block(text)
    if not failure_block:
        fallback_lines = []
        traceback_header = _extract_first_pytest_traceback_header(text)
        if traceback_header:
            fallback_lines.append(traceback_header)
        if first_failed_node:
            fallback_lines.append(f"FAILED {first_failed_node}")
        if summary_line:
            fallback_lines.append(summary_line)
        return redact_sensitive("\n".join(dict.fromkeys(line for line in fallback_lines if line)).strip())

    selected: list[str] = []
    selected.extend(failure_block[:GATE_DIAGNOSTIC_MAX_LINES])
    truncated_block = len(failure_block) > GATE_DIAGNOSTIC_MAX_LINES
    if truncated_block:
        selected.append("[diagnostic trimmed after first failure excerpt]")
    if first_failed_node:
        failed_summary_line = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith((f"FAILED {first_failed_node}", f"ERROR {first_failed_node}"))
            ),
            next(
                (
                    f"ERROR {first_failed_node}"
                    for line in text.splitlines()
                    if line.strip().startswith(f"ERROR {first_failed_node}")
                ),
                f"FAILED {first_failed_node}",
            ),
        )
        if failed_summary_line not in selected:
            selected.extend(["", failed_summary_line])
    if summary_line and summary_line not in selected:
        selected.append(summary_line)
    if len(failed_tests) > 1:
        label = "test" if len(failed_tests) - 1 == 1 else "tests"
        selected.append(f"[{len(failed_tests) - 1} additional failing {label} omitted from diagnostic excerpt]")

    rendered = "\n".join(selected).strip()
    if len(rendered) > GATE_DIAGNOSTIC_MAX_CHARS:
        rendered = (
            rendered[: GATE_DIAGNOSTIC_MAX_CHARS - len("\n[diagnostic trimmed]")].rstrip() + "\n[diagnostic trimmed]"
        )
    return redact_sensitive(rendered)


def _first_targeted_test_diagnostic(
    entries: list[dict[str, str]] | None,
    *,
    first_failed_nodeid: str = "",
) -> dict[str, str]:
    if not entries:
        return {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nodeid = str(entry.get("nodeid") or "").strip()
        if first_failed_nodeid and nodeid != first_failed_nodeid:
            continue
        detail = str(entry.get("detail") or "").strip()
        status = str(entry.get("status") or "").strip()
        if nodeid and detail and status:
            return {
                "nodeid": redact_sensitive(nodeid),
                "status": status,
                "detail": redact_sensitive(detail) if detail.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX) else detail,
            }

    return {}


def _stored_targeted_test_diagnostics(
    entries: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    stored: list[dict[str, str]] = []
    if not entries:
        return stored

    for entry in entries[:TARGETED_TEST_DIAGNOSTIC_MAX_FAILURES]:
        if not isinstance(entry, dict):
            continue
        nodeid = str(entry.get("nodeid") or "").strip()
        status = str(entry.get("status") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        if not nodeid or not status or not detail:
            continue

        if detail.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX):
            stored_detail = redact_sensitive(detail[:GATE_DIAGNOSTIC_MAX_CHARS].strip())
        elif status == "failed":
            stored_detail = _stored_test_diagnostic(detail)
            if not stored_detail:
                stored_detail = redact_sensitive(detail[:GATE_DIAGNOSTIC_MAX_CHARS].strip())
        else:
            stored_detail = redact_sensitive(detail[:GATE_DIAGNOSTIC_MAX_CHARS].strip())

        if not stored_detail:
            continue
        stored.append(
            {
                "nodeid": redact_sensitive(nodeid),
                "status": status,
                "detail": stored_detail,
            }
        )
    return stored


def _stored_gate_stdout(text: str) -> str:
    return _sanitize_gate_stream(
        text,
        max_lines=GATE_STDOUT_MAX_LINES,
        max_chars=GATE_STDOUT_MAX_CHARS,
    )


def _stored_gate_stderr(text: str) -> str:
    return _sanitize_gate_stream(
        text,
        max_lines=GATE_STDERR_MAX_LINES,
        max_chars=GATE_STDERR_MAX_CHARS,
    )


def _coerce_optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerce_nonfatal_warning(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}

    warning = {
        "phase": str(payload.get("phase") or "").strip(),
        "failure_type": str(payload.get("failure_type") or "").strip(),
        "failure_subtype": str(payload.get("failure_subtype") or "").strip(),
        "retryable": bool(payload.get("retryable", False)),
        "nonfatal": True,
        "gate_name": str(payload.get("gate_name") or "").strip(),
        "summary": str(payload.get("summary") or "").strip(),
        "action": str(payload.get("action") or "").strip(),
        "detail": str(payload.get("detail") or "").strip(),
        "recorded_at": str(payload.get("recorded_at") or "").strip(),
    }
    if not warning["recorded_at"]:
        warning["recorded_at"] = _now_iso()
    if not warning["summary"]:
        return {}
    return warning


def _coerce_nonfatal_warnings(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        return []

    warnings: list[dict[str, object]] = []
    for item in payload:
        warning = _coerce_nonfatal_warning(item)
        if warning:
            warnings.append(warning)
    return warnings


def _attempt_artifact_filename(filename: str, attempt_number: int) -> str:
    path = Path(filename)
    suffix = "".join(path.suffixes) or ".json"
    stem = path.name[: -len(suffix)] if suffix else path.name
    return f"{stem}.attempt-{attempt_number}{suffix}"


def _launch_artifact_filename(filename: str, launch_number: int) -> str:
    path = Path(filename)
    suffix = "".join(path.suffixes) or ".json"
    stem = path.name[: -len(suffix)] if suffix else path.name
    return f"{stem}.launch-{launch_number}{suffix}"


def _attempt_artifact_path(run_dir: Path, filename: str, attempt_number: int) -> Path:
    return run_dir / _attempt_artifact_filename(filename, attempt_number)


def _zero_based_attempt_to_human(attempt: int | None) -> int | None:
    if attempt is None:
        return None
    return max(1, attempt + 1)


def _write_latest_and_attempt_artifacts(
    run_dir: Path,
    filename: str,
    payload: dict[str, object],
    *,
    attempt_number: int | None = None,
    launch_number: int | None = None,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_path = run_dir / filename
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    latest_path.write_text(rendered)
    if attempt_number is not None and attempt_number > 0:
        _attempt_artifact_path(run_dir, filename, attempt_number).write_text(rendered)
    if launch_number is not None and launch_number > 0:
        launch_path = run_dir / _launch_artifact_filename(filename, launch_number)
        # Launch identifiers are monotonic and immutable. A later annotation of
        # the latest/attempt alias must not erase the evidence captured when the
        # agent launch was first recorded.
        if not launch_path.exists():
            launch_path.write_text(rendered)
    return latest_path


def _load_attempt_or_latest_json_payload(
    run_dir: Path,
    filename: str,
    *,
    attempt_number: int | None = None,
) -> tuple[object | None, Path]:
    if attempt_number is not None and attempt_number > 0:
        attempt_path = _attempt_artifact_path(run_dir, filename, attempt_number)
        return _read_optional_json_payload(attempt_path), attempt_path
    latest_path = run_dir / filename
    return _read_optional_json_payload(latest_path), latest_path


def _load_attempt_or_latest_text_path(
    run_dir: Path,
    filename: str,
    *,
    attempt_number: int | None = None,
) -> Path:
    if attempt_number is not None and attempt_number > 0:
        return _attempt_artifact_path(run_dir, filename, attempt_number)
    return run_dir / filename


@dataclass(frozen=True)
class ImplementManagedProcess:
    name: str
    kind: str
    pid: int
    started_at: str
    command: str = ""
    termination_scope: str = "pid"
    pgid: int = 0


@dataclass(frozen=True)
class ImplementSetupFailure:
    command: str
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    message: str
    launch_error: bool


@dataclass(frozen=True)
class ImplementSetupManifest:
    env: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    mcp_prompt: str = ""
    mcp_servers: dict[str, dict[str, object]] = field(default_factory=dict)
    managed_processes: tuple[ImplementManagedProcess, ...] = ()
    failure: ImplementSetupFailure | None = None


def _coerce_implement_setup_mcp_servers(
    payload: object,
) -> dict[str, dict[str, object]]:
    if not isinstance(payload, dict):
        return {}

    servers: dict[str, dict[str, object]] = {}
    for raw_name, raw_server in payload.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_server, dict):
            continue
        try:
            servers[name] = json.loads(json.dumps(raw_server))
        except (TypeError, ValueError):
            continue
    return servers


def _coerce_implement_setup_managed_processes(
    payload: object,
) -> tuple[ImplementManagedProcess, ...]:
    if not isinstance(payload, list):
        return ()

    processes: list[ImplementManagedProcess] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid", 0))
            pgid = int(item.get("pgid", 0))
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        started_at = str(item.get("started_at", "")).strip()
        if not started_at:
            continue
        name = str(item.get("name", "")).strip() or "process"
        kind = str(item.get("kind", "")).strip() or name
        termination_scope = str(item.get("termination_scope", "pid")).strip() or "pid"
        if termination_scope not in {"pid", "pgid"}:
            termination_scope = "pid"
        if termination_scope == "pgid" and pgid <= 0:
            pgid = pid
        processes.append(
            ImplementManagedProcess(
                name=name,
                kind=kind,
                pid=pid,
                started_at=started_at,
                command=str(item.get("command", "")).strip(),
                termination_scope=termination_scope,
                pgid=pgid if pgid > 0 else 0,
            )
        )
    return tuple(processes)


def _coerce_implement_setup_manifest(payload: object) -> ImplementSetupManifest:
    if not isinstance(payload, dict):
        return ImplementSetupManifest()

    env_payload = payload.get("env", {})
    env = {}
    if isinstance(env_payload, dict):
        env = {str(key).strip(): str(value) for key, value in env_payload.items() if str(key).strip()}

    return ImplementSetupManifest(
        env=env,
        prompt=str(payload.get("prompt", "")).strip(),
        mcp_prompt=str(payload.get("mcp_prompt", "")).strip(),
        mcp_servers=_coerce_implement_setup_mcp_servers(payload.get("mcp_servers", {})),
        managed_processes=_coerce_implement_setup_managed_processes(
            payload.get("managed_processes", []),
        ),
    )


# Manifest keys mapped to the Python type their values must have in a real
# manifest. Matching on key+value-type rather than on key alone lets us
# accept manifests that carry unrelated metadata (``timestamp``, ``version``,
# …) while still rejecting structured log records that happen to reuse a
# manifest key name as a scalar (``{"msg":"env ready"}``).
_MANIFEST_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "env": dict,
    "prompt": str,
    "mcp_prompt": str,
    "mcp_servers": dict,
    "managed_processes": list,
}

# Keys that, when present together, strongly indicate a structured log
# record rather than a setup manifest. ``level``+``msg`` is the canonical
# signature emitted by stdlib logging JSON formatters, structlog, etc.
# Real manifests never carry both simultaneously.
_LOG_LEVEL_KEYS = frozenset({"level", "severity", "levelname"})
_LOG_MESSAGE_KEYS = frozenset({"msg", "message"})
# Benign (non-diagnostic) log-level values. A log-shaped payload that
# carries an explicit benign level is treated as a logger wrapper around
# a real manifest emission (setup helpers sometimes emit their manifest
# through a structured logger that stamps the record with level/msg), so
# its env/prompt/MCP fields still contribute. Any other level value
# (warn/error/fatal/...) marks the payload as diagnostic context — env
# carried there is context attached to a failure, not a manifest.
_BENIGN_LOG_LEVELS = frozenset(
    {"info", "information", "debug", "notice", "trace", "verbose"}
)
# Numeric level values that JSON loggers like Bunyan/Pino emit for benign
# levels (10=TRACE, 20=DEBUG, 30=INFO). Anything >=40 is WARN/ERROR/FATAL
# in those conventions and is treated as diagnostic, not a manifest
# emission. Bunyan/Pino are the dominant JSON loggers that stamp numeric
# levels; stdlib Python JSON formatters almost always use ``levelname``
# strings, so limiting the benign set to {10,20,30} is safe.
_BENIGN_NUMERIC_LOG_LEVELS = frozenset({10, 20, 30})
# Structured-event / record markers. When a payload carries one of these at
# the top level and does not also carry a strong manifest key, it is almost
# certainly a log/event record that happens to include an ``env`` context
# dict — not a manifest. Without this, a trailing
# ``{"event":"bootstrap_failed","env":{"STAGE":"test"}}`` log line would be
# indistinguishable from a real env-only manifest and could overwrite one
# emitted earlier in stdout.
_LOG_EVENT_KEYS = frozenset({"event", "event_name", "event_type"})

# Manifest-only keys — if any of these is present with the right type the
# payload is almost certainly a manifest, even if it also has a top-level
# ``env`` dict. ``env`` alone is ambiguous (structured logs often carry it
# as context), so it is intentionally excluded from this strong set.
_MANIFEST_STRONG_KEYS = ("prompt", "mcp_prompt", "mcp_servers", "managed_processes")

# Top-level keys that strongly mark a payload as a diagnostic/error
# record even without ``level``/``msg``/``event``/``error`` markers.
# Frameworks like FastAPI/Django/etc. emit plain JSON error objects keyed
# by ``detail``/``reason``/``description``/``cause``/``failure``/stack
# trace fields. A weak (no strong-manifest-key) payload that carries any
# of these alongside ``env`` is diagnostic context, not a manifest, and
# must not be accepted as a primary. Matched case-insensitively on the
# normalized key name (``-``/``_`` stripped). This is intentionally a
# *specific* list rather than "any unknown top-level key" — real
# manifests routinely carry harmless side-channel metadata like
# ``cwd``/``service``/``host``/``pid`` and stripping those payloads
# silently drops valid env-only manifests.
_LOG_DIAGNOSTIC_KEYS = frozenset(
    {
        "detail",
        "details",
        "reason",
        "description",
        "cause",
        "failure",
        "err",
        "errno",
        "exception",
        "exc",
        "excinfo",
        "stack",
        "stacktrace",
        "trace",
        "traceback",
    }
)


def _is_log_diagnostic_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.replace("-", "").replace("_", "").lower()
    return normalized in _LOG_DIAGNOSTIC_KEYS


def _looks_like_log_record(payload: dict) -> bool:
    if any(k in payload for k in _LOG_LEVEL_KEYS):
        return True
    if any(k in payload for k in _LOG_EVENT_KEYS):
        return True
    # A top-level ``msg``/``message`` string is a conventional log marker
    # (stdlib logging JSON formatters, structlog, bunyan, pino all emit it).
    # Real manifests never carry these keys, so their presence alone — even
    # without a matching ``level`` — is enough to mark the payload as log-shaped.
    for key in _LOG_MESSAGE_KEYS:
        if isinstance(payload.get(key), str):
            return True
    # Plain diagnostic JSON like {"error":"migration failed", "env":{...}}
    # is log-shaped, not a manifest: ``error`` as a top-level string is a
    # conventional failure marker that real manifests do not carry.
    if isinstance(payload.get("error"), str):
        return True
    # Weak payload (no strong manifest key) carrying a known diagnostic key
    # (``detail``/``reason``/``cause``/stack-trace fields) is a plain JSON
    # error object that happens to include ``env`` as context — not a
    # manifest. Catches framework-emitted failures (FastAPI/Django/etc.
    # ``{"detail":"migration failed", "env":{...}}``) that don't carry any
    # of the ``level`` / ``msg`` / ``event`` / ``error`` markers above.
    # Intentionally a narrow key list: real env-only manifests routinely
    # carry harmless side-channel metadata (``cwd``/``service``/``host``/
    # ``pid``) and must not be dropped.
    if not _has_strong_manifest_key(payload):
        for key in payload:
            if _is_log_diagnostic_key(key):
                return True
    return False


def _has_strong_manifest_key(payload: dict) -> bool:
    for key in _MANIFEST_STRONG_KEYS:
        expected = _MANIFEST_KEY_TYPES[key]
        if key in payload and isinstance(payload[key], expected):
            return True
    return False


def _is_manifest_shaped(payload: object) -> bool:
    """True iff ``payload`` could plausibly be a setup manifest.

    A real manifest carries at least one documented manifest key whose value
    has the expected type. Log-shaped payloads (``level``+``msg`` / ``event``
    / ``error`` string) are still accepted when they carry valid manifest
    keys — setup helpers sometimes emit the manifest through a structured
    logger that stamps extra metadata. Ranking (not acceptance) is what
    prevents a trailing log record from overwriting an earlier real
    manifest.
    """
    if not isinstance(payload, dict):
        return False
    for key, expected in _MANIFEST_KEY_TYPES.items():
        if key in payload and isinstance(payload[key], expected):
            return True
    return False


_SETUP_TIER_PRIMARY = 2
_SETUP_TIER_FALLBACK = 1
_SETUP_TIER_DROP = 0


def _classify_setup_candidate(payload: dict) -> int:
    """Classify a manifest-shaped payload for ranking in parse.

    Returns:
      * ``_SETUP_TIER_PRIMARY`` — authoritative manifest emission. Plain
        payloads with no log signals, or benign logger-wrapped payloads
        that carry a strong manifest key (``prompt`` / ``mcp_prompt`` /
        ``mcp_servers`` / ``managed_processes``). Msg/message on a strong
        payload is treated as status metadata, not a demotion.
      * ``_SETUP_TIER_FALLBACK`` — benign log-shaped env-only payloads
        (``{"level":"info","msg":"ready","env":{...}}``) or msg-only
        payloads without a strong key. These carry real state when setup
        emits its manifest through a logger, but a weak payload cannot be
        distinguished from an ordinary status log that happens to attach
        env as context. Used only when no primary candidate exists —
        otherwise a stray ``{"level":"info","msg":"starting","env":{...}}``
        log silently overwrites a real partial manifest.
      * ``_SETUP_TIER_DROP`` — diagnostic records: non-benign level,
        weak ``event``-wrapped (no strong manifest key), top-level ``error``
        string, or a weak payload carrying a known diagnostic key
        (``detail``/``reason``/stack).
    """
    has_strong = _has_strong_manifest_key(payload)
    # ``event``-wrapped payload without a strong manifest key is a
    # structured diagnostic emission (``{"event":"bootstrap_retry",
    # "env":{...}}``) and must not overwrite a real partial manifest.
    # But a structlog-style manifest emission that stamps the record with
    # ``event`` AND carries a strong manifest key
    # (``{"event":"ready","managed_processes":[...],"env":{...}}``) is a
    # real manifest — dropping it silently loses env/MCP/process handoff
    # on the failure path.
    if any(key in payload for key in _LOG_EVENT_KEYS) and not has_strong:
        return _SETUP_TIER_DROP
    if isinstance(payload.get("error"), str):
        return _SETUP_TIER_DROP

    has_level = False
    benign_level = False
    for key in _LOG_LEVEL_KEYS:
        if key not in payload:
            continue
        has_level = True
        value = payload[key]
        if isinstance(value, str) and value.strip().lower() in _BENIGN_LOG_LEVELS:
            benign_level = True
        elif (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value in _BENIGN_NUMERIC_LOG_LEVELS
        ):
            benign_level = True
    # Non-benign level (warn/error/fatal/...) demotes a weak payload to DROP —
    # diagnostic context carried alongside an ``env`` dict is not a manifest.
    # A strong manifest key (``managed_processes`` / ``mcp_servers`` /
    # ``prompt`` / ``mcp_prompt``) carried on an error-level record is a
    # setup helper emitting its final partial manifest through a warning/
    # error logger — demoting that to FALLBACK (not DROP) keeps the
    # best-effort contract: when no primary exists the partial handoff still
    # wins so teardown can reach already-started processes, but any earlier
    # plain or benign-level primary still overrides it.
    if has_level and not benign_level:
        if has_strong:
            return _SETUP_TIER_FALLBACK
        return _SETUP_TIER_DROP

    if not has_strong:
        for key in payload:
            if _is_log_diagnostic_key(key):
                return _SETUP_TIER_DROP

    has_msg = any(isinstance(payload.get(key), str) for key in _LOG_MESSAGE_KEYS)

    if not has_level and not has_msg:
        return _SETUP_TIER_PRIMARY
    if has_strong:
        return _SETUP_TIER_PRIMARY
    return _SETUP_TIER_FALLBACK


def _should_strip_as_manifest(payload: object) -> bool:
    """True when the payload should be stripped entirely from stdout_tail.

    Stricter than :func:`_is_manifest_shaped`: env-carrying payloads that
    also look like log records (``level``+``msg`` / ``event``) are **not**
    stripped whole — the caller redacts their ``env`` value instead so the
    msg/level still reaches diagnostics without leaking secret env values.
    """
    if not isinstance(payload, dict):
        return False
    if _has_strong_manifest_key(payload):
        return True
    if _looks_like_log_record(payload):
        return False
    for key, expected in _MANIFEST_KEY_TYPES.items():
        if key in payload and isinstance(payload[key], expected):
            return True
    return False


_MANIFEST_PAYLOAD_KEYS_TO_SCRUB = frozenset(
    {"managed_processes", "mcp_servers", "prompt", "mcp_prompt"}
)

# Keys whose values almost always carry secrets in structured log records
# (stdlib logging JSON formatters, structlog, bunyan, pino). Matched
# case-insensitively on the normalized key name — hyphens and underscores
# are treated as equivalent so ``api-key`` / ``api_key`` / ``APIKEY`` all
# match. ``redact_sensitive`` only scrubs well-known ``k=v`` / Bearer /
# URL-auth string patterns, not secrets stored as structured JSON values,
# so the log sanitizer has to drop them explicitly before the JSON is
# re-serialized into stdout_tail/stderr_tail.
_SENSITIVE_LOG_KEY_RE = re.compile(
    r"^(?:password|passwd|pwd|secret|apikey|"
    r"databaseurl|dburl|connectionstring|"
    r"token|auth|authorization|cookie|bearer|"
    r"sessionkey|sessiontoken|accesskey|accesstoken|"
    r"refreshtoken|clientsecret|privatekey|credential|credentials)$",
    re.IGNORECASE,
)

# Substring variant for structured JSON log field names: ``SECRET_KEY``,
# ``DATABASE_PASSWORD``, ``STRIPE_API_KEY``, ``user_access_token``, etc.
# Real-world log/context fields freely compose prefixes and suffixes, so
# anchored exact matches leak the most common suffix-form secret names. The
# tradeoff is a rare false-positive redaction on benign compound names in
# diagnostic output — acceptable, since the alternative is leaking live
# secrets into stdout_tail/stderr_tail and the agent prompt.
_SENSITIVE_LOG_FIELD_RE = re.compile(
    r"password|passwd|secret|apikey|"
    r"databaseurl|dburl|connectionstring|"
    r"authorization|bearer|"
    r"sessionkey|sessiontoken|accesskey|accesstoken|"
    r"refreshtoken|clientsecret|privatekey|credential",
    re.IGNORECASE,
)

# Tokens too short for safe substring matching on field names: ``auth``
# collides with ``author``/``authentic``/``authoring``, ``token`` with
# ``tokenize``, ``pwd`` with noise, etc. Require a trailing ``token`` /
# ``key`` / ``secret`` / ``id`` suffix *or* an anchored match so we still
# catch ``AUTH_TOKEN``, ``API_TOKEN``, ``GITHUB_TOKEN``, ``MYTOKEN`` but
# not ``tokenize`` or ``authored``. The trailing ``(?:auth|token)$``
# alternative additionally catches separator-less ``*token``/``*auth``
# composites (``authtoken``, ``apitoken``, ``githubtoken``, ``oauth``)
# that the boundary-anchored form misses — argv flags like
# ``--authtoken secret`` previously leaked their value because
# ``authtoken`` doesn't satisfy a non-letter boundary on either side.
_SENSITIVE_LOG_FIELD_SUFFIX_RE = re.compile(
    r"(?:^|[^a-z])(?:auth|token|pwd|cookie)(?:$|(?=[^a-z]))"
    r"|(?:auth|token)$",
    re.IGNORECASE,
)


def _is_sensitive_log_key(key: object) -> bool:
    """Exact-name match used for argv flag redaction.

    Kept strict so flags like ``--author-name`` are not treated as
    credentials. The broader structured-field matcher
    :func:`_is_sensitive_log_field` is used for JSON log key names where
    suffix composition (``SECRET_KEY``) dominates.
    """
    if not isinstance(key, str):
        return False
    normalized = key.replace("-", "").replace("_", "")
    return bool(_SENSITIVE_LOG_KEY_RE.match(normalized))


def _is_sensitive_log_field(key: object) -> bool:
    """Substring/suffix match for structured JSON field names.

    Catches compound credential names that exact matching misses —
    ``SECRET_KEY``, ``DATABASE_PASSWORD``, ``STRIPE_API_KEY``,
    ``GITHUB_TOKEN`` — while still avoiding gross false positives on
    short tokens (``auth``/``token``/``pwd``) that only match on word
    boundaries.
    """
    if not isinstance(key, str):
        return False
    if _is_sensitive_log_key(key):
        return True
    # Normalize by stripping separators so ``API_KEY`` / ``api-key`` /
    # ``apikey`` all collapse to ``apikey`` and match the compound regex.
    normalized = key.replace("-", "").replace("_", "")
    if _SENSITIVE_LOG_FIELD_RE.search(normalized):
        return True
    # Short tokens (``auth`` / ``token`` / ``pwd`` / ``cookie``) need a
    # separator boundary to avoid matching ``author``/``tokenize``/etc.
    # Use the original (un-normalized) key so separators still delimit.
    return bool(_SENSITIVE_LOG_FIELD_SUFFIX_RE.search(key))


def _sanitize_log_record_for_diagnostics(payload: dict) -> str:
    """Re-serialize a log-shaped ``payload`` for inclusion in stdout_tail.

    Drops manifest payload keys (managed_processes / mcp_servers / prompt /
    mcp_prompt) so the manifest body does not leak into warnings, redacts
    every dict-valued ``env`` at any depth, and redacts values stored under
    known sensitive key names (``token``, ``password``, ``authorization``,
    etc.) so secrets carried as structured context don't survive. The
    surrounding log text (level/msg/error/event) is preserved so the
    diagnostic stays actionable for the agent.
    """

    def _clean(obj: object) -> object:
        if isinstance(obj, dict):
            cleaned: dict[str, object] = {}
            for k, v in obj.items():
                if k in _MANIFEST_PAYLOAD_KEYS_TO_SCRUB:
                    continue
                if k == "env" and isinstance(v, dict):
                    cleaned[k] = "<REDACTED>"
                elif _is_sensitive_log_field(k):
                    cleaned[k] = "<REDACTED>"
                else:
                    cleaned[k] = _clean(v)
            return cleaned
        if isinstance(obj, list):
            return [_clean(item) for item in obj]
        return obj

    return json.dumps(_clean(payload))


def _parse_implement_setup_manifest(
    stdout: str,
    *,
    allow_trailing: bool = False,
) -> ImplementSetupManifest:
    text = (stdout or "").strip()
    if not text:
        return ImplementSetupManifest()

    decoder = json.JSONDecoder()
    # Probe every ``{`` position and try to decode — setup helpers routinely
    # prefix the trailing JSON with progress prose on the same line
    # (``INFO ready {"env":{...}}``), so restricting decoding to the start
    # of a line would silently drop env/prompt/MCP handoff. A successfully
    # decoded object consumes its full span, so nested ``{`` inside a
    # structured record (``{"payload":{"env":{...}}}``) is never re-probed
    # as a standalone manifest. Candidate acceptance additionally requires
    # the JSON to be the last non-whitespace content on its line; a JSON
    # fragment embedded mid-line with prose trailing it
    # (``ERROR validating {"env":"X"} after fail``) is log prose, not a
    # manifest emission.
    # Each candidate carries: (coerced manifest, tier, present_keys).
    # ``present_keys`` is computed from the *raw* payload so a partial
    # manifest that declares a field with an empty value
    # (``managed_processes: []``, ``mcp_servers: {}``) is still treated as
    # an authoritative assertion about that field. Truthiness on the
    # coerced manifest would hide such explicit clears and let earlier
    # stale state win.
    primaries: list[tuple[ImplementSetupManifest, frozenset[str]]] = []
    fallbacks: list[tuple[ImplementSetupManifest, frozenset[str]]] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("{", i)
        if j == -1:
            break
        try:
            payload, end = decoder.raw_decode(text[j:])
        except json.JSONDecodeError:
            i = j + 1
            continue
        absolute_end = j + end
        newline_index = text.find("\n", absolute_end)
        line_tail_end = n if newline_index == -1 else newline_index
        line_trailing = text[absolute_end:line_tail_end]
        trailing = text[absolute_end:].strip()
        if (
            _is_manifest_shaped(payload)
            and not line_trailing.strip()
            and (allow_trailing or not trailing)
        ):
            assert isinstance(payload, dict)
            tier = _classify_setup_candidate(payload)
            if tier != _SETUP_TIER_DROP:
                present_keys = frozenset(
                    key
                    for key, expected in _MANIFEST_KEY_TYPES.items()
                    if key in payload and isinstance(payload[key], expected)
                )
                entry = (_coerce_implement_setup_manifest(payload), present_keys)
                if tier == _SETUP_TIER_PRIMARY:
                    primaries.append(entry)
                else:
                    fallbacks.append(entry)
        i = absolute_end

    # Setup helpers may emit multiple partial manifests before failing —
    # e.g. an env-only snapshot followed by a separate managed_processes
    # snapshot after more state is registered. Primaries (plain manifests
    # and benign logger wrappers that carry a strong manifest key) merge
    # per-key with the latest winning. Fallbacks (benign env-only logger
    # wrappers, msg-only payloads without a strong key, error-level
    # records that still carry a strong manifest key) are consulted
    # per-key for any field no primary supplies: an ordinary
    # ``{"level":"info","msg":"starting","env":{...}}`` status log is
    # indistinguishable from a deliberate logger-wrapped env emission, so
    # a real primary's ``env`` must win over a trailing fallback's
    # ``env``, but a strong field (``managed_processes`` / ``mcp_servers``
    # / ``prompt`` / ``mcp_prompt``) that *only* the fallback carries is
    # valid late partial handoff — typically a setup helper emitting its
    # final registered-processes list through a warning/error logger
    # after the primary env snapshot. Dropping those wholesale leaks
    # already-started children across attempts because teardown never
    # learns about them. Diagnostic records (non-benign level without a
    # strong key, ``event``/``error`` string, diagnostic keys) are dropped
    # entirely — a failing setup must not inject bogus MCP/prompt state
    # through a trailing pure-log record.
    if not primaries and not fallbacks:
        return ImplementSetupManifest()

    _MISSING = object()

    def _pick(key: str, getter):
        for manifest, present in reversed(primaries):
            if key in present:
                return getter(manifest)
        for manifest, present in reversed(fallbacks):
            if key in present:
                return getter(manifest)
        return _MISSING

    def _pick_merged_env() -> dict[str, str] | object:
        # Env handoff is a dict keyed by individual variable names, not a
        # wholesale snapshot: setup helpers often emit multiple partial
        # env snapshots (early DATABASE_URL, later NODE_ENV alongside a
        # managed_processes registration). Later writes must override the
        # same key but MUST NOT wipe unrelated keys an earlier primary
        # already declared, otherwise a logger-wrapped partial manifest
        # that carries a strong field alongside a tiny env context dict
        # silently strips the real DATABASE_URL/TEST_DATABASE_URL handoff
        # the earlier primary emitted. Merging primaries earliest-to-latest
        # (later keys winning on conflict) matches the documented
        # best-effort prewarm semantics while preserving "latest wins"
        # for same-key updates.
        #
        # Fallbacks carrying a strong manifest key alongside ``env`` are
        # late partial handoffs — typically a setup helper emitting its
        # final ``managed_processes``/``mcp_servers`` list through an
        # error/warn logger after an earlier primary env snapshot, with
        # the connection vars the agent needs to use those processes
        # bundled in the same record. Their env MUST merge alongside the
        # strong field, otherwise we register child processes for teardown
        # but drop the vars the agent needs to talk to them. Env-only
        # fallbacks (no strong key — benign logger-wrapped status logs
        # indistinguishable from a deliberate env emission) are still
        # only consulted when no primary declares env, so a stray
        # ``{"level":"info","msg":"starting","env":{...}}`` cannot
        # overwrite a real primary's env.
        primary_envs = [
            manifest.env for manifest, present in primaries if "env" in present
        ]
        strong_fallback_envs = [
            manifest.env
            for manifest, present in fallbacks
            if "env" in present
            and any(key in present for key in _MANIFEST_STRONG_KEYS)
        ]
        if primary_envs or strong_fallback_envs:
            # Strong fallback envs fill gaps where no primary declared a
            # key, but primary envs always win on conflicts regardless of
            # stdout order. A fallback record emitted earlier than a
            # plain primary must not clobber the primary's handoff on an
            # overlapping key — otherwise a diagnostic fallback can
            # replace the actual setup env the agent receives.
            sources = [*strong_fallback_envs, *primary_envs]
        else:
            sources = [
                manifest.env for manifest, present in fallbacks if "env" in present
            ]
        if not sources:
            return _MISSING
        merged: dict[str, str] = {}
        for env in sources:
            merged.update(env)
        return merged

    env_value = _pick_merged_env()
    prompt_value = _pick("prompt", lambda m: m.prompt)
    mcp_prompt_value = _pick("mcp_prompt", lambda m: m.mcp_prompt)
    mcp_servers_value = _pick("mcp_servers", lambda m: m.mcp_servers)
    managed_processes_value = _pick("managed_processes", lambda m: m.managed_processes)
    return ImplementSetupManifest(
        env={} if env_value is _MISSING else env_value,
        prompt="" if prompt_value is _MISSING else prompt_value,
        mcp_prompt="" if mcp_prompt_value is _MISSING else mcp_prompt_value,
        mcp_servers={} if mcp_servers_value is _MISSING else mcp_servers_value,
        managed_processes=()
        if managed_processes_value is _MISSING
        else managed_processes_value,
    )


def _manifest_has_content(manifest: ImplementSetupManifest) -> bool:
    return bool(
        manifest.env
        or manifest.prompt
        or manifest.mcp_prompt
        or manifest.mcp_servers
        or manifest.managed_processes
    )


def _record_nonfatal_warning(
    run: RunState,
    *,
    phase: str,
    failure_type: str,
    failure_subtype: str,
    summary: str,
    action: str = "",
    detail: str = "",
    gate_name: str = "",
    retryable: bool = False,
) -> dict[str, object]:
    warning = _coerce_nonfatal_warning(
        {
            "phase": phase,
            "failure_type": failure_type,
            "failure_subtype": failure_subtype,
            "retryable": retryable,
            "nonfatal": True,
            "gate_name": gate_name,
            "summary": summary,
            "action": action,
            "detail": detail,
            "recorded_at": _now_iso(),
        }
    )
    if warning:
        run.nonfatal_warnings.append(warning)
    return warning


def _build_implement_command_metadata(
    run: RunState,
    worktree_path: Path,
) -> tuple[dict[str, str], list[str]]:
    spec_path = _spec_path_for_run(run)
    env = {
        "SPEC_ID": run.spec_id,
        "SPEC_RUN_ID": run.run_id,
        "SPEC_PATH": spec_path,
        "SPEC_WORKTREE": str(worktree_path),
        "SPEC_ATTEMPT": str(run.attempts + 1),
    }
    args = [
        "--worktree",
        str(worktree_path),
        "--spec-id",
        run.spec_id,
        "--run-id",
        run.run_id,
        "--attempt",
        str(run.attempts + 1),
    ]
    return env, args


_CLAUDE_PORTABLE_AUTH_ENV_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def _apply_implement_run_identity(env: dict[str, str], run: RunState) -> None:
    """Set environment values owned by the active implement launch."""
    env["SPEC_ID"] = run.spec_id
    env["SPEC_RUN_ID"] = run.run_id
    env["SPEC_PATH"] = _spec_path_for_run(run)
    env["SPEC_ATTEMPT"] = str(run.attempts)
    env["SPEC_IMPLEMENT_LAUNCH"] = str(run.implement_launches)


def _build_implement_agent_env(
    run: RunState,
    worktree_path: Path,
) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CLAUDE_PORTABLE_AUTH_ENV_KEYS
    }
    _inject_worktree_venv_into_env(env, worktree_path)
    _apply_implement_run_identity(env, run)
    return env


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _strip_manifest_json_objects(text: str) -> str:
    """Remove only manifest-shaped JSON objects from ``text``.

    Setup helpers may interleave structured log records with the real
    manifest. The manifest can carry env values (e.g. DATABASE_URL) that must
    not leak into failure diagnostics, so we drop manifest-shaped objects
    wherever they appear. Other JSON — log records like
    ``{"level":"error","msg":"migration failed"}`` — is preserved because
    it is exactly the actionable detail the agent needs to repair the failure.
    """
    if not text:
        return text
    decoder = json.JSONDecoder()
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            try:
                payload, end = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                out.append(text[i])
                i += 1
                continue
            absolute_end = i + end
            newline_index = text.find("\n", absolute_end)
            line_tail_end = n if newline_index == -1 else newline_index
            line_trailing = text[absolute_end:line_tail_end]
            # Only strip manifest-shaped JSON when it is the last non-whitespace
            # content on its line. JSON fragments embedded inside a log line
            # (``ERROR validating {"env":"X"} after fail``) are preserved so
            # the surrounding diagnostic text remains intact; the ``{...}``
            # there is not a real manifest emission.
            at_line_end = not line_trailing.strip()
            # A real manifest that annotates itself with a status message
            # (``{"prompt":"Ready","message":"bootstrapped"}``) has no
            # ``level`` / ``event`` / ``error`` marker — only the
            # ``msg``/``message`` annotation. ``_looks_like_log_record``
            # still fires on that alone, and the sanitizer would scrub
            # the manifest payload keys but keep the surviving log
            # metadata (``{"message":"bootstrapped"}``) — which
            # ``_first_non_manifest_stdout_line`` then surfaces as
            # ``failure.message`` instead of the real error further down
            # in stdout. Detect this shape (strong manifest key, no
            # explicit log-level / event / error marker) and strip the
            # record entirely so manifest status annotations never reach
            # the failure diagnostics. Records that also carry an
            # explicit log-level / event / error marker
            # (``{"level":"error","msg":"boom","managed_processes":[...]}``)
            # fall through to the sanitizer below so the diagnostic text
            # is preserved while the manifest body is scrubbed.
            if (
                isinstance(payload, dict)
                and _has_strong_manifest_key(payload)
                and not any(key in payload for key in _LOG_LEVEL_KEYS)
                and not any(key in payload for key in _LOG_EVENT_KEYS)
                and not isinstance(payload.get("error"), str)
            ):
                i = absolute_end
                continue
            # A log-shaped record (level+msg / event / error) might carry a
            # real manifest payload alongside its log text (setup helpers
            # that emit through a structured logger stamp every record with
            # level/msg). Scrub the manifest payload keys and redact every
            # env/secret mapping so the surrounding msg/level/error still
            # reaches the failure log and agent prompt — even when the
            # record is embedded inside a larger prose line, since
            # ``redact_sensitive`` does not scrub secrets carried as JSON
            # fields (``"password":"hunter2"``).
            if isinstance(payload, dict) and _looks_like_log_record(payload):
                out.append(_sanitize_log_record_for_diagnostics(payload))
                i = absolute_end
                continue
            if at_line_end and _should_strip_as_manifest(payload):
                i = absolute_end
                continue
            # Manifest-shaped JSON embedded mid-line (``ERROR x {"env":
            # {"API_TOKEN":"..."}} after fail``) is not a real manifest
            # emission — the parser rejects it — but it can still carry
            # secrets in structured env/field values. ``redact_sensitive``
            # only scrubs string-level ``k=v`` / URL-auth patterns, not
            # secrets stored as JSON values, so sanitize the payload here
            # before it reaches stdout_tail. The surrounding prose is
            # preserved so the diagnostic context remains actionable.
            if isinstance(payload, dict) and _should_strip_as_manifest(payload):
                out.append(_sanitize_log_record_for_diagnostics(payload))
                i = absolute_end
                continue
            # Plain JSON dict that is neither a manifest nor a log-shaped
            # record still needs its secret-bearing fields scrubbed —
            # ``redact_sensitive`` only scrubs string-level ``k=v`` / URL /
            # Bearer patterns, not secrets stored as structured JSON values
            # (``{"password":"hunter2"}``, ``{"api_key":"..."}``). Without
            # this, a plain diagnostic dump on stderr/stdout would leak
            # secrets verbatim into ``stdout_tail``/``stderr_tail``, the
            # warning log, and the agent failure prompt.
            if isinstance(payload, dict):
                out.append(_sanitize_log_record_for_diagnostics(payload))
                i = absolute_end
                continue
            # Successfully decoded but not a dict (list / scalar) — preserve
            # verbatim and skip past it so nested objects are not re-examined
            # as standalone manifests.
            out.append(text[i:absolute_end])
            i = absolute_end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _first_non_manifest_stdout_line(text: str) -> str:
    """First stdout line that is not part of a manifest JSON object.

    Used to extract a human-readable error summary from stdout without leaking
    manifest payload. Non-manifest JSON (log records) is kept so a structured
    error log can still serve as the one-line summary.
    """
    return _first_nonempty_line(_strip_manifest_json_objects(text or ""))


def _stdout_is_pure_manifest(stdout_tail: str) -> bool:
    """Return True if ``stdout_tail`` is exactly a manifest-shaped JSON object.

    Defensive guard for the failure prompt in case manifest JSON ever reaches
    the rendering step un-stripped (e.g. when callers fabricate a failure
    record directly). Non-manifest JSON falls through to the regular stdout
    rendering path so log records remain visible to the agent.
    """
    text = (stdout_tail or "").strip()
    if not text or not text.startswith("{"):
        return False
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return False
    if text[end:].strip():
        return False
    return _is_manifest_shaped(payload)


def _run_implement_setup_command(
    run: RunState,
    worktree_path: Path,
) -> ImplementSetupManifest:
    variants = SPEC_RUNTIME_CONFIG.implement.setup
    selected = variants.select()
    command = SPEC_RUNTIME_CONFIG.implement.setup_command.strip()
    if selected is not None and not command:
        command = selected.display()
    typed = selected is not None and _selected_command_uses_typed_runtime(variants, selected)
    backend = _resolve_execution_backend()
    if not command:
        _snapshot_container_workspace_after_setup(run, worktree_path, backend)
        return ImplementSetupManifest()

    env, args = _build_implement_command_metadata(run, worktree_path)
    _inject_worktree_venv_into_env(env, worktree_path)
    # Malformed shell quoting in ``implement.setup_command`` (e.g. an
    # unclosed quote) must degrade to a best-effort failure manifest, not
    # raise past ``phase_implement``. Surfacing it as an ``ImplementSetupFailure``
    # lets the agent launch with diagnostics instead of aborting the phase.
    try:
        split_command = [] if typed else [*shlex.split(command), *args]
    except ValueError as exc:
        # ``shlex.split`` failed (e.g. unclosed quote), so we cannot rely on
        # proper tokenization. Fall back to argv-aware best-effort redaction
        # which scrubs the same argv shapes as the normal nonzero-exit path
        # (``PGPASSWORD=...``, ``--github-token tok``, ``-ptok``) and also
        # redacts run-on tokens after a sensitive bare flag so an unclosed
        # quote cannot leak the value tail. ``redact_sensitive`` alone only
        # matches string-level patterns and would miss those forms.
        redacted_command = _redact_unparseable_command(command)
        message = f"could not parse setup command {redacted_command!r}: {exc}"
        logger.warning(
            "Implement setup command parse failed for %s: %s",
            run.run_id,
            redact_sensitive(message),
        )
        failure = ImplementSetupFailure(
            command=redacted_command,
            exit_code=None,
            stdout_tail="",
            stderr_tail="",
            message=redact_sensitive(message),
            launch_error=True,
        )
        return ImplementSetupManifest(failure=failure)
    command_str = selected.display() if typed and selected is not None else command
    try:
        launch = (
            selected.launch_argv(
                cwd=worktree_path,
                arguments=_shell_metadata_arguments(selected, args),
            )
            if typed and selected is not None
            else nullcontext(split_command)
        )
        with launch as setup_cmd:
            # Use the configured display for typed scripts so a generated
            # batch path never appears in logs or failure manifests.
            command_str = (
                selected.display() if typed and selected is not None
                else shlex.join(_redact_argv(setup_cmd))
            )
            result = backend.run_command(
                CommandRequest(
                    argv=setup_cmd,
                    cwd=worktree_path,
                    env=env,
                    inherit_env=True,
                )
            )
    except ExecutionBackendImportError:
        raise
    except OSError as exc:
        message = f"could not start {command_str}: {exc}"
        logger.warning("Implement setup command launch failed for %s: %s", run.run_id, redact_sensitive(message))
        failure = ImplementSetupFailure(
            command=command_str,
            exit_code=None,
            stdout_tail="",
            stderr_tail="",
            message=redact_sensitive(message),
            launch_error=True,
        )
        return ImplementSetupManifest(failure=failure)
    if result.returncode != 0:
        partial = _parse_implement_setup_manifest(
            result.stdout or "", allow_trailing=True
        )
        stdout_for_tail = _strip_manifest_json_objects(result.stdout or "")
        # Structured loggers emit to stderr too (``{"level":"error","env":{
        # "SECRET":"..."},"msg":"boom"}``). ``redact_sensitive`` only scrubs
        # well-known key=value / URL-auth patterns, not secrets buried in
        # JSON env mappings, so apply the same manifest/env-in-log scrubbing
        # to stderr before it reaches ``stderr_tail``/``message`` — otherwise
        # those secrets would be rendered in the setup-failure prompt and
        # warning log.
        stderr_for_tail = _strip_manifest_json_objects(result.stderr or "")
        stdout_tail = redact_sensitive(_tail_chars(tail_lines(stdout_for_tail)).strip())
        stderr_tail = redact_sensitive(_tail_chars(tail_lines(stderr_for_tail)).strip())
        # Prefer stderr for the human summary. If stderr is empty, fall back to
        # the first stdout line that is not part of a manifest JSON payload, so
        # shell helpers that log errors to stdout still produce an actionable
        # one-line summary without leaking manifest env values.
        message = (
            _first_nonempty_line(stderr_for_tail)
            or _first_non_manifest_stdout_line(result.stdout or "")
            or f"exit code {result.returncode}"
        )
        failure = ImplementSetupFailure(
            command=command_str,
            exit_code=result.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            message=redact_sensitive(message),
            launch_error=False,
        )
        log_sections = []
        if stdout_tail:
            log_sections.append(f"--- stdout ---\n{stdout_tail}")
        if stderr_tail:
            log_sections.append(f"--- stderr ---\n{stderr_tail}")
        log_detail = "\n".join(log_sections) if log_sections else f"exit code {result.returncode}"
        logger.warning(
            "Implement setup command failed for %s (exit %s): %s",
            run.run_id,
            result.returncode,
            log_detail,
        )
        return ImplementSetupManifest(
            env=partial.env,
            prompt=partial.prompt,
            mcp_prompt=partial.mcp_prompt,
            mcp_servers=partial.mcp_servers,
            managed_processes=partial.managed_processes,
            failure=failure,
        )
    manifest = _parse_implement_setup_manifest(result.stdout or "")
    _snapshot_container_workspace_after_setup(run, worktree_path, backend)
    return manifest


def _snapshot_container_workspace_after_setup(
    run: RunState,
    worktree_path: Path,
    backend: ExecutionBackend,
) -> None:
    if backend.identity.backend != "container":
        return
    run_root = worktree_path.parent
    snapshot_path = run_root / "snapshots" / "pre-implement"
    if snapshot_path.exists():
        return
    workspace = WorkspaceHandle(
        path=worktree_path,
        outbox_path=run_root / "outbox",
        branch=run.branch,
        backend=backend.identity.backend,
    )
    try:
        backend.snapshot(workspace, "pre-implement")
    except (NotImplementedError, OSError, RuntimeError) as exc:
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "snapshot-restore-fallback.log").write_text(
            f"Container backend setup snapshot unavailable: {exc}\n"
        )


def _restore_container_workspace_for_retry(
    workspace: WorkspaceHandle,
    backend: ExecutionBackend,
    ctx: ImplementContext,
) -> WorkspaceHandle:
    if backend.identity.backend != "container":
        return workspace
    if ctx.implement_reason in ("initial", "merge_conflict"):
        return workspace
    if ctx.triggering_phase not in ("verify", "review", "merge"):
        return workspace

    run_root = workspace.outbox_path.parent
    snapshot_path = run_root / "snapshots" / "pre-implement"
    snapshot = SnapshotRef(
        label="pre-implement",
        path=snapshot_path,
        metadata={"backend": "container", "snapshot_kind": "source-copy"},
    )
    prior_head = ""
    head_probe = run_subprocess(["git", "rev-parse", "HEAD"], cwd=workspace.path)
    if head_probe.returncode == 0:
        prior_head = (head_probe.stdout or "").strip()
    try:
        restored = backend.restore(workspace, snapshot)
    except (NotImplementedError, OSError, RuntimeError) as exc:
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "snapshot-restore-fallback.log").write_text(
            f"Container backend retry restore unavailable: {exc}\n"
        )
        return workspace
    _reposition_restored_workspace_head(restored, backend, ctx, prior_head)
    return restored


def _latest_rescue_bundle_for_head(run_root: Path, head_sha: str) -> Path | None:
    """Return the newest rescue bundle whose manifest recorded ``head_sha``."""
    index_path = run_root / "rescue" / "index.json"
    try:
        entries = json.loads(index_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("head_sha") or "").strip() != head_sha:
            continue
        artifacts = entry.get("artifacts")
        bundle = str((artifacts or {}).get("bundle") or "").strip() if isinstance(artifacts, dict) else ""
        if bundle and Path(bundle).is_file():
            return Path(bundle)
    return None


def _reposition_restored_workspace_head(
    workspace: WorkspaceHandle,
    backend: ExecutionBackend,
    ctx: ImplementContext,
    prior_head: str,
) -> None:
    """Move a snapshot-restored workspace back to its pre-restore branch head.

    The pre-implement snapshot restore rolls tracked files and HEAD back to the
    run's base commit. Review-triggered retries are repositioned to the
    reviewed head afterwards, but verify- and merge-triggered retries otherwise
    have no repositioning: once the branch carries commits, a gate failure can
    reset the workspace to base and the agent can "fix" base-equivalent code.
    Restore the pre-restore head so committed implementation work survives;
    uncommitted changes were already rescued by the restore itself.
    """
    if not prior_head:
        return
    if ctx.triggering_phase == "review" and (ctx.reviewed_head_sha or "").strip():
        # _position_review_retry_workspace_head owns positioning for this case.
        return
    worktree = workspace.path
    if _commit_present(worktree, prior_head).returncode != 0:
        branch = (getattr(workspace, "branch", "") or "").strip()
        if branch:
            try:
                run_git_fetch_with_timeout(
                    ["origin", f"{branch}:refs/remotes/origin/{branch}"],
                    cwd=worktree,
                    timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
                    runner=_orchestrator_fetch_runner,
                )
            except GitFetchTimeoutError:
                pass
        if _commit_present(worktree, prior_head).returncode != 0:
            # Pre-publish verify failures are the common case here: the
            # implementation commits exist only locally, and the restore
            # replaced .git wholesale. The restore rescued them as a git
            # bundle first — rehydrate it.
            bundle_path = _latest_rescue_bundle_for_head(
                workspace.outbox_path.parent, prior_head
            )
            if bundle_path is not None:
                fetched = run_subprocess(
                    ["git", "fetch", str(bundle_path), "HEAD"], cwd=worktree
                )
                if fetched.returncode != 0:
                    # Rescue bundles are thin. Their prerequisites include the
                    # base-ref commits the agent merged in during implement, and
                    # the restore replaced .git wholesale with a snapshot that
                    # predates them — so the fetch dies with "Repository lacks
                    # these prerequisite commits" and the work looks lost even
                    # though the bundle holds it. Refill the object store from
                    # origin, then rehydrate.
                    try:
                        run_git_fetch_with_timeout(
                            ["origin"],
                            cwd=worktree,
                            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
                            runner=_orchestrator_fetch_runner,
                        )
                    except GitFetchTimeoutError:
                        pass
                    else:
                        run_subprocess(
                            ["git", "fetch", str(bundle_path), "HEAD"], cwd=worktree
                        )
        if _commit_present(worktree, prior_head).returncode != 0:
            # Continuing from the snapshot would run the agent against
            # base-state code while the branch nominally carries commits —
            # the silent-loss failure mode this function exists to prevent.
            raise ValueError(
                "Snapshot restore discarded pre-restore head "
                f"{_short_sha(prior_head)} and it cannot be recovered from "
                "origin or a rescue bundle. Refusing to continue from the "
                "base snapshot; restore the implementation branch (or rescue "
                "bundle) and resume."
            )
    reset = run_subprocess(["git", "reset", "--hard", prior_head], cwd=worktree)
    if reset.returncode != 0:
        logger.warning(
            "Could not reposition restored workspace to pre-restore head %s: %s",
            _short_sha(prior_head),
            (reset.stderr or reset.stdout or "").strip()[-200:],
        )
        return
    # In volume workspace mode the worker runs against a Docker volume seeded by
    # the restore; push the repositioned head into that volume too, mirroring
    # _position_review_retry_workspace_head. Without this, volume-backed
    # verify/merge retries would still run agents against base-state code.
    reseed = getattr(backend, "reseed_workspace_volume", None)
    if callable(reseed):
        try:
            reseed(workspace)
        except (OSError, RuntimeError) as exc:
            # In volume mode the agent runs from the Docker volume: continuing
            # after a failed reseed would run it against the base snapshot
            # while the host checkout sits at the implementation head — the
            # silent-loss mode this function exists to prevent. Fail the retry
            # like the review positioning path does.
            raise ValueError(
                "Repositioned host workspace to pre-restore head "
                f"{_short_sha(prior_head)} but could not re-seed the worker "
                f"volume: {exc}"
            ) from exc


def _position_review_retry_workspace_head(
    run: RunState,
    workspace: WorkspaceHandle,
    backend: ExecutionBackend,
    ctx: ImplementContext,
) -> None:
    """Position a container review-retry workspace at the reviewed head.

    For a container-backed, review-triggered retry the pre-implement snapshot
    restore (:func:`_restore_container_workspace_for_retry`) rolls the workspace
    tree back to the base commit, discarding attempt 1's implementation and
    moving ``.git`` HEAD to base. The lineage guard then sees the reviewed head
    missing and blocks with a misleading drift error.

    Move the workspace HEAD (and the ``code/<slug>`` branch it is on) back to the
    reviewed implementation head ``ctx.reviewed_head_sha`` — fetching the code
    branch from ``origin`` (the forge remote) when the commit is missing — so the
    implement agent applies the pinned review feedback on top of the reviewed
    head and ``_validate_review_retry_lineage`` short-circuits as a pass.

    This is the implement-retry analogue of ``_ensure_review_head_present``, but
    with ``cwd`` = the retry workspace dir. Only tracked
    files move (``git reset --hard``), so gitignored dependencies/build caches the
    restore seeded are preserved. In ``volume`` workspace mode the worker
    volume is re-seeded from the repositioned host source so the agent runs against
    the reviewed head, not a base-state volume.

    Raises :class:`ValueError` with an actionable message naming the reviewed short
    SHA and branch when the reviewed head cannot be recovered — the caller
    surfaces this as a terminal error rather than running the agent against base.
    """
    if backend.identity.backend != "container":
        return
    if ctx.triggering_phase != "review":
        return
    reviewed_head = (ctx.reviewed_head_sha or "").strip()
    if not reviewed_head:
        return

    worktree_path = workspace.path
    branch_name = (run.branch or "").strip()
    branch_label = branch_name or "(unknown branch)"

    if _commit_present(worktree_path, reviewed_head).returncode != 0:
        if not branch_name:
            raise ValueError(
                "Review-triggered retry could not position the workspace at reviewed head "
                f"{_short_sha(reviewed_head)}: the commit is missing from the restored workspace "
                "and no code branch is recorded to fetch it from origin."
            )
        try:
            fetch_outcome = run_git_fetch_with_timeout(
                ["origin", f"{branch_name}:refs/remotes/origin/{branch_name}"],
                cwd=worktree_path,
                timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
                runner=_orchestrator_fetch_runner,
            )
        except GitFetchTimeoutError as exc:
            raise ValueError(
                "Review-triggered retry could not position the workspace at reviewed head "
                f"{_short_sha(reviewed_head)} for branch '{branch_label}': "
                f"git fetch timed out after {exc.timeout_seconds:.0f}s"
            ) from exc

        recheck = _commit_present(worktree_path, reviewed_head)
        if recheck.returncode != 0:
            fetch_detail = ""
            if not fetch_outcome.is_success:
                fetch_detail = fetch_outcome.stderr.strip() or fetch_outcome.stdout.strip()
            catfile_detail = recheck.stderr.strip() or recheck.stdout.strip()
            detail = fetch_detail or catfile_detail or "commit is still not present after fetch"
            raise ValueError(
                "Review-triggered retry could not position the workspace at reviewed head "
                f"{_short_sha(reviewed_head)} for branch '{branch_label}' "
                f"(stale or missing remote head): {detail}"
            )

    reset_result = run_subprocess(
        ["git", "reset", "--hard", reviewed_head],
        cwd=worktree_path,
    )
    if reset_result.returncode != 0:
        detail = reset_result.stderr.strip() or reset_result.stdout.strip() or "git reset --hard failed"
        raise ValueError(
            "Review-triggered retry could not position the workspace at reviewed head "
            f"{_short_sha(reviewed_head)} for branch '{branch_label}': {detail}"
        )

    # In volume workspace mode the worker runs against a Docker volume seeded
    # from the host source; push the repositioned head into that volume too.
    reseed = getattr(backend, "reseed_workspace_volume", None)
    if callable(reseed):
        try:
            reseed(workspace)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "Review-triggered retry positioned the host workspace at reviewed head "
                f"{_short_sha(reviewed_head)} for branch '{branch_label}' but could not re-seed "
                f"the worker volume: {exc}"
            ) from exc


def _run_implement_teardown_command(run: RunState, worktree_path: Path) -> None:
    variants = SPEC_RUNTIME_CONFIG.implement.teardown
    selected = variants.select()
    command = SPEC_RUNTIME_CONFIG.implement.teardown_command.strip()
    if selected is not None and not command:
        command = selected.display()
    if not command:
        return

    env, args = _build_implement_command_metadata(run, worktree_path)
    _inject_worktree_venv_into_env(env, worktree_path)
    typed = selected is not None and _selected_command_uses_typed_runtime(variants, selected)
    backend = _resolve_execution_backend()
    try:
        launch = (
            selected.launch_argv(
                cwd=worktree_path,
                arguments=_shell_metadata_arguments(selected, args),
            )
            if typed and selected is not None
            else nullcontext([*shlex.split(command), *args])
        )
        with launch as teardown_cmd:
            result = backend.run_command(
                CommandRequest(
                    argv=teardown_cmd,
                    cwd=worktree_path,
                    env=env,
                    inherit_env=True,
                )
            )
    except OSError as exc:
        logger.warning(
            "Implement teardown command failed for %s: could not start %s: %s",
            run.run_id,
            selected.display() if typed and selected is not None else command,
            exc,
        )
        return
    if result.returncode == 0:
        return

    logger.warning(
        "Implement teardown command failed for %s: %s",
        run.run_id,
        _format_subprocess_failure(result),
    )


def _parse_pytest_summary_counts(text: str) -> dict[str, int] | None:
    """Parse pytest's summary line into failed/passed/error counts."""
    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(nonempty_lines[-10:]):
        matches = re.findall(r"(\d+)\s+(passed|failed|errors?)\b", line)
        if not matches:
            continue
        counts = {"passed_count": 0, "failed_count": 0, "error_count": 0}
        for raw_value, label in matches:
            value = int(raw_value)
            if label == "passed":
                counts["passed_count"] = value
            elif label == "failed":
                counts["failed_count"] = value
            else:
                counts["error_count"] = value
        return counts
    return None


def _parse_pytest_failed_tests(
    text: str,
    *,
    failed_count: int | None,
    error_count: int | None = None,
) -> list[str] | None:
    """Parse failing pytest node ids from short test summary lines."""
    if failed_count is None and error_count is None:
        return None
    total_problem_count = max(failed_count or 0, 0) + max(error_count or 0, 0)
    if total_problem_count == 0:
        return []

    failed_tests = _extract_failed_test_node_ids(text)
    return failed_tests or None


def _is_generic_failure_wrapper_line(line: str) -> bool:
    return bool(
        re.match(
            r"^make(?:\[\d+\])?: \*\*\* (?:\[[^\]]+\] )?Error \d+\s*$",
            line,
        )
    )


def _looks_like_failure_detail(line: str) -> bool:
    return bool(
        line.startswith(("FAILED ", "FAIL ", "Error:", "ERROR:", "Traceback"))
        or re.search(r"\b(error|failed|failure|exception)\b", line, flags=re.IGNORECASE)
        or re.search(r":\d+:\d+:", line)
        or re.search(r"\(\d+,\d+\):\s*error\b", line, flags=re.IGNORECASE)
    )


def _first_meaningful_failure_line(*texts: str) -> str:
    fallback = ""
    for text in texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(("==", "---", "> ")):
                continue
            if line == "All checks passed!":
                continue
            if line.startswith("e2e: "):
                continue
            if _is_generic_failure_wrapper_line(line):
                continue
            redacted = redact_sensitive(line)
            if _looks_like_failure_detail(line):
                return redacted
            if not fallback:
                fallback = redacted
    return fallback


def _first_pytest_failure_headline(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("FAILED "):
            return redact_sensitive(line)
        if re.match(r"^_{3,}.+_{3,}$", line):
            return redact_sensitive(line)
    return ""


def _gate_failure_fingerprint(
    gate: str,
    *,
    stdout: str,
    stderr: str,
) -> str:
    payload: dict[str, object] = {"gate": gate}
    if _is_test_gate(gate):
        counts = _parse_pytest_summary_counts(stdout)
        failed_tests = _parse_pytest_failed_tests(
            stdout,
            failed_count=counts["failed_count"] if counts is not None else None,
            error_count=counts["error_count"] if counts is not None else None,
        )
        if failed_tests:
            payload["failed_tests"] = failed_tests
        headline = _first_pytest_failure_headline(stdout)
        if headline:
            payload["headline"] = headline
    else:
        headline = _first_meaningful_failure_line(stdout, stderr)
        if headline:
            payload["headline"] = headline

    if len(payload) == 1:
        headline = _first_meaningful_failure_line(stdout, stderr)
        if not headline:
            return ""
        payload["headline"] = headline

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _build_gate_history_entry(
    gate: str,
    *,
    attempt: int,
    exit_code: int,
    timestamp: str,
    stdout: str,
    stderr: str,
    diagnostic: str = "",
) -> dict[str, object]:
    """Build the per-attempt gate history entry."""
    entry: dict[str, object] = {
        "attempt": attempt,
        "status": "passed" if exit_code == 0 else "failed",
        "passed_count": None,
        "failed_count": None,
        "error_count": None,
        "failed_tests": None,
        "failure_fingerprint": "",
        "timestamp": timestamp,
    }
    if exit_code != 0:
        if _is_test_gate(gate):
            entry["failure_fingerprint"] = _build_test_failure_fingerprint(
                stdout,
                diagnostic,
            )
        else:
            entry["failure_fingerprint"] = _gate_failure_fingerprint(
                gate,
                stdout=stdout,
                stderr=stderr,
            )
    if not _is_test_gate(gate):
        return entry

    parse_source = stdout
    counts = _parse_pytest_summary_counts(parse_source)
    if counts is None:
        return entry

    entry.update(counts)
    entry["failed_tests"] = _parse_pytest_failed_tests(
        parse_source,
        failed_count=counts["failed_count"],
        error_count=counts["error_count"],
    )
    return entry


def _build_test_failure_fingerprint(stdout: str, diagnostic: str = "") -> str:
    diagnostic_source = (
        diagnostic if diagnostic and not diagnostic.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX) else ""
    )
    failed_tests = _extract_failed_test_node_ids(diagnostic_source) or _extract_failed_test_node_ids(stdout)
    traceback_header = _extract_first_pytest_traceback_header(
        diagnostic_source
    ) or _extract_first_pytest_traceback_header(stdout)
    failure_shape = _stored_test_failure_shape(stdout, diagnostic_source or stdout)
    if not failed_tests and not traceback_header and not failure_shape:
        return ""

    payload = json.dumps(
        {
            "failed_tests": failed_tests,
            "traceback_header": traceback_header,
            "failure_shape": failure_shape,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:TEST_FAILURE_FINGERPRINT_HEX_LENGTH]


def _diagnostic_note(message: str) -> str:
    return f"{TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX} {message.strip()}"


def _test_gate_diagnostic_command(worktree_path: Path) -> list[str]:
    """Mirror `make test` by running pytest via the worktree virtualenv."""
    return [str(_worktree_venv_python(worktree_path)), *TEST_GATE_DIAGNOSTIC_ARGS]


def _test_gate_targeted_diagnostic_command(
    worktree_path: Path,
    nodeid: str,
) -> list[str]:
    return [
        str(_worktree_venv_python(worktree_path)),
        "-m",
        "pytest",
        "--tb=short",
        "--no-header",
        "-q",
        "--maxfail=1",
        nodeid,
    ]


def _render_test_gate_targeted_diagnostic_command(
    worktree_path: Path,
    nodeid: str,
) -> str:
    return shlex.join(_test_gate_targeted_diagnostic_command(worktree_path, nodeid))


def _run_failed_test_diagnostic(
    worktree_path: Path,
    *,
    env: dict[str, str] | None = None,
    backend: ExecutionBackend | None = None,
) -> str:
    diagnostic_command = _test_gate_diagnostic_command(worktree_path)
    command = shlex.join(diagnostic_command)
    if backend is None:
        backend = _resolve_execution_backend()
    try:
        result = backend.run_command(
            CommandRequest(
                argv=list(diagnostic_command),
                cwd=worktree_path,
                env=env,
                inherit_env=True,
                timeout=TEST_GATE_DIAGNOSTIC_TIMEOUT_SECONDS,
            )
        )
    except subprocess.TimeoutExpired:
        return _diagnostic_note(f"timed out after {TEST_GATE_DIAGNOSTIC_TIMEOUT_SECONDS}s while running {command}.")
    except OSError as exc:
        return _diagnostic_note(f"could not start {command}: {exc}.")

    if result.returncode not in {0, 1}:
        detail_parts: list[str] = []
        if result.stdout:
            detail_parts.append(f"stdout: {_stored_gate_stdout(result.stdout)}")
        if result.stderr:
            detail_parts.append(f"stderr: {_stored_gate_stderr(result.stderr)}")
        detail = "; ".join(part for part in detail_parts if part)
        if detail:
            return _diagnostic_note(f"exited {result.returncode} while running {command}. {detail}")
        return _diagnostic_note(f"exited {result.returncode} while running {command}.")

    return result.stdout or ""


def _every_failed_test_passed_in_isolation(
    pytest_output: str, diagnostics: list[dict[str, str]]
) -> bool:
    """Whether the gate's only failures each rerun green on their own.

    That combination — a red suite whose every failing node passes by itself — is
    order-dependence or pollution, not a defect in the head under test. The
    orchestrator already gathers the evidence, so it can avoid spending a full
    implement attempt and another gate run asking an agent to fix a test that is
    not broken.

    Requires the diagnostics to cover *every* failed node. `_run_targeted_test_diagnostics`
    stops at TARGETED_TEST_DIAGNOSTIC_MAX_FAILURES, so a wider failure set is never
    fully checked and must not be judged on the subset that was.
    """
    nodeids = _extract_failed_test_node_ids(pytest_output)
    if not nodeids or len(nodeids) > TARGETED_TEST_DIAGNOSTIC_MAX_FAILURES:
        return False
    if len(diagnostics) != len(nodeids):
        return False
    return all(entry.get("status") == "passed" for entry in diagnostics)


def _run_targeted_test_diagnostics(
    worktree_path: Path,
    pytest_output: str,
    *,
    env: dict[str, str] | None = None,
    backend: ExecutionBackend | None = None,
) -> list[dict[str, str]]:
    nodeids = _extract_failed_test_node_ids(pytest_output)
    if not nodeids:
        return []

    if backend is None:
        backend = _resolve_execution_backend()

    diagnostics: list[dict[str, str]] = []
    for nodeid in nodeids[:TARGETED_TEST_DIAGNOSTIC_MAX_FAILURES]:
        command = _test_gate_targeted_diagnostic_command(worktree_path, nodeid)
        rendered_command = shlex.join(command)
        try:
            result = backend.run_command(
                CommandRequest(
                    argv=list(command),
                    cwd=worktree_path,
                    env=env,
                    inherit_env=True,
                    timeout=TARGETED_TEST_DIAGNOSTIC_TIMEOUT_SECONDS,
                )
            )
        except subprocess.TimeoutExpired:
            diagnostics.append(
                {
                    "nodeid": nodeid,
                    "status": "timeout",
                    "detail": _diagnostic_note(
                        f"timed out after {TARGETED_TEST_DIAGNOSTIC_TIMEOUT_SECONDS}s while running {rendered_command}."
                    ),
                }
            )
            continue
        except OSError as exc:
            diagnostics.append(
                {
                    "nodeid": nodeid,
                    "status": "error",
                    "detail": _diagnostic_note(f"could not start {rendered_command}: {exc}."),
                }
            )
            continue

        if result.returncode == 0:
            diagnostics.append(
                {
                    "nodeid": nodeid,
                    "status": "passed",
                    "detail": "Targeted rerun passed individually.",
                }
            )
            continue

        if result.returncode == 1:
            detail = result.stdout or result.stderr or _diagnostic_note(f"no output while running {rendered_command}.")
            diagnostics.append(
                {
                    "nodeid": nodeid,
                    "status": "failed",
                    "detail": detail,
                }
            )
            continue

        detail_parts: list[str] = []
        if result.stdout:
            detail_parts.append(f"stdout: {_stored_gate_stdout(result.stdout)}")
        if result.stderr:
            detail_parts.append(f"stderr: {_stored_gate_stderr(result.stderr)}")
        detail = "; ".join(part for part in detail_parts if part)
        if detail:
            detail = _diagnostic_note(f"exited {result.returncode} while running {rendered_command}. {detail}")
        else:
            detail = _diagnostic_note(f"exited {result.returncode} while running {rendered_command}.")
        diagnostics.append(
            {
                "nodeid": nodeid,
                "status": "error",
                "detail": detail,
            }
        )

    return diagnostics


def _format_targeted_test_diagnostics(entries: object) -> str:
    if not isinstance(entries, list):
        return ""

    sections: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nodeid = str(entry.get("nodeid") or "").strip()
        status = str(entry.get("status") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        if not nodeid or not detail:
            continue
        header = f"[{status}] {nodeid}" if status else nodeid
        sections.append(f"{header}\n{detail}")

    return "\n\n".join(sections)


def _has_actionable_test_diagnostic(gate_name: str, diagnostic: str) -> bool:
    return (
        gate_name == "test" and bool(diagnostic) and not diagnostic.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX)
    )


def _format_pytest_progress(entry: dict[str, object]) -> str:
    """Format parsed pytest counts for retry summaries."""
    failed_count = entry.get("failed_count")
    error_count = entry.get("error_count")
    passed_count = entry.get("passed_count")
    parts: list[str] = []
    if isinstance(failed_count, int):
        parts.append(f"{failed_count} failed")
    if isinstance(error_count, int) and (error_count > 0 or not parts):
        label = "error" if error_count == 1 else "errors"
        parts.append(f"{error_count} {label}")
    if isinstance(passed_count, int):
        parts.append(f"{passed_count} passed")
    return ", ".join(parts)


def _format_gate_failure_summary(gate_name: str, gate_data: dict) -> str:
    """Build a concise retry summary for a failing gate."""
    status = str(gate_data.get("last_status", "unknown"))
    attempt = int(gate_data.get("attempts", 0))
    first_failed_test_nodeid = str(gate_data.get("first_failed_test_nodeid", "") or "").strip()
    history = gate_data.get("history", [])
    latest = history[-1] if isinstance(history, list) and history else {}

    if isinstance(latest, dict):
        progress = _format_pytest_progress(latest)
        if progress:
            baseline = None
            current_failed = latest.get("failed_count")
            if isinstance(history, list):
                for entry in history:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("attempt") == latest.get("attempt"):
                        continue
                    if isinstance(entry.get("failed_count"), int):
                        baseline = entry
                        break

            summary = f"{gate_name}: {status} (attempt {attempt}: {progress}"
            if gate_name == "test" and first_failed_test_nodeid:
                summary += f"; first failing node {first_failed_test_nodeid}"
            if (
                isinstance(current_failed, int)
                and isinstance(baseline, dict)
                and isinstance(baseline.get("failed_count"), int)
                and int(baseline.get("attempt", 0)) > 0
                and baseline["failed_count"] != current_failed
            ):
                summary += f"; was {baseline['failed_count']} failed on attempt {baseline['attempt']}"
            return summary + ")"
    return f"{gate_name}: {status} (attempt {attempt})"


def _format_gate_retry_output(gate_name: str, gate_data: dict) -> str:
    """Format the stored gate output shown to the agent on retry."""
    sections = []
    diagnostic = str(gate_data.get("last_diagnostic", "") or "")
    stdout = str(gate_data.get("last_stdout", "") or "")
    stderr = str(gate_data.get("last_stderr", "") or "")
    first_failed_test_nodeid = str(gate_data.get("first_failed_test_nodeid", "") or "").strip()
    first_failed_test_reproducer = str(gate_data.get("first_failed_test_reproducer", "") or "").strip()
    final_failure_shape = str(gate_data.get("final_failure_shape", "") or "").strip()
    first_targeted = _first_targeted_test_diagnostic(
        gate_data.get("last_targeted_diagnostics", []),
        first_failed_nodeid=first_failed_test_nodeid,
    )
    live_reproducer_lines: list[str] = []
    if gate_name == "test" and first_failed_test_nodeid:
        live_reproducer_lines.append(f"First failing node: {first_failed_test_nodeid}")
        if first_failed_test_reproducer:
            live_reproducer_lines.append(f"Rerun first: {first_failed_test_reproducer}")
    if final_failure_shape:
        live_reproducer_lines.append(f"Final failure shape: {final_failure_shape}")
    if live_reproducer_lines:
        sections.append(f"--- {gate_name} (live reproducer) ---\n" + "\n".join(live_reproducer_lines))

    targeted_entries = []
    if first_targeted:
        targeted_entries.append(first_targeted)
    for entry in gate_data.get("last_targeted_diagnostics", []):
        if not isinstance(entry, dict):
            continue
        if first_targeted and entry.get("nodeid") == first_targeted.get("nodeid"):
            continue
        targeted_entries.append(entry)
    targeted = _format_targeted_test_diagnostics(targeted_entries)

    if _has_actionable_test_diagnostic(gate_name, diagnostic):
        sections.append(f"--- {gate_name} (diagnostic) ---\n{diagnostic}")
    elif stdout:
        sections.append(f"--- {gate_name} (stdout) ---\n{stdout}")
    elif diagnostic:
        sections.append(f"--- {gate_name} (diagnostic note) ---\n{diagnostic}")
    if targeted:
        sections.append(f"--- {gate_name} (targeted diagnostics) ---\n{targeted}")
    if stderr:
        sections.append(f"--- {gate_name} (stderr) ---\n{stderr}")
    return "\n".join(sections)


def is_non_actionable_gate_environment_block(summary: str) -> bool:
    """Return True when blocked text indicates local sandbox/runtime constraints."""
    text = (summary or "").lower()
    if not text:
        return False

    gate_context = (
        *VERIFY_GATE_COMMANDS.values(),
        "playwright",
        "browser",
        "pg_ctl",
        "postgres",
    )
    env_signatures = (
        "machport",
        "permission denied",
        "operation not permitted",
        "abort trap",
        "could not create shared memory segment",
        "shmat",
        "bootstrap_check_in",
        "browsers cannot launch",
    )
    return any(marker in text for marker in gate_context) and any(sig in text for sig in env_signatures)


CLAUDE_CREDENTIALS_PREFLIGHT_MARGIN_SECONDS = 120
CLAUDE_CREDENTIALS_EXPIRY_WARN_SECONDS = 1800
AGENT_CAPACITY_DEFAULT_BACKOFF_SECONDS = 300.0
AGENT_CAPACITY_MAX_BACKOFF_SECONDS = 24 * 60 * 60.0
AGENT_CAPACITY_LEASE_REFRESH_SECONDS = 30.0


def _claude_credentials_preflight_error(
    source_credentials: Path | None = None,
    *,
    backend: ExecutionBackend | None = None,
) -> str:
    """Return a blocking error when the host Claude OAuth token is (nearly) expired.

    Container workers receive a copy of the host access token with the refresh
    token deliberately stripped (``_write_claude_isolated_home``) and cannot
    reach the host ``~/.claude`` to refresh, so an expired host token
    guarantees a silent 401: in stream-json mode the CLI exits nonzero with
    EMPTY stdout/stderr, defeating every message classifier and burning the
    full retry cap as generic no_handshake failures. Catch it before launch;
    the returned message
    deliberately matches `_is_agent_auth_failure_message` so the workflow
    blocks as an auth outage instead of retrying.

    Only container backends are affected. The default worktree backend runs
    Claude on the host with the intact refresh token, so the CLI refreshes a
    near-expired access token itself — gating on the backend avoids failing a
    perfectly recoverable local run.
    """
    resolved_backend = backend if backend is not None else _resolve_execution_backend()
    if getattr(getattr(resolved_backend, "identity", None), "backend", "") != "container":
        return ""
    src = (
        source_credentials
        if source_credentials is not None
        else Path.home() / ".claude" / ".credentials.json"
    )
    if not src.exists():
        # macOS keychain setups have no credentials file; absence is not
        # evidence of expiry.
        return ""
    try:
        payload = json.loads(src.read_text())
    except (OSError, ValueError):
        return ""
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        return ""
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)) or expires_at <= 0:
        return ""
    remaining = expires_at / 1000.0 - time.time()
    if remaining <= CLAUDE_CREDENTIALS_PREFLIGHT_MARGIN_SECONDS:
        return (
            "Claude agent credentials are not authenticated: the host OAuth access token "
            f"in {src} is expired (or expires within "
            f"{CLAUDE_CREDENTIALS_PREFLIGHT_MARGIN_SECONDS}s). Container workers cannot "
            "refresh it. Re-authenticate on the host (`claude /login`) and resume."
        )
    if remaining < CLAUDE_CREDENTIALS_EXPIRY_WARN_SECONDS:
        logger.warning(
            "Host Claude OAuth token expires in %.0f minutes; a long agent session may "
            "die mid-run with a silent 401. Consider re-authenticating first.",
            remaining / 60,
        )
    return ""


def _is_agent_auth_failure_message(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    auth_markers = (
        "not logged in",
        "please run /login",
        "run /login",
        "not authenticated",
        "authentication required",
        "missing auth",
        "login required",
        "failed to authenticate",
        "invalid authentication credentials",
        "401 unauthorized",
        "oauth access token has expired",
    )
    agent_markers = (
        "agent",
        "codex",
        "claude",
        "no_handshake",
        "handshake",
    )
    if "not logged in" in text and "/login" in text:
        return True
    # Current Claude CLI phrasing carries no agent marker in the stream line
    # itself ("Failed to authenticate. API Error: 401 Invalid authentication
    # credentials"), so recognize that form directly.
    if "failed to authenticate" in text and (
        "401" in text or "invalid authentication credentials" in text
    ):
        return True
    return any(marker in text for marker in auth_markers) and any(marker in text for marker in agent_markers)


def _is_agent_capacity_failure_message(message: object) -> bool:
    """Return True when an agent provider says the account window is spent.

    This is deliberately narrower than generic HTTP/rate-limit detection. A
    transient 429 should keep using the ordinary retry policy; a session or
    usage window with an advertised reset time cannot recover by immediately
    relaunching the agent and must not consume convergence attempts.
    """
    text = str(message or "").strip().lower().replace("’", "'")
    if not text:
        return False
    markers = (
        "you've hit your session limit",
        "you have hit your session limit",
        "you've reached your usage limit",
        "you have reached your usage limit",
        "session usage limit reached",
        "weekly usage limit reached",
    )
    return any(marker in text for marker in markers)


def _agent_capacity_retry_delay_seconds(
    message: object,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return the provider-window delay, or None for a non-capacity error.

    Claude currently reports values such as ``resets 8:30pm
    (America/Los_Angeles)``. Parse that when available and retain a bounded
    five-minute fallback for wording without a timestamp.
    """
    if not _is_agent_capacity_failure_message(message):
        return None
    text = str(message or "").strip().replace("’", "'")
    match = re.search(
        r"\bresets\s+(\d{1,2}):(\d{2})\s*(am|pm)(?:\s*\(([^)]+)\))?",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return AGENT_CAPACITY_DEFAULT_BACKOFF_SECONDS

    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    minute = int(match.group(2))
    if minute > 59:
        return AGENT_CAPACITY_DEFAULT_BACKOFF_SECONDS

    timezone_name = (match.group(4) or "UTC").strip()
    try:
        reset_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        reset_tz = timezone.utc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(reset_tz)
    reset = local_now.replace(hour=hour, minute=minute, second=5, microsecond=0)
    if reset <= local_now:
        reset += timedelta(days=1)
    delay = (reset - local_now).total_seconds()
    return min(max(delay, 1.0), AGENT_CAPACITY_MAX_BACKOFF_SECONDS)


def _is_forge_auth_failure_message(message: object) -> bool:
    """Return True for _check_forge_auth's failure surfaced as a phase error.

    A forge auth outage cannot be fixed by re-running agents; retrying burns
    the cap on identical infrastructure failures.
    """
    text = str(message or "").strip().lower()
    if not text:
        return False
    return "github cli not authenticated" in text or (
        "not authenticated" in text and "gh auth login" in text
    )


def _is_verify_environment_failure_message(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    setup_markers = (
        "could not prepare verify test environment",
        "verify environment preparation failed",
        "prepare-environment",
        "verify-environment",
    )
    infra_markers = (
        "postgres binaries not found",
        "local_postgres.sh",
        "missing executable",
        "command not found",
        "no such file or directory",
        "failed to prepare",
        "bootstrap failed",
        # DB death detected mid-execution rather than at env prep: retrying
        # implement against a dead database burns the cap without progress.
        # Kept conjunctive with the "gate '" context below so a
        # passing test that merely mentions these strings does not classify.
        "psycopg2.operationalerror",
        "psycopg.operationalerror",
        "server closed the connection unexpectedly",
        "is the server running on",
    )
    return any(marker in text for marker in setup_markers) or (
        "gate '" in text and any(marker in text for marker in infra_markers)
    )


def redact_sensitive(text: str) -> str:
    """Replace known sensitive patterns with <REDACTED>."""
    for pat in SENSITIVE_PATTERNS:
        text = pat.sub("<REDACTED>", text)
    return text


def _redact_unparseable_command(command: str) -> str:
    """Best-effort argv-aware redaction when ``shlex.split`` cannot tokenize.

    Used when ``implement.setup_command`` has malformed quoting (unclosed
    quote, etc.) — the normal argv redaction path assumes a proper split.
    Here the command is split on whitespace and each token scrubbed by
    :func:`_redact_argv`. To stop a run-on secret from leaking across the
    missing quote boundary (e.g. ``--access-token "tok secret`` splits
    into ``['--access-token', '"tok', 'secret']`` and ``_redact_argv``
    would only redact ``'"tok'``), any non-flag token following a
    sensitive flag is also replaced with ``<REDACTED>`` until the next
    ``-``-prefixed token appears. Both bare forms (``--password secret``)
    and attached forms whose value opened an unmatched quote
    (``--password="secret tail``, ``-p"secret tail``) are caught — for
    the attached forms the first token is redacted by :func:`_redact_argv`
    but the spillover tokens carry the value tail.
    """
    tokens = command.split()
    if not tokens:
        return redact_sensitive(command)
    base = _redact_argv(tokens)
    out: list[str] = []
    redact_until_flag = False
    for original, redacted in zip(tokens, base, strict=True):
        if redact_until_flag:
            if original.startswith("-"):
                redact_until_flag = False
            else:
                out.append("<REDACTED>")
                continue
        out.append(redacted)
        bare = _ARGV_BARE_FLAG.match(original)
        if bare and _is_argv_credential_flag(bare.group(1)):
            redact_until_flag = True
            continue
        if (
            len(original) == 2
            and original[0] == "-"
            and original[1] != "-"
            and original[1] in _SENSITIVE_SHORT_FLAGS
        ):
            redact_until_flag = True
            continue
        # Attached long-flag form (``--password=…``/``--password:…``) whose
        # value opened a quote that never closed inside this whitespace
        # token. The value tail spilled into the following tokens, so keep
        # redacting until the next ``-``-prefixed token appears.
        flag_match = _ARGV_FLAG_WITH_VALUE.match(original)
        if flag_match and _is_argv_credential_flag(flag_match.group(2)):
            if _has_unterminated_quote(flag_match.group(3)):
                redact_until_flag = True
            continue
        # Attached short-flag form (``-p"secret tail``, ``-psecret tail``)
        # with a sensitive single-letter flag and an unterminated quote in
        # the value portion. Same spillover hazard as the long form above.
        if (
            len(original) > 2
            and original[0] == "-"
            and original[1] != "-"
            and original[1] in _SENSITIVE_SHORT_FLAGS
        ):
            sep = original[2]
            value_tail = original[3:] if sep in ("=", ":") else original[2:]
            if _has_unterminated_quote(value_tail):
                redact_until_flag = True
    return " ".join(out)


def _has_unterminated_quote(text: str) -> bool:
    """Return True when ``text`` contains an unmatched ``'`` or ``\"``.

    The check is intentionally naive (odd count of either quote
    character) — the caller is already in the ``shlex.split`` failure
    path, so any dangling quote is strong evidence the value tail has
    spilled into the next whitespace token.
    """
    return text.count('"') % 2 == 1 or text.count("'") % 2 == 1


def _redact_argv(argv: Sequence[str]) -> list[str]:
    """Return ``argv`` with sensitive elements scrubbed per-element.

    Applying :func:`redact_sensitive` after ``shlex.join`` can leave tails
    of quoted values intact (the flag regex only consumes ``\\S+``). Here
    each element is treated as one atomic value across several argv
    shapes:

    * ``--flag=value`` / ``--flag:value`` — value replaced when the flag
      name is a sensitive long option. Suffix-aware matching via
      :func:`_is_argv_credential_flag` catches split compound names such
      as ``--github-token``, ``--api-token``, ``--auth-token`` as well as
      userinfo-bearing flags (``--user``, ``--username``, ``--login``,
      ``--netrc-file``) that conventionally carry ``user:password`` pairs
      across curl/psql/mysql and similar CLIs.
    * Bare ``--password`` / ``--github-token`` / ``--user`` — redacts the
      following element (same matching as the ``--flag=value`` form).
    * Short options: ``-p``, ``-phunter2``, ``-p=hunter2``,
      ``-u alice:secret`` — value replaced for a small set of
      single-letter flags that commonly carry passwords or userinfo
      pairs across well-known CLIs.
    * Env-assignment prefix ``NAME=value`` — value replaced when NAME is
      a sensitive environment variable (``PGPASSWORD``, ``GITHUB_TOKEN``,
      ``DATABASE_PASSWORD``, ``STRIPE_API_KEY`` …).

    The joined command is safe to echo into failure diagnostics and
    logs. Redactions are defensive: a value is only ever reported to the
    agent, never re-executed.
    """
    out: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("<REDACTED>")
            redact_next = False
            continue
        match = _ARGV_FLAG_WITH_VALUE.match(arg)
        if match and _is_argv_credential_flag(match.group(2)):
            out.append(f"{match.group(1)}<REDACTED>")
            continue
        bare = _ARGV_BARE_FLAG.match(arg)
        if bare and _is_argv_credential_flag(bare.group(1)):
            out.append(arg)
            redact_next = True
            continue
        # Short-option forms: ``-p``, ``-phunter2``, ``-p=hunter2``.
        if (
            len(arg) >= 2
            and arg[0] == "-"
            and arg[1] != "-"
            and arg[1] in _SENSITIVE_SHORT_FLAGS
        ):
            if len(arg) == 2:
                out.append(arg)
                redact_next = True
                continue
            sep = arg[2]
            if sep in ("=", ":"):
                out.append(f"-{arg[1]}{sep}<REDACTED>")
            else:
                out.append(f"-{arg[1]}<REDACTED>")
            continue
        # Env-assignment prefix (``NAME=value``). Only for tokens that
        # do not start with ``-`` so ``--flag=value`` is never reclassified.
        if not arg.startswith("-"):
            env_match = _ARGV_ENV_ASSIGNMENT.match(arg)
            if env_match and _is_sensitive_log_field(env_match.group(1)):
                out.append(f"{env_match.group(1)}=<REDACTED>")
                continue
        out.append(redact_sensitive(arg))
    return out


def _is_merge_conflict(error: str) -> bool:
    """Return True if the error message indicates a merge conflict."""
    error_lower = error.lower()
    # GitHub can reject merges because the base moved underneath us; those are
    # transient merge races, not content conflicts.
    if _is_retryable_merge_race_error(error):
        return False
    conflict_indicators = [
        "not mergeable",
        "merge conflict",
        "cannot be cleanly created",
    ]
    return any(ind in error_lower for ind in conflict_indicators)


def _is_required_checks_failure(error: str) -> bool:
    """Return True when merge failed because required checks are failing."""
    if not error:
        return False
    error_lower = error.lower()
    if "base branch policy" in error_lower:
        return True
    if "required checks failing:" in error_lower:
        return True
    return False


def _is_retryable_merge_race_error(error: str) -> bool:
    """Return True when GitHub rejected the merge because required state is settling."""
    if not error:
        return False
    error_lower = error.lower()
    indicators = (
        "base branch policy",
        "base branch was modified",
    )
    if any(indicator in error_lower for indicator in indicators):
        return True
    return (
        "repository rule violations found" in error_lower
        and "required status check" in error_lower
        and (
            " is expected" in error_lower
            or " is pending" in error_lower
            or " is queued" in error_lower
        )
    )


def _is_readiness_promotion_forge_failure(error: str) -> bool:
    """Return True when the forge failed the host-owned draft-to-ready mutation."""
    if not error:
        return False
    error_lower = error.lower()
    return "failed to mark pr #" in error_lower and "as ready" in error_lower


def _is_auto_merge_pending_checks_timeout(error: str) -> bool:
    """Return True when the auto-merge wait expired with checks still pending.

    Auto-merge stays armed on the PR in this state, so the merge phase can
    simply be retried; failing the workflow would strand a run whose PR is
    about to merge itself once CI finishes.
    """
    if not error:
        return False
    error_lower = error.lower()
    return (
        "timed out waiting for auto-merge" in error_lower
        and "required checks still pending" in error_lower
    )


def _is_auto_merge_capability_error(error: str) -> bool:
    """Return True when gh cannot enable auto-merge for this repository."""
    if not error:
        return False
    error_lower = error.lower()
    indicators = (
        "auto-merge is disabled",
        "auto merge is disabled",
        "auto-merge is not enabled",
        "auto merge is not enabled",
    )
    if any(ind in error_lower for ind in indicators):
        return True
    # GitHub App / integration tokens may lack permission for the
    # enablePullRequestAutoMerge mutation even when direct squash merge works.
    return "resource not accessible by integration" in error_lower


def _is_draft_pr_error(error: str) -> bool:
    """Return True when the merge failed because the PR is a draft."""
    if not error:
        return False
    error_lower = error.lower()
    return "is a draft" in error_lower or "pull request is a draft" in error_lower


def _is_auto_merge_graphql_retryable_error(error: str) -> bool:
    """Return True when gh returned a transient auto-merge GraphQL mutation error."""
    if not error:
        return False
    if _is_auto_merge_capability_error(error):
        return False
    if _is_draft_pr_error(error):
        return False
    error_lower = error.lower()
    return "graphql:" in error_lower and "enablepullrequestautomerge" in error_lower


def _required_checks_from_error(error: str) -> list[str]:
    """Extract failing required-check labels from a merge error string."""
    marker = "required checks failing:"
    lowered = error.lower()
    idx = lowered.find(marker)
    if idx < 0:
        return []
    detail = error[idx + len(marker) :].strip()
    if not detail:
        return []
    return [part.strip() for part in detail.split(",") if part.strip()]


def _split_check_label(label: str) -> tuple[str, str]:
    """Split a decorated check label into name and link."""
    trimmed = label.strip()
    match = re.match(r"^(?P<name>.+?)\s+\((?P<link>https?://[^)]+)\)\s*$", trimmed)
    if not match:
        return trimmed, ""
    return match.group("name").strip(), match.group("link").strip()


def parse_spec_frontmatter(spec_path: Path) -> dict:
    """Parse YAML frontmatter from a spec file."""
    return load_spec_frontmatter(spec_path)


def _resolve_dependency_spec_path(repo_root: Path, dep: str) -> Path | None:
    """Return the spec file path for a dependency id, looking in the catalog
    then the task-specs dir. Returns None if neither exists."""
    catalog = _specs_root(repo_root) / f"{dep}.md"
    task = _task_specs_root(repo_root) / f"{dep}.md"
    if catalog.exists():
        return catalog
    if task.exists():
        return task
    return None


def check_dependencies_merged(repo_root: Path, spec_id: str) -> list[str]:
    """Return list of blocker strings for invalid or unmet dependencies."""
    spec_path = _resolve_dependency_spec_path(repo_root, spec_id)
    if spec_path is None:
        spec_path = _specs_root(repo_root) / f"{spec_id}.md"
    fm = parse_spec_frontmatter(spec_path)
    deps = fm.get("depends_on", [])
    if deps is None:
        deps = []
    if isinstance(deps, str):
        deps = [deps]
    blockers = []
    for dep in deps:
        dep_path = _resolve_dependency_spec_path(repo_root, dep)
        if dep_path is None:
            blockers.append(f"{dep} (missing spec file)")
            continue
        dep_fm = parse_spec_frontmatter(dep_path)
        superseded_by = str(dep_fm.get("superseded_by", "")).strip()
        if superseded_by:
            blockers.append(f"{dep} (superseded by {superseded_by})")
            continue
        dep_status = read_spec_status(repo_root, dep, dep_path)
        if dep_status != "merged":
            blockers.append(f"{dep} ({dep_status})")
    return blockers


def _to_int(value: object, *, default: int) -> int:
    """Convert value to int with fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: object) -> bool:
    """Interpret common boolean-like values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered in ("1", "true", "yes", "y", "on", "required")
    return bool(value)


def _normalize_intake_input_type(raw_type: object) -> str:
    value = str(raw_type or "string").strip().lower()
    aliases = {
        "str": "string",
        "text": "string",
        "integer": "int",
        "number": "float",
        "double": "float",
        "boolean": "bool",
        "enum": "choice",
        "select": "choice",
    }
    normalized = aliases.get(value, value)
    if normalized not in INTAKE_INPUT_TYPES:
        raise ValueError(
            f"Unsupported intake input type '{raw_type}'. Expected one of: {', '.join(INTAKE_INPUT_TYPES)}"
        )
    return normalized


def parse_intake_spec(spec_path: Path) -> IntakeSpec:
    """Parse intake metadata from spec frontmatter."""
    fm = parse_spec_frontmatter(spec_path)
    intake_meta = fm.get("intake")
    intake_questions = fm.get("intake_questions")
    required = False
    source_mode = "none"
    schema_version = 1

    if isinstance(intake_meta, dict):
        mode = str(intake_meta.get("mode", "")).strip().lower()
        if "required" in intake_meta:
            required = _to_bool(intake_meta.get("required"))
        elif mode:
            required = mode in ("required", "true", "yes")
        else:
            required = True
        source_mode = mode or ("required" if required else "optional")
        schema_version = _to_int(intake_meta.get("schema_version"), default=1)
        if intake_questions is None:
            intake_questions = intake_meta.get("questions")
    elif isinstance(intake_meta, str):
        lowered = intake_meta.strip().lower()
        if lowered in ("required", "true", "yes", "interactive"):
            required = True
            source_mode = "required"
        elif lowered in ("optional", "false", "no", "none", "disabled"):
            required = False
            source_mode = lowered or "none"
        else:
            raise ValueError(
                f"Invalid intake mode '{intake_meta}' in {spec_path.name}. Use 'required', 'optional', or a mapping."
            )
    elif isinstance(intake_meta, bool):
        required = intake_meta
        source_mode = "required" if required else "none"
    elif intake_meta is None:
        required = False
        source_mode = "none"
    else:
        raise ValueError(f"Invalid intake metadata type '{type(intake_meta).__name__}' in {spec_path.name}")

    if not required:
        return IntakeSpec(required=False, schema_version=1, questions=[], source_mode=source_mode)

    if intake_questions is None:
        raise ValueError(f"Spec '{spec_path.name}' sets intake as required but defines no questions.")
    if not isinstance(intake_questions, list):
        raise ValueError(f"Spec '{spec_path.name}' intake questions must be a list.")

    questions: list[IntakeQuestion] = []
    seen_ids: set[str] = set()
    for idx, raw_question in enumerate(intake_questions, start=1):
        if not isinstance(raw_question, dict):
            raise ValueError(f"Spec '{spec_path.name}' intake question #{idx} must be an object.")
        qid = str(raw_question.get("id", "")).strip()
        if not qid:
            raise ValueError(f"Spec '{spec_path.name}' intake question #{idx} is missing id.")
        if qid in seen_ids:
            raise ValueError(f"Spec '{spec_path.name}' intake question id '{qid}' is duplicated.")
        seen_ids.add(qid)

        prompt = str(raw_question.get("prompt", "")).strip()
        if not prompt:
            raise ValueError(f"Spec '{spec_path.name}' intake question '{qid}' is missing prompt.")

        raw_type = (
            raw_question.get("expected_input_type")
            or raw_question.get("input_type")
            or raw_question.get("type")
            or "string"
        )
        input_type = _normalize_intake_input_type(raw_type)
        question_required = _to_bool(raw_question.get("required", True))
        default_value = raw_question.get("default")

        constraints: dict = {}
        raw_constraints = raw_question.get("constraints")
        if isinstance(raw_constraints, dict):
            constraints.update(raw_constraints)
        for key in ("options", "min", "max", "min_length", "max_length", "pattern"):
            if key in raw_question:
                constraints[key] = raw_question[key]

        if input_type == "choice":
            options = constraints.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError(
                    f"Spec '{spec_path.name}' intake question '{qid}' must define non-empty options for choice input."
                )
            constraints["options"] = [str(opt).strip() for opt in options]
        if "pattern" in constraints:
            try:
                re.compile(str(constraints["pattern"]))
            except re.error as exc:
                raise ValueError(f"Spec '{spec_path.name}' intake question '{qid}' has invalid pattern.") from exc

        questions.append(
            IntakeQuestion(
                id=qid,
                prompt=prompt,
                input_type=input_type,
                required=question_required,
                constraints=constraints,
                default=default_value,
            )
        )

    return IntakeSpec(
        required=True,
        schema_version=max(1, schema_version),
        questions=questions,
        source_mode=source_mode or "required",
    )


def _format_intake_prompt(question: IntakeQuestion) -> str:
    details = [f"type={question.input_type}"]
    if not question.required:
        details.append("optional")
    if question.default is not None:
        details.append(f"default={question.default}")
    options = question.constraints.get("options")
    if isinstance(options, list) and options:
        details.append(f"options={', '.join(str(opt) for opt in options)}")
    return f"{question.prompt} ({'; '.join(details)}): "


def _coerce_and_validate_intake_answer(raw_answer: str, question: IntakeQuestion) -> object | None:
    value = raw_answer.strip()
    if value == "":
        if question.default is not None:
            answer: object | None = question.default
        elif question.required:
            raise ValueError("a value is required")
        else:
            return None
    elif question.input_type == "string":
        answer = value
    elif question.input_type == "int":
        try:
            answer = int(value)
        except ValueError as exc:
            raise ValueError("expected an integer") from exc
    elif question.input_type == "float":
        try:
            answer = float(value)
        except ValueError as exc:
            raise ValueError("expected a number") from exc
    elif question.input_type == "bool":
        lowered = value.lower()
        if lowered in ("true", "t", "yes", "y", "1"):
            answer = True
        elif lowered in ("false", "f", "no", "n", "0"):
            answer = False
        else:
            raise ValueError("expected true/false, yes/no, or 1/0")
    elif question.input_type == "choice":
        options = question.constraints.get("options", [])
        if not isinstance(options, list) or not options:
            raise ValueError("no options configured")
        if value.isdigit():
            idx = int(value) - 1
            if 0 <= idx < len(options):
                answer = str(options[idx])
            else:
                raise ValueError(f"pick an option between 1 and {len(options)}")
        else:
            exact = [opt for opt in options if str(opt) == value]
            if exact:
                answer = str(exact[0])
            else:
                lowered_matches = [str(opt) for opt in options if str(opt).lower() == value.lower()]
                if len(lowered_matches) == 1:
                    answer = lowered_matches[0]
                else:
                    raise ValueError(f"expected one of: {', '.join(str(opt) for opt in options)}")
    else:
        raise ValueError(f"unsupported input type {question.input_type}")

    min_value = question.constraints.get("min")
    max_value = question.constraints.get("max")
    if isinstance(answer, (int, float)):
        if min_value is not None and float(answer) < float(min_value):
            raise ValueError(f"value must be >= {min_value}")
        if max_value is not None and float(answer) > float(max_value):
            raise ValueError(f"value must be <= {max_value}")

    if isinstance(answer, str):
        min_len = question.constraints.get("min_length")
        max_len = question.constraints.get("max_length")
        pattern = question.constraints.get("pattern")
        if min_len is not None and len(answer) < int(min_len):
            raise ValueError(f"value length must be >= {min_len}")
        if max_len is not None and len(answer) > int(max_len):
            raise ValueError(f"value length must be <= {max_len}")
        if pattern is not None and not re.fullmatch(str(pattern), answer):
            raise ValueError(f"value must match pattern '{pattern}'")

    return answer


def _validate_intake_answers(
    intake_spec: IntakeSpec,
    answers: dict,
) -> list[str]:
    """Validate stored intake answers against the current intake schema."""
    if not isinstance(answers, dict):
        return ["answers payload is not an object"]

    errors: list[str] = []
    for question in intake_spec.questions:
        if question.id not in answers:
            if question.required:
                errors.append(f"{question.id}: missing required answer")
            continue
        value = answers.get(question.id)
        raw = ""
        if value is None:
            raw = ""
        elif isinstance(value, bool):
            raw = "true" if value else "false"
        else:
            raw = str(value)
        try:
            _coerce_and_validate_intake_answer(raw, question)
        except ValueError as exc:
            errors.append(f"{question.id}: {exc}")
    return errors


def _pending_intake_questions(intake_spec: IntakeSpec, answers: dict | None) -> list[str]:
    if not isinstance(answers, dict):
        return [question.id for question in intake_spec.questions]
    pending = []
    for question in intake_spec.questions:
        if question.required and question.id not in answers:
            pending.append(question.id)
    return pending


def _audit_intake_reset(
    repo_root: Path,
    run: RunState,
    previous_payload: dict,
    reason: str,
) -> None:
    audit_dir = _state_root(repo_root) / "orchestrator"
    audit_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    payload = {
        "event": "intake_reset",
        "spec_id": run.spec_id,
        "run_id": run.run_id,
        "phase": "intake",
        "reason": reason,
        "recorded_at": _now_iso(),
        "previous_intake": previous_payload,
    }
    (audit_dir / f"{run.run_id}-intake-reset-{ts}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def _poll_sleep(seconds: float) -> None:
    """Wait between poll-loop iterations.

    Dedicated seam for every wait the orchestrator performs itself, so tests can
    observe and neutralize orchestrator waiting without patching process-global
    ``time.sleep``.

    Patching ``time.sleep`` is not a usable seam for assertions: it is the same
    object for the whole process, and ``subprocess.Popen.wait(timeout=...)``
    busy-waits on it (``delay = min(delay * 2, remaining, .05)``). Any phase that
    shells out with a timeout — ``phase_merge`` always does, via the completion
    fence's ``git fetch`` — therefore lands tens to thousands of stdlib sleeps in
    the same counter, and how many depends only on how fast the runner reaps the
    child. That makes sleep-count assertions measure the machine instead of the
    code.

    Calling ``time.sleep`` here (rather than aliasing it at import) keeps the
    existing ``patch.object(orch.time, "sleep")`` neutralization working for
    tests that only need the orchestrator not to wait for real.
    """
    time.sleep(seconds)


def run_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: int | None = None,
    *,
    inherit_env: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with optional env inheritance and timeout."""
    _raise_if_active_phase_lease_lost()
    merged_env = {**os.environ, **(env or {})} if inherit_env else dict(env or {})
    with _ACTIVE_PHASE_LEASE_FAILURE_LOCK:
        active_lease_failure = _ACTIVE_PHASE_LEASE_FAILURE
    if active_lease_failure is None:
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "env": merged_env,
            "capture_output": True,
            "text": True,
            "timeout": timeout,
        }
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        runner = run_supervised if timeout is not None else subprocess.run
        return runner(
            cmd,
            **kwargs,
        )

    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": merged_env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if input_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["stdin"] = subprocess.PIPE
    proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(cmd, **kwargs)
    started_at = time.monotonic()
    communicate_input = input_text
    while True:
        failure_message = _active_phase_lease_failure_message()
        if failure_message:
            proc.terminate()
            try:
                proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
            raise RuntimeError(failure_message)
        wait_timeout = 0.2
        if timeout is not None:
            remaining = float(timeout) - (time.monotonic() - started_at)
            if remaining <= 0:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
            wait_timeout = min(wait_timeout, remaining)
        try:
            stdout, stderr = proc.communicate(input=communicate_input, timeout=wait_timeout)
            failure_message = _active_phase_lease_failure_message()
            if failure_message:
                raise RuntimeError(failure_message)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            communicate_input = None
            continue


def _run_local_review_subprocess(
    repo_root: Path,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    artifact_paths: dict[str, Path] | None = None,
) -> subprocess.CompletedProcess:
    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    proc: subprocess.Popen[str] | None = None
    try:
        proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(cmd, **popen_kwargs)
        process_identity = read_process_identity(proc.pid)
        process_started_at = process_identity.started_at if process_identity is not None else ""
        token = getattr(proc, "token", None)
        pgid = int(getattr(token, "pgid", 0) or 0)
        logger.info(
            "Started local review subprocess pid=%s pgid=%s cwd=%s timeout=%ss",
            proc.pid,
            pgid or "n/a",
            cwd,
            timeout,
        )
        if artifact_paths is not None:
            _write_local_review_process_diagnostics(
                repo_root,
                artifact_paths,
                payload={
                    "command": _redacted_local_review_command_preview(cmd),
                    "cwd": str(cwd),
                    "launched_at": _now_iso(),
                    "pgid": pgid,
                    "pid": proc.pid,
                    "process_started_at": process_started_at,
                    "prompt_path": str(artifact_paths["prompt"].relative_to(repo_root)),
                    "raw_review_path": str(artifact_paths["raw_review"].relative_to(repo_root)),
                    "timeout_seconds": timeout,
                },
            )
        _register_worktree_process_from_popen(
            repo_root,
            cwd,
            proc,
            name="local-review",
            kind="agent",
        )
        stdout_text, stderr_text = proc.communicate(timeout=timeout)
        signal_name = _signal_name_from_returncode(proc.returncode)
        if proc.returncode == 0:
            logger.info(
                "Local review subprocess pid=%s pgid=%s completed successfully",
                proc.pid,
                pgid or "n/a",
            )
        else:
            logger.warning(
                "Local review subprocess pid=%s pgid=%s exited with returncode=%s signal=%s",
                proc.pid,
                pgid or "n/a",
                proc.returncode,
                signal_name or "n/a",
            )
        if artifact_paths is not None:
            _write_local_review_process_diagnostics(
                repo_root,
                artifact_paths,
                payload={
                    "completed_at": _now_iso(),
                    "returncode": proc.returncode,
                    "signal": signal_name,
                    "stderr_tail": _sanitize_gate_stream(
                        stderr_text,
                        max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
                        max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
                    ),
                    "stdout_tail": _sanitize_gate_stream(
                        stdout_text,
                        max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
                        max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
                    ),
                    "timed_out": False,
                },
            )
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            stdout_text,
            stderr_text,
        )
    except subprocess.TimeoutExpired as exc:
        pid = proc.pid if proc is not None else 0
        token = getattr(proc, "token", None) if proc is not None else None
        pgid = int(getattr(token, "pgid", 0) or 0)
        logger.warning(
            "Local review subprocess pid=%s pgid=%s timed out after %ss; terminating process group",
            pid or "n/a",
            pgid or "n/a",
            timeout,
        )
        if artifact_paths is not None:
            _write_local_review_process_diagnostics(
                repo_root,
                artifact_paths,
                payload={
                    "timed_out": True,
                    "timeout_detected_at": _now_iso(),
                    "timeout_seconds": timeout,
                    "stderr_tail": _sanitize_gate_stream(
                        _coerce_subprocess_stream_text(getattr(exc, "stderr", None)),
                        max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
                        max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
                    ),
                    "stdout_tail": _sanitize_gate_stream(
                        _coerce_subprocess_stream_text(getattr(exc, "stdout", None) or getattr(exc, "output", None)),
                        max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
                        max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
                    ),
                },
            )
        if proc is not None:
            _terminate_agent_process(proc)
            if artifact_paths is not None:
                _write_local_review_process_diagnostics(
                    repo_root,
                    artifact_paths,
                    payload={
                        "completed_at": _now_iso(),
                        "returncode": proc.returncode,
                        "signal": _signal_name_from_returncode(proc.returncode),
                    },
                )
        raise
    finally:
        _prune_registered_worktree_processes(repo_root, cwd)


def run_or_fail(
    run: RunState,
    cmd: list[str],
    *,
    cwd: Path | None = None,
    action: str,
) -> bool:
    """Run command and set run.last_error when it fails."""
    result = run_subprocess(cmd, cwd=cwd)
    if result.returncode == 0:
        return True
    detail = result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"exit code {result.returncode}"
    run.last_error = f"{action} failed: {detail}"
    return False


def _parse_exported_env(output: str) -> dict[str, str]:
    """Parse `export KEY=value` lines into an environment mapping."""
    parsed: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("export "):
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            continue
        if len(tokens) != 2 or tokens[0] != "export" or "=" not in tokens[1]:
            continue
        key, value = tokens[1].split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        parsed[key] = value
    return parsed


def _worktree_venv_executable_dir(
    worktree_path: Path,
    *,
    windows: bool | None = None,
) -> Path:
    """Return the platform's executable directory for a worktree virtualenv."""
    use_windows = os.name == "nt" if windows is None else windows
    return worktree_path / ".venv" / ("Scripts" if use_windows else "bin")


def _worktree_venv_python(
    worktree_path: Path,
    *,
    windows: bool | None = None,
) -> Path:
    executable = "python.exe" if (os.name == "nt" if windows is None else windows) else "python"
    return _worktree_venv_executable_dir(worktree_path, windows=windows) / executable


@contextmanager
def _inject_worktree_venv_into_env(env: dict[str, str], worktree_path: Path) -> None:
    """Prefer the worktree virtualenv for implement-agent subprocesses."""
    venv_dir = worktree_path / ".venv"
    venv_bin = _worktree_venv_executable_dir(worktree_path)

    current_path = env.get("PATH") or os.environ.get("PATH", "")
    path_entries = [entry for entry in current_path.split(os.pathsep) if entry]
    venv_bin_str = str(venv_bin)
    path_entries = [entry for entry in path_entries if entry != venv_bin_str]
    path_entries.insert(0, venv_bin_str)
    env["PATH"] = os.pathsep.join(path_entries)
    env["VIRTUAL_ENV"] = str(venv_dir)


def _enforce_playwright_browser_pin(
    mcp_servers: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Guarantee the Playwright MCP entry carries the configured ``--browser``.

    The pin is applied at each individual source (defaults, persisted backend
    state), but those sources are *merged* before launch and later sources
    overwrite earlier ones wholesale. A single unpinned contributor anywhere in
    that chain is therefore enough to launch the MCP on the chrome *channel*,
    which worker images lack; its installer escalates via ``su root`` and hangs
    forever with no tty. Both the persisted backend state and the defaults can
    carry the pin while the merged launch config does not.

    Enforce it once, last, on the merged result — the only place that
    corresponds to what the agent actually runs.
    """
    configured_browser = SPEC_RUNTIME_CONFIG.execution.container.playwright_mcp.browser
    if not configured_browser:
        return mcp_servers
    server = mcp_servers.get("playwright")
    if not isinstance(server, dict):
        return mcp_servers
    args = server.get("args")
    if not isinstance(args, list):
        return mcp_servers
    rendered = [str(item) for item in args]
    if "--browser" in rendered:
        return mcp_servers
    if not any(str(item).endswith("cli.js") or "@playwright/mcp" in str(item) for item in rendered):
        # A user- or manifest-supplied server under the "playwright" key that is
        # not the Playwright MCP CLI; do not inject flags it may not accept.
        return mcp_servers
    patched = dict(server)
    patched["args"] = [*rendered, "--browser", configured_browser]
    updated = dict(mcp_servers)
    updated["playwright"] = patched
    return updated


def _default_claude_mcp_servers(worktree_path: Path) -> dict[str, dict[str, object]]:
    """Return the default MCP servers available to Claude sessions."""
    servers: dict[str, dict[str, object]] = {}
    playwright_cli = worktree_path / "frontend" / "node_modules" / "@playwright" / "mcp" / "cli.js"
    if playwright_cli.is_file():
        args = [str(playwright_cli), "--headless"]
        # Pin the browser here, not only when reconciling persisted backend
        # state: these defaults are what an agent actually launches
        # whenever the backend-state lookup misses (e.g. a bind-mode run whose
        # worktree path is not the workspace source dir). Without the pin,
        # @playwright/mcp falls back to the chrome *channel*, which worker
        # images lack, and its installer escalates via `su root` and hangs
        # forever with no tty.
        configured_browser = SPEC_RUNTIME_CONFIG.execution.container.playwright_mcp.browser
        if configured_browser:
            args.extend(["--browser", configured_browser])
        servers["playwright"] = {
            "command": shutil.which("node") or "node",
            "args": args,
        }
    return servers


def _worker_mcp_servers_for_container(
    mcp_servers: dict[str, dict[str, object]],
    worktree_path: Path,
) -> dict[str, dict[str, object]]:
    """Translate worktree-local MCP argv paths for containerized agents."""
    if SPEC_RUNTIME_CONFIG.execution.backend != "container":
        return mcp_servers
    translated: dict[str, dict[str, object]] = {}
    host_prefix = worktree_path.resolve().as_posix()
    container_prefix = "/workspace/source"
    for name, server in mcp_servers.items():
        copied = dict(server)
        args = copied.get("args")
        if isinstance(args, list):
            copied["args"] = [
                ContainerExecutionBackend._translate_container_paths(
                    str(item),
                    [(host_prefix, container_prefix), (worktree_path.as_posix(), container_prefix)],
                )
                for item in args
            ]
        translated[name] = copied
    return translated


def _backend_state_path_for_workspace(worktree_path: Path) -> Path | None:
    """Locate the container backend state for *worktree_path*.

    The state lives next to the workspace source directory
    (``.spec-workspaces/<run-id>/backend-state``), so resolving it from the
    worktree only works when the worktree *is* that source dir. Other layouts
    (a bind-mode run whose worktree is ``.worktrees/<branch>``) missed and
    silently produced an empty server set, dropping whatever the backend had
    configured. Check the plausible roots instead of assuming one.
    """
    seen: set[Path] = set()
    candidates = [worktree_path.parent, worktree_path, *worktree_path.parents[1:3]]
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        candidate = base / "backend-state" / "container-backend-state.json"
        if candidate.is_file():
            return candidate
    return None


def _backend_mcp_servers_for_workspace(worktree_path: Path) -> dict[str, dict[str, object]]:
    """Return MCP servers supplied by the active execution backend, if any."""
    state_path = _backend_state_path_for_workspace(worktree_path)
    if state_path is None:
        if SPEC_RUNTIME_CONFIG.execution.backend == "container":
            # Silence here is what made a dropped --browser pin so hard to
            # trace: the launch simply proceeded without the backend's servers.
            logger.warning(
                "No container backend state found near %s; backend-supplied MCP "
                "servers will be omitted from this launch.",
                worktree_path,
            )
        return {}
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read container backend state %s: %s", state_path, exc)
        return {}
    playwright_mcp = state.get("playwright_mcp", {})
    if not isinstance(playwright_mcp, dict):
        return {}
    if playwright_mcp.get("topology") == "sidecar":
        sidecar_server = playwright_mcp.get("sidecar_mcp_server", {})
        if isinstance(sidecar_server, dict) and sidecar_server:
            return {"playwright": dict(sidecar_server)}
        return {}
    if (
        playwright_mcp.get("enabled") is False
        or playwright_mcp.get("topology") != "in-worker"
    ):
        return {}
    command = playwright_mcp.get("command")
    args = playwright_mcp.get("args", [])
    if not isinstance(command, str) or not command.strip():
        return {}
    if not isinstance(args, list):
        return {}
    host_prefix = worktree_path.resolve().as_posix()
    container_prefix = "/workspace/source"
    args = [str(item) for item in args]
    configured_browser = SPEC_RUNTIME_CONFIG.execution.container.playwright_mcp.browser
    if configured_browser and "--browser" not in args:
        # Backend state persisted before the chromium pin (for example,
        # long-lived workspaces resumed across orchestrator upgrades) lacks
        # --browser; launching the MCP without it re-opens the chrome-install
        # su-root hang. Reconcile at read time so every launch gets the pin.
        args.extend(["--browser", configured_browser])
    return {
        "playwright": {
            "command": command,
            "args": [
                ContainerExecutionBackend._translate_container_paths(
                    item,
                    [(host_prefix, container_prefix), (worktree_path.as_posix(), container_prefix)],
                )
                for item in args
            ],
        }
    }


def _write_claude_mcp_config(
    worktree_path: Path,
    *,
    extra_mcp_servers: dict[str, dict[str, object]] | None = None,
    host_subprocess: bool = False,
) -> None:
    """Write Claude's MCP server config without touching provider sandbox settings.

    Always merges in ``[mcp].allow_from_user`` passthrough at the end so the
    isolated config Claude reads with ``--strict-mcp-config`` includes any
    user-allowlisted servers in addition to the orchestrator defaults and the
    setup manifest's ``extra_mcp_servers``.

    When *host_subprocess* is True, skip the worktree→``/workspace/source`` path
    translation. Review and block-debugger Claude launches always run as host
    subprocesses (via ``_run_local_review_subprocess``) regardless of the
    globally configured execution backend, so their MCP commands must remain
    host paths or the agent will try to execute non-existent paths.
    """
    config_dir = worktree_path / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    mcp_servers = _default_claude_mcp_servers(worktree_path)
    if extra_mcp_servers:
        mcp_servers.update(extra_mcp_servers)
    user_passthrough = _user_mcp_servers_for_passthrough(
        "claude",
        SPEC_RUNTIME_CONFIG.mcp.allow_from_user,
    )
    if user_passthrough:
        mcp_servers.update(user_passthrough)
    if not host_subprocess:
        mcp_servers = _worker_mcp_servers_for_container(mcp_servers, worktree_path)
    mcp_servers = _enforce_playwright_browser_pin(mcp_servers)
    _mcp_config_path(worktree_path).write_text(
        json.dumps(
            {
                "mcpServers": mcp_servers,
            },
            indent=2,
        )
        + "\n"
    )


def _compute_non_interactive_mcp_servers(
    worktree_path: Path,
    *,
    agent_name: str,
    setup_manifest_servers: dict[str, dict[str, object]] | None = None,
    host_subprocess: bool = False,
) -> dict[str, dict[str, object]]:
    """Merge the MCP server set visible to a non-interactive agent session.

    Order (later wins): ``_default_claude_mcp_servers`` →
    ``setup_manifest.mcp_servers`` → ``[mcp].allow_from_user`` passthrough.
    Container path translation is applied at the end so the result is safe
    to write to either ``.claude/mcp-servers.json`` or the Codex isolated
    ``config.toml``.

    When *host_subprocess* is True, skip the container path translation. The
    review and block-debugger code paths launch their agents as host
    subprocesses (via ``_run_local_review_subprocess``) even when the globally
    configured execution backend is ``container``, so the MCP commands they
    consume must remain host paths.
    """
    mcp_servers: dict[str, dict[str, object]] = {}
    mcp_servers.update(_default_claude_mcp_servers(worktree_path))
    if setup_manifest_servers:
        mcp_servers.update(setup_manifest_servers)
    user_passthrough = _user_mcp_servers_for_passthrough(
        agent_name,
        SPEC_RUNTIME_CONFIG.mcp.allow_from_user,
    )
    if user_passthrough:
        mcp_servers.update(user_passthrough)
    if host_subprocess:
        return _enforce_playwright_browser_pin(mcp_servers)
    return _enforce_playwright_browser_pin(
        _worker_mcp_servers_for_container(mcp_servers, worktree_path)
    )


def _user_codex_home() -> Path:
    """Return the user's real Codex home directory.

    Honors the operator's ``CODEX_HOME`` environment variable when set so that
    users with a non-default Codex home keep working auth and configuration in
    non-interactive sessions. Falls back to ``~/.codex`` when unset. The
    orchestrator sets ``CODEX_HOME`` on the subprocess env (not ``os.environ``)
    when launching the isolated session, so this still resolves to the
    operator's real home — never to the isolated one we are about to write.
    """
    env_value = os.environ.get("CODEX_HOME")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".codex"


def _user_mcp_servers_for_passthrough(
    agent_name: str,
    allow_from_user: tuple[str, ...] | Sequence[str],
) -> dict[str, dict[str, object]]:
    """Return the subset of user-declared MCP servers permitted by the allowlist.

    Reads ``<CODEX_HOME or ~/.codex>/config.toml`` for ``codex`` and
    ``~/.claude.json`` for ``claude``. Names absent from the user's config are
    silently skipped with a warning. Errors reading the user's file
    (missing/unreadable/malformed) yield an empty dict so non-interactive
    launches continue without the optional passthrough.
    """
    names = tuple(name for name in allow_from_user if name)
    if not names:
        return {}

    if agent_name == "codex":
        source = _user_codex_home() / "config.toml"
        try:
            payload = tomllib.loads(source.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning(
                "Could not read Codex MCP user config %s for [mcp].allow_from_user passthrough: %s",
                source,
                exc,
            )
            return {}
        servers = payload.get("mcp_servers", {})
        if not isinstance(servers, dict):
            return {}
    elif agent_name == "claude":
        source = Path.home() / ".claude.json"
        try:
            payload = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read Claude MCP user config %s for [mcp].allow_from_user passthrough: %s",
                source,
                exc,
            )
            return {}
        servers = payload.get("mcpServers", {})
        if not isinstance(servers, dict):
            return {}
    else:
        return {}

    result: dict[str, dict[str, object]] = {}
    for name in names:
        entry = servers.get(name)
        if not isinstance(entry, dict):
            logger.warning(
                "Skipping [mcp].allow_from_user entry %r: name not present in %s",
                name,
                source,
            )
            continue
        result[name] = dict(entry)
    return result


def _write_codex_isolated_home(
    worktree_path: Path,
    *,
    mcp_servers: dict[str, dict[str, object]] | None,
    source_home: Path | None = None,
    copy_auth: bool = False,
) -> Path:
    """Materialize the per-worktree isolated ``CODEX_HOME``.

    Writes ``config.toml`` containing only the supplied MCP servers and (by
    default) symlinks ``auth.json`` from ``source_home`` (default ``~/.codex``)
    so the Codex CLI keeps its credentials. The directory is created if missing
    and the config file is rewritten on every call so it never goes stale. A
    missing source ``auth.json`` is logged as a warning but does not raise —
    Codex will surface the auth issue itself if it tries to use the network.

    When *copy_auth* is true, copy the auth file instead of symlinking. This
    is required for container backends: the worker bind-mounts the worktree
    into ``/workspace/source`` but not the host's ``~/.codex``, so an absolute
    symlink target is unreachable inside the container.
    """
    home = codex_isolated_home(worktree_path)
    home.mkdir(parents=True, exist_ok=True)

    # Make the directory self-ignoring so a copied auth.json (or any other
    # runtime state) cannot appear as untracked worktree content in repos
    # whose top-level .gitignore predates this change. A `.gitignore`
    # containing `*` here ignores every file in the directory, including
    # itself, regardless of the parent repo's .gitignore.
    (home / ".gitignore").write_text("*\n")

    config_body = _render_codex_mcp_toml(mcp_servers or {})
    (home / "config.toml").write_text(config_body)

    src_home = source_home if source_home is not None else Path.home() / ".codex"
    src_auth = src_home / "auth.json"
    dst_auth = home / "auth.json"
    if src_auth.exists():
        try:
            if dst_auth.is_symlink() or dst_auth.exists():
                dst_auth.unlink()
            if copy_auth:
                shutil.copy2(src_auth, dst_auth)
            else:
                dst_auth.symlink_to(src_auth)
        except OSError as exc:
            logger.warning(
                "Could not materialize Codex auth.json in isolated home %s: %s",
                home,
                exc,
            )
    else:
        logger.warning(
            "Codex source home %s is missing auth.json; the isolated session "
            "may fail to authenticate at runtime.",
            src_home,
        )
    return home


def _subprocess_env_with_codex_home(
    env: dict[str, str] | None,
    codex_home: Path,
) -> dict[str, str]:
    """Return a copy of ``env`` (or ``os.environ``) with ``CODEX_HOME`` set."""
    if env is None:
        merged = dict(os.environ)
    else:
        merged = dict(env)
    merged["CODEX_HOME"] = str(codex_home)
    return merged


def _write_claude_isolated_home(
    worktree_path: Path,
    *,
    source_config: Path | None = None,
    source_credentials: Path | None = None,
) -> Path:
    """Materialize the per-worktree ``HOME`` used by containerized Claude.

    Claude Code stores account/config state in ``~/.claude.json``. Container
    workers do not mount the operator's home directory, so copy that file into
    a worktree-local home and point ``HOME`` there for Claude launches. The
    home self-ignores all contents so copied credentials cannot become
    untracked repository files.

    On Linux the OAuth credentials live in ``~/.claude/.credentials.json``
    (macOS uses the keychain instead), so that file is copied in as well —
    with ``refreshToken`` stripped. Anthropic refresh tokens are single-use:
    if the worker refreshed, it would rotate and thereby invalidate the
    operator's host session. The access token alone is sufficient for
    phase-length sessions and its copy expires harmlessly.
    """
    home = claude_isolated_home(worktree_path)
    # Every level of the home lives inside the agent-writable worktree, so a
    # prior attempt could have replaced any of them with a symlink to
    # redirect later secret writes. Replace non-directories (including
    # symlinks to directories) with real directories before writing anything.
    # Staging always runs before the agent launches, so there is no live
    # writer to race with.
    for directory in (home, home / ".claude"):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
    (home / ".gitignore").write_text("*\n")

    src_config = source_config if source_config is not None else Path.home() / ".claude.json"
    dst_config = home / ".claude.json"
    if src_config.exists():
        try:
            if dst_config.is_symlink() or dst_config.exists():
                dst_config.unlink()
            shutil.copy2(src_config, dst_config)
        except OSError as exc:
            logger.warning(
                "Could not materialize Claude .claude.json in isolated home %s: %s",
                home,
                exc,
            )
    else:
        logger.warning(
            "Claude source config %s is missing; the isolated session may fail "
            "to authenticate at runtime.",
            src_config,
        )

    src_creds = (
        source_credentials
        if source_credentials is not None
        else Path.home() / ".claude" / ".credentials.json"
    )
    dst_creds = home / ".claude" / ".credentials.json"
    if src_creds.exists():
        try:
            payload = json.loads(src_creds.read_text())
            if isinstance(payload, dict):
                oauth = payload.get("claudeAiOauth")
                if isinstance(oauth, dict):
                    oauth.pop("refreshToken", None)
            # The destination lives inside the (agent-writable) worktree, so a
            # prior attempt could have left a symlink here to exfiltrate the
            # token. Unlink whatever exists, then create exclusively without
            # following symlinks so a race re-planting one fails the open
            # instead of redirecting the write.
            if dst_creds.is_symlink() or dst_creds.exists():
                dst_creds.unlink()
            fd = os.open(
                str(dst_creds),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(payload))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not materialize Claude credentials in isolated home %s: %s",
                home,
                exc,
            )
    else:
        # Expected on macOS, where credentials live in the keychain and
        # portable auth env vars (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY)
        # cover container launches instead.
        logger.debug(
            "Claude credentials file %s is missing; relying on portable auth "
            "env for containerized sessions.",
            src_creds,
        )
    return home


def _subprocess_env_with_home(
    env: dict[str, str] | None,
    home: Path,
) -> dict[str, str]:
    """Return a copy of ``env`` (or ``os.environ``) with ``HOME`` set."""
    if env is None:
        merged = dict(os.environ)
    else:
        merged = dict(env)
    merged["HOME"] = str(home)
    return merged


def _inject_container_claude_auth_env(env: dict[str, str]) -> tuple[str, ...]:
    """Restore portable Claude auth env for containerized launches.

    ``_build_implement_agent_env`` intentionally strips portable Claude auth
    from the baseline agent env. Container workers cannot read macOS
    keychain/OAuth state, so explicit auth environment variables are restored
    here only for containerized Claude launches.
    """
    redactions: list[str] = []
    for key in _CLAUDE_PORTABLE_AUTH_ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        env[key] = value
        redactions.append(value)
    return tuple(redactions)


def _codex_isolated_home_requires_auth_copy(backend: ExecutionBackend) -> bool:
    """Return True when ``auth.json`` must be copied (not symlinked) into the
    isolated ``CODEX_HOME``.

    Containerized worker backends bind-mount the worktree into the container
    but not the host's ``~/.codex``, so an absolute symlink target is broken
    inside the worker. Copy the auth file in that case so Codex can read it
    through the worktree bind mount. All other backends keep the symlink so
    Codex's atomic-rename token refreshes propagate back to the real home.
    """
    return not _backend_uses_provider_sandbox_config(backend)


def _sync_orchestrator_paths_into_workspace(
    backend: ExecutionBackend,
    worktree_path: Path,
    relative_paths: Sequence[str],
) -> None:
    """Propagate orchestrator-written host paths into the workspace.

    The container backend in volume mode seeds ``/workspace/source`` at
    ``prepare_workspace`` time. Files the orchestrator writes to the host
    worktree later (isolated homes, ``.claude/mcp-servers.json``) must be
    pushed into the volume before the agent launches or the agent will not see
    them. For bind-mode and non-container backends the host worktree already
    *is* the workspace, so this is a no-op.
    """
    sync_into_workspace = getattr(backend, "sync_host_paths_into_workspace", None)
    if not callable(sync_into_workspace):
        return
    try:
        sync_into_workspace(worktree_path, tuple(relative_paths))
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "Could not sync orchestrator paths %r into workspace %s: %s",
            list(relative_paths),
            worktree_path,
            exc,
        )


def _worktree_local_postgres_is_running(worktree_path: Path) -> bool:
    """Return True when the worktree-local Postgres cluster is already running."""
    postgres_script = worktree_path / "scripts" / "local_postgres.sh"
    if not postgres_script.is_file():
        return False
    result = run_subprocess([str(postgres_script), "status"], cwd=worktree_path)
    if result.returncode != 0:
        return False
    status_text = result.stdout.strip().lower()
    return "running" in status_text and "not running" not in status_text


@contextmanager
def _container_verify_test_environment(
    worktree_path: Path,
    backend: ExecutionBackend | None = None,
) -> Iterator[dict[str, str]]:
    """Test-gate env for container backends: never leak host DB URLs.

    The host-side `_with_verify_test_environment` provisions Postgres on the
    host and exports its `127.0.0.1:<port>` URL — unreachable from a worker
    container on a bridge network. Repo suites that skip DB-backed tests when
    the database is unavailable can then pass vacuously even though CI would
    fail the same head.

    Instead, probe the backend-managed service database from inside the
    worker. When it answers (sidecar topology, or an in-worker Postgres the
    image actually runs), omit the DB keys so the backend's service env flows
    into the gate via `setdefault`. When it does not, export the database URLs
    explicitly EMPTY — empty strings override the service-env defaults and
    make `make test`-style recipes provision their own database inside the
    container, where it is actually reachable.
    """
    test_env: dict[str, str] = {}
    _inject_worktree_venv_into_env(test_env, worktree_path)
    service_db_reachable = False
    probe = getattr(backend, "service_database_reachable", None)
    if callable(probe):
        try:
            service_db_reachable = bool(probe(worktree_path))
        except (OSError, RuntimeError):
            service_db_reachable = False
    if not service_db_reachable:
        for key in CONTAINER_SERVICE_POSTGRES_ENVS:
            test_env[key] = ""
    yield test_env


@contextmanager
def _with_verify_test_environment(
    worktree_path: Path,
    repo_root: Path | None = None,
) -> Iterator[dict[str, str]]:
    """Mirror `make test` env setup so retries use the same interpreter and DB."""
    test_env: dict[str, str] = {}
    _inject_worktree_venv_into_env(test_env, worktree_path)

    test_database_url = str(os.environ.get("SIM_TEST_DATABASE_URL") or os.environ.get("SIM_DATABASE_URL") or "").strip()
    if test_database_url:
        test_env["SIM_DATABASE_URL"] = test_database_url
        test_env["SIM_TEST_DATABASE_URL"] = test_database_url
        yield test_env
        return

    postgres_script = worktree_path / "scripts" / "local_postgres.sh"
    if not postgres_script.is_file():
        yield test_env
        return

    was_running = _worktree_local_postgres_is_running(worktree_path)
    start_result = run_subprocess([str(postgres_script), "start"], cwd=worktree_path)
    if start_result.returncode != 0:
        detail = _format_subprocess_failure(start_result)
        raise RuntimeError(f"Could not prepare verify test environment: local_postgres.sh start failed.\n{detail}")
    auto_started_postgres = not was_running and "Started local Postgres" in (start_result.stdout or "")
    if auto_started_postgres and repo_root is not None:
        _register_worktree_postgres_process(
            repo_root,
            worktree_path,
            name="verify-postgres",
        )

    try:
        url_result = run_subprocess([str(postgres_script), "url"], cwd=worktree_path)
        if url_result.returncode != 0:
            detail = _format_subprocess_failure(url_result)
            raise RuntimeError(f"Could not prepare verify test environment: local_postgres.sh url failed.\n{detail}")

        exports = _parse_exported_env(url_result.stdout or "")
        test_database_url = str(exports.get("SIM_TEST_DATABASE_URL") or exports.get("SIM_DATABASE_URL") or "").strip()
        if not test_database_url:
            raise RuntimeError(
                "Could not prepare verify test environment: "
                "local_postgres.sh url did not export SIM_TEST_DATABASE_URL "
                "or SIM_DATABASE_URL."
            )

        test_env["SIM_DATABASE_URL"] = test_database_url
        test_env["SIM_TEST_DATABASE_URL"] = test_database_url
        yield test_env
    finally:
        if auto_started_postgres:
            _stop_worktree_local_postgres(worktree_path)
            if repo_root is not None:
                _prune_registered_worktree_processes(repo_root, worktree_path)


def _stop_worktree_local_postgres(
    worktree_path: Path,
    *,
    strict: bool = False,
) -> None:
    """Stop the shared local Postgres cluster and optionally verify shutdown."""
    postgres_script = worktree_path / "scripts" / "local_postgres.sh"
    if not postgres_script.is_file():
        if strict:
            raise RuntimeError("Could not stop implement dev Postgres: scripts/local_postgres.sh not found.")
        return
    stop_result = run_subprocess([str(postgres_script), "stop"], cwd=worktree_path)
    if stop_result.returncode != 0:
        detail = redact_sensitive(tail_lines(stop_result.stderr or stop_result.stdout))
        if strict:
            raise RuntimeError(f"Failed to stop implement dev Postgres: {detail[-240:]}")
        logger.warning(
            "Failed to stop implement dev Postgres (non-blocking): %s",
            detail[-240:],
        )
        return
    if strict and _worktree_local_postgres_is_running(worktree_path):
        raise RuntimeError("Implement dev Postgres is still running after stop.")


def _count_sysv_shm_segments() -> int | None:
    """Return count of SysV shared-memory segments, or None when unavailable."""
    result = run_subprocess(["ipcs", "-m"])
    if result.returncode != 0:
        return None
    segment_count = 0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].isdigit():
            segment_count += 1
    return segment_count


def _run_shm_cleanup(repo_root: Path) -> subprocess.CompletedProcess:
    """Run shared-memory cleanup helper in orphan-only mode."""
    cleanup_script = repo_root / "scripts" / "cleanup_local_postgres.sh"
    if not cleanup_script.is_file():
        return subprocess.CompletedProcess(
            args=[str(cleanup_script), "--orphans-only"],
            returncode=1,
            stdout="",
            stderr=f"Cleanup script is missing: {cleanup_script}",
        )
    return run_subprocess([str(cleanup_script), "--orphans-only"], cwd=repo_root)


def _verify_preflight_shared_memory(run: RunState, repo_root: Path) -> bool:
    """Run a shared-memory preflight before verify gates."""
    before = _count_sysv_shm_segments()
    if before is None:
        logger.info("Skipping shared-memory preflight (ipcs unavailable)")
        return True
    if before < SHM_PREFLIGHT_CLEANUP_THRESHOLD:
        return True

    logger.warning(
        "High shared-memory segment usage detected (%d); running cleanup preflight",
        before,
    )
    cleanup = _run_shm_cleanup(repo_root)
    cleanup_tail = redact_sensitive(tail_lines(cleanup.stderr or cleanup.stdout))
    if cleanup.returncode != 0:
        logger.warning(
            "Shared-memory preflight cleanup failed (non-blocking): before=%d details=%s",
            before,
            cleanup_tail[-240:],
        )
        return True

    after = _count_sysv_shm_segments()
    if after is None:
        logger.info("Shared-memory preflight cleanup completed (post-check unavailable)")
        return True
    logger.info("Shared-memory preflight segment count: before=%d after=%d", before, after)
    if after >= SHM_PREFLIGHT_CLEANUP_THRESHOLD:
        logger.warning(
            "Shared-memory segment usage remains high after cleanup (count=%d). Proceeding with verify gates.",
            after,
        )
    return True


def _head_sha(worktree_path: Path) -> str | None:
    """Return HEAD SHA for a worktree, or None if unavailable."""
    result = run_subprocess(["git", "rev-parse", "HEAD"], cwd=worktree_path)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _worktree_dirty_files(worktree_path: Path) -> list[str]:
    """Return non-empty git status porcelain lines for a worktree."""
    result = run_subprocess(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _worktree_dirty_state(worktree_path: Path) -> tuple[list[str], bool, bool]:
    dirty_files = _worktree_dirty_files(worktree_path)
    has_staged_changes = False
    has_unstaged_changes = False
    for line in dirty_files:
        status = line[:2]
        if len(status) < 2:
            continue
        index_status, worktree_status = status
        if index_status not in {" ", "?"}:
            has_staged_changes = True
        if worktree_status != " " or status == "??":
            has_unstaged_changes = True
    return dirty_files, has_staged_changes, has_unstaged_changes


def _classify_implement_tree_status(
    *,
    has_new_commit: bool,
    has_staged_changes: bool,
    has_unstaged_changes: bool,
) -> str:
    parts: list[str] = []
    if has_new_commit:
        parts.append("new_commit")
    if has_staged_changes:
        parts.append("staged_changes")
    if has_unstaged_changes:
        parts.append("unstaged_changes")
    return "+".join(parts) if parts else "unchanged_tree"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(spec_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{spec_id}-{ts}"


def _run_timestamp_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _slugify_label(raw: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower())
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def _task_identifiers(label: str) -> tuple[str, str, str]:
    token = _run_timestamp_token()
    slug = _slugify_label(label)
    spec_id = f"task-{slug}-{token}"
    run_id = _run_id(spec_id)
    branch = f"task/{slug}-{token}"
    return spec_id, run_id, branch


def _spec_authoring_branch(spec_id: str) -> str:
    return f"{SPEC_AUTHORING_BRANCH_PREFIX}{spec_id}"


def _spec_authoring_worktree_path(repo_root: Path, spec_id: str) -> Path:
    return _worktrees_root(repo_root) / f"spec-{spec_id}"


def _spec_authoring_session_branch(token: str) -> str:
    return f"{SPEC_AUTHORING_SESSION_BRANCH_PREFIX}{token}"


def _spec_authoring_session_worktree_path(repo_root: Path, token: str) -> Path:
    return _worktrees_root(repo_root) / f"{SPEC_AUTHORING_SESSION_WORKTREE_PREFIX}{token}"


def _spec_authoring_session_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _spec_authoring_session_token_from_branch(branch: str) -> str | None:
    match = re.fullmatch(
        rf"{re.escape(SPEC_AUTHORING_SESSION_BRANCH_PREFIX)}([A-Za-z0-9][A-Za-z0-9-]*)",
        branch.strip(),
    )
    if match is None:
        return None
    return match.group(1)


def _spec_id_from_authoring_branch(branch: str) -> str | None:
    identity = authoring_branch_identity(branch)
    return identity.spec_id if identity is not None and identity.kind == "spec" else None


def _registered_worktrees(repo_root: Path) -> tuple[list[tuple[Path, str]], str]:
    result = run_subprocess(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        detail = redact_sensitive(tail_lines(result.stderr or result.stdout))
        return [], f"git worktree list failed: {detail[-240:]}"

    entries: list[tuple[Path, str]] = []
    current_path: Path | None = None
    current_branch = ""
    for raw_line in [*result.stdout.splitlines(), ""]:
        line = raw_line.strip()
        if not line:
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = None
            current_branch = ""
            continue
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
            continue
        if line.startswith("branch "):
            current_branch = line[len("branch ") :]
            if current_branch.startswith("refs/heads/"):
                current_branch = current_branch[len("refs/heads/") :]
    return entries, ""


def _registered_worktree_for_branch(
    repo_root: Path,
    branch: str,
) -> tuple[Path | None, str]:
    entries, error = _registered_worktrees(repo_root)
    if error:
        return None, error

    matches = [path for path, branch_name in entries if branch_name == branch]
    if not matches:
        return None, ""
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in matches)
        return None, f"Multiple worktrees are registered for branch '{branch}': {listed}"
    return matches[0], ""


def _registered_spec_authoring_session(
    repo_root: Path,
) -> tuple[Path | None, str, str]:
    entries, error = _registered_worktrees(repo_root)
    if error:
        return None, "", error

    matches = [
        (path, branch) for path, branch in entries if _spec_authoring_session_token_from_branch(branch) is not None
    ]
    if not matches:
        return None, "", ""
    if len(matches) > 1:
        listed = ", ".join(
            f"{branch} ({path})" for path, branch in sorted(matches, key=lambda item: (item[1], str(item[0])))
        )
        return (
            None,
            "",
            "Multiple anonymous spec authoring sessions are registered: "
            f"{listed}. Remove stale session worktrees and rerun `spec create`.",
        )
    path, branch = matches[0]
    return path, branch, ""


def _is_dedicated_worktree_path(repo_root: Path, worktree_path: Path) -> bool:
    dedicated_root = _worktrees_root(repo_root)
    try:
        worktree_path.relative_to(dedicated_root)
    except ValueError:
        return False
    return True


def _load_spec_creation_prompt(
    repo_root: Path,
    spec_id: str | None,
    branch: str,
    *,
    resume: bool,
) -> str:
    prompt_path = repo_root / SPEC_CREATION_PROMPT_FILE
    if prompt_path.exists():
        prompt = prompt_path.read_text().strip()
    else:
        prompt = (
            "Author a focused spec for this repository. Read AGENTS.md and "
            f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / 'TEMPLATE.md'}` "
            "first, then write "
            f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}`, "
            "commit it, "
            "push `spec/<spec-id>`, and open a PR."
        )

    effective_spec_id = spec_id or _spec_id_from_authoring_branch(branch)
    resume_note = (
        "This worktree already exists. Inspect the current branch, spec draft, commits, "
        "and PR state, then continue from there."
        if resume
        else "This is a fresh spec-authoring session."
    )
    if effective_spec_id:
        session_details = (
            f"Spec ID: {effective_spec_id}\n"
            f"Branch: {_spec_authoring_branch(effective_spec_id)}\n"
            f"Target file: {_catalog_spec_relpath(effective_spec_id)}\n"
        )
    else:
        session_details = (
            "Spec ID: choose it during the conversation.\n"
            f"Current branch: {branch}\n"
            "You may author one or more specs in this session. For each spec, choose a "
            "lowercase slug matching `[a-z0-9][a-z0-9-]*`, write "
            f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}`, "
            "and commit it individually. "
            "Keep the session branch as-is (do not rename it) and open a single PR "
            "containing all authored specs.\n"
        )
    return f"{prompt}\n\n{session_details}{resume_note}"


def _prepare_spec_authoring_worktree(
    repo_root: Path,
    spec_id: str | None,
    *,
    base_ref: str,
) -> tuple[Path, str, bool]:
    resumed = False
    if spec_id:
        branch = _spec_authoring_branch(spec_id)
        registered_path, error = _registered_worktree_for_branch(repo_root, branch)
        if error:
            raise RuntimeError(error)
        if registered_path is not None:
            if not _is_dedicated_worktree_path(repo_root, registered_path):
                raise RuntimeError(
                    f"Spec authoring branch '{branch}' is checked out in non-dedicated "
                    f"worktree {registered_path}. Switch that checkout away from '{branch}' "
                    "before resuming spec authoring in a dedicated worktree."
                )
            if not registered_path.is_dir():
                raise RuntimeError(
                    f"Worktree registered but directory missing: {registered_path}. Run 'git worktree prune' and retry."
                )
            branch_error = _worktree_branch_alignment_error(registered_path, branch)
            if branch_error:
                raise RuntimeError(branch_error)
            return registered_path, branch, True
        worktree_path = _spec_authoring_worktree_path(repo_root, spec_id)
    else:
        registered_path, branch, error = _registered_spec_authoring_session(repo_root)
        if error:
            raise RuntimeError(error)
        if registered_path is not None:
            if not _is_dedicated_worktree_path(repo_root, registered_path):
                raise RuntimeError(
                    f"Anonymous spec authoring branch '{branch}' is checked out in "
                    f"non-dedicated worktree {registered_path}. Switch that checkout "
                    "away from the session branch before resuming spec authoring in a "
                    "dedicated worktree."
                )
            if not registered_path.is_dir():
                raise RuntimeError(
                    f"Worktree registered but directory missing: {registered_path}. Run 'git worktree prune' and retry."
                )
            branch_error = _worktree_branch_alignment_error(registered_path, branch)
            if branch_error:
                raise RuntimeError(branch_error)
            return registered_path, branch, True

        token = _spec_authoring_session_token()
        branch = _spec_authoring_session_branch(token)
        worktree_path = _spec_authoring_session_worktree_path(repo_root, token)

    worktree_exists, error = _worktree_is_registered(repo_root, worktree_path)
    if error:
        raise RuntimeError(error)

    if worktree_exists:
        if not worktree_path.is_dir():
            raise RuntimeError(
                f"Worktree registered but directory missing: {worktree_path}. Run 'git worktree prune' and retry."
            )
        branch_error = _worktree_branch_alignment_error(worktree_path, branch)
        if branch_error:
            raise RuntimeError(branch_error)
        resumed = True
    else:
        if worktree_path.exists():
            remove_tree(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        should_reuse_existing_branch = spec_id is not None
        branch_check = (
            run_subprocess(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo_root,
            )
            if should_reuse_existing_branch
            else subprocess.CompletedProcess([], 1, "", "")
        )
        if should_reuse_existing_branch and branch_check.returncode == 0:
            resumed = True
            wt_result = run_subprocess(
                ["git", "worktree", "add", str(worktree_path), branch],
                cwd=repo_root,
            )
        else:
            base_check = run_subprocess(
                ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
                cwd=repo_root,
            )
            if base_check.returncode != 0:
                raise RuntimeError(f"Base ref '{base_ref}' not available. Run 'git fetch origin'.")
            wt_result = run_subprocess(
                ["git", "worktree", "add", "--no-track", str(worktree_path), "-b", branch, base_ref],
                cwd=repo_root,
            )

        if wt_result.returncode != 0:
            detail = wt_result.stderr.strip() or wt_result.stdout.strip()
            raise RuntimeError(f"git worktree add failed: {detail}")

    return worktree_path, branch, resumed


def _build_spec_authoring_command(
    agent: str,
    repo_root: Path,
    worktree_path: Path,
    spec_id: str | None,
    branch: str,
    *,
    resume: bool,
) -> list[str]:
    prompt = _load_spec_creation_prompt(
        repo_root,
        spec_id,
        branch,
        resume=resume,
    )
    effective_spec_id = spec_id or _spec_id_from_authoring_branch(branch)
    if effective_spec_id:
        initial_prompt = (
            f"Resume authoring spec `{effective_spec_id}` and continue from the existing "
            "branch, worktree, commits, and PR state."
            if resume
            else f"Author spec `{effective_spec_id}` for this repository."
        )
    else:
        initial_prompt = (
            "Ask the user what they want to build, then author one or more specs. "
            "For each spec, choose a spec id and commit the file individually. "
            "You may create multiple specs in this session."
        )
    state_dir = _common_state_root(repo_root)

    adapter = get_agent_adapter(agent)
    authoring_kwargs: dict[str, object] = {
        "prompt": prompt,
        "worktree_path": worktree_path,
        "state_dir": state_dir,
        "initial_prompt": initial_prompt,
        "mcp_config_path": _mcp_config_path(worktree_path),
    }
    return adapter.build_authoring_command(**authoring_kwargs)


def _print_spec_authoring_summary(
    repo_root: Path,
    spec_id: str,
    branch: str,
    worktree_path: Path,
) -> None:
    spec_path = _specs_root(worktree_path) / f"{spec_id}.md"
    print(f"Spec ID: {spec_id}")
    print(f"Branch: {branch}")
    print(f"Spec File: {spec_path}")
    pr_data = _find_pr_for_branch(repo_root, branch, state="all")
    if pr_data is not None:
        print(f"PR: {pr_data.get('url', '')}")
        pr_state = str(pr_data.get("state", "")).strip().lower()
        if pr_state:
            print(f"PR State: {pr_state}")


def _print_multi_spec_authoring_summary(
    repo_root: Path,
    spec_ids: list[str],
    branch: str,
    worktree_path: Path,
) -> None:
    """Print summary for a multi-spec authoring session."""
    print(f"\nAuthored {len(spec_ids)} spec(s):\n")
    for sid in spec_ids:
        spec_path = _specs_root(worktree_path) / f"{sid}.md"
        description = ""
        if spec_path.exists():
            fm = parse_spec_frontmatter(spec_path)
            description = str(fm.get("description", "")).strip()
        parts = [f"  {sid}"]
        parts.append(str(spec_path))
        if description:
            parts[-1] += f" — {description}"
        print(": ".join(parts))
    print(f"\nBranch: {branch}")
    pr_data = _find_pr_for_branch(repo_root, branch, state="all")
    if pr_data is not None:
        print(f"PR: {pr_data.get('url', '')}")
        pr_state = str(pr_data.get("state", "")).strip().lower()
        if pr_state:
            print(f"PR State: {pr_state}")


def _authored_spec_ids(worktree_path: Path) -> set[str]:
    specs_dir = _specs_root(worktree_path)
    if not specs_dir.is_dir():
        return set()
    return {spec_path.stem for spec_path in specs_dir.glob("*.md") if spec_path.name != "TEMPLATE.md"}


def _current_worktree_branch(worktree_path: Path) -> str:
    branch_result = run_subprocess(["git", "branch", "--show-current"], cwd=worktree_path)
    if branch_result.returncode != 0:
        detail = redact_sensitive(tail_lines(branch_result.stderr or branch_result.stdout))
        raise RuntimeError(f"Could not determine checked-out branch for {worktree_path}: {detail[-240:]}")
    return branch_result.stdout.strip()


def _resolve_completed_spec_authoring_result(
    worktree_path: Path,
    requested_spec_id: str | None,
    preexisting_spec_ids: set[str],
) -> tuple[str, str]:
    branch = _current_worktree_branch(worktree_path)
    current_spec_ids = _authored_spec_ids(worktree_path)

    if requested_spec_id:
        spec_path = _specs_root(worktree_path) / f"{requested_spec_id}.md"
        if spec_path.exists():
            return requested_spec_id, branch
        raise RuntimeError(f"expected authored spec at {spec_path}, but it was not created")

    branch_spec_id = _spec_id_from_authoring_branch(branch)
    if branch_spec_id:
        spec_path = _specs_root(worktree_path) / f"{branch_spec_id}.md"
        if branch_spec_id in preexisting_spec_ids:
            raise RuntimeError(
                f"spec id '{branch_spec_id}' already existed before this authoring session. "
                "Choose a new spec id instead of reusing an existing "
                f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}` "
                "path."
            )
        if spec_path.exists():
            return branch_spec_id, branch

    new_spec_ids = sorted(current_spec_ids - preexisting_spec_ids)
    if len(new_spec_ids) == 1:
        spec_id = new_spec_ids[0]
        expected_branch = _spec_authoring_branch(spec_id)
        raise RuntimeError(
            f"authored spec `{_catalog_spec_relpath(spec_id)}`, but the current "
            f"branch is `{branch}` "
            f"instead of `{expected_branch}`. Rename the branch to `{expected_branch}` "
            "before pushing or opening a PR, then rerun `spec create` to resume."
        )
    if not new_spec_ids:
        raise RuntimeError(
            "expected the authoring session to create "
            f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}`, "
            "but no new spec file was found"
        )
    raise RuntimeError(
        "could not determine a single authored spec file. Ensure the session writes exactly "
        "one new "
        f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}` file "
        "and renames the branch to `spec/<spec-id>`."
    )


def _resolve_completed_multi_spec_authoring_result(
    worktree_path: Path,
    preexisting_spec_ids: set[str],
) -> tuple[list[str], str]:
    """Resolve authored specs after an anonymous multi-spec session.

    Returns (sorted list of new spec IDs, branch name).
    """
    branch = _current_worktree_branch(worktree_path)
    current_spec_ids = _authored_spec_ids(worktree_path)
    new_spec_ids = sorted(current_spec_ids - preexisting_spec_ids)
    if not new_spec_ids:
        raise RuntimeError(
            "expected the authoring session to create at least one "
            f"`{PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir) / '<spec-id>.md'}`, "
            "but no new spec file was found"
        )
    return new_spec_ids, branch


def _current_actor() -> str:
    return os.getenv("SPEC_ACTOR") or os.getenv("USER") or os.getenv("LOGNAME") or "unknown"


def _state_root(repo_root: Path) -> Path:
    return repo_root / SPEC_RUNTIME_CONFIG.paths.state_dir


def _worktree_state_root(worktree_path: Path) -> Path:
    return worktree_path / SPEC_RUNTIME_CONFIG.paths.state_dir


def _lock_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def _locked_state_path(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(path)
    with FileLock(lock_path):
        yield


def _write_json_file_atomically(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _run_state_path(repo_root: Path, run_id: str) -> Path:
    return _state_root(repo_root) / "runs" / f"{run_id}.json"


def _autopilot_active_path(repo_root: Path) -> Path:
    return _common_state_root(repo_root) / "autopilot" / "active.json"


def _worktree_registry_state_root(repo_root: Path, worktree_path: Path) -> Path:
    worktrees_dir_name = Path(SPEC_RUNTIME_CONFIG.paths.worktrees_dir).name
    if worktree_path.parent.name == worktrees_dir_name:
        return worktree_path.parent.parent / SPEC_RUNTIME_CONFIG.paths.state_dir
    return _state_root(repo_root)


def _read_worktree_postgres_pid(worktree_path: Path) -> int:
    pid_file = worktree_path / ".local" / "postgres" / "data" / "postmaster.pid"
    try:
        first_line = pid_file.read_text().splitlines()[0].strip()
    except (IndexError, OSError):
        return 0
    try:
        return int(first_line)
    except ValueError:
        return 0


def _register_worktree_process(
    repo_root: Path,
    worktree_path: Path,
    *,
    name: str,
    kind: str,
    pid: int,
    started_at: str,
    command: str = "",
    termination_scope: str = "pid",
    pgid: int = 0,
    supervision_token: SupervisionToken | None = None,
) -> None:
    try:
        worktree_process_registry.register_process(
            _worktree_registry_state_root(repo_root, worktree_path),
            worktree_path,
            name=name,
            kind=kind,
            pid=pid,
            started_at=started_at,
            command=command,
            termination_scope=termination_scope,
            pgid=pgid,
            supervision_token=supervision_token,
        )
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Could not persist %s cleanup registration for %s: %s",
            name,
            worktree_path,
            exc,
        )


def _register_worktree_process_from_popen(
    repo_root: Path,
    worktree_path: Path,
    proc: subprocess.Popen[str] | subprocess.Popen[bytes] | object | None,
    *,
    name: str,
    kind: str,
) -> None:
    if proc is None:
        return
    token = getattr(proc, "token", None)
    if type(proc).__module__ != "subprocess" and token is None:
        return
    if token is not None and token.identity.started_at == "test-double":
        return
    if token is not None:
        identity = token.identity
    else:
        try:
            identity = read_process_identity(proc.pid)
        except Exception as exc:  # pragma: no cover - defensive best effort
            logger.warning(
                "Could not inspect %s process %s for cleanup registration in %s: %s",
                name,
                proc.pid,
                worktree_path,
                exc,
            )
            return
    if identity is None:
        logger.warning(
            "Could not register %s process %s for cleanup in %s",
            name,
            proc.pid,
            worktree_path,
        )
        return
    _register_worktree_process(
        repo_root,
        worktree_path,
        name=name,
        kind=kind,
        pid=proc.pid,
        started_at=identity.started_at,
        command=identity.command,
        termination_scope="pgid",
        pgid=proc.pid if os.name == "posix" else 0,
        supervision_token=token,
    )


def _register_worktree_postgres_process(
    repo_root: Path,
    worktree_path: Path,
    *,
    name: str,
) -> None:
    pid = _read_worktree_postgres_pid(worktree_path)
    if pid <= 0:
        return
    try:
        identity = read_process_identity(pid)
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Could not inspect %s postgres process %s for cleanup registration in %s: %s",
            name,
            pid,
            worktree_path,
            exc,
        )
        return
    if identity is None:
        logger.warning(
            "Could not register %s postgres process %s for cleanup in %s",
            name,
            pid,
            worktree_path,
        )
        return
    _register_worktree_process(
        repo_root,
        worktree_path,
        name=name,
        kind="postgres",
        pid=pid,
        started_at=identity.started_at,
        command=identity.command,
        termination_scope="pid",
    )


def _register_setup_manifest_processes(
    repo_root: Path,
    worktree_path: Path,
    manifest: ImplementSetupManifest,
) -> None:
    for process in manifest.managed_processes:
        _register_worktree_process(
            repo_root,
            worktree_path,
            name=process.name,
            kind=process.kind,
            pid=process.pid,
            started_at=process.started_at,
            command=process.command,
            termination_scope=process.termination_scope,
            pgid=process.pgid,
        )


def _prune_registered_worktree_processes(
    repo_root: Path,
    worktree_path: Path,
) -> tuple[str, ...]:
    try:
        removed = worktree_process_registry.prune_dead_processes(
            _worktree_registry_state_root(repo_root, worktree_path),
            worktree_path,
        )
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Could not prune registered helpers for %s: %s",
            worktree_path,
            exc,
        )
        return ()
    for entry in removed:
        logger.info("Pruned stale registered helper for %s: %s", worktree_path, entry)
    return removed


def _reap_registered_worktree_processes(
    repo_root: Path,
    worktree_path: Path,
    *,
    reason: str,
) -> worktree_process_registry.ReapReport:
    try:
        report = worktree_process_registry.reap_registered_processes(
            _worktree_registry_state_root(repo_root, worktree_path),
            worktree_path,
            reason=reason,
        )
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Could not reap registered helpers for %s: %s",
            worktree_path,
            exc,
        )
        return worktree_process_registry.ReapReport()
    for entry in report.terminated:
        logger.info("Reaped registered helper for %s (reason=%s): %s", worktree_path, reason, entry)
    for entry in report.stale:
        logger.info("Removed stale registered helper for %s (reason=%s): %s", worktree_path, reason, entry)
    for entry in report.surviving:
        logger.warning(
            "Registered helper survived cleanup for %s (reason=%s): %s",
            worktree_path,
            reason,
            entry,
        )
    return report


def _resolve_recorded_process_group(
    repo_root: Path,
    run: RunState,
) -> tuple[int, str] | None:
    leader_pid = run.pgid or 0
    started_at = run.process_started_at.strip()
    if leader_pid > 0 and started_at:
        return (leader_pid, started_at)

    active_payload = _read_json_dict(_autopilot_active_path(repo_root)) or {}
    active_entry = active_payload.get(run.spec_id)
    if not isinstance(active_entry, dict):
        return None

    active_run_id = str(active_entry.get("run_id", "")).strip()
    if active_run_id and run.run_id and active_run_id != run.run_id:
        return None

    leader_pid = _coerce_optional_int(active_entry.get("pid")) or 0
    started_at = str(active_entry.get("process_started_at", "")).strip()
    if leader_pid <= 0 or not started_at:
        return None
    return (leader_pid, started_at)


def read_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    identity = inspect_process(pid)
    if identity is None:
        return None
    return ProcessIdentity(
        pid=identity.pid,
        started_at=identity.started_at,
        command=identity.command,
    )


def _list_live_process_group_members(pgid: int) -> list[int] | None:
    if pgid <= 0 or os.name != "posix":
        return []
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    members: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            live_pid = int(parts[0])
            live_pgid = int(parts[1])
        except ValueError:
            continue
        stat = parts[2].strip().upper()
        if live_pgid != pgid or not stat or stat.startswith("Z"):
            continue
        members.append(live_pid)
    return members


def is_pid_alive(pid: int, expected_started_at: str = "") -> bool:
    identity = read_process_identity(pid)
    if identity is None:
        return False
    if expected_started_at and identity.started_at != expected_started_at:
        return False
    return True


def _is_process_group_alive(pgid: int, leader_pid: int, leader_started_at: str = "") -> bool:
    if pgid <= 0 or os.name != "posix":
        return False

    members = _list_live_process_group_members(pgid)
    if members is not None:
        return bool(members)

    if is_pid_alive(leader_pid, leader_started_at):
        return True

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _refresh_run_retry_cap_from_disk(
    repo_root: Path,
    run: RunState,
    *,
    fallback: int,
) -> int:
    payload = _read_json_dict(_run_state_path(repo_root, run.run_id))
    if payload is None:
        return run.retry_cap if run.retry_cap > 0 else fallback
    try:
        refreshed = int(payload.get("retry_cap", run.retry_cap))
    except (TypeError, ValueError):
        refreshed = run.retry_cap if run.retry_cap > 0 else fallback
    if refreshed > 0:
        run.retry_cap = refreshed
    return run.retry_cap if run.retry_cap > 0 else fallback


def _set_active_agent_process(proc: subprocess.Popen[str] | None) -> None:
    global _ACTIVE_AGENT_PROCESS
    with _ACTIVE_AGENT_PROCESS_LOCK:
        _ACTIVE_AGENT_PROCESS = proc


def _terminate_registered_agent_process() -> None:
    with _ACTIVE_AGENT_PROCESS_LOCK:
        proc = _ACTIVE_AGENT_PROCESS
    if proc is None or proc.poll() is not None:
        return
    _terminate_agent_process(proc)


def _current_process_started_at() -> str:
    identity = read_process_identity(os.getpid())
    return identity.started_at if identity is not None else ""


@contextmanager
def _orchestrator_sigterm_guard(
    run: RunState,
    repo_root: Path | None = None,
) -> Iterator[None]:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(_signum: int, _frame) -> None:  # noqa: ANN001
        logger.warning(
            "Received SIGTERM for run %s (spec=%s phase=%s worktree=%s pgid=%s); "
            "terminating registered worktree helpers before exit.",
            run.run_id,
            run.spec_id,
            run.phase or "unknown",
            run.worktree_path or "unknown",
            run.pgid or "unknown",
        )
        _terminate_registered_agent_process()
        raw_worktree_path = str(run.worktree_path).strip()
        worktree_path: Path | None = None
        if raw_worktree_path:
            worktree_path = Path(raw_worktree_path).resolve(strict=False)
        elif repo_root is not None:
            try:
                worktree_path = resolve_worktree_path(run, repo_root)
            except Exception:
                worktree_path = None
        if worktree_path is not None:
            cleanup_root = repo_root
            if cleanup_root is None:
                cleanup_root = (
                    worktree_path.parent.parent if worktree_path.parent.name == ".worktrees" else worktree_path
                )
            _reap_registered_worktree_processes(
                cleanup_root,
                worktree_path,
                reason="orchestrator SIGTERM",
            )
        raise OrchestratorTerminationRequested("stopped by user")

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _ensure_orchestrator_process_group(run: RunState, repo_root: Path) -> None:
    if os.name != "posix":
        run.pgid = None
        token = claim_current_process(f"orchestrator-{run.run_id}")
        run.process_started_at = token.identity.started_at
        run.supervision_token = token.to_dict()
        run.save(repo_root)
        return

    pid = os.getpid()
    current_pgid = os.getpgrp()
    if current_pgid != pid:
        os.setpgrp()
        current_pgid = os.getpgrp()
        # Claim the foreground process group on the terminal so interactive
        # child processes (e.g. claude in the scoping phase) can still use
        # stdin/stdout without being stopped by SIGTTIN/SIGTTOU.
        # We must ignore SIGTTOU first: after setpgrp() we are a background
        # process group, and tcsetpgrp() would otherwise stop us with SIGTTOU.
        old_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            fd = sys.stdin.fileno()
            os.tcsetpgrp(fd, current_pgid)
        except (OSError, AttributeError):
            pass  # not a TTY or no stdin — non-interactive runs unaffected
        finally:
            signal.signal(signal.SIGTTOU, old_sigttou)

    run.pgid = current_pgid
    run.process_started_at = _current_process_started_at()
    run.save(repo_root)


def _default_spec_path(spec_id: str, run_mode: str) -> str:
    if run_mode == "task":
        return _task_spec_relpath(spec_id)
    return _catalog_spec_relpath(spec_id)


def _normalize_spec_path(spec_path: str) -> str:
    raw_path = spec_path.strip()
    if not raw_path or "\\" in raw_path:
        raise ValueError(f"Invalid spec path: {spec_path!r}")

    normalized_path = PurePosixPath(raw_path)
    normalized = normalized_path.as_posix()
    if (
        normalized == "."
        or normalized_path.is_absolute()
        or any(part == ".." for part in normalized_path.parts)
        or not normalized.endswith(".md")
        or not any(_is_relative_to(root, normalized_path) for root in _configured_spec_roots())
    ):
        raise ValueError(f"Invalid spec path: {spec_path!r}")
    return normalized


def _spec_path_for_run(run: RunState) -> str:
    raw_path = (run.spec_path or "").strip()
    if raw_path:
        return _normalize_spec_path(raw_path)
    legacy_path = str(getattr(run, "generated_spec_path", "") or "").strip()
    if legacy_path and legacy_path != "CURRENT_SPEC.md":
        return _normalize_spec_path(legacy_path)
    return _default_spec_path(run.spec_id, run.run_mode)


def _spec_path_in_tree(tree_root: Path, run: RunState) -> Path:
    return tree_root / _spec_path_for_run(run)


def _spec_path_for_run_id(repo_root: Path, run: RunState | None, spec_id: str) -> Path:
    if run is not None:
        source_path = _existing_spec_source_path(repo_root, run)
        if source_path is not None and source_path.exists():
            return source_path
        if run.spec_revision:
            return _run_spec_snapshot_path(repo_root, run.run_id)
        return repo_root / _spec_path_for_run(run)

    catalog_spec = _specs_root(repo_root) / f"{spec_id}.md"
    task_spec = _task_specs_root(repo_root) / f"{spec_id}.md"
    if catalog_spec.exists() and task_spec.exists():
        raise RuntimeError(f"Spec id '{spec_id}' is ambiguous between {catalog_spec} and {task_spec}.")
    if catalog_spec.exists():
        return catalog_spec
    if task_spec.exists():
        return task_spec
    return catalog_spec


def _command_spec_path(repo_root: Path, spec_id: str, run: RunState | None) -> Path:
    """Resolve the required spec path for CLI run/step commands.

    Resumed runs may rely on a pinned task snapshot under `.spec-state`, but
    new workflow runs still require a catalog spec under the configured specs dir.
    """
    return _spec_path_for_run_id(repo_root, run, spec_id)


def _run_spec_snapshot_path(repo_root: Path, run_id: str) -> Path:
    return _state_root(repo_root) / "runs" / run_id / PINNED_SPEC_FILENAME


def _spec_revision_for_text(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _persist_pinned_spec(
    repo_root: Path,
    run: RunState,
    *,
    spec_path: str,
    text: str,
) -> None:
    _set_pinned_spec_metadata(run, spec_path=spec_path, text=text)
    snapshot_path = _run_spec_snapshot_path(repo_root, run.run_id)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(text)


def _set_pinned_spec_metadata(
    run: RunState,
    *,
    spec_path: str,
    text: str,
) -> None:
    run.spec_path = _normalize_spec_path(spec_path)
    run.spec_revision = _spec_revision_for_text(text)


def _pin_run_spec_from_file(
    repo_root: Path,
    run: RunState,
    spec_file: Path,
    *,
    tree_root: Path,
) -> None:
    rel_path = spec_file.relative_to(tree_root).as_posix()
    _persist_pinned_spec(
        repo_root,
        run,
        spec_path=rel_path,
        text=spec_file.read_text(),
    )


def _legacy_current_spec_path(repo_root: Path, run: RunState) -> Path:
    return resolve_worktree_path(run, repo_root) / "CURRENT_SPEC.md"


def _spec_path_matches_revision(spec_path: Path, spec_revision: str) -> bool:
    if not spec_revision:
        return True
    try:
        return _spec_revision_for_text(spec_path.read_text()) == spec_revision
    except OSError:
        return False


def _existing_spec_source_path(repo_root: Path, run: RunState) -> Path | None:
    snapshot_path = _run_spec_snapshot_path(repo_root, run.run_id)
    if snapshot_path.exists():
        return snapshot_path

    pinned_revision = (run.spec_revision or "").strip()
    repo_spec = repo_root / _spec_path_for_run(run)
    worktree_spec = _spec_path_in_tree(resolve_worktree_path(run, repo_root), run)
    legacy_spec = _legacy_current_spec_path(repo_root, run)
    if pinned_revision:
        candidates = (repo_spec, worktree_spec, legacy_spec)
    else:
        # Legacy runs without a stored revision must prefer run-owned sources before
        # any mutable catalog copy in the repo.
        candidates = (legacy_spec, worktree_spec, repo_spec)

    for candidate in candidates:
        if candidate.exists() and _spec_path_matches_revision(candidate, pinned_revision):
            return candidate

    return None


def _ensure_run_spec_binding(run: RunState, repo_root: Path) -> RunState:
    changed = False
    desired_path = _spec_path_for_run(run)
    if run.spec_path != desired_path:
        run.spec_path = desired_path
        changed = True

    snapshot_path = _run_spec_snapshot_path(repo_root, run.run_id)
    if snapshot_path.exists():
        revision = _spec_revision_for_text(snapshot_path.read_text())
        if run.spec_revision != revision:
            run.spec_revision = revision
            changed = True
    else:
        source_path = _existing_spec_source_path(repo_root, run)
        if source_path is not None:
            if source_path == _legacy_current_spec_path(repo_root, run):
                source_text = source_path.read_text()
                try:
                    _persist_pinned_spec(
                        repo_root,
                        run,
                        spec_path=run.spec_path,
                        text=source_text,
                    )
                except OSError:
                    _set_pinned_spec_metadata(
                        run,
                        spec_path=run.spec_path,
                        text=source_text,
                    )
                changed = True
            elif source_path != snapshot_path:
                source_text = source_path.read_text()
                try:
                    worktree_root = resolve_worktree_path(run, repo_root)
                    if source_path.is_relative_to(worktree_root):
                        tree_root = worktree_root
                    elif source_path.is_relative_to(repo_root):
                        tree_root = repo_root
                    else:
                        tree_root = worktree_root
                    _pin_run_spec_from_file(repo_root, run, source_path, tree_root=tree_root)
                except (ValueError, OSError):
                    fallback_path = run.spec_path
                    if source_path.is_relative_to(worktree_root):
                        fallback_path = source_path.relative_to(worktree_root).as_posix()
                    elif source_path.is_relative_to(repo_root):
                        fallback_path = source_path.relative_to(repo_root).as_posix()
                    _set_pinned_spec_metadata(
                        run,
                        spec_path=fallback_path,
                        text=source_text,
                    )
                changed = True

    if changed:
        try:
            run.save(repo_root)
        except OSError:
            logger.debug(
                "Could not persist spec binding for run %s in %s",
                run.run_id,
                repo_root,
            )
    return run


def _restore_pinned_spec_into_worktree(
    repo_root: Path,
    run: RunState,
    worktree_path: Path,
) -> Path:
    snapshot_path = _run_spec_snapshot_path(repo_root, run.run_id)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Pinned spec snapshot missing for run {run.run_id}: {snapshot_path}")

    expected_text = snapshot_path.read_text()
    target_path = _spec_path_in_tree(worktree_path, run)
    if target_path.exists() and target_path.read_text() == expected_text:
        return target_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(expected_text)
    return target_path


def _active_spec_path(
    repo_root: Path,
    run: RunState,
    *,
    prefer_worktree: bool,
) -> Path | None:
    run = _ensure_run_spec_binding(run, repo_root)
    worktree_path = resolve_worktree_path(run, repo_root)

    if prefer_worktree and worktree_path.is_dir():
        snapshot_path = _run_spec_snapshot_path(repo_root, run.run_id)
        if snapshot_path.exists():
            return _restore_pinned_spec_into_worktree(repo_root, run, worktree_path)

    source_path = _existing_spec_source_path(repo_root, run)
    if source_path is not None and source_path.exists():
        return source_path

    if (run.spec_revision or "").strip():
        return None

    if worktree_path.is_dir():
        worktree_spec = _spec_path_in_tree(worktree_path, run)
        if worktree_spec.exists():
            return worktree_spec

    return None


def _ensure_run_spec_committed(
    run: RunState,
    *,
    worktree_path: Path,
    spec_path: Path,
) -> bool:
    try:
        relative_spec = spec_path.relative_to(worktree_path).as_posix()
    except ValueError:
        return True

    status_result = run_subprocess(
        ["git", "status", "--porcelain", "--", relative_spec],
        cwd=worktree_path,
    )
    if status_result.returncode != 0:
        detail = status_result.stderr.strip() or status_result.stdout.strip()
        if not detail:
            detail = f"exit code {status_result.returncode}"
        run.last_error = f"git status -- {relative_spec} failed: {detail}"
        return False
    if not status_result.stdout.strip():
        return True

    if not run_or_fail(
        run,
        ["git", "add", "--", relative_spec],
        cwd=worktree_path,
        action=f"git add {relative_spec}",
    ):
        return False
    return run_or_fail(
        run,
        [
            "git",
            "commit",
            "-m",
            f"Pin spec contract for {run.spec_id}",
            "--only",
            "--",
            relative_spec,
        ],
        cwd=worktree_path,
        action="git commit (pin spec contract)",
    )


def _legacy_gate_status_path(repo_root: Path, spec_id: str) -> Path:
    return _state_root(repo_root) / spec_id / "gate-status.json"


def _gate_status_path(repo_root: Path, run: RunState) -> Path:
    return _state_root(repo_root) / "runs" / run.run_id / "gate-status.json"


def _read_gate_status(repo_root: Path, run: RunState) -> tuple[Path, dict | None]:
    for path in (_gate_status_path(repo_root, run), _legacy_gate_status_path(repo_root, run.spec_id)):
        if not path.exists():
            continue
        try:
            return path, json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            return path, None
    return _gate_status_path(repo_root, run), None


def _run_token_for_spec(spec_id: str, run_id: str) -> str:
    prefix = f"{spec_id}-"
    if run_id.startswith(prefix):
        token = run_id[len(prefix) :]
        if token:
            return token
    digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()
    return digest[:12]


def _create_spec_run(
    repo_root: Path,
    spec_id: str,
    *,
    agent: str,
    review_agent: str,
    base_ref: str,
    requested_by: str,
    branch: str = "",
) -> RunState:
    preallocated_run_id = os.environ.get("SPEC_PREALLOCATED_RUN_ID", "").strip()
    run_id = preallocated_run_id if preallocated_run_id.startswith(f"{spec_id}-") else _run_id(spec_id)
    run_token = _run_token_for_spec(spec_id, run_id)
    branch_name = branch.strip() or spec_run_branch(spec_id, run_token)
    worktree_path = _worktrees_root(repo_root) / spec_run_worktree_name(
        spec_id,
        run_token,
    )
    resumed_from_branch = ""
    if branch.strip():
        resumed_from_branch = branch_name
        registered_worktree, error = _registered_worktree_for_branch(
            repo_root,
            branch_name,
        )
        if error:
            raise RuntimeError(error)
        if registered_worktree is not None:
            worktree_path = registered_worktree
    run = RunState(
        run_id=run_id,
        spec_id=spec_id,
        branch=branch_name,
        worktree_path=str(worktree_path),
        spec_path=_default_spec_path(spec_id, "spec"),
        agent=agent,
        review_agent=review_agent,
        base_ref=base_ref,
        requested_by=requested_by,
        resumed_from_branch=resumed_from_branch,
        backend=SPEC_RUNTIME_CONFIG.execution.backend,
        safety_mode=SPEC_RUNTIME_CONFIG.execution.safety_mode,
        backend_source=(
            "repo-config"
            if SPEC_RUNTIME_CONFIG.execution.backend_explicit
            else (
                "rollout-policy"
                if os.environ.get("SPEC_ACTOR") == "autopilot"
                and SPEC_RUNTIME_CONFIG.autopilot.container_default_enabled
                and SPEC_RUNTIME_CONFIG.execution.backend == "container"
                else "default"
            )
        ),
        backend_workspace_root=SPEC_RUNTIME_CONFIG.execution.workspace_root,
    )
    spec_file = _specs_root(repo_root) / f"{spec_id}.md"
    _pin_run_spec_from_file(repo_root, run, spec_file, tree_root=repo_root)
    run.save(repo_root)
    return run


def _ensure_run_identity(run: RunState, repo_root: Path) -> RunState:
    if not run.worktree_path:
        run.worktree_path = str(resolve_worktree_path(run, repo_root))
        try:
            run.save(repo_root)
        except OSError:
            # Some implement sessions can read common state but not write it.
            # Keep the derived identity in memory so status/complete can proceed.
            logger.debug(
                "Could not persist derived worktree_path for run %s in %s",
                run.run_id,
                repo_root,
            )
    return _ensure_run_spec_binding(run, repo_root)


def _select_resumable_run(
    repo_root: Path,
    spec_id: str,
    *,
    run_id: str | None = None,
    ensure_identity: bool = True,
) -> RunState | None:
    if run_id:
        try:
            run = RunState.load(repo_root, run_id)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Run '{run_id}' was not found.") from exc
        if run.spec_id != spec_id:
            raise RuntimeError(f"Run '{run_id}' belongs to spec '{run.spec_id}', not '{spec_id}'.")
        if not _is_run_workflow_resumable(repo_root, run) or run.is_superseded:
            raise RuntimeError(f"Run '{run_id}' is not resumable (status={run.status}).")
        return _ensure_run_identity(run, repo_root) if ensure_identity else run

    resumable = [
        (_ensure_run_identity(run, repo_root) if ensure_identity else run)
        for run in RunState.list_for_spec(repo_root, spec_id)
        if _is_run_workflow_resumable(repo_root, run) and not run.is_superseded
    ]
    if not resumable:
        return None
    if len(resumable) > 1:
        run_ids = ", ".join(run.run_id for run in resumable)
        raise RuntimeError(f"Multiple resumable runs exist for {spec_id}: {run_ids}. Re-run with RUN=<run-id>.")
    return resumable[0]


def _select_step_run(
    repo_root: Path,
    spec_id: str,
    *,
    run_id: str | None = None,
) -> RunState | None:
    if run_id:
        try:
            run = RunState.load(repo_root, run_id)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Run '{run_id}' was not found.") from exc
        if run.spec_id != spec_id:
            raise RuntimeError(f"Run '{run_id}' belongs to spec '{run.spec_id}', not '{spec_id}'.")
        if not run.is_step_selectable:
            raise RuntimeError(f"Run '{run_id}' cannot be used for step execution (status={run.status}).")
        return _ensure_run_identity(run, repo_root)

    step_selectable = [
        _ensure_run_identity(run, repo_root)
        for run in RunState.list_for_spec(repo_root, spec_id)
        if run.is_step_selectable and not run.is_superseded
    ]
    if not step_selectable:
        return None
    if len(step_selectable) > 1:
        run_ids = ", ".join(run.run_id for run in step_selectable)
        raise RuntimeError(f"Multiple step-selectable runs exist for {spec_id}: {run_ids}. Re-run with RUN=<run-id>.")
    return step_selectable[0]


def _select_completion_run(
    repo_root: Path,
    spec_id: str | None,
    *,
    run_id: str | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> RunState | None:
    env = env or os.environ
    explicit_run_id = str(run_id or "").strip()
    explicit_spec_id = str(spec_id or "").strip()
    env_run_id = str(env.get("SPEC_RUN_ID", env.get("SIM_RUN_ID", ""))).strip()
    env_spec_id = str(env.get("SPEC_ID", env.get("SIM_SPEC_ID", ""))).strip()
    resolved_spec_id = explicit_spec_id or env_spec_id
    env_selection_error: RuntimeError | None = None

    def _load_selected_run(selected_run_id: str) -> RunState:
        try:
            run = RunState.load(repo_root, selected_run_id)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Run '{selected_run_id}' was not found.") from exc
        if resolved_spec_id and run.spec_id != resolved_spec_id:
            raise RuntimeError(f"Run '{selected_run_id}' belongs to spec '{run.spec_id}', not '{resolved_spec_id}'.")
        return _ensure_run_identity(run, repo_root)

    if explicit_run_id:
        return _load_selected_run(explicit_run_id)

    if env_run_id:
        try:
            return _load_selected_run(env_run_id)
        except RuntimeError as exc:
            env_selection_error = exc

    worktree_cwd = (cwd or Path.cwd()).resolve()
    matching_runs: list[RunState] = []
    candidate_runs = (
        RunState.list_for_spec(repo_root, explicit_spec_id) if explicit_spec_id else RunState.list_all(repo_root)
    )
    for run in candidate_runs:
        resolved = resolve_worktree_path(_ensure_run_identity(run, repo_root), repo_root).resolve()
        try:
            worktree_cwd.relative_to(resolved)
        except ValueError:
            continue
        matching_runs.append(run)

    if len(matching_runs) == 1:
        return _ensure_run_identity(matching_runs[0], repo_root)
    if len(matching_runs) > 1:
        run_ids = ", ".join(run.run_id for run in matching_runs)
        raise RuntimeError(
            f"Multiple runs{f' for {resolved_spec_id}' if resolved_spec_id else ''} "
            f"match {worktree_cwd}: {run_ids}. "
            "Re-run with RUN=<run-id>."
        )

    if not resolved_spec_id:
        if env_selection_error is not None:
            raise env_selection_error
        raise RuntimeError(
            "Could not infer spec/run identity for completion. Provide `--spec`, "
            "run the command from the implementation worktree, or export "
            "`SPEC_ID`/`SPEC_RUN_ID`."
        )

    fallback_run = _select_resumable_run(repo_root, resolved_spec_id, run_id=None)
    if fallback_run is not None:
        return fallback_run
    if env_selection_error is not None and not explicit_spec_id:
        raise env_selection_error
    return None


def _latest_non_superseded_run(
    repo_root: Path,
    spec_id: str,
    *,
    ensure_identity: bool = True,
) -> RunState | None:
    for run in RunState.list_for_spec(repo_root, spec_id):
        if not run.is_superseded:
            return _ensure_run_identity(run, repo_root) if ensure_identity else run
    return None


def add_retries(spec_id: str, count: int, *, repo_root: Path | None = None) -> int:
    if count <= 0:
        raise RuntimeError("Retry increment must be a positive integer.")
    root = repo_root or resolve_repo_root()
    latest = _latest_non_superseded_run(root, spec_id)
    if latest is None:
        raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")

    path = _run_state_path(root, latest.run_id)
    with _locked_state_path(path):
        payload = _read_json_dict(path)
        if payload is None:
            raise RuntimeError(f"Run record is unreadable for '{latest.run_id}'.")
        try:
            current_retry_cap = int(payload.get("retry_cap", latest.retry_cap))
        except (TypeError, ValueError):
            current_retry_cap = latest.retry_cap
        payload["retry_cap"] = current_retry_cap + count
        payload["updated_at"] = _now_iso()
        _write_json_file_atomically(path, payload)
    return int(payload["retry_cap"])


def stop_run(spec_id: str, *, repo_root: Path | None = None) -> RunState:
    root = repo_root or resolve_repo_root()
    latest = _latest_non_superseded_run(root, spec_id)
    if latest is None:
        raise RuntimeError(f"No non-superseded run found for spec '{spec_id}'.")

    if os.name != "posix" and latest.supervision_token:
        token = SupervisionToken.from_dict(latest.supervision_token)
        process_was_alive = identity_matches(token.identity)
        if process_was_alive and not terminate_supervised(token, grace_seconds=RUN_STOP_GRACE_SECONDS):
            raise RuntimeError(f"Failed to stop live supervised process for spec '{spec_id}'.")
        process_group = None
    else:
        process_group = _resolve_recorded_process_group(root, latest)

    if process_group is None and not (os.name != "posix" and latest.supervision_token):
        raise RuntimeError(f"Spec '{spec_id}' does not have a recorded orchestrator process group.")
    if process_group is not None:
        pgid, leader_started_at = process_group
        process_was_alive = is_pid_alive(pgid, leader_started_at)
        group_was_alive = _is_process_group_alive(pgid, pgid, leader_started_at)
    else:
        pgid, leader_started_at, group_was_alive = 0, "", False
    if not process_was_alive and group_was_alive:
        raise RuntimeError(
            f"Recorded leader {pgid} for spec '{spec_id}' has exited or changed identity, "
            f"but process group {pgid} still has live members. Refusing to signal an "
            "orphaned or reused process group without a verifiable leader; inspect and "
            "terminate only the run-owned processes manually."
        )
    if process_group is not None and process_was_alive:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError as exc:
            raise RuntimeError(f"No live process is currently running for spec '{spec_id}'.") from exc

        deadline = time.monotonic() + RUN_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _is_process_group_alive(pgid, pgid, leader_started_at):
                break
            _poll_sleep(0.1)

        if _is_process_group_alive(pgid, pgid, leader_started_at):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if not _is_process_group_alive(pgid, pgid, leader_started_at):
                    break
                _poll_sleep(0.1)

        if _is_process_group_alive(pgid, pgid, leader_started_at):
            raise RuntimeError(f"Failed to stop live process group for spec '{spec_id}'.")
    elif latest.status != "running":
        raise RuntimeError(f"No live process is currently running for spec '{spec_id}'.")

    path = _run_state_path(root, latest.run_id)
    with _locked_state_path(path):
        payload = _read_json_dict(path)
        if payload is None:
            payload = asdict(latest)
        # For the dead-process fallback, re-check the persisted status under
        # the lock to avoid clobbering a run that completed between the
        # pre-lock snapshot and now.
        if not process_was_alive and payload.get("status") != "running":
            return RunState.load(root, latest.run_id)
        payload["status"] = "failed"
        payload["last_error"] = "stopped by user"
        payload["updated_at"] = _now_iso()
        _write_json_file_atomically(path, payload)
    return RunState.load(root, latest.run_id)


def _is_retryable_failed_implement_run(
    run: RunState,
    *,
    retry_cap: int | None = None,
) -> bool:
    if run.status != "failed" or run.phase not in ("implement", "verify"):
        return False
    effective_retry_cap = retry_cap if retry_cap is not None else run.retry_cap
    # Explicit --retry-cap override bypasses the failure-message check;
    # the user is intentionally asking to resume.
    if retry_cap is None and not _is_retryable_implement_failure_message(run.last_error):
        return False
    if effective_retry_cap > 0 and _convergence_attempts(run) >= effective_retry_cap:
        return False
    return True


def _branch_commits_ahead_of_base(repo_root: Path, branch: str, base_ref: str) -> int:
    """Return the number of commits ``branch`` has that ``base_ref`` does not.

    Returns 0 when the branch is missing, the base ref is unresolvable, or git
    otherwise cannot answer — callers treat 0 as "no committed work to protect".
    """
    branch = (branch or "").strip()
    base_ref = (base_ref or "").strip() or BASE_REF
    if not branch:
        return 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _run_branch_has_committed_work(repo_root: Path, run: RunState) -> bool:
    """True when the run's branch carries commits ahead of its base ref.

    Container-backed runs use an isolated source clone, so an unpushed branch
    can be present there without existing in the orchestration repository.
    Inspect the run workspace first and retain the root-repository lookup as a
    fallback for worktree-backed and already-published branches.
    """
    workspace = resolve_worktree_path(run, repo_root)
    candidate_roots = [workspace, repo_root] if workspace != repo_root else [repo_root]
    for candidate_root in candidate_roots:
        if not (candidate_root / ".git").exists():
            continue
        if _branch_commits_ahead_of_base(
            candidate_root,
            run.branch,
            run.base_ref or BASE_REF,
        ) > 0:
            return True
    return False


def _select_default_run(
    repo_root: Path,
    spec_id: str,
    *,
    retry_cap: int | None = None,
    ensure_identity: bool = True,
) -> RunState | None:
    latest = _latest_non_superseded_run(repo_root, spec_id, ensure_identity=ensure_identity)
    if latest is None:
        return None
    if latest.is_auto_resumable:
        return latest
    if latest.status == "waiting-for-input":
        return latest
    if latest.status == "passed" and latest.phase == "implement" and _operator_continuation_for_run(repo_root, latest):
        return latest
    if latest.status in ("pending", "running", "blocked"):
        return latest
    if _is_retryable_failed_implement_run(latest, retry_cap=retry_cap):
        return latest
    if latest.status == "failed" and latest.phase in FAILED_LATE_PHASE_RESUME_PHASES:
        return latest
    # Dispatch discipline: never supersede a failed run whose branch
    # holds committed implementation work. Resume it (same run id, existing
    # branch) instead of replacing it and reimplementing from scratch.
    if latest.status == "failed" and _run_branch_has_committed_work(repo_root, latest):
        return latest
    return None


def _passed_implement_run_requires_fresh_attempt(
    repo_root: Path,
    run: RunState,
) -> bool:
    if run.status != "passed" or run.phase != "implement":
        return False

    return _passed_implement_continuation_requires_fresh_attempt(repo_root, run)


def _schedule_operator_guided_implement_attempt(
    repo_root: Path,
    run: RunState,
    *,
    retry_cap: int,
) -> bool:
    """Advance attempt lineage before applying active operator guidance.

    Steering can arrive while an implement agent is already running. The
    current attempt cannot consume that replacement event, so a successful
    implement phase must loop back through implement instead of advancing to
    verification. Resolved operator requests use the same continuation path.
    Advancing ``run.attempts`` here gives the follow-up its own attempt-scoped
    artifacts and keeps operator guidance inside the shared retry limit.
    """
    if not _passed_implement_run_requires_fresh_attempt(repo_root, run):
        return False

    if _convergence_attempts(run) >= retry_cap:
        run.status = "blocked"
        run.last_error = (
            f"Retry cap reached ({_convergence_attempts(run)}/{retry_cap}) before active "
            "operator guidance could be applied. Resume with "
            f"`spec implement --spec {run.spec_id} --retry-cap {retry_cap + 10}`."
        )
        run.save(repo_root)
        return False

    run.attempts += 1
    run.status = "pending"
    run.last_error = ""
    run.save(repo_root)
    logger.info(
        "Active operator guidance requires implement attempt %d for %s.",
        run.attempts + 1,
        run.spec_id,
    )
    return True


def _mark_superseded_runs(
    repo_root: Path,
    spec_id: str,
    *,
    superseded_by_run_id: str,
    keep_run_ids: set[str] | None = None,
) -> None:
    keep = keep_run_ids or set()
    for run in RunState.list_for_spec(repo_root, spec_id):
        if run.run_id in keep or run.is_superseded or not run.is_non_terminal:
            continue
        run.superseded_from_status = run.status
        run.superseded_from_phase = run.phase
        run.status = "superseded"
        run.superseded_by = superseded_by_run_id
        run.superseded_at = _now_iso()
        run.save(repo_root)


def _select_prior_review_run(
    repo_root: Path,
    run: RunState,
    *,
    preferred_prior_run_id: str | None = None,
) -> RunState | None:
    def is_terminal_predecessor(candidate: RunState) -> bool:
        if candidate.status in ("abandoned", "failed"):
            return True
        if candidate.status != "superseded":
            return False

        prior_status = str(candidate.superseded_from_status or "").strip()
        if not prior_status:
            # Legacy run state may not record the pre-supersede status.
            return True

        # RUN=new can supersede an active run before we inspect it. Only carry
        # findings forward when the predecessor was already terminal.
        return prior_status in ("abandoned", "failed", "superseded")

    prior_runs = [
        candidate for candidate in RunState.list_for_spec(repo_root, run.spec_id) if candidate.run_id != run.run_id
    ]
    preferred_run_id = str(preferred_prior_run_id or "").strip()
    if preferred_run_id:
        preferred_prior_run = next(
            (candidate for candidate in prior_runs if candidate.run_id == preferred_run_id),
            None,
        )
        if preferred_prior_run is not None and is_terminal_predecessor(preferred_prior_run):
            return preferred_prior_run
    direct_prior_run = next(
        (
            candidate
            for candidate in prior_runs
            if candidate.superseded_by == run.run_id and is_terminal_predecessor(candidate)
        ),
        None,
    )
    if direct_prior_run is not None:
        return direct_prior_run

    # Failed and abandoned runs are terminal, so starting a fresh attempt does not
    # automatically mark them as superseded by the new run. Fall back to the
    # latest such predecessor so fresh attempts can still inherit its review
    # findings on the first implement prompt.
    fallback_prior_run = next(
        (
            candidate
            for candidate in prior_runs
            if not candidate.is_superseded and candidate.status in ("abandoned", "failed")
        ),
        None,
    )
    return fallback_prior_run


def _populate_prior_review_findings(
    repo_root: Path,
    run: RunState,
    *,
    preferred_prior_run_id: str | None = None,
) -> None:
    run.prior_review_run_id = ""
    run.prior_review_summary = ""
    run.prior_review_findings = []

    prior_run = _select_prior_review_run(
        repo_root,
        run,
        preferred_prior_run_id=preferred_prior_run_id,
    )
    if prior_run is None:
        return

    review_result = ReviewResult.load(repo_root, prior_run.run_id)
    if review_result is None or not review_result.findings:
        return

    run.prior_review_run_id = prior_run.run_id
    run.prior_review_summary = review_result.summary or ""
    run.prior_review_findings = _top_review_findings(review_result.findings)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorRequest:
    request_id: str
    spec_id: str
    branch: str
    action: str
    requested_by: str = "user"
    created_at: str = field(default_factory=_now_iso)

    def validate(self) -> list[str]:
        errors = []
        if not SPEC_ID_RE.fullmatch(self.spec_id):
            errors.append(f"Invalid spec_id: {self.spec_id!r}")
        if not re.match(
            (
                r"^(spec/[a-z0-9][a-z0-9-]*"
                r"|code/[a-z0-9][a-z0-9-]*--[A-Za-z0-9][A-Za-z0-9-]*"
                r"|specrun/[a-z0-9][a-z0-9-]*--[A-Za-z0-9][A-Za-z0-9-]*"
                r"|specdoc/[a-z0-9][a-z0-9-]*"
                r"|fix/[a-z0-9][a-z0-9-]*"
                r"|task/[a-z0-9][a-z0-9-]*)$"
            ),
            self.branch,
        ):
            errors.append(f"Invalid branch: {self.branch!r}")
        if self.action not in VALID_ACTIONS:
            errors.append(f"Invalid action: {self.action!r}")
        return errors


@dataclass
class OrchestratorResult:
    status: str  # "passed" | "failed" | "blocked" | "error"
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    exit_code: int | None = None
    error_code: str = ""
    failure_type: str = ""
    failure_subtype: str = ""
    retryable: bool = False
    nonfatal: bool = False
    gate_name: str = ""
    warnings: list[dict[str, object]] = field(default_factory=list)


def _default_verify_passed_once(phase: str, status: str) -> bool:
    return phase in {"publish", "review", "merge", "cleanup"} or (phase == "verify" and status == "passed")


@dataclass
class RunState:
    run_id: str
    spec_id: str
    branch: str
    worktree_path: str = ""
    run_mode: str = "spec"  # spec | task
    spec_path: str = ""
    spec_revision: str = ""
    generated_spec_path: str = ""
    requested_by: str = ""
    phase: str = "bootstrap"
    status: str = "pending"  # pending | running | passed | failed | blocked | superseded | waiting-for-input
    attempts: int = 0
    # Monotonic identity for every implement-agent process launch. ``attempts``
    # remains the convergence/retry budget for compatibility; launch_numbered
    # artifacts keep recovery and post-cap operator resumes from overwriting one
    # another when the convergence count is intentionally unchanged.
    implement_launches: int = 0
    implement_failures: int = 0
    verify_failures: int = 0
    merge_conflicts: int = 0
    review_changes: int = 0
    no_progress_retries: int = 0
    last_error: str = ""
    nonfatal_warnings: list[dict[str, object]] = field(default_factory=list)
    merge_conflict_error: str = ""
    mergeability_issue: str = ""
    last_verify_failure_gate: str = ""
    last_verify_failure_test_nodeid: str = ""
    implement_head_sha_before: str = ""
    implement_head_sha_after: str = ""
    implement_has_new_commit: bool = False
    implement_staged_changes: bool = False
    implement_unstaged_changes: bool = False
    implement_tree_status: str = ""
    # Set only when the current attempt's agent explicitly reported failure, and
    # cleared on entry to every implement phase. The implement_* commit fields
    # alone cannot carry this: phase_implement has early returns ahead of where
    # they are reset, so a prelaunch failure can be judged on the *previous*
    # attempt's clean-commit state.
    implement_agent_reported_failure: bool = False
    resumed_from_branch: str = ""
    last_merged_master_sha: str = ""
    publish_as_draft: bool = False
    verify_passed_once: bool = False
    verify_head_sha: str = ""
    review_expected_head_sha: str = ""
    review_decision_status: str = ""
    review_decision_summary: str = ""
    review_decision_check_url: str = ""
    readiness_status: str = ""
    readiness_head_sha: str = ""
    readiness_blocker: str = ""
    pending_block_debugger_signature: str = ""
    last_block_debugger_guided_retry_signature: str = ""
    block_debugger_auto_resumes: int = 0
    superseded_by: str = ""
    superseded_at: str = ""
    superseded_from_status: str = ""
    superseded_from_phase: str = ""
    prior_review_run_id: str = ""
    prior_review_summary: str = ""
    prior_review_findings: list[dict] = field(default_factory=list)
    base_ref: str = BASE_REF
    retry_cap: int = RETRY_CAP
    pgid: int | None = None
    process_started_at: str = ""
    supervision_token: dict[str, object] = field(default_factory=dict)
    intake_reset_requested: bool = False
    slug_was_provided: bool = False
    updated_at: str = field(default_factory=_now_iso)
    agent: str = "claude"
    review_agent: str = ""
    created_at: str = field(default_factory=_now_iso)
    input_question: str = ""
    input_response: str = ""
    heartbeat_at: str = ""
    backend: str = ""
    safety_mode: str = ""
    backend_source: str = ""
    backend_workspace_root: str = ""
    # Classification of the most recent failed phase, persisted onto the run
    # record so status projection and autopilot can consult retryability
    # without scanning per-phase audit JSON. ``None`` means "no classification
    # recorded" (pre-existing records), which callers treat as retryable to
    # preserve today's behavior.
    last_failure_retryable: bool | None = None
    last_failure_type: str = ""
    last_failure_subtype: str = ""

    def save(self, repo_root: Path) -> Path:
        p = _run_state_path(repo_root, self.run_id)
        now = _now_iso()
        self.updated_at = now
        self.heartbeat_at = now
        payload = asdict(self)
        with _locked_state_path(p):
            _write_json_file_atomically(p, payload)
        return p

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> RunState:
        p = _run_state_path(repo_root, run_id)
        data = json.loads(p.read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> RunState:
        agent_was_recorded = "agent" in data
        allowed_fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in allowed_fields}
        payload.setdefault("run_mode", "spec")
        payload.setdefault("worktree_path", "")
        payload.setdefault("spec_path", "")
        payload.setdefault("spec_revision", "")
        payload.setdefault("generated_spec_path", "")
        payload.pop("task_contract_path", None)
        payload.setdefault("requested_by", "")
        payload.setdefault("implement_failures", 0)
        payload.setdefault("implement_launches", 0)
        payload.setdefault("verify_failures", 0)
        payload.setdefault("merge_conflicts", 0)
        payload.setdefault("review_changes", 0)
        payload.setdefault("no_progress_retries", 0)
        payload.pop("implement_dev_server_warning", None)
        payload["nonfatal_warnings"] = _coerce_nonfatal_warnings(
            payload.get("nonfatal_warnings", []),
        )
        payload.setdefault("merge_conflict_error", "")
        payload.setdefault("mergeability_issue", "")
        payload.setdefault("last_verify_failure_gate", "")
        payload.setdefault("implement_head_sha_before", "")
        payload.setdefault("implement_head_sha_after", "")
        payload.setdefault("implement_has_new_commit", False)
        payload.setdefault("implement_staged_changes", False)
        payload.setdefault("implement_unstaged_changes", False)
        payload.setdefault("implement_tree_status", "")
        payload.setdefault("implement_agent_reported_failure", False)
        payload.setdefault("resumed_from_branch", "")
        payload.setdefault("last_merged_master_sha", "")
        payload.setdefault("publish_as_draft", False)
        if "verify_passed_once" in payload:
            payload["verify_passed_once"] = bool(payload.get("verify_passed_once", False))
        else:
            payload["verify_passed_once"] = _default_verify_passed_once(
                str(payload.get("phase") or ""),
                str(payload.get("status") or ""),
            )
        payload.setdefault("verify_head_sha", "")
        payload.setdefault("review_expected_head_sha", "")
        payload.setdefault("review_decision_status", "")
        payload.setdefault("review_decision_summary", "")
        payload.setdefault("review_decision_check_url", "")
        payload.setdefault("readiness_status", "")
        payload.setdefault("readiness_head_sha", "")
        payload.setdefault("readiness_blocker", "")
        payload.setdefault("pending_block_debugger_signature", "")
        payload.setdefault("last_block_debugger_guided_retry_signature", "")
        payload.setdefault("block_debugger_auto_resumes", 0)
        payload.setdefault("superseded_by", "")
        payload.setdefault("superseded_at", "")
        payload.setdefault("superseded_from_status", "")
        payload.setdefault("superseded_from_phase", "")
        payload.setdefault("prior_review_run_id", "")
        payload.setdefault("prior_review_summary", "")
        payload["prior_review_findings"] = _coerce_saved_review_findings(
            payload.get("prior_review_findings", []),
        )
        payload["pgid"] = _coerce_optional_int(payload.get("pgid"))
        payload.setdefault("process_started_at", "")
        payload.setdefault("supervision_token", {})
        payload.setdefault("intake_reset_requested", False)
        payload.setdefault("slug_was_provided", False)
        payload.setdefault("input_question", "")
        payload.setdefault("input_response", "")
        payload.setdefault("backend", "")
        payload.setdefault("safety_mode", "")
        payload.setdefault("backend_source", "")
        payload.setdefault("backend_workspace_root", "")
        # Graceful default for records created before heartbeat_at existed
        if "heartbeat_at" not in payload or not payload["heartbeat_at"]:
            payload["heartbeat_at"] = payload.get("updated_at") or payload.get("created_at") or ""
        if payload["run_mode"] not in RUN_MODES:
            # Normalize legacy "ad_hoc" and any unknown modes to "spec".
            payload["run_mode"] = "spec"
        run = cls(**payload)
        # Legacy run-state JSON may omit agent entirely; retain that distinction so
        # cmd_input can still fall back to the configured default instead of the
        # dataclass field default.
        setattr(run, "_agent_was_recorded", agent_was_recorded)
        if not run.spec_path:
            legacy_path = str(payload.get("generated_spec_path", "") or "").strip()
            if legacy_path and legacy_path != "CURRENT_SPEC.md":
                try:
                    run.spec_path = _normalize_spec_path(legacy_path)
                except ValueError:
                    run.spec_path = _default_spec_path(run.spec_id, run.run_mode)
            else:
                run.spec_path = _default_spec_path(run.spec_id, run.run_mode)
        return run

    @classmethod
    def find_latest(cls, repo_root: Path, spec_id: str) -> RunState | None:
        runs = cls.list_for_spec(repo_root, spec_id)
        return runs[0] if runs else None

    @classmethod
    def list_all(cls, repo_root: Path) -> list[RunState]:
        runs_dir = _state_root(repo_root) / "runs"
        if not runs_dir.exists():
            return []

        runs: list[RunState] = []
        for candidate in sorted(runs_dir.glob("*.json"), key=lambda path: path.name):
            try:
                data = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            runs.append(cls.from_dict(data))

        runs.sort(
            key=lambda run: (
                run.created_at or "",
                run.updated_at or "",
                run.run_id,
            ),
            reverse=True,
        )
        return runs

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_RUN_STATUSES

    @property
    def is_step_selectable(self) -> bool:
        # Manual phase stepping should continue a run that just passed its prior phase.
        return self.status in STEP_SELECTABLE_RUN_STATUSES

    @property
    def is_auto_resumable(self) -> bool:
        return self.status == "passed" and self.phase in AUTO_RESUME_PHASES

    @property
    def is_intake_resume_candidate(self) -> bool:
        return self.status == "passed" and self.phase == "intake"

    @property
    def is_workflow_resumable(self) -> bool:
        return self.is_resumable or self.is_auto_resumable or self.is_intake_resume_candidate

    @property
    def is_superseded(self) -> bool:
        return self.status == "superseded"

    @property
    def is_non_terminal(self) -> bool:
        return self.is_workflow_resumable

    @classmethod
    def list_for_spec(cls, repo_root: Path, spec_id: str) -> list[RunState]:
        runs_dir = _state_root(repo_root) / "runs"
        if not runs_dir.exists():
            return []

        runs: list[RunState] = []
        for candidate in sorted(runs_dir.glob("*.json"), key=lambda path: path.name):
            try:
                data = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            if data.get("spec_id") != spec_id:
                continue
            runs.append(cls.from_dict(data))

        runs.sort(
            key=lambda run: (
                run.created_at or "",
                run.updated_at or "",
                run.run_id,
            ),
            reverse=True,
        )
        return runs


@dataclass(frozen=True)
class MergeOriginMasterResult:
    status: str  # "success" | "noop" | "conflict" | "error"
    stderr: str = ""


@dataclass
class IntakeQuestion:
    id: str
    prompt: str
    input_type: str
    required: bool = True
    constraints: dict = field(default_factory=dict)
    default: object | None = None

    def to_schema_payload(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "input_type": self.input_type,
            "required": self.required,
            "constraints": self.constraints,
            "default": self.default,
        }


@dataclass
class IntakeSpec:
    required: bool = False
    schema_version: int = 1
    questions: list[IntakeQuestion] = field(default_factory=list)
    source_mode: str = "none"

    def schema_hash(self) -> str:
        payload = {
            "required": self.required,
            "schema_version": self.schema_version,
            "questions": [q.to_schema_payload() for q in self.questions],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class IntakeResult:
    version: int = INTAKE_FILE_VERSION
    spec_id: str = ""
    run_id: str = ""
    schema_version: int = 1
    schema_hash: str = ""
    questions: list[dict] = field(default_factory=list)
    answers: dict = field(default_factory=dict)
    completed_at: str = field(default_factory=_now_iso)

    def save(self, repo_root: Path, run_id: str) -> Path:
        d = _state_root(repo_root) / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / "intake.json"
        p.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        return p

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> IntakeResult | None:
        p = _state_root(repo_root) / "runs" / run_id / "intake.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None


@dataclass
class ImplementContext:
    implement_reason: str = "initial"
    objective: str = ""
    recovery_objective: str = ""
    run_id: str = ""
    attempt_number: int = 1
    launch_number: int = 0
    triggering_phase: str = ""
    previous_implement_attempt_number: int = 0
    previous_implement_result_path: str = ""
    triggering_review_result_path: str = ""
    triggering_block_diagnosis_path: str = ""
    run_state_dir: str = ""
    current_head_sha: str = ""
    reviewed_head_sha: str = ""
    first_failed_test_nodeid: str = ""
    first_failed_test_reproducer: str = ""
    first_failed_test_diagnostic: str = ""
    spec_path: str = ""
    spec_revision: str = ""
    acceptance_checklist: list[str] = field(default_factory=list)
    verification_expectations: list[str] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)
    failing_commands: list[str] = field(default_factory=list)
    failure_summary: str = ""
    gate_output: str = ""
    review_feedback_active: bool = False
    stale_review_feedback: bool = False
    review_findings_count: int = 0
    unresolved_review_findings: list[dict] = field(default_factory=list)
    review_source_check_url: str = ""
    mergeability_issue: str = ""
    operator_request_path: str = ""
    operator_request_kind: str = ""
    operator_request_prompt: str = ""
    operator_request_context: dict[str, object] = field(default_factory=dict)
    operator_request_suggested_action: str = ""
    operator_request_options: list[dict[str, str]] = field(default_factory=list)
    operator_request_requires_full_session: bool = False
    operator_request_response: str = ""
    operator_request_response_source: str = ""
    operator_steering_path: str = ""
    operator_steering_event_id: str = ""
    operator_steering_message: str = ""
    operator_steering_context: dict[str, object] = field(default_factory=dict)
    operator_steering_provided_by: str = ""
    operator_steering_provided_at: str = ""
    operator_steering_source: str = ""
    debugger_summary: str = ""
    debugger_root_cause: str = ""
    debugger_confidence: float = 0.0
    debugger_category: str = ""
    debugger_next_best_action: str = ""
    debugger_requires_human_attention: bool = False
    debugger_needs_new_commit: bool = False
    debugger_blocker_signature: str = ""
    debugger_diagnosis_stale: bool = False
    targeted_test_not_executed: bool = False
    targeted_test_not_executed_warning: str = ""
    visual_feedback_available: bool = True
    rescue_snapshot_path: str = ""
    rescue_snapshot_summary: str = ""
    intake: dict = field(default_factory=dict)

    def save(self, repo_root: Path, run_id: str) -> Path:
        d = _state_root(repo_root) / "runs" / run_id
        return _write_latest_and_attempt_artifacts(
            d,
            "implement-context.json",
            asdict(self),
            attempt_number=self.attempt_number,
            launch_number=self.launch_number,
        )

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> ImplementContext | None:
        p = _state_root(repo_root) / "runs" / run_id / "implement-context.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            data.pop("implement_dev_server_warning", None)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    @classmethod
    def load_attempt(
        cls,
        repo_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> ImplementContext | None:
        payload, _ = _load_attempt_or_latest_json_payload(
            _state_root(repo_root) / "runs" / run_id,
            "implement-context.json",
            attempt_number=attempt_number,
        )
        if not isinstance(payload, dict):
            return None
        try:
            payload.pop("implement_dev_server_warning", None)
            return cls(**payload)
        except TypeError:
            return None

    @classmethod
    def attempt_path(cls, repo_root: Path, run_id: str, attempt_number: int) -> Path:
        return _load_attempt_or_latest_text_path(
            _state_root(repo_root) / "runs" / run_id,
            "implement-context.json",
            attempt_number=attempt_number,
        )


@dataclass
class ImplementAttemptContextBundle:
    reason: str
    context: ImplementContext
    pending_guided_retry_signature: str = ""


def _clear_stale_implement_context(repo_root: Path, run_id: str) -> None:
    """Remove a prior attempt's implement context before a fresh launch starts."""
    path = _state_root(repo_root) / "runs" / run_id / "implement-context.json"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not remove stale implement-context for run %s: %s",
            run_id,
            exc,
        )


@dataclass
class ImplementResult:
    status: str = "passed"  # passed | failed | blocked
    summary: str = ""
    commits: list[str] = field(default_factory=list)
    attempt: int | None = None
    launch_number: int = 0
    result_source: str = ""
    completed_at: str = field(default_factory=_now_iso)

    def save_to_state_root(self, state_root: Path, run_id: str) -> Path:
        d = state_root / "runs" / run_id
        return _write_latest_and_attempt_artifacts(
            d,
            "implement-result.json",
            asdict(self),
            attempt_number=_zero_based_attempt_to_human(self.attempt),
            launch_number=self.launch_number,
        )

    def save(self, repo_root: Path, run_id: str) -> Path:
        return self.save_to_state_root(_state_root(repo_root), run_id)

    @classmethod
    def load_from_state_root(
        cls,
        state_root: Path,
        run_id: str,
    ) -> ImplementResult | None:
        p = state_root / "runs" / run_id / "implement-result.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> ImplementResult | None:
        return cls.load_from_state_root(_state_root(repo_root), run_id)

    @classmethod
    def load_attempt_from_state_root(
        cls,
        state_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> ImplementResult | None:
        payload, _ = _load_attempt_or_latest_json_payload(
            state_root / "runs" / run_id,
            "implement-result.json",
            attempt_number=attempt_number,
        )
        if not isinstance(payload, dict):
            return None
        try:
            return cls(**payload)
        except TypeError:
            return None

    @classmethod
    def load_attempt(
        cls,
        repo_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> ImplementResult | None:
        return cls.load_attempt_from_state_root(_state_root(repo_root), run_id, attempt_number)

    @classmethod
    def attempt_path_from_state_root(
        cls,
        state_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> Path:
        return _load_attempt_or_latest_text_path(
            state_root / "runs" / run_id,
            "implement-result.json",
            attempt_number=attempt_number,
        )

    @classmethod
    def attempt_path(cls, repo_root: Path, run_id: str, attempt_number: int) -> Path:
        return cls.attempt_path_from_state_root(_state_root(repo_root), run_id, attempt_number)


HEARTBEAT_PERSIST_INTERVAL_SECONDS = 60


@dataclass
class AgentProgressTracker:
    agent: str
    run_id: str = ""
    repo_root: Path | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)
    last_event_summary: str = ""
    timeout_message: str = ""
    _last_heartbeat_persist: float = field(default_factory=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _inflight_items: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.last_event_summary:
            self.last_event_summary = f"{self.agent} launch"

    def command_started(self, item_id: str) -> None:
        """Mark a blocking agent item (shell command, MCP call) as in flight."""
        with self._lock:
            self._inflight_items.add(item_id)

    def command_finished(self, item_id: str) -> None:
        with self._lock:
            self._inflight_items.discard(item_id)

    def has_inflight_command(self) -> bool:
        with self._lock:
            return bool(self._inflight_items)

    def record(self, summary: str) -> str:
        clean_summary = redact_sensitive(summary.strip()) or f"{self.agent} progress"
        if len(clean_summary) > 160:
            clean_summary = f"{clean_summary[:157]}..."
        with self._lock:
            self.last_progress_at = time.monotonic()
            self.last_event_summary = clean_summary
        self._maybe_persist_heartbeat()
        return clean_summary

    def heartbeat(self) -> None:
        with self._lock:
            self.last_progress_at = time.monotonic()
        self._maybe_persist_heartbeat()

    def _maybe_persist_heartbeat(self) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_heartbeat_persist
            if elapsed < HEARTBEAT_PERSIST_INTERVAL_SECONDS:
                return
            self._last_heartbeat_persist = now
        if not self.run_id or self.repo_root is None:
            return
        p = _run_state_path(self.repo_root, self.run_id)
        if not p.exists():
            return
        try:
            with _locked_state_path(p):
                data = _read_json_dict(p)
                if data is None:
                    return
                data["heartbeat_at"] = _now_iso()
                _write_json_file_atomically(p, data)
        except OSError:
            pass

    def snapshot(self) -> tuple[float, str]:
        with self._lock:
            return time.monotonic() - self.last_progress_at, self.last_event_summary

    def clear_inflight_commands(self) -> None:
        with self._lock:
            self._inflight_items.clear()

    def mark_timeout(self, timeout_seconds: float) -> str:
        idle_for, last_event = self.snapshot()
        message = (
            f"Agent became inactive for {timeout_seconds:.0f}s during implement; "
            f"last progress was '{last_event}' {idle_for:.0f}s ago. "
            "The orchestrator terminated the process automatically."
        )
        with self._lock:
            if not self.timeout_message:
                self.timeout_message = message
            return self.timeout_message


@dataclass
class ReviewFinding:
    id: str = ""
    title: str = ""
    severity: str = ""
    file: str = ""
    start_line: int = 1
    end_line: int = 1
    body: str = ""
    confidence: float = 0.0


@dataclass
class ReviewResult:
    status: str = ""
    summary: str = ""
    findings: list[ReviewFinding] = field(default_factory=list)
    attempt_number: int | None = None
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    reviewer_role: str = ""
    reviewer_agent: str = ""
    source_check_name: str = REVIEW_GATE_CHECK_NAME
    source_check_url: str = ""
    reviewed_at: str = field(default_factory=_now_iso)

    def save(self, repo_root: Path, run_id: str) -> Path:
        d = _state_root(repo_root) / "runs" / run_id
        return _write_latest_and_attempt_artifacts(
            d,
            "review-result.json",
            asdict(self),
            attempt_number=self.attempt_number,
        )

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> ReviewResult | None:
        p = _state_root(repo_root) / "runs" / run_id / "review-result.json"
        return cls.load_from_path(p)

    @classmethod
    def load_from_path(cls, path: Path) -> ReviewResult | None:
        p = path
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, TypeError):
            return None

        findings = data.get("findings", [])
        data["findings"] = [ReviewFinding(**f) for f in findings if isinstance(f, dict)]
        try:
            return cls(**data)
        except TypeError:
            return None

    @classmethod
    def load_attempt(
        cls,
        repo_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> ReviewResult | None:
        payload, path = _load_attempt_or_latest_json_payload(
            _state_root(repo_root) / "runs" / run_id,
            "review-result.json",
            attempt_number=attempt_number,
        )
        if not isinstance(payload, dict):
            return None
        return cls.load_from_path(path)

    @classmethod
    def attempt_path(cls, repo_root: Path, run_id: str, attempt_number: int) -> Path:
        return _load_attempt_or_latest_text_path(
            _state_root(repo_root) / "runs" / run_id,
            "review-result.json",
            attempt_number=attempt_number,
        )


def _coerce_block_diagnosis_evidence(payload: object) -> list[str]:
    if not isinstance(payload, list):
        return []
    evidence: list[str] = []
    for item in payload:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            source = str(item.get("source", "")).strip()
            detail = str(item.get("detail", "")).strip()
            value = ": ".join(part for part in (source, detail) if part)
            if not value:
                value = json.dumps(item, sort_keys=True)
        else:
            value = str(item).strip()
        if value:
            evidence.append(value)
    return evidence


@dataclass
class BlockDiagnosis:
    summary: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    category: str = ""
    evidence: list[str] = field(default_factory=list)
    next_best_action: str = ""
    requires_human_attention: bool = False
    needs_new_commit: bool = False
    blocker_signature: str = ""
    source_phase: str = ""
    block_reason: str = ""
    debugger_agent: str = ""
    first_failed_test_nodeid: str = ""
    attempt_number: int | None = None
    debugged_at: str = field(default_factory=_now_iso)

    def save(self, repo_root: Path, run_id: str) -> Path:
        d = _state_root(repo_root) / "runs" / run_id
        return _write_latest_and_attempt_artifacts(
            d,
            BLOCK_DIAGNOSIS_FILENAME,
            asdict(self),
            attempt_number=self.attempt_number,
        )

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> BlockDiagnosis | None:
        p = _state_root(repo_root) / "runs" / run_id / BLOCK_DIAGNOSIS_FILENAME
        if not p.exists():
            return None
        try:
            return _coerce_block_diagnosis(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    def load_attempt(
        cls,
        repo_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> BlockDiagnosis | None:
        payload, _ = _load_attempt_or_latest_json_payload(
            _state_root(repo_root) / "runs" / run_id,
            BLOCK_DIAGNOSIS_FILENAME,
            attempt_number=attempt_number,
        )
        return _coerce_block_diagnosis(payload)

    @classmethod
    def attempt_path(cls, repo_root: Path, run_id: str, attempt_number: int) -> Path:
        return _load_attempt_or_latest_text_path(
            _state_root(repo_root) / "runs" / run_id,
            BLOCK_DIAGNOSIS_FILENAME,
            attempt_number=attempt_number,
        )


def _coerce_json_bool(val: object) -> bool:
    """Coerce a JSON value to bool, handling string ``"false"``/``"true"``."""
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def _coerce_block_diagnosis(payload: object) -> BlockDiagnosis | None:
    if not isinstance(payload, dict):
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    diagnosis = BlockDiagnosis(
        summary=str(payload.get("summary", "")).strip(),
        root_cause=str(payload.get("root_cause", "")).strip(),
        confidence=confidence,
        category=str(payload.get("category", "")).strip(),
        evidence=_coerce_block_diagnosis_evidence(payload.get("evidence", [])),
        next_best_action=str(payload.get("next_best_action", "")).strip(),
        requires_human_attention=_coerce_json_bool(payload.get("requires_human_attention", False)),
        needs_new_commit=_coerce_json_bool(payload.get("needs_new_commit", False)),
        blocker_signature=str(payload.get("blocker_signature", "")).strip(),
        source_phase=str(payload.get("source_phase", "")).strip(),
        block_reason=str(payload.get("block_reason", "")).strip(),
        debugger_agent=str(payload.get("debugger_agent", "")).strip(),
        first_failed_test_nodeid=str(payload.get("first_failed_test_nodeid", "")).strip(),
        attempt_number=_coerce_optional_int(payload.get("attempt_number")),
        debugged_at=str(payload.get("debugged_at", _now_iso())).strip() or _now_iso(),
    )
    required_fields = (
        diagnosis.summary,
        diagnosis.root_cause,
        diagnosis.category,
        diagnosis.next_best_action,
        diagnosis.blocker_signature,
    )
    if not all(required_fields):
        return None
    if not diagnosis.evidence:
        return None
    return diagnosis


def _coerce_operator_options(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        return []
    options: list[dict[str, str]] = []
    for item in payload:
        if isinstance(item, str):
            value = item.strip()
            if not value:
                continue
            options.append({"value": value, "label": value, "description": ""})
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "") or "").strip()
        label = str(item.get("label", "") or value).strip()
        description = str(item.get("description", "") or "").strip()
        if not value and label:
            value = label
        if not value:
            continue
        options.append(
            {
                "value": value,
                "label": label or value,
                "description": description,
            }
        )
    return options


def _operator_steering_event_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + hashlib.sha256(os.urandom(8)).hexdigest()[:8]


def _operator_steering_event_filename(event_id: str) -> str:
    return f"operator-steering.event-{event_id}.json"


@dataclass
class OperatorSteering:
    message: str = ""
    context: dict[str, object] = field(default_factory=dict)
    provided_by: str = ""
    provided_at: str = field(default_factory=_now_iso)
    source: str = ""
    status: str = "active"  # active | consumed | superseded
    event_id: str = field(default_factory=_operator_steering_event_id)
    superseded_by_event_id: str = ""
    superseded_at: str = ""
    influenced_attempt_number: int | None = None
    influenced_at: str = ""

    def save(
        self,
        repo_root: Path,
        run_id: str,
        *,
        update_latest: bool = True,
    ) -> Path:
        run_dir = _state_root(repo_root) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        event_path = run_dir / _operator_steering_event_filename(self.event_id)
        event_path.write_text(rendered)
        if update_latest:
            (run_dir / OPERATOR_STEERING_FILENAME).write_text(rendered)
        return event_path

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> OperatorSteering | None:
        path = _state_root(repo_root) / "runs" / run_id / OPERATOR_STEERING_FILENAME
        if not path.exists():
            return None
        try:
            return _coerce_operator_steering(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    def list_events(cls, repo_root: Path, run_id: str) -> list[OperatorSteering]:
        run_dir = _state_root(repo_root) / "runs" / run_id
        if not run_dir.exists():
            return []
        events: list[OperatorSteering] = []
        for path in sorted(run_dir.glob("operator-steering.event-*.json")):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            steering = _coerce_operator_steering(payload)
            if steering is not None:
                events.append(steering)
        events.sort(key=lambda item: (item.provided_at, item.event_id), reverse=True)
        return events


def _coerce_operator_steering(payload: object) -> OperatorSteering | None:
    if not isinstance(payload, dict):
        return None
    message = str(payload.get("message", "") or "").strip()
    event_id = str(payload.get("event_id", "") or "").strip()
    if not message or not event_id:
        return None
    return OperatorSteering(
        message=message,
        context=dict(payload.get("context", {})) if isinstance(payload.get("context", {}), dict) else {},
        provided_by=str(payload.get("provided_by", "") or "").strip(),
        provided_at=str(payload.get("provided_at", _now_iso()) or _now_iso()).strip() or _now_iso(),
        source=str(payload.get("source", "") or "").strip(),
        status=str(payload.get("status", "active") or "active").strip() or "active",
        event_id=event_id,
        superseded_by_event_id=str(payload.get("superseded_by_event_id", "") or "").strip(),
        superseded_at=str(payload.get("superseded_at", "") or "").strip(),
        influenced_attempt_number=_coerce_optional_int(payload.get("influenced_attempt_number")),
        influenced_at=str(payload.get("influenced_at", "") or "").strip(),
    )


def _record_operator_steering(
    repo_root: Path,
    run: RunState,
    message: str,
    *,
    source: str,
    provided_by: str = "",
) -> OperatorSteering:
    guidance = str(message or "").strip()
    if not guidance:
        raise RuntimeError("Steering guidance cannot be empty.")

    operator_request = _load_operator_request(repo_root, run)
    if operator_request is not None and operator_request.status == "pending":
        raise RuntimeError(
            "This run is already waiting on a reactive operator request. "
            f"Resolve it with `spec input --spec {run.spec_id}` before adding proactive steering."
        )

    previous = OperatorSteering.load(repo_root, run.run_id)
    steering = OperatorSteering(
        message=guidance,
        context={
            "spec_id": run.spec_id,
            "run_id": run.run_id,
            "phase": run.phase or "",
            "status": run.status or "",
        },
        provided_by=(provided_by or _current_actor()).strip() or "unknown",
        provided_at=_now_iso(),
        source=str(source or "").strip(),
        status="active",
    )
    if previous is not None and previous.status == "active":
        previous.status = "superseded"
        previous.superseded_by_event_id = steering.event_id
        previous.superseded_at = steering.provided_at
        previous.save(repo_root, run.run_id, update_latest=False)
    steering.save(repo_root, run.run_id)
    return steering


def _consume_operator_steering(
    repo_root: Path,
    run_id: str,
    *,
    attempt_number: int,
    expected_event_id: str = "",
) -> None:
    if expected_event_id:
        expected_event_id = expected_event_id.strip()
    if not expected_event_id:
        return
    steering = OperatorSteering.load(repo_root, run_id)
    if steering is None or steering.status != "active":
        return
    if steering.event_id != expected_event_id:
        return
    if steering.influenced_attempt_number is not None:
        return
    steering.influenced_attempt_number = attempt_number
    steering.influenced_at = _now_iso()
    steering.status = "consumed"
    steering.save(repo_root, run_id)


def _format_operator_steering_for_prompt(
    message: str,
    *,
    provided_by: str = "",
    provided_at: str = "",
) -> str:
    lines = [
        "Proactive operator steering:",
        f"- Guidance: {message}",
    ]
    if provided_by:
        lines.append(f"- Provided by: {provided_by}")
    if provided_at:
        lines.append(f"- Provided at: {provided_at}")
    return "\n".join(lines)


@dataclass
class OperatorRequest:
    kind: str = ""  # agent_question | debugger_guidance
    prompt: str = ""
    context: dict[str, object] = field(default_factory=dict)
    suggested_action: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    requires_full_session: bool = False
    status: str = "pending"  # pending | resolved | consumed
    response: str = ""
    response_source: str = ""
    requested_by_phase: str = ""
    requested_at: str = field(default_factory=_now_iso)
    request_attempt_number: int | None = None
    resolved_at: str = ""
    continuation: str = ""
    continuation_source: str = ""
    continuation_selected_at: str = ""
    continuation_session_completed_implement: bool = False
    response_consumed_attempt_number: int | None = None
    consumed_at: str = ""

    def save(self, repo_root: Path, run_id: str) -> Path:
        d = _state_root(repo_root) / "runs" / run_id
        attempt_number = self.response_consumed_attempt_number
        if attempt_number is None:
            attempt_number = self.request_attempt_number
        return _write_latest_and_attempt_artifacts(
            d,
            OPERATOR_REQUEST_FILENAME,
            asdict(self),
            attempt_number=attempt_number,
        )

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> OperatorRequest | None:
        p = _state_root(repo_root) / "runs" / run_id / OPERATOR_REQUEST_FILENAME
        if not p.exists():
            return None
        try:
            return _coerce_operator_request(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    def load_attempt(
        cls,
        repo_root: Path,
        run_id: str,
        attempt_number: int,
    ) -> OperatorRequest | None:
        payload, _ = _load_attempt_or_latest_json_payload(
            _state_root(repo_root) / "runs" / run_id,
            OPERATOR_REQUEST_FILENAME,
            attempt_number=attempt_number,
        )
        return _coerce_operator_request(payload)

    @classmethod
    def attempt_path(cls, repo_root: Path, run_id: str, attempt_number: int) -> Path:
        return _load_attempt_or_latest_text_path(
            _state_root(repo_root) / "runs" / run_id,
            OPERATOR_REQUEST_FILENAME,
            attempt_number=attempt_number,
        )


def _coerce_operator_request(payload: object) -> OperatorRequest | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind", "") or "").strip()
    prompt = str(payload.get("prompt", "") or "").strip()
    if not kind or not prompt:
        return None
    return OperatorRequest(
        kind=kind,
        prompt=prompt,
        context=dict(payload.get("context", {})) if isinstance(payload.get("context", {}), dict) else {},
        suggested_action=str(payload.get("suggested_action", "") or "").strip(),
        options=_coerce_operator_options(payload.get("options", [])),
        requires_full_session=_coerce_json_bool(payload.get("requires_full_session", False)),
        status=str(payload.get("status", "pending") or "pending").strip() or "pending",
        response=str(payload.get("response", "") or "").strip(),
        response_source=str(payload.get("response_source", "") or "").strip(),
        requested_by_phase=str(payload.get("requested_by_phase", "") or "").strip(),
        requested_at=str(payload.get("requested_at", _now_iso()) or _now_iso()).strip() or _now_iso(),
        request_attempt_number=_coerce_optional_int(payload.get("request_attempt_number")),
        resolved_at=str(payload.get("resolved_at", "") or "").strip(),
        continuation=str(payload.get("continuation", "") or "").strip(),
        continuation_source=str(payload.get("continuation_source", "") or "").strip(),
        continuation_selected_at=str(payload.get("continuation_selected_at", "") or "").strip(),
        continuation_session_completed_implement=_coerce_json_bool(
            payload.get("continuation_session_completed_implement", False)
        ),
        response_consumed_attempt_number=_coerce_optional_int(payload.get("response_consumed_attempt_number")),
        consumed_at=str(payload.get("consumed_at", "") or "").strip(),
    )


def _legacy_operator_request_from_run(run: RunState) -> OperatorRequest | None:
    prompt = (run.input_question or "").strip()
    if not prompt:
        return None
    response = (run.input_response or "").strip()
    status = "resolved" if response else "pending"
    resolved_at = run.updated_at if response else ""
    return OperatorRequest(
        kind="agent_question",
        prompt=prompt,
        context={
            "legacy_input_question": True,
            "spec_id": run.spec_id,
        },
        suggested_action="Provide the missing implementation guidance, then resume the run.",
        requires_full_session=_input_requires_full_session(prompt),
        status=status,
        response=response,
        response_source="legacy-input-response" if response else "",
        requested_by_phase=run.phase or "implement",
        requested_at=run.created_at or _now_iso(),
        request_attempt_number=_current_attempt_number(run),
        resolved_at=resolved_at,
    )


def _operator_request_from_block_diagnosis(
    run: RunState,
    diagnosis: BlockDiagnosis,
    *,
    source_phase: str,
) -> OperatorRequest:
    options: list[dict[str, str]] = [
        {
            "value": "resume",
            "label": "Resume with debugger guidance",
            "description": diagnosis.next_best_action or "Apply the debugger's recommended next move.",
        },
        {
            "value": "reset",
            "label": "Reset run",
            "description": "Discard the current run state and start a fresh implementation attempt.",
        },
    ]
    return OperatorRequest(
        kind="debugger_guidance",
        prompt=diagnosis.summary or "Blocked-run debugger requires operator intervention.",
        context={
            "spec_id": run.spec_id,
            "source_phase": source_phase,
            "root_cause": diagnosis.root_cause,
            "confidence": diagnosis.confidence,
            "category": diagnosis.category,
            "evidence": list(diagnosis.evidence),
            "blocker_signature": diagnosis.blocker_signature,
            "block_reason": diagnosis.block_reason,
        },
        suggested_action=diagnosis.next_best_action,
        options=options,
        requires_full_session=False,
        status="pending",
        requested_by_phase=source_phase,
        requested_at=_now_iso(),
        request_attempt_number=_current_attempt_number(run),
    )


def _sync_legacy_input_fields(run: RunState, request: OperatorRequest | None) -> None:
    if request is None or request.kind != "agent_question":
        return
    run.input_question = request.prompt
    run.input_response = request.response


OPERATOR_CONTINUATION_RESUME_IMPLEMENT = "resume_implement"
OPERATOR_CONTINUATION_CONTINUE_WORKFLOW = "continue_workflow"


@dataclass(frozen=True)
class OperatorContinuation:
    action: str
    run_id: str
    spec_id: str
    request_kind: str = ""
    response_source: str = ""
    response: str = ""
    continuation_source: str = ""
    session_completed_implement: bool = False
    requested_by_phase: str = ""

    @property
    def resumes_implement(self) -> bool:
        return self.action == OPERATOR_CONTINUATION_RESUME_IMPLEMENT

    @property
    def continues_workflow(self) -> bool:
        return self.action == OPERATOR_CONTINUATION_CONTINUE_WORKFLOW


def _select_operator_continuation(
    request: OperatorRequest,
    *,
    session_completed_implement: bool,
) -> str:
    if request.kind == "debugger_guidance":
        return OPERATOR_CONTINUATION_RESUME_IMPLEMENT
    if request.kind == "agent_question":
        requested_by_phase = str(request.requested_by_phase or "").strip()
        if requested_by_phase in {"", "implement"} and session_completed_implement:
            return OPERATOR_CONTINUATION_CONTINUE_WORKFLOW
        return OPERATOR_CONTINUATION_RESUME_IMPLEMENT
    return OPERATOR_CONTINUATION_RESUME_IMPLEMENT


def _legacy_operator_continuation_action(run: RunState, request: OperatorRequest) -> str:
    if request.kind == "agent_question" and run.status == "passed" and run.phase == "implement":
        return OPERATOR_CONTINUATION_CONTINUE_WORKFLOW
    return _select_operator_continuation(request, session_completed_implement=False)


def _operator_continuation_from_request(
    run: RunState,
    request: OperatorRequest,
) -> OperatorContinuation | None:
    if not _is_resolved_operator_request(request):
        return None
    # A resolved response requests one implementation attempt. Once that
    # response has been attached to an implement context, ``consumed`` means
    # the attempt has already received it. Treat legacy consumed artifacts
    # that still say ``resume_implement`` as workflow continuations so a
    # successful implement phase cannot loop forever on the same answer.
    if request.status == "consumed":
        action = OPERATOR_CONTINUATION_CONTINUE_WORKFLOW
    else:
        action = str(request.continuation or "").strip()
    if action not in {OPERATOR_CONTINUATION_RESUME_IMPLEMENT, OPERATOR_CONTINUATION_CONTINUE_WORKFLOW}:
        action = _legacy_operator_continuation_action(run, request)
    return OperatorContinuation(
        action=action,
        run_id=run.run_id,
        spec_id=run.spec_id,
        request_kind=request.kind,
        response_source=request.response_source,
        response=request.response,
        continuation_source=request.continuation_source,
        session_completed_implement=request.continuation_session_completed_implement,
        requested_by_phase=request.requested_by_phase,
    )


def _operator_continuation_for_run(repo_root: Path, run: RunState) -> OperatorContinuation | None:
    steering = OperatorSteering.load(repo_root, run.run_id)
    if steering is not None and steering.status == "active":
        return OperatorContinuation(
            action=OPERATOR_CONTINUATION_RESUME_IMPLEMENT,
            run_id=run.run_id,
            spec_id=run.spec_id,
            response=steering.message,
            response_source=steering.source,
            continuation_source="operator-steering",
        )

    request = _load_operator_request(repo_root, run)
    if request is None:
        return None
    return _operator_continuation_from_request(run, request)


def _is_run_workflow_resumable(repo_root: Path, run: RunState) -> bool:
    if run.is_workflow_resumable:
        return True
    if run.status == "passed" and run.phase == "implement":
        return _operator_continuation_for_run(repo_root, run) is not None
    return False


def _passed_implement_continuation_requires_fresh_attempt(
    repo_root: Path,
    run: RunState,
) -> bool:
    if run.status != "passed" or run.phase != "implement":
        return False
    continuation = _operator_continuation_for_run(repo_root, run)
    return continuation is not None and continuation.resumes_implement


def _clear_current_implement_results(repo_root: Path, run: RunState) -> None:
    state_roots = {_state_root(repo_root)}
    try:
        state_roots.add(_worktree_state_root(resolve_worktree_path(run, repo_root)))
    except Exception:
        pass
    for state_root in state_roots:
        try:
            (state_root / "runs" / run.run_id / "implement-result.json").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clear implement result for run %s in %s: %s", run.run_id, state_root, exc)


def resolve_operator_request(
    repo_root: Path,
    run: RunState,
    response: str,
    *,
    source: str,
    session_completed_implement: bool = False,
    allow_debugger_promotion: bool = False,
) -> OperatorContinuation:
    request = _ensure_operator_request(
        repo_root,
        run,
        allow_debugger_promotion=allow_debugger_promotion,
    )
    if request is None:
        raise RuntimeError("No active operator request is available for this run.")
    if request.status not in {"pending", "resolved"}:
        raise RuntimeError(f"Operator request is not resolvable (status={request.status}).")

    resolved_response = str(response or "").strip()
    if not resolved_response:
        raise RuntimeError("Operator response cannot be empty.")

    action = _select_operator_continuation(
        request,
        session_completed_implement=session_completed_implement,
    )
    now = _now_iso()
    request.response = resolved_response
    request.response_source = str(source or "").strip()
    request.status = "resolved"
    request.resolved_at = now
    request.continuation = action
    request.continuation_source = str(source or "").strip()
    request.continuation_selected_at = now
    request.continuation_session_completed_implement = bool(session_completed_implement)
    request.save(repo_root, run.run_id)
    _sync_legacy_input_fields(run, request)

    if action == OPERATOR_CONTINUATION_RESUME_IMPLEMENT:
        run.status = "pending"
        run.phase = "implement"
        _clear_current_implement_results(repo_root, run)
    else:
        run.status = "passed"
    run.save(repo_root)

    return OperatorContinuation(
        action=action,
        run_id=run.run_id,
        spec_id=run.spec_id,
        request_kind=request.kind,
        response_source=request.response_source,
        response=request.response,
        continuation_source=request.continuation_source,
        session_completed_implement=session_completed_implement,
        requested_by_phase=request.requested_by_phase,
    )


def _resolved_operator_request_requires_new_implement_attempt(request: OperatorRequest | None) -> bool:
    if request is None:
        return False
    dummy_run = RunState(run_id="", spec_id="", branch="", phase=request.requested_by_phase or "implement")
    continuation = _operator_continuation_from_request(dummy_run, request)
    return continuation is not None and continuation.resumes_implement


def _fresh_input_completion(
    *,
    before: ImplementResult | None,
    after: ImplementResult | None,
    attempt: int,
    requested_at: str,
) -> bool:
    """Return True when cmd_input observed a fresh successful spec report."""
    if after is None or after.status != "passed":
        return False
    if not _attempt_matches_current_result(after, attempt):
        return False
    if before is None:
        return True
    if asdict(after) != asdict(before):
        return True

    completed_at = _parse_iso_datetime(after.completed_at)
    request_time = _parse_iso_datetime(requested_at)
    return completed_at is not None and request_time is not None and completed_at >= request_time


def _load_operator_request(repo_root: Path, run: RunState) -> OperatorRequest | None:
    request = OperatorRequest.load(repo_root, run.run_id)
    if request is not None:
        return request
    return _legacy_operator_request_from_run(run)


def _ensure_operator_request(
    repo_root: Path,
    run: RunState,
    *,
    allow_debugger_promotion: bool = False,
) -> OperatorRequest | None:
    request = OperatorRequest.load(repo_root, run.run_id)
    if request is not None:
        return request

    legacy_request = _legacy_operator_request_from_run(run)
    if legacy_request is not None:
        legacy_request.save(repo_root, run.run_id)
        return legacy_request

    if not allow_debugger_promotion:
        return None
    diagnosis = BlockDiagnosis.load(repo_root, run.run_id)
    if diagnosis is None or not diagnosis.requires_human_attention:
        return None
    request = _operator_request_from_block_diagnosis(
        run,
        diagnosis,
        source_phase=diagnosis.source_phase or run.phase or "debugger",
    )
    request.save(repo_root, run.run_id)
    return request


def _format_operator_request_for_prompt(request: OperatorRequest) -> str:
    lines = [
        "Resolved operator intervention:",
        f"- Kind: {request.kind}",
        f"- Prompt: {request.prompt}",
    ]
    if request.suggested_action:
        lines.append(f"- Suggested action: {request.suggested_action}")
    if request.options:
        lines.append("- Options:")
        for option in request.options:
            label = option.get("label") or option.get("value") or "option"
            value = option.get("value") or label
            description = option.get("description") or ""
            line = f"  - {label} ({value})"
            if description:
                line += f": {description}"
            lines.append(line)
    lines.append(f"- Response: {request.response}")
    return "\n".join(lines)


def _is_resolved_operator_request(request: OperatorRequest | None) -> bool:
    return request is not None and bool(request.response.strip()) and request.status in {"resolved", "consumed"}


@dataclass
class RetryFailurePackage:
    run_id: str = ""
    attempt_number: int = 1
    run_state_dir: str = ""
    triggering_phase: str = ""
    previous_implement_attempt_number: int = 0
    previous_implement_result_path: str = ""
    review_result_path: str = ""
    block_diagnosis_path: str = ""
    operator_request_path: str = ""
    current_head_sha: str = ""
    reviewed_head_sha: str = ""
    first_failed_test_nodeid: str = ""
    first_failed_test_reproducer: str = ""
    first_failed_test_diagnostic: str = ""
    active_gates_or_checks: list[str] = field(default_factory=list)
    summary_parts: list[str] = field(default_factory=list)
    gate_output_parts: list[str] = field(default_factory=list)
    review_feedback_active: bool = False
    stale_review_feedback: bool = False
    review_summary: str = ""
    review_findings: list[dict] = field(default_factory=list)
    review_findings_count: int = 0
    review_source_check_url: str = ""
    targeted_test_not_executed: bool = False
    targeted_test_not_executed_warning: str = ""
    mergeability_issue: str = ""
    rescue_snapshot_path: str = ""
    rescue_snapshot_summary: str = ""


@dataclass
class ImplementLaunchPlan:
    use_stream_json: bool
    agent_env: dict[str, str]
    agent_cmd: list[str]
    popen_kwargs: dict[str, object]
    progress_tracker: AgentProgressTracker | None = None
    agent_env_redactions: tuple[str, ...] = ()


@dataclass
class ImplementAttemptOutcome:
    exit_code: int
    head_before: str
    head_after: str
    has_new_commit: bool
    has_uncommitted_changes: bool
    use_stream_json: bool = False
    progress_tracker: AgentProgressTracker | None = None


@dataclass(frozen=True)
class WorkflowFailurePolicy:
    retry_reason: str = ""
    next_phase: str = ""
    retry_log_message: str = ""
    retry_cap_source_phase: str = ""
    terminal_reason: str = ""
    publish_retry_cap_draft: bool = False
    confirm_merge_conflict: bool = False
    direct_phase_retry: bool = False


def _run_state_dir_for_run(run_id: str) -> str:
    return f"{SPEC_RUNTIME_CONFIG.paths.state_dir}/runs/{run_id}/"


def _current_attempt_number(run: RunState) -> int:
    return max(1, run.attempts + 1)


def _reserve_implement_launch(run: RunState, repo_root: Path) -> int:
    """Persist and return the next immutable implement launch identity."""
    run.implement_launches = max(0, int(run.implement_launches)) + 1
    run.save(repo_root)
    return run.implement_launches


def _previous_attempt_number(run: RunState) -> int:
    return max(0, _current_attempt_number(run) - 1)


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _no_progress_retry_threshold() -> int:
    raw = os.getenv("SIM_SPEC_NO_PROGRESS_RETRY_THRESHOLD", "").strip()
    if not raw:
        return NO_PROGRESS_RETRY_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        return NO_PROGRESS_RETRY_THRESHOLD
    return value if value > 0 else NO_PROGRESS_RETRY_THRESHOLD


def _is_retryable_implement_failure_message(message: object) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    return (
        "no_handshake" in normalized
        or "agent became inactive" in normalized
        or "targeted test validation" in normalized
    )


def _agent_reported_failure_on_clean_commit(run: RunState) -> bool:
    """Whether the agent reported failure but left committed work behind.

    An agent that reports ``error`` ends its run immediately, at attempt 0. Every
    neighbouring outcome has a path -- ``blocked`` has the environment-block
    inference, ``needs-input`` opens an operator request, a missing handshake gets
    a recovery attempt and retries -- but ``error``, the status an agent naturally
    picks when a gate goes red, has none. One agent's verdict, on its first try,
    ends the run.

    A clean commit is what makes a retry worth spending: there is committed work
    to build on, the retry prompt carries the failure summary, and the attempt is
    bounded by the same retry cap as every other failure. It deliberately does not
    ask *why* the agent failed. A transient cause clears on the next attempt;
    unfinished work gets another pass at finishing; a real defect burns the cap
    and ends the run with the same message it would have ended with at attempt 0.

    Gated on ``implement_agent_reported_failure`` rather than on the commit fields
    alone. Those persist across attempts and ``phase_implement`` has early returns
    ahead of where they are reset, so a prelaunch failure -- a review retry that
    cannot position the workspace at the reviewed head, say, which is deliberately
    non-retryable -- would otherwise be judged on the previous attempt's clean
    commit and quietly spend retry budget instead of surfacing.
    """
    return bool(
        run.implement_agent_reported_failure
        and run.implement_has_new_commit
        and not run.implement_staged_changes
        and not run.implement_unstaged_changes
    )


def _relative_run_artifact_path(repo_root: Path, path: Path) -> str:
    return _try_relative_posix(path, repo_root)


def _find_review_result_for_head(
    repo_root: Path,
    run_id: str,
    *,
    reviewed_head_sha: str,
) -> tuple[ReviewResult | None, Path]:
    run_dir = _state_root(repo_root) / "runs" / run_id
    attempt_candidates = sorted(
        run_dir.glob("review-result.attempt-*.json"),
        key=lambda path: int(re.search(r"\.attempt-(\d+)\.json$", path.name).group(1)),
        reverse=True,
    )
    latest_path = run_dir / "review-result.json"
    for candidate in [*attempt_candidates, latest_path]:
        review_result = ReviewResult.load_from_path(candidate)
        if review_result is None:
            continue
        if review_result.status != "request_changes":
            continue
        if review_result.reviewed_head_sha == reviewed_head_sha:
            return review_result, candidate
    return None, latest_path


def _load_review_result_for_attempt_or_legacy_latest(
    repo_root: Path,
    run_id: str,
    *,
    attempt_number: int,
) -> tuple[ReviewResult | None, Path]:
    if attempt_number <= 0:
        latest_path = _state_root(repo_root) / "runs" / run_id / "review-result.json"
        return ReviewResult.load(repo_root, run_id), latest_path

    attempt_path = ReviewResult.attempt_path(repo_root, run_id, attempt_number)
    review_result = ReviewResult.load_attempt(repo_root, run_id, attempt_number)
    if review_result is not None:
        return review_result, attempt_path

    latest_path = _state_root(repo_root) / "runs" / run_id / "review-result.json"
    latest_result = ReviewResult.load(repo_root, run_id)
    if latest_result is None:
        return None, attempt_path
    if latest_result.attempt_number not in (None, attempt_number):
        return None, attempt_path
    return latest_result, latest_path


def _load_implement_result_for_attempt_or_legacy_latest(
    repo_root: Path,
    run_id: str,
    *,
    attempt_number: int,
) -> tuple[ImplementResult | None, Path]:
    if attempt_number <= 0:
        latest_path = _state_root(repo_root) / "runs" / run_id / "implement-result.json"
        return ImplementResult.load(repo_root, run_id), latest_path

    attempt_path = ImplementResult.attempt_path(repo_root, run_id, attempt_number)
    implement_result = ImplementResult.load_attempt(repo_root, run_id, attempt_number)
    if implement_result is not None:
        return implement_result, attempt_path

    latest_path = _state_root(repo_root) / "runs" / run_id / "implement-result.json"
    latest_result = ImplementResult.load(repo_root, run_id)
    if latest_result is None:
        return None, attempt_path
    expected_attempt = attempt_number - 1
    if latest_result.attempt not in (None, expected_attempt):
        return None, attempt_path
    return latest_result, latest_path


def _git_ref_is_ancestor(worktree_path: Path, ancestor_sha: str, descendant_sha: str) -> bool | None:
    if not ancestor_sha or not descendant_sha:
        return None
    result = run_subprocess(
        ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        cwd=worktree_path,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _has_non_merge_commit_since(worktree_path: Path, base_sha: str, head_sha: str) -> bool:
    if not base_sha or not head_sha or base_sha == head_sha:
        return False
    result = run_subprocess(
        ["git", "rev-list", "--max-count=1", "--no-merges", f"{base_sha}..{head_sha}"],
        cwd=worktree_path,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _review_retry_allows_inferred_success(
    worktree_path: Path,
    ctx: ImplementContext,
    head_sha: str,
) -> bool:
    if ctx.triggering_phase != "review":
        return True
    if not ctx.reviewed_head_sha or not head_sha:
        return False
    relation = _git_ref_is_ancestor(worktree_path, ctx.reviewed_head_sha, head_sha)
    if relation is not True:
        return False
    return _has_non_merge_commit_since(worktree_path, ctx.reviewed_head_sha, head_sha)


def _review_retry_history_rewritten(
    worktree_path: Path,
    ctx: ImplementContext,
    head_sha: str,
) -> bool:
    """Whether a review-triggered retry moved HEAD off the reviewed lineage.

    The retry must *append* commits on top of the reviewed head. When the agent
    rebases/resets/amends instead — for example by recommitting from base — the
    reviewed head is no longer an ancestor of the new head and
    the branch has been rewritten. Returns ``True`` only when that non-ancestry
    is positively confirmed; an indeterminate result (missing objects) returns
    ``False`` so a genuine append is never misclassified as a rewrite.
    """
    if ctx.triggering_phase != "review":
        return False
    reviewed_head = (ctx.reviewed_head_sha or "").strip()
    if not reviewed_head or not head_sha or reviewed_head == head_sha:
        return False
    return _git_ref_is_ancestor(worktree_path, reviewed_head, head_sha) is False


def _review_retry_missing_appended_commit(
    worktree_path: Path,
    ctx: ImplementContext,
    head_sha: str,
) -> bool:
    """Whether a review-triggered retry reported success without appending a commit.

    The retry must *append* at least one commit on top of the
    reviewed head. When the agent reports an explicit success but HEAD carries no
    new non-merge commit beyond the reviewed head — it left HEAD unchanged, or
    only a merge landed — accepting it would re-submit the exact head the review
    already rejected. Returns ``True`` only when that absence is positively
    confirmed for a review-triggered retry; an empty head or non-review phase
    returns ``False`` so a genuine append is never misclassified. History
    rewrites are handled separately by :func:`_review_retry_history_rewritten`.
    """
    if ctx.triggering_phase != "review":
        return False
    reviewed_head = (ctx.reviewed_head_sha or "").strip()
    if not reviewed_head or not head_sha:
        return False
    return not _has_non_merge_commit_since(worktree_path, reviewed_head, head_sha)


def _latest_rescue_snapshot(run: RunState, repo_root: Path) -> dict | None:
    """Return the most recent backend rescue-snapshot manifest for *run*.

    When a backend was forced to reset/restore a workspace it preserves any
    unpushed work under ``<run_root>/rescue/`` and appends the manifest to
    ``rescue/index.json`` (see ``CloneExecutionBackend._rescue_unpushed_work``).
    The worktree backend never writes one, so this returns ``None`` there.
    """
    try:
        backend = _resolve_execution_backend()
    except Exception:
        return None
    if backend.identity.backend == "worktree":
        return None
    workspace_root = Path(backend.identity.workspace_root).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = repo_root / workspace_root
    index_path = workspace_root / run.run_id / "rescue" / "index.json"
    try:
        entries = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(entries, list) or not entries:
        return None
    latest = entries[-1]
    return latest if isinstance(latest, dict) else None


def _rescue_snapshot_fields(run: RunState, repo_root: Path) -> tuple[str, str]:
    """Return ``(rescue_snapshot_path, rescue_snapshot_summary)`` for the most
    recent backend rescue snapshot, or ``("", "")`` when none exists.

    Shared by the failure-package composer and the retry restore path so the
    prompt handed to the next agent always references the freshest rescue —
    including work snapshotted by the restore that fires during this very retry
    launch (which happens *after* the failure package is first built).
    """
    rescue = _latest_rescue_snapshot(run, repo_root)
    if rescue is None:
        return "", ""
    manifest_path = str(rescue.get("manifest_path") or "").strip()
    path = _try_relative_posix(Path(manifest_path), repo_root) if manifest_path else ""
    unpushed = rescue.get("unpushed_commits") or []
    parts: list[str] = []
    if unpushed:
        parts.append(f"{len(unpushed)} unpushed commit(s)")
    artifacts = rescue.get("artifacts") if isinstance(rescue.get("artifacts"), dict) else {}
    if isinstance(artifacts, dict):
        if artifacts.get("bundle"):
            parts.append("git bundle")
        if artifacts.get("uncommitted_patch"):
            parts.append("uncommitted patch")
    return path, ", ".join(parts)


def _build_retry_failure_package(run: RunState, repo_root: Path) -> RetryFailurePackage:
    package = RetryFailurePackage(
        run_id=run.run_id,
        attempt_number=_current_attempt_number(run),
        run_state_dir=_run_state_dir_for_run(run.run_id),
    )
    worktree_path = resolve_worktree_path(run, repo_root)
    if worktree_path.is_dir():
        package.current_head_sha = _head_sha(worktree_path) or ""
    if not package.current_head_sha:
        package.current_head_sha = run.implement_head_sha_after or ""
    package.previous_implement_attempt_number = _previous_attempt_number(run)
    if package.previous_implement_attempt_number > 0:
        package.previous_implement_result_path = _relative_run_artifact_path(
            repo_root,
            ImplementResult.attempt_path(repo_root, run.run_id, package.previous_implement_attempt_number),
        )
    operator_request = _load_operator_request(repo_root, run)
    if operator_request is not None:
        package.operator_request_path = _relative_run_artifact_path(
            repo_root,
            _state_root(repo_root) / "runs" / run.run_id / OPERATOR_REQUEST_FILENAME,
        )

    gate_status_path, gate_data = _read_gate_status(repo_root, run)
    if gate_data is not None:
        try:
            gates = gate_data.get("gates", {})
            ordered_gate_names = [gate for gate in REQUIRED_GATES if gate in gates]
            ordered_gate_names.extend(gate_name for gate_name in gates if gate_name not in REQUIRED_GATES)
            for gate_name in ordered_gate_names:
                gate_entry = gates.get(gate_name)
                if not isinstance(gate_entry, dict):
                    continue
                if gate_entry.get("last_status") == "passed":
                    continue
                package.active_gates_or_checks.append(f"make {gate_name}")
                summary = _format_gate_failure_summary(gate_name, gate_entry)
                if summary:
                    package.summary_parts.append(summary)
                gate_output = _format_gate_retry_output(gate_name, gate_entry)
                if gate_output:
                    package.gate_output_parts.append(gate_output)
                if gate_name == "test" and not package.first_failed_test_nodeid:
                    first_failed_test_nodeid = str(gate_entry.get("first_failed_test_nodeid", "") or "").strip()
                    if not first_failed_test_nodeid:
                        first_failed_test_nodeid = _first_failed_test_nodeid(
                            str(gate_entry.get("last_diagnostic", "") or ""),
                            str(gate_entry.get("last_stdout", "") or ""),
                        )
                    if first_failed_test_nodeid:
                        package.first_failed_test_nodeid = first_failed_test_nodeid
                        package.first_failed_test_reproducer = str(
                            gate_entry.get("first_failed_test_reproducer", "") or ""
                        ).strip()
                        if not package.first_failed_test_reproducer:
                            package.first_failed_test_reproducer = _render_test_gate_targeted_diagnostic_command(
                                resolve_worktree_path(run, repo_root),
                                first_failed_test_nodeid,
                            )
                        first_targeted = gate_entry.get("first_targeted_diagnostic", {})
                        if not isinstance(first_targeted, dict) or not first_targeted:
                            first_targeted = _first_targeted_test_diagnostic(
                                gate_entry.get("last_targeted_diagnostics", []),
                                first_failed_nodeid=first_failed_test_nodeid,
                            )
                        if isinstance(first_targeted, dict):
                            package.first_failed_test_diagnostic = str(first_targeted.get("detail") or "").strip()
        except (AttributeError, TypeError):
            package.summary_parts.append(f"Could not parse gate status from {gate_status_path.as_posix()}")

    if _is_required_checks_failure(run.last_error):
        failed_checks = _required_checks_from_error(run.last_error)
        package.active_gates_or_checks.extend(
            [f"required check: {name}" for name in failed_checks] if failed_checks else ["required GitHub checks"]
        )
        if run.last_error:
            package.summary_parts.append(run.last_error)

    review_attempt_number = package.previous_implement_attempt_number
    review_result, review_result_path = _load_review_result_for_attempt_or_legacy_latest(
        repo_root,
        run.run_id,
        attempt_number=review_attempt_number,
    )
    # Backward scan: if no review result found for the immediately previous attempt,
    # scan backwards through earlier attempts (up to 5) for a request_changes review.
    # However, do NOT resurrect request_changes that were already superseded by a
    # later approval — check that no approved review exists between the found
    # request_changes attempt and the current attempt.
    if (review_result is None or review_result.status != "request_changes") and review_attempt_number > 1:
        # First, check if the immediately previous result was an approval — if so,
        # the prior request_changes is superseded and should not be resurrected.
        # But only if the approval is on the current branch lineage (not stale
        # after a force-push/rebase).
        has_later_approval = review_result is not None and review_result.status == "approved"
        if has_later_approval and package.current_head_sha and review_result.reviewed_head_sha:
            approval_on_lineage = _git_ref_is_ancestor(
                worktree_path, review_result.reviewed_head_sha, package.current_head_sha,
            )
            if approval_on_lineage is not True:
                has_later_approval = False
        if not has_later_approval:
            for scan_attempt in range(review_attempt_number - 1, max(0, review_attempt_number - 5) - 1, -1):
                scan_result, scan_path = _load_review_result_for_attempt_or_legacy_latest(
                    repo_root,
                    run.run_id,
                    attempt_number=scan_attempt,
                )
                if scan_result is not None and scan_result.status == "approved":
                    # Only treat as superseding if the approval is on the
                    # current branch lineage — after a force-push the approval
                    # may cover code that is no longer on the branch.
                    if package.current_head_sha and scan_result.reviewed_head_sha:
                        scan_on_lineage = _git_ref_is_ancestor(
                            worktree_path, scan_result.reviewed_head_sha, package.current_head_sha,
                        )
                        if scan_on_lineage is not True:
                            continue  # Skip this stale approval, keep scanning
                    has_later_approval = True
                    break
                if scan_result is not None and scan_result.status == "request_changes":
                    review_result = scan_result
                    review_result_path = scan_path
                    break
        if has_later_approval:
            # Reset to avoid using a superseded request_changes result.
            review_result = None
    review_requested = False
    # If the reviewed head is not an ancestor of the current head (e.g. after a
    # force-push), discard the review result — its findings cover code that is no
    # longer on the branch.
    if (
        review_result is not None
        and review_result.status == "request_changes"
        and package.current_head_sha
        and review_result.reviewed_head_sha
        and review_result.reviewed_head_sha != package.current_head_sha
    ):
        is_ancestor = _git_ref_is_ancestor(
            worktree_path,
            review_result.reviewed_head_sha,
            package.current_head_sha,
        )
        if is_ancestor is False:
            review_result = None

    if review_result is not None and review_result.status == "request_changes":
        package.review_result_path = _relative_run_artifact_path(repo_root, review_result_path)
        package.reviewed_head_sha = review_result.reviewed_head_sha
        package.review_summary = review_result.summary or "Independent review requested changes."
        package.review_source_check_url = review_result.source_check_url
        package.review_findings = [asdict(finding) for finding in review_result.findings]
        package.review_findings_count = len(review_result.findings)
        if (
            package.current_head_sha
            and review_result.reviewed_head_sha
            and review_result.reviewed_head_sha != package.current_head_sha
        ):
            package.stale_review_feedback = True
        else:
            review_requested = True
    elif run.review_decision_status == "request_changes":
        if review_attempt_number > 0:
            package.review_result_path = _relative_run_artifact_path(repo_root, review_result_path)
        package.reviewed_head_sha = run.review_expected_head_sha
        package.review_summary = run.review_decision_summary or "Independent review requested changes."
        package.review_source_check_url = run.review_decision_check_url
        # If the reviewed head is an ancestor of the current head (implement
        # added commits on top), the review is genuinely stale — mark it so
        # the retry is classified as verify-driven rather than review-driven.
        # If heads differ but are NOT in an ancestor relationship (external
        # drift), keep as review-driven so the lineage validator can block.
        if (
            package.reviewed_head_sha
            and package.current_head_sha
            and package.reviewed_head_sha != package.current_head_sha
            and worktree_path.is_dir()
            and _git_ref_is_ancestor(
                worktree_path, package.reviewed_head_sha, package.current_head_sha,
            ) is True
        ):
            package.stale_review_feedback = True
        else:
            review_requested = True

    if review_requested:
        package.review_feedback_active = True
        review_summary = package.review_summary or "Independent review requested changes."
        if package.review_findings_count:
            review_summary += f" ({package.review_findings_count} unresolved findings)"
        package.summary_parts.append(review_summary)
    elif package.stale_review_feedback:
        stale_summary = "Stale review evidence"
        if package.reviewed_head_sha:
            stale_summary += f" from {_short_sha(package.reviewed_head_sha)}"
        if package.review_findings_count:
            stale_summary += f" ({package.review_findings_count} findings)"
        stale_summary += (
            f": {package.review_summary or 'Independent review findings exist for an older reviewed head.'}"
        )
        package.summary_parts.append(stale_summary)

    mergeability_issue = (run.mergeability_issue or run.merge_conflict_error or "").strip()
    if not mergeability_issue and _is_merge_conflict(run.last_error):
        mergeability_issue = run.last_error.strip()
    if mergeability_issue:
        package.mergeability_issue = mergeability_issue
        package.summary_parts.append(mergeability_issue)

    # Detect if the previous implement result was annotated as having a skipped targeted test.
    if package.previous_implement_attempt_number > 0:
        prev_result = ImplementResult.load_attempt(
            repo_root, run.run_id, package.previous_implement_attempt_number,
        )
        if prev_result is not None and "targeted_test_not_executed" in (prev_result.summary or ""):
            package.targeted_test_not_executed = True
            package.targeted_test_not_executed_warning = (
                run.last_error
                if "targeted test" in (run.last_error or "").lower()
                else (
                    "The targeted test was skipped/deselected locally — "
                    "your prior `ok` was not validated against the actual failing test."
                )
            )

    if _is_retryable_implement_failure_message(run.last_error):
        package.active_gates_or_checks.append("implement agent session")
        package.summary_parts.append(run.last_error.strip())

    if run.review_decision_status == "request_changes" and not package.stale_review_feedback:
        package.triggering_phase = "review"
    elif run.pending_block_debugger_signature:
        package.triggering_phase = "debugger"
        if package.previous_implement_attempt_number > 0:
            diagnosis_path = BlockDiagnosis.attempt_path(repo_root, run.run_id, package.previous_implement_attempt_number)
            package.block_diagnosis_path = _relative_run_artifact_path(repo_root, diagnosis_path)
    elif run.merge_conflict_error:
        package.triggering_phase = "merge"
    elif package.active_gates_or_checks:
        package.triggering_phase = "verify"
    elif run.attempts > 0:
        package.triggering_phase = "implement"

    if _is_resolved_operator_request(operator_request):
        response_summary = f"Operator response ({operator_request.kind}): {operator_request.response}"
        package.summary_parts.append(response_summary)

    package.rescue_snapshot_path, package.rescue_snapshot_summary = _rescue_snapshot_fields(
        run, repo_root
    )

    package.active_gates_or_checks = _dedupe_preserving_order(package.active_gates_or_checks)
    package.summary_parts = _dedupe_preserving_order(package.summary_parts)
    return package


def _gate_failure_marker(gate_entry: dict[str, object]) -> str:
    fingerprint = str(gate_entry.get("failure_fingerprint", "") or "").strip()
    if fingerprint:
        return fingerprint
    detail = "\n".join(
        [
            str(gate_entry.get("last_command", "") or ""),
            str(gate_entry.get("last_stdout", "") or ""),
            str(gate_entry.get("last_stderr", "") or ""),
            str(gate_entry.get("last_diagnostic", "") or ""),
        ]
    ).strip()
    if not detail:
        return ""
    return hashlib.sha256(detail.encode("utf-8")).hexdigest()[:TEST_FAILURE_FINGERPRINT_HEX_LENGTH]


def _orchestrator_phase_result_paths(repo_root: Path, run_id: str) -> list[str]:
    audit_dir = _state_root(repo_root) / "orchestrator"
    if not audit_dir.exists():
        return []
    return sorted(_try_relative_posix(path, repo_root) for path in audit_dir.glob(f"{run_id}-*.json"))


def _read_optional_json_payload(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _worktree_status_text(worktree_path: Path) -> str:
    if not worktree_path.is_dir():
        return ""
    result = run_subprocess(
        ["git", "status", "--short", "--branch"],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip()
    return result.stdout.strip()


def _current_blocked_head_sha(run: RunState, repo_root: Path) -> str:
    worktree_path = resolve_worktree_path(run, repo_root)
    if worktree_path.is_dir():
        head_sha = _head_sha(worktree_path)
        if head_sha:
            return head_sha
    return run.implement_head_sha_after or run.implement_head_sha_before or ""


def _normalize_error_for_fingerprint(error_text: str) -> str:
    """Strip volatile tokens from error text so it can be hashed stably.

    Removes hex SHAs (7+ hex chars), decimal numbers, ISO-ish timestamps,
    and collapses whitespace.  The result preserves the structural content
    of the error while ignoring retry counters, specific SHAs, and
    timestamps that change across otherwise identical blocks.
    """
    import re

    text = error_text
    # Strip ISO-ish timestamps (2026-03-27T13:51:37...)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<TS>", text)
    # Strip hex SHA-like tokens (7+ hex chars, word-bounded)
    text = re.sub(r"\b[0-9a-f]{7,}\b", "<HEX>", text)
    # Strip standalone decimal numbers (attempt counters, line numbers, etc.)
    text = re.sub(r"\b\d+\b", "<N>", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_block_category(error_text: str) -> str:
    """Return a stable category token for the block reason.

    The category captures the *type* of blocker (no-progress, superseded,
    retry-cap, etc.) without volatile details such as retry counters, SHAs,
    or human-readable resume instructions.  This keeps the blocker signature
    deterministic across repeated blocks of the same kind.
    """
    lowered = error_text.lower()
    if "no-progress circuit breaker" in lowered:
        return "no-progress"
    if "superseded" in lowered:
        return "superseded"
    if "unmet dependencies" in lowered or "unmet dependency" in lowered:
        return "unmet-dependencies"
    if "spec not found" in lowered:
        return "spec-not-found"
    if "merge conflict" in lowered:
        return "merge-conflict"
    if "required checks failing" in lowered:
        return "required-checks-failing"
    if "retry" in lowered and ("cap" in lowered or "exhaust" in lowered or "limit" in lowered):
        return "retry-cap"
    if "worktree missing" in lowered:
        return "worktree-missing"
    if error_text.strip():
        return "other"
    return ""


def _compute_blocker_signature(
    run: RunState,
    repo_root: Path,
    *,
    source_phase: str,
    block_reason_override: str | None = None,
) -> str:
    head_sha = _current_blocked_head_sha(run, repo_root)
    gate_status_path, gate_data = _read_gate_status(repo_root, run)
    failing_gates: list[dict[str, str]] = []
    if gate_data is not None:
        gates = gate_data.get("gates", {})
        if isinstance(gates, dict):
            ordered_names = [gate for gate in REQUIRED_GATES if gate in gates]
            ordered_names.extend(gate for gate in gates if gate not in ordered_names)
            for gate_name in ordered_names:
                gate_entry = gates.get(gate_name)
                if not isinstance(gate_entry, dict):
                    continue
                if str(gate_entry.get("last_status", "")).strip() == "passed":
                    continue
                failing_gates.append(
                    {
                        "name": gate_name,
                        "command": str(gate_entry.get("last_command", "") or ""),
                        "marker": _gate_failure_marker(gate_entry),
                    }
                )

    review_attempt_number = (
        _current_attempt_number(run)
        if source_phase == "review"
        else _previous_attempt_number(run)
        if run.review_decision_status == "request_changes"
        else _current_attempt_number(run)
    )
    review_result, _ = _load_review_result_for_attempt_or_legacy_latest(
        repo_root,
        run.run_id,
        attempt_number=review_attempt_number,
    )
    # Include a fingerprint of individual findings so that different
    # request-changes sets on the same head SHA produce distinct signatures
    # (prevents stale diagnosis reuse when findings change — F2).
    review_findings_fingerprint = ""
    if review_result is not None and review_result.findings:
        findings_data = [
            {
                "id": f.id,
                "title": f.title,
                "severity": f.severity,
                "file": f.file,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "body": f.body,
            }
            for f in sorted(review_result.findings, key=lambda f: f.id)
        ]
        review_findings_fingerprint = json.dumps(findings_data, sort_keys=True, separators=(",", ":"))
    review_state = {
        "status": (
            review_result.status if review_result is not None and review_result.status else run.review_decision_status
        ),
        "head_sha": (
            review_result.reviewed_head_sha
            if review_result is not None and review_result.reviewed_head_sha
            else run.review_expected_head_sha
        ),
        "findings_fingerprint": review_findings_fingerprint,
    }
    implement_result_fingerprint = ""
    impl_result, _ = _load_implement_result_for_attempt_or_legacy_latest(
        repo_root,
        run.run_id,
        attempt_number=_current_attempt_number(run),
    )
    if impl_result is not None:
        # Exclude summary/timestamps because they are volatile prose rather than
        # durable evidence; the commit list captures what changed materially.
        fingerprint_payload = {
            "status": impl_result.status,
            "commits": sorted(impl_result.commits),
        }
        implement_result_fingerprint = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))

    # Extract a *stable* block category and a normalized error fingerprint.
    # The category captures the *type* of blocker; the normalized fingerprint
    # adds enough detail to distinguish different errors within the same
    # category (e.g. two distinct "other" errors) without including volatile
    # tokens (retry counters, SHAs, timestamps) that would break
    # signature-based reuse and escalation.
    effective_error = block_reason_override if block_reason_override is not None else run.last_error
    block_category = _extract_block_category(effective_error or "")
    normalized_error = _normalize_error_for_fingerprint(effective_error or "")
    error_fingerprint = hashlib.sha256(normalized_error.encode("utf-8")).hexdigest()

    payload = {
        "head_sha": head_sha,
        "source_phase": source_phase,
        "block_category": block_category,
        "error_fingerprint": error_fingerprint,
        "failing_gates": failing_gates,
        "required_checks": sorted(_required_checks_from_error(effective_error)),
        "review_state": review_state,
        "mergeability_issue": (run.mergeability_issue or run.merge_conflict_error or "").strip(),
        "implement_tree_status": run.implement_tree_status,
        "implement_result_fingerprint": implement_result_fingerprint,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _try_relative_posix(path: Path, base: Path) -> str:
    """Return *path* relative to *base* as a POSIX string, or the absolute path if not possible."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _resolve_run_artifact_reference(
    repo_root: Path,
    reference: str,
    *,
    default_path: Path,
) -> Path:
    ref = str(reference or "").strip()
    if not ref:
        return default_path
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate
    return repo_root / candidate


def _write_block_debugger_context(
    run: RunState,
    repo_root: Path,
    *,
    source_phase: str,
    blocker_signature: str,
) -> tuple[Path, str, str]:
    worktree_path = resolve_worktree_path(run, repo_root)
    worktree_status = _worktree_status_text(worktree_path)
    head_sha = _current_blocked_head_sha(run, repo_root)
    run_state_dir = _state_root(repo_root) / "runs" / run.run_id
    current_attempt_number = _current_attempt_number(run)
    current_context = ImplementContext.load_attempt(repo_root, run.run_id, current_attempt_number)
    implement_context_path = ImplementContext.attempt_path(repo_root, run.run_id, current_attempt_number)
    implement_result_path = ImplementResult.attempt_path(repo_root, run.run_id, current_attempt_number)
    gate_status_path = run_state_dir / "gate-status.json"
    review_result_path = _resolve_run_artifact_reference(
        repo_root,
        current_context.triggering_review_result_path if current_context is not None else "",
        default_path=ReviewResult.attempt_path(repo_root, run.run_id, current_attempt_number),
    )
    previous_implement_result_path = _resolve_run_artifact_reference(
        repo_root,
        current_context.previous_implement_result_path if current_context is not None else "",
        default_path=implement_result_path,
    )
    block_diagnosis_path = _resolve_run_artifact_reference(
        repo_root,
        current_context.triggering_block_diagnosis_path if current_context is not None else "",
        default_path=BlockDiagnosis.attempt_path(repo_root, run.run_id, current_attempt_number),
    )
    artifact_paths = _block_debugger_artifact_paths(repo_root, run.run_id)
    payload = {
        "spec_id": run.spec_id,
        "run_id": run.run_id,
        "source_phase": source_phase,
        "block_reason": run.last_error,
        "blocker_signature": blocker_signature,
        "evidence_root": _run_state_dir_for_run(run.run_id),
        "spec_path": _spec_path_for_run(run),
        "spec_revision": run.spec_revision,
        "worktree_path": str(worktree_path),
        "current_head_sha": head_sha,
        "worktree_status": worktree_status,
        "artifacts": {
            "implement_context": {
                "path": _try_relative_posix(implement_context_path, repo_root),
                "exists": implement_context_path.is_file(),
                "payload": _read_optional_json_payload(implement_context_path),
            },
            "implement_result": {
                "path": _try_relative_posix(implement_result_path, repo_root),
                "exists": implement_result_path.is_file(),
                "payload": _read_optional_json_payload(implement_result_path),
            },
            "previous_implement_result": {
                "path": _try_relative_posix(previous_implement_result_path, repo_root),
                "exists": previous_implement_result_path.is_file(),
                "payload": _read_optional_json_payload(previous_implement_result_path),
            },
            "gate_status": {
                "path": _try_relative_posix(gate_status_path, repo_root),
                "exists": gate_status_path.is_file(),
                "payload": _read_optional_json_payload(gate_status_path),
            },
            "review_result": {
                "path": _try_relative_posix(review_result_path, repo_root),
                "exists": review_result_path.is_file(),
                "payload": _read_optional_json_payload(review_result_path),
            },
            "block_diagnosis": {
                "path": _try_relative_posix(block_diagnosis_path, repo_root),
                "exists": block_diagnosis_path.is_file(),
                "payload": _read_optional_json_payload(block_diagnosis_path),
            },
        },
        "phase_result_files": [
            {
                "path": p,
                "payload": _read_optional_json_payload(repo_root / p),
            }
            for p in _orchestrator_phase_result_paths(repo_root, run.run_id)
        ],
    }
    artifact_paths["context"].parent.mkdir(parents=True, exist_ok=True)
    artifact_paths["context"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return artifact_paths["context"], head_sha, worktree_status


def _escalate_repeated_block_diagnosis(
    diagnosis: BlockDiagnosis,
    *,
    source_phase: str,
    debugger_agent: str,
) -> BlockDiagnosis:
    evidence = list(diagnosis.evidence)
    escalation_note = "The same blocker_signature recurred after one debugger-guided implement retry without any material evidence change."
    if escalation_note not in evidence:
        evidence.append(escalation_note)
    summary = diagnosis.summary.rstrip(".")
    if summary:
        summary = f"{summary}. Unchanged blocker recurred after one guided retry; human attention is now required."
    else:
        summary = "Unchanged blocker recurred after one guided retry; human attention is now required."
    return BlockDiagnosis(
        summary=summary,
        root_cause=diagnosis.root_cause,
        confidence=max(diagnosis.confidence, 0.85),
        category=diagnosis.category or "repeated-block",
        evidence=evidence,
        next_best_action=(
            "A human should inspect the existing diagnosis artifact, decide whether the spec, environment, or branch "
            "state is wrong, and only then launch another implement attempt."
        ),
        requires_human_attention=True,
        needs_new_commit=diagnosis.needs_new_commit,
        blocker_signature=diagnosis.blocker_signature,
        source_phase=source_phase,
        debugger_agent=diagnosis.debugger_agent or debugger_agent,
        debugged_at=_now_iso(),
    )


def _record_block_debugger_phase_audit(
    repo_root: Path,
    run: RunState,
    *,
    result_status: str,
    error_code: str = "",
) -> None:
    _persist_audit(
        repo_root,
        run,
        "debugger",
        OrchestratorResult(
            status=result_status,
            started_at=_now_iso(),
            finished_at=_now_iso(),
            error_code=error_code,
        ),
    )


def _matching_pending_block_diagnosis(
    run: RunState,
    diagnosis: BlockDiagnosis | None,
) -> bool:
    return bool(
        diagnosis is not None
        and run.pending_block_debugger_signature
        and not diagnosis.requires_human_attention
        and diagnosis.blocker_signature == run.pending_block_debugger_signature
    )


def _claim_block_debugger_auto_resume(
    run: RunState,
    repo_root: Path,
    diagnosis: BlockDiagnosis | None,
) -> bool:
    """Persist the one-shot autopilot debugger-resume grant before dispatch.

    Explicit operator resumes deliberately bypass this allowance. Persisting
    before resetting the blocked run means a setup/prelaunch failure cannot
    make the automatic resume free and create another dispatch loop.
    """
    if _current_actor() != "autopilot" or not _matching_pending_block_diagnosis(run, diagnosis):
        return False
    used = max(0, int(run.block_debugger_auto_resumes))
    if used >= BLOCK_DEBUGGER_AUTO_RESUME_LIMIT:
        raise BlockDebuggerAutoResumeExhausted(
            "Automatic blocked-run debugger resume allowance is exhausted for "
            f"{run.run_id}; resume explicitly as an operator if another attempt is intended."
        )
    run.block_debugger_auto_resumes = used + 1
    run.save(repo_root)
    _persist_audit(
        repo_root,
        run,
        "debugger-resume",
        OrchestratorResult(
            status="passed",
            finished_at=_now_iso(),
            stdout_tail=(
                "claimed automatic debugger-guided resume "
                f"{run.block_debugger_auto_resumes}/{BLOCK_DEBUGGER_AUTO_RESUME_LIMIT} "
                f"for signature {run.pending_block_debugger_signature}"
            ),
        ),
    )
    return True


def _stage_block_debugger_isolated_config(
    agent: AgentAdapter,
    debug_worktree: Path,
) -> tuple[Path | None, Path | None]:
    """Create an isolated debugger config with no MCP server capabilities."""
    if not agent.capabilities.supports_mcp:
        return None, None
    if agent.name == "claude":
        empty_config = _mcp_config_path(debug_worktree)
        empty_config.parent.mkdir(parents=True, exist_ok=True)
        empty_config.write_text('{"mcpServers": {}}\n')
        return empty_config, None
    if agent.name == "codex":
        codex_home = _write_codex_isolated_home(
            debug_worktree,
            mcp_servers={},
            copy_auth=False,
        )
        return None, codex_home
    return None, None


def _maybe_run_block_debugger(
    run: RunState,
    repo_root: Path,
    *,
    source_phase: str,
) -> BlockDiagnosis | None:
    original_last_error = run.last_error
    blocker_signature = _compute_blocker_signature(
        run,
        repo_root,
        source_phase=source_phase,
    )
    artifact_paths = _block_debugger_artifact_paths(repo_root, run.run_id)
    existing = BlockDiagnosis.load(repo_root, run.run_id)
    debugger_agent = _effective_review_agent(run)

    if existing is not None and existing.blocker_signature == blocker_signature:
        diagnosis = existing
        if not diagnosis.block_reason:
            diagnosis.block_reason = run.last_error or ""
        if (
            run.last_block_debugger_guided_retry_signature == blocker_signature
            and not diagnosis.requires_human_attention
        ):
            diagnosis = _escalate_repeated_block_diagnosis(
                diagnosis,
                source_phase=source_phase,
                debugger_agent=debugger_agent,
            )
            diagnosis.attempt_number = _current_attempt_number(run)
            diagnosis.save(repo_root, run.run_id)
        _record_block_debugger_phase_audit(repo_root, run, result_status="passed")
    else:
        # Keep the last valid diagnosis visible until a replacement has been
        # parsed and saved. Context/prompt/raw output are per-invocation scratch;
        # a failed debugger must not erase the only useful recovery artifact.
        _clear_block_debugger_artifacts(artifact_paths, keep_diagnosis=True)
        try:
            context_path, head_sha, worktree_status = _write_block_debugger_context(
                run,
                repo_root,
                source_phase=source_phase,
                blocker_signature=blocker_signature,
            )
            run_state_dir = _state_root(repo_root) / "runs" / run.run_id
            prompt = _render_block_debugger_prompt(
                spec_id=run.spec_id,
                run_id=run.run_id,
                source_phase=source_phase,
                block_reason=run.last_error,
                current_head_sha=head_sha,
                worktree_status=worktree_status,
                context_path=str(context_path),
                evidence_root=str(run_state_dir),
            )
            schema_path = _block_debugger_schema_path(repo_root)
            agent = get_agent_adapter(debugger_agent)
            if debugger_agent == "codex" and not _validate_codex_exec(run, require_output_schema=True):
                validation_error = run.last_error or "Codex CLI cannot run the blocked-run debugger."
                run.last_error = original_last_error
                raise ValueError(validation_error)
            if agent.capabilities.review_output_on_stdout and schema_path.is_file():
                schema_text = schema_path.read_text().strip()
                prompt = (
                    f"{prompt}\n\n"
                    "You MUST output a single JSON object (no markdown wrapping) matching this schema:\n"
                    f"```json\n{schema_text}\n```"
                )
            if context_path.is_file():
                # Debuggers never receive state-dir access: Codex --add-dir is
                # writable, and Claude has no equivalent read-only grant.
                # Inline the complete evidence package for both agents.
                try:
                    context_content = context_path.read_text().strip()
                    prompt += (
                        "\n\nInlined evidence context "
                        f"(from {_try_relative_posix(context_path, repo_root)}):\n"
                        f"```json\n{context_content}\n```"
                    )
                except OSError:
                    pass
            artifact_paths["prompt"].write_text(prompt)
            # Always use a temporary worktree so the debugger cannot mutate
            # the real PR branch. At bootstrap-time
            # blocks no worktree/head SHA exists yet, so fall back to the
            # configured base ref resolved to a commit SHA — passing a ref name
            # would crash either in `git worktree add` (empty string) or in the
            # post-add SHA validation (ref name != resolved commit SHA).
            if head_sha:
                effective_head_sha = head_sha
            else:
                fallback_ref = run.base_ref or BASE_REF
                effective_head_sha = _resolve_git_ref_sha(repo_root, fallback_ref)
                if not effective_head_sha:
                    raise ValueError(
                        "Could not resolve fallback ref for blocked-run debugger worktree: "
                        f"{fallback_ref}"
                    )
            # Stage isolated, explicitly empty MCP config before the worktree
            # is sealed read-only. A filesystem read-only debugger can still
            # mutate external systems through MCP, so debugger sessions never
            # receive setup or user-registered servers.
            #
            # block-debugger worktree is chmod'd read-only after creation, so
            # writing MCP files inside the context manager would fail with
            # PermissionError. The Codex isolated home is also returned as a
            # path to keep writable, since Codex writes its own session/state
            # bookkeeping into ``CODEX_HOME`` at runtime.
            mcp_setup: dict[str, Path | None] = {
                "mcp_config_path": None,
                "codex_home": None,
            }

            def _block_debugger_pre_seal(debug_worktree: Path) -> Path | None:
                mcp_config_path, codex_home = _stage_block_debugger_isolated_config(
                    agent,
                    debug_worktree,
                )
                mcp_setup["mcp_config_path"] = mcp_config_path
                mcp_setup["codex_home"] = codex_home
                return codex_home

            worktree_ctx = _temporary_block_debugger_worktree(
                repo_root,
                head_sha=effective_head_sha,
                surviving_workspace=_validated_block_debugger_surviving_workspace(run, repo_root),
                pre_seal=_block_debugger_pre_seal,
            )
            # Codex's --add-dir is writable even under the read-only sandbox.
            # Give it a fresh scratch directory for ``-o`` rather than the
            # authoritative state tree, validate that output after exit, and
            # only then copy it into the run artifact directory ourselves.
            # Claude continues to return JSON on stdout and is written to the
            # established raw-output artifact path below.
            with tempfile.TemporaryDirectory(prefix="spec-block-debug-output-") as output_dir_name:
                output_dir = Path(output_dir_name)
                command_output_path = (
                    output_dir / BLOCK_DEBUGGER_RAW_OUTPUT_FILENAME
                    if debugger_agent == "codex"
                    else artifact_paths["raw_output"]
                )
                with worktree_ctx as (debug_worktree, wt_extra_env):
                    debugger_mcp_config_path = mcp_setup["mcp_config_path"]
                    debugger_codex_home = mcp_setup["codex_home"]
                    cmd = _build_block_debugger_command(
                        prompt=prompt,
                        schema_path=schema_path,
                        output_path=command_output_path,
                        agent_name=debugger_agent,
                        writable_output_dir=(
                            output_dir if debugger_agent == "codex" else None
                        ),
                        mcp_config_path=debugger_mcp_config_path,
                    )
                    debugger_env = _build_local_review_env(extra_git_configs=wt_extra_env)
                    if debugger_codex_home is not None:
                        debugger_env = _subprocess_env_with_codex_home(
                            debugger_env, debugger_codex_home
                        )
                    completed = _run_local_review_subprocess(
                        repo_root,
                        cmd,
                        cwd=debug_worktree,
                        env=debugger_env,
                        timeout=BLOCK_DEBUGGER_TIMEOUT_SECONDS,
                    )
                    if agent.capabilities.review_output_on_stdout:
                        stdout_text = _coerce_subprocess_stream_text(completed.stdout).strip()
                        if stdout_text:
                            json_text = stdout_text
                            brace_start = stdout_text.find("{")
                            brace_end = stdout_text.rfind("}")
                            if brace_start >= 0 and brace_end > brace_start:
                                json_text = stdout_text[brace_start : brace_end + 1]
                            artifact_paths["raw_output"].write_text(json_text)
                    if completed.returncode != 0:
                        raise ValueError(
                            f"Blocked-run debugger agent failed (exit_code={completed.returncode}): "
                            f"{_format_subprocess_failure(completed)}"
                        )
                diagnosis = _load_block_diagnosis_from_output(command_output_path)
                if debugger_agent == "codex":
                    shutil.copyfile(command_output_path, artifact_paths["raw_output"])
            diagnosis.blocker_signature = blocker_signature
            diagnosis.source_phase = source_phase
            diagnosis.block_reason = run.last_error or ""
            diagnosis.debugger_agent = debugger_agent
            diagnosis.attempt_number = _current_attempt_number(run)
            diagnosis.debugged_at = _now_iso()
            diagnosis.first_failed_test_nodeid = _diagnosis_first_failed_test_nodeid(repo_root, run)
            diagnosis.save(repo_root, run.run_id)
            _record_block_debugger_phase_audit(repo_root, run, result_status="passed")
        except (ValueError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            run.last_error = original_last_error
            _record_nonfatal_warning(
                run,
                phase="debugger",
                failure_type="debugger",
                failure_subtype="diagnosis_failed",
                summary="Blocked-run debugger did not produce a diagnosis artifact.",
                detail=str(exc),
            )
            _record_block_debugger_phase_audit(
                repo_root,
                run,
                result_status="failed",
                error_code=str(exc),
            )
            return None

    if diagnosis.requires_human_attention:
        run.pending_block_debugger_signature = ""
        request = _operator_request_from_block_diagnosis(
            run,
            diagnosis,
            source_phase=source_phase,
        )
        request.save(repo_root, run.run_id)
        run.status = "waiting-for-input"
    else:
        run.pending_block_debugger_signature = diagnosis.blocker_signature
    return diagnosis


# ---------------------------------------------------------------------------
# SpecLock — per-spec file lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockOwner:
    """Identity of the process currently holding a :class:`SpecLock`."""

    pid: int
    started_at: str = ""
    command: str = ""

    def describe(self) -> str:
        parts = [f"pid={self.pid}" if self.pid else "pid=unknown"]
        if self.started_at:
            parts.append(f"started={self.started_at}")
        if self.command:
            parts.append(f"command={self.command}")
        return " ".join(parts)


def _spec_lock_path(repo_root: Path, spec_id: str) -> Path:
    return _state_root(repo_root) / "locks" / f"{spec_id}.lock"


class SpecLock:
    """Cross-platform non-blocking per-spec file lock.

    The holder records its process identity (pid, start time, command) in the
    lock file so a contender can surface *who* holds the lock instead of only
    reporting that contention occurred. See :func:`read_spec_lock_owner`.
    """

    def __init__(self, repo_root: Path, spec_id: str):
        self._path = _spec_lock_path(repo_root, spec_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock: FileLock | None = None

    def __enter__(self) -> SpecLock:
        self._lock = FileLock(self._path, blocking=False)
        if not self._lock.acquire():
            self._lock = None
            owner = read_spec_lock_owner_from_path(self._path)
            owner_hint = f" (held by {owner.describe()})" if owner is not None else ""
            raise RuntimeError(
                f"Lock contention: another process holds the lock for {self._path.stem}{owner_hint}"
            )
        self._record_owner()
        return self

    def _record_owner(self) -> None:
        if self._lock is None or self._lock.file is None:
            return
        identity: ProcessIdentity | None
        try:
            identity = read_process_identity(os.getpid())
        except Exception:  # noqa: BLE001 - owner metadata is best-effort
            identity = None
        payload = {
            "pid": os.getpid(),
            "started_at": identity.started_at if identity is not None else "",
            "command": identity.command if identity is not None else "",
        }
        try:
            stream = self._lock.file
            stream.seek(lock_metadata_offset())
            stream.truncate()
            stream.write((json.dumps(payload) + "\n").encode("utf-8"))
            stream.flush()
        except OSError:
            pass

    def __exit__(self, *_: object) -> None:
        if self._lock is not None:
            try:
                stream = self._lock.file
                if stream is not None:
                    stream.seek(lock_metadata_offset())
                    stream.truncate()
            except OSError:
                pass
            self._lock.release()
            self._lock = None


def _parse_lock_owner_payload(raw: str) -> LockOwner:
    text = (raw or "").lstrip("\0").strip()
    if not text:
        return LockOwner(pid=0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return LockOwner(pid=0)
    if not isinstance(payload, dict):
        return LockOwner(pid=0)
    try:
        pid = int(payload.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    return LockOwner(
        pid=pid if pid > 0 else 0,
        started_at=str(payload.get("started_at", "") or "").strip(),
        command=str(payload.get("command", "") or "").strip(),
    )


def read_spec_lock_owner_from_path(path: Path) -> LockOwner | None:
    """Return the owner of an *actively held* lock file, or ``None`` if free.

    The lock file existing on disk does not mean it is held — ``flock`` locks
    release when the owning process exits. We probe with a non-blocking
    exclusive ``flock``: if the probe succeeds the lock is free (we immediately
    release), otherwise it is held and we read the recorded owner metadata.
    """
    if not path.exists():
        return None
    try:
        probe = FileLock(path, blocking=False)
    except OSError:
        return None
    try:
        if not probe.acquire():
            try:
                raw = read_lock_metadata(path)
            except OSError:
                raw = ""
            return _parse_lock_owner_payload(raw)
        return None
    finally:
        probe.release()


def read_spec_lock_owner(repo_root: Path, spec_id: str) -> LockOwner | None:
    """Return the owner of the spec's lock when actively held, else ``None``."""
    return read_spec_lock_owner_from_path(_spec_lock_path(repo_root, spec_id))


# ---------------------------------------------------------------------------
# Review + PR helpers
# ---------------------------------------------------------------------------


def _short_sha(sha: str) -> str:
    return sha[:12] if sha else ""


def _summarize_review_findings(findings: list[ReviewFinding], limit: int = 3) -> str:
    if not findings:
        return "(none)"
    snippets = []
    for finding in findings[:limit]:
        location = f"{finding.file}:{finding.start_line}-{finding.end_line}" if finding.file else "unknown location"
        snippets.append(f"[{finding.severity or 'P2'}] {finding.title or finding.id} ({location})")
    return "; ".join(snippets)


def _coerce_line_number(value: object, default: int = 1) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_confidence(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _coerce_saved_review_findings(findings: object) -> list[dict]:
    if not isinstance(findings, list):
        return []

    normalized: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        start_line = _coerce_line_number(finding.get("start_line"), default=1)
        end_line = _coerce_line_number(finding.get("end_line"), default=start_line)
        normalized.append(
            {
                "id": str(finding.get("id", "") or "").strip(),
                "title": str(finding.get("title", "") or "").strip(),
                "severity": str(finding.get("severity", "") or "").strip(),
                "file": str(finding.get("file", "") or "").strip(),
                "start_line": start_line,
                "end_line": end_line,
                "body": str(finding.get("body", "") or "").strip(),
                "confidence": _coerce_confidence(finding.get("confidence")),
            }
        )
    return normalized


def _rank_review_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    ranked = list(enumerate(findings))
    ranked.sort(key=lambda item: (-item[1].confidence, item[0]))
    return [finding for _, finding in ranked]


def _top_review_findings(findings: list[ReviewFinding]) -> list[dict]:
    return [asdict(finding) for finding in _rank_review_findings(findings)[:CARRIED_FORWARD_REVIEW_FINDINGS_LIMIT]]


def _parse_json_object(text: str) -> dict | None:
    if not text:
        return None

    stripped = text.strip()
    candidates = [stripped]
    for match in re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidates.append(match.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1].strip())

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _normalize_review_decision(raw: object) -> str:
    val = str(raw or "").strip().lower()
    aliases = {
        "approve": "approved",
        "approved": "approved",
        "changes_requested": "request_changes",
        "request_changes": "request_changes",
        "request-changes": "request_changes",
        "blocked": "blocked",
        "failed": "failed",
    }
    return aliases.get(val, "")


def _normalize_review_finding(item: dict, index: int) -> ReviewFinding:
    start_line = _coerce_line_number(
        item.get("start_line", item.get("line", 1)),
        default=1,
    )
    end_line = _coerce_line_number(
        item.get("end_line", item.get("line", start_line)),
        default=start_line,
    )
    if end_line < start_line:
        end_line = start_line
    finding_id = str(item.get("id") or f"finding-{index + 1}").strip()
    title = str(item.get("title") or f"Finding {index + 1}").strip()
    severity = str(item.get("severity") or "").strip()
    file_path = str(item.get("file") or "").strip()
    body = str(item.get("body") or "").strip()
    confidence = _coerce_confidence(item.get("confidence", 0.0))
    return ReviewFinding(
        id=finding_id,
        title=title,
        severity=severity,
        file=file_path,
        start_line=start_line,
        end_line=end_line,
        body=body,
        confidence=confidence,
    )


def _is_review_payload_candidate(payload: dict) -> bool:
    if any(
        key in payload
        for key in (
            "decision",
            "reviewed_head_sha",
            "head_sha",
            "reviewed_base_sha",
            "base_sha",
            "findings",
        )
    ):
        return True
    if "status" in payload:
        # Treat any status-bearing payload as a candidate so malformed values
        # fail closed instead of falling back to synthetic approval.
        return True
    return False


def _normalize_review_payload(
    payload: dict,
    *,
    expected_head_sha: str,
    expected_base_sha: str,
    check_run: dict,
) -> ReviewResult:
    decision = _normalize_review_decision(payload.get("status", payload.get("decision")))
    if decision not in REVIEW_DECISION_VALUES:
        raise ValueError("review payload has invalid decision/status")

    reviewed_head_sha = str(payload.get("reviewed_head_sha", payload.get("head_sha", ""))).strip()
    if not reviewed_head_sha:
        raise ValueError("review payload missing reviewed_head_sha")
    if reviewed_head_sha != expected_head_sha:
        raise ValueError(
            "review payload head SHA mismatch: "
            f"expected {_short_sha(expected_head_sha)}, got {_short_sha(reviewed_head_sha)}"
        )

    reviewed_base_sha = str(payload.get("reviewed_base_sha", payload.get("base_sha", expected_base_sha))).strip()
    summary = str(payload.get("summary", payload.get("message", ""))).strip()
    if not summary:
        summary = f"Review decision: {decision}"

    findings_raw = payload.get("findings", [])
    if findings_raw is None:
        findings_raw = []
    if not isinstance(findings_raw, list):
        raise ValueError("review payload findings must be an array")

    findings: list[ReviewFinding] = []
    for idx, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            raise ValueError("review payload finding item must be an object")
        findings.append(_normalize_review_finding(item, idx))

    return ReviewResult(
        status=decision,
        summary=summary,
        findings=findings,
        reviewed_head_sha=reviewed_head_sha,
        reviewed_base_sha=reviewed_base_sha,
        reviewer_role=str(payload.get("reviewer_role", "independent-review")).strip(),
        reviewer_agent=str(payload.get("reviewer_agent", "codex")).strip(),
        source_check_name=str(check_run.get("name", REVIEW_GATE_CHECK_NAME)).strip(),
        source_check_url=str(check_run.get("details_url", "")).strip(),
        reviewed_at=str(payload.get("reviewed_at", check_run.get("completed_at", _now_iso()))).strip(),
    )


def _extract_run_id_from_check_url(url: str) -> str:
    match = re.search(r"/actions/runs/(\d+)", url)
    return match.group(1) if match else ""


def _extract_job_id_from_check_url(url: str) -> str:
    match = re.search(r"/job/(\d+)", url)
    return match.group(1) if match else ""


def _summarize_failed_check_log(log_text: str, *, max_lines: int = 6) -> str:
    """Extract a compact failure summary from a GitHub Actions failed-job log."""
    if not log_text.strip():
        return ""

    ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    cleaned_lines: list[str] = []
    for raw_line in log_text.splitlines():
        line = ansi_re.sub("", raw_line).strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        message = parts[2].strip() if len(parts) == 3 else line
        if message:
            cleaned_lines.append(message)

    if not cleaned_lines:
        return ""

    critical_markers = (
        "error:",
        "traceback",
        "assert",
        "exception",
        "make: ***",
        "process completed with exit code",
    )
    secondary_markers = ("failed", "timed out", "timeout")

    selected: list[str] = []
    seen: set[str] = set()
    for message in cleaned_lines:
        lowered = message.lower()
        if any(marker in lowered for marker in critical_markers):
            if message not in seen:
                selected.append(message)
                seen.add(message)
    if not selected:
        for message in cleaned_lines:
            lowered = message.lower()
            if any(marker in lowered for marker in secondary_markers):
                if message not in seen:
                    selected.append(message)
                    seen.add(message)

    if not selected:
        selected = cleaned_lines[-max_lines:]
    else:
        selected = selected[-max_lines:]

    summary = " | ".join(selected)
    if len(summary) > 700:
        summary = f"{summary[:697]}..."
    return summary


def _fetch_required_check_log_summary(repo_root: Path, link: str) -> str:
    """Return a concise failure summary for a required-check URL."""
    run_id = _extract_run_id_from_check_url(link)
    if not run_id:
        return ""

    job_id = _extract_job_id_from_check_url(link)
    cmd = ["gh", "run", "view", run_id]
    if job_id:
        cmd.extend(["--job", job_id])
    cmd.append("--log-failed")
    result = run_subprocess(cmd, cwd=repo_root)
    if result.returncode != 0:
        return ""
    return _summarize_failed_check_log(result.stdout)


def _augment_required_checks_error(repo_root: Path, checks_error: str) -> str:
    """Append failed-check log summaries to a required-checks failure message."""
    failing_checks = _required_checks_from_error(checks_error)
    if not failing_checks:
        return checks_error

    summaries: list[str] = []
    for check in failing_checks:
        check_name, link = _split_check_label(check)
        if not link:
            continue
        log_summary = _fetch_required_check_log_summary(repo_root, link)
        if not log_summary:
            continue
        summaries.append(f"{check_name}: {log_summary}")

    if not summaries:
        return checks_error

    summary_block = "\n".join(f"- {item}" for item in summaries)
    return f"{checks_error}\nRequired check log summary:\n{summary_block}"


def _load_review_payload_from_gate_artifact(
    repo_root: Path,
    check_run: dict,
) -> dict | None:
    return review_feedback.load_review_payload_from_gate_artifact(
        repo_root,
        check_run,
        run_subprocess=run_subprocess,
    )


def _extract_review_result_from_check_run(
    check_run: dict,
    *,
    repo_root: Path | None = None,
    expected_head_sha: str,
    expected_base_sha: str,
    require_payload: bool,
) -> ReviewResult:
    return review_feedback.extract_review_result_from_check_run(
        check_run,
        repo_root=repo_root,
        expected_head_sha=expected_head_sha,
        expected_base_sha=expected_base_sha,
        require_payload=require_payload,
        run_subprocess=run_subprocess,
        load_artifact_payload=_load_review_payload_from_gate_artifact,
    )


def _check_name_matches(actual: str, expected: str) -> bool:
    return review_feedback.check_name_matches(actual, expected)


def _find_pr_for_branch(
    repo_root: Path,
    branch: str,
    *,
    state: str = "open",
) -> dict | None:
    pr_check = run_subprocess(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            PR_BASE_BRANCH,
            "--state",
            state,
            "--json",
            ("number,state,url,body,headRefName,headRefOid,mergeStateStatus,isDraft"),
            "--jq",
            ".[0] // empty",
        ],
        cwd=repo_root,
    )
    if pr_check.returncode != 0:
        detail = pr_check.stderr.strip() or pr_check.stdout.strip() or "unknown error"
        logger.warning("Could not query PR for branch %s: %s", branch, detail)
        return _find_pr_for_branch_via_rest(repo_root, branch, state=state)
    pr_info = pr_check.stdout.strip()
    if not pr_info:
        return None
    try:
        parsed = json.loads(pr_info)
    except json.JSONDecodeError:
        logger.warning(
            "Could not decode `gh pr list` response for branch %s; trying the REST API.",
            branch,
        )
        return _find_pr_for_branch_via_rest(repo_root, branch, state=state)
    return parsed if isinstance(parsed, dict) else None


def _find_pr_for_branch_via_rest(
    repo_root: Path,
    branch: str,
    *,
    state: str,
) -> dict | None:
    """Fall back to GitHub REST when ``gh pr list`` GraphQL is unavailable.

    GitHub's GraphQL and REST frontends fail independently in practice.  Merge
    recovery must not turn a GraphQL 5xx into a false "No PR found" result,
    especially after auto-merge has already landed the pull request.
    """
    try:
        repo_name = _repo_name_with_owner(repo_root)
    except ValueError as exc:
        logger.warning("Could not resolve repository for PR REST fallback: %s", exc)
        return None

    owner, separator, _repo = repo_name.partition("/")
    if not separator or not owner:
        logger.warning("Invalid repository slug for PR REST fallback: %s", repo_name)
        return None

    query = urllib_parse.urlencode({
        "state": state,
        "head": f"{owner}:{branch}",
        "base": PR_BASE_BRANCH,
        "sort": "updated",
        "direction": "desc",
        "per_page": 1,
    })
    endpoint = (
        f"repos/{urllib_parse.quote(repo_name, safe='/')}/pulls?{query}"
    )
    result = run_subprocess(
        ["gh", "api", "-X", "GET", endpoint],
        cwd=repo_root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        logger.warning("PR REST fallback failed for branch %s: %s", branch, detail)
        return None

    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("Could not decode PR REST fallback response for branch %s", branch)
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None

    pr = payload[0]
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    merged_at = str(pr.get("merged_at", "") or "").strip()
    pr_state = "MERGED" if merged_at else str(pr.get("state", "") or "").upper()
    merge_state = str(pr.get("mergeable_state", "") or "UNKNOWN").upper()
    return {
        "number": pr.get("number"),
        "state": pr_state,
        "url": pr.get("html_url", ""),
        "body": pr.get("body", "") or "",
        "headRefName": head.get("ref", branch),
        "headRefOid": head.get("sha", ""),
        "mergeStateStatus": merge_state,
        "isDraft": bool(pr.get("draft", False)),
    }


def _load_pr_merge_metadata(repo_root: Path, pr_number: int) -> dict | None:
    result = run_subprocess(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,headRefName,mergeCommit,mergedAt,mergedBy",
        ],
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _commit_message(repo_root: Path, commit_sha: str) -> str:
    result = run_subprocess(
        ["git", "show", "-s", "--format=%B", commit_sha],
        cwd=repo_root,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _post_merge_commit_candidates(
    repo_root: Path,
    *,
    previous_master_sha: str,
    current_master_ref: str,
) -> tuple[list[str], str]:
    if not previous_master_sha:
        return [], "pre-merge origin/master SHA is unavailable"

    current_master_sha = _resolve_git_ref_sha(repo_root, current_master_ref)
    if not current_master_sha:
        return [], f"could not resolve {current_master_ref}"
    if current_master_sha == previous_master_sha:
        return [], (f"{current_master_ref} did not advance past {_short_sha(previous_master_sha)} after the merge")

    result = run_subprocess(
        [
            "git",
            "rev-list",
            "--reverse",
            f"{previous_master_sha}..{current_master_sha}",
        ],
        cwd=repo_root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return [], f"git rev-list failed: {detail}"

    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not candidates:
        return [], (f"no commits found in {previous_master_sha}..{current_master_sha}")
    return candidates, ""


def _infer_merge_commit_from_post_merge_history(
    run: RunState,
    repo_root: Path,
    *,
    pr_number: int,
    previous_master_sha: str,
) -> tuple[str, str]:
    candidates, candidate_error = _post_merge_commit_candidates(
        repo_root,
        previous_master_sha=previous_master_sha,
        current_master_ref=BASE_REF,
    )
    if candidate_error:
        return "", candidate_error

    if len(candidates) == 1:
        return candidates[0], ""

    matching = [
        commit_sha
        for commit_sha in candidates
        if spec_id_referenced(_commit_message(repo_root, commit_sha), run.spec_id)
    ]
    if len(matching) == 1:
        return matching[0], ""

    if matching:
        return "", (
            "multiple post-merge commits reference "
            f"{run.spec_id!r} for PR #{pr_number}: "
            f"{', '.join(_short_sha(sha) for sha in matching)}"
        )

    return "", (
        "multiple commits landed after the pre-merge origin/master SHA, and none "
        f"uniquely reference spec {run.spec_id!r} for PR #{pr_number}: "
        f"{', '.join(_short_sha(sha) for sha in candidates)}"
    )


def _merge_tag_provenance_for_pr(
    run: RunState,
    repo_root: Path,
    *,
    pr_number: int,
    pr_data: dict,
    previous_master_sha: str,
) -> tuple[str, MergeTagProvenance, str]:
    pr_metadata = _load_pr_merge_metadata(repo_root, pr_number) or {}

    merge_commit_sha = ""
    merge_commit = pr_metadata.get("mergeCommit")
    if isinstance(merge_commit, dict):
        merge_commit_sha = str(merge_commit.get("oid", "") or "").strip()

    if not merge_commit_sha:
        merge_commit_sha, merge_commit_error = _infer_merge_commit_from_post_merge_history(
            run,
            repo_root,
            pr_number=pr_number,
            previous_master_sha=previous_master_sha,
        )
    else:
        merge_commit_error = ""

    merged_by = ""
    merged_by_data = pr_metadata.get("mergedBy")
    if isinstance(merged_by_data, dict):
        merged_by = str(merged_by_data.get("login", "") or "").strip()

    source_branch = (
        str(pr_metadata.get("headRefName", "") or "").strip()
        or str(pr_data.get("headRefName", "") or "").strip()
        or run.branch
    )
    timestamp = str(pr_metadata.get("mergedAt", "") or "").strip() or utc_timestamp_now()

    provenance = MergeTagProvenance(
        spec_id=run.spec_id,
        merge_commit_sha=merge_commit_sha,
        pr_number=pr_number,
        source_branch=source_branch,
        actor=merged_by or run.requested_by or _current_actor(),
        timestamp=timestamp,
    )
    return merge_commit_sha, provenance, merge_commit_error


def _find_open_pr_for_branch(
    repo_root: Path,
    branch: str,
) -> tuple[dict | None, dict | None]:
    """Return an open PR plus the latest observed PR state for the branch."""
    pr_data = _find_pr_for_branch(repo_root, branch, state="open")
    if pr_data is not None:
        return pr_data, pr_data

    fallback = _find_pr_for_branch(repo_root, branch, state="all")
    if not isinstance(fallback, dict):
        return None, None

    fallback_state = str(fallback.get("state", "")).strip().upper()
    if fallback_state != "OPEN":
        return None, fallback

    pr_number = fallback.get("number")
    if isinstance(pr_number, int):
        logger.warning(
            "Open PR lookup returned no result for %s, but PR #%s still appears OPEN; using fallback.",
            branch,
            pr_number,
        )
    else:
        logger.warning(
            "Open PR lookup returned no result for %s, but an all-state query still reports it OPEN; using fallback.",
            branch,
        )
    return fallback, fallback


def _describe_non_open_pr_state(
    repo_root: Path,
    branch: str,
    *,
    context: str,
    pr_data: dict | None = None,
) -> str:
    if pr_data is None:
        pr_data = _find_pr_for_branch(repo_root, branch, state="all")
    if pr_data is None:
        return f"No PR found for branch {branch}"

    pr_number = pr_data.get("number")
    pr_state = str(pr_data.get("state", "")).strip().upper()
    prefix = f"PR #{pr_number} for branch {branch}" if isinstance(pr_number, int) else f"PR for branch {branch}"
    if pr_state == "MERGED":
        return f"{prefix} merged {context}"
    if pr_state == "CLOSED":
        return f"{prefix} was closed {context}"
    return f"{prefix} is no longer open {context} (state={pr_state or 'UNKNOWN'})"


def _disable_pr_auto_merge(repo_root: Path, pr_number: int) -> str:
    result = run_subprocess(
        ["gh", "pr", "merge", str(pr_number), "--disable-auto"],
        cwd=repo_root,
    )
    if result.returncode == 0:
        return ""

    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    detail_lower = detail.lower()
    benign_errors = (
        "auto merge is not enabled",
        "auto-merge is not enabled",
        "auto merge is disabled",
        "auto-merge is disabled",
        "not enabled for this repository",
        "not allowed for this repository",
        "can't disable auto-merge",
    )
    if any(marker in detail_lower for marker in benign_errors):
        return ""
    return detail


def _fail_after_auto_merge_armed(
    run: RunState,
    repo_root: Path,
    pr_number: int,
    message: str,
) -> str:
    disable_error = _disable_pr_auto_merge(repo_root, pr_number)
    if disable_error:
        logger.warning(
            "Could not disable auto-merge for PR #%s after failure: %s",
            pr_number,
            disable_error,
        )
    run.last_error = message
    return "failed"


def _remote_branch_head_sha(repo_root: Path, branch: str) -> str | None:
    """Return origin/<branch> HEAD SHA, or None when unavailable."""
    result = run_subprocess(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    sha = lines[0].split(maxsplit=1)[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return None
    return sha


def _find_check_run_for_sha(
    repo_root: Path,
    head_sha: str,
    check_name: str,
) -> dict | None:
    return review_feedback.find_check_run_for_sha(
        repo_root,
        head_sha,
        check_name,
        run_subprocess=run_subprocess,
    )


def _local_review_state_dir(repo_root: Path, run_id: str) -> Path:
    return _state_root(repo_root) / "runs" / run_id / "local-review"


def _local_review_artifact_paths(repo_root: Path, run_id: str) -> dict[str, Path]:
    output_dir = _local_review_state_dir(repo_root, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "prompt": output_dir / "prompt.md",
        "process_debug": output_dir / "process-debug.json",
        "raw_review": output_dir / "codex-review.json",
        "raw_review_partial": output_dir / "codex-review.partial.json",
        "review_result": output_dir / "review-decision.json",
        "summary": output_dir / "review-decision-summary.json",
        "job_summary": output_dir / "review-decision-summary.md",
        "timeout_debug": output_dir / "timeout-debug.json",
        "bootstrap_warning": output_dir / "review-bootstrap-warning.json",
    }


def _clear_local_review_artifacts(artifact_paths: dict[str, Path]) -> None:
    for path in artifact_paths.values():
        path.unlink(missing_ok=True)


def _local_review_agent_label(agent_name: str) -> str:
    normalized = str(agent_name or "").strip()
    if not normalized:
        return "review"
    aliases = {
        "claude": "Claude",
        "codex": "Codex",
    }
    return aliases.get(normalized.lower(), normalized)


def _configured_review_agent_default() -> str:
    return str(SPEC_RUNTIME_CONFIG.agents.review_default or "").strip()


def _effective_review_agent(run: RunState) -> str:
    explicit = str(run.review_agent or "").strip()
    if explicit:
        return explicit
    configured = _configured_review_agent_default()
    if configured:
        return configured
    implement_agent = str(run.agent or "").strip()
    if implement_agent:
        return implement_agent
    return SPEC_RUNTIME_CONFIG.agents.default


def _format_implement_resume_command(
    *,
    spec_id: str,
    agent: str,
    review_agent: str,
    branch: str,
) -> str:
    cmd = ["spec", "implement", "--spec", spec_id, "--agent", agent]
    if review_agent and review_agent != agent:
        cmd += ["--review-agent", review_agent]
    cmd += ["--branch", branch]
    return shlex.join(cmd)


def _implement_resume_command(run: RunState) -> str:
    return _format_implement_resume_command(
        spec_id=run.spec_id,
        agent=run.agent,
        review_agent=_effective_review_agent(run),
        branch=run.branch,
    )


def _is_local_review_timeout_message(message: object) -> bool:
    normalized = str(message or "").strip().lower()
    return bool(re.search(r"\blocal(?: [^ ]+)? reviewer timed out after\b", normalized))


def _format_gate_evidence_for_review(repo_root: Path, run: "RunState") -> str:
    """Summarize the orchestrator's own gate run for the reviewer.

    The review agent runs under a read-only sandbox, so it cannot execute the
    suite: reviews repeatedly reported "the read-only environment had no usable
    temporary directory" and fell back to reading the diff. Meanwhile the
    orchestrator has just run every required gate against this exact head.
    Hand over that result instead of leaving the reviewer to guess -- or worse,
    to assert coverage claims it had no way to check.
    """
    _, gate_data = _read_gate_status(repo_root, run)
    if not isinstance(gate_data, dict):
        return ""
    gates = gate_data.get("gates")
    if not isinstance(gates, dict) or not gates:
        return ""
    lines: list[str] = []
    for gate_name in [g for g in REQUIRED_GATES if g in gates] + [
        g for g in gates if g not in REQUIRED_GATES
    ]:
        entry = gates.get(gate_name)
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("last_status", "") or "unknown")
        command = str(entry.get("last_command", "") or f"make {gate_name}")
        lines.append(f"- `{command}`: **{status}**")
    if not lines:
        return ""
    return (
        "## Verification already performed by the orchestrator\n\n"
        "These gates were run against this exact head commit by the orchestrator, "
        "outside your sandbox, before review started:\n\n"
        + "\n".join(lines)
        + "\n\nTreat these as authoritative. You are running read-only and cannot "
        "execute the suite yourself, so do not report an inability to run tests as "
        "a finding, and do not assume a gate failed merely because you could not "
        "run it. Judge the diff on correctness against the spec, and rely on the "
        "results above for whether the suite passes.\n"
    )


def _render_local_review_prompt(
    repo_root: Path,
    *,
    repo_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    head_ref: str,
    pr_body: str,
    review_changes: int = 0,
    gate_evidence: str = "",
) -> str:
    # Look for repo-level override, then bundled template
    template_path = repo_root / ".github" / "prompts" / "review.md"
    if not template_path.is_file():
        template_path = repo_root / ".github" / "prompts" / "codex-review.md"
    if template_path.is_file():
        rendered = template_path.read_text()
    else:
        import importlib.resources

        templates = importlib.resources.files("spec_runtime") / "templates"
        rendered = (templates / "review.md").read_text(encoding="utf-8")
    spec_id = resolve_spec_id_for_pr(head_ref, pr_body) or ""
    replacements = {
        "${REPO}": repo_name,
        "${PR_NUMBER}": str(pr_number),
        "${BASE_SHA}": base_sha,
        "${HEAD_SHA}": head_sha,
        "${HEAD_REF}": head_ref,
        "${SPEC_ID}": spec_id,
    }
    for needle, value in replacements.items():
        rendered = rendered.replace(needle, value)
    if gate_evidence:
        if "${GATE_RESULTS}" in rendered:
            rendered = rendered.replace("${GATE_RESULTS}", gate_evidence)
        else:
            # Repo-level templates predate this placeholder; append so they
            # still get the evidence rather than silently going without.
            rendered = f"{rendered.rstrip()}\n\n{gate_evidence}"
    else:
        rendered = rendered.replace("${GATE_RESULTS}", "")
    if review_changes == 0:
        rendered = f"{LOCAL_REVIEW_FIRST_PASS_EXHAUSTIVE_INSTRUCTION}\n\n{rendered}"
    return rendered


def _coerce_subprocess_stream_text(stream: object) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    if isinstance(stream, str):
        return stream
    return str(stream)


def _signal_name_from_returncode(returncode: int | None) -> str:
    if returncode is None or returncode >= 0:
        return ""
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


def _redacted_local_review_command_preview(cmd: list[str]) -> list[str]:
    preview = list(cmd)
    if preview:
        preview[-1] = "<prompt omitted; see prompt.md>"
    return preview


def _write_local_review_process_diagnostics(
    repo_root: Path,
    artifact_paths: dict[str, Path],
    *,
    payload: dict[str, object],
) -> None:
    debug_path = artifact_paths["process_debug"]
    existing = _read_json_dict(debug_path) or {}
    existing.update(payload)
    debug_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


def _write_local_review_timeout_diagnostics(
    repo_root: Path,
    artifact_paths: dict[str, Path],
    *,
    review_agent: str,
    cmd: list[str],
    cwd: Path,
    timeout_exc: subprocess.TimeoutExpired,
) -> tuple[Path, Path | None]:
    raw_review_path = artifact_paths["raw_review"]
    partial_path: Path | None = None
    raw_review_excerpt = ""
    raw_review_size_bytes = 0
    if raw_review_path.is_file():
        raw_review_text = raw_review_path.read_text(encoding="utf-8", errors="replace")
        raw_review_size_bytes = raw_review_path.stat().st_size
        if raw_review_text.strip():
            partial_path = artifact_paths["raw_review_partial"]
            partial_path.write_text(raw_review_text)
            raw_review_excerpt = redact_sensitive(raw_review_text[:LOCAL_REVIEW_TIMEOUT_RAW_REVIEW_MAX_CHARS].strip())

    stdout_text = _coerce_subprocess_stream_text(
        getattr(timeout_exc, "stdout", None) or getattr(timeout_exc, "output", None)
    )
    stderr_text = _coerce_subprocess_stream_text(getattr(timeout_exc, "stderr", None))
    command_preview = _redacted_local_review_command_preview(cmd)

    payload = {
        "captured_at": _now_iso(),
        "command": command_preview,
        "cwd": str(cwd),
        "prompt_path": str(artifact_paths["prompt"].relative_to(repo_root)),
        "raw_review_path": str(raw_review_path.relative_to(repo_root)),
        "raw_review_partial_path": (str(partial_path.relative_to(repo_root)) if partial_path is not None else ""),
        "raw_review_present": raw_review_path.is_file(),
        "raw_review_size_bytes": raw_review_size_bytes,
        "raw_review_excerpt": raw_review_excerpt,
        "stdout_tail": _sanitize_gate_stream(
            stdout_text,
            max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
            max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
        ),
        "stderr_tail": _sanitize_gate_stream(
            stderr_text,
            max_lines=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_LINES,
            max_chars=LOCAL_REVIEW_TIMEOUT_STDIO_MAX_CHARS,
        ),
        "timeout_seconds": timeout_exc.timeout,
    }
    debug_path = artifact_paths["timeout_debug"]
    debug_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return debug_path, partial_path


def _summarize_local_review_timeout(
    repo_root: Path,
    artifact_paths: dict[str, Path],
    *,
    review_agent: str,
    cmd: list[str],
    cwd: Path,
    timeout_exc: subprocess.TimeoutExpired,
) -> str:
    debug_path, partial_path = _write_local_review_timeout_diagnostics(
        repo_root,
        artifact_paths,
        review_agent=review_agent,
        cmd=cmd,
        cwd=cwd,
        timeout_exc=timeout_exc,
    )
    timeout_seconds = timeout_exc.timeout
    if timeout_seconds is None:
        timeout_seconds = REVIEW_TIMEOUT_SECONDS
    summary = (
        f"Local {_local_review_agent_label(review_agent)} reviewer timed out after {int(timeout_seconds)}s. "
        f"Diagnostics saved to {debug_path.relative_to(repo_root).as_posix()}."
    )
    if partial_path is not None:
        summary = f"{summary} Preserved partial reviewer output at {partial_path.relative_to(repo_root).as_posix()}."
    return summary


def _build_local_review_env(
    *,
    extra_git_configs: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in LOCAL_REVIEW_DISABLED_CREDENTIAL_ENV_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = shutil.which("false") or "/usr/bin/false"

    try:
        config_count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        config_count = 0

    # Base git config overrides: disable credential helpers and push URL.
    git_configs: list[tuple[str, str]] = [
        ("credential.helper", ""),
        ("remote.origin.pushurl", "codex-review-disabled://origin"),
    ]

    # Merge extra env overrides (e.g. GIT_OBJECT_DIRECTORY for read-only
    # object store, hooks path).  Non-GIT_CONFIG vars are applied directly;
    # the sentinel ``_SPEC_DEBUGGER_HOOKS_PATH`` is translated to a
    # ``core.hooksPath`` git config entry.
    if extra_git_configs:
        hooks_path = extra_git_configs.pop("_SPEC_DEBUGGER_HOOKS_PATH", None)
        if hooks_path:
            git_configs.append(("core.hooksPath", hooks_path))
        for key, value in extra_git_configs.items():
            env[key] = value

    for i, (cfg_key, cfg_val) in enumerate(git_configs):
        env[f"GIT_CONFIG_KEY_{config_count + i}"] = cfg_key
        env[f"GIT_CONFIG_VALUE_{config_count + i}"] = cfg_val
    env["GIT_CONFIG_COUNT"] = str(config_count + len(git_configs))
    return env


def _build_local_review_command(
    *,
    prompt: str,
    schema_path: Path,
    output_path: Path,
    agent_name: str = "",
    reasoning_effort: str = LOCAL_REVIEW_REASONING_EFFORT,
    mcp_config_path: Path | None = None,
) -> list[str]:
    from .agent_adapter import get_agent_adapter

    agent = get_agent_adapter(agent_name or _configured_review_agent_default() or SPEC_RUNTIME_CONFIG.agents.default)
    review_kwargs: dict[str, object] = {
        "prompt": prompt,
        "output_path": output_path,
        "schema_path": schema_path,
    }
    if _adapter_method_accepts_kwarg(agent.build_review_command, "mcp_config_path"):
        review_kwargs["mcp_config_path"] = mcp_config_path
    return agent.build_review_command(**review_kwargs)


def _block_debugger_artifact_paths(repo_root: Path, run_id: str) -> dict[str, Path]:
    artifact_dir = _state_root(repo_root) / "runs" / run_id
    return {
        "context": artifact_dir / BLOCK_DEBUGGER_CONTEXT_FILENAME,
        "prompt": artifact_dir / BLOCK_DEBUGGER_PROMPT_FILENAME,
        "raw_output": artifact_dir / BLOCK_DEBUGGER_RAW_OUTPUT_FILENAME,
        "diagnosis": artifact_dir / BLOCK_DIAGNOSIS_FILENAME,
    }


def _clear_block_debugger_artifacts(artifact_paths: dict[str, Path], *, keep_diagnosis: bool) -> None:
    for key, path in artifact_paths.items():
        if keep_diagnosis and key == "diagnosis":
            continue
        path.unlink(missing_ok=True)


def _block_debugger_schema_path(repo_root: Path) -> Path:
    schema_path = repo_root / ".github" / "schemas" / "block-diagnosis.schema.json"
    if schema_path.is_file():
        return schema_path
    import importlib.resources

    bundled = importlib.resources.files("spec_runtime") / "templates" / "block-diagnosis-schema.json"
    return Path(str(bundled))


def _build_block_debugger_command(
    *,
    prompt: str,
    schema_path: Path,
    output_path: Path,
    agent_name: str = "",
    writable_output_dir: Path | None = None,
    mcp_config_path: Path | None = None,
) -> list[str]:
    agent = get_agent_adapter(agent_name or _configured_review_agent_default() or SPEC_RUNTIME_CONFIG.agents.default)
    if agent.name == "claude":
        # Keep this read-only: omit --add-dir and --dangerously-skip-permissions
        # so the agent cannot mutate state files.  All evidence is already
        # inlined in the prompt, so the agent needs no tool access.  Without
        # the permissions bypass, write-capable tools (Bash, Edit, Write) are
        # denied in -p mode, enforcing read-only at the tool level in
        # addition to the filesystem chmod and git-guard defenses.
        cmd = ["claude", "-p"]
        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
        cmd.append(prompt)
        return cmd
    if agent.name == "codex":
        cmd = ["codex", "exec", "--ephemeral", "-s", "read-only"]
        cmd += _codex_linux_sandbox_overrides()
        if writable_output_dir:
            cmd += ["--add-dir", str(writable_output_dir)]
        if schema_path:
            cmd += ["--output-schema", str(schema_path)]
        cmd += ["-o", str(output_path), prompt]
        return cmd
    review_kwargs: dict[str, object] = {
        "prompt": prompt,
        "output_path": output_path,
        "schema_path": schema_path,
    }
    if _adapter_method_accepts_kwarg(agent.build_review_command, "mcp_config_path"):
        review_kwargs["mcp_config_path"] = mcp_config_path
    return agent.build_review_command(**review_kwargs)


def _render_block_debugger_prompt(
    *,
    spec_id: str,
    run_id: str,
    source_phase: str,
    block_reason: str,
    current_head_sha: str,
    worktree_status: str,
    context_path: str,
    evidence_root: str,
) -> str:
    lines = [
        "Blocked-run evidence package:",
        f"- Spec ID: {spec_id}",
        f"- Run ID: {run_id}",
        f"- Source phase: {source_phase}",
        f"- Evidence root: {evidence_root}",
        f"- Context file: {context_path}",
        f"- Current HEAD SHA: {current_head_sha or '(unknown)'}",
        f"- Current worktree status: {worktree_status or '(unavailable)'}",
        f"- Original block reason: {block_reason or '(none recorded)'}",
    ]
    if source_phase == "bootstrap" and not current_head_sha:
        lines.append(
            "- No implementation worktree or head SHA exists yet — this is a "
            "bootstrap-time block, so the debugger worktree was checked out "
            "from the configured base ref."
        )
    lines.extend(
        [
            "- Inspect the context file first. It points to implement-context.json, implement-result.json, "
            "gate-status.json, review-result.json if present, and all orchestrator phase-result files for this run.",
            "- If confidence is low, say so directly and frame the diagnosis as a hypothesis.",
            "- Set requires_human_attention=true only when the smallest credible next move still requires "
            "a human decision, missing external input, or an intervention the implement agent should not guess.",
            "- Set needs_new_commit=true only when the next best action requires code or branch changes.",
        ]
    )
    return "\n\n".join([BLOCK_DEBUGGER_PROMPT, "\n".join(lines)])


def _load_block_diagnosis_from_output(path: Path) -> BlockDiagnosis:
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not parse blocked-run debugger output {path}: line {exc.lineno} column {exc.colno}"
        ) from exc
    diagnosis = _coerce_block_diagnosis(payload)
    if diagnosis is None:
        raise ValueError(f"Blocked-run debugger output is missing required fields: {path}")
    return diagnosis


def _write_failed_local_review_payload(
    output_path: Path,
    *,
    summary: str,
    expected_head_sha: str,
    expected_base_sha: str,
    reviewer_agent: str,
) -> None:
    payload = {
        "schema_version": "v1",
        "decision": "failed",
        "summary": summary,
        "reviewed_base_sha": expected_base_sha,
        "reviewed_head_sha": expected_head_sha,
        "findings": [],
        "reviewer_role": "independent-review",
        "reviewer_agent": reviewer_agent or "codex",
        "reviewed_at": _now_iso(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _is_local_review_timeout_result(review_result: ReviewResult) -> bool:
    return review_result.status == "failed" and _is_local_review_timeout_message(review_result.summary)


def _load_review_result_from_gate_output(result_path: Path) -> ReviewResult:
    try:
        payload = json.loads(result_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not parse local review gate output {result_path}: line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Local review gate output must be a JSON object: {result_path}")

    status = str(payload.get("status", payload.get("decision", ""))).strip().lower()
    if status not in REVIEW_DECISION_VALUES:
        raise ValueError(f"Local review gate output has invalid status/decision: {status or '(missing)'}")

    findings_payload = payload.get("findings", [])
    if findings_payload is None:
        findings_payload = []
    if not isinstance(findings_payload, list):
        raise ValueError("Local review gate output findings must be a JSON array")

    findings: list[ReviewFinding] = []
    for index, item in enumerate(findings_payload):
        if not isinstance(item, dict):
            raise ValueError(f"Local review gate output finding #{index + 1} must be a JSON object")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            start_line = max(1, int(item.get("start_line", 1)))
        except (TypeError, ValueError):
            start_line = 1
        try:
            end_line = max(start_line, int(item.get("end_line", start_line)))
        except (TypeError, ValueError):
            end_line = start_line
        findings.append(
            ReviewFinding(
                id=str(item.get("id", f"finding-{index + 1}")).strip(),
                title=str(item.get("title", f"Finding {index + 1}")).strip(),
                severity=str(item.get("severity", "")).strip(),
                file=str(item.get("file", "")).strip(),
                start_line=start_line,
                end_line=end_line,
                body=str(item.get("body", "")).strip(),
                confidence=max(0.0, min(1.0, confidence)),
            )
        )

    return ReviewResult(
        status=status,
        summary=(str(payload.get("summary", "")).strip() or f"Review decision: {status}"),
        findings=findings,
        reviewed_head_sha=str(payload.get("reviewed_head_sha", "")).strip(),
        reviewed_base_sha=str(payload.get("reviewed_base_sha", "")).strip(),
        reviewer_role=str(payload.get("reviewer_role", "")).strip(),
        reviewer_agent=str(payload.get("reviewer_agent", "")).strip(),
        source_check_name=str(payload.get("source_check_name", REVIEW_GATE_CHECK_NAME)).strip()
        or REVIEW_GATE_CHECK_NAME,
        source_check_url=str(payload.get("source_check_url", "")).strip(),
        reviewed_at=str(payload.get("reviewed_at", _now_iso())).strip(),
    )


def _publish_review_gate_sticky_comment(
    repo_root: Path,
    *,
    repo_name: str,
    pr_number: int,
    review_result_path: Path,
) -> str:
    token = _forge().get_auth_token()
    if not token:
        # Fallback to run_subprocess for compatibility with orchestrator test mocks.
        token_result = run_subprocess(["gh", "auth", "token"], cwd=repo_root)
        token = token_result.stdout.strip()
    if not token:
        return "Could not read GitHub auth token for sticky review comment: no token available"

    try:
        from .review_gate_sticky_comment import main as sticky_main

        old_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = token
        try:
            rc = sticky_main(
                [
                    "--review-result",
                    str(review_result_path),
                    "--repo",
                    repo_name,
                    "--pr-number",
                    str(pr_number),
                ]
            )
        finally:
            if old_token is not None:
                os.environ["GITHUB_TOKEN"] = old_token
            else:
                os.environ.pop("GITHUB_TOKEN", None)
        if rc == 0:
            return ""
        return "sticky comment publication failed"
    except Exception as exc:
        return f"sticky comment publication failed: {exc}"


def _truncate_commit_status_description(text: str, *, default: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip() or default
    if len(normalized) <= 140:
        return normalized
    return normalized[:137].rstrip() + "..."


def _publish_local_review_pending_check_run(
    run: RunState,
    repo_root: Path,
    *,
    repo_name: str,
    expected_head_sha: str,
) -> str:
    del run, repo_root, repo_name, expected_head_sha
    return ""


def _publish_review_gate_status(
    repo_root: Path,
    *,
    repo_name: str,
    expected_head_sha: str,
    state: str,
    description: str,
    target_url: str = "",
    default_description: str,
) -> str:
    payload: dict[str, object] = {
        "state": state,
        "context": REVIEW_GATE_CHECK_NAME,
        "description": _truncate_commit_status_description(
            description,
            default=default_description,
        ),
    }
    if target_url:
        payload["target_url"] = target_url

    publish_result = run_subprocess(
        [
            "gh",
            "api",
            f"repos/{repo_name}/statuses/{expected_head_sha}",
            "-X",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "--input",
            "-",
        ],
        cwd=repo_root,
        input_text=json.dumps(payload),
    )
    if publish_result.returncode != 0:
        detail = publish_result.stderr.strip() or publish_result.stdout.strip() or "unknown error"
        raise ValueError(
            f"Could not publish local {REVIEW_GATE_CHECK_NAME} status for {_short_sha(expected_head_sha)}: {detail}"
        )

    try:
        response = json.loads(publish_result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse local {REVIEW_GATE_CHECK_NAME} status response: {exc}") from exc
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected local {REVIEW_GATE_CHECK_NAME} status response payload")
    return str(response.get("target_url") or target_url).strip()


def _clear_saved_review_result(repo_root: Path, run_id: str) -> None:
    review_result_path = _state_root(repo_root) / "runs" / run_id / "review-result.json"
    try:
        review_result_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "Could not remove stale review-result.json for run %s: %s",
            run_id,
            exc,
        )


def _reset_local_review_gate_for_head(
    run: RunState,
    repo_root: Path,
    *,
    current_head_sha: str,
) -> bool:
    should_reset_local_review_gate = (
        current_head_sha != run.review_expected_head_sha or not run.review_decision_status
    )
    if should_reset_local_review_gate:
        _clear_saved_review_result(repo_root, run.run_id)
        run.review_expected_head_sha = current_head_sha
        run.review_decision_status = ""
        run.review_decision_summary = ""
        run.review_decision_check_url = ""
        run.save(repo_root)
    return should_reset_local_review_gate


def _mark_local_review_for_rerun_after_sync(
    run: RunState,
    repo_root: Path,
    *,
    pr_body: str,
    current_head_sha: str,
) -> bool:
    if not pr_body_uses_local_review(pr_body):
        return False
    if not current_head_sha:
        return False
    previous_review_head = run.review_expected_head_sha
    previous_review_status = run.review_decision_status
    reset = _reset_local_review_gate_for_head(
        run,
        repo_root,
        current_head_sha=current_head_sha,
    )
    if not reset and previous_review_status:
        return False

    prior = _short_sha(previous_review_head) if previous_review_head else "none"
    current = _short_sha(current_head_sha)
    status_note = previous_review_status or "pending"
    run.last_error = (
        f"{LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX}: "
        f"review status {status_note} covered {prior}, but the branch now points to {current}."
    )
    return True


def _publish_local_review_status(
    repo_root: Path,
    *,
    repo_name: str,
    expected_head_sha: str,
    review_result: ReviewResult,
    target_url: str = "",
) -> str:
    return _publish_review_gate_status(
        repo_root,
        repo_name=repo_name,
        expected_head_sha=expected_head_sha,
        state="success" if review_result.status == "approved" else "failure",
        description=review_result.summary,
        target_url=target_url,
        default_description=f"Local review: {review_result.status.replace('_', ' ')}",
    )


def _publish_pending_local_review_status(
    repo_root: Path,
    *,
    repo_name: str,
    expected_head_sha: str,
    target_url: str = "",
) -> str:
    return _publish_review_gate_status(
        repo_root,
        repo_name=repo_name,
        expected_head_sha=expected_head_sha,
        state="pending",
        description=LOCAL_REVIEW_TIMEOUT_PENDING_DESCRIPTION,
        target_url=target_url,
        default_description="Local review pending",
    )


def _restore_approved_local_review_status_if_pending(
    run: RunState,
    repo_root: Path,
    *,
    expected_head_sha: str,
    checks_error: str,
) -> tuple[bool, str]:
    """Restore an approved local-review status reset by PR readiness events.

    Marking a draft PR ready starts the repository review workflow again. Its
    seed job intentionally publishes ``review-decision-gate=pending`` while it
    waits for the host orchestrator, which can overwrite the success status
    that the just-completed local review published for the same head. During
    merge polling, re-publish the saved decision only when that exact required
    check is pending and the approval still covers the expected head.
    """
    if REVIEW_GATE_CHECK_NAME.lower() not in checks_error.lower():
        return False, ""
    if run.review_decision_status != "approved":
        return False, ""
    if not expected_head_sha or run.review_expected_head_sha != expected_head_sha:
        return False, ""

    review_result = ReviewResult(
        status="approved",
        summary=run.review_decision_summary or "Local review approved the current head",
        reviewed_head_sha=expected_head_sha,
        source_check_name=REVIEW_GATE_CHECK_NAME,
        source_check_url=run.review_decision_check_url,
    )
    try:
        check_url = _publish_local_review_status(
            repo_root,
            repo_name=_repo_name_with_owner(repo_root),
            expected_head_sha=expected_head_sha,
            review_result=review_result,
            target_url=run.review_decision_check_url,
        )
    except ValueError as exc:
        return False, str(exc)

    if check_url:
        run.review_decision_check_url = check_url
    logger.info(
        "Restored approved %s status for %s after readiness workflow reset",
        REVIEW_GATE_CHECK_NAME,
        _short_sha(expected_head_sha),
    )
    return True, ""


def _temp_worktree_belongs_to_repo(repo_root: Path, worktree_path: Path) -> bool:
    """Return whether a detached temp worktree belongs to *repo_root*."""
    return resolve_common_root(repo_root) == resolve_common_root(worktree_path)


def _cleanup_stale_detached_temp_worktrees(
    repo_root: Path,
    *,
    prefix: str,
    label: str,
) -> None:
    for candidate in sorted(LOCAL_REVIEW_WORKTREE_ROOT.glob(f"{prefix}*")):
        if not candidate.is_dir():
            continue
        if not _temp_worktree_belongs_to_repo(repo_root, candidate):
            continue

        registered, error = _worktree_is_registered(repo_root, candidate)
        if error:
            logger.warning(
                "Could not inspect %s %s before cleanup: %s",
                label,
                candidate,
                error,
            )
            continue
        if registered:
            logger.info(
                "Skipping registered %s %s during stale cleanup.",
                label,
                candidate,
            )
            continue

        cleanup_error = _cleanup_worktree_checkout(
            repo_root,
            candidate,
            delete_branch=False,
        )
        if cleanup_error:
            logger.warning(
                "Could not remove stale %s %s: %s",
                label,
                candidate,
                cleanup_error,
            )


def _cleanup_stale_review_worktrees(repo_root: Path) -> None:
    _cleanup_stale_detached_temp_worktrees(
        repo_root,
        prefix=LOCAL_REVIEW_WORKTREE_PREFIX,
        label="review worktree",
    )


def _commit_present(repo_root: Path, head_sha: str) -> subprocess.CompletedProcess:
    """Return the ``git cat-file -e`` result for *head_sha* in *repo_root*."""
    return run_subprocess(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=repo_root,
    )


def _ensure_review_head_present(
    repo_root: Path, *, head_sha: str, branch: str | None, purpose: str = "local review"
) -> None:
    """Ensure *head_sha* is present in *repo_root* before the review worktree checkout.

    Container-backed runs push the reviewed branch to origin from a separate
    clone, so the host orchestration checkout may not yet contain ``head_sha``.
    Fetch the exact branch ref from origin when the commit is missing and a
    branch name is available; raise a clear ``ValueError`` if it still cannot be
    made present. When no branch is available the missing-object case is left for
    ``git worktree add`` to report (behavior unchanged for local runs).
    """
    if _commit_present(repo_root, head_sha).returncode == 0:
        return

    branch_name = (branch or "").strip()
    if not branch_name:
        # No branch to fetch from; fall through to the existing worktree-add
        # error path (behavior unchanged for local/non-container runs).
        return

    try:
        fetch_outcome = run_git_fetch_with_timeout(
            ["origin", f"{branch_name}:refs/remotes/origin/{branch_name}"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
            runner=_orchestrator_fetch_runner,
        )
    except GitFetchTimeoutError as exc:
        raise ValueError(
            "Could not make commit "
            f"{_short_sha(head_sha)} available for {purpose} of branch "
            f"'{branch_name}': git fetch timed out after {exc.timeout_seconds:.0f}s"
        ) from exc

    recheck = _commit_present(repo_root, head_sha)
    if recheck.returncode == 0:
        return

    fetch_detail = ""
    if not fetch_outcome.is_success:
        fetch_detail = fetch_outcome.stderr.strip() or fetch_outcome.stdout.strip()
    catfile_detail = recheck.stderr.strip() or recheck.stdout.strip()
    detail = fetch_detail or catfile_detail or "commit is still not present after fetch"
    raise ValueError(
        "Could not make commit "
        f"{_short_sha(head_sha)} available for {purpose} of branch "
        f"'{branch_name}' (stale or missing remote head): {detail}"
    )


@contextmanager
def _temporary_review_worktree(
    repo_root: Path, *, head_sha: str, branch: str | None = None
) -> Iterator[Path]:
    _cleanup_stale_review_worktrees(repo_root)

    _ensure_review_head_present(repo_root, head_sha=head_sha, branch=branch)

    worktree_path = Path(
        tempfile.mkdtemp(
            prefix=LOCAL_REVIEW_WORKTREE_PREFIX,
            dir=LOCAL_REVIEW_WORKTREE_ROOT,
        )
    )
    worktree_path.rmdir()

    add_result = run_subprocess(
        ["git", "worktree", "add", "--detach", str(worktree_path), head_sha],
        cwd=repo_root,
    )
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git worktree add failed"
        raise ValueError(f"git worktree add failed for local review: {detail}")

    actual_head_sha = _head_sha(worktree_path)
    if actual_head_sha and actual_head_sha != head_sha:
        cleanup_error = _cleanup_worktree_checkout(
            repo_root,
            worktree_path,
            delete_branch=False,
        )
        if cleanup_error:
            logger.warning(
                "Could not remove mismatched review worktree %s: %s",
                worktree_path,
                cleanup_error,
            )
        raise ValueError(
            "Detached local review worktree checked out the wrong commit: "
            f"expected {_short_sha(head_sha)}, got {_short_sha(actual_head_sha)}"
        )

    try:
        yield worktree_path
    finally:
        cleanup_error = _cleanup_worktree_checkout(
            repo_root,
            worktree_path,
            delete_branch=False,
        )
        if cleanup_error:
            logger.warning(
                "Could not remove local review worktree %s: %s",
                worktree_path,
                cleanup_error,
            )


def _cleanup_stale_mergeability_worktrees(repo_root: Path) -> None:
    _cleanup_stale_detached_temp_worktrees(
        repo_root,
        prefix=LOCAL_MERGEABILITY_WORKTREE_PREFIX,
        label="mergeability worktree",
    )


@contextmanager
def _temporary_mergeability_worktree(
    repo_root: Path, *, head_sha: str, branch: str = ""
) -> Iterator[Path]:
    _cleanup_stale_mergeability_worktrees(repo_root)

    # Same race the review worktree already guards against: container-backed runs
    # build the head in a separate clone and push it to origin, so repo_root can
    # be missing the object when this runs. Without the fetch the check dies on a
    # bare "invalid reference", which reads like corruption and is classified
    # non-retryable even when the commit is present on origin and in the run
    # workspace.
    _ensure_review_head_present(
        repo_root, head_sha=head_sha, branch=branch, purpose="the mergeability check"
    )

    worktree_path = Path(
        tempfile.mkdtemp(
            prefix=LOCAL_MERGEABILITY_WORKTREE_PREFIX,
            dir=LOCAL_REVIEW_WORKTREE_ROOT,
        )
    )
    worktree_path.rmdir()

    add_result = run_subprocess(
        ["git", "worktree", "add", "--detach", str(worktree_path), head_sha],
        cwd=repo_root,
    )
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git worktree add failed"
        raise ValueError(f"git worktree add failed for mergeability check: {detail}")

    actual_head_sha = _head_sha(worktree_path)
    if actual_head_sha and actual_head_sha != head_sha:
        cleanup_error = _cleanup_worktree_checkout(
            repo_root,
            worktree_path,
            delete_branch=False,
        )
        if cleanup_error:
            logger.warning(
                "Could not remove mismatched mergeability worktree %s: %s",
                worktree_path,
                cleanup_error,
            )
        raise ValueError(
            "Detached mergeability worktree checked out the wrong commit: "
            f"expected {_short_sha(head_sha)}, got {_short_sha(actual_head_sha)}"
        )

    try:
        yield worktree_path
    finally:
        cleanup_error = _cleanup_worktree_checkout(
            repo_root,
            worktree_path,
            delete_branch=False,
        )
        if cleanup_error:
            logger.warning(
                "Could not remove mergeability worktree %s: %s",
                worktree_path,
                cleanup_error,
            )


def _cleanup_stale_block_debugger_worktrees(repo_root: Path) -> None:
    _cleanup_stale_detached_temp_worktrees(
        repo_root,
        prefix=LOCAL_BLOCK_DEBUGGER_WORKTREE_PREFIX,
        label="block debugger worktree",
    )
    common_root = resolve_common_root(repo_root).resolve()
    for candidate in sorted(LOCAL_REVIEW_WORKTREE_ROOT.glob(f"{LOCAL_BLOCK_DEBUGGER_WORKTREE_PREFIX}*")):
        marker = candidate / BLOCK_DEBUGGER_PRIVATE_CLONE_MARKER
        if not candidate.is_dir() or not marker.is_file() or not (candidate / ".git").is_dir():
            continue
        try:
            payload = json.loads(marker.read_text())
            owner = Path(str(payload.get("repo_root") or "")).resolve()
            marker_pid = int(payload.get("pid") or 0)
            marker_started_at = str(payload.get("process_started_at") or "").strip()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
        if owner != common_root:
            continue
        # Unlike linked worktrees, a standalone private clone is not present
        # in the host repository's worktree registry. Its owner marker is the
        # only way to distinguish an active debugger from a crash remnant.
        # Match PID *and* process start time so PID reuse cannot preserve or
        # delete the wrong checkout.
        if marker_pid > 0 and marker_started_at and is_pid_alive(marker_pid, marker_started_at):
            continue
        _restore_tree_writable(candidate)
        remove_tree(candidate, ignore_errors=True)


def _validated_block_debugger_surviving_workspace(
    run: RunState,
    repo_root: Path,
) -> Path | None:
    """Return the run-owned clone/container source checkout, if safely provable."""
    backend_name = (run.backend or SPEC_RUNTIME_CONFIG.execution.backend or "").strip()
    if backend_name not in {"clone", "container"}:
        return None
    recorded = str(run.worktree_path or "").strip()
    if not recorded:
        return None

    common_root = resolve_common_root(repo_root).resolve()
    configured_root = Path(SPEC_RUNTIME_CONFIG.execution.workspace_root).expanduser()
    if not configured_root.is_absolute():
        configured_root = common_root / configured_root
    workspace_root = configured_root.resolve()
    try:
        workspace_root.relative_to(common_root)
    except ValueError:
        return None
    if workspace_root == common_root:
        return None

    run_root = (workspace_root / run.run_id).resolve()
    if run_root.parent != workspace_root:
        return None
    expected_source = (run_root / "source").resolve()
    recorded_source = Path(recorded).expanduser().resolve()
    if recorded_source != expected_source:
        return None
    if not recorded_source.is_dir() or not (recorded_source / ".git").is_dir():
        return None
    return recorded_source


def _create_private_block_debugger_clone(
    *,
    source: Path,
    destination: Path,
    head_sha: str,
    owner_repo_root: Path,
) -> None:
    """Clone a surviving backend checkout without linking or mutating it."""
    verify_result = run_subprocess(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=source,
    )
    if verify_result.returncode != 0:
        detail = verify_result.stderr.strip() or verify_result.stdout.strip() or "object is not present"
        raise ValueError(
            "missing-workspace-ref for blocked-run debugger: "
            f"commit {_short_sha(head_sha)} is not available in the surviving workspace. Detail: {detail}"
        )

    clone_result = run_subprocess(
        ["git", "clone", "--no-local", "--no-checkout", str(source), str(destination)],
        cwd=owner_repo_root,
    )
    if clone_result.returncode != 0:
        detail = clone_result.stderr.strip() or clone_result.stdout.strip() or "git clone failed"
        remove_tree(destination, ignore_errors=True)
        raise ValueError(f"private debugger clone failed: {detail}")

    checkout_result = run_subprocess(
        ["git", "checkout", "--detach", head_sha],
        cwd=destination,
    )
    if checkout_result.returncode != 0:
        detail = checkout_result.stderr.strip() or checkout_result.stdout.strip() or "git checkout failed"
        remove_tree(destination, ignore_errors=True)
        raise ValueError(f"private debugger checkout failed: {detail}")

    # The clone must have no path back to the authoritative workspace. The
    # local-review environment also disables push URLs, but removing the remote
    # makes the isolation explicit and independently testable.
    run_subprocess(["git", "remote", "remove", "origin"], cwd=destination)
    (destination / BLOCK_DEBUGGER_PRIVATE_CLONE_MARKER).write_text(
        json.dumps(
            {
                "repo_root": str(resolve_common_root(owner_repo_root).resolve()),
                "pid": os.getpid(),
                "process_started_at": _current_process_started_at(),
            }
        )
        + "\n"
    )


def _resolve_worktree_linked_gitdir(worktree_path: Path) -> Path | None:
    """Return the linked gitdir for a git worktree, or None for a normal repo."""
    dot_git = worktree_path / ".git"
    if not dot_git.is_file():
        return None
    try:
        text = dot_git.read_text().strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not text.startswith(prefix):
        return None
    raw = text[len(prefix) :].strip()
    linked = Path(raw) if Path(raw).is_absolute() else (worktree_path / raw).resolve()
    return linked if linked.is_dir() else None


def _resolve_common_dir_from_linked_gitdir(linked_gitdir: Path) -> Path | None:
    """Resolve the common git dir from a linked gitdir's ``commondir`` file."""
    commondir_file = linked_gitdir / "commondir"
    if not commondir_file.is_file():
        return None
    try:
        raw = commondir_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    common = Path(raw) if Path(raw).is_absolute() else (linked_gitdir / raw).resolve()
    return common if common.is_dir() else None


def _setup_debugger_common_dir_guard(
    linked_gitdir: Path | None,
) -> tuple[dict[str, str], list[Path]]:
    """Create env overrides and temp dirs that prevent git writes through the common dir.

    Returns ``(extra_env, temp_dirs)`` — the caller must clean up
    *temp_dirs* after the debugger subprocess exits.  The env dict contains
    ``GIT_OBJECT_DIRECTORY`` / ``GIT_ALTERNATE_OBJECTS_DIRECTORIES`` to redirect
    object writes, ``GIT_COMMON_DIR`` pointing to a shadow common dir whose
    ``refs/`` and ``packed-refs`` are read-only (preventing ``git update-ref``
    from mutating real branch state), and ``core.hooksPath`` (via
    ``GIT_CONFIG_*`` entries) to install always-failing hooks.
    """
    extra_env: dict[str, str] = {}
    temp_dirs: list[Path] = []

    common_dir: Path | None = None
    if linked_gitdir is not None:
        common_dir = _resolve_common_dir_from_linked_gitdir(linked_gitdir)

    # --- Object-write guard ---------------------------------------------------
    # In a worktree, objects are stored in the common dir.  Redirect
    # GIT_OBJECT_DIRECTORY to a read-only temp dir so loose object creation
    # fails, while reads still work through GIT_ALTERNATE_OBJECTS_DIRECTORIES.
    if common_dir is not None:
        real_objects = common_dir / "objects"
        if real_objects.is_dir():
            ro_objects_dir = Path(tempfile.mkdtemp(prefix="spec-dbg-obj-"))
            os.chmod(str(ro_objects_dir), stat.S_IRUSR | stat.S_IXUSR)
            extra_env["GIT_OBJECT_DIRECTORY"] = str(ro_objects_dir)
            extra_env["GIT_ALTERNATE_OBJECTS_DIRECTORIES"] = str(real_objects)
            temp_dirs.append(ro_objects_dir)

    # --- Ref-write guard (shadow common dir) ----------------------------------
    # Create a shadow of the common git dir where refs/ and packed-refs are
    # read-only copies.  Point GIT_COMMON_DIR at the shadow so that
    # ``git update-ref`` (which bypasses hooks and does not create objects)
    # cannot mutate the real repository's branch state.
    if common_dir is not None:
        shadow = Path(tempfile.mkdtemp(prefix="spec-dbg-common-"))
        temp_dirs.append(shadow)
        # Copy *every* entry as read-only so no symlink can write back to
        # the real repository metadata (config, logs, HEAD, etc.).  The
        # objects directory is handled separately via GIT_OBJECT_DIRECTORY
        # and GIT_ALTERNATE_OBJECTS_DIRECTORIES above, so we can safely
        # skip it here to avoid a large copy.
        for entry in common_dir.iterdir():
            target = shadow / entry.name
            if entry.name == "objects":
                # Already redirected via env vars; symlink is fine here
                # because the empty read-only object dir takes precedence.
                try:
                    target.symlink_to(entry)
                except OSError:
                    pass
            elif entry.is_dir():
                shutil.copytree(str(entry), str(target))
                _make_tree_readonly(target)
            elif entry.is_file():
                shutil.copy2(str(entry), str(target))
                os.chmod(str(target), stat.S_IRUSR)
        extra_env["GIT_COMMON_DIR"] = str(shadow)

    # --- Hook guard ------------------------------------------------------------
    # Install always-failing hooks so that high-level git operations (commit,
    # merge, push) are blocked even if the agent bypasses filesystem permissions.
    hooks_dir = Path(tempfile.mkdtemp(prefix="spec-dbg-hooks-"))
    temp_dirs.append(hooks_dir)
    for hook_name in ("pre-commit", "pre-merge-commit", "pre-push"):
        hook_path = hooks_dir / hook_name
        hook_path.write_text("#!/bin/sh\nexit 1\n")
        hook_path.chmod(0o555)
    # Inject core.hooksPath via GIT_CONFIG_* env vars so it merges cleanly
    # with the caller's existing GIT_CONFIG entries.
    extra_env["_SPEC_DEBUGGER_HOOKS_PATH"] = str(hooks_dir)

    return extra_env, temp_dirs


@contextmanager
def _temporary_block_debugger_worktree(
    repo_root: Path,
    *,
    head_sha: str,
    surviving_workspace: Path | None = None,
    pre_seal: Callable[[Path], Path | None] | None = None,
) -> Iterator[tuple[Path, dict[str, str]]]:
    """Yield a detached, read-only worktree for blocked-run diagnosis.

    *pre_seal*, if provided, runs after the worktree is created but **before**
    the read-only chmod is applied so callers can stage files (e.g. MCP config,
    Codex isolated ``CODEX_HOME``) that must exist on disk before the agent
    launches. Its optional return value is treated as a subtree to keep
    writable when the worktree is sealed — used to keep ``CODEX_HOME`` writable
    for Codex's own runtime state writes while the rest of the worktree remains
    read-only.
    """
    _cleanup_stale_block_debugger_worktrees(repo_root)

    worktree_path = Path(
        tempfile.mkdtemp(
            prefix=LOCAL_BLOCK_DEBUGGER_WORKTREE_PREFIX,
            dir=LOCAL_REVIEW_WORKTREE_ROOT,
        )
    )
    worktree_path.rmdir()

    verify_result = run_subprocess(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=repo_root,
    )
    private_clone = False
    if verify_result.returncode != 0:
        if surviving_workspace is None:
            detail = verify_result.stderr.strip() or verify_result.stdout.strip() or "object is not present in host repo"
            raise ValueError(
                "missing-host-ref for blocked-run debugger: "
                f"commit {_short_sha(head_sha)} is not available in the host repository, and no validated "
                "run-owned surviving workspace is available at the expected "
                f"`.spec-workspaces/<run>/source` path. Detail: {detail}"
            )
        _create_private_block_debugger_clone(
            source=surviving_workspace,
            destination=worktree_path,
            head_sha=head_sha,
            owner_repo_root=repo_root,
        )
        private_clone = True
    else:
        add_result = run_subprocess(
            ["git", "worktree", "add", "--detach", str(worktree_path), head_sha],
            cwd=repo_root,
        )
        if add_result.returncode != 0:
            detail = add_result.stderr.strip() or add_result.stdout.strip() or "git worktree add failed"
            raise ValueError(f"git worktree add failed for blocked-run debugger: {detail}")

    actual_head_sha = _head_sha(worktree_path)
    if actual_head_sha and actual_head_sha != head_sha:
        if private_clone:
            _restore_tree_writable(worktree_path)
            remove_tree(worktree_path, ignore_errors=True)
            cleanup_error = ""
        else:
            cleanup_error = _cleanup_worktree_checkout(
                repo_root,
                worktree_path,
                delete_branch=False,
            )
        if cleanup_error:
            logger.warning(
                "Could not remove mismatched block debugger worktree %s: %s",
                worktree_path,
                cleanup_error,
            )
        raise ValueError(
            "Detached block debugger worktree checked out the wrong commit: "
            f"expected {_short_sha(head_sha)}, got {_short_sha(actual_head_sha)}"
        )

    writable_exclude: Path | None = None
    if pre_seal is not None:
        writable_exclude = pre_seal(worktree_path)

    # Make the worktree read-only at the filesystem level so the
    # debugger agent cannot edit files or create commits.
    # In a git worktree the `.git` file points to a linked gitdir
    # (e.g. `<repo>/.git/worktrees/<name>`).  We must also lock that
    # directory so the agent cannot create commits or update refs.
    _make_tree_readonly(worktree_path, exclude=writable_exclude)
    linked_gitdir = _resolve_worktree_linked_gitdir(worktree_path)
    if linked_gitdir is not None:
        _make_tree_readonly(linked_gitdir)

    # Prevent writes through the common git dir (shared object store,
    # refs, and packed-refs) by creating a shadow common dir with
    # read-only ref copies, redirecting GIT_OBJECT_DIRECTORY, and
    # installing always-failing hooks.
    extra_env, guard_temp_dirs = _setup_debugger_common_dir_guard(linked_gitdir)

    try:
        yield worktree_path, extra_env
    finally:
        if linked_gitdir is not None:
            _restore_tree_writable(linked_gitdir)
        _restore_tree_writable(worktree_path)
        for temp_dir in guard_temp_dirs:
            _restore_tree_writable(temp_dir)
            remove_tree(temp_dir, ignore_errors=True)
        if private_clone:
            remove_tree(worktree_path, ignore_errors=True)
            cleanup_error = ""
        else:
            cleanup_error = _cleanup_worktree_checkout(
                repo_root,
                worktree_path,
                delete_branch=False,
            )
        if cleanup_error:
            logger.warning(
                "Could not remove block debugger worktree %s: %s",
                worktree_path,
                cleanup_error,
            )


def _make_tree_readonly(root: Path, *, exclude: Path | None = None) -> None:
    """Remove write permission from all entries under *root*.

    If *exclude* is provided, that path (and everything under it) is skipped
    so callers can keep a writable subdirectory inside an otherwise read-only
    tree — used by the block debugger to keep the Codex isolated ``CODEX_HOME``
    writable for session/state bookkeeping while the worktree itself stays
    locked down.
    """
    root_str = os.path.abspath(os.fspath(root))
    try:
        if stat.S_ISLNK(os.lstat(root_str).st_mode):
            return
    except OSError:
        return

    exclude_str: str | None = None
    if exclude is not None:
        try:
            candidate = os.path.abspath(os.fspath(exclude))
            if os.path.commonpath((root_str, candidate)) == root_str:
                exclude_str = candidate
        except (OSError, ValueError):
            exclude_str = None

    def _excluded(path: str) -> bool:
        if exclude_str is None:
            return False
        try:
            candidate = os.path.abspath(path)
        except (OSError, ValueError):
            return False
        return candidate == exclude_str or candidate.startswith(exclude_str + os.sep)

    def _remove_write_bits(path: str) -> None:
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                return
            os.chmod(path, mode & ~_write_bits)
        except OSError:
            pass

    _write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if _excluded(dirpath):
            continue
        for name in filenames:
            p = os.path.join(dirpath, name)
            if _excluded(p):
                continue
            _remove_write_bits(p)
        for name in dirnames:
            p = os.path.join(dirpath, name)
            if _excluded(p):
                continue
            _remove_write_bits(p)
    if not _excluded(str(root)):
        _remove_write_bits(str(root))


def _restore_tree_writable(root: Path) -> None:
    """Restore owner-write permission so the tree can be removed."""
    root_str = os.path.abspath(os.fspath(root))
    try:
        if stat.S_ISLNK(os.lstat(root_str).st_mode):
            return
    except OSError:
        return

    def _restore_owner_write(path: str) -> None:
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                return
            os.chmod(path, mode | stat.S_IWUSR)
        except OSError:
            pass

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        _restore_owner_write(dirpath)
        for name in filenames:
            _restore_owner_write(os.path.join(dirpath, name))
        for name in dirnames:
            _restore_owner_write(os.path.join(dirpath, name))


def _check_local_mergeability_against_origin_master(
    repo_root: Path,
    worktree_path: Path,
    *,
    branch: str = "",
) -> MergeOriginMasterResult:
    if not worktree_path.is_dir():
        return MergeOriginMasterResult(
            status="error",
            stderr=f"Worktree missing: {worktree_path}",
        )

    fetch_error = _fetch_origin_master(worktree_path)
    if fetch_error:
        return MergeOriginMasterResult(status="error", stderr=fetch_error)

    head_sha = _head_sha(worktree_path)
    if not head_sha:
        return MergeOriginMasterResult(
            status="error",
            stderr=f"Could not determine HEAD SHA for {worktree_path}",
        )

    try:
        with _temporary_mergeability_worktree(
            repo_root, head_sha=head_sha, branch=branch
        ) as merge_worktree:
            return _merge_origin_master(
                merge_worktree,
                fetch_origin_master=False,
            )
    except ValueError as exc:
        return MergeOriginMasterResult(status="error", stderr=str(exc))


def _dirty_pr_state_allows_continue(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    *,
    context: str,
    pr_number: int | None = None,
) -> bool:
    mergeability_check = _check_local_mergeability_against_origin_master(
        repo_root,
        worktree_path,
        branch=run.branch,
    )
    if mergeability_check.status in {"success", "noop"}:
        issue = (
            f"PR for branch {run.branch} reports mergeStateStatus=DIRTY during {context}, "
            "but a local merge against origin/master is clean. Treating this as stale "
            "or remote-only DIRTY PR state."
        )
        if isinstance(pr_number, int):
            issue = (
                f"PR #{pr_number} for branch {run.branch} reports mergeStateStatus=DIRTY "
                f"during {context}, but a local merge against origin/master is clean. "
                "Treating this as stale or remote-only DIRTY PR state."
            )
        run.merge_conflict_error = ""
        run.mergeability_issue = issue
        logger.warning("%s", issue)
        return True

    if mergeability_check.status == "conflict":
        detail = mergeability_check.stderr or "git merge origin/master reported conflicts"
        issue = f"Confirmed local merge conflict with origin/master during {context}: {detail}"
        run.merge_conflict_error = issue
        run.mergeability_issue = issue
        run.last_error = issue
        return False

    detail = mergeability_check.stderr or "unknown error"
    issue = (
        f"PR for branch {run.branch} reports mergeStateStatus=DIRTY during {context}, "
        f"but the local mergeability check failed: {detail}"
    )
    run.merge_conflict_error = ""
    run.mergeability_issue = issue
    run.last_error = issue
    return False


def _required_checks_green(repo_root: Path, pr_number: int) -> tuple[bool, str]:
    forge_checks = _forge().get_required_checks(pr_number, cwd=repo_root)
    if forge_checks is None:
        # Could not query checks (gh error, unparseable payload): transient —
        # treat as pending so wait loops re-poll instead of either refusing
        # or falsely greening. `gh pr checks` exits non-zero for pending
        # checks while still printing JSON, so this branch is NOT taken for
        # ordinary pending checks (the forge parses the payload regardless
        # of exit code).
        return False, "Required checks still pending: unable to query required checks"
    if not forge_checks:
        # A definitively empty set is ambiguous: either the repo requires no
        # checks (so waiting would loop forever) or checks have not registered
        # on a freshly pushed head yet (so refusing the merge here could strand
        # the run). Treat it as green and let the merge
        # command itself arbitrate: GitHub's "required status check ... is
        # expected" rejection already routes to the bounded merge-race retry,
        # which polls until the checks register.
        logger.info(
            "No required checks reported for PR #%s; proceeding and letting the merge command arbitrate.",
            pr_number,
        )
        return True, ""

    pending: list[str] = []
    failing: list[str] = []
    for check in forge_checks:
        name = check.name or "unknown"
        bucket = (check.bucket or "").lower()
        link = check.url or ""
        decorated = f"{name} ({link})" if link else name
        if bucket in ("pass", "skipping"):
            continue
        if bucket in ("pending", "cancel"):
            pending.append(decorated)
            continue
        failing.append(decorated)

    if pending:
        return False, f"Required checks still pending: {', '.join(pending)}"
    if failing:
        return False, f"Required checks failing: {', '.join(failing)}"
    return True, ""


def _required_checks_still_pending(checks_error: str) -> bool:
    return checks_error.lower().startswith("required checks still pending:")


def _wait_for_pr_merged(
    repo_root: Path,
    branch: str,
    pr_number: int,
) -> tuple[bool, str]:
    """Wait for a PR to transition to MERGED."""
    deadline = time.time() + MERGE_CHECKS_TIMEOUT_SECONDS
    poll_interval = max(1, MERGE_CHECKS_POLL_INTERVAL_SECONDS)

    while True:
        pr_data = _find_pr_for_branch(repo_root, branch, state="all")
        if pr_data is None:
            return False, f"Could not query PR #{pr_number} while waiting for merge"

        pr_state = str(pr_data.get("state", "")).strip().upper()
        if pr_state == "MERGED":
            return True, ""
        if pr_state and pr_state != "OPEN":
            return (
                False,
                f"PR #{pr_number} entered unexpected state '{pr_state}' while waiting for merge",
            )

        remaining = deadline - time.time()
        if remaining <= 0:
            return False, f"Timed out waiting for PR #{pr_number} to merge"

        logger.info(
            "Waiting for PR #%s to merge (state=%s)...",
            pr_number,
            pr_state or "unknown",
        )
        _poll_sleep(min(poll_interval, remaining))


# ---------------------------------------------------------------------------
# Phase Handlers
# ---------------------------------------------------------------------------


def phase_bootstrap(run: RunState, repo_root: Path) -> str:
    """Check deps, create/reuse worktree, restore spec, write sandbox config, set status."""
    run = _ensure_run_spec_binding(run, repo_root)
    # Resolve the execution backend before mutating any worktree or branch state
    # so unimplemented/unknown backends fail deterministically at the lifecycle
    # boundary (per spec: clone/container must fail before workspace mutation).
    try:
        backend = _resolve_execution_backend()
    except (ExecutionBackendNotImplementedError, UnknownExecutionBackendError) as exc:
        run.last_error = str(exc)
        return "failed"
    if run.run_mode == "task":
        worktree_path = resolve_worktree_path(run, repo_root)
    else:
        spec_source = _existing_spec_source_path(repo_root, run)
        if spec_source is None or not spec_source.exists():
            missing_path = repo_root / _spec_path_for_run(run)
            run.last_error = f"Spec not found: {missing_path}"
            return "failed"

        spec_fm = parse_spec_frontmatter(spec_source)
        superseded_by = str(spec_fm.get("superseded_by", "")).strip()
        if superseded_by:
            resume_cmd = ["spec", "implement", "--spec", superseded_by, "--agent", run.agent]
            review_agent = _effective_review_agent(run)
            if review_agent and review_agent != run.agent:
                resume_cmd += ["--review-agent", review_agent]
            run.last_error = f"Spec '{run.spec_id}' is superseded by '{superseded_by}'. Use '{shlex.join(resume_cmd)}'."
            return "blocked"

        # Check dependencies
        blockers = check_dependencies_merged(repo_root, run.spec_id)
        if blockers:
            run.last_error = f"Unmet dependencies: {', '.join(blockers)}"
            return "blocked"

        worktree_path = resolve_worktree_path(run, repo_root)
    run.worktree_path = str(worktree_path)
    branch = run.branch

    if backend.identity.backend != "worktree":
        try:
            workspace = backend.prepare_workspace(
                run_id=run.run_id,
                spec_id=run.spec_id,
                branch=branch,
                repo_root=repo_root,
                worktree_path=worktree_path,
                base_ref=run.base_ref or BASE_REF,
            )
        except RuntimeError as exc:
            # prepare_workspace tears down any docker resources it created
            # before raising (see ContainerExecutionBackend.prepare_workspace),
            # so there is nothing to leak here — record the error and fail.
            run.last_error = str(exc)
            return "failed"
        worktree_path = workspace.path
        run.worktree_path = str(worktree_path)

        if run.run_mode == "task":
            inherited_spec = _spec_path_in_tree(worktree_path, run)
            if inherited_spec.exists():
                inherited_spec.unlink()
        else:
            spec_path = _restore_pinned_spec_into_worktree(repo_root, run, worktree_path)
            relative_spec = spec_path.relative_to(worktree_path).as_posix()
            status_result = run_subprocess(
                ["git", "status", "--porcelain", "--", relative_spec],
                cwd=worktree_path,
            )
            if status_result.returncode == 0 and status_result.stdout.strip():
                if not run_or_fail(
                    run,
                    ["git", "add", relative_spec],
                    cwd=worktree_path,
                    action=f"git add {relative_spec}",
                ):
                    return "failed"
                if not run_or_fail(
                    run,
                    ["git", "commit", "-m", f"Pin spec contract for {run.spec_id}"],
                    cwd=worktree_path,
                    action="git commit (pin spec contract)",
                ):
                    return "failed"

        if _backend_uses_provider_sandbox_config(backend):
            _write_sandbox_config(run.agent, worktree_path)

        install_cmd = _host_bootstrap_install_command(backend)
        if install_cmd:
            with install_cmd.launch_argv(cwd=worktree_path) as install_argv:
                if not run_or_fail(
                    run, install_argv, cwd=worktree_path, action=install_cmd.display()
                ):
                    return "failed"

        if backend.identity.backend != "container":
            try:
                run.save(worktree_path)
            except OSError as exc:
                run.last_error = (
                    "Could not mirror active run state into clone backend workspace "
                    f"for spec report: {exc}"
                )
                return "failed"

        return "passed"

    # Check if worktree already exists
    worktree_exists, error = _worktree_is_registered(repo_root, worktree_path)
    if error:
        run.last_error = error
        return "failed"

    if worktree_exists:
        if not worktree_path.is_dir():
            run.last_error = (
                f"Worktree registered but directory missing: {worktree_path}. Run 'git worktree prune' and retry."
            )
            return "failed"
        branch_error = _worktree_branch_alignment_error(worktree_path, branch)
        if branch_error:
            run.last_error = branch_error
            return "failed"
        logger.info("Reusing existing worktree at %s", worktree_path)
    else:
        # Clean up stale directory if present
        if worktree_path.exists():
            remove_tree(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        if run.resumed_from_branch:
            branch_error = _ensure_local_branch_available(repo_root, branch)
            if branch_error:
                run.last_error = branch_error
                return "failed"
            wt_result = run_subprocess(
                ["git", "worktree", "add", str(worktree_path), branch],
                cwd=repo_root,
            )
        else:
            # Check if branch already exists locally
            branch_check = run_subprocess(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo_root,
            )
            if branch_check.returncode == 0:
                wt_result = run_subprocess(
                    ["git", "worktree", "add", str(worktree_path), branch],
                    cwd=repo_root,
                )
            else:
                # Verify base ref exists
                base_ref = run.base_ref or BASE_REF
                base_check = run_subprocess(
                    ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
                    cwd=repo_root,
                )
                if base_check.returncode != 0:
                    run.last_error = f"Base ref '{base_ref}' not available. Run 'git fetch origin'."
                    return "failed"
                wt_result = run_subprocess(
                    ["git", "worktree", "add", "--no-track", str(worktree_path), "-b", branch, base_ref],
                    cwd=repo_root,
                )

        if wt_result.returncode != 0:
            run.last_error = f"git worktree add failed: {wt_result.stderr.strip()}"
            return "failed"

    if run.run_mode == "task":
        if not worktree_exists:
            inherited_spec = _spec_path_in_tree(worktree_path, run)
            if inherited_spec.exists():
                inherited_spec.unlink()
    else:
        spec_path = _restore_pinned_spec_into_worktree(repo_root, run, worktree_path)
        if not worktree_exists:
            relative_spec = spec_path.relative_to(worktree_path).as_posix()
            status_result = run_subprocess(
                ["git", "status", "--porcelain", "--", relative_spec],
                cwd=worktree_path,
            )
            if status_result.returncode == 0 and status_result.stdout.strip():
                if not run_or_fail(
                    run,
                    ["git", "add", relative_spec],
                    cwd=worktree_path,
                    action=f"git add {relative_spec}",
                ):
                    return "failed"
                if not run_or_fail(
                    run,
                    ["git", "commit", "-m", f"Pin spec contract for {run.spec_id}"],
                    cwd=worktree_path,
                    action="git commit (pin spec contract)",
                ):
                    return "failed"

    if _backend_uses_provider_sandbox_config(backend):
        _write_sandbox_config(run.agent, worktree_path)

    # Install dependencies so the worktree is ready to use.
    install_cmd = _host_bootstrap_install_command(backend)
    if install_cmd:
        with install_cmd.launch_argv(cwd=worktree_path) as install_argv:
            if not run_or_fail(
                run, install_argv, cwd=worktree_path, action=install_cmd.display()
            ):
                return "failed"

    return "passed"


def _host_bootstrap_install_command(backend: ExecutionBackend) -> CommandSpec | None:
    install_cmd = _selected_bootstrap_install_command()
    if backend.identity.backend == "container":
        return None
    return install_cmd


def _selected_bootstrap_install_command() -> CommandSpec | None:
    """Select typed bootstrap config while honoring legacy constructed configs."""
    selected = SPEC_RUNTIME_CONFIG.bootstrap_install.select()
    if selected is not None:
        return selected
    legacy = str(SPEC_RUNTIME_CONFIG.bootstrap_install_command or "").strip()
    return CommandSpec("script", legacy, "sh", "[bootstrap].install_command") if legacy else None


def _backend_uses_provider_sandbox_config(backend: ExecutionBackend) -> bool:
    identity = getattr(backend, "identity", None)
    backend_name = getattr(identity, "backend", "")
    if backend_name != "container":
        return True
    return backend.__class__.__name__ == "WorktreeExecutionBackend"


def _read_slug_from_spec(spec_path: Path) -> str:
    """Read a task slug from frontmatter `id:` or a legacy `slug:` line."""
    if not spec_path.exists():
        return ""
    frontmatter = parse_spec_frontmatter(spec_path)
    frontmatter_id = str(frontmatter.get("id", "")).strip()
    if frontmatter_id:
        return frontmatter_id
    for line in spec_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("slug:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _load_task_scoping_prompt(
    repo_root: Path,
    worktree_path: Path,
    *,
    resume: bool,
    agent: str = "",
) -> str:
    """Build the prompt for the task-scoping conversation."""
    prompt_path = repo_root / TASK_SCOPING_PROMPT_FILE
    top_level_spec_pattern = f"{SPEC_RUNTIME_CONFIG.paths.specs_dir}/*.md"
    if prompt_path.exists():
        prompt = prompt_path.read_text().strip()
    else:
        prompt = (
            "You are a task-scoping assistant. Ask the user what they want to build, "
            f"explore the codebase, agree on scope, then write exactly one task spec at "
            f"{TASK_SPEC_DIR}/<spec-id>.md. Do not write top-level {top_level_spec_pattern} files in task mode."
        )
    slug_note = (
        "Slug: choose a slug during the conversation, then write the spec to "
        f"`{TASK_SPEC_DIR}/<slug>.md` with matching frontmatter `id: <slug>`. "
        f"Do not write `{SPEC_RUNTIME_CONFIG.paths.specs_dir}/<slug>.md`; "
        "top-level specs are for `spec create`, not `spec task`.\n"
    )
    resume_note = (
        "This is a resumed session. Inspect existing work (the task spec under "
        f"{TASK_SPEC_DIR}/, "
        "branch, commits) "
        "and continue from where you left off.\n"
        if resume
        else "This is a fresh session.\n"
    )
    exit_method = _agent_exit_suffix(agent)
    exit_note = (
        "After writing the task spec file and committing it, clearly tell the user "
        "to exit the session so the orchestrator can continue to implementation. "
        f'For example: "You can now exit this session{exit_method} so the orchestrator '
        'can proceed to implementation."\n'
    )
    return f"{prompt}\n\n{slug_note}{resume_note}{exit_note}Worktree: {worktree_path}\n"


def _task_spec_files(worktree_path: Path) -> set[Path]:
    task_specs_dir = worktree_path / TASK_SPEC_DIR
    if not task_specs_dir.is_dir():
        return set()
    return set(task_specs_dir.glob("*.md"))


def _catalog_spec_files(worktree_path: Path) -> set[Path]:
    specs_dir = _specs_root(worktree_path)
    if not specs_dir.is_dir():
        return set()
    return set(specs_dir.glob("*.md"))


def _git_added_direct_spec_files(
    worktree_path: Path,
    *,
    base_ref: str,
    spec_dir: PurePosixPath,
) -> set[Path] | None:
    """Return direct markdown spec files added on this branch, or None if unavailable."""
    def add_relative_path(raw_path: str, added: set[Path]) -> None:
        rel = PurePosixPath(raw_path.strip())
        if rel.suffix == ".md" and rel.parent == spec_dir:
            added.add(worktree_path / rel.as_posix())

    diff_result = run_subprocess(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=A",
            f"{base_ref}...HEAD",
            "--",
            spec_dir.as_posix(),
        ],
        cwd=worktree_path,
    )
    status_result = run_subprocess(
        ["git", "status", "--porcelain", "--", spec_dir.as_posix()],
        cwd=worktree_path,
    )
    if diff_result.returncode != 0 and status_result.returncode != 0:
        return None

    added: set[Path] = set()
    if diff_result.returncode == 0:
        for raw_line in (diff_result.stdout or "").splitlines():
            add_relative_path(raw_line, added)
    if status_result.returncode == 0:
        for raw_line in (status_result.stdout or "").splitlines():
            status_code = raw_line[:2]
            if "A" not in status_code and status_code != "??":
                continue
            raw_path = raw_line[3:]
            if " -> " in raw_path:
                raw_path = raw_path.rsplit(" -> ", 1)[1]
            add_relative_path(raw_path, added)
    return added


def _narrow_new_specs_with_git(
    worktree_path: Path,
    candidates: set[Path],
    *,
    base_ref: str,
    spec_dir: PurePosixPath,
) -> list[Path]:
    if not candidates:
        return []
    git_added = _git_added_direct_spec_files(
        worktree_path,
        base_ref=base_ref,
        spec_dir=spec_dir,
    )
    if git_added is None:
        return sorted(candidates)
    return sorted(candidates & git_added)


def _format_spec_candidates(paths: list[Path], worktree_path: Path) -> str:
    return ", ".join(f"`{path.relative_to(worktree_path).as_posix()}`" for path in paths)


def _new_scoping_catalog_specs(
    worktree_path: Path,
    *,
    preexisting_catalog_specs: set[Path],
    base_ref: str,
) -> list[Path]:
    current_catalog_specs = _catalog_spec_files(worktree_path)
    return _narrow_new_specs_with_git(
        worktree_path,
        current_catalog_specs - preexisting_catalog_specs,
        base_ref=base_ref,
        spec_dir=PurePosixPath(SPEC_RUNTIME_CONFIG.paths.specs_dir),
    )


def _assert_no_task_scoping_catalog_specs(catalog_candidates: list[Path], worktree_path: Path) -> None:
    if len(catalog_candidates) == 1:
        misplaced_spec = catalog_candidates[0]
        raise RuntimeError(
            "Task scoping wrote top-level spec "
            f"`{misplaced_spec.relative_to(worktree_path).as_posix()}`, but task mode requires "
            f"`{TASK_SPEC_DIR}/{misplaced_spec.name}`."
        )
    if len(catalog_candidates) > 1:
        candidates = _format_spec_candidates(catalog_candidates, worktree_path)
        raise RuntimeError(
            "Task scoping wrote multiple top-level specs: "
            f"{candidates}. Task mode requires exactly one `{TASK_SPEC_DIR}/<spec-id>.md` file."
        )


def _assert_task_spec_id_is_unambiguous(
    repo_root: Path,
    worktree_path: Path,
    *,
    spec_id: str,
) -> None:
    product_spec = _specs_root(worktree_path) / f"{spec_id}.md"
    if product_spec.exists() or (_specs_root(repo_root) / f"{spec_id}.md").exists():
        raise RuntimeError(f"Task spec id '{spec_id}' collides with top-level {_catalog_spec_relpath(spec_id)}.")


def _resolve_scoped_task_spec(
    repo_root: Path,
    run: RunState,
    worktree_path: Path,
    *,
    preexisting_task_specs: set[Path],
    preexisting_catalog_specs: set[Path],
) -> Path:
    current_task_specs = _task_spec_files(worktree_path)
    task_candidates = current_task_specs - preexisting_task_specs
    base_ref = run.base_ref or BASE_REF
    if task_candidates:
        new_task_specs = _narrow_new_specs_with_git(
            worktree_path,
            task_candidates,
            base_ref=base_ref,
            spec_dir=PurePosixPath(TASK_SPEC_DIR),
        )
    else:
        new_task_specs = sorted(task_candidates)

    catalog_candidates: list[Path] | None = None

    def assert_no_catalog_specs() -> None:
        nonlocal catalog_candidates
        if catalog_candidates is None:
            catalog_candidates = _new_scoping_catalog_specs(
                worktree_path,
                preexisting_catalog_specs=preexisting_catalog_specs,
                base_ref=base_ref,
            )
        _assert_no_task_scoping_catalog_specs(catalog_candidates, worktree_path)

    if len(new_task_specs) == 1:
        assert_no_catalog_specs()
        task_spec = new_task_specs[0]
    elif len(new_task_specs) > 1:
        candidates = _format_spec_candidates(new_task_specs, worktree_path)
        raise RuntimeError(
            "Scoping created multiple task specs: "
            f"{candidates}. Keep exactly one `{TASK_SPEC_DIR}/<spec-id>.md` file."
        )
    else:
        existing_spec = _spec_path_in_tree(worktree_path, run)
        if existing_spec.exists():
            assert_no_catalog_specs()
            task_spec = existing_spec
        else:
            assert_no_catalog_specs()
            legacy_spec = _legacy_current_spec_path(repo_root, run)
            if legacy_spec.exists():
                raise RuntimeError(
                    "Scoping wrote legacy CURRENT_SPEC.md. Write the contract to "
                    f"`{TASK_SPEC_DIR}/<spec-id>.md` instead."
                )
            raise RuntimeError(f"Scoping session exited without producing `{TASK_SPEC_DIR}/<spec-id>.md`.")

    chosen_slug = task_spec.stem
    frontmatter_slug = _read_slug_from_spec(task_spec)
    if frontmatter_slug and frontmatter_slug != chosen_slug:
        raise RuntimeError(
            f"Task spec file path `{task_spec.name}` does not match frontmatter id `{frontmatter_slug}`."
        )
    if not SPEC_ID_RE.fullmatch(chosen_slug):
        raise RuntimeError(f"Task spec id '{chosen_slug}' is invalid; use lowercase kebab-case.")
    _assert_task_spec_id_is_unambiguous(
        repo_root,
        worktree_path,
        spec_id=chosen_slug,
    )
    return task_spec


def _build_task_scoping_command(
    agent: str,
    worktree_path: Path,
    state_dir: Path,
    prompt: str,
    initial_prompt: str,
) -> list[str]:
    """Build the CLI command for the task-scoping agent session."""
    adapter = get_agent_adapter(agent)
    authoring_kwargs: dict[str, object] = {
        "prompt": prompt,
        "worktree_path": worktree_path,
        "state_dir": state_dir,
        "initial_prompt": initial_prompt,
        "mcp_config_path": _mcp_config_path(worktree_path),
    }
    return adapter.build_authoring_command(**authoring_kwargs)


def _agent_exit_suffix(agent: str) -> str:
    """Return the agent-specific exit shortcut suffix for user-facing copy."""
    if agent in ("claude", "codex"):
        return " (Ctrl+D)"
    return ""


def phase_scoping(run: RunState, repo_root: Path) -> str:
    """Run a conversational agent session that produces a task spec."""
    if run.run_mode != "task":
        # Scoping is only for task-mode runs; spec-mode skips it.
        return "passed"

    worktree_path = resolve_worktree_path(run, repo_root)
    if not worktree_path.is_dir():
        run.last_error = f"Worktree missing: {worktree_path}"
        return "failed"

    spec_file = _spec_path_in_tree(worktree_path, run)
    if spec_file.exists():
        logger.info("%s already exists, skipping scoping.", spec_file)
        return "passed"

    if not sys.stdin.isatty():
        run.last_error = "Task scoping requires an interactive terminal."
        return "failed"

    prompt = _load_task_scoping_prompt(
        repo_root,
        worktree_path,
        resume=False,
        agent=run.agent,
    )

    _write_sandbox_config(run.agent, worktree_path)
    state_dir = _state_root(repo_root)
    preexisting_task_specs = _task_spec_files(worktree_path)
    preexisting_catalog_specs = _catalog_spec_files(worktree_path)

    initial_prompt = (
        "Ask the user what they want to build, then scope it and write exactly one task contract "
        f"to `{TASK_SPEC_DIR}/<spec-id>.md`. Do not write top-level "
        f"{SPEC_RUNTIME_CONFIG.paths.specs_dir}/*.md files."
    )
    cmd = _build_task_scoping_command(
        run.agent,
        worktree_path,
        state_dir,
        prompt,
        initial_prompt,
    )

    logger.info("Launching task-scoping session in %s", worktree_path)
    print(f"Launching task-scoping session in {worktree_path}")
    exit_hint = f"exit the agent session{_agent_exit_suffix(run.agent)}"
    print(f"Tip: when scoping is done, {exit_hint} to start implementation.")

    try:
        completed = subprocess.run(cmd, cwd=worktree_path)
    except FileNotFoundError as exc:
        run.last_error = f"Agent binary not found: {exc}"
        return "failed"

    try:
        scoped_spec = _resolve_scoped_task_spec(
            repo_root,
            run,
            worktree_path,
            preexisting_task_specs=preexisting_task_specs,
            preexisting_catalog_specs=preexisting_catalog_specs,
        )
    except RuntimeError as exc:
        if completed.returncode != 0:
            run.last_error = f"Scoping agent exited with code {completed.returncode}. {exc}"
        else:
            run.last_error = str(exc)
        return "failed"

    if completed.returncode != 0 and not scoped_spec.exists():
        run.last_error = f"Scoping agent exited with code {completed.returncode}."
        return "failed"

    # Read slug from the task spec file and rename branch to match.
    chosen_slug = _read_slug_from_spec(scoped_spec) or scoped_spec.stem
    if chosen_slug and SPEC_ID_RE.fullmatch(chosen_slug):
        old_branch = run.branch
        # Extract the run token from the current branch.
        # Branch format: task/<spec_id>--<token>
        parts = old_branch.rsplit("--", 1)
        token = parts[1] if len(parts) == 2 else ""
        new_branch = f"{TASK_BRANCH_PREFIX}{chosen_slug}--{token}" if token else f"{TASK_BRANCH_PREFIX}{chosen_slug}"

        if new_branch != old_branch:
            rename_result = run_subprocess(
                ["git", "branch", "-m", old_branch, new_branch],
                cwd=worktree_path,
            )
            if rename_result.returncode == 0:
                run.branch = new_branch
                run.spec_id = chosen_slug
                logger.info(
                    "Renamed branch %s -> %s (slug: %s)",
                    old_branch,
                    new_branch,
                    chosen_slug,
                )
            else:
                run.last_error = f"Branch rename failed ({old_branch} -> {new_branch}): {rename_result.stderr.strip()}"
                run.save(repo_root)
                return "failed"

    _pin_run_spec_from_file(repo_root, run, scoped_spec, tree_root=worktree_path)
    run.save(repo_root)
    return "passed"


def phase_intake(run: RunState, repo_root: Path) -> str:
    """Collect and persist interactive intake answers for specs that require them."""
    if run.run_mode == "task":
        return "passed"
    spec_path = _active_spec_path(repo_root, run, prefer_worktree=True)
    if spec_path is None or not spec_path.exists():
        missing_path = repo_root / _spec_path_for_run(run)
        run.last_error = f"Spec not found: {missing_path}"
        return "failed"

    try:
        intake_spec = parse_intake_spec(spec_path)
    except ValueError as exc:
        run.last_error = f"Invalid intake schema for {run.spec_id}: {exc}"
        return "failed"

    if not intake_spec.required:
        run.intake_reset_requested = False
        return "passed"

    expected_schema_hash = intake_spec.schema_hash()
    intake_path = _state_root(repo_root) / "runs" / run.run_id / "intake.json"
    existing_intake = IntakeResult.load(repo_root, run.run_id)

    if run.intake_reset_requested and intake_path.exists():
        previous_payload: dict = {}
        if existing_intake is not None:
            previous_payload = asdict(existing_intake)
        else:
            try:
                previous_payload = json.loads(intake_path.read_text())
            except (json.JSONDecodeError, OSError):
                previous_payload = {}
        _audit_intake_reset(
            repo_root,
            run,
            previous_payload=previous_payload,
            reason="explicit reset requested",
        )
        intake_path.unlink(missing_ok=True)
        existing_intake = None

    if intake_path.exists() and existing_intake is None:
        run.last_error = "Stored intake answers are unreadable. Re-run with --reset-intake to recapture answers."
        return "blocked"

    if existing_intake is not None:
        if existing_intake.version != INTAKE_FILE_VERSION:
            run.last_error = (
                "Stored intake answers use an unsupported payload version. "
                "Re-run with --reset-intake to capture fresh answers."
            )
            return "blocked"
        if (
            existing_intake.schema_version != intake_spec.schema_version
            or existing_intake.schema_hash != expected_schema_hash
        ):
            run.last_error = (
                "Stored intake answers are incompatible with the current intake schema. Re-run with --reset-intake."
            )
            return "blocked"
        validation_errors = _validate_intake_answers(
            intake_spec,
            existing_intake.answers,
        )
        if validation_errors:
            run.last_error = (
                "Stored intake answers are invalid for the current schema: "
                f"{validation_errors[0]}. Re-run with --reset-intake."
            )
            return "blocked"
        run.intake_reset_requested = False
        return "passed"

    if not sys.stdin.isatty():
        run.last_error = (
            "Spec intake requires interactive answers but stdin is not interactive. "
            f"Run `spec phase --spec {run.spec_id} --phase intake` in a tty "
            "or re-run with --reset-intake once interactive input is available."
        )
        return "blocked"

    print(f"Intake required for spec '{run.spec_id}'. Answer the questions below.")
    answers: dict = {}
    for question in intake_spec.questions:
        while True:
            raw_answer = input(_format_intake_prompt(question))
            try:
                parsed_answer = _coerce_and_validate_intake_answer(
                    raw_answer,
                    question,
                )
            except ValueError as exc:
                print(f"Invalid answer for '{question.id}': {exc}")
                continue
            if parsed_answer is not None:
                answers[question.id] = parsed_answer
            break

    intake_result = IntakeResult(
        version=INTAKE_FILE_VERSION,
        spec_id=run.spec_id,
        run_id=run.run_id,
        schema_version=intake_spec.schema_version,
        schema_hash=expected_schema_hash,
        questions=[question.to_schema_payload() for question in intake_spec.questions],
        answers=answers,
        completed_at=_now_iso(),
    )
    intake_result.save(repo_root, run.run_id)
    run.intake_reset_requested = False
    return "passed"


def _ensure_required_intake_before_implement(
    run: RunState,
    repo_root: Path,
) -> str:
    """Fail-closed precondition for required intake before implement."""
    spec_path = _active_spec_path(repo_root, run, prefer_worktree=True)
    if spec_path is None or not spec_path.exists():
        missing_path = repo_root / _spec_path_for_run(run)
        run.last_error = f"Spec not found: {missing_path}"
        return "failed"

    try:
        intake_spec = parse_intake_spec(spec_path)
    except ValueError as exc:
        run.last_error = f"Invalid intake schema for {run.spec_id}: {exc}"
        return "failed"

    if not intake_spec.required:
        return "passed"

    if run.intake_reset_requested:
        run.last_error = (
            "Intake reset requested; required intake must be re-captured before implement. "
            f"Run `spec phase --spec {run.spec_id} --phase intake`."
        )
        return "blocked"

    intake_path = _state_root(repo_root) / "runs" / run.run_id / "intake.json"
    intake_result = IntakeResult.load(repo_root, run.run_id)
    expected_schema_hash = intake_spec.schema_hash()

    if intake_path.exists() and intake_result is None:
        run.last_error = "Stored intake answers are unreadable. Re-run with --reset-intake to recapture answers."
        return "blocked"

    if intake_result is None:
        run.last_error = (
            "Required intake answers are missing. "
            f"Run `spec phase --spec {run.spec_id} --phase intake` before implement."
        )
        return "blocked"

    if intake_result.version != INTAKE_FILE_VERSION:
        run.last_error = (
            "Stored intake answers use an unsupported payload version. "
            "Re-run with --reset-intake to capture fresh answers."
        )
        return "blocked"

    if intake_result.schema_version != intake_spec.schema_version or intake_result.schema_hash != expected_schema_hash:
        run.last_error = (
            "Stored intake answers are incompatible with the current intake schema. Re-run with --reset-intake."
        )
        return "blocked"

    validation_errors = _validate_intake_answers(
        intake_spec,
        intake_result.answers,
    )
    if validation_errors:
        run.last_error = (
            "Stored intake answers are invalid for the current schema: "
            f"{validation_errors[0]}. Re-run with --reset-intake."
        )
        return "blocked"

    return "passed"


def _write_sandbox_config(
    agent: str,
    worktree_path: Path,
    *,
    extra_mcp_servers: dict[str, dict[str, object]] | None = None,
) -> None:
    """Write agent-specific sandbox configuration."""
    if agent == "claude":
        config_dir = worktree_path / ".claude"
        if config_dir.is_symlink() or (
            config_dir.exists() and not config_dir.is_dir()
        ):
            raise ValueError(
                f"Refusing to write Claude config through non-directory path {config_dir}"
            )
        config_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "skipDangerousModePermissionPrompt": True,
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "network": {
                    "allowedDomains": [
                        "github.com",
                        "api.github.com",
                        "*.blob.core.windows.net",
                        "api.anthropic.com",
                        "*.npmjs.org",
                        "pypi.org",
                        "files.pythonhosted.org",
                        "registry.yarnpkg.com",
                        "localhost",
                        "127.0.0.1",
                    ]
                },
            },
            "permissions": {
                "deny": [
                    "Bash(git push --force*)",
                    "Bash(git push * --force*)",
                    "Bash(git push -f*)",
                    "Bash(git push * -f*)",
                    "Bash(git reset --hard*)",
                    "Bash(git reset * --hard*)",
                ],
            },
        }
        (config_dir / "settings.local.json").write_text(json.dumps(config, indent=2) + "\n")
        _write_claude_mcp_config(
            worktree_path,
            extra_mcp_servers=extra_mcp_servers,
        )
    elif agent == "codex":
        # MCP servers for Codex are injected via `-c mcp_servers.<name>.*` CLI
        # overrides at launch time (see CodexAgent.build_implement_command), not
        # written here. Codex does not read this .codex/config.toml file for
        # MCP configuration, so ``extra_mcp_servers`` is intentionally ignored.
        config_dir = worktree_path / ".codex"
        if config_dir.is_symlink() or (
            config_dir.exists() and not config_dir.is_dir()
        ):
            raise ValueError(
                f"Refusing to write Codex config through non-directory path {config_dir}"
            )
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            'sandbox_mode = "workspace-write"\n'
            'approval_policy = "never"\n\n'
            "[sandbox_workspace_write]\n"
            "network_access = true\n"
        )


def _claude_supports_stream_json() -> bool:
    """Return True if the installed Claude CLI supports --output-format stream-json."""
    try:
        result = run_subprocess(["claude", "--help"])
        if result.returncode == 0 and "stream-json" in (result.stdout or ""):
            return True
    except FileNotFoundError:
        pass
    return False


def _validate_codex_exec(run: RunState, *, require_output_schema: bool = False) -> bool:
    """Check that `codex exec` is available. Return True if supported."""
    try:
        result = run_subprocess(["codex", "exec", "--help"])
        if result.returncode != 0:
            run.last_error = (
                "Codex CLI does not support 'exec' subcommand. "
                "Upgrade to a version with non-interactive mode: "
                "npm install -g @openai/codex"
            )
            return False
        help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if "--json" not in help_text:
            run.last_error = (
                "Codex CLI does not support 'exec --json'. "
                "Upgrade to a version with JSON progress events: "
                "npm install -g @openai/codex"
            )
            return False
        if require_output_schema and "--output-schema" not in help_text:
            run.last_error = (
                "Codex CLI does not support 'exec --output-schema'. "
                "Upgrade to a version with schema-constrained non-interactive mode: "
                "npm install -g @openai/codex"
            )
            return False
    except FileNotFoundError:
        run.last_error = "Codex CLI not found on PATH. Install it with: npm install -g @openai/codex"
        return False
    return True


def _sync_reused_branch_before_implement(run: RunState, worktree_path: Path) -> str:
    """Bring a reused branch up to date with origin/master before first implement."""
    if not run.resumed_from_branch or run.attempts > 0 or run.last_merged_master_sha or run.merge_conflict_error:
        return "passed"

    # Web sessions save base_ref="HEAD" as a fallback when origin/main is
    # unavailable locally.  In that case there is no remote ref to sync
    # against, so skip the sync entirely — the branch is already current.
    if run.base_ref == "HEAD":
        return "passed"

    # Honor the per-run base ref when set, otherwise default to BASE_REF.
    ref = run.base_ref if run.base_ref else BASE_REF

    fetch_error = _fetch_origin_master(worktree_path, base_ref=ref)
    if fetch_error:
        run.last_error = f"Failed to fetch {ref} before implement on reused branch: {fetch_error}"
        return "failed"

    origin_master_sha = _resolve_git_ref_sha(worktree_path, ref)
    if not origin_master_sha:
        run.last_error = f"Could not resolve {ref} before implement on reused branch."
        return "failed"

    contains_master, contains_error = _branch_contains_ref(worktree_path, ref)
    if contains_master is None:
        run.last_error = f"Failed to check whether reused branch already contains {ref}: {contains_error}"
        return "failed"
    if contains_master:
        run.last_merged_master_sha = origin_master_sha
        return "passed"

    merge_result = _merge_origin_master(worktree_path, fetch_origin_master=False, base_ref=ref)
    if merge_result.status in {"success", "noop"}:
        run.last_merged_master_sha = origin_master_sha
        return "passed"
    if merge_result.status == "conflict":
        detail = merge_result.stderr or f"git merge {ref} reported conflicts"
        run.merge_conflict_error = (
            f"Merge conflict while merging {ref} into reused branch before implement: {detail}"
        )
        run.mergeability_issue = run.merge_conflict_error
        return "passed"

    detail = merge_result.stderr or "unknown error"
    run.last_error = f"Failed to merge {ref} into reused branch before implement: {detail}"
    return "failed"


def _build_implement_attempt_context(
    run: RunState,
    repo_root: Path,
) -> ImplementAttemptContextBundle:
    if run.merge_conflict_error:
        reason = "merge_conflict"
    elif run.review_decision_status == "request_changes":
        reason = "review_feedback"
    elif run.attempts > 0 or run.pending_block_debugger_signature:
        reason = "retry"
    else:
        reason = "initial"

    spec_path = _spec_path_for_run(run)
    spec_file = repo_root / spec_path
    ctx = ImplementContext(
        implement_reason=reason,
        objective=f"Implement spec {run.spec_id}",
        run_id=run.run_id,
        attempt_number=run.attempts + 1,
        # Before a fresh launch is reserved, this identifies the most recent
        # launch whose result may be deliberately reused after operator input.
        launch_number=max(0, int(run.implement_launches)),
        run_state_dir=_run_state_dir_for_run(run.run_id),
        spec_path=spec_path,
        spec_revision=run.spec_revision,
        acceptance_checklist=_extract_acceptance_checklist_items(spec_file),
        verification_expectations=_extract_verification_expectations(spec_file),
    )

    intake_result = IntakeResult.load(repo_root, run.run_id)
    if intake_result is not None:
        ctx.intake = {
            "version": intake_result.version,
            "schema_version": intake_result.schema_version,
            "schema_hash": intake_result.schema_hash,
            "answers": intake_result.answers,
            "completed_at": intake_result.completed_at,
        }

    operator_request = _load_operator_request(repo_root, run)
    if operator_request is not None:
        ctx.operator_request_path = _relative_run_artifact_path(
            repo_root,
            _state_root(repo_root) / "runs" / run.run_id / OPERATOR_REQUEST_FILENAME,
        )
        if _is_resolved_operator_request(operator_request):
            ctx.operator_request_kind = operator_request.kind
            ctx.operator_request_prompt = operator_request.prompt
            ctx.operator_request_context = dict(operator_request.context)
            ctx.operator_request_suggested_action = operator_request.suggested_action
            ctx.operator_request_options = list(operator_request.options)
            ctx.operator_request_requires_full_session = operator_request.requires_full_session
            ctx.operator_request_response = operator_request.response
            ctx.operator_request_response_source = operator_request.response_source
            if operator_request.response_consumed_attempt_number is None:
                operator_request.response_consumed_attempt_number = ctx.attempt_number
                operator_request.consumed_at = _now_iso()
                operator_request.status = "consumed"
                operator_request.continuation = OPERATOR_CONTINUATION_CONTINUE_WORKFLOW
                operator_request.save(repo_root, run.run_id)

    operator_steering = OperatorSteering.load(repo_root, run.run_id)
    if operator_steering is not None:
        ctx.operator_steering_path = _relative_run_artifact_path(
            repo_root,
            _state_root(repo_root) / "runs" / run.run_id / OPERATOR_STEERING_FILENAME,
        )
        if operator_steering.status == "active":
            ctx.operator_steering_event_id = operator_steering.event_id
            ctx.operator_steering_message = operator_steering.message
            ctx.operator_steering_context = dict(operator_steering.context)
            ctx.operator_steering_provided_by = operator_steering.provided_by
            ctx.operator_steering_provided_at = operator_steering.provided_at
            ctx.operator_steering_source = operator_steering.source

    pending_guided_retry_signature = ""
    if reason != "initial":
        retry_package = _build_retry_failure_package(run, repo_root)
        ctx.triggering_phase = retry_package.triggering_phase
        ctx.previous_implement_attempt_number = retry_package.previous_implement_attempt_number
        ctx.previous_implement_result_path = retry_package.previous_implement_result_path
        ctx.triggering_review_result_path = retry_package.review_result_path
        ctx.triggering_block_diagnosis_path = retry_package.block_diagnosis_path
        if not ctx.operator_request_path:
            ctx.operator_request_path = retry_package.operator_request_path
        ctx.current_head_sha = retry_package.current_head_sha
        ctx.reviewed_head_sha = retry_package.reviewed_head_sha
        ctx.first_failed_test_nodeid = retry_package.first_failed_test_nodeid
        ctx.first_failed_test_reproducer = retry_package.first_failed_test_reproducer
        ctx.first_failed_test_diagnostic = retry_package.first_failed_test_diagnostic
        ctx.failing_commands = retry_package.active_gates_or_checks
        ctx.failure_summary = "; ".join(retry_package.summary_parts)
        ctx.gate_output = "\n".join(retry_package.gate_output_parts)
        ctx.review_feedback_active = retry_package.review_feedback_active
        ctx.stale_review_feedback = retry_package.stale_review_feedback
        ctx.review_findings_count = retry_package.review_findings_count
        ctx.unresolved_review_findings = retry_package.review_findings
        ctx.review_source_check_url = retry_package.review_source_check_url
        ctx.targeted_test_not_executed = retry_package.targeted_test_not_executed
        ctx.targeted_test_not_executed_warning = retry_package.targeted_test_not_executed_warning
        ctx.mergeability_issue = retry_package.mergeability_issue
        ctx.rescue_snapshot_path = retry_package.rescue_snapshot_path
        ctx.rescue_snapshot_summary = retry_package.rescue_snapshot_summary

        diagnosis = BlockDiagnosis.load(repo_root, run.run_id)
        if (
            diagnosis is not None
            and run.pending_block_debugger_signature
            and diagnosis.blocker_signature == run.pending_block_debugger_signature
            and not diagnosis.requires_human_attention
        ):
            ctx.debugger_summary = diagnosis.summary
            ctx.debugger_root_cause = diagnosis.root_cause
            ctx.debugger_confidence = diagnosis.confidence
            ctx.debugger_category = diagnosis.category
            ctx.debugger_next_best_action = diagnosis.next_best_action
            ctx.debugger_requires_human_attention = diagnosis.requires_human_attention
            ctx.debugger_needs_new_commit = diagnosis.needs_new_commit
            ctx.debugger_blocker_signature = diagnosis.blocker_signature
            pending_guided_retry_signature = diagnosis.blocker_signature
        elif (
            diagnosis is not None
            and not diagnosis.requires_human_attention
            and diagnosis.attempt_number is not None
            and ctx.attempt_number - diagnosis.attempt_number <= 3
            and ctx.first_failed_test_nodeid
            and diagnosis.first_failed_test_nodeid
            and ctx.first_failed_test_nodeid == diagnosis.first_failed_test_nodeid
        ):
            # Stale diagnosis: same test node keeps failing with evolving symptoms.
            # Attach as advisory context only — no guided retry grant.
            ctx.debugger_summary = diagnosis.summary
            ctx.debugger_root_cause = diagnosis.root_cause
            ctx.debugger_confidence = diagnosis.confidence
            ctx.debugger_category = diagnosis.category
            ctx.debugger_next_best_action = diagnosis.next_best_action
            ctx.debugger_requires_human_attention = diagnosis.requires_human_attention
            ctx.debugger_needs_new_commit = diagnosis.needs_new_commit
            ctx.debugger_blocker_signature = diagnosis.blocker_signature
            ctx.debugger_diagnosis_stale = True

    return ImplementAttemptContextBundle(
        reason=reason,
        context=ctx,
        pending_guided_retry_signature=pending_guided_retry_signature,
    )


def _validate_review_retry_lineage(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
) -> str:
    if ctx.triggering_phase != "review":
        return ""
    if not ctx.triggering_review_result_path or not ctx.reviewed_head_sha or not ctx.current_head_sha:
        return ""
    if ctx.reviewed_head_sha == ctx.current_head_sha:
        return ""

    newer_result, newer_path = _find_review_result_for_head(
        repo_root,
        run.run_id,
        reviewed_head_sha=ctx.current_head_sha,
    )
    if newer_result is not None:
        ctx.triggering_review_result_path = _relative_run_artifact_path(repo_root, newer_path)
        ctx.reviewed_head_sha = newer_result.reviewed_head_sha
        ctx.review_feedback_active = newer_result.status == "request_changes"
        ctx.stale_review_feedback = False
        ctx.review_findings_count = len(newer_result.findings)
        ctx.unresolved_review_findings = [asdict(finding) for finding in newer_result.findings]
        ctx.review_source_check_url = newer_result.source_check_url
        summary = newer_result.summary or "Independent review requested changes."
        if ctx.review_findings_count:
            summary += f" ({ctx.review_findings_count} unresolved findings)"
        summary_parts = [part for part in ctx.failure_summary.split("; ") if part and not part.startswith("Stale review evidence")]
        summary_parts.append(summary)
        ctx.failure_summary = "; ".join(dict.fromkeys(summary_parts))
        return ""

    relation = _git_ref_is_ancestor(worktree_path, ctx.reviewed_head_sha, ctx.current_head_sha)
    if relation is True:
        return (
            "Review-triggered retry is pinned to "
            f"{ctx.triggering_review_result_path} for reviewed head {_short_sha(ctx.reviewed_head_sha)}, "
            f"but the branch moved to {_short_sha(ctx.current_head_sha)} before retry construction. "
            "This looks like same-run branch movement after review, so the orchestrator is stopping instead "
            "of silently reusing stale findings."
        )
    if relation is False:
        return (
            "Review-triggered retry is pinned to "
            f"{ctx.triggering_review_result_path} for reviewed head {_short_sha(ctx.reviewed_head_sha)}, "
            f"but the branch now points at {_short_sha(ctx.current_head_sha)} and no longer descends from the "
            "reviewed head. This looks like external branch drift or manual movement, so the retry is blocked "
            "until review evidence is regenerated for the current head."
        )
    return (
        "Review-triggered retry could not confirm that the current head matches the pinned review artifact "
        f"{ctx.triggering_review_result_path}. The orchestrator is stopping rather than guessing which review "
        "payload should drive this retry."
    )


def _recent_commit_lines(worktree_path: Path) -> list[str]:
    log_result = run_subprocess(
        ["git", "log", "--oneline", "-10"],
        cwd=worktree_path,
    )
    if log_result.returncode != 0:
        return []
    return log_result.stdout.strip().splitlines()


def _build_setup_failure_prompt(
    setup_manifest: ImplementSetupManifest,
) -> str:
    failure = setup_manifest.failure
    if failure is None:
        return ""

    exit_line = (
        "launch failed"
        if failure.launch_error
        else f"exit_code: {failure.exit_code}"
    )

    env_keys = sorted(setup_manifest.env.keys())
    mcp_names = sorted(setup_manifest.mcp_servers.keys())
    managed_names = [proc.name for proc in setup_manifest.managed_processes]

    lines = [
        "⚠️ Implement prepare step failed (best-effort prewarm; not an admission gate).",
        "",
        f"command: {failure.command}",
        exit_line,
    ]
    if failure.launch_error and failure.message:
        lines.extend(["message:", f"  {failure.message}"])
    if failure.stderr_tail:
        lines.append("stderr (tail):")
        for tail_line in failure.stderr_tail.splitlines():
            lines.append(f"  {tail_line}")
    # stdout_tail normally has manifest-shaped JSON stripped upstream, so only
    # non-manifest content (plain logs, structured error records) reaches here.
    # The pure-manifest guard is defensive — keeps DATABASE_URL etc. out of the
    # prompt if a caller ever fabricates a failure with raw manifest stdout.
    if failure.stdout_tail and not _stdout_is_pure_manifest(failure.stdout_tail):
        lines.append("stdout (tail):")
        for tail_line in failure.stdout_tail.splitlines():
            lines.append(f"  {tail_line}")
    lines.append("partially-initialized:")
    lines.append(f"  env: {', '.join(env_keys) if env_keys else '(none)'}")
    lines.append(f"  mcp_servers: {', '.join(mcp_names) if mcp_names else '(none)'}")
    lines.append(
        f"  managed_processes: {', '.join(managed_names) if managed_names else '(none)'}"
    )
    lines.append("")
    lines.append(
        "The environment may be partially initialized. This is often caused by branch "
        "code depending on schema/config that prepare hasn't applied yet. Inspect the "
        "failure, fix the underlying issue as part of this spec, and rerun any missing "
        "bootstrap manually if needed. Verify gates remain strict."
    )
    return "\n".join(lines)


def _prepare_implement_launch_plan(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
    *,
    reason: str,
    use_stream_json: bool,
) -> ImplementLaunchPlan:
    setup_manifest = _run_implement_setup_command(run, worktree_path)
    backend = _resolve_execution_backend()
    adapter = get_agent_adapter(run.agent)
    setup_mcp_servers = None
    if adapter.capabilities.supports_mcp:
        merged_mcp_servers = _backend_mcp_servers_for_workspace(worktree_path)
        merged_mcp_servers.update(setup_manifest.mcp_servers)
        setup_mcp_servers = _worker_mcp_servers_for_container(
            merged_mcp_servers,
            worktree_path,
        )
    if _backend_uses_provider_sandbox_config(backend):
        _write_sandbox_config(
            run.agent,
            worktree_path,
            extra_mcp_servers=setup_mcp_servers,
        )
    elif run.agent == "claude":
        _write_claude_mcp_config(
            worktree_path,
            extra_mcp_servers=setup_mcp_servers,
        )

    agent_env = _build_implement_agent_env(run, worktree_path)
    agent_env_redactions: tuple[str, ...] = ()
    if setup_manifest.env:
        agent_env.update(setup_manifest.env)
        # Keep orchestrator-owned run identity stable for `spec report`.
        _apply_implement_run_identity(agent_env, run)

    if run.agent == "codex" and adapter.capabilities.supports_mcp:
        codex_isolated_servers = _compute_non_interactive_mcp_servers(
            worktree_path,
            agent_name="codex",
            setup_manifest_servers=setup_mcp_servers,
        )
        codex_home = _write_codex_isolated_home(
            worktree_path,
            mcp_servers=codex_isolated_servers,
            copy_auth=_codex_isolated_home_requires_auth_copy(backend),
        )
        agent_env = _subprocess_env_with_codex_home(agent_env, codex_home)
        # The isolated config.toml carries the full server set (defaults +
        # setup-manifest + [mcp].allow_from_user passthrough). Do NOT mirror
        # the user-passthrough subset into the `-c mcp_servers.*` argv
        # overrides: those servers commonly carry API tokens in `env`, and
        # argv is visible in process listings and container command logs.
        # Keeping the argv overrides scoped to the orchestrator-known
        # defaults+setup-manifest preserves the
        # `default_tools_approval_mode="approve"` force-set without exposing
        # user secrets via argv.
        _sync_orchestrator_paths_into_workspace(
            backend, worktree_path, (".spec-codex-home",),
        )
    elif run.agent == "claude" and adapter.capabilities.supports_mcp:
        if not _backend_uses_provider_sandbox_config(backend):
            claude_home = _write_claude_isolated_home(worktree_path)
            agent_env = _subprocess_env_with_home(agent_env, claude_home)
            agent_env_redactions = _inject_container_claude_auth_env(agent_env)
        _sync_orchestrator_paths_into_workspace(
            backend, worktree_path, (".spec-claude-home", ".claude/mcp-servers.json"),
        )

    _register_setup_manifest_processes(
        repo_root,
        worktree_path,
        setup_manifest,
    )
    if adapter.capabilities.supports_mcp:
        ctx.visual_feedback_available = bool(setup_mcp_servers)

    setup_prompt_value = setup_manifest.prompt or ""
    if setup_manifest.failure is not None:
        diagnostic_block = _build_setup_failure_prompt(setup_manifest)
        if setup_prompt_value:
            setup_prompt_value = f"{diagnostic_block}\n\n{setup_prompt_value}"
        else:
            setup_prompt_value = diagnostic_block
        _record_nonfatal_warning(
            run,
            phase="implement",
            failure_type="setup",
            failure_subtype="prepare_failed",
            summary=(
                f"Implement prepare step failed (launch_error={setup_manifest.failure.launch_error}): "
                f"{setup_manifest.failure.message}"
            )[:400],
            retryable=True,
        )

    ctx.save(repo_root, run.run_id)
    run.save(repo_root)
    operator_request = _load_operator_request(repo_root, run)

    agent_command_kwargs = {
        "retry_context": ctx if reason != "initial" else None,
        # Intake is durable implementation context even on an initial attempt;
        # retry_context intentionally remains None there because it also marks
        # no-handshake recovery launches throughout the existing interface.
        "intake": ctx.intake,
        "spec_id": run.spec_id,
        "spec_path": _spec_path_for_run(run),
        "spec_revision": run.spec_revision,
        "acceptance_checklist": ctx.acceptance_checklist,
        "verification_expectations": ctx.verification_expectations,
        "operator_request": operator_request,
        "input_question": run.input_question or None,
        "input_response": run.input_response or None,
        "setup_prompt": setup_prompt_value or None,
        "setup_mcp_prompt": setup_manifest.mcp_prompt or None,
        "prior_review_run_id": run.prior_review_run_id or None,
        "prior_review_summary": run.prior_review_summary or None,
        "prior_review_findings": list(run.prior_review_findings),
        "mcp_servers": (
            setup_mcp_servers
        ),
    }
    if use_stream_json:
        agent_command_kwargs["stream_json"] = True
    agent_cmd = _build_agent_command(
        run.agent,
        worktree_path,
        **agent_command_kwargs,
    )

    popen_kwargs: dict[str, object] = {
        "cwd": worktree_path,
        "env": agent_env,
        "text": True,
    }
    progress_tracker: AgentProgressTracker | None = None
    if run.agent == "claude" and use_stream_json:
        popen_kwargs["stdout"] = subprocess.PIPE
        progress_tracker = AgentProgressTracker(
            agent="claude",
            run_id=run.run_id,
            repo_root=repo_root,
        )
    elif run.agent == "claude":
        progress_tracker = AgentProgressTracker(
            agent="claude",
            run_id=run.run_id,
            repo_root=repo_root,
        )
    elif run.agent == "codex":
        popen_kwargs["stdout"] = subprocess.PIPE
        progress_tracker = AgentProgressTracker(
            agent="codex",
            run_id=run.run_id,
            repo_root=repo_root,
        )

    return ImplementLaunchPlan(
        use_stream_json=use_stream_json,
        agent_env=agent_env,
        agent_cmd=agent_cmd,
        popen_kwargs=popen_kwargs,
        progress_tracker=progress_tracker,
        agent_env_redactions=agent_env_redactions,
    )


def _launch_implement_attempt(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    plan: ImplementLaunchPlan,
    *,
    launch_number: int | None = None,
    process_name: str = "agent",
) -> int:
    expected_launch_number = (
        max(0, int(run.implement_launches))
        if launch_number is None
        else max(0, int(launch_number))
    )
    backend = _resolve_execution_backend()
    popen_extras = {
        key: value
        for key, value in plan.popen_kwargs.items()
        if key not in {"cwd", "env"}
    }
    request = AgentRequest(
        argv=plan.agent_cmd,
        cwd=worktree_path,
        env=plan.agent_env,
        popen_kwargs=popen_extras,
        redactions=plan.agent_env_redactions,
    )

    def _supervise(proc: subprocess.Popen) -> int:
        _set_active_agent_process(proc)
        _register_worktree_process_from_popen(
            repo_root,
            worktree_path,
            proc,
            name=process_name,
            kind="agent",
        )
        try:
            progress_thread = (
                _start_claude_progress_thread(proc, plan.progress_tracker)
                if run.agent == "claude" and plan.use_stream_json
                else _start_codex_progress_thread(proc, plan.progress_tracker)
                if run.agent == "codex"
                else None
            )
            return _wait_for_agent_exit(
                proc=proc,
                agent=run.agent,
                repo_root=repo_root,
                worktree_path=worktree_path,
                run_id=run.run_id,
                attempt=run.attempts,
                launch_number=expected_launch_number,
                progress_thread=progress_thread,
                progress_tracker=plan.progress_tracker,
                spec_id=run.spec_id,
            )
        finally:
            _set_active_agent_process(None)
            _prune_registered_worktree_processes(repo_root, worktree_path)

    result = backend.launch_agent(request, monitor=_supervise)
    return result.returncode


def _record_implement_attempt_outcome(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    *,
    head_before: str,
    exit_code: int,
    use_stream_json: bool,
    progress_tracker: AgentProgressTracker | None,
) -> ImplementAttemptOutcome:
    head_after = _head_sha(worktree_path) or ""
    dirty_files, has_staged_changes, has_unstaged_changes = _worktree_dirty_state(worktree_path)
    has_new_commit = bool(head_before and head_after and head_before != head_after)
    has_uncommitted_changes = bool(dirty_files)
    run.implement_head_sha_after = head_after
    run.implement_has_new_commit = has_new_commit
    run.implement_staged_changes = has_staged_changes
    run.implement_unstaged_changes = has_unstaged_changes
    run.implement_tree_status = _classify_implement_tree_status(
        has_new_commit=has_new_commit,
        has_staged_changes=has_staged_changes,
        has_unstaged_changes=has_unstaged_changes,
    )
    run.save(repo_root)
    return ImplementAttemptOutcome(
        exit_code=exit_code,
        head_before=head_before,
        head_after=head_after,
        has_new_commit=has_new_commit,
        has_uncommitted_changes=has_uncommitted_changes,
        use_stream_json=use_stream_json,
        progress_tracker=progress_tracker,
    )


def _detect_container_lost_implementation(
    run: RunState,
    worktree_path: Path,
    outcome: ImplementAttemptOutcome,
    impl_result: ImplementResult,
) -> str | None:
    """Detect a container run whose reported commits never reached the host.

    The container backend runs the agent against a copy of the repo inside the
    worker and synchronizes the result back to the host worktree. If that sync
    drops the agent's commits, the host worktree is left at the base ref while
    the agent's ``implement-result.json`` still reports ``passed`` and lists the
    commits it made — silently losing completed implementation work. This
    protects committed work during container workspace export.

    The detection is deliberately conservative: it fires only when the agent
    *claims* it produced commits (``impl_result.commits``) but the host worktree
    preserves none of that work (no non-merge commit above the base and a clean
    tree). A genuine no-op pass, or a pass whose work is present on the host,
    returns ``None``. Non-container backends are never flagged.
    """
    if not impl_result.commits:
        return None
    try:
        backend_name = _resolve_execution_backend().identity.backend
    except Exception:  # pragma: no cover - defensive: never block on detection
        return None
    if backend_name != "container":
        return None
    if outcome.has_uncommitted_changes or outcome.has_new_commit:
        return None

    head_after = outcome.head_after or ""
    base_ref = run.base_ref or BASE_REF
    if head_after:
        base_result = run_subprocess(
            ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
            cwd=worktree_path,
        )
        base_sha = base_result.stdout.strip() if base_result.returncode == 0 else ""
        if base_sha and _has_non_merge_commit_since(worktree_path, base_sha, head_after):
            return None

    claimed = ", ".join(impl_result.commits[:3])
    return (
        "Container backend reported implement success and claimed commits "
        f"({claimed}), but the host worktree preserves no implementation "
        f"(HEAD {_short_sha(head_after)} is at the base ref {base_ref} with a "
        "clean tree). The agent's commits were not synchronized back to the "
        "orchestrator, so the work would be lost. Failing as an orchestration "
        "error instead of recording a false success."
    )


def _interpret_implement_attempt_outcome(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
    outcome: ImplementAttemptOutcome,
    *,
    prefetched_result: tuple[ImplementResult | None, bool] | None = None,
    allow_legacy_launch: bool = False,
) -> str:
    if prefetched_result is None:
        impl_result, loaded_from_local = _load_matching_implement_result(
            repo_root=repo_root,
            worktree_path=worktree_path,
            run_id=run.run_id,
            attempt=run.attempts,
            launch_number=ctx.launch_number,
            spec_id=run.spec_id,
        )
    else:
        impl_result, loaded_from_local = prefetched_result
        if impl_result is not None and not _implement_result_matches_launch(
            impl_result,
            attempt=run.attempts,
            launch_number=ctx.launch_number,
            allow_legacy_launch=allow_legacy_launch,
        ):
            logger.warning(
                "Ignoring prefetched implement-result for run %s: "
                "result_attempt=%s current_attempt=%s result_launch=%s current_launch=%s",
                run.run_id,
                impl_result.attempt,
                run.attempts,
                impl_result.launch_number,
                ctx.launch_number,
            )
            impl_result, loaded_from_local = None, False
    if impl_result is not None and loaded_from_local:
        try:
            impl_result.save(repo_root, run.run_id)
        except OSError as exc:
            logger.warning(
                "Could not mirror worktree-local implement-result for %s: %s",
                run.run_id,
                exc,
            )

    if impl_result is not None:
        if impl_result.status == "passed":
            lost_work_error = _detect_container_lost_implementation(
                run,
                worktree_path,
                outcome,
                impl_result,
            )
            if lost_work_error is not None:
                run.last_error = lost_work_error
                run.save(repo_root)
                logger.error(
                    "Refusing to record implement success for %s: %s",
                    run.run_id,
                    lost_work_error,
                )
                return "failed"
            if _review_retry_history_rewritten(worktree_path, ctx, outcome.head_after):
                rewrite_error = (
                    "Review-triggered implement retry reported success but rewrote history: "
                    f"the reviewed head {_short_sha(ctx.reviewed_head_sha)} is no longer an ancestor of "
                    f"the new head {_short_sha(outcome.head_after)}. The retry must append commits on top "
                    "of the reviewed head, not recreate the branch from base. Failing the attempt."
                )
                run.last_error = rewrite_error
                run.save(repo_root)
                logger.error(
                    "Refusing to record implement success for %s: %s",
                    run.run_id,
                    rewrite_error,
                )
                return "failed"
            if _review_retry_missing_appended_commit(worktree_path, ctx, outcome.head_after):
                no_commit_error = (
                    "Review-triggered implement retry reported success but appended no commit: "
                    f"the new head {_short_sha(outcome.head_after)} carries no new non-merge commit on top of "
                    f"the reviewed head {_short_sha(ctx.reviewed_head_sha)}. A review retry must append the fix "
                    "as a commit on top of the reviewed head; leaving HEAD unchanged re-submits the "
                    "already-rejected head. Failing the attempt."
                )
                run.last_error = no_commit_error
                run.save(repo_root)
                logger.error(
                    "Refusing to record implement success for %s: %s",
                    run.run_id,
                    no_commit_error,
                )
                return "failed"
            return "passed"
        if impl_result.status == "blocked":
            summary = impl_result.summary or "Agent reported blocked"
            if (
                outcome.has_new_commit
                and not outcome.has_uncommitted_changes
                and is_non_actionable_gate_environment_block(summary)
            ):
                if not _review_retry_allows_inferred_success(worktree_path, ctx, outcome.head_after):
                    run.last_error = (
                        "Review-triggered implement retry produced a new clean HEAD and an environment-only block, "
                        "but the preserved evidence does not show a new non-merge commit on top of the triggering "
                        "reviewed head. A merge-only update or raw HEAD movement does not count as fresh review-fix "
                        "evidence."
                    )
                    return "failed"
                inferred = ImplementResult(
                    status="passed",
                    summary=(
                        "Agent reported blocked due to local environment constraints; "
                        "treating as passed because a clean commit was produced. "
                        f"Original note: {summary}"
                    ),
                    attempt=run.attempts,
                    launch_number=run.implement_launches,
                    result_source="orchestrator_inferred_environment_block",
                    completed_at=_now_iso(),
                )
                if impl_result.commits:
                    inferred.commits = impl_result.commits
                inferred.save(repo_root, run.run_id)
                logger.warning(
                    "Treating implement blocked as passed for %s due to sandbox/"
                    "environment gate limitation after clean commit.",
                    run.run_id,
                )
                return "passed"

            run.last_error = summary
            return "blocked"
        if impl_result.status == "needs-input":
            question = impl_result.summary or "Agent requires human input"
            request = OperatorRequest(
                kind="agent_question",
                prompt=question,
                context={
                    "spec_id": run.spec_id,
                    "run_id": run.run_id,
                },
                suggested_action="Clarify the ambiguity for the implement agent, then resume the run.",
                requires_full_session=_input_requires_full_session(question),
                status="pending",
                requested_by_phase="implement",
                requested_at=_now_iso(),
                request_attempt_number=_current_attempt_number(run),
            )
            request.save(repo_root, run.run_id)
            run.input_question = question
            run.input_response = ""
            run.status = "waiting-for-input"
            run.save(repo_root)
            return "waiting-for-input"

        run.last_error = impl_result.summary or "Agent reported failure"
        run.implement_agent_reported_failure = True
        return "failed"

    if outcome.exit_code == 0 and outcome.has_new_commit and not outcome.has_uncommitted_changes:
        if not _review_retry_allows_inferred_success(worktree_path, ctx, outcome.head_after):
            run.last_error = (
                "Review-triggered implement retry produced a new clean HEAD without an explicit completion handshake, "
                "but the preserved evidence does not show a new non-merge commit on top of the triggering reviewed "
                "head. A merge-only update or raw HEAD movement does not count as fresh review-fix evidence."
            )
            return "failed"
        inferred = ImplementResult(
            status="passed",
            summary=(
                "Agent exited without explicit completion handshake; inferred passed from a new clean commit."
            ),
            attempt=run.attempts,
            launch_number=run.implement_launches,
            result_source="orchestrator_inferred_clean_commit",
            completed_at=_now_iso(),
        )
        inferred.commits = _recent_commit_lines(worktree_path)
        inferred.save(repo_root, run.run_id)
        logger.warning(
            "No handshake for %s; inferred success from commit delta (%s -> %s).",
            run.run_id,
            outcome.head_before,
            outcome.head_after,
        )
        return "passed"

    completion_instruction = COMPLETE_HANDSHAKE_INSTRUCTION.replace("<spec-id>", run.spec_id)
    manual_completion_command = _manual_completion_helper_command(run)

    if outcome.has_uncommitted_changes:
        recovery_status = _attempt_no_handshake_recovery(
            run,
            repo_root=repo_root,
            worktree_path=worktree_path,
            ctx=ctx,
            use_stream_json=outcome.use_stream_json,
        )
        if recovery_status is not None:
            return recovery_status
        run.last_error = (
            "Agent exited without a completion handshake and left uncommitted changes. "
            "The orchestrator made one automatic recovery attempt in the same worktree, "
            f"but no handshake was recorded (no_handshake). Manual completion helper: "
            f"`{manual_completion_command}`"
        )
        return "failed"

    if outcome.progress_tracker is not None and outcome.progress_tracker.timeout_message:
        if outcome.has_new_commit and not outcome.has_uncommitted_changes:
            if not _review_retry_allows_inferred_success(worktree_path, ctx, outcome.head_after):
                run.last_error = (
                    "Review-triggered implement retry timed out after producing a new clean HEAD, but the preserved "
                    "evidence does not show a new non-merge commit on top of the triggering reviewed head. "
                    "The orchestrator is refusing to infer success from merge-only movement."
                )
                return "failed"
            inferred = ImplementResult(
                status="passed",
                summary=(
                    "Agent was terminated after inactivity without an explicit completion "
                    "handshake; inferred passed from a new clean commit. "
                    f"Timeout detail: {outcome.progress_tracker.timeout_message}"
                ),
                attempt=run.attempts,
                launch_number=run.implement_launches,
                result_source="orchestrator_inferred_timeout_clean_commit",
                completed_at=_now_iso(),
            )
            inferred.commits = _recent_commit_lines(worktree_path)
            inferred.save(repo_root, run.run_id)
            logger.warning(
                "Inferred success for %s after codex inactivity timeout because the "
                "worktree has a new clean commit.",
                run.run_id,
            )
            return "passed"
        run.last_error = outcome.progress_tracker.timeout_message
        return "failed"

    if outcome.progress_tracker is not None:
        _, last_event = outcome.progress_tracker.snapshot()
        if _is_agent_auth_failure_message(last_event):
            run.last_error = (
                f"Agent CLI is not authenticated in this execution environment "
                f"(no_handshake). Last agent output: {last_event}"
            )
            return "blocked"
        if _is_agent_capacity_failure_message(last_event):
            run.last_error = (
                "Agent provider capacity window is exhausted "
                f"(provider_capacity, no_handshake). Last agent output: {last_event}"
            )
            return "failed"

    run.last_error = (
        f"Agent exited with code {outcome.exit_code} (no_handshake). "
        f"{completion_instruction} Explicit helper: `{manual_completion_command}`"
    )
    return "failed"


def _reuse_current_attempt_implement_result_if_possible(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
) -> str | None:
    if not ctx.operator_request_response:
        return None

    impl_result, loaded_from_local = _load_matching_implement_result(
        repo_root=repo_root,
        worktree_path=worktree_path,
        run_id=run.run_id,
        attempt=run.attempts,
        launch_number=ctx.launch_number,
        spec_id=run.spec_id,
        allow_legacy_launch=True,
    )
    if impl_result is None or impl_result.status != "passed":
        return None

    dirty_files, has_staged_changes, has_unstaged_changes = _worktree_dirty_state(worktree_path)
    if dirty_files:
        return None

    current_head = _head_sha(worktree_path) or ""
    run.implement_head_sha_before = current_head
    run.implement_head_sha_after = current_head
    run.implement_has_new_commit = bool(
        current_head and ctx.reviewed_head_sha and current_head != ctx.reviewed_head_sha
    )
    run.implement_staged_changes = has_staged_changes
    run.implement_unstaged_changes = has_unstaged_changes
    run.implement_tree_status = _classify_implement_tree_status(
        has_new_commit=run.implement_has_new_commit,
        has_staged_changes=has_staged_changes,
        has_unstaged_changes=has_unstaged_changes,
    )
    ctx.current_head_sha = current_head
    ctx.save(repo_root, run.run_id)
    run.save(repo_root)
    logger.info(
        "Reusing current-attempt implement-result for %s without relaunching implement.",
        run.run_id,
    )
    outcome = ImplementAttemptOutcome(
        exit_code=0,
        head_before=current_head,
        head_after=current_head,
        has_new_commit=run.implement_has_new_commit,
        has_uncommitted_changes=False,
        use_stream_json=False,
        progress_tracker=None,
    )
    return _interpret_implement_attempt_outcome(
        run,
        repo_root,
        worktree_path,
        ctx,
        outcome,
        prefetched_result=(impl_result, loaded_from_local),
        allow_legacy_launch=True,
    )


def _validate_targeted_test_after_implement(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
) -> str:
    """Post-implement validation: run the targeted test to detect skipped/deselected false passes.

    When implement reports 'passed' and a first_failed_test_nodeid was in the
    retry context, run that single test in the worktree. If the test was skipped,
    deselected, or not collected, annotate the result so the next retry sees a
    warning. If the test actually runs and fails, treat implement as failed
    (skip the full verify cycle).
    """
    nodeid = ctx.first_failed_test_nodeid
    if not nodeid:
        return "passed"

    if not worktree_path.is_dir():
        return "passed"

    command = _test_gate_targeted_diagnostic_command(worktree_path, nodeid)
    try:
        with _with_verify_test_environment(worktree_path, repo_root) as gate_env:
            result = run_subprocess(
                command,
                cwd=worktree_path,
                env=gate_env,
                timeout=TARGETED_TEST_DIAGNOSTIC_TIMEOUT_SECONDS,
            )
    except (subprocess.TimeoutExpired, OSError, RuntimeError):
        # Cannot validate — proceed normally to verify.
        return "passed"

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = f"{stdout}\n{stderr}"

    # Pytest exit codes: 0=passed, 1=failed, 2=interrupted, 3=internal error,
    # 4=usage error, 5=no tests collected
    if result.returncode == 0:
        # Exit code 0 can still mean the test was skipped (pytest reports
        # "1 skipped" with exit 0).  Only treat as a genuine pass if the
        # output confirms at least one test actually *passed*.
        combined_lower = combined.lower()
        has_passed = "passed" in combined_lower or "1 passed" in combined_lower
        has_only_skipped = "skipped" in combined_lower and not has_passed
        if not has_only_skipped:
            return "passed"
        # Fall through to the skip/deselect handling below.

    if result.returncode == 1:
        # Test ran and failed — skip full verify, treat implement as failed.
        run.last_error = (
            f"Post-implement targeted test validation: {nodeid} still fails. "
            f"Skipping verify cycle."
        )
        logger.info(
            "Targeted test %s failed after implement passed for %s — treating as failed.",
            nodeid,
            run.run_id,
        )
        return "failed"

    # Exit code 5 = no tests collected; also check for skip/deselect patterns
    test_not_executed = (
        result.returncode == 5
        or "no tests ran" in combined.lower()
        or "deselected" in combined.lower()
        or ("skipped" in combined.lower() and "passed" not in combined.lower())
    )
    if test_not_executed:
        # Annotate the implement result so the retry package carries the
        # warning forward as advisory context.  Do NOT return "failed" — that
        # would trigger implement retries (via _is_retryable_implement_failure_message)
        # and livelock when the test keeps being skipped.  Instead, let the
        # workflow advance to verify; if verify fails the annotation surfaces
        # in the next retry context.
        impl_result = ImplementResult.load(repo_root, run.run_id)
        if impl_result is not None:
            impl_result.summary = (
                (impl_result.summary or "") + f" [targeted_test_not_executed: {nodeid}]"
            ).strip()
            impl_result.save(repo_root, run.run_id)
        logger.warning(
            "Targeted test %s was not executed for %s — annotating result.",
            nodeid,
            run.run_id,
        )
        return "passed"

    # Other exit codes (2, 3, 4) — cannot validate, proceed to verify.
    return "passed"


def phase_implement(run: RunState, repo_root: Path) -> str:
    """Write implement-context, launch agent, read implement-result on exit."""
    # First statement in the phase, ahead of every early return below, so a
    # prelaunch failure is never judged on the previous attempt's agent report.
    run.implement_agent_reported_failure = False
    workspace = _resolve_workspace_handle(run, repo_root)
    worktree_path = workspace.path
    # Clone/container backends materialize their checkout outside the legacy
    # ``.worktrees`` location recorded by older runs. Persist the authoritative
    # path before composing retry context: lineage checks in that composer run
    # Git in ``run.worktree_path`` and otherwise crash before the agent can
    # repair a merge conflict.
    if run.worktree_path != str(worktree_path):
        run.worktree_path = str(worktree_path)
        run.save(repo_root)
    _clear_stale_implement_context(repo_root, run.run_id)
    if not worktree_path.is_dir():
        run.last_error = f"Worktree missing: {worktree_path}"
        return "failed"

    intake_precondition = _ensure_required_intake_before_implement(run, repo_root)
    if intake_precondition != "passed":
        return intake_precondition

    branch_sync_status = _sync_reused_branch_before_implement(run, worktree_path)
    if branch_sync_status != "passed":
        return branch_sync_status

    attempt_bundle = _build_implement_attempt_context(run, repo_root)
    reason = attempt_bundle.reason
    ctx = attempt_bundle.context
    backend = _resolve_execution_backend()
    try:
        workspace = _restore_container_workspace_for_retry(workspace, backend, ctx)
    except ValueError as exc:
        run.last_error = str(exc)
        ctx.save(repo_root, run.run_id)
        run.save(repo_root)
        return "failed"
    worktree_path = workspace.path

    # The restore above may have force-reset the tree and rescued unpushed work
    # to a fresh snapshot — but the retry failure package (and thus ctx) was
    # composed before it ran. Re-read the latest rescue manifest so the prompt
    # handed to this attempt points at work rescued during *this* launch, not a
    # stale earlier snapshot (or nothing at all).
    if reason != "initial":
        refreshed_path, refreshed_summary = _rescue_snapshot_fields(run, repo_root)
        if refreshed_path or refreshed_summary:
            ctx.rescue_snapshot_path = refreshed_path
            ctx.rescue_snapshot_summary = refreshed_summary

    # For a container review retry, the restore above rolled the tree back to
    # the base commit. Move HEAD back to the reviewed implementation head before
    # head_before is read and the lineage guard runs, so review feedback applies on
    # top of the reviewed head instead of blocking on a misleading base-drift error.
    try:
        _position_review_retry_workspace_head(run, workspace, backend, ctx)
    except ValueError as exc:
        run.last_error = str(exc)
        ctx.save(repo_root, run.run_id)
        run.save(repo_root)
        return "failed"

    # A new implement attempt should only surface errors from this phase. Keep
    # prior failure details in the retry context, then clear the fatal channel.
    run.last_error = ""

    ctx.recent_commits = _recent_commit_lines(worktree_path)

    _clear_stale_implement_results(
        repo_root=repo_root,
        worktree_path=worktree_path,
        run_id=run.run_id,
        attempt=run.attempts,
    )
    reused_result = _reuse_current_attempt_implement_result_if_possible(
        run,
        repo_root,
        worktree_path,
        ctx,
    )
    if reused_result is not None:
        return reused_result

    # The reuse decision above is the last legitimate consumer of leftover
    # completion artifacts; anything still present would be replayed as this
    # attempt's handshake.
    _discard_prelaunch_completion_artifacts(repo_root, worktree_path, run.run_id)

    # Validate codex exec support before launch (fail-fast)
    if run.agent == "codex" and not _validate_codex_exec(run):
        return "failed"

    # Fail fast on expired host credentials before burning an attempt.
    if run.agent == "claude":
        credentials_error = _claude_credentials_preflight_error()
        if credentials_error:
            run.last_error = credentials_error
            return "failed"

    # Probe Claude CLI for stream-json support (graceful fallback)
    use_stream_json = run.agent == "claude" and _claude_supports_stream_json()
    logger.info(
        "Launching %s in %s (run_id=%s, attempt=%d)",
        run.agent,
        worktree_path,
        run.run_id,
        run.attempts + 1,
    )
    head_before = _head_sha(worktree_path)
    run.implement_head_sha_before = head_before or ""
    run.implement_head_sha_after = ""
    run.implement_has_new_commit = False
    run.implement_staged_changes = False
    run.implement_unstaged_changes = False
    run.implement_tree_status = ""
    ctx.current_head_sha = run.implement_head_sha_before
    lineage_error = _validate_review_retry_lineage(
        run,
        repo_root,
        worktree_path,
        ctx,
    )
    if lineage_error:
        run.last_error = lineage_error
        ctx.save(repo_root, run.run_id)
        run.save(repo_root)
        return "blocked"
    should_run_teardown = True

    try:
        try:
            # Reserve before launch-plan construction because that step
            # persists the context and may fail after setup. The durable
            # sequence prevents a resume from reusing this launch's files.
            ctx.launch_number = _reserve_implement_launch(run, repo_root)
            launch_plan = _prepare_implement_launch_plan(
                run,
                repo_root,
                worktree_path,
                ctx,
                reason=reason,
                use_stream_json=use_stream_json,
            )
            exit_code = _launch_implement_attempt(
                run,
                repo_root,
                worktree_path,
                launch_plan,
            )
            _consume_operator_steering(
                repo_root,
                run.run_id,
                attempt_number=ctx.attempt_number,
                expected_event_id=ctx.operator_steering_event_id,
            )
        except FileNotFoundError as exc:
            run.last_error = f"Agent binary not found: {exc}"
            return "failed"
        except ExecutionBackendImportError as exc:
            run.last_error = f"Container backend import failed after worker execution: {exc}"
            return "failed"
        except RuntimeError as exc:
            run.last_error = str(exc)
            return "failed"

        # The agent actually ran — now consume the guided retry (F2).
        if attempt_bundle.pending_guided_retry_signature:
            run.last_block_debugger_guided_retry_signature = attempt_bundle.pending_guided_retry_signature
            run.pending_block_debugger_signature = ""

        outcome = _record_implement_attempt_outcome(
            run,
            repo_root,
            worktree_path,
            head_before=head_before or "",
            exit_code=exit_code,
            use_stream_json=launch_plan.use_stream_json,
            progress_tracker=launch_plan.progress_tracker,
        )
        prefetched_result: tuple[ImplementResult | None, bool] | None = None
        if outcome.has_uncommitted_changes:
            # Preserve a worktree-local completion handshake before teardown
            # potentially removes it from disk.
            prefetched_result = _load_matching_implement_result(
                repo_root=repo_root,
                worktree_path=worktree_path,
                run_id=run.run_id,
                attempt=run.attempts,
                launch_number=ctx.launch_number,
                spec_id=run.spec_id,
            )
            if prefetched_result[0] is None:
                _run_implement_teardown_command(run, worktree_path)
                _prune_registered_worktree_processes(repo_root, worktree_path)
                should_run_teardown = False
        result = _interpret_implement_attempt_outcome(
            run,
            repo_root,
            worktree_path,
            ctx,
            outcome,
            prefetched_result=prefetched_result,
        )
        if result == "passed":
            result = _validate_targeted_test_after_implement(
                run, repo_root, worktree_path, ctx,
            )
        return result
    finally:
        if should_run_teardown:
            _run_implement_teardown_command(run, worktree_path)
            _prune_registered_worktree_processes(repo_root, worktree_path)


# Untracked test scratch that must never reach a branch even when the
# no-handshake recovery agent stages work with a broad ``git add``. These
# augment (never replace) the repo's own ``.gitignore``. ``pytest`` writes its
# tmpdir tree under a ``pytest-of-<user>`` root and this project stages e2e
# scratch under ``.tmp/``; without these exclusions, broad staging can sweep
# those artifacts into a recovery commit.
_RECOVERY_COMMIT_EXCLUDE_PATTERNS: tuple[str, ...] = (".tmp/", "pytest-of-*")


def _worktree_git_common_dir(worktree_path: Path) -> Path | None:
    """Resolve the shared git directory for *worktree_path* without spawning git.

    Handles both a plain checkout (``.git`` is a directory) and a linked
    worktree (``.git`` is a file pointing at ``…/worktrees/<name>`` whose
    ``commondir`` names the shared git dir). Returns ``None`` when there is no
    git checkout — the no-handshake recovery path forbids direct git
    subprocesses, so this stays pure-filesystem.
    """
    git_path = worktree_path / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    try:
        content = git_path.read_text(encoding="utf-8")
    except OSError:
        return None
    gitdir: Path | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("gitdir:"):
            candidate = Path(stripped[len("gitdir:"):].strip())
            gitdir = candidate if candidate.is_absolute() else (worktree_path / candidate).resolve()
            break
    if gitdir is None:
        return None
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        try:
            rel = commondir_file.read_text(encoding="utf-8").strip()
        except OSError:
            rel = ""
        if rel:
            common = Path(rel)
            return common if common.is_absolute() else (gitdir / common).resolve()
    return gitdir


def _seed_recovery_commit_excludes(
    worktree_path: Path,
    backend: ExecutionBackend | None = None,
) -> None:
    """Keep the no-handshake recovery commit free of untracked test scratch.

    Appends :data:`_RECOVERY_COMMIT_EXCLUDE_PATTERNS` to the worktree's
    ``info/exclude`` so ``git add`` skips those untracked paths without touching
    any tracked ``.gitignore``. Idempotent: patterns already present are not
    re-added. Best-effort and pure-filesystem — a failure here never blocks
    recovery and it must not spawn a git subprocess (the recovery path routes
    all git through the execution backend).

    On the container backend in volume mode the authoritative git dir lives
    inside the Docker volume, not the host ``source`` mirror this function
    writes; the host write alone never reaches the recovery agent's ``git add``.
    When a *backend* is provided the seeded ``info/exclude`` is synced into the
    workspace volume so the exclusion actually takes effect there. No-op for
    bind/worktree backends, where the host worktree already *is* the workspace.
    """
    common_dir = _worktree_git_common_dir(worktree_path)
    if common_dir is None:
        return
    exclude_path = common_dir / "info" / "exclude"

    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    except OSError:
        existing = ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [pat for pat in _RECOVERY_COMMIT_EXCLUDE_PATTERNS if pat not in present]

    if missing:
        block = ""
        if existing and not existing.endswith("\n"):
            block += "\n"
        block += "# specbutler: keep no-handshake recovery commits free of test scratch\n"
        block += "\n".join(missing) + "\n"
        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            with exclude_path.open("a", encoding="utf-8") as handle:
                handle.write(block)
        except OSError as exc:
            logger.warning(
                "Could not seed recovery-commit excludes in %s: %s", worktree_path, exc
            )
            return

    # Push the exclude into a container volume workspace (no-op elsewhere). Sync
    # unconditionally — even when the host mirror already carried the patterns,
    # a freshly seeded volume may not, and the recovery agent reads the volume.
    if backend is not None:
        try:
            rel = exclude_path.relative_to(worktree_path).as_posix()
        except ValueError:
            return
        _sync_orchestrator_paths_into_workspace(backend, worktree_path, (rel,))


def _attempt_no_handshake_recovery(
    run: RunState,
    *,
    repo_root: Path,
    worktree_path: Path,
    ctx: ImplementContext,
    use_stream_json: bool,
) -> str | None:
    if run.agent == "claude":
        credentials_error = _claude_credentials_preflight_error()
        if credentials_error:
            # A recovery session with a dead token 401s silently and wastes
            # the single recovery slot; let the caller surface the
            # no-handshake failure instead.
            logger.warning("Skipping no-handshake recovery: %s", credentials_error)
            return None
    recovery_launch_number = _reserve_implement_launch(run, repo_root)
    # Recovery is a distinct worker launch even though it shares the retry
    # attempt. Remove mutable aliases from the failed launch before starting
    # it; immutable attempt/launch histories remain available for diagnosis.
    _discard_prelaunch_completion_artifacts(repo_root, worktree_path, run.run_id)
    _seed_recovery_commit_excludes(worktree_path, _resolve_execution_backend())
    recovery_ctx = ImplementContext(
        implement_reason="recovery",
        objective=ctx.objective,
        recovery_objective=(
            "Do not re-implement the spec. Inspect the current worktree state, "
            f"commit any already-completed work, rerun {_format_verify_commands(_non_e2e_verify_commands())} only if "
            "that is still feasible, and report STATUS=ok|blocked|error|needs-input. If the existing "
            "changes are incomplete or unclear, report blocked/error with a concise reason. "
            "Run any commands in the FOREGROUND and wait for their output in-band — never as "
            "background tasks you 'wait to be notified about'; this session ends with your "
            "final message and is never re-invoked. When in doubt, skip the rerun, commit, "
            "and report: the orchestrator's verify phase re-runs every gate anyway."
        ),
        run_id=ctx.run_id,
        attempt_number=ctx.attempt_number,
        launch_number=recovery_launch_number,
        run_state_dir=ctx.run_state_dir,
        spec_path=ctx.spec_path,
        spec_revision=ctx.spec_revision,
        acceptance_checklist=list(ctx.acceptance_checklist),
        verification_expectations=list(ctx.verification_expectations),
        recent_commits=list(ctx.recent_commits),
        failure_summary=(
            "Automatic handshake recovery: the prior implement process exited without "
            "recording completion and left uncommitted changes in the worktree."
        ),
        intake=dict(ctx.intake),
    )
    try:
        recovery_env = _build_implement_agent_env(run, worktree_path)
        recovery_env_redactions: tuple[str, ...] = ()
        setup_manifest = _run_implement_setup_command(run, worktree_path)
        backend = _resolve_execution_backend()
        adapter = get_agent_adapter(run.agent)
        setup_mcp_servers = None
        if adapter.capabilities.supports_mcp:
            merged_mcp_servers = _backend_mcp_servers_for_workspace(worktree_path)
            merged_mcp_servers.update(setup_manifest.mcp_servers)
            setup_mcp_servers = _worker_mcp_servers_for_container(
                merged_mcp_servers,
                worktree_path,
            )
        if _backend_uses_provider_sandbox_config(backend):
            _write_sandbox_config(
                run.agent,
                worktree_path,
                extra_mcp_servers=setup_mcp_servers,
            )
        elif run.agent == "claude":
            _write_claude_mcp_config(
                worktree_path,
                extra_mcp_servers=setup_mcp_servers,
            )
        if setup_manifest.env:
            recovery_env.update(setup_manifest.env)
            # Keep orchestrator-owned run identity stable for `spec report`.
            _apply_implement_run_identity(recovery_env, run)
        if run.agent == "codex" and adapter.capabilities.supports_mcp:
            codex_isolated_servers = _compute_non_interactive_mcp_servers(
                worktree_path,
                agent_name="codex",
                setup_manifest_servers=setup_mcp_servers,
            )
            codex_home = _write_codex_isolated_home(
                worktree_path,
                mcp_servers=codex_isolated_servers,
                copy_auth=_codex_isolated_home_requires_auth_copy(backend),
            )
            recovery_env = _subprocess_env_with_codex_home(recovery_env, codex_home)
            # See the matching note in the initial implement path: the
            # user-passthrough servers must not flow into the Codex `-c`
            # argv overrides because their `env` may contain API tokens.
            # The isolated config.toml is the sole transport for those
            # servers; argv stays scoped to defaults+setup-manifest.
            _sync_orchestrator_paths_into_workspace(
                backend, worktree_path, (".spec-codex-home",),
            )
        elif run.agent == "claude" and adapter.capabilities.supports_mcp:
            if not _backend_uses_provider_sandbox_config(backend):
                claude_home = _write_claude_isolated_home(worktree_path)
                recovery_env = _subprocess_env_with_home(recovery_env, claude_home)
                recovery_env_redactions = _inject_container_claude_auth_env(
                    recovery_env
                )
            _sync_orchestrator_paths_into_workspace(
                backend, worktree_path, (".spec-claude-home", ".claude/mcp-servers.json"),
            )
        _register_setup_manifest_processes(
            repo_root,
            worktree_path,
            setup_manifest,
        )
        if adapter.capabilities.supports_mcp:
            recovery_ctx.visual_feedback_available = bool(setup_mcp_servers)

        recovery_setup_prompt = setup_manifest.prompt or ""
        if setup_manifest.failure is not None:
            diagnostic_block = _build_setup_failure_prompt(setup_manifest)
            if recovery_setup_prompt:
                recovery_setup_prompt = f"{diagnostic_block}\n\n{recovery_setup_prompt}"
            else:
                recovery_setup_prompt = diagnostic_block
            _record_nonfatal_warning(
                run,
                phase="implement",
                failure_type="setup",
                failure_subtype="prepare_failed",
                summary=(
                    f"Implement prepare step failed (launch_error={setup_manifest.failure.launch_error}): "
                    f"{setup_manifest.failure.message}"
                )[:400],
                retryable=True,
            )

        recovery_ctx.save(repo_root, run.run_id)
        run.save(repo_root)
        operator_request = _load_operator_request(repo_root, run)

        recovery_cmd_kwargs = {
            "retry_context": recovery_ctx,
            "spec_id": run.spec_id,
            "spec_path": _spec_path_for_run(run),
            "spec_revision": run.spec_revision,
            "acceptance_checklist": recovery_ctx.acceptance_checklist,
            "verification_expectations": recovery_ctx.verification_expectations,
            "operator_request": operator_request,
            "input_question": run.input_question or None,
            "input_response": run.input_response or None,
            "setup_prompt": recovery_setup_prompt or None,
            "setup_mcp_prompt": setup_manifest.mcp_prompt or None,
            "mcp_servers": setup_mcp_servers,
        }
        if use_stream_json:
            recovery_cmd_kwargs["stream_json"] = True
        recovery_cmd = _build_agent_command(
            run.agent,
            worktree_path,
            **recovery_cmd_kwargs,
        )

        progress_tracker: AgentProgressTracker | None = None
        popen_kwargs: dict[str, object] = {
            "cwd": worktree_path,
            "env": recovery_env,
            "text": True,
        }
        if run.agent == "claude" and use_stream_json:
            popen_kwargs["stdout"] = subprocess.PIPE
            progress_tracker = AgentProgressTracker(
                agent="claude",
                run_id=run.run_id,
                repo_root=repo_root,
            )
        elif run.agent == "claude":
            progress_tracker = AgentProgressTracker(
                agent="claude",
                run_id=run.run_id,
                repo_root=repo_root,
            )
        elif run.agent == "codex":
            popen_kwargs["stdout"] = subprocess.PIPE
            progress_tracker = AgentProgressTracker(
                agent="codex",
                run_id=run.run_id,
                repo_root=repo_root,
            )

        recovery_plan = ImplementLaunchPlan(
            use_stream_json=use_stream_json,
            agent_env=recovery_env,
            agent_cmd=recovery_cmd,
            popen_kwargs=popen_kwargs,
            progress_tracker=progress_tracker,
            agent_env_redactions=recovery_env_redactions,
        )
        try:
            _launch_implement_attempt(
                run,
                repo_root,
                worktree_path,
                recovery_plan,
                process_name="agent-recovery",
            )
        except FileNotFoundError as exc:
            run.last_error = f"Agent binary not found during handshake recovery: {exc}"
            return "failed"
        except RuntimeError as exc:
            run.last_error = str(exc)
            return "failed"

        impl_result, loaded_from_local = _load_matching_implement_result(
            repo_root=repo_root,
            worktree_path=worktree_path,
            run_id=run.run_id,
            attempt=run.attempts,
            launch_number=recovery_launch_number,
            spec_id=run.spec_id,
        )
        if impl_result is not None and loaded_from_local:
            try:
                impl_result.save(repo_root, run.run_id)
            except OSError as exc:
                logger.warning(
                    "Could not mirror worktree-local implement-result after recovery for %s: %s",
                    run.run_id,
                    exc,
                )

        if impl_result is None:
            return None
        if impl_result.status == "passed":
            return "passed"
        if impl_result.status == "blocked":
            run.last_error = impl_result.summary or "Agent reported blocked during handshake recovery"
            return "blocked"

        run.last_error = impl_result.summary or "Agent reported failure during handshake recovery"
        return "failed"
    finally:
        _run_implement_teardown_command(run, worktree_path)
        _prune_registered_worktree_processes(repo_root, worktree_path)


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Create a short summary of a tool call for progress display."""
    if tool_name in ("Read", "Edit", "Write"):
        path = redact_sensitive(str(tool_input.get("file_path", "")))
        parts = path.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) >= 2 else path
    if tool_name == "Bash":
        desc = redact_sensitive(str(tool_input.get("description", "")))
        if desc:
            return desc[:100]
        cmd = redact_sensitive(str(tool_input.get("command", "")))
        return cmd[:100] + ("..." if len(cmd) > 100 else "")
    if tool_name == "Grep":
        pattern = redact_sensitive(str(tool_input.get("pattern", "")))
        path = redact_sensitive(str(tool_input.get("path", "")))
        return f'"{pattern}" in {path or "."}'
    if tool_name == "Glob":
        return redact_sensitive(str(tool_input.get("pattern", "")))
    if tool_name == "Agent":
        return redact_sensitive(str(tool_input.get("description", "")))[:80]
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos", [])
        in_prog = [t for t in todos if t.get("status") == "in_progress"]
        if in_prog:
            return redact_sensitive(str(in_prog[0].get("activeForm", f"{len(todos)} items")))
        return f"{len(todos)} items"
    for _k, v in tool_input.items():
        return redact_sensitive(str(v))[:80]
    return ""


def _attempt_matches_current_result(result: ImplementResult, attempt: int) -> bool:
    """Return True when an implement result belongs to the current attempt."""
    return result.attempt == attempt if result.attempt is not None else attempt == 0


def _implement_result_matches_launch(
    result: ImplementResult,
    *,
    attempt: int,
    launch_number: int | None,
    allow_legacy_launch: bool = False,
) -> bool:
    """Return whether ``result`` is valid for one exact worker launch.

    ``launch_number=None`` retains attempt-only lookup for historical callers
    that are not supervising a live worker. Active launch, prefetch, and
    interpretation paths always provide a number. Legacy launch zero is only
    accepted explicitly by the pre-launch operator-result reuse path, where no
    new worker is started and the clean-tree/result checks still apply.
    """
    if not _attempt_matches_current_result(result, attempt):
        return False
    if launch_number is None:
        return True
    expected_launch = max(0, int(launch_number))
    result_launch = max(0, int(result.launch_number))
    return result_launch == expected_launch or (
        allow_legacy_launch and result_launch == 0
    )


def _clear_stale_implement_result(
    state_root: Path,
    run_id: str,
    attempt: int,
    *,
    source_label: str,
) -> None:
    """Delete an older-attempt implement result so polling does not keep reloading it."""
    impl_result = ImplementResult.load_from_state_root(state_root, run_id)
    if impl_result is None or _attempt_matches_current_result(impl_result, attempt):
        return

    result_path = state_root / "runs" / run_id / "implement-result.json"
    try:
        result_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not remove stale %simplement-result for run %s: result_attempt=%s current_attempt=%s (%s)",
            source_label,
            run_id,
            impl_result.attempt,
            attempt,
            exc,
        )


def _discard_prelaunch_completion_artifacts(
    repo_root: Path,
    worktree_path: Path,
    run_id: str,
) -> None:
    """Delete mutable completion aliases that predate a fresh agent launch.

    Operator resumes and handshake recovery can launch multiple workers at the
    same attempt number. Therefore both container outboxes and directly-written
    worktree ``implement-result.json`` aliases are stale at this boundary. The
    immutable ``attempt-*`` and ``launch-*`` histories are intentionally left
    untouched for diagnostics.
    """
    outbox_path = _container_completion_outbox_path(worktree_path)
    removed: list[str] = []
    if outbox_path is not None and outbox_path.is_file():
        try:
            outbox_path.unlink()
            removed.append(str(outbox_path))
        except OSError as exc:
            logger.warning("Could not discard stale outbox completion report %s: %s", outbox_path, exc)
    for state_root in (_state_root(repo_root), _worktree_state_root(worktree_path)):
        result_path = state_root / "runs" / run_id / "implement-result.json"
        if result_path.is_file():
            try:
                result_path.unlink()
                removed.append(str(result_path))
            except OSError as exc:
                logger.warning("Could not discard stale implement result %s: %s", result_path, exc)
    if removed:
        logger.warning(
            "Discarded pre-launch completion artifacts for run %s (stale-handshake guard): %s",
            run_id,
            ", ".join(removed),
        )


def _clear_stale_implement_results(
    repo_root: Path,
    worktree_path: Path,
    run_id: str,
    attempt: int,
) -> None:
    """Remove prior-attempt implement results before launching a fresh attempt."""
    _clear_stale_implement_result(
        _state_root(repo_root),
        run_id,
        attempt,
        source_label="",
    )
    _clear_stale_implement_result(
        _worktree_state_root(worktree_path),
        run_id,
        attempt,
        source_label="worktree-local ",
    )


_stale_result_warned: set[tuple[str, int | None, int, int, int | None]] = set()


def _load_matching_implement_result(
    repo_root: Path,
    worktree_path: Path,
    run_id: str,
    attempt: int,
    launch_number: int | None = None,
    spec_id: str | None = None,
    *,
    allow_legacy_launch: bool = False,
) -> tuple[ImplementResult | None, bool]:
    """Load a matching result from common, outbox, or worktree-local state."""
    impl_result = ImplementResult.load(repo_root, run_id)
    if impl_result is not None:
        if _implement_result_matches_launch(
            impl_result,
            attempt=attempt,
            launch_number=launch_number,
            allow_legacy_launch=allow_legacy_launch,
        ):
            return impl_result, False
        key = (run_id, impl_result.attempt, attempt, impl_result.launch_number, launch_number)
        if key not in _stale_result_warned:
            _stale_result_warned.add(key)
            result_path = _state_root(repo_root) / "runs" / run_id / "implement-result.json"
            try:
                mtime = datetime.fromtimestamp(result_path.stat().st_mtime).isoformat()
            except OSError:
                mtime = "unknown"
            logger.warning(
                "Ignoring stale implement-result for run %s: result_attempt=%s "
                "current_attempt=%s result_launch=%s current_launch=%s "
                "(file_mtime=%s completed_at=%s)",
                run_id,
                impl_result.attempt,
                attempt,
                impl_result.launch_number,
                launch_number,
                mtime,
                impl_result.completed_at,
            )

    container_outbox_path = _container_completion_outbox_path(worktree_path)
    if container_outbox_path is not None:
        outbox_result = _load_container_outbox_completion_result(
            worktree_path, run_id, spec_id=spec_id
        )
        if outbox_result is not None:
            if _implement_result_matches_launch(
                outbox_result,
                attempt=attempt,
                launch_number=launch_number,
                allow_legacy_launch=allow_legacy_launch,
            ):
                try:
                    outbox_result.save(repo_root, run_id)
                except OSError as exc:
                    logger.warning(
                        "Could not import container outbox implement-result for %s: %s",
                        run_id,
                        exc,
                    )
                return outbox_result, False
            key = (
                run_id,
                outbox_result.attempt,
                attempt,
                outbox_result.launch_number,
                launch_number,
            )
            if key not in _stale_result_warned:
                _stale_result_warned.add(key)
                logger.warning(
                    "Ignoring stale container outbox implement-result for run %s: "
                    "result_attempt=%s current_attempt=%s result_launch=%s "
                    "current_launch=%s completed_at=%s",
                    run_id,
                    outbox_result.attempt,
                    attempt,
                    outbox_result.launch_number,
                    launch_number,
                    outbox_result.completed_at,
                )
        return None, False

    local_result = ImplementResult.load_from_state_root(
        _worktree_state_root(worktree_path),
        run_id,
    )
    if local_result is not None:
        if _implement_result_matches_launch(
            local_result,
            attempt=attempt,
            launch_number=launch_number,
            allow_legacy_launch=allow_legacy_launch,
        ):
            logger.info("Loaded implement-result from worktree-local state for %s", run_id)
            return local_result, True
        key = (run_id, local_result.attempt, attempt, local_result.launch_number, launch_number)
        if key not in _stale_result_warned:
            _stale_result_warned.add(key)
            local_path = _worktree_state_root(worktree_path) / "runs" / run_id / "implement-result.json"
            try:
                mtime = datetime.fromtimestamp(local_path.stat().st_mtime).isoformat()
            except OSError:
                mtime = "unknown"
            logger.warning(
                "Ignoring stale worktree-local implement-result for run %s: result_attempt=%s "
                "current_attempt=%s result_launch=%s current_launch=%s "
                "(file_mtime=%s completed_at=%s)",
                run_id,
                local_result.attempt,
                attempt,
                local_result.launch_number,
                launch_number,
                mtime,
                local_result.completed_at,
            )

    return None, False


def _container_completion_outbox_path(worktree_path: Path) -> Path | None:
    run_root = worktree_path.resolve().parent
    container_state = run_root / "backend-state" / "container-backend-state.json"
    if (
        worktree_path.name != "source"
        or not (run_root / "logs").is_dir()
        or not container_state.is_file()
    ):
        return None
    return run_root / "outbox" / "completion-report.json"


def _load_container_outbox_completion_result(
    worktree_path: Path,
    run_id: str,
    spec_id: str | None = None,
) -> ImplementResult | None:
    result_path = _container_completion_outbox_path(worktree_path)
    if result_path is None or not result_path.is_file():
        return None
    try:
        payload = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return None
    if spec_id is not None and payload.get("spec_id") != spec_id:
        # A report whose run_id matches but spec_id does not is not a valid
        # handshake. A test exercising `spec report` under inherited
        # SPEC_RUN_ID/SPEC_COMPLETION_OUTBOX env can write a fixture spec id
        # into the live outbox.
        logger.warning(
            "Ignoring container outbox completion report for run %s: spec_id "
            "%r does not match expected %r.",
            run_id,
            payload.get("spec_id"),
            spec_id,
        )
        return None
    result_payload = payload.get("implement_result")
    if not isinstance(result_payload, dict):
        return None
    allowed = {field.name for field in fields(ImplementResult)}
    try:
        return ImplementResult(
            **{key: value for key, value in result_payload.items() if key in allowed}
        )
    except TypeError:
        return None


def _handle_claude_stream_line(
    line: str,
    tracker: AgentProgressTracker | None = None,
) -> None:
    """Parse and print one Claude stream-json line."""
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        if tracker is not None:
            tracker.heartbeat()
        return

    if not isinstance(event, dict):
        if tracker is not None:
            tracker.heartbeat()
        return

    event_type = event.get("type")
    if tracker is not None:
        _track_claude_inflight_tools(event, tracker)
    if event_type == "assistant":
        content = _claude_message_content(event)
        if not content and tracker is not None:
            tracker.heartbeat()
        for item in content:
            item_type = item.get("type")
            if item_type == "tool_use":
                name = item.get("name", "?")
                summary = _summarize_tool_input(name, item.get("input", {}))
                if tracker is not None:
                    tracker.record(f"> {name}: {summary}")
                print(f"  > {name}: {summary}", file=sys.stderr, flush=True)
            elif item_type == "text":
                text = redact_sensitive(str(item.get("text", "")).strip())
                if text:
                    first_line = text.split("\n")[0][:120]
                    if tracker is not None:
                        tracker.record(first_line)
                    print(f"  {first_line}", file=sys.stderr, flush=True)
                elif tracker is not None:
                    tracker.heartbeat()
            elif tracker is not None:
                tracker.heartbeat()
    elif event_type == "result":
        cost = event.get("total_cost_usd")
        duration_ms = event.get("duration_ms")
        num_turns = event.get("num_turns")
        parts = ["  Done"]
        if num_turns is not None:
            parts.append(f"{num_turns} turns")
        if duration_ms is not None:
            parts.append(f"{duration_ms / 1000:.0f}s")
        if cost is not None:
            parts.append(f"${cost:.2f}")
        if tracker is not None:
            tracker.record("session complete")
        print(" | ".join(parts), file=sys.stderr, flush=True)
    elif tracker is not None:
        tracker.heartbeat()


def _claude_message_content(event: dict) -> list[dict]:
    """Return well-formed Claude message blocks from a stream-json event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _claude_tool_key(value: object) -> str:
    """Return the protocol ID used to pair Claude tool_use/tool_result blocks."""
    tool_id = str(value or "").strip()
    return f"claude-tool:{tool_id}" if tool_id else ""


def _track_claude_inflight_tools(event: dict, tracker: AgentProgressTracker) -> None:
    """Track blocking Claude tools so stream silence uses the tool ceiling.

    Claude's stream-json protocol emits ``tool_use`` blocks in assistant
    messages and matching ``tool_result`` blocks in user messages. More than
    one tool can be outstanding (including forwarded subagent activity), so
    IDs are tracked independently rather than as a single boolean.
    """
    if str(event.get("type", "")).strip() == "result":
        # A terminal session result proves that no tool remains in flight and
        # repairs state if a malformed or omitted tool_result was encountered.
        tracker.clear_inflight_commands()
        return
    for item in _claude_message_content(event):
        item_type = str(item.get("type", "")).strip()
        if item_type == "tool_use":
            tool_key = _claude_tool_key(item.get("id"))
            if tool_key:
                tracker.command_started(tool_key)
        elif item_type == "tool_result":
            tool_key = _claude_tool_key(item.get("tool_use_id"))
            if tool_key:
                tracker.command_finished(tool_key)


def _stream_claude_progress(
    stream: Iterator[str],
    tracker: AgentProgressTracker | None = None,
) -> None:
    """Read Claude stream-json output and print concise progress lines."""
    for line in stream:
        _handle_claude_stream_line(line, tracker)


def _summarize_codex_item(item: dict) -> str | None:
    """Return a short summary for a Codex item dict, or None to suppress."""
    item_type = str(item.get("type", "")).strip()
    if item_type == "command_execution":
        cmd = redact_sensitive(str(item.get("command", "")).strip())
        return f"> shell: {cmd[:100]}" if cmd else "> shell"
    if item_type == "agent_message":
        text = redact_sensitive(str(item.get("text", "")).strip())
        if text:
            first_line = text.split("\n", 1)[0]
            return first_line[:120]
        return None
    if item_type == "mcp_tool_call":
        server = str(item.get("server", "")).strip()
        tool = str(item.get("tool", "")).strip()
        return f"> mcp: {server}/{tool}"
    if item_type == "file_change":
        return "> file_change"
    if item_type == "reasoning":
        return None
    if item_type == "error":
        text = redact_sensitive(str(item.get("text", "")).strip())
        return text or item_type
    # Fallback for unknown item types
    return item_type or None


def _summarize_codex_event(event: dict) -> str | None:
    """Return a short human-readable summary for a Codex JSON event, or None to suppress."""
    event_type = str(event.get("type", "event")).strip() or "event"
    if event_type == "error":
        return str(event.get("message", "")).strip() or event_type
    if event_type in ("item.completed", "item.updated"):
        item = event.get("item") or {}
        return _summarize_codex_item(item)
    if event_type == "item.started":
        return None
    if event_type in ("turn.started", "turn.completed", "thread.started"):
        return None
    # Legacy response_item format (keep for backward compat)
    if event_type == "response_item":
        payload = event.get("payload") or {}
        payload_type = str(payload.get("type", "payload")).strip() or "payload"
        if payload_type == "function_call":
            return f"response_item: tool {payload.get('name', '?')}"
        return f"response_item: {payload_type}"
    return event_type


def _agent_idle_timeout(
    agent: str,
    tracker: AgentProgressTracker,
    *,
    has_progress_thread: bool,
) -> float | None:
    """Idle ceiling for ``agent``, or None when inactivity must not be enforced."""
    if agent == "codex":
        idle_timeout = CODEX_IDLE_TIMEOUT_SECONDS
    elif agent == "claude" and has_progress_thread:
        # Only when a stream thread feeds the tracker; without one the tracker
        # never advances and any timeout would always fire.
        idle_timeout = CLAUDE_IDLE_TIMEOUT_SECONDS
    else:
        return None
    if tracker.has_inflight_command():
        # A shell command or MCP call is still running: the stream is silent by
        # design, so hold the agent to the command ceiling instead of the
        # between-events idle timeout.
        return max(idle_timeout, AGENT_COMMAND_IDLE_TIMEOUT_SECONDS)
    return idle_timeout


def _codex_item_key(item: dict) -> str:
    """Stable key pairing an item's started event with its completion."""
    item_id = str(item.get("id", "")).strip()
    return item_id or f"type:{str(item.get('type', '')).strip()}"


def _track_codex_inflight_items(event: dict, tracker: AgentProgressTracker) -> None:
    """Record which blocking items are running so the watchdog can allow for them."""
    event_type = str(event.get("type", "")).strip()
    if event_type == "turn.completed":
        # The agent cannot be blocked on a command while completing a turn;
        # this also recovers if a completion event was missed or re-keyed.
        tracker.clear_inflight_commands()
        return
    item = event.get("item")
    if not isinstance(item, dict):
        return
    if str(item.get("type", "")).strip() not in CODEX_LONG_RUNNING_ITEM_TYPES:
        return
    if event_type == "item.started":
        tracker.command_started(_codex_item_key(item))
    elif event_type in ("item.completed", "item.failed"):
        tracker.command_finished(_codex_item_key(item))


def _handle_codex_stream_line(line: str, tracker: AgentProgressTracker) -> None:
    """Parse one Codex JSON event line and update inactivity tracking."""
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        tracker.record(line)
        return

    _track_codex_inflight_items(event, tracker)
    summary = _summarize_codex_event(event)
    if summary is None:
        tracker.heartbeat()
        return
    clean_summary = tracker.record(summary)
    print(f"  Codex: {clean_summary}", file=sys.stderr, flush=True)


def _stream_codex_progress(
    stream: Iterator[str],
    tracker: AgentProgressTracker,
) -> None:
    """Read Codex JSONL progress output and keep the watchdog fresh."""
    for line in stream:
        _handle_codex_stream_line(line, tracker)


def _start_claude_progress_thread(
    proc: subprocess.Popen[str],
    tracker: AgentProgressTracker | None = None,
) -> threading.Thread | None:
    """Start a background reader for Claude stream-json progress."""
    if proc.stdout is None:
        return None
    thread = threading.Thread(
        target=_stream_claude_progress,
        args=(proc.stdout, tracker),
        name=f"claude-progress-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    thread.start()
    return thread


def _start_codex_progress_thread(
    proc: subprocess.Popen[str],
    tracker: AgentProgressTracker | None,
) -> threading.Thread | None:
    """Start a background reader for Codex exec JSON progress."""
    if proc.stdout is None or tracker is None:
        return None
    thread = threading.Thread(
        target=_stream_codex_progress,
        args=(proc.stdout, tracker),
        name=f"codex-progress-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    thread.start()
    return thread


def _terminate_agent_process(proc: subprocess.Popen[str] | ManagedProcess) -> None:
    """Terminate an owned agent tree, escalating after a bounded wait."""
    pid = getattr(proc, "pid", None)
    if isinstance(proc, ManagedProcess):
        try:
            proc.terminate(grace_seconds=AGENT_TERMINATE_TIMEOUT_SECONDS)
        except (OSError, ProcessLookupError, PermissionError):
            pass

        try:
            proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            proc.kill()
        except (OSError, ProcessLookupError, PermissionError):
            pass

        try:
            proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error("Agent process %s did not terminate after hard termination", pid)
        return

    try:
        terminated = terminate_legacy_popen_tree(
            proc,
            grace_seconds=AGENT_TERMINATE_TIMEOUT_SECONDS,
        )
    except TypeError:
        # Bounded compatibility for lightweight Popen-like test doubles. Real
        # custom-backend processes must prove an owned boundary above.
        terminated = None
    if terminated is not None:
        if not terminated:
            logger.error("Refusing to terminate unowned legacy agent process %s", pid)
            return
        try:
            proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error("Legacy agent process %s remained after tree termination", pid)
        return

    try:
        proc.terminate()
    except (OSError, ProcessLookupError, PermissionError):
        pass

    try:
        proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        proc.kill()
    except (OSError, ProcessLookupError, PermissionError):
        pass

    try:
        proc.wait(timeout=AGENT_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("Agent process %s did not terminate after hard termination", pid)


def _wait_for_agent_exit(
    proc: subprocess.Popen[str],
    agent: str,
    repo_root: Path,
    worktree_path: Path,
    run_id: str,
    attempt: int,
    launch_number: int | None = None,
    progress_thread: threading.Thread | None = None,
    progress_tracker: AgentProgressTracker | None = None,
    spec_id: str | None = None,
) -> int:
    """Wait for agent exit, but stop waiting once completion is recorded."""
    completion_seen_at: float | None = None

    while True:
        impl_result, _ = _load_matching_implement_result(
            repo_root=repo_root,
            worktree_path=worktree_path,
            run_id=run_id,
            attempt=attempt,
            launch_number=launch_number,
            spec_id=spec_id,
        )
        if impl_result is not None and completion_seen_at is None:
            completion_seen_at = time.monotonic()
            logger.info(
                "Detected completion handshake for %s (%s); allowing %.1fs for clean exit.",
                run_id,
                agent,
                AGENT_EXIT_GRACE_SECONDS,
            )

        exit_code = proc.poll()
        if exit_code is not None:
            if progress_thread is not None:
                progress_thread.join(timeout=1.0)
            return exit_code

        if completion_seen_at is not None:
            elapsed = time.monotonic() - completion_seen_at
            if elapsed >= AGENT_EXIT_GRACE_SECONDS:
                logger.warning(
                    "Agent process for %s did not exit after completion handshake; terminating it.",
                    run_id,
                )
                _terminate_agent_process(proc)
                if progress_thread is not None:
                    progress_thread.join(timeout=1.0)
                return proc.returncode if proc.returncode is not None else 1

        if completion_seen_at is None and progress_tracker is not None:
            idle_timeout = _agent_idle_timeout(
                agent,
                progress_tracker,
                has_progress_thread=progress_thread is not None,
            )
            if idle_timeout is not None:
                idle_for, last_event = progress_tracker.snapshot()
                if idle_for >= idle_timeout:
                    timeout_message = progress_tracker.mark_timeout(
                        idle_timeout,
                    )
                    logger.warning(
                        "Terminating inactive %s implement process for %s after %.1fs (last progress: %s)",
                        agent,
                        run_id,
                        idle_for,
                        last_event,
                    )
                    logger.warning("%s", timeout_message)
                    _terminate_agent_process(proc)
                    if progress_thread is not None:
                        progress_thread.join(timeout=1.0)
                    return proc.returncode if proc.returncode is not None else 1
            elif progress_thread is None:
                # No stream thread feeding the tracker (e.g. Claude without
                # stream-json); persist heartbeats from the wait loop so the
                # run is not misclassified as stale by autopilot-watch/gc.
                progress_tracker.heartbeat()

        _poll_sleep(AGENT_COMPLETION_POLL_SECONDS)


def _manual_completion_helper_command(run: RunState) -> str:
    return (
        f"spec report --spec {shlex.quote(run.spec_id)} --run {shlex.quote(run.run_id)} "
        "--status ok --summary 'plain text summary'"
    )


def _format_spec_contract_for_prompt(
    acceptance_checklist: list[str],
    verification_expectations: list[str],
) -> str:
    acceptance_lines = acceptance_checklist or ["- [ ] No checklist items found in spec."]
    verification_lines = verification_expectations or _default_verification_expectations()
    return "\n".join(
        [
            "Spec Contract:",
            "Acceptance Checklist:",
            *acceptance_lines,
            "",
            "Verification Expectations:",
            *verification_lines,
        ]
    )


def _format_exit_checklist(spec_id: str | None = None) -> str:
    spec_token = spec_id or "<spec-id>"
    return "\n".join(
        [
            "Exit Checklist:",
            "1. Commit any completed code changes in this worktree.",
            f"2. Run {_format_verify_commands(_non_e2e_verify_commands())} before reporting completion.",
            "   Run these commands in the FOREGROUND and wait for their output in-band. "
            "Never launch them as background tasks or monitors and end your turn "
            "'waiting for the notification' — this session terminates with your final "
            "message and is NEVER re-invoked, so work left waiting is discarded.",
            "3. Report completion with "
            "`spec report --status ok|blocked|error|needs-input "
            f"--summary 'plain text summary'` (explicit fallback: "
            f"`spec report --spec {spec_token} "
            "--status ok|blocked|error|needs-input "
            "--summary 'plain text summary'`).",
            "   Keep the summary shell-safe: single-quote the complete value, avoid apostrophes, "
            "and do not include backticks or `$()`; describe commands without Markdown code "
            "delimiters because the shell evaluates substitutions before `spec report` starts.",
            f"4. Wait for `Completion recorded for {spec_token}:` before exiting.",
        ]
    )


def _summarize_prior_attempt_body(text: str) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= CARRIED_FORWARD_REVIEW_BODY_MAX_CHARS:
        return collapsed
    return collapsed[: CARRIED_FORWARD_REVIEW_BODY_MAX_CHARS - 3].rstrip() + "..."


def _format_prior_attempt_findings_for_prompt(
    prior_review_run_id: str,
    prior_review_summary: str,
    prior_review_findings: list[dict] | None,
) -> str:
    findings = _coerce_saved_review_findings(prior_review_findings or [])
    if not prior_review_run_id or not findings:
        return ""

    lines = [
        "Prior attempt findings:",
        "These findings were carried forward from a previous abandoned attempt on this spec.",
        "Address them proactively during your initial implementation, not as a later "
        "retry after review rediscovers them.",
        ("The file paths and line numbers are approximate because they came from a different branch."),
        f"- Source review file: {SPEC_RUNTIME_CONFIG.paths.state_dir}/runs/{prior_review_run_id}/review-result.json",
    ]
    if prior_review_summary:
        lines.append(f"- Prior review summary: {prior_review_summary}")
    lines.append("- Findings:")
    for index, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity", "") or "P2").strip() or "P2"
        title = str(finding.get("title", "") or finding.get("id", "") or "Untitled finding").strip()
        file_path = str(finding.get("file", "") or "").strip()
        start_line = _coerce_line_number(finding.get("start_line"), default=1)
        end_line = _coerce_line_number(finding.get("end_line"), default=start_line)
        confidence = _coerce_confidence(finding.get("confidence"))
        if file_path:
            if start_line == end_line:
                location = f"{file_path}:{start_line}"
            else:
                location = f"{file_path}:{start_line}-{end_line}"
        else:
            location = "location unavailable"
        lines.append(f"  {index}. [{severity}] {title} ({location}; confidence {confidence:.2f})")
        body = _summarize_prior_attempt_body(str(finding.get("body", "") or ""))
        if body:
            lines.append(f"     {body}")
    return "\n".join(lines)


def _format_resolved_input_for_prompt(
    input_question: str | None,
    input_response: str | None,
) -> str:
    response = str(input_response or "").strip()
    if not response:
        return ""

    question = str(input_question or "").strip()
    lines = [
        "Resolved implement input:",
        "The run was previously waiting for human input. Continue the implement phase with this answer.",
    ]
    if question:
        lines.append(f"- Question: {question}")
    lines.append(f"- Answer: {response}")
    return "\n".join(lines)


def _format_resolved_intake_for_prompt(intake: dict | None) -> str:
    """Render persisted spec-intake choices as authoritative agent context."""
    answers = intake.get("answers") if isinstance(intake, dict) else None
    if not isinstance(answers, dict) or not answers:
        return ""

    lines = [
        "Resolved spec intake:",
        "These persisted choices are authoritative for this implementation. "
        "Do not re-open the resolved questions.",
    ]
    for question_id, answer in answers.items():
        lines.append(
            f"- {question_id}: {json.dumps(answer, sort_keys=True, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _adapter_method_accepts_kwarg(method: object, name: str) -> bool:
    """Return True if *method* can accept ``name`` as a keyword argument.

    Used so the orchestrator can forward optional kwargs (e.g. ``mcp_servers``)
    only to adapters whose signature opts in, preserving backward compatibility
    for out-of-tree adapters registered via ``register_agent_adapter`` that
    predate the new parameter.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return True
    params = sig.parameters
    if name in params:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _build_agent_command(
    agent: str,
    worktree_path: Path,
    retry_context: ImplementContext | None = None,
    spec_id: str | None = None,
    spec_path: str | None = None,
    spec_revision: str | None = None,
    acceptance_checklist: list[str] | None = None,
    verification_expectations: list[str] | None = None,
    intake: dict | None = None,
    operator_request: OperatorRequest | None = None,
    input_question: str | None = None,
    input_response: str | None = None,
    setup_prompt: str | None = None,
    setup_mcp_prompt: str | None = None,
    prior_review_run_id: str | None = None,
    prior_review_summary: str | None = None,
    prior_review_findings: list[dict] | None = None,
    stream_json: bool = False,
    mcp_servers: dict[str, dict[str, object]] | None = None,
) -> list[str]:
    """Build the command to launch the agent."""
    acceptance_checklist = list(
        acceptance_checklist
        if acceptance_checklist is not None
        else (retry_context.acceptance_checklist if retry_context is not None else [])
    )
    verification_expectations = list(
        verification_expectations
        if verification_expectations is not None
        else (
            retry_context.verification_expectations
            if retry_context is not None
            else _default_verification_expectations()
        )
    )
    spec_contract = _format_spec_contract_for_prompt(
        acceptance_checklist,
        verification_expectations,
    )
    resolved_operator_request: OperatorRequest | None = None
    if _is_resolved_operator_request(operator_request):
        resolved_operator_request = operator_request
    elif retry_context is not None and retry_context.operator_request_response:
        resolved_operator_request = OperatorRequest(
            kind=retry_context.operator_request_kind or "agent_question",
            prompt=retry_context.operator_request_prompt or (input_question or "Operator request"),
            context=dict(retry_context.operator_request_context),
            suggested_action=retry_context.operator_request_suggested_action,
            options=list(retry_context.operator_request_options),
            requires_full_session=retry_context.operator_request_requires_full_session,
            status="consumed",
            response=retry_context.operator_request_response,
            response_source=retry_context.operator_request_response_source,
        )
    resolved_input = (
        _format_operator_request_for_prompt(resolved_operator_request)
        if resolved_operator_request is not None
        else _format_resolved_input_for_prompt(input_question, input_response)
    )
    resolved_steering = (
        _format_operator_steering_for_prompt(
            retry_context.operator_steering_message,
            provided_by=retry_context.operator_steering_provided_by,
            provided_at=retry_context.operator_steering_provided_at,
        )
        if retry_context is not None and retry_context.operator_steering_message
        else ""
    )
    intake_context = intake
    if intake_context is None and retry_context is not None:
        intake_context = retry_context.intake
    resolved_intake = _format_resolved_intake_for_prompt(intake_context)

    if retry_context and retry_context.implement_reason != "initial":
        run_state_dir = retry_context.run_state_dir or (
            _run_state_dir_for_run(retry_context.run_id)
            if retry_context.run_id
            else f"{SPEC_RUNTIME_CONFIG.paths.state_dir}/"
        )
        active_checks = ", ".join(retry_context.failing_commands) if retry_context.failing_commands else "none recorded"
        review_state = "none"
        if retry_context.review_feedback_active:
            review_state = (
                f"unresolved live findings ({retry_context.review_findings_count} structured findings)"
                if retry_context.review_findings_count
                else "unresolved live findings (review requested changes)"
            )
        elif retry_context.stale_review_feedback:
            review_state = (
                f"stale review evidence ({retry_context.review_findings_count} structured findings)"
                if retry_context.review_findings_count
                else "stale review evidence"
            )
        merge_state = retry_context.mergeability_issue or "no active mergeability issue"
        summary = retry_context.failure_summary or "No concise retry summary recorded."
        gate_diagnostic = (retry_context.gate_output or "").strip()
        debugger_section = ""
        if retry_context.debugger_summary:
            confidence_label = (
                "low-confidence hypothesis"
                if retry_context.debugger_confidence < 0.5
                else "moderate-confidence diagnosis"
                if retry_context.debugger_confidence < 0.8
                else "high-confidence diagnosis"
            )
            if retry_context.debugger_diagnosis_stale:
                debugger_header = (
                    "Prior diagnosis for the same recurring test — "
                    "the symptom has evolved but the root cause likely still applies."
                )
            else:
                debugger_header = (
                    "This section comes from the blocked-run debugger. "
                    "Treat low-confidence conclusions as hypotheses."
                )
            debugger_lines = [
                "Debugger diagnosis:",
                debugger_header,
                f"- Summary: {retry_context.debugger_summary}",
                f"- Root cause: {retry_context.debugger_root_cause}",
                f"- Next best action: {retry_context.debugger_next_best_action}",
                f"- Confidence: {retry_context.debugger_confidence:.2f} ({confidence_label})",
                f"- Needs new commit: {'yes' if retry_context.debugger_needs_new_commit else 'no'}",
            ]
            if retry_context.debugger_category:
                debugger_lines.append(f"- Category: {retry_context.debugger_category}")
            if retry_context.debugger_blocker_signature:
                debugger_lines.append(f"- Blocker signature: {retry_context.debugger_blocker_signature}")
            debugger_section = "\n".join(debugger_lines)
        reason_label = {
            "retry": "the orchestrator's verify phase found test/lint failures",
            "merge_conflict": "a merge conflict with master needs resolution",
            "review_feedback": "a code review requested changes",
            "recovery": "the previous implement attempt failed before completion",
        }.get(retry_context.implement_reason, "a prior attempt did not pass")
        prompt_lines = [
            spec_contract,
            "",
            (
                f"You are resuming the IMPLEMENT phase for spec "
                f"{retry_context.spec_path or retry_context.run_id or '(unknown)'}. "
                f"This is attempt {retry_context.attempt_number} — {reason_label}. "
                f"Your goal is to land this spec with all checks passing. "
                f"Read the spec, study the failure output below, and do whatever "
                f"you need to make the code correct."
            ),
            "",
            "Retry failure package:",
            f"- Run ID: {retry_context.run_id or '(unknown)'}",
            f"- Attempt: {retry_context.attempt_number}",
            f"- Pinned spec path: {retry_context.spec_path or '(unknown)'}",
            f"- Pinned spec revision: {retry_context.spec_revision or '(unknown)'}",
        ]
        prompt_lines.extend(
            line
            for line in [
                (
                    f"- Previous implement result: {retry_context.previous_implement_result_path}"
                    if retry_context.previous_implement_result_path
                    else ""
                ),
                (
                    f"- Triggering debugger artifact: {retry_context.triggering_block_diagnosis_path}"
                    if retry_context.triggering_block_diagnosis_path
                    else ""
                ),
                (
                    f"- Operator intervention artifact: {retry_context.operator_request_path}"
                    if retry_context.operator_request_path
                    else ""
                ),
                (
                    f"- Operator steering artifact: {retry_context.operator_steering_path}"
                    if retry_context.operator_steering_path
                    else ""
                ),
                f"- Evidence root: {run_state_dir}",
                f"- Current local head SHA: {retry_context.current_head_sha or '(unknown)'}",
                f"- Triggering phase: {retry_context.triggering_phase or '(unknown)'}",
                ("- Inspect before editing: implement-context.json, gate-status.json, and review-result.json if present."),
                f"- Active gates/checks: {active_checks}",
                (
                    f"- First failing test node: {retry_context.first_failed_test_nodeid}"
                    if retry_context.first_failed_test_nodeid
                    else ""
                ),
                (
                    f"- Rerun this exact node first on the current head: {retry_context.first_failed_test_reproducer}"
                    if retry_context.first_failed_test_reproducer
                    else ""
                ),
                f"- Summary: {summary}",
                (
                    f"- Latest review head SHA: {retry_context.reviewed_head_sha} (differs from local head)"
                    if retry_context.reviewed_head_sha and retry_context.reviewed_head_sha != retry_context.current_head_sha
                    else ""
                ),
                (
                    f"- Triggering review artifact: {retry_context.triggering_review_result_path}"
                    if retry_context.triggering_review_result_path
                    else ""
                ),
                f"- Review findings: {review_state}",
                f"- Merge conflict or PR mergeability issue: {merge_state}",
            ]
            if line
        )
        if gate_diagnostic:
            prompt_lines.append("")
            prompt_lines.append("Gate failure output (from the orchestrator's verify run):")
            prompt_lines.append(gate_diagnostic)

        instruction_lines: list[str] = []
        if retry_context.targeted_test_not_executed:
            instruction_lines.append(
                retry_context.targeted_test_not_executed_warning
                or (
                    "WARNING: The targeted test was skipped/deselected locally — "
                    "your prior `ok` was not validated against the actual failing test."
                )
            )
        if retry_context.first_failed_test_reproducer:
            instruction_lines.append(
                "Before claiming the branch is fixed, rerun the exact failing node from the live verify run first:\n"
                f"  {retry_context.first_failed_test_reproducer}\n"
                "Use the stored targeted diagnostic below if the rerun still fails."
            )
        if retry_context.failing_commands:
            instruction_lines.append("Make the code changes needed to pass all failing checks, then commit.")
            self_verify_commands = [
                command for command in retry_context.failing_commands if command in _non_e2e_verify_commands()
            ]
            if self_verify_commands:
                verify_lines = "\n".join(f"  {command}" for command in self_verify_commands)
                instruction_lines.append(
                    "Before reporting completion, run the failing command yourself:\n"
                    f"{verify_lines}\n"
                    "If it fails, diagnose and fix. Only report done when the command "
                    "passes or you are genuinely stuck and need orchestrator help."
                )
            elif any(command in _e2e_verify_commands() for command in retry_context.failing_commands):
                instruction_lines.append(
                    "Use the captured error output to fix the issue. Do not run "
                    f"{_format_verify_commands(_e2e_verify_commands())} during implement; "
                    "the orchestrator verify phase will rerun it."
                )
        if retry_context.review_feedback_active:
            review_instruction = (
                "Address the unresolved review findings. Use `review-result.json` for the full finding list"
            )
            if retry_context.review_source_check_url:
                review_instruction += f" and source check details at {retry_context.review_source_check_url}"
            instruction_lines.append(f"{review_instruction}.")
            instruction_lines.append(REVIEW_FINDING_CLASS_INSTRUCTION)
        elif retry_context.stale_review_feedback:
            stale_instruction = (
                "Review-result.json contains stale review evidence for an older head. "
                "Use it as hints only after you address the current live failures."
            )
            if retry_context.review_source_check_url:
                stale_instruction += f" Source check details: {retry_context.review_source_check_url}."
            instruction_lines.append(stale_instruction)
        if retry_context.triggering_phase == "review":
            reviewed_head_label = _short_sha(retry_context.reviewed_head_sha) or "the reviewed head"
            instruction_lines.append(
                "Append your fixes as new commits on top of the current HEAD "
                f"({reviewed_head_label}). Do NOT amend, rebase, reset, or otherwise rewrite "
                "existing history — the orchestrator verifies that the reviewed head remains an "
                "ancestor of your new head after implement and fails the attempt if the branch "
                "was recreated from base."
            )
        if retry_context.mergeability_issue and retry_context.implement_reason == "merge_conflict":
            instruction_lines.append(
                "Resolve the merge conflict or PR mergeability issue on this branch. "
                f"If needed, run `git fetch origin && git merge {BASE_REF}`, then "
                f"run {_format_verify_commands(_non_e2e_verify_commands())}, "
                "commit the resolution, and Do NOT push. "
                "Once the branch is fixed locally, report STATUS=ok."
            )
        elif retry_context.mergeability_issue:
            instruction_lines.append(
                "Keep the recorded PR mergeability note in mind, but do not treat it as "
                "the primary task unless a fresh local `git merge origin/master` now "
                "conflicts."
            )
        if retry_context.rescue_snapshot_path:
            rescue_detail = retry_context.rescue_snapshot_summary or "unpushed work"
            instruction_lines.append(
                "IMPORTANT: a prior attempt's workspace had to be reset, so its "
                f"work is NOT present in the current tree. It was rescued ({rescue_detail}) "
                f"to {retry_context.rescue_snapshot_path}. Do not assume earlier changes "
                "survived — inspect the actual worktree state, and recover from the rescue "
                "snapshot (git bundle / patch) if you need that work before continuing."
            )
        if retry_context.recovery_objective:
            instruction_lines.append(retry_context.recovery_objective)
        elif (
            retry_context.failure_summary
            and not retry_context.failing_commands
            and not retry_context.review_feedback_active
            and not retry_context.mergeability_issue
        ):
            instruction_lines.append(
                "The prior implement attempt ended before completion. Inspect the "
                "current branch and worktree state first — do not assume earlier work is "
                "still present. If there are already local changes, continue from them "
                "instead of starting over. Commit any completed work, then report STATUS=ok."
            )
        if not instruction_lines:
            instruction_lines.append(
                "Use the evidence directory to determine the remaining work, then make the minimal fix and commit it."
            )
        instruction_lines.append(
            "Read AGENTS.md for project conventions. Do NOT re-implement from scratch "
            "— only address the active failures summarized above."
        )
        prompt_sections = [spec_contract]
        if resolved_intake:
            prompt_sections.append(resolved_intake)
        if resolved_input:
            prompt_sections.append(resolved_input)
        if resolved_steering:
            prompt_sections.append(resolved_steering)
        prompt_sections.append("\n".join(prompt_lines[2:]))
        if debugger_section:
            prompt_sections.append(debugger_section)
        prompt_sections.append("\n".join(instruction_lines))
        prompt = "\n\n".join(prompt_sections)
    else:
        spec_prompt = (
            SPEC_PROMPT.replace("<spec-id>", spec_id or "<spec-id>")
            .replace("<spec-path>", spec_path or "<spec-path>")
            .replace("<spec-revision>", spec_revision or "(unknown)")
        )
        prior_findings = _format_prior_attempt_findings_for_prompt(
            prior_review_run_id or "",
            prior_review_summary or "",
            prior_review_findings,
        )
        prompt_sections = [spec_contract]
        if resolved_intake:
            prompt_sections.append(resolved_intake)
        if resolved_input:
            prompt_sections.append(resolved_input)
        if resolved_steering:
            prompt_sections.append(resolved_steering)
        if prior_findings:
            prompt_sections.append(prior_findings)
        prompt_sections.append(spec_prompt)
        prompt = "\n\n".join(prompt_sections)

    completion_instruction = COMPLETE_HANDSHAKE_INSTRUCTION.replace("<spec-id>", spec_id or "<spec-id>")
    exit_checklist = _format_exit_checklist(spec_id)
    prompt = f"{exit_checklist}\n\n{prompt}\n\n{completion_instruction}"
    state_dir = (
        worktree_path.parent.parent / SPEC_RUNTIME_CONFIG.paths.state_dir
        if worktree_path.parent.name == SPEC_RUNTIME_CONFIG.paths.worktrees_dir
        else worktree_path / SPEC_RUNTIME_CONFIG.paths.state_dir
    )

    adapter = get_agent_adapter(agent)
    setup_prompt = str(setup_prompt or "").strip()
    setup_mcp_prompt = str(setup_mcp_prompt or "").strip()
    if setup_prompt:
        prompt = f"{prompt}\n\n{setup_prompt}"
    if setup_mcp_prompt and adapter.capabilities.supports_mcp:
        prompt = f"{prompt}\n\n{setup_mcp_prompt}"
    implement_kwargs: dict[str, object] = {
        "prompt": prompt,
        "worktree_path": worktree_path,
        "state_dir": state_dir,
        "stream_json": stream_json,
        "mcp_config_path": _mcp_config_path(worktree_path),
    }
    if _adapter_method_accepts_kwarg(adapter.build_implement_command, "mcp_servers"):
        implement_kwargs["mcp_servers"] = mcp_servers
    return adapter.build_implement_command(**implement_kwargs)


def _is_git_merge_conflict_output(text: str) -> bool:
    """Return True when git merge output indicates a conflict."""
    lower = (text or "").lower()
    return "conflict (" in lower or "automatic merge failed" in lower


def _orchestrator_fetch_runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Adapter so ``run_git_fetch_with_timeout`` can drive ``run_subprocess``.

    Routing fetches through ``run_subprocess`` preserves the orchestrator's env
    handling and keeps fetches interceptable by tests that patch
    ``run_subprocess``.
    """
    cwd = kwargs.get("cwd")
    timeout_value = kwargs.get("timeout")
    timeout_int = int(timeout_value) if timeout_value is not None else None
    return run_subprocess(cmd, cwd=Path(cwd) if cwd else None, timeout=timeout_int)


def _fetch_origin_master(worktree_path: Path, *, base_ref: str = "") -> str:
    """Fetch the configured base ref and return an empty string on success."""
    ref = base_ref or BASE_REF
    try:
        backend = _resolve_execution_backend()
    except (ExecutionBackendNotImplementedError, UnknownExecutionBackendError):
        backend = None
    if (
        backend is not None
        and backend.identity.backend == "clone"
        and _resolve_git_ref_sha(worktree_path, ref)
    ):
        return ""
    remote_name, branch_name = ref.split("/", 1) if "/" in ref else ("origin", ref)
    try:
        outcome = run_git_fetch_with_timeout(
            [remote_name, branch_name],
            cwd=worktree_path,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
            runner=_orchestrator_fetch_runner,
        )
    except GitFetchTimeoutError as exc:
        return f"git fetch {remote_name} {branch_name} timed out after {exc.timeout_seconds:.0f}s"
    if outcome.is_success:
        return ""
    return (
        outcome.stderr.strip()
        or outcome.stdout.strip()
        or f"git fetch {remote_name} {branch_name} failed"
    )


def _resolve_git_ref_sha(worktree_path: Path, ref: str) -> str:
    result = run_subprocess(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _branch_contains_ref(worktree_path: Path, ref: str) -> tuple[bool | None, str]:
    check = run_subprocess(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"],
        cwd=worktree_path,
    )
    if check.returncode == 0:
        return True, ""
    if check.returncode == 1:
        return False, ""
    detail = check.stderr.strip() or check.stdout.strip() or "git merge-base --is-ancestor failed"
    return None, detail


def _abort_merge_after_conflict(worktree_path: Path) -> str:
    abort_result = run_subprocess(["git", "merge", "--abort"], cwd=worktree_path)
    if abort_result.returncode == 0:
        return ""

    detail = abort_result.stderr.strip() or abort_result.stdout.strip()
    lower = detail.lower()
    if "no merge to abort" in lower or "there is no merge to abort" in lower:
        return ""
    if "merge_head" in lower and "missing" in lower:
        return ""
    return detail or "git merge --abort failed"


def _spec_auto_merge_enabled() -> bool:
    return os.getenv("SIM_SPEC_AUTO_MERGE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _merge_origin_master(
    worktree_path: Path,
    *,
    fetch_origin_master: bool = True,
    strategy_option: str | None = None,
    base_ref: str = "",
) -> MergeOriginMasterResult:
    """Merge the configured base ref into the current branch when needed."""
    ref = base_ref or BASE_REF
    if fetch_origin_master:
        fetch_error = _fetch_origin_master(worktree_path, base_ref=ref)
        if fetch_error:
            return MergeOriginMasterResult(status="error", stderr=fetch_error)

    contains_master, contains_error = _branch_contains_ref(worktree_path, ref)
    if contains_master is True:
        return MergeOriginMasterResult(status="noop")
    if contains_master is None:
        return MergeOriginMasterResult(
            status="error",
            stderr=contains_error,
        )

    merge_cmd = ["git", "merge"]
    if strategy_option:
        merge_cmd += ["-X", strategy_option]
    merge_cmd += [ref, "--no-edit"]
    merge_result = run_subprocess(merge_cmd, cwd=worktree_path)
    if merge_result.returncode == 0:
        return MergeOriginMasterResult(status="success")

    merge_detail = merge_result.stderr.strip() or merge_result.stdout.strip() or "git merge failed"
    merge_output = f"{merge_result.stdout}\n{merge_result.stderr}".strip()
    if _is_git_merge_conflict_output(merge_output):
        abort_result = run_subprocess(["git", "merge", "--abort"], cwd=worktree_path)
        if abort_result.returncode != 0:
            abort_detail = abort_result.stderr.strip() or abort_result.stdout.strip()
            if abort_detail:
                merge_detail = f"{merge_detail} (merge abort failed: {abort_detail})"
        return MergeOriginMasterResult(status="conflict", stderr=merge_detail)
    return MergeOriginMasterResult(status="error", stderr=merge_detail)


def phase_verify(run: RunState, repo_root: Path) -> str:
    """Run configured verify gates in worktree and record gate results."""
    workspace = _resolve_workspace_handle(run, repo_root)
    worktree_path = workspace.path
    if not worktree_path.is_dir():
        run.last_error = f"Worktree missing: {worktree_path}"
        return "failed"

    state_file = _gate_status_path(repo_root, run)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    if not _verify_preflight_shared_memory(run, repo_root):
        logger.warning("Verify preflight failed for %s", run.spec_id)
        return "failed"

    fetch_error = _fetch_origin_master(worktree_path)
    if fetch_error:
        run.last_error = f"Verify preflight failed while fetching {BASE_REF}: {fetch_error}"
        logger.warning("Verify preflight fetch failed for %s: %s", run.spec_id, fetch_error)
        return "failed"

    origin_master_sha = _resolve_git_ref_sha(worktree_path, BASE_REF)
    if not origin_master_sha:
        run.last_error = f"Verify preflight could not resolve {BASE_REF} after fetch."
        logger.warning("Verify preflight could not resolve %s for %s", BASE_REF, run.spec_id)
        return "failed"

    contains_master, contains_error = _branch_contains_ref(worktree_path, BASE_REF)
    if contains_master is None:
        run.last_error = f"Verify preflight failed while checking merge-base with {BASE_REF}: {contains_error}"
        logger.warning(
            "Verify preflight merge-base check failed for %s: %s",
            run.spec_id,
            contains_error,
        )
        return "failed"

    merge_result = MergeOriginMasterResult(status="noop")
    if contains_master:
        run.last_merged_master_sha = origin_master_sha
    else:
        if run.last_merged_master_sha == origin_master_sha:
            logger.info(
                "%s SHA %s was previously merged for %s, but HEAD no longer contains it; retrying merge.",
                BASE_REF,
                _short_sha(origin_master_sha),
                run.spec_id,
            )
        merge_result = _merge_origin_master(
            worktree_path,
            fetch_origin_master=False,
        )
        if merge_result.status == "conflict" and _spec_auto_merge_enabled():
            abort_error = _abort_merge_after_conflict(worktree_path)
            if abort_error:
                run.last_error = (
                    f"Verify preflight could not abort the conflicted merge before auto-resolve: {abort_error}"
                )
                logger.warning(
                    "Verify preflight abort failed for %s: %s",
                    run.spec_id,
                    abort_error,
                )
                return "failed"

            merge_result = _merge_origin_master(
                worktree_path,
                fetch_origin_master=False,
                strategy_option="theirs",
            )
            if merge_result.status in {"success", "noop"}:
                logger.warning(
                    "Auto-resolved merge conflict with -X theirs for %s. Gates will verify correctness.",
                    run.spec_id,
                )

    if merge_result.status == "conflict":
        detail = merge_result.stderr or f"git merge {BASE_REF} reported conflicts"
        run.merge_conflict_error = f"Merge conflict while merging {BASE_REF} before verify: {detail}"
        run.mergeability_issue = run.merge_conflict_error
        run.last_error = run.merge_conflict_error
        logger.warning("Verify preflight merge conflict for %s: %s", run.spec_id, detail)
        return "failed"
    if merge_result.status == "error":
        detail = merge_result.stderr or "unknown error"
        run.last_error = f"Verify preflight failed while merging {BASE_REF}: {detail}"
        logger.warning("Verify preflight merge failed for %s: %s", run.spec_id, detail)
        return "failed"
    run.last_merged_master_sha = origin_master_sha

    parallel_gates: list[str] = []
    sequential_gates: list[str] = []
    serial_phase_started = False
    for gate in REQUIRED_GATES:
        if not serial_phase_started and gate in PARALLEL_VERIFY_GATES:
            parallel_gates.append(gate)
            continue
        serial_phase_started = True
        sequential_gates.append(gate)

    state_run_dir = state_file.parent

    parallel_results: dict[str, VerifyGateResult] = {}
    if len(parallel_gates) > 1:
        with ThreadPoolExecutor(max_workers=len(parallel_gates)) as executor:
            future_by_gate = {}
            for gate in parallel_gates:
                command = VERIFY_GATE_COMMANDS.get(gate, f"make {gate}")
                logger.info("Running gate: %s (spec=%s)", command, run.spec_id)
                _emit_user_progress(f"[spec] {run.spec_id}: verify running {command}")
                _record_verify_gate_started(state_run_dir, gate, command, worktree_path)
                future_by_gate[gate] = executor.submit(
                    _run_verify_gate,
                    worktree_path,
                    gate,
                    repo_root,
                )
            for gate in parallel_gates:
                parallel_results[gate] = future_by_gate[gate].result()
    else:
        for gate in parallel_gates:
            command = VERIFY_GATE_COMMANDS.get(gate, f"make {gate}")
            logger.info("Running gate: %s (spec=%s)", command, run.spec_id)
            _emit_user_progress(f"[spec] {run.spec_id}: verify running {command}")
            _record_verify_gate_started(state_run_dir, gate, command, worktree_path)
            parallel_results[gate] = _run_verify_gate(worktree_path, gate, repo_root)

    first_failed_gate = ""
    for gate in parallel_gates:
        result = parallel_results[gate]
        _record_verify_gate_result(state_file, run.spec_id, gate, result, worktree_path=worktree_path)
        if result.completed_process.returncode != 0:
            logger.warning(
                "Gate %s failed for %s: exit=%d",
                gate,
                run.spec_id,
                result.completed_process.returncode,
            )
            if not first_failed_gate:
                first_failed_gate = gate
                _set_failed_verify_gate_error(run, gate, result)

    if first_failed_gate:
        return "failed"

    for gate in sequential_gates:
        command = VERIFY_GATE_COMMANDS.get(gate, f"make {gate}")
        logger.info("Running gate: %s (spec=%s)", command, run.spec_id)
        _emit_user_progress(f"[spec] {run.spec_id}: verify running {command}")
        _record_verify_gate_started(state_run_dir, gate, command, worktree_path)
        result = _run_verify_gate(worktree_path, gate, repo_root)
        _record_verify_gate_result(state_file, run.spec_id, gate, result, worktree_path=worktree_path)
        if result.completed_process.returncode != 0:
            logger.warning(
                "Gate %s failed for %s: exit=%d",
                gate,
                run.spec_id,
                result.completed_process.returncode,
            )
            _set_failed_verify_gate_error(run, gate, result)
            return "failed"

    return "passed"


@dataclass
class VerifyGateResult:
    completed_process: subprocess.CompletedProcess
    diagnostic: str = ""
    targeted_diagnostics: list[dict[str, str]] = field(default_factory=list)
    failure_subtype: str = ""


def _run_verify_gate(
    worktree_path: Path,
    gate: str,
    repo_root: Path | None = None,
) -> VerifyGateResult:
    command = _verify_gate_command_args(gate)
    typed_command = _verify_gate_typed_command(gate)
    timeout_seconds = DEFAULT_VERIFY_GATE_TIMEOUT_SECONDS
    backend = _resolve_execution_backend()

    def _run_via_backend(env: dict[str, str]) -> subprocess.CompletedProcess:
        try:
            launch = (
                typed_command.launch_argv(cwd=worktree_path)
                if typed_command is not None
                else nullcontext(command)
            )
            with launch as launch_argv:
                result = backend.run_command(
                    CommandRequest(
                        argv=launch_argv,
                        cwd=worktree_path,
                        env=env,
                        inherit_env=True,
                        timeout=timeout_seconds,
                    )
                )
        except subprocess.TimeoutExpired as exc:
            stderr_value = exc.stderr or ""
            if isinstance(stderr_value, bytes):
                stderr_value = stderr_value.decode("utf-8", "replace")
            stdout_value = exc.stdout or ""
            if isinstance(stdout_value, bytes):
                stdout_value = stdout_value.decode("utf-8", "replace")
            message = f"verify gate exceeded {timeout_seconds:.0f}s timeout"
            completed = subprocess.CompletedProcess(
                args=list(command),
                returncode=124,
                stdout=stdout_value,
                stderr=(
                    stderr_value
                    + ("\n" if stderr_value and not stderr_value.endswith("\n") else "")
                    + message
                ),
            )
            completed._timed_out = True  # type: ignore[attr-defined]
            return completed
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    if not _is_test_gate(gate):
        gate_env: dict[str, str] = {}
        _inject_worktree_venv_into_env(gate_env, worktree_path)
        return VerifyGateResult(completed_process=_run_via_backend(gate_env))

    diagnostic = ""
    targeted_diagnostics: list[dict[str, str]] = []
    if isinstance(backend, ContainerExecutionBackend):
        verify_env_cm = _container_verify_test_environment(worktree_path, backend)
    else:
        verify_env_cm = _with_verify_test_environment(worktree_path, repo_root)
    try:
        with verify_env_cm as gate_env:
            completed_process = _run_via_backend(gate_env)
            if completed_process.returncode != 0 and not getattr(
                completed_process, "_timed_out", False
            ):
                diagnostic = _run_failed_test_diagnostic(
                    worktree_path,
                    env=gate_env,
                    backend=backend,
                )
                targeted_output = completed_process.stdout or ""
                if not _extract_failed_test_node_ids(targeted_output):
                    targeted_output = "\n".join(
                        part.strip()
                        for part in (targeted_output, diagnostic)
                        if part and part.strip()
                    )
                targeted_diagnostics = _run_targeted_test_diagnostics(
                    worktree_path,
                    targeted_output,
                    env=gate_env,
                    backend=backend,
                )
                if _every_failed_test_passed_in_isolation(targeted_output, targeted_diagnostics):
                    failed_nodes = ", ".join(_extract_failed_test_node_ids(targeted_output))
                    logger.warning(
                        "Gate %s failed only on tests that pass in isolation (%s); "
                        "rerunning the gate once before charging a retry.",
                        gate,
                        failed_nodes,
                    )
                    rerun = _run_via_backend(gate_env)
                    if rerun.returncode == 0:
                        logger.warning(
                            "Gate %s passed on rerun; treating the first result as a flake.",
                            gate,
                        )
                        completed_process = rerun
                        diagnostic = (
                            f"[flake] {gate} failed once on tests that each pass in "
                            f"isolation ({failed_nodes}), then passed on an immediate "
                            "rerun. No retry was charged."
                        )
                        targeted_diagnostics = []
                    else:
                        # Reproduced. Order-dependence rather than flake — fall
                        # through with the original failure and let verify retry.
                        logger.warning(
                            "Gate %s failed again on rerun; keeping the original failure.",
                            gate,
                        )
    except RuntimeError as exc:
        completed_process = subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
        return VerifyGateResult(
            completed_process=completed_process,
            failure_subtype="prepare_environment_failed",
        )

    return VerifyGateResult(
        completed_process=completed_process,
        diagnostic=diagnostic,
        targeted_diagnostics=targeted_diagnostics,
    )


def _verify_gate_command_args(gate: str) -> list[str]:
    selected = _verify_gate_typed_command(gate)
    if selected is not None:
        return selected.argv()
    return shlex.split(VERIFY_GATE_COMMANDS.get(gate, f"make {gate}"))


def _verify_gate_typed_command(gate: str) -> CommandSpec | None:
    config = next((item for item in SPEC_RUNTIME_CONFIG.verify_gates if item.name == gate), None)
    if config is not None:
        variants = config.command_variants
        selected = variants.select()
        if selected is not None and _selected_command_uses_typed_runtime(variants, selected):
            return selected
    return None


def _selected_command_uses_typed_runtime(
    variants: CommandVariants,
    selected: CommandSpec,
) -> bool:
    """Whether the variant selected for this platform opts into typed execution.

    Legacy POSIX command strings remain argv-split unless they declare a shell.
    A Windows-only script is additive and must not affect that POSIX decision.
    """
    return bool(
        selected.mode == "argv"
        or variants.shell
        or (os.name == "nt" and variants.windows_command)
    )


def _shell_metadata_arguments(selected: CommandSpec, arguments: list[str]) -> tuple[str, ...]:
    """Return positional metadata only for shells that preserve its boundary.

    cmd hooks receive the same metadata through SPEC_ID, SPEC_RUN_ID, SPEC_PATH,
    and SPEC_WORKTREE in their environment. Direct argv and PowerShell hooks
    retain positional metadata for backward compatibility.
    """
    return () if selected.mode == "script" and selected.shell == "cmd" else tuple(arguments)


def _run_verify_subprocess_with_timeout(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess:
    """Run a verify-gate subprocess with a host-bounded timeout.

    On timeout the returned :class:`subprocess.CompletedProcess` carries a
    ``_timed_out=True`` attribute that the durable gate-record path uses to
    distinguish a timeout from a generic non-zero exit.
    """
    try:
        completed = run_subprocess(
            command,
            cwd=cwd,
            env=env,
            timeout=int(timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        stderr_value = exc.stderr or ""
        if isinstance(stderr_value, bytes):
            stderr_value = stderr_value.decode("utf-8", "replace")
        stdout_value = exc.stdout or ""
        if isinstance(stdout_value, bytes):
            stdout_value = stdout_value.decode("utf-8", "replace")
        message = f"verify gate exceeded {timeout_seconds:.0f}s timeout"
        completed = subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=stdout_value,
            stderr=(stderr_value + ("\n" if stderr_value and not stderr_value.endswith("\n") else "") + message),
        )
        completed._timed_out = True  # type: ignore[attr-defined]
    return completed


def _record_verify_gate_started(
    state_run_dir: Path,
    gate: str,
    command_str: str,
    worktree_path: Path,
) -> None:
    """Persist a durable started-record so status/watch can surface pending gates."""
    try:
        record_gate_started(
            state_run_dir,
            name=gate,
            command=_verify_gate_command_args(gate),
            cwd=str(worktree_path),
            timeout_seconds=DEFAULT_VERIFY_GATE_TIMEOUT_SECONDS,
            log_path=str(state_run_dir / f"{gate}.log"),
        )
    except OSError as exc:
        logger.warning("Failed to record gate-started for %s: %s", gate, exc)


def _record_verify_gate_finished(
    state_run_dir: Path,
    gate: str,
    result: VerifyGateResult,
) -> None:
    """Persist a durable terminal record for the gate execution."""
    try:
        if isinstance(result.completed_process, subprocess.CompletedProcess) and (
            getattr(result.completed_process, "_timed_out", False)
        ):
            record_gate_timeout(
                state_run_dir,
                name=gate,
                diagnostic=result.diagnostic or "",
            )
        else:
            record_gate_completed(
                state_run_dir,
                name=gate,
                exit_status=int(result.completed_process.returncode),
                diagnostic=result.diagnostic or "",
            )
    except OSError as exc:
        logger.warning("Failed to record gate result for %s: %s", gate, exc)


def _record_verify_gate_result(
    state_file: Path,
    spec_id: str,
    gate: str,
    result: VerifyGateResult,
    *,
    worktree_path: Path | None = None,
) -> None:
    completed_process = result.completed_process
    first_failed_test_reproducer = ""
    if worktree_path is not None and _is_test_gate(gate):
        first_failed_test_nodeid = _first_failed_test_nodeid(
            completed_process.stdout or "",
            result.diagnostic,
        )
        if first_failed_test_nodeid:
            first_failed_test_reproducer = _render_test_gate_targeted_diagnostic_command(
                worktree_path,
                first_failed_test_nodeid,
            )
    _record_gate_result(
        state_file,
        spec_id,
        gate,
        VERIFY_GATE_COMMANDS.get(gate, f"make {gate}"),
        completed_process.returncode,
        stdout=completed_process.stdout or "",
        stderr=completed_process.stderr or "",
        diagnostic=result.diagnostic,
        targeted_diagnostics=result.targeted_diagnostics,
        first_failed_test_reproducer=first_failed_test_reproducer,
    )
    _record_verify_gate_finished(state_file.parent, gate, result)


def _set_failed_verify_gate_error(
    run: RunState,
    gate: str,
    result: VerifyGateResult,
) -> None:
    completed_process = result.completed_process
    stdout = completed_process.stdout or ""
    stderr = completed_process.stderr or ""
    # Prefer the targeted test diagnostic when one exists. Otherwise most
    # build/browser tools write the actionable failure to stderr while stdout
    # ends with unrelated successful setup or bundle output. The full streams
    # remain in gate-status.json and stdout is still the final fallback.
    stored_diagnostic = _stored_test_diagnostic(result.diagnostic)
    # A supplemental diagnostic can fail to start in repos that do not use
    # pytest (or whose Python test environment is unavailable). Keep that note
    # in gate-status.json, but never let it replace the primary gate failure:
    # doing so can misclassify an ordinary retryable test failure as a
    # non-retryable environment-preparation failure.
    if stored_diagnostic.startswith(TEST_GATE_DIAGNOSTIC_UNAVAILABLE_PREFIX):
        stored_diagnostic = ""
    detail_source = stored_diagnostic or _stored_gate_stderr(stderr) or _stored_gate_stdout(stdout)
    detail = detail_source[-200:] if detail_source else "see gate-status.json"
    if result.failure_subtype == "prepare_environment_failed":
        run.last_error = (
            f"Verify environment preparation failed for gate '{gate}' "
            f"(exit {completed_process.returncode}). output: {detail}"
        )
        return
    run.last_error = f"Gate '{gate}' failed (exit {completed_process.returncode}). output: {detail}"


def _record_gate_result(
    state_file: Path,
    spec_id: str,
    gate: str,
    command: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    diagnostic: str = "",
    targeted_diagnostics: list[dict[str, str]] | None = None,
    first_failed_test_reproducer: str = "",
) -> None:
    """Record gate result in gate-status.json (same format as spec_workflow.sh)."""
    if state_file.exists():
        data = json.loads(state_file.read_text())
    else:
        data = {"spec_id": spec_id, "gates": {}}

    data["spec_id"] = spec_id
    data.setdefault("gates", {})
    gate_data = data["gates"].get(gate, {})
    if not isinstance(gate_data, dict):
        gate_data = {}
    attempts = int(gate_data.get("attempts", 0)) + 1
    timestamp = _now_iso()
    history = gate_data.get("history", [])
    if not isinstance(history, list):
        history = []
    stored_targeted_diagnostics = _stored_targeted_test_diagnostics(targeted_diagnostics) if _is_test_gate(gate) else []
    first_failed_test_nodeid = _first_failed_test_nodeid(stdout, diagnostic) if _is_test_gate(gate) else ""
    first_failed_test_reproducer = first_failed_test_reproducer.strip() if _is_test_gate(gate) else ""
    first_targeted_diagnostic = (
        _first_targeted_test_diagnostic(
            stored_targeted_diagnostics,
            first_failed_nodeid=first_failed_test_nodeid,
        )
        if _is_test_gate(gate)
        else {}
    )
    final_failure_shape = _stored_test_failure_shape(stdout, diagnostic) if _is_test_gate(gate) else ""
    if _is_test_gate(gate) and history:
        last_entry = history[-1]
        if (
            isinstance(last_entry, dict)
            and gate_data.get("last_status") == "failed"
            and last_entry.get("status") == "failed"
        ):
            previous_fingerprint = str(gate_data.get("failure_fingerprint", "") or "").strip()
            if not previous_fingerprint:
                previous_fingerprint = _build_test_failure_fingerprint(
                    str(gate_data.get("last_stdout", "") or ""),
                    str(gate_data.get("last_diagnostic", "") or ""),
                )
            if previous_fingerprint:
                last_entry["failure_fingerprint"] = previous_fingerprint
    history.append(
        _build_gate_history_entry(
            gate,
            attempt=attempts,
            exit_code=exit_code,
            timestamp=timestamp,
            stdout=stdout,
            stderr=stderr,
            diagnostic=diagnostic,
        )
    )
    data["gates"][gate] = {
        "attempts": attempts,
        "last_status": "passed" if exit_code == 0 else "failed",
        "last_command": command,
        "last_run_at": timestamp,
        "last_stdout": _stored_gate_stdout(stdout),
        "last_stderr": _stored_gate_stderr(stderr),
        "last_diagnostic": (_stored_test_diagnostic(diagnostic) if _is_test_gate(gate) else ""),
        "last_targeted_diagnostics": stored_targeted_diagnostics,
        "first_failed_test_nodeid": first_failed_test_nodeid,
        "first_failed_test_reproducer": first_failed_test_reproducer,
        "first_targeted_diagnostic": first_targeted_diagnostic if _is_test_gate(gate) else {},
        "final_failure_shape": final_failure_shape,
        "failure_fingerprint": (
            _build_test_failure_fingerprint(stdout, diagnostic) if _is_test_gate(gate) and exit_code != 0 else ""
        ),
        "history": history[-GATE_HISTORY_LIMIT:],
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _check_spec_authoring_policy(worktree_path: Path, branch: str) -> str:
    """Validate that a spec authoring branch only touches allowed paths.

    Compares the full branch delta against the merge-base with the configured
    base ref, so all commits on the branch are checked — not just the last one.

    Returns an error message if policy is violated, empty string otherwise.
    """
    ALLOWED_PREFIXES = ("specs/", "prompts/", ".github/prompts/")
    base_ref = SPEC_RUNTIME_CONFIG.base_ref
    try:
        # Find the merge-base between the branch and the base ref
        merge_base = run_subprocess(
            ["git", "merge-base", base_ref, "HEAD"],
            cwd=worktree_path,
        )
        if merge_base.returncode != 0:
            # No merge-base (orphan branch or missing ref) — fall back to
            # diffing all files tracked on HEAD
            result = run_subprocess(
                ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
                cwd=worktree_path,
            )
        else:
            base_sha = merge_base.stdout.strip()
            result = run_subprocess(
                ["git", "diff", "--name-only", f"{base_sha}...HEAD"],
                cwd=worktree_path,
            )
        if result.returncode != 0:
            return ""
    except Exception:
        return ""

    changed_files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    violations = [f for f in changed_files if not any(f.startswith(prefix) for prefix in ALLOWED_PREFIXES)]
    if violations:
        file_list = ", ".join(violations[:5])
        suffix = f" (+{len(violations) - 5} more)" if len(violations) > 5 else ""
        return (
            f"Spec authoring branch '{branch}' modifies non-spec files: "
            f"{file_list}{suffix}. "
            f"Authoring branches may only touch files under: "
            f"{', '.join(ALLOWED_PREFIXES)}"
        )
    return ""


def _reconcile_publish_push_rejection(
    run: RunState,
    forge,
    *,
    branch: str,
    worktree_path: Path,
    push_message: str,
) -> PushResult | None:
    """Attempt safe self-reconciliation after a non-fast-forward publish push.

    One rejection shape is provably caused by this run's own writer and can be
    recovered without operator action: the remote still points at the head
    this run last published (``run.readiness_head_sha``) while the local
    worktree rewrote history (a post-review re-implement that recommitted from
    base instead of appending). Recovery force-pushes with the lease pinned to
    that exact head, so a concurrent third-party write still aborts the push.

    A remote that moved anywhere else — including strictly ahead of the local
    head — is NOT auto-adopted: without a provenance check the orchestrator
    cannot distinguish its own container-side push from a third-party
    fast-forward, and adopting the latter would record unverified code as
    ready.

    Returns the PushResult to use, or None when the rejection is not
    self-reconcilable (anything else keeps the existing terminal
    ``remote_diverged`` behavior).
    """
    normalized = str(push_message or "").lower()
    non_fast_forward = (
        "fetch first" in normalized
        or "[rejected]" in normalized
        or "non-fast-forward" in normalized
        or "tip of your current branch is behind" in normalized
    )
    if not non_fast_forward:
        return None
    readiness_head = str(run.readiness_head_sha or "").strip()
    if not readiness_head:
        return None
    if run_subprocess(["git", "fetch", "origin", branch], cwd=worktree_path).returncode != 0:
        return None
    # `git fetch origin <branch>` reliably updates only FETCH_HEAD; the
    # remote-tracking ref may be stale or absent in narrow run workspaces.
    remote_ref = run_subprocess(["git", "rev-parse", "FETCH_HEAD"], cwd=worktree_path)
    remote_head = (remote_ref.stdout or "").strip() if remote_ref.returncode == 0 else ""
    local_head = _head_sha(worktree_path) or ""
    if not remote_head or not local_head or remote_head == local_head:
        return None
    if remote_head != readiness_head:
        return None
    logger.warning(
        "Publish reconciling %s: remote still points at the last published head "
        "%s while the local worktree rewrote history; force-pushing with the "
        "lease pinned to that head.",
        branch,
        _short_sha(remote_head),
    )
    return forge.push_branch(branch, cwd=worktree_path, force=True, expect_sha=remote_head)


def _is_no_commits_between_error(message: str) -> bool:
    """Return whether a forge rejected PR creation because the trees have no delta."""
    return "no commits between" in str(message or "").lower()


def _create_verified_no_diff_completion_commit(
    run: RunState,
    repo_root: Path,
    *,
    worktree_path: Path,
    create_error: str,
) -> bool:
    """Create an auditable marker when verified work already exists on the base.

    A retroactive spec can be authored after its implementation has landed. In that
    case the implementation agent legitimately produces no tree delta, and GitHub
    refuses to create the lifecycle PR. Only carry verification across a marker
    commit when the checked-in spec explicitly marks every acceptance item complete,
    the exact current head passed verify, and the tracked tree is clean.
    """
    if run.run_mode != "spec":
        run.last_error = create_error
        return False

    spec_path = _active_spec_path(repo_root, run, prefer_worktree=True)
    if spec_path is None or not spec_path.is_file():
        run.last_error = f"{create_error}. The run's spec could not be read."
        return False
    checklist = _extract_acceptance_checklist_items(spec_path)
    if not checklist or any(not item.lower().startswith("- [x]") for item in checklist):
        run.last_error = (
            f"{create_error}. Refusing to synthesize completion provenance because "
            "the spec acceptance checklist is not fully checked."
        )
        return False

    current_head = _head_sha(worktree_path) or ""
    verified_head = str(run.verify_head_sha or "").strip()
    if not run.verify_passed_once or not verified_head or current_head != verified_head:
        run.last_error = (
            f"{create_error}. Refusing to synthesize completion provenance because "
            "the exact current head has not passed verify."
        )
        return False

    worktree_status = run_subprocess(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
    )
    if worktree_status.returncode != 0:
        detail = worktree_status.stderr.strip() or worktree_status.stdout.strip()
        run.last_error = f"Could not inspect the worktree before no-diff publish: {detail}"
        return False
    if worktree_status.stdout.strip():
        run.last_error = (
            f"{create_error}. Refusing to synthesize completion provenance because "
            "the worktree is not clean."
        )
        return False

    marker_message = f"Spec: record {run.spec_id} completion provenance"
    commit = run_subprocess(
        ["git", "commit", "--allow-empty", "-m", marker_message],
        cwd=worktree_path,
    )
    if commit.returncode != 0:
        detail = commit.stderr.strip() or commit.stdout.strip()
        run.last_error = f"Could not create no-diff completion provenance commit: {detail}"
        return False

    marker_head = _head_sha(worktree_path) or ""
    if not marker_head or marker_head == current_head:
        run.last_error = "No-diff completion provenance commit did not advance HEAD."
        return False
    unchanged = run_subprocess(
        ["git", "diff", "--quiet", current_head, marker_head],
        cwd=worktree_path,
    )
    if unchanged.returncode != 0:
        run.last_error = (
            "No-diff completion provenance commit changed the verified tree; "
            "refusing to carry verification forward."
        )
        return False

    run.verify_head_sha = marker_head
    _record_nonfatal_warning(
        run,
        phase="publish",
        failure_type="publish",
        failure_subtype="no_diff_completion_provenance",
        summary=(
            "Forge reported no commits between the implementation branch and base; "
            "created an empty completion-provenance commit for the already-verified tree."
        ),
        detail=(
            f"Verification was carried from {_short_sha(current_head)} to the tree-identical "
            f"marker {_short_sha(marker_head)} after all {len(checklist)} acceptance items "
            "were confirmed checked."
        ),
    )
    logger.warning(
        "Publish for %s had no tree delta; created verified completion marker %s.",
        run.spec_id,
        _short_sha(marker_head),
    )
    return True


def phase_publish(run: RunState, repo_root: Path) -> str:
    """Push branch and create PR (idempotent)."""
    workspace = _resolve_publish_workspace_handle(run, repo_root)
    worktree_path = workspace.path
    if not worktree_path.is_dir():
        run.last_error = f"Worktree missing: {worktree_path}"
        return "failed"

    branch = run.branch
    branch_error = _worktree_branch_alignment_error(worktree_path, branch)
    if branch_error:
        run.last_error = branch_error
        return "failed"

    # Enforce spec-PR policy: authoring branches may only touch spec files.
    if is_authoring_branch(branch):
        policy_error = _check_spec_authoring_policy(worktree_path, branch)
        if policy_error:
            run.last_error = policy_error
            return "failed"

    spec_path = _active_spec_path(repo_root, run, prefer_worktree=True)
    if spec_path is None or not spec_path.exists():
        run.last_error = f"Spec not found: {repo_root / _spec_path_for_run(run)}"
        return "failed"
    canonical_worktree_spec = _spec_path_in_tree(worktree_path, run)
    if spec_path == canonical_worktree_spec:
        if not _ensure_run_spec_committed(
            run,
            worktree_path=worktree_path,
            spec_path=spec_path,
        ):
            return "failed"
    checklist = _extract_acceptance_checklist(spec_path)

    # Check forge auth
    forge = _forge()
    auth_error = _check_forge_auth()
    if auth_error:
        run.last_error = auth_error
        return "failed"

    known_issues = _known_issues_markdown(_gate_status_path(repo_root, run))

    # Host-created implementation PRs start as draft. Promotion to ready is a
    # separate host-owned transition after exact-head readiness is satisfied.
    draft = True

    spec_ref = _spec_path_for_run(run)

    spec_block = f"[{spec_ref}]({spec_ref})"
    if run.run_mode == "task":
        summary = f"Task: {run.spec_id}"
        pr_title = f"Task: {run.spec_id}"
    else:
        summary = f"Implements the `{run.spec_id}` spec."
        pr_title = f"Spec: {run.spec_id}"

    body = (
        f"## Spec\nSpec-ID: {run.spec_id}\n{spec_block}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Review\n{format_pr_review_owner()}\n\n"
        f"## Acceptance Criteria\n{checklist}\n\n"
        f"## Known Issues\n{known_issues}\n"
    )
    labels: tuple[str, ...] = ()

    # Honor backend-supplied PR/MR metadata when present. Absent metadata is
    # the default for the worktree backend; the host falls back to the
    # generated title/body above. The metadata's head_sha (when set)
    # identifies the commit it describes — stale entries are ignored.
    outbox_metadata = collect_workspace_outbox_metadata(workspace)
    if outbox_metadata is not None:
        worktree_head_sha = _head_sha(worktree_path) or ""
        head_match = (
            not outbox_metadata.head_sha
            or outbox_metadata.head_sha == worktree_head_sha
        )
        if head_match:
            if outbox_metadata.title:
                pr_title = outbox_metadata.title
            if outbox_metadata.summary:
                summary = outbox_metadata.summary
                body = (
                    f"## Spec\nSpec-ID: {run.spec_id}\n{spec_block}\n\n"
                    f"## Summary\n{summary}\n\n"
                    f"## Review\n{format_pr_review_owner()}\n\n"
                    f"## Acceptance Criteria\n{checklist}\n\n"
                    f"## Known Issues\n{known_issues}\n"
                )
            if outbox_metadata.body:
                body = outbox_metadata.body
            labels = outbox_metadata.labels

    # Refresh an existing PR body before push so synchronize sees the local-review marker.
    existing = forge.find_pr_for_branch(branch, base_branch=PR_BASE_BRANCH, cwd=worktree_path)
    existing_pr = str(existing.number) if existing else ""
    if existing_pr:
        if not forge.update_pr(
            existing.number,
            title=pr_title,
            body=body,
            add_labels=labels,
            cwd=worktree_path,
        ):
            run.last_error = f"gh pr edit failed for PR #{existing_pr}"
            return "failed"
        logger.info("PR #%s already exists for %s; refreshed body", existing_pr, branch)
    existing_was_ready = bool(existing_pr and existing is not None and existing.is_draft is False)

    # Push branch after existing PR metadata is up to date.
    push = forge.push_branch(branch, cwd=worktree_path)
    if not push.ok:
        reconciled = _reconcile_publish_push_rejection(
            run,
            forge,
            branch=branch,
            worktree_path=worktree_path,
            push_message=push.message,
        )
        if reconciled is not None:
            push = reconciled
    if not push.ok:
        run.last_error = f"git push failed: {push.message}"
        return "failed"

    if not existing_pr:
        try:
            forge.create_pr(
                title=pr_title,
                body=body,
                head=branch,
                base=PR_BASE_BRANCH,
                draft=draft,
                labels=labels,
                cwd=worktree_path,
            )
        except RuntimeError as exc:
            create_error = str(exc)
            if not _is_no_commits_between_error(create_error):
                run.last_error = create_error
                return "failed"
            if not _create_verified_no_diff_completion_commit(
                run,
                repo_root,
                worktree_path=worktree_path,
                create_error=create_error,
            ):
                return "failed"
            marker_push = forge.push_branch(branch, cwd=worktree_path)
            if not marker_push.ok:
                run.last_error = f"git push failed after no-diff completion marker: {marker_push.message}"
                return "failed"
            try:
                forge.create_pr(
                    title=pr_title,
                    body=body,
                    head=branch,
                    base=PR_BASE_BRANCH,
                    draft=draft,
                    labels=labels,
                    cwd=worktree_path,
                )
            except RuntimeError as retry_exc:
                run.last_error = str(retry_exc)
                return "failed"

        logger.info("PR created for %s%s", branch, " (draft)" if draft else "")

    current_head_sha = _head_sha(worktree_path)
    if not current_head_sha:
        run.last_error = f"Could not determine HEAD SHA for {worktree_path}"
        return "failed"
    existing_ready_reset_to_draft = False
    if existing_pr and existing_was_ready:
        previous_ready_head = str(run.readiness_head_sha or "").strip()
        if previous_ready_head != current_head_sha:
            if not forge.mark_pr_draft(int(existing_pr), cwd=worktree_path):
                run.last_error = f"Failed to reset PR #{existing_pr} to draft for new head"
                return "failed"
            existing_ready_reset_to_draft = True
            logger.info(
                "PR #%s already exists for %s; reset draft state after pushing new head %s",
                existing_pr,
                branch,
                _short_sha(current_head_sha),
            )
    run.readiness_head_sha = current_head_sha
    if existing_was_ready and not existing_ready_reset_to_draft:
        run.readiness_status = "ready"
    else:
        run.readiness_status = "draft-published"
    run.readiness_blocker = ""

    _reset_local_review_gate_for_head(
        run,
        repo_root,
        current_head_sha=current_head_sha,
    )

    return "passed"


def _extract_acceptance_checklist(spec_path: Path) -> str:
    """Extract acceptance criteria checklist from a spec file."""
    items = _extract_acceptance_checklist_items(spec_path)
    return "\n".join(items) if items else "- [ ] No checklist items found in spec."


def _extract_markdown_section_items(
    spec_path: Path,
    heading: str,
    *,
    checklist_only: bool,
) -> list[str]:
    if not spec_path.exists():
        return []

    text = spec_path.read_text()
    in_section = False
    items: list[str] = []
    heading_pattern = f"## {heading}"
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if line.startswith(heading_pattern):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not stripped:
            continue
        if checklist_only:
            if re.match(r"^- \[[ xX]\]\s+", stripped):
                items.append(stripped)
            continue
        if re.match(r"^- \[[ xX]\]\s+", stripped) or re.match(r"^- .+", stripped) or re.match(r"^\d+\. .+", stripped):
            items.append(stripped)
    return items


def _extract_acceptance_checklist_items(spec_path: Path) -> list[str]:
    return _extract_markdown_section_items(
        spec_path,
        "Acceptance Criteria",
        checklist_only=True,
    )


def _default_verification_expectations() -> list[str]:
    expectations = [f"- run `{command}` during implement" for command in _non_e2e_verify_commands()]
    expectations.extend(
        f"- do not run `{command}` during implement; orchestrator verify reruns it"
        for command in _e2e_verify_commands()
    )
    return expectations


def _extract_verification_expectations(spec_path: Path) -> list[str]:
    items = _extract_markdown_section_items(
        spec_path,
        "Verification",
        checklist_only=False,
    )
    return items if items else _default_verification_expectations()


def _known_issues_markdown(state_file: Path) -> str:
    """Generate known issues markdown from gate status."""
    if not state_file.exists():
        return "- Gate status file is missing; required gates have not been recorded."
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, TypeError):
        return "- Gate status file is corrupt."
    gates = data.get("gates", {})
    issues = []
    for gate in REQUIRED_GATES:
        gate_data = gates.get(gate)
        if gate_data is None:
            issues.append(f"- `{gate}` has no recorded result.")
            continue
        status = gate_data.get("last_status", "unknown")
        attempts = int(gate_data.get("attempts", 0))
        command = gate_data.get("last_command", "n/a")
        if status != "passed":
            suffix = " (retry cap reached)" if attempts >= RETRY_CAP else ""
            issues.append(f"- `{gate}` last status is `{status}` after {attempts} attempts via `{command}`{suffix}.")
    return "\n".join(issues) if issues else "None."


def _retry_cap_escalation_summary_path(repo_root: Path, run: RunState) -> Path:
    return _state_root(repo_root) / "runs" / run.run_id / "retry-cap-escalation.md"


def _normalized_required_gate_status(
    repo_root: Path,
    run: RunState,
) -> dict[str, dict[str, object]]:
    _, gate_payload = _read_gate_status(repo_root, run)
    gates = gate_payload.get("gates", {}) if isinstance(gate_payload, dict) else {}
    normalized: dict[str, dict[str, object]] = {}
    for gate in REQUIRED_GATES:
        gate_entry = gates.get(gate, {})
        if not isinstance(gate_entry, dict):
            gate_entry = {}
        history = gate_entry.get("history", [])
        if not isinstance(history, list):
            history = []
        normalized[gate] = {
            "attempts": max(0, int(gate_entry.get("attempts", 0) or 0)),
            "last_status": str(gate_entry.get("last_status", "") or "").strip(),
            "history": [entry for entry in history if isinstance(entry, dict)],
        }
    return normalized


def _normalize_acceptance_checklist_item(item: str) -> str:
    return re.sub(r"^- \[[ xX]\]\s+", "", item.strip())


def _delivery_evidence_summary(
    run: RunState,
    gates: dict[str, dict[str, object]],
) -> str:
    parts = []
    for gate in REQUIRED_GATES:
        gate_entry = gates.get(gate, {})
        attempts = int(gate_entry.get("attempts", 0) or 0)
        status = str(gate_entry.get("last_status", "") or "").strip()
        if not attempts:
            parts.append(f"{gate}=not run")
            continue
        parts.append(f"{gate}={status or 'unknown'} (attempt {attempts})")

    review_status = run.review_decision_status or "not reached"
    parts.append(f"review={review_status}")
    if run.merge_conflict_error:
        parts.append("merge=conflict")
    elif _is_required_checks_failure(run.last_error):
        parts.append("merge=required checks failing")
    return "; ".join(parts)


def _durable_implementation_evidence_summary(run: RunState) -> tuple[bool, str]:
    evidence: list[str] = []
    if run.implement_has_new_commit:
        before = _short_sha(run.implement_head_sha_before)
        after = _short_sha(run.implement_head_sha_after)
        if before and after:
            evidence.append(f"implementation commit preserved ({before} -> {after})")
        else:
            evidence.append("implementation commit preserved")
    if run.implement_staged_changes:
        evidence.append("staged implementation changes preserved")
    if run.implement_unstaged_changes:
        evidence.append("unstaged implementation changes preserved")
    if evidence:
        return True, "; ".join(evidence)
    return False, "no durable implementation commit or diff evidence was preserved"


def _retry_cap_sections_evidence() -> str:
    return "sections=What was delivered/Retry history/Root cause/Recommended action"


def _retry_history_source_evidence(
    run: RunState,
    repo_root: Path,
    *,
    retry_rows: list[dict[str, object]],
) -> tuple[bool, str]:
    gate_status_path, gate_payload = _read_gate_status(repo_root, run)
    gate_history_available = False
    if isinstance(gate_payload, dict):
        raw_gates = gate_payload.get("gates", {})
        if isinstance(raw_gates, dict):
            gate_history_available = any(
                isinstance(entry, dict) and entry.get("history") for entry in raw_gates.values()
            )

    total_attempts = max(
        1,
        run.attempts + 1,
        max(
            (int(row.get("attempt", 0) or 0) for row in retry_rows if isinstance(row, dict)),
            default=0,
        ),
    )
    gate_status_label = (
        gate_status_path.relative_to(repo_root).as_posix()
        if gate_status_path.exists()
        else f"{gate_status_path.relative_to(repo_root).as_posix()} (missing)"
    )
    passed = bool(retry_rows) and gate_history_available
    evidence = (
        f"retry_rows={len(retry_rows)}; attempts={total_attempts}; "
        f"gate_history={'present' if gate_history_available else 'missing'} "
        f"at `{gate_status_label}`"
    )
    return passed, evidence


def _acceptance_item_evidence(
    item: str,
    run: RunState,
    repo_root: Path,
    *,
    pr_number: int | None,
    gates: dict[str, dict[str, object]],
    retry_rows: list[dict[str, object]],
) -> tuple[bool, str]:
    normalized_item = _normalize_acceptance_checklist_item(item)
    key = normalized_item.lower()
    delivery_evidence = _delivery_evidence_summary(run, gates)
    has_implementation_evidence, implementation_evidence = _durable_implementation_evidence_summary(run)

    if "when retry cap is exhausted and a pr exists" in key or "post a structured comment" in key:
        if pr_number is None:
            return (
                False,
                f"{_retry_cap_sections_evidence()}; no PR found, so the stdout/log fallback path applies instead",
            )
        return (
            True,
            f"{_retry_cap_sections_evidence()}; PR #{pr_number}; {delivery_evidence}",
        )

    if "maps acceptance criteria" in key or "test/gate status" in key or "gate/review" in key:
        evidence_available = any(int(gates[gate].get("attempts", 0) or 0) > 0 for gate in REQUIRED_GATES) or bool(
            run.review_decision_status
        )
        if not evidence_available:
            return False, "no preserved gate/review evidence was available to map"
        return True, delivery_evidence

    if "retry history" in key and "gate-status.json" in key:
        return _retry_history_source_evidence(
            run,
            repo_root,
            retry_rows=retry_rows,
        )

    if "github api" in key or "curl-based pattern" in key:
        if pr_number is None:
            return (
                False,
                "no PR found, so the GitHub API comment path was not exercised",
            )
        return (
            True,
            f"PR #{pr_number} exists; comment delivery uses the curl-based GitHub issues comments API path",
        )

    if "if no pr exists" in key or "written to the run log" in key or "printed to stdout" in key:
        if pr_number is None:
            return True, "no PR found; summary is printed to stdout and logged locally"
        return (
            False,
            f"PR #{pr_number} exists, so the PR-comment path applies instead",
        )

    evidence_available = any(int(gates[gate].get("attempts", 0) or 0) > 0 for gate in REQUIRED_GATES) or bool(
        run.review_decision_status
    )
    if evidence_available and has_implementation_evidence:
        return True, f"{implementation_evidence}; {delivery_evidence}"
    if evidence_available:
        return False, f"{implementation_evidence}; {delivery_evidence}"
    return False, "no preserved automation evidence was available for this item"


def _acceptance_checklist_with_evidence(
    run: RunState,
    repo_root: Path,
    *,
    pr_number: int | None = None,
) -> list[str]:
    spec_path = _active_spec_path(repo_root, run, prefer_worktree=False)
    if spec_path is None or not spec_path.exists():
        return ["- [ ] Spec file is unavailable for this run."]

    checklist_items = _extract_acceptance_checklist_items(spec_path)
    if not checklist_items:
        return ["- [ ] No checklist items found in spec."]

    gates = _normalized_required_gate_status(repo_root, run)
    retry_rows = _build_retry_history_rows(
        run,
        repo_root,
        terminal_reason="unknown",
    )
    lines: list[str] = []
    for item in checklist_items:
        delivered, evidence = _acceptance_item_evidence(
            item,
            run,
            repo_root,
            pr_number=pr_number,
            gates=gates,
            retry_rows=retry_rows,
        )
        mark = "x" if delivered else " "
        lines.append(f"- [{mark}] {_normalize_acceptance_checklist_item(item)} (evidence: {evidence})")
    return lines


def _gate_history_by_attempt(
    gates: dict[str, dict[str, object]],
) -> tuple[dict[int, dict[str, dict[str, object]]], dict[str, list[dict[str, object]]]]:
    attempts: dict[int, dict[str, dict[str, object]]] = {}
    history_by_gate: dict[str, list[dict[str, object]]] = {}

    for gate in REQUIRED_GATES:
        gate_entry = gates.get(gate, {})
        history = gate_entry.get("history", [])
        normalized_history: list[dict[str, object]] = []
        if isinstance(history, list):
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                attempt = int(entry.get("attempt", 0) or 0)
                if attempt <= 0:
                    continue
                normalized_entry = dict(entry)
                normalized_entry["attempt"] = attempt
                normalized_entry["status"] = str(entry.get("status", "") or "").strip()
                normalized_history.append(normalized_entry)
        if not normalized_history and int(gate_entry.get("attempts", 0) or 0) > 0:
            normalized_history.append(
                {
                    "attempt": int(gate_entry.get("attempts", 0) or 0),
                    "status": str(gate_entry.get("last_status", "") or "").strip(),
                    "failed_count": None,
                    "passed_count": None,
                    "error_count": None,
                }
            )
        normalized_history.sort(key=lambda item: int(item["attempt"]))
        history_by_gate[gate] = normalized_history
        for entry in normalized_history:
            attempts.setdefault(int(entry["attempt"]), {})[gate] = entry

    return attempts, history_by_gate


def _describe_gate_retry_fix(
    gate_name: str,
    *,
    history: list[dict[str, object]],
    attempt_number: int,
) -> str:
    current = next(
        (entry for entry in history if int(entry.get("attempt", 0) or 0) == attempt_number),
        None,
    )
    if current is None:
        return "No later gate evidence was recorded."

    later = next(
        (entry for entry in history if int(entry.get("attempt", 0) or 0) > attempt_number),
        None,
    )
    if later is None:
        return "No later gate result was recorded after this failure."

    later_attempt = int(later.get("attempt", 0) or 0)
    later_status = str(later.get("status", "") or "").strip()
    current_failed = current.get("failed_count")
    later_failed = later.get("failed_count")
    if later_status == "passed":
        if isinstance(current_failed, int) and current_failed > 0:
            return (
                f"`make {gate_name}` passed on attempt {later_attempt} after "
                f"{current_failed} failing tests on this run."
            )
        return f"`make {gate_name}` passed on attempt {later_attempt}."
    if isinstance(current_failed, int) and isinstance(later_failed, int) and current_failed != later_failed:
        direction = "down" if later_failed < current_failed else "up"
        return (
            f"`make {gate_name}` still failed on attempt {later_attempt}, but the "
            f"failure count moved {direction} from {current_failed} to {later_failed}."
        )
    return f"`make {gate_name}` still failed on attempt {later_attempt}."


def _retry_reason_label(reason_key: str) -> str:
    return {
        "implement_failures": "Implement phase failed before handoff",
        "review_changes": "Review requested changes",
        "merge_conflicts": "Merge conflict with `master`",
        "verify_failures": "Required checks failed after publish",
        "unknown": "Retry budget consumed without preserved failure detail",
    }.get(reason_key, RETRY_REASON_LABELS.get(reason_key, reason_key.replace("_", " ")).strip())


def _synthetic_retry_fix_summary(
    run: RunState,
    *,
    reason_key: str,
    attempt_number: int,
    total_attempts: int,
    review_result: ReviewResult | None,
) -> str:
    final_attempt = attempt_number >= total_attempts
    if reason_key == "implement_failures":
        return (
            "No later successful implement handoff was recorded."
            if final_attempt
            else "A later implement attempt did report completion, but the run failed again afterwards."
        )
    if reason_key == "review_changes":
        if final_attempt:
            return (
                review_result.summary
                if review_result is not None and review_result.summary
                else "The latest saved review result still requests changes."
            )
        return "A later attempt moved past this review cycle, but another failure remained."
    if reason_key == "merge_conflicts":
        if final_attempt:
            return run.merge_conflict_error or "Base-branch drift remained unresolved at escalation time."
        return "A later attempt rebased past this conflict, but another failure remained."
    if reason_key == "verify_failures":
        if final_attempt:
            return run.last_error or "The latest attempt still failed required checks."
        return "A later attempt got further, but required checks still failed later in the loop."
    return "No more specific fix evidence was preserved for this attempt."


def _build_retry_history_rows(
    run: RunState,
    repo_root: Path,
    *,
    terminal_reason: str,
) -> list[dict[str, object]]:
    gates = _normalized_required_gate_status(repo_root, run)
    attempts, history_by_gate = _gate_history_by_attempt(gates)
    total_attempts = max(
        1,
        run.attempts + 1,
        max(attempts.keys(), default=0),
    )
    rows: dict[int, dict[str, object]] = {}

    for attempt_number in sorted(attempts):
        failed_gate = next(
            (gate for gate in REQUIRED_GATES if attempts[attempt_number].get(gate, {}).get("status") == "failed"),
            "",
        )
        if not failed_gate:
            continue
        rows[attempt_number] = {
            "attempt": attempt_number,
            "reason": f"Verify failure: `make {failed_gate}`",
            "what_was_fixed": _describe_gate_retry_fix(
                failed_gate,
                history=history_by_gate.get(failed_gate, []),
                attempt_number=attempt_number,
            ),
        }

    unresolved_attempts = [
        attempt_number for attempt_number in range(1, total_attempts + 1) if attempt_number not in rows
    ]
    review_result = ReviewResult.load(repo_root, run.run_id)
    verify_rows_before_final = sum(1 for attempt_number in rows if attempt_number < total_attempts)
    pending_counts = {
        "implement_failures": max(0, int(run.implement_failures)),
        "review_changes": max(0, int(run.review_changes)),
        "merge_conflicts": max(0, int(run.merge_conflicts)),
        "verify_failures": max(0, int(run.verify_failures) - verify_rows_before_final),
    }

    if unresolved_attempts and total_attempts in unresolved_attempts:
        latest_attempt = unresolved_attempts.pop()
        rows[latest_attempt] = {
            "attempt": latest_attempt,
            "reason": _retry_reason_label(terminal_reason),
            "what_was_fixed": _synthetic_retry_fix_summary(
                run,
                reason_key=terminal_reason,
                attempt_number=latest_attempt,
                total_attempts=total_attempts,
                review_result=review_result,
            ),
        }
        if pending_counts.get(terminal_reason, 0) > 0:
            pending_counts[terminal_reason] -= 1

    for reason_key in (
        "implement_failures",
        "review_changes",
        "merge_conflicts",
        "verify_failures",
    ):
        while pending_counts.get(reason_key, 0) > 0 and unresolved_attempts:
            attempt_number = unresolved_attempts.pop(0)
            rows[attempt_number] = {
                "attempt": attempt_number,
                "reason": _retry_reason_label(reason_key),
                "what_was_fixed": _synthetic_retry_fix_summary(
                    run,
                    reason_key=reason_key,
                    attempt_number=attempt_number,
                    total_attempts=total_attempts,
                    review_result=review_result,
                ),
            }
            pending_counts[reason_key] -= 1

    while unresolved_attempts:
        attempt_number = unresolved_attempts.pop(0)
        rows[attempt_number] = {
            "attempt": attempt_number,
            "reason": _retry_reason_label("unknown"),
            "what_was_fixed": _synthetic_retry_fix_summary(
                run,
                reason_key="unknown",
                attempt_number=attempt_number,
                total_attempts=total_attempts,
                review_result=review_result,
            ),
        }

    return [rows[attempt_number] for attempt_number in sorted(rows)]


def _render_retry_history_markdown(rows: list[dict[str, object]]) -> str:
    header = [
        "| Attempt | Reason | What was fixed |",
        "| --- | --- | --- |",
    ]
    body = [
        (
            f"| {int(row.get('attempt', 0) or 0)} | "
            + str(row.get("reason", "") or "").replace("|", r"\|")
            + " | "
            + str(row.get("what_was_fixed", "") or "").replace("|", r"\|")
            + " |"
        )
        for row in rows
    ]
    rendered = "\n".join(header + body)
    if len(rendered) <= RETRY_CAP_ESCALATION_HISTORY_CHAR_LIMIT:
        return rendered

    trimmed = list(header)
    omitted = 0
    for line in body:
        candidate_lines = trimmed + [line]
        if omitted:
            candidate_lines.append(
                f"| ... | ... | Omitted {omitted} additional rows to stay within the GitHub comment limit. |"
            )
        candidate = "\n".join(candidate_lines)
        if len(candidate) > RETRY_CAP_ESCALATION_HISTORY_CHAR_LIMIT:
            omitted += 1
            continue
        trimmed.append(line)
    if omitted:
        trimmed.append(f"| ... | ... | Omitted {omitted} additional rows to stay within the GitHub comment limit. |")
    return "\n".join(trimmed)


def _infer_retry_cap_root_cause(
    run: RunState,
    repo_root: Path,
) -> str:
    gates = _normalized_required_gate_status(repo_root, run)
    gate_failures = {
        gate: sum(
            1 for entry in history.get("history", []) if isinstance(entry, dict) and entry.get("status") == "failed"
        )
        for gate, history in gates.items()
    }
    dominant_gate, dominant_gate_failures = max(
        gate_failures.items(),
        key=lambda item: item[1],
        default=("", 0),
    )
    if dominant_gate and dominant_gate_failures >= 2:
        return (
            f"Hypothesis: the run kept returning to `make {dominant_gate}` and failed "
            f"there {dominant_gate_failures} times, which suggests the underlying defect "
            "was only partially fixed between retries."
        )

    if run.review_changes > 0 or run.review_decision_status == "request_changes":
        review_result = ReviewResult.load(repo_root, run.run_id)
        detail = (
            review_result.summary
            if review_result is not None and review_result.summary
            else "unresolved review findings"
        )
        return (
            "Hypothesis: local verification was good enough to reach review, but "
            f"{detail} kept reopening the loop and exhausted the budget."
        )

    if run.merge_conflicts > 0 or run.merge_conflict_error:
        return (
            "Hypothesis: base-branch drift outpaced the retry loop, so conflict "
            "resolution never stayed current long enough to land."
        )

    if run.implement_failures > 0:
        return (
            "Hypothesis: implement-phase stability or handshake failures consumed "
            "multiple retries before the code path itself could stabilize."
        )

    if dominant_gate:
        return (
            f"Hypothesis: `{dominant_gate}` remained the main validation chokepoint "
            "and the loop exhausted itself on that same gate."
        )

    return (
        "Hypothesis: the workflow kept making partial progress, but the preserved "
        "evidence does not isolate a single dominant failure mode."
    )


def _render_attempt_budget_breakdown(run: RunState) -> str:
    """Say how the budget was spent, so the cap does not read as "this is broken".

    "Retry cap reached (10/10)" invites the reading that the spec could not be
    made to work. When most of those attempts went to merge conflicts, the honest
    reading is that the spec was unlucky about scheduling.
    """
    rows = [
        ("Implement failures", int(run.implement_failures)),
        ("Verify failures", int(run.verify_failures)),
        ("Review changes", int(run.review_changes)),
    ]
    lines = [
        f"- {label}: {count}" for label, count in rows if count
    ]
    conflicts = int(run.merge_conflicts)
    if conflicts:
        lines.append(
            f"- Merge conflicts: {conflicts}/{MERGE_CONFLICT_RETRY_CAP} "
            "(separate budget — caused by other branches landing, not by this run)"
        )
    if not lines:
        lines.append("- No retries were charged.")
    lines.append(
        f"- Counted against the shared cap: {_convergence_attempts(run)}/{run.retry_cap} "
        f"(of {int(run.attempts)} attempts total)"
    )
    return "\n".join(lines)


def _render_retry_cap_escalation_summary(
    run: RunState,
    repo_root: Path,
    *,
    terminal_reason: str,
    pr_number: int | None,
) -> str:
    checklist_lines = _acceptance_checklist_with_evidence(
        run,
        repo_root,
        pr_number=pr_number,
    )
    retry_history = _render_retry_history_markdown(
        _build_retry_history_rows(
            run,
            repo_root,
            terminal_reason=terminal_reason,
        )
    )
    summary_path = _retry_cap_escalation_summary_path(repo_root, run)
    recommended_action = [
        f"1. Resume the same run with `spec phase --spec {run.spec_id} --phase implement`.",
        (
            f"2. Inspect `{_run_state_dir_for_run(run.run_id)}gate-status.json` and "
            "`review-result.json` if present before changing code."
        ),
    ]
    if pr_number is not None:
        recommended_action.append(
            f"3. Review PR #{pr_number} together with `{summary_path.relative_to(_state_root(repo_root)).as_posix()}`."
        )
        if run.review_decision_status == "request_changes":
            recommended_action.append(f"4. Fetch the full review payload with `spec review --pr {pr_number}`.")

    lines = [
        RETRY_CAP_ESCALATION_COMMENT_MARKER,
        f"## Retry Cap Escalation for `{run.spec_id}`",
        "",
        (
            "Automation evidence only: checklist status is derived from the last "
            "available gate/review state, not from semantic human validation."
        ),
        "",
        "## What was delivered",
        *checklist_lines,
        "",
        "## Where the attempts went",
        _render_attempt_budget_breakdown(run),
        "",
        "## Retry history",
        retry_history,
        "",
        "## Root cause",
        _infer_retry_cap_root_cause(run, repo_root),
        "",
        "## Recommended action",
        *recommended_action,
    ]
    return "\n".join(lines).rstrip() + "\n"


def _post_retry_cap_escalation_comment(
    repo_root: Path,
    *,
    repo_name: str,
    pr_number: int,
    body: str,
) -> str:
    token = _forge().get_auth_token()
    if not token:
        # Fallback: check env vars and gh auth token via run_subprocess
        for env_var in ("GH_TOKEN", "GITHUB_TOKEN"):
            token = os.environ.get(env_var, "").strip()
            if token:
                break
        if not token:
            token_result = run_subprocess(["gh", "auth", "token"], cwd=repo_root)
            token = token_result.stdout.strip()
    if not token:
        return "Could not read GitHub auth token for retry-cap escalation comment: no token available"

    comment_url = (
        f"https://api.github.com/repos/{urllib_parse.quote(repo_name, safe='/')}/issues/{int(pr_number)}/comments"
    )
    publish_result = run_subprocess(
        [
            "curl",
            "-fsS",
            "-X",
            "POST",
            "-H",
            f"Authorization: token {token}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            comment_url,
            "-d",
            json.dumps({"body": body}),
        ],
        cwd=repo_root,
    )
    if publish_result.returncode == 0:
        return ""
    return (
        publish_result.stderr.strip()
        or publish_result.stdout.strip()
        or "curl failed while posting retry-cap escalation comment"
    )


def _emit_retry_cap_escalation_summary(
    run: RunState,
    repo_root: Path,
    *,
    terminal_reason: str,
) -> None:
    _normalize_logger_state()
    pr_data = _find_pr_for_branch(repo_root, run.branch, state="all")
    pr_number = pr_data.get("number") if isinstance(pr_data, dict) else None
    if not isinstance(pr_number, int):
        pr_number = None

    summary = _render_retry_cap_escalation_summary(
        run,
        repo_root,
        terminal_reason=terminal_reason,
        pr_number=pr_number,
    )
    summary_path = _retry_cap_escalation_summary_path(repo_root, run)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary)

    if pr_number is not None:
        try:
            repo_name = _repo_name_with_owner(repo_root)
        except ValueError as exc:
            logger.warning(
                "Retry-cap escalation comment skipped for %s: %s",
                run.spec_id,
                exc,
            )
            print(summary)
            return

        publish_error = _post_retry_cap_escalation_comment(
            repo_root,
            repo_name=repo_name,
            pr_number=pr_number,
            body=summary,
        )
        if not publish_error:
            logger.info(
                "Posted retry-cap escalation comment to PR #%s for %s",
                pr_number,
                run.spec_id,
            )
            return

        logger.warning(
            "Retry-cap escalation comment failed for PR #%s (%s): %s",
            pr_number,
            run.spec_id,
            publish_error,
        )

    logger.warning(
        "Retry-cap escalation summary for %s:\n%s",
        run.spec_id,
        summary,
    )
    print(summary)


def _should_publish_retry_cap_verify_escalation(
    run: RunState,
    repo_root: Path,
) -> bool:
    if run.verify_passed_once:
        return True
    return _find_pr_for_branch(repo_root, run.branch, state="all") is not None


def _apply_review_result(
    run: RunState,
    repo_root: Path,
    review_result: ReviewResult,
    *,
    conclusion: str,
    fallback_check_url: str = "",
) -> str:
    review_result.attempt_number = _current_attempt_number(run)
    review_result.save(repo_root, run.run_id)
    run.review_decision_status = review_result.status
    run.review_decision_summary = review_result.summary
    check_name = review_result.source_check_name or REVIEW_GATE_CHECK_NAME
    check_url = review_result.source_check_url or fallback_check_url
    finding_count = len(review_result.findings)
    finding_summary = _summarize_review_findings(review_result.findings)
    run.review_decision_check_url = check_url

    if conclusion == "success":
        if review_result.status != "approved":
            run.last_error = (
                f"Review gate {check_name} succeeded but payload is not approved "
                f"(conclusion={conclusion}, url={check_url or 'n/a'}): "
                f"{review_result.status}. Key findings: {finding_summary}"
            )
            return "failed"
        return "passed"

    if review_result.status == "request_changes":
        run.last_error = (
            "Independent review requested changes via "
            f"{check_name} (conclusion={conclusion}, url={check_url or 'n/a'}, "
            f"findings={finding_count}): {review_result.summary}. "
            f"Key findings: {finding_summary}"
        )
        return "failed"

    run.last_error = (
        f"Independent review status '{review_result.status}' via {check_name} "
        f"(conclusion={conclusion}, url={check_url or 'n/a'}, findings={finding_count}): "
        f"{review_result.summary}. Key findings: {finding_summary}"
    )
    return "failed"


def _bootstrap_review_worktree(
    repo_root: Path,
    review_worktree: Path,
    *,
    warning_path: Path,
) -> str:
    """Install the project in the review worktree so reviewers can run tests.

    The review worktree is checked out at the PR head, so its ``.spec.toml``
    (and anything it references) is attacker-controlled. The bootstrap command
    is therefore sourced from trusted configuration — the orchestrator host's
    own ``SPEC_RUNTIME_CONFIG`` (loaded from the base checkout), never the tree
    under review — and it runs with review-scoped credentials stripped (see
    ``_build_local_review_env``). Even so, ``pip install -e .`` executes the PR
    head's build hooks, so this is treated as untrusted code execution.

    Stripping named credential *env vars* (forge tokens, agent auth keys) is
    not sufficient on its own: build hooks run as the same OS user with the
    same ``$HOME``, so they can read credential files straight off disk
    (``~/.ssh``, ``~/.codex/auth.json``, ``~/.aws/credentials``, ``~/.netrc``,
    ``~/.config/gh``, ...) regardless of which env vars are set. So the
    bootstrap subprocess additionally gets an isolated, empty ``HOME`` (and
    the credential-path overrides that could point back at the real one) for
    the duration of the install command, then the temp dir is removed.

    Bootstrap is best-effort: on failure (or timeout) the failure is recorded as
    a review-environment warning and the empty string is returned so review
    still completes diff-only rather than blocking. Returns a non-empty warning
    summary when bootstrap did not complete cleanly.
    """
    warning_path.unlink(missing_ok=True)
    install_command = _selected_bootstrap_install_command()
    if install_command is None:
        # No trusted bootstrap command configured: keep today's diff-only
        # review rather than guessing an install command from the tree under
        # review.
        return ""
    if install_command.mode == "script" and (install_command.shell or "sh") == "sh":
        # Review bootstrap historically used sh -lc. Keep that established
        # POSIX behavior while routing the launch through the typed runner.
        install_command = replace(install_command, login_shell=True)
    install_display = install_command.display()
    if SPEC_RUNTIME_CONFIG.bootstrap_install.select() is None:
        install_display = str(SPEC_RUNTIME_CONFIG.bootstrap_install_command).strip()

    # Credentials stripped (no forge tokens) — reuse the hardened env the
    # reviewer subprocess runs with, then additionally drop the portable agent
    # auth keys. The reviewer subprocess legitimately keeps those (it launches
    # an agent), but the bootstrap runs the PR head's untrusted build hooks
    # (``pip install -e .``), so it must not inherit any agent auth.
    env = _build_local_review_env()
    for key in _CLAUDE_PORTABLE_AUTH_ENV_KEYS:
        env.pop(key, None)

    isolated_home = tempfile.mkdtemp(prefix="spec-review-bootstrap-home-")
    env["HOME"] = isolated_home
    if os.name == "nt":
        # Windows-native tools commonly ignore HOME and resolve credentials
        # through USERPROFILE / APPDATA instead.  Point every standard profile
        # root at the same disposable directory so an untrusted build backend
        # cannot discover the operator's real profile through platform APIs.
        isolated_profile = Path(isolated_home)
        roaming = isolated_profile / "AppData" / "Roaming"
        local = isolated_profile / "AppData" / "Local"
        roaming.mkdir(parents=True, exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        drive, tail = os.path.splitdrive(str(isolated_profile))
        env["USERPROFILE"] = str(isolated_profile)
        env["APPDATA"] = str(roaming)
        env["LOCALAPPDATA"] = str(local)
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = tail or "\\"
        else:
            env.pop("HOMEDRIVE", None)
            env.pop("HOMEPATH", None)
    for key in (
        "CODEX_HOME",
        "NETRC",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "DOCKER_CONFIG",
        "KUBECONFIG",
    ):
        env.pop(key, None)

    def _record_warning(summary: str, detail: str) -> str:
        payload = {
            "recorded_at": _now_iso(),
            "worktree": str(review_worktree),
            "command": install_display,
            "summary": summary,
            "detail": detail[-4000:],
        }
        try:
            warning_path.parent.mkdir(parents=True, exist_ok=True)
            warning_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except OSError:
            pass
        logger.warning(
            "Review worktree bootstrap did not complete cleanly for %s: %s",
            review_worktree,
            summary,
        )
        return summary

    # The install command is untrusted PR-head code (build hooks can fork
    # children that outlive the shell, e.g. a detached PEP 517 build
    # backend). Run it in its own process group and, on timeout, kill the
    # whole group — killing only the ``sh`` process (as a plain
    # ``subprocess.run(..., timeout=...)`` would) leaves those descendants
    # holding the stdout/stderr pipes open, so draining them to EOF after
    # the kill blocks forever and defeats the timeout entirely.
    try:
        try:
            launch_argv = install_command.launch_argv(cwd=review_worktree)
            with launch_argv as argv:
                proc = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
                    argv,
                    cwd=review_worktree,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    stdout_text, stderr_text = proc.communicate(
                        timeout=REVIEW_BOOTSTRAP_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    _terminate_agent_process(proc)
                    return _record_warning(
                        "Review worktree bootstrap timed out; reviewer may not be able to run "
                        "tests (falling back to diff-only review).",
                        f"Timed out after {REVIEW_BOOTSTRAP_TIMEOUT_SECONDS:.0f}s",
                    )
        except OSError as exc:
            return _record_warning(
                "Review worktree bootstrap could not be launched; reviewer may not be "
                "able to run tests (falling back to diff-only review).",
                str(exc),
            )
        if proc.returncode != 0:
            detail = (stderr_text or "").strip() or (stdout_text or "").strip()
            return _record_warning(
                "Review worktree bootstrap failed; reviewer may not be able to run "
                "tests (falling back to diff-only review).",
                detail or f"exit_code={proc.returncode}",
            )
        return ""
    finally:
        remove_tree(isolated_home, ignore_errors=True)


def _review_env_prompt_note(
    review_worktree: Path,
    *,
    windows: bool | None = None,
) -> str:
    """Return a prompt suffix telling the reviewer how to run the bootstrapped gates.

    Putting the worktree venv on the reviewer subprocess ``PATH`` (see
    ``_inject_worktree_venv_into_env``) is necessary but not sufficient: the
    reviewer agent runs gate commands through a shell, and a login shell can
    re-source ``/etc/profile`` and reset ``PATH`` — dropping ``.venv/bin`` so
    bare ``pytest`` / ``ruff`` still resolve to nothing (the exact
    ``pytest: command not found`` symptom this spec targets). So when the
    bootstrap actually produced a venv, tell the reviewer it exists and to
    invoke gate tools by absolute path (``.venv/bin/pytest``), which survives
    any PATH reset. When there is no venv (bootstrap skipped or failed), return
    an empty string so the prompt does not falsely promise a runnable project.
    """
    use_windows = os.name == "nt" if windows is None else windows
    venv_bin = _worktree_venv_executable_dir(
        review_worktree,
        windows=use_windows,
    )
    if not venv_bin.is_dir():
        return ""
    if use_windows:
        tool_examples = (
            r"`.venv\Scripts\python.exe -m pytest` and "
            r"`.venv\Scripts\python.exe -m ruff check .`"
        )
        executable_dir_name = "Scripts"
    else:
        tool_examples = "`.venv/bin/pytest` and `.venv/bin/ruff check .`"
        executable_dir_name = "bin"
    return (
        "\n\nReview environment: the project under review has been installed "
        "into a local virtualenv at `.venv` in this worktree. Its "
        f"`{executable_dir_name}` is on "
        "PATH, but because some shells reset PATH, prefer invoking gate tools by "
        f"an explicit virtualenv path — e.g. {tool_examples} — "
        "to actually exercise the changed behavior. If a gate command still "
        "cannot be found or run, note that and continue with diff-only review "
        "rather than returning `blocked`."
    )


def _run_local_review(
    run: RunState,
    repo_root: Path,
    *,
    repo_name: str,
    pr_number: int,
    pr_body: str,
    expected_head_sha: str,
    expected_base_sha: str,
) -> tuple[ReviewResult, Path]:
    _normalize_logger_state()
    artifact_paths = _local_review_artifact_paths(repo_root, run.run_id)
    _clear_local_review_artifacts(artifact_paths)
    review_agent = _effective_review_agent(run)
    reasoning_effort = (
        LOCAL_REVIEW_FIRST_PASS_REASONING_EFFORT if run.review_changes == 0 else LOCAL_REVIEW_REASONING_EFFORT
    )
    logger.info(
        "Running local review for %s with reasoning effort=%s (review_changes=%d)",
        run.branch,
        reasoning_effort,
        run.review_changes,
    )
    _emit_user_progress(f"[spec] {run.spec_id}: review running with {review_agent} for PR #{pr_number}")
    prompt = _render_local_review_prompt(
        repo_root,
        repo_name=repo_name,
        pr_number=pr_number,
        base_sha=expected_base_sha,
        head_sha=expected_head_sha,
        head_ref=run.branch,
        pr_body=pr_body,
        review_changes=run.review_changes,
        gate_evidence=_format_gate_evidence_for_review(repo_root, run),
    )
    artifact_paths["prompt"].write_text(prompt)

    # Resolve schema: repo-level override, then bundled
    schema_path = repo_root / ".github" / "schemas" / "codex-review.schema.json"
    if not schema_path.is_file():
        import importlib.resources

        bundled = importlib.resources.files("spec_runtime") / "templates" / "review-schema.json"
        schema_path = Path(str(bundled))

    # Determine review agent
    from .agent_adapter import get_agent_adapter

    agent = get_agent_adapter(review_agent)

    # For agents that write to stdout (no --output-schema), embed the
    # expected JSON structure in the prompt so the agent knows the format.
    if agent.capabilities.review_output_on_stdout and schema_path.is_file():
        schema_text = schema_path.read_text().strip()
        prompt = (
            f"{prompt}\n\n"
            "You MUST output a single JSON object (no markdown wrapping) matching this schema:\n"
            f"```json\n{schema_text}\n```"
        )

    with _temporary_review_worktree(
        repo_root, head_sha=expected_head_sha, branch=run.branch
    ) as review_worktree:
        review_exec_failed_summary = ""
        review_mcp_config_path: Path | None = None
        review_codex_home: Path | None = None
        # Bootstrap the review worktree (trusted host config, credentials
        # stripped) so the reviewer can run targeted tests. Best-effort: a
        # failure is surfaced as a review-environment warning rather than
        # blocking the review, which continues diff-only.
        bootstrap_warning = _bootstrap_review_worktree(
            repo_root,
            review_worktree,
            warning_path=artifact_paths["bootstrap_warning"],
        )
        if bootstrap_warning:
            _emit_user_progress(
                f"[spec] {run.spec_id}: review environment warning: {bootstrap_warning}"
            )
        # When the bootstrap produced a runnable venv, tell the reviewer it is
        # there and how to invoke it by absolute path (PATH injection alone is
        # not enough if the agent's shell resets PATH). Recorded to the prompt
        # artifact so the effective prompt is reproducible.
        env_note = _review_env_prompt_note(review_worktree)
        if env_note:
            prompt = f"{prompt}{env_note}"
            artifact_paths["prompt"].write_text(prompt)
        if agent.capabilities.supports_mcp:
            # The reviewer subprocess is launched on the host via
            # ``_run_local_review_subprocess``, never through the container
            # backend. Build the MCP config with host paths so the review
            # agent can actually exec the MCP commands; container path
            # translation here would point at /workspace/source paths that
            # do not exist on the host reviewer.
            if agent.name == "claude":
                _write_claude_mcp_config(review_worktree, host_subprocess=True)
                review_mcp_config_path = _mcp_config_path(review_worktree)
            elif agent.name == "codex":
                isolated_servers = _compute_non_interactive_mcp_servers(
                    review_worktree,
                    agent_name="codex",
                    host_subprocess=True,
                )
                review_codex_home = _write_codex_isolated_home(
                    review_worktree,
                    mcp_servers=isolated_servers,
                    copy_auth=_codex_isolated_home_requires_auth_copy(
                        _resolve_execution_backend()
                    ),
                )
        review_cmd = _build_local_review_command(
            prompt=prompt,
            schema_path=schema_path,
            output_path=artifact_paths["raw_review"],
            agent_name=review_agent,
            reasoning_effort=reasoning_effort,
            mcp_config_path=review_mcp_config_path,
        )
        review_env = _build_local_review_env()
        # Put the review worktree's venv on the reviewer's PATH so bootstrap
        # actually pays off: the reviewer can run bare ``pytest`` / ``ruff``
        # against the installed project instead of falling back to diff-only.
        # A missing ``.venv/bin`` (bootstrap skipped or failed) is harmless.
        _inject_worktree_venv_into_env(review_env, review_worktree)
        if review_codex_home is not None:
            review_env = _subprocess_env_with_codex_home(review_env, review_codex_home)
        try:
            review_exec = _run_local_review_subprocess(
                repo_root,
                review_cmd,
                cwd=review_worktree,
                env=review_env,
                timeout=REVIEW_TIMEOUT_SECONDS,
                artifact_paths=artifact_paths,
            )
        except subprocess.TimeoutExpired as exc:
            review_exec_failed_summary = _summarize_local_review_timeout(
                repo_root,
                artifact_paths,
                review_agent=review_agent,
                cmd=review_cmd,
                cwd=review_worktree,
                timeout_exc=exc,
            )
        except FileNotFoundError as exc:
            review_exec_failed_summary = f"Review agent CLI not found: {exc}"
        else:
            # If agent writes to stdout, capture it to the artifact file
            if agent.capabilities.review_output_on_stdout:
                stdout_text = _coerce_subprocess_stream_text(review_exec.stdout).strip()
                if stdout_text:
                    # Extract JSON object from stdout — agent may emit
                    # preamble text before/after the JSON.
                    json_text = stdout_text
                    brace_start = stdout_text.find("{")
                    brace_end = stdout_text.rfind("}")
                    if brace_start >= 0 and brace_end > brace_start:
                        json_text = stdout_text[brace_start : brace_end + 1]
                    artifact_paths["raw_review"].parent.mkdir(parents=True, exist_ok=True)
                    artifact_paths["raw_review"].write_text(json_text)

            if review_exec.returncode != 0:
                detail = _format_subprocess_failure(review_exec)
                review_exec_failed_summary = f"Local review agent failed (exit_code={review_exec.returncode})"
                if detail:
                    review_exec_failed_summary = f"{review_exec_failed_summary}: {detail}"

        if review_exec_failed_summary:
            _write_failed_local_review_payload(
                artifact_paths["raw_review"],
                summary=review_exec_failed_summary,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
                reviewer_agent=review_agent,
            )
        elif not artifact_paths["raw_review"].is_file() or not artifact_paths["raw_review"].read_text().strip():
            _write_failed_local_review_payload(
                artifact_paths["raw_review"],
                summary="Missing review output artifact.",
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
                reviewer_agent=review_agent,
            )

    # Evaluate review gate in-process (no external script dependency)
    from .review_gate import evaluate_review_gate

    evaluation = evaluate_review_gate(
        input_path=artifact_paths["raw_review"],
        schema_path=schema_path,
        expected_head_sha=expected_head_sha,
        expected_base_sha=expected_base_sha,
        check_name=REVIEW_GATE_CHECK_NAME,
    )
    artifact_paths["review_result"].parent.mkdir(parents=True, exist_ok=True)
    artifact_paths["review_result"].write_text(json.dumps(evaluation.result_payload, indent=2, sort_keys=True) + "\n")
    artifact_paths["summary"].parent.mkdir(parents=True, exist_ok=True)
    artifact_paths["summary"].write_text(json.dumps(evaluation.machine_summary, indent=2, sort_keys=True) + "\n")
    artifact_paths["job_summary"].parent.mkdir(parents=True, exist_ok=True)
    artifact_paths["job_summary"].write_text(evaluation.human_summary.rstrip() + "\n")

    if not artifact_paths["review_result"].is_file():
        raise ValueError("Local review gate did not produce review output")

    review_result = _load_review_result_from_gate_output(artifact_paths["review_result"])
    _emit_user_progress(f"[spec] {run.spec_id}: review completed with {review_result.status}")
    return (
        review_result,
        artifact_paths["review_result"],
    )


def _phase_review_local(
    run: RunState,
    repo_root: Path,
    *,
    pr_data: dict,
    expected_head_sha: str,
    expected_base_sha: str,
) -> str:
    review_agent = _effective_review_agent(run)
    if not shutil.which(review_agent):
        run.last_error = (
            f"Review agent '{review_agent}' not found on PATH. Install it or configure a different agent in .spec.toml."
        )
        return "failed"

    pr_number = pr_data.get("number")
    if not isinstance(pr_number, int):
        run.last_error = f"Could not determine PR number for branch {run.branch}"
        return "failed"

    disable_error = _disable_pr_auto_merge(repo_root, pr_number)
    if disable_error:
        logger.warning(
            "Could not disable auto-merge for PR #%s before local review: %s",
            pr_number,
            disable_error,
        )

    try:
        repo_name = _repo_name_with_owner(repo_root)
        review_result, review_result_path = _run_local_review(
            run,
            repo_root,
            repo_name=repo_name,
            pr_number=pr_number,
            pr_body=str(pr_data.get("body", "")),
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )
    except ValueError as exc:
        run.last_error = str(exc)
        return "failed"

    latest_pr, observed_pr = _find_open_pr_for_branch(repo_root, run.branch)
    if latest_pr is None:
        run.last_error = _describe_non_open_pr_state(
            repo_root,
            run.branch,
            context="during local review",
            pr_data=observed_pr,
        )
        return "failed"
    if str(latest_pr.get("mergeStateStatus", "")).strip().upper() == "DIRTY":
        review_worktree = resolve_worktree_path(run, repo_root)
        if not _dirty_pr_state_allows_continue(
            run,
            repo_root,
            review_worktree,
            context="local review",
            pr_number=pr_number,
        ):
            return "failed"

    current_head_sha = str(latest_pr.get("headRefOid", "")).strip()
    if current_head_sha and current_head_sha != expected_head_sha:
        remote_head_sha = _remote_branch_head_sha(repo_root, run.branch)
        if not remote_head_sha or remote_head_sha != expected_head_sha:
            run.last_error = (
                "PR head changed during local review phase: "
                f"expected {_short_sha(expected_head_sha)}, got {_short_sha(current_head_sha)}"
            )
            return "failed"

    review_result.source_check_name = REVIEW_GATE_CHECK_NAME
    review_result.source_check_url = str(latest_pr.get("url", "")).strip()
    review_result.attempt_number = _current_attempt_number(run)
    review_result_path.write_text(json.dumps(asdict(review_result), indent=2, sort_keys=True) + "\n")
    review_result.save(repo_root, run.run_id)

    if _is_local_review_timeout_result(review_result):
        pending_error = ""
        try:
            check_url = _publish_pending_local_review_status(
                repo_root,
                repo_name=repo_name,
                expected_head_sha=expected_head_sha,
                target_url=review_result.source_check_url,
            )
        except ValueError as exc:
            pending_error = str(exc)
            logger.warning(
                "Could not refresh pending local review status for PR #%s: %s",
                pr_number,
                pending_error,
            )
        else:
            if check_url:
                review_result.source_check_url = check_url
                review_result_path.write_text(json.dumps(asdict(review_result), indent=2, sort_keys=True) + "\n")
                review_result.save(repo_root, run.run_id)

        run.review_decision_status = "blocked"
        run.review_decision_summary = review_result.summary
        run.review_decision_check_url = review_result.source_check_url
        run.last_error = review_result.summary
        if pending_error:
            run.last_error = f"{run.last_error} Pending status refresh failed: {pending_error}"
        return "blocked"

    sticky_error = _publish_review_gate_sticky_comment(
        repo_root,
        repo_name=repo_name,
        pr_number=pr_number,
        review_result_path=review_result_path,
    )
    if sticky_error:
        logger.warning(
            "Sticky review comment publication failed for PR #%s (non-blocking): %s",
            pr_number,
            sticky_error,
        )

    try:
        check_url = _publish_local_review_status(
            repo_root,
            repo_name=repo_name,
            expected_head_sha=expected_head_sha,
            review_result=review_result,
            target_url=review_result.source_check_url,
        )
    except ValueError as exc:
        run.last_error = str(exc)
        return "failed"

    if check_url:
        review_result.source_check_url = check_url
        review_result_path.write_text(json.dumps(asdict(review_result), indent=2, sort_keys=True) + "\n")

    conclusion = "success" if review_result.status == "approved" else "failure"
    return _apply_review_result(
        run,
        repo_root,
        review_result,
        conclusion=conclusion,
    )


def _phase_review_action(
    run: RunState,
    repo_root: Path,
    *,
    expected_head_sha: str,
    expected_base_sha: str,
) -> str:
    deadline = time.time() + REVIEW_TIMEOUT_SECONDS
    poll_interval = max(1, REVIEW_POLL_INTERVAL_SECONDS)
    last_state = "not_started"
    last_url = ""

    while time.time() <= deadline:
        latest_pr, observed_pr = _find_open_pr_for_branch(repo_root, run.branch)
        if latest_pr is None:
            run.last_error = _describe_non_open_pr_state(
                repo_root,
                run.branch,
                context="during review",
                pr_data=observed_pr,
            )
            return "failed"
        if str(latest_pr.get("mergeStateStatus", "")).strip().upper() == "DIRTY":
            review_worktree = resolve_worktree_path(run, repo_root)
            if not _dirty_pr_state_allows_continue(
                run,
                repo_root,
                review_worktree,
                context="review polling",
                pr_number=latest_pr.get("number") if isinstance(latest_pr.get("number"), int) else None,
            ):
                return "failed"

        current_head_sha = str(latest_pr.get("headRefOid", "")).strip()
        if current_head_sha and current_head_sha != expected_head_sha:
            remote_head_sha = _remote_branch_head_sha(repo_root, run.branch)
            if remote_head_sha and remote_head_sha == expected_head_sha:
                logger.info(
                    "Waiting for PR metadata sync for %s: expected %s, observed %s",
                    run.branch,
                    _short_sha(expected_head_sha),
                    _short_sha(current_head_sha),
                )
                last_state = "waiting_for_pr_head_sync"
                _poll_sleep(poll_interval)
                continue
            run.last_error = (
                "PR head changed during review phase: "
                f"expected {_short_sha(expected_head_sha)}, got {_short_sha(current_head_sha)}"
            )
            return "failed"

        check_run = _find_check_run_for_sha(
            repo_root,
            expected_head_sha,
            REVIEW_GATE_CHECK_NAME,
        )
        if check_run is None:
            last_state = "not_found"
        elif "__error__" in check_run:
            run.last_error = (
                f"Could not query {REVIEW_GATE_CHECK_NAME} check run: {check_run.get('__error__', 'unknown error')}"
            )
            return "failed"
        else:
            status = str(check_run.get("status", "unknown")).strip().lower()
            conclusion = str(check_run.get("conclusion", "")).strip().lower()
            last_state = status
            last_url = str(check_run.get("details_url", "")).strip()

            if status == "completed":
                require_payload = conclusion != "success"
                try:
                    review_result = _extract_review_result_from_check_run(
                        check_run,
                        repo_root=repo_root,
                        expected_head_sha=expected_head_sha,
                        expected_base_sha=expected_base_sha,
                        require_payload=require_payload,
                    )
                except ValueError as exc:
                    run.last_error = f"Review decision payload invalid for {REVIEW_GATE_CHECK_NAME}: {exc}"
                    return "failed"

                return _apply_review_result(
                    run,
                    repo_root,
                    review_result,
                    conclusion=conclusion,
                    fallback_check_url=last_url,
                )

        logger.info(
            "Waiting for %s on %s (state=%s)...",
            REVIEW_GATE_CHECK_NAME,
            _short_sha(expected_head_sha),
            last_state,
        )
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _poll_sleep(min(poll_interval, remaining))

    timeout_detail = f"last_state={last_state}"
    if last_url:
        timeout_detail = f"{timeout_detail} ({last_url})"
    run.last_error = (
        f"Timed out waiting for {REVIEW_GATE_CHECK_NAME} on {_short_sha(expected_head_sha)}: {timeout_detail}"
    )
    return "failed"


def phase_review(run: RunState, repo_root: Path) -> str:
    """Evaluate an independent review decision for the PR head SHA."""
    auth_error = _check_forge_auth()
    if auth_error:
        run.last_error = auth_error
        return "failed"

    pr_data, observed_pr = _find_open_pr_for_branch(repo_root, run.branch)
    if pr_data is None:
        run.last_error = _describe_non_open_pr_state(
            repo_root,
            run.branch,
            context="before review started",
            pr_data=observed_pr,
        )
        return "failed"
    worktree_path = resolve_worktree_path(run, repo_root)
    if str(pr_data.get("mergeStateStatus", "")).strip().upper() == "DIRTY":
        if not _dirty_pr_state_allows_continue(
            run,
            repo_root,
            worktree_path,
            context="review start",
            pr_number=pr_data.get("number") if isinstance(pr_data.get("number"), int) else None,
        ):
            return "failed"

    expected_head_sha = str(pr_data.get("headRefOid", "")).strip()
    expected_base_sha = str(pr_data.get("baseRefOid", "")).strip()
    if not expected_base_sha:
        expected_base_sha = _resolve_git_ref_sha(repo_root, BASE_REF)
    if not expected_head_sha:
        run.last_error = "Could not determine PR head SHA for review phase"
        return "failed"

    worktree_head_sha = ""
    if worktree_path.is_dir():
        local_head = _head_sha(worktree_path)
        if local_head:
            worktree_head_sha = local_head

    if worktree_head_sha and worktree_head_sha != expected_head_sha:
        logger.warning(
            "PR head appears stale for %s: gh=%s worktree=%s. Using worktree head.",
            run.branch,
            _short_sha(expected_head_sha),
            _short_sha(worktree_head_sha),
        )
        expected_head_sha = worktree_head_sha
    elif not worktree_head_sha:
        remote_head_sha = _remote_branch_head_sha(repo_root, run.branch)
        if remote_head_sha and remote_head_sha != expected_head_sha:
            logger.warning(
                "PR head appears stale for %s: gh=%s remote=%s. Using remote head.",
                run.branch,
                _short_sha(expected_head_sha),
                _short_sha(remote_head_sha),
            )
            expected_head_sha = remote_head_sha

    run.review_expected_head_sha = expected_head_sha
    run.review_decision_status = ""
    run.review_decision_summary = ""
    run.review_decision_check_url = ""
    run.save(repo_root)

    # Always use local review — no CI review-decision-gate dependency.
    return _phase_review_local(
        run,
        repo_root,
        pr_data=pr_data,
        expected_head_sha=expected_head_sha,
        expected_base_sha=expected_base_sha,
    )


def _try_auto_merge(
    run: RunState,
    repo_root: Path,
    pr_number: int,
    *,
    expected_head_sha: str = "",
) -> str:
    """Attempt to enable auto-merge on the PR and wait for it to land.

    Returns:
        "merged"      – PR was merged via auto-merge.
        "unavailable" – auto-merge unavailable before attempting merge.
        "unavailable_after_auto" – auto-merge command was attempted; retry manual
            flow with --auto enabled.
        "unavailable_after_auto_no_auto" – auto-merge command was attempted and
            reported unsupported; retry manual flow without --auto.
        "failed"      – auto-merge was enabled but the merge failed (run.last_error set).
    """
    logger.info("Attempting auto-merge on PR #%s...", pr_number)
    auto_merge = _forge().merge_pr(
        pr_number,
        method="squash",
        auto=True,
        expected_head_sha=expected_head_sha or None,
        cwd=repo_root,
    )
    if not auto_merge.ok:
        err = auto_merge.message
        err_lower = err.lower()
        # Draft PR error: cannot auto-merge a draft PR.
        if _is_draft_pr_error(err):
            run.last_error = f"PR #{pr_number} is a draft and cannot be merged"
            return "failed"
        # Repo capability errors: retry manual merge without --auto.
        if _is_auto_merge_capability_error(err) or ("auto-merge" in err_lower and "not allowed" in err_lower):
            logger.info("Auto-merge not available for this repo, falling back to manual merge")
            return "unavailable_after_auto_no_auto"
        # GraphQL mutation errors can be transient; back off and retry manually.
        if _is_auto_merge_graphql_retryable_error(err):
            logger.info(
                "Auto-merge GraphQL response was retryable for PR #%s; falling back to manual merge with --auto.",
                pr_number,
            )
            _poll_sleep(MERGE_CHECKS_POLL_INTERVAL_SECONDS)
            return "unavailable_after_auto"
        # Merge races should fall back to the manual retry path.
        if _is_retryable_merge_race_error(err):
            logger.info(
                "Auto-merge hit a merge race for PR #%s; falling back to manual merge retries.",
                pr_number,
            )
            _poll_sleep(MERGE_CHECKS_POLL_INTERVAL_SECONDS)
            return "unavailable_after_auto"
        # Any other error is a real failure.
        run.last_error = f"gh pr merge --auto failed: {err}"
        return "failed"

    logger.info("Auto-merge enabled on PR #%s, waiting for merge...", pr_number)
    deadline = time.time() + MERGE_CHECKS_TIMEOUT_SECONDS
    poll_interval = max(1, MERGE_CHECKS_POLL_INTERVAL_SECONDS)
    while True:
        pr_data = _find_pr_for_branch(repo_root, run.branch, state="all")
        if pr_data is None:
            return _fail_after_auto_merge_armed(
                run,
                repo_root,
                pr_number,
                f"PR for branch {run.branch} disappeared while waiting for auto-merge",
            )
        state = str(pr_data.get("state", "")).upper()
        if state == "MERGED":
            logger.info("PR #%s merged via auto-merge", pr_number)
            return "merged"
        if state == "CLOSED":
            return _fail_after_auto_merge_armed(
                run,
                repo_root,
                pr_number,
                f"PR #{pr_number} was closed while waiting for auto-merge",
            )
        merge_state = str(pr_data.get("mergeStateStatus", "")).upper()
        if merge_state == "DIRTY":
            worktree_path = resolve_worktree_path(run, repo_root)
            if not _dirty_pr_state_allows_continue(
                run,
                repo_root,
                worktree_path,
                context="auto-merge wait",
                pr_number=pr_number,
            ):
                return _fail_after_auto_merge_armed(
                    run,
                    repo_root,
                    pr_number,
                    run.last_error,
                )
        if merge_state == "BEHIND":
            disable_error = _disable_pr_auto_merge(repo_root, pr_number)
            if disable_error:
                logger.warning(
                    "Could not disable auto-merge for PR #%s before local branch sync: %s",
                    pr_number,
                    disable_error,
                )
            logger.info(
                "Auto-merge wait detected PR #%s is behind %s; falling back to manual branch sync.",
                pr_number,
                BASE_REF,
            )
            return "unavailable_after_auto_no_auto"
        # Check if required checks are genuinely failing (not just pending).
        # If so, surface the error so run_full_workflow can loop to implement.
        checks_ok, checks_error = _required_checks_green(repo_root, pr_number)
        if not checks_ok and _required_checks_still_pending(checks_error):
            restored, restore_error = _restore_approved_local_review_status_if_pending(
                run,
                repo_root,
                expected_head_sha=expected_head_sha,
                checks_error=checks_error,
            )
            if restore_error:
                return _fail_after_auto_merge_armed(
                    run,
                    repo_root,
                    pr_number,
                    restore_error,
                )
            if restored:
                checks_ok, checks_error = _required_checks_green(repo_root, pr_number)
        if not checks_ok and not _required_checks_still_pending(checks_error):
            checks_error = _augment_required_checks_error(repo_root, checks_error)
            return _fail_after_auto_merge_armed(
                run,
                repo_root,
                pr_number,
                f"Merge refused for PR #{pr_number}; {checks_error}",
            )
        remaining = deadline - time.time()
        if remaining <= 0:
            timeout_message = (
                f"Timed out waiting for auto-merge on PR #{pr_number} (mergeStateStatus={merge_state})"
            )
            # The loop has already returned for genuinely failing checks, so a
            # timeout can only mean checks are still pending OR the required
            # set looked green/ambiguous (fresh head where checks had not yet
            # registered) while GitHub still reports the PR BLOCKED on
            # expected checks. Both states resolve themselves once CI settles
            # and auto-merge stays armed, so classify every timeout as
            # pending-checks: run_full_workflow then retries the merge phase
            # (bounded by the merge retry cap) instead of failing a run whose
            # PR is about to merge itself while the head's rollup is PENDING.
            timeout_message += "; required checks still pending"
            return _fail_after_auto_merge_armed(
                run,
                repo_root,
                pr_number,
                timeout_message,
            )
        _poll_sleep(min(poll_interval, remaining))


def _manual_merge(
    run: RunState,
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    pr_number: int,
    pr_head_sha: str,
    pr_data: dict,
    *,
    use_auto_merge: bool = True,
    prior_merge_attempts: int = 0,
) -> str:
    """Poll required checks then squash-merge the PR.

    This is the fallback path when auto-merge is unavailable. It polls
    required checks, handles BEHIND branches, and retries the merge
    command to cope with race conditions.

    Returns "passed" on success or "failed" with run.last_error set.
    """
    checks_deadline = time.time() + MERGE_CHECKS_TIMEOUT_SECONDS
    checks_poll_interval = max(1, MERGE_CHECKS_POLL_INTERVAL_SECONDS)
    while True:
        checks_ok, checks_error = _required_checks_green(repo_root, pr_number)
        if checks_ok:
            break
        if _required_checks_still_pending(checks_error):
            _restored, restore_error = _restore_approved_local_review_status_if_pending(
                run,
                repo_root,
                expected_head_sha=pr_head_sha,
                checks_error=checks_error,
            )
            if restore_error:
                run.last_error = restore_error
                return "failed"
            remaining = checks_deadline - time.time()
            if remaining <= 0:
                run.last_error = (
                    f"Merge refused for {_short_sha(pr_head_sha)}; "
                    f"timed out waiting for required checks: {checks_error}"
                )
                return "failed"
            logger.info(
                "Waiting for required checks before merge on PR #%s: %s",
                pr_number,
                checks_error,
            )
            _poll_sleep(min(checks_poll_interval, remaining))
            continue
        checks_error = _augment_required_checks_error(repo_root, checks_error)
        run.last_error = f"Merge refused for {_short_sha(pr_head_sha)}; {checks_error}"
        return "failed"
    latest_pr, _ = _find_open_pr_for_branch(repo_root, branch)
    latest_head_sha = str(latest_pr.get("headRefOid", "")).strip() if isinstance(latest_pr, dict) else ""
    if latest_head_sha and pr_head_sha and latest_head_sha != pr_head_sha:
        run.last_error = (
            "Merge refused because PR head changed while checking required checks: "
            f"expected {_short_sha(pr_head_sha)}, got {_short_sha(latest_head_sha)}"
        )
        return "failed"
    # If branch is behind base, merge origin/master locally and push.
    merge_state = str((latest_pr or pr_data).get("mergeStateStatus", "")).strip().upper()
    if merge_state == "BEHIND":
        logger.info(
            "PR #%s branch is behind base — merging origin/master locally...",
            pr_number,
        )
        merge_result = _merge_origin_master(worktree_path)
        if merge_result.status == "conflict":
            detail = merge_result.stderr or "git merge origin/master reported conflicts"
            run.last_error = f"Merge conflict while merging origin/master before merge: {detail}"
            run.merge_conflict_error = run.last_error
            run.mergeability_issue = run.last_error
            return "failed"
        if merge_result.status == "error":
            detail = merge_result.stderr or "unknown error"
            run.last_error = f"Failed to merge {BASE_REF} before merge phase: {detail}"
            return "failed"
        origin_master_sha = _resolve_git_ref_sha(worktree_path, BASE_REF)
        if origin_master_sha:
            run.last_merged_master_sha = origin_master_sha

        push_result = run_subprocess(
            ["git", "push", "origin", branch],
            cwd=worktree_path,
        )
        if push_result.returncode != 0:
            run.last_error = (
                "Failed to push branch after merging origin/master: "
                f"{push_result.stderr.strip() or push_result.stdout.strip()}"
            )
            return "failed"

        current_head_sha = _head_sha(worktree_path) or _remote_branch_head_sha(repo_root, branch)
        pr_body = str((latest_pr or pr_data).get("body", "") or "")
        if _mark_local_review_for_rerun_after_sync(
            run,
            repo_root,
            pr_body=pr_body,
            current_head_sha=current_head_sha,
        ):
            return "failed"

        # After syncing with master, wait for required checks to pass again.
        checks_deadline = time.time() + MERGE_CHECKS_TIMEOUT_SECONDS
        while True:
            checks_ok, checks_error = _required_checks_green(
                repo_root,
                pr_number,
            )
            if checks_ok:
                break
            if _required_checks_still_pending(checks_error):
                remaining = checks_deadline - time.time()
                if remaining <= 0:
                    run.last_error = f"Timed out waiting for checks after syncing with origin/master: {checks_error}"
                    return "failed"
                logger.info(
                    "Waiting for checks after syncing with origin/master on PR #%s: %s",
                    pr_number,
                    checks_error,
                )
                _poll_sleep(min(checks_poll_interval, remaining))
                continue
            run.last_error = f"Checks failed after syncing with origin/master: {checks_error}"
            return "failed"
    # Retry gh pr merge a few times to handle race between checks
    # passing and GitHub branch protection allowing the merge.
    merge_retries = max(
        1,
        max(
            3,
            MERGE_CHECKS_TIMEOUT_SECONDS // max(1, MERGE_CHECKS_POLL_INTERVAL_SECONDS),
        )
        - prior_merge_attempts,
    )
    forge = _forge()
    merged_successfully = False
    for merge_try in range(merge_retries):
        logger.info(
            "Merging PR #%s (squash)... (try %d/%d)",
            pr_number,
            merge_try + 1,
            merge_retries,
        )
        merge_result = forge.merge_pr(
            pr_number,
            method="squash",
            auto=use_auto_merge,
            expected_head_sha=pr_head_sha or None,
            cwd=repo_root,
        )
        if merge_result.ok:
            merged_successfully = True
            break
        merge_err = merge_result.message
        if use_auto_merge and _is_auto_merge_capability_error(merge_err):
            logger.info(
                "Auto-merge unavailable for PR #%s; retrying without --auto.",
                pr_number,
            )
            use_auto_merge = False
            fallback_result = forge.merge_pr(
                pr_number,
                method="squash",
                auto=False,
                expected_head_sha=pr_head_sha or None,
                cwd=repo_root,
            )
            if fallback_result.ok:
                merged_successfully = True
                break
            merge_err = fallback_result.message
        if use_auto_merge and _is_auto_merge_graphql_retryable_error(merge_err) and merge_try < merge_retries - 1:
            logger.info(
                "Auto-merge GraphQL response was retryable for PR #%s; retrying with --auto.",
                pr_number,
            )
            _poll_sleep(MERGE_CHECKS_POLL_INTERVAL_SECONDS)
            continue
        if _is_retryable_merge_race_error(merge_err):
            checks_ok, checks_error = _required_checks_green(repo_root, pr_number)
            if not checks_ok and checks_error.lower().startswith("required checks failing:"):
                checks_error = _augment_required_checks_error(repo_root, checks_error)
                run.last_error = f"Merge refused for {_short_sha(pr_head_sha)}; {checks_error}"
                return "failed"
            if merge_try < merge_retries - 1:
                if checks_ok:
                    logger.info("Merge race blocked merge despite green checks; waiting before retry...")
                elif _required_checks_still_pending(checks_error):
                    logger.info(
                        "Merge race blocked merge while required checks are still pending; waiting before retry..."
                    )
                else:
                    logger.info(
                        "Merge race blocked merge and required-check status "
                        "is inconclusive (%s); waiting before retry...",
                        checks_error,
                    )
                _poll_sleep(MERGE_CHECKS_POLL_INTERVAL_SECONDS)
                continue
        run.last_error = f"gh pr merge failed: {merge_err}"
        return "failed"
    if not merged_successfully:
        run.last_error = f"gh pr merge exhausted {merge_retries} retries"
        return "failed"

    merged, wait_error = _wait_for_pr_merged(repo_root, branch, pr_number)
    if not merged:
        run.last_error = wait_error
        return "failed"

    return "passed"


@dataclass(frozen=True)
class ReadinessDecision:
    ready: bool
    head_sha: str
    status: str
    blocker: str = ""
    blocker_kind: str = ""


def _record_readiness_decision(run: RunState, decision: ReadinessDecision) -> None:
    run.readiness_head_sha = decision.head_sha
    run.readiness_status = decision.status
    run.readiness_blocker = decision.blocker


def _revalidate_readiness_pr_head(
    run: RunState,
    repo_root: Path,
    *,
    current_head_sha: str,
    context: str,
) -> ReadinessDecision | None:
    latest_pr = _find_pr_for_branch(repo_root, run.branch, state="all")
    if not isinstance(latest_pr, dict):
        return ReadinessDecision(
            False,
            current_head_sha,
            "merge-blocked",
            f"could not revalidate PR head {context}",
        )

    latest_head_sha = str(latest_pr.get("headRefOid", "") or "").strip()
    if not latest_head_sha:
        return ReadinessDecision(
            False,
            current_head_sha,
            "merge-blocked",
            f"PR head SHA is unavailable {context}",
        )
    if latest_head_sha != current_head_sha:
        return ReadinessDecision(
            False,
            current_head_sha,
            "merge-blocked",
            (
                f"PR head changed {context}: "
                f"expected {_short_sha(current_head_sha)}, got {_short_sha(latest_head_sha)}"
            ),
        )
    return None


def _evaluate_readiness_for_promotion(
    run: RunState,
    repo_root: Path,
    *,
    pr_number: int,
    pr_data: dict,
    current_head_sha: str,
    require_forge_checks: bool = True,
    require_integration_checkpoint: bool = True,
) -> ReadinessDecision:
    if not current_head_sha:
        return ReadinessDecision(False, "", "merge-blocked", "PR head SHA is unavailable")

    pr_head_sha = str(pr_data.get("headRefOid", "") or "").strip()
    if pr_head_sha and pr_head_sha != current_head_sha:
        return ReadinessDecision(
            False,
            current_head_sha,
            "merge-blocked",
            (
                "PR head changed before readiness promotion: "
                f"expected {_short_sha(current_head_sha)}, got {_short_sha(pr_head_sha)}"
            ),
        )

    if not run.verify_passed_once:
        return ReadinessDecision(False, current_head_sha, "draft-published", "independent verify has not passed")

    verified_head = str(run.verify_head_sha or "").strip()
    if not verified_head:
        return ReadinessDecision(
            False,
            current_head_sha,
            "draft-published",
            "exact independent verify head is unavailable",
        )
    if verified_head != current_head_sha:
        return ReadinessDecision(
            False,
            current_head_sha,
            "draft-published",
            (
                "current head has not passed independent verify: "
                f"verified {_short_sha(verified_head)}, current {_short_sha(current_head_sha)}"
            ),
        )

    if run.review_decision_status != "approved":
        return ReadinessDecision(
            False,
            current_head_sha,
            "draft-published",
            f"review is not approved for the current head (status={run.review_decision_status or 'missing'})",
        )
    if run.review_expected_head_sha and run.review_expected_head_sha != current_head_sha:
        return ReadinessDecision(
            False,
            current_head_sha,
            "draft-published",
            (
                "review result is stale: "
                f"reviewed {_short_sha(run.review_expected_head_sha)}, current {_short_sha(current_head_sha)}"
            ),
        )

    if require_forge_checks and SPEC_RUNTIME_CONFIG.readiness.require_forge_checks:
        checks_deadline = time.time() + MERGE_CHECKS_TIMEOUT_SECONDS
        checks_poll_interval = max(1, MERGE_CHECKS_POLL_INTERVAL_SECONDS)
        while True:
            checks_ok, checks_error = _required_checks_green(repo_root, pr_number)
            if checks_ok:
                break
            if not _required_checks_still_pending(checks_error):
                checks_error = _augment_required_checks_error(repo_root, checks_error)
                return ReadinessDecision(False, current_head_sha, "draft-published", checks_error)
            remaining = checks_deadline - time.time()
            if remaining <= 0:
                return ReadinessDecision(
                    False,
                    current_head_sha,
                    "draft-published",
                    f"timed out waiting for required checks before readiness promotion: {checks_error}",
                )
            logger.info(
                "Waiting for required checks before readiness promotion on PR #%s: %s",
                pr_number,
                checks_error,
            )
            _poll_sleep(min(checks_poll_interval, remaining))

        head_decision = _revalidate_readiness_pr_head(
            run,
            repo_root,
            current_head_sha=current_head_sha,
            context="after required checks passed",
        )
        if head_decision is not None:
            return head_decision

    operator_request = _load_operator_request(repo_root, run)
    if operator_request is not None and operator_request.status == "pending":
        if operator_request.kind == "debugger_guidance" and (
            operator_request.requested_by_phase or "implement"
        ) in {
            "implement",
            "verify",
            "review",
        }:
            # Pending debugger guidance raised during a pre-merge phase is a
            # stale block replay by the time readiness promotion runs: the
            # guidance described a blocked state, and the current head has
            # since passed that phase plus every later gate. Refusing
            # promotion here would park an otherwise fully-green run for human
            # attention. Agent questions are different — a human
            # answer is owed regardless of progress — and keep blocking.
            now = _now_iso()
            operator_request.response = (
                "Auto-consumed at readiness promotion: this request was raised "
                f"during the '{operator_request.requested_by_phase or 'implement'}' phase "
                f"(attempt {operator_request.request_attempt_number}), and the run has "
                "since passed that phase and every later gate on the published head. "
                "Stale-block replay guard."
            )
            operator_request.response_source = "orchestrator"
            operator_request.status = "consumed"
            operator_request.resolved_at = now
            operator_request.consumed_at = now
            operator_request.save(repo_root, run.run_id)
            logger.warning(
                "Auto-consumed stale pending operator request (%s, raised by %s) at "
                "readiness promotion for run %s.",
                operator_request.kind or "operator input",
                operator_request.requested_by_phase or "implement",
                run.run_id,
            )
        else:
            return ReadinessDecision(
                False,
                current_head_sha,
                "draft-published",
                f"pending operator request: {operator_request.kind or 'operator input'}",
                "pending_operator_request",
            )

    if run.pending_block_debugger_signature:
        return ReadinessDecision(
            False,
            current_head_sha,
            "merge-blocked",
            f"blocked-run diagnosis remains unresolved: {run.pending_block_debugger_signature}",
        )
    if run.merge_conflict_error:
        return ReadinessDecision(False, current_head_sha, "merge-blocked", run.merge_conflict_error)
    if run.review_decision_status == "request_changes":
        return ReadinessDecision(False, current_head_sha, "merge-blocked", run.review_decision_summary)

    if require_integration_checkpoint:
        merge_state = str(pr_data.get("mergeStateStatus", "") or "").strip().upper()
        if merge_state in {"BEHIND", "DIRTY"}:
            return ReadinessDecision(
                False,
                current_head_sha,
                "merge-blocked",
                f"latest integration checkpoint reports mergeStateStatus={merge_state}",
            )

    return ReadinessDecision(True, current_head_sha, "merge-eligible")


def _best_effort_post_merge_local_sync(run: RunState, repo_root: Path) -> None:
    """Refresh the repo-root base branch ref after a remote merge without blocking the run."""
    # Resolve the remote-tracking ref to advance from. base_ref may be:
    #   - "origin/master" — already a remote-tracking ref
    #   - "main" — a plain local branch; we still fetch and read from origin/main
    # The plain-local form is the one that previously broke: `git fetch origin main`
    # updates `refs/remotes/origin/main`, not local `main`, so updating local `main`
    # from itself was a no-op. We always sync the local branch from the remote-tracking
    # ref, never from `BASE_REF` directly.
    if "/" in BASE_REF:
        remote_name, remote_branch = BASE_REF.split("/", 1)
        remote_tracking_ref = BASE_REF
    else:
        remote_name = "origin"
        remote_branch = BASE_REF
        remote_tracking_ref = f"origin/{BASE_REF}"

    try:
        fetch_outcome = run_git_fetch_with_timeout(
            [remote_name, remote_branch],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
            runner=_orchestrator_fetch_runner,
        )
    except GitFetchTimeoutError as exc:
        _record_nonfatal_warning(
            run,
            phase="merge",
            failure_type="merge",
            failure_subtype="local_post_merge_sync_timed_out",
            summary=(
                "Remote merge succeeded, but post-merge local master refresh timed out. "
                "Cleanup can continue; refresh local refs manually if needed."
            ),
            action=f"git fetch {remote_name} {remote_branch}",
            detail=f"git fetch timed out after {exc.timeout_seconds:.0f}s",
        )
        return
    if not fetch_outcome.is_success:
        detail = fetch_outcome.stderr.strip() or fetch_outcome.stdout.strip() or "unknown error"
        _record_nonfatal_warning(
            run,
            phase="merge",
            failure_type="merge",
            failure_subtype="local_post_merge_sync_failed",
            summary=(
                "Remote merge succeeded, but post-merge local master refresh failed. "
                "Cleanup can continue; refresh local refs manually if needed."
            ),
            action=f"git fetch {remote_name} {remote_branch}",
            detail=detail,
        )
        return

    head_ref_result = run_subprocess(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo_root,
    )
    on_master = head_ref_result.returncode == 0 and head_ref_result.stdout.strip() == PR_BASE_BRANCH
    if on_master:
        merge_result = run_subprocess(
            ["git", "merge", "--ff-only", remote_tracking_ref],
            cwd=repo_root,
        )
        if merge_result.returncode != 0:
            detail = merge_result.stderr.strip() or merge_result.stdout.strip() or "unknown error"
            _record_nonfatal_warning(
                run,
                phase="merge",
                failure_type="merge",
                failure_subtype="local_post_merge_sync_failed",
                summary=(
                    f"Remote merge succeeded, but repo-root `{PR_BASE_BRANCH}` could not fast-forward to `{remote_tracking_ref}`."
                ),
                action=f"git merge --ff-only {remote_tracking_ref}",
                detail=detail,
            )
        return

    branch_update = run_subprocess(
        ["git", "update-ref", f"refs/heads/{PR_BASE_BRANCH}", remote_tracking_ref],
        cwd=repo_root,
    )
    if branch_update.returncode != 0:
        detail = branch_update.stderr.strip() or branch_update.stdout.strip() or "unknown error"
        _record_nonfatal_warning(
            run,
            phase="merge",
            failure_type="merge",
            failure_subtype="local_post_merge_sync_failed",
            summary=("Remote merge succeeded, but the local `master` ref could not be updated after the merge."),
            action=f"git update-ref refs/heads/{PR_BASE_BRANCH} {remote_tracking_ref}",
            detail=detail,
        )


def phase_merge(run: RunState, repo_root: Path) -> str:
    """Find PR, squash-merge, update spec status to merged."""
    branch = run.branch
    worktree_path = resolve_worktree_path(run, repo_root)

    # Check forge auth
    auth_error = _check_forge_auth()
    if auth_error:
        run.last_error = auth_error
        return "failed"

    # Completion fence: refresh remote base ref + spec/merged/* tags so
    # is_spec_merged() reflects authoritative state, then short-circuit if
    # another run has already landed this spec. Fail closed if we cannot
    # verify completion state, so we never merge against a stale view.
    fence_action, fence_error, fenced_ref = refresh_merge_completion_state(repo_root)
    if fence_error:
        run.last_error = (
            f"Merge skipped: completion state for {run.spec_id} could not be verified. "
            f"`{fence_action}` failed: {fence_error}"
        )
        return "failed"
    if is_spec_merged(repo_root, run.spec_id, base_ref=fenced_ref):
        logger.info(
            "Spec %s already merged by another run; skipping merge mutations.",
            run.spec_id,
        )
        run.last_error = ""
        _record_nonfatal_warning(
            run,
            phase="merge",
            failure_type="merge",
            failure_subtype="superseded_by_completed_spec",
            summary="spec already merged by another run; merge phase skipped",
            detail=(
                f"refs/tags/{merge_tag_name(run.spec_id)} is already reachable "
                f"from {BASE_REF}; no PR, branch, or tag mutations performed."
            ),
        )
        return "passed"

    pr_data = _find_pr_for_branch(repo_root, branch, state="all")
    if pr_data is None:
        run.last_error = f"No PR found for branch {branch}"
        return "failed"

    pr_number = pr_data.get("number")
    pr_state = pr_data.get("state", "")
    pr_head_sha = str(pr_data.get("headRefOid", "")).strip()
    pr_body = str(pr_data.get("body", "") or "")

    if not isinstance(pr_number, int):
        run.last_error = f"Could not determine PR number for branch {branch}"
        return "failed"

    if pr_state == "MERGED":
        logger.info("PR #%s already merged", pr_number)
    elif pr_state != "OPEN":
        run.last_error = f"PR #{pr_number} is in unexpected state '{pr_state}'"
        return "failed"

    # Recompute HEAD from local git state instead of trusting pr_data["headRefOid"],
    # which can lag behind reality due to GitHub's eventual consistency.
    merge_head_sha = (_head_sha(worktree_path) if worktree_path.is_dir() else None) or pr_head_sha

    readiness: ReadinessDecision | None = None
    is_open_pr = pr_state == "OPEN"
    is_draft_pr = bool(pr_data.get("isDraft"))
    if is_open_pr and _mark_local_review_for_rerun_after_sync(
        run,
        repo_root,
        pr_body=pr_body,
        current_head_sha=merge_head_sha,
    ):
        return "failed"
    if is_open_pr:
        readiness = _evaluate_readiness_for_promotion(
            run,
            repo_root,
            pr_number=pr_number,
            pr_data=pr_data,
            current_head_sha=merge_head_sha,
        )
        _record_readiness_decision(run, readiness)
        if not readiness.ready:
            run.last_error = (
                f"Readiness promotion refused for {_short_sha(readiness.head_sha)}: "
                f"{readiness.blocker or 'readiness criteria were not satisfied'}"
            )
            if readiness.blocker_kind == "pending_operator_request":
                return "waiting-for-input"
            return "failed"

    if is_open_pr and is_draft_pr:
        logger.info("PR #%s is a draft, marking ready for review...", pr_number)
        run.readiness_status = "ready-promoting"
        run.readiness_head_sha = readiness.head_sha if readiness else merge_head_sha
        run.readiness_blocker = ""
        head_decision = _revalidate_readiness_pr_head(
            run,
            repo_root,
            current_head_sha=run.readiness_head_sha,
            context="before readiness promotion",
        )
        if head_decision is not None:
            _record_readiness_decision(run, head_decision)
            run.last_error = (
                f"Readiness promotion refused for {_short_sha(head_decision.head_sha)}: "
                f"{head_decision.blocker}"
            )
            return "failed"
        if not _forge().mark_pr_ready(pr_number, cwd=repo_root):
            run.readiness_status = "merge-blocked"
            run.readiness_blocker = f"forge failed to mark PR #{pr_number} ready"
            run.last_error = f"Failed to mark PR #{pr_number} as ready"
            return "failed"
        run.readiness_status = "ready"
        run.readiness_blocker = ""
    elif pr_state == "OPEN":
        run.readiness_status = "ready"
        run.readiness_head_sha = readiness.head_sha if readiness else merge_head_sha
        run.readiness_blocker = ""

    if pr_state == "MERGED":
        pass
    elif pr_state == "OPEN":
        expected_merge_head_sha = str(run.readiness_head_sha or merge_head_sha).strip()
        head_decision = _revalidate_readiness_pr_head(
            run,
            repo_root,
            current_head_sha=expected_merge_head_sha,
            context="before merge",
        )
        if head_decision is not None:
            _record_readiness_decision(run, head_decision)
            run.last_error = f"Merge refused for {_short_sha(head_decision.head_sha)}: {head_decision.blocker}"
            return "failed"
        auto = _try_auto_merge(
            run,
            repo_root,
            pr_number,
            expected_head_sha=expected_merge_head_sha,
        )
        if auto == "merged":
            pass  # fall through to post-merge cleanup
        elif auto == "failed":
            return "failed"
        else:
            # Auto-merge unavailable — fall back to manual poll-then-merge.
            manual_use_auto_merge = True
            prior_merge_attempts = 0
            if auto == "unavailable_after_auto":
                prior_merge_attempts = 1
            elif auto == "unavailable_after_auto_no_auto":
                manual_use_auto_merge = False
                prior_merge_attempts = 1
            manual = _manual_merge(
                run,
                repo_root,
                worktree_path,
                branch,
                pr_number,
                merge_head_sha,
                pr_data,
                use_auto_merge=manual_use_auto_merge,
                prior_merge_attempts=prior_merge_attempts,
            )
            if manual != "passed":
                return manual
    tag_name = merge_tag_name(run.spec_id)
    tag_check = run_subprocess(
        ["git", "tag", "--list", tag_name],
        cwd=repo_root,
    )
    tag_missing = tag_name not in tag_check.stdout.strip().splitlines()
    pre_merge_master_sha = ""
    if tag_missing:
        # Capture the pre-merge baseline before the post-merge fetch updates
        # origin/master. The missing-mergeCommit fallback relies on this range
        # to infer the newly landed commit safely.
        pre_merge_master_sha = (
            run.last_merged_master_sha
            or _resolve_git_ref_sha(repo_root, BASE_REF)
            or _resolve_git_ref_sha(repo_root, PR_BASE_BRANCH)
        )

    # Refreshing repo-root `master` is best-effort after the remote merge lands.
    _best_effort_post_merge_local_sync(run, repo_root)

    # Create and push a durable merge tag so status is derivable from git refs
    if tag_missing:
        merge_commit_sha, tag_provenance, merge_commit_error = _merge_tag_provenance_for_pr(
            run,
            repo_root,
            pr_number=pr_number,
            pr_data=pr_data,
            previous_master_sha=pre_merge_master_sha,
        )
        if not merge_commit_sha:
            detail = merge_commit_error or "unknown error"
            run.last_error = f"Could not determine merge commit for {tag_name}: {detail}"
            return "failed"
        if not run_or_fail(
            run,
            annotated_tag_command(
                tag_name,
                merge_commit_sha,
                build_tag_message(tag_provenance),
            ),
            cwd=repo_root,
            action="git tag",
        ):
            return "failed"

    if not run_or_fail(
        run,
        ["git", "push", "origin", f"refs/tags/{tag_name}"],
        cwd=repo_root,
        action=f"git push tag {tag_name}",
    ):
        run.last_error = (
            f"Tag push failed for {tag_name}. "
            "Ensure you have push access to the remote. "
            f"Original error: {run.last_error}"
        )
        return "failed"

    return "passed"


def phase_cleanup(run: RunState, repo_root: Path) -> str:
    """Remove worktree, prune, delete local branch."""
    backend = _resolve_execution_backend()
    if backend.identity.backend != "worktree":
        workspace_root = Path(backend.identity.workspace_root).expanduser()
        if not workspace_root.is_absolute():
            workspace_root = repo_root / workspace_root
        run_root = workspace_root / run.run_id
        workspace = WorkspaceHandle(
            path=run_root / "source",
            outbox_path=run_root / "outbox",
            branch=run.branch,
            backend=backend.identity.backend,
        )
        try:
            # The cleanup phase runs after a successful merge, so unpushed work
            # is expected to be absent — but even if the branch tip was not yet
            # mirrored to origin, the merge means the work is durably captured.
            # Opt out of the resume-safety deletion guard here (the spec allows
            # deletion in the post-merge cleanup phase and in `spec clean`).
            backend.cleanup(workspace, allow_unpushed_work=True)
        except OSError as exc:
            detail = f"Could not remove clone backend workspace {run_root}: {exc}"
            _record_nonfatal_warning(
                run,
                phase="cleanup",
                failure_type="cleanup",
                failure_subtype="backend_workspace_cleanup_failed",
                summary=detail,
                detail=detail,
            )
        return "passed"

    worktree_path = resolve_worktree_path(run, repo_root)
    cleanup_error = _cleanup_worktree_checkout(
        repo_root,
        worktree_path,
        branch=run.branch,
        delete_branch=True,
    )
    if cleanup_error:
        run.last_error = cleanup_error
        return "failed"
    return "passed"


# ---------------------------------------------------------------------------
# Phase dispatch table
# ---------------------------------------------------------------------------

PHASE_HANDLERS = {
    "bootstrap": phase_bootstrap,
    "scoping": phase_scoping,
    "intake": phase_intake,
    "implement": phase_implement,
    "verify": phase_verify,
    "publish": phase_publish,
    "review": phase_review,
    "merge": phase_merge,
    "cleanup": phase_cleanup,
}


# ---------------------------------------------------------------------------
# Orchestrator Engine
# ---------------------------------------------------------------------------


def _gate_name_from_error(message: str) -> str:
    if not message:
        return ""
    # Match configured gate names (longer names first to avoid partial matches).
    for gate_name in sorted(REQUIRED_GATES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(gate_name)}\b", message, flags=re.IGNORECASE):
            return gate_name
    return ""


def _is_local_review_rerun_after_sync(message: str) -> bool:
    normalized = str(message or "")
    return normalized.startswith(LOCAL_REVIEW_RERUN_AFTER_SYNC_PREFIX) or normalized.startswith(
        LOCAL_REVIEW_RERUN_AFTER_SYNC_LEGACY_PREFIX
    )


def _latest_gate_failure_fingerprint(
    repo_root: Path,
    run: RunState,
    gate_name: str,
) -> str:
    _, gate_data = _read_gate_status(repo_root, run)
    if not isinstance(gate_data, dict):
        return ""
    gate_entry = gate_data.get("gates", {}).get(gate_name)
    if not isinstance(gate_entry, dict):
        return ""
    history = gate_entry.get("history", [])
    if not isinstance(history, list):
        return ""
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "failed":
            continue
        fingerprint = str(entry.get("failure_fingerprint", "") or "").strip()
        if fingerprint:
            return fingerprint
    return ""


def _previous_gate_failure_fingerprint(
    repo_root: Path,
    run: RunState,
    gate_name: str,
) -> str:
    _, gate_data = _read_gate_status(repo_root, run)
    if not isinstance(gate_data, dict):
        return ""
    gate_entry = gate_data.get("gates", {}).get(gate_name)
    if not isinstance(gate_entry, dict):
        return ""
    history = gate_entry.get("history", [])
    if not isinstance(history, list):
        return ""
    failed_entries = [entry for entry in history if isinstance(entry, dict) and entry.get("status") == "failed"]
    if len(failed_entries) < 2:
        return ""
    return str(failed_entries[-2].get("failure_fingerprint", "") or "").strip()


def _maybe_trip_no_progress_circuit_breaker(
    run: RunState,
    repo_root: Path,
) -> str | None:
    failure_gate = _gate_name_from_error(run.last_error)
    current_fingerprint = _latest_gate_failure_fingerprint(repo_root, run, failure_gate) if failure_gate else ""
    previous_fingerprint = (
        _previous_gate_failure_fingerprint(repo_root, run, failure_gate)
        if failure_gate and run.last_verify_failure_gate == failure_gate
        else ""
    )
    same_head = bool(
        run.implement_head_sha_before
        and run.implement_head_sha_after
        and run.implement_head_sha_before == run.implement_head_sha_after
    )
    same_failure = bool(current_fingerprint and previous_fingerprint and current_fingerprint == previous_fingerprint)

    # Track the current first failed test nodeid for same-test detection.
    current_test_nodeid = (
        _latest_first_failed_test_nodeid_from_gate(repo_root, run, failure_gate) if failure_gate else ""
    )

    # Treat an unchanged failure fingerprint as the primary signal. A merge from
    # origin/master can change HEAD without making progress on the failing gate.
    if same_failure:
        run.no_progress_retries += 1
        run.last_verify_failure_gate = failure_gate
        run.last_verify_failure_test_nodeid = current_test_nodeid
        threshold = _no_progress_retry_threshold()
        if run.no_progress_retries >= threshold:
            tree_status = run.implement_tree_status or "unchanged_tree"
            head_detail = (
                f"same head {_short_sha(run.implement_head_sha_after)}"
                if same_head
                else (
                    f"new head {_short_sha(run.implement_head_sha_after)} "
                    f"(from {_short_sha(run.implement_head_sha_before)})"
                )
            )
            run.last_error = (
                f"No-progress circuit breaker triggered: implement returned "
                f"{head_detail} "
                f"(tree status: {tree_status}) and verify "
                f"failed again on gate '{failure_gate}' with unchanged failure "
                f"fingerprint {current_fingerprint} "
                f"({run.no_progress_retries}/{threshold}). "
                "Produce a new commit that changes the failing tests or "
                "investigate the stale evidence before retrying."
            )
            run.status = "blocked"
            run.save(repo_root)
            return "blocked"
        return None

    # Fingerprint changed — the failure is evolving.
    previous_test_nodeid = run.last_verify_failure_test_nodeid
    if current_test_nodeid and previous_test_nodeid and current_test_nodeid == previous_test_nodeid:
        # Same test with evolving fingerprint = partial progress.  Leave the
        # no-progress counter unchanged so the run converges toward the breaker
        # threshold instead of resetting indefinitely.
        pass
    else:
        # Different test or first occurrence — full progress, reset counter.
        run.no_progress_retries = 0
    run.last_verify_failure_gate = failure_gate
    run.last_verify_failure_test_nodeid = current_test_nodeid
    return None


def _maybe_prepare_debugger_guided_retry(
    run: RunState,
    repo_root: Path,
) -> bool:
    failure_gate = _gate_name_from_error(run.last_error)
    if not failure_gate:
        return False

    current_attempt_result = ImplementResult.load_attempt(
        repo_root,
        run.run_id,
        _current_attempt_number(run),
    )
    if current_attempt_result is None or current_attempt_result.status != "passed":
        return False

    current_fingerprint = _latest_gate_failure_fingerprint(repo_root, run, failure_gate)
    previous_fingerprint = (
        _previous_gate_failure_fingerprint(repo_root, run, failure_gate)
        if run.last_verify_failure_gate == failure_gate
        else ""
    )
    same_head = bool(
        run.implement_head_sha_before
        and run.implement_head_sha_after
        and run.implement_head_sha_before == run.implement_head_sha_after
    )
    same_failure = bool(current_fingerprint and previous_fingerprint and current_fingerprint == previous_fingerprint)
    if not (same_head and same_failure):
        return False

    previous_pending_signature = run.pending_block_debugger_signature
    diagnosis = _maybe_run_block_debugger(
        run,
        repo_root,
        source_phase="verify",
    )
    if diagnosis is None:
        return False

    logger.info(
        "Prepared debugger-guided retry for %s on unchanged head %s after repeated verify failure fingerprint %s",
        run.spec_id,
        _short_sha(run.implement_head_sha_after),
        current_fingerprint,
    )
    run.save(repo_root)
    return bool(
        run.pending_block_debugger_signature
        and run.pending_block_debugger_signature == diagnosis.blocker_signature
        and run.pending_block_debugger_signature != previous_pending_signature
        and not diagnosis.requires_human_attention
    )


def _confirm_merge_conflict_retry(
    run: RunState,
    repo_root: Path,
    *,
    context: str,
) -> bool:
    if run.merge_conflict_error:
        run.mergeability_issue = run.merge_conflict_error
        return True

    worktree_path = resolve_worktree_path(run, repo_root)
    mergeability_check = _check_local_mergeability_against_origin_master(
        repo_root,
        worktree_path,
        branch=run.branch,
    )
    if mergeability_check.status == "conflict":
        detail = mergeability_check.stderr or "git merge origin/master reported conflicts"
        issue = f"Confirmed local merge conflict with origin/master during {context}: {detail}"
        run.merge_conflict_error = issue
        run.mergeability_issue = issue
        run.last_error = issue
        return True

    if mergeability_check.status in {"success", "noop"}:
        run.merge_conflict_error = ""
        run.mergeability_issue = (
            f"PR mergeability issue during {context} appears stale or remote-only: "
            f"{run.last_error}. A local merge against origin/master is clean."
        )
        run.last_error = run.mergeability_issue
        return False

    detail = mergeability_check.stderr or "unknown error"
    run.merge_conflict_error = ""
    run.mergeability_issue = f"PR mergeability issue during {context} could not be confirmed locally: {detail}"
    run.last_error = run.mergeability_issue
    return False


def _result_warnings_for_phase(run: RunState, phase: str) -> list[dict[str, object]]:
    return [
        warning
        for warning in _coerce_nonfatal_warnings(run.nonfatal_warnings)
        if str(warning.get("phase") or "").strip() == phase
    ]


def _classify_phase_result(
    run: RunState,
    phase: str,
    result_status: str,
) -> dict[str, object]:
    warnings = _result_warnings_for_phase(run, phase)
    metadata: dict[str, object] = {
        "failure_type": "",
        "failure_subtype": "",
        "retryable": False,
        "nonfatal": False,
        "gate_name": "",
        "warnings": warnings,
    }
    if warnings:
        first_warning = warnings[0]
        metadata.update(
            {
                "failure_type": str(first_warning.get("failure_type") or "").strip(),
                "failure_subtype": str(first_warning.get("failure_subtype") or "").strip(),
                "retryable": bool(first_warning.get("retryable", False)),
                "nonfatal": True,
                "gate_name": str(first_warning.get("gate_name") or "").strip(),
            }
        )
    if result_status == "passed":
        return metadata

    message = run.last_error or ""
    normalized = message.lower()
    if "container backend import failed" in normalized:
        metadata.update(
            {
                "failure_type": "import",
                "failure_subtype": "container_workspace_import_failed",
                "retryable": True,
                "nonfatal": False,
            }
        )
        return metadata
    if _is_agent_auth_failure_message(message) and phase != "implement":
        # Agent CLI credential outages hit review too because local review runs
        # the agent CLI. Give them the same treatment as implement-phase agent
        # auth and forge auth: non-retryable, needs operator re-login.
        metadata.update(
            {
                "failure_type": phase or "workflow",
                "failure_subtype": "agent_auth_missing",
                "retryable": False,
                "nonfatal": False,
            }
        )
        return metadata
    if _is_forge_auth_failure_message(message):
        # Forge auth outages hit any phase that talks to GitHub (review,
        # publish, merge). No amount of agent retries fixes credentials;
        # classify non-retryable so autopilot stops re-dispatching and the
        # operator sees needs-attention instead of a burned retry cap.
        metadata.update(
            {
                "failure_type": phase or "workflow",
                "failure_subtype": "forge_auth_missing",
                "retryable": False,
                "nonfatal": False,
            }
        )
        return metadata
    if phase == "implement":
        if _is_agent_auth_failure_message(message):
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "agent_auth_missing",
                    "retryable": False,
                    "nonfatal": False,
                }
            )
        elif _is_agent_capacity_failure_message(message):
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "provider_capacity",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif "could not position the workspace at reviewed head" in normalized:
            # The reviewed head is unreachable, so the retry cannot start
            # from it. Retrying would silently implement from base; surface it as
            # needs-attention (non-retryable) instead.
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "reviewed_head_unrecoverable",
                    "retryable": False,
                    "nonfatal": False,
                }
            )
        elif "no_handshake" in normalized and "left uncommitted changes" in normalized:
            # The agent finished (or nearly finished) real work and exited
            # without the handshake — typically after parking gate commands in
            # a background task and ending its turn. Distinct subtype so
            # the debugger prescribes completion of the existing work instead
            # of re-implementation.
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "unreported_work",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif "no_handshake" in normalized:
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "no_handshake",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif "agent became inactive" in normalized:
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "agent_timeout",
                    "retryable": False,
                    "nonfatal": False,
                }
            )
        elif "implement dev server" in normalized:
            metadata.update(
                {
                    "failure_type": "implement",
                    "failure_subtype": "dev_server_startup_failed",
                    "retryable": False,
                    "nonfatal": False,
                }
            )
        return metadata

    if phase == "verify":
        if _is_verify_environment_failure_message(message):
            metadata.update(
                {
                    "failure_type": "verify",
                    "failure_subtype": "prepare_environment_failed",
                    "retryable": False,
                    "nonfatal": False,
                    "gate_name": _gate_name_from_error(message),
                }
            )
        elif "merge conflict while merging origin/master before verify" in normalized:
            metadata.update(
                {
                    "failure_type": "verify",
                    "failure_subtype": "preflight_merge_conflict",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif "gate '" in normalized:
            metadata.update(
                {
                    "failure_type": "verify",
                    "failure_subtype": "gate_failed",
                    "retryable": True,
                    "nonfatal": False,
                    "gate_name": _gate_name_from_error(message),
                }
            )
        return metadata

    if phase == "review":
        if run.review_decision_status == "request_changes" or "requested changes" in normalized:
            metadata.update(
                {
                    "failure_type": "review",
                    "failure_subtype": "request_changes",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif _is_local_review_timeout_message(message):
            metadata.update(
                {
                    "failure_type": "review",
                    "failure_subtype": "local_reviewer_timeout",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif "no pr found" in normalized or "not open" in normalized:
            metadata.update(
                {
                    "failure_type": "review",
                    "failure_subtype": "pr_not_open",
                    "retryable": False,
                    "nonfatal": False,
                }
            )
        return metadata

    if phase == "merge":
        if "failed to mark pr #" in normalized and "as ready" in normalized:
            metadata.update(
                {
                    "failure_type": "merge",
                    "failure_subtype": "readiness_promotion_failed",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif message.startswith("Readiness promotion refused"):
            metadata.update(
                {
                    "failure_type": "merge",
                    "failure_subtype": "readiness_blocked",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        elif _is_local_review_rerun_after_sync(message):
            metadata.update(
                {
                    "failure_type": "merge",
                    "failure_subtype": "local_review_stale",
                    "retryable": True,
                    "nonfatal": False,
                    "gate_name": REVIEW_GATE_CHECK_NAME,
                }
            )
        elif _is_required_checks_failure(message):
            metadata.update(
                {
                    "failure_type": "merge",
                    "failure_subtype": "required_checks_failed",
                    "retryable": True,
                    "nonfatal": False,
                    "gate_name": _gate_name_from_error(message),
                }
            )
        elif _is_merge_conflict(message):
            metadata.update(
                {
                    "failure_type": "merge",
                    "failure_subtype": "remote_conflict",
                    "retryable": True,
                    "nonfatal": False,
                }
            )
        return metadata

    if phase == "publish" and message.startswith("git push failed:"):
        # A non-fast-forward rejection that reaches this classifier already
        # failed _reconcile_publish_push_rejection's safe self-recovery case
        # (lease-pinned re-push over our own last published head), so the
        # remote moved somewhere this run cannot prove it owns. Surface it as
        # a distinct, clearly non-retryable subtype so operators see the
        # divergence rather than a silent retry storm.
        non_fast_forward = (
            "fetch first" in normalized
            or "[rejected]" in normalized
            or "non-fast-forward" in normalized
            or "tip of your current branch is behind" in normalized
        )
        metadata.update(
            {
                "failure_type": "publish",
                "failure_subtype": ("remote_diverged" if non_fast_forward else "push_failed"),
                "retryable": False,
                "nonfatal": False,
            }
        )
        return metadata

    if phase == "scoping" and (
        "without producing" in normalized
        or "multiple task specs" in normalized
        or "legacy current_spec.md" in normalized
    ):
        metadata.update(
            {
                "failure_type": "scoping",
                "failure_subtype": "missing_output",
                "retryable": False,
                "nonfatal": False,
            }
        )
        return metadata

    return metadata


def run_single_phase(run: RunState, phase: str, repo_root: Path) -> str:
    """Execute one phase, update run state, persist audit trail."""
    handler = PHASE_HANDLERS.get(phase)
    if handler is None:
        run.last_error = f"Unknown phase: {phase}"
        run.status = "failed"
        run.save(repo_root)
        return "failed"

    run.phase = phase
    run.status = "running"
    run.save(repo_root)
    _refresh_active_run_lease(repo_root, run, phase)
    lease_failure = LeaseHeartbeatFailure()
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_run_lease_heartbeat_loop,
        args=(repo_root, run, phase, heartbeat_stop, lease_failure),
        name=f"lease-heartbeat-{run.run_id or 'unknown'}",
        daemon=True,
    )
    _set_active_phase_lease_failure(lease_failure)
    heartbeat_thread.start()

    started_at = _now_iso()
    logger.info(
        "Phase %s started (spec=%s, run=%s, attempt=%d)",
        phase,
        run.spec_id,
        run.run_id,
        run.attempts,
    )
    _emit_phase_progress(run, phase, "started")

    try:
        result_status = handler(run, repo_root)
    except Exception as exc:
        result_status = "failed"
        run.last_error = f"Exception in {phase}: {exc}"
        logger.exception("Phase %s raised exception", phase)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5.0)
        _set_active_phase_lease_failure(None)

    lease_failure_message = lease_failure.failure_message()
    if lease_failure_message:
        result_status = "failed"
        run.last_error = lease_failure_message

    if phase == "verify" and result_status == "passed":
        run.verify_passed_once = True
        worktree_path = resolve_worktree_path(run, repo_root)
        run.verify_head_sha = (_head_sha(worktree_path) if worktree_path.is_dir() else None) or ""
    run.status = result_status

    # Classify the phase result and persist the classification onto the run
    # record itself (not only the per-phase audit JSON) so status projection
    # and autopilot can consult retryability without scanning audit files.
    metadata = _classify_phase_result(run, phase, result_status)
    if result_status == "passed":
        # A successful phase clears any prior failure classification so the
        # retryable hint / circuit breaker resets on forward progress.
        run.last_failure_retryable = None
        run.last_failure_type = ""
        run.last_failure_subtype = ""
    else:
        run.last_failure_retryable = bool(metadata.get("retryable", False))
        run.last_failure_type = str(metadata.get("failure_type") or "")
        run.last_failure_subtype = str(metadata.get("failure_subtype") or "")
    run.save(repo_root)

    # Persist audit trail
    audit_result = OrchestratorResult(
        status=result_status,
        started_at=started_at,
        finished_at=_now_iso(),
        error_code=run.last_error if result_status != "passed" else "",
        failure_type=str(metadata.get("failure_type") or ""),
        failure_subtype=str(metadata.get("failure_subtype") or ""),
        retryable=bool(metadata.get("retryable", False)),
        nonfatal=bool(metadata.get("nonfatal", False)),
        gate_name=str(metadata.get("gate_name") or ""),
        warnings=list(metadata.get("warnings") or []),
    )
    _persist_audit(repo_root, run, phase, audit_result)

    logger.info(
        "Phase %s finished: %s (spec=%s, run=%s)",
        phase,
        result_status,
        run.spec_id,
        run.run_id,
    )
    _emit_phase_progress(run, phase, result_status)
    return result_status


def _persist_audit(
    repo_root: Path,
    run: RunState,
    phase: str,
    result: OrchestratorResult,
) -> None:
    """Save request+result audit entry."""
    audit_dir = _state_root(repo_root) / "orchestrator"
    audit_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    audit_path = audit_dir / f"{run.run_id}-{phase}-{ts}.json"
    audit_data = {
        "run_id": run.run_id,
        "spec_id": run.spec_id,
        "phase": phase,
        "result": asdict(result),
    }
    audit_path.write_text(json.dumps(audit_data, indent=2, sort_keys=True) + "\n")


def _convergence_attempts(run: RunState) -> int:
    """Attempts that say something about whether this run is converging.

    ``run.attempts`` also numbers per-attempt artifacts, so every retry has to
    increment it. Merge conflicts are excluded here instead, so they neither
    consume the shared cap directly nor inflate it for the failures that follow.
    """
    return max(0, int(run.attempts) - int(run.merge_conflicts))


def _consume_retry_budget(
    run: RunState,
    reason: str,
    retry_cap: int,
) -> bool:
    if reason == "merge_conflicts":
        used, cap = int(run.merge_conflicts), MERGE_CONFLICT_RETRY_CAP
    else:
        used, cap = _convergence_attempts(run), retry_cap

    if used >= cap:
        logger.warning(
            "Retry cap reached (%d/%d) for %s while handling %s.",
            used,
            cap,
            run.spec_id,
            RETRY_REASON_LABELS[reason].lower(),
        )
        return False

    setattr(run, reason, getattr(run, reason) + 1)
    # Always advance: run.attempts is the attempt number for this run's
    # implement-context/implement-result artifacts, so reusing it would make two
    # attempts collide on one filename.
    run.attempts += 1
    return True


def _retry_resume_message(run: RunState, headline: str) -> str:
    cmd = _implement_resume_command(run)
    if _convergence_attempts(run) >= run.retry_cap:
        cmd += f" --retry-cap {run.retry_cap + 10}"
    return f"{headline} Branch: {run.branch}\nResume with: {cmd}"


def _handle_successful_phase_transition(run: RunState, phase: str) -> None:
    if phase == "verify":
        run.merge_conflict_error = ""
        run.mergeability_issue = ""
        run.no_progress_retries = 0
        run.last_verify_failure_gate = ""
        run.last_verify_failure_test_nodeid = ""
    # Do NOT clear review_decision_status on implement success — review findings
    # should persist as stale context until verify passes (which advances the run
    # past verify) or a newer review approves.


def _workflow_failure_policy(
    run: RunState,
    phase: str,
) -> WorkflowFailurePolicy | None:
    if _is_forge_auth_failure_message(run.last_error) or (
        _is_agent_auth_failure_message(run.last_error) and phase != "implement"
    ):
        # Credentials are an operator problem in every phase; never spend
        # retry budget on them. Implement-phase agent auth keeps its dedicated
        # branch.
        return None
    if phase == "verify":
        if _is_verify_environment_failure_message(run.last_error):
            return None
        return WorkflowFailurePolicy(
            retry_reason="verify_failures",
            next_phase="implement",
            retry_log_message="%s failed, retrying implement (attempt %d/%d)",
            retry_cap_source_phase="verify",
            terminal_reason="verify_failures",
            publish_retry_cap_draft=True,
        )
    if phase == "implement" and _is_agent_auth_failure_message(run.last_error):
        return None
    if phase == "implement" and (
        _is_retryable_implement_failure_message(run.last_error)
        or _agent_reported_failure_on_clean_commit(run)
    ):
        return WorkflowFailurePolicy(
            retry_reason="implement_failures",
            next_phase="implement",
            retry_log_message="Retryable implement failure, retrying implement (attempt %d/%d): %s",
            retry_cap_source_phase="implement",
            terminal_reason="implement_failures",
        )
    if phase == "review" and run.review_decision_status == "request_changes":
        return WorkflowFailurePolicy(
            retry_reason="review_changes",
            next_phase="implement",
            retry_log_message="Review requested changes, retrying implement (attempt %d/%d)",
            retry_cap_source_phase="review",
            terminal_reason="review_changes",
        )
    if phase == "review" and _is_merge_conflict(run.last_error):
        return WorkflowFailurePolicy(
            retry_reason="merge_conflicts",
            next_phase="implement",
            retry_log_message="Review blocked by merge conflict, retrying implement (attempt %d/%d): %s",
            retry_cap_source_phase="review",
            terminal_reason="merge_conflicts",
            confirm_merge_conflict=True,
        )
    if phase == "merge" and _is_local_review_rerun_after_sync(run.last_error):
        return WorkflowFailurePolicy(
            retry_reason="verify_failures",
            next_phase="verify",
            retry_log_message=(
                "Merge invalidated local review for %s; rerunning verify and review on the updated head "
                "(attempt %d/%d)."
            ),
            retry_cap_source_phase="merge",
            terminal_reason="verify_failures",
        )
    if phase == "merge" and _is_merge_conflict(run.last_error):
        return WorkflowFailurePolicy(
            retry_reason="merge_conflicts",
            next_phase="implement",
            retry_log_message="Merge conflict, retrying implement (attempt %d/%d): %s",
            retry_cap_source_phase="merge",
            terminal_reason="merge_conflicts",
            confirm_merge_conflict=True,
        )
    if phase == "merge" and (
        _is_retryable_merge_race_error(run.last_error)
        or _is_readiness_promotion_forge_failure(run.last_error)
        or _is_auto_merge_pending_checks_timeout(run.last_error)
    ):
        return WorkflowFailurePolicy(
            next_phase="merge",
            retry_cap_source_phase="merge",
            direct_phase_retry=True,
        )
    if phase == "merge" and _is_required_checks_failure(run.last_error):
        return WorkflowFailurePolicy(
            retry_reason="verify_failures",
            next_phase="implement",
            retry_log_message="Merge blocked by failing required checks, retrying implement (attempt %d/%d): %s",
            retry_cap_source_phase="merge",
            terminal_reason="verify_failures",
        )
    if (
        phase == "merge"
        and run.last_error.startswith("Readiness promotion refused")
        and "pending operator request:" in run.last_error
    ):
        return None
    if phase == "merge" and run.last_error.startswith("Readiness promotion refused"):
        return WorkflowFailurePolicy(
            retry_reason="verify_failures",
            next_phase="implement",
            retry_log_message="Readiness promotion blocked, retrying implement (attempt %d/%d): %s",
            retry_cap_source_phase="merge",
            terminal_reason="verify_failures",
        )
    return None


def _handle_retry_cap_for_policy(
    run: RunState,
    repo_root: Path,
    policy: WorkflowFailurePolicy,
) -> str:
    logger.warning(
        "Retry cap reached for %s during %s.",
        run.spec_id,
        policy.retry_cap_source_phase,
    )
    if policy.publish_retry_cap_draft:
        if _should_publish_retry_cap_verify_escalation(run, repo_root):
            logger.warning(
                "Publishing draft PR for %s because a PR already exists or "
                "verify passed earlier in the run.",
                run.spec_id,
            )
            run.publish_as_draft = True
            publish_result = run_single_phase(run, "publish", repo_root)
            if publish_result != "passed":
                logger.error("Draft publish also failed: %s", run.last_error)
        else:
            logger.warning(
                "Skipping draft publish for %s because verify never passed and no PR exists yet.",
                run.spec_id,
            )
    _emit_retry_cap_escalation_summary(
        run,
        repo_root,
        terminal_reason=policy.terminal_reason,
    )
    return _transition_to_blocked_with_debugger(
        run,
        repo_root,
        source_phase=policy.retry_cap_source_phase,
        block_error=_retry_resume_message(
            run,
            "Retry cap reached.",
        ),
    )


def _apply_workflow_failure_policy(
    run: RunState,
    repo_root: Path,
    *,
    phase: str,
    phase_order: list[str],
    current_retry_cap: int,
    merge_race_retries: int,
    policy: WorkflowFailurePolicy,
) -> tuple[str | None, int, int]:
    retry_reason = policy.retry_reason
    if phase == "verify":
        guided_retry_prepared = _maybe_prepare_debugger_guided_retry(
            run,
            repo_root,
        )
        if run.status == "waiting-for-input":
            logger.info("Debugger escalated %s to waiting-for-input during verify failure handling.", run.spec_id)
            return "waiting-for-input", phase_order.index(phase), merge_race_retries
        if not guided_retry_prepared:
            no_progress_result = _maybe_trip_no_progress_circuit_breaker(
                run,
                repo_root,
            )
            if no_progress_result == "blocked":
                logger.warning("No-progress circuit breaker blocked %s", run.spec_id)
                return (
                    _transition_to_blocked_with_debugger(
                        run,
                        repo_root,
                        source_phase="verify",
                    ),
                    phase_order.index(phase),
                    merge_race_retries,
                )
        if run.merge_conflict_error and not _is_merge_conflict(run.last_error):
            run.merge_conflict_error = ""
        retry_reason = "merge_conflicts" if run.merge_conflict_error or _is_merge_conflict(run.last_error) else "verify_failures"

    if policy.confirm_merge_conflict and not _confirm_merge_conflict_retry(
        run,
        repo_root,
        context=f"{phase} failure",
    ):
        logger.error("Phase %s failed: %s", phase, run.last_error)
        return "failed", phase_order.index(phase), merge_race_retries

    if policy.direct_phase_retry:
        remaining_merge_retries = max(0, current_retry_cap - _convergence_attempts(run))
        if merge_race_retries >= remaining_merge_retries:
            logger.warning(
                "Merge-race retry cap reached (%d/%d) for %s.",
                merge_race_retries,
                remaining_merge_retries,
                run.spec_id,
            )
            return "failed", phase_order.index(phase), merge_race_retries
        merge_race_retries += 1
        logger.info(
            "Merge blocked by a GitHub merge race, retrying merge (%d/%d): %s",
            merge_race_retries,
            remaining_merge_retries,
            run.last_error,
        )
        return None, phase_order.index(policy.next_phase), merge_race_retries

    if _consume_retry_budget(run, retry_reason, current_retry_cap):
        if retry_reason == "merge_conflicts":
            run.merge_conflict_error = run.last_error
        if phase == "verify" and not run.verify_passed_once:
            logger.info("Deferring publish until verify passes")
        retry_label = RETRY_REASON_LABELS.get(retry_reason, retry_reason)
        if phase == "verify":
            logger.info(
                policy.retry_log_message,
                retry_label,
                run.attempts,
                current_retry_cap,
            )
        elif phase == "merge" and policy.next_phase == "verify":
            logger.info(
                policy.retry_log_message,
                run.spec_id,
                run.attempts,
                current_retry_cap,
            )
        elif phase == "review" and policy.retry_reason == "review_changes":
            logger.info(
                policy.retry_log_message,
                run.attempts,
                current_retry_cap,
            )
        else:
            logger.info(
                policy.retry_log_message,
                run.attempts,
                current_retry_cap,
                run.last_error,
            )
        return None, phase_order.index(policy.next_phase), merge_race_retries

    return _handle_retry_cap_for_policy(run, repo_root, policy), phase_order.index(phase), merge_race_retries


def _transition_to_blocked_with_debugger(
    run: RunState,
    repo_root: Path,
    *,
    source_phase: str,
    block_error: str | None = None,
) -> str:
    # Run the debugger BEFORE overwriting last_error so it sees the real
    # failure reason, not a generic retry-cap message (F3).
    original_last_error = run.last_error
    run.status = "blocked"
    _maybe_run_block_debugger(
        run,
        repo_root,
        source_phase=source_phase,
    )
    # Apply the caller-supplied block_error (e.g. retry-resume message).
    # Always preserve the original block reason alongside the retry
    # instructions so the user sees the real gate/review/merge failure,
    # regardless of whether the debugger succeeded.
    if block_error is not None:
        if original_last_error:
            run.last_error = f"{original_last_error}\n{block_error}"
        else:
            run.last_error = block_error
    else:
        run.last_error = original_last_error
    run.save(repo_root)
    return run.status


def _wait_for_agent_capacity_window(
    run: RunState,
    repo_root: Path,
    delay_seconds: float,
) -> None:
    """Wait out a provider quota window without letting the run lease expire."""
    delay_seconds = max(1.0, min(delay_seconds, AGENT_CAPACITY_MAX_BACKOFF_SECONDS))
    deadline = time.monotonic() + delay_seconds
    run.status = "running"
    run.save(repo_root)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _refresh_active_run_lease(repo_root, run, "implement")
        _poll_sleep(min(remaining, AGENT_CAPACITY_LEASE_REFRESH_SECONDS))


def run_full_workflow(
    run: RunState,
    repo_root: Path,
    *,
    retry_cap: int = RETRY_CAP,
) -> str:
    """Run all phases with shared retry handling."""
    _normalize_logger_state()
    run.retry_cap = retry_cap
    try:
        with _orchestrator_sigterm_guard(run, repo_root):
            # Install the graceful cleanup path before publishing a Windows
            # control token. A concurrent `spec stop` may act as soon as the
            # token reaches disk.
            _ensure_orchestrator_process_group(run, repo_root)
            if not run.worktree_path:
                try:
                    run.worktree_path = str(resolve_worktree_path(run, repo_root))
                except Exception:
                    pass
            phase_order = list(PHASES)
            phase_idx = phase_order.index(run.phase) if run.phase in phase_order else 0
            merge_race_retries = 0
            if run.status == "passed" and run.phase in phase_order:
                if _passed_implement_run_requires_fresh_attempt(repo_root, run):
                    current_retry_cap = _refresh_run_retry_cap_from_disk(
                        repo_root,
                        run,
                        fallback=retry_cap,
                    )
                    if not _schedule_operator_guided_implement_attempt(
                        repo_root,
                        run,
                        retry_cap=current_retry_cap,
                    ):
                        return run.status
                else:
                    phase_idx += 1

            # When resuming past bootstrap (e.g. agent changed from codex to claude),
            # ensure sandbox config matches the current agent.
            if phase_idx > 0:
                worktree_path = resolve_worktree_path(run, repo_root)
                if worktree_path.is_dir():
                    try:
                        backend = _resolve_execution_backend()
                        if _backend_uses_provider_sandbox_config(backend):
                            _write_sandbox_config(run.agent, worktree_path)
                    except Exception as exc:
                        run.last_error = f"Failed to write sandbox config on resume: {exc}"
                        run.save(repo_root)
                        return "failed"

            while phase_idx < len(phase_order):
                phase = phase_order[phase_idx]
                result = run_single_phase(run, phase, repo_root)

                if result == "passed":
                    if phase == "implement" and _passed_implement_run_requires_fresh_attempt(
                        repo_root,
                        run,
                    ):
                        current_retry_cap = _refresh_run_retry_cap_from_disk(
                            repo_root,
                            run,
                            fallback=retry_cap,
                        )
                        if not _schedule_operator_guided_implement_attempt(
                            repo_root,
                            run,
                            retry_cap=current_retry_cap,
                        ):
                            return run.status
                        continue
                    _handle_successful_phase_transition(run, phase)
                    phase_idx += 1
                    continue

                if result == "waiting-for-input":
                    logger.info(
                        "Waiting for input on %s. Run: spec input --spec %s",
                        run.spec_id,
                        run.spec_id,
                    )
                    return "waiting-for-input"

                if result == "blocked":
                    logger.info(
                        "Phase %s blocked: %s",
                        phase,
                        run.last_error,
                    )
                    return _transition_to_blocked_with_debugger(
                        run,
                        repo_root,
                        source_phase=phase,
                    )

                capacity_delay = (
                    _agent_capacity_retry_delay_seconds(run.last_error)
                    if phase == "implement"
                    else None
                )
                if capacity_delay is not None:
                    logger.warning(
                        "Agent provider capacity exhausted for %s; waiting %.0fs "
                        "before relaunching the same implement attempt.",
                        run.spec_id,
                        capacity_delay,
                    )
                    _emit_user_progress(
                        f"[spec] {run.spec_id}: agent capacity exhausted; "
                        f"waiting {capacity_delay:.0f}s without consuming an attempt"
                    )
                    _wait_for_agent_capacity_window(
                        run,
                        repo_root,
                        capacity_delay,
                    )
                    run.last_error = ""
                    run.status = "pending"
                    run.save(repo_root)
                    continue

                current_retry_cap = _refresh_run_retry_cap_from_disk(
                    repo_root,
                    run,
                    fallback=retry_cap,
                )
                policy = _workflow_failure_policy(run, phase)
                if policy is None:
                    logger.error("Phase %s failed: %s", phase, run.last_error)
                    return "failed"

                terminal_status, phase_idx, merge_race_retries = _apply_workflow_failure_policy(
                    run,
                    repo_root,
                    phase=phase,
                    phase_order=phase_order,
                    current_retry_cap=current_retry_cap,
                    merge_race_retries=merge_race_retries,
                    policy=policy,
                )
                if terminal_status is not None:
                    return terminal_status
                continue
    except OrchestratorTerminationRequested as exc:
        run.last_error = str(exc) or "stopped by user"
        run.status = "failed"
        run.save(repo_root)
        return "failed"
    finally:
        _set_active_agent_process(None)

    run.status = "passed"
    run.save(repo_root)
    return "passed"


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def _format_intake_answer(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return json.dumps(value)
    return json.dumps(value, sort_keys=True)


def _print_nonfatal_warnings(warnings: list[dict[str, object]]) -> None:
    warnings = _coerce_nonfatal_warnings(warnings)
    if not warnings:
        return
    print("  Nonfatal Warnings:")
    for warning in warnings:
        summary = str(warning.get("summary") or "").strip() or "warning recorded"
        phase = str(warning.get("phase") or "").strip()
        subtype = str(warning.get("failure_subtype") or "").strip()
        prefix = f"[{phase}]" if phase else "[warning]"
        if subtype:
            prefix += f" {subtype}:"
        else:
            prefix += ":"
        print(f"    {prefix} {summary}")
        action = str(warning.get("action") or "").strip()
        if action:
            print(f"    Action: {action}")
        detail = str(warning.get("detail") or "").strip()
        if detail:
            print(f"    Detail: {detail}")


def _print_intake_status(repo_root: Path, spec_path: Path, run: RunState | None) -> None:
    print("\nIntake:")
    try:
        intake_spec = parse_intake_spec(spec_path)
    except ValueError as exc:
        print("  State: invalid")
        print(f"  Error: {exc}")
        return

    if not intake_spec.required:
        print("  State: not-required")
        return

    intake_result = IntakeResult.load(repo_root, run.run_id) if run else None
    if intake_result is None:
        print("  State: pending")
        pending = [question.id for question in intake_spec.questions]
        print(f"  Pending Questions: {', '.join(pending) if pending else 'none'}")
        return

    expected_schema_hash = intake_spec.schema_hash()
    if intake_result.schema_version != intake_spec.schema_version or intake_result.schema_hash != expected_schema_hash:
        print("  State: re-intake-required")
        print("  Reason: stored answers are incompatible with current intake schema")
        pending = [question.id for question in intake_spec.questions]
        print(f"  Pending Questions: {', '.join(pending) if pending else 'none'}")
        return

    validation_errors = _validate_intake_answers(
        intake_spec,
        intake_result.answers,
    )
    pending = _pending_intake_questions(intake_spec, intake_result.answers)
    if validation_errors:
        print("  State: re-intake-required")
        print(f"  Reason: {validation_errors[0]}")
    else:
        print("  State: completed")
        print(f"  Captured At: {intake_result.completed_at}")
    print(f"  Pending Questions: {', '.join(pending) if pending else 'none'}")

    if isinstance(intake_result.answers, dict) and intake_result.answers:
        print("  Answers:")
        for question in intake_spec.questions:
            if question.id in intake_result.answers:
                answer = intake_result.answers[question.id]
                print(f"    {question.id}: {_format_intake_answer(answer)}")


def cmd_spec(args: argparse.Namespace) -> int:
    """Launch an interactive spec-authoring session in a dedicated worktree."""
    repo_root = resolve_repo_root()
    agent = args.agent or SPEC_RUNTIME_CONFIG.agents.default
    base_ref = args.base or BASE_REF
    raw_spec_id = str(getattr(args, "spec", "") or getattr(args, "label", "")).strip()

    if agent not in VALID_AGENTS:
        print(f"Error: AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1
    if raw_spec_id and not SPEC_ID_RE.fullmatch(raw_spec_id):
        print(
            "Error: SPEC must be a lowercase slug matching `[a-z0-9][a-z0-9-]*`.",
            file=sys.stderr,
        )
        return 1
    if not sys.stdin.isatty():
        print(
            "Error: spec authoring requires an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    spec_id = raw_spec_id or None

    try:
        worktree_path, branch, resumed = _prepare_spec_authoring_worktree(
            repo_root,
            spec_id,
            base_ref=base_ref,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    preexisting_spec_ids = _authored_spec_ids(worktree_path)
    _write_sandbox_config(agent, worktree_path)
    author_cmd = _build_spec_authoring_command(
        agent,
        repo_root,
        worktree_path,
        spec_id,
        branch,
        resume=resumed,
    )

    print(f"{'Resuming' if resumed else 'Launching'} spec authoring in {worktree_path}")

    try:
        completed = subprocess.run(author_cmd, cwd=worktree_path)
    except FileNotFoundError as exc:
        print(f"Error: Agent binary not found: {exc}", file=sys.stderr)
        return 1

    if completed.returncode != 0:
        print(
            f"Spec-authoring session exited before completion. Worktree preserved at {worktree_path}.",
            file=sys.stderr,
        )
        return completed.returncode or 1

    if spec_id:
        # Single-spec path (--spec provided)
        try:
            resolved_spec_id, resolved_branch = _resolve_completed_spec_authoring_result(
                worktree_path,
                spec_id,
                preexisting_spec_ids,
            )
        except RuntimeError as exc:
            print(
                f"Error: {exc}. Worktree preserved at {worktree_path}.",
                file=sys.stderr,
            )
            return 1

        _print_spec_authoring_summary(
            repo_root,
            resolved_spec_id,
            resolved_branch,
            worktree_path,
        )
        print(f"Next step: spec implement --spec {resolved_spec_id}")
    else:
        # Multi-spec path (anonymous session)
        try:
            resolved_spec_ids, resolved_branch = _resolve_completed_multi_spec_authoring_result(
                worktree_path,
                preexisting_spec_ids,
            )
        except RuntimeError as exc:
            print(
                f"Error: {exc}. Worktree preserved at {worktree_path}.",
                file=sys.stderr,
            )
            return 1

        _print_multi_spec_authoring_summary(
            repo_root,
            resolved_spec_ids,
            resolved_branch,
            worktree_path,
        )
        print("\nNext steps:")
        for sid in resolved_spec_ids:
            print(f"  spec implement --spec {sid}")
    return 0


def cmd_steer(args: argparse.Namespace) -> int:
    """Attach proactive operator steering to the latest run for a spec."""
    repo_root = resolve_repo_root()
    spec_id = args.spec
    message = str(getattr(args, "message", "") or "").strip()
    if not message:
        print("Error: Steering guidance cannot be empty.", file=sys.stderr)
        return 1

    run = _latest_non_superseded_run(repo_root, spec_id)
    if run is None:
        print(f"Error: No run found for spec {spec_id}", file=sys.stderr)
        return 1

    try:
        previous = OperatorSteering.load(repo_root, run.run_id)
        steering = _record_operator_steering(
            repo_root,
            run,
            message,
            source="spec steer",
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    action = "Replaced" if previous is not None and previous.status == "active" else "Recorded"
    print(f"{action} proactive steering for {spec_id}.")
    print(f"  Guidance: {steering.message}")
    print(f"  Provided By: {steering.provided_by or 'unknown'}")
    print(f"  Provided At: {steering.provided_at}")
    print("  Effect: advisory context for the next implement attempt")
    return 0


def cmd_input(args: argparse.Namespace) -> int:
    """Launch an interactive session to resolve an operator intervention."""
    repo_root = resolve_repo_root()
    spec_id = args.spec

    if not sys.stdin.isatty():
        print(
            "Error: input resolution requires an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    run = RunState.find_latest(repo_root, spec_id)
    if run is None:
        print(f"Error: No run found for spec {spec_id}", file=sys.stderr)
        return 1
    if not getattr(run, "_agent_was_recorded", True):
        run.agent = ""
    request = _ensure_operator_request(
        repo_root,
        run,
        allow_debugger_promotion=True,
    )
    if request is None:
        print(
            f"Error: Latest run for {spec_id} has status '{run.status}' and no active operator intervention.",
            file=sys.stderr,
        )
        return 1
    if run.status != "waiting-for-input":
        if request.status != "pending":
            print(
                f"Error: Latest run for {spec_id} has status '{run.status}', not 'waiting-for-input'.",
                file=sys.stderr,
            )
            return 1
        run.status = "waiting-for-input"
        run.save(repo_root)

    worktree_path = _resolve_existing_workspace_path(run, repo_root)
    if not worktree_path.is_dir():
        print(f"Error: Worktree not found: {worktree_path}", file=sys.stderr)
        return 1

    requested_agent = getattr(args, "agent", None)
    agent = requested_agent or run.agent or SPEC_RUNTIME_CONFIG.agents.default

    # Build context for the interactive prompt
    spec_file = worktree_path / _spec_path_for_run(run)
    spec_content = ""
    if spec_file.exists():
        spec_content = spec_file.read_text()

    request_label = "agent question" if request.kind == "agent_question" else "debugger guidance"
    question = request.prompt or "No specific request recorded."
    request_details = [question]
    if request.suggested_action:
        request_details.append(f"Suggested action: {request.suggested_action}")
    if request.options:
        options_text = "; ".join(
            f"{option.get('label') or option.get('value')}: {option.get('description') or option.get('value')}"
            for option in request.options
        )
        request_details.append(f"Options: {options_text}")
    request_summary = "\n".join(request_details)

    # Recent git log for context
    log_result = run_subprocess(
        ["git", "log", "--oneline", "-20"],
        cwd=worktree_path,
    )
    recent_log = log_result.stdout.strip() if log_result.returncode == 0 else ""

    # Review findings if present
    review_result = ReviewResult.load(repo_root, run.run_id)
    review_section = ""
    if review_result and review_result.findings:
        findings_text = "\n".join(f"- [{f.severity}] {f.title}: {f.body}" for f in review_result.findings)
        review_section = f"\n\nReview findings from prior run:\n{findings_text}"

    exit_suffix = _agent_exit_suffix(agent)
    prompt = (
        f"You are in an interactive operator-intervention session for spec {spec_id}.\n"
        f"\n"
        f"The run is waiting for operator intervention. Resolve the {request_label} below.\n"
        f"\n"
        f"## Active operator request\n"
        f"Kind: {request.kind}\n"
        f"{request_summary}\n"
        f"\n"
        f"## Spec content\n"
        f"{spec_content}\n"
        f"\n"
        f"## Recent commits in worktree\n"
        f"{recent_log}\n"
        f"{review_section}\n"
        f"\n"
        f"## Your task\n"
        f"1. Discuss the intervention with the user and reach a concrete resolution.\n"
        f"2. Make any necessary code changes based on the resolution.\n"
        f"3. Run {_format_verify_commands(_non_e2e_verify_commands())} to verify.\n"
        f"4. Commit your changes.\n"
        f"5. When done, report STATUS=ok with "
        f"`spec report --status ok --summary 'plain text summary'` describing the operator response and what changed. "
        f"Keep the value shell-safe and do not include backticks or `$()` in it.\n"
        f"6. After the required changes, verification, commit, and "
        f"`spec report --status ok --summary 'plain text summary'` step are complete, clearly tell "
        f"the user to exit the session{exit_suffix} so control returns to the orchestrator "
        f"for the next step.\n"
    )

    state_dir = _state_root(repo_root)
    _write_sandbox_config(agent, worktree_path)
    initial_prompt = f"Resolve the {request_label} for spec {spec_id}. Request: {question}"
    try:
        adapter = get_agent_adapter(agent)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    agent_cmd = adapter.build_authoring_command(
        prompt=prompt,
        worktree_path=worktree_path,
        state_dir=state_dir,
        initial_prompt=initial_prompt,
    )

    print(f"Launching operator intervention for {spec_id} in {worktree_path}")
    print(f"Request kind: {request.kind}")
    print(f"Prompt: {question}")
    print(
        f"Tip: when input work is done, exit the agent session{exit_suffix} "
        "so the orchestrator can continue with the next step."
    )

    pre_session_result = ImplementResult.load(repo_root, run.run_id)
    try:
        completed = subprocess.run(agent_cmd, cwd=worktree_path)
    except FileNotFoundError as exc:
        print(f"Error: Agent binary not found: {exc}", file=sys.stderr)
        return 1

    # Trust `spec report --status ok` over the subprocess exit code: an agent
    # may exit non-zero (e.g. SIGINT from Ctrl+C) after already recording a
    # successful ImplementResult. phase_scoping() uses the same pattern.
    impl_result = ImplementResult.load(repo_root, run.run_id)
    if _fresh_input_completion(
        before=pre_session_result,
        after=impl_result,
        attempt=run.attempts,
        requested_at=request.requested_at,
    ):
        response = (
            impl_result.summary.strip()
            or request.response.strip()
            or request.suggested_action.strip()
            or "Resolved via interactive operator session."
        )
        try:
            continuation = resolve_operator_request(
                repo_root,
                run,
                response,
                source="spec input",
                session_completed_implement=True,
                allow_debugger_promotion=True,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Operator intervention resolved for {spec_id}. Resuming workflow...")
        resume_args = argparse.Namespace(
            spec=spec_id,
            agent=run.agent or SPEC_RUNTIME_CONFIG.agents.default,
            review_agent=run.review_agent or _configured_review_agent_default() or run.agent,
            base=run.base_ref or BASE_REF,
            run=run.run_id,
            new=False,
            retry_cap=run.retry_cap,
            reset_intake=False,
        )
        if continuation.continues_workflow:
            logger.info("Continuing workflow after completed input session for run %s", run.run_id)
        return cmd_run(resume_args)

    print(
        f"Operator-intervention session ended but no STATUS=ok was reported. "
        f"Worktree preserved at {worktree_path}. "
        f"Run `spec input --spec {spec_id}` again or manually report completion.",
        file=sys.stderr,
    )
    return completed.returncode or 1


def _task_run_token() -> str:
    """Generate a short token for task run identification."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + hashlib.sha256(os.urandom(8)).hexdigest()[:6]


def _create_task_run(
    repo_root: Path,
    *,
    agent: str,
    review_agent: str,
    base_ref: str,
) -> RunState:
    """Create a RunState for a task-mode run."""
    token = _task_run_token()
    spec_id = f"task-{token}"
    branch = f"{TASK_BRANCH_PREFIX}{spec_id}--{token}"
    worktree_name = f"task-{spec_id}--{token}"
    worktree_path = _worktrees_root(repo_root) / worktree_name

    run_id = _run_id(spec_id)
    run = RunState(
        run_id=run_id,
        spec_id=spec_id,
        branch=branch,
        worktree_path=str(worktree_path),
        run_mode="task",
        spec_path=_default_spec_path(spec_id, "task"),
        agent=agent,
        review_agent=review_agent,
        base_ref=base_ref,
        requested_by=_current_actor(),
        backend=SPEC_RUNTIME_CONFIG.execution.backend,
        safety_mode=SPEC_RUNTIME_CONFIG.execution.safety_mode,
        backend_source=(
            "repo-config" if SPEC_RUNTIME_CONFIG.execution.backend_explicit else "default"
        ),
        backend_workspace_root=SPEC_RUNTIME_CONFIG.execution.workspace_root,
    )
    run.save(repo_root)
    return run


def cmd_task(args: argparse.Namespace) -> int:
    """Describe and execute a task in one shot via scoping + implementation."""
    repo_root = resolve_repo_root()
    agent = args.agent or SPEC_RUNTIME_CONFIG.agents.default
    review_agent = getattr(args, "review_agent", "") or _configured_review_agent_default() or agent
    base_ref = args.base or BASE_REF

    if agent not in VALID_AGENTS:
        print(f"Error: AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1
    if review_agent not in VALID_AGENTS:
        print(f"Error: REVIEW_AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1

    try:
        run = _create_task_run(repo_root, agent=agent, review_agent=review_agent, base_ref=base_ref)
        logger.info("Starting new task run %s", run.run_id)
        retry_cap = getattr(args, "retry_cap", None) or run.retry_cap
        result = run_full_workflow(run, repo_root, retry_cap=retry_cap)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result == "passed":
        print(f"Task completed successfully: {run.spec_id}")
        return 0
    elif result == "blocked":
        print(f"Task blocked: {run.last_error}")
        return 2
    else:
        print(f"Task failed: {run.last_error}")
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Start or resume a full workflow run."""
    repo_root = resolve_repo_root()
    spec_id = args.spec
    run: RunState | None = None
    lease_run: RunState | None = None
    result = "failed"
    selected_run_id = getattr(args, "run", None)
    force_new_run = str(selected_run_id or "").strip().lower() == "new"
    if force_new_run:
        selected_run_id = ""
    agent = args.agent or SPEC_RUNTIME_CONFIG.agents.default
    review_agent = getattr(args, "review_agent", "") or _configured_review_agent_default() or agent
    base_ref = args.base or BASE_REF
    branch = str(getattr(args, "branch", "") or "").strip()
    reset_intake = bool(getattr(args, "reset_intake", False))

    if agent not in VALID_AGENTS:
        print(f"Error: AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1
    if review_agent not in VALID_AGENTS:
        print(f"Error: REVIEW_AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1

    def planned_new_run_lease() -> tuple[RunState, str, str | None]:
        preallocated_run_id = os.environ.get("SPEC_PREALLOCATED_RUN_ID", "").strip()
        previous_preallocated_run_id = os.environ.get("SPEC_PREALLOCATED_RUN_ID")
        if not preallocated_run_id.startswith(f"{spec_id}-"):
            preallocated_run_id = _run_id(spec_id)
        run_token = _run_token_for_spec(spec_id, preallocated_run_id)
        planned_branch = branch.strip() or spec_run_branch(spec_id, run_token)
        planned_worktree_path = _worktrees_root(repo_root) / spec_run_worktree_name(spec_id, run_token)
        return (
            RunState(
                run_id=preallocated_run_id,
                spec_id=spec_id,
                branch=planned_branch,
                worktree_path=str(planned_worktree_path),
                spec_path=_default_spec_path(spec_id, "spec"),
                agent=agent,
                review_agent=review_agent,
                base_ref=base_ref,
                requested_by=_current_actor(),
            ),
            preallocated_run_id,
            previous_preallocated_run_id,
        )

    try:
        with SpecLock(repo_root, spec_id):
            latest_existing_run = _latest_non_superseded_run(repo_root, spec_id, ensure_identity=False)
            preallocated_run_id = ""
            previous_preallocated_run_id: str | None = None
            if selected_run_id:
                lease_run = RunState(
                    run_id=selected_run_id,
                    spec_id=spec_id,
                    branch="",
                    spec_path=_default_spec_path(spec_id, "spec"),
                    agent=agent,
                    review_agent=review_agent,
                    base_ref=base_ref,
                    requested_by=_current_actor(),
                )
                _acquire_coordinator_lease(repo_root, lease_run, args=args)
                run = _select_resumable_run(
                    repo_root,
                    spec_id,
                    run_id=selected_run_id,
                    ensure_identity=False,
                )
            elif force_new_run:
                run = None
            else:
                if latest_existing_run is not None:
                    lease_run = latest_existing_run
                else:
                    lease_run, preallocated_run_id, previous_preallocated_run_id = planned_new_run_lease()
                _acquire_coordinator_lease(repo_root, lease_run, args=args)
                run = _select_default_run(
                    repo_root,
                    spec_id,
                    retry_cap=getattr(args, "retry_cap", None),
                    ensure_identity=False,
                )

            if run is None:
                if is_spec_merged(repo_root, spec_id):
                    raise RuntimeError(
                        f"Spec '{spec_id}' is already merged on {BASE_REF}; refusing to start a new implementation run."
                    )
                # Dispatch discipline: never supersede a run whose branch
                # holds committed work by reimplementing from scratch. Resumable
                # runs were already returned above; if we reach here with an
                # un-resumable run that still has commits ahead of base, refuse
                # to start a fresh run and surface what work exists and where.
                # An explicit operator override (RUN=new / --force-new-run) is
                # honored, and a caller-selected run id has its own path.
                if (
                    not force_new_run
                    and not selected_run_id
                    and latest_existing_run is not None
                    and _run_branch_has_committed_work(repo_root, latest_existing_run)
                ):
                    commits = _branch_commits_ahead_of_base(
                        repo_root,
                        latest_existing_run.branch,
                        latest_existing_run.base_ref or BASE_REF,
                    )
                    raise RuntimeError(
                        f"Spec '{spec_id}' needs attention: run {latest_existing_run.run_id} has "
                        f"{commits} commit(s) on {latest_existing_run.branch} ahead of "
                        f"{latest_existing_run.base_ref or BASE_REF} "
                        f"(status={latest_existing_run.status}); resume it "
                        f"(RUN={latest_existing_run.run_id}) or clean up the branch before "
                        f"starting a new run — refusing to supersede committed work."
                    )
                if not preallocated_run_id:
                    if lease_run is not None:
                        _release_coordinator_lease(repo_root, lease_run, "reselecting")
                    lease_run, preallocated_run_id, previous_preallocated_run_id = planned_new_run_lease()
                    _acquire_coordinator_lease(repo_root, lease_run, args=args)
                spec_path = _command_spec_path(repo_root, spec_id, None)
                if not spec_path.exists():
                    raise RuntimeError(f"Spec not found: {spec_path}")
                try:
                    os.environ["SPEC_PREALLOCATED_RUN_ID"] = preallocated_run_id
                    run = _create_spec_run(
                        repo_root,
                        spec_id,
                        agent=agent,
                        review_agent=review_agent,
                        base_ref=base_ref,
                        requested_by=_current_actor(),
                        branch=branch,
                    )
                finally:
                    if previous_preallocated_run_id is None or previous_preallocated_run_id == preallocated_run_id:
                        os.environ.pop("SPEC_PREALLOCATED_RUN_ID", None)
                    else:
                        os.environ["SPEC_PREALLOCATED_RUN_ID"] = previous_preallocated_run_id
                if force_new_run or (latest_existing_run is not None and latest_existing_run.status == "failed"):
                    _mark_superseded_runs(
                        repo_root,
                        spec_id,
                        superseded_by_run_id=run.run_id,
                        keep_run_ids={run.run_id},
                    )
                _populate_prior_review_findings(
                    repo_root,
                    run,
                    preferred_prior_run_id=(latest_existing_run.run_id if latest_existing_run is not None else None),
                )
                logger.info("Starting new run %s", run.run_id)
            else:
                if lease_run is None:
                    _acquire_coordinator_lease(repo_root, run, args=args)
                lease_run = run
                run = _ensure_run_identity(run, repo_root)
                spec_path = _command_spec_path(repo_root, spec_id, run)
                if not spec_path.exists():
                    raise RuntimeError(f"Spec not found: {spec_path}")
                run.agent = agent
                run.review_agent = review_agent
                # Preserve the run's saved base_ref (e.g. web handoffs that
                # fell back to HEAD) unless the user explicitly passed --base.
                run.base_ref = args.base or run.base_ref or BASE_REF
                if run.status == "blocked":
                    diagnosis = BlockDiagnosis.load(repo_root, run.run_id)
                    _claim_block_debugger_auto_resume(run, repo_root, diagnosis)
                    logger.info(
                        "Resuming blocked run %s — resetting to failed for retry",
                        run.run_id,
                    )
                    run.status = "failed"
                    # Always reset to implement so the next attempt gets a
                    # full implement pass, regardless of which phase was
                    # blocked.  The human-attention flag only prevents
                    # *automatic* retry — explicit `spec implement` should
                    # still land in implement.
                    run.phase = "implement"
                    if diagnosis is not None and not diagnosis.requires_human_attention:
                        current_sig = _compute_blocker_signature(
                            run,
                            repo_root,
                            source_phase=diagnosis.source_phase or run.phase,
                            block_reason_override=diagnosis.block_reason or None,
                        )
                        if current_sig == diagnosis.blocker_signature:
                            run.pending_block_debugger_signature = diagnosis.blocker_signature
                        else:
                            logger.info(
                                "Blocker signature changed since diagnosis was recorded "
                                "(was %s, now %s) — discarding stale diagnosis",
                                diagnosis.blocker_signature[:12],
                                current_sig[:12],
                            )
                            run.pending_block_debugger_signature = ""
                            run.last_block_debugger_guided_retry_signature = ""
                else:
                    logger.info("Resuming run %s at phase %s", run.run_id, run.phase)

            if reset_intake:
                run.intake_reset_requested = True
                if run.phase in PHASES and PHASES.index(run.phase) > PHASES.index("intake"):
                    run.phase = "intake"
            run.save(repo_root)

            explicit_cap = getattr(args, "retry_cap", None)
            if explicit_cap is not None:
                retry_cap = explicit_cap
            else:
                retry_cap = run.retry_cap
            result = run_full_workflow(
                run,
                repo_root,
                retry_cap=retry_cap,
            )
    except BlockDebuggerAutoResumeExhausted as exc:
        # Keep the terminal blocked state and original diagnosis intact. This
        # is a dispatch refusal, not a workflow failure; converting it to
        # ``failed`` would make the same run eligible for another automatic
        # failed-implement resume path.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if run is not None and _ACTIVE_COORDINATOR_LEASE is not None:
            run.status = "failed"
            run.last_error = str(exc)
            run.save(repo_root)
            lease_run = run
        if lease_run is not None:
            _release_coordinator_lease(repo_root, lease_run, result)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if lease_run is not None:
            _release_coordinator_lease(repo_root, lease_run, result)

    if result == "passed":
        print(f"Workflow completed successfully for {spec_id}")
        return 0
    elif result == "waiting-for-input":
        print(f"Waiting for input on {spec_id}. Run: spec input --spec {spec_id}")
        return 2
    elif result == "blocked":
        print(f"Workflow blocked for {spec_id}: {run.last_error}")
        return 2
    else:
        print(f"Workflow failed for {spec_id}: {run.last_error}")
        return 1


def cmd_step(args: argparse.Namespace) -> int:
    """Run a single phase explicitly."""
    repo_root = resolve_repo_root()
    spec_id = args.spec
    phase = args.phase
    selected_run_id = getattr(args, "run", None)
    agent = args.agent or SPEC_RUNTIME_CONFIG.agents.default
    review_agent = getattr(args, "review_agent", "") or _configured_review_agent_default() or agent
    base_ref = args.base or BASE_REF
    reset_intake = bool(getattr(args, "reset_intake", False))

    if phase not in PHASES:
        print(f"Error: Invalid phase '{phase}'. Must be one of: {', '.join(PHASES)}", file=sys.stderr)
        return 1
    if agent not in VALID_AGENTS:
        print(f"Error: AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1
    if review_agent not in VALID_AGENTS:
        print(f"Error: REVIEW_AGENT must be one of {VALID_AGENTS}", file=sys.stderr)
        return 1

    try:
        with SpecLock(repo_root, spec_id):
            latest_existing_run = _latest_non_superseded_run(repo_root, spec_id)
            run = _select_step_run(
                repo_root,
                spec_id,
                run_id=selected_run_id,
            )

            spec_path = _command_spec_path(repo_root, spec_id, run)
            if not spec_path.exists():
                print(f"Error: Spec not found: {spec_path}", file=sys.stderr)
                return 1

            if run is None:
                if is_spec_merged(repo_root, spec_id):
                    raise RuntimeError(
                        f"Spec '{spec_id}' is already merged on origin/master; "
                        "use RUN=<run-id> to inspect or clean up an existing run."
                    )
                run = _create_spec_run(
                    repo_root,
                    spec_id,
                    agent=agent,
                    review_agent=review_agent,
                    base_ref=base_ref,
                    requested_by=_current_actor(),
                )
                if latest_existing_run is not None and latest_existing_run.status == "failed":
                    _mark_superseded_runs(
                        repo_root,
                        spec_id,
                        superseded_by_run_id=run.run_id,
                        keep_run_ids={run.run_id},
                    )
                _populate_prior_review_findings(
                    repo_root,
                    run,
                    preferred_prior_run_id=(latest_existing_run.run_id if latest_existing_run is not None else None),
                )
                run.phase = phase
            else:
                run.agent = agent
                run.review_agent = review_agent
                run.base_ref = args.base or run.base_ref or BASE_REF
                run.intake_reset_requested = run.intake_reset_requested or reset_intake

            if reset_intake and phase != "intake":
                run.save(repo_root)
            else:
                run.save(repo_root)

            result = run_single_phase(run, phase, repo_root)
            if result == "blocked":
                _transition_to_blocked_with_debugger(
                    run,
                    repo_root,
                    source_phase=phase,
                )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result == "passed":
        print(f"Phase '{phase}' passed for {spec_id}")
        return 0
    elif result == "blocked":
        print(f"Phase '{phase}' blocked for {spec_id}: {run.last_error}")
        return 2
    else:
        print(f"Phase '{phase}' failed for {spec_id}: {run.last_error}")
        return 1


LEASE_HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("SPEC_LEASE_HEARTBEAT_INTERVAL_SECONDS", "60")
)
COORDINATOR_LEASE_TTL_SECONDS = int(os.environ.get("SPEC_COORDINATOR_LEASE_TTL_SECONDS", "900"))


@dataclass
class ActiveCoordinatorLease:
    lease_id: str
    run_id: str
    spec_id: str
    agent: str
    payload: dict[str, object]


_ACTIVE_COORDINATOR_LEASE: ActiveCoordinatorLease | None = None


def _coordination_bypass_enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None and bool(getattr(args, "coordination_bypass", False)):
        return True
    return os.environ.get("SPEC_COORDINATION_BYPASS", "").strip().lower() in {"1", "true", "yes", "on"}


def _coordination_repo_id(repo_root: Path) -> str:
    configured = SPEC_RUNTIME_CONFIG.coordination.repo_id.strip()
    if configured:
        return configured
    try:
        common = resolve_common_root(repo_root)
    except Exception:
        common = repo_root
    return common.name or "repo"


def _coordinator_lease_payload(repo_root: Path, run: RunState, phase: str = "") -> dict[str, object]:
    config = SPEC_RUNTIME_CONFIG.coordination
    payload: dict[str, object] = {
        "repo_id": _coordination_repo_id(repo_root),
        "spec_id": run.spec_id,
        "run_id": run.run_id,
        "machine_id": config.machine_id,
        "agent": run.agent or SPEC_RUNTIME_CONFIG.agents.default,
        "ttl_seconds": COORDINATOR_LEASE_TTL_SECONDS,
        "hostname": config.machine_id,
    }
    if run.worktree_path:
        payload["worktree_path"] = run.worktree_path
    if phase:
        payload["phase"] = phase
    return payload


def _format_lease_conflict(exc: CoordinatorLeaseConflictError) -> str:
    lease = exc.lease
    owner = str(lease.get("machine_id") or lease.get("hostname") or "unknown").strip()
    run_id = str(lease.get("run_id") or "unknown").strip()
    expires_at = str(lease.get("expires_at") or "unknown").strip()
    age = lease_age_seconds(lease)
    age_text = "unknown" if age is None else f"{age:.0f}s"
    return (
        "Coordinator lease is held by another owner: "
        f"owner={owner} heartbeat_age={age_text} expires_at={expires_at} run_id={run_id}"
    )


def _acquire_coordinator_lease(repo_root: Path, run: RunState, *, args: argparse.Namespace | None = None) -> None:
    global _ACTIVE_COORDINATOR_LEASE
    _ACTIVE_COORDINATOR_LEASE = None
    config = SPEC_RUNTIME_CONFIG.coordination
    if not config.enabled or _coordination_bypass_enabled(args):
        return
    try:
        client = build_coordinator_client(config)
        lease = client.acquire_lease(_coordinator_lease_payload(repo_root, run))
    except CoordinatorLeaseConflictError as exc:
        raise RuntimeError(_format_lease_conflict(exc)) from exc
    except CoordinatorError as exc:
        raise RuntimeError(
            "Coordinator is required but unavailable; failing closed. "
            "Run `spec coord doctor` to diagnose coordinator connectivity, auth, and leases. "
            "Use --coordination-bypass only for an emergency local-only run. "
            f"Details: {exc}"
        ) from exc

    lease_id = str(lease.get("lease_id") or "").strip()
    if not lease_id:
        raise RuntimeError("Coordinator acquire response did not include lease_id")
    _ACTIVE_COORDINATOR_LEASE = ActiveCoordinatorLease(
        lease_id=lease_id,
        run_id=run.run_id,
        spec_id=run.spec_id,
        agent=run.agent or SPEC_RUNTIME_CONFIG.agents.default,
        payload=_coordinator_lease_payload(repo_root, run),
    )


def _heartbeat_coordinator_lease(repo_root: Path, run: RunState, phase: str) -> None:
    active = _ACTIVE_COORDINATOR_LEASE
    config = SPEC_RUNTIME_CONFIG.coordination
    if active is None or not config.enabled:
        return
    if active.run_id != run.run_id or active.spec_id != run.spec_id:
        raise RuntimeError("Active coordinator lease does not match current run")
    payload = _coordinator_lease_payload(repo_root, run, phase=phase)
    try:
        build_coordinator_client(config).heartbeat_lease(active.lease_id, payload)
    except CoordinatorLeaseConflictError as exc:
        raise RuntimeError(_format_lease_conflict(exc)) from exc
    except CoordinatorError as exc:
        raise RuntimeError(f"Coordinator lease heartbeat failed closed: {exc}") from exc
    active.payload = payload


def _release_coordinator_lease(repo_root: Path, run: RunState, final_state: str) -> None:
    global _ACTIVE_COORDINATOR_LEASE
    active = _ACTIVE_COORDINATOR_LEASE
    config = SPEC_RUNTIME_CONFIG.coordination
    if active is None or not config.enabled:
        return
    if active.run_id != run.run_id or active.spec_id != run.spec_id:
        return
    payload = _coordinator_lease_payload(repo_root, run, phase=run.phase)
    payload["final_state"] = final_state
    try:
        build_coordinator_client(config).release_lease(active.lease_id, payload)
    except CoordinatorError as exc:
        logger.warning("Failed to release coordinator lease %s for run %s: %s", active.lease_id, run.run_id, exc)
    finally:
        _ACTIVE_COORDINATOR_LEASE = None


def _run_lease_heartbeat_loop(
    repo_root: Path,
    run: RunState,
    phase: str,
    stop: threading.Event,
    failure: LeaseHeartbeatFailure | None = None,
) -> None:
    """Refresh the run lease on a fixed interval until ``stop`` is set.

    Long phases (implement, verify) can run well past the lease timeout. Without
    a periodic refresh the lease classifies as EXPIRED while the orchestrator is
    still alive, causing autopilot to treat the run as stale and re-queue it.
    The interval defaults to 60s so even an aggressive operator-tuned timeout
    sees several refreshes within the window. Coordinator heartbeat failures
    are reported through ``failure`` so active mutating phases fail closed
    instead of continuing after lease ownership is lost.
    """

    interval = max(LEASE_HEARTBEAT_INTERVAL_SECONDS, 0.05)
    while not stop.wait(interval):
        try:
            _refresh_active_run_lease(repo_root, run, phase)
        except Exception as exc:  # noqa: BLE001 - active phases fail closed on ownership loss
            if failure is not None:
                failure.fail(exc)
            _terminate_registered_agent_process()
            logger.exception(
                "Lease heartbeat refresh raised for run %s phase %s",
                run.run_id,
                phase,
            )
            stop.set()
            return


def _refresh_active_run_lease(repo_root: Path, run: RunState, phase: str) -> None:
    """Create or refresh a durable lease for an active run.

    The lease records the host, worker pid, phase, and heartbeat so ``spec
    status``/``spec watch`` and process adoption can distinguish a live owner
    from a stale or expired record. Local lease IO failures must never block the
    phase, so those errors are swallowed with a warning. Coordinator heartbeat
    failures propagate because losing distributed ownership must fail closed.
    """

    if not run.run_id:
        return
    _heartbeat_coordinator_lease(repo_root, run, phase)
    try:
        runs_dir = _state_root(repo_root) / "runs"
        existing = load_run_lease(runs_dir, run.run_id)
        process_pid = os.getpid()
        process_started_at = ""
        try:
            identity = read_process_identity(process_pid)
        except Exception:  # noqa: BLE001 - identity probe must not fail the phase
            identity = None
        if identity is not None:
            process_started_at = identity.started_at or ""
        actor = _current_actor()
        if existing is not None and existing.process_pid == process_pid:
            metadata = dict(existing.metadata) if isinstance(existing.metadata, dict) else {}
            if actor:
                metadata["actor"] = actor
            lease = replace(
                existing,
                phase=phase,
                process_pid=process_pid,
                process_started_at=process_started_at or existing.process_started_at,
                metadata=metadata,
            ).with_heartbeat()
        else:
            lease = build_lease(
                run_id=run.run_id,
                spec_id=run.spec_id,
                phase=phase,
                backend=run.backend or SPEC_RUNTIME_CONFIG.execution.backend,
                process_pid=process_pid,
                process_started_at=process_started_at,
                actor=actor,
            )
        save_run_lease(runs_dir, lease)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to refresh lease for run %s: %s", run.run_id, exc)


def _canonical_run_status_for_display(repo_root: Path, run: RunState):
    """Return a canonical status projection for ``run`` or ``None`` on failure.

    The projection is the same one used by ``spec list``, ``spec watch``, and
    autopilot dispatch so that status reports are derived from a single source.
    """
    try:
        runs_dir = _state_root(repo_root) / "runs"
        lease = load_run_lease(runs_dir, run.run_id)
        run_state_dir = runs_dir / run.run_id
        gate_records = ()
        if run_state_dir.exists():
            gate_records = tuple(GateRecordStore(run_state_dir).load())
        is_merged = bool(getattr(run, "is_merged", False))
        process_alive: bool | None = None
        if lease is not None and lease.process_pid:
            try:
                process_alive = is_pid_alive(lease.process_pid, lease.process_started_at)
            except Exception:
                process_alive = None
        return project_run_status(
            run_status=run.status or "",
            lease=lease,
            process_alive=process_alive,
            gate_records=gate_records,
            is_merged=is_merged,
        )
    except (OSError, ValueError):
        return None


def _print_effective_autopilot_policy(repo_root: Path) -> None:
    from .autopilot import resolve_autopilot_backend_policy
    from .container import container_image_source

    repo_config = load_spec_runtime_config(require=False, config_path=repo_root / ".spec.toml")
    policy = resolve_autopilot_backend_policy(repo_config)
    source_text = f" ({policy.source})" if policy.source else ""
    print(f"  Backend: {policy.backend or 'unknown'}{source_text}")
    if policy.backend == "container":
        print(f"  Container Engine: {repo_config.execution.container.engine}")
        print(f"  Worker Source: {container_image_source(repo_config, repo_root)}")
    print(f"  Safety Mode: {policy.safety_mode or 'unknown'}")


def _status_should_show_queued_autopilot_policy(*, run: RunState | None, status: str, is_task_spec: bool) -> bool:
    if run is not None or is_task_spec:
        return False
    return status not in {"merged", "obsolete", "superseded", "task"}


def cmd_status(args: argparse.Namespace) -> int:
    """Display run state and gate status."""
    repo_root = resolve_repo_root()
    spec_id = args.spec
    selected_run_id = getattr(args, "run", None)
    try:
        run = RunState.load(repo_root, selected_run_id) if selected_run_id else RunState.find_latest(repo_root, spec_id)
    except FileNotFoundError:
        print(f"Error: Run '{selected_run_id}' was not found.", file=sys.stderr)
        return 1
    if run is not None:
        if run.spec_id != spec_id:
            print(
                f"Error: Run '{run.run_id}' belongs to spec '{run.spec_id}', not '{spec_id}'",
                file=sys.stderr,
            )
            return 1
        run = _ensure_run_identity(run, repo_root)
    try:
        spec_path = _spec_path_for_run_id(repo_root, run, spec_id)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    git_state = collect_git_spec_state(repo_root)

    if not spec_path.exists():
        print(f"Error: Spec not found: {spec_path}", file=sys.stderr)
        return 1

    # Show spec frontmatter
    fm = parse_spec_frontmatter(spec_path)
    is_task_spec = _is_task_spec_path(spec_path, repo_root)
    if run and run.run_mode == "task":
        status = "task"
    elif is_task_spec:
        status = "task"
    else:
        status = read_spec_status(repo_root, spec_id, spec_path, git_state=git_state)
    print(f"Spec: {spec_id}")
    print(f"  Status: {status}")
    deps = fm.get("depends_on", [])
    if deps:
        print(f"  Dependencies: {', '.join(deps) if isinstance(deps, list) else deps}")

    orphaned = git_state.orphaned_artifacts(spec_id)
    if orphaned:
        print("  Diagnostics:")
        for entry in orphaned:
            print(f"    {entry}")

    runs_for_spec = RunState.list_for_spec(repo_root, spec_id)
    resumable = [
        candidate
        for candidate in runs_for_spec
        if _is_run_workflow_resumable(repo_root, candidate) and not candidate.is_superseded
    ]
    if len(resumable) > 1:
        print("\nResumable Runs:")
        for candidate in resumable:
            candidate = _ensure_run_identity(candidate, repo_root)
            print(f"  {candidate.run_id}: phase={candidate.phase} status={candidate.status} branch={candidate.branch}")
        print("  Selector: use --run <run-id> with spec implement or spec phase.")

    if runs_for_spec:
        print("\nRuns:")
        for candidate in runs_for_spec:
            candidate = _ensure_run_identity(candidate, repo_root)
            if candidate.is_superseded:
                previous_status = candidate.superseded_from_status or "unknown"
                previous_phase = candidate.superseded_from_phase or candidate.phase
                print(f"  Run {candidate.run_id} — superseded (was: {previous_status}, phase: {previous_phase})")
                continue
            print(
                f"  Run {candidate.run_id} — {candidate.status} "
                f"(phase: {candidate.phase}, attempt {format_attempt_progress(candidate.attempts, candidate.retry_cap)})"
            )

    # Show latest run state
    if run:
        label = "Run" if selected_run_id else "Latest Run"
        print(f"\n{label}: {run.run_id}")
        print(f"  Phase: {run.phase}")
        print(f"  Status: {run.status}")
        canonical = _canonical_run_status_for_display(repo_root, run)
        if canonical is not None:
            extra = ""
            if canonical.lease_status is not None:
                extra = f" (lease={canonical.lease_status.value})"
            print(f"  Canonical Status: {canonical.status.value}{extra}")
            if canonical.pending_gates:
                print(f"  Pending Gates: {', '.join(canonical.pending_gates)}")
            if canonical.timed_out_gates:
                print(f"  Timed Out Gates: {', '.join(canonical.timed_out_gates)}")
            if canonical.failed_gates:
                print(f"  Failed Gates: {', '.join(canonical.failed_gates)}")
            for warning in canonical.warnings:
                print(f"  Warning: {warning}")
        print(f"  Agent: {run.agent}")
        if run.backend or run.safety_mode:
            backend_text = run.backend or "unknown"
            source_text = f" ({run.backend_source})" if run.backend_source else ""
            print(f"  Backend: {backend_text}{source_text}")
            if backend_text == "container":
                repo_config = load_spec_runtime_config(require=False, config_path=repo_root / ".spec.toml")
                from .container import container_image_source

                print(f"  Container Engine: {repo_config.execution.container.engine}")
                print(f"  Worker Source: {container_image_source(repo_config, repo_root)}")
            print(f"  Safety Mode: {run.safety_mode or 'unknown'}")
        print(f"  Attempts: {format_attempt_progress(_convergence_attempts(run), run.retry_cap)}" + (f" (+{run.merge_conflicts} merge conflict retries)" if run.merge_conflicts else ""))
        print(f"  Mode: {run.run_mode}")
        print(f"  Branch: {run.branch}")
        print(f"  Worktree: {resolve_worktree_path(run, repo_root)}")
        print(f"  Spec Path: {_spec_path_for_run(run)}")
        if run.spec_revision:
            print(f"  Spec Revision: {run.spec_revision}")
        if run.requested_by:
            print(f"  Actor: {run.requested_by}")
        if run.last_error:
            print(f"  Last Error: {run.last_error}")
        if run.readiness_status or run.readiness_head_sha or run.readiness_blocker:
            print("  Readiness:")
            print(f"    State: {run.readiness_status or 'unknown'}")
            if run.readiness_head_sha:
                print(f"    Head: {_short_sha(run.readiness_head_sha)}")
            if run.readiness_blocker:
                print(f"    Blocker: {run.readiness_blocker}")
        if run.nonfatal_warnings:
            _print_nonfatal_warnings(run.nonfatal_warnings)
        print(f"  Updated: {run.updated_at}")
    else:
        print("\nNo runs found.")
        if _status_should_show_queued_autopilot_policy(run=run, status=status, is_task_spec=is_task_spec):
            _print_effective_autopilot_policy(repo_root)

    if run:
        operator_request = _load_operator_request(repo_root, run)
        if operator_request is not None:
            artifact_path = _state_root(repo_root) / "runs" / run.run_id / OPERATOR_REQUEST_FILENAME
            print("\nOperator Intervention:")
            print(f"  Kind: {operator_request.kind}")
            print(f"  Status: {operator_request.status}")
            print(f"  Prompt: {operator_request.prompt}")
            if operator_request.suggested_action:
                print(f"  Suggested Action: {operator_request.suggested_action}")
            if operator_request.options:
                options = ", ".join(
                    option.get("label") or option.get("value") or "option" for option in operator_request.options
                )
                print(f"  Options: {options}")
            if operator_request.response:
                print(f"  Response: {operator_request.response}")
            print(f"  Requires Full Session: {'yes' if operator_request.requires_full_session else 'no'}")
            print(f"  Artifact: {_try_relative_posix(artifact_path, repo_root)}")
        operator_steering = OperatorSteering.load(repo_root, run.run_id)
        if operator_steering is not None:
            artifact_path = _state_root(repo_root) / "runs" / run.run_id / OPERATOR_STEERING_FILENAME
            print("\nOperator Steering:")
            print(f"  Status: {operator_steering.status}")
            print(f"  Guidance: {operator_steering.message}")
            print(f"  Provided By: {operator_steering.provided_by or 'unknown'}")
            print(f"  Provided At: {operator_steering.provided_at or 'unknown'}")
            if operator_steering.source:
                print(f"  Source: {operator_steering.source}")
            if operator_steering.influenced_attempt_number is not None:
                print(f"  Influenced Attempt: {operator_steering.influenced_attempt_number}")
            if operator_steering.superseded_by_event_id:
                print(f"  Superseded By: {operator_steering.superseded_by_event_id}")
            print(f"  Artifact: {_try_relative_posix(artifact_path, repo_root)}")
            history = OperatorSteering.list_events(repo_root, run.run_id)
            if history:
                print("  History:")
                for event in history:
                    summary = (
                        f"{event.event_id} [{event.status}] by {event.provided_by or 'unknown'} "
                        f"at {event.provided_at or 'unknown'}"
                    )
                    if event.influenced_attempt_number is not None:
                        summary += f" -> attempt {event.influenced_attempt_number}"
                    if event.superseded_by_event_id:
                        summary += f" superseded-by {event.superseded_by_event_id}"
                    print(f"    {summary}")

    if run and run.status == "blocked":
        diagnosis = BlockDiagnosis.load(repo_root, run.run_id)
        if diagnosis is not None:
            artifact_path = _state_root(repo_root) / "runs" / run.run_id / BLOCK_DIAGNOSIS_FILENAME
            print("\nBlocked Run Diagnosis:")
            print(f"  Summary: {diagnosis.summary}")
            print(f"  Root Cause: {diagnosis.root_cause}")
            print(f"  Confidence: {diagnosis.confidence:.2f}")
            print(f"  Category: {diagnosis.category or 'unknown'}")
            print(f"  Requires Human Attention: {'yes' if diagnosis.requires_human_attention else 'no'}")
            print(f"  Needs New Commit: {'yes' if diagnosis.needs_new_commit else 'no'}")
            print(f"  Blocker Signature: {diagnosis.blocker_signature}")
            print(f"  Artifact: {_try_relative_posix(artifact_path, repo_root)}")
            print(f"  Next Best Action: {diagnosis.next_best_action}")

    _print_intake_status(repo_root, spec_path, run)

    if run:
        current_attempt_number = _current_attempt_number(run)
        lineage_context = ImplementContext.load_attempt(repo_root, run.run_id, current_attempt_number) or ImplementContext.load(
            repo_root,
            run.run_id,
        )
        if lineage_context is not None and (
            lineage_context.triggering_phase
            or lineage_context.triggering_review_result_path
            or lineage_context.previous_implement_result_path
        ):
            print("\nCurrent Attempt Lineage:")
            print(f"  Attempt Number: {lineage_context.attempt_number}")
            if lineage_context.triggering_phase:
                print(f"  Triggering Phase: {lineage_context.triggering_phase}")
            if lineage_context.reviewed_head_sha:
                print(f"  Reviewed Head SHA: {lineage_context.reviewed_head_sha}")
            if lineage_context.triggering_review_result_path:
                print(f"  Triggering Review Artifact: {lineage_context.triggering_review_result_path}")
            if lineage_context.previous_implement_result_path:
                print(f"  Previous Implement Result: {lineage_context.previous_implement_result_path}")
            latest_attempt_result_path = ImplementResult.attempt_path(repo_root, run.run_id, lineage_context.attempt_number)
            print(f"  Latest Implement Result: {_try_relative_posix(latest_attempt_result_path, repo_root)}")

        review_result = ReviewResult.load(repo_root, run.run_id)
        review_result_matches_run = (
            review_result is not None
            and bool(run.review_decision_status)
            and (
                not run.review_expected_head_sha
                or not review_result.reviewed_head_sha
                or review_result.reviewed_head_sha == run.review_expected_head_sha
            )
        )
        if review_result_matches_run:
            print("\nReview Decision:")
            print(f"  Status: {review_result.status or 'unknown'}")
            print(f"  Summary: {review_result.summary or '(none)'}")
            if review_result.reviewed_head_sha:
                print(f"  Reviewed Head SHA: {review_result.reviewed_head_sha}")
            if review_result.reviewed_base_sha:
                print(f"  Reviewed Base SHA: {review_result.reviewed_base_sha}")
            if review_result.source_check_url:
                print(f"  Source Check: {review_result.source_check_url}")
            print(f"  Findings: {len(review_result.findings)}")
        elif run.review_decision_status or run.review_expected_head_sha:
            print("\nReview Decision:")
            print(f"  Status: {run.review_decision_status or 'pending'}")
            if run.review_decision_summary:
                print(f"  Summary: {run.review_decision_summary}")
            if run.review_expected_head_sha:
                print(f"  Expected Head SHA: {run.review_expected_head_sha}")
            if run.review_decision_check_url:
                print(f"  Source Check: {run.review_decision_check_url}")

    # Show gate status
    gate_file, gate_data = _read_gate_status(repo_root, run) if run else (None, None)
    if gate_data is not None:
        try:
            print("\nGate Status:")
            for gate in REQUIRED_GATES:
                gd = gate_data.get("gates", {}).get(gate)
                if gd is None:
                    print(f"  {gate}: not run")
                else:
                    print(f"  {gate}: {gd.get('last_status', 'unknown')} (attempts={gd.get('attempts', 0)})")
        except (AttributeError, TypeError):
            print("\nGate Status: corrupt gate-status.json")
    else:
        print("\nGate Status: no gate results recorded")

    return 0


def _parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration_seconds(started_at: str, finished_at: str) -> float | None:
    start = _parse_iso_datetime(started_at)
    finish = _parse_iso_datetime(finished_at)
    if start is None or finish is None:
        return None
    return max(0.0, (finish - start).total_seconds())


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(value, 3)


def _run_matches_filters(
    run: RunState,
    *,
    spec_id: str | None,
    run_id: str | None,
    since: datetime | None,
) -> bool:
    if spec_id and run.spec_id != spec_id:
        return False
    if run_id and run.run_id != run_id:
        return False
    if since is not None:
        updated = _parse_iso_datetime(run.updated_at or run.created_at)
        if updated is None or updated < since:
            return False
    return True


def _audit_matches_filters(
    payload: dict[str, object],
    *,
    spec_id: str | None,
    run_id: str | None,
    since: datetime | None,
) -> bool:
    if spec_id and str(payload.get("spec_id") or "").strip() != spec_id:
        return False
    if run_id and str(payload.get("run_id") or "").strip() != run_id:
        return False
    if since is not None:
        result = payload.get("result")
        if not isinstance(result, dict):
            return False
        finished = _parse_iso_datetime(str(result.get("finished_at") or ""))
        if finished is None or finished < since:
            return False
    return True


def _compute_orchestrator_analytics(
    repo_root: Path,
    *,
    spec_id: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
) -> dict[str, object]:
    since_dt = _parse_iso_datetime(since or "")
    if since and since_dt is None:
        raise ValueError(f"Invalid --since value: {since}")

    runs = [
        run
        for run in RunState.list_all(repo_root)
        if _run_matches_filters(run, spec_id=spec_id, run_id=run_id, since=since_dt)
    ]
    terminal_run_counts: dict[str, int] = {}
    for run in runs:
        if run.status in {"pending", "running"}:
            continue
        key = f"{run.phase}:{run.status}"
        terminal_run_counts[key] = terminal_run_counts.get(key, 0) + 1

    audit_dir = _state_root(repo_root) / "orchestrator"
    phase_attempt_counts: dict[str, dict[str, int]] = {}
    failures_by_type: dict[str, int] = {}
    nonfatal_warnings: dict[str, int] = {}
    gate_failures: dict[str, int] = {}
    durations: dict[str, list[float]] = {}

    for audit_path in sorted(audit_dir.glob("*.json")) if audit_dir.exists() else []:
        try:
            payload = json.loads(audit_path.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if not _audit_matches_filters(
            payload,
            spec_id=spec_id,
            run_id=run_id,
            since=since_dt,
        ):
            continue

        phase = str(payload.get("phase") or "").strip() or "unknown"
        result = payload.get("result")
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "").strip() or "unknown"
        phase_counts = phase_attempt_counts.setdefault(phase, {})
        phase_counts[status] = phase_counts.get(status, 0) + 1

        duration = _duration_seconds(
            str(result.get("started_at") or ""),
            str(result.get("finished_at") or ""),
        )
        if duration is not None:
            durations.setdefault(phase, []).append(duration)

        failure_type = str(result.get("failure_type") or "").strip()
        failure_subtype = str(result.get("failure_subtype") or "").strip()
        if failure_type and failure_subtype:
            key = f"{failure_type}.{failure_subtype}"
            if bool(result.get("nonfatal", False)):
                nonfatal_warnings[key] = nonfatal_warnings.get(key, 0) + 1
            else:
                failures_by_type[key] = failures_by_type.get(key, 0) + 1

        gate_name = str(result.get("gate_name") or "").strip()
        if gate_name:
            gate_failures[gate_name] = gate_failures.get(gate_name, 0) + 1

    duration_summary = {
        phase: {
            "count": len(values),
            "p50": _percentile(values, 50),
            "p90": _percentile(values, 90),
            "p95": _percentile(values, 95),
        }
        for phase, values in sorted(durations.items())
    }

    return {
        "filters": {
            "spec_id": spec_id or "",
            "run_id": run_id or "",
            "since": since or "",
        },
        "run_counts_by_terminal_phase_status": dict(sorted(terminal_run_counts.items())),
        "phase_attempt_counts_by_outcome": {
            phase: dict(sorted(counts.items())) for phase, counts in sorted(phase_attempt_counts.items())
        },
        "failures_by_type_subtype": dict(sorted(failures_by_type.items())),
        "nonfatal_warnings_by_type_subtype": dict(sorted(nonfatal_warnings.items())),
        "gate_failures_by_gate_name": dict(sorted(gate_failures.items())),
        "durations_seconds_by_phase": duration_summary,
    }


def cmd_analytics(args: argparse.Namespace) -> int:
    """Summarize local orchestrator history from .spec-state."""
    repo_root = resolve_common_root()
    try:
        summary = _compute_orchestrator_analytics(
            repo_root,
            spec_id=str(getattr(args, "spec", "") or "").strip() or None,
            run_id=str(getattr(args, "run", "") or "").strip() or None,
            since=str(getattr(args, "since", "") or "").strip() or None,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _cmd_report_completion(args: argparse.Namespace) -> int:
    """Agent handshake — report implementation completion."""
    spec_id = str(getattr(args, "spec", "") or "").strip() or None
    selected_run_id = getattr(args, "run", None)
    raw_status = args.status
    status_map = {
        "ok": "passed",
        "error": "failed",
        "passed": "passed",
        "failed": "failed",
        "blocked": "blocked",
        "needs-input": "needs-input",
    }
    status = status_map[raw_status]
    summary = args.summary or ""

    outbox_result_path = str(os.environ.get("SPEC_COMPLETION_OUTBOX", "")).strip()
    if outbox_result_path:
        return _cmd_report_completion_to_outbox(
            path=Path(outbox_result_path),
            spec_id=spec_id,
            run_id=str(selected_run_id or "").strip() or None,
            status=status,
            summary=summary,
            env=os.environ,
        )

    repo_root = resolve_common_root()

    try:
        run = _select_completion_run(
            repo_root,
            spec_id,
            run_id=selected_run_id,
            cwd=Path.cwd(),
            env=os.environ,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not run:
        identity = spec_id or selected_run_id or "current context"
        print(f"Error: No active run found for {identity}", file=sys.stderr)
        return 1

    raw_launch_number = str(os.environ.get("SPEC_IMPLEMENT_LAUNCH", "")).strip()
    try:
        launch_number = int(raw_launch_number) if raw_launch_number else run.implement_launches
    except ValueError:
        print(
            f"Error: invalid SPEC_IMPLEMENT_LAUNCH value {raw_launch_number!r}.",
            file=sys.stderr,
        )
        return 1

    result = ImplementResult(
        status=status,
        summary=summary,
        attempt=run.attempts,
        launch_number=max(0, launch_number),
        result_source="agent_report",
        completed_at=_now_iso(),
    )

    # Collect recent commits
    worktree_path = resolve_worktree_path(run, repo_root)
    if worktree_path.is_dir():
        log_result = run_subprocess(
            ["git", "log", "--oneline", "-10"],
            cwd=worktree_path,
        )
        if log_result.returncode == 0:
            result.commits = log_result.stdout.strip().splitlines()

    try:
        result.save(repo_root, run.run_id)
        print(f"Completion recorded for {run.spec_id}: status={status}")
        return 0
    except OSError as common_err:
        local_state_root = _worktree_state_root(worktree_path)
        try:
            result.save_to_state_root(local_state_root, run.run_id)
            print(f"Completion recorded for {run.spec_id}: status={status} (worktree-local fallback)")
            print(
                "Warning: could not write completion to common .spec-state; "
                "orchestrator will mirror worktree-local result.",
                file=sys.stderr,
            )
            return 0
        except OSError as fallback_err:
            print(
                f"Error: failed to record completion result in common and local state ({common_err}; {fallback_err})",
                file=sys.stderr,
            )
            return 1


def _cmd_report_completion_to_outbox(
    *,
    path: Path,
    spec_id: str | None,
    run_id: str | None,
    status: str,
    summary: str,
    env: dict[str, str],
) -> int:
    resolved_spec_id = spec_id or str(env.get("SPEC_ID", env.get("SIM_SPEC_ID", ""))).strip()
    resolved_run_id = run_id or str(env.get("SPEC_RUN_ID", env.get("SIM_RUN_ID", ""))).strip()
    if not resolved_spec_id or not resolved_run_id:
        print(
            "Error: container completion outbox requires SPEC_ID and SPEC_RUN_ID.",
            file=sys.stderr,
        )
        return 1

    raw_attempt = str(env.get("SPEC_ATTEMPT", "")).strip()
    attempt: int | None = None
    if raw_attempt:
        try:
            attempt = int(raw_attempt)
        except ValueError:
            print(f"Error: invalid SPEC_ATTEMPT value {raw_attempt!r}.", file=sys.stderr)
            return 1

    raw_launch_number = str(env.get("SPEC_IMPLEMENT_LAUNCH", "")).strip()
    launch_number = 0
    if raw_launch_number:
        try:
            launch_number = max(0, int(raw_launch_number))
        except ValueError:
            print(
                f"Error: invalid SPEC_IMPLEMENT_LAUNCH value {raw_launch_number!r}.",
                file=sys.stderr,
            )
            return 1

    result = ImplementResult(
        status=status,
        summary=summary,
        attempt=attempt,
        launch_number=launch_number,
        result_source="agent_report_outbox",
        completed_at=_now_iso(),
    )
    payload = {
        "artifact": "spec-container-completion-report",
        "version": 1,
        "spec_id": resolved_spec_id,
        "run_id": resolved_run_id,
        "recorded_at": _now_iso(),
        "implement_result": asdict(result),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"Error: failed to write container completion outbox {path}: {exc}", file=sys.stderr)
        return 1

    print(f"Completion recorded for {resolved_spec_id}: status={status} (container outbox)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Short-form completion handshake that infers run identity from env/cwd."""
    return _cmd_report_completion(args)


def cmd_complete(args: argparse.Namespace) -> int:
    """Backward-compatible completion handshake."""
    return _cmd_report_completion(args)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec-runtime",
        description="Spec Butler lifecycle runner",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    subparsers = parser.add_subparsers(dest="command")

    # run
    p_run = subparsers.add_parser("run", help="Start/resume full workflow")
    p_run.add_argument("--spec", required=True, help="Spec ID")
    p_run.add_argument("--agent", default=SPEC_RUNTIME_CONFIG.agents.default, help="Implementation agent: claude|codex")
    p_run.add_argument(
        "--review-agent",
        default=_configured_review_agent_default() or SPEC_RUNTIME_CONFIG.agents.default,
        help="Review agent: claude|codex",
    )
    p_run.add_argument("--base", default=None, help="Base ref for new worktrees")
    p_run.add_argument("--run", default="", help="Resume a specific run id")
    p_run.add_argument("--branch", default="", help="Reuse an existing branch for a new run")
    p_run.add_argument(
        "--coordination-bypass",
        action="store_true",
        help=(
            "Emergency bypass for a configured coordinator; runs local-only and may allow "
            "cross-machine duplicate work."
        ),
    )
    p_run.add_argument(
        "--reset-intake",
        action="store_true",
        help="Force re-capture intake answers before implementation",
    )
    p_run.add_argument(
        "--retry-cap",
        type=int,
        default=None,
        help=f"Max implement retries (default: {RETRY_CAP})",
    )

    # spec (interactive spec authoring — formerly named 'task')
    p_spec_author = subparsers.add_parser(
        "spec",
        help="Launch interactive spec authoring in a dedicated worktree",
    )
    p_spec_author.add_argument(
        "--spec",
        default="",
        help="Optional spec ID to author; when omitted the authoring session chooses it",
    )
    p_spec_author.add_argument("--agent", default=SPEC_RUNTIME_CONFIG.agents.default, help="Agent: claude|codex")
    p_spec_author.add_argument("--base", default=BASE_REF, help="Base ref for new worktrees")
    p_spec_author.add_argument(
        "--label",
        default="",
        help="Legacy alias for --spec during the transition to the spec CLI",
    )

    # input (resolve ambiguity interactively)
    p_input = subparsers.add_parser(
        "input",
        help="Resolve ambiguity for a spec waiting for input",
    )
    p_input.add_argument("--spec", required=True, help="Spec ID")
    p_input.add_argument("--agent", default=None, help="Agent: claude|codex")

    p_steer = subparsers.add_parser(
        "steer",
        help="Attach proactive steering to the latest run for a spec",
    )
    p_steer.add_argument("--spec", required=True, help="Spec ID")
    p_steer.add_argument("--message", required=True, help="Advisory guidance for the next implement attempt")

    # task (describe + execute in one shot)
    p_task = subparsers.add_parser(
        "task",
        help="Describe a task conversationally then execute it autonomously",
    )
    p_task.add_argument(
        "--agent", default=SPEC_RUNTIME_CONFIG.agents.default, help="Implementation agent: claude|codex"
    )
    p_task.add_argument(
        "--review-agent",
        default=_configured_review_agent_default() or SPEC_RUNTIME_CONFIG.agents.default,
        help="Review agent: claude|codex",
    )
    p_task.add_argument("--base", default=BASE_REF, help="Base ref for new worktrees")

    # step
    p_step = subparsers.add_parser("step", help="Run a single phase")
    p_step.add_argument("--spec", required=True, help="Spec ID")
    p_step.add_argument("--phase", required=True, help="Phase name")
    p_step.add_argument(
        "--agent", default=SPEC_RUNTIME_CONFIG.agents.default, help="Implementation agent: claude|codex"
    )
    p_step.add_argument(
        "--review-agent",
        default=_configured_review_agent_default() or SPEC_RUNTIME_CONFIG.agents.default,
        help="Review agent: claude|codex",
    )
    p_step.add_argument("--base", default=None, help="Base ref for new worktrees")
    p_step.add_argument("--run", default="", help="Operate on a specific run id")
    p_step.add_argument(
        "--reset-intake",
        action="store_true",
        help="Force re-capture intake answers when running intake",
    )

    # status
    p_status = subparsers.add_parser("status", help="Show run status")
    p_status.add_argument("--spec", required=True, help="Spec ID")
    p_status.add_argument("--run", default="", help="Show a specific run id")

    # analytics
    p_analytics = subparsers.add_parser(
        "analytics",
        help="Summarize local orchestrator history from .spec-state",
    )
    p_analytics.add_argument("--spec", default="", help="Filter to one spec id")
    p_analytics.add_argument("--run", default="", help="Filter to one run id")
    p_analytics.add_argument(
        "--since",
        default="",
        help="Filter to records updated on/after this ISO timestamp or YYYY-MM-DD date",
    )

    # report
    p_report = subparsers.add_parser(
        "report",
        help="Short-form completion handshake (infers run from env/cwd)",
    )
    p_report.add_argument("--spec", default="", help="Optional spec ID override")
    p_report.add_argument("--run", default="", help="Optional run id override")
    p_report.add_argument(
        "--status",
        required=True,
        choices=["passed", "blocked", "failed", "ok", "error", "needs-input"],
        help="Completion status",
    )
    p_report.add_argument("--summary", default="", help="Summary text")

    # complete
    p_complete = subparsers.add_parser("complete", help="Agent completion handshake")
    p_complete.add_argument("--spec", default="", help="Optional spec ID override")
    p_complete.add_argument("--run", default="", help="Record completion for a specific run id")
    p_complete.add_argument(
        "--status",
        required=True,
        choices=["passed", "blocked", "failed", "ok", "error", "needs-input"],
        help="Completion status",
    )
    p_complete.add_argument("--summary", default="", help="Summary text")

    args = parser.parse_args(argv)

    # Set up logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "run": cmd_run,
        "spec": cmd_spec,
        "input": cmd_input,
        "steer": cmd_steer,
        "task": cmd_task,
        "step": cmd_step,
        "status": cmd_status,
        "analytics": cmd_analytics,
        "report": cmd_report,
        "complete": cmd_complete,
    }
    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
