"""UTF-8 subprocess boundaries for Git and GitHub CLI."""

from __future__ import annotations

import io
import json
import locale
import subprocess
from pathlib import Path

from spec_runtime import autopilot, backfill_merge_tags, doctor, forge, orchestrator, review_feedback, update
from spec_runtime.git_common import (
    is_git_command,
    is_github_cli_command,
    subprocess_text_kwargs,
)
from spec_runtime.review_gate import evaluate_review_gate


def test_utf8_cli_detection_accepts_resolved_paths_exe_suffix_and_case() -> None:
    assert is_git_command([r"C:\Program Files\Git\cmd\GiT.ExE", "status"])
    assert is_git_command(["/opt/git/bin/GIT", "status"])
    assert is_github_cli_command([r"C:\Program Files\GitHub CLI\GH.EXE", "pr", "list"])
    assert is_github_cli_command(["/opt/gh/bin/Gh", "pr", "list"])
    assert not is_git_command([])
    assert not is_github_cli_command(["python", "-m", "gh"])


def test_github_cli_utf8_json_and_crlf_survive_cp1252_locale(
    monkeypatch,
) -> None:
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    payload = {
        "title": "Résumé 雪",
        "path": r"C:\Spec Butler snow-雪\specs\café.md",
        "body": "Zażółć gęślą jaźń",
    }
    raw = (json.dumps(payload, ensure_ascii=False) + "\r\n").encode("utf-8")
    kwargs = subprocess_text_kwargs([r"C:\Program Files\GitHub CLI\Gh.ExE", "pr", "view"])

    assert kwargs == {"text": True, "encoding": "utf-8", "errors": "replace"}
    assert raw.decode(locale.getencoding()) != raw.decode("utf-8")
    with io.TextIOWrapper(
        io.BytesIO(raw),
        encoding=str(kwargs["encoding"]),
        errors=str(kwargs["errors"]),
    ) as stream:
        decoded = stream.read()

    # subprocess text mode performs universal-newline conversion for native
    # GitHub CLI's CRLF output, without changing the JSON content.
    assert decoded.endswith("\n")
    assert "\r" not in decoded
    assert json.loads(decoded) == payload


def test_utf8_cli_malformed_output_is_replaced_instead_of_crashing() -> None:
    kwargs = subprocess_text_kwargs(["gh", "api"])
    raw = b"failure from caf\xc3\xa9: \xff\r\n"
    with io.TextIOWrapper(
        io.BytesIO(raw),
        encoding=str(kwargs["encoding"]),
        errors=str(kwargs["errors"]),
    ) as stream:
        assert stream.read() == "failure from café: \ufffd\n"


def test_non_utf8_cli_retains_locale_text_mode() -> None:
    assert subprocess_text_kwargs(["python", "-V"]) == {
        "text": True,
        "errors": "replace",
    }


def test_runtime_github_cli_subprocess_seams_request_utf8(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        argv = list(command)
        calls.append((argv, dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        autopilot,
        "load_repo_spec_runtime_config",
        lambda _root: autopilot.SPEC_RUNTIME_CONFIG,
    )

    forge._default_run_fn([r"C:\Program Files\GitHub CLI\Gh.ExE", "pr", "list"])
    review_feedback.default_run_subprocess(["gh", "api", "repos/:owner/:repo"])
    backfill_merge_tags._run_gh("pr", "list", cwd=tmp_path)
    doctor._run_command([r"C:\Program Files\GitHub CLI\GH.EXE", "auth", "status"], tmp_path, 1)
    update._github_token()
    orchestrator.run_subprocess(["gh", "pr", "view"], cwd=tmp_path)
    autopilot._merged_pr_for_branch(tmp_path, "code/unicode-雪--run")

    assert len(calls) == 7
    for _command, kwargs in calls:
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"


def test_injected_forge_runner_contract_does_not_gain_text_kwargs() -> None:
    captured: dict[str, object] = {}

    def fake_run(command, cwd=None, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    assert forge.GitHubForge(run_fn=fake_run).find_pr_for_branch("unicode-雪") is None
    assert "encoding" not in captured
    assert "errors" not in captured
    assert "text" not in captured


def test_review_gate_replaces_non_utf8_agent_text_without_skipping_schema(
    tmp_path: Path,
) -> None:
    head_sha = "a" * 40
    base_sha = "b" * 40
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "spec_runtime"
        / "templates"
        / "review-schema.json"
    )
    input_path = tmp_path / "review.json"
    payload = {
        "schema_version": "v1",
        "decision": "approved",
        "summary": "No issues - native review",
        "reviewed_base_sha": base_sha,
        "reviewed_head_sha": head_sha,
        "findings": [],
        "reviewer_role": "independent-review",
        "reviewer_agent": "codex",
        "reviewed_at": "2026-09-01T00:00:00Z",
    }
    encoded = json.dumps(payload).encode("utf-8").replace(b" - native", b" \x97 native")
    input_path.write_bytes(encoded)

    evaluation = evaluate_review_gate(
        input_path=input_path,
        schema_path=schema_path,
        expected_head_sha=head_sha,
        expected_base_sha=base_sha,
    )

    assert evaluation.exit_code == 0
    assert evaluation.result_payload["decision"] == "approved"
    assert "\ufffd" in evaluation.result_payload["summary"]
