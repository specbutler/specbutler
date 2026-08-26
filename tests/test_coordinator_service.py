from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from spec_runtime.coordinator_service import (
    API_VERSION,
    CAPABILITIES,
    AuthError,
    CoordinatorHTTPServer,
    CoordinatorStore,
    hash_token,
)


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
        assert [row[0] for row in migrations] == [1]
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
        assert store.authenticate("worker-secret").scope == "worker"

        rotated = store.create_token(name="worker-main", scope="worker", token="new-secret")
        assert rotated == "new-secret"
        assert store.authenticate("worker-secret") is None
        assert store.authenticate("new-secret").scope == "worker"

        assert store.revoke_token(name="worker-main") is True
        assert store.authenticate("new-secret") is None
    finally:
        store.close()


def test_successful_acquire_heartbeat_release(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        status, body = store.acquire_lease(lease_payload())
        assert status == 201
        lease = body["lease"]
        assert lease["repo_id"] == "repo"
        assert lease["status"] == "active"

        clock.advance(10)
        renewed = store.heartbeat_lease(lease["lease_id"], lease_payload(ttl_seconds=120))
        assert renewed["status"] == "active"
        assert renewed["heartbeat_at"] != lease["heartbeat_at"]

        released = store.release_lease(lease["lease_id"], lease_payload())
        assert released["status"] == "released"
        events = [event["event_type"] for event in store.list_events(repo_id="repo", spec_id="spec-a")]
        assert events == ["acquired", "heartbeat", "released"]
    finally:
        store.close()


def test_idempotent_same_owner_acquire_for_resume_retry_flows(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        _, first = store.acquire_lease(lease_payload())
        clock.advance(5)
        status, second = store.acquire_lease(lease_payload(worktree_path="/tmp/new"))
        assert status == 200
        assert second["idempotent"] is True
        assert second["lease"]["lease_id"] == first["lease"]["lease_id"]
        assert second["lease"]["worktree_path"] == "/tmp/new"
    finally:
        store.close()


def test_conflicting_acquire_rejection(tmp_path: Path):
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=Clock())
    try:
        store.acquire_lease(lease_payload())
        status, body = store.acquire_lease(lease_payload(run_id="run-2", machine_id="machine-b"))
        assert status == 409
        assert body["error"] == "lease-conflict"
        assert body["lease"]["run_id"] == "run-1"
    finally:
        store.close()


def test_expired_lease_takeover_records_history(tmp_path: Path):
    clock = Clock()
    store = CoordinatorStore(tmp_path / "coord.sqlite", now=clock)
    try:
        store.acquire_lease(lease_payload(ttl_seconds=5))
        clock.advance(6)
        status, body = store.acquire_lease(lease_payload(run_id="run-2", machine_id="machine-b"))
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
        _, body = store.acquire_lease(lease_payload(ttl_seconds=5))
        lease_id = body["lease"]["lease_id"]
        clock.advance(6)

        try:
            store.heartbeat_lease(lease_id, lease_payload(ttl_seconds=60))
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
        _, body = store.acquire_lease(lease_payload())
        lease_id = body["lease"]["lease_id"]

        try:
            store.heartbeat_lease(lease_id, lease_payload(run_id="run-2", machine_id="machine-b"))
        except AuthError as exc:
            assert exc.status == 403
            assert str(exc) == "lease owner mismatch"
        else:
            raise AssertionError("cross-owner heartbeat should fail")

        assert store.get_lease(lease_id)["status"] == "active"

        try:
            store.release_lease(lease_id, lease_payload(run_id="run-2", machine_id="machine-b"))
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
