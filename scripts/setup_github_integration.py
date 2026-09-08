#!/usr/bin/env python3
"""Interactive GitHub setup for automated reviewer integration."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REQUIRED_CHECKS = [
    "ci",
    "review-decision-gate",
    "spec-pr-policy",
]


class SetupError(RuntimeError):
    """Raised when setup cannot proceed."""


@dataclass
class SetupConfig:
    repo: str
    branch: str
    ruleset_name: str
    required_checks: list[str]
    reviewer_agent: str
    review_api_key: str | None
    review_secret_name: str
    set_review_secret: bool
    configure_actions_permissions: bool
    configure_workflow_permissions: bool
    configure_ruleset: bool
    allowed_actions: str
    workflow_permission: str
    can_approve_pull_request_reviews: bool
    strict_required_status_checks: bool
    enforcement: str
    include_pull_request_rule: bool
    required_approving_review_count: int
    dry_run: bool


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    capture_output: bool = True,
) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise SetupError(f"Command failed: {' '.join(command)}\n{detail}")
    return (result.stdout or "").strip() if capture_output else ""


def _gh_api(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> str:
    command = ["gh", "api"]
    if method.upper() != "GET":
        command.extend(["-X", method.upper()])
    command.extend([path, "-H", "X-GitHub-Api-Version: 2022-11-28"])
    if payload is None:
        return _run_command(command)
    return _run_command(command + ["--input", "-"], input_text=json.dumps(payload))


def _parse_repo_from_remote_url(url: str) -> str | None:
    cleaned = url.strip()
    if not cleaned:
        return None
    if cleaned.startswith("git@github.com:"):
        tail = cleaned.removeprefix("git@github.com:")
        return tail[:-4] if tail.endswith(".git") else tail
    if cleaned.startswith("https://github.com/"):
        tail = cleaned.removeprefix("https://github.com/")
        return tail[:-4] if tail.endswith(".git") else tail
    return None


def _detect_repo() -> str | None:
    try:
        value = _run_command(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
        if value:
            return value
    except SetupError:
        pass
    try:
        remote = _run_command(["git", "config", "--get", "remote.origin.url"])
    except SetupError:
        return None
    return _parse_repo_from_remote_url(remote)


def _prompt_text(prompt: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("Value is required.")


def _prompt_yes_no(prompt: str, *, default: bool = True) -> bool:
    choices = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{choices}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def _prompt_int(prompt: str, *, default: int = 0, minimum: int = 0) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Please enter an integer.")
            continue
        if value < minimum:
            print(f"Please enter a value >= {minimum}.")
            continue
        return value


def _prompt_csv(prompt: str, *, default_items: list[str]) -> list[str]:
    default_csv = ", ".join(default_items)
    while True:
        raw = input(f"{prompt} [{default_csv}]: ").strip()
        selected = default_items if not raw else [item.strip() for item in raw.split(",")]
        checks = [item for item in selected if item]
        if checks:
            return checks
        print("Provide at least one check context.")


def _prompt_review_api_key(secret_name: str) -> str:
    while True:
        value = getpass.getpass(f"{secret_name} (input hidden): ").strip()
        if value:
            return value
        print(f"{secret_name} is required unless you skip secret setup.")


def _read_review_api_key_file(path_value: str) -> str:
    """Read one private, regular secret file without following a symlink."""
    path = Path(path_value).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise SetupError("Could not inspect the OpenAI API key file.") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SetupError("The OpenAI API key file must be a regular file, not a link.")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SetupError("Could not open the OpenAI API key file safely.") from exc
    try:
        metadata = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SetupError("The OpenAI API key file changed while it was opened.")
        if not stat.S_ISREG(metadata.st_mode):
            raise SetupError("The OpenAI API key file must be a regular file, not a link.")
        if metadata.st_nlink != 1:
            raise SetupError("The OpenAI API key file must have exactly one hard link.")
        if os.name == "posix":
            if metadata.st_uid != os.getuid():
                raise SetupError("The OpenAI API key file must be owned by the current user.")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SetupError(
                    "The OpenAI API key file must not be accessible by group or other users."
                )
        try:
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                payload = stream.read()
        except (OSError, UnicodeError) as exc:
            raise SetupError("Could not read the OpenAI API key file.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    lines = payload.splitlines()
    if len(lines) != 1 or not lines[0].strip() or lines[0] != lines[0].strip():
        raise SetupError("The OpenAI API key file must contain exactly one non-empty line.")
    return lines[0]


def _provided_review_api_key(args: argparse.Namespace) -> str | None:
    if args.openai_api_key_file:
        return _read_review_api_key_file(args.openai_api_key_file)
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    return value or None


def _list_repo_rulesets(repo: str) -> list[dict[str, Any]]:
    output = _gh_api(f"repos/{repo}/rulesets")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SetupError(f"Could not parse rulesets JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SetupError("Unexpected rulesets payload shape.")
    return [item for item in payload if isinstance(item, dict)]


def _build_ruleset_payload(
    config: SetupConfig,
    *,
    existing_bypass_actors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rules: list[dict[str, Any]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": config.strict_required_status_checks,
                "required_status_checks": [{"context": context} for context in config.required_checks],
            },
        }
    ]
    if config.include_pull_request_rule:
        rules.append(
            {
                "type": "pull_request",
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": config.required_approving_review_count,
                    "required_review_thread_resolution": False,
                },
            }
        )
    return {
        "name": config.ruleset_name,
        "target": "branch",
        "enforcement": config.enforcement,
        "conditions": {
            "ref_name": {
                "include": [f"refs/heads/{config.branch}"],
                "exclude": [],
            }
        },
        "rules": rules,
        "bypass_actors": existing_bypass_actors or [],
    }


def _validate_prereqs() -> None:
    _run_command(["gh", "--version"])
    _run_command(["gh", "auth", "status"])


def _set_review_secret(config: SetupConfig) -> None:
    assert config.review_api_key is not None
    if config.dry_run:
        print(f"[dry-run] gh secret set {config.review_secret_name} -R {config.repo}")
        return
    _run_command(
        ["gh", "secret", "set", config.review_secret_name, "-R", config.repo],
        input_text=f"{config.review_api_key}\n",
        capture_output=False,
    )
    print(f"Configured repository secret {config.review_secret_name}.")


def _set_actions_permissions(config: SetupConfig) -> None:
    payload = {
        "enabled": True,
        "allowed_actions": config.allowed_actions,
        "sha_pinning_required": True,
    }
    selected_payload = {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": ["openai/codex-action@*"],
    }
    if config.dry_run:
        print(f"[dry-run] PUT repos/{config.repo}/actions/permissions")
        print(json.dumps(payload, indent=2))
        if config.allowed_actions == "selected":
            print(f"[dry-run] PUT repos/{config.repo}/actions/permissions/selected-actions")
            print(json.dumps(selected_payload, indent=2))
        return
    _gh_api(f"repos/{config.repo}/actions/permissions", method="PUT", payload=payload)
    if config.allowed_actions == "selected":
        # The repository workflows use GitHub-owned actions plus one explicit
        # OpenAI action. Keep every other third-party action disabled.
        _gh_api(
            f"repos/{config.repo}/actions/permissions/selected-actions",
            method="PUT",
            payload=selected_payload,
        )
    print("Configured Actions permissions.")


def _set_workflow_permissions(config: SetupConfig) -> None:
    payload = {
        "default_workflow_permissions": config.workflow_permission,
        "can_approve_pull_request_reviews": config.can_approve_pull_request_reviews,
    }
    if config.dry_run:
        print(f"[dry-run] PUT repos/{config.repo}/actions/permissions/workflow")
        print(json.dumps(payload, indent=2))
        return
    _gh_api(
        f"repos/{config.repo}/actions/permissions/workflow",
        method="PUT",
        payload=payload,
    )
    print("Configured workflow permissions.")


def _upsert_ruleset(config: SetupConfig) -> None:
    existing = None
    for candidate in _list_repo_rulesets(config.repo):
        if candidate.get("name") == config.ruleset_name and candidate.get("target") == "branch":
            existing = candidate
            break
    bypass = existing.get("bypass_actors") if isinstance(existing, dict) else None
    payload = _build_ruleset_payload(config, existing_bypass_actors=bypass)
    if config.dry_run:
        action = "PUT" if existing else "POST"
        endpoint = f"repos/{config.repo}/rulesets/{existing.get('id')}" if existing else f"repos/{config.repo}/rulesets"
        print(f"[dry-run] {action} {endpoint}")
        print(json.dumps(payload, indent=2))
        return
    if existing:
        _gh_api(
            f"repos/{config.repo}/rulesets/{existing['id']}",
            method="PUT",
            payload=payload,
        )
        print(f"Updated ruleset '{config.ruleset_name}' (id={existing['id']}).")
    else:
        _gh_api(f"repos/{config.repo}/rulesets", method="POST", payload=payload)
        print(f"Created ruleset '{config.ruleset_name}'.")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Configure GitHub integration for an automated review gate. "
            "By default this runs interactively and prompts for required values."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--repo", help="GitHub repository in OWNER/REPO form.")
    parser.add_argument("--branch", help="Target protected branch (default: main).")
    parser.add_argument("--ruleset-name", help="Ruleset name for create/update.")
    parser.add_argument(
        "--reviewer-agent",
        help="Automated reviewer to run in CI (only codex is currently supported).",
    )
    parser.add_argument(
        "--required-check",
        action="append",
        dest="required_checks",
        default=[],
        help="Required check context (repeatable).",
    )
    parser.add_argument(
        "--openai-api-key-file",
        help=(
            "Private one-line file containing OPENAI_API_KEY. "
            "Alternatively set OPENAI_API_KEY in the environment or use the hidden prompt."
        ),
    )
    parser.add_argument(
        "--anthropic-api-key",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-openai-secret",
        action="store_true",
        help="Skip setting repository secret OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--skip-anthropic-secret",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-actions-permissions",
        action="store_true",
        help="Skip configuring repository Actions permissions.",
    )
    parser.add_argument(
        "--skip-workflow-permissions",
        action="store_true",
        help="Skip configuring workflow default permissions.",
    )
    parser.add_argument(
        "--skip-ruleset",
        action="store_true",
        help="Skip creating/updating a ruleset.",
    )
    parser.add_argument(
        "--allowed-actions",
        choices=["all", "local_only", "selected"],
        help="Actions allowlist mode (default: selected: GitHub-owned plus openai/codex-action).",
    )
    parser.add_argument(
        "--workflow-permission",
        choices=["read", "write"],
        help="Default workflow token permission (default: read).",
    )
    parser.add_argument(
        "--can-approve-pull-request-reviews",
        action="store_true",
        help="Allow GitHub Actions to approve PR reviews with GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--strict-required-checks",
        action="store_true",
        default=True,
        help="Require up-to-date branch before status checks pass (default: on).",
    )
    parser.add_argument(
        "--no-strict-required-checks",
        action="store_false",
        dest="strict_required_checks",
        help="Disable strict required status check policy.",
    )
    parser.add_argument(
        "--enforcement",
        choices=["active", "evaluate", "disabled"],
        help="Ruleset enforcement status (default: active).",
    )
    parser.add_argument(
        "--no-pull-request-rule",
        action="store_true",
        help="Do not include pull_request rule in ruleset.",
    )
    parser.add_argument(
        "--required-approvals",
        type=int,
        default=0,
        help="Required approvals if pull_request rule is included (default: 0).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when required values are missing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended API operations without applying changes.",
    )
    return parser


def _validate_reviewer_selection(args: argparse.Namespace) -> str:
    reviewer_agent = args.reviewer_agent or "codex"
    if reviewer_agent == "claude" or args.anthropic_api_key or args.skip_anthropic_secret:
        raise SetupError(
            "Claude GitHub review setup is no longer supported because this repository "
            "does not ship a Claude review workflow. Use --reviewer-agent codex; Claude "
            "remains available for local spec implementation and review sessions."
        )
    if reviewer_agent != "codex":
        raise SetupError(f"Unsupported reviewer agent {reviewer_agent!r}; use 'codex'.")
    return reviewer_agent


def _resolve_config(args: argparse.Namespace) -> SetupConfig:
    if args.required_approvals < 0:
        raise SetupError("--required-approvals must be >= 0")

    reviewer_agent = _validate_reviewer_selection(args)
    detected_repo = _detect_repo()

    provided_review_api_key = _provided_review_api_key(args)
    skip_review_secret = args.skip_openai_secret
    review_secret_name = "OPENAI_API_KEY"
    required_check_defaults = DEFAULT_REQUIRED_CHECKS

    if args.non_interactive:
        repo = args.repo or detected_repo
        if not repo:
            raise SetupError("Missing --repo and could not auto-detect repository.")
        branch = args.branch or "main"
        ruleset_name = args.ruleset_name or f"{branch} merge gates"
        required_checks = args.required_checks or required_check_defaults
        set_review_secret = not skip_review_secret
        review_api_key = provided_review_api_key if set_review_secret else None
        if set_review_secret and not review_api_key:
            raise SetupError(
                "Missing OPENAI_API_KEY or --openai-api-key-file "
                "(or use --skip-openai-secret)."
            )
        return SetupConfig(
            repo=repo,
            branch=branch,
            ruleset_name=ruleset_name,
            required_checks=required_checks,
            reviewer_agent=reviewer_agent,
            review_api_key=review_api_key,
            review_secret_name=review_secret_name,
            set_review_secret=set_review_secret,
            configure_actions_permissions=not args.skip_actions_permissions,
            configure_workflow_permissions=not args.skip_workflow_permissions,
            configure_ruleset=not args.skip_ruleset,
            allowed_actions=args.allowed_actions or "selected",
            workflow_permission=args.workflow_permission or "read",
            can_approve_pull_request_reviews=args.can_approve_pull_request_reviews,
            strict_required_status_checks=args.strict_required_checks,
            enforcement=args.enforcement or "active",
            include_pull_request_rule=not args.no_pull_request_rule,
            required_approving_review_count=args.required_approvals,
            dry_run=args.dry_run,
        )

    repo = _prompt_text("Repository", default=args.repo or detected_repo)
    branch = _prompt_text("Protected branch", default=args.branch or "main")
    ruleset_name = _prompt_text(
        "Ruleset name",
        default=args.ruleset_name or f"{branch} merge gates",
    )
    required_checks = args.required_checks or _prompt_csv(
        "Required check contexts",
        default_items=required_check_defaults,
    )
    set_review_secret = not skip_review_secret
    if set_review_secret:
        review_api_key = provided_review_api_key or _prompt_review_api_key(review_secret_name)
    else:
        review_api_key = None

    return SetupConfig(
        repo=repo,
        branch=branch,
        ruleset_name=ruleset_name,
        required_checks=required_checks,
        reviewer_agent=reviewer_agent,
        review_api_key=review_api_key,
        review_secret_name=review_secret_name,
        set_review_secret=set_review_secret,
        configure_actions_permissions=not args.skip_actions_permissions
        and _prompt_yes_no("Configure Actions permissions?", default=True),
        configure_workflow_permissions=not args.skip_workflow_permissions
        and _prompt_yes_no("Configure workflow token defaults?", default=True),
        configure_ruleset=not args.skip_ruleset
        and _prompt_yes_no("Create or update the branch ruleset?", default=True),
        allowed_actions=args.allowed_actions or _prompt_text("Allowed actions", default="selected"),
        workflow_permission=args.workflow_permission or _prompt_text("Default workflow permission", default="read"),
        can_approve_pull_request_reviews=args.can_approve_pull_request_reviews
        or _prompt_yes_no("Allow workflows to approve PR reviews?", default=False),
        strict_required_status_checks=args.strict_required_checks,
        enforcement=args.enforcement or _prompt_text("Ruleset enforcement", default="active"),
        include_pull_request_rule=not args.no_pull_request_rule,
        required_approving_review_count=args.required_approvals
        if args.required_approvals
        else _prompt_int("Required approvals", default=0, minimum=0),
        dry_run=args.dry_run,
    )


def _print_summary(config: SetupConfig) -> None:
    print("GitHub integration plan:")
    print(f"  repo: {config.repo}")
    print(f"  branch: {config.branch}")
    print(f"  reviewer agent: {config.reviewer_agent}")
    print(f"  required checks: {', '.join(config.required_checks)}")
    print(f"  set review secret: {config.set_review_secret}")
    print(f"  configure actions permissions: {config.configure_actions_permissions}")
    print(f"  configure workflow permissions: {config.configure_workflow_permissions}")
    print(f"  configure ruleset: {config.configure_ruleset}")
    if config.dry_run:
        print("  mode: dry-run")


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        # Fail with an actionable migration message before asking the user to
        # authenticate gh or answering interactive prompts.
        _validate_reviewer_selection(args)
        _validate_prereqs()
        config = _resolve_config(args)
        _print_summary(config)
        if config.set_review_secret:
            _set_review_secret(config)
        if config.configure_actions_permissions:
            _set_actions_permissions(config)
        if config.configure_workflow_permissions:
            _set_workflow_permissions(config)
        if config.configure_ruleset:
            _upsert_ruleset(config)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
