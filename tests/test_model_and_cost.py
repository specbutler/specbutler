"""Hermetic model selection, usage receipts and budget regression tests."""
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from spec_runtime import doctor
from spec_runtime import orchestrator as orch
from spec_runtime.config import AgentConfig, BudgetConfig, SpecConfigError, load_spec_runtime_config
from spec_runtime.usage import effective_usage, grouped, new_launch, parse_event, totals, update_launch


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("role", ["implement", "review", "debugger", "authoring"])
def test_models_per_role(monkeypatch, provider, role):
    agents = AgentConfig(models={role: {provider: "selected-model"}})
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, agents=agents))
    assert orch._model_command([provider, "exec"], role, provider) == [
        provider, "--model", "selected-model", "exec"
    ]
    assert orch._model_command([provider, "exec"], role, "other") == [provider, "exec"]


def test_escalation_and_no_duplicate_flags(monkeypatch):
    agents = AgentConfig(models={"implement": {"codex": "small", "escalate_after_attempts": 2,
                                               "escalate_model": {"codex": "large"}}})
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, agents=agents))
    assert agents.model_for("implement", "codex", 1) == "small"
    cmd = orch._model_command(["codex", "--model", "small", "exec"], "implement", "codex", 2)
    assert cmd == ["codex", "--model", "large", "exec"]


def test_config_validation(tmp_path):
    path = tmp_path / ".spec.toml"
    path.write_text('[agents.models.implement]\ncodex = "small"\nescalate_after_attempts = 2\nescalate_model = "large"\n[budget]\nmax_cost_usd_per_run = 3.5\n')
    config = load_spec_runtime_config(config_path=path, env={})
    assert config.agents.model_for("implement", "codex", 2) == "large"
    assert config.budget.max_cost_usd_per_run == 3.5
    path.write_text('[budget]\nmax_cost_usd_per_run = nan\n')
    with pytest.raises(SpecConfigError):
        load_spec_runtime_config(config_path=path, env={})


def test_claude_subagents_and_resumed_session():
    event = {"type": "result", "session_id": "session", "total_cost_usd": 1.25,
             "usage": {"input_tokens": 1}, "modelUsage": {
                 "main": {"inputTokens": 10, "outputTokens": 4, "cacheReadInputTokens": 7},
                 "subagent": {"inputTokens": 20, "outputTokens": 5, "cacheCreationInputTokens": 8}}}
    first = new_launch("implement", "claude", "main", "implement")
    update_launch(first, parse_event("claude", event))
    assert first["input_tokens"] == 30
    assert first["cache_creation_tokens"] == 8
    second = new_launch("implement", "claude", "main", "implement")
    update_launch(second, parse_event("claude", {**event, "total_cost_usd": 2.5}))
    assert effective_usage([first, second]) == [second]
    assert totals([first, second])["estimated_cost_usd"] == 2.5
    assert grouped([first, second], "role")["implement"]["input_tokens"] == 30


def test_codex_fixture_events():
    launch = new_launch("review", "codex", "test", "review")
    tracker = orch.AgentProgressTracker(agent="codex")
    event = {"type": "turn.completed", "usage": {
        "input_tokens": 110, "output_tokens": 12, "cached_input_tokens": 80}}
    for _ in range(2):
        orch._handle_codex_stream_line(json.dumps(event), tracker)
    for parsed in tracker.usage_events:
        update_launch(launch, parsed)
    assert launch["input_tokens"] == 220
    assert launch["cache_read_tokens"] == 160
    assert launch["estimated_cost_usd"] is None
    assert parse_event("codex", {"type": "item.completed"}) is None


