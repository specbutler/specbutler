"""/api/v1/chat route handlers for interactive agent chat sessions.

Provides endpoints for creating, messaging, listing, inspecting, and
stopping chat sessions.  Agent responses are streamed back as SSE
(``text/event-stream``) so the frontend can render them incrementally.

Each session creates a dedicated worktree so the agent works on an
isolated branch, respecting the repo's worktree-only editing rule.
Agent turns run in background asyncio tasks so they survive client
disconnection (session continuity).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from spec_runtime.agent_git_isolation import (
    UnsafeAgentGitIsolationError,
    agent_git_added_paths,
    agent_git_head,
    agent_git_show_file,
    cleanup_agent_git_isolation,
    prepare_agent_git_isolation_if_linked,
    reconcile_agent_git_isolation,
)
from spec_runtime.git_publish_guard import (
    UnsafeRepositoryGitConfigError,
    assert_repository_publication_baseline,
    capture_repository_publication_baseline,
)
from spec_runtime.platform_fs import remove_tree
from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor
from spec_runtime.worktree_safety import (
    UnsafeWorktreePathError,
    expected_branch_worktree_names,
    validate_owned_worktree_path,
)

from .bridge import (
    AgentEvent,
    ChatSession,
    create_session,
    get_bridge,
    get_session,
    list_sessions,
    register_bridge,
    remove_session,
)

logger = logging.getLogger(__name__)

# Default tool whitelist for browser-initiated sessions.  Agents run with
# pre-approved tools so the browser never needs an approval prompt (see spec
# "Out of Scope").  Both Claude and Codex backends honour this list.
_DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
]

# ---------------------------------------------------------------------------
# Background turn tracking — module-level dicts keyed by session_id
# ---------------------------------------------------------------------------

_turn_tasks: dict[str, asyncio.Task] = {}
_turn_notifiers: dict[str, asyncio.Event] = {}
_turn_completions: dict[str, asyncio.Event] = {}
_turn_event_lists: dict[str, list] = {}
_turn_owners: dict[str, str] = {}
_provider_stop_tasks: dict[str, asyncio.Task] = {}
_TURN_CANCEL_TIMEOUT_SECONDS = 5.0
_PROVIDER_STOP_TIMEOUT_SECONDS = 12.0


def _is_turn_active(session_id: str) -> bool:
    """Return True if the session has a background turn still running."""
    task = _turn_tasks.get(session_id)
    return task is not None and not task.done()


def _repo_root(request: Request) -> Path:
    return request.app.state.repo_root


def _chat_owner_id(request: Request) -> str:
    """Return the stable lifecycle owner for this Starlette app instance."""
    return str(getattr(request.app.state, "chat_owner_id", "") or "")


def _session_for_request(request: Request, session_id: str) -> ChatSession | None:
    """Resolve a session without allowing cross-app registry access."""
    session = get_session(session_id)
    if session is None:
        return None
    owner_id = _chat_owner_id(request)
    if session.owner_id and session.owner_id != owner_id:
        return None
    return session


def _sessions_for_request(request: Request) -> list[ChatSession]:
    """List sessions owned by this app, plus unowned legacy/test sessions."""
    owner_id = _chat_owner_id(request)
    return [
        session
        for session in list_sessions()
        if not session.owner_id or session.owner_id == owner_id
    ]


def _json(data: object, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _publication_baseline_error(session: ChatSession) -> str:
    """Return a safe error if agent-visible Git publication state changed."""
    baseline = session.publication_baseline
    if baseline is None:
        # Legacy and narrowly constructed unit-test sessions predate the
        # launch invariant. Externally created sessions always receive one.
        return ""
    try:
        assert_repository_publication_baseline(
            Path(session.worktree_path),
            expected_remote_url=baseline[0],
            expected_config_fingerprint=baseline[1],
        )
    except UnsafeRepositoryGitConfigError:
        return (
            "Repository publication configuration changed during chat. "
            "The session was terminated and cannot be handed off; inspect "
            "and restore the repository Git config from an operator shell."
        )
    return ""


def _task_slug_from_content(text: str) -> str:
    """Read a task id from the exact committed bytes selected for handoff."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() != "---":
                continue
            try:
                frontmatter = yaml.safe_load("\n".join(lines[1:index])) or {}
            except yaml.YAMLError:
                frontmatter = {}
            if isinstance(frontmatter, dict):
                spec_id = str(frontmatter.get("id", "")).strip()
                if spec_id:
                    return spec_id
            break
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("slug:"):
            return stripped.split(":", 1)[1].strip()
    return ""


# ---------------------------------------------------------------------------
# Worktree setup — creates a dedicated branch/worktree per chat session
# ---------------------------------------------------------------------------


