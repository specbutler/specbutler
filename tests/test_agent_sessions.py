"""Provider session persistence, resume, structured completion and attach.

The phase-level tests run the real implement launch path; only the provider
binary is swapped for a fake ``claude``/``codex`` script that records its argv
and emulates session storage, so resume and fallback behaviour is exercised
end to end without a model provider.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runtime import execution_backend as eb
from spec_runtime import orchestrator as orch
from spec_runtime.agent_adapter import (
    COMPLETION_SCHEMA,
    ClaudeAgent,
    CodexAgent,
    claude_session_unavailability_reason,
    codex_resume_unavailability_reason,
    provider_attach_command,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake provider binaries are POSIX scripts",
)

REAL_CLAUDE_SUPPORTS_STREAM_JSON = orch._claude_supports_stream_json
REAL_PROVIDER_SESSION_SUPPORT = orch._provider_session_support

SPEC_ID = "my-feature"
RUN_ID = "my-feature-20260101T000000"
OK_COMPLETION = {"status": "ok", "summary": "structured done", "question": None}


@pytest.fixture(autouse=True)
def _hermetic_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "require_host_agent_available", lambda _agent: None)
    monkeypatch.setattr(
        orch,
        "SPEC_RUNTIME_CONFIG",
        replace(
            orch.SPEC_RUNTIME_CONFIG,
            execution=replace(orch.SPEC_RUNTIME_CONFIG.execution, backend="worktree"),
        ),
    )
    monkeypatch.setattr(orch, "_prepare_agent_git_boundary", lambda _path: None)
    monkeypatch.setattr(orch, "_claude_credentials_preflight_error", lambda: "")
    monkeypatch.setattr(orch, "_claude_supports_stream_json", lambda *_probe_target: True)
    monkeypatch.setattr(orch, "_validate_codex_exec", lambda _run: True)
    monkeypatch.setattr(
        orch,
        "_provider_session_support",
        lambda _agent, *_probe_target: orch.ProviderSessionSupport(
            persist=True,
            resume=True,
            structured_output=True,
        ),
    )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    (work / "specs").mkdir(parents=True)
    (work / "specs" / f"{SPEC_ID}.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            id: {SPEC_ID}
            depends_on: []
            description: A test spec
            ---

            ## Acceptance Criteria
            - [ ] First criterion
            """
        ),
        encoding="utf-8",
    )
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git("init", cwd=work)
    _git("config", "user.email", "test@test.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    _git("push", "-u", "origin", "HEAD:refs/heads/master", cwd=work)
    # The operator's real Codex home and provider-home state must never be
    # read or written.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "operator-codex-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    return work


class FakeProvider:
    """A fake provider CLI that records argv and emulates session storage.

    ``scenario.json`` controls the final turn: ``structured`` is the
    structured completion to return, and ``report`` makes the fake run the
    ``spec report`` fallback by writing the scoped completion outbox.
    ``transcripts`` makes Claude keep sessions only where the real CLI does,
    under its provider home, so resume depends on that home's contents.
    ``hold`` makes Codex wait, with a half-written rollout record, until the
    ``release`` file appears. ``delay`` sleeps between the ``spec report``
    fallback and the final structured response.
    """

    def __init__(self, root: Path, agent: str) -> None:
        self.agent = agent
        self.state = root / f"fake-{agent}-state"
        self.state.mkdir(parents=True)
        self.binary = root / "bin" / agent
        self.binary.parent.mkdir(exist_ok=True)
        self.binary.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(_FAKE_PROVIDER_BODY).replace("@STATE@", repr(str(self.state)))
            .replace("@AGENT@", repr(agent)),
            encoding="utf-8",
        )
        self.binary.chmod(0o755)
        self.scenario(structured=OK_COMPLETION)

    def scenario(
        self,
        *,
        structured: dict | None = None,
        report: bool = False,
        transcripts: bool = False,
        hold: bool = False,
        delay: float = 0.0,
    ) -> None:
        (self.state / "scenario.json").write_text(
            json.dumps(
                {
                    "structured": structured,
                    "report": report,
                    "transcripts": transcripts,
                    "hold": hold,
                    "delay": delay,
                }
            ),
            encoding="utf-8",
        )

    def forget_sessions(self) -> None:
        (self.state / "sessions.json").unlink(missing_ok=True)

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


_FAKE_PROVIDER_BODY = """\
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

STATE = Path(@STATE@)
AGENT = @AGENT@
args = sys.argv[1:]
with (STATE / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
scenario = json.loads((STATE / "scenario.json").read_text(encoding="utf-8"))
known_path = STATE / "sessions.json"
known = json.loads(known_path.read_text(encoding="utf-8")) if known_path.exists() else []

if AGENT == "claude":
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(os.environ["HOME"]) / ".claude")
    project_dir = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    if "--resume" in args:
        sid = args[args.index("--resume") + 1]
        found = (
            (project_dir / f"{sid}.jsonl").is_file()
            if scenario.get("transcripts")
            else sid in known
        )
        if not found:
            print(f"No conversation found with session ID: {sid}", file=sys.stderr)
            # Like the real CLI (2.1.x), stream-json mode still emits an error
            # result that echoes the id it was asked to resume.
            print(json.dumps({
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "session_id": sid,
            }), flush=True)
            raise SystemExit(1)
    else:
        sid = args[args.index("--session-id") + 1] if "--session-id" in args else ""
    if sid and scenario.get("transcripts"):
        project_dir.mkdir(parents=True, exist_ok=True)
        with (project_dir / f"{sid}.jsonl").open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps({"sessionId": sid}) + "\\n")
else:
    sessions_dir = Path(os.environ["CODEX_HOME"]) / "sessions"
    if "resume" in args:
        sid = args[-2]
        if not (sessions_dir / f"rollout-{sid}.jsonl").exists():
            print(f"Error: thread/resume failed: no rollout found for {sid}", file=sys.stderr)
            raise SystemExit(1)
    else:
        sid = str(uuid.uuid4())
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"rollout-{sid}.jsonl").write_text("{}\\n", encoding="utf-8")
    if scenario.get("hold"):
        rollout = sessions_dir / f"rollout-{sid}.jsonl"
        with rollout.open("a", encoding="utf-8") as stream:
            stream.write('{"partial')
        (STATE / "holding").write_text(sid, encoding="utf-8")
        deadline = time.monotonic() + 60
        while not (STATE / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        rollout.write_text("{}\\n{}\\n", encoding="utf-8")
if sid and sid not in known:
    known.append(sid)
    known_path.write_text(json.dumps(known), encoding="utf-8")

if scenario.get("report"):
    outbox = Path(os.environ["SPEC_COMPLETION_OUTBOX"])
    outbox.write_text(json.dumps({
        "artifact": "spec-agent-completion-report",
        "version": 1,
        "spec_id": os.environ["SPEC_ID"],
        "run_id": os.environ["SPEC_RUN_ID"],
        "implement_result": {
            "status": "passed",
            "summary": "reported via spec report",
            "commits": [],
            "attempt": int(os.environ["SPEC_ATTEMPT"]),
            "launch_number": int(os.environ["SPEC_IMPLEMENT_LAUNCH"]),
            "result_source": "agent_report_outbox",
            "completed_at": "2026-01-01T00:00:00Z",
        },
    }), encoding="utf-8")
time.sleep(scenario.get("delay") or 0)

structured = scenario.get("structured")
if AGENT == "claude":
    if sid:
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    result = {"type": "result", "subtype": "success", "num_turns": 1}
    if sid:
        result["session_id"] = sid
    if structured is not None and "--json-schema" in args:
        result["structured_output"] = structured
    print(json.dumps(result), flush=True)
else:
    print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)
    text = json.dumps(structured) if structured is not None and "--output-schema" in args else "done"
    print(json.dumps({
        "type": "item.completed",
        "item": {"id": "msg", "type": "agent_message", "text": text},
    }), flush=True)
    print(json.dumps({"type": "turn.completed"}), flush=True)
"""


