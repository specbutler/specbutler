"""Public ``spec`` CLI — the primary interface for humans and agents.

This module provides the installable ``spec`` command with intent-level
subcommands for the spec-driven development workflow.  It delegates to
the orchestrator runtime for execution while keeping the public surface
clean and free of forge-specific or agent-specific imports.

Primary commands (happy-path)::

    spec init        — bootstrap a repo for spec-driven development
    spec create      — author a new spec interactively
    spec implement   — start or resume an implementation workflow
    spec stop        — stop the active workflow process group for a spec
    spec status      — show run state and gate status
    spec list        — list specs with status and dependencies
    spec show        — display a spec's content
    spec report      — report implement-phase completion
    spec clean       — remove worktrees and branches for a spec
    spec steer       — attach proactive steering guidance to a run
    spec task        — describe and execute a quick task
    spec watch       — interactive TUI dashboard
    spec gc          — reconcile stale run state
    spec doctor      — validate repository onboarding and local dependencies

Autopilot (fleet-level)::

    spec auto run    — dispatch loop for parallel spec runs
    spec auto stop   — graceful shutdown of running dispatcher

Advanced / debug::

    spec phase       — run a single orchestrator phase
    spec input       — resolve operator intervention for a waiting-for-input run
    spec steer       — attach proactive steering to the latest run for a spec
    spec analytics   — summarize local orchestrator history
"""

from __future__ import annotations

import argparse
import ipaddress
import subprocess
import sys
from pathlib import Path


def _lazy_config():
    """Lazy-load config to avoid import-time side effects in tests."""
    from .config import load_spec_runtime_config

    return load_spec_runtime_config()


def _lazy_orchestrator():
    """Lazy-import the orchestrator module."""
    from . import orchestrator

    return orchestrator


# ---------------------------------------------------------------------------
# Helpers for list / show / clean (new commands not in orchestrator)
# ---------------------------------------------------------------------------


def _resolve_repo_root() -> Path:
    from .git_common import resolve_common_root

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return resolve_common_root(Path(result.stdout.strip()))
    return Path.cwd()


def _cmd_list(args: argparse.Namespace) -> int:
    """List specs with status and dependencies (equivalent to ``spec list``)."""
    repo_root = _resolve_repo_root()
    config = _lazy_config()
    include_merged = getattr(args, "all", False)

    # Refresh remote refs so status/dependency data is current (mirrors the
    # behaviour of the old ``spec list`` target which ran git fetch first).
    from .control_plane import (
        DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        GitFetchTimeoutError,
        run_git_fetch_with_timeout,
    )

    try:
        run_git_fetch_with_timeout(
            ["--quiet", "--tags", "--prune", "origin"],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_FETCH_TIMEOUT_SECONDS,
        )
    except GitFetchTimeoutError as exc:
        print(
            f"warning: git fetch timed out after {exc.timeout_seconds:.0f}s; spec status may be stale.",
            file=sys.stderr,
        )

    from .autopilot import load_run_record_index, resolve_autopilot_backend_policy
    from .container import container_image_source
    from .spec_metadata import iter_spec_metadata
    from .spec_status import collect_git_spec_state, get_spec_status

    git_state = collect_git_spec_state(repo_root)
    records = iter_spec_metadata(repo_root)
    run_index = load_run_record_index(repo_root, config=config)
    autopilot_policy = resolve_autopilot_backend_policy(config)

    fmt = "%-22s %-14s %-24s %-15s %-20s %-10s %s"
    lines = [
        fmt % ("SPEC", "AREA", "DEPENDS ON", "STATUS", "BACKEND", "SAFETY", "DESCRIPTION"),
        fmt % ("----", "----", "----------", "------", "-------", "------", "-----------"),
    ]
    for record in records:
        if record.obsolete and not include_merged:
            continue
        if record.superseded_by:
            if not include_merged:
                continue
            status = "superseded"
        else:
            status = get_spec_status(
                repo_root,
                record.spec_id,
                repo_root / config.paths.specs_dir / f"{record.spec_id}.md",
                git_state=git_state,
            )
            if not include_merged and status in {"merged", "obsolete"}:
                continue

        deps_display = ""
        if record.depends_on:
            if include_merged:
                deps_display = f"[{', '.join(record.depends_on)}]"
            else:
                visible = [
                    dep
                    for dep in record.depends_on
                    if get_spec_status(
                        repo_root,
                        dep,
                        repo_root / config.paths.specs_dir / f"{dep}.md",
                        git_state=git_state,
                    )
                    != "merged"
                ]
                deps_display = f"[{', '.join(visible)}]" if visible else ""

        latest_run = run_index.latest_by_spec.get(record.spec_id, {})
        backend = str(latest_run.get("backend", "")).strip()
        safety_mode = str(latest_run.get("safety_mode", "")).strip()
        backend_source = str(latest_run.get("backend_source", "")).strip()
        if not backend and status not in {"merged", "obsolete", "superseded"}:
            backend = autopilot_policy.backend
            safety_mode = autopilot_policy.safety_mode
            backend_source = autopilot_policy.source
        backend_display = backend or "-"
        if backend_source == "rollout-policy":
            backend_display = f"{backend_display} (rollout-policy)"
        if backend == "container":
            backend_display = f"{backend_display} [{container_image_source(config, repo_root)}]"

        lines.append(
            fmt
            % (
                record.spec_id,
                record.area or "-",
                deps_display or "-",
                status,
                backend_display,
                safety_mode or "-",
                record.description,
            )
        )

    print("\n".join(lines))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Display a spec's content."""
    repo_root = _resolve_repo_root()
    config = _lazy_config()
    spec_id = args.spec

    spec_path = repo_root / config.paths.specs_dir / f"{spec_id}.md"
    if not spec_path.exists():
        task_path = repo_root / config.paths.task_specs_dir / f"{spec_id}.md"
        if task_path.exists():
            spec_path = task_path
        else:
            print(f"Error: Spec not found: {spec_path}", file=sys.stderr)
            return 1

    print(spec_path.read_text())
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    """Remove inactive, run-owned worktrees/workspaces and local branches."""
    spec_id = args.spec
    from .spec_identity import SPEC_ID_RE

    # The identifier is interpolated into worktree glob patterns below. Reject
    # metacharacters and path syntax before any repository lookup, subprocess,
    # process inspection, or filesystem mutation can occur.
    if not SPEC_ID_RE.fullmatch(spec_id):
        print(
            "Error: Invalid spec ID. Use at most 64 lowercase letters, digits, and hyphens; "
            "start with an alphanumeric character and avoid Windows device names.",
            file=sys.stderr,
        )
        return 1

    # Resolve the common root (parent of .git common dir)
    from .git_common import resolve_common_root

    try:
        common_root = resolve_common_root()
    except Exception:
        print("Error: Not in a git repository.", file=sys.stderr)
        return 1

    # resolve_common_root falls back to cwd when not in a git repo —
    # verify we actually landed in a git repository.
    if not (common_root / ".git").exists():
        print("Error: Not in a git repository.", file=sys.stderr)
        return 1

    config = _lazy_config()
    orch = _lazy_orchestrator()
    runs = orch.RunState.list_for_spec(common_root, spec_id)
    live_blockers = _live_clean_blockers(orch, common_root, config, runs)
    if live_blockers:
        _print_clean_refusal(spec_id, live_blockers, _runs_use_container(runs))
        return 1

    # Serialize the destructive half with implement/phase. The read-only
    # process check above gives a useful remediation instead of only reporting
    # lock contention; the repeated check inside the lock closes the race with
    # a run starting between resolution and cleanup.
    try:
        with orch.SpecLock(common_root, spec_id):
            runs = orch.RunState.list_for_spec(common_root, spec_id)
            live_blockers = _live_clean_blockers(orch, common_root, config, runs)
            if live_blockers:
                _print_clean_refusal(spec_id, live_blockers, _runs_use_container(runs))
                return 1
            return _clean_inactive_spec_artifacts(
                spec_id=spec_id,
                common_root=common_root,
                config=config,
                runs=runs,
            )
    except RuntimeError as exc:
        print(f"Error: Refusing to clean '{spec_id}': {exc}", file=sys.stderr)
        _print_clean_remediation(spec_id, _runs_use_container(runs))
        return 1


