"""Opt-in, credentialed Linux regression for real Claude web chat.

The default test suite runs only the hermetic SSE parser test below.  The live
test is deliberately gated by ``SPEC_LINUX_CLAUDE_REAL_PROVIDER=1`` because it
uses the operator's existing Claude authentication and consumes provider
capacity.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import pytest

from spec_runtime.process_supervisor import (
    LifetimeMode,
    ProcessIdentity,
    ProcessSupervisor,
    identity_matches,
    inspect_process,
    is_process_group_alive,
    list_live_process_group_members,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PROVIDER_ENV = "SPEC_LINUX_CLAUDE_REAL_PROVIDER"


class _ProofFailure(AssertionError):
    """A live protocol assertion failed without embedding response content."""


def _digest(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _parse_sse(lines: Iterable[str | bytes]) -> list[dict[str, Any]]:
    """Parse only the ``agent_event`` records used by the chat API."""
    events: list[dict[str, Any]] = []
    event_name = ""
    data_parts: list[str] = []
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else raw_line
        ).rstrip("\r\n")
        if not line:
            if event_name == "agent_event" and data_parts:
                value = json.loads("\n".join(data_parts))
                if not isinstance(value, dict):
                    raise _ProofFailure("SSE agent_event payload was not an object")
                events.append(value)
            event_name = ""
            data_parts = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_parts.append(value)

    if event_name == "agent_event" and data_parts:
        value = json.loads("\n".join(data_parts))
        if not isinstance(value, dict):
            raise _ProofFailure("SSE agent_event payload was not an object")
        events.append(value)
    return events


def test_linux_claude_real_provider_sse_parser_is_hermetic() -> None:
    assert _parse_sse(
        [
            b": keepalive\r\n",
            b"\r\n",
            b"event: agent_event\r\n",
            b'data: {"kind":"text","text":"hello"}\r\n',
            b"\r\n",
            b"event: agent_event\n",
            b'data: {"kind":"done"}\n',
            b"\n",
        ]
    ) == [
        {"kind": "text", "text": "hello"},
        {"kind": "done"},
    ]


def _request(
    base_url: str,
    path: str,
    *,
    token: str,
    method: str = "GET",
    payload: object | None = None,
    timeout: float = 30.0,
):
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=data,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _json_request(*args: Any, **kwargs: Any) -> Any:
    try:
        with _request(*args, **kwargs) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        raise _ProofFailure(
            f"HTTP {exc.code} from {kwargs.get('method', 'GET')}; "
            f"response bytes={len(body)} sha256={_digest(body)}"
        ) from exc
    return json.loads(payload.decode("utf-8"))


def _read_turn(response: Any) -> list[dict[str, Any]]:
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" not in content_type:
        raise _ProofFailure(f"chat turn did not return SSE: {content_type!r}")

    events: list[dict[str, Any]] = []
    block: list[bytes] = []
    while True:
        line = response.readline()
        if not line:
            if block:
                events.extend(_parse_sse([*block, b"\n"]))
            break
        block.append(line)
        if line in (b"\n", b"\r\n"):
            parsed = _parse_sse(block)
            block = []
            events.extend(parsed)
            if any(event.get("kind") == "done" for event in parsed):
                break

    errors = [event for event in events if event.get("kind") == "error"]
    if errors:
        error_payload = json.dumps(errors, sort_keys=True, default=str)
        raise _ProofFailure(
            f"Claude returned {len(errors)} error event(s); "
            f"payload sha256={_digest(error_payload)}"
        )
    if not any(event.get("kind") == "done" for event in events):
        raise _ProofFailure("Claude SSE turn ended without a done event")
    return events


def _send_turn(
    base_url: str,
    token: str,
    session_id: str,
    prompt: str,
) -> list[dict[str, Any]]:
    with _request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/messages",
        token=token,
        method="POST",
        payload={"text": prompt},
        timeout=600,
    ) as response:
        return _read_turn(response)


def _assistant_text(base_url: str, token: str, session_id: str) -> str:
    history = _json_request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/history",
        token=token,
    )
    assistant_entries = [
        entry
        for entry in history.get("history", [])
        if isinstance(entry, dict) and entry.get("role") == "assistant"
    ]
    if not assistant_entries:
        raise _ProofFailure("chat history contained no assistant response")
    return "".join(
        str(event.get("text", ""))
        for event in assistant_entries[-1].get("events", [])
        if isinstance(event, dict) and event.get("kind") == "text"
    )


def _assert_contains(text: str, *markers: str, turn: int) -> None:
    missing = [marker for marker in markers if marker not in text]
    if missing:
        raise _ProofFailure(
            f"Claude turn {turn} lost {len(missing)} expected prior marker(s); "
            f"response chars={len(text)} sha256={_digest(text)}"
        )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise _ProofFailure(f"git {args[0]} failed with exit code {completed.returncode}")


def _create_repo(repo: Path) -> None:
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text(".spec-state/\n.worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("# Real Claude web regression fixture\n", encoding="utf-8")
    (repo / ".spec.toml").write_text(
        """base_ref = "HEAD"

