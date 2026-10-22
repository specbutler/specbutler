from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runtime import autopilot, cli, container, doctor
from spec_runtime.config import (
    CONTAINER_ALPHA_WARNING,
    ExecutionConfig,
    SpecRuntimeConfig,
    container_alpha_warning,
    emit_container_alpha_warning,
)


def _config(backend: str) -> SpecRuntimeConfig:
    return replace(SpecRuntimeConfig(), execution=ExecutionConfig(backend=backend))


def test_warning_text_is_single_line_and_names_known_issues() -> None:
    assert "\n" not in CONTAINER_ALPHA_WARNING
    assert CONTAINER_ALPHA_WARNING.startswith("Alpha: container mode")
    assert "Prefer the clone or worktree backend." in CONTAINER_ALPHA_WARNING


def test_helper_warns_only_for_container() -> None:
    assert container_alpha_warning(_config("container")) == CONTAINER_ALPHA_WARNING
    assert container_alpha_warning(_config("worktree")) is None
    assert container_alpha_warning(_config("clone")) is None


def test_emit_prints_one_stderr_line_for_container(capsys: pytest.CaptureFixture[str]) -> None:
    emit_container_alpha_warning(_config("container"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [f"Warning: {CONTAINER_ALPHA_WARNING}"]


@pytest.mark.parametrize("backend", ["worktree", "clone"])
def test_emit_is_silent_for_non_container(
    backend: str, capsys: pytest.CaptureFixture[str]
) -> None:
    emit_container_alpha_warning(_config(backend))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_spec_doctor_reports_alpha_as_warning_not_error() -> None:
    checks = doctor._container_alpha_checks(_config("container"))
    assert [(c.name, c.status, c.detail) for c in checks] == [
        ("container backend (alpha)", "warning", CONTAINER_ALPHA_WARNING)
    ]
    assert not checks[0].blocker


@pytest.mark.parametrize("backend", ["worktree", "clone"])
def test_spec_doctor_has_no_alpha_check_for_non_container(backend: str) -> None:
    assert doctor._container_alpha_checks(_config(backend)) == []


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("container", True), ("worktree", False), ("clone", False)],
)
def test_spec_container_doctor_warning(
    backend: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(container, "load_repo_spec_runtime_config", lambda _root: _config(backend))
    monkeypatch.setattr(container, "run_doctor_checks", lambda *_a, **_k: [])

    assert container.cmd_doctor(argparse.Namespace(repo_root=str(tmp_path))) == 0

    err = capsys.readouterr().err
    assert (CONTAINER_ALPHA_WARNING in err) is expected
    assert err.count("Alpha: container mode") == (1 if expected else 0)


def _patch_launch_commands(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setattr(cli, "_lazy_config", lambda: _config(backend))
    orchestrator = SimpleNamespace(cmd_run=lambda _a: 0, cmd_task=lambda _a: 0)
    monkeypatch.setattr(cli, "_lazy_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(autopilot, "run_loop", lambda _a: 0)


def _launch(name: str) -> int:
    args = argparse.Namespace(auto_command="run", notify=[])
    if name == "implement":
        return cli._cmd_implement(args)
    if name == "task":
        return cli._cmd_task(args)
    return cli._cmd_auto(args)


@pytest.mark.parametrize("command", ["implement", "task", "auto-run"])
@pytest.mark.parametrize(
    ("backend", "expected"),
    [("container", 1), ("worktree", 0), ("clone", 0)],
)
def test_launch_commands_warn_once_for_container_only(
    command: str,
    backend: str,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_launch_commands(monkeypatch, backend)

    assert _launch(command) == 0

    assert capsys.readouterr().err.count(CONTAINER_ALPHA_WARNING) == expected