def _use_fake_binary(monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> None:
    """Build the real adapter command, then point argv[0] at the fake CLI."""
    real_build = orch._build_agent_command

    def build(*args, **kwargs):
        cmd = real_build(*args, **kwargs)
        assert cmd[0] == fake.agent
        return [str(fake.binary), *cmd[1:]]

    monkeypatch.setattr(orch, "_build_agent_command", build)


def _make_run(repo: Path, agent: str) -> tuple[orch.RunState, Path]:
    run = orch.RunState(
        run_id=RUN_ID,
        spec_id=SPEC_ID,
        branch=f"code/{SPEC_ID}--20260101T000000",
        agent=agent,
    )
    run.save(repo)
    worktree = repo / ".worktrees" / SPEC_ID
    worktree.mkdir(parents=True, exist_ok=True)
    return run, worktree


def _implement(run: orch.RunState, repo: Path, worktree: Path, **patches) -> str:
    with (
        patch.object(orch, "resolve_worktree_path", return_value=worktree),
        patch.object(orch, "_head_sha", return_value="abc123"),
        patch.object(orch, "_worktree_dirty_files", return_value=patches.get("dirty", [])),
        patch.object(orch, "_recent_commit_lines", return_value=["abc123 work"]),
    ):
        return orch.phase_implement(run, repo)


def _prepare_retry(run: orch.RunState, repo: Path) -> None:
    run.attempts += 1
    run.status = "running"
    run.save(repo)


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# ---------------------------------------------------------------------------
# Adapter command construction
# ---------------------------------------------------------------------------


def test_claude_implement_command_keeps_a_named_session(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(COMPLETION_SCHEMA), encoding="utf-8")
    cmd = ClaudeAgent().build_implement_command(
        prompt="Do the work",
        worktree_path=tmp_path,
        state_dir=tmp_path / ".state",
        session_id="11111111-1111-4111-8111-111111111111",
        completion_schema_path=schema,
    )
    assert "--no-session-persistence" not in cmd
    assert _flag_value(cmd, "--session-id") == "11111111-1111-4111-8111-111111111111"
    assert json.loads(_flag_value(cmd, "--json-schema")) == COMPLETION_SCHEMA
    assert cmd[-2:] == ["--", "Do the work"]


def test_claude_implement_command_resumes_without_new_session(tmp_path: Path) -> None:
    cmd = ClaudeAgent().build_implement_command(
        prompt="delta",
        worktree_path=tmp_path,
        state_dir=tmp_path / ".state",
        resume_session_id="sess-1",
        session_id="ignored",
    )
    assert _flag_value(cmd, "--resume") == "sess-1"
    assert "--session-id" not in cmd
    assert "--no-session-persistence" not in cmd


def test_claude_implement_command_without_session_stays_unpersisted(tmp_path: Path) -> None:
    cmd = ClaudeAgent().build_implement_command(
        prompt="p",
        worktree_path=tmp_path,
        state_dir=tmp_path / ".state",
    )
    assert "--no-session-persistence" in cmd
    assert "--json-schema" not in cmd


def test_codex_implement_command_resumes_thread_with_output_schema(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    cmd = CodexAgent().build_implement_command(
        prompt="delta",
        worktree_path=tmp_path,
        state_dir=tmp_path / ".state",
        resume_session_id="thread-1",
        completion_schema_path=schema,
    )
    exec_index = cmd.index("exec")
    assert cmd[exec_index + 1 : exec_index + 3] == ["resume", "--json"]
    assert _flag_value(cmd, "--output-schema") == str(schema)
    assert cmd[-2:] == ["thread-1", "delta"]


def test_codex_fresh_implement_command_has_no_resume(tmp_path: Path) -> None:
    cmd = CodexAgent().build_implement_command(
        prompt="p",
        worktree_path=tmp_path,
        state_dir=tmp_path / ".state",
        session_id="ignored",
    )
    assert "resume" not in cmd
    assert "--output-schema" not in cmd
    assert cmd[-1] == "p"


def test_session_capability_reasons() -> None:
    assert claude_session_unavailability_reason(
        "--session-id --resume --json-schema --fork-session"
    ) == ""
    assert "--json-schema" in claude_session_unavailability_reason("--session-id --resume")
    assert codex_resume_unavailability_reason("--json --output-schema") == ""
    assert "exec resume" in codex_resume_unavailability_reason("")


def test_completion_schema_is_strict() -> None:
    assert COMPLETION_SCHEMA["additionalProperties"] is False
    assert set(COMPLETION_SCHEMA["required"]) == {"status", "summary", "question"}
    assert COMPLETION_SCHEMA["properties"]["status"]["enum"] == [
        "ok",
        "blocked",
        "error",
        "needs-input",
    ]


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------


def test_claude_stream_captures_session_and_structured_output() -> None:
    tracker = orch.AgentProgressTracker(agent="claude")
    orch._handle_claude_stream_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-9"}),
        tracker,
    )
    orch._handle_claude_stream_line(
        json.dumps(
            {
                "type": "result",
                "session_id": "sess-9",
                "structured_output": {
                    "status": "needs-input",
                    "summary": "unclear",
                    "question": "Which API?",
                },
            }
        ),
        tracker,
    )
    assert tracker.provider_session_id() == "sess-9"
    assert tracker.structured_completion() == {
        "status": "needs-input",
        "summary": "unclear",
        "question": "Which API?",
    }


def test_codex_stream_captures_thread_and_final_message() -> None:
    tracker = orch.AgentProgressTracker(agent="codex")
    for event in (
        {"type": "thread.started", "thread_id": "thread-7"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "working"}},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(OK_COMPLETION)},
        },
    ):
        orch._handle_codex_stream_line(json.dumps(event), tracker)
    assert tracker.provider_session_id() == "thread-7"
    assert tracker.structured_completion() == {
        "status": "ok",
        "summary": "structured done",
        "question": "",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "done", "summary": "x", "question": None},
        {"status": "ok", "summary": 3, "question": None},
        {"status": "ok", "summary": "x", "question": 4},
        ["ok"],
    ],
)
def test_invalid_structured_output_is_ignored(payload: object) -> None:
    tracker = orch.AgentProgressTracker(agent="claude")
    tracker.record_structured_output(payload)
    assert tracker.structured_completion() is None


