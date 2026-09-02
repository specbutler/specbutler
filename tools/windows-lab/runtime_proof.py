#!/usr/bin/env python3
"""Live Windows release-proof probes for web chat and process cleanup.

The PowerShell controller owns repository/provider setup.  This helper keeps
the protocol assertions in portable, unit-testable Python and deliberately
uses only the standard library plus the candidate wheel under test.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable


class ProofFailure(RuntimeError):
    """A release-proof invariant was not observed."""


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _source_revision(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise ProofFailure("source revision must be an exact 40-character Git SHA")
    return normalized


def _request(
    base_url: str,
    path: str,
    *,
    token: str = "",
    method: str = "GET",
    payload: object | None = None,
    timeout: float = 30.0,
):
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(base_url + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _json_request(*args: Any, **kwargs: Any) -> Any:
    with _request(*args, **kwargs) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_sse(lines: Iterable[bytes | str]) -> list[dict[str, Any]]:
    """Parse Spec Butler's agent_event SSE records.

    Kept as a small public seam so the checked-in harness can be tested without
    a provider or a listening server.
    """
    events: list[dict[str, Any]] = []
    data_parts: list[str] = []
    event_name = ""
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r\n")
        if not line:
            if event_name == "agent_event" and data_parts:
                decoded = json.loads("\n".join(data_parts))
                if not isinstance(decoded, dict):
                    raise ProofFailure("SSE agent_event payload is not an object")
                events.append(decoded)
            data_parts = []
            event_name = ""
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
        decoded = json.loads("\n".join(data_parts))
        if not isinstance(decoded, dict):
            raise ProofFailure("SSE agent_event payload is not an object")
        events.append(decoded)
    return events


def _read_sse(response: Any, *, stop_after: int | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    block: list[bytes] = []
    while True:
        line = response.readline()
        if not line:
            if block:
                events.extend(parse_sse(block + [b"\n"]))
            return events
        block.append(line)
        if line in (b"\n", b"\r\n"):
            parsed = parse_sse(block)
            block = []
            events.extend(parsed)
            if stop_after is not None and len(events) >= stop_after:
                return events
            if any(event.get("kind") == "done" for event in parsed):
                return events


def _sse_message(
    base_url: str,
    token: str,
    session_id: str,
    text: str,
    *,
    stop_after: int | None = None,
) -> list[dict[str, Any]]:
    with _request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/messages",
        token=token,
        method="POST",
        payload={"text": text},
        timeout=600,
    ) as response:
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" not in content_type:
            raise ProofFailure(f"chat message did not return SSE: {content_type}")
        return _read_sse(response, stop_after=stop_after)


def _sse_reconnect(
    base_url: str,
    token: str,
    session_id: str,
    from_index: int,
) -> list[dict[str, Any]]:
    with _request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/stream?from={from_index}",
        token=token,
        timeout=600,
    ) as response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise ProofFailure("chat reconnect did not return SSE")
        return _read_sse(response)


def _history(base_url: str, token: str, session_id: str) -> dict[str, Any]:
    value = _json_request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/history",
        token=token,
    )
    if not isinstance(value, dict):
        raise ProofFailure("chat history was not an object")
    return value


def _wait_turn(base_url: str, token: str, session_id: str, timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _history(base_url, token, session_id)
        if not value.get("turn_active"):
            return value
        time.sleep(0.25)
    raise ProofFailure(f"chat turn timed out for {session_id}")


def _assistant_text(history: dict[str, Any]) -> str:
    entries = [item for item in history.get("history", []) if item.get("role") == "assistant"]
    if not entries:
        return ""
    return "".join(
        str(event.get("text", ""))
        for event in entries[-1].get("events", [])
        if event.get("kind") == "text"
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _create_session(base_url: str, token: str, prompt: str) -> str:
    value = _json_request(
        base_url,
        "/api/v1/chat/sessions",
        token=token,
        method="POST",
        payload={"mode": "create", "agent": "codex", "prompt": prompt},
        timeout=90,
    )
    session_id = str(value.get("session_id", "")) if isinstance(value, dict) else ""
    if not session_id:
        raise ProofFailure("Codex chat session did not return a session id")
    return session_id


def _stop_session(base_url: str, token: str, session_id: str) -> None:
    _json_request(
        base_url,
        f"/api/v1/chat/sessions/{session_id}/stop",
        token=token,
        method="POST",
        payload={},
        timeout=30,
    )


def _tree_identities(root_pid: int) -> dict[tuple[int, str], object]:
    from spec_runtime.process_supervisor import _windows_tree_identities

    return {
        (identity.pid, identity.started_at): identity
        for identity in _windows_tree_identities(root_pid)
        if identity.pid != root_pid
    }


def _identity_is_live(identity: object) -> bool:
    from spec_runtime.process_supervisor import identity_matches

    return bool(identity_matches(identity))


def prove_chat(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    evidence_root = Path(args.evidence_root)
    sessions: list[str] = []
    event_log: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "status": "failed",
        "transport": "authenticated HTTP with server-sent event streaming",
    }
    revision = _source_revision(args.source_revision)
    if revision is not None:
        result["source_revision"] = revision
    try:
        try:
            _json_request(base_url, "/api/v1/chat/backends")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            result["unauthenticated_status"] = 401
        else:
            raise ProofFailure("unauthenticated API request did not return 401")

        backends = _json_request(base_url, "/api/v1/chat/backends", token=token)
        if not backends.get("backends", {}).get("codex"):
            raise ProofFailure("native Codex web backend is unavailable")
        if backends.get("backends", {}).get("claude"):
            raise ProofFailure("native Claude web backend did not fail closed")
        try:
            _json_request(
                base_url,
                "/api/v1/chat/sessions",
                token=token,
                method="POST",
                payload={
                    "mode": "create",
                    "agent": "claude",
                    "prompt": "Native backend availability proof; do not start a session.",
                },
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
            error_payload = json.loads(exc.read().decode("utf-8"))
            claude_reason = str(error_payload.get("error", ""))
        else:
            raise ProofFailure("native Claude session did not fail closed")
        if "not available" not in claude_reason.lower() or not any(
            term in claude_reason.lower()
            for term in ("sandbox", "install", "authenticate", "allowed")
        ):
            raise ProofFailure("native Claude failure had no precise actionable explanation")

        marker_one = f"CTX-A-{uuid.uuid4().hex[:12].upper()}"
        marker_two = f"CTX-B-{uuid.uuid4().hex[:12].upper()}"
        first_prompt = (
            f"Do not edit files. Remember exact marker {marker_one}. "
            "Reply with ACK and the marker only."
        )
        primary = _create_session(base_url, token, first_prompt)
        sessions.append(primary)
        first_events = _sse_message(base_url, token, primary, first_prompt)
        event_log.extend({"session": "primary", "turn": 1, **event} for event in first_events)
        first_history = _wait_turn(base_url, token, primary)
        first_text = _assistant_text(first_history)
        if marker_one.lower() not in first_text.lower():
            raise ProofFailure("first Codex turn did not acknowledge its marker")

        second_prompt = (
            f"State the exact marker from my prior turn, then remember {marker_two}. "
            "Begin with RECONNECT-READY and do not edit files."
        )
        partial = _sse_message(
            base_url,
            token,
            primary,
            second_prompt,
            stop_after=1,
        )
        if not partial:
            raise ProofFailure("second turn produced no event before reconnect")
        event_log.extend({"session": "primary", "turn": 2, "connection": "initial", **event} for event in partial)
        live_history = _history(base_url, token, primary)
        replay_offset = int(live_history.get("live_event_count", 0))
        replay = _sse_reconnect(base_url, token, primary, replay_offset)
        if not replay or not any(event.get("kind") == "done" for event in replay):
            raise ProofFailure("reattached stream did not run through turn completion")
        event_log.extend({"session": "primary", "turn": 2, "connection": "reattached", **event} for event in replay)
        second_history = _wait_turn(base_url, token, primary)
        second_text = _assistant_text(second_history)
        if marker_one.lower() not in second_text.lower():
            raise ProofFailure("second Codex turn lost first-turn context")

        third_prompt = (
            "Reply with both exact markers learned in the two prior turns, "
            "in first-then-second order. Do not edit files."
        )
        third_events = _sse_message(base_url, token, primary, third_prompt)
        event_log.extend({"session": "primary", "turn": 3, **event} for event in third_events)
        third_history = _wait_turn(base_url, token, primary)
        third_text = _assistant_text(third_history)
        lowered_third = third_text.lower()
        if marker_one.lower() not in lowered_third or marker_two.lower() not in lowered_third:
            raise ProofFailure("third Codex turn did not retain both earlier-turn facts")

        isolated_markers = [
            f"ISOLATED-X-{uuid.uuid4().hex[:10].upper()}",
            f"ISOLATED-Y-{uuid.uuid4().hex[:10].upper()}",
        ]
        isolated_prompts = [
            f"Remember only {marker}. Reply ACK. Do not edit files."
            for marker in isolated_markers
        ]
        isolated_sessions = [
            _create_session(base_url, token, prompt)
            for prompt in isolated_prompts
        ]
        sessions.extend(isolated_sessions)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            initial_futures = [
                executor.submit(_sse_message, base_url, token, session, prompt)
                for session, prompt in zip(isolated_sessions, isolated_prompts, strict=True)
            ]
            for index, future in enumerate(initial_futures):
                event_log.extend(
                    {"session": f"isolated-{index}", "turn": 1, **event}
                    for event in future.result(timeout=600)
                )
        for session in isolated_sessions:
            _wait_turn(base_url, token, session)
        recall_prompt = "What exact isolated marker did I give you? Return only that marker."
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            recall_futures = [
                executor.submit(_sse_message, base_url, token, session, recall_prompt)
                for session in isolated_sessions
            ]
            for index, future in enumerate(recall_futures):
                event_log.extend(
                    {"session": f"isolated-{index}", "turn": 2, **event}
                    for event in future.result(timeout=600)
                )
        isolated_texts = [
            _assistant_text(_wait_turn(base_url, token, session))
            for session in isolated_sessions
        ]
        for index, text in enumerate(isolated_texts):
            own = isolated_markers[index].lower()
            other = isolated_markers[1 - index].lower()
            if own not in text.lower() or other in text.lower():
                raise ProofFailure(f"concurrent chat {index} leaked or lost session context")
        for session in isolated_sessions:
            _stop_session(base_url, token, session)

        baseline_tree = _tree_identities(args.server_pid)
        cancel_prompt = (
            "Do not edit files. Immediately run a Python child process that sleeps for "
            "300 seconds, wait for it, and only then reply CANCELLATION-FINISHED."
        )
        cancelled = _create_session(base_url, token, cancel_prompt)
        sessions.append(cancelled)
        cancellation_children: dict[tuple[int, str], object] = {}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = _tree_identities(args.server_pid)
            cancellation_children = {
                key: identity for key, identity in current.items() if key not in baseline_tree
            }
            history = _history(base_url, token, cancelled)
            if history.get("turn_active") and cancellation_children:
                break
            time.sleep(0.25)
        if not cancellation_children:
            raise ProofFailure("cancellation turn did not launch a provider child process")
        _stop_session(base_url, token, cancelled)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(
            _identity_is_live(identity) for identity in cancellation_children.values()
        ):
            time.sleep(0.1)
        leaked = [
            pid for (pid, _), identity in cancellation_children.items()
            if _identity_is_live(identity)
        ]
        if leaked:
            raise ProofFailure(f"chat cancellation leaked provider descendants: {leaked}")

        result.update(
            {
                "status": "passed",
                "backend": "codex",
                "native_claude_available": False,
                "native_claude_failed_closed": True,
                "native_claude_unavailability_reason": claude_reason,
                "dependent_turns": 3,
                "turn_2_retained_turn_1": True,
                "turn_3_retained_turns_1_and_2": True,
                "reconnect_endpoint_exercised": True,
                "reconnect_history_event_offset": replay_offset,
                "reconnect_replayed_event_count": len(replay),
                "concurrent_sessions": 2,
                "concurrent_sessions_isolated": True,
                "cancelled_descendant_count": len(cancellation_children),
                "cancelled_descendants_remaining": 0,
                "response_sha256": {
                    "turn_1": _digest(first_text),
                    "turn_2": _digest(second_text),
                    "turn_3": _digest(third_text),
                    "isolated_1": _digest(isolated_texts[0]),
                    "isolated_2": _digest(isolated_texts[1]),
                },
            }
        )
        return 0
    finally:
        for session_id in reversed(sessions):
            try:
                _stop_session(base_url, token, session_id)
            except Exception:
                pass
        _write_json(evidence_root / "web-chat-events.json", event_log)
        _write_json(evidence_root / "web-chat-result.json", result)


def prove_timeout_tree(args: argparse.Namespace) -> int:
    from spec_runtime.process_supervisor import inspect_process, run

    work_root = Path(args.work_root)
    evidence_root = Path(args.evidence_root)
    work_root.mkdir(parents=True, exist_ok=True)
    prefix = work_root / "timeout-tree-pid"
    script = work_root / "timeout-tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "level=int(sys.argv[1]); prefix=sys.argv[2]\n"
        "open(prefix+'-'+str(level),'w',encoding='utf-8').write(str(os.getpid()))\n"
        "child=None if level == 2 else subprocess.Popen([sys.executable,__file__,str(level+1),prefix])\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    timed_out = False
    try:
        run(
            [sys.executable, str(script), "0", str(prefix)],
            timeout=2,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    pids: list[int] = []
    for level in range(3):
        path = Path(f"{prefix}-{level}")
        if not path.exists():
            raise ProofFailure(f"timeout process level {level} did not start")
        pids.append(int(path.read_text(encoding="utf-8")))
    remaining = [pid for pid in pids if inspect_process(pid) is not None]
    result = {
        "status": "passed" if timed_out and not remaining else "failed",
        "timeout_observed": timed_out,
        "tree_depth": 3,
        "processes_remaining": remaining,
    }
    revision = _source_revision(args.source_revision)
    if revision is not None:
        result["source_revision"] = revision
    _write_json(evidence_root / "timeout-tree-result.json", result)
    if result["status"] != "passed":
        raise ProofFailure(f"timeout cleanup failed: {result}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat = subparsers.add_parser("chat")
    chat.add_argument("--base-url", required=True)
    chat.add_argument("--token-file", required=True)
    chat.add_argument("--evidence-root", required=True)
    chat.add_argument("--server-pid", required=True, type=int)
    chat.add_argument("--source-revision")
    chat.set_defaults(function=prove_chat)
    timeout_tree = subparsers.add_parser("timeout-tree")
    timeout_tree.add_argument("--work-root", required=True)
    timeout_tree.add_argument("--evidence-root", required=True)
    timeout_tree.add_argument("--source-revision")
    timeout_tree.set_defaults(function=prove_timeout_tree)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.function(args))
    except (ProofFailure, urllib.error.URLError, http.client.HTTPException) as exc:
        print(f"runtime proof failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
