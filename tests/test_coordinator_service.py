from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spec_runtime.coordinator_service import (
    API_VERSION,
    CAPABILITIES,
    MAX_LEASE_TTL_SECONDS,
    MAX_REQUEST_BODY_BYTES,
    AuthError,
    CoordinatorHTTPServer,
    CoordinatorRequestHandler,
    CoordinatorServiceError,
    CoordinatorStore,
    RequestBodyError,
    _read_token_file,
    _secure_sqlite_file,
    connect_db,
    hash_token,
    serve_from_args,
)

WORKER_A_PRINCIPAL = "token:worker-a"
WORKER_B_PRINCIPAL = "token:worker-b"


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def lease_payload(**overrides):
    payload = {
        "repo_id": "repo",
        "spec_id": "spec-a",
        "run_id": "run-1",
        "machine_id": "machine-a",
        "hostname": "host-a",
        "display_name": "Host A",
        "agent": "codex",
        "ttl_seconds": 60,
        "worktree_path": "/tmp/worktree",
    }
    payload.update(overrides)
    return payload


def _body_reader(headers: dict[str, str], body: bytes) -> CoordinatorRequestHandler:
    handler = object.__new__(CoordinatorRequestHandler)
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    handler.connection = MagicMock()
    handler.connection.gettimeout.return_value = None
    return handler


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, b"", 411),
        ({"Content-Length": "not-an-integer"}, b"", 400),
        ({"Content-Length": "-1"}, b"", 400),
        ({"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}, b"", 413),
        ({"Content-Length": "10"}, b"{}", 400),
    ],
)
def test_coordinator_rejects_unbounded_or_incomplete_request_bodies(
    headers: dict[str, str],
    body: bytes,
    status: int,
) -> None:
    handler = _body_reader(headers, body)
    with pytest.raises(RequestBodyError) as failure:
        handler._read_json()
    assert failure.value.status == status


def test_coordinator_reads_only_a_bounded_complete_json_object() -> None:
    body = json.dumps({"key": "value"}).encode("utf-8")
    handler = _body_reader({"Content-Length": str(len(body))}, body)
    assert handler._read_json() == {"key": "value"}
    handler.connection.settimeout.assert_any_call(5.0)
    handler.connection.settimeout.assert_any_call(None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_connect_db_creates_private_files_under_umask_022(tmp_path: Path):
    shared_parent = tmp_path / "existing-shared"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)
    db_path = shared_parent / "private" / "nested" / "coord.sqlite"
    previous_umask = os.umask(0o022)
    try:
        conn = connect_db(db_path)
    finally:
        os.umask(previous_umask)

    try:
        assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755
        assert stat.S_IMODE((shared_parent / "private").stat().st_mode) == 0o700
        assert stat.S_IMODE((shared_parent / "private" / "nested").stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        sidecars = [Path(f"{db_path}{suffix}") for suffix in ("-wal", "-shm")]
        assert all(path.is_file() for path in sidecars)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sidecars)
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require Windows privileges")
def test_connect_db_rejects_symlink_database_target(tmp_path: Path):
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    link = tmp_path / "coord.sqlite"
    link.symlink_to(target)

    with pytest.raises(CoordinatorServiceError, match="link-shaped"):
        connect_db(link)


