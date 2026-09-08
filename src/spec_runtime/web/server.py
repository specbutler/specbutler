"""Starlette app factory, auth middleware, and server lifecycle.

Starlette/uvicorn imports are deferred to function scope so that PID-file
helpers and CLI dispatch work even when the ``[web]`` extras are not installed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .auth import (
    cookie_name_for_port,
    load_or_create_token,
    parse_cookies,
    read_token,
)

# Minimal HTML login form for unauthenticated requests
_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spec web — login</title>
<style>
  body { font-family: system-ui, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #0f172a; color: #e2e8f0; }
  form { background: #1e293b; padding: 2rem; border-radius: 0.75rem; max-width: 360px; width: 100%; }
  h1 { font-size: 1.25rem; margin: 0 0 1rem; }
  label { display: block; margin-bottom: 0.5rem; font-size: 0.875rem; }
  input[type=password] { width: 100%; padding: 0.75rem; border: 1px solid #334155;
                     border-radius: 0.375rem; background: #0f172a; color: #e2e8f0;
                     font-size: 1rem; box-sizing: border-box; }
  button { margin-top: 1rem; width: 100%; padding: 0.75rem; border: none;
           border-radius: 0.375rem; background: #3b82f6; color: #fff; font-size: 1rem;
           cursor: pointer; min-height: 44px; }
  button:hover { background: #2563eb; }
</style>
</head>
<body>
<form method="post" action="/auth/login">
  <h1>spec web</h1>
  <label for="token">Token</label>
  <input type="password" id="token" name="token" autocomplete="off" autofocus required>
  <button type="submit">Authenticate</button>
</form>
</body>
</html>
"""


class ServerOwnershipStateError(RuntimeError):
    """Persisted web ownership exists but cannot be verified safely."""


def _native_windows_host() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Security/auth middleware (plain ASGI callables; no eager web dependency)
# ---------------------------------------------------------------------------


