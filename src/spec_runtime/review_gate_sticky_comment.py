#!/usr/bin/env python3
"""Publish a sticky PR comment for the latest review-decision-gate result."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .review_decision import review_payload_decision

STICKY_MARKER = "<!-- review-decision-gate-sticky-comment -->"
EMBEDDED_REVIEW_RESULT_MARKER = "review-decision-gate-review-result"
MAX_COMMENT_FINDINGS = 5
REVIEW_GATE_ARTIFACT_NAME = "review-decision-gate-result"
SEVERITY_ORDER = {
    "P0": 0,
    "P1": 1,
    "P2": 2,
    "P3": 3,
    "P4": 4,
}
EMBEDDED_REVIEW_RESULT_RE = re.compile(
    rf"<!--\s*{re.escape(EMBEDDED_REVIEW_RESULT_MARKER)}:(?P<payload>[A-Za-z0-9_=-]+)\s*-->"
)


class GitHubApiError(RuntimeError):
    """Raised when the GitHub issue comment API returns an error."""


class GitHubIssueCommentClient:
    """Minimal GitHub REST client for PR issue comments."""

    def __init__(
        self,
        *,
        repo: str,
        pr_number: int,
        token: str,
        api_base_url: str = "https://api.github.com",
        urlopen=urllib_request.urlopen,
    ) -> None:
        self.repo = repo.strip()
        self.pr_number = int(pr_number)
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self._urlopen = urlopen

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "specbutler-review-gate-sticky-comment",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib_request.Request(url, data=body, headers=headers, method=method)

        try:
            with self._urlopen(request, timeout=30) as response:
                response_text = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = detail or exc.reason or "unknown HTTP error"
            raise GitHubApiError(f"GitHub API {method} {path} failed with HTTP {exc.code}: {message}") from exc
        except urllib_error.URLError as exc:
            raise GitHubApiError(f"GitHub API {method} {path} failed: {exc.reason}") from exc

        if not response_text.strip():
            return {}

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(
                f"GitHub API {method} {path} returned invalid JSON: line {exc.lineno} column {exc.colno}"
            ) from exc

    def list_comments(self) -> list[dict[str, Any]]:
        repo = urllib_parse.quote(self.repo, safe="/")
        params = urllib_parse.urlencode(
            {
                "per_page": 100,
                "sort": "updated",
                "direction": "desc",
            }
        )
        payload = self._request(
            "GET",
            f"repos/{repo}/issues/{self.pr_number}/comments?{params}",
        )
        if not isinstance(payload, list):
            raise GitHubApiError("GitHub API returned a non-list issue comment payload.")
        return [comment for comment in payload if isinstance(comment, dict)]

    def create_comment(self, body: str) -> dict[str, Any]:
        repo = urllib_parse.quote(self.repo, safe="/")
        payload = self._request(
            "POST",
            f"repos/{repo}/issues/{self.pr_number}/comments",
            {"body": body},
        )
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub API returned a non-object create-comment payload.")
        return payload

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        repo = urllib_parse.quote(self.repo, safe="/")
        payload = self._request(
            "PATCH",
            f"repos/{repo}/issues/comments/{int(comment_id)}",
            {"body": body},
        )
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub API returned a non-object update-comment payload.")
        return payload


def _decision_title(decision: str) -> str:
    normalized = decision.strip().lower() or "failed"
    return normalized.replace("_", " ").title()


def _format_location(finding: dict[str, Any]) -> str:
    file_path = str(finding.get("file", "")).strip()
    start_line = finding.get("start_line")
    end_line = finding.get("end_line")
    if file_path and isinstance(start_line, int) and isinstance(end_line, int) and end_line != start_line:
        return f"{file_path}:{start_line}-{end_line}"
    if file_path and isinstance(start_line, int):
        return f"{file_path}:{start_line}"
    if file_path:
        return file_path
    return "unknown location"


def _normalize_findings(review_result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = review_result.get("findings", [])
    if not isinstance(findings, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, raw_finding in enumerate(findings):
        if not isinstance(raw_finding, dict):
            continue
        severity = str(raw_finding.get("severity", "P2")).upper().strip() or "P2"
        normalized.append(
            {
                "index": index,
                "severity": severity,
                "title": str(raw_finding.get("title") or raw_finding.get("id") or "Untitled finding").strip(),
                "location": _format_location(raw_finding),
                "body": str(raw_finding.get("body", "")).strip(),
            }
        )

    normalized.sort(
        key=lambda finding: (
            SEVERITY_ORDER.get(str(finding["severity"]), 99),
            int(finding["index"]),
        )
    )
    return normalized


def encode_embedded_review_result(review_result: dict[str, Any]) -> str:
    payload = json.dumps(review_result, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def render_embedded_review_result(review_result: dict[str, Any]) -> str:
    return f"<!-- {EMBEDDED_REVIEW_RESULT_MARKER}:{encode_embedded_review_result(review_result)} -->"


def extract_embedded_review_result(comment_body: str) -> dict[str, Any] | None:
    match = EMBEDDED_REVIEW_RESULT_RE.search(comment_body or "")
    if not match:
        return None
    encoded = match.group("payload")
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        parsed = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def render_sticky_comment(
    review_result: dict[str, Any],
    *,
    pr_number: int | None = None,
    marker: str = STICKY_MARKER,
) -> str:
    decision = review_payload_decision(review_result) or "failed"
    summary = str(review_result.get("summary", "")).strip() or f"Review decision: {decision}"
    source_check_name = (
        str(review_result.get("source_check_name", "review-decision-gate")).strip() or "review-decision-gate"
    )
    source_check_url = str(review_result.get("source_check_url", "")).strip()
    findings = _normalize_findings(review_result)

    lines = [
        marker,
        render_embedded_review_result(review_result),
        f"## Codex Review Gate: {_decision_title(decision)}",
        "",
        ("Informational only: the required `review-decision-gate` status check remains the merge authority."),
        "",
        f"Current decision: `{decision}`.",
        "",
        f"Summary: {summary}",
    ]

    if decision == "approved":
        lines.extend(
            [
                "",
                "The latest `review-decision-gate` run approved this PR.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"Findings: `{len(findings)}` total; showing up to `{MAX_COMMENT_FINDINGS}`.",
            ]
        )
        if findings:
            lines.extend(["", "### High-Signal Findings"])
            for finding in findings[:MAX_COMMENT_FINDINGS]:
                lines.append(f"- [{finding['severity']}] {finding['title']} ({finding['location']})")
                if finding["body"]:
                    lines.append(f"  {finding['body']}")

    lines.extend(["", "### Deeper Inspection"])
    if source_check_url:
        lines.append(f"- Check Details / job summary: [{source_check_name}]({source_check_url})")
    else:
        lines.append(f"- Check Details / job summary: `{source_check_name}`")
    if pr_number is not None:
        lines.append(f"- Full machine-readable findings: `spec review --pr {pr_number}`")
    else:
        lines.append("- Full machine-readable findings: `spec review --pr <pr-number>`")
    if "/actions/runs/" in source_check_url:
        lines.append(f"- Artifact fallback: download `{REVIEW_GATE_ARTIFACT_NAME}` from the workflow run.")
    else:
        lines.append("- Local review payload: embedded in this sticky comment for `spec review`.")

    return "\n".join(lines).rstrip() + "\n"


def _extract_comment_id(comment_payload: dict[str, Any]) -> int:
    comment_id = comment_payload.get("id")
    if not isinstance(comment_id, int):
        raise GitHubApiError("GitHub API comment payload is missing an integer `id`.")
    return comment_id


def upsert_sticky_comment(
    client: GitHubIssueCommentClient,
    body: str,
    *,
    marker: str = STICKY_MARKER,
) -> tuple[str, int]:
    for comment in client.list_comments():
        if marker in str(comment.get("body", "")):
            comment_id = _extract_comment_id(comment)
            updated = client.update_comment(comment_id, body)
            return "updated", _extract_comment_id(updated)

    created = client.create_comment(body)
    return "created", _extract_comment_id(created)


def publish_sticky_comment(
    client: GitHubIssueCommentClient,
    review_result: dict[str, Any],
    *,
    pr_number: int,
    marker: str = STICKY_MARKER,
) -> tuple[str, int]:
    body = render_sticky_comment(review_result, pr_number=pr_number, marker=marker)
    return upsert_sticky_comment(client, body, marker=marker)


def load_review_result(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read review result file: {path} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Review result file is not valid JSON: {path} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Review result file must contain a JSON object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_gate_sticky_comment",
        description="Publish or update the review-decision-gate sticky PR comment.",
    )
    parser.add_argument(
        "--review-result",
        required=True,
        help="Path to review-decision.json",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--pr-number", required=True, type=int, help="Pull request number")
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token with pull-requests: write",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="Base URL for the GitHub API",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    token = str(args.github_token).strip()
    if not token:
        print(
            "Failed to publish review gate sticky comment: missing GitHub token.",
            file=sys.stderr,
        )
        return 1

    try:
        review_result = load_review_result(Path(args.review_result))
        client = GitHubIssueCommentClient(
            repo=str(args.repo),
            pr_number=int(args.pr_number),
            token=token,
            api_base_url=str(args.api_base_url),
        )
        action, comment_id = publish_sticky_comment(
            client,
            review_result,
            pr_number=int(args.pr_number),
        )
    except (GitHubApiError, ValueError) as exc:
        print(f"Failed to publish review gate sticky comment: {exc}", file=sys.stderr)
        return 1

    print(f"{action.capitalize()} sticky review gate comment on PR #{args.pr_number} (comment id {comment_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
