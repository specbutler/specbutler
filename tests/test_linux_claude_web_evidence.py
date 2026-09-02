from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tools" / "linux_claude_web_evidence.py"
REVISION = "a" * 40


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("linux_claude_web_evidence", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt(module: ModuleType, revision: str, challenge: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "source_revision": revision,
        "backend": "claude",
        "real_provider": True,
        "transport": "http-sse",
        "dependent_turns": 3,
        "turn_1_marker_returned": True,
        "turn_2_retained_turn_1": True,
        "turn_2_marker_returned": True,
        "turn_3_retained_turns_1_and_2": True,
        "provider_processes_observed": 2,
        "provider_processes_remaining": 0,
        "server_processes_remaining": 0,
        "server_stopped_cleanly": True,
        "web_token_removed": True,
        "credential_files_copied": 0,
        "proof_test": module.TEST_NODE,
        "run_challenge_sha256": hashlib.sha256(challenge.encode("ascii")).hexdigest(),
    }


def _prepare_producer(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pytest_returncode: int = 0,
    receipt_change: tuple[str, Any] | None = None,
    write_receipt: bool = True,
) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_checkout_revision", lambda _root: REVISION)
    monkeypatch.setattr(module, "_require_clean_checkout", lambda _root: None)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        env = kwargs["env"]
        if write_receipt:
            payload = _receipt(module, REVISION, env[module.CHALLENGE_ENV])
            if receipt_change is not None:
                payload[receipt_change[0]] = receipt_change[1]
            Path(env[module.RECEIPT_PATH_ENV]).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, pytest_returncode)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return commands


def test_producer_runs_one_exact_marked_test_and_publishes_validated_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_helper()
    commands = _prepare_producer(module, monkeypatch)
    output = tmp_path / "linux-claude-web-result.json"

    module.produce(output=output, expected_revision=REVISION)

    assert commands == [
        [
            module.sys.executable,
            "-m",
            "pytest",
            "--strict-markers",
            "-m",
            "linux_claude_real_provider",
            module.TEST_NODE,
            "-vv",
        ]
    ]
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "passed"
    assert result["source_revision"] == REVISION
    assert result["backend"] == "claude"
    assert result["dependent_turns"] == 3
    assert result["turn_2_retained_turn_1"] is True
    assert result["turn_3_retained_turns_1_and_2"] is True
    assert result["provider_processes_remaining"] == 0
    assert result["server_processes_remaining"] == 0
    assert "run_challenge_sha256" not in result


@pytest.mark.parametrize("returncode,write_receipt", [(1, False), (0, False)])
def test_producer_removes_stale_success_on_failed_or_skipped_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    write_receipt: bool,
) -> None:
    module = _load_helper()
    _prepare_producer(
        module,
        monkeypatch,
        pytest_returncode=returncode,
        write_receipt=write_receipt,
    )
    output = tmp_path / "linux-claude-web-result.json"
    output.write_text('{"status":"passed"}\n', encoding="utf-8")

    with pytest.raises(module.EvidenceError):
        module.produce(output=output, expected_revision=REVISION)

    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "codex"),
        ("dependent_turns", 2),
        ("turn_2_retained_turn_1", False),
        ("turn_3_retained_turns_1_and_2", False),
        ("provider_processes_observed", 0),
        ("provider_processes_remaining", 1),
        ("server_processes_remaining", 1),
        ("source_revision", "b" * 40),
        ("run_challenge_sha256", "0" * 64),
    ],
)
def test_producer_rejects_unproven_or_cross_revision_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    module = _load_helper()
    _prepare_producer(module, monkeypatch, receipt_change=(field, value))
    output = tmp_path / "linux-claude-web-result.json"

    with pytest.raises(module.EvidenceError):
        module.produce(output=output, expected_revision=REVISION)

    assert not output.exists()


def test_producer_rejects_dirty_or_wrong_revision_without_running_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_helper()
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_checkout_revision", lambda _root: "b" * 40)
    called = False

    def unexpected_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("pytest must not run")

    monkeypatch.setattr(module.subprocess, "run", unexpected_run)
    output = tmp_path / "linux-claude-web-result.json"

    with pytest.raises(module.EvidenceError, match="does not match"):
        module.produce(output=output, expected_revision=REVISION)

    assert called is False
    assert not output.exists()