_SECURITY_RESPONSE_HEADERS = (
    (b"cache-control", b"no-store, private"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
_SECURITY_RESPONSE_HEADER_NAMES = {
    name for name, _value in _SECURITY_RESPONSE_HEADERS
}

_BROWSER_SESSION_TTL_SECONDS = 60 * 60
_BOOTSTRAP_NONCE_TTL_SECONDS = 60
_CSRF_HEADER = "x-spec-csrf"


@dataclass(frozen=True)
class _BrowserSession:
    """A launch-local browser credential, intentionally distinct from bearer auth."""

    csrf_digest: bytes
    operator_token_digest: bytes
    expires_at: float


class SecurityHeadersMiddleware:
    """Apply browser security policy to every HTTP response, including errors."""

    def __init__(self, app: object) -> None:
        self.app = app

    def __getattr__(self, name: str) -> object:
        # Preserve Starlette's public surface (notably ``state``) for embedded
        # callers and tests while keeping this middleware outside Starlette's
        # own ServerErrorMiddleware so generated 500 responses are covered.
        return getattr(self.app, name)

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_RESPONSE_HEADER_NAMES
                ]
                headers.extend(_SECURITY_RESPONSE_HEADERS)
                message = {**message, "headers": headers}
            await send(message)  # type: ignore[arg-type]

        await self.app(scope, receive, send_with_security_headers)


class AuthMiddleware:
    """ASGI middleware enforcing token authentication on every request.

    When *repo_root* is provided the token is re-read from the on-disk
    auth-token file on every request so that ``spec web token --reset``
    takes effect immediately without a server restart.
    """

    def __init__(
        self,
        app: object,
        token: str,
        repo_root: object = None,
        port: int | None = None,
        bootstrap_nonce: str | None = None,
    ) -> None:
        self.app = app
        self._static_token = token
        self._repo_root = repo_root
        self._cookie_name = cookie_name_for_port(port)
        self._browser_sessions: dict[str, _BrowserSession] = {}
        self._bootstrap_nonces: dict[bytes, float] = {}
        if bootstrap_nonce:
            digest = hashlib.sha256(bootstrap_nonce.encode("utf-8")).digest()
            self._bootstrap_nonces[digest] = (
                time.monotonic() + _BOOTSTRAP_NONCE_TTL_SECONDS
            )

    def _current_token(self) -> str | None:
        """Return the live token, failing closed when durable state is unreadable."""
        if self._repo_root is not None:
            try:
                file_token = read_token(self._repo_root)
                if file_token:
                    return file_token
            except Exception:
                return None
            return None
        return self._static_token

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        current_token = self._current_token()

        headers = dict(
            (k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])
        )
        cookies = parse_cookies(headers.get("cookie", ""))
        query_params = _query_params(scope)
        bootstrap_nonce = _single_query_value(query_params, "bootstrap")
        bearer_token = _bearer_token(headers)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))

        # `--open` carries only a short-lived, single-use nonce in its URL.
        # The durable bearer and the browser request proof never appear in a
        # navigation URL, including redirect history retained by browsers.
        if bootstrap_nonce is not None:
            if (
                method not in {"GET", "HEAD"}
                or path != "/"
                or current_token is None
                or not self._consume_bootstrap_nonce(bootstrap_nonce)
            ):
                await self._unauthenticated(scope, receive, send)
                return
            session_id, csrf_token = self._new_browser_session(current_token)
            await self._send_browser_bootstrap(
                scope,
                receive,
                send,
                session_id=session_id,
                csrf_token=csrf_token,
            )
            return

        if method == "POST" and path == "/auth/login":
            submitted_token = await _read_login_token(receive, headers)
            if (
                current_token is None
                or submitted_token is None
                or not secrets.compare_digest(submitted_token, current_token)
            ):
                await self._unauthenticated(scope, receive, send)
                return
            session_id, csrf_token = self._new_browser_session(current_token)
            await self._send_browser_bootstrap(
                scope,
                receive,
                send,
                session_id=session_id,
                csrf_token=csrf_token,
            )
            return

        if bearer_token is not None:
            if (
                current_token is not None
                and secrets.compare_digest(bearer_token, current_token)
            ):
                await self.app(scope, receive, send)
                return
            await self._unauthenticated(scope, receive, send)
            return

        browser_session = self._valid_browser_session(
            cookies.get(self._cookie_name), current_token
        )
        if browser_session is not None:
            if path.startswith("/api/"):
                csrf_token = headers.get(_CSRF_HEADER, "")
                if not csrf_token or not secrets.compare_digest(
                    hashlib.sha256(csrf_token.encode("utf-8")).digest(),
                    browser_session.csrf_digest,
                ):
                    from starlette.responses import JSONResponse

                    response = JSONResponse(
                        {"error": "Browser session proof required"}, status_code=403
                    )
                    await response(scope, receive, send)
                    return
                if not _is_safe_http_method(method) and not _has_same_origin_browser_context(
                    scope, headers
                ):
                    from starlette.responses import JSONResponse

                    response = JSONResponse(
                        {"error": "Cross-origin browser request rejected"},
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return
            await self.app(scope, receive, send)
            return

        # Unauthenticated — return JSON for API routes, HTML login form otherwise
        await self._unauthenticated(scope, receive, send)

    def _new_browser_session(self, current_token: str) -> tuple[str, str]:
        now = time.monotonic()
        self._browser_sessions = {
            session_id: session
            for session_id, session in self._browser_sessions.items()
            if session.expires_at > now
        }
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        self._browser_sessions[session_id] = _BrowserSession(
            csrf_digest=hashlib.sha256(csrf_token.encode("utf-8")).digest(),
            operator_token_digest=hashlib.sha256(current_token.encode("utf-8")).digest(),
            expires_at=now + _BROWSER_SESSION_TTL_SECONDS,
        )
        return session_id, csrf_token

    def _consume_bootstrap_nonce(self, nonce: str) -> bool:
        digest = hashlib.sha256(nonce.encode("utf-8")).digest()
        expires_at = self._bootstrap_nonces.pop(digest, None)
        return expires_at is not None and expires_at > time.monotonic()

    def _valid_browser_session(
        self, session_id: str | None, current_token: str | None
    ) -> _BrowserSession | None:
        if not session_id or current_token is None:
            return None
        session = self._browser_sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= time.monotonic():
            self._browser_sessions.pop(session_id, None)
            return None
        current_digest = hashlib.sha256(current_token.encode("utf-8")).digest()
        if not secrets.compare_digest(session.operator_token_digest, current_digest):
            self._browser_sessions.pop(session_id, None)
            return None
        return session

    async def _send_browser_bootstrap(
        self,
        scope: dict,
        receive: object,
        send: object,
        *,
        session_id: str,
        csrf_token: str,
    ) -> None:
        from starlette.responses import HTMLResponse

        rendered_proof = json.dumps(csrf_token).replace("<", "\\u003c")
        response = HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Signing in</title>"
            "<script>(function(){"
            f'localStorage.setItem("spec-web-csrf",{rendered_proof});'
            'window.location.replace("/#/");'
            "})();</script>",
            status_code=200,
        )
        response.set_cookie(
            self._cookie_name,
            session_id,
            httponly=True,
            samesite="strict",
            secure=scope.get("scheme") == "https",
            max_age=_BROWSER_SESSION_TTL_SECONDS,
            path="/",
        )
        await response(scope, receive, send)

    async def _unauthenticated(
        self, scope: dict, receive: object, send: object
    ) -> None:
        path = scope.get("path", "")
        if path.startswith("/api/"):
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "Authentication required"}, status_code=401)
        else:
            from starlette.responses import HTMLResponse

            response = HTMLResponse(_LOGIN_HTML, status_code=401)
        await response(scope, receive, send)


def _query_params(scope: dict) -> list[tuple[str, str]]:
    raw_query = scope.get("query_string", b"")
    query = raw_query.decode("utf-8", errors="replace")
    return parse_qsl(query, keep_blank_values=True)


def _single_query_value(params: list[tuple[str, str]], name: str) -> str | None:
    values = [value for key, value in params if key == name]
    return values[0] if len(values) == 1 and values[0] else None


