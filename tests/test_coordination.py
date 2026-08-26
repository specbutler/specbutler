"""Tests for coordination configuration parsing, overrides, env precedence,
the no-op coordinator client, and diagnostic output redaction.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime.config import (
    CoordinationConfig,
    _coordination_from_env,
    _redact_url_credentials,
    load_spec_runtime_config,
)
from spec_runtime.coordination import (
    SUPPORTED_API_VERSIONS,
    CoordinatorAuthError,
    CoordinatorDisabledError,
    CoordinatorLeaseConflictError,
    CoordinatorMalformedResponseError,
    CoordinatorStatus,
    CoordinatorUnavailableError,
    CoordinatorUnsupportedProtocolError,
    CoordinatorUnsupportedVersionError,
    HttpCoordinatorClient,
    NoOpCoordinatorClient,
    build_client,
)


def _clear_coord_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in (
        "SPEC_COORDINATOR_URL",
        "SPEC_COORDINATOR_TOKEN",
        "SPEC_MACHINE_ID",
        "SPEC_COORDINATOR_REPO_ID",
        "SPEC_COORDINATOR_BACKEND",
    ):
        monkeypatch.delenv(env_var, raising=False)

# ---------------------------------------------------------------------------
# .spec.toml parsing
# ---------------------------------------------------------------------------


class TestSpecTomlCoordinationParsing:
    def test_no_section_means_disabled(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')

        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.enabled is False
        assert config.coordination.url == ""
        assert config.coordination.token == ""
        # machine_id always falls back to a stable hostname-derived value
        assert config.coordination.machine_id

    def test_full_section_parses(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
base_ref = "origin/main"

[coordination]
backend = "http"
url = "https://coord.example/"
repo_id = "myrepo"
machine_id = "alpha"
"""
        )
        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.enabled is True
        assert config.coordination.backend == "http"
        assert config.coordination.url == "https://coord.example/"
        assert config.coordination.repo_id == "myrepo"
        assert config.coordination.machine_id == "alpha"
        assert config.coordination.token == ""

    def test_token_not_required_in_committed_toml(self, tmp_path):
        """Tokens may be omitted from .spec.toml — the value comes from local
        files or env. The config still parses cleanly with no token field."""
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
repo_id = "myrepo"
"""
        )
        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.token == ""
        assert config.coordination.enabled is True


# ---------------------------------------------------------------------------
# .spec.local.toml override
# ---------------------------------------------------------------------------


class TestLocalTomlOverride:
    def test_local_token_overrides_committed(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
repo_id = "myrepo"
"""
        )
        local_toml = tmp_path / ".spec.local.toml"
        local_toml.write_text(
            """
[coordination]
token = "secret-from-local"
"""
        )

        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.token == "secret-from-local"
        # url still comes from the committed file
        assert config.coordination.url == "https://coord.example"

    def test_local_can_provide_url_when_committed_absent(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        (tmp_path / ".spec.local.toml").write_text(
            """
[coordination]
url = "https://local-only.example"
token = "local-token"
machine_id = "host-from-local"
"""
        )
        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.url == "https://local-only.example"
        assert config.coordination.token == "local-token"
        assert config.coordination.machine_id == "host-from-local"

    def test_malformed_local_toml_does_not_break_load(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
"""
        )
        (tmp_path / ".spec.local.toml").write_text("not valid = toml [[[")
        # Should not raise — we just ignore malformed local file.
        config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.url == "https://coord.example"


# ---------------------------------------------------------------------------
# Environment variable precedence
# ---------------------------------------------------------------------------


class TestEnvPrecedence:
    def test_env_overrides_local_and_committed(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://committed.example"
repo_id = "committed-repo"
machine_id = "committed-machine"
"""
        )
        (tmp_path / ".spec.local.toml").write_text(
            """
[coordination]
url = "https://local.example"
token = "local-token"
"""
        )
        env = {
            "SPEC_COORDINATOR_URL": "https://env.example",
            "SPEC_COORDINATOR_TOKEN": "env-token",
            "SPEC_MACHINE_ID": "env-machine",
            "SPEC_COORDINATOR_REPO_ID": "env-repo",
        }
        config = load_spec_runtime_config(config_path=spec_toml, env=env)
        assert config.coordination.url == "https://env.example"
        assert config.coordination.token == "env-token"
        assert config.coordination.machine_id == "env-machine"
        assert config.coordination.repo_id == "env-repo"

    def test_env_partial_override_layers_on_file_values(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://committed.example"
repo_id = "committed-repo"
"""
        )
        env = {"SPEC_COORDINATOR_TOKEN": "env-token"}
        config = load_spec_runtime_config(config_path=spec_toml, env=env)
        assert config.coordination.url == "https://committed.example"
        assert config.coordination.repo_id == "committed-repo"
        assert config.coordination.token == "env-token"

    def test_no_config_file_with_env_only(self, tmp_path):
        env = {
            "SPEC_COORDINATOR_URL": "https://env.example",
            "SPEC_COORDINATOR_TOKEN": "env-token",
            "SPEC_COORDINATOR_REPO_ID": "env-repo",
            "SPEC_MACHINE_ID": "env-machine",
        }
        # require=False so we don't error on missing file
        config = load_spec_runtime_config(
            require=False,
            config_path=tmp_path / ".spec.toml",
            env=env,
        )
        assert config.coordination.url == "https://env.example"
        assert config.coordination.token == "env-token"

    def test_machine_id_defaults_to_hostname(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
"""
        )
        with patch("spec_runtime.config.socket.gethostname", return_value="my-host"):
            config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.machine_id == "my-host"

    def test_explicit_machine_id_beats_hostname_default(self, tmp_path):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
machine_id = "explicit-id"
"""
        )
        with patch("spec_runtime.config.socket.gethostname", return_value="my-host"):
            config = load_spec_runtime_config(config_path=spec_toml, env={})
        assert config.coordination.machine_id == "explicit-id"

    def test_coordination_from_env_helper(self):
        env = {
            "SPEC_COORDINATOR_URL": "https://x.example",
            "SPEC_COORDINATOR_TOKEN": "tok",
            "SPEC_MACHINE_ID": "m",
            "SPEC_COORDINATOR_REPO_ID": "r",
            "SPEC_COORDINATOR_BACKEND": "http",
        }
        coord = _coordination_from_env(env)
        assert coord.url == "https://x.example"
        assert coord.token == "tok"
        assert coord.machine_id == "m"
        assert coord.repo_id == "r"
        assert coord.backend == "http"


# ---------------------------------------------------------------------------
# Disabled / no-op client
# ---------------------------------------------------------------------------


class TestNoOpClient:
    def test_build_client_returns_noop_when_disabled(self):
        client = build_client(CoordinationConfig())
        assert isinstance(client, NoOpCoordinatorClient)

    def test_status_reports_disabled(self):
        client = NoOpCoordinatorClient(CoordinationConfig())
        status = client.status()
        assert status.enabled is False
        assert status.ok is True
        assert "disabled" in status.message

    def test_require_enabled_raises(self):
        client = NoOpCoordinatorClient(CoordinationConfig())
        with pytest.raises(CoordinatorDisabledError):
            client.require_enabled()


class TestUnsupportedBackend:
    def test_unknown_backend_raises_protocol_error(self):
        config = CoordinationConfig(
            backend="grpc",
            url="https://coord.example",
        )
        with pytest.raises(CoordinatorUnsupportedProtocolError) as exc_info:
            build_client(config)
        assert "grpc" in str(exc_info.value)

    def test_http_and_https_backends_build_http_client(self):
        for backend in ("http", "https", "HTTP", ""):
            config = CoordinationConfig(
                backend=backend, url="https://coord.example"
            )
            client = build_client(config)
            assert isinstance(client, HttpCoordinatorClient)


# ---------------------------------------------------------------------------
# HTTP client behavior (faked transport)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._buf.close()
        return False


class TestHttpClient:
    def _make(self, opener):
        config = CoordinationConfig(url="https://coord.example", token="secret")
        return HttpCoordinatorClient(config, opener=opener)

    def test_status_supported_version(self):
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            return _FakeResponse(json.dumps({"api_version": "1", "message": "hi"}).encode())

        client = self._make(fake_open)
        status = client.status()
        assert status.enabled is True
        assert status.ok is True
        assert status.api_version == "1"
        assert status.message == "hi"
        assert captured["url"].endswith("/v1/status")
        # Authorization header included with bearer
        auth = {k.lower(): v for k, v in captured["headers"].items()}
        assert auth.get("authorization") == "Bearer secret"

    def test_status_unsupported_version(self):
        def fake_open(request, timeout):
            return _FakeResponse(json.dumps({"api_version": "999"}).encode())

        client = self._make(fake_open)
        with pytest.raises(CoordinatorUnsupportedVersionError):
            client.status()

    def test_status_missing_version(self):
        def fake_open(request, timeout):
            return _FakeResponse(json.dumps({"message": "hi"}).encode())

        client = self._make(fake_open)
        with pytest.raises(CoordinatorMalformedResponseError):
            client.status()

    def test_status_non_json(self):
        def fake_open(request, timeout):
            return _FakeResponse(b"not json at all")

        client = self._make(fake_open)
        with pytest.raises(CoordinatorMalformedResponseError):
            client.status()

    def test_status_auth_failure(self):
        from urllib import error as urlerror

        def fake_open(request, timeout):
            raise urlerror.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"")
            )

        client = self._make(fake_open)
        with pytest.raises(CoordinatorAuthError):
            client.status()

    def test_status_unavailable(self):
        from urllib import error as urlerror

        def fake_open(request, timeout):
            raise urlerror.URLError("network down")

        client = self._make(fake_open)
        with pytest.raises(CoordinatorUnavailableError):
            client.status()

    def test_supported_versions_const_includes_v1(self):
        assert "1" in SUPPORTED_API_VERSIONS

    def test_acquire_lease_returns_lease_payload(self):
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return _FakeResponse(json.dumps({"lease": {"lease_id": 123}}).encode())

        client = self._make(fake_open)
        lease = client.acquire_lease({"repo_id": "repo", "spec_id": "spec-a"})

        assert lease == {"lease_id": 123}
        assert captured["url"].endswith("/v1/leases/acquire")
        assert captured["body"]["repo_id"] == "repo"

    def test_acquire_lease_conflict_raises_with_owner_payload(self):
        from urllib import error as urlerror

        body = json.dumps(
            {
                "error": "lease-conflict",
                "message": "busy",
                "lease": {"machine_id": "machine-b", "run_id": "run-2"},
            }
        ).encode()

        def fake_open(request, timeout):
            raise urlerror.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(body))

        client = self._make(fake_open)
        with pytest.raises(CoordinatorLeaseConflictError) as exc_info:
            client.acquire_lease({"repo_id": "repo", "spec_id": "spec-a"})

        assert exc_info.value.lease["machine_id"] == "machine-b"

    def test_list_leases_returns_payload(self):
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            return _FakeResponse(
                json.dumps(
                    {
                        "leases": [{"spec_id": "spec-a", "machine_id": "machine-b"}],
                        "machines": [{"machine_id": "machine-b"}],
                    }
                ).encode()
            )

        client = self._make(fake_open)
        payload = client.list_leases(repo_id="repo id")

        assert captured["url"].endswith("/v1/leases?repo_id=repo+id")
        assert payload["leases"][0]["spec_id"] == "spec-a"
        assert payload["machines"][0]["machine_id"] == "machine-b"


# ---------------------------------------------------------------------------
# URL redaction & diagnostic output
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redact_strips_basic_auth_credentials(self):
        assert (
            _redact_url_credentials("https://user:pass@coord.example/path")
            == "https://coord.example/path"
        )

    def test_redact_passes_url_without_creds(self):
        assert (
            _redact_url_credentials("https://coord.example/path")
            == "https://coord.example/path"
        )

    def test_redact_handles_empty(self):
        assert _redact_url_credentials("") == ""

    def test_redact_does_not_strip_at_in_path(self):
        # An "@" inside the path must not be confused for a userinfo separator.
        url = "https://coord.example/foo@bar"
        assert _redact_url_credentials(url) == url


class TestCoordStatusCli:
    def _patch_root_to(self, tmp_path):
        return patch("spec_runtime.cli._resolve_repo_root", return_value=tmp_path)

    def test_status_disabled_redacts_and_reports(self, tmp_path, capsys):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')

        from spec_runtime.cli import main

        with patch(
            "spec_runtime.config._config_path",
            return_value=spec_toml,
        ), patch.dict(
            "os.environ", {}, clear=False
        ):
            # Strip any env var overrides
            for env_var in (
                "SPEC_COORDINATOR_URL",
                "SPEC_COORDINATOR_TOKEN",
                "SPEC_MACHINE_ID",
                "SPEC_COORDINATOR_REPO_ID",
                "SPEC_COORDINATOR_BACKEND",
            ):
                __import__("os").environ.pop(env_var, None)
            rc = main(["coord", "status"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "disabled (local-only)" in out
        assert "Token:        not set" in out

    def test_status_enabled_never_prints_token(self, tmp_path, capsys):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://user:supersecret@coord.example"
repo_id = "myrepo"
"""
        )
        (tmp_path / ".spec.local.toml").write_text(
            """
[coordination]
token = "supersecret-token"
"""
        )

        # Patch the HTTP client status to avoid network.
        fake_status = CoordinatorStatus(
            enabled=True, ok=True, api_version="1", message="ok"
        )

        from spec_runtime.cli import main

        with patch(
            "spec_runtime.config._config_path",
            return_value=spec_toml,
        ), patch(
            "spec_runtime.coordination.HttpCoordinatorClient.status",
            return_value=fake_status,
        ):
            for env_var in (
                "SPEC_COORDINATOR_URL",
                "SPEC_COORDINATOR_TOKEN",
                "SPEC_MACHINE_ID",
                "SPEC_COORDINATOR_REPO_ID",
                "SPEC_COORDINATOR_BACKEND",
            ):
                __import__("os").environ.pop(env_var, None)
            rc = main(["coord", "status"])

        out = capsys.readouterr().out
        assert rc == 0
        # Token must never appear, neither the value nor the embedded auth
        assert "supersecret" not in out
        # Redacted URL has no userinfo
        assert "user:supersecret@" not in out
        assert "https://coord.example" in out
        assert "set (hidden)" in out
        assert "API version:  1" in out

    def test_status_reports_unsupported_protocol(self, tmp_path, capsys):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