def test_budget_remaining_and_nonretryable_failure(monkeypatch):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=2)))
    run = orch.RunState(run_id="run", spec_id="spec", branch="branch")
    launch = new_launch("implement", "claude", "test", "implement")
    launch["estimated_cost_usd"] = 0.75
    run.usage.append(launch)
    assert orch._budget_command(["claude", "-p"], run)[1:3] == ["--max-budget-usd", "1.25"]
    assert orch._budget_command(["codex", "exec"], run) == ["codex", "exec"]
    launch["estimated_cost_usd"] = 2
    with pytest.raises(orch.BudgetExceeded):
        orch._budget_command(["claude", "-p"], run)
    run.last_error = "budget_exceeded"
    classification = orch.classify_failure(run.last_error)
    assert classification.kind == "budget_exceeded"
    assert not classification.transient
    assert orch._classify_phase_result(run, "implement", "blocked")["retryable"] is False


@pytest.mark.parametrize("key,provider,output", [
    ("ANTHROPIC_API_KEY", "claude", '{"loggedIn": true}'),
    ("OPENAI_API_KEY", "codex", "Logged in using ChatGPT"),
    ("CODEX_API_KEY", "codex", "Logged in using ChatGPT"),
])
def test_doctor_billing_warning(monkeypatch, key, provider, output):
    monkeypatch.setenv(key, "secret-never-disclosed")
    check = doctor._provider_auth_check(
        provider, provider, Path("."),
        lambda *args: subprocess.CompletedProcess([], 0, output, ""), required=True)
    assert "API billing" in check.detail
    assert "secret-never-disclosed" not in check.detail


def test_usage_subprocess_persists_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(orch.RunState, "save", lambda self, root: None)
    event = {"type": "result", "session_id": "s", "usage": {"input_tokens": 9}, "total_cost_usd": 0.2}
    monkeypatch.setattr(orch, "_run_local_review_subprocess", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, json.dumps(event), ""))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    orch._run_usage_subprocess(run, "review", "claude", tmp_path, ["claude", "-p"])
    assert run.usage[0]["input_tokens"] == 9
    assert run.usage[0]["role"] == "review"
    restored = orch.RunState.from_dict({"run_id": "r", "spec_id": "s", "branch": "b", "usage": run.usage})
    assert restored.usage == run.usage


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_real_command_builders_select_distinct_models(monkeypatch, tmp_path, provider):
    agents = AgentConfig(models={role: {provider: role + "-model"}
                                 for role in ("implement", "review", "debugger", "authoring")})
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, agents=agents))
    commands = {
        "implement": orch._build_agent_command(provider, tmp_path),
        "review": orch._build_local_review_command(
            prompt="review", schema_path=tmp_path / "schema.json",
            output_path=tmp_path / "review.json", agent_name=provider),
        "debugger": orch._build_block_debugger_command(
            prompt="debug", schema_path=tmp_path / "schema.json",
            output_path=tmp_path / "debug.json", agent_name=provider),
        "authoring": orch._build_task_scoping_command(provider, tmp_path, "scope", "start"),
    }
    for role, command in commands.items():
        assert command[command.index("--model") + 1] == role + "-model"
    if provider == "claude":
        assert commands["review"][commands["review"].index("--output-format") + 1] == "json"
    else:
        assert "--json" in commands["review"]


def test_claude_result_body_is_preserved_for_reviewer(monkeypatch, tmp_path):
    monkeypatch.setattr(orch.RunState, "save", lambda self, root: None)
    body = json.dumps({"decision": "approved", "summary": "review"})
    event = {"type": "result", "result": body, "total_cost_usd": 0.5,
             "usage": {"input_tokens": 5}}
    monkeypatch.setattr(orch, "_run_local_review_subprocess", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, json.dumps(event), ""))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    result = orch._run_usage_subprocess(run, "review", "claude", tmp_path, ["claude", "-p"])
    assert result.stdout == body
    assert run.usage[0]["estimated_cost_usd"] == 0.5


def test_budget_blocks_before_provider_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=1)))
    monkeypatch.setattr(orch, "_run_local_review_subprocess", lambda *a, **kw: pytest.fail("launched over budget"))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    launch = new_launch("implement", "claude", "test", "implement")
    update_launch(launch, parse_event("claude", {"type": "result", "total_cost_usd": 1}))
    run.usage.append(launch)
    with pytest.raises(orch.BudgetExceeded):
        orch._run_usage_subprocess(run, "review", "claude", tmp_path, ["claude", "-p"])
    assert len(run.usage) == 1