def _bearer_token(headers: dict[str, str]) -> str | None:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def _read_login_token(receive: object, headers: dict[str, str]) -> str | None:
    """Read a bounded URL-encoded login form without an optional parser dependency."""
    content_type = headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    while True:
        message = await receive()  # type: ignore[operator]
        if message.get("type") != "http.request":
            return None
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes) or len(body) + len(chunk) > 8192:
            return None
        body.extend(chunk)
        if not message.get("more_body", False):
            break
    try:
        params = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    values = [value for key, value in params if key == "token"]
    return values[0] if len(values) == 1 and values[0] else None


def _is_safe_http_method(method: object) -> bool:
    return str(method).upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}


def _normalized_origin(value: str, *, allow_path: bool) -> tuple[str, str, int] | None:
    """Parse an HTTP origin into a stable comparison tuple."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (not allow_path and (parsed.path not in {"", "/"} or parsed.query))
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.rstrip(".").lower(), port


def _request_origin(scope: dict, headers: dict[str, str]) -> tuple[str, str, int] | None:
    scheme = str(scope.get("scheme", "http")).lower()
    host = headers.get("host", "").strip()
    if host:
        return _normalized_origin(f"{scheme}://{host}", allow_path=False)
    server = scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) == 2:
        server_host, server_port = server
        rendered_host = str(server_host)
        if ":" in rendered_host and not rendered_host.startswith("["):
            rendered_host = f"[{rendered_host}]"
        return _normalized_origin(
            f"{scheme}://{rendered_host}:{int(server_port)}",
            allow_path=False,
        )
    return None


def _has_same_origin_browser_context(scope: dict, headers: dict[str, str]) -> bool:
    expected = _request_origin(scope, headers)
    if expected is None:
        return False
    supplied_origin = headers.get("origin", "").strip()
    if supplied_origin:
        return _normalized_origin(supplied_origin, allow_path=False) == expected
    supplied_referer = headers.get("referer", "").strip()
    if supplied_referer:
        return _normalized_origin(supplied_referer, allow_path=True) == expected
    return False


# ---------------------------------------------------------------------------
# PID file management (no starlette dependency)
# ---------------------------------------------------------------------------


def _web_state_dir(repo_root: Path) -> Path:
    from spec_runtime.config import load_spec_runtime_config

    config = load_spec_runtime_config(require=False)
    return repo_root / config.paths.state_dir / "web"


def _pid_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "server.pid"


def _port_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "server.port"


def _supervision_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "server.supervision.json"


def _ready_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "server.ready.json"


def _launch_path(repo_root: Path) -> Path:
    return _web_state_dir(repo_root) / "server.launch.json"


def _helper_metadata_path(_repo_root: Path, supervision_id: str) -> Path:
    from spec_runtime.process_supervisor import durable_metadata_path

    return durable_metadata_path(supervision_id)


def _write_launch_reservation(
    repo_root: Path,
    *,
    supervision_id: str,
    helper_path: Path,
    nonce: str,
    host: str,
    port: int,
) -> None:
    from spec_runtime.platform_fs import atomic_write_text

    payload = {
        "schema": 1,
        "state": "launching",
        "supervision_id": supervision_id,
        "helper_path": str(helper_path.resolve()),
        "nonce": nonce,
        "host": host,
        "port": port,
        "listener": f"{host}:{port}",
    }
    path = _launch_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _read_valid_launch_reservation(
    repo_root: Path,
) -> tuple[str, str, str, int, Path] | None:
    """Parse a launch reservation only when every ownership field is valid."""
    try:
        reservation = json.loads(_launch_path(repo_root).read_text(encoding="utf-8"))
        if (
            not isinstance(reservation, dict)
            or reservation.get("schema") != 1
            or reservation.get("state") != "launching"
        ):
            return None
        supervision_id = reservation["supervision_id"]
        nonce = reservation["nonce"]
        host = reservation["host"]
        port = reservation["port"]
        helper_path = reservation["helper_path"]
        if (
            not isinstance(supervision_id, str)
            or not supervision_id
            or not isinstance(nonce, str)
            or not nonce
            or not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 < port <= 65535
            or not isinstance(helper_path, str)
            or not helper_path
            or reservation.get("listener") != f"{host}:{port}"
        ):
            return None
        expected_helper = _helper_metadata_path(repo_root, supervision_id).resolve()
        if Path(helper_path).resolve() != expected_helper:
            return None
        return supervision_id, nonce, host, port, expected_helper
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, json.JSONDecodeError):
        return None


def _recover_launch(repo_root: Path, *, readiness_timeout: float = 1.0) -> object | None:
    """Recover or reserve a supervised launch left by another launcher.

    A valid live token is returned even before readiness.  Callers therefore
    treat the launch as occupied instead of starting a duplicate service.  The
    durable public token is only written after the authenticated child-ready
    record and listener probe both succeed.
    """
    from spec_runtime.process_supervisor import SupervisionToken, identity_matches

    reservation = _read_valid_launch_reservation(repo_root)
    if reservation is None:
        return None
    supervision_id, nonce, host, port, expected_helper = reservation
    try:
        # The payload executes the normal foreground start path.  It must not
        # mistake its own reservation for an already-running server.  The
        # nonce alone is not authorization: environment variables can be
        # inherited or operator-supplied, so also bind the current identity to
        # the live durable ownership boundary.
        if _launch_reservation_belongs_to_current_payload(
            repo_root,
            reservation=reservation,
            helper_timeout=10.0,
        ):
            return None
        token = SupervisionToken.from_dict(json.loads(expected_helper.read_text(encoding="utf-8")))
        if token.token != supervision_id:
            return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None

    helper_live = identity_matches(token.identity)
    payload_live = identity_matches(token.payload)
    if not helper_live and not payload_live:
        _launch_path(repo_root).unlink(missing_ok=True)
        _ready_path(repo_root).unlink(missing_ok=True)
        expected_helper.unlink(missing_ok=True)
        return None

    ready_token = None
    if helper_live:
        ready_token = _wait_for_ready_record(
            repo_root,
            nonce=nonce,
            token=token,
            host=host,
            port=port,
            timeout=readiness_timeout,
        )
    if isinstance(ready_token, SupervisionToken):
        token = ready_token
    authenticated_ready = (
        isinstance(ready_token, SupervisionToken)
        and _ready_record_matches(
            repo_root,
            nonce=nonce,
            token=token,
            host=host,
            port=port,
        )
        and identity_matches(token.identity)
        and identity_matches(token.payload)
        and _wait_for_port(_connectable_host(host), port, timeout=0.25)
    )
    if not authenticated_ready:
        return token

    write_supervision_token(repo_root, token)
    _launch_path(repo_root).unlink(missing_ok=True)
    expected_helper.unlink(missing_ok=True)
    return token


def _launch_reservation_belongs_to_current_payload(
    repo_root: Path,
    *,
    reservation: tuple[str, str, str, int, Path] | None = None,
    helper_timeout: float = 0.0,
) -> bool:
    """Return whether the reservation owns this exact foreground payload."""
    from spec_runtime.process_supervisor import (
        SupervisionToken,
        inspect_process,
        supervision_token_contains_process,
    )

    reservation = reservation or _read_valid_launch_reservation(repo_root)
    if reservation is None:
        return False
    supervision_id, nonce, _host, _port, expected_helper = reservation
    if os.environ.get("SPEC_WEB_READY_NONCE") != nonce:
        return False

    deadline = time.monotonic() + max(0.0, helper_timeout)
    first_attempt = True
    while first_attempt or time.monotonic() < deadline:
        first_attempt = False
        try:
            token = SupervisionToken.from_dict(
                json.loads(expected_helper.read_text(encoding="utf-8"))
            )
            current = inspect_process(os.getpid())
            if token.token != supervision_id or current is None:
                return False
            return supervision_token_contains_process(token, current)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.02, remaining))
    return False


def _publish_ready_record(repo_root: Path, *, nonce: str, host: str, port: int) -> None:
    """Authenticate readiness as a record written by the listening child."""
    if not nonce:
        return
    from spec_runtime.platform_fs import atomic_write_text
    from spec_runtime.process_supervisor import inspect_process

    identity = inspect_process(os.getpid())
    if identity is None:
        raise RuntimeError("Could not record web payload identity")
    payload = {
        "nonce": nonce,
        "payload_identity": identity.to_dict(),
        "host": host,
        "port": port,
        "listener": f"{host}:{port}",
    }
    atomic_write_text(_ready_path(repo_root), json.dumps(payload, sort_keys=True) + "\n")


def _ready_record_matches(
    repo_root: Path,
    *,
    nonce: str,
    token: object,
    host: str,
    port: int,
) -> bool:
    from spec_runtime.process_supervisor import SupervisionToken, identity_matches

    if not isinstance(token, SupervisionToken):
        return False
    try:
        payload = json.loads(_ready_path(repo_root).read_text(encoding="utf-8"))
        return bool(
            payload.get("nonce") == nonce
            and payload.get("host") == host
            and int(payload.get("port", -1)) == port
            and payload.get("listener") == f"{host}:{port}"
            and payload.get("payload_identity") == token.payload.to_dict()
            and identity_matches(token.payload)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _wait_for_ready_record(
    repo_root: Path,
    *,
    nonce: str,
    token: object,
    host: str,
    port: int,
    timeout: float = 10.0,
) -> object | None:
    from spec_runtime.process_supervisor import (
        ProcessIdentity,
        SupervisionToken,
        identity_matches,
        promote_payload_identity,
    )

    if not isinstance(token, SupervisionToken):
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    first_attempt = True
    while first_attempt or time.monotonic() < deadline:
        first_attempt = False
        try:
            payload = json.loads(_ready_path(repo_root).read_text(encoding="utf-8"))
            if (
                payload.get("nonce") == nonce
                and payload.get("host") == host
                and int(payload.get("port", -1)) == port
                and payload.get("listener") == f"{host}:{port}"
                and isinstance(payload.get("payload_identity"), dict)
            ):
                candidate = ProcessIdentity.from_dict(payload["payload_identity"])
                if os.name == "nt":
                    promoted = promote_payload_identity(token, candidate)
                elif candidate == token.payload and identity_matches(candidate):
                    promoted = token
                else:
                    promoted = None
                if promoted is not None and _ready_record_matches(
                    repo_root,
                    nonce=nonce,
                    token=promoted,
                    host=host,
                    port=port,
                ):
                    return promoted
        except (OSError, TypeError, ValueError, json.JSONDecodeError, ProcessLookupError):
            pass
        if not identity_matches(token.identity):
            return None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.02, remaining))
    return None


def write_supervision_token(repo_root: Path, token: object) -> None:
    import json

    from spec_runtime.platform_fs import atomic_write_text
    from spec_runtime.process_supervisor import SupervisionToken

    if not isinstance(token, SupervisionToken):
        raise TypeError("expected SupervisionToken")
    path = _supervision_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(token.to_dict(), sort_keys=True) + "\n")


def read_supervision_token(repo_root: Path) -> object | None:
    import json

    from spec_runtime.process_supervisor import SupervisionToken

    try:
        payload = json.loads(_supervision_path(repo_root).read_text(encoding="utf-8"))
        return SupervisionToken.from_dict(payload)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _read_process_started_at(pid: int) -> str:
    """Best-effort process start time via ``ps``. Returns '' on failure."""
    try:
        from spec_runtime.autopilot import read_process_identity

        identity = read_process_identity(pid)
        return identity.started_at if identity else ""
    except Exception:
        return ""


def write_pid(repo_root: Path, port: int | None = None) -> None:
    path = _pid_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    started_at = _read_process_started_at(pid)
    path.write_text(f"{pid}\n{started_at}", encoding="utf-8")
    if port is not None:
        _port_path(repo_root).write_text(str(port), encoding="utf-8")


def read_pid(repo_root: Path) -> tuple[int | None, str]:
    """Return (pid, started_at) from the PID file. started_at may be empty."""
    path = _pid_path(repo_root)
    if not path.exists():
        return None, ""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(lines[0])
        started_at = lines[1] if len(lines) > 1 else ""
        return pid, started_at
    except (ValueError, OSError, IndexError):
        return None, ""


def read_port(repo_root: Path) -> int | None:
    path = _port_path(repo_root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def remove_pid(repo_root: Path) -> None:
    path = _pid_path(repo_root)
    if path.exists():
        path.unlink(missing_ok=True)
    port_p = _port_path(repo_root)
    if port_p.exists():
        port_p.unlink(missing_ok=True)
    _supervision_path(repo_root).unlink(missing_ok=True)
    _ready_path(repo_root).unlink(missing_ok=True)
    _launch_path(repo_root).unlink(missing_ok=True)


def is_server_running(repo_root: Path) -> tuple[bool, int | None]:
    from spec_runtime.process_supervisor import (
        SupervisionToken,
        identity_matches,
        legacy_pid_record_is_live,
    )

    supervision_state_exists = _supervision_path(repo_root).exists()
    supervision_token = read_supervision_token(repo_root)
    if supervision_token is None and supervision_state_exists:
        # Never downgrade malformed durable ownership state to a raw PID.
        raise ServerOwnershipStateError(
            f"durable supervision state is malformed: {_supervision_path(repo_root)}"
        )
    if supervision_token is None and _launch_path(repo_root).exists():
        supervision_token = _recover_launch(repo_root)
    if isinstance(supervision_token, SupervisionToken):
        if identity_matches(supervision_token.identity) and identity_matches(supervision_token.payload):
            return True, supervision_token.payload.pid
        remove_pid(repo_root)
        return False, None
    if _launch_path(repo_root).exists():
        # A malformed or otherwise unverifiable launch reservation is an
        # ownership boundary, not permission to fall back to a raw PID.
        if not _launch_reservation_belongs_to_current_payload(repo_root):
            raise ServerOwnershipStateError(
                f"launch reservation is malformed or unverifiable: {_launch_path(repo_root)}"
            )
    pid_state_exists = _pid_path(repo_root).exists()
    pid, stored_started_at = read_pid(repo_root)
    if pid is None:
        if pid_state_exists:
            raise ServerOwnershipStateError(
                f"legacy PID state is malformed: {_pid_path(repo_root)}"
            )
        return False, None
    if legacy_pid_record_is_live(pid, stored_started_at):
        return True, pid
    remove_pid(repo_root)
    return False, None


# ---------------------------------------------------------------------------
# App factory (requires starlette)
# ---------------------------------------------------------------------------


def create_app(
    repo_root: Path,
    token: str,
    lifespan: object = None,
    port: int | None = None,
    *,
    reload_token: bool = True,
    bootstrap_nonce: str | None = None,
) -> object:
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    from .api import api_routes
    from .chat_api import chat_api_routes

    static_dir = Path(__file__).parent / "static"

    routes = list(api_routes) + list(chat_api_routes)
    if static_dir.is_dir():
        routes.append(Mount("/static", app=StaticFiles(directory=str(static_dir)), name="static"))

    async def index(request: object) -> object:
        index_path = static_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>spec web</h1><p>Static files not found.</p>")

    routes.append(Route("/", index))

    @asynccontextmanager
    async def app_lifespan(app: object):
        """Run caller lifecycle hooks and release per-app process references.

        Implement subprocesses deliberately outlive the web server and are
        adopted by the orchestrator's persisted process metadata. Chat
        provider processes are web-owned and are stopped here. Shutdown drops
        only in-memory references to implementation ``Popen`` objects; it does
        not terminate active implementation work.
        """
        try:
            if lifespan is None:
                yield
            else:
                async with lifespan(app) as state:  # type: ignore[operator]
                    yield state
        finally:
            from .chat_api import shutdown_chat_sessions

            await shutdown_chat_sessions(
                owner_id=app.state.chat_owner_id,  # type: ignore[attr-defined]
            )
            app.state.web_started_procs.clear()  # type: ignore[attr-defined]

    app = Starlette(routes=routes, lifespan=app_lifespan)
    app.state.repo_root = repo_root
    app.state.chat_owner_id = uuid.uuid4().hex
    app.state.web_started_procs = {}
    app.add_middleware(
        AuthMiddleware,
        token=token,
        repo_root=repo_root if reload_token else None,
        port=port,
        bootstrap_nonce=bootstrap_nonce,
    )

    # Keep security headers outside Starlette's generated error middleware so
    # even authentication failures and unhandled-error responses cannot be
    # framed by another localhost origin.
    return SecurityHeadersMiddleware(app)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _connectable_host(host: str) -> str:
    """Return a loopback address when *host* is a wildcard bind address.

    Wildcard addresses like ``0.0.0.0`` or ``::`` tell the OS to bind every
    interface but are not routable destinations.  For readiness probes and
    browser URLs we need a real connectable address — ``127.0.0.1`` or
    ``::1`` respectively.
    """
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def is_loopback_bind(host: str) -> bool:
    """Return whether *host* unambiguously names a loopback interface."""
    candidate = host.strip().lower().rstrip(".")
    if candidate == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Do not resolve arbitrary hostnames here: their address can change
        # between this check and bind, and a wildcard DNS result is ambiguous.
        return False


def _validated_trusted_proxies(proxies: tuple[str, ...]) -> tuple[str, ...]:
    validated: list[str] = []
    for raw_proxy in proxies:
        proxy = raw_proxy.strip()
        if not proxy or proxy == "*":
            raise ValueError("trusted proxy entries must be explicit IP addresses")
        try:
            validated.append(str(ipaddress.ip_address(proxy)))
        except ValueError as exc:
            raise ValueError(f"invalid trusted proxy IP: {raw_proxy!r}") from exc
    return tuple(validated)


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until *host:port* accepts a TCP connection, or *timeout* elapses."""
    import socket
    import time

    probe_host = _connectable_host(host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((probe_host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run_server(
    repo_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 7700,
    background: bool = False,
    open_browser: bool = False,
    verbose: bool = False,
    allow_remote: bool = False,
    trusted_proxies: tuple[str, ...] = (),
) -> int:
    if not is_loopback_bind(host) and not allow_remote:
        print(
            "Refusing to expose the web operator control plane on a non-loopback "
            "interface without --allow-remote.",
            file=sys.stderr,
        )
        return 2
    try:
        trusted_proxies = _validated_trusted_proxies(trusted_proxies)
    except ValueError as exc:
        print(f"spec web cannot start: {exc}", file=sys.stderr)
        return 2

    # Refuse to start if a server is already running — prevents orphaning
    # the existing process by overwriting its PID file.
    try:
        running, existing_pid = is_server_running(repo_root)
    except ServerOwnershipStateError as exc:
        print(f"spec web cannot start safely: {exc}", file=sys.stderr)
        return 1
    if running:
        existing_port = read_port(repo_root)
        port_info = f" on port {existing_port}" if existing_port else ""
        print(
            f"spec web is already running (pid {existing_pid}{port_info}). "
            "Stop it first with `spec web stop`.",
            file=sys.stderr,
        )
        return 1

    token = load_or_create_token(repo_root)
    probe_host = _connectable_host(host)
    bootstrap_nonce = (
        os.environ.get("SPEC_WEB_BOOTSTRAP_NONCE", "").strip()
        or secrets.token_urlsafe(32)
    )
    auth_url = f"http://{probe_host}:{port}/?bootstrap={bootstrap_nonce}"

    # Configure Python logging for spec_runtime.web.* loggers so that
    # INFO-level diagnostics (e.g. log_backend_availability, codex stderr)
    # are visible on the console.  Uvicorn configures its own loggers
    # separately; this covers the application loggers.
    import logging

    app_log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=app_log_level)

    # Warn at startup which chat backends are available.
    try:
        from .chat_api import log_backend_availability

        log_backend_availability(repo_root)
    except Exception:
        pass  # chat_api may fail to import if optional deps missing

    if background:
        # Pre-launch: verify the port is not already occupied by another
        # process. Without this check the readiness probe in the parent
        # would succeed immediately (because something *is* listening)
        # while the supervised child later dies with EADDRINUSE.
        import socket

        try:
            with socket.create_connection((probe_host, port), timeout=0.5):
                print(
                    f"Port {port} is already in use by another process.",
                    file=sys.stderr,
                )
                return 1
        except OSError:
            pass  # Good — port is free

        from spec_runtime.process_supervisor import (
            LifetimeMode,
            ProcessSupervisor,
            SupervisionToken,
            identity_matches,
        )

        state_dir = _web_state_dir(repo_root)
        state_dir.mkdir(parents=True, exist_ok=True)
        supervision_id = uuid.uuid4().hex
        ready_nonce = uuid.uuid4().hex
        helper_path = _helper_metadata_path(repo_root, supervision_id)
        _ready_path(repo_root).unlink(missing_ok=True)
        _write_launch_reservation(
            repo_root,
            supervision_id=supervision_id,
            helper_path=helper_path,
            nonce=ready_nonce,
            host=host,
            port=port,
        )
        log_path = state_dir / "server.log"
        try:
            log_fd = os.open(
                log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                log_handle = os.fdopen(log_fd, "a", encoding="utf-8")
            except BaseException:
                os.close(log_fd)
                raise
        except OSError as exc:
            _launch_path(repo_root).unlink(missing_ok=True)
            _ready_path(repo_root).unlink(missing_ok=True)
            helper_path.unlink(missing_ok=True)
            print(f"spec web failed to open server.log: {exc}", file=sys.stderr)
            return 1
        try:
            try:
                log_path.chmod(0o600)
            except OSError:
                pass
            command = [
                sys.executable,
                "-m",
                "spec_runtime.cli",
                "web",
                "start",
                "--host",
                host,
                "--port",
                str(port),
            ]
            if verbose:
                command.append("--verbose")
            if allow_remote:
                command.append("--allow-remote")
            for trusted_proxy in trusted_proxies:
                command.extend(["--trusted-proxy", trusted_proxy])
            child_env = os.environ.copy()
            child_env["SPEC_WEB_READY_NONCE"] = ready_nonce
            child_env["SPEC_WEB_BOOTSTRAP_NONCE"] = bootstrap_nonce
            managed = ProcessSupervisor(
                LifetimeMode.DETACHED,
                supervision_id=supervision_id,
                publish_durable_token=True,
            ).spawn(
                command,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=child_env,
            )
        except Exception as exc:
            _launch_path(repo_root).unlink(missing_ok=True)
            _ready_path(repo_root).unlink(missing_ok=True)
            helper_path.unlink(missing_ok=True)
            print(f"spec web failed to launch: {exc}", file=sys.stderr)
            return 1
        finally:
            log_handle.close()

        try:
            ready_token = _wait_for_ready_record(
                repo_root,
                nonce=ready_nonce,
                token=managed.token,
                host=host,
                port=port,
            )
            if isinstance(ready_token, SupervisionToken):
                # Keep failure cleanup on the same identity that readiness
                # authenticated (and Windows atomically promoted).
                managed.token = ready_token
            ready_confirmed = (
                isinstance(ready_token, SupervisionToken)
                and _ready_record_matches(
                    repo_root,
                    nonce=ready_nonce,
                    token=ready_token,
                    host=host,
                    port=port,
                )
                and _wait_for_port(probe_host, port, timeout=0.5)
            )
            if not ready_confirmed:
                raise RuntimeError("child did not publish authenticated readiness")
            # Publish only after readiness. Publishing earlier makes the child
            # observe its own durable token and reject startup as a duplicate.
            write_supervision_token(repo_root, ready_token)
        except Exception:
            try:
                managed.terminate(grace_seconds=0.5)
            except Exception:
                pass
            if not identity_matches(managed.token.identity):
                remove_pid(repo_root)
                helper_path.unlink(missing_ok=True)
            print("spec web failed to start (see server.log).", file=sys.stderr)
            return 1

        _launch_path(repo_root).unlink(missing_ok=True)
        helper_path.unlink(missing_ok=True)
        print(f"spec web running on http://{probe_host}:{port}", file=sys.stderr)
        print(f"One-time browser URL: {auth_url}", file=sys.stderr)
        if open_browser:
            import webbrowser

            webbrowser.open(auth_url)
        return 0

    background_payload = _launch_reservation_belongs_to_current_payload(repo_root)
    try:
        if not background_payload:
            # A direct foreground server has no durable helper parent. Claim
            # the current process before publishing it so status, a second
            # start, and an out-of-process stop use an owned POSIX process
            # group or reopenable Windows Job instead of an unsafe raw PID.
            # The supervised background payload carries SPEC_WEB_READY_NONCE
            # and must leave helper-token publication to its authenticated
            # parent.
            from spec_runtime.process_supervisor import claim_current_process

            foreground_token = claim_current_process(f"web-foreground-{uuid.uuid4().hex}")
            write_supervision_token(repo_root, foreground_token)
        write_pid(repo_root, port=port)
    except Exception as exc:
        remove_pid(repo_root)
        print(f"spec web failed to establish process ownership: {exc}", file=sys.stderr)
        return 1

    uvi_log_level = "debug" if verbose else "info"

    try:
        import uvicorn

        app = create_app(
            repo_root,
            token,
            port=port,
            bootstrap_nonce=bootstrap_nonce,
        )

        # Use uvicorn.Server directly so we can hook into startup() and publish
        # authenticated readiness only after the socket binds. A supervised
        # background child executes this same foreground path.
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=uvi_log_level,
            # The browser bootstrap nonce is single-use, short-lived, and not
            # the durable operator bearer. Keep request-target logging off so
            # even that consumed nonce is not retained in terminal/server logs.
            access_log=False,
            proxy_headers=bool(trusted_proxies),
            forwarded_allow_ips=",".join(trusted_proxies),
        )
        server = uvicorn.Server(config)
        _orig_startup = server.startup

        async def _startup_then_banner(**kw: object) -> None:
            await _orig_startup(**kw)
            if server.started:
                _publish_ready_record(
                    repo_root,
                    nonce=(
                        os.environ.get("SPEC_WEB_READY_NONCE", "")
                        if background_payload
                        else ""
                    ),
                    host=host,
                    port=port,
                )
                print(
                    f"spec web running on http://{probe_host}:{port}",
                    file=sys.stderr,
                )
                # A supervised background child writes stdout/stderr to the
                # repository-local server.log.  Its parent already prints the
                # authenticated URL to the invoking terminal, so never copy
                # the bearer token into the durable child log.
                if not background_payload:
                    print(f"One-time browser URL: {auth_url}", file=sys.stderr)
                if open_browser:
                    import webbrowser

                    webbrowser.open(auth_url)

        server.startup = _startup_then_banner  # type: ignore[assignment]
        try:
            server.run()
        except KeyboardInterrupt:
            # Uvicorn versions differ on whether Server.run() consumes SIGINT.
            # A normal foreground Ctrl-C is a clean operator stop.
            pass
    finally:
        # Only remove PID file if it still refers to this process — avoids
        # deleting a file that a concurrent start may have written.
        current_pid, _ = read_pid(repo_root)
        if current_pid == os.getpid():
            remove_pid(repo_root)

    return 0


def stop_server(repo_root: Path) -> int:
    try:
        running, pid = is_server_running(repo_root)
    except ServerOwnershipStateError as exc:
        print(f"spec web cannot stop safely: {exc}", file=sys.stderr)
        return 1
    if not running or pid is None:
        print("spec web is not running.", file=sys.stderr)
        return 1
    from spec_runtime.process_supervisor import (
        SupervisionToken,
        identity_matches,
        retire_inactive_control_state,
        terminate,
        terminate_legacy_pid_record,
    )

    supervision_token = read_supervision_token(repo_root)
    if supervision_token is None and _launch_path(repo_root).exists():
        supervision_token = _recover_launch(repo_root, readiness_timeout=0.0)
    if isinstance(supervision_token, SupervisionToken):
        if not terminate(supervision_token):
            print(
                f"spec web failed to stop owned process tree (pid {pid}); supervision state retained.",
                file=sys.stderr,
            )
            return 1
        if _native_windows_host():
            # A durable helper can report an empty Job just before its own
            # finally block retires the authenticated records. Wait only while
            # that exact keeper identity remains live, then make one final
            # exact-token retirement attempt. A same-ID replacement has a
            # different nonce/identity and remains fail-closed.
            retirement_deadline = time.monotonic() + 5.0
            while not retire_inactive_control_state(supervision_token):
                if not identity_matches(supervision_token.identity):
                    if retire_inactive_control_state(supervision_token):
                        break
                    print(
                        f"spec web stopped process tree (pid {pid}) but could not "
                        "retire its authenticated control state; recovery state retained.",
                        file=sys.stderr,
                    )
                    return 1
                remaining = retirement_deadline - time.monotonic()
                if remaining <= 0:
                    print(
                        f"spec web stopped process tree (pid {pid}) but could not "
                        "retire its authenticated control state; recovery state retained.",
                        file=sys.stderr,
                    )
                    return 1
                time.sleep(min(0.05, remaining))
    else:
        recorded_pid, recorded_started_at = read_pid(repo_root)
        if recorded_pid != pid or not terminate_legacy_pid_record(
            pid,
            recorded_started_at,
        ):
            print(
                "spec web cannot stop safely because durable supervision is "
                "missing and legacy PID ownership could not be verified.",
                file=sys.stderr,
            )
            return 1
    remove_pid(repo_root)
    print(f"spec web stopped (pid {pid}).", file=sys.stderr)
    return 0


def server_status(repo_root: Path) -> int:
    try:
        running, pid = is_server_running(repo_root)
    except ServerOwnershipStateError as exc:
        print(f"spec web ownership state is invalid: {exc}", file=sys.stderr)
        return 1
    if running:
        port = read_port(repo_root)
        if port:
            print(f"spec web is running on port {port} (pid {pid}).")
        else:
            print(f"spec web is running (pid {pid}).")
    else:
        print("spec web is not running.")
    return 0


def print_token(repo_root: Path, *, reset: bool = False) -> int:
    if reset:
        from .auth import reset_token

        token = reset_token(repo_root)
        print(f"Token reset: {token}")
    else:
        token = read_token(repo_root)
        if token:
            print(token)
        else:
            token = load_or_create_token(repo_root)
            print(token)
    return 0
