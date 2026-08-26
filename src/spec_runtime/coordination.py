"""Coordinator client abstraction.

This module provides the small client boundary used by Spec Butler to
talk to a coordination service when one is configured. The boundary is kept
deliberately small so tests can fake it without a network and so future
backends (HTTP, local socket, in-memory) can plug in without churn.

Behavior summary:

* When coordination is disabled (no ``url``), :func:`build_client` returns a
  :class:`NoOpCoordinatorClient` whose ``status()`` reports the local-only
  state and whose lease-related methods are not callable.
* When coordination is enabled, :func:`build_client` returns the configured
  backend's client. The default ``http`` backend uses :class:`HttpCoordinatorClient`,
  which calls the coordinator's status/health endpoint and verifies the
  reported API version is supported before any lease endpoint is used.
* Errors are surfaced as a small typed hierarchy
  (:class:`CoordinatorError` and subclasses) so callers can distinguish
  between unavailable coordinators, auth failures, unsupported protocol or
  version, and malformed responses.

This spec only adds plumbing: no orchestration behavior changes here beyond
exposing :func:`build_client`, the no-op client, and ``spec coord status``
diagnostics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .config import CoordinationConfig

SUPPORTED_API_VERSIONS: tuple[str, ...] = ("1", "1.0")
"""Coordinator API versions this client understands.

The coordinator is expected to advertise its API version via the
``api_version`` field on its status/health response. The client refuses to
use lease endpoints when the reported version is not in this list.
"""

DEFAULT_STATUS_PATH = "/v1/status"
DEFAULT_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class CoordinatorError(Exception):
    """Base class for coordinator client errors."""


class CoordinatorUnavailableError(CoordinatorError):
    """Raised when the coordinator cannot be reached (network/DNS/timeout)."""


class CoordinatorAuthError(CoordinatorError):
    """Raised when the coordinator rejects authentication (401/403)."""


class CoordinatorUnsupportedVersionError(CoordinatorError):
    """Raised when the coordinator's API version is not supported."""


class CoordinatorUnsupportedProtocolError(CoordinatorError):
    """Raised when the configured backend/protocol is not supported.

    Distinct from :class:`CoordinatorUnsupportedVersionError`: the version
    error is about the coordinator advertising an API version we don't
    understand, while this is about the *local* config naming a transport
    (e.g. ``backend = "grpc"``) we have no client for.
    """


class CoordinatorMalformedResponseError(CoordinatorError):
    """Raised when the coordinator returns a response we can't parse."""


class CoordinatorDisabledError(CoordinatorError):
    """Raised when a lease-related method is called on the no-op client."""


