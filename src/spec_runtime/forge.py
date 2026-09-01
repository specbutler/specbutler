"""Forge adapter boundary.

Isolates forge-specific publish/review/merge behavior behind a clean
interface so that GitHub automation is not entangled with the generic
public CLI surface.

Usage
-----
Consumers obtain a ``ForgeAdapter`` through ``get_forge_adapter()`` which
returns the appropriate implementation based on the detected environment.
Currently only GitHub (via ``gh`` CLI) is supported.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import load_spec_runtime_config
from .git_common import subprocess_text_kwargs

AUTO_MERGE_ARM_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequest:
    """Minimal PR representation returned by forge operations."""

    number: int
    url: str = ""
    head_branch: str = ""
    base_branch: str = ""
    title: str = ""
    state: str = ""  # "open", "closed", "merged"
    is_draft: bool | None = None


@dataclass(frozen=True)
class CheckRun:
    """Single CI check result."""

    name: str
    state: str  # "success", "failure", "pending", "neutral", etc.
    url: str = ""
    bucket: str = ""  # "pass", "fail", "pending"


@dataclass(frozen=True)
class MergeResult:
    ok: bool
    message: str = ""


@dataclass(frozen=True)
class PushResult:
    ok: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Protocol (the boundary contract)
# ---------------------------------------------------------------------------


@runtime_checkable
class ForgeAdapter(Protocol):
    """Abstract forge operations required by the Spec Butler orchestrator.

    Any forge backend (GitHub, GitLab, Bitbucket, etc.) must implement
    this protocol to integrate with the spec workflow.
    """

    def get_auth_token(self) -> str:
        """Return an authentication token for API calls."""
        ...

    def push_branch(
        self,
        branch: str,
        *,
        cwd: Path,
        remote: str = "origin",
        force: bool = False,
        expect_sha: str = "",
    ) -> PushResult:
        """Push a local branch to the remote.

        When ``expect_sha`` is set the push uses a lease pinned to that SHA
        (``--force-with-lease=<branch>:<expect_sha>``), so it succeeds only if
        the remote branch still points at exactly that commit.
        """
        ...

    def find_pr_for_branch(
        self,
        head_branch: str,
        *,
        base_branch: str | None = None,
        cwd: Path | None = None,
    ) -> PullRequest | None:
        """Find an open PR for the given head branch targeting base_branch, or None."""
        ...

    def create_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> PullRequest:
        """Create a new pull request. Raises RuntimeError on failure."""
        ...

    def update_pr(
        self,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        add_labels: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> bool:
        """Update an existing PR's title/body/labels. Returns True on success."""
        ...

    def mark_pr_ready(self, pr_number: int, *, cwd: Path | None = None) -> bool:
        """Mark a draft PR ready for review. Returns True on success."""
        ...

    def mark_pr_draft(self, pr_number: int, *, cwd: Path | None = None) -> bool:
        """Mark a PR as draft. Returns True on success."""
        ...

    def get_pr_checks(self, pr_number: int, *, cwd: Path | None = None) -> list[CheckRun]:
        """Return the list of check runs for a PR."""
        ...

    def get_required_checks(self, pr_number: int, *, cwd: Path | None = None) -> list[CheckRun] | None:
        """Return required checks, [] when none are configured, None when the
        query failed and the state is unknown."""
        ...

    def merge_pr(
        self,
        pr_number: int,
        *,
        method: str = "squash",
        auto: bool = False,
        expected_head_sha: str | None = None,
        cwd: Path | None = None,
    ) -> MergeResult:
        """Merge a PR. Returns MergeResult with status."""
        ...

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: str,
        description: str = "",
        target_url: str = "",
    ) -> bool:
        """Set a commit status (pending/success/failure/error)."""
        ...

    def get_repo_slug(self, *, cwd: Path | None = None) -> str:
        """Return 'owner/repo' for the current repository."""
        ...


# ---------------------------------------------------------------------------
# GitHub implementation
# ---------------------------------------------------------------------------


def _default_run_fn(
    cmd: list[str],
    cwd: Path | None = None,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Default command runner using ``subprocess.run``."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        **subprocess_text_kwargs(cmd),
        **kwargs,
    )


# Module-level type alias for the runner callable.
RunFn = type(_default_run_fn)