def test_authoring_receipts_do_not_become_workflow_resume_candidates(tmp_path):
    implementation = orch.RunState(run_id="implementation", spec_id="s", branch="code/s")
    implementation.save(tmp_path)
    authoring = orch.RunState(run_id="authoring", spec_id="s", branch="spec/s", phase="authoring")
    authoring.save(tmp_path)
    assert not authoring.is_workflow_resumable
    assert not authoring.is_step_selectable
    assert orch.RunState.find_latest(tmp_path, "s").run_id == "implementation"
    assert len(orch.RunState.list_all(tmp_path)) == 2
    summary = orch._usage_analytics([implementation, authoring])
    assert summary["total"]["estimated_cost_usd"] is None


def test_claude_budget_result_blocks_even_without_reported_cost(monkeypatch):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=1)))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    launch = new_launch("implement", "claude", "test", "implement")
    update_launch(launch, parse_event("claude", {"type": "result", "subtype": "error_max_budget_usd"}))
    run.usage.append(launch)
    with pytest.raises(orch.BudgetExceeded):
        orch._check_budget(run)


def test_implement_launch_records_usage_and_escalates(monkeypatch, tmp_path):
    calls = []
    agents = AgentConfig(models={"implement": {"claude": "small", "escalate_after_attempts": 2,
                                               "escalate_model": "large"}})
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, agents=agents))

    class Backend:
        def launch_agent(self, request, **kwargs):
            calls.append(request.argv)
            return subprocess.CompletedProcess(request.argv, 0)

    monkeypatch.setattr(orch, "_resolve_execution_backend", Backend)
    monkeypatch.setattr(orch, "_record_live_provider_sessions", lambda *args: False)
    monkeypatch.setattr(orch, "_reconcile_and_cleanup_agent_git", lambda *args: None)
    monkeypatch.setattr(orch, "_remove_claude_isolated_auth", lambda *args: None)
    monkeypatch.setattr(orch, "_sync_orchestrator_paths_into_workspace", lambda *args, **kw: None)
    tracker = orch.AgentProgressTracker(agent="claude")
    orch._handle_claude_stream_line(json.dumps({
        "type": "result", "usage": {"input_tokens": 10}, "total_cost_usd": 0.3,
        "session_id": "session"}), tracker)
    plan = orch.ImplementLaunchPlan(False, {}, ["claude", "-p"], {}, progress_tracker=tracker)
    run = orch.RunState(run_id="r", spec_id="s", branch="b", attempts=5, implement_failures=2, phase="implement")
    assert orch._launch_implement_attempt(run, tmp_path, tmp_path, plan) == 0
    assert calls[0][1:3] == ["--model", "large"]
    saved = orch.RunState.load(tmp_path, "r")
    assert saved.usage[0]["estimated_cost_usd"] == 0.3
    assert saved.usage[0]["input_tokens"] == 10
    assert saved.usage[0]["model"] == "large"


def test_phase_budget_is_blocked_and_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=1)))
    monkeypatch.setattr(orch, "_backend_resume_mismatch", lambda *args: "")
    monkeypatch.setattr(orch, "_refresh_active_run_lease", lambda *args: None)
    monkeypatch.setattr(orch, "_run_lease_heartbeat_loop", lambda *args: None)
    monkeypatch.setitem(orch.PHASE_HANDLERS, "implement", lambda *args: pytest.fail("over-budget phase ran"))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    launch = new_launch("implement", "claude", "test", "implement")
    launch["estimated_cost_usd"] = 1
    run.usage.append(launch)
    assert orch.run_single_phase(run, "implement", tmp_path) == "blocked"
    saved = orch.RunState.load(tmp_path, "r")
    assert saved.status == "blocked"
    assert saved.last_failure_subtype == "budget_exceeded"
    assert saved.last_failure_retryable is False
    assert saved.attempts == 0


