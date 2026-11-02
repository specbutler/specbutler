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
        source = path.read_text(encoding="utf-8")
        for identifier in ("SIM_LOCAL_PG_BIN", "postgresql@16", "_stop_worktree_postgres_if_present"):
            assert identifier not in source, path


def test_prompt_constants_do_not_hardcode_master():
    tree = ast.parse(Path(orch.__file__).read_text(encoding="utf-8"))
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
    tree = ast.parse(Path(orch.__file__).read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "MERGE_CONFLICT_PROMPT" for t in node.targets))
    prompt = eval(compile(ast.Expression(assignment.value), "prompt", "eval"), vars(orch))
    assert f"conflicts with {branch}" in prompt
    assert f"git merge origin/{branch}" in prompt
    assert branch in orch._retry_reason_label("merge_conflicts")


@pytest.mark.parametrize("command,diagnostic,expected", [
    ("npm test", "", []),
    ("make test", "", []),
    ("ruff check pytest", "", []),
    ("python scripts/check.py pytest", "", []),
    ("python -c pytest", "", []),
    ("python -m other pytest", "", []),
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
@pytest.mark.parametrize("command", ["npm test", "ruff check pytest", "python scripts/check.py pytest"])
def test_non_pytest_failure_skips_all_reruns(tmp_path, monkeypatch, test_role, command):
    gate = VerifyGateConfig(name="custom", command=command, role="test" if test_role else "lint")
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


@pytest.mark.parametrize("command", [
    "python -m pytest integration_tests -k smoke",
    "/custom/bin/python3.12 -I -m pytest integration_tests",
    "pytest integration_tests",
    "py.test integration_tests",
    "pytest.exe integration_tests",
    "python -m pytest -- integration_tests",
])
def test_pytest_diagnostics_preserve_configured_command(tmp_path, monkeypatch, command):
    import shlex

    gate = VerifyGateConfig(name="custom", command=command)
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, verify_gates=(gate,)))
    monkeypatch.setattr(orch, "VERIFY_GATE_COMMANDS", {"custom": command})
    args = shlex.split(command)
    selected = orch._gate_diagnostic_command(tmp_path, "custom")
    flags = ["--tb=short", "--no-header", "-q", "--maxfail=20"]
    index = args.index("--") if "--" in args else len(args)
    assert selected == args[:index] + flags + args[index:]
    assert "tests" not in selected


@pytest.mark.parametrize("command, expected", [
    ("pytest integration_tests", True),
    ("python3.12 -m pytest integration_tests", True),
    (r"C:\Python\pytest.exe integration_tests", True),
    ("ruff check pytest", False),
    ("python scripts/check.py pytest", False),
])
def test_pytest_invocation_recognition(command, expected):
    import shlex

    assert orch._command_is_pytest(shlex.split(command, posix=False)) is expected


def test_diagnostic_runs_repository_custom_test_directory(tmp_path, monkeypatch):
    import shlex
    import subprocess
    import sys

    suite = tmp_path / "integration_tests"
    suite.mkdir()
    (suite / "test_example.py").write_text("def test_example():\n    assert False\n", encoding="utf-8")
    command = shlex.join([sys.executable, "-m", "pytest", "integration_tests"])
    gate = VerifyGateConfig(name="custom", command=command)
    monkeypatch.setattr(orch, "SPEC_RUNTIME_CONFIG", replace(orch.SPEC_RUNTIME_CONFIG, verify_gates=(gate,)))
    monkeypatch.setattr(orch, "VERIFY_GATE_COMMANDS", {"custom": command})
    result = subprocess.run(
        orch._gate_diagnostic_command(tmp_path, "custom"),
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 1
    assert "test_example" in result.stdout
    assert "assert False" in result.stdout