def _setup_chat_worktree(repo_root: Path, mode: str) -> tuple[Path, str, str, str]:
    """Create a dedicated worktree and branch for a chat session.

    Returns ``(worktree_path, branch_name, base_sha, base_ref)``.
    """
    from spec_runtime.orchestrator import (
        SPEC_AUTHORING_SESSION_BRANCH_PREFIX,
        SPEC_AUTHORING_SESSION_WORKTREE_PREFIX,
        _worktrees_root,
        run_subprocess,
    )

    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    wt_root = _worktrees_root(repo_root)

    if mode == "create":
        branch = f"{SPEC_AUTHORING_SESSION_BRANCH_PREFIX}{token}"
        worktree_path = wt_root / f"{SPEC_AUTHORING_SESSION_WORKTREE_PREFIX}{token}"
    else:
        slug = f"web-task-{token}"
        branch = f"task/{slug}--{token}"
        worktree_path = wt_root / f"task-{slug}--{token}"

    from spec_runtime.config import load_repo_spec_runtime_config

    # Use the repository's configured base. Falling back to local HEAD can
    # silently author from an unrelated branch, so an unresolved base is an
    # actionable onboarding error instead.
    base_ref = load_repo_spec_runtime_config(repo_root, require=True).base_ref
    check = run_subprocess(
        ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
        cwd=repo_root,
    )
    if check.returncode != 0:
        raise RuntimeError(
            f"Configured base_ref {base_ref!r} does not resolve. "
            "Run `git fetch origin --prune` and `spec doctor`."
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_subprocess(
        ["git", "worktree", "add", "--no-track",
         str(worktree_path), "-b", branch, base_ref],
        cwd=repo_root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to create chat worktree: {detail}")

    # Resolve the base SHA so _detect_task_spec can diff against it later.
    sha_result = run_subprocess(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
    )
    base_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""

    return worktree_path, branch, base_sha, base_ref


def _auto_commit_worktree(worktree_path: str) -> bool:
    """Stage and commit any uncommitted files in the worktree.

    Called before worktree removal so that artifacts (specs, task output)
    produced during a chat session survive the session ending.  If there
    is nothing to commit, this is a no-op and returns ``True``.

    Returns ``True`` on success, ``False`` if the commit failed.
    """
    from spec_runtime.orchestrator import run_subprocess

    # Stage everything — including untracked files the agent created.
    run_subprocess(["git", "add", "-A"], cwd=worktree_path)

    # Check if there is anything to commit.
    status = run_subprocess(
        ["git", "status", "--porcelain"], cwd=worktree_path,
    )
    if not status.stdout.strip():
        return True

    result = run_subprocess(
        ["git", "commit", "-m", "auto-commit: save chat session work"],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        logger.error(
            "Auto-commit failed in %s: %s",
            worktree_path,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    return True


def _push_branch(repo_root: Path, branch: str) -> bool:
    """Push *branch* to the ``origin`` remote so chat work is visible.

    Returns ``True`` on success, ``False`` on failure (e.g. no remote).
    """
    from spec_runtime.orchestrator import run_subprocess

    result = run_subprocess(
        ["git", "push", "origin", branch],
        cwd=repo_root,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to push branch %s: %s",
            branch,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    return True


def _cleanup_chat_worktree(
    repo_root: Path,
    worktree_path: str | None,
    branch: str | None,
) -> None:
    """Remove the worktree directory and local branch created for a chat session."""
    from spec_runtime.orchestrator import _worktrees_root, run_subprocess

    if worktree_path:
        # Browser chat always creates the current generated layout.  Legacy
        # run names are accepted by lifecycle cleanup, but are not valid for a
        # newly created in-memory chat session.
        expected_names = expected_branch_worktree_names(branch or "")[:1]
        if not expected_names:
            raise RuntimeError(
                "Refusing chat worktree cleanup because its branch has no recognized identity"
            )
        try:
            worktree_path = str(
                validate_owned_worktree_path(
                    owner_root=_worktrees_root(repo_root),
                    target=worktree_path,
                    relative_to=repo_root,
                    expected_names=expected_names,
                )
            )
        except UnsafeWorktreePathError as exc:
            raise RuntimeError(f"Refusing unsafe chat worktree cleanup: {exc}") from exc

        listing = run_subprocess(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
        if listing.returncode != 0:
            raise RuntimeError("Could not inspect chat worktree registration")
        registered = {
            line.removeprefix("worktree ").strip()
            for line in listing.stdout.splitlines()
            if line.startswith("worktree ")
        }
        path_exists = os.path.lexists(worktree_path)
        if path_exists or worktree_path in registered:
            removed = run_subprocess(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repo_root,
            )
            if removed.returncode != 0:
                detail = removed.stderr.strip() or removed.stdout.strip()
                raise RuntimeError(f"Failed to remove chat worktree: {detail}")

        verified = run_subprocess(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
        if verified.returncode != 0:
            raise RuntimeError("Could not verify chat worktree removal")
        if os.path.lexists(worktree_path) or any(
            line == f"worktree {worktree_path}"
            for line in verified.stdout.splitlines()
        ):
            raise RuntimeError("Chat worktree removal was not confirmed")

    if branch:
        ref = f"refs/heads/{branch}"
        exists = run_subprocess(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=repo_root,
        )
        if exists.returncode not in {0, 1}:
            raise RuntimeError("Could not inspect chat branch registration")
        if exists.returncode == 0:
            deleted = run_subprocess(["git", "branch", "-D", branch], cwd=repo_root)
            if deleted.returncode != 0:
                detail = deleted.stderr.strip() or deleted.stdout.strip()
                raise RuntimeError(f"Failed to delete chat branch: {detail}")
        verified = run_subprocess(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=repo_root,
        )
        if verified.returncode != 1:
            raise RuntimeError("Chat branch deletion was not confirmed")


# ---------------------------------------------------------------------------
# Prompt loading — delegates to orchestrator builders while adapting lifecycle
# ownership to the browser's credential-free, host-published workflow.
# ---------------------------------------------------------------------------


def _load_system_prompt(
    repo_root: Path,
    mode: str,
    *,
    branch: str = "",
    worktree_path: Path | None = None,
    agent: str = "",
) -> str:
    """Load the system prompt for *mode* using the orchestrator prompt builders.

    For ``create`` mode this delegates to ``_load_spec_creation_prompt`` but
    keeps publication host-owned: chat writes and commits locally, then hands
    the branch/worktree back to the user.

    For ``task`` mode the agent only scopes (writes and commits the task
    spec).  The orchestrator handles implementation after the user reviews
    the spec and clicks "Implement" in the web UI.
    """
    from spec_runtime.orchestrator import (
        _load_spec_creation_prompt,
        _load_task_scoping_prompt,
    )

    if mode == "create":
        return _load_spec_creation_prompt(
            repo_root,
            spec_id=None,
            branch=branch,
            resume=False,
            publish=False,
        )

    # task mode — scoping only, no implementation
    wt = worktree_path or repo_root
    scoping_prompt = _load_task_scoping_prompt(
        repo_root,
        wt,
        resume=False,
        agent=agent,
    )

    # Replace the CLI-oriented "exit the session" instruction with a
    # web-oriented "done scoping" instruction.  The agent should not
    # attempt implementation — the orchestrator handles that after the
    # user reviews the spec in the web UI.
    # Strip out the entire exit instruction block (everything from "After
    # writing the task spec" through the next newline) and replace it with
    # a web-oriented instruction.  Use a regex to be robust against minor
    # wording changes in the orchestrator's exit_note template.
    import re
    prompt = re.sub(
        r"After writing the task spec file and committing it.*?(?=\n)",
        "After writing and committing the task spec, tell the user you're "
        "done scoping and show the spec file path.",
        scoping_prompt,
        count=1,
    )
    return prompt


# ---------------------------------------------------------------------------
# Backend availability detection
# ---------------------------------------------------------------------------


def _available_backends(repo_root: Path | None = None) -> dict[str, bool]:
    """Return runnable agent backends allowed by repository configuration."""
    claude_ok = False
    codex_ok = False

    try:
        from .bridge_claude import _claude_cli_unavailability_reason, _sdk_available

        claude_ok = _sdk_available() and not _claude_cli_unavailability_reason()
    except Exception:
        pass

    try:
        from .bridge_codex import _codex_available

        codex_ok = _codex_available()
    except Exception:
        pass

    backends = {"claude": claude_ok, "codex": codex_ok}
    if repo_root is not None:
        codex_project_config = repo_root / ".codex"
        if codex_project_config.is_symlink() or (
            codex_project_config.exists() and not codex_project_config.is_dir()
        ):
            backends["codex"] = False

        from spec_runtime.config import load_repo_spec_runtime_config

        allowed = set(load_repo_spec_runtime_config(repo_root, require=True).agents.allowed)
        backends = {name: available and name in allowed for name, available in backends.items()}
    return backends


def _backend_unavailability_reason(agent: str, repo_root: Path) -> str:
    """Explain a known provider preflight failure without starting a process."""
    if agent == "claude":
        from .bridge_claude import _claude_cli_unavailability_reason, _sdk_available

        if not _sdk_available():
            return (
                "Claude backend unavailable — install the `web` extra and "
                "authenticate Claude Code."
            )
        reason = _claude_cli_unavailability_reason()
        if reason:
            return reason
    elif agent == "codex":
        from .bridge_codex import _codex_available

        if not _codex_available():
            return "Codex backend unavailable — install and authenticate the Codex CLI."
        config_path = repo_root / ".codex"
        if config_path.is_symlink() or (
            config_path.exists() and not config_path.is_dir()
        ):
            return (
                f"Codex backend unavailable — {config_path} is not a real directory, "
                "but current Codex requires `.codex/` to be one. Inspect and rename "
                "or remove the legacy path, then run `spec doctor`."
            )
    return ""


def _default_chat_agent(repo_root: Path, backends: dict[str, bool]) -> str:
    from spec_runtime.config import load_repo_spec_runtime_config

    configured = load_repo_spec_runtime_config(repo_root, require=True).agents.default
    if backends.get(configured, False):
        return configured
    return next((name for name, available in backends.items() if available), "")


def log_backend_availability(repo_root: Path | None = None) -> dict[str, bool]:
    """Log which chat backends are available at startup.

    Called by the server during startup so operators see immediately
    which agent SDKs are installed. Returns the backends dict for convenience.
    """
    backends = _available_backends(repo_root)
    available = [name for name, ok in backends.items() if ok]
    unavailable = [name for name, ok in backends.items() if not ok]

    if available:
        logger.info("Chat backends available: %s", ", ".join(available))
    if unavailable:
        logger.warning(
            "Chat backends NOT runnable (dependency, policy, or configuration): %s",
            ", ".join(unavailable),
        )
    if not available:
        logger.warning("No runnable chat backends — run `spec doctor` for remediation")

    return backends


def _create_bridge(agent: str):  # noqa: ANN201
    """Instantiate the appropriate bridge for the given agent name."""
    if agent == "claude":
        from .bridge_claude import ClaudeBridge

        return ClaudeBridge()
    elif agent == "codex":
        from .bridge_codex import CodexBridge

        return CodexBridge()
    else:
        raise ValueError(f"Unknown agent: {agent!r}. Use 'claude' or 'codex'.")


# ---------------------------------------------------------------------------
# Background turn runner
# ---------------------------------------------------------------------------


def _start_background_turn(
    session_id: str,
    session,  # noqa: ANN001
    bridge,  # noqa: ANN001
    text: str,
) -> None:
    """Set up turn-tracking state and launch _run_turn_bg as a background task."""
    session.live_turn = []
    notify = asyncio.Event()
    done_evt = asyncio.Event()
    events: list = []
    _turn_notifiers[session_id] = notify
    _turn_completions[session_id] = done_evt
    _turn_event_lists[session_id] = events
    _turn_owners[session_id] = str(getattr(session, "owner_id", "") or "")

    task = asyncio.create_task(_run_turn_bg(session_id, session, bridge, text))
    _turn_tasks[session_id] = task


def _begin_turn_cancel(session_id: str) -> asyncio.Task | None:
    """Request cancellation without waiting, so its provider can stop first."""
    task = _turn_tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    return task


async def _finish_turn_cancel(
    session_id: str,
    task: asyncio.Task | None,
) -> bool:
    """Reap a cancelled turn and clear relay state only after confirmation."""
    if task is not None:
        done, _ = await asyncio.wait(
            {task},
            timeout=_TURN_CANCEL_TIMEOUT_SECONDS,
        )
        if task not in done:
            logger.warning("Chat turn did not exit after cancellation: %s", session_id)
            return False
        await asyncio.gather(task, return_exceptions=True)
    _turn_tasks.pop(session_id, None)
    _turn_notifiers.pop(session_id, None)
    _turn_completions.pop(session_id, None)
    _turn_event_lists.pop(session_id, None)
    _turn_owners.pop(session_id, None)
    return True


async def _confirm_provider_stop(session_id: str, bridge: object) -> None:
    """Run or rejoin provider cleanup without abandoning a timed-out stop."""
    task = _provider_stop_tasks.get(session_id)
    if task is None:
        task = asyncio.create_task(bridge.stop_session(session_id))
        _provider_stop_tasks[session_id] = task

    done, _ = await asyncio.wait(
        {task},
        timeout=_PROVIDER_STOP_TIMEOUT_SECONDS,
    )
    if task not in done:
        raise TimeoutError("provider cleanup is still running")

    _provider_stop_tasks.pop(session_id, None)
    try:
        task.result()
    except asyncio.CancelledError as exc:
        raise RuntimeError("provider cleanup was cancelled") from exc


async def _finalize_agent_git(
    session: ChatSession,
    *,
    expected_head: str = "",
    discard: bool = False,
) -> str:
    """Reconcile or discard private Git metadata, then remove it.

    Callers must first confirm that both the provider and the background turn
    relay have stopped.  ``expected_head`` pins an implementation handoff to
    the exact private commit whose spec bytes were reviewed.
    """
    isolation = session.agent_git_isolation
    if isolation is None:
        return session.agent_git_reconciled_head or expected_head

    if discard:
        await asyncio.to_thread(cleanup_agent_git_isolation, isolation)
        session.agent_git_isolation = None
        session.agent_git_reconciled_head = ""
        return ""

    reconciled_head = session.agent_git_reconciled_head
    if not reconciled_head:
        try:
            private_head = await asyncio.to_thread(agent_git_head, isolation)
            if expected_head and private_head != expected_head:
                raise UnsafeAgentGitIsolationError(
                    "Private Git HEAD changed after the task spec was selected"
                )
            reconciliation = await asyncio.to_thread(
                reconcile_agent_git_isolation,
                isolation,
            )
        except BaseException:
            # A poisoned or racing private repository must never become a
            # permanent terminal-state trap. Its working files are outside the
            # disposable GIT_DIR and remain available for operator recovery.
            await asyncio.to_thread(cleanup_agent_git_isolation, isolation)
            session.agent_git_isolation = None
            session.agent_git_reconciled_head = ""
            raise
        reconciled_head = reconciliation.final_head
        session.agent_git_reconciled_head = reconciled_head

    if expected_head and reconciled_head != expected_head:
        raise UnsafeAgentGitIsolationError(
            "Reconciled Git HEAD does not match the selected task spec"
        )
    await asyncio.to_thread(cleanup_agent_git_isolation, isolation)
    session.agent_git_isolation = None
    return reconciled_head


async def _shutdown_chat_sessions(*, owner_id: str | None = None) -> None:
    """Perform the provider and relay cleanup for ``shutdown_chat_sessions``."""
    sessions = [
        session
        for session in list_sessions()
        if owner_id is None or session.owner_id == owner_id
    ]
    session_ids = {session.session_id for session in sessions}
    session_ids.update(
        session_id
        for session_id, turn_owner in _turn_owners.items()
        if owner_id is None or turn_owner == owner_id
    )
    pending_turns = {
        session_id: _begin_turn_cancel(session_id)
        for session_id in session_ids
    }
    sessions_by_id = {session.session_id: session for session in sessions}
    provider_stopped: set[str] = set()
    for session_id in session_ids:
        session = sessions_by_id.get(session_id)
        if session is not None:
            session.status = "stopping"
        bridge = get_bridge(session_id)
        if bridge is None:
            provider_stopped.add(session_id)
            continue
        while session_id not in provider_stopped:
            try:
                await _confirm_provider_stop(session_id, bridge)
                provider_stopped.add(session_id)
            except Exception:
                # Lifespan exit is the final in-process ownership boundary.
                # Keep joining/retrying the exact provider cleanup rather than
                # returning and orphaning a process or credential directory.
                logger.warning(
                    "Waiting for chat provider stop during shutdown: %s",
                    session_id,
                    exc_info=True,
                )
                await asyncio.sleep(0.05)
    for session_id, task in pending_turns.items():
        relay_stopped = await _finish_turn_cancel(session_id, task)
        while not relay_stopped:
            await asyncio.sleep(0.05)
            relay_stopped = await _finish_turn_cancel(session_id, task)
        if relay_stopped and session_id in provider_stopped:
            session = sessions_by_id.get(session_id)
            try:
                if session is not None:
                    await _finalize_agent_git(
                        session,
                        discard=session.cleanup_worktree_on_stop,
                    )
            except Exception:
                # The provider and relay are gone, so retaining the session and
                # private metadata is safe and leaves evidence recoverable for
                # an operator instead of silently discarding committed work.
                logger.error(
                    "Could not finalize private Git metadata during shutdown: %s",
                    session_id,
                    exc_info=True,
                )
            else:
                remove_session(session_id)

    # Defensive cleanup for partially-created or completed turns whose session
    # record is gone. Ownership remains recorded until relay state is removed.
    if owner_id is None and not _provider_stop_tasks and not _turn_owners:
        _turn_notifiers.clear()
        _turn_completions.clear()
        _turn_event_lists.clear()
        _turn_owners.clear()


async def shutdown_chat_sessions(*, owner_id: str | None = None) -> None:
    """Stop provider processes owned by one app, or all when unspecified.

    ASGI servers may cancel lifespan shutdown while a provider is still
    disconnecting.  Keep the cleanup in its own task so caller cancellation
    cannot detach the provider process or leave its registry entries behind.
    """
    cleanup = asyncio.create_task(_shutdown_chat_sessions(owner_id=owner_id))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


def _detect_task_spec(session) -> dict | None:  # noqa: ANN001
    """Check if a *newly created* task spec file exists in the session's worktree.

    Uses ``git diff`` against the session's ``base_sha`` to find only specs
    added during this session, avoiding false positives from pre-existing
    ``specs/tasks/*.md`` files inherited from the base branch.

    Returns ``{"spec_id": ..., "spec_content": ..., "head_sha": ...}``
    when a spec is found, ``None`` otherwise.  ``head_sha`` is the commit
    whose content was read so callers can detect whether a new revision
    was committed since the last review.
    """
    from spec_runtime.orchestrator import TASK_SPEC_DIR, run_subprocess

    wt = Path(session.worktree_path)
    isolation = getattr(session, "agent_git_isolation", None)
    if isolation is not None:
        base_sha = getattr(session, "base_sha", "")
        if not base_sha:
            return None
        head_sha = agent_git_head(isolation)
        new_files = list(
            agent_git_added_paths(
                isolation,
                base_sha,
                f"{TASK_SPEC_DIR}/*.md",
            )
        )
        if not new_files:
            return None
        if len(new_files) > 1:
            raise ValueError(
                f"Multiple task specs detected ({', '.join(new_files)}). "
                "Keep exactly one specs/tasks/<spec-id>.md file."
            )
        rel_path = new_files[0]
        content = agent_git_show_file(isolation, head_sha, rel_path)
        if not content.strip():
            return None
        return {
            "spec_id": Path(rel_path).stem,
            "spec_content": content,
            "head_sha": head_sha,
        }

    task_dir = wt / TASK_SPEC_DIR
    if not task_dir.is_dir():
        return None

    # Resolve HEAD once — used both for content reads and for the returned
    # ``head_sha`` that lets callers suppress duplicate review cards.
    head_result = run_subprocess(["git", "rev-parse", "HEAD"], cwd=wt)
    head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""

    base_sha = getattr(session, "base_sha", "")
    if base_sha:
        # Find spec files *added* since the session started.  Using
        # --diff-filter=A (not AM) ensures we only pick up newly created
        # specs, not pre-existing ones that the agent merely modified.
        diff_result = run_subprocess(
            ["git", "diff", "--name-only", "--diff-filter=A",
             base_sha, "HEAD", "--", f"{TASK_SPEC_DIR}/*.md"],
            cwd=wt,
        )
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            new_files = [
                line.strip() for line in diff_result.stdout.strip().splitlines()
                if line.strip()
            ]
            if not new_files:
                return None
            if len(new_files) > 1:
                raise ValueError(
                    f"Multiple task specs detected ({', '.join(new_files)}). "
                    "Keep exactly one specs/tasks/<spec-id>.md file."
                )
            rel_path = new_files[0]
            spec_id = Path(rel_path).stem
            # Read the committed version from HEAD — not the worktree —
            # so that uncommitted edits are never surfaced for handoff.
            show_result = run_subprocess(
                ["git", "show", f"HEAD:{rel_path}"],
                cwd=wt,
            )
            if show_result.returncode != 0 or not show_result.stdout.strip():
                return None
            return {"spec_id": spec_id, "spec_content": show_result.stdout,
                    "head_sha": head_sha}
        return None

    # Fallback when base_sha is unavailable: use glob (original behavior).
    specs = list(task_dir.glob("*.md"))
    if not specs:
        return None
    if len(specs) > 1:
        names = [s.name for s in specs]
        raise ValueError(
            f"Multiple task specs detected ({', '.join(names)}). "
            "Keep exactly one specs/tasks/<spec-id>.md file."
        )
    spec_path = specs[0]
    rel_path = spec_path.relative_to(wt)
    # Read from HEAD to avoid surfacing uncommitted edits.
    show_result = run_subprocess(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=wt,
    )
    if show_result.returncode != 0 or not show_result.stdout.strip():
        # File exists on disk but not committed — skip it.
        return None
    return {"spec_id": spec_path.stem, "spec_content": show_result.stdout,
            "head_sha": head_sha}


async def _run_turn_bg(
    session_id: str,
    session,  # noqa: ANN001
    bridge,  # noqa: ANN001
    text: str,
) -> None:
    """Background task that drives an agent turn.

    Events are accumulated in both ``_turn_event_lists[session_id]`` (for
    SSE consumers) and ``session.live_turn`` (for the ``/history`` endpoint).
    On completion the events are promoted into ``session.history``.
    """
    events = _turn_event_lists.get(session_id, [])
    notify = _turn_notifiers.get(session_id)
    done_evt = _turn_completions.get(session_id)

    try:
        async for event in bridge.send_message(session_id, text):
            event_dict = asdict(event)
            if event.kind == "error":
                events.append(event_dict)
                session.live_turn.append(event_dict)
                session.status = "error"
            elif event.kind == "done":
                break
            else:
                events.append(event_dict)
                session.live_turn.append(event_dict)
            if notify:
                notify.set()
    except Exception as exc:
        err = asdict(AgentEvent(kind="error", text=str(exc)))
        events.append(err)
        session.live_turn.append(err)
        session.status = "error"
        if notify:
            notify.set()
    finally:
        publication_error = _publication_baseline_error(session)
        if publication_error:
            err = asdict(AgentEvent(kind="error", text=publication_error))
            events.append(err)
            session.live_turn.append(err)
            session.status = "error"
            if notify:
                notify.set()
            try:
                await _confirm_provider_stop(session_id, bridge)
            except Exception:
                # Keep the bridge registration as the retryable ownership
                # capability for Stop/server shutdown.
                logger.warning(
                    "Provider cleanup remains pending after Git config violation: %s",
                    session_id,
                    exc_info=True,
                )

        # Promote live turn into permanent history and clear it.
        if session.live_turn:
            session.history.append(
                {"role": "assistant", "events": list(session.live_turn)}
            )
            session.live_turn = []

        # For task-mode sessions, check whether a task spec was committed
        # and emit a task_spec_ready event so the frontend can show the
        # review UI.  Persist it into session.history so that the review
        # card survives page reloads via the /history endpoint.
        # Skip if a spec_review with the same spec_id is already in history
        # (dedup: the git-diff stays non-empty across turns).
        # When the user clicks "Keep Editing" the clear-review endpoint
        # saves last_reviewed_head_sha, so we only re-surface the spec
        # when a new commit touching the spec has been made.
        if session.mode == "task" and session.status == "active":
            already_surfaced = {
                h["spec_id"] for h in session.history
                if h.get("role") == "spec_review"
            }
            try:
                spec_info = _detect_task_spec(session)
            except Exception as exc:
                spec_info = None
                err_event = {"kind": "error", "text": str(exc)}
                events.append(err_event)
                session.status = "error"
                session.history.append(
                    {"role": "assistant", "events": [err_event]}
                )
                if notify:
                    notify.set()
            if spec_info is not None and spec_info["spec_id"] not in already_surfaced:
                # After "Keep Editing", only re-surface when a new commit
                # touching the spec exists (HEAD moved past the last review).
                last_reviewed = getattr(session, "last_reviewed_head_sha", "")
                head_sha = spec_info.get("head_sha", "")
                if last_reviewed and head_sha and last_reviewed == head_sha:
                    pass  # same commit — don't re-surface
                else:
                    spec_event = {
                        "kind": "task_spec_ready",
                        "spec_id": spec_info["spec_id"],
                        "spec_content": spec_info["spec_content"],
                    }
                    events.append(spec_event)
                    session.history.append(
                        {"role": "spec_review", "spec_id": spec_info["spec_id"],
                         "spec_content": spec_info["spec_content"],
                         "head_sha": head_sha}
                    )
                if notify:
                    notify.set()

        if done_evt:
            done_evt.set()
        if notify:
            notify.set()
        _turn_tasks.pop(session_id, None)


def _make_sse_generator(session_id, done_evt, notify, from_idx: int = 0):
    """Return an async generator that streams turn events as SSE."""
    events = _turn_event_lists.get(session_id, [])

    async def event_stream():
        pos = from_idx
        while True:
            # Yield any new events
            while pos < len(events):
                event_dict = events[pos]
                pos += 1
                data = json.dumps(event_dict, default=str)
                yield f"event: agent_event\ndata: {data}\n\n"

            # If turn is done, emit final done event and return
            if done_evt.is_set():
                # Drain any last events
                while pos < len(events):
                    event_dict = events[pos]
                    pos += 1
                    data = json.dumps(event_dict, default=str)
                    yield f"event: agent_event\ndata: {data}\n\n"
                done_event = AgentEvent(kind="done")
                yield f"event: agent_event\ndata: {json.dumps(asdict(done_event))}\n\n"
                return

            # Wait for new events — re-check after clearing to avoid race
            notify.clear()
            if pos < len(events) or done_evt.is_set():
                continue
            try:
                await asyncio.wait_for(notify.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                # Keepalive comment to prevent connection timeout
                yield ": keepalive\n\n"

    return event_stream()


def _streaming_response(generator):
    """Wrap an SSE generator in a StreamingResponse."""
    from starlette.responses import StreamingResponse

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/v1/chat/sessions — create a new session
# ---------------------------------------------------------------------------


async def create_chat_session(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)

    mode = body.get("mode", "")
    prompt = body.get("prompt", "")

    if mode not in ("create", "task"):
        return _json({"error": "mode must be 'create' or 'task'"}, 422)

    if not prompt:
        return _json({"error": "prompt is required"}, 422)

    repo_root = _repo_root(request)

    # Check installed providers against the repository's allowed agent set.
    backends = _available_backends(repo_root)
    agent = str(body.get("agent", "")).strip() or _default_chat_agent(repo_root, backends)
    if not backends.get(agent, False):
        reason = _backend_unavailability_reason(agent, repo_root)
        return _json(
            {"error": (
                f"Agent backend '{agent}' is not available"
                + (f": {reason}" if reason else ".")
                + f" Available backends: {[k for k, v in backends.items() if v]}"
            )},
            422,
        )

    # Create a dedicated worktree so the agent edits an isolated branch
    try:
        worktree_path, branch, base_sha, base_ref = await asyncio.to_thread(
            _setup_chat_worktree, repo_root, mode
        )
    except RuntimeError as exc:
        return _json({"error": f"Worktree setup failed: {exc}"}, 500)

    # The linked worktree exposes repository Git metadata to both providers.
    # Capture the trusted URL/config state before either provider can inspect
    # or mutate it, then verify the same immutable baseline after every turn
    # and immediately before implementation handoff.
    try:
        publication_baseline = await asyncio.to_thread(
            capture_repository_publication_baseline,
            worktree_path,
        )
    except UnsafeRepositoryGitConfigError as exc:
        try:
            await asyncio.to_thread(
                _cleanup_chat_worktree,
                repo_root,
                str(worktree_path),
                branch,
            )
        except Exception:
            logger.warning(
                "Failed to clean up chat worktree after Git baseline rejection",
                exc_info=True,
            )
        return _json({"error": str(exc)}, 422)

    # Give the provider a disposable Git directory so staging and commits do
    # not require write access to shared refs, objects, config, or another
    # worktree's administrative metadata. Prepare it before materializing any
    # sandbox policy so an obsolete shared-metadata grant can never be used.
    try:
        git_isolation = await asyncio.to_thread(
            prepare_agent_git_isolation_if_linked,
            worktree_path,
        )
    except UnsafeAgentGitIsolationError as exc:
        try:
            await asyncio.to_thread(
                _cleanup_chat_worktree,
                repo_root,
                str(worktree_path),
                branch,
            )
        except Exception:
            logger.warning(
                "Failed to clean up chat worktree after Git isolation rejection",
                exc_info=True,
            )
        return _json({"error": f"Git isolation setup failed: {exc}"}, 422)

    # Write sandbox config so the agent session has the same permissions
    # as the CLI path (mirroring _write_sandbox_config in orchestrator).
    try:
        from spec_runtime.orchestrator import _write_sandbox_config

        await asyncio.to_thread(
            _write_sandbox_config,
            agent,
            worktree_path,
            git_isolation=git_isolation,
        )
    except Exception:
        logger.warning("Failed to write sandbox config for %s", worktree_path, exc_info=True)
        try:
            if git_isolation is not None:
                await asyncio.to_thread(cleanup_agent_git_isolation, git_isolation)
            await asyncio.to_thread(
                _cleanup_chat_worktree, repo_root, str(worktree_path), branch,
            )
        except Exception:
            logger.warning(
                "Failed to clean up chat worktree after sandbox config rejection",
                exc_info=True,
            )
        return _json({"error": "Sandbox config write failed — refusing to start session without sandbox policy"}, 500)

    system_prompt = _load_system_prompt(
        repo_root, mode,
        branch=branch,
        worktree_path=worktree_path,
        agent=agent,
    )

    # Create session record
    session = create_session(
        mode=mode,
        agent=agent,
        owner_id=_chat_owner_id(request),
    )
    session.status = "starting"
    session.initial_prompt = prompt
    session.worktree_path = str(worktree_path)
    session.branch = branch
    session.base_sha = base_sha
    session.base_ref = base_ref
    session.publication_baseline = publication_baseline
    session.agent_git_isolation = git_isolation
    session.history.append({"role": "user", "content": prompt})

    # Instantiate and register the bridge
    bridge = None
    try:
        bridge = _create_bridge(agent)
        # Register before provider startup so failed connect/handshake cleanup
        # remains reachable through the public Stop endpoint.
        register_bridge(session.session_id, bridge)
        # Only pass allowed_tools for Claude — Codex uses its own native
        # tool set (edit, command, etc.) and would be crippled by a
        # Claude-specific whitelist.
        tools = _DEFAULT_ALLOWED_TOOLS if agent == "claude" else None
        await bridge.start_session(
            prompt=system_prompt,
            agent=agent,
            cwd=str(worktree_path),
            allowed_tools=tools,
            session_id=session.session_id,
            initial_prompt=prompt,
            git_isolation=git_isolation,
        )
    except BaseException as exc:
        cleanup_confirmed = bridge is None
        if bridge is not None:
            try:
                await bridge.stop_session(session.session_id)
                cleanup_confirmed = True
            except Exception:
                logger.warning(
                    "Failed to stop partially-started chat provider: %s",
                    session.session_id,
                    exc_info=True,
                )
        if not cleanup_confirmed:
            # Preserve the session, bridge, and worktree as retryable ownership
            # capabilities. Removing any of them here can orphan an auth-bearing
            # provider process after a startup timeout.
            session.status = "stopping"
            session.cleanup_worktree_on_stop = True
            session.startup_done.set()
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            return _json(
                {
                    "error": f"Provider startup failed and cleanup is still pending: {exc}",
                    "session_id": session.session_id,
                    "status": "stopping",
                },
                503,
            )
        try:
            await _finalize_agent_git(session, discard=True)
        except Exception as cleanup_exc:
            session.status = "stopping"
            session.cleanup_worktree_on_stop = True
            session.startup_done.set()
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            return _json(
                {
                    "error": (
                        "Provider startup failed and private Git cleanup is "
                        f"pending: {cleanup_exc}"
                    ),
                    "session_id": session.session_id,
                    "status": "stopping",
                },
                503,
            )
        try:
            await asyncio.to_thread(
                _cleanup_chat_worktree, repo_root, str(worktree_path), branch,
            )
        except Exception as cleanup_exc:
            session.status = "stopping"
            session.cleanup_worktree_on_stop = True
            session.startup_done.set()
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            return _json(
                {
                    "error": f"Provider startup failed and worktree cleanup is pending: {cleanup_exc}",
                    "session_id": session.session_id,
                    "status": "stopping",
                },
                503,
            )
        remove_session(session.session_id)
        session.startup_done.set()
        if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
            raise
        return _json({"error": str(exc)}, 422)

    if session.stop_requested:
        # Stop joined startup rather than racing provider connect/handshake.
        # Leave the now-owned provider reachable for the waiting Stop request;
        # in particular, never start the initial turn after cancellation won.
        session.status = "stopping"
        session.startup_done.set()
        return _json(
            {
                "error": "Session stop was requested during provider startup",
                "session_id": session.session_id,
                "status": "stopping",
            },
            409,
        )

    # Kick off the initial turn in the background so the session starts
    # processing the user's prompt immediately — callers should not need
    # a second POST /messages to trigger the first agent response.
    session.status = "active"
    session.initial_turn_dispatched = True
    _start_background_turn(session.session_id, session, bridge, prompt)
    session.startup_done.set()

    return _json({"session_id": session.session_id})


# ---------------------------------------------------------------------------
# POST /api/v1/chat/sessions/{id}/messages — send message, stream response
# ---------------------------------------------------------------------------


async def send_chat_message(request: Request) -> Response:
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    try:
        body = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)

    text = body.get("text", "")
    if not text:
        return _json({"error": "text is required"}, 422)

    bridge = get_bridge(session_id)
    if bridge is None:
        return _json({"error": "Session bridge not available"}, 500)

    # Handle initial-prompt reconnect: the client sends the initial prompt
    # via /messages to obtain the SSE stream for the turn that was already
    # started at session creation.  The flag is one-shot so that a
    # subsequent identical message is treated as a genuinely new turn.
    # This check runs *before* the status guard so that a fast-failing
    # initial turn (session already in "error") still streams the real
    # error events back to the client instead of returning a bare 409.
    if (session.initial_turn_dispatched
            and text == session.initial_prompt):
        session.initial_turn_dispatched = False
        session.touch()
        notify = _turn_notifiers.get(session_id)
        done_evt = _turn_completions.get(session_id)
        if notify is not None and done_evt is not None:
            return _streaming_response(
                _make_sse_generator(session_id, done_evt, notify))
        # Turn tracking was cleaned up (e.g. session stopped between
        # create and this request) — return a done-only stream.
        async def _done_only():
            done_event = AgentEvent(kind="done")
            yield f"event: agent_event\ndata: {json.dumps(asdict(done_event))}\n\n"
        return _streaming_response(_done_only())

    # Reject messages to non-active sessions (e.g. after /stop).
    if session.status != "active":
        return _json({"error": "Session is no longer active"}, 409)

    if _is_turn_active(session_id):
        return _json({"error": "A turn is already in progress"}, 409)

    session.touch()
    session.history.append({"role": "user", "content": text})

    _start_background_turn(session_id, session, bridge, text)

    notify = _turn_notifiers[session_id]
    done_evt = _turn_completions[session_id]
    return _streaming_response(_make_sse_generator(session_id, done_evt, notify))


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions/{id}/stream — reattach to in-progress turn
# ---------------------------------------------------------------------------


async def stream_chat_session(request: Request) -> Response:
    """Reattach to a live agent turn after navigating away.

    Accepts an optional ``?from=N`` query parameter to skip the first *N*
    events (the client already received them via ``/history``).
    """
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    raw_from = request.query_params.get("from", "0")
    try:
        from_idx = int(raw_from)
    except (TypeError, ValueError):
        return _json({"error": "from must be a non-negative integer"}, 422)
    if from_idx < 0:
        return _json({"error": "from must be a non-negative integer"}, 422)

    # Only a valid reattach consumes the create-time turn. A malformed replay
    # request must not change whether the initial prompt can still be claimed.
    session.initial_turn_dispatched = False

    if not _is_turn_active(session_id):
        # No active turn — replay any buffered events the client hasn't
        # seen yet (the turn may have finished between /history and this
        # /stream?from=N reconnect), then send done.
        buffered = _turn_event_lists.get(session_id, [])

        async def replay_and_done():
            for evt_dict in buffered[from_idx:]:
                data = json.dumps(evt_dict, default=str)
                yield f"event: agent_event\ndata: {data}\n\n"
            done_event = AgentEvent(kind="done")
            yield f"event: agent_event\ndata: {json.dumps(asdict(done_event))}\n\n"

        return _streaming_response(replay_and_done())

    notify = _turn_notifiers.get(session_id)
    done_evt = _turn_completions.get(session_id)

    if notify is None or done_evt is None:
        async def empty():
            done_event = AgentEvent(kind="done")
            yield f"event: agent_event\ndata: {json.dumps(asdict(done_event))}\n\n"

        return _streaming_response(empty())

    return _streaming_response(
        _make_sse_generator(session_id, done_evt, notify, from_idx=from_idx)
    )


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions/{id} — session metadata
# ---------------------------------------------------------------------------


async def get_chat_session(request: Request) -> Response:
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    return _json({
        "session_id": session.session_id,
        "mode": session.mode,
        "agent": session.agent,
        "created_at": session.created_at,
        "last_active": session.last_active,
        "status": session.status,
        "turn_active": _is_turn_active(session.session_id),
    })


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions — list sessions
# ---------------------------------------------------------------------------


async def list_chat_sessions(request: Request) -> Response:
    sessions = _sessions_for_request(request)
    return _json([
        {
            "session_id": s.session_id,
            "mode": s.mode,
            "agent": s.agent,
            "created_at": s.created_at,
            "last_active": s.last_active,
            "status": s.status,
            "turn_active": _is_turn_active(s.session_id),
        }
        for s in sessions
    ])


# ---------------------------------------------------------------------------
# POST /api/v1/chat/sessions/{id}/stop — stop a session
# ---------------------------------------------------------------------------


async def stop_chat_session(request: Request) -> Response:
    """Serialize Stop with provider startup and implementation handoff."""
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    # Publishing intent before waiting closes the window where startup or a
    # handoff could resume after Stop had already begun waiting on the lock.
    session.stop_requested = True
    if session.status == "starting":
        await asyncio.shield(session.startup_done.wait())

    async with session.terminal_lock:
        if session.handoff_completed:
            return _json(
                {
                    "error": "Session ownership was transferred to an implementation run",
                    "session_id": session_id,
                    "status": "handed_off",
                },
                409,
            )
        return await _stop_chat_session_locked(request)


async def _stop_chat_session_locked(request: Request) -> Response:
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    session.status = "stopping"
    turn_task = _begin_turn_cancel(session_id)
    bridge = get_bridge(session_id)
    if bridge is not None:
        try:
            await _confirm_provider_stop(session_id, bridge)
        except TimeoutError as exc:
            logger.warning(
                "Timed out while stopping chat provider for session %s",
                session_id,
                exc_info=True,
            )
            return _json(
                {
                    "error": f"Provider stop was not confirmed: {exc}",
                    "session_id": session_id,
                    "status": "stopping",
                },
                504,
            )
        except Exception as exc:
            logger.warning(
                "Failed to confirm chat provider stop for session %s",
                session_id,
                exc_info=True,
            )
            return _json(
                {
                    "error": f"Provider stop was not confirmed: {exc}",
                    "session_id": session_id,
                    "status": "stopping",
                },
                503,
            )
    relay_stopped = await _finish_turn_cancel(session_id, turn_task)
    if not relay_stopped:
        return _json(
            {
                "error": "Chat turn cancellation is still pending",
                "session_id": session_id,
                "status": "stopping",
            },
            503,
        )

    branch = getattr(session, "branch", None)
    worktree_path = getattr(session, "worktree_path", None)
    try:
        await _finalize_agent_git(
            session,
            discard=bool(getattr(session, "cleanup_worktree_on_stop", False)),
        )
    except Exception as exc:
        session.status = "error"
        logger.error(
            "Failed to finalize private Git metadata for session %s",
            session_id,
            exc_info=True,
        )
        return _json(
            {
                "error": f"Private Git finalization failed: {exc}",
                "session_id": session_id,
                "status": "error",
            },
            409,
        )
    if getattr(session, "cleanup_worktree_on_stop", False):
        # A provider that failed during startup never produced a usable chat
        # session. Once its exit is confirmed, finish the deferred rollback
        # that was unsafe while provider ownership remained unresolved.
        try:
            await asyncio.to_thread(
                _cleanup_chat_worktree,
                _repo_root(request),
                worktree_path,
                branch,
            )
        except Exception as exc:
            logger.warning(
                "Failed deferred startup worktree cleanup: %s",
                session_id,
                exc_info=True,
            )
            return _json(
                {
                    "error": f"Worktree cleanup is still pending: {exc}",
                    "session_id": session_id,
                    "status": "stopping",
                },
                503,
            )
        session.status = "completed"
        remove_session(session_id)
        return _json({
            "session_id": session_id,
            "status": "stopped",
            "branch": branch,
            "worktree_removed": True,
        })

    session.status = "completed"

    # Stop is cancellation only — do NOT auto-commit or push partial work.
    # The worktree is left in place so the user can inspect, commit, or
    # discard the changes explicitly.

    return _json({
        "session_id": session_id,
        "status": "stopped",
        "branch": branch,
        **({"worktree": worktree_path} if worktree_path else {}),
    })


# ---------------------------------------------------------------------------
# POST /api/v1/chat/sessions/{id}/implement — hand off to orchestrator
# ---------------------------------------------------------------------------


async def implement_chat_task(request: Request) -> Response:
    """Serialize handoff against Stop and other terminal transitions."""
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)
    async with session.terminal_lock:
        if session.stop_requested:
            return _json(
                {
                    "error": "Session stop has already been requested",
                    "session_id": session_id,
                    "status": "stopping",
                },
                409,
            )
        return await _implement_chat_task_locked(request)


async def _implement_chat_task_locked(request: Request) -> Response:
    """Hand a scoped task spec off to the orchestrator for implementation.

    Validates that the session is a task-mode session with a detected spec,
    then spawns ``spec implement`` for the task spec on the session's branch.
    Provider and relay shutdown are confirmed before the implementation is
    launched.  Only then is the session marked ``completed`` and worktree
    ownership transferred to the orchestrator run (AC 6).
    """
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    if session.mode != "task":
        return _json({"error": "Only task-mode sessions support implement"}, 422)

    if session.status != "active":
        return _json({"error": "Session already handed off"}, 409)

    # Reject if a chat turn is still running — the agent would be writing to
    # the same worktree concurrently with the orchestrator.
    if _is_turn_active(session_id):
        return _json({"error": "A chat turn is still running"}, 409)

    # Atomically claim the session so a concurrent request from another tab
    # cannot pass the status guard above. Validation failures restore the
    # active chat; once shutdown begins, failures remain retryable via Stop.
    session.status = "stopping"

    # Detect the spec in the worktree
    try:
        spec_info = _detect_task_spec(session)
    except ValueError as exc:
        session.status = "active"
        return _json({"error": str(exc)}, 422)
    if spec_info is None:
        session.status = "active"
        return _json({"error": "No task spec found in session worktree"}, 422)

    spec_id = spec_info["spec_id"]
    branch = session.branch
    repo_root = _repo_root(request)

    # Validate the spec id the same way the CLI does in
    # _resolve_scoped_task_spec / _assert_task_spec_id_is_unambiguous.
    from spec_runtime.orchestrator import _specs_root
    from spec_runtime.spec_identity import SPEC_ID_RE

    wt = Path(session.worktree_path)
    # Frontmatter id must match the filename selected from committed HEAD.
    # Never validate a mutable worktree file and then pin different bytes.
    frontmatter_id = _task_slug_from_content(spec_info["spec_content"])
    if frontmatter_id and frontmatter_id != spec_id:
        session.status = "active"
        return _json(
            {"error": f"Task spec file '{spec_id}.md' does not match frontmatter id '{frontmatter_id}'."},
            422,
        )

    # Must be valid kebab-case
    if not SPEC_ID_RE.fullmatch(spec_id):
        session.status = "active"
        return _json(
            {"error": f"Task spec id '{spec_id}' is invalid; use lowercase kebab-case."},
            422,
        )

    # Must not collide with a top-level catalog spec
    if (_specs_root(wt) / f"{spec_id}.md").exists() or (
        _specs_root(repo_root) / f"{spec_id}.md"
    ).exists():
        session.status = "active"
        return _json(
            {"error": f"Task spec id '{spec_id}' collides with top-level specs/{spec_id}.md."},
            409,
        )

    # The persisted session agent is authoritative; fall back to the request
    # body only when the session has no agent set (e.g. legacy sessions).
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    agent = session.agent or body.get("agent", "")
    review_agent = body.get("review_agent", "")

    # Worktree ownership cannot transfer while either the provider or its
    # background event relay may still be writing to it.  Do this before
    # creating durable run state or spawning an adoptable process so a failed
    # stop cannot leave a ghost run or an unowned implementation behind.
    turn_task = _begin_turn_cancel(session_id)
    bridge = get_bridge(session_id)
    if bridge is not None:
        try:
            await _confirm_provider_stop(session_id, bridge)
        except TimeoutError as exc:
            logger.warning(
                "Timed out stopping chat provider during implementation handoff: %s",
                session_id,
                exc_info=True,
            )
            return _json(
                {
                    "error": f"Provider stop was not confirmed: {exc}",
                    "session_id": session_id,
                    "status": "stopping",
                },
                504,
            )
        except Exception as exc:
            logger.warning(
                "Failed to stop chat provider during implementation handoff: %s",
                session_id,
                exc_info=True,
            )
            return _json(
                {
                    "error": f"Provider stop was not confirmed: {exc}",
                    "session_id": session_id,
                    "status": "stopping",
                },
                503,
            )

    relay_stopped = await _finish_turn_cancel(session_id, turn_task)
    if not relay_stopped:
        return _json(
            {
                "error": "Chat turn cancellation is still pending",
                "session_id": session_id,
                "status": "stopping",
            },
            503,
        )

    try:
        reconciled_head = await _finalize_agent_git(
            session,
            expected_head=spec_info.get("head_sha", ""),
        )
    except Exception as exc:
        session.status = "error"
        logger.error(
            "Failed to reconcile private Git metadata during handoff: %s",
            session_id,
            exc_info=True,
        )
        return _json(
            {
                "error": f"Private Git reconciliation failed: {exc}",
                "session_id": session_id,
                "status": "error",
            },
            409,
        )
    if spec_info.get("head_sha") and reconciled_head != spec_info["head_sha"]:
        session.status = "error"
        return _json(
            {
                "error": "The reconciled commit does not match the reviewed task spec",
                "session_id": session_id,
                "status": "error",
            },
            409,
        )

    if session.stop_requested:
        # Stop can publish intent while waiting for this terminal lock. It won
        # the transition as long as no implementation process exists yet.
        session.status = "stopping"
        return _json(
            {
                "error": "Session stop was requested during implementation handoff",
                "session_id": session_id,
                "status": "stopping",
            },
            409,
        )

    publication_error = _publication_baseline_error(session)
    if publication_error:
        session.status = "error"
        return _json(
            {
                "error": publication_error,
                "session_id": session_id,
                "status": "error",
            },
            409,
        )

    # Pre-create a task-mode RunState so the orchestrator picks up the correct
    # run_mode and spec_path.  The spec file only exists in the session's
    # worktree (not the main checkout), so we pin it from there.  Shelling out
    # to ``spec implement --run <run_id>`` resumes this pre-created run without
    # needing the spec in the main checkout.
    from spec_runtime.orchestrator import (
        BASE_REF,
        TASK_SPEC_DIR,
        RunState,
        _current_actor,
        _default_spec_path,
        _persist_pinned_spec,
        _run_id,
        _run_spec_snapshot_path,
        _run_state_path,
    )

    # Use the configured base ref that actually created the chat worktree.
    base_ref = getattr(session, "base_ref", "") or BASE_REF

    run_id = _run_id(spec_id)
    run = RunState(
        run_id=run_id,
        spec_id=spec_id,
        branch=branch,
        worktree_path=str(session.worktree_path),
        run_mode="task",
        spec_path=_default_spec_path(spec_id, "task"),
        agent=agent,
        review_agent=review_agent,
        base_ref=base_ref,
        requested_by=_current_actor(),
        resumed_from_branch=branch,
    )
    # Pin the committed content that _detect_task_spec read from HEAD —
    # not the live worktree — so the orchestrator implements exactly the
    # bytes the user reviewed.
    _persist_pinned_spec(
        repo_root, run,
        spec_path=f"{TASK_SPEC_DIR}/{spec_id}.md",
        text=spec_info["spec_content"],
    )
    run.save(repo_root)

    def _cleanup_orphaned_run() -> None:
        """Remove the pre-created run state so no ghost record remains on disk."""
        run_json = _run_state_path(repo_root, run_id)
        run_json.unlink(missing_ok=True)
        snapshot_dir = _run_spec_snapshot_path(repo_root, run_id).parent
        if snapshot_dir.is_dir():
            remove_tree(snapshot_dir, ignore_errors=True)

    # Resume the pre-created run.  The orchestrator will find the pinned spec
    # in .spec-state and the worktree already checked out on the session branch.
    from .api import _spec_executable

    try:
        # This is a durable ownership transfer, not a web-owned child. The
        # implementation must survive a web-server restart and remain
        # stoppable through its persisted supervision token.
        proc = ProcessSupervisor(LifetimeMode.ADOPTABLE).spawn(
            [
                _spec_executable(),
                "implement",
                "--spec", spec_id,
                "--run", run_id,
                *(["--agent", agent] if agent else []),
                *(["--review-agent", review_agent] if review_agent else []),
            ],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, RuntimeError) as exc:
        _cleanup_orphaned_run()
        session.status = "error"
        return _json(
            {"error": f"Failed to launch spec implement: {exc}",
             "spec_id": spec_id},
            422,
        )

    # Register the Popen object in the web-side process registry so the
    # stop endpoint can terminate it during the startup window before the
    # orchestrator has written pgid/active-run metadata to disk.
    from .api import _started_processes

    started_processes = _started_processes(request)
    started_processes[spec_id] = proc

    # Give the process a moment to fail on startup.
    await asyncio.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None and exit_code != 0:
        _cleanup_orphaned_run()
        started_processes.pop(spec_id, None)
        session.status = "error"
        return _json(
            {"error": f"spec implement exited immediately (code {exit_code})",
             "spec_id": spec_id},
            422,
        )

    session.status = "completed"
    session.handoff_completed = True

    # Clear stale spec_review entries so that replayHistory() on reload
    # does not re-surface Implement/Keep Editing buttons for a completed
    # session.
    session.history = [
        h for h in session.history if h.get("role") != "spec_review"
    ]

    # Build run_state matching the standard implement API response shape.
    # Re-read from disk so we pick up any updates the orchestrator made
    # during the 0.5 s startup window.
    import json as _json_mod

    try:
        run_state = _json_mod.loads(
            _run_state_path(repo_root, run_id).read_text(encoding="utf-8")
        )
    except (OSError, _json_mod.JSONDecodeError):
        run_state = {
            "spec_id": spec_id,
            "run_id": run_id,
            "status": "starting",
            "agent": agent,
            "pid": proc.pid,
        }

    return _json({
        "spec_id": spec_id,
        "pid": proc.pid,
        "status": "started",
        "session_id": session_id,
        "run_id": run_id,
        "run_state": run_state,
    })


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions/{id}/history — message history for reconnect
# ---------------------------------------------------------------------------


async def get_chat_history(request: Request) -> Response:
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    history = list(session.history)
    # Include any in-progress turn so reconnecting clients see partial output.
    if session.live_turn:
        history.append({"role": "assistant", "events": list(session.live_turn)})
    return _json({
        "session_id": session_id,
        "history": history,
        "turn_active": _is_turn_active(session_id),
        "live_event_count": len(session.live_turn),
    })


# ---------------------------------------------------------------------------
# POST /api/v1/chat/sessions/{id}/clear-review — drop spec_review from history
# ---------------------------------------------------------------------------


async def clear_chat_review(request: Request) -> Response:
    """Remove spec_review entries from session history.

    Called by the frontend when the user clicks "Keep Editing" so that
    the next turn can re-detect and re-surface the (possibly updated)
    task spec without being blocked by the dedup check.

    Records the HEAD SHA of the most recently reviewed version so that the
    post-turn detection only re-surfaces the spec when a new commit is made.
    """
    session_id = request.path_params["id"]
    session = _session_for_request(request, session_id)
    if session is None:
        return _json({"error": "Session not found"}, 404)

    # Preserve the HEAD SHA from the latest spec_review so the post-turn
    # detection can tell whether a new commit was made after Keep Editing.
    for entry in reversed(session.history):
        if entry.get("role") == "spec_review" and entry.get("head_sha"):
            session.last_reviewed_head_sha = entry["head_sha"]
            break
    else:
        # Fallback: resolve HEAD now so older reviews without head_sha
        # still get the dedup guard.
        from spec_runtime.orchestrator import run_subprocess

        wt = getattr(session, "worktree_path", "")
        if wt:
            r = run_subprocess(["git", "rev-parse", "HEAD"], cwd=Path(wt))
            if r.returncode == 0 and r.stdout.strip():
                session.last_reviewed_head_sha = r.stdout.strip()

    session.history = [
        h for h in session.history if h.get("role") != "spec_review"
    ]
    return _json({"cleared": True})


# ---------------------------------------------------------------------------
# GET /api/v1/chat/backends — available agent backends
# ---------------------------------------------------------------------------


async def get_chat_backends(request: Request) -> Response:
    repo_root = _repo_root(request)
    backends = _available_backends(repo_root)
    return _json({
        "backends": backends,
        "default_agent": _default_chat_agent(repo_root, backends),
    })


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

chat_api_routes = [
    Route("/api/v1/chat/backends", get_chat_backends),
    Route("/api/v1/chat/sessions", create_chat_session, methods=["POST"]),
    Route("/api/v1/chat/sessions", list_chat_sessions),
    Route("/api/v1/chat/sessions/{id}", get_chat_session),
    Route("/api/v1/chat/sessions/{id}/clear-review", clear_chat_review, methods=["POST"]),
    Route("/api/v1/chat/sessions/{id}/history", get_chat_history),
    Route("/api/v1/chat/sessions/{id}/implement", implement_chat_task, methods=["POST"]),
    Route("/api/v1/chat/sessions/{id}/messages", send_chat_message, methods=["POST"]),
    Route("/api/v1/chat/sessions/{id}/stop", stop_chat_session, methods=["POST"]),
    Route("/api/v1/chat/sessions/{id}/stream", stream_chat_session),
]