class CoordinatorLeaseConflictError(CoordinatorError):
    """Raised when another machine owns the requested spec lease."""

    def __init__(self, message: str, *, lease: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.lease = lease or {}


# ---------------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatorStatus:
    """Result of a coordinator status/health probe.

    ``enabled`` is False when coordination is disabled locally (no URL).
    ``ok`` is True only when a real coordinator answered with a supported
    API version. ``api_version`` and ``message`` describe what was observed.
    """

    enabled: bool
    ok: bool
    api_version: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


class CoordinatorClient(Protocol):
    """Minimal coordinator client surface.

    This protocol is intentionally tiny — additional lease endpoints are
    expected to be added in follow-up specs. Anything that requires the
    coordinator to be available must call :meth:`require_enabled` first
    so the no-op client can refuse early with a clear error.
    """

    config: CoordinationConfig

    def status(self) -> CoordinatorStatus: ...

    def require_enabled(self) -> None: ...

    def acquire_lease(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def heartbeat_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def release_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def list_leases(self, *, repo_id: str = "", spec_id: str = "") -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# No-op client
# ---------------------------------------------------------------------------


class NoOpCoordinatorClient:
    """Client used when coordination is disabled.

    All lease-related operations raise :class:`CoordinatorDisabledError`.
    ``status()`` always returns a structured "disabled/local-only" result so
    diagnostics can render uniformly.
    """

    def __init__(self, config: CoordinationConfig) -> None:
        self.config = config

    def status(self) -> CoordinatorStatus:
        return CoordinatorStatus(
            enabled=False,
            ok=True,
            api_version="",
            message="coordination disabled (local-only)",
        )

    def require_enabled(self) -> None:
        raise CoordinatorDisabledError(
            "Coordination is not configured. Set [coordination].url in "
            ".spec.toml or .spec.local.toml, or export "
            "SPEC_COORDINATOR_URL."
        )

    def acquire_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()

    def heartbeat_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()

    def release_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()

    def list_leases(self, *, repo_id: str = "", spec_id: str = "") -> dict[str, Any]:
        self.require_enabled()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class HttpCoordinatorClient:
    """Default HTTP-backed coordinator client.

    The status check hits ``<url>/v1/status`` and expects a JSON body with
    at least an ``api_version`` field. Any non-2xx response, parse failure,
    or unsupported version is surfaced as a typed :class:`CoordinatorError`.
    """

    def __init__(
        self,
        config: CoordinationConfig,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        opener: object | None = None,
    ) -> None:
        self.config = config
        self._timeout = timeout
        # ``opener`` is a callable matching urllib.request.urlopen signature.
        # Tests inject a fake to avoid real network. None falls back to
        # urllib's default.
        self._opener = opener

    def _build_status_url(self) -> str:
        url = self.config.url.rstrip("/")
        return url + DEFAULT_STATUS_PATH

    def _build_url(self, path: str) -> str:
        return self.config.url.rstrip("/") + path

    def _open(self, request: urlrequest.Request):
        if self._opener is not None:
            return self._opener(request, timeout=self._timeout)  # type: ignore[misc]
        return urlrequest.urlopen(request, timeout=self._timeout)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request(self._build_url(path), data=data, headers=headers, method=method)
        if self.config.token:
            request.add_header("Authorization", f"Bearer {self.config.token}")

        try:
            response = self._open(request)
            status_code = int(getattr(response, "status", 200))
            with response:
                raw = response.read()
        except urlerror.HTTPError as exc:
            raw = exc.read()
            status_code = int(exc.code)
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise CoordinatorUnavailableError(f"Cannot reach coordinator: {exc}") from exc

        try:
            parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoordinatorMalformedResponseError(
                f"Coordinator response was not JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise CoordinatorMalformedResponseError("Coordinator response was not a JSON object")

        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise CoordinatorAuthError(str(parsed.get("message") or "coordinator authentication failed"))
        if status_code == HTTPStatus.CONFLICT:
            raise CoordinatorLeaseConflictError(
                str(parsed.get("message") or "coordinator lease conflict"),
                lease=parsed.get("lease") if isinstance(parsed.get("lease"), dict) else {},
            )
        if status_code < 200 or status_code >= 300:
            raise CoordinatorUnavailableError(
                f"Coordinator returned HTTP {status_code}: {parsed.get('message') or parsed.get('error') or 'request failed'}"
            )
        return status_code, parsed

    def status(self) -> CoordinatorStatus:
        if not self.config.enabled:
            return CoordinatorStatus(
                enabled=False,
                ok=True,
                message="coordination disabled (local-only)",
            )

        request = urlrequest.Request(self._build_status_url(), method="GET")
        if self.config.token:
            request.add_header("Authorization", f"Bearer {self.config.token}")

        try:
            response = self._open(request)
        except urlerror.HTTPError as exc:
            if exc.code in (401, 403):
                raise CoordinatorAuthError(
                    f"Coordinator rejected authentication (HTTP {exc.code})"
                ) from exc
            raise CoordinatorUnavailableError(
                f"Coordinator returned HTTP {exc.code}"
            ) from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise CoordinatorUnavailableError(
                f"Cannot reach coordinator: {exc}"
            ) from exc

        with response:
            try:
                raw = response.read()
            except OSError as exc:
                raise CoordinatorMalformedResponseError(
                    f"Failed to read coordinator response: {exc}"
                ) from exc

        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoordinatorMalformedResponseError(
                f"Coordinator status response was not JSON: {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise CoordinatorMalformedResponseError(
                "Coordinator status response was not a JSON object"
            )

        api_version = str(payload.get("api_version", "")).strip()
        if not api_version:
            raise CoordinatorMalformedResponseError(
                "Coordinator status response missing 'api_version'"
            )

        if api_version not in SUPPORTED_API_VERSIONS:
            raise CoordinatorUnsupportedVersionError(
                f"Coordinator advertises API version {api_version!r}; "
                f"supported: {', '.join(SUPPORTED_API_VERSIONS)}"
            )

        message = str(payload.get("message", "ok")).strip() or "ok"
        return CoordinatorStatus(
            enabled=True,
            ok=True,
            api_version=api_version,
            message=message,
        )

    def require_enabled(self) -> None:
        if not self.config.enabled:
            raise CoordinatorDisabledError(
                "Coordination URL is not configured."
            )

    def acquire_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()
        _, response = self._request_json("POST", "/v1/leases/acquire", payload=payload)
        lease = response.get("lease")
        if not isinstance(lease, dict):
            raise CoordinatorMalformedResponseError("Coordinator acquire response missing lease object")
        return lease

    def heartbeat_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()
        _, response = self._request_json("POST", f"/v1/leases/{lease_id}/heartbeat", payload=payload)
        lease = response.get("lease")
        if not isinstance(lease, dict):
            raise CoordinatorMalformedResponseError("Coordinator heartbeat response missing lease object")
        return lease

    def release_lease(self, lease_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()
        _, response = self._request_json("POST", f"/v1/leases/{lease_id}/release", payload=payload)
        lease = response.get("lease")
        if not isinstance(lease, dict):
            raise CoordinatorMalformedResponseError("Coordinator release response missing lease object")
        return lease

    def list_leases(self, *, repo_id: str = "", spec_id: str = "") -> dict[str, Any]:
        self.require_enabled()
        params = {
            key: value
            for key, value in {
                "repo_id": repo_id,
                "spec_id": spec_id,
            }.items()
            if value
        }
        path = "/v1/leases"
        if params:
            path += "?" + urlparse.urlencode(params)
        _, response = self._request_json("GET", path)
        leases = response.get("leases")
        machines = response.get("machines")
        if not isinstance(leases, list):
            raise CoordinatorMalformedResponseError("Coordinator leases response missing leases list")
        if machines is not None and not isinstance(machines, list):
            raise CoordinatorMalformedResponseError("Coordinator leases response has invalid machines list")
        return {
            "leases": leases,
            "machines": machines or [],
        }


def lease_age_seconds(lease: dict[str, Any]) -> float | None:
    heartbeat_at = str(lease.get("heartbeat_at") or "").strip()
    if not heartbeat_at:
        return None
    try:
        parsed = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_client(
    config: CoordinationConfig,
    *,
    opener: object | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CoordinatorClient:
    """Return the appropriate client for the given coordination config.

    Returns a :class:`NoOpCoordinatorClient` when coordination is disabled.
    Selects the backend by ``config.backend`` (default: ``"http"``).
    Unknown backends raise :class:`CoordinatorUnsupportedProtocolError` so
    misconfiguration is reported up-front instead of silently degrading to
    an HTTP probe of a non-HTTP endpoint.
    """
    if not config.enabled:
        return NoOpCoordinatorClient(config)

    backend = (config.backend or "http").strip().lower()
    if backend in ("", "http", "https"):
        return HttpCoordinatorClient(config, timeout=timeout, opener=opener)
    raise CoordinatorUnsupportedProtocolError(
        f"Unsupported coordinator backend {config.backend!r}; "
        f"supported: http, https"
    )
