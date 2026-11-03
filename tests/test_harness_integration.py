"""Hermetic protocol, launch, packaging and CI review coverage."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spec_runtime import mcp_server as mcp
from spec_runtime import orchestrator as orch
from spec_runtime import review_feedback
from spec_runtime.config import SpecConfigError, _parse_review_section


class Client:
    def __init__(self, root):
        self.root = root
        self.sequence = 0

    def send(self, method, **params):
        self.sequence += 1
        return mcp.handle_request(
            self.root,
            {
                "jsonrpc": "2.0",
                "id": self.sequence,
                "method": method,
                "params": params,
            },
        )["result"]

    def call(self, name, **arguments):
        result = self.send("tools/call", name=name, arguments=arguments)
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])


def test_mcp_client_tools(tmp_path, monkeypatch):
    client = Client(tmp_path)
    assert client.send("initialize")["capabilities"] == {"tools": {}}
    definitions = client.send("tools/list")["tools"]
    assert {t["name"] for t in definitions} == set(mcp._DESCRIPTIONS)
    assert all(t["description"] for t in definitions)
    monkeypatch.setattr(mcp.auto, "build_dispatch_queue", lambda root: [])
    monkeypatch.setattr(mcp, "iter_spec_metadata", lambda root: [])
    assert client.call("list_specs", ready_only=True) == []
    assert client.call("list_specs") == []
    from spec_runtime import spec_lint

    monkeypatch.setattr(spec_lint, "lint_specs", lambda root, spec: [])
    assert client.call("lint_spec", spec_id="demo") == []
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "command output", ""))
    monkeypatch.setattr(mcp.subprocess, "run", runner)
    for tool in ["spec_status", "stop_run", "attach_command"]:
        assert client.call(tool, spec_id="demo") == {"output": "command output"}
    client.call("steer_run", spec_id="demo", message="Fix the parser")
    assert runner.call_args.args[0][-2:] == ["--message", "Fix the parser"]
    monkeypatch.setattr(mcp, "implement_spec", lambda root, spec: {"run_id": "demo-123"})
    assert client.call("implement_spec", spec_id="demo") == {"run_id": "demo-123"}
    result = client.send("tools/call", name="stop_run", arguments={"spec_id": "../bad"})
    assert result["isError"]


def test_detached_dispatch_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp.auto, "autopilot_state_root", lambda root: root / "state")
    monkeypatch.setattr(orch.RunState, "list_for_spec", lambda root, spec: [])
    candidate = SimpleNamespace(spec_id="demo")
    monkeypatch.setattr(mcp.auto, "build_dispatch_queue", lambda root: [candidate])
    monkeypatch.setattr(mcp.auto, "acquire_candidate_lease", lambda *args: ("demo-123", None))
    proc = mcp.auto.ActiveRunProcess(
        spec_id="demo",
        agent="codex",
        pid=123,
        started_at="now",
        started_monotonic=0,
        log_path="run.log",
        run_id="demo-123",
        process_started_at="identity",
    )
    start = Mock(return_value=proc)
    monkeypatch.setattr(mcp.auto, "start_candidate", start)
    monkeypatch.setattr(mcp.auto, "is_pid_alive", lambda pid, started: (pid, started) == (123, "identity"))
    assert mcp.implement_spec(tmp_path, "demo") == {"run_id": "demo-123"}
    assert mcp.implement_spec(tmp_path, "demo") == {"run_id": "demo-123"}
    assert start.call_count == 1
    assert start.call_args.kwargs["preallocated_run_id"] == "demo-123"


def test_plugin_files():
    root = Path(__file__).parents[1] / "integrations/claude-code"
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "specbutler"
    wiring = json.loads((root / ".mcp.json").read_text())["mcpServers"]["specbutler"]
    assert wiring == {"command": "spec", "args": ["mcp", "serve"]}
    assert (root / "skills/spec-workflow/SKILL.md").read_text().startswith("---\n")
    assert "[mcp_servers.specbutler]" in (root / "README.md").read_text()


def test_review_source_config():
    assert _parse_review_section(None).source == "local"
    assert _parse_review_section({"source": "ci", "checks": ["one", "two"]}).checks == ("one", "two")
    for payload in [{"source": "invalid"}, {"checks": []}, {"checks": "one"}]:
        with pytest.raises(SpecConfigError):
            _parse_review_section(payload)


def test_ci_fixture_parsing_and_convergence(tmp_path, monkeypatch):
    payload = {
        "status": "request_changes",
        "reviewed_head_sha": "head",
        "reviewed_base_sha": "base",
        "summary": "Fix validation",
        "findings": [{"id": "F1", "severity": "P1", "title": "Input bug"}],
    }
    check = {"name": "ci-review", "conclusion": "failure", "output": {"text": json.dumps(payload)}}
    result = review_feedback.extract_review_result_from_check_run(
        check,
        expected_head_sha="head",
        expected_base_sha="base",
        require_payload=True,
    )
    monkeypatch.setattr(
        orch,
        "SPEC_RUNTIME_CONFIG",
        replace(
            orch.SPEC_RUNTIME_CONFIG,
            review=_parse_review_section({"source": "ci", "max_rounds": 1}),
        ),
    )
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    assert orch._apply_ci_review_results(run, tmp_path, [result]) == "waiting-for-input"
    assert run.status == "waiting-for-input"
    saved = orch.ReviewResult.load(tmp_path, run.run_id)
    assert saved.review_round == 1
    assert saved.blocking_finding_ids == ["F1"]
    with pytest.raises(ValueError, match="head SHA mismatch"):
        review_feedback.extract_review_result_from_check_run(
            check,
            expected_head_sha="other",
            expected_base_sha="base",
            require_payload=True,
        )


def test_ci_waits_for_every_configured_check(tmp_path, monkeypatch):
    monkeypatch.setattr(
        orch,
        "SPEC_RUNTIME_CONFIG",
        replace(
            orch.SPEC_RUNTIME_CONFIG,
            review=_parse_review_section({"source": "ci", "checks": ["one", "two"]}),
        ),
    )
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    monkeypatch.setattr(orch, "_accepted_review_round_cap_request", lambda *a: None)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda run: "owner/repo")
    monkeypatch.setattr(
        orch,
        "_find_open_pr_for_branch",
        lambda *a, **kw: (
            {"headRefOid": "head", "number": 1},
            None,
        ),
    )
    seen = []

    def find(root, head, name, **kwargs):
        seen.append(name)
        return {
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "output": {
                "text": json.dumps(
                    {
                        "status": "approved",
                        "summary": name,
                        "reviewed_head_sha": head,
                        "findings": [],
                    }
                )
            },
        }

    monkeypatch.setattr(orch, "_find_check_run_for_sha", find)
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    assert orch._phase_review_action(run, tmp_path, expected_head_sha="head", expected_base_sha="base") == "passed"
    assert seen == ["one", "two"]
    assert orch.ReviewResult.load(tmp_path, run.run_id).source_check_name == "one, two"


def test_ci_sticky_comment_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        orch,
        "SPEC_RUNTIME_CONFIG",
        replace(
            orch.SPEC_RUNTIME_CONFIG,
            review=_parse_review_section({"source": "ci"}),
        ),
    )
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    monkeypatch.setattr(orch, "_accepted_review_round_cap_request", lambda *a: None)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda run: "owner/repo")
    monkeypatch.setattr(
        orch,
        "_find_open_pr_for_branch",
        lambda *a, **kw: (
            {"headRefOid": "head", "number": 1},
            None,
        ),
    )
    monkeypatch.setattr(
        orch,
        "_find_check_run_for_sha",
        lambda *a, **kw: {
            "name": "review-decision-gate",
            "status": "completed",
            "conclusion": "failure",
        },
    )
    monkeypatch.setattr(orch, "_extract_review_result_from_check_run", Mock(side_effect=ValueError("No output")))
    monkeypatch.setattr(
        review_feedback,
        "_load_sticky_review_comment_payload",
        lambda *a, **kw: {
            "reviewed_head_sha": "head",
            "status": "request_changes",
            "findings": [
                {"id": "F1", "severity": "P1", "title": "Input validation"},
            ],
        },
    )
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    assert orch._phase_review_action(run, tmp_path, expected_head_sha="head", expected_base_sha="base") == "failed"
    assert orch.ReviewResult.load(tmp_path, run.run_id).blocking_finding_ids == ["F1"]


def test_stdio_transport(tmp_path, monkeypatch):
    import io

    source = io.StringIO(
        "\n".join(
            json.dumps(r)
            for r in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        + "\n"
    )
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", source)
    monkeypatch.setattr(mcp.sys, "stdout", output)
    assert mcp.serve(tmp_path) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert len(responses[1]["result"]["tools"]) == 7
