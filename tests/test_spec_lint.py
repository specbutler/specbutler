"""Hermetic spec quality, authoring and single-PR coverage."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spec_runtime import cli
from spec_runtime import orchestrator as orch
from spec_runtime.config import SpecConfigError, load_repo_spec_runtime_config
from spec_runtime.spec_lint import critique, lint_specs


def write_spec(root: Path, *, frontmatter: str = "", body: str | None = None) -> Path:
    path = root / "specs" / "feature.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "---\nid: feature\ndepends_on: []\n"
        + frontmatter
        + "\n---\n"
        + (
            body
            if body is not None
            else "\n## Goal\nShip the feature.\n\n## Acceptance Criteria\n- [ ] Works\n\n## Out of Scope\nOther work.\n"
        )
    )
    return path


@pytest.mark.parametrize(
    ("frontmatter", "body", "message"),
    [
        ("priority: true", None, "priority has an invalid type"),
        ("area: 5", None, "area has an invalid type"),
        ("depends_on: wrong", None, "depends_on has an invalid type"),
        ("depends_on: [missing]", None, "unknown spec"),
        ("depends_on: [feature]", None, "dependency cycle"),
        ("id: other", None, "filename stem"),
        ("", "## Acceptance Criteria\n- [ ] Works", "missing Goal"),
        ("", "## Goal\nWorks", "missing Acceptance Criteria"),
        ("", "## Goal\nWorks\n## Acceptance Criteria\nNarrative", "zero parseable"),
    ],
)
def test_errors(tmp_path, frontmatter, body, message):
    write_spec(tmp_path, frontmatter=frontmatter, body=body)
    assert any(message in error for error in lint_specs(tmp_path)[0].errors)


def test_warnings_do_not_become_errors(tmp_path):
    write_spec(
        tmp_path,
        frontmatter="custom: true",
        body=("## Goal\nWorks\n## Acceptance Criteria\n" + "\n".join(f"- [ ] Criterion {i}" for i in range(9))),
    )
    result = lint_specs(tmp_path)[0]
    assert not result.errors
    assert len(result.warnings) == 3


def test_json_and_exit_status(tmp_path, monkeypatch, capsys):
    write_spec(tmp_path)
    monkeypatch.setattr(cli, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_lazy_config", lambda: load_repo_spec_runtime_config(tmp_path))
    assert cli.main(["lint", "--spec", "feature", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["errors"] == []
    assert cli.main(["lint", "--spec", "missing"]) == 1


def test_implement_refuses_errors_before_workflow(tmp_path, monkeypatch, capsys):
    write_spec(tmp_path, body="## Goal\nIncomplete")
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: tmp_path)
    workflow = Mock()
    monkeypatch.setattr(orch, "run_full_workflow", workflow)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    assert orch.cmd_run(argparse.Namespace(spec="feature", agent="codex", base="origin/main")) == 1
    workflow.assert_not_called()
    assert "missing Acceptance Criteria" in capsys.readouterr().out


def test_autopilot_reports_lint_errors(tmp_path):
    from spec_runtime.autopilot import spec_graph_skip_reasons
    from spec_runtime.spec_metadata import iter_spec_metadata

    write_spec(tmp_path, body="## Goal\nIncomplete")
    reasons = spec_graph_skip_reasons(tmp_path, iter_spec_metadata(tmp_path))
    assert "zero parseable" in reasons["feature"]


def test_prompt_fallback_and_override(tmp_path):
    from spec_runtime.init import _read_bundled_template

    prompt = orch._load_spec_creation_prompt(tmp_path, spec_id="feature", branch="spec/feature", resume=False)
    assert _read_bundled_template("spec-creation.md").strip() in prompt
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/spec-creation.md").write_text("Operator custom prompt")
    assert "Operator custom prompt" in orch._load_spec_creation_prompt(
        tmp_path, spec_id="feature", branch="spec/feature", resume=False
    )


def test_single_pr_config_defaults_and_validation(tmp_path):
    assert load_repo_spec_runtime_config(tmp_path).specs.require_merged_spec is True
    (tmp_path / ".spec.toml").write_text("[specs]\nrequire_merged_spec = false\n")
    assert load_repo_spec_runtime_config(tmp_path).specs.require_merged_spec is False
    (tmp_path / ".spec.toml").write_text('[specs]\nrequire_merged_spec = "false"\n')
    with pytest.raises(SpecConfigError):
        load_repo_spec_runtime_config(tmp_path)


def test_single_pr_restores_and_commits_local_spec(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base")
    source = write_spec(tmp_path)
    run = orch.RunState(
        run_id="feature-test", spec_id="feature", branch="code/feature--test", spec_path="specs/feature.md"
    )
    orch._pin_run_spec_from_file(tmp_path, run, source, tree_root=tmp_path)
    source.unlink()
    restored = orch._restore_pinned_spec_into_worktree(tmp_path, run, tmp_path)
    # Supply identity per command without changing repository Git configuration.
    import os

    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    assert orch._ensure_run_spec_committed(run, worktree_path=tmp_path, spec_path=restored, git_env=env)
    assert "id: feature" in git("show", "HEAD:specs/feature.md").stdout


def test_critique_uses_restricted_adapter_and_is_advisory(tmp_path, monkeypatch, capsys):
    from spec_runtime import agent_adapter

    write_spec(tmp_path)
    adapter = Mock()
    adapter.build_review_command.return_value = ["review-agent", "--read-only"]
    monkeypatch.setattr(agent_adapter, "get_agent_adapter", lambda _: adapter)
    suggestions = {key: [] for key in ("ambiguities", "untestable_criteria", "missing_out_of_scope", "decomposition")}
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(suggestions), ""))
    monkeypatch.setattr(subprocess, "run", runner)
    assert critique(tmp_path, "feature") == suggestions
    assert "id: feature" in runner.call_args.kwargs["input"]
    runner.side_effect = OSError("unavailable")
    assert "advisory" in critique(tmp_path, "feature")["error"]


def test_task_prompt_fallback_and_override(tmp_path):
    from spec_runtime.init import _read_bundled_template

    prompt = orch._load_task_scoping_prompt(tmp_path, tmp_path, resume=False)
    assert _read_bundled_template("task-scoping.md").strip() in prompt
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/task-scoping.md").write_text("Custom task prompt")
    assert "Custom task prompt" in orch._load_task_scoping_prompt(tmp_path, tmp_path, resume=False)


def test_implement_default_requires_base_spec(tmp_path, monkeypatch, capsys):
    write_spec(tmp_path)
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    with pytest.raises(RuntimeError, match="merge it first"):
        orch._validate_new_spec_run(tmp_path, "feature", "origin/main")


def test_single_pr_preflight_allows_local_spec(tmp_path, monkeypatch):
    write_spec(tmp_path)
    (tmp_path / ".spec.toml").write_text("[specs]\nrequire_merged_spec = false\n")
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: tmp_path)
    # Single-PR mode does not require a base-tree spec lookup.
    from spec_runtime import spec_status

    monkeypatch.setattr(spec_status, "_git_lines", lambda *a, **kw: [])
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("unexpected base-tree lookup")))
    orch._validate_new_spec_run(tmp_path, "feature", "origin/main")


def test_doctor_includes_lint_summary(tmp_path, monkeypatch, capsys):
    from spec_runtime import doctor

    write_spec(tmp_path)
    monkeypatch.setattr(doctor, "cmd_doctor", lambda args: 0)
    assert cli._cmd_doctor(argparse.Namespace(repo_root=str(tmp_path))) == 0
    assert "Lint: 0 errors" in capsys.readouterr().out


def test_pinned_source_is_linted_instead_of_mutable_catalog(tmp_path):
    source = write_spec(tmp_path)
    pinned = tmp_path / "snapshot.md"
    pinned.write_text(source.read_text())
    source.write_text("broken catalog")
    assert not lint_specs(tmp_path, "feature", source_path=pinned)[0].errors
    pinned.write_text("---\nid: feature\n---\n## Goal\nIncomplete")
    assert "zero parseable acceptance criteria" in lint_specs(tmp_path, "feature", source_path=pinned)[0].errors


def test_merged_dependency_need_not_have_a_catalog_file(tmp_path, monkeypatch):
    from spec_runtime import spec_status

    write_spec(tmp_path, frontmatter="depends_on: [removed]")
    monkeypatch.setattr(spec_status, "_git_lines", lambda *a, **kw: ["spec/merged/removed"])
    assert not lint_specs(tmp_path, "feature")[0].errors


@pytest.mark.parametrize("failure", ["", "exit", "os", "invalid"])
def test_json_critique_is_one_document(tmp_path, monkeypatch, capsys, failure):
    from spec_runtime import agent_adapter, spec_status
    from spec_runtime.spec_lint import cmd_lint

    write_spec(tmp_path)
    monkeypatch.setattr(spec_status, "_git_lines", lambda *a, **kw: [])
    suggestions = {key: [] for key in ("ambiguities", "untestable_criteria", "missing_out_of_scope", "decomposition")}
    adapter = Mock()
    adapter.build_review_command.return_value = ["review-agent"]
    monkeypatch.setattr(agent_adapter, "get_agent_adapter", lambda _: adapter)
    runner = Mock(return_value=subprocess.CompletedProcess(
        [], int(failure == "exit"), "invalid" if failure == "invalid" else json.dumps(suggestions), "failed"
    ))
    if failure == "os":
        runner.side_effect = OSError("unavailable")
    monkeypatch.setattr(subprocess, "run", runner)
    assert cmd_lint(argparse.Namespace(spec="feature", json=True, critique=True), repo_root=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lint"][0]["errors"] == []
    assert ("error" in payload["critique"]) == bool(failure)


@pytest.mark.parametrize("pinned_valid", [True, False])
@pytest.mark.parametrize("partial_record", [True, False])
def test_autopilot_resume_lints_pinned_contract(tmp_path, monkeypatch, pinned_valid, partial_record):
    from spec_runtime import autopilot
    from spec_runtime.spec_metadata import iter_spec_metadata

    source = write_spec(tmp_path)
    run = orch.RunState(run_id="feature-test", spec_id="feature", branch="code/feature--test")
    orch._pin_run_spec_from_file(tmp_path, run, source, tree_root=tmp_path)
    if pinned_valid:
        write_spec(tmp_path, frontmatter="depends_on: [missing]", body="## Goal\nIncomplete")
    else:
        orch._run_spec_snapshot_path(tmp_path, run.run_id).write_text("---\nid: feature\n---\n## Goal\nIncomplete")
    monkeypatch.setattr(autopilot, "resolve_resume_run", lambda *a, **kw: (run.run_id, "resume-implement"))
    record = asdict(run)
    if partial_record:
        record.pop("branch")
        record.pop("spec_id")
    monkeypatch.setattr(autopilot, "read_latest_run_record", lambda *a, **kw: record)
    reasons = autopilot.spec_graph_skip_reasons(tmp_path, iter_spec_metadata(tmp_path))
    assert ("feature" not in reasons) == pinned_valid
    monkeypatch.setattr(autopilot, "get_spec_status", lambda *a, **kw: "in-progress")
    monkeypatch.setattr(autopilot, "project_canonical_spec_status", lambda *a, **kw: None)
    monkeypatch.setattr(autopilot, "_annotate_candidate_with_lease", lambda candidate, snapshot: candidate)
    queue = autopilot.build_dispatch_queue(
        tmp_path, git_state=SimpleNamespace(merged_specs=set()), run_index=Mock()
    )
    assert [candidate.run_id for candidate in queue] == ([run.run_id] if pinned_valid else [])


def test_doctor_can_diagnose_invalid_config_even_when_lint_cannot_run(tmp_path, monkeypatch, capsys):
    from spec_runtime import doctor

    (tmp_path / ".spec.toml").write_text('[specs]\nrequire_merged_spec = "invalid"\n')
    monkeypatch.setattr(doctor, "cmd_doctor", lambda args: 1)
    assert cli._cmd_doctor(argparse.Namespace(repo_root=str(tmp_path))) == 1
    assert "Lint unavailable" in capsys.readouterr().out