def _runs_use_container(runs: list[object]) -> bool:
    return any(str(getattr(run, "backend", "") or "").strip() == "container" for run in runs)


def _recorded_group_is_live(orch: object, pgid: int, started_at: str) -> bool:
    """Check a recorded process group without treating a reused PID as its owner."""
    identity = orch.read_process_identity(pgid)
    if identity is not None and started_at and identity.started_at != started_at:
        return False
    return bool(orch._is_process_group_alive(pgid, pgid, started_at))


def _live_clean_blockers(
    orch: object,
    common_root: Path,
    config: object,
    runs: list[object],
) -> list[str]:
    """Read process identity state for exact matching runs; never mutate it."""
    blockers: list[str] = []
    for run in runs:
        process_group = orch._resolve_recorded_process_group(common_root, run)
        if process_group is not None:
            pgid, started_at = process_group
            if _recorded_group_is_live(orch, pgid, started_at):
                blockers.append(f"run {run.run_id} has live orchestrator process group {pgid}")

    # Agents and setup helpers launch in their own process groups. Refuse if a
    # live, identity-matched registered process survived its orchestrator; a
    # raw worktree removal would otherwise strand or kill it unpredictably.
    from . import worktree_process_registry

    state_root = common_root / config.paths.state_dir
    seen_paths: set[str] = set()
    for run in runs:
        raw_path = str(getattr(run, "worktree_path", "") or "").strip()
        if not raw_path:
            continue
        worktree_path = Path(raw_path).expanduser().resolve(strict=False)
        path_key = str(worktree_path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        for entry in worktree_process_registry.load_registered_processes(state_root, worktree_path):
            if worktree_process_registry.is_process_alive(entry.pid, entry.started_at):
                blockers.append(
                    f"run {run.run_id} has live registered {entry.kind} process {entry.pid}"
                )
    return blockers


def _print_clean_remediation(spec_id: str, container_run: bool) -> None:
    print("Remediation:", file=sys.stderr)
    print(f"  spec stop --spec {spec_id}", file=sys.stderr)
    print(f"  spec status --spec {spec_id}", file=sys.stderr)
    if container_run:
        print("  spec container gc          # inspect stale labeled resources", file=sys.stderr)
        print("  spec container gc --apply  # remove them after the run is stopped", file=sys.stderr)


def _print_clean_refusal(spec_id: str, blockers: list[str], container_run: bool) -> None:
    print(f"Error: Refusing to clean active spec '{spec_id}':", file=sys.stderr)
    for blocker in blockers:
        print(f"  - {blocker}", file=sys.stderr)
    _print_clean_remediation(spec_id, container_run)


def _clean_inactive_spec_artifacts(
    *,
    spec_id: str,
    common_root: Path,
    config: object,
    runs: list[object],
) -> int:
    worktrees_root = common_root / config.paths.worktrees_dir

    # Remove matching worktree directories
    patterns = [
        f"spec-{spec_id}",
        spec_id,
    ]
    glob_patterns = [
        f"code-{spec_id}--*",
        f"task-{spec_id}--*",
        f"specrun-{spec_id}--*",
    ]

    removed_worktrees = 0
    for pattern in patterns:
        target = worktrees_root / pattern
        if target.exists():
            removed_worktrees += _remove_worktree_path(target)

    for gp in glob_patterns:
        for target in worktrees_root.glob(gp):
            removed_worktrees += _remove_worktree_path(target)

    removed_workspaces, workspace_failures = _cleanup_run_owned_workspaces(
        common_root=common_root,
        config=config,
        runs=runs,
    )

    # Remove worktrees discovered by branch name
    wt_path = ""
    wt_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    for line in wt_list.stdout.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            branch_ref = line[len("branch refs/heads/") :]
            if (
                branch_ref.startswith(f"code/{spec_id}--")
                or branch_ref.startswith(f"task/{spec_id}--")
                or branch_ref.startswith(f"specrun/{spec_id}--")
            ) and wt_path:
                result = subprocess.run(
                    ["git", "worktree", "remove", wt_path, "--force"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"Removed worktree {wt_path} (by branch)")
                    removed_worktrees += 1

    # Remove local branches
    branch_patterns = [
        spec_id,
        f"spec/{spec_id}",
        f"specdoc/{spec_id}",
    ]

    # Find code/task/specrun branches
    branch_result = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    )
    if branch_result.returncode == 0:
        for branch in branch_result.stdout.strip().splitlines():
            branch = branch.strip()
            if (
                branch.startswith(f"code/{spec_id}--")
                or branch.startswith(f"task/{spec_id}--")
                or branch.startswith(f"specrun/{spec_id}--")
            ):
                branch_patterns.append(branch)

    removed_branches = 0
    for branch in branch_patterns:
        if not branch:
            continue
        check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
        )
        if check.returncode == 0:
            delete_result = subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True,
                text=True,
            )
            if delete_result.returncode == 0:
                print(f"Deleted local branch {branch}")
                removed_branches += 1

    if removed_worktrees == 0 and removed_branches == 0 and removed_workspaces == 0 and workspace_failures == 0:
        print(f"No artifacts found for spec '{spec_id}'.")
    return 1 if workspace_failures else 0


