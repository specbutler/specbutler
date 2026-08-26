from __future__ import annotations

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
