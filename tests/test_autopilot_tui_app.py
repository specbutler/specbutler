from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")
rich_console = pytest.importorskip("rich.console")
Console = rich_console.Console

from spec_runtime.autopilot_tui import app as tui_app  # noqa: E402
from spec_runtime.autopilot_tui import dashboard as tui_dashboard  # noqa: E402


class _CompletedChatProcess:
    def __init__(self, stdout: str) -> None:
        self.stdout = io.StringIO(stdout)
        self.pid = 0
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_claude_chat_provider_uses_oauth_compatible_safe_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return _CompletedChatProcess(
            '{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"claude-ok"}]}}\n'
        )

    monkeypatch.setattr(tui_app.subprocess, "Popen", fake_popen)
    provider = tui_app.CliChatProvider(agent="claude", repo_root=tmp_path)

    assert list(provider._stream_claude_output("provider prompt")) == ["claude-ok"]
    assert calls[0][0] == "claude"
    assert "--safe-mode" in calls[0]
    assert "--bare" not in calls[0]
    assert calls[0][-2:] == ["--", "provider prompt"]
    tools_index = calls[0].index("--tools")
    assert calls[0][tools_index + 1] == ""


def test_codex_chat_provider_uses_read_only_ephemeral_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return _CompletedChatProcess(
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"id":"assistant-1","text":"codex-ok"}}\n'
        )

    monkeypatch.setattr(tui_app.subprocess, "Popen", fake_popen)
    provider = tui_app.CliChatProvider(agent="codex", repo_root=tmp_path)

    assert list(provider._stream_codex_output("provider prompt")) == ["codex-ok"]
    assert calls[0][:4] == ["codex", "-a", "never", "exec"]
    assert "--ephemeral" in calls[0]
    assert calls[0][calls[0].index("-s") + 1] == "read-only"
    assert calls[0][-1] == "provider prompt"


def _stream_synthetic_chat_provider(tmp_path: Path, script: str):
    return tui_app._stream_chat_provider_process(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={},
        provider_name="Synthetic",
        parse_line=lambda line: line.rstrip("\n"),
    )


def test_chat_provider_process_streams_ordinary_success(tmp_path: Path) -> None:
    chunks = list(
        _stream_synthetic_chat_provider(
            tmp_path,
            'print("first", flush=True); print("second", flush=True)',
        )
    )

    assert chunks == ["first", "second"]


def test_chat_provider_large_stderr_cannot_deadlock_stdout(tmp_path: Path) -> None:
    chunks = list(
        _stream_synthetic_chat_provider(
            tmp_path,
            'import sys; sys.stderr.write("x" * (2 * 1024 * 1024)); '
            'sys.stderr.flush(); print("stdout-ready", flush=True)',
        )
    )

    assert chunks == ["stdout-ready"]


