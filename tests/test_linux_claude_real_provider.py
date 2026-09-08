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
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
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
CODEX_REAL_PROVIDER_ENV = "SPEC_LINUX_CODEX_REAL_PROVIDER"
RECEIPT_PATH_ENV = "SPEC_LINUX_CLAUDE_PROOF_RECEIPT"
EXPECTED_REVISION_ENV = "SPEC_LINUX_CLAUDE_EXPECTED_REVISION"
CHALLENGE_ENV = "SPEC_LINUX_CLAUDE_PROOF_CHALLENGE"
REAL_PROVIDER_TEST_NODE = (
    "tests/test_linux_claude_real_provider.py::"
    "test_linux_real_claude_web_chat_preserves_context_and_reaps_provider"
)
REVISION_RE = re.compile(r"[0-9a-f]{40}")


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


def _event_shape(events: list[dict[str, Any]]) -> str:
    """Describe provider activity without embedding prompts or model output."""
    counts: dict[str, int] = {}
    command_exit_codes: list[object] = []
    for event in events:
        kind = str(event.get("kind", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "command":
            command_exit_codes.append(event.get("exit_code"))
    return json.dumps(
        {"counts": counts, "command_exit_codes": command_exit_codes},
        sort_keys=True,
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


def _create_repo(repo: Path, *, agent: str = "claude") -> None:
    # Keep both the checkout and plain local-path origin whitespace-bearing.
    # Private Git accepts internal spaces in this shell-free representation,
    # while still rejecting raw whitespace in URL and SCP-like transports.
    remote = repo.parent / f"{repo.name}.git"
    _run_git(repo.parent, "init", "--bare", str(remote))
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text(".spec-state/\n.worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text(
        f"# Real {agent.title()} web regression fixture\n",
        encoding="utf-8",
    )
    (repo / ".spec.toml").write_text(
        f"""base_ref = "HEAD"

[agents]
default = "{agent}"
review_default = "{agent}"
allowed = ["{agent}"]
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
        f"Create real {agent.title()} web regression fixture",
    )
    _run_git(repo, "remote", "add", "origin", str(remote))


def test_real_provider_fixture_uses_a_sanitizable_origin_url(tmp_path: Path) -> None:
    repo = tmp_path / "real provider repo with spaces"
    _create_repo(repo)

    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )

    origin_url = completed.stdout.rstrip("\n")
    assert " " in origin_url

    from spec_runtime.agent_git_isolation import (
        cleanup_agent_git_isolation,
        prepare_agent_git_isolation,
    )

    worktree = tmp_path / "linked provider worktree"
    _run_git(repo, "worktree", "add", "-b", "provider-test", str(worktree))
    isolation = prepare_agent_git_isolation(worktree)
    try:
        assert isolation.origin_url == origin_url
    finally:
        cleanup_agent_git_isolation(isolation)


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


def _new_codex_app_server_identities(
    baseline: set[tuple[int, str]],
) -> list[ProcessIdentity]:
    """Find exact new Codex app-server identities across detached process groups."""
    identities: list[ProcessIdentity] = []
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ")
            pid = int(process_dir.name)
        except (OSError, ValueError):
            continue
        if b"codex" not in cmdline or b"app-server" not in cmdline:
            continue
        identity = inspect_process(pid)
        if identity is None or (identity.pid, identity.started_at) in baseline:
            continue
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


def _write_proof_receipt(payload: dict[str, Any]) -> None:
    """Write the runner's private receipt only from a clean exact checkout."""
    receipt_value = os.environ.get(RECEIPT_PATH_ENV)
    if not receipt_value:
        return
    expected_revision = os.environ.get(EXPECTED_REVISION_ENV, "").lower()
    challenge = os.environ.get(CHALLENGE_ENV, "")
    if not REVISION_RE.fullmatch(expected_revision) or not challenge:
        raise _ProofFailure("evidence runner did not supply exact proof provenance")
    receipt_path = Path(receipt_value)
    if not receipt_path.is_absolute() or not receipt_path.parent.is_dir():
        raise _ProofFailure("evidence runner supplied an invalid proof receipt path")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if revision.returncode != 0 or revision.stdout.strip().lower() != expected_revision:
        raise _ProofFailure("tested checkout does not match the requested proof revision")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        raise _ProofFailure("tested checkout changed while the real-provider proof ran")

    receipt = {
        **payload,
        "source_revision": expected_revision,
        "proof_test": REAL_PROVIDER_TEST_NODE,
        "run_challenge_sha256": _digest(challenge),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=receipt_path.parent,
            prefix=f".{receipt_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(receipt_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    user_state_root = tmp_path / "user-state"
    env = _server_env(repo, control_root)
    env["XDG_STATE_HOME"] = str(user_state_root)
    from spec_runtime.web.auth import _token_path, load_or_create_token

    with pytest.MonkeyPatch.context() as token_env:
        token_env.setenv("XDG_STATE_HOME", str(user_state_root))
        token = load_or_create_token(repo)
        token_path = _token_path(repo)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    sessions: list[str] = []
    managed = None
    stopped_cleanly = False
    provider_identities: list[ProcessIdentity] = []
    turn_1_marker_returned = False
    turn_2_retained_turn_1 = False
    turn_2_marker_returned = False
    turn_3_retained_turns_1_and_2 = False

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
            turn_1_marker_returned = True

            second_prompt = (
                "Return the exact marker supplied only in my prior turn. Then memorize exact "
                f"second marker {marker_two}. Do not use tools or edit files."
            )
            _send_turn(base_url, token, session_id, second_prompt)
            second_text = _assistant_text(base_url, token, session_id)
            _assert_contains(second_text, marker_one, marker_two, turn=2)
            turn_2_retained_turn_1 = True
            turn_2_marker_returned = True

            third_prompt = (
                "Return both exact markers learned in my two prior turns, in first-then-second "
                "order. Do not use tools or edit files."
            )
            _send_turn(base_url, token, session_id, third_prompt)
            third_text = _assistant_text(base_url, token, session_id)
            _assert_contains(third_text, marker_one, marker_two, turn=3)
            turn_3_retained_turns_1_and_2 = True

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
            # Uvicorn completes graceful shutdown, restores the prior handler,
            # and then deliberately re-raises the captured signal.  Both that
            # POSIX status and a server implementation that returns zero are
            # clean outcomes after the control-plane stop succeeded.
            if returncode not in {0, -signal.SIGTERM}:
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
    provider_processes_remaining = sum(
        1 for identity in provider_identities if identity_matches(identity)
    )
    assert provider_processes_remaining == 0
    assert not is_process_group_alive(pgid)
    _write_proof_receipt(
        {
            "status": "passed",
            "backend": "claude",
            "real_provider": True,
            "transport": "http-sse",
            "dependent_turns": 3,
            "turn_1_marker_returned": turn_1_marker_returned,
            "turn_2_retained_turn_1": turn_2_retained_turn_1,
            "turn_2_marker_returned": turn_2_marker_returned,
            "turn_3_retained_turns_1_and_2": turn_3_retained_turns_1_and_2,
            "provider_processes_observed": len(provider_identities),
            "provider_processes_remaining": provider_processes_remaining,
            "server_processes_remaining": 0,
            "server_stopped_cleanly": stopped_cleanly,
            "web_token_removed": not token_path.exists(),
            "credential_files_copied": len(list(repo.rglob(".credentials.json"))),
        }
    )


@pytest.mark.linux_codex_real_provider
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="credentialed Codex web proof requires the supported Linux sandbox",
)
@pytest.mark.skipif(
    os.environ.get(CODEX_REAL_PROVIDER_ENV) != "1",
    reason=f"set {CODEX_REAL_PROVIDER_ENV}=1 to opt into the credentialed real-Codex web proof",
)
def test_linux_real_codex_web_chat_edits_and_preserves_context(
    tmp_path: Path,
) -> None:
    """Exercise the real server, auth middleware, SSE, and persistent Codex thread."""
    repo = tmp_path / "real Codex web repo"
    control_root = tmp_path / "process-controls"
    _create_repo(repo, agent="codex")
    user_state_root = tmp_path / "user-state"
    env = _server_env(repo, control_root)
    env["XDG_STATE_HOME"] = str(user_state_root)

    from spec_runtime.web.auth import _token_path, load_or_create_token

    with pytest.MonkeyPatch.context() as token_env:
        token_env.setenv("XDG_STATE_HOME", str(user_state_root))
        token = load_or_create_token(repo)
        token_path = _token_path(repo)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    sessions: list[str] = []
    managed = None
    stopped_cleanly = False
    provider_identities: list[ProcessIdentity] = []
    provider_homes_root = (
        Path(env.get("HOME", str(Path.home())))
        / ".local"
        / "state"
        / "specbutler"
        / "provider-homes"
    )
    codex_homes_before = set(provider_homes_root.glob("spec-codex-home-*"))

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
            if not backends.get("backends", {}).get("codex"):
                raise _ProofFailure(
                    "real server did not expose Codex; verify Codex login and CLI version"
                )
            baseline_codex = {
                (identity.pid, identity.started_at)
                for identity in _new_codex_app_server_identities(set())
            }
            context_marker = f"CODEX-CONTEXT-ONLY-{secrets.token_hex(8).upper()}"
            edit_marker = f"CODEX-WORKSPACE-EDIT-{secrets.token_hex(8).upper()}"
            first_prompt = (
                "Do not use tools or edit files. Memorize this exact context-only marker: "
                f"{context_marker}. Reply with the exact marker."
            )
            created = _json_request(
                base_url,
                "/api/v1/chat/sessions",
                token=token,
                method="POST",
                payload={"mode": "create", "agent": "codex", "prompt": first_prompt},
                timeout=120,
            )
            session_id = str(created.get("session_id", ""))
            if not session_id:
                raise _ProofFailure("Codex session creation returned no session id")
            sessions.append(session_id)

            turn_events = [_send_turn(base_url, token, session_id, first_prompt)]
            codex_texts = [_assistant_text(base_url, token, session_id)]
            _assert_contains(
                codex_texts[-1],
                context_marker,
                turn=1,
            )
            second_prompt = (
                "Recall the context-only marker from our prior turn without asking me to "
                f"repeat it. Create notes.txt containing exactly {edit_marker} followed by "
                "a newline. Reply with both markers. Never write the context-only marker "
                "to a file."
            )
            turn_events.append(_send_turn(base_url, token, session_id, second_prompt))
            codex_texts.append(_assistant_text(base_url, token, session_id))
            _assert_contains(
                codex_texts[-1],
                context_marker,
                edit_marker,
                turn=2,
            )
            third_prompt = (
                "Read notes.txt for the workspace marker and recall the context-only marker "
                "from our conversation. Reply with both markers in context-then-workspace "
                "order. Do not write the context-only marker to disk."
            )
            turn_events.append(_send_turn(base_url, token, session_id, third_prompt))
            codex_texts.append(_assistant_text(base_url, token, session_id))
            _assert_contains(
                codex_texts[-1],
                context_marker,
                edit_marker,
                turn=3,
            )

            provider_identities = _new_codex_app_server_identities(baseline_codex)
            if not provider_identities:
                raise _ProofFailure("no live Codex provider child was observed")
            stopped_session = _json_request(
                base_url,
                f"/api/v1/chat/sessions/{session_id}/stop",
                token=token,
                method="POST",
                payload={},
            )
            sessions.remove(session_id)
            worktree = Path(str(stopped_session.get("worktree", "")))
            if not worktree.is_dir():
                raise _ProofFailure("Codex stop response omitted the session worktree")
            notes_path = worktree / "notes.txt"
            if not notes_path.is_file():
                raise _ProofFailure(
                    "Codex session created no notes.txt; provider event shapes="
                    + json.dumps([_event_shape(events) for events in turn_events])
                )
            content = notes_path.read_text(encoding="utf-8")
            if edit_marker not in content:
                raise _ProofFailure("Codex session did not preserve its workspace edit")
            if context_marker in content:
                raise _ProofFailure(
                    "Codex wrote the context-only marker to disk, invalidating the context proof"
                )
            _wait_for_identities_exit(provider_identities)

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
                raise _ProofFailure(
                    f"spec web stop failed with exit code {stopped.returncode}"
                )
            returncode = managed.wait(timeout=20)
            if returncode not in {0, -signal.SIGTERM}:
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
    assert not list(repo.rglob("auth.json"))
    assert not list(repo.rglob(".credentials.json"))
    assert set(provider_homes_root.glob("spec-codex-home-*")) == codex_homes_before
    assert all(not identity_matches(identity) for identity in provider_identities)
    assert not is_process_group_alive(pgid)