def _run_gh(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    run_fn: RunFn | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` CLI command and return the result."""
    runner = run_fn or _default_run_fn
    cmd = ["gh", *args]
    result = runner(cmd, cwd=cwd)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result


class GitHubForge:
    """GitHub forge adapter using the ``gh`` CLI.

    Parameters
    ----------
    run_fn : callable, optional
        Command runner function with the signature ``(cmd, cwd=None, **kw)``.
        Defaults to ``subprocess.run`` with ``capture_output=True, text=True``.
        The orchestrator passes its own ``run_subprocess`` so that tests can
        mock command execution via a single patch point.
    """

    def __init__(self, *, run_fn: RunFn | None = None) -> None:
        self._run = run_fn or _default_run_fn

    def get_auth_token(self) -> str:
        # Prefer explicit env vars over gh CLI to avoid unnecessary subprocess calls.
        for env_var in ("GH_TOKEN", "GITHUB_TOKEN"):
            token = os.getenv(env_var, "").strip()
            if token:
                return token
        result = self._run(["gh", "auth", "token"])
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _gh(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``gh`` CLI command through the configured runner."""
        kwargs: dict[str, object] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._run(["gh", *args], cwd=cwd, **kwargs)

    def push_branch(
        self,
        branch: str,
        *,
        cwd: Path,
        remote: str = "origin",
        force: bool = False,
        set_upstream: bool = True,
        expect_sha: str = "",
    ) -> PushResult:
        cmd = ["git", "push"]
        if set_upstream:
            cmd.append("-u")
        if expect_sha:
            cmd.append(f"--force-with-lease={branch}:{expect_sha}")
        elif force:
            cmd.append("--force-with-lease")
        cmd.extend([remote, branch])
        result = self._run(cmd, cwd=cwd)
        if result.returncode != 0:
            return PushResult(ok=False, message=result.stderr.strip())
        return PushResult(ok=True)

    def find_pr_for_branch(
        self,
        head_branch: str,
        *,
        base_branch: str | None = None,
        cwd: Path | None = None,
    ) -> PullRequest | None:
        if not base_branch:
            config_path = cwd / ".spec.toml" if cwd is not None else None
            base_branch = load_spec_runtime_config(require=False, config_path=config_path).pr_base_branch
        result = self._gh(
            [
                "pr",
                "list",
                "--head",
                head_branch,
                "--base",
                base_branch,
                "--state",
                "open",
                "--json",
                "number,url,headRefName,baseRefName,title,state,isDraft",
                "--limit",
                "1",
            ],
            cwd=cwd,
        )
        if result.returncode != 0:
            return None
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            # Plain number output (e.g. from --jq) — treat as PR number.
            try:
                number = int(stdout)
                return PullRequest(number=number, head_branch=head_branch)
            except ValueError:
                return None
        if isinstance(parsed, list):
            if not parsed:
                return None
            pr = parsed[0]
        elif isinstance(parsed, dict):
            pr = parsed
        elif isinstance(parsed, int):
            return PullRequest(number=parsed, head_branch=head_branch)
        else:
            return None
        return PullRequest(
            number=pr.get("number", 0),
            url=pr.get("url", ""),
            head_branch=pr.get("headRefName", head_branch),
            base_branch=pr.get("baseRefName", ""),
            title=pr.get("title", ""),
            state=pr.get("state", ""),
            is_draft=bool(pr["isDraft"]) if "isDraft" in pr else None,
        )

    def create_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> PullRequest:
        args = [
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        if draft:
            args.append("--draft")
        for label in labels:
            label_value = label.strip()
            if label_value:
                args.extend(["--label", label_value])
        result = self._gh(args, cwd=cwd)
        if result.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {result.stderr.strip()}")
        url = result.stdout.strip()
        number = 0
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))
        return PullRequest(number=number, url=url, head_branch=head, base_branch=base, title=title)

    def update_pr(
        self,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        add_labels: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> bool:
        args = ["pr", "edit", str(pr_number)]
        if title is not None:
            args.extend(["--title", title])
        if body is not None:
            args.extend(["--body", body])
        for label in add_labels:
            label_value = label.strip()
            if label_value:
                args.extend(["--add-label", label_value])
        result = self._gh(args, cwd=cwd)
        return result.returncode == 0

    def mark_pr_ready(self, pr_number: int, *, cwd: Path | None = None) -> bool:
        result = self._gh(["pr", "ready", str(pr_number)], cwd=cwd)
        return result.returncode == 0

    def mark_pr_draft(self, pr_number: int, *, cwd: Path | None = None) -> bool:
        result = self._gh(["pr", "ready", str(pr_number), "--undo"], cwd=cwd)
        return result.returncode == 0

    def get_pr_checks(self, pr_number: int, *, cwd: Path | None = None) -> list[CheckRun]:
        result = self._gh(
            ["pr", "checks", str(pr_number), "--json", "name,state,bucket,link"],
            cwd=cwd,
        )
        if result.returncode != 0:
            return []
        try:
            checks = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return []
        return [
            CheckRun(
                name=c.get("name", ""),
                state=c.get("state", ""),
                url=c.get("link", ""),
                bucket=c.get("bucket", ""),
            )
            for c in checks
        ]

    def get_required_checks(self, pr_number: int, *, cwd: Path | None = None) -> list[CheckRun] | None:
        """Return required checks, [] when none are configured, None when unknown.

        ``gh pr checks`` exits non-zero when checks are pending or failing
        (exit code 8 for pending) while still printing the JSON payload, so
        the payload — not the exit code — is authoritative. Only an
        unparseable payload returns None, letting callers treat "could not
        query" differently from "no checks required".
        """
        result = self._gh(
            ["pr", "checks", str(pr_number), "--required", "--json", "name,state,bucket,link"],
            cwd=cwd,
        )
        try:
            checks = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(checks, list):
            return None
        return [
            CheckRun(
                name=c.get("name", ""),
                state=c.get("state", ""),
                url=c.get("link", ""),
                bucket=c.get("bucket", ""),
            )
            for c in checks
        ]

    def merge_pr(
        self,
        pr_number: int,
        *,
        method: str = "squash",
        auto: bool = False,
        expected_head_sha: str | None = None,
        cwd: Path | None = None,
    ) -> MergeResult:
        args = ["pr", "merge", str(pr_number), f"--{method}"]
        if auto:
            args.append("--auto")
        if expected_head_sha:
            args.extend(["--match-head-commit", expected_head_sha])
        try:
            result = self._gh(
                args,
                cwd=cwd,
                timeout=AUTO_MERGE_ARM_TIMEOUT_SECONDS if auto else None,
            )
        except subprocess.TimeoutExpired:
            # Recent gh versions can keep `pr merge --auto` attached until the
            # PR actually lands. That blocks the orchestrator from polling
            # required checks (and from restoring an exact-head local-review
            # status reset by draft-to-ready workflows). Confirm the mutation
            # took effect, then return control to the orchestrator's bounded
            # merge poll loop.
            confirmation = self._gh(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,autoMergeRequest,headRefOid",
                ],
                cwd=cwd,
            )
            if confirmation.returncode == 0:
                try:
                    payload = json.loads(confirmation.stdout or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                head_matches = not expected_head_sha or (
                    isinstance(payload, dict)
                    and str(payload.get("headRefOid", "")).lower()
                    == expected_head_sha.lower()
                )
                merge_confirmed = isinstance(payload, dict) and (
                    str(payload.get("state", "")).upper() == "MERGED"
                    or payload.get("autoMergeRequest") is not None
                )
                if head_matches and merge_confirmed:
                    return MergeResult(ok=True, message="auto-merge armed; gh wait detached")
            return MergeResult(
                ok=False,
                message=(
                    "GraphQL: enablePullRequestAutoMerge timed out before "
                    "the resulting PR state could be confirmed"
                ),
            )
        if result.returncode != 0:
            return MergeResult(ok=False, message=result.stderr.strip())
        return MergeResult(ok=True)

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: str,
        description: str = "",
        target_url: str = "",
    ) -> bool:
        token = self.get_auth_token()
        if not token:
            return False
        slug = self.get_repo_slug()
        if not slug:
            return False
        import urllib.request

        url = f"https://api.github.com/repos/{slug}/statuses/{sha}"
        payload: dict = {"context": context, "state": state}
        if description:
            payload["description"] = description
        if target_url:
            payload["target_url"] = target_url
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except Exception:
            return False

    def get_repo_slug(self, *, cwd: Path | None = None) -> str:
        result = self._gh(
            ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_FORGE_ADAPTER: ForgeAdapter | None = None


def get_forge_adapter() -> ForgeAdapter:
    """Return the forge adapter for the current environment.

    Currently always returns a ``GitHubForge`` instance.  Future providers
    (GitLab, Bitbucket) can be selected via configuration.
    """
    global _FORGE_ADAPTER
    if _FORGE_ADAPTER is None:
        _FORGE_ADAPTER = GitHubForge()
    return _FORGE_ADAPTER


def set_forge_adapter(adapter: ForgeAdapter) -> None:
    """Override the forge adapter (useful for testing)."""
    global _FORGE_ADAPTER
    _FORGE_ADAPTER = adapter
