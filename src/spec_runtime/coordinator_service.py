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
import socket
import sqlite3
import stat
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
MAX_LEASE_TTL_SECONDS = 3600
MAX_TOKEN_FILE_BYTES = 16 * 1024
MAX_REQUEST_BODY_BYTES = 64 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 5.0


class CoordinatorServiceError(Exception):
    """Base class for coordinator service failures."""


class AuthError(CoordinatorServiceError):
    """Raised when a request is missing or has invalid credentials."""

    def __init__(self, message: str, *, status: HTTPStatus = HTTPStatus.UNAUTHORIZED) -> None:
        super().__init__(message)
        self.status = status


class RequestBodyError(CoordinatorServiceError):
    """Raised when an HTTP request body cannot be consumed safely."""

    def __init__(self, message: str, *, status: HTTPStatus) -> None:
        super().__init__(message)
        self.status = status


LEASE_OWNER_FIELDS = ("repo_id", "spec_id", "run_id", "machine_id")


@dataclass(frozen=True)
class AuthenticatedToken:
    name: str
    scope: str
    principal: str


def utc_now() -> float:
    return time.time()


def iso_timestamp(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(utc_now() if epoch is None else epoch, UTC).isoformat().replace("+00:00", "Z")


def _lease_ttl_seconds(payload: dict[str, Any]) -> int:
    raw = payload.get("ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
    if raw is None or raw == "":
        raw = DEFAULT_LEASE_TTL_SECONDS
    if isinstance(raw, bool):
        raise ValueError("ttl_seconds must be an integer")
    try:
        ttl = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("ttl_seconds must be an integer") from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError("ttl_seconds must be an integer")
    if ttl < 1 or ttl > MAX_LEASE_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be between 1 and {MAX_LEASE_TTL_SECONDS}"
        )
    return ttl


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "spec_" + secrets.token_urlsafe(32)


_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _is_reparse_point(metadata: os.stat_result) -> bool:
    """Return whether Windows marked a filesystem entry as a reparse point."""
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(reparse_flag and attributes & reparse_flag)


def _ensure_private_db_parent(parent: Path) -> None:
    """Create only missing DB parents with private permissions.

    Existing ancestors belong to the operator and may intentionally be shared;
    never chmod them as a side effect of opening one database. The database
    file itself remains private even under such a parent.
    """
    missing: list[Path] = []
    cursor = parent
    while True:
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            missing.append(cursor)
            next_cursor = cursor.parent
            if next_cursor == cursor:
                raise CoordinatorServiceError(
                    f"Could not find an existing parent for coordinator database: {parent}"
                )
            cursor = next_cursor
            continue
        except OSError as exc:
            raise CoordinatorServiceError(
                f"Could not inspect coordinator database parent {cursor}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise CoordinatorServiceError(
                f"Refusing link-shaped coordinator database parent: {cursor}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise CoordinatorServiceError(
                f"Coordinator database parent is not a directory: {cursor}"
            )
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError as exc:
            # A path appearing between inspection and creation is not ours to
            # chmod or trust. Fail closed and let the operator inspect it.
            raise CoordinatorServiceError(
                f"Coordinator database parent appeared during secure creation: {directory}"
            ) from exc
        except OSError as exc:
            raise CoordinatorServiceError(
                f"Could not create coordinator database parent {directory}: {exc}"
            ) from exc
        if os.name != "nt":
            os.chmod(directory, 0o700)

    try:
        final_parent = os.lstat(parent)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not validate coordinator database parent {parent}: {exc}"
        ) from exc
    if os.name != "nt" and (
        final_parent.st_uid != os.geteuid()
        or stat.S_IMODE(final_parent.st_mode) & 0o022
    ):
        raise CoordinatorServiceError(
            "Coordinator database parent must be owned by the current user "
            f"and not group/world-writable: {parent}"
        )