def test_budget_block_does_not_launch_debugger(monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "_maybe_run_block_debugger", lambda *a, **kw: pytest.fail("budget diagnosis launched"))
    run = orch.RunState(run_id="r", spec_id="s", branch="b", last_error="budget_exceeded")
    assert orch._transition_to_blocked_with_debugger(run, tmp_path, source_phase="implement") == "blocked"
    assert run.last_error == "budget_exceeded"


def test_budget_limit_increase_clears_provider_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=1)))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    launch = orch._begin_usage(run, tmp_path, "implement", "claude", ["claude"])
    update_launch(launch, parse_event("claude", {"type": "result", "subtype": "error_max_budget_usd"}))
    with pytest.raises(orch.BudgetExceeded):
        orch._check_budget(run)
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, budget=BudgetConfig(max_cost_usd_per_run=2)))
    orch._check_budget(run)


def test_monthly_snapshots_keep_previous_month():
    first = new_launch("implement", "claude", "test", "implement")
    update_launch(first, parse_event("claude", {"type": "result", "session_id": "s", "total_cost_usd": 5,
                                             "usage": {"input_tokens": 50}}))
    first["recorded_at"] = "2026-10-01"
    second = dict(first, recorded_at="2026-11-01", estimated_cost_usd=7, input_tokens=70)
    months = grouped([second, first], "recorded_at")
    assert months["2026-10"]["estimated_cost_usd"] == 5
    assert months["2026-11"]["estimated_cost_usd"] == 2
    assert months["2026-11"]["input_tokens"] == 20


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_interactive_usage_reads_provider_transcript(monkeypatch, tmp_path, provider):
    from spec_runtime.usage import transcript_snapshot
    cwd = tmp_path / "work"
    cwd.mkdir()
    env = {"HOME": str(tmp_path)}
    root, _ = transcript_snapshot(provider, cwd, env)
    root.mkdir(parents=True)
    def launch(cmd, **kwargs):
        if provider == "claude":
            events = [{"type": "assistant", "uuid": "a", "message": {
                "id": "m", "model": "claude-sonnet-4-5", "usage": {"input_tokens": 100, "output_tokens": 10}}}]
        else:
            events = [{"type": "session_meta", "payload": {"cwd": str(cwd)}},
                      {"type": "event_msg", "payload": {"type": "token_count", "info": {
                          "total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}}]
        # Providers write raw UTF-8; "\u014d" encodes to a byte cp1252 cannot decode.
        events.insert(0, {"type": "user", "message": {"content": "snow \u96ea in T\u014dky\u014d"}})
        (root / "session.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(orch, "_run_interactive_agent_owned", launch)
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    orch._run_interactive_usage(run, tmp_path, provider, [provider], cwd=cwd, env=env)
    assert run.usage[0]["input_tokens"] == 100
    assert run.usage[0]["output_tokens"] == 10
    assert run.usage[0]["usage_available"]
    if provider == "claude":
        assert run.usage[0]["estimated_cost_usd"] == pytest.approx(.00045)


def test_successful_steering_does_not_escalate(monkeypatch):
    agents = AgentConfig(models={"implement": {"claude": "small", "escalate_after_attempts": 1,
                                               "escalate_model": "large"}})
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, agents=agents))
    run = orch.RunState(run_id="r", spec_id="s", branch="b", attempts=10)
    failures = run.implement_failures + run.verify_failures + run.review_changes
    assert orch._model_command(["claude"], "implement", run.agent, failures)[2] == "small"


def test_authoring_receipts_are_excluded_from_workflow_readers(monkeypatch, tmp_path):
    from spec_runtime import autopilot, spec_status
    from spec_runtime.config import SpecRuntimeConfig
    config = SpecRuntimeConfig()
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", config)
    run = orch.RunState(run_id="author", spec_id="s", branch="spec/s", phase="authoring", status="failed")
    run.save(tmp_path)
    monkeypatch.setattr(spec_status, "_state_runs_dir", lambda *a, **k: orch._state_root(tmp_path) / "runs")
    assert spec_status._load_active_runs(tmp_path, config=config) == {}
    assert spec_status._load_runs_for_spec(tmp_path, "s", config=config) == ()
    monkeypatch.setattr(autopilot, "runs_dir", lambda *a, **k: orch._state_root(tmp_path) / "runs")
    assert not autopilot.load_run_record_index(tmp_path, config=config).by_run_id


def test_gc_does_not_publish_merge_tag_for_authoring(monkeypatch, tmp_path):
    import argparse
    from types import SimpleNamespace

    from spec_runtime import autopilot
    monkeypatch.setattr(autopilot, "resolve_repo_root", lambda *a: tmp_path)
    monkeypatch.setattr(autopilot, "resolve_common_root", lambda *a: tmp_path)
    monkeypatch.setattr(autopilot, "collect_git_spec_state", lambda *a: SimpleNamespace(merged_specs={"s"}))
    run = orch.RunState(run_id="author", spec_id="s", branch="spec/s", phase="authoring", status="failed")
    path = run.save(tmp_path)
    before = path.read_text()
    assert autopilot.gc_command(argparse.Namespace(repo_root=str(tmp_path), apply=True)) == 0
    assert path.read_text() == before


def test_interactive_claude_resume_keeps_subagents_and_months(monkeypatch, tmp_path):
    from spec_runtime.usage import transcript_snapshot
    env = {"HOME": str(tmp_path)}
    root, _ = transcript_snapshot("claude", tmp_path, env)
    sub = root / "session" / "subagents"
    sub.mkdir(parents=True)
    def message(tokens):
        return json.dumps({"type": "assistant", "message": {"id": "m", "model": "claude-sonnet-4-5",
                                                               "usage": {"input_tokens": tokens}}}) + "\n"
    (sub / "agent-sub.jsonl").write_text(message(20))
    main = root / "session.jsonl"
    main.write_text(message(100))
    run = orch.RunState(run_id="r", spec_id="s", branch="b")
    def invoke(cmd, **kwargs):
        with main.open("a") as stream:
            stream.write(message(200))
        return subprocess.CompletedProcess(cmd, 0)
    # First launch creates the transcripts; snapshot must precede their creation.
    from spec_runtime.usage import collect_transcript_usage
    first = new_launch("authoring", "claude", "", "authoring")
    collect_transcript_usage(first, tmp_path, (root, {}))
    first["recorded_at"] = "2026-10-01"
    run.usage.append(first)
    monkeypatch.setattr(orch, "_run_interactive_agent_owned", invoke)
    orch._run_interactive_usage(run, tmp_path, "claude", ["claude"], cwd=tmp_path, env=env)
    run.usage[-1]["recorded_at"] = "2026-11-01"
    assert totals(run.usage)["input_tokens"] == 220
    assert grouped(run.usage, "recorded_at")["2026-11"]["input_tokens"] == 100


def test_codex_interactive_resume_counts_delta_and_ignores_other_cwd(tmp_path):
    from spec_runtime.usage import collect_transcript_usage, transcript_snapshot
    env = {"HOME": str(tmp_path)}
    root, _ = transcript_snapshot("codex", tmp_path, env)
    root.mkdir(parents=True)
    def rollout(cwd, tokens):
        return "\n".join(json.dumps(e) for e in [
            {"type": "session_meta", "payload": {"cwd": str(cwd)}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": tokens, "output_tokens": 10}}}}]) + "\n"
    path = root / "session.jsonl"
    path.write_text(rollout(tmp_path, 100))
    snapshot = transcript_snapshot("codex", tmp_path, env)
    with path.open("a") as stream:
        stream.write(rollout(tmp_path, 150))
    (root / "other.jsonl").write_text(rollout(tmp_path / "other", 999))
    launch = new_launch("authoring", "codex", "", "scoping")
    collect_transcript_usage(launch, tmp_path, snapshot)
    assert launch["input_tokens"] == 50
    assert launch["output_tokens"] == 0