def test_tracker_persists_session_id_onto_run_state(repo: Path) -> None:
    run, _ = _make_run(repo, "codex")
    tracker = orch.AgentProgressTracker(agent="codex", run_id=run.run_id, repo_root=repo)
    tracker.record_provider_session("thread-live")
    saved = orch.RunState.load(repo, run.run_id)
    assert saved.agent_session_id == "thread-live"
    assert saved.agent_session_agent == "codex"


# ---------------------------------------------------------------------------
# Claude: one session per run
# ---------------------------------------------------------------------------


def test_claude_run_keeps_one_session_and_resumes_with_delta_prompt(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "passed"
    first = fake.calls()[-1]
    session_id = _flag_value(first, "--session-id")
    assert "--no-session-persistence" not in first
    assert "--json-schema" in first
    assert "Exit Checklist:" in first[-1]
    assert run.agent_session_id == session_id
    assert orch.RunState.load(repo, run.run_id).agent_session_id == session_id
    result = orch.ImplementResult.load(repo, run.run_id)
    assert result is not None
    assert result.result_source == "structured_output"
    assert result.summary == "structured done"

    _prepare_retry(run, repo)
    orch._record_operator_steering(
        repo,
        run,
        "Prefer the smallest fix.",
        source="spec steer",
    )
    assert _implement(run, repo, worktree) == "passed"
    retry = fake.calls()[-1]
    assert _flag_value(retry, "--resume") == session_id
    assert "--session-id" not in retry
    prompt = retry[-1]
    assert prompt.startswith(orch.RESUMED_SESSION_PREAMBLE)
    assert "Exit Checklist:" not in prompt
    assert "Retry failure package:" in prompt
    # Steering arrives through the resumed session at the attempt boundary.
    assert "Prefer the smallest fix." in prompt
    assert run.agent_session_id == session_id
    assert run.agent_session_fallback_reason == ""


def test_claude_resume_failure_falls_back_to_fresh_session(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    assert _implement(run, repo, worktree) == "passed"
    old_session = run.agent_session_id

    fake.forget_sessions()
    _prepare_retry(run, repo)
    with caplog.at_level("WARNING", logger=orch.logger.name):
        assert _implement(run, repo, worktree) == "passed"

    resumed, fresh = fake.calls()[-2:]
    assert _flag_value(resumed, "--resume") == old_session
    new_session = _flag_value(fresh, "--session-id")
    assert new_session != old_session
    # The fresh session gets the full prompt again.
    assert "Exit Checklist:" in fresh[-1]
    assert run.agent_session_id == new_session
    assert old_session in run.agent_session_fallback_reason
    assert "Falling back to a fresh claude session" in caplog.text
    assert any(
        warning.get("failure_subtype") == "resume_failed" for warning in run.nonfatal_warnings
    )


def test_resume_falls_back_when_changes_predate_the_launch(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unfinished edits (or an unresolved merge) must not pin a dead session."""
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("half done\n", encoding="utf-8")
    dirty = [" M src/app.py", "UU src/merge.py"]
    assert _implement(run, repo, worktree, dirty=dirty) == "passed"
    old_session = run.agent_session_id

    fake.forget_sessions()
    _prepare_retry(run, repo)
    assert _implement(run, repo, worktree, dirty=dirty) == "passed"

    resumed, fresh = fake.calls()[-2:]
    assert _flag_value(resumed, "--resume") == old_session
    assert _flag_value(fresh, "--session-id") == run.agent_session_id != old_session
    assert old_session in run.agent_session_fallback_reason


def test_worktree_fingerprint_tracks_edits_to_already_dirty_files(repo: Path) -> None:
    tracked = repo / "specs" / f"{SPEC_ID}.md"
    tracked.write_text("edit one\n", encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "todo.txt").write_text("a\n", encoding="utf-8")
    before = orch._worktree_change_fingerprint(repo)
    assert orch._worktree_change_fingerprint(repo) == before

    # Same dirty-file list, different content: the launch changed something.
    tracked.write_text("edit two\n", encoding="utf-8")
    assert orch._worktree_change_fingerprint(repo) != before
    edited = orch._worktree_change_fingerprint(repo)
    (repo / "notes" / "todo.txt").write_text("b\n", encoding="utf-8")
    assert orch._worktree_change_fingerprint(repo) != edited


def test_resume_with_new_workspace_changes_is_not_a_resume_failure(
    repo: Path,
    tmp_path: Path,
) -> None:
    run, worktree = _make_run(repo, "claude")
    plan = orch.ImplementLaunchPlan(
        use_stream_json=True,
        agent_env={},
        agent_cmd=["claude"],
        popen_kwargs={},
        progress_tracker=orch.AgentProgressTracker(agent="claude"),
        session=orch.AgentSessionPlan(resume_session_id="sess-1"),
    )
    with patch.object(orch, "_worktree_change_fingerprint", return_value="after"):
        assert orch._resume_failure_reason(run, plan, 1, worktree, "before") == ""
        assert "could not resume session sess-1" in orch._resume_failure_reason(
            run, plan, 1, worktree, "after"
        )


@pytest.mark.parametrize(
    ("event", "confirmed"),
    [
        ({"type": "system", "subtype": "init", "session_id": "s"}, True),
        ({"type": "result", "subtype": "success", "num_turns": 3, "session_id": "s"}, True),
        # A session that ran and then failed (for example at max turns) existed.
        (
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 4,
             "session_id": "s"},
            True,
        ),
        # `claude --resume` of a missing session echoes the requested id (#116).
        (
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "num_turns": 0, "session_id": "s"},
            False,
        ),
        ({"type": "result", "is_error": True, "session_id": "s"}, False),
        ({"type": "assistant", "session_id": "s"}, False),
        ({"type": "system", "subtype": "init"}, False),
    ],
)
def test_claude_event_confirms_session(event: dict, confirmed: bool) -> None:
    assert orch._claude_event_confirms_session(event) is confirmed


def test_missing_session_error_result_is_a_resume_failure(repo: Path) -> None:
    run, worktree = _make_run(repo, "claude")
    tracker = orch.AgentProgressTracker(agent="claude")
    orch._handle_claude_stream_line(
        json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 0,
            "session_id": "sess-1",
        }),
        tracker,
    )
    assert tracker.provider_session_id() == ""
    plan = orch.ImplementLaunchPlan(
        use_stream_json=True,
        agent_env={},
        agent_cmd=["claude"],
        popen_kwargs={},
        progress_tracker=tracker,
        session=orch.AgentSessionPlan(resume_session_id="sess-1"),
    )
    with patch.object(orch, "_worktree_change_fingerprint", return_value="same"):
        assert "could not resume session sess-1" in orch._resume_failure_reason(
            run, plan, 1, worktree, "same"
        )


def test_claude_spec_report_is_fallback_without_structured_output(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(structured=None, report=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "passed"
    result = orch.ImplementResult.load(repo, run.run_id)
    assert result is not None
    assert result.summary == "reported via spec report"


def test_structured_output_wins_over_spec_report(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(
        structured={"status": "blocked", "summary": "need creds", "question": None},
        report=True,
    )
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "blocked"
    assert run.last_error == "need creds"


def test_structured_needs_input_uses_question(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(
        structured={"status": "needs-input", "summary": "ambiguous", "question": "Option A or B?"},
    )
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "waiting-for-input"
    assert run.input_question == "Option A or B?"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_spec_report_does_not_cut_off_a_delayed_structured_response(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    """A fallback report must not end the launch before the final response."""
    fake = FakeProvider(tmp_path, agent)
    fake.scenario(
        structured={"status": "blocked", "summary": "need creds", "question": None},
        report=True,
        delay=1.0,
    )
    _use_fake_binary(monkeypatch, fake)
    monkeypatch.setattr(orch, "AGENT_EXIT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(orch, "AGENT_COMPLETION_POLL_SECONDS", 0.05)
    run, worktree = _make_run(repo, agent)

    assert _implement(run, repo, worktree) == "blocked"
    assert run.last_error == "need creds"
    result = orch.ImplementResult.load(repo, run.run_id)
    assert result is not None
    assert result.result_source == "structured_output"


def test_schema_launch_prompt_makes_spec_report_optional(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "passed"
    prompt = fake.calls()[-1][-1]
    assert orch.STRUCTURED_COMPLETION_INSTRUCTION in prompt
    assert "Wait for `Completion recorded for" not in prompt
    assert "Before exiting, report implement-phase status" not in prompt


def test_stateless_launch_prompt_still_requires_spec_report(tmp_path: Path) -> None:
    cmd = orch._build_agent_command("claude", tmp_path, spec_id=SPEC_ID)
    prompt = cmd[-1]
    assert f"Wait for `Completion recorded for {SPEC_ID}:`" in prompt
    assert orch.STRUCTURED_COMPLETION_INSTRUCTION not in prompt


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_first_attempt_resumes_session_after_operator_input(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    """Answering input keeps run.attempts at zero but continues the session."""
    fake = FakeProvider(tmp_path, agent)
    fake.scenario(
        structured={"status": "needs-input", "summary": "ambiguous", "question": "A or B?"},
    )
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, agent)
    assert _implement(run, repo, worktree) == "waiting-for-input"
    session_id = run.agent_session_id
    assert session_id

    orch.resolve_operator_request(repo, run, "Use option B.", source="spec input")
    assert run.attempts == 0
    fake.scenario(structured=OK_COMPLETION)
    assert _implement(run, repo, worktree) == "passed"

    resumed = fake.calls()[-1]
    if agent == "claude":
        assert _flag_value(resumed, "--resume") == session_id
        assert "--session-id" not in resumed
    else:
        assert resumed[resumed.index("exec") + 1] == "resume"
        assert resumed[-2] == session_id
    prompt = resumed[-1]
    assert prompt.startswith(orch.RESUMED_SESSION_PREAMBLE)
    assert "Use option B." in prompt
    assert "Exit Checklist:" not in prompt
    assert run.agent_session_id == session_id
    assert run.agent_session_fallback_reason == ""


def test_first_attempt_relaunch_falls_back_when_session_is_gone(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An infrastructure relaunch at attempt zero resumes, then falls back."""
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    assert _implement(run, repo, worktree) == "passed"
    old_session = run.agent_session_id

    fake.forget_sessions()
    run.status = "running"
    run.save(repo)
    assert run.attempts == 0
    assert _implement(run, repo, worktree) == "passed"

    resumed, fresh = fake.calls()[-2:]
    assert _flag_value(resumed, "--resume") == old_session
    assert _flag_value(fresh, "--session-id") == run.agent_session_id != old_session
    assert "Exit Checklist:" in fresh[-1]
    assert old_session in run.agent_session_fallback_reason


def test_no_handshake_recovery_runs_only_without_any_completion(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    recovery_calls: list[str] = []

    def recovery(*_args, **_kwargs):
        recovery_calls.append("called")
        return None

    monkeypatch.setattr(orch, "_attempt_no_handshake_recovery", recovery)

    assert _implement(run, repo, worktree, dirty=["src/app.py"]) == "passed"
    assert recovery_calls == []

    fake.scenario(structured=None)
    _prepare_retry(run, repo)
    assert _implement(run, repo, worktree, dirty=["src/app.py"]) == "failed"
    assert recovery_calls == ["called"]
    assert "no_handshake" in run.last_error


def test_unsupported_cli_keeps_stateless_launch(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orch,
        "_provider_session_support",
        lambda _agent, *_probe_target: orch.ProviderSessionSupport(),
    )
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(structured=None, report=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    assert _implement(run, repo, worktree) == "passed"
    argv = fake.calls()[-1]
    assert "--no-session-persistence" in argv
    assert "--session-id" not in argv
    assert "--json-schema" not in argv
    assert run.agent_session_id == ""


# ---------------------------------------------------------------------------
# Container workers: probe and stream through the backend
# ---------------------------------------------------------------------------

WORKER_CLAUDE_HELP = (
    "--output-format stream-json --session-id --resume --json-schema --fork-session"
)


class _ContainerIdentityBackend(eb.WorktreeExecutionBackend):
    """A backend reporting a container identity whose CLI probes are recorded.

    The agent still runs as a local process, so the phase sees exactly what a
    container worker's piped stdout would deliver.
    """

    def __init__(self, base: eb.WorktreeExecutionBackend) -> None:
        self.__dict__.update(base.__dict__)
        self.probes: list[list[str]] = []

    @property
    def identity(self) -> eb.BackendIdentity:
        return replace(super().identity, backend="container")

    def run_command(self, request: eb.CommandRequest) -> eb.CommandResult:
        if request.argv[:1] == ["claude"]:
            self.probes.append(list(request.argv))
            return eb.CommandResult(
                returncode=0,
                stdout=WORKER_CLAUDE_HELP,
                stderr="",
                argv=list(request.argv),
            )
        return super().run_command(request)


_ContainerIdentityBackend.__name__ = "WorktreeExecutionBackend"


def test_container_claude_launch_streams_structured_completion(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ContainerIdentityBackend(orch._resolve_execution_backend())
    monkeypatch.setattr(orch, "_resolve_execution_backend", lambda: backend)
    monkeypatch.setattr(orch, "_claude_supports_stream_json", REAL_CLAUDE_SUPPORTS_STREAM_JSON)
    monkeypatch.setattr(orch, "_provider_session_support", REAL_PROVIDER_SESSION_SUPPORT)

    real_run_subprocess = orch.run_subprocess

    def host_subprocess(cmd, *args, **kwargs):
        if cmd[:1] in (["claude"], ["codex"]):
            raise AssertionError("container capability probes must not run on the host")
        return real_run_subprocess(cmd, *args, **kwargs)

    monkeypatch.setattr(orch, "run_subprocess", host_subprocess)
    # The fake commits nothing; lost-work detection is covered elsewhere.
    monkeypatch.setattr(orch, "_detect_container_lost_implementation", lambda *_a, **_k: None)
    fake = FakeProvider(tmp_path, "claude")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    with patch.object(orch, "_worktree_dirty_files", return_value=[]):
        assert _implement(run, repo, worktree) == "passed"

    argv = fake.calls()[-1]
    assert _flag_value(argv, "--output-format") == "stream-json"
    assert "--json-schema" in argv
    assert run.agent_session_id == _flag_value(argv, "--session-id")
    assert ["claude", "--help"] in backend.probes
    result = orch.ImplementResult.load(repo, run.run_id)
    assert result is not None
    assert result.result_source == "structured_output"
    assert result.summary == "structured done"


class _IsolatedHomeContainerBackend(_ContainerIdentityBackend):
    """A container backend whose Claude launches use ``.spec-claude-home``."""


def _use_isolated_home_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _IsolatedHomeContainerBackend:
    backend = _IsolatedHomeContainerBackend(orch._resolve_execution_backend())
    monkeypatch.setattr(orch, "_resolve_execution_backend", lambda: backend)
    monkeypatch.setattr(orch, "_detect_container_lost_implementation", lambda *_a, **_k: None)
    # The operator's own Claude config directory, as seen by `spec attach`.
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return backend


def _claude_transcript(config_dir: Path, cwd: Path, session_id: str) -> Path:
    return (
        config_dir / "projects" / orch._claude_project_dir_name(cwd.resolve())
        / f"{session_id}.jsonl"
    )


def test_container_claude_session_survives_the_isolated_home(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The transcript lives only in the transient launch home; it is archived,
    restored for the retry's resume, and installed for the operator's attach."""
    _use_isolated_home_container(monkeypatch, tmp_path)
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(structured=OK_COMPLETION, transcripts=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")

    with patch.object(orch, "_worktree_dirty_files", return_value=[]):
        assert _implement(run, repo, worktree) == "passed"
    session_id = run.agent_session_id
    assert session_id == _flag_value(fake.calls()[-1], "--session-id")
    # The launch HOME was the isolated home, which is gone after the launch.
    assert not (worktree / ".spec-claude-home").exists()
    operator_config = tmp_path / "operator-home" / ".claude"
    assert not _claude_transcript(operator_config, worktree, session_id).exists()
    archive = orch._claude_session_archive_dir(repo, run.run_id)
    assert any(archive.glob(f"*/{session_id}.jsonl"))

    _prepare_retry(run, repo)
    with patch.object(orch, "_worktree_dirty_files", return_value=[]):
        assert _implement(run, repo, worktree) == "passed"
    retry = fake.calls()[-1]
    assert _flag_value(retry, "--resume") == session_id
    assert run.agent_session_id == session_id
    assert run.agent_session_fallback_reason == ""

    code, out, _ = _attach(repo, worktree, capsys, exec_=False)

    assert code == 0
    assert f"claude --resume {session_id}" in out
    installed = _claude_transcript(operator_config, worktree, session_id)
    # Both turns (initial and resumed) are in the installed transcript.
    assert len(installed.read_text(encoding="utf-8").splitlines()) == 2
    assert f"Restored Claude session {session_id}" in out


def test_reattach_keeps_interactive_claude_turns(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Turns the operator added after the first attach survive a second one."""
    _use_isolated_home_container(monkeypatch, tmp_path)
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(structured=OK_COMPLETION, transcripts=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    with patch.object(orch, "_worktree_dirty_files", return_value=[]):
        assert _implement(run, repo, worktree) == "passed"
    session_id = run.agent_session_id
    assert _attach(repo, worktree, capsys, exec_=False)[0] == 0
    installed = _claude_transcript(tmp_path / "operator-home" / ".claude", worktree, session_id)
    sidecar = installed.parent / session_id / "subagents" / "agent-1.jsonl"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{\"operator-subagent\": 1}\n", encoding="utf-8")
    interactive = installed.read_text(encoding="utf-8") + "{\"operator\": 1}\n"
    installed.write_text(interactive, encoding="utf-8")

    code, _, _ = _attach(repo, worktree, capsys, exec_=False)

    assert code == 0
    assert installed.read_text(encoding="utf-8") == interactive
    assert sidecar.read_text(encoding="utf-8") == "{\"operator-subagent\": 1}\n"


def test_attach_exec_resumes_container_claude_session_from_operator_home(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--exec` runs a Claude CLI that only searches the operator's home."""
    _use_isolated_home_container(monkeypatch, tmp_path)
    fake = FakeProvider(tmp_path, "claude")
    fake.scenario(structured=OK_COMPLETION, transcripts=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "claude")
    with patch.object(orch, "_worktree_dirty_files", return_value=[]):
        assert _implement(run, repo, worktree) == "passed"

    monkeypatch.setenv("PATH", f"{fake.binary.parent}{os.pathsep}{os.environ['PATH']}")
    code, _, err = _attach(repo, worktree, capsys, exec_=True)

    assert code == 0, err
    assert fake.calls()[-1] == ["--resume", run.agent_session_id]


def test_host_claude_attach_leaves_operator_config_alone(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Host launches write into the operator's config already; nothing to copy."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    run, worktree = _make_run(repo, "claude")
    run.agent_session_id = "sess-host"
    run.agent_session_agent = "claude"
    run.save(repo)

    code, out, _ = _attach(repo, worktree, capsys)

    assert code == 0
    assert "Restored" not in out
    assert not (tmp_path / "operator-home" / ".claude").exists()


def test_live_fork_snapshots_container_claude_transcript(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A running container launch's transcript is newer than any archive."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    run, worktree = _make_run(repo, "claude")
    run.agent_session_id = "sess-live"
    run.agent_session_agent = "claude"
    run.save(repo)
    stale = orch._claude_session_archive_dir(repo, run.run_id) / "-workspace-source"
    stale.mkdir(parents=True)
    (stale / "sess-live.jsonl").write_text("turn-1\n", encoding="utf-8")
    live = worktree / orch.CLAUDE_ISOLATED_PROJECTS_RELPATH / "-workspace-source"
    live.mkdir(parents=True)
    (live / "sess-live.jsonl").write_text('turn-1\nturn-2\n{"partial', encoding="utf-8")
    orch._record_live_provider_sessions(
        repo,
        run,
        orch.ImplementLaunchPlan(
            use_stream_json=True,
            agent_env={},
            agent_cmd=[],
            popen_kwargs={},
            claude_isolated_home=worktree / ".spec-claude-home",
        ),
    )

    code, out, _ = _attach(repo, worktree, capsys, alive=True, fork=True)

    assert code == 0
    assert "claude --resume sess-live --fork-session" in out
    installed = _claude_transcript(tmp_path / "operator-home" / ".claude", worktree, "sess-live")
    assert installed.read_text(encoding="utf-8") == "turn-1\nturn-2\n"


def test_provider_probes_run_inside_container_worker(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Worker:
        identity = eb.BackendIdentity(
            backend="container",
            safety_mode="safe",
            workspace_root=".spec-workspaces",
        )

        def run_command(self, request: eb.CommandRequest) -> eb.CommandResult:
            calls.append(list(request.argv))
            text = (
                WORKER_CLAUDE_HELP
                if request.argv[0] == "claude"
                else "--json --output-schema"
            )
            return eb.CommandResult(returncode=0, stdout=text, stderr="", argv=request.argv)

    with patch.object(orch, "run_subprocess", side_effect=AssertionError("host probe")):
        assert REAL_CLAUDE_SUPPORTS_STREAM_JSON(Worker(), tmp_path)
        claude = REAL_PROVIDER_SESSION_SUPPORT("claude", Worker(), tmp_path)
        codex = REAL_PROVIDER_SESSION_SUPPORT("codex", Worker(), tmp_path)
    assert claude == orch.ProviderSessionSupport(persist=True, resume=True, structured_output=True)
    assert codex.structured_output and codex.resume
    assert ["codex", "exec", "resume", "--help"] in calls


# ---------------------------------------------------------------------------
# Codex: one thread per run
# ---------------------------------------------------------------------------


def test_codex_run_records_thread_and_resumes_it(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "codex")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "codex")

    assert _implement(run, repo, worktree) == "passed"
    first = fake.calls()[-1]
    assert "resume" not in first
    assert "--output-schema" in first
    thread_id = run.agent_session_id
    assert thread_id
    assert run.agent_session_agent == "codex"
    # The disposable launch home is gone, but the rollout was archived.
    assert (
        orch._codex_session_archive_dir(repo, run.run_id) / f"rollout-{thread_id}.jsonl"
    ).is_file()
    assert orch.ImplementResult.load(repo, run.run_id).result_source == "structured_output"

    _prepare_retry(run, repo)
    assert _implement(run, repo, worktree) == "passed"
    retry = fake.calls()[-1]
    exec_index = retry.index("exec")
    assert retry[exec_index + 1] == "resume"
    assert retry[-2] == thread_id
    assert retry[-1].startswith(orch.RESUMED_SESSION_PREAMBLE)
    assert run.agent_session_id == thread_id


def test_codex_resume_failure_falls_back_to_fresh_thread(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "codex")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "codex")
    assert _implement(run, repo, worktree) == "passed"
    old_thread = run.agent_session_id

    shutil.rmtree(orch._codex_session_archive_dir(repo, run.run_id))
    _prepare_retry(run, repo)
    assert _implement(run, repo, worktree) == "passed"

    resumed, fresh = fake.calls()[-2:]
    assert "resume" in resumed
    assert "resume" not in fresh
    assert run.agent_session_id not in ("", old_thread)
    assert old_thread in run.agent_session_fallback_reason


def test_preserved_container_codex_home_is_archived_and_restored(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container worker's provider home is cleared after each launch too."""
    preserved_root = tmp_path / "worker-provider-homes" / "codex"
    preserved_root.mkdir(parents=True)
    monkeypatch.setattr(
        orch,
        "_prepare_codex_launch_home",
        lambda _backend, _worktree: (None, preserved_root, True),
    )
    fake = FakeProvider(tmp_path, "codex")
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "codex")

    assert _implement(run, repo, worktree) == "passed"
    thread_id = run.agent_session_id
    archive = orch._codex_session_archive_dir(repo, run.run_id)
    assert (archive / f"rollout-{thread_id}.jsonl").is_file()
    # The preserved home was emptied; only the archive keeps the thread.
    assert not any(preserved_root.rglob(f"rollout-{thread_id}.jsonl"))

    _prepare_retry(run, repo)
    assert _implement(run, repo, worktree) == "passed"
    retry = fake.calls()[-1]
    assert "resume" in retry
    assert retry[-2] == thread_id
    assert run.agent_session_id == thread_id
    assert run.agent_session_fallback_reason == ""


def test_codex_session_copy_skips_links(tmp_path: Path) -> None:
    source = tmp_path / "home" / "sessions"
    (source / "2026").mkdir(parents=True)
    (source / "2026" / "rollout-t.jsonl").write_text("{}\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (source / "2026" / "rollout-link.jsonl").symlink_to(secret)
    destination = tmp_path / "operator" / "sessions"

    orch._copy_codex_sessions(source, destination)

    assert (destination / "2026" / "rollout-t.jsonl").is_file()
    assert not (destination / "2026" / "rollout-link.jsonl").exists()
    assert not (destination / "2026" / "rollout-link.jsonl").is_symlink()


def test_codex_spec_report_is_fallback_without_structured_output(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider(tmp_path, "codex")
    fake.scenario(structured=None, report=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "codex")

    assert _implement(run, repo, worktree) == "passed"
    assert orch.ImplementResult.load(repo, run.run_id).summary == "reported via spec report"


# ---------------------------------------------------------------------------
# spec attach
# ---------------------------------------------------------------------------


def _attach(
    repo: Path,
    worktree: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    alive: bool = False,
    exec_: bool = False,
    fork: bool = False,
) -> tuple[int, str, str]:
    args = argparse.Namespace(spec=SPEC_ID, exec=exec_, fork=fork)
    with (
        patch.object(orch, "resolve_repo_root", return_value=repo),
        patch.object(orch, "resolve_worktree_path", return_value=worktree),
        patch.object(orch, "_run_agent_process_alive", return_value=alive),
    ):
        code = orch.cmd_attach(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_provider_attach_commands() -> None:
    assert provider_attach_command("claude", "s1") == ["claude", "--resume", "s1"]
    assert provider_attach_command("claude", "s1", fork=True) == [
        "claude",
        "--resume",
        "s1",
        "--fork-session",
    ]
    assert provider_attach_command("codex", "t1") == ["codex", "resume", "t1"]
    with pytest.raises(ValueError):
        provider_attach_command("custom", "x")


def test_attach_prints_resume_command(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, worktree = _make_run(repo, "claude")
    run.agent_session_id = "sess-1"
    run.agent_session_agent = "claude"
    run.save(repo)

    code, out, _ = _attach(repo, worktree, capsys)

    assert code == 0
    assert "claude --resume sess-1" in out
    assert str(worktree) in out


def test_attach_refuses_while_agent_is_alive_unless_forked(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "claude")
    run.agent_session_id = "sess-1"
    run.agent_session_agent = "claude"
    run.save(repo)

    code, out, err = _attach(repo, worktree, capsys, alive=True)
    assert code == 1
    assert "still running" in err
    assert "--resume" not in out

    code, out, _ = _attach(repo, worktree, capsys, alive=True, fork=True)
    assert code == 0
    assert "claude --resume sess-1 --fork-session" in out


def test_attach_without_session_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_run(repo, "claude")
    worktree = repo / ".worktrees" / SPEC_ID
    code, _, err = _attach(repo, worktree, capsys)
    assert code == 1
    assert "no recorded" in err


def _archive_codex_rollout(repo: Path, run: orch.RunState, thread_id: str) -> None:
    archive = orch._codex_session_archive_dir(repo, run.run_id) / "2026"
    archive.mkdir(parents=True)
    (archive / f"rollout-{thread_id}.jsonl").write_text("{}\n", encoding="utf-8")


@pytest.mark.parametrize("fork", [False, True])
def test_attach_prints_codex_command_that_finds_the_thread(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    fork: bool,
) -> None:
    """The printed command works on its own: the rollout is already restored."""
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    _archive_codex_rollout(repo, run, "thread-1")

    code, out, _ = _attach(repo, worktree, capsys, fork=fork)

    assert code == 0
    expected = "codex fork thread-1" if fork else "codex resume thread-1"
    assert expected in out
    operator_home = Path(os.environ["CODEX_HOME"])
    assert (operator_home / "sessions" / "2026" / "rollout-thread-1.jsonl").is_file()
    assert "Restored archived Codex rollouts" in out


def test_attach_refusal_does_not_touch_operator_codex_home(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    _archive_codex_rollout(repo, run, "thread-1")

    code, _, err = _attach(repo, worktree, capsys, alive=True)

    assert code == 1
    assert "still running" in err
    assert not (Path(os.environ["CODEX_HOME"]) / "sessions").exists()


def test_attach_warns_when_codex_rollout_archive_is_missing(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)

    code, out, err = _attach(repo, worktree, capsys)

    assert code == 0
    assert "codex resume thread-1" in out
    assert "No archived Codex rollouts" in err


def test_attach_exec_runs_codex_resume_in_workspace(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    archive = orch._codex_session_archive_dir(repo, run.run_id) / "2026" / "rollout-thread-1.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text("{}\n", encoding="utf-8")

    bin_dir = tmp_path / "attach-bin"
    bin_dir.mkdir()
    record = tmp_path / "attach-record.json"
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    code, _, _ = _attach(repo, worktree, capsys, exec_=True)

    assert code == 0
    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert recorded["argv"] == ["resume", "thread-1"]
    assert Path(recorded["cwd"]).resolve() == worktree.resolve()
    # The archived rollout is visible to the operator's own Codex home.
    operator_home = Path(os.environ["CODEX_HOME"])
    assert (operator_home / "sessions" / "2026" / "rollout-thread-1.jsonl").is_file()


def test_attach_fork_reads_the_running_codex_rollout(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On the first attempt the thread exists only in the live launch home."""
    import threading

    fake = FakeProvider(tmp_path, "codex")
    fake.scenario(structured=OK_COMPLETION, hold=True)
    _use_fake_binary(monkeypatch, fake)
    run, worktree = _make_run(repo, "codex")
    attached: dict[str, object] = {}
    registered = threading.Event()
    register_process = orch._register_worktree_process_from_popen

    def register_and_signal(*args: object, **kwargs: object) -> None:
        register_process(*args, **kwargs)
        registered.set()

    def attach_while_running() -> None:
        try:
            holding = fake.state / "holding"
            for _ in range(600):
                if holding.exists():
                    break
                threading.Event().wait(0.05)
            thread_id = holding.read_text(encoding="utf-8")
            # The child can reach its hold point before the parent enters the
            # launch monitor. Attach only after registration has completed.
            assert registered.wait(timeout=30), "agent process was not registered"
            assert not orch._codex_session_archive_dir(repo, run.run_id).exists()
            live = orch.RunState.load(repo, run.run_id)
            live.agent_session_id = thread_id
            live.agent_session_agent = "codex"
            live.save(repo)
            with (
                patch.object(orch, "resolve_repo_root", return_value=repo),
                patch.object(orch, "resolve_worktree_path", return_value=worktree),
            ):
                attached["alive"] = orch._run_agent_process_alive(repo, worktree)
                attached["code"] = orch.cmd_attach(
                    argparse.Namespace(spec=SPEC_ID, exec=False, fork=True)
                )
            attached["thread_id"] = thread_id
        except BaseException as exc:  # surfaced by the main thread
            attached["error"] = exc
        finally:
            (fake.state / "release").write_text("go", encoding="utf-8")

    worker = threading.Thread(target=attach_while_running)
    worker.start()
    try:
        with patch.object(
            orch, "_register_worktree_process_from_popen", side_effect=register_and_signal
        ):
            assert _implement(run, repo, worktree) == "passed"
    finally:
        (fake.state / "release").write_text("go", encoding="utf-8")
        worker.join(timeout=60)
    if "error" in attached:
        raise attached["error"]  # type: ignore[misc]

    out = capsys.readouterr().out
    thread_id = attached["thread_id"]
    assert attached["alive"] is True
    assert attached["code"] == 0
    assert f"codex fork {thread_id}" in out
    assert "Restored live Codex rollouts" in out
    restored = Path(os.environ["CODEX_HOME"]) / "sessions" / f"rollout-{thread_id}.jsonl"
    # The half-written record the provider was appending is not carried over.
    assert restored.read_text(encoding="utf-8") == "{}\n"
    assert not orch._live_provider_sessions_path(repo, run.run_id).exists()


def test_attach_fork_prefers_live_codex_rollout_over_stale_archive(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    archive = orch._codex_session_archive_dir(repo, run.run_id)
    archive.mkdir(parents=True)
    (archive / "rollout-thread-1.jsonl").write_text("turn-1\n", encoding="utf-8")
    live_home = tmp_path / "live-codex-home"
    (live_home / "sessions").mkdir(parents=True)
    (live_home / "sessions" / "rollout-thread-1.jsonl").write_text(
        "turn-1\nturn-2\n{\"partial", encoding="utf-8"
    )
    orch._record_live_provider_sessions(
        repo,
        run,
        orch.ImplementLaunchPlan(
            use_stream_json=False,
            agent_env={},
            agent_cmd=[],
            popen_kwargs={},
            codex_home=live_home,
        ),
    )

    code, out, _ = _attach(repo, worktree, capsys, alive=True, fork=True)

    assert code == 0
    assert "codex fork thread-1" in out
    restored = Path(os.environ["CODEX_HOME"]) / "sessions" / "rollout-thread-1.jsonl"
    assert restored.read_text(encoding="utf-8") == "turn-1\nturn-2\n"
    # The archive itself is left for the launch to finalize.
    assert (archive / "rollout-thread-1.jsonl").read_text(encoding="utf-8") == "turn-1\n"


def test_reattach_keeps_interactive_codex_turns(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second attach must not roll the operator's rollout back to the archive."""
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    _archive_codex_rollout(repo, run, "thread-1")
    assert _attach(repo, worktree, capsys)[0] == 0
    restored = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "rollout-thread-1.jsonl"
    # The operator resumed interactively and added a turn.
    restored.write_text("{}\n{\"operator\": 1}\n", encoding="utf-8")

    code, out, _ = _attach(repo, worktree, capsys)

    assert code == 0
    assert "codex resume thread-1" in out
    assert restored.read_text(encoding="utf-8") == "{}\n{\"operator\": 1}\n"


def test_reattach_restores_rollout_that_extends_operator_copy(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A later retry that grew the thread is still brought to the operator."""
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    _archive_codex_rollout(repo, run, "thread-1")
    assert _attach(repo, worktree, capsys)[0] == 0
    archived = orch._codex_session_archive_dir(repo, run.run_id) / "2026" / "rollout-thread-1.jsonl"
    archived.write_text("{}\n{\"retry\": 2}\n", encoding="utf-8")

    assert _attach(repo, worktree, capsys)[0] == 0

    restored = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "rollout-thread-1.jsonl"
    assert restored.read_text(encoding="utf-8") == "{}\n{\"retry\": 2}\n"


def test_reattach_keeps_diverged_operator_rollout_and_warns(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, worktree = _make_run(repo, "codex")
    run.agent_session_id = "thread-1"
    run.agent_session_agent = "codex"
    run.save(repo)
    _archive_codex_rollout(repo, run, "thread-1")
    assert _attach(repo, worktree, capsys)[0] == 0
    restored = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "rollout-thread-1.jsonl"
    restored.write_text("{}\n{\"operator\": 1}\n", encoding="utf-8")
    archived = orch._codex_session_archive_dir(repo, run.run_id) / "2026" / "rollout-thread-1.jsonl"
    archived.write_text("{}\n{\"retry\": 2}\n", encoding="utf-8")

    code, _, err = _attach(repo, worktree, capsys)

    assert code == 0
    assert restored.read_text(encoding="utf-8") == "{}\n{\"operator\": 1}\n"
    assert "Kept your existing" in err


# ---------------------------------------------------------------------------
# spec status
# ---------------------------------------------------------------------------


def test_status_shows_agent_session(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, worktree = _make_run(repo, "claude")
    run.agent_session_id = "sess-status"
    run.agent_session_agent = "claude"
    run.save(repo)

    with (
        patch.object(orch, "resolve_repo_root", return_value=repo),
        patch.object(orch, "resolve_worktree_path", return_value=worktree),
    ):
        code = orch.cmd_status(argparse.Namespace(spec=SPEC_ID, run=None))

    out = capsys.readouterr().out
    assert code == 0
    assert "Agent Session: sess-status (claude)" in out
    assert "session sess-status" in out