def test_chat_provider_silent_hang_times_out_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_popen = subprocess.Popen
    started: list[subprocess.Popen[str]] = []

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(tui_app.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(tui_app, "CHAT_PROVIDER_INACTIVITY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(tui_app, "CHAT_PROVIDER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(tui_app, "CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(tui_app.ChatProviderError, match="timed out"):
        list(
            _stream_synthetic_chat_provider(
                tmp_path,
                "import time; time.sleep(30)",
            )
        )

    assert len(started) == 1
    assert started[0].poll() is not None
    assert started[0].wait(timeout=0.1) == started[0].returncode


def test_chat_provider_generator_cancel_terminates_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_popen = subprocess.Popen
    started: list[subprocess.Popen[str]] = []

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(tui_app.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(tui_app, "CHAT_PROVIDER_TERMINATE_TIMEOUT_SECONDS", 0.5)
    stream = _stream_synthetic_chat_provider(
        tmp_path,
        'import time; print("ready", flush=True); time.sleep(30)',
    )

    assert next(stream) == "ready"
    stream.close()

    assert len(started) == 1
    assert started[0].poll() is not None
    assert started[0].wait(timeout=0.1) == started[0].returncode


def test_provider_command_parser_preserves_steering_guidance() -> None:
    command = tui_app._parse_provider_command_json(
        '{"name":"record_steering","spec_id":"web-chat",'
        '"guidance":"Try the narrowest fix first."}'
    )

    assert command is not None
    assert command.guidance == "Try the narrowest fix first."


@pytest.mark.parametrize("name", sorted(tui_app.CHAT_MUTATING_ACTIONS))
def test_provider_mutations_require_confirmation(name: str) -> None:
    assert tui_app._provider_command_requires_confirmation(
        tui_app.ChatCommand(name=name)
    )


@pytest.mark.parametrize("name", ["list_runs", "show_run", "tail_log", "help"])
def test_provider_reads_do_not_require_confirmation(name: str) -> None:
    assert not tui_app._provider_command_requires_confirmation(
        tui_app.ChatCommand(name=name)
    )


def test_global_chat_uses_repo_default_available_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".spec.toml").write_text(
        '[agents]\ndefault = "claude"\nallowed = ["claude", "codex"]\n'
    )
    monkeypatch.setattr(
        tui_app.shutil,
        "which",
        lambda agent: "/usr/bin/codex" if agent == "codex" else None,
    )
    app = tui_app.AutopilotWatchApp(repo_root=tmp_path, refresh_interval=5)

    assert app._chat_agent() == "codex"


def test_reset_spec_run_always_starts_fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(tui_app, "_latest_run", lambda repo_root, spec_id: None)
    monkeypatch.setattr(tui_app, "_resolve_live_process_group", lambda repo_root, spec_id, run=None: None)
    monkeypatch.setattr(tui_app, "_run_requires_live_guard", lambda latest: False)
    monkeypatch.setattr(
        tui_app,
        "_run_code_clean",
        lambda repo_root, spec_id: calls.append(("clean", spec_id)),
    )
    monkeypatch.setattr(
        tui_app,
        "_remove_spec_run_state",
        lambda repo_root, spec_id: calls.append(("remove", spec_id)),
    )
    monkeypatch.setattr(
        tui_app,
        "_launch_make_code_compat",
        lambda repo_root, spec_id, **kwargs: calls.append(("launch", {"spec_id": spec_id, **kwargs})),
    )

    tui_app.reset_spec_run(tmp_path, "my-spec", agent="codex")

    assert calls == [
        ("clean", "my-spec"),
        ("remove", "my-spec"),
        ("launch", {"spec_id": "my-spec", "agent": "codex", "actor": "autopilot-watch"}),
    ]


def test_reset_spec_run_reuses_task_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    latest = SimpleNamespace(run_mode="task", run_id="task-123")

    monkeypatch.setattr(tui_app, "_latest_run", lambda repo_root, spec_id: latest)
    monkeypatch.setattr(tui_app, "_resolve_live_process_group", lambda repo_root, spec_id, run=None: None)
    monkeypatch.setattr(tui_app, "_run_requires_live_guard", lambda latest: False)
    monkeypatch.setattr(tui_app, "_run_code_clean", lambda repo_root, spec_id: calls.append(("clean", spec_id)))
    monkeypatch.setattr(
        tui_app,
        "_clear_spec_runtime_artifacts",
        lambda repo_root, spec_id: calls.append(("clear", spec_id)),
    )
    monkeypatch.setattr(
        tui_app,
        "_launch_make_code_compat",
        lambda repo_root, spec_id, **kwargs: calls.append(("launch", {"spec_id": spec_id, **kwargs})),
    )

    tui_app.reset_spec_run(tmp_path, "task-spec", agent="claude")

    assert calls == [
        ("clean", "task-spec"),
        ("clear", "task-spec"),
        ("launch", {"spec_id": "task-spec", "run_id": "task-123", "actor": "autopilot-watch"}),
    ]


def test_handle_reset_always_requests_fresh_run(tmp_path: Path) -> None:
    reset_calls: list[tuple[str, str]] = []
    app = tui_app.AutopilotWatchApp(
        repo_root=tmp_path,
        refresh_interval=5,
        reset_runner=lambda spec_id, agent: reset_calls.append((spec_id, agent)),
    )
    app.notify = lambda *args, **kwargs: None
    app.refresh_snapshot = lambda: None
    app._screen_stack.append(SimpleNamespace(focused=None))
    original_default_agent = tui_app._default_agent
    tui_app._default_agent = lambda repo_root, spec_id, fallback_agent="": "codex"
    try:
        app._handle_reset("my-spec", "reset")
    finally:
        tui_app._default_agent = original_default_agent

    assert reset_calls == [("my-spec", "codex")]


def test_reset_screen_copy_describes_fresh_run() -> None:
    assert any(binding.key == "enter" and binding.action == "reset" for binding in tui_app.ResetScreen.BINDINGS)
    assert not any(binding.key == "f" for binding in tui_app.ResetScreen.BINDINGS)


# --- delete_spec_artifacts tests ---


def test_delete_spec_artifacts_task_on_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deleting a task whose spec file exists on disk (happy path)."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    task_dir = tmp_path / "specs" / "tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "my-task.md").write_text("---\nid: my-task\n---\n")

    monkeypatch.setattr(tui_app, "_run_code_clean", lambda repo_root, spec_id: calls.append(("clean", spec_id)))
    monkeypatch.setattr(
        tui_app,
        "_remove_spec_run_state",
        lambda repo_root, spec_id: calls.append(("remove_state", spec_id)),
    )

    result = tui_app.delete_spec_artifacts(tmp_path, "my-task")

    assert "Deleted task" in result
    assert "my-task" in result
    assert not (task_dir / "my-task.md").exists()
    assert ("clean", "my-task") in calls
    assert ("remove_state", "my-task") in calls


def _write_run_json(tmp_path: Path, filename: str, payload: dict) -> Path:
    """Write a run-state JSON file under .spec-state/runs/."""
    runs_dir = tmp_path / ".spec-state" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_file = runs_dir / filename
    run_file.write_text(json.dumps(payload))
    return run_file


def test_delete_spec_artifacts_orphan_task_with_run_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deleting a task with no spec file on disk but with real task run records should succeed."""
    calls: list[tuple[str, str]] = []

    # Write a real run-state JSON with run_mode=task so _has_task_run_records finds it
    _write_run_json(tmp_path, "orphan-task-run1.json", {
        "spec_id": "orphan-task",
        "run_mode": "task",
        "status": "completed",
    })

    monkeypatch.setattr(
        tui_app,
        "_run_code_clean",
        lambda repo_root, spec_id: (_ for _ in ()).throw(RuntimeError("spec clean failed")),
    )
    monkeypatch.setattr(
        tui_app,
        "_remove_spec_run_state",
        lambda repo_root, spec_id: calls.append(("remove_state", spec_id)),
    )

    result = tui_app.delete_spec_artifacts(tmp_path, "orphan-task")

    assert "orphan task" in result.lower()
    assert "orphan-task" in result
    assert ("remove_state", "orphan-task") in calls


def test_delete_spec_artifacts_no_spec_no_run_records(tmp_path: Path) -> None:
    """Deleting a spec_id with no spec file and no run records should raise."""
    # No run-state files exist at all — _has_task_run_records will return False
    with pytest.raises(RuntimeError, match="Could not find a task or product spec"):
        tui_app.delete_spec_artifacts(tmp_path, "nonexistent")


def test_delete_spec_artifacts_product_spec_run_not_treated_as_orphan(tmp_path: Path) -> None:
    """A product spec with run records but run_mode=spec must NOT be silently purged."""
    # Write a run-state JSON with run_mode=spec (not task)
    _write_run_json(tmp_path, "prod-spec-run1.json", {
        "spec_id": "missing-product-spec",
        "run_mode": "spec",
        "status": "completed",
    })

    with pytest.raises(RuntimeError, match="Could not find a task or product spec"):
        tui_app.delete_spec_artifacts(tmp_path, "missing-product-spec")


def test_spec_row_carries_diagnosis_fields() -> None:
    """SpecRow accepts diagnosis fields and defaults them correctly."""
    row_default = tui_app.SpecRow(
        spec_id="a", display_spec_id="a", agent="codex", phase="implement",
        retries="1/10", elapsed="2m", status="blocked", branch="b", run_id="r",
        run_mode="spec", created_at="2026-01-01",
    )
    assert row_default.requires_human_attention is False
    assert row_default.diagnosis_summary == ""
    assert row_default.diagnosis_next_action == ""
    assert row_default.has_active_steering is False
    assert row_default.steering_summary == ""

    row_attention = tui_app.SpecRow(
        spec_id="a", display_spec_id="a", agent="codex", phase="implement",
        retries="1/10", elapsed="2m", status="blocked", branch="b", run_id="r",
        run_mode="spec", created_at="2026-01-01",
        requires_human_attention=True,
        diagnosis_summary="stuck in loop",
        diagnosis_next_action="invalidate implement-result",
    )
    assert row_attention.requires_human_attention is True
    assert row_attention.diagnosis_summary == "stuck in loop"


def test_needs_attention_count_property() -> None:
    """DashboardSnapshot.needs_attention_count counts correctly."""
    def _row(spec_id: str, attention: bool = False) -> tui_app.SpecRow:
        return tui_app.SpecRow(
            spec_id=spec_id, display_spec_id=spec_id, agent="codex", phase="implement",
            retries="1/10", elapsed="2m", status="blocked", branch="b", run_id="r",
            run_mode="spec", created_at="2026-01-01",
            requires_human_attention=attention,
        )

    snap = tui_app.DashboardSnapshot(
        rows=(_row("a", True), _row("b", False), _row("c", True)),
        queue=(),
        merged_count=0,
    )
    assert snap.needs_attention_count == 2


def test_row_sort_key_attention_runs_sort_above_blocked() -> None:
    """Runs needing human attention sort above normal blocked/failed runs."""
    def _row(spec_id: str, status: str, attention: bool = False) -> tui_app.SpecRow:
        return tui_app.SpecRow(
            spec_id=spec_id, display_spec_id=spec_id, agent="codex", phase="implement",
            retries="1/10", elapsed="2m", status=status, branch="b", run_id="r",
            run_mode="spec", created_at="2026-01-01",
            requires_human_attention=attention,
        )

    attention_blocked = _row("a", "blocked", attention=True)
    normal_blocked = _row("b", "blocked", attention=False)
    running = _row("c", "running", attention=False)

    sorted_rows = sorted(
        [normal_blocked, attention_blocked, running],
        key=tui_app._row_sort_key,
    )
    assert sorted_rows[0].spec_id == "c"  # running first
    assert sorted_rows[1].spec_id == "a"  # attention-needed second
    assert sorted_rows[2].spec_id == "b"  # normal blocked last


def test_load_dashboard_snapshot_loads_diagnosis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_dashboard_snapshot populates requires_human_attention from BlockDiagnosis."""
    run_data = {
        "spec_id": "web-server",
        "status": "blocked",
        "agent": "codex",
        "phase": "review",
        "attempts": 10,
        "retry_cap": 10,
        "run_id": "web-server-123",
        "branch": "code/web-server",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
    }

    # Write block diagnosis
    runs_dir = tmp_path / ".spec-state" / "runs"
    run_dir = runs_dir / "web-server-123"
    run_dir.mkdir(parents=True)
    (run_dir / "block-diagnosis.json").write_text(json.dumps({
        "summary": "stuck in loop",
        "root_cause": "stale result",
        "category": "stale_implement_result_same_attempt_guided_retry_loop",
        "next_best_action": "invalidate implement-result",
        "requires_human_attention": True,
        "confidence": 0.97,
        "blocker_signature": "abc123",
        "evidence": ["implement-result.json is stale"],
    }))

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-server": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)
    attention_rows = [r for r in snapshot.rows if r.requires_human_attention]
    assert len(attention_rows) == 1
    assert attention_rows[0].spec_id == "web-server"
    assert attention_rows[0].retries == "11/11"
    assert attention_rows[0].diagnosis_summary == "stuck in loop"
    assert attention_rows[0].diagnosis_next_action == "invalidate implement-result"


def test_load_dashboard_snapshot_keeps_non_live_pending_runs_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_data = {
        "spec_id": "web-chat",
        "status": "pending",
        "agent": "claude",
        "phase": "implement",
        "attempts": 11,
        "retry_cap": 11,
        "run_id": "web-chat-123",
        "branch": "code/web-chat",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
    }

    runs_dir = tmp_path / ".spec-state" / "runs"
    run_dir = runs_dir / "web-chat-123"
    run_dir.mkdir(parents=True)
    (run_dir / "block-diagnosis.json").write_text(json.dumps({
        "summary": "stuck in loop",
        "root_cause": "stale result",
        "category": "retry-artifact-lineage",
        "next_best_action": "resume from implement",
        "requires_human_attention": True,
        "confidence": 0.97,
        "blocker_signature": "abc123",
        "evidence": ["review-result.json is stale"],
    }))

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-chat": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].status == "pending"
    assert snapshot.rows[0].requires_human_attention is True
    assert snapshot.active_count == 0


def test_load_dashboard_snapshot_surfaces_container_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
engine = "podman"
image = "example/spec-worker:latest"
""".lstrip(),
    )
    run_data = {
        "spec_id": "web-chat",
        "status": "running",
        "agent": "codex",
        "phase": "implement",
        "attempts": 1,
        "retry_cap": 10,
        "run_id": "web-chat-123",
        "branch": "code/web-chat",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
        "backend": "container",
        "safety_mode": "host-mediated",
        "backend_source": "rollout-policy",
    }

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-chat": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].backend == "container"
    assert snapshot.rows[0].container_engine == "podman"
    assert snapshot.rows[0].worker_source == "image:example/spec-worker:latest"


def test_render_runs_table_includes_container_metadata() -> None:
    row = tui_app.SpecRow(
        spec_id="web-chat",
        display_spec_id="web-chat",
        agent="codex",
        phase="implement",
        retries="2/11",
        elapsed="2m",
        status="running",
        branch="code/web-chat",
        run_id="web-chat-123",
        run_mode="spec",
        created_at="2026-01-01T00:00:00Z",
        backend="container",
        safety_mode="host-mediated",
        container_engine="podman",
        worker_source="image:example/spec-worker:latest",
    )

    console = Console(record=True, width=160)
    console.print(tui_app._render_runs_table((row,)))
    rendered = console.export_text()

    assert "ENGINE" in rendered
    assert "WORKER" in rendered
    assert "podman" in rendered
    assert "image:example/spec-worker:latest" in rendered


def test_render_run_detail_includes_container_metadata(tmp_path: Path) -> None:
    (tmp_path / ".spec.toml").write_text(
        """
[execution]
backend = "container"

[execution.container]
engine = "podman"
image = "example/spec-worker:latest"
""".lstrip(),
    )
    run = tui_app.orch.RunState(
        run_id="web-chat-123",
        spec_id="web-chat",
        branch="code/web-chat",
        status="running",
        phase="implement",
        agent="codex",
        backend="container",
        safety_mode="host-mediated",
        backend_source="rollout-policy",
    )

    console = Console(record=True, width=160)
    console.print(tui_app._render_run_detail(tmp_path, run))
    rendered = console.export_text()

    assert "Container Engine" in rendered
    assert "podman" in rendered
    assert "Worker Source" in rendered
    assert "image:example/spec-worker:latest" in rendered


def test_load_dashboard_snapshot_uses_repo_retry_cap_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".spec.toml").write_text('base_ref = "origin/main"\n[retry]\ncap = 30\n')
    run_data = {
        "spec_id": "web-chat",
        "status": "failed",
        "agent": "claude",
        "phase": "implement",
        "attempts": 2,
        "run_id": "web-chat-123",
        "branch": "code/web-chat",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
    }

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-chat": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].retries == "3/31"


def test_load_dashboard_snapshot_loads_active_steering(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_data = {
        "spec_id": "web-server",
        "status": "failed",
        "agent": "codex",
        "phase": "implement",
        "attempts": 2,
        "retry_cap": 10,
        "run_id": "web-server-123",
        "branch": "code/web-server",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
    }
    runs_dir = tmp_path / ".spec-state" / "runs"
    run_dir = runs_dir / "web-server-123"
    run_dir.mkdir(parents=True)
    (run_dir / "operator-steering.json").write_text(json.dumps({
        "message": "Retry with the smallest schema fix first.",
        "provided_by": "alice",
        "provided_at": "2026-01-02T03:04:05Z",
        "source": "spec steer",
        "status": "active",
        "event_id": "evt-1",
    }))

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-server": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].has_active_steering is True
    assert snapshot.rows[0].steering_summary == "Retry with the smallest schema fix first."


def test_load_dashboard_snapshot_keeps_passed_run_with_active_steering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_data = {
        "spec_id": "web-server",
        "status": "passed",
        "agent": "codex",
        "phase": "implement",
        "attempts": 2,
        "retry_cap": 10,
        "run_id": "web-server-123",
        "branch": "code/web-server",
        "created_at": "2026-01-01T00:00:00Z",
        "run_mode": "spec",
    }
    runs_dir = tmp_path / ".spec-state" / "runs"
    run_dir = runs_dir / "web-server-123"
    run_dir.mkdir(parents=True)
    (run_dir / "operator-steering.json").write_text(json.dumps({
        "message": "Retry with the smallest schema fix first.",
        "provided_by": "alice",
        "provided_at": "2026-01-02T03:04:05Z",
        "source": "spec steer",
        "status": "active",
        "event_id": "evt-1",
    }))

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_dashboard, "_read_active_data", lambda repo_root: {})
    monkeypatch.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
    monkeypatch.setattr(tui_app.autopilot, "load_run_record_index", lambda repo_root: SimpleNamespace(records=[]))
    monkeypatch.setattr(tui_app.autopilot, "hidden_spec_ids", lambda repo_root: set())
    monkeypatch.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
    monkeypatch.setattr(tui_dashboard, "_latest_row_records", lambda repo_root, run_index=None: {"web-server": run_data})
    monkeypatch.setattr(tui_app.autopilot, "_is_stale_run", lambda data, *_a, **_k: False)

    snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].status == "passed"
    assert snapshot.rows[0].has_active_steering is True
    assert snapshot.passed_count == 0


def test_human_attention_guidance_does_not_advertise_answer_command() -> None:
    diagnosis = tui_app.orch.BlockDiagnosis(
        summary="stuck in loop",
        root_cause="stale result",
        confidence=0.97,
        category="review-feedback-loop",
        evidence=["implement-result.json is stale"],
        next_best_action="Regenerate the retry from the latest review payload.",
        requires_human_attention=True,
    )

    message = tui_app._human_attention_guidance("web-chat", diagnosis)

    assert "web-chat needs human attention." in message
    assert "Use `answer:` only when the run has an active operator request." in message
    assert "`steer: <guidance>`" in message
    assert "- `add retries N` and `resume` if you want to try again with more budget" in message
    assert "- `answer:" not in message


def test_resolve_input_unavailable_message_uses_human_attention_guidance() -> None:
    diagnosis = tui_app.orch.BlockDiagnosis(
        summary="stuck in loop",
        root_cause="stale result",
        confidence=0.97,
        category="review-feedback-loop",
        evidence=["implement-result.json is stale"],
        next_best_action="Regenerate the retry from the latest review payload.",
        requires_human_attention=True,
    )

    message = tui_app._resolve_input_unavailable_message("web-chat", "blocked", diagnosis)

    assert "Spec 'web-chat' is not waiting for operator intervention; current status is 'blocked'." in message
    assert "`answer:` only works for active operator requests." in message
    assert "Suggested action: Regenerate the retry from the latest review payload." in message


def test_resolve_input_run_resets_debugger_guidance_to_implement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run = tui_app.orch.RunState(
        run_id="web-chat-20260101T000000",
        spec_id="web-chat",
        branch="code/web-chat",
        status="blocked",
        phase="review",
    )
    run.save(tmp_path)
    tui_app.orch.BlockDiagnosis(
        summary="stuck in loop",
        root_cause="stale result",
        confidence=0.97,
        category="review-feedback-loop",
        evidence=["review-result.json"],
        next_best_action="Retry from implement with the selected fix strategy.",
        requires_human_attention=True,
        blocker_signature="sig-123",
        source_phase="review",
    ).save(tmp_path, run.run_id)

    launches: list[dict[str, str]] = []
    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_app, "_latest_run", lambda repo_root, spec_id: run)
    monkeypatch.setattr(tui_app, "_clear_run_implement_results", lambda repo_root, saved_run: None)
    monkeypatch.setattr(
        tui_app,
        "_launch_make_code_compat",
        lambda repo_root, spec_id, **kwargs: launches.append({"spec_id": spec_id, **kwargs}) or Path("launch.log"),
    )

    result = tui_app.resolve_input_run(tmp_path, "web-chat", "Resume with the minimal fix.")

    reloaded = tui_app.orch.RunState.find_latest(tmp_path, "web-chat")
    request = tui_app.orch.OperatorRequest.load(tmp_path, run.run_id)

    assert result == Path("launch.log")
    assert reloaded is not None
    assert reloaded.status == "pending"
    assert reloaded.phase == "implement"
    assert request is not None
    assert request.kind == "debugger_guidance"
    assert request.status == "resolved"
    assert request.response == "Resume with the minimal fix."
    assert launches == [{"spec_id": "web-chat", "run_id": run.run_id, "actor": "tui-chat"}]


def test_resolve_input_run_answer_only_uses_operator_continuation_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run = tui_app.orch.RunState(
        run_id="web-chat-20260101T000000",
        spec_id="web-chat",
        branch="code/web-chat",
        status="waiting-for-input",
        phase="implement",
    )
    run.save(tmp_path)
    tui_app.orch.OperatorRequest(
        kind="agent_question",
        prompt="Which API version?",
        requested_by_phase="implement",
        requires_full_session=False,
    ).save(tmp_path, run.run_id)
    tui_app.orch.ImplementResult(status="passed", summary="old result", attempt=run.attempts).save(
        tmp_path,
        run.run_id,
    )

    launches: list[dict[str, str]] = []
    cleared: list[str] = []
    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_app, "_latest_run", lambda repo_root, spec_id: run)
    monkeypatch.setattr(
        tui_app,
        "_clear_run_implement_results",
        lambda repo_root, saved_run: cleared.append(saved_run.run_id),
    )
    monkeypatch.setattr(
        tui_app,
        "_launch_make_code_compat",
        lambda repo_root, spec_id, **kwargs: launches.append({"spec_id": spec_id, **kwargs}) or Path("launch.log"),
    )

    result = tui_app.resolve_input_run(tmp_path, "web-chat", "Use v2.")

    reloaded = tui_app.orch.RunState.find_latest(tmp_path, "web-chat")
    request = tui_app.orch.OperatorRequest.load(tmp_path, run.run_id)

    assert result == Path("launch.log")
    assert reloaded is not None
    assert reloaded.status == "pending"
    assert reloaded.phase == "implement"
    assert request is not None
    assert request.status == "resolved"
    assert request.response == "Use v2."
    assert request.continuation == tui_app.orch.OPERATOR_CONTINUATION_RESUME_IMPLEMENT
    assert cleared == [run.run_id]
    assert launches == [{"spec_id": "web-chat", "run_id": run.run_id, "actor": "tui-chat"}]


def test_parse_chat_command_recognizes_explicit_steering(tmp_path: Path) -> None:
    command = tui_app.parse_chat_command(
        "steer: retry with the smaller schema change first",
        tmp_path,
        default_spec_id="web-chat",
    )

    assert command is not None
    assert command.name == "record_steering"
    assert command.spec_id == "web-chat"
    assert command.guidance == "retry with the smaller schema change first"


def test_record_operator_steering_uses_orchestrator_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run = tui_app.orch.RunState(
        run_id="web-chat-20260101T000000",
        spec_id="web-chat",
        branch="code/web-chat",
        status="failed",
        phase="implement",
    )
    run.save(tmp_path)
    captured: dict[str, str] = {}

    def fake_record(common_root, saved_run, guidance, *, source):
        captured["common_root"] = str(common_root)
        captured["run_id"] = saved_run.run_id
        captured["guidance"] = guidance
        captured["source"] = source
        return tui_app.orch.OperatorSteering(
            message=guidance,
            provided_by="alice",
            source=source,
            event_id="evt-1",
        )

    monkeypatch.setattr(tui_app, "_latest_run", lambda repo_root, spec_id: run)
    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
    monkeypatch.setattr(tui_app.orch, "_record_operator_steering", fake_record)

    steering = tui_app.record_operator_steering(tmp_path, "web-chat", "Try the narrowest fix first.")

    assert steering.message == "Try the narrowest fix first."
    assert captured == {
        "common_root": str(tmp_path),
        "run_id": run.run_id,
        "guidance": "Try the narrowest fix first.",
        "source": "tui-chat",
    }


def test_delete_spec_artifacts_uses_common_root_when_launched_from_linked_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "code-scratch"
    task_dir = repo / "specs" / "tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "codex-worktree-sandbox.md").write_text("---\nid: codex-worktree-sandbox\n---\n")

    monkeypatch.setattr(tui_app.autopilot, "resolve_common_root", lambda path: repo)
    monkeypatch.setattr(tui_app, "_run_code_clean", lambda repo_root, spec_id: calls.append(("clean", spec_id)))
    monkeypatch.setattr(
        tui_app,
        "_remove_spec_run_state",
        lambda repo_root, spec_id: calls.append(("remove_state", spec_id)),
    )

    result = tui_app.delete_spec_artifacts(worktree, "codex-worktree-sandbox")

    assert "Deleted task" in result
    assert ("clean", "codex-worktree-sandbox") in calls
    assert ("remove_state", "codex-worktree-sandbox") in calls
    assert not (task_dir / "codex-worktree-sandbox.md").exists()


def test_load_dashboard_snapshot_renders_hung_run_as_stale(tmp_path: Path) -> None:
    """A running run whose heartbeat went quiet must render 'stale', even while
    an autopilot active.json entry exists for it. A hung tool call must not
    continue to display as healthy and running."""
    from datetime import UTC, datetime, timedelta

    runs_dir = tmp_path / ".spec-state" / "runs"
    runs_dir.mkdir(parents=True)
    stale_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    (runs_dir / "hung-spec-1.json").write_text(json.dumps({
        "spec_id": "hung-spec",
        "run_id": "hung-spec-1",
        "status": "running",
        "phase": "implement",
        "agent": "claude",
        "branch": "code/hung-spec--1",
        "created_at": stale_ts,
        "updated_at": stale_ts,
        "heartbeat_at": stale_ts,
        "run_mode": "task",
    }))
    autopilot_dir = tmp_path / ".spec-state" / "autopilot"
    autopilot_dir.mkdir(parents=True)
    (autopilot_dir / "active.json").write_text(json.dumps({
        "hung-spec": {"run_id": "hung-spec-1", "phase": "implement", "pid": 12345},
    }))

    with (
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr(tui_app.autopilot, "resolve_common_root", lambda path: tmp_path)
        mp.setattr(tui_dashboard, "collect_git_spec_state", lambda repo_root: SimpleNamespace(merged_specs=set()))
        mp.setattr(tui_app.autopilot, "build_dispatch_queue", lambda *a, **kw: [])
        mp.setattr(tui_app.autopilot, "fetch_coordinator_lease_snapshot", lambda repo_root: SimpleNamespace(unavailable_message="", leases_by_spec={}))
        snapshot = tui_app.load_dashboard_snapshot(tmp_path)

    rows = {r.spec_id: r for r in snapshot.rows}
    assert "hung-spec" in rows
    assert rows["hung-spec"].status == "stale"