def test_connect_db_rejects_nonregular_database_target(tmp_path: Path):
    db_path = tmp_path / "coord.sqlite"
    db_path.mkdir()

    with pytest.raises(CoordinatorServiceError, match="non-regular"):
        connect_db(db_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_connect_db_tightens_existing_database_without_changing_parent(tmp_path: Path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    db_path = parent / "coord.sqlite"
    sqlite3.connect(db_path).close()
    db_path.chmod(0o644)

    conn = connect_db(db_path)
    try:
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_connect_db_rejects_group_writable_database_parent(tmp_path: Path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o770)

    with pytest.raises(CoordinatorServiceError, match="not group/world-writable"):
        connect_db(parent / "coord.sqlite")


def test_secure_sqlite_file_rejects_replacement_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "coord.sqlite"
    db_path.write_bytes(b"")
    real_open = os.open
    replaced = False

    def replacing_open(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal replaced
        if Path(path) == db_path and not replaced:
            replaced = True
            db_path.replace(tmp_path / "original.sqlite")
            db_path.write_bytes(b"")
            if os.name != "nt":
                db_path.chmod(0o600)
        return real_open(path, flags, mode)

    monkeypatch.setattr("spec_runtime.coordinator_service.os.open", replacing_open)

    with pytest.raises(CoordinatorServiceError, match="changed during secure open"):
        _secure_sqlite_file(db_path, create=False)


def _serve_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8765,
        "db": str(tmp_path / "coord.sqlite"),
        "worker_token": "",
        "worker_token_file": "",
        "operator_token": "",
        "operator_token_file": "",
        "verbose": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_serve_reads_tokens_from_private_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_file = tmp_path / "worker.token"
    operator_file = tmp_path / "operator.token"
    worker_file.write_text("worker-secret\n", encoding="utf-8")
    operator_file.write_text("operator-secret\n", encoding="utf-8")
    if os.name != "nt":
        worker_file.chmod(0o600)
        operator_file.chmod(0o600)
    captured: dict[str, object] = {}

    def fake_run_server(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "spec_runtime.coordinator_service.run_server",
        fake_run_server,
    )

    result = serve_from_args(
        _serve_args(
            tmp_path,
            worker_token_file=str(worker_file),
            operator_token_file=str(operator_file),
        )
    )

    assert result == 0
    assert captured["worker_token"] == "worker-secret"
    assert captured["operator_token"] == "operator-secret"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require Windows privileges")
def test_serve_rejects_symlink_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target.token"
    target.write_text("secret\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "worker.token"
    link.symlink_to(target)
    launch = MagicMock(return_value=0)
    monkeypatch.setattr("spec_runtime.coordinator_service.run_server", launch)

    result = serve_from_args(
        _serve_args(tmp_path, worker_token_file=str(link))
    )

    assert result == 1
    assert "link-shaped worker token file" in capsys.readouterr().err
    launch.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_serve_rejects_world_readable_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o644)
    launch = MagicMock(return_value=0)
    monkeypatch.setattr("spec_runtime.coordinator_service.run_server", launch)

    result = serve_from_args(
        _serve_args(tmp_path, worker_token_file=str(token_file))
    )

    assert result == 1
    assert "readable by group or other users" in capsys.readouterr().err
    launch.assert_not_called()


def test_token_file_replacement_during_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("original-secret\n", encoding="utf-8")
    if os.name != "nt":
        token_file.chmod(0o600)
    real_open = os.open
    replaced = False

    def replacing_open(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal replaced
        if Path(path) == token_file and not replaced:
            replaced = True
            token_file.replace(tmp_path / "original.token")
            token_file.write_text("replacement-secret\n", encoding="utf-8")
            if os.name != "nt":
                token_file.chmod(0o600)
        return real_open(path, flags, mode)

    monkeypatch.setattr("spec_runtime.coordinator_service.os.open", replacing_open)

    with pytest.raises(CoordinatorServiceError, match="changed while being opened"):
        _read_token_file(str(token_file), scope="worker")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
def test_token_file_permission_change_during_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("worker-secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    real_open = os.open
    changed = False

    def chmod_open(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal changed
        descriptor = real_open(path, flags, mode)
        if Path(path) == token_file and not changed:
            changed = True
            token_file.chmod(0o644)
        return descriptor

    monkeypatch.setattr("spec_runtime.coordinator_service.os.open", chmod_open)

    with pytest.raises(CoordinatorServiceError, match="permissions changed"):
        _read_token_file(str(token_file), scope="worker")


def test_serve_raw_token_warns_that_argv_option_is_deprecated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run_server(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "spec_runtime.coordinator_service.run_server",
        fake_run_server,
    )

    result = serve_from_args(
        _serve_args(tmp_path, worker_token="legacy-worker-secret")
    )

    assert result == 0
    assert captured["worker_token"] == "legacy-worker-secret"
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "process listings" in warning
    assert "deprecated" in warning


def test_coord_serve_help_recommends_token_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from spec_runtime import cli

    with pytest.raises(SystemExit, match="0"):
        cli.main(["coord", "serve", "--help"])

    help_text = capsys.readouterr().out
    assert "--worker-token-file" in help_text
    assert "--operator-token-file" in help_text
    assert "Deprecated: worker token in argv" in help_text


def test_schema_initialization_and_migration_idempotence(tmp_path: Path):
    db_path = tmp_path / "coord.sqlite"
    store = CoordinatorStore(db_path)
    store.close()
    store = CoordinatorStore(db_path)
    try:
        tables = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"machines", "leases", "lease_events", "tokens", "schema_migrations"} <= tables
        migrations = store.conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert [row[0] for row in migrations] == [1, 2]
    finally:
        store.close()


def test_v1_schema_migration_does_not_assign_legacy_active_lease(tmp_path: Path):
    db_path = tmp_path / "coord.sqlite"
    clock = Clock()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, '2023-01-01T00:00:00Z');

            CREATE TABLE leases (
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
            """
        )
        conn.execute(
            """
            INSERT INTO leases(
                repo_id, spec_id, run_id, machine_id, agent, status,
                heartbeat_at, expires_at, expires_at_epoch, worktree_path,
                created_at, updated_at, released_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "repo",
                "spec-a",
                "run-1",
                "machine-a",
                "codex",
                "2023-01-01T00:00:00Z",
                "2023-01-01T00:01:00Z",
                clock.value + 60,
                "/tmp/worktree",
                "2023-01-01T00:00:00Z",
                "2023-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    store = CoordinatorStore(db_path, now=clock)
    try:
        columns = {
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(leases)").fetchall()
        }
        assert "owner_principal" in columns
        migrations = store.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in migrations] == [1, 2]

        status, conflict = store.acquire_lease(
            lease_payload(),
            principal=WORKER_A_PRINCIPAL,
        )
        assert status == 409
        assert "lease" not in conflict

        with pytest.raises(AuthError, match="lease owner mismatch"):
            store.heartbeat_lease(
                1,
                lease_payload(),
                principal=WORKER_A_PRINCIPAL,
            )
        with pytest.raises(AuthError, match="lease owner mismatch"):
            store.release_lease(
                1,
                lease_payload(),
                principal=WORKER_A_PRINCIPAL,
            )

        clock.advance(61)
        status, acquired = store.acquire_lease(
            lease_payload(),
            principal=WORKER_A_PRINCIPAL,
        )
        assert status == 201
        assert acquired["lease"]["lease_id"] != 1
    finally:
        store.close()


def test_token_create_rotate_and_revoke_hashes_secret(tmp_path: Path):
    db_path = tmp_path / "coord.sqlite"
    store = CoordinatorStore(db_path)
    try:
        token = store.create_token(name="worker-main", scope="worker", token="worker-secret")
        assert token == "worker-secret"
        row = store.conn.execute("SELECT token_hash, revoked_at FROM tokens WHERE name = 'worker-main'").fetchone()
        assert row["token_hash"] == hash_token("worker-secret")
        assert "worker-secret" not in db_path.read_bytes().decode("latin1")
        authenticated = store.authenticate("worker-secret")
        assert authenticated is not None
        assert authenticated.scope == "worker"
        original_principal = authenticated.principal

        rotated = store.create_token(name="worker-main", scope="worker", token="new-secret")
        assert rotated == "new-secret"
        assert store.authenticate("worker-secret") is None
        rotated_auth = store.authenticate("new-secret")
        assert rotated_auth is not None
        assert rotated_auth.scope == "worker"
        assert rotated_auth.principal == original_principal

        assert store.revoke_token(name="worker-main") is True
        assert store.authenticate("new-secret") is None
    finally:
        store.close()


def test_environment_token_principal_is_stable_and_rotates_with_secret(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite")
    try:
        first = store.authenticate("env-secret", {"worker": "env-secret"})
        second = store.authenticate("env-secret", {"worker": "env-secret"})
        rotated = store.authenticate("new-secret", {"worker": "new-secret"})

        assert first is not None
        assert second is not None
        assert rotated is not None
        assert first.principal == second.principal
        assert first.principal != rotated.principal
        assert "env-secret" not in first.principal
    finally:
        store.close()


def test_successful_acquire_heartbeat_release(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        status, body = store.acquire_lease(lease_payload(), principal=WORKER_A_PRINCIPAL)
        assert status == 201
        lease = body["lease"]
        assert lease["repo_id"] == "repo"
        assert lease["status"] == "active"

        clock.advance(10)
        renewed = store.heartbeat_lease(
            lease["lease_id"],
            lease_payload(ttl_seconds=120),
            principal=WORKER_A_PRINCIPAL,
        )
        assert renewed["status"] == "active"
        assert renewed["heartbeat_at"] != lease["heartbeat_at"]

        released = store.release_lease(
            lease["lease_id"],
            lease_payload(),
            principal=WORKER_A_PRINCIPAL,
        )
        assert released["status"] == "released"
        events = [event["event_type"] for event in store.list_events(repo_id="repo", spec_id="spec-a")]
        assert events == ["acquired", "heartbeat", "released"]
    finally:
        store.close()


def test_lease_ttl_is_bounded_for_acquire_and_heartbeat(tmp_path: Path) -> None:
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        status, acquired = store.acquire_lease(
            lease_payload(ttl_seconds=MAX_LEASE_TTL_SECONDS),
            principal=WORKER_A_PRINCIPAL,
        )
        assert status == 201
        lease_id = acquired["lease"]["lease_id"]
        assert acquired["lease"]["expires_at_epoch"] == (
            clock.value + MAX_LEASE_TTL_SECONDS
        )
        renewed = store.heartbeat_lease(
            lease_id,
            lease_payload(ttl_seconds=MAX_LEASE_TTL_SECONDS),
            principal=WORKER_A_PRINCIPAL,
        )
        assert renewed["expires_at_epoch"] == clock.value + MAX_LEASE_TTL_SECONDS

        for invalid in (
            0,
            -1,
            MAX_LEASE_TTL_SECONDS + 1,
            10**1000,
            True,
            1.5,
            "inf",
        ):
            with pytest.raises(ValueError, match="ttl_seconds"):
                store.heartbeat_lease(
                    lease_id,
                    lease_payload(ttl_seconds=invalid),
                    principal=WORKER_A_PRINCIPAL,
                )
            with pytest.raises(ValueError, match="ttl_seconds"):
                store.acquire_lease(
                    lease_payload(
                        spec_id=f"invalid-{str(invalid)[:20]}",
                        ttl_seconds=invalid,
                    ),
                    principal=WORKER_A_PRINCIPAL,
                )
    finally:
        store.close()


def test_idempotent_same_owner_acquire_for_resume_retry_flows(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        _, first = store.acquire_lease(lease_payload(), principal=WORKER_A_PRINCIPAL)
        clock.advance(5)
        status, second = store.acquire_lease(
            lease_payload(worktree_path="/tmp/new"),
            principal=WORKER_A_PRINCIPAL,
        )
        assert status == 200
        assert second["idempotent"] is True
        assert second["lease"]["lease_id"] == first["lease"]["lease_id"]
        assert second["lease"]["worktree_path"] == "/tmp/new"
    finally:
        store.close()


def test_conflicting_acquire_rejection(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=Clock())
    try:
        store.acquire_lease(lease_payload(), principal=WORKER_A_PRINCIPAL)
        status, body = store.acquire_lease(
            lease_payload(run_id="run-2", machine_id="machine-b"),
            principal=WORKER_B_PRINCIPAL,
        )
        assert status == 409
        assert body["error"] == "lease-conflict"
        assert "lease" not in body
    finally:
        store.close()


def test_expired_lease_takeover_records_history(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        store.acquire_lease(
            lease_payload(ttl_seconds=5),
            principal=WORKER_A_PRINCIPAL,
        )
        clock.advance(6)
        status, body = store.acquire_lease(
            lease_payload(run_id="run-2", machine_id="machine-b"),
            principal=WORKER_B_PRINCIPAL,
        )
        assert status == 201
        assert body["lease"]["run_id"] == "run-2"
        leases = store.list_leases(repo_id="repo", spec_id="spec-a")
        assert [lease["status"] for lease in leases] == ["expired", "active"]
        assert [event["event_type"] for event in store.list_events(repo_id="repo", spec_id="spec-a")] == [
            "acquired",
            "expired",
            "acquired",
        ]
    finally:
        store.close()


def test_expired_lease_cannot_be_renewed_by_heartbeat(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        _, body = store.acquire_lease(
            lease_payload(ttl_seconds=5),
            principal=WORKER_A_PRINCIPAL,
        )
        lease_id = body["lease"]["lease_id"]
        clock.advance(6)

        try:
            store.heartbeat_lease(
                lease_id,
                lease_payload(ttl_seconds=60),
                principal=WORKER_A_PRINCIPAL,
            )
        except KeyError:
            pass
        else:
            raise AssertionError("expired lease heartbeat should fail")

        lease = store.get_lease(lease_id)
        assert lease["status"] == "expired"
        assert [event["event_type"] for event in store.list_events(repo_id="repo", spec_id="spec-a")] == [
            "acquired",
            "expired",
        ]
    finally:
        store.close()


def test_lease_mutations_require_matching_owner_identity(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=Clock())
    try:
        _, body = store.acquire_lease(lease_payload(), principal=WORKER_A_PRINCIPAL)
        lease_id = body["lease"]["lease_id"]

        try:
            store.heartbeat_lease(
                lease_id,
                lease_payload(run_id="run-2", machine_id="machine-b"),
                principal=WORKER_A_PRINCIPAL,
            )
        except AuthError as exc:
            assert exc.status == 403
            assert str(exc) == "lease owner mismatch"
        else:
            raise AssertionError("cross-owner heartbeat should fail")

        assert store.get_lease(lease_id)["status"] == "active"

        try:
            store.release_lease(
                lease_id,
                lease_payload(run_id="run-2", machine_id="machine-b"),
                principal=WORKER_A_PRINCIPAL,
            )
        except AuthError as exc:
            assert exc.status == 403
            assert str(exc) == "lease owner mismatch"
        else:
            raise AssertionError("cross-owner release should fail")

        lease = store.get_lease(lease_id)
        assert lease["status"] == "active"
        assert lease["run_id"] == "run-1"
        assert [event["event_type"] for event in store.list_events(repo_id="repo", spec_id="spec-a")] == ["acquired"]
    finally:
        store.close()


def _request(port: int, method: str, path: str, token: str = "", body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    raw_body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        raw_body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, raw_body, headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def test_http_api_health_auth_failures_and_inspection(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite")
    store.create_token(name="worker", scope="worker", token="worker-token")
    store.create_token(name="operator", scope="operator", token="operator-token")
    server = CoordinatorHTTPServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, body = _request(port, "GET", "/v1/status")
        assert status == 401
        assert body["error"] == "auth-failed"

        status, body = _request(port, "GET", "/v1/status", token="worker-token")
        assert status == 200
        assert body["api_version"] == API_VERSION
        assert set(CAPABILITIES) <= set(body["capabilities"])

        status, body = _request(port, "GET", "/v1/leases", token="worker-token")
        assert status == 403

        status, first = _request(port, "POST", "/v1/leases/acquire", token="worker-token", body=lease_payload())
        assert status == 201
        status, body = _request(
            port,
            "POST",
            "/v1/leases/acquire",
            token="operator-token",
            body=lease_payload(repo_id="repo", spec_id="spec-b", run_id="run-2", machine_id="machine-b"),
        )
        assert status == 403
        assert body["message"] == "worker scope required"

        status, body = _request(
            port,
            "POST",
            "/v1/leases/acquire",
            token="worker-token",
            body=lease_payload(repo_id="repo", spec_id="spec-b", run_id="run-2", machine_id="machine-b"),
        )
        assert status == 201
        lease_id = first["lease"]["lease_id"]

        status, body = _request(port, "POST", f"/v1/leases/{lease_id}/heartbeat", token="operator-token", body={})
        assert status == 403
        assert body["message"] == "worker scope required"

        status, body = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/heartbeat",
            token="worker-token",
            body=lease_payload(run_id="run-2", machine_id="machine-b"),
        )
        assert status == 403
        assert body["message"] == "lease owner mismatch"

        status, body = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/heartbeat",
            token="worker-token",
            body=lease_payload(),
        )
        assert status == 200
        assert body["lease"]["lease_id"] == lease_id

        status, body = _request(port, "GET", "/v1/leases?repo_id=repo", token="operator-token")
        assert status == 200
        assert len(body["leases"]) == 2
        assert {machine["machine_id"] for machine in body["machines"]} == {"machine-a", "machine-b"}

        status, body = _request(port, "POST", f"/v1/leases/{lease_id}/release", token="operator-token", body={})
        assert status == 403
        assert body["message"] == "worker scope required"

        status, body = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/release",
            token="worker-token",
            body=lease_payload(run_id="run-2", machine_id="machine-b"),
        )
        assert status == 403
        assert body["message"] == "lease owner mismatch"

        status, body = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/release",
            token="worker-token",
            body=lease_payload(),
        )
        assert status == 200
        assert body["lease"]["status"] == "released"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def test_http_worker_cannot_spoof_another_tokens_lease_owner(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite")
    store.create_token(name="worker-a", scope="worker", token="worker-a-token")
    store.create_token(name="worker-b", scope="worker", token="worker-b-token")
    store.create_token(name="operator", scope="operator", token="operator-token")
    server = CoordinatorHTTPServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, acquired = _request(
            port,
            "POST",
            "/v1/leases/acquire",
            token="worker-a-token",
            body=lease_payload(),
        )
        assert status == 201
        lease_id = acquired["lease"]["lease_id"]
        assert "owner_principal" not in acquired["lease"]

        # Matching the user-controlled owner fields is insufficient: the
        # authenticated credential that acquired the lease must also match.
        status, conflict = _request(
            port,
            "POST",
            "/v1/leases/acquire",
            token="worker-b-token",
            body=lease_payload(),
        )
        assert status == 409
        assert conflict == {
            "error": "lease-conflict",
            "message": "non-expired lease already exists for repo/spec",
        }

        status, heartbeat = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/heartbeat",
            token="worker-b-token",
            body=lease_payload(),
        )
        assert status == 403
        assert heartbeat["message"] == "lease owner mismatch"

        status, release = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/release",
            token="worker-b-token",
            body=lease_payload(),
        )
        assert status == 403
        assert release["message"] == "lease owner mismatch"

        # Operators retain inspection access, but never receive the internal
        # principal binding.
        status, inspection = _request(
            port,
            "GET",
            "/v1/leases?repo_id=repo&spec_id=spec-a",
            token="operator-token",
        )
        assert status == 200
        assert len(inspection["leases"]) == 1
        assert inspection["leases"][0]["status"] == "active"
        assert "owner_principal" not in inspection["leases"][0]

        # Worker A retains idempotent resume, heartbeat, and release behavior.
        status, reacquired = _request(
            port,
            "POST",
            "/v1/leases/acquire",
            token="worker-a-token",
            body=lease_payload(),
        )
        assert status == 200
        assert reacquired["idempotent"] is True
        assert reacquired["lease"]["lease_id"] == lease_id

        status, _ = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/heartbeat",
            token="worker-a-token",
            body=lease_payload(),
        )
        assert status == 200
        status, released = _request(
            port,
            "POST",
            f"/v1/leases/{lease_id}/release",
            token="worker-a-token",
            body=lease_payload(),
        )
        assert status == 200
        assert released["lease"]["status"] == "released"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def test_http_concurrent_acquire_requests_are_serialized(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite")
    store.create_token(name="worker", scope="worker", token="worker-token")
    server = CoordinatorHTTPServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]

        def acquire(index: int):
            return _request(
                port,
                "POST",
                "/v1/leases/acquire",
                token="worker-token",
                body=lease_payload(run_id=f"run-{index}", machine_id=f"machine-{index}"),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(acquire, range(20)))

        statuses = [status for status, _ in results]
        assert statuses.count(201) == 1
        assert statuses.count(409) == 19
        assert len(store.list_leases(repo_id="repo", spec_id="spec-a")) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()