def _secure_sqlite_file(path: Path, *, create: bool) -> bool:
    """Validate and tighten one SQLite file without following links."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not inspect coordinator database file {path}: {exc}"
        ) from exc

    if before is None and not create:
        return False
    if before is not None and (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse_point(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise CoordinatorServiceError(
            f"Refusing non-regular or link-shaped coordinator database file: {path}"
        )

    flags = os.O_RDWR
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if not create:
            return False
        raise
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not securely open coordinator database file {path}: {exc}"
        ) from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise CoordinatorServiceError(
                f"Refusing non-regular or multiply-linked coordinator database file: {path}"
            )
        if before is not None and (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise CoordinatorServiceError(
                f"Coordinator database file changed during secure open: {path}"
            )
        if os.name != "nt":
            if opened.st_uid != os.geteuid():
                raise CoordinatorServiceError(
                    f"Refusing coordinator database file owned by another user: {path}"
                )
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)

    try:
        after = os.lstat(path)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Coordinator database file changed during secure open: {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or _is_reparse_point(after)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or (
            os.name != "nt"
            and (
                after.st_uid != os.geteuid()
                or stat.S_IMODE(after.st_mode) & 0o077
            )
        )
    ):
        raise CoordinatorServiceError(
            f"Coordinator database file changed during secure open: {path}"
        )
    return True


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path).expanduser()
    _ensure_private_db_parent(db_path.parent)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        _secure_sqlite_file(Path(f"{db_path}{suffix}"), create=False)
    _secure_sqlite_file(db_path, create=True)

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        init_db(conn)
        # SQLite's Unix VFS derives sidecar permissions from the main database
        # mode, but validate and tighten files that exist now as defense in
        # depth. WAL/SHM files recreated later inherit the 0600 main-file mode.
        _secure_sqlite_file(db_path, create=False)
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            _secure_sqlite_file(Path(f"{db_path}{suffix}"), create=False)
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        raise


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
            owner_principal TEXT NOT NULL,
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, iso_timestamp()),
        )
        lease_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(leases)").fetchall()
        }
        if "owner_principal" not in lease_columns:
            # Existing active leases cannot safely be assigned to a credential:
            # the v1 schema did not retain enough information to identify it.
            # Keep the column NULL so those leases remain effective until their
            # normal expiry, but cannot be renewed, released, or idempotently
            # reacquired by an arbitrary worker token.
            conn.execute("ALTER TABLE leases ADD COLUMN owner_principal TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, iso_timestamp()),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _lease_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize a lease without exposing its credential-bound principal."""
    return {key: row[key] for key in row.keys() if key != "owner_principal"}


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
                SELECT token_id, name, scope FROM tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        if row:
            return AuthenticatedToken(
                name=str(row["name"]),
                scope=str(row["scope"]),
                principal=f"token:{int(row['token_id'])}",
            )
        for scope, configured in (env_tokens or {}).items():
            if configured and secrets.compare_digest(token, configured):
                # Environment-provided credentials have no database row. A
                # hash-derived identity remains stable across process restarts
                # while ensuring rotation cannot inherit the old token's
                # active leases. This value is never returned by the API.
                return AuthenticatedToken(
                    name=f"env:{scope}",
                    scope=scope,
                    principal=f"env:{scope}:{token_hash}",
                )
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

    def acquire_lease(self, payload: dict[str, Any], *, principal: str) -> tuple[int, dict[str, Any]]:
        required = ("repo_id", "spec_id", "run_id", "machine_id", "agent")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        if not principal:
            raise ValueError("authenticated principal is required")

        now_epoch = self._now()
        ttl = _lease_ttl_seconds(payload)
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
                        and row["owner_principal"] == principal
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
                        },
                    )

                cursor = self.conn.execute(
                    """
                    INSERT INTO leases(
                        repo_id, spec_id, run_id, machine_id, owner_principal, agent, status,
                        heartbeat_at, expires_at, expires_at_epoch, worktree_path,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["repo_id"],
                        payload["spec_id"],
                        payload["run_id"],
                        payload["machine_id"],
                        principal,
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
        return _lease_row_to_dict(row)

    def _lease_owner(self, payload: dict[str, Any]) -> dict[str, str]:
        missing = [name for name in LEASE_OWNER_FIELDS if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        return {name: str(payload[name]).strip() for name in LEASE_OWNER_FIELDS}

    def heartbeat_lease(self, lease_id: int, payload: dict[str, Any], *, principal: str) -> dict[str, Any]:
        owner = self._lease_owner(payload)
        if not principal:
            raise ValueError("authenticated principal is required")
        now_epoch = self._now()
        ttl = _lease_ttl_seconds(payload)
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
                        AND owner_principal = ?
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
                        principal,
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

    def release_lease(
        self,
        lease_id: int,
        payload: dict[str, Any] | None = None,
        *,
        principal: str,
    ) -> dict[str, Any]:
        owner_payload = payload or {}
        owner = self._lease_owner(owner_payload)
        if not principal:
            raise ValueError("authenticated principal is required")
        now_text = iso_timestamp(self._now())
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.conn.execute(
                    """
                    UPDATE leases
                    SET status = 'released', released_at = ?, updated_at = ?
                    WHERE lease_id = ? AND repo_id = ? AND spec_id = ? AND run_id = ? AND machine_id = ?
                        AND owner_principal = ?
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
                        principal,
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
        return [_lease_row_to_dict(row) for row in rows]

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
                status, payload = self.server.store.acquire_lease(
                    self._read_json(),
                    principal=token.principal,
                )
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
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "lease": self.server.store.heartbeat_lease(
                            lease_id,
                            self._read_json(),
                            principal=token.principal,
                        )
                    },
                )
                return
            if lease_id is not None and method == "POST" and parsed.path.endswith("/release"):
                self._require_scope(token, "worker")
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "lease": self.server.store.release_lease(
                            lease_id,
                            self._read_json(),
                            principal=token.principal,
                        )
                    },
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not-found", "message": "endpoint not found"})
        except AuthError as exc:
            self._send_json(exc.status, {"error": "auth-failed", "message": str(exc)})
        except RequestBodyError as exc:
            # Oversize/incomplete bodies leave unread bytes on this HTTP/1.1
            # connection. Never let a subsequent request be parsed from them.
            self.close_connection = True
            self._send_json(exc.status, {"error": "bad-request", "message": str(exc)})
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
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestBodyError(
                "Content-Length is required",
                status=HTTPStatus.LENGTH_REQUIRED,
            )
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise RequestBodyError(
                "Content-Length must be a non-negative integer",
                status=HTTPStatus.BAD_REQUEST,
            ) from exc
        if length < 0:
            raise RequestBodyError(
                "Content-Length must be a non-negative integer",
                status=HTTPStatus.BAD_REQUEST,
            )
        if length > MAX_REQUEST_BODY_BYTES:
            raise RequestBodyError(
                f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        if length == 0:
            return {}
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(REQUEST_BODY_TIMEOUT_SECONDS)
            body = self.rfile.read(length)
        except (TimeoutError, socket.timeout) as exc:
            raise RequestBodyError(
                "request body timed out",
                status=HTTPStatus.REQUEST_TIMEOUT,
            ) from exc
        finally:
            self.connection.settimeout(previous_timeout)
        if len(body) != length:
            raise RequestBodyError(
                "request body ended before Content-Length bytes arrived",
                status=HTTPStatus.BAD_REQUEST,
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise RequestBodyError(
                "request body is not valid UTF-8",
                status=HTTPStatus.BAD_REQUEST,
            ) from exc
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


def _read_token_file(path_value: str, *, scope: str) -> str:
    """Read one private bearer-token file without following links."""
    path = Path(path_value).expanduser()
    try:
        parent = os.lstat(path.parent)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not inspect {scope} token directory {path.parent}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(parent.st_mode)
        or _is_reparse_point(parent)
        or not stat.S_ISDIR(parent.st_mode)
    ):
        raise CoordinatorServiceError(
            f"Refusing unsafe {scope} token directory: {path.parent}"
        )
    if os.name != "nt" and (
        parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise CoordinatorServiceError(
            f"Refusing group/world-writable or foreign-owned {scope} token directory: "
            f"{path.parent}"
        )
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not inspect {scope} token file {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse_point(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise CoordinatorServiceError(
            f"Refusing non-regular or link-shaped {scope} token file: {path}"
        )
    if before.st_nlink != 1:
        raise CoordinatorServiceError(
            f"Refusing multiply-linked {scope} token file: {path}"
        )
    if os.name != "nt":
        if before.st_uid != os.geteuid():
            raise CoordinatorServiceError(
                f"Refusing {scope} token file owned by another user: {path}"
            )
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise CoordinatorServiceError(
                f"Refusing {scope} token file readable by group or other users: "
                f"{path}; run `chmod 600 {path}`"
            )

    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"Could not securely open {scope} token file {path}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise CoordinatorServiceError(
                f"Refusing non-regular or multiply-linked {scope} token file: {path}"
            )
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CoordinatorServiceError(
                f"{scope.capitalize()} token file changed while being opened: {path}"
            )
        if os.name != "nt" and (
            opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise CoordinatorServiceError(
                f"{scope.capitalize()} token permissions changed while opening: {path}"
            )
        if opened.st_size > MAX_TOKEN_FILE_BYTES:
            raise CoordinatorServiceError(
                f"{scope.capitalize()} token file is unexpectedly large: {path}"
            )
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise CoordinatorServiceError(
            f"{scope.capitalize()} token file changed while being read: {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or _is_reparse_point(after)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or (
            os.name != "nt"
            and (
                after.st_uid != os.geteuid()
                or stat.S_IMODE(after.st_mode) & 0o077
            )
        )
    ):
        raise CoordinatorServiceError(
            f"{scope.capitalize()} token file changed while being read: {path}"
        )
    if len(payload) > MAX_TOKEN_FILE_BYTES:
        raise CoordinatorServiceError(
            f"{scope.capitalize()} token file is unexpectedly large: {path}"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoordinatorServiceError(
            f"{scope.capitalize()} token file is not valid UTF-8: {path}"
        ) from exc
    token = text.rstrip("\r\n")
    if not token or "\n" in token or "\r" in token or token != token.strip():
        raise CoordinatorServiceError(
            f"{scope.capitalize()} token file must contain exactly one non-empty token: {path}"
        )
    return token


def _serve_token_from_args(args: argparse.Namespace, *, scope: str) -> str:
    raw_value = str(getattr(args, f"{scope}_token", "") or "")
    file_value = str(getattr(args, f"{scope}_token_file", "") or "").strip()
    if raw_value and file_value:
        raise CoordinatorServiceError(
            f"Use only one of --{scope}-token and --{scope}-token-file."
        )
    if raw_value:
        print(
            f"WARNING: --{scope}-token exposes a bearer token in process listings "
            f"and is deprecated; use --{scope}-token-file or "
            f"SPEC_COORDINATOR_{scope.upper()}_TOKEN instead.",
            file=sys.stderr,
        )
        return raw_value
    if file_value:
        return _read_token_file(file_value, scope=scope)
    return os.getenv(f"SPEC_COORDINATOR_{scope.upper()}_TOKEN", "")


def serve_from_args(args: argparse.Namespace) -> int:
    try:
        worker_token = _serve_token_from_args(args, scope="worker")
        operator_token = _serve_token_from_args(args, scope="operator")
    except CoordinatorServiceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return run_server(
        host=args.host,
        port=args.port,
        db_path=Path(args.db).expanduser(),
        worker_token=worker_token,
        operator_token=operator_token,
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
