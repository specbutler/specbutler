"""SQLite-backed coordinator HTTP service.

The service is intentionally small and dependency-free: it uses stdlib
``http.server`` for the API surface and SQLite as the single writer-owned
state store. Clients talk HTTP only; the database file is private to the
coordinator process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

API_VERSION = "1"
CAPABILITIES = (
    "leases.acquire",
    "leases.heartbeat",
    "leases.release",
    "leases.inspect",
    "machines.inspect",
)
DEFAULT_LEASE_TTL_SECONDS = 900


class CoordinatorServiceError(Exception):
    """Base class for coordinator service failures."""


class AuthError(CoordinatorServiceError):
    """Raised when a request is missing or has invalid credentials."""

    def __init__(self, message: str, *, status: HTTPStatus = HTTPStatus.UNAUTHORIZED) -> None:
        super().__init__(message)
        self.status = status


LEASE_OWNER_FIELDS = ("repo_id", "spec_id", "run_id", "machine_id")


@dataclass(frozen=True)
class AuthenticatedToken:
    name: str
    scope: str


def utc_now() -> float:
    return time.time()


def iso_timestamp(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(utc_now() if epoch is None else epoch, UTC).isoformat().replace("+00:00", "Z")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "spec_" + secrets.token_urlsafe(32)


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize or migrate the coordinator schema idempotently."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS machines (
            machine_id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            display_name TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leases (
            lease_id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT NOT NULL,
            spec_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            status TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            expires_at_epoch REAL NOT NULL,
            worktree_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_leases_repo_spec_status
            ON leases(repo_id, spec_id, status, expires_at_epoch);
        CREATE INDEX IF NOT EXISTS idx_leases_owner
            ON leases(repo_id, spec_id, run_id, machine_id);

        CREATE TABLE IF NOT EXISTS lease_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            lease_id INTEGER,
            event_type TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            spec_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lease_id) REFERENCES leases(lease_id)
        );

        CREATE TABLE IF NOT EXISTS tokens (
            token_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (1, iso_timestamp()),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class CoordinatorStore:
    def __init__(self, db_path: Path, *, now: Any = utc_now) -> None:
        self.db_path = db_path
        self._now = now
        self._lock = threading.RLock()
        self.conn = connect_db(db_path)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def create_token(self, *, name: str, scope: str, token: str | None = None) -> str:
        if scope not in {"worker", "operator"}:
            raise ValueError("scope must be worker or operator")
        token_value = token or generate_token()
        now = iso_timestamp(self._now())
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tokens(name, scope, token_hash, created_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(name) DO UPDATE SET
                    scope = excluded.scope,
                    token_hash = excluded.token_hash,
                    created_at = excluded.created_at,
                    revoked_at = NULL
                """,
                (name, scope, hash_token(token_value), now),
            )
        return token_value

    def token_exists(self, *, name: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM tokens WHERE name = ? AND revoked_at IS NULL",
                (name,),
            ).fetchone()
        return row is not None

    def revoke_token(self, *, name: str) -> bool:
        with self._lock:
            result = self.conn.execute(
                "UPDATE tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (iso_timestamp(self._now()), name),
            )
        return result.rowcount > 0

    def authenticate(self, token: str, env_tokens: dict[str, str] | None = None) -> AuthenticatedToken | None:
        token_hash = hash_token(token)
        with self._lock:
            row = self.conn.execute(
                """
                SELECT name, scope FROM tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        if row:
            return AuthenticatedToken(name=str(row["name"]), scope=str(row["scope"]))
        for scope, configured in (env_tokens or {}).items():
            if configured and secrets.compare_digest(token, configured):
                return AuthenticatedToken(name=f"env:{scope}", scope=scope)
        return None

    def _upsert_machine(self, payload: dict[str, Any], now_epoch: float) -> None:
        machine_id = str(payload["machine_id"]).strip()
        hostname = str(payload.get("hostname") or machine_id).strip()
        display_name = str(payload.get("display_name") or hostname).strip()
        now_text = iso_timestamp(now_epoch)
        self.conn.execute(
            """
            INSERT INTO machines(machine_id, hostname, display_name, last_heartbeat_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id) DO UPDATE SET
                hostname = excluded.hostname,
                display_name = excluded.display_name,
                last_heartbeat_at = excluded.last_heartbeat_at,
                updated_at = excluded.updated_at
            """,
            (machine_id, hostname, display_name, now_text, now_text, now_text),
        )

    def _record_event(self, lease_id: int | None, event_type: str, payload: dict[str, Any], message: str) -> None:
        self.conn.execute(
            """
            INSERT INTO lease_events(
                lease_id, event_type, repo_id, spec_id, run_id, machine_id, agent, message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                event_type,
                str(payload["repo_id"]).strip(),
                str(payload["spec_id"]).strip(),
                str(payload["run_id"]).strip(),
                str(payload["machine_id"]).strip(),
                str(payload["agent"]).strip(),
                message,
                iso_timestamp(self._now()),
            ),
        )

    def acquire_lease(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        required = ("repo_id", "spec_id", "run_id", "machine_id", "agent")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")

        now_epoch = self._now()
        ttl = max(1, int(payload.get("ttl_seconds") or DEFAULT_LEASE_TTL_SECONDS))
        heartbeat_at = iso_timestamp(now_epoch)
        expires_epoch = now_epoch + ttl
        expires_at = iso_timestamp(expires_epoch)

        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_machine(payload, now_epoch)
                conflicts = self.conn.execute(
                    """
                    SELECT * FROM leases
                    WHERE repo_id = ? AND spec_id = ? AND status = 'active'
                    ORDER BY lease_id
                    """,
                    (payload["repo_id"], payload["spec_id"]),
                ).fetchall()

                for row in conflicts:
                    if float(row["expires_at_epoch"]) <= now_epoch:
                        self.conn.execute(
                            """
                            UPDATE leases
                            SET status = 'expired', updated_at = ?
                            WHERE lease_id = ?
                            """,
                            (heartbeat_at, row["lease_id"]),
                        )
                        expired_payload = dict(payload)
                        expired_payload["run_id"] = row["run_id"]
                        expired_payload["machine_id"] = row["machine_id"]
                        expired_payload["agent"] = row["agent"]
                        self._record_event(int(row["lease_id"]), "expired", expired_payload, "expired lease reclaimed")
                        continue

                    same_owner = (
                        row["run_id"] == payload["run_id"]
                        and row["machine_id"] == payload["machine_id"]
                    )
                    if same_owner:
                        self.conn.execute(
                            """
                            UPDATE leases
                            SET heartbeat_at = ?, expires_at = ?, expires_at_epoch = ?,
                                agent = ?, worktree_path = COALESCE(?, worktree_path), updated_at = ?
                            WHERE lease_id = ?
                            """,
                            (
                                heartbeat_at,
                                expires_at,
                                expires_epoch,
                                payload["agent"],
                                payload.get("worktree_path"),
                                heartbeat_at,
                                row["lease_id"],
                            ),
                        )
                        self._record_event(int(row["lease_id"]), "renewed", payload, "same owner reacquired lease")
                        renewed = self.get_lease(int(row["lease_id"]))
                        self.conn.execute("COMMIT")
                        return HTTPStatus.OK, {"lease": renewed, "idempotent": True}

                    self.conn.execute("ROLLBACK")
                    return (
                        HTTPStatus.CONFLICT,
                        {
                            "error": "lease-conflict",
                            "message": "non-expired lease already exists for repo/spec",
                            "lease": _row_to_dict(row),
                        },
                    )

                cursor = self.conn.execute(
                    """
                    INSERT INTO leases(
                        repo_id, spec_id, run_id, machine_id, agent, status,
                        heartbeat_at, expires_at, expires_at_epoch, worktree_path,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["repo_id"],
                        payload["spec_id"],
                        payload["run_id"],
                        payload["machine_id"],
                        payload["agent"],
                        heartbeat_at,
                        expires_at,
                        expires_epoch,
                        payload.get("worktree_path"),
                        heartbeat_at,
                        heartbeat_at,
                    ),
                )
                lease_id = int(cursor.lastrowid)
                self._record_event(lease_id, "acquired", payload, "lease acquired")
                lease = self.get_lease(lease_id)
                self.conn.execute("COMMIT")
                return HTTPStatus.CREATED, {"lease": lease, "idempotent": False}
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def get_lease(self, lease_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise KeyError(str(lease_id))
        return _row_to_dict(row)

    def _lease_owner(self, payload: dict[str, Any]) -> dict[str, str]:
        missing = [name for name in LEASE_OWNER_FIELDS if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        return {name: str(payload[name]).strip() for name in LEASE_OWNER_FIELDS}

    def heartbeat_lease(self, lease_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        owner = self._lease_owner(payload)
        now_epoch = self._now()
        ttl = max(1, int(payload.get("ttl_seconds") or DEFAULT_LEASE_TTL_SECONDS))
        now_text = iso_timestamp(now_epoch)
        expires_epoch = now_epoch + ttl
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.conn.execute(
                    """
                    UPDATE leases
                    SET heartbeat_at = ?, expires_at = ?, expires_at_epoch = ?, updated_at = ?
                    WHERE lease_id = ? AND repo_id = ? AND spec_id = ? AND run_id = ? AND machine_id = ?
                        AND status = 'active' AND expires_at_epoch > ?
                    """,
                    (
                        now_text,
                        iso_timestamp(expires_epoch),
                        expires_epoch,
                        now_text,
                        lease_id,
                        owner["repo_id"],
                        owner["spec_id"],
                        owner["run_id"],
                        owner["machine_id"],
                        now_epoch,
                    ),
                )
                if result.rowcount == 0:
                    row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
                    if row is not None and row["status"] == "active" and float(row["expires_at_epoch"]) <= now_epoch:
                        self.conn.execute(
                            """
                            UPDATE leases
                            SET status = 'expired', updated_at = ?
                            WHERE lease_id = ? AND status = 'active'
                            """,
                            (now_text, lease_id),
                        )
                        self._record_event(lease_id, "expired", _row_to_dict(row), "expired lease heartbeat rejected")
                        self.conn.execute("COMMIT")
                    elif row is not None and row["status"] == "active":
                        self.conn.execute("ROLLBACK")
                        raise AuthError("lease owner mismatch", status=HTTPStatus.FORBIDDEN)
                    else:
                        self.conn.execute("ROLLBACK")
                    raise KeyError(str(lease_id))
                lease = self.get_lease(lease_id)
                self._upsert_machine(lease, now_epoch)
                self._record_event(lease_id, "heartbeat", lease, "lease heartbeat")
                self.conn.execute("COMMIT")
                return lease
            except Exception:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def release_lease(self, lease_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        owner_payload = payload or {}
        owner = self._lease_owner(owner_payload)
        now_text = iso_timestamp(self._now())
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.conn.execute(
                    """
                    UPDATE leases
                    SET status = 'released', released_at = ?, updated_at = ?
                    WHERE lease_id = ? AND repo_id = ? AND spec_id = ? AND run_id = ? AND machine_id = ?
                        AND status = 'active'
                    """,
                    (
                        now_text,
                        now_text,
                        lease_id,
                        owner["repo_id"],
                        owner["spec_id"],
                        owner["run_id"],
                        owner["machine_id"],
                    ),
                )
                if result.rowcount == 0:
                    row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
                    self.conn.execute("ROLLBACK")
                    if row is not None and row["status"] == "active":
                        raise AuthError("lease owner mismatch", status=HTTPStatus.FORBIDDEN)
                    raise KeyError(str(lease_id))
                lease = self.get_lease(lease_id)
                event_payload = owner_payload | lease
                self._record_event(lease_id, "released", event_payload, "lease released")
                self.conn.execute("COMMIT")
                return lease
            except Exception:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def list_leases(self, *, repo_id: str = "", spec_id: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if repo_id:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if spec_id:
            clauses.append("spec_id = ?")
            params.append(spec_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM leases {where} ORDER BY repo_id, spec_id, lease_id",
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_machines(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM machines ORDER BY machine_id").fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_events(self, *, repo_id: str = "", spec_id: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if repo_id:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if spec_id:
            clauses.append("spec_id = ?")
            params.append(spec_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM lease_events {where} ORDER BY event_id",
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]


class CoordinatorRequestHandler(BaseHTTPRequestHandler):
    server: "CoordinatorHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            token = self._require_auth()
            if parsed.path == "/v1/status" and method == "GET":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "api_version": API_VERSION,
                        "message": "ok",
                        "capabilities": list(CAPABILITIES),
                    },
                )
                return
            if parsed.path == "/v1/leases/acquire" and method == "POST":
                self._require_scope(token, "worker")
                status, payload = self.server.store.acquire_lease(self._read_json())
                self._send_json(status, payload)
                return
            if parsed.path == "/v1/leases" and method == "GET":
                self._require_scope(token, "operator")
                query = parse_qs(parsed.query)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "leases": self.server.store.list_leases(
                            repo_id=(query.get("repo_id") or [""])[0],
                            spec_id=(query.get("spec_id") or [""])[0],
                        ),
                        "machines": self.server.store.list_machines(),
                    },
                )
                return
            if parsed.path == "/v1/machines" and method == "GET":
                self._require_scope(token, "operator")
                self._send_json(HTTPStatus.OK, {"machines": self.server.store.list_machines()})
                return
            if parsed.path == "/v1/events" and method == "GET":
                self._require_scope(token, "operator")
                query = parse_qs(parsed.query)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "events": self.server.store.list_events(
                            repo_id=(query.get("repo_id") or [""])[0],
                            spec_id=(query.get("spec_id") or [""])[0],
                        )
                    },
                )
                return

            lease_id = self._lease_id_from_path(parsed.path)
            if lease_id is not None and method == "POST" and parsed.path.endswith("/heartbeat"):
                self._require_scope(token, "worker")
                self._send_json(HTTPStatus.OK, {"lease": self.server.store.heartbeat_lease(lease_id, self._read_json())})
                return
            if lease_id is not None and method == "POST" and parsed.path.endswith("/release"):
                self._require_scope(token, "worker")
                self._send_json(HTTPStatus.OK, {"lease": self.server.store.release_lease(lease_id, self._read_json())})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not-found", "message": "endpoint not found"})
        except AuthError as exc:
            self._send_json(exc.status, {"error": "auth-failed", "message": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad-request", "message": str(exc)})
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not-found", "message": "lease not found"})

    def _lease_id_from_path(self, path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "leases" and parts[3] in {"heartbeat", "release"}:
            try:
                return int(parts[2])
            except ValueError:
                return None
        return None

    def _require_auth(self) -> AuthenticatedToken:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthError("missing bearer token")
        raw_token = header[len("Bearer ") :].strip()
        if not raw_token:
            raise AuthError("missing bearer token")
        token = self.server.store.authenticate(raw_token, self.server.env_tokens)
        if token is None:
            raise AuthError("invalid bearer token")
        return token

    def _require_scope(self, token: AuthenticatedToken, required: str) -> None:
        if token.scope != required:
            raise AuthError(f"{required} scope required", status=HTTPStatus.FORBIDDEN)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, status: int | HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CoordinatorHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        store: CoordinatorStore,
        *,
        env_tokens: dict[str, str] | None = None,
        verbose: bool = False,
    ) -> None:
        super().__init__(server_address, CoordinatorRequestHandler)
        self.store = store
        self.env_tokens = env_tokens or {}
        self.verbose = verbose


def run_server(
    *,
    host: str,
    port: int,
    db_path: Path,
    worker_token: str = "",
    operator_token: str = "",
    verbose: bool = False,
) -> int:
    store = CoordinatorStore(db_path)
    env_tokens = {"worker": worker_token, "operator": operator_token}
    server = CoordinatorHTTPServer((host, port), store, env_tokens=env_tokens, verbose=verbose)
    bound_host, bound_port = server.server_address
    print(f"Coordinator serving on http://{bound_host}:{bound_port} with database {db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        store.close()
    return 0


def serve_from_args(args: argparse.Namespace) -> int:
    return run_server(
        host=args.host,
        port=args.port,
        db_path=Path(args.db).expanduser(),
        worker_token=args.worker_token or os.getenv("SPEC_COORDINATOR_WORKER_TOKEN", ""),
        operator_token=args.operator_token or os.getenv("SPEC_COORDINATOR_OPERATOR_TOKEN", ""),
        verbose=args.verbose,
    )


def token_create_from_args(args: argparse.Namespace) -> int:
    store = CoordinatorStore(Path(args.db).expanduser())
    try:
        token = store.create_token(name=args.name, scope=args.scope)
    finally:
        store.close()
    print(f"Created {args.scope} token {args.name}. Store this secret in a local env var or uncommitted file:")
    print(token)
    return 0


def token_revoke_from_args(args: argparse.Namespace) -> int:
    store = CoordinatorStore(Path(args.db).expanduser())
    try:
        revoked = store.revoke_token(name=args.name)
    finally:
        store.close()
    if revoked:
        print(f"Revoked token {args.name}.")
        return 0
    print(f"Token not found or already revoked: {args.name}", file=sys.stderr)
    return 1
