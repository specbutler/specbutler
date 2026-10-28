import ast
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime.config import VerifyGateConfig, load_repo_spec_runtime_config
from spec_runtime.execution_backend import CommandResult


def test_core_has_no_legacy_cleanup_identifiers():
    root = Path(orch.__file__).parent
    for path in root.rglob("*.py"):
        source = path.read_text()
        for identifier in ("SIM_LOCAL_PG_BIN", "postgresql@16", "_stop_worktree_postgres_if_present"):
            assert identifier not in source, path


def test_prompt_constants_do_not_hardcode_master():
    tree = ast.parse(Path(orch.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and "PROMPT" in target.id for target in node.targets
        ):
            for value in ast.walk(node.value):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    assert "master" not in value.value


@pytest.mark.parametrize("branch", ["main", "release/stable"])
def test_merge_conflict_prompt_uses_base_branch(branch, monkeypatch):
    monkeypatch.setattr(orch, "BASE_REF", f"origin/{branch}")
    monkeypatch.setattr(orch, "PR_BASE_BRANCH", branch)
    tree = ast.parse(Path(orch.__file__).read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "MERGE_CONFLICT_PROMPT" for t in node.targets))
    prompt = eval(compile(ast.Expression(assignment.value), "prompt", "eval"), vars(orch))
    assert f"conflicts with {branch}" in prompt
    assert f"git merge origin/{branch}" in prompt
    assert branch in orch._retry_reason_label("merge_conflicts")


@pytest.mark.parametrize("command,diagnostic,expected", [
    ("npm test", "", []),
    ("make test", "", []),
    ("npm test", "npm test -- --verbose", ["npm", "test", "--", "--verbose"]),
    ("python -m pytest", "", None),
])
def test_gate_diagnostic_selection(tmp_path, monkeypatch, command, diagnostic, expected):
    gate = VerifyGateConfig(name="custom", command=command, diagnostic_command=diagnostic)
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, verify_gates=(gate,)))
    monkeypatch.setattr(orch, "VERIFY_GATE_COMMANDS", {"custom": command})
    selected = orch._gate_diagnostic_command(tmp_path, "custom")
    if expected is None:
        assert "pytest" in selected
    else:
        assert selected == expected


def test_non_test_gate_runs_configured_diagnostic(tmp_path, monkeypatch):
    gate = VerifyGateConfig(name="custom", command="npm test", diagnostic_command="npm run diagnose")
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, verify_gates=(gate,)))
    monkeypatch.setattr(orch, "VERIFY_GATE_COMMANDS", {"custom": gate.command})
    backend = MagicMock()
    backend.run_command.side_effect = [
        CommandResult(returncode=1, stdout="failed", stderr="", argv=[]),
        CommandResult(returncode=2, stdout="diagnosis", stderr="diagnostic detail", argv=[]),
    ]
    monkeypatch.setattr(orch, "_resolve_execution_backend", lambda: backend)
    result = orch._run_verify_gate(tmp_path, "custom")
    assert "diagnosis" in result.diagnostic
    assert "diagnostic detail" in result.diagnostic
    assert backend.run_command.call_args_list[1].args[0].argv == ["npm", "run", "diagnose"]


def test_config_loads_diagnostic_command(tmp_path):
    (tmp_path / ".spec.toml").write_text(
        "[[verify.gates]]\nname = \"custom\"\ncommand = \"npm test\"\ndiagnostic_command = \"npm run diagnose\"\n"
    )
    assert load_repo_spec_runtime_config(tmp_path).verify_gates[0].diagnostic_command == "npm run diagnose"


@pytest.mark.parametrize("test_role", [True, False])
def test_non_pytest_failure_skips_all_reruns(tmp_path, monkeypatch, test_role):
    gate = VerifyGateConfig(name="custom", command="npm test", role="test" if test_role else "lint")
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, verify_gates=(gate,)))
    monkeypatch.setattr(orch, "VERIFY_GATE_COMMANDS", {"custom": gate.command})
    monkeypatch.setattr(orch, "TEST_ROLE_GATES", frozenset({"custom"}) if test_role else frozenset())
    backend = MagicMock()
    backend.run_command.return_value = CommandResult(
        returncode=1, stdout="FAILED tests/test_api.py::test_alpha - error", stderr="", argv=[]
    )
    monkeypatch.setattr(orch, "_resolve_execution_backend", lambda: backend)
    result = orch._run_verify_gate(tmp_path, "custom")
    assert result.completed_process.returncode == 1
    assert result.diagnostic == ""
    assert result.targeted_diagnostics == []
    assert backend.run_command.call_count == 1
