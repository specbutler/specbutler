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
    monkeypatch.setattr(orch, "_claude_supports_stream_json", lambda: True)
    monkeypatch.setattr(orch, "_validate_codex_exec", lambda _run: True)
    monkeypatch.setattr(
        orch,
        "_provider_session_support",
        lambda _agent: orch.ProviderSessionSupport(
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

    def scenario(self, *, structured: dict | None = None, report: bool = False) -> None:
        (self.state / "scenario.json").write_text(
            json.dumps({"structured": structured, "report": report}),
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
import sys
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
    if "--resume" in args:
        sid = args[args.index("--resume") + 1]
        if sid not in known:
            print(f"No conversation found with session ID: {sid}", file=sys.stderr)
            raise SystemExit(1)
    else:
        sid = args[args.index("--session-id") + 1] if "--session-id" in args else ""
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
        lambda _agent: orch.ProviderSessionSupport(),
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
