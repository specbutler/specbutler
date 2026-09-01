"""Starlette app factory, auth middleware, and server lifecycle.

Starlette/uvicorn imports are deferred to function scope so that PID-file
helpers and CLI dispatch work even when the ``[web]`` extras are not installed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .auth import (
    cookie_name_for_port,
    extract_token_from_request,
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
  input[type=text] { width: 100%; padding: 0.75rem; border: 1px solid #334155;
                     border-radius: 0.375rem; background: #0f172a; color: #e2e8f0;
                     font-size: 1rem; box-sizing: border-box; }
  button { margin-top: 1rem; width: 100%; padding: 0.75rem; border: none;
           border-radius: 0.375rem; background: #3b82f6; color: #fff; font-size: 1rem;
           cursor: pointer; min-height: 44px; }
  button:hover { background: #2563eb; }
</style>
</head>
<body>
<form method="get" action="/">
  <h1>spec web</h1>
  <label for="token">Token</label>
  <input type="text" id="token" name="token" autocomplete="off" autofocus required>
  <button type="submit">Authenticate</button>
</form>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Auth middleware (uses starlette types but defined as plain ASGI callable)
# ---------------------------------------------------------------------------


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
    ) -> None:
        self.app = app
        self._static_token = token
        self._repo_root = repo_root
        self._cookie_name = cookie_name_for_port(port)

    def _current_token(self) -> str:
        """Return the live token, re-reading from disk when possible."""
        if self._repo_root is not None:
            try:
                file_token = read_token(self._repo_root)
                if file_token:
                    return file_token
            except Exception:
                pass
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
        request_token = extract_token_from_request(
            scope, headers, cookies, self._cookie_name
        )

        if request_token == current_token:
            # If token was in query string, set cookie and redirect to strip it.
            # Only redirect safe methods (GET/HEAD); for POST and others, set
            # the cookie via a wrapper and let the request proceed so the action
            # actually executes (a 302 would convert POST to GET).
            qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
            if f"token={current_token}" in qs:
                method = scope.get("method", "GET").upper()
                path = scope.get("path", "/")
                if method in ("GET", "HEAD"):
                    params = []
                    for pair in qs.split("&"):
                        if "=" in pair:
                            key, _, value = pair.partition("=")
                            if key != "token":
                                params.append(f"{key}={value}")
                    redirect_path = path
                    if params:
                        redirect_path += "?" + "&".join(params)

                    from starlette.responses import RedirectResponse

                    is_https = scope.get("scheme") == "https"
                    response = RedirectResponse(url=redirect_path, status_code=302)
                    response.set_cookie(
                        self._cookie_name,
                        current_token,
                        httponly=True,
                        samesite="lax",
                        secure=is_https,
                        path="/",
                    )
                    await response(scope, receive, send)
                    return

                # Non-redirect path: set cookie via Set-Cookie header on the
                # response produced by the downstream app, without redirecting.
                # Covers non-safe methods (POST etc.) where a redirect would
                # convert the request to GET and lose the request body.
                is_https = scope.get("scheme") == "https"
                secure_part = "; Secure" if is_https else ""
                cookie_header = (
                    f"{self._cookie_name}={current_token}; HttpOnly; "
                    f"SameSite=Lax; Path=/{secure_part}"
                )

                async def send_with_cookie(message: dict) -> None:
                    if message.get("type") == "http.response.start":
                        headers = list(message.get("headers", []))
                        headers.append(
                            (b"set-cookie", cookie_header.encode("latin-1"))
                        )
                        message = {**message, "headers": headers}
                    await send(message)  # type: ignore[arg-type]

                await self.app(scope, receive, send_with_cookie)
                return

            await self.app(scope, receive, send)
            return

        # Unauthenticated — return JSON for API routes, HTML login form otherwise
        path = scope.get("path", "")
        if path.startswith("/api/"):
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "Authentication required"}, status_code=401)
        else:
            from starlette.responses import HTMLResponse

            response = HTMLResponse(_LOGIN_HTML, status_code=401)
        await response(scope, receive, send)


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


def _helper_metadata_path(repo_root: Path, supervision_id: str) -> Path:
    return repo_root / f".spec-supervisor-{supervision_id}.json"


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


def _recover_launch(repo_root: Path, *, readiness_timeout: float = 1.0) -> object | None:
    """Recover or reserve a Windows launch left by another launcher.

    A valid live token is returned even before readiness.  Callers therefore
    treat the launch as occupied instead of starting a duplicate service.  The
    durable public token is only written after the authenticated child-ready
    record and listener probe both succeed.
    """
    from spec_runtime.process_supervisor import SupervisionToken, identity_matches

    try:
        reservation = json.loads(_launch_path(repo_root).read_text(encoding="utf-8"))
        if not isinstance(reservation, dict) or reservation.get("state") != "launching":
            return None
        supervision_id = str(reservation["supervision_id"])
        nonce = str(reservation["nonce"])
        host = str(reservation["host"])
        port = int(reservation["port"])
        listener = f"{host}:{port}"
        expected_helper = _helper_metadata_path(repo_root, supervision_id).resolve()
        if Path(str(reservation["helper_path"])).resolve() != expected_helper:
            return None
        if reservation.get("listener") != listener or not supervision_id or not nonce:
            return None
        # The payload executes the normal foreground start path.  It must not
        # mistake its own reservation for an already-running server.
        if os.environ.get("SPEC_WEB_READY_NONCE") == nonce:
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

    if helper_live and payload_live:
        _wait_for_ready_record(
            repo_root,
            nonce=nonce,
            token=token,
            timeout=readiness_timeout,
        )
    try:
        ready = json.loads(_ready_path(repo_root).read_text(encoding="utf-8"))
        payload_live = identity_matches(token.payload)
        authenticated_ready = (
            ready.get("nonce") == nonce
            and ready.get("host") == host
            and int(ready.get("port", -1)) == port
            and ready.get("listener") == listener
            and ready.get("payload_identity") == token.payload.to_dict()
            and payload_live
            and _wait_for_port(host, port, timeout=0.25)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        authenticated_ready = False
    if not authenticated_ready:
        return token

    write_supervision_token(repo_root, token)
    _launch_path(repo_root).unlink(missing_ok=True)
    expected_helper.unlink(missing_ok=True)
    return token


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


def _wait_for_ready_record(repo_root: Path, *, nonce: str, token: object, timeout: float = 10.0) -> bool:
    from spec_runtime.process_supervisor import SupervisionToken, identity_matches

    if not isinstance(token, SupervisionToken):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(_ready_path(repo_root).read_text(encoding="utf-8"))
            if (
                payload.get("nonce") == nonce
                and payload.get("payload_identity") == token.payload.to_dict()
                and identity_matches(token.payload)
            ):
                return True
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        if not identity_matches(token.identity):
            return False
        time.sleep(0.02)
    return False


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
    path.write_text(f"{pid}\n{started_at}")
    if port is not None:
        _port_path(repo_root).write_text(str(port))


def read_pid(repo_root: Path) -> tuple[int | None, str]:
    """Return (pid, started_at) from the PID file. started_at may be empty."""
    path = _pid_path(repo_root)
    if not path.exists():
        return None, ""
    try:
        lines = path.read_text().strip().splitlines()
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
        return int(path.read_text().strip())
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
    from spec_runtime.process_supervisor import SupervisionToken, identity_matches

    supervision_token = read_supervision_token(repo_root)
    if supervision_token is None and os.name == "nt":
        supervision_token = _recover_launch(repo_root)
    if isinstance(supervision_token, SupervisionToken):
        if identity_matches(supervision_token.identity) and identity_matches(supervision_token.payload):
            return True, supervision_token.payload.pid
        remove_pid(repo_root)
        return False, None
    if os.name == "nt":
        # A PID record is diagnostic only on Windows; without the durable Job
        # token there is no safe ownership primitive to inspect or signal.
        return False, None
    pid, stored_started_at = read_pid(repo_root)
    if pid is None:
        return False, None
    # Check the process is alive at all.
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        remove_pid(repo_root)
        return False, None
    # If we recorded a start identity, verify the live process matches to
    # guard against PID reuse.
    if stored_started_at:
        live_started_at = _read_process_started_at(pid)
        if live_started_at and live_started_at != stored_started_at:
            remove_pid(repo_root)
            return False, None
    return True, pid


# ---------------------------------------------------------------------------
# App factory (requires starlette)
# ---------------------------------------------------------------------------


def create_app(
    repo_root: Path,
    token: str,
    lifespan: object = None,
    port: int | None = None,
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
            return HTMLResponse(index_path.read_text())
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
    app.add_middleware(AuthMiddleware, token=token, repo_root=repo_root, port=port)

    return app


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
) -> int:
    # Refuse to start if a server is already running — prevents orphaning
    # the existing process by overwriting its PID file.
    running, existing_pid = is_server_running(repo_root)
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
    auth_url = f"http://{probe_host}:{port}/?token={token}"

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
        # Pre-fork: verify the port is not already occupied by another
        # process.  Without this check _wait_for_port() in the parent
        # would succeed immediately (because something *is* listening)
        # while the forked child later dies with EADDRINUSE.
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

        if os.name == "nt":
            from spec_runtime.process_supervisor import LifetimeMode, ProcessSupervisor

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
            log_handle = open(state_dir / "server.log", "a", encoding="utf-8")  # noqa: SIM115
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
            try:
                child_env = os.environ.copy()
                child_env["SPEC_WEB_READY_NONCE"] = ready_nonce
                managed = ProcessSupervisor(
                    LifetimeMode.DETACHED,
                    supervision_id=supervision_id,
                ).spawn(
                    command,
                    cwd=repo_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                )
            finally:
                log_handle.close()
            if not _wait_for_ready_record(repo_root, nonce=ready_nonce, token=managed.token):
                managed.terminate(grace_seconds=0.5)
                from spec_runtime.process_supervisor import identity_matches

                if not identity_matches(managed.token.identity):
                    remove_pid(repo_root)
                    helper_path.unlink(missing_ok=True)
                print("spec web failed to start (see server.log).", file=sys.stderr)
                return 1
            # Publish only after readiness. Publishing earlier makes the child
            # observe its own durable token and reject startup as a duplicate.
            write_supervision_token(repo_root, managed.token)
            _launch_path(repo_root).unlink(missing_ok=True)
            helper_path.unlink(missing_ok=True)
            print(f"spec web running on http://{probe_host}:{port}", file=sys.stderr)
            print(f"Authenticated URL: {auth_url}", file=sys.stderr)
            if open_browser:
                import webbrowser

                webbrowser.open(auth_url)
            return 0

        pid = os.fork()
        if pid > 0:
            # Parent — verify the child actually binds the port before
            # reporting success.  This catches port-in-use and other
            # startup failures that a fixed sleep would miss.
            if not _wait_for_port(host, port):
                print(
                    "spec web failed to start (port may already be in use).",
                    file=sys.stderr,
                )
                return 1
            # Double-check the child is still alive — guards against a
            # narrow race where the port became occupied between our
            # pre-fork check and the child's bind() call.
            try:
                exited_pid, exit_status = os.waitpid(pid, os.WNOHANG)
                if exited_pid != 0:
                    print(
                        "spec web child exited unexpectedly "
                        f"(status {exit_status}).",
                        file=sys.stderr,
                    )
                    return 1
            except ChildProcessError:
                print(
                    "spec web child exited unexpectedly.",
                    file=sys.stderr,
                )
                return 1
            state_dir = _web_state_dir(repo_root)
            state_dir.mkdir(parents=True, exist_ok=True)
            _port_path(repo_root).write_text(str(port))
            print(f"spec web running on http://{probe_host}:{port}", file=sys.stderr)
            print(f"Authenticated URL: {auth_url}", file=sys.stderr)
            if open_browser:
                import webbrowser

                webbrowser.open(auth_url)
            return 0
        # Child — detach fully so we don't hold the caller's terminal or
        # stdio pipe open (which would make `spec web start --background`
        # appear to hang until the daemon exits).  After setsid(), redirect
        # the inherited stdin/stdout/stderr file descriptors: stdin from
        # /dev/null, stdout/stderr to a rotating-ish server.log.  dup2 on the
        # underlying fds also captures uvicorn's C-level/native writes.
        os.setsid()
        try:
            log_dir = _web_state_dir(repo_root)
            log_dir.mkdir(parents=True, exist_ok=True)
            devnull_fd = os.open(os.devnull, os.O_RDONLY)
            # Private (0o600) — uvicorn access logs redirected here can contain
            # the `/?token=...` auth URL, so the log must not be world-readable
            # even though the token file itself is chmod 600.
            log_path = log_dir / "server.log"
            log_fd = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            # Enforce private mode even if the file pre-existed with looser bits
            # (O_CREAT does not tighten an existing file's permissions).
            try:
                os.fchmod(log_fd, 0o600)
            except OSError:
                pass
            os.dup2(devnull_fd, 0)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            if devnull_fd > 2:
                os.close(devnull_fd)
            if log_fd > 2:
                os.close(log_fd)
        except OSError:
            # Best-effort: even if redirection fails, continue running the
            # daemon rather than aborting the start.
            pass
    else:
        # Foreground: defer the success banner until after the socket binds.
        pass

    write_pid(repo_root, port=port)

    uvi_log_level = "debug" if verbose else "info"

    try:
        import uvicorn

        app = create_app(repo_root, token, port=port)

        if not background:
            # Use uvicorn.Server directly so we can hook into startup()
            # and print the banner *after* the socket is bound.  The
            # lifespan "startup" event fires *before* bind, so it is
            # too early for the banner / browser open.
            config = uvicorn.Config(app, host=host, port=port, log_level=uvi_log_level, proxy_headers=True, forwarded_allow_ips="*")
            server = uvicorn.Server(config)
            _orig_startup = server.startup

            async def _startup_then_banner(**kw: object) -> None:
                await _orig_startup(**kw)
                if server.started:
                    _publish_ready_record(
                        repo_root,
                        nonce=os.environ.get("SPEC_WEB_READY_NONCE", ""),
                        host=host,
                        port=port,
                    )
                    print(
                        f"spec web running on http://{probe_host}:{port}",
                        file=sys.stderr,
                    )
                    print(f"Authenticated URL: {auth_url}", file=sys.stderr)
                    if open_browser:
                        import webbrowser

                        webbrowser.open(auth_url)

            server.startup = _startup_then_banner  # type: ignore[assignment]
            try:
                server.run()
            except KeyboardInterrupt:
                # Uvicorn versions differ on whether Server.run() consumes
                # SIGINT. A normal foreground Ctrl-C is a clean operator stop,
                # not an application failure or traceback-worthy condition.
                pass
        else:
            uvicorn.run(app, host=host, port=port, log_level=uvi_log_level, proxy_headers=True, forwarded_allow_ips="*")
    finally:
        # Only remove PID file if it still refers to this process — avoids
        # deleting a file that a concurrent start may have written.
        current_pid, _ = read_pid(repo_root)
        if current_pid == os.getpid():
            remove_pid(repo_root)

    return 0


def stop_server(repo_root: Path) -> int:
    running, pid = is_server_running(repo_root)
    if not running or pid is None:
        print("spec web is not running.", file=sys.stderr)
        return 1
    from spec_runtime.process_supervisor import SupervisionToken, terminate

    supervision_token = read_supervision_token(repo_root)
    if supervision_token is None and os.name == "nt":
        supervision_token = _recover_launch(repo_root, readiness_timeout=0.0)
    if isinstance(supervision_token, SupervisionToken):
        if not terminate(supervision_token):
            print(
                f"spec web failed to stop owned process tree (pid {pid}); supervision state retained.",
                file=sys.stderr,
            )
            return 1
    else:
        if os.name == "nt":
            print(
                "spec web cannot stop safely because its Windows supervision token is missing.",
                file=sys.stderr,
            )
            return 1
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    remove_pid(repo_root)
    print(f"spec web stopped (pid {pid}).", file=sys.stderr)
    return 0


def server_status(repo_root: Path) -> int:
    running, pid = is_server_running(repo_root)
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
