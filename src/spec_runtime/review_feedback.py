#!/usr/bin/env python3
"""Shared review-gate lookup/parsing helpers plus a local PR review CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import load_spec_runtime_config
from .git_common import subprocess_text_kwargs
from .review_gate_sticky_comment import STICKY_MARKER, extract_embedded_review_result
from .spec_identity import pr_body_uses_local_review

REVIEW_GATE_CHECK_NAME = "review-decision-gate"
REVIEW_GATE_ARTIFACT_NAME = "review-decision-gate-result"
REVIEW_GATE_ARTIFACT_FILE = "review-decision.json"
REVIEW_DECISION_VALUES = ("approved", "request_changes", "blocked", "failed")

RunSubprocess = Callable[..., subprocess.CompletedProcess[str]]
ArtifactPayloadLoader = Callable[[Path, dict], dict | None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_root(repo_root: Path) -> Path:
    return repo_root / load_spec_runtime_config(require=False).paths.state_dir


def default_run_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        timeout=timeout,
        **subprocess_text_kwargs(cmd),
    )


def resolve_repo_root() -> Path:
    result = default_run_subprocess(
        ["git", "rev-parse", "--show-toplevel"],
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Could not resolve repository root: {detail}")
    return Path(result.stdout.strip())


@dataclass
class ReviewFinding:
    id: str = ""
    title: str = ""
    severity: str = ""
    file: str = ""
    start_line: int = 1
    end_line: int = 1
    body: str = ""
    confidence: float = 0.0


@dataclass
class ReviewResult:
    status: str = ""
    summary: str = ""
    findings: list[ReviewFinding] = field(default_factory=list)
    attempt_number: int | None = None
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    reviewer_role: str = ""
    reviewer_agent: str = ""
    source_check_name: str = REVIEW_GATE_CHECK_NAME
    source_check_url: str = ""
    reviewed_at: str = field(default_factory=_now_iso)

    def save(self, repo_root: Path, run_id: str) -> Path:
        output_dir = _state_root(repo_root) / "runs" / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "review-result.json"
        rendered = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        output_path.write_text(rendered)
        if self.attempt_number is not None and self.attempt_number > 0:
            attempt_path = output_dir / f"review-result.attempt-{self.attempt_number}.json"
            attempt_path.write_text(rendered)
        return output_path

    @classmethod
    def load(cls, repo_root: Path, run_id: str) -> ReviewResult | None:
        input_path = _state_root(repo_root) / "runs" / run_id / "review-result.json"
        if not input_path.exists():
            return None
        try:
            data = json.loads(input_path.read_text())
        except (json.JSONDecodeError, TypeError):
            return None

        findings = data.get("findings", [])
        data["findings"] = [ReviewFinding(**finding) for finding in findings if isinstance(finding, dict)]
        try:
            return cls(**data)
        except TypeError:
            return None


@dataclass
class ReviewGateStatus:
    pr_number: int
    pr_url: str
    head_sha: str
    base_sha: str
    check_name: str
    check_state: str
    check_bucket: str
    details_url: str
    run_id: str
    artifact_name: str = REVIEW_GATE_ARTIFACT_NAME
    artifact_file: str = REVIEW_GATE_ARTIFACT_FILE


class ReviewLookupError(RuntimeError):
    """Raised when a PR review result cannot be located or parsed."""

    def __init__(
        self,
        message: str,
        *,
        details_url: str = "",
        run_id: str = "",
        artifact_name: str = REVIEW_GATE_ARTIFACT_NAME,
        artifact_file: str = REVIEW_GATE_ARTIFACT_FILE,
    ) -> None:
        super().__init__(message)
        self.details_url = details_url
        self.run_id = run_id
        self.artifact_name = artifact_name
        self.artifact_file = artifact_file

    def recovery_message(self) -> str:
        details_url = self.details_url or "n/a"
        run_id = self.run_id or "unknown"
        return (
            "Recovery: start with `gh pr checks <pr-number> --required --json "
            "name,state,bucket,link`, open the review gate Details URL for the job "
            f"summary ({details_url}), then inspect GitHub Actions run {run_id} and "
            f"look for artifact `{self.artifact_name}` containing "
            f"`{self.artifact_file}`."
        )


def short_sha(sha: str) -> str:
    return sha[:12] if sha else ""


def summarize_review_findings(findings: list[ReviewFinding], limit: int = 3) -> str:
    if not findings:
        return "(none)"
    snippets = []
    for finding in findings[:limit]:
        location = f"{finding.file}:{finding.start_line}-{finding.end_line}" if finding.file else "unknown location"
        snippets.append(f"[{finding.severity or 'P2'}] {finding.title or finding.id} ({location})")
    return "; ".join(snippets)


def _coerce_line_number(value: object, default: int = 1) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_confidence(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def parse_json_object(text: str) -> dict | None:
    if not text:
        return None

    stripped = text.strip()
    candidates = [stripped]
    for match in re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidates.append(match.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1].strip())

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _normalize_review_decision(raw: object) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "approve": "approved",
        "approved": "approved",
        "changes_requested": "request_changes",
        "request_changes": "request_changes",
        "request-changes": "request_changes",
        "blocked": "blocked",
        "failed": "failed",
    }
    return aliases.get(value, "")


def _normalize_review_finding(item: dict, index: int) -> ReviewFinding:
    start_line = _coerce_line_number(
        item.get("start_line", item.get("line", 1)),
        default=1,
    )
    end_line = _coerce_line_number(
        item.get("end_line", item.get("line", start_line)),
        default=start_line,
    )
    if end_line < start_line:
        end_line = start_line
    return ReviewFinding(
        id=str(item.get("id") or f"finding-{index + 1}").strip(),
        title=str(item.get("title") or f"Finding {index + 1}").strip(),
        severity=str(item.get("severity") or "").strip(),
        file=str(item.get("file") or "").strip(),
        start_line=start_line,
        end_line=end_line,
        body=str(item.get("body") or "").strip(),
        confidence=_coerce_confidence(item.get("confidence", 0.0)),
    )


def _is_review_payload_candidate(payload: dict) -> bool:
    if any(
        key in payload
        for key in (
            "decision",
            "reviewed_head_sha",
            "head_sha",
            "reviewed_base_sha",
            "base_sha",
            "findings",
        )
    ):
        return True
    return "status" in payload


def normalize_review_payload(
    payload: dict,
    *,
    expected_head_sha: str,
    expected_base_sha: str,
    check_run: dict,
) -> ReviewResult:
    decision = _normalize_review_decision(payload.get("status", payload.get("decision")))
    if decision not in REVIEW_DECISION_VALUES:
        raise ValueError("review payload has invalid decision/status")

    reviewed_head_sha = str(payload.get("reviewed_head_sha", payload.get("head_sha", ""))).strip()
    if not reviewed_head_sha:
        raise ValueError("review payload missing reviewed_head_sha")
    if reviewed_head_sha != expected_head_sha:
        raise ValueError(
            "review payload head SHA mismatch: "
            f"expected {short_sha(expected_head_sha)}, got {short_sha(reviewed_head_sha)}"
        )

    reviewed_base_sha = str(payload.get("reviewed_base_sha", payload.get("base_sha", expected_base_sha))).strip()
    summary = str(payload.get("summary", payload.get("message", ""))).strip()
    if not summary:
        summary = f"Review decision: {decision}"

    findings_raw = payload.get("findings", [])
    if findings_raw is None:
        findings_raw = []
    if not isinstance(findings_raw, list):
        raise ValueError("review payload findings must be an array")

    findings: list[ReviewFinding] = []
    for index, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            raise ValueError("review payload finding item must be an object")
        findings.append(_normalize_review_finding(item, index))

    return ReviewResult(
        status=decision,
        summary=summary,
        findings=findings,
        reviewed_head_sha=reviewed_head_sha,
        reviewed_base_sha=reviewed_base_sha,
        reviewer_role=str(payload.get("reviewer_role", "independent-review")).strip(),
        reviewer_agent=str(payload.get("reviewer_agent", "codex")).strip(),
        source_check_name=str(check_run.get("name", REVIEW_GATE_CHECK_NAME)).strip(),
        source_check_url=str(check_run.get("details_url", "")).strip(),
        reviewed_at=str(payload.get("reviewed_at", check_run.get("completed_at", _now_iso()))).strip(),
    )


def extract_run_id_from_check_url(url: str) -> str:
    match = re.search(r"/actions/runs/(\d+)", url)
    return match.group(1) if match else ""


def extract_job_id_from_check_url(url: str) -> str:
    match = re.search(r"/job/(\d+)", url)
    return match.group(1) if match else ""


def check_name_matches(actual: str, expected: str) -> bool:
    actual_norm = actual.strip().lower()
    expected_norm = expected.strip().lower()
    if actual_norm == expected_norm:
        return True
    return (
        actual_norm.endswith(f"/ {expected_norm}")
        or actual_norm.endswith(f": {expected_norm}")
        or actual_norm.endswith(f" {expected_norm}")
    )


def find_check_run_for_sha(
    repo_root: Path,
    head_sha: str,
    check_name: str,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> dict | None:
    check_result = run_subprocess(
        ["gh", "api", f"repos/:owner/:repo/commits/{head_sha}/check-runs?per_page=100"],
        cwd=repo_root,
    )
    if check_result.returncode != 0:
        detail = check_result.stderr.strip() or check_result.stdout.strip()
        return {"__error__": detail or "gh api check-runs failed"}
    try:
        payload = json.loads(check_result.stdout)
    except json.JSONDecodeError:
        return {"__error__": "Could not parse check-runs JSON"}

    check_runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
    if not isinstance(check_runs, list):
        return None

    matching = [
        item
        for item in check_runs
        if isinstance(item, dict) and check_name_matches(str(item.get("name", "")), check_name)
    ]
    if not matching:
        return None
    matching.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
    return matching[0]


def find_commit_status_for_sha(
    repo_root: Path,
    head_sha: str,
    context: str,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> dict | None:
    status_result = run_subprocess(
        ["gh", "api", f"repos/:owner/:repo/commits/{head_sha}/status"],
        cwd=repo_root,
    )
    if status_result.returncode != 0:
        detail = status_result.stderr.strip() or status_result.stdout.strip()
        return {"__error__": detail or "gh api commit status failed"}
    try:
        payload = json.loads(status_result.stdout)
    except json.JSONDecodeError:
        return {"__error__": "Could not parse commit status JSON"}

    statuses = payload.get("statuses", []) if isinstance(payload, dict) else []
    if not isinstance(statuses, list):
        return None

    matching = [
        item
        for item in statuses
        if isinstance(item, dict) and check_name_matches(str(item.get("context", "")), context)
    ]
    if not matching:
        return None
    matching.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
    return matching[0]


def _load_sticky_review_comment_payload(
    repo_root: Path,
    pr_number: int,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> dict | None:
    comments_result = run_subprocess(
        [
            "gh",
            "api",
            f"repos/:owner/:repo/issues/{pr_number}/comments?per_page=100&sort=updated&direction=desc",
        ],
        cwd=repo_root,
    )
    if comments_result.returncode != 0:
        detail = comments_result.stderr.strip() or comments_result.stdout.strip()
        raise ValueError(f"could not query review gate sticky comment: {detail or 'unknown error'}")
    try:
        comments = json.loads(comments_result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("could not parse review gate sticky comment payload") from exc
    if not isinstance(comments, list):
        return None

    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body", ""))
        if STICKY_MARKER not in body:
            continue
        payload = extract_embedded_review_result(body)
        if payload is not None:
            return payload
    return None


def load_review_payload_from_gate_artifact(
    repo_root: Path,
    check_run: dict,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> dict | None:
    details_url = str(check_run.get("details_url", "")).strip()
    run_id = extract_run_id_from_check_url(details_url)
    if not run_id:
        return None

    with tempfile.TemporaryDirectory(prefix="review-gate-artifact-") as tmpdir:
        download_result = run_subprocess(
            [
                "gh",
                "run",
                "download",
                run_id,
                "-n",
                REVIEW_GATE_ARTIFACT_NAME,
                "-D",
                tmpdir,
            ],
            cwd=repo_root,
        )
        if download_result.returncode != 0:
            detail = download_result.stderr.strip() or download_result.stdout.strip()
            raise ValueError(
                f"could not download {REVIEW_GATE_ARTIFACT_NAME} artifact from run {run_id}: "
                f"{detail or 'unknown error'}"
            )

        candidate_paths = sorted(Path(tmpdir).rglob(REVIEW_GATE_ARTIFACT_FILE))
        if not candidate_paths:
            raise ValueError(f"artifact {REVIEW_GATE_ARTIFACT_NAME} does not contain {REVIEW_GATE_ARTIFACT_FILE}")

        parsed = parse_json_object(candidate_paths[0].read_text())
        if parsed is None:
            raise ValueError(f"artifact {REVIEW_GATE_ARTIFACT_NAME} contains invalid JSON payload")

        nested_candidates = [
            parsed,
            parsed.get("review_result"),
            parsed.get("result"),
            parsed.get("review"),
        ]
        for payload in nested_candidates:
            if not isinstance(payload, dict):
                continue
            if "decision" in payload or "status" in payload or "reviewed_head_sha" in payload:
                return payload

    raise ValueError(f"artifact {REVIEW_GATE_ARTIFACT_NAME} is missing decision payload")


def extract_review_result_from_check_run(
    check_run: dict,
    *,
    repo_root: Path | None = None,
    expected_head_sha: str,
    expected_base_sha: str,
    require_payload: bool,
    run_subprocess: RunSubprocess = default_run_subprocess,
    load_artifact_payload: ArtifactPayloadLoader | None = None,
) -> ReviewResult:
    output = check_run.get("output") if isinstance(check_run.get("output"), dict) else {}
    text_candidates = [
        str(output.get("text", "")),
        str(output.get("summary", "")),
    ]
    last_payload_error = ""

    for text in text_candidates:
        parsed = parse_json_object(text)
        if parsed is None:
            continue
        nested_candidates = [
            parsed,
            parsed.get("review_result"),
            parsed.get("result"),
            parsed.get("review"),
        ]
        for payload in nested_candidates:
            if not isinstance(payload, dict):
                continue
            if not _is_review_payload_candidate(payload):
                continue
            try:
                return normalize_review_payload(
                    payload,
                    expected_head_sha=expected_head_sha,
                    expected_base_sha=expected_base_sha,
                    check_run=check_run,
                )
            except ValueError as exc:
                last_payload_error = str(exc)

    artifact_error = ""
    if require_payload and repo_root is not None:
        artifact_loader = load_artifact_payload or (
            lambda current_repo_root, current_check_run: load_review_payload_from_gate_artifact(
                current_repo_root,
                current_check_run,
                run_subprocess=run_subprocess,
            )
        )
        try:
            artifact_payload = artifact_loader(repo_root, check_run)
        except ValueError as exc:
            artifact_error = str(exc)
        else:
            if artifact_payload is not None:
                try:
                    return normalize_review_payload(
                        artifact_payload,
                        expected_head_sha=expected_head_sha,
                        expected_base_sha=expected_base_sha,
                        check_run=check_run,
                    )
                except ValueError as exc:
                    artifact_error = str(exc)

    if require_payload:
        if artifact_error:
            raise ValueError(artifact_error)
        if last_payload_error:
            raise ValueError(last_payload_error)
        raise ValueError("missing machine-readable review payload from review-decision-gate output")

    if last_payload_error:
        raise ValueError(last_payload_error)

    return ReviewResult(
        status="approved",
        summary=f"{REVIEW_GATE_CHECK_NAME} passed for {short_sha(expected_head_sha)}",
        findings=[],
        reviewed_head_sha=expected_head_sha,
        reviewed_base_sha=expected_base_sha,
        reviewer_role="independent-review",
        reviewer_agent="unknown",
        source_check_name=str(check_run.get("name", REVIEW_GATE_CHECK_NAME)).strip(),
        source_check_url=str(check_run.get("details_url", "")).strip(),
        reviewed_at=str(check_run.get("completed_at", _now_iso())).strip(),
    )


def extract_review_result_from_commit_status(
    commit_status: dict,
    *,
    repo_root: Path | None = None,
    pr_number: int,
    expected_head_sha: str,
    expected_base_sha: str,
    require_payload: bool,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> ReviewResult:
    state = str(commit_status.get("state", "")).strip().lower()
    description = str(commit_status.get("description", "")).strip()
    target_url = str(commit_status.get("target_url", "")).strip()

    if repo_root is not None:
        payload = _load_sticky_review_comment_payload(
            repo_root,
            pr_number,
            run_subprocess=run_subprocess,
        )
        if payload is not None:
            return normalize_review_payload(
                payload,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
                check_run={
                    "name": commit_status.get("context", REVIEW_GATE_CHECK_NAME),
                    "details_url": target_url,
                    "completed_at": commit_status.get("updated_at", _now_iso()),
                },
            )

    if require_payload:
        raise ValueError("missing machine-readable review payload from local review sticky comment")

    if state == "pending":
        summary = description or f"Local review is still pending for {short_sha(expected_head_sha)}"
        status = "blocked"
    else:
        summary = description or f"{REVIEW_GATE_CHECK_NAME} passed for {short_sha(expected_head_sha)}"
        status = "approved"

    return ReviewResult(
        status=status,
        summary=summary,
        findings=[],
        reviewed_head_sha=expected_head_sha,
        reviewed_base_sha=expected_base_sha,
        reviewer_role="independent-review",
        reviewer_agent="codex",
        source_check_name=str(commit_status.get("context", REVIEW_GATE_CHECK_NAME)).strip(),
        source_check_url=target_url,
        reviewed_at=str(commit_status.get("updated_at", _now_iso())).strip(),
    )


def _load_pr_metadata(
    repo_root: Path,
    pr_number: int,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> dict:
    pr_result = run_subprocess(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,url,headRefOid,baseRefOid,body",
        ],
        cwd=repo_root,
    )
    if pr_result.returncode != 0:
        detail = pr_result.stderr.strip() or pr_result.stdout.strip() or "unknown error"
        raise ReviewLookupError(
            f"Could not query PR #{pr_number}: {detail}",
        )
    try:
        payload = json.loads(pr_result.stdout)
    except json.JSONDecodeError as exc:
        raise ReviewLookupError(
            f"Could not parse `gh pr view` output for PR #{pr_number}",
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewLookupError(f"Unexpected PR payload for PR #{pr_number}")
    return payload


def _load_required_checks(
    repo_root: Path,
    pr_number: int,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> list[dict]:
    checks_result = run_subprocess(
        [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--required",
            "--json",
            "name,state,bucket,link",
        ],
        cwd=repo_root,
    )
    if checks_result.returncode != 0:
        detail = checks_result.stderr.strip() or checks_result.stdout.strip() or "unknown error"
        raise ReviewLookupError(
            f"Could not query required checks for PR #{pr_number}: {detail}",
        )
    try:
        payload = json.loads(checks_result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReviewLookupError(
            f"Could not parse required checks from `gh pr checks` for PR #{pr_number}",
        ) from exc
    if not isinstance(payload, list):
        raise ReviewLookupError(
            f"Unexpected required-check payload for PR #{pr_number}",
        )
    return [item for item in payload if isinstance(item, dict)]


def load_review_result_for_pr(
    repo_root: Path,
    pr_number: int,
    *,
    run_subprocess: RunSubprocess = default_run_subprocess,
) -> tuple[ReviewGateStatus, ReviewResult]:
    pr_payload = _load_pr_metadata(
        repo_root,
        pr_number,
        run_subprocess=run_subprocess,
    )
    head_sha = str(pr_payload.get("headRefOid", "")).strip()
    base_sha = str(pr_payload.get("baseRefOid", "")).strip()
    pr_url = str(pr_payload.get("url", "")).strip()
    pr_body = str(pr_payload.get("body", "")).strip()
    if not head_sha:
        raise ReviewLookupError(f"PR #{pr_number} is missing headRefOid")

    required_checks = _load_required_checks(
        repo_root,
        pr_number,
        run_subprocess=run_subprocess,
    )
    gate_check = next(
        (item for item in required_checks if check_name_matches(str(item.get("name", "")), REVIEW_GATE_CHECK_NAME)),
        None,
    )
    if gate_check is None:
        raise ReviewLookupError(
            f"PR #{pr_number} does not report required check {REVIEW_GATE_CHECK_NAME}",
        )

    check_name = str(gate_check.get("name", REVIEW_GATE_CHECK_NAME)).strip()
    details_url = str(gate_check.get("link", "")).strip()
    check_state = str(gate_check.get("state", "")).strip()
    check_bucket = str(gate_check.get("bucket", "")).strip()

    if pr_body_uses_local_review(pr_body):
        commit_status = find_commit_status_for_sha(
            repo_root,
            head_sha,
            REVIEW_GATE_CHECK_NAME,
            run_subprocess=run_subprocess,
        )
        if commit_status is not None and "__error__" in commit_status:
            raise ReviewLookupError(
                f"Could not query local {REVIEW_GATE_CHECK_NAME} status for PR #{pr_number}: "
                f"{commit_status.get('__error__', 'unknown error')}",
                details_url=details_url,
            )
        if isinstance(commit_status, dict):
            state = str(commit_status.get("state", "")).strip().lower()
            if state:
                check_state = state.upper()
                check_bucket = "pass" if state == "success" else "pending" if state == "pending" else "fail"
            details_url = str(commit_status.get("target_url", details_url)).strip()
            try:
                review_result = extract_review_result_from_commit_status(
                    commit_status,
                    repo_root=repo_root,
                    pr_number=pr_number,
                    expected_head_sha=head_sha,
                    expected_base_sha=base_sha,
                    require_payload=state not in {"success", "pending"},
                    run_subprocess=run_subprocess,
                )
            except ValueError as exc:
                raise ReviewLookupError(
                    f"Could not retrieve full local review result for PR #{pr_number}: {exc}",
                    details_url=details_url,
                ) from exc

            status = ReviewGateStatus(
                pr_number=pr_number,
                pr_url=pr_url,
                head_sha=head_sha,
                base_sha=base_sha,
                check_name=REVIEW_GATE_CHECK_NAME,
                check_state=check_state,
                check_bucket=check_bucket,
                details_url=details_url,
                run_id=extract_run_id_from_check_url(details_url),
            )
            return status, review_result

    check_run = find_check_run_for_sha(
        repo_root,
        head_sha,
        REVIEW_GATE_CHECK_NAME,
        run_subprocess=run_subprocess,
    )
    if check_run is None:
        raise ReviewLookupError(
            f"Could not find {REVIEW_GATE_CHECK_NAME} check run for PR #{pr_number} head {short_sha(head_sha)}",
            details_url=details_url,
            run_id=extract_run_id_from_check_url(details_url),
        )
    if "__error__" in check_run:
        raise ReviewLookupError(
            f"Could not query {REVIEW_GATE_CHECK_NAME} check run for PR #{pr_number}: "
            f"{check_run.get('__error__', 'unknown error')}",
            details_url=details_url,
            run_id=extract_run_id_from_check_url(details_url),
        )

    check_run = dict(check_run)
    if not str(check_run.get("details_url", "")).strip() and details_url:
        check_run["details_url"] = details_url
    details_url = str(check_run.get("details_url", details_url)).strip()
    run_id = extract_run_id_from_check_url(details_url)
    conclusion = str(check_run.get("conclusion", "")).strip().lower()
    require_payload = conclusion != "success"

    try:
        review_result = extract_review_result_from_check_run(
            check_run,
            repo_root=repo_root,
            expected_head_sha=head_sha,
            expected_base_sha=base_sha,
            require_payload=require_payload,
            run_subprocess=run_subprocess,
        )
    except ValueError as exc:
        raise ReviewLookupError(
            f"Could not retrieve full review result for PR #{pr_number}: {exc}",
            details_url=details_url,
            run_id=run_id,
        ) from exc

    status = ReviewGateStatus(
        pr_number=pr_number,
        pr_url=pr_url,
        head_sha=head_sha,
        base_sha=base_sha,
        check_name=check_name,
        check_state=check_state,
        check_bucket=check_bucket,
        details_url=details_url,
        run_id=run_id,
    )
    return status, review_result


def _render_finding(finding: ReviewFinding) -> str:
    location = f"{finding.file}:{finding.start_line}-{finding.end_line}" if finding.file else "unknown location"
    prefix = f"- [{finding.severity or 'P2'}] {finding.title or finding.id} | {location}"
    if finding.body:
        return f"{prefix} | {finding.body}"
    return prefix


def render_review_report(status: ReviewGateStatus, result: ReviewResult) -> str:
    lines = [
        f"PR #{status.pr_number} review feedback",
        "",
        "Discovery:",
        f"- Status discovery: gh pr checks {status.pr_number} --required --json name,state,bucket,link",
        f"- Job summary: {status.details_url or 'open the review gate Details URL from gh pr checks'}",
        f"- Full machine-readable findings: spec review --pr {status.pr_number}",
        "",
        f"PR URL: {status.pr_url or 'n/a'}",
        f"Head SHA: {status.head_sha}",
        f"Gate: {status.check_name} (state={status.check_state or 'unknown'}, "
        f"bucket={status.check_bucket or 'unknown'})",
        f"Details URL: {status.details_url or 'n/a'}",
        f"Run ID: {status.run_id or 'n/a'}",
        f"Artifact: {status.artifact_name}/{status.artifact_file}",
        f"Decision: {result.status}",
        f"Summary: {result.summary}",
        f"Reviewed at: {result.reviewed_at}",
        f"Findings: {len(result.findings)}",
    ]
    if result.findings:
        lines.append("")
        lines.append("Structured findings:")
        lines.extend(_render_finding(finding) for finding in result.findings)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Fetch the full machine-readable review-decision payload for a pull request.")
    )
    parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="Pull request number to inspect.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = resolve_repo_root()
        status, result = load_review_result_for_pr(repo_root, args.pr)
    except (RuntimeError, ReviewLookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, ReviewLookupError):
            print(exc.recovery_message(), file=sys.stderr)
        return 1

    print(render_review_report(status, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
