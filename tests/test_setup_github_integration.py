from __future__ import annotations

import os
import runpy
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "setup_github_integration.py"
SCRIPT = runpy.run_path(str(SCRIPT_PATH), run_name="setup_github_integration")


def _args(*values: str):
    return SCRIPT["_build_arg_parser"]().parse_args(list(values))


def test_non_interactive_defaults_are_least_privilege(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve_config = SCRIPT["_resolve_config"]
    monkeypatch.setitem(resolve_config.__globals__, "_detect_repo", lambda: None)

    config = resolve_config(
        _args(
            "--non-interactive",
            "--repo",
            "acme/spec",
            "--skip-openai-secret",
        )
    )

    assert config.reviewer_agent == "codex"
    assert config.allowed_actions == "selected"
    assert config.workflow_permission == "read"
    assert config.can_approve_pull_request_reviews is False
    assert config.required_checks == ["ci", "review-decision-gate", "spec-pr-policy"]


def test_non_interactive_review_key_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_config = SCRIPT["_resolve_config"]
    monkeypatch.setitem(resolve_config.__globals__, "_detect_repo", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")

    config = resolve_config(
        _args("--non-interactive", "--repo", "acme/spec")
    )

    assert config.review_api_key == "environment-secret"


def test_review_key_file_must_be_private_and_is_never_a_cli_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = tmp_path / "review-key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    if os.name == "posix":
        key_file.chmod(0o600)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resolve_config = SCRIPT["_resolve_config"]
    monkeypatch.setitem(resolve_config.__globals__, "_detect_repo", lambda: None)

    config = resolve_config(
        _args(
            "--non-interactive",
            "--repo",
            "acme/spec",
            "--openai-api-key-file",
            str(key_file),
        )
    )

    assert config.review_api_key == "file-secret"
    with pytest.raises(SystemExit):
        _args("--openai-api-key", "command-line-secret")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission enforcement")
def test_review_key_file_rejects_group_readable_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = tmp_path / "review-key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    key_file.chmod(0o640)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SCRIPT["SetupError"], match="group or other"):
        SCRIPT["_provided_review_api_key"](
            _args("--openai-api-key-file", str(key_file))
        )


def test_review_key_file_rejects_symlink_and_multiple_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = tmp_path / "review-key"
    key_file.write_text("first\nsecond\n", encoding="utf-8")
    if os.name == "posix":
        key_file.chmod(0o600)
    link = tmp_path / "review-key-link"
    link.symlink_to(key_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SCRIPT["SetupError"], match="regular file"):
        SCRIPT["_provided_review_api_key"](
            _args("--openai-api-key-file", str(link))
        )
    with pytest.raises(SCRIPT["SetupError"], match="exactly one"):
        SCRIPT["_provided_review_api_key"](
            _args("--openai-api-key-file", str(key_file))
        )


def test_claude_reviewer_has_actionable_migration_error() -> None:
    with pytest.raises(SCRIPT["SetupError"], match="does not ship a Claude review workflow"):
        SCRIPT["_validate_reviewer_selection"](
            _args("--reviewer-agent", "claude")
        )


def test_selected_actions_dry_run_configures_only_trusted_publishers(capsys: pytest.CaptureFixture[str]) -> None:
    config = SCRIPT["SetupConfig"](
        repo="acme/spec",
        branch="main",
        ruleset_name="main merge gates",
        required_checks=["ci"],
        reviewer_agent="codex",
        review_api_key=None,
        review_secret_name="OPENAI_API_KEY",
        set_review_secret=False,
        configure_actions_permissions=True,
        configure_workflow_permissions=True,
        configure_ruleset=False,
        allowed_actions="selected",
        workflow_permission="read",
        can_approve_pull_request_reviews=False,
        strict_required_status_checks=True,
        enforcement="active",
        include_pull_request_rule=True,
        required_approving_review_count=0,
        dry_run=True,
    )

    SCRIPT["_set_actions_permissions"](config)

    output = capsys.readouterr().out
    assert "/actions/permissions/selected-actions" in output
    assert '"sha_pinning_required": true' in output
    assert '"github_owned_allowed": true' in output
    assert '"verified_allowed": false' in output
    assert '"openai/codex-action@*"' in output