[agents]
default = "claude"
review_default = "claude"
allowed = ["claude"]
""",
        encoding="utf-8",
    )
    _run_git(repo, "add", ".")
    _run_git(
        repo,
        "-c",
        "user.name=Spec Butler Real Provider Test",
        "-c",
        "user.email=real-provider@example.invalid",
        "commit",
        "-m",
        "Create real Claude web regression fixture",
    )


def _server_env(repo: Path, control_root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("SPEC_")}
    existing_pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (str(REPO_ROOT / "src"), existing_pythonpath)
                if part
            ),
            "SPEC_CONFIG": str(repo / ".spec.toml"),
            "SPEC_NO_UPDATE_CHECK": "1",
            "SPEC_PROCESS_CONTROL_ROOT": str(control_root),
        }
    )
    return env


def _wait_for_server(base_url: str, token: str, process: object) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            raise _ProofFailure("web server exited before readiness")
        try:
            value = _json_request(
                base_url,
                "/api/v1/chat/backends",
                token=token,
            )
            if isinstance(value, dict):
                return value
        except (OSError, urllib.error.URLError, _ProofFailure):
            pass
        time.sleep(0.1)
    raise _ProofFailure("web server did not reach authenticated readiness")


def _new_provider_identities(
    pgid: int,
    baseline_pids: set[int],
) -> list[ProcessIdentity]:
    identities: list[ProcessIdentity] = []
    for pid in list_live_process_group_members(pgid) or []:
        if pid in baseline_pids:
            continue
        identity = inspect_process(pid)
        if identity is not None:
            identities.append(identity)
    return identities


def _wait_for_identities_exit(identities: list[ProcessIdentity], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(identity_matches(item) for item in identities):
        time.sleep(0.05)
    remaining = sum(1 for item in identities if identity_matches(item))
    if remaining:
        raise _ProofFailure(f"{remaining} exact Claude provider process identity/identities survived stop")


def _wait_for_group_exit(pgid: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_process_group_alive(pgid):
        time.sleep(0.05)
    if is_process_group_alive(pgid):
        raise _ProofFailure("web server process group survived shutdown")


@pytest.mark.linux_claude_real_provider
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="credentialed Claude web proof requires the supported Linux host sandbox",
)
@pytest.mark.skipif(
    os.environ.get(REAL_PROVIDER_ENV) != "1",
    reason=f"set {REAL_PROVIDER_ENV}=1 to opt into the credentialed real-Claude web proof",
)
def test_linux_real_claude_web_chat_preserves_context_and_reaps_provider(
    tmp_path: Path,
) -> None:
    """Use the real HTTP/SSE server and one persistent authenticated Claude client."""
    repo = tmp_path / "real Claude web repo"
    control_root = tmp_path / "process-controls"
    _create_repo(repo)
    token = secrets.token_urlsafe(32)
    token_path = repo / ".spec-state" / "web" / "auth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = _server_env(repo, control_root)
    sessions: list[str] = []
    managed = None
    stopped_cleanly = False

    # Foreground startup prints an authenticated URL containing the web token.
    # Discard all server/provider output so the credentialed proof never records
    # that token, inherited provider credentials, prompts, or model responses.
    with open(os.devnull, "w", encoding="utf-8") as discarded:
        try:
            managed = ProcessSupervisor(LifetimeMode.RUN_OWNED).spawn(
                [
                    sys.executable,
                    "-m",
                    "spec_runtime.cli",
                    "web",
                    "start",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=repo,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=discarded,
                stderr=discarded,
            )
        except BaseException:
            token_path.unlink(missing_ok=True)
            raise
        pgid = managed.token.pgid
        try:
            backends = _wait_for_server(base_url, token, managed)
            if not backends.get("backends", {}).get("claude"):
                raise _ProofFailure(
                    "real server did not expose Claude; verify Claude login plus bubblewrap/socat"
                )
            baseline_pids = set(list_live_process_group_members(pgid) or [])

            marker_one = f"CLAUDE-CONTEXT-A-{secrets.token_hex(8).upper()}"
            marker_two = f"CLAUDE-CONTEXT-B-{secrets.token_hex(8).upper()}"
            first_prompt = (
                f"Do not use tools or edit files. Memorize exact marker {marker_one}. "
                "Reply with ACK-ONE followed by that marker."
            )
            created = _json_request(
                base_url,
                "/api/v1/chat/sessions",
                token=token,
                method="POST",
                payload={"mode": "create", "agent": "claude", "prompt": first_prompt},
                timeout=120,
            )
            session_id = str(created.get("session_id", ""))
            if not session_id:
                raise _ProofFailure("Claude session creation returned no session id")
            sessions.append(session_id)

            _send_turn(base_url, token, session_id, first_prompt)
            first_text = _assistant_text(base_url, token, session_id)
            _assert_contains(first_text, marker_one, turn=1)

            second_prompt = (
                "Return the exact marker supplied only in my prior turn. Then memorize exact "
                f"second marker {marker_two}. Do not use tools or edit files."
            )
            _send_turn(base_url, token, session_id, second_prompt)
            second_text = _assistant_text(base_url, token, session_id)
            _assert_contains(second_text, marker_one, marker_two, turn=2)

            third_prompt = (
                "Return both exact markers learned in my two prior turns, in first-then-second "
                "order. Do not use tools or edit files."
            )
            _send_turn(base_url, token, session_id, third_prompt)
            third_text = _assistant_text(base_url, token, session_id)
            _assert_contains(third_text, marker_one, marker_two, turn=3)

            provider_identities = _new_provider_identities(pgid, baseline_pids)
            if not provider_identities:
                raise _ProofFailure("no live Claude provider child was observed in the server process group")
            _json_request(
                base_url,
                f"/api/v1/chat/sessions/{session_id}/stop",
                token=token,
                method="POST",
                payload={},
            )
            sessions.remove(session_id)
            _wait_for_identities_exit(provider_identities)
            remaining_members = set(list_live_process_group_members(pgid) or []) - baseline_pids
            if remaining_members:
                raise _ProofFailure(
                    f"{len(remaining_members)} provider process(es) remained after session stop"
                )

            stopped = subprocess.run(
                [sys.executable, "-m", "spec_runtime.cli", "web", "stop"],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if stopped.returncode != 0:
                raise _ProofFailure(f"spec web stop failed with exit code {stopped.returncode}")
            returncode = managed.wait(timeout=20)
            if returncode != 0:
                raise _ProofFailure(f"web server exited with status {returncode}")
            _wait_for_group_exit(pgid)
            stopped_cleanly = True
        finally:
            for session_id in reversed(sessions):
                try:
                    _json_request(
                        base_url,
                        f"/api/v1/chat/sessions/{session_id}/stop",
                        token=token,
                        method="POST",
                        payload={},
                    )
                except Exception:
                    pass
            if managed.poll() is None:
                try:
                    subprocess.run(
                        [sys.executable, "-m", "spec_runtime.cli", "web", "stop"],
                        cwd=repo,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if managed.poll() is None:
                managed.terminate(grace_seconds=0.1)
                try:
                    managed.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    managed.kill()
                    try:
                        managed.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
            token_path.unlink(missing_ok=True)

    assert stopped_cleanly
    assert not token_path.exists()
    assert not list(repo.rglob(".credentials.json"))
