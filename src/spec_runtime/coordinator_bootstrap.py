"""Bootstrap and doctor commands for the SQLite coordinator."""

from __future__ import annotations

import argparse
import contextlib
import io
import socket
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

from . import config as config_module
from .config import CoordinationConfig, load_spec_runtime_config
from .coordination import (
    CoordinatorAuthError,
    CoordinatorError,
    CoordinatorLeaseConflictError,
    CoordinatorMalformedResponseError,
    CoordinatorUnavailableError,
    CoordinatorUnsupportedProtocolError,
    CoordinatorUnsupportedVersionError,
    build_client,
)
from .coordinator_service import CoordinatorStore
from .git_common import resolve_common_root, run_git

DEFAULT_DB_PATH = "~/.local/state/spec/coord.sqlite"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WORKER_TOKEN_NAME = "worker-default"
DEFAULT_OPERATOR_TOKEN_NAME = "operator-cli"
DOCTOR_SPEC_ID = "__coord_doctor__"


def coord_init_from_args(args: argparse.Namespace) -> int:
    if bool(args.server) == bool(args.worker):
        print("Error: choose exactly one of --server or --worker.", file=sys.stderr)
        return 2
    if args.server:
        return _init_server(args)
    return _init_worker(args)


def coord_doctor_from_args(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    config = load_spec_runtime_config()
    coordination = config.coordination

    print("Coordinator doctor")
    print(f"Backend:      {coordination.backend or '-'}")
    print(f"Coordinator:  {coordination.redacted_url() or '-'}")
    print(f"Repo ID:      {coordination.repo_id or '-'}")
    print(f"Machine ID:   {coordination.machine_id or '-'}")
    print(f"Token:        {'set (hidden)' if coordination.token else 'not set'}")

    failures: list[str] = []
    if _coordination_bypass_help_visible():
        print("OK: spec implement --help shows --coordination-bypass for emergencies")
    else:
        failures.append("spec implement --help does not show --coordination-bypass")
    if not coordination.enabled:
        failures.append("coordination URL is not configured")
    if not coordination.repo_id:
        failures.append("repo id is missing")
    if not coordination.machine_id:
        failures.append("machine id is missing")
    if not coordination.token:
        failures.append("token is missing")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    try:
        client = build_client(coordination)
    except CoordinatorUnsupportedProtocolError as exc:
        print(f"FAIL: unsupported coordinator backend: {exc}")
        return 1

    try:
        status = client.status()
    except CoordinatorAuthError as exc:
        print(f"FAIL: authentication failed: {exc}")
        return 1
    except CoordinatorUnsupportedVersionError as exc:
        print(f"FAIL: incompatible API version: {exc}")
        return 1
    except CoordinatorMalformedResponseError as exc:
        print(f"FAIL: malformed coordinator response: {exc}")
        return 1
    except CoordinatorUnavailableError as exc:
        print(f"FAIL: coordinator unreachable: {exc}")
        return 1

    print(f"OK: API version {status.api_version or '-'} ({status.message})")
    payload = _doctor_payload(repo_root, coordination)
    lease: dict[str, Any] | None = None
    released = False
    conflict_payload: dict[str, Any] | None = None
    conflict_id = ""
    conflict_released = False
    reacquire_payload: dict[str, Any] | None = None
    reacquire_id = ""
    reacquire_released = False
    try:
        lease = client.acquire_lease(payload)
        lease_id = str(lease.get("lease_id") or "").strip()
        if not lease_id:
            print("FAIL: lease acquire response did not include lease_id")
            return 1
        print(f"OK: acquired synthetic lease {lease_id}")

        conflict_payload = payload | {
            "run_id": f"{payload['run_id']}-conflict",
            "machine_id": f"{payload['machine_id']}-conflict",
        }
        try:
            conflict_lease = client.acquire_lease(conflict_payload)
        except CoordinatorLeaseConflictError:
            print("OK: conflicting synthetic lease was rejected")
        else:
            conflict_id = str(conflict_lease.get("lease_id") or "").strip()
            print("FAIL: conflicting synthetic lease was unexpectedly acquired")
            return 1

        client.heartbeat_lease(lease_id, payload)
        print("OK: heartbeat accepted")
        client.release_lease(lease_id, payload)
        released = True
        print("OK: released synthetic lease")
        reacquire_payload = payload | {"run_id": f"{payload['run_id']}-reacquire"}
        reacquired = client.acquire_lease(reacquire_payload)
        reacquire_id = str(reacquired.get("lease_id") or "").strip()
        if not reacquire_id:
            print("FAIL: re-acquire response did not include lease_id")
            return 1
        client.release_lease(reacquire_id, reacquire_payload)
        reacquire_released = True
        print("OK: re-acquired and cleaned up synthetic lease")
    except CoordinatorAuthError as exc:
        print(f"FAIL: token scope/authentication rejected lease operation: {exc}")
        return 1
    except CoordinatorError as exc:
        print(f"FAIL: coordinator lease smoke test failed: {exc}")
        return 1
    finally:
        if conflict_payload and conflict_id and not conflict_released:
            try:
                client.release_lease(conflict_id, conflict_payload)
                conflict_released = True
                print("OK: cleaned up unexpected conflict lease after failure")
            except CoordinatorError as exc:
                print(f"WARN: failed to clean up unexpected conflict lease {conflict_id}: {exc}")
        if lease and not released:
            lease_id = str(lease.get("lease_id") or "").strip()
            if lease_id:
                try:
                    client.release_lease(lease_id, payload)
                    print("OK: cleaned up synthetic lease after failure")
                except CoordinatorError as exc:
                    print(f"WARN: failed to clean up synthetic lease {lease_id}: {exc}")
        if reacquire_payload and reacquire_id and not reacquire_released:
            try:
                client.release_lease(reacquire_id, reacquire_payload)
                print("OK: cleaned up re-acquired synthetic lease after failure")
            except CoordinatorError as exc:
                print(f"WARN: failed to clean up re-acquired synthetic lease {reacquire_id}: {exc}")

    print("Doctor status: ok")
    return 0


def _init_server(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    store = CoordinatorStore(db_path)
    try:
        try:
            _preflight_bootstrap_tokens(
                store,
                worker_token_name=args.worker_token_name,
                operator_token_name=args.operator_token_name,
                force=args.force,
                skip_existing=args.skip_existing_tokens,
            )
            worker_token = _create_bootstrap_token(
                store,
                name=args.worker_token_name,
                scope="worker",
                force=args.force,
                skip_existing=args.skip_existing_tokens,
            )
            operator_token = _create_bootstrap_token(
                store,
                name=args.operator_token_name,
                scope="operator",
                force=args.force,
                skip_existing=args.skip_existing_tokens,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    finally:
        store.close()

    print(f"Coordinator DB initialized: {db_path}")
    if worker_token:
        print(f"worker token {args.worker_token_name}:")
        print(worker_token)
    else:
        print(f"worker token {args.worker_token_name}: unchanged (existing token skipped)")
    if operator_token:
        print(f"operator token {args.operator_token_name}:")
        print(operator_token)
    else:
        print(f"operator token {args.operator_token_name}: unchanged (existing token skipped)")
    print("Start coordinator with:")
    print(f"spec coord serve --host {args.host} --port {args.port} --db {db_path}")
    return 0


def _preflight_bootstrap_tokens(
    store: CoordinatorStore,
    *,
    worker_token_name: str,
    operator_token_name: str,
    force: bool,
    skip_existing: bool,
) -> None:
    if worker_token_name == operator_token_name:
        raise ValueError("worker and operator token names must differ")
    if force or skip_existing:
        return
    existing = [
        name
        for name in (worker_token_name, operator_token_name)
        if store.token_exists(name=name)
    ]
    if existing:
        quoted = ", ".join(repr(name) for name in existing)
        raise ValueError(f"token(s) {quoted} already exist; pass --force or --skip-existing-tokens")


def _init_worker(args: argparse.Namespace) -> int:
    repo_root = _active_config_root()
    values = {
        "backend": "http",
        "url": args.url.strip(),
        "repo_id": args.repo_id.strip(),
        "machine_id": args.machine_id.strip() or socket.gethostname(),
        "token": args.token,
    }
    missing = [name for name, value in values.items() if not str(value).strip()]
    if missing:
        print(f"Error: missing required worker setting(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.env_only:
        _print_env_exports(values)
        return 0

    local_path = repo_root / ".spec.local.toml"
    existing = _read_local_toml(local_path)
    existing_token = str(existing.get("coordination", {}).get("token", "")).strip()
    if existing_token and existing_token != values["token"] and not args.force:
        print(
            "Error: .spec.local.toml already has a coordination token; pass --force to overwrite it.",
            file=sys.stderr,
        )
        return 1

    _write_local_coordination(local_path, values)
    print(f"Wrote coordinator worker config to {local_path}")
    _warn_if_local_config_tracked_or_unignored(repo_root)
    _print_env_exports(values)
    return 0


def _create_bootstrap_token(
    store: CoordinatorStore,
    *,
    name: str,
    scope: str,
    force: bool,
    skip_existing: bool,
) -> str:
    if store.token_exists(name=name) and not force:
        if skip_existing:
            return ""
        raise ValueError(f"token {name!r} already exists; pass --force or --skip-existing-tokens")
    return store.create_token(name=name, scope=scope)


def _repo_root() -> Path:
    try:
        return resolve_common_root()
    except Exception:
        return Path.cwd()


def _active_config_root() -> Path:
    return config_module._config_path().parent


def _doctor_payload(repo_root: Path, coordination: CoordinationConfig) -> dict[str, Any]:
    run_suffix = uuid.uuid4().hex[:12]
    return {
        "repo_id": coordination.repo_id,
        "spec_id": DOCTOR_SPEC_ID,
        "run_id": f"doctor-{run_suffix}",
        "machine_id": coordination.machine_id,
        "hostname": socket.gethostname(),
        "display_name": f"doctor:{coordination.machine_id}",
        "agent": "coord-doctor",
        "ttl_seconds": 60,
        "worktree_path": str(repo_root),
        "metadata": {"kind": "coord-doctor"},
    }


def _coordination_bypass_help_visible() -> bool:
    from .cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main(["implement", "--help"])
    except SystemExit as exc:
        if exc.code not in (0, None):
            return False
    return "--coordination-bypass" in stdout.getvalue()


def _read_local_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_local_coordination(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    output: list[str] = []
    index = 0
    in_coordination = False
    written = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_coordination and not written:
                output.extend(_coordination_lines(values))
                written = True
            in_coordination = stripped == "[coordination]"
            output.append(line)
            index += 1
            continue
        if in_coordination and _toml_key(line) in values:
            index += 1
            continue
        output.append(line)
        index += 1

    if in_coordination and not written:
        output.extend(_coordination_lines(values))
        written = True
    if not written:
        if output and output[-1].strip():
            output.append("")
        output.append("[coordination]")
        output.extend(_coordination_lines(values))
    path.write_text("\n".join(output).rstrip() + "\n")


def _coordination_lines(values: dict[str, str]) -> list[str]:
    return [f'{key} = "{_toml_escape(value)}"' for key, value in values.items()]


def _toml_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    return stripped.split("=", 1)[0].strip()


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _print_env_exports(values: dict[str, str]) -> None:
    print("Equivalent environment exports:")
    print(f"export SPEC_COORDINATOR_BACKEND={_shell_quote(values['backend'])}")
    print(f"export SPEC_COORDINATOR_URL={_shell_quote(values['url'])}")
    print(f"export SPEC_COORDINATOR_REPO_ID={_shell_quote(values['repo_id'])}")
    print(f"export SPEC_MACHINE_ID={_shell_quote(values['machine_id'])}")
    print(f"export SPEC_COORDINATOR_TOKEN={_shell_quote(values['token'])}")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _warn_if_local_config_tracked_or_unignored(repo_root: Path) -> None:
    tracked = run_git(
        ["ls-files", "--error-unmatch", ".spec.local.toml"],
        cwd=repo_root,
    ).returncode == 0
    ignored = run_git(
        ["check-ignore", "-q", ".spec.local.toml"],
        cwd=repo_root,
    ).returncode == 0
    if tracked or not ignored:
        print(
            "WARNING: .spec.local.toml is not safely ignored by git. "
            "Add this exact entry to .gitignore:",
            file=sys.stderr,
        )
        print(".spec.local.toml", file=sys.stderr)
