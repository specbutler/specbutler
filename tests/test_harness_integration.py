"""Hermetic protocol, launch, packaging and CI review coverage."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spec_runtime import mcp_server as mcp
from spec_runtime import orchestrator as orch
from spec_runtime import review_feedback
from spec_runtime.config import SpecConfigError, _parse_review_section
from spec_runtime.coordination import CoordinatorError


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
    assert saved.blocking_finding_ids == ["ci-review::F1"]
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
    monkeypatch.setattr(orch, "_publish_ci_decision_status", Mock())
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
    assert seen == ["one", "two", "one", "two"]
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
    assert orch.ReviewResult.load(tmp_path, run.run_id).blocking_finding_ids == ["review-decision-gate::F1"]


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


@pytest.mark.parametrize("error_name", [cls.__name__ for cls in CoordinatorError.__subclasses__()])
def test_mcp_coordinator_errors_keep_server_alive(tmp_path, monkeypatch, error_name):
    from spec_runtime import coordination

    def fail(*args):
        raise getattr(coordination, error_name)("coordinator failed")

    monkeypatch.setattr(mcp, "implement_spec", fail)
    client = Client(tmp_path)
    result = client.send("tools/call", name="implement_spec", arguments={"spec_id": "demo"})
    assert result["isError"]
    assert client.send("ping") == {}


def test_success_reads_artifact_before_empty_approval(tmp_path):
    payload = {
        "status": "approved", "summary": "Resolved", "reviewed_head_sha": "head",
        "findings": [{"id": "N1", "severity": "P2", "title": "Follow up"}],
        "prior_findings": [{"id": "F1", "status": "resolved"}],
    }
    loader = Mock(return_value=payload)
    result = review_feedback.extract_review_result_from_check_run(
        {"name": "one", "conclusion": "success"}, repo_root=tmp_path,
        expected_head_sha="head", expected_base_sha="base", require_payload=False,
        load_artifact_payload=loader,
    )
    loader.assert_called_once()
    assert result.findings[0].id == "N1"
    assert result.prior_findings == payload["prior_findings"]


def test_ci_two_rounds_preserve_check_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    monkeypatch.setattr(orch, "_publish_effective_ci_approval", Mock())
    monkeypatch.setattr(orch, "_local_review_incremental_diff", lambda *a, **kw: ("diff", {}))
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, review=_parse_review_section({"source": "ci", "max_rounds": 5}),
    ))
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")

    def review(name, head, status, findings, prior=None):
        return orch.ReviewResult(
            status=status, summary=name, findings=findings, reviewed_head_sha=head,
            source_check_name=name, prior_findings=prior or [],
        )

    first = [
        review(name, "head1", "request_changes",
               [orch.ReviewFinding(id="F1", severity="P1", title=name)])
        for name in ("one", "two")
    ]
    assert orch._apply_ci_review_results(run, tmp_path, first) == "failed"
    saved = orch.ReviewResult.load(tmp_path, run.run_id)
    assert saved.blocking_finding_ids == ["one::F1", "two::F1"]
    second = [
        review("one", "head2", "approved", [], [{"id": "one::F1", "status": "resolved"}]),
        review("two", "head2", "approved", [], [{"id": "F1", "status": "resolved"}]),
    ]
    assert orch._apply_ci_review_results(run, tmp_path, second) == "passed"
    saved = orch.ReviewResult.load(tmp_path, run.run_id)
    assert saved.blocking_finding_ids == []
    assert saved.outstanding_findings == []
    assert saved.review_round == 2


@pytest.mark.parametrize("status", ["failed", "blocked"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ci_incomplete_review_wins_over_findings(tmp_path, monkeypatch, status, reverse):
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    results = [
        orch.ReviewResult(status="request_changes", summary="nonblocking",
                          findings=[orch.ReviewFinding(id="F1", severity="P2", title="Minor")],
                          source_check_name="one", reviewed_head_sha="head"),
        orch.ReviewResult(status=status, summary="incomplete", source_check_name="two",
                          reviewed_head_sha="head"),
    ]
    if reverse:
        results.reverse()
    assert orch._apply_ci_review_results(run, tmp_path, results) in {"failed", "blocked"}
    assert orch.ReviewResult.load(tmp_path, run.run_id).status == status
    assert orch.review_convergence.load_review_rounds(tmp_path / "state/runs/demo-123") == []


@pytest.mark.parametrize("author,kind,run_id,check_name,accepted", [
    ("attacker", "User", "123", "one", False),
    ("github-actions[bot]", "Bot", "999", "one", False),
    ("github-actions[bot]", "Bot", "123", "two", False),
    ("github-actions[bot]", "Bot", "123", "one", True),
])
def test_sticky_authority_bound_to_publisher_and_run(
    tmp_path, author, kind, run_id, check_name, accepted,
):
    from spec_runtime.review_gate_sticky_comment import render_sticky_comment

    payload = {
        "status": "approved", "summary": "OK", "reviewed_head_sha": "head",
        "source_check_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
        "source_check_name": check_name, "findings": [],
    }
    comment = {"user": {"login": author, "type": kind}, "body": render_sticky_comment(payload),
               "updated_at": "2026-01-01T00:01:00Z"}
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([comment]), ""))
    result = review_feedback._load_sticky_review_comment_payload(
        tmp_path, 1, run_subprocess=runner, expected_check={
            "name": "one", "details_url": "https://github.com/owner/repo/actions/runs/123",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    assert (result is not None) == accepted


def test_effective_ci_approval_changes_only_configured_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, review=_parse_review_section({"source": "ci", "checks": ["one"]}),
    ))
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda run: "owner/repo")
    monkeypatch.setattr(orch, "_find_check_run_for_sha", lambda *a, **kw: {
        "id": 42, "head_sha": "head", "status": "completed", "conclusion": "failure",
    })
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(orch, "run_subprocess", runner)
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    result = orch.ReviewResult(status="approved", summary="Accepted", reviewed_head_sha="head")
    orch._publish_effective_ci_approval(run, tmp_path, result)
    assert runner.call_args_list[0].args[0][2] == "repos/owner/repo/check-runs/42"
    assert json.loads(runner.call_args_list[1].kwargs["input_text"])["context"] == "one"
    runner.return_value = subprocess.CompletedProcess([], 1, "", "Permission denied")
    with pytest.raises(ValueError, match="Cannot publish"):
        orch._publish_effective_ci_approval(run, tmp_path, result)


def test_ci_prompt_materializes_evidence_and_history(tmp_path, monkeypatch):
    from spec_runtime.ci_review_prompt import render_ci_evidence

    outputs = ["full diff", "canonical spec", '{"check_runs": []}']
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), "")

    monkeypatch.setattr(subprocess, "run", invoke)
    rendered = render_ci_evidence(
        tmp_path, base_sha="base", head_sha="head", spec_id="demo", repo="owner/repo",
        pr_body="PR body\n<!-- spec-ci-rereview-context -->\nPrior review findings: one::F1\n"
                "Implementer response: fixed\nIncremental diff: changed validation",
    )
    for evidence in ["full diff", "canonical spec", "check_runs", "one::F1",
                     "Implementer response: fixed", "Incremental diff: changed validation"]:
        assert evidence in rendered
    assert calls[0][-2:] == ["base...head", "--"]
    assert calls[1][-1] == "base:specs/demo.md"


def test_ci_acceptance_publishes_all_outstanding_followups(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "_state_root", lambda root: root / "state")
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(
        orch.SPEC_RUNTIME_CONFIG, review=_parse_review_section({"source": "ci", "max_rounds": 1}),
    ))
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    result = orch.ReviewResult(
        status="request_changes", summary="Fix", reviewed_head_sha="head",
        source_check_name="one", findings=[orch.ReviewFinding(id="F1", severity="P1", title="Bug")],
    )
    assert orch._apply_ci_review_results(run, tmp_path, [result]) == "waiting-for-input"
    accepted = SimpleNamespace(save=Mock())
    monkeypatch.setattr(orch, "_accepted_review_round_cap_request", lambda *a: accepted)
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda run: "owner/repo")
    monkeypatch.setattr(orch, "_find_open_pr_for_branch", lambda *a, **kw: (
        {"headRefOid": "head", "number": 1, "body": "PR"}, None,
    ))
    publish = Mock()
    followups = Mock()
    monkeypatch.setattr(orch, "_publish_effective_ci_approval", publish)
    monkeypatch.setattr(orch, "_publish_review_followups", followups)
    assert orch._phase_review_action(
        run, tmp_path, expected_head_sha="head", expected_base_sha="base",
    ) == "passed"
    publish.assert_called_once()
    assert followups.call_args.kwargs["accepted_at_cap"]
    assert followups.call_args.args[2][0]["id"] == "one::F1"
    assert accepted.status == "consumed"
    assert orch.ReviewResult.load(tmp_path, run.run_id).status == "approved"


def test_effective_ci_approval_refuses_stale_head(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "_trusted_publication_repo_slug", lambda run: "owner/repo")
    monkeypatch.setattr(orch, "_find_check_run_for_sha", lambda *a, **kw: {
        "id": 42, "head_sha": "old", "status": "completed", "conclusion": "success",
    })
    runner = Mock()
    monkeypatch.setattr(orch, "run_subprocess", runner)
    run = orch.RunState(spec_id="demo", run_id="demo-123", branch="code/demo--123")
    with pytest.raises(ValueError, match="current-head"):
        orch._publish_effective_ci_approval(
            run, tmp_path, orch.ReviewResult(status="approved", summary="OK", reviewed_head_sha="new"),
        )
    runner.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="executes the Linux review workflow Bash step")
@pytest.mark.parametrize("owner,decision,outcome,expected", [
    ("ci", "request_changes", "failure", 0),
    ("ci", "approved", "success", 0),
    ("ci", "failed", "failure", 1),
    ("ci", "blocked", "failure", 1),
    ("local", "request_changes", "failure", 1),
])
def test_workflow_ci_completion_separate_from_decision(owner, decision, outcome, expected):
    import os

    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/codex-review.yml").read_text())
    steps = workflow["jobs"]["review-decision-gate"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Enforce blocking decision")
    script = script.replace("${{ steps.publish.outputs.decision }}", decision)
    script = script.replace("${{ steps.gate.outcome }}", outcome)
    result = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True, text=True,
        env={**os.environ, "PR_BODY": f"Review-Owner: {owner}\n"},
    )
    assert result.returncode == expected


@pytest.mark.parametrize("state,description,approved", [
    ("success", "Spec Butler effective review: approved", True),
    ("failure", "Spec Butler effective review: approved", False),
    ("success", "CI review approved", False),
])
def test_ci_metadata_events_preserve_exact_head_host_approval(
    tmp_path, monkeypatch, state, description, approved,
):
    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/codex-review.yml").read_text())
    job = workflow["jobs"]["determine-review-routing"]
    shell = job["steps"][0]["run"]
    script = shell.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setenv("PR_BODY", "Review-Owner: ci")
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "head")
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([
        {"context": "other-check", "state": "failure"},
        {"context": "review-decision-gate", "state": state, "description": description},
    ]), ""))
    monkeypatch.setattr(subprocess, "run", runner)
    exec(compile(script, "<workflow routing>", "exec"), {})
    assert f"ci_approved={'true' if approved else 'false'}" in output.read_text()
    assert runner.call_args.args[0][-1] == "repos/owner/repo/commits/head/statuses"
    assert "ci_approved != 'true'" in workflow["jobs"]["review-decision-gate"]["if"]


def test_ci_new_spec_authoring_uses_diff_without_base_spec(tmp_path, monkeypatch):
    from spec_runtime.ci_review_prompt import render_ci_evidence

    def invoke(argv, **kwargs):
        if "show" in argv:
            raise subprocess.CalledProcessError(128, argv, stderr="path does not exist at base")
        return subprocess.CompletedProcess(argv, 0, "new spec diff", "")

    monkeypatch.setattr(subprocess, "run", invoke)
    rendered = render_ci_evidence(
        tmp_path, base_sha="base", head_sha="head", spec_id="new-spec", repo="owner/repo", pr_body="",
    )
    assert "new spec diff" in rendered
    assert "No canonical specs/new-spec.md exists at the base commit" in rendered
    assert "spec-authoring PR" in rendered


def test_ci_workflow_bootstrap_is_self_contained_and_matches_helper():
    import ast

    import yaml

    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/codex-review.yml').read_text())
    steps = workflow['jobs']['review-decision-gate']['steps']
    shell = next(s['run'] for s in steps if s.get('name') == 'Materialize trusted review code and prompt')
    script = shell.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    tree = ast.parse(script)
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'render_ci_evidence')
    source = ast.parse((root / 'src/spec_runtime/ci_review_prompt.py').read_text())
    expected = next(n for n in source.body if isinstance(n, ast.FunctionDef))
    assert ast.dump(helper) == ast.dump(expected)
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert imports == ['pathlib', 'spec_runtime.spec_identity']
    # Compile the embedded helper with stdlib only: no new base module is needed.
    namespace = {'Path': Path, 'subprocess': subprocess, 're': __import__('re')}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), '<bootstrap>', 'exec'), namespace)
    assert callable(namespace['render_ci_evidence'])


def test_ci_prompt_reconstructs_large_incremental_diff(tmp_path, monkeypatch):
    from spec_runtime.ci_review_prompt import render_ci_evidence

    previous = 'a' * 40
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        output = 'x' * 80000 if f'{previous}..head' in argv else 'evidence'
        return subprocess.CompletedProcess(argv, 0, output, '')

    monkeypatch.setattr(subprocess, 'run', invoke)
    rendered = render_ci_evidence(
        tmp_path, base_sha='base', head_sha='head', spec_id='', repo='owner/repo',
        pr_body=f'<!-- spec-ci-rereview-context -->\nPrevious-reviewed-head: {previous}\nF1',
    )
    assert 'x' * 80000 in rendered
    assert calls[-1][-2:] == [f'{previous}..head', '--']
    assert '--no-ext-diff' in calls[-1]
    assert 'Re-review blocking rule' in rendered


def test_ci_pr_metadata_is_bounded_and_records_previous_head():
    body = orch._ci_review_pr_body('b' * 80000, context='f' * 80000, previous_head='a' * 40)
    assert len(body) < 65536
    assert 'Previous-reviewed-head: ' + 'a' * 40 in body
    assert 'History truncated' in body
    assert 'BEGIN_INCREMENTAL_DIFF' not in body
    assert orch._ci_review_pr_body('description', context='F1', previous_head='a' * 40).endswith('F1')


@pytest.mark.parametrize('previous', ['a' * 40, '--output=/tmp/untrusted'])
def test_ci_prompt_unavailable_or_invalid_previous_head(tmp_path, monkeypatch, previous):
    from spec_runtime.ci_review_prompt import render_ci_evidence

    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        if f'{previous}..head' in argv:
            raise subprocess.CalledProcessError(128, argv)
        return subprocess.CompletedProcess(argv, 0, 'evidence', '')

    monkeypatch.setattr(subprocess, 'run', invoke)
    rendered = render_ci_evidence(
        tmp_path, base_sha='base', head_sha='head', spec_id='', repo='owner/repo',
        pr_body=f'<!-- spec-ci-rereview-context -->\nPrevious-reviewed-head: {previous}\n',
    )
    if previous.startswith('--'):
        assert len(calls) == 2
    else:
        assert 'Previous head unavailable' in rendered