backend = "grpc"
url = "https://coord.example"
"""
        )

        from spec_runtime.cli import main

        with patch(
            "spec_runtime.config._config_path",
            return_value=spec_toml,
        ):
            for env_var in (
                "SPEC_COORDINATOR_URL",
                "SPEC_COORDINATOR_TOKEN",
                "SPEC_MACHINE_ID",
                "SPEC_COORDINATOR_REPO_ID",
                "SPEC_COORDINATOR_BACKEND",
            ):
                __import__("os").environ.pop(env_var, None)
            rc = main(["coord", "status"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "unsupported-protocol" in out
        assert "grpc" in out

    def test_status_reports_auth_failure(self, tmp_path, capsys):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "https://coord.example"
"""
        )

        from spec_runtime.cli import main

        with patch(
            "spec_runtime.config._config_path",
            return_value=spec_toml,
        ), patch(
            "spec_runtime.coordination.HttpCoordinatorClient.status",
            side_effect=CoordinatorAuthError("bad token"),
        ):
            for env_var in (
                "SPEC_COORDINATOR_URL",
                "SPEC_COORDINATOR_TOKEN",
                "SPEC_MACHINE_ID",
                "SPEC_COORDINATOR_REPO_ID",
                "SPEC_COORDINATOR_BACKEND",
            ):
                __import__("os").environ.pop(env_var, None)
            rc = main(["coord", "status"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "auth-failed" in out


class TestCoordBootstrap:
    def test_server_init_creates_db_and_tokens_without_printing_hashes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        db_path = tmp_path / "state" / "coord.sqlite"
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(["coord", "init", "--server", "--db", str(db_path)])

        captured = capsys.readouterr()
        assert rc == 0
        assert "worker token worker-default:" in captured.out
        assert "operator token operator-cli:" in captured.out
        assert "spec coord serve" in captured.out
        conn = sqlite3.connect(db_path)
        try:
            hashes = [row[0] for row in conn.execute("SELECT token_hash FROM tokens").fetchall()]
        finally:
            conn.close()
        assert len(hashes) == 2
        for token_hash in hashes:
            assert token_hash not in captured.out

    def test_server_init_preflights_existing_tokens_before_creating_any_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        db_path = tmp_path / "state" / "coord.sqlite"
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main
        from spec_runtime.coordinator_service import CoordinatorStore

        store = CoordinatorStore(db_path)
        try:
            store.create_token(name="operator-cli", scope="operator", token="existing-operator-token")
        finally:
            store.close()

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(["coord", "init", "--server", "--db", str(db_path)])

        captured = capsys.readouterr()
        assert rc == 1
        assert "operator-cli" in captured.err
        assert "worker token worker-default:" not in captured.out
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT name FROM tokens ORDER BY name").fetchall()
        finally:
            conn.close()
        assert rows == [("operator-cli",)]

    def test_server_init_rejects_duplicate_token_names_before_creating_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        db_path = tmp_path / "state" / "coord.sqlite"
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(
                [
                    "coord",
                    "init",
                    "--server",
                    "--db",
                    str(db_path),
                    "--worker-token-name",
                    "shared",
                    "--operator-token-name",
                    "shared",
                ]
            )

        assert rc == 1
        assert "worker and operator token names must differ" in capsys.readouterr().err
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_worker_init_writes_local_toml_and_preserves_unrelated_settings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        local_toml = tmp_path / ".spec.local.toml"
        local_toml.write_text(
            """
[ui]
theme = "plain"

[coordination]
token = "same-token"
"""
        )
        monkeypatch.chdir(tmp_path)
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(
                [
                    "coord",
                    "init",
                    "--worker",
                    "--url",
                    "http://127.0.0.1:8765",
                    "--repo-id",
                    "repo-a",
                    "--machine-id",
                    "machine-a",
                    "--token",
                    "same-token",
                ]
            )

        assert rc == 0
        text = local_toml.read_text()
        assert '[ui]\ntheme = "plain"' in text
        assert 'backend = "http"' in text
        assert 'url = "http://127.0.0.1:8765"' in text
        assert 'repo_id = "repo-a"' in text
        assert 'machine_id = "machine-a"' in text
        assert 'token = "same-token"' in text
        assert "SPEC_COORDINATOR_TOKEN" in capsys.readouterr().out

    def test_worker_init_writes_local_toml_next_to_active_config_in_worktree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        common_root = tmp_path / "main"
        worktree_root = tmp_path / "linked-worktree"
        common_root.mkdir()
        worktree_root.mkdir()
        spec_toml = worktree_root / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        monkeypatch.chdir(worktree_root)
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.resolve_common_root",
            return_value=common_root,
        ):
            rc = main(
                [
                    "coord",
                    "init",
                    "--worker",
                    "--url",
                    "http://127.0.0.1:8765",
                    "--repo-id",
                    "repo-a",
                    "--machine-id",
                    "machine-a",
                    "--token",
                    "worker-token",
                ]
            )

        assert rc == 0
        assert (worktree_root / ".spec.local.toml").is_file()
        assert not (common_root / ".spec.local.toml").exists()

    def test_worker_init_refuses_to_overwrite_token_without_force(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        (tmp_path / ".spec.local.toml").write_text('[coordination]\ntoken = "old-token"\n')
        monkeypatch.chdir(tmp_path)
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(
                [
                    "coord",
                    "init",
                    "--worker",
                    "--url",
                    "http://127.0.0.1:8765",
                    "--repo-id",
                    "repo-a",
                    "--machine-id",
                    "machine-a",
                    "--token",
                    "new-token",
                ]
            )

        assert rc == 1
        assert "already has a coordination token" in capsys.readouterr().err
        assert "new-token" not in (tmp_path / ".spec.local.toml").read_text()

    def test_worker_init_env_only_does_not_write_local_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text('base_ref = "origin/main"\n')
        monkeypatch.chdir(tmp_path)
        _clear_coord_env(monkeypatch)

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(
                [
                    "coord",
                    "init",
                    "--worker",
                    "--env-only",
                    "--url",
                    "http://127.0.0.1:8765",
                    "--repo-id",
                    "repo-a",
                    "--machine-id",
                    "machine-a",
                    "--token",
                    "worker-token",
                ]
            )

        assert rc == 0
        assert not (tmp_path / ".spec.local.toml").exists()
        out = capsys.readouterr().out
        assert "export SPEC_COORDINATOR_URL='http://127.0.0.1:8765'" in out
        assert "export SPEC_COORDINATOR_TOKEN='worker-token'" in out

    def test_doctor_success_path_against_fake_client(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)
        events: list[str] = []

        class FakeClient:
            def status(self):
                events.append("status")
                return CoordinatorStatus(enabled=True, ok=True, api_version="1", message="ok")

            def acquire_lease(self, payload):
                events.append(f"acquire:{payload['run_id']}")
                if str(payload["run_id"]).endswith("-conflict"):
                    raise CoordinatorLeaseConflictError("busy", lease={"machine_id": "other"})
                return {"lease_id": len(events), "status": "active"}

            def heartbeat_lease(self, lease_id, payload):
                events.append(f"heartbeat:{lease_id}")
                return {"lease_id": lease_id, "status": "active"}

            def release_lease(self, lease_id, payload):
                events.append(f"release:{lease_id}")
                return {"lease_id": lease_id, "status": "released"}

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=FakeClient(),
        ):
            rc = main(["coord", "doctor"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Doctor status: ok" in out
        assert "OK: spec implement --help shows --coordination-bypass" in out
        assert "worker-token" not in out
        assert [event.split(":", 1)[0] for event in events] == [
            "status",
            "acquire",
            "acquire",
            "heartbeat",
            "release",
            "acquire",
            "release",
        ]

    def test_doctor_reports_auth_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "bad-token"
"""
        )
        _clear_coord_env(monkeypatch)

        class FakeClient:
            def status(self):
                raise CoordinatorAuthError("bad token")

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=FakeClient(),
        ):
            rc = main(["coord", "doctor"])

        assert rc == 1
        assert "authentication failed" in capsys.readouterr().out

    def test_doctor_reports_missing_repo_and_machine_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)
        monkeypatch.setattr("spec_runtime.config._default_machine_id", lambda: "")

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml):
            rc = main(["coord", "doctor"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "repo id is missing" in out
        assert "machine id is missing" in out

    def test_doctor_reports_unreachable_and_incompatible_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)

        class UnreachableClient:
            def status(self):
                raise CoordinatorUnavailableError("connection refused")

        class IncompatibleClient:
            def status(self):
                raise CoordinatorUnsupportedVersionError("version 99")

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=UnreachableClient(),
        ):
            assert main(["coord", "doctor"]) == 1
        assert "coordinator unreachable" in capsys.readouterr().out

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=IncompatibleClient(),
        ):
            assert main(["coord", "doctor"]) == 1
        assert "incompatible API version" in capsys.readouterr().out

    def test_doctor_cleans_up_synthetic_lease_after_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)
        events: list[str] = []

        class FakeClient:
            def status(self):
                return CoordinatorStatus(enabled=True, ok=True, api_version="1", message="ok")

            def acquire_lease(self, payload):
                events.append("acquire")
                if len(events) == 2:
                    raise CoordinatorLeaseConflictError("busy", lease={"machine_id": "other"})
                return {"lease_id": "lease-1"}

            def heartbeat_lease(self, lease_id, payload):
                raise CoordinatorUnavailableError("heartbeat failed")

            def release_lease(self, lease_id, payload):
                events.append(f"release:{lease_id}")
                return {"lease_id": lease_id, "status": "released"}

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=FakeClient(),
        ):
            rc = main(["coord", "doctor"])

        assert rc == 1
        assert "cleaned up synthetic lease after failure" in capsys.readouterr().out
        assert events == ["acquire", "acquire", "release:lease-1"]

    def test_doctor_cleans_up_unexpected_conflict_lease(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)
        events: list[str] = []

        class FakeClient:
            def status(self):
                return CoordinatorStatus(enabled=True, ok=True, api_version="1", message="ok")

            def acquire_lease(self, payload):
                run_id = str(payload["run_id"])
                events.append(f"acquire:{run_id}")
                if run_id.endswith("-conflict"):
                    return {"lease_id": "lease-conflict"}
                return {"lease_id": "lease-1"}

            def release_lease(self, lease_id, payload):
                events.append(f"release:{lease_id}:{payload['run_id']}")
                return {"lease_id": lease_id, "status": "released"}

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=FakeClient(),
        ):
            rc = main(["coord", "doctor"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "conflicting synthetic lease was unexpectedly acquired" in out
        assert "cleaned up unexpected conflict lease after failure" in out
        assert "cleaned up synthetic lease after failure" in out
        doctor_run_id = events[0].split(":", 1)[1]
        assert events == [
            f"acquire:{doctor_run_id}",
            f"acquire:{doctor_run_id}-conflict",
            f"release:lease-conflict:{doctor_run_id}-conflict",
            f"release:lease-1:{doctor_run_id}",
        ]

    def test_doctor_cleans_up_reacquired_synthetic_lease_after_release_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        spec_toml = tmp_path / ".spec.toml"
        spec_toml.write_text(
            """
[coordination]
url = "http://coord.example"
repo_id = "repo-a"
machine_id = "machine-a"
token = "worker-token"
"""
        )
        _clear_coord_env(monkeypatch)
        events: list[str] = []
        reacquire_release_attempts = 0

        class FakeClient:
            def status(self):
                return CoordinatorStatus(enabled=True, ok=True, api_version="1", message="ok")

            def acquire_lease(self, payload):
                events.append(f"acquire:{payload['run_id']}")
                if str(payload["run_id"]).endswith("-conflict"):
                    raise CoordinatorLeaseConflictError("busy", lease={"machine_id": "other"})
                if str(payload["run_id"]).endswith("-reacquire"):
                    return {"lease_id": "lease-2"}
                return {"lease_id": "lease-1"}

            def heartbeat_lease(self, lease_id, payload):
                events.append(f"heartbeat:{lease_id}")
                return {"lease_id": lease_id, "status": "active"}

            def release_lease(self, lease_id, payload):
                nonlocal reacquire_release_attempts
                events.append(f"release:{lease_id}")
                if lease_id == "lease-2":
                    reacquire_release_attempts += 1
                    if reacquire_release_attempts == 1:
                        raise CoordinatorUnavailableError("release failed")
                return {"lease_id": lease_id, "status": "released"}

        from spec_runtime.cli import main

        with patch("spec_runtime.config._config_path", return_value=spec_toml), patch(
            "spec_runtime.coordinator_bootstrap.build_client",
            return_value=FakeClient(),
        ):
            rc = main(["coord", "doctor"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "cleaned up re-acquired synthetic lease after failure" in out
        assert events[-2:] == ["release:lease-2", "release:lease-2"]