def _cleanup_run_owned_workspaces(
    *,
    common_root: Path,
    config: object,
    runs: list[object],
) -> tuple[int, int]:
    """Route exact run workspaces through their execution backend cleanup."""
    from dataclasses import replace

    from .execution_backend import WorkspaceHandle, get_execution_backend

    workspace_root = Path(config.execution.workspace_root).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = common_root / workspace_root
    workspace_root = workspace_root.resolve(strict=False)
    try:
        workspace_root.relative_to(common_root.resolve())
    except ValueError:
        print(
            f"Error: Refusing execution workspace cleanup outside the repository: {workspace_root}",
            file=sys.stderr,
        )
        return 0, 1

    removed = 0
    failures = 0
    cleaned_roots: set[str] = set()
    for run in runs:
        expected_source = (workspace_root / run.run_id / "source").resolve(strict=False)
        recorded = str(getattr(run, "worktree_path", "") or "").strip()
        recorded_source = Path(recorded).expanduser().resolve(strict=False) if recorded else expected_source
        if recorded_source != expected_source:
            continue
        run_root = expected_source.parent
        if not run_root.is_dir() or str(run_root) in cleaned_roots:
            continue

        backend_name = str(getattr(run, "backend", "") or "").strip()
        container_state = run_root / "backend-state" / "container-backend-state.json"
        if not backend_name:
            backend_name = "container" if container_state.is_file() else "clone"
        if backend_name not in {"clone", "container"}:
            continue
        if backend_name == "container" and not container_state.is_file():
            print(
                f"Error: Container state is missing for {run.run_id}; leaving {run_root} intact. ",
                "Run `spec container gc` to inspect labeled resources.",
                file=sys.stderr,
            )
            failures += 1
            continue

        execution = replace(
            config.execution,
            backend=backend_name,
            safety_mode=str(getattr(run, "safety_mode", "") or "").strip()
            or config.execution.safety_mode,
        )
        try:
            backend = get_execution_backend(execution)
            backend.cleanup(
                WorkspaceHandle(
                    path=expected_source,
                    outbox_path=run_root / "outbox",
                    branch=str(getattr(run, "branch", "") or ""),
                    backend=backend_name,
                ),
                # `spec clean` is the operator's explicit discard action. The
                # backend still enforces run-root/resource ownership and owns
                # container/volume teardown; only the recoverability guard is
                # intentionally waived for an inactive run.
                allow_unpushed_work=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: Could not clean execution workspace for {run.run_id}: {exc}", file=sys.stderr)
            if backend_name == "container":
                print("Run `spec container gc` to inspect remaining labeled resources.", file=sys.stderr)
            failures += 1
            continue
        cleaned_roots.add(str(run_root))
        print(f"Removed execution workspace {run_root} via {backend_name} backend")
        removed += 1
    return removed, failures


def _remove_worktree_path(target: Path) -> int:
    """Remove a worktree or stale directory. Returns 1 if removed, 0 otherwise."""
    # Check if it's a registered worktree
    wt_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    registered = False
    for line in wt_list.stdout.splitlines():
        if line.startswith("worktree ") and line[len("worktree ") :] == str(target):
            registered = True
            break

    if registered:
        result = subprocess.run(
            ["git", "worktree", "remove", str(target), "--force"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Removed worktree {target}")
            return 1
    elif target.is_dir():
        import shutil

        shutil.rmtree(target)
        print(f"Removed stale directory {target}")
        return 1
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    """Update the installed Spec Butler package in place."""
    from .update import cmd_update

    return cmd_update(args)


# ---------------------------------------------------------------------------
# Command handlers that delegate to orchestrator
# ---------------------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    """Launch interactive spec authoring (maps to orchestrator ``spec`` command)."""
    orch = _lazy_orchestrator()
    return orch.cmd_spec(args)


def _cmd_implement(args: argparse.Namespace) -> int:
    """Start or resume a full implementation workflow (maps to orchestrator ``run`` command)."""
    orch = _lazy_orchestrator()
    return orch.cmd_run(args)


def _cmd_stop(args: argparse.Namespace) -> int:
    """Stop the latest live orchestrator process group for a spec."""
    orch = _lazy_orchestrator()
    try:
        run = orch.stop_run(args.spec, repo_root=_resolve_repo_root())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Stopped run {run.run_id} for spec '{args.spec}'.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Display run state and gate status."""
    orch = _lazy_orchestrator()
    return orch.cmd_status(args)


def _cmd_report(args: argparse.Namespace) -> int:
    """Report implement-phase completion."""
    orch = _lazy_orchestrator()
    return orch.cmd_report(args)


def _cmd_task(args: argparse.Namespace) -> int:
    """Describe and execute a quick task."""
    orch = _lazy_orchestrator()
    return orch.cmd_task(args)


def _cmd_input(args: argparse.Namespace) -> int:
    """Resolve operator intervention for a waiting-for-input run."""
    orch = _lazy_orchestrator()
    return orch.cmd_input(args)


def _cmd_steer(args: argparse.Namespace) -> int:
    """Attach proactive steering guidance to the latest run for a spec."""
    orch = _lazy_orchestrator()
    return orch.cmd_steer(args)


def _cmd_analytics(args: argparse.Namespace) -> int:
    """Summarize local orchestrator history."""
    orch = _lazy_orchestrator()
    return orch.cmd_analytics(args)


def _cmd_phase(args: argparse.Namespace) -> int:
    """Run a single orchestrator phase (advanced/debug)."""
    orch = _lazy_orchestrator()
    return orch.cmd_step(args)


# ---------------------------------------------------------------------------
# Autopilot command handlers (watch, gc, auto run, auto stop)
# ---------------------------------------------------------------------------


def _cmd_watch(args: argparse.Namespace) -> int:
    """Interactive TUI dashboard."""
    from .autopilot import watch_command

    return watch_command(args)


def _cmd_gc(args: argparse.Namespace) -> int:
    """Reconcile stale run state."""
    from .autopilot import gc_command

    return gc_command(args)


def _cmd_web(args: argparse.Namespace) -> int:
    """Dispatch to web start / stop / status / token, or print help."""
    web_cmd = getattr(args, "web_command", None)
    if not web_cmd:
        args._web_parser.print_help()
        return 1

    repo_root = _resolve_repo_root()

    if web_cmd == "start":
        # Only the start subcommand requires web extras (starlette/uvicorn).
        # stop/status/token use only PID/token helpers and must work without them.
        try:
            import starlette  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            print(
                'Error: Web dependencies not installed.\nInstall with: pip install "specbutler[web]"',
                file=sys.stderr,
            )
            return 1
        from .web.server import run_server

        host = getattr(args, "host", "127.0.0.1")
        try:
            loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            print(
                "Warning: spec web is an operator control plane and does not terminate TLS. "
                "Prefer a loopback bind with a TLS-protected SSH or private-network tunnel.",
                file=sys.stderr,
            )
        return run_server(
            repo_root,
            host=host,
            port=getattr(args, "port", 7700),
            background=getattr(args, "background", False),
            open_browser=getattr(args, "open", False),
            verbose=getattr(args, "verbose", False),
        )
    if web_cmd == "stop":
        from .web.server import stop_server

        return stop_server(repo_root)
    if web_cmd == "status":
        from .web.server import server_status

        return server_status(repo_root)
    if web_cmd == "token":
        from .web.server import print_token

        return print_token(repo_root, reset=getattr(args, "reset", False))
    args._web_parser.print_help()
    return 1


def _cmd_coord(args: argparse.Namespace) -> int:
    """Dispatch to coord commands, or print help."""
    coord_cmd = getattr(args, "coord_command", None)
    if not coord_cmd:
        args._coord_parser.print_help()
        return 1
    if coord_cmd == "serve":
        from .coordinator_service import serve_from_args

        return serve_from_args(args)
    if coord_cmd == "init":
        from .coordinator_bootstrap import coord_init_from_args

        return coord_init_from_args(args)
    if coord_cmd == "doctor":
        from .coordinator_bootstrap import coord_doctor_from_args

        return coord_doctor_from_args(args)
    if coord_cmd == "token":
        token_cmd = getattr(args, "token_command", None)
        if token_cmd == "create":
            from .coordinator_service import token_create_from_args

            return token_create_from_args(args)
        if token_cmd == "revoke":
            from .coordinator_service import token_revoke_from_args

            return token_revoke_from_args(args)
        args._coord_parser.print_help()
        return 1
    if coord_cmd == "status":
        from .coordination import (
            CoordinatorAuthError,
            CoordinatorMalformedResponseError,
            CoordinatorUnavailableError,
            CoordinatorUnsupportedProtocolError,
            CoordinatorUnsupportedVersionError,
            build_client,
        )

        config = _lazy_config()
        coordination = config.coordination

        print(f"Coordination: {'enabled' if coordination.enabled else 'disabled (local-only)'}")
        print(f"Backend:      {coordination.backend or '-'}")
        print(f"Coordinator:  {coordination.redacted_url() or '-'}")
        print(f"Repo ID:      {coordination.repo_id or '-'}")
        print(f"Machine ID:   {coordination.machine_id or '-'}")
        print(f"Token:        {'set (hidden)' if coordination.token else 'not set'}")

        if not coordination.enabled:
            return 0

        try:
            client = build_client(coordination)
        except CoordinatorUnsupportedProtocolError as exc:
            print(f"Status:       unsupported-protocol ({exc})")
            return 1
        try:
            status = client.status()
        except CoordinatorAuthError as exc:
            print(f"Status:       auth-failed ({exc})")
            return 1
        except CoordinatorUnsupportedVersionError as exc:
            print(f"Status:       unsupported-version ({exc})")
            return 1
        except CoordinatorMalformedResponseError as exc:
            print(f"Status:       malformed-response ({exc})")
            return 1
        except CoordinatorUnavailableError as exc:
            print(f"Status:       unavailable ({exc})")
            return 1

        print(f"API version:  {status.api_version or '-'}")
        print(f"Status:       {'ok' if status.ok else 'error'} ({status.message})")
        return 0
    args._coord_parser.print_help()
    return 1


def _cmd_container(args: argparse.Namespace) -> int:
    """Dispatch to container diagnostics/bootstrap/smoke commands."""
    from .container import cmd_container

    return cmd_container(args)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run the read-only repository onboarding preflight."""
    from .doctor import cmd_doctor

    return cmd_doctor(args)


def _cmd_auto(args: argparse.Namespace) -> int:
    """Dispatch to auto run / auto stop, or print help."""
    auto_cmd = getattr(args, "auto_command", None)
    if not auto_cmd:
        # No sub-subcommand — print help and exit 1
        args._auto_parser.print_help()
        return 1
    if auto_cmd == "run":
        from .autopilot import parse_notify_backends, run_loop

        args.notify_backends = parse_notify_backends(
            getattr(args, "notify", []),
            default=True,
        )
        return run_loop(args)
    if auto_cmd == "stop":
        from .autopilot import stop_command

        return stop_command(args)
    args._auto_parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap a repo for spec-driven development."""
    from .init import cmd_init

    return cmd_init(args)


def _emit_startup_update_notice(config: object | None = None) -> None:
    from .config import SpecRuntimeConfig
    from .update import maybe_print_update_notice

    effective_config = config if isinstance(config, SpecRuntimeConfig) else SpecRuntimeConfig()
    maybe_print_update_notice(_resolve_repo_root(), effective_config)


def _maybe_print_update_notice_for_init() -> None:
    from .config import load_spec_runtime_config

    try:
        config = load_spec_runtime_config(require=False)
    except Exception:
        config = None

    _emit_startup_update_notice(config)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``spec`` CLI."""
    effective_argv = argv if argv is not None else sys.argv[1:]

    # Pre-detect "init" — it must bypass config loading since there's no
    # .spec.toml yet.  Do a lightweight scan for the first positional arg.
    first_positional = None
    for arg in effective_argv:
        if not arg.startswith("-"):
            first_positional = arg
            break

    # Handle --version before config loading so it works without .spec.toml.
    # Only match when --version is the first argument to avoid hijacking
    # subcommands where "--version" appears as a data value (e.g. --summary).
    if effective_argv and effective_argv[0] == "--version":
        from importlib.metadata import version

        print(version("specbutler"))
        return 0

    # Internal container parity probe. Keep this independent of repository
    # config so a freshly-built worker can report its exact package source.
    if effective_argv and effective_argv[0] == "--source-id":
        from .execution_backend import host_spec_runtime_source_id

        print(host_spec_runtime_source_id())
        return 0

    if first_positional == "init":
        _maybe_print_update_notice_for_init()
        init_parser = argparse.ArgumentParser(prog="spec init")
        init_parser.add_argument("-v", "--verbose", action="store_true")
        init_parser.add_argument(
            "--force",
            action="store_true",
            help="Refresh managed templates while preserving existing .spec.toml",
        )
        init_parser.add_argument(
            "--yolo",
            action="store_true",
            help="Use agent-assisted detection for build/test config",
        )
        init_args = init_parser.parse_args([a for a in effective_argv if a != "init"])
        if init_args.verbose:
            import logging

            logging.basicConfig(level=logging.DEBUG)
        return _cmd_init(init_args)

    if first_positional == "update":
        update_parser = argparse.ArgumentParser(prog="spec update")
        update_parser.add_argument("-v", "--verbose", action="store_true")
        update_args = update_parser.parse_args([a for a in effective_argv if a != "update"])
        if update_args.verbose:
            import logging

            logging.basicConfig(level=logging.DEBUG)
        return _cmd_update(update_args)

    if first_positional == "doctor":
        doctor_parser = argparse.ArgumentParser(
            prog="spec doctor",
            description="Validate repository configuration and local workflow dependencies without changing anything.",
        )
        doctor_parser.add_argument("-v", "--verbose", action="store_true")
        doctor_parser.add_argument(
            "--repo-root",
            default=None,
            help="Repository root (default: current Git checkout)",
        )
        doctor_argv = list(effective_argv)
        doctor_argv.remove("doctor")
        doctor_args = doctor_parser.parse_args(doctor_argv)
        if doctor_args.verbose:
            import logging

            logging.basicConfig(level=logging.DEBUG)
        return _cmd_doctor(doctor_args)

    # All other commands require config.
    from .config import SpecConfigNotFoundError

    config_error: Exception | None = None
    try:
        config = _lazy_config()
    except Exception as exc:
        config = None
        config_error = exc

    from .config import SpecRuntimeConfig

    if isinstance(config, SpecRuntimeConfig):
        _emit_startup_update_notice(config)
    elif config_error is not None:
        _emit_startup_update_notice()

    if config_error is not None:
        if isinstance(config_error, SpecConfigNotFoundError):
            print(f"Error: {config_error}", file=sys.stderr)
            return 1
        raise config_error

    parser = argparse.ArgumentParser(
        prog="spec",
        description=(
            "Spec-driven development workflow CLI. "
            "Use intent-level commands for the standard workflow "
            "and 'spec phase' for advanced/debug control."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ----- Primary commands (happy-path) -----

    raw_review_default = getattr(config.agents, "review_default", "")
    review_default = raw_review_default.strip() if isinstance(raw_review_default, str) else ""
    review_default = review_default or config.agents.default

    # create — author a new spec
    p_create = subparsers.add_parser(
        "create",
        help="Author a new spec interactively",
    )
    p_create.add_argument(
        "--spec",
        default="",
        help="Optional spec ID; when omitted, chosen during authoring",
    )
    p_create.add_argument("--agent", default=config.agents.default, help="Agent: claude|codex")
    p_create.add_argument("--base", default=config.base_ref, help="Base ref for new worktrees")
    p_create.add_argument("--label", default="", help="Legacy alias for --spec")

    # implement — start or resume implementation
    p_implement = subparsers.add_parser(
        "implement",
        help="Start or resume a full implementation workflow",
    )
    p_implement.add_argument("--spec", required=True, help="Spec ID")
    p_implement.add_argument("--agent", default=config.agents.default, help="Implementation agent: claude|codex")
    p_implement.add_argument(
        "--review-agent",
        default=review_default,
        help="Review agent: claude|codex",
    )
    p_implement.add_argument("--base", default=None, help="Base ref for new worktrees")
    p_implement.add_argument("--run", default="", help="Resume a specific run id")
    p_implement.add_argument("--branch", default="", help="Reuse an existing branch")
    p_implement.add_argument(
        "--coordination-bypass",
        action="store_true",
        help=(
            "Emergency bypass for a configured coordinator; runs local-only and may allow cross-machine duplicate work."
        ),
    )
    p_implement.add_argument(
        "--reset-intake",
        action="store_true",
        help="Force re-capture intake answers",
    )
    p_implement.add_argument(
        "--retry-cap",
        type=int,
        default=None,
        help=f"Max implement retries (default: {config.retry_cap})",
    )

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop the active workflow for a spec")
    p_stop.add_argument("--spec", required=True, help="Spec ID")

    # status
    p_status = subparsers.add_parser("status", help="Show run state and gate status")
    p_status.add_argument("--spec", required=True, help="Spec ID")
    p_status.add_argument("--run", default="", help="Show a specific run id")

    # list
    p_list = subparsers.add_parser("list", help="List specs with status and dependencies")
    p_list.add_argument(
        "--all",
        action="store_true",
        help="Include merged and obsolete specs",
    )

    # show
    p_show = subparsers.add_parser("show", help="Display a spec's content")
    p_show.add_argument("--spec", required=True, help="Spec ID")

    # report
    p_report = subparsers.add_parser(
        "report",
        help="Report implement-phase completion status",
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

    # clean
    p_clean = subparsers.add_parser(
        "clean",
        help="Remove worktrees and branches for a spec",
    )
    p_clean.add_argument("--spec", required=True, help="Spec ID to clean")

    # task
    p_task = subparsers.add_parser(
        "task",
        help="Describe a task conversationally then execute it",
    )
    p_task.add_argument("--agent", default=config.agents.default, help="Implementation agent: claude|codex")
    p_task.add_argument(
        "--review-agent",
        default=review_default,
        help="Review agent: claude|codex",
    )
    p_task.add_argument("--base", default=config.base_ref, help="Base ref for new worktrees")

    # update
    subparsers.add_parser(
        "update",
        help="Check for and install the latest Spec Butler version",
    )

    # doctor — read-only onboarding preflight
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Validate repository configuration and local workflow dependencies",
    )
    p_doctor.add_argument("--repo-root", default=None, help="Repository root")

    # ----- Autopilot commands -----

    # watch — interactive TUI dashboard
    p_watch = subparsers.add_parser(
        "watch",
        help="Interactive TUI dashboard for spec runs",
    )
    p_watch.add_argument("--repo-root", default=None, help="Repository root")
    p_watch.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds")
    p_watch.add_argument("--agent", choices=("claude", "codex"), default=None, help="Filter by agent")

    # gc — reconcile stale run state
    p_gc = subparsers.add_parser(
        "gc",
        help="Reconcile stale run state (dry-run by default)",
    )
    p_gc.add_argument("--repo-root", default=None, help="Repository root")
    p_gc.add_argument("--apply", action="store_true", help="Actually mutate run records (dry-run by default)")

    # web — local web server for monitoring and control
    p_web = subparsers.add_parser(
        "web",
        help="Local web server for spec monitoring and control",
    )
    web_sub = p_web.add_subparsers(dest="web_command")

    p_web_start = web_sub.add_parser(
        "start",
        help="Launch the web server",
    )
    p_web_start.add_argument("--port", type=int, default=7700, help="Port (default: 7700)")
    p_web_start.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    p_web_start.add_argument(
        "--open",
        action="store_true",
        help="Open the authenticated URL in the default browser",
    )
    p_web_start.add_argument(
        "--background",
        "-b",
        action="store_true",
        help="Daemonise (write PID to .spec-state/web/server.pid)",
    )
    p_web_start.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug-level logging (shows Codex stderr, etc.)",
    )

    web_sub.add_parser("stop", help="Stop the background web server")

    web_sub.add_parser("status", help="Show whether the web server is running")

    p_web_token = web_sub.add_parser("token", help="Print or reset the auth token")
    p_web_token.add_argument(
        "--reset",
        action="store_true",
        help="Regenerate the auth token",
    )

    # coord — coordination diagnostics and service
    p_coord = subparsers.add_parser(
        "coord",
        help="Coordination commands (init, doctor, status, serve, token)",
    )
    coord_sub = p_coord.add_subparsers(dest="coord_command")
    p_coord_init = coord_sub.add_parser(
        "init",
        help="Bootstrap coordinator server or worker configuration",
    )
    init_mode = p_coord_init.add_mutually_exclusive_group(required=True)
    init_mode.add_argument("--server", action="store_true", help="Initialize a local coordinator database and tokens")
    init_mode.add_argument("--worker", action="store_true", help="Write local worker coordinator configuration")
    p_coord_init.add_argument(
        "--db", default="~/.local/state/spec/coord.sqlite", help="Coordinator SQLite database path"
    )
    p_coord_init.add_argument("--host", default="127.0.0.1", help="Coordinator bind host for printed serve command")
    p_coord_init.add_argument("--port", type=int, default=8765, help="Coordinator bind port for printed serve command")
    p_coord_init.add_argument("--worker-token-name", default="worker-default", help="Worker token name to create")
    p_coord_init.add_argument("--operator-token-name", default="operator-cli", help="Operator token name to create")
    p_coord_init.add_argument("--skip-existing-tokens", action="store_true", help="Do not rotate existing named tokens")
    p_coord_init.add_argument("--url", default="", help="Coordinator URL for --worker")
    p_coord_init.add_argument("--repo-id", default="", help="Coordinator repo id for --worker")
    p_coord_init.add_argument("--machine-id", default="", help="Worker machine id for --worker")
    p_coord_init.add_argument("--token", default="", help="Worker token for --worker")
    p_coord_init.add_argument("--env-only", action="store_true", help="Print environment exports without writing files")
    p_coord_init.add_argument(
        "--force", action="store_true", help="Rotate existing tokens or overwrite local worker token"
    )
    coord_sub.add_parser(
        "doctor",
        help="Validate coordinator connectivity, auth, and lease behavior",
    )
    coord_sub.add_parser(
        "status",
        help="Show coordinator configuration and connectivity (no secrets)",
    )
    p_coord_serve = coord_sub.add_parser(
        "serve",
        help="Run the authenticated SQLite coordinator service",
    )
    p_coord_serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_coord_serve.add_argument("--port", type=int, default=8765, help="Bind port")
    p_coord_serve.add_argument("--db", required=True, help="Coordinator SQLite database path")
    p_coord_serve.add_argument(
        "--worker-token",
        default="",
        help="Optional worker token; falls back to SPEC_COORDINATOR_WORKER_TOKEN and is hidden from startup logs",
    )
    p_coord_serve.add_argument(
        "--operator-token",
        default="",
        help="Optional operator token; falls back to SPEC_COORDINATOR_OPERATOR_TOKEN and is hidden from startup logs",
    )
    p_coord_token = coord_sub.add_parser(
        "token",
        help="Create or revoke coordinator bearer tokens stored as hashes",
    )
    token_sub = p_coord_token.add_subparsers(dest="token_command")
    p_token_create = token_sub.add_parser("create", help="Create or rotate a token and print it once")
    p_token_create.add_argument("--db", required=True, help="Coordinator SQLite database path")
    p_token_create.add_argument("--name", required=True, help="Token name")
    p_token_create.add_argument("--scope", required=True, choices=("worker", "operator"), help="Token scope")
    p_token_revoke = token_sub.add_parser("revoke", help="Revoke a token by name")
    p_token_revoke.add_argument("--db", required=True, help="Coordinator SQLite database path")
    p_token_revoke.add_argument("--name", required=True, help="Token name")

    # auto — subcommand group for fleet-level autopilot
    p_auto = subparsers.add_parser(
        "auto",
        help="Fleet-level autopilot commands (run, stop)",
    )
    auto_sub = p_auto.add_subparsers(dest="auto_command")

    p_auto_run = auto_sub.add_parser("run", help="Dispatch loop — run multiple specs in parallel")
    p_auto_run.add_argument("--repo-root", default=None, help="Repository root")
    p_auto_run.add_argument("--concurrency", type=int, default=None, help="Max parallel spec runs")
    p_auto_run.add_argument("--poll-interval", type=int, default=5, help="Poll interval in seconds")
    p_auto_run.add_argument("--notify", action="append", default=[], help="Notification backends")
    p_auto_run.add_argument("--notify-success", action="store_true", help="Also notify on success")
    p_auto_run.add_argument("--dry-run", action="store_true", help="Show what would be dispatched")
    p_auto_run.add_argument("--agent", choices=("claude", "codex"), default=None, help="Agent to use")

    p_auto_stop = auto_sub.add_parser("stop", help="Graceful shutdown of running dispatcher")
    p_auto_stop.add_argument("--repo-root", default=None, help="Repository root")

    # container — diagnostics and bootstrap for the container execution backend
    p_container = subparsers.add_parser(
        "container",
        help="Container backend diagnostics, bootstrap, and smoke tests",
    )
    container_sub = p_container.add_subparsers(dest="container_command")
    p_container_doctor = container_sub.add_parser("doctor", help="Diagnose container backend readiness")
    p_container_doctor.add_argument("--repo-root", default=None, help="Repository root")
    p_container_init = container_sub.add_parser("init", help="Create repo-local container worker bootstrap files")
    p_container_init.add_argument("--repo-root", default=None, help="Repository root")
    p_container_init.add_argument("--force", action="store_true", help="Overwrite existing bootstrap files")
    p_container_init.add_argument(
        "--no-agents",
        action="store_true",
        help="Do not install configured agent CLIs in the generated worker Dockerfile",
    )
    p_container_init.add_argument(
        "--source-repository",
        default="",
        metavar="HTTPS_URL",
        help="Override the auto-detected public Spec Butler source repository",
    )
    p_container_smoke = container_sub.add_parser("smoke", help="Smoke test the configured container backend")
    p_container_smoke.add_argument("--repo-root", default=None, help="Repository root")
    p_container_smoke.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip the configured [bootstrap].install_command during smoke",
    )
    p_container_smoke.add_argument(
        "--verify-gates",
        action="store_true",
        help="Also run configured verify gates inside the worker",
    )
    p_container_smoke.add_argument("--timeout", type=float, default=300, help="Per-command timeout in seconds")
    p_container_gc = container_sub.add_parser("gc", help="Discover and remove stale spec-owned Docker resources")
    p_container_gc.add_argument("--repo-root", default=None, help="Repository root")
    gc_mode = p_container_gc.add_mutually_exclusive_group()
    gc_mode.add_argument("--dry-run", action="store_true", help="List stale resources (default)")
    gc_mode.add_argument("--apply", action="store_true", help="Remove stale resources")

    # ----- Advanced / debug commands -----

    # phase — run a single phase
    p_phase = subparsers.add_parser(
        "phase",
        help="[Advanced] Run a single orchestrator phase",
    )
    p_phase.add_argument("--spec", required=True, help="Spec ID")
    p_phase.add_argument("--phase", required=True, dest="phase", help="Phase name")
    p_phase.add_argument("--agent", default=config.agents.default, help="Implementation agent: claude|codex")
    p_phase.add_argument(
        "--review-agent",
        default=review_default,
        help="Review agent: claude|codex",
    )
    p_phase.add_argument("--base", default=None, help="Base ref for new worktrees")
    p_phase.add_argument("--run", default="", help="Operate on a specific run id")
    p_phase.add_argument(
        "--reset-intake",
        action="store_true",
        help="Force re-capture intake answers",
    )

    # input
    p_input = subparsers.add_parser(
        "input",
        help="[Advanced] Resolve operator intervention for a waiting-for-input run",
    )
    p_input.add_argument("--spec", required=True, help="Spec ID")
    p_input.add_argument("--agent", default=None, help="Agent: claude|codex")

    p_steer = subparsers.add_parser(
        "steer",
        help="Attach proactive steering guidance to the latest run for a spec",
    )
    p_steer.add_argument("--spec", required=True, help="Spec ID")
    p_steer.add_argument("--message", required=True, help="Advisory guidance for the next implement attempt")

    # analytics
    p_analytics = subparsers.add_parser(
        "analytics",
        help="[Advanced] Summarize local orchestrator history",
    )
    p_analytics.add_argument("--spec", default="", help="Filter to one spec id")
    p_analytics.add_argument("--run", default="", help="Filter to one run id")
    p_analytics.add_argument(
        "--since",
        default="",
        help="Filter to records updated on/after this date",
    )

    # ----- Parse and dispatch -----

    args = parser.parse_args(argv)

    if args.verbose:
        import logging

        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        # Primary
        "create": _cmd_create,
        "implement": _cmd_implement,
        "stop": _cmd_stop,
        "status": _cmd_status,
        "list": _cmd_list,
        "show": _cmd_show,
        "report": _cmd_report,
        "clean": _cmd_clean,
        "steer": _cmd_steer,
        "task": _cmd_task,
        "update": _cmd_update,
        "doctor": _cmd_doctor,
        # Autopilot
        "watch": _cmd_watch,
        "gc": _cmd_gc,
        "auto": _cmd_auto,
        "coord": _cmd_coord,
        "container": _cmd_container,
        # Web
        "web": _cmd_web,
        # Advanced
        "phase": _cmd_phase,
        "input": _cmd_input,
        "analytics": _cmd_analytics,
    }

    # Stash auto/web/coord parsers on args so handlers can print help
    if args.command == "auto":
        args._auto_parser = p_auto
    if args.command == "web":
        args._web_parser = p_web
    if args.command == "coord":
        args._coord_parser = p_coord
    if args.command == "container":
        args._container_parser = p_container

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
