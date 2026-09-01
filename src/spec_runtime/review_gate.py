"""Evaluate the review-decision gate from agent JSON output."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_DECISIONS = ("approved", "request_changes", "blocked", "failed")
DEFAULT_CHECK_NAME = "review-decision-gate"


@dataclass
class GateEvaluation:
    result_payload: dict[str, Any]
    machine_summary: dict[str, Any]
    human_summary: str
    exit_code: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)


def _format_path(path: str, key: str) -> str:
    if path == "$":
        return f"$.{key}"
    return f"{path}.{key}"


def _validate_against_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> str | None:
    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(instance, dict):
            return f"{path}: expected object"

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return f"{path}: schema properties must be an object"

        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in instance:
                    return f"{path}: missing required property '{key}'"

        additional_properties = schema.get("additionalProperties", True)
        if additional_properties is False:
            extras = [k for k in instance if k not in properties]
            if extras:
                extras_csv = ", ".join(sorted(extras))
                return f"{path}: additional properties not allowed: {extras_csv}"

        for key, value in instance.items():
            prop_schema = properties.get(key)
            if isinstance(prop_schema, dict):
                error = _validate_against_schema(value, prop_schema, _format_path(path, key))
                if error is not None:
                    return error

    elif schema_type == "array":
        if not isinstance(instance, list):
            return f"{path}: expected array"
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, item in enumerate(instance):
                error = _validate_against_schema(item, items_schema, f"{path}[{idx}]")
                if error is not None:
                    return error

    elif schema_type == "string":
        if not isinstance(instance, str):
            return f"{path}: expected string"
        min_length = schema.get("minLength")
        if _is_integer(min_length) and len(instance) < int(min_length):
            return f"{path}: string shorter than minLength={min_length}"

    elif schema_type == "integer":
        if not _is_integer(instance):
            return f"{path}: expected integer"
        minimum = schema.get("minimum")
        if _is_number(minimum) and instance < float(minimum):
            return f"{path}: value below minimum={minimum}"
        maximum = schema.get("maximum")
        if _is_number(maximum) and instance > float(maximum):
            return f"{path}: value above maximum={maximum}"

    elif schema_type == "number":
        if not _is_number(instance):
            return f"{path}: expected number"
        minimum = schema.get("minimum")
        if _is_number(minimum) and float(instance) < float(minimum):
            return f"{path}: value below minimum={minimum}"
        maximum = schema.get("maximum")
        if _is_number(maximum) and float(instance) > float(maximum):
            return f"{path}: value above maximum={maximum}"

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and instance not in enum_values:
        allowed = ", ".join(repr(v) for v in enum_values)
        return f"{path}: value {instance!r} not in enum [{allowed}]"

    return None


def _render_human_summary(result_payload: dict[str, Any], machine_summary: dict[str, Any]) -> str:
    summary_lines = [
        "## review-decision-gate",
        f"- Decision: `{machine_summary['decision']}`",
        f"- Conclusion: `{machine_summary['conclusion']}`",
        f"- Reviewed Head SHA: `{machine_summary['reviewed_head_sha']}`",
        f"- Expected Head SHA: `{machine_summary['expected_head_sha']}`",
        f"- Findings: `{machine_summary['findings_count']}`",
        f"- Summary: {machine_summary['summary']}",
    ]

    source_url = str(result_payload.get("source_check_url", "")).strip()
    if source_url:
        summary_lines.append(f"- Source Check: {source_url}")

    findings = result_payload.get("findings", [])
    if isinstance(findings, list) and findings:
        summary_lines.append("")
        summary_lines.append("### Key Findings")
        for finding in findings[:5]:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity", "P2")).strip() or "P2"
            title = str(finding.get("title", finding.get("id", "finding"))).strip()
            file_path = str(finding.get("file", "")).strip()
            start_line = finding.get("start_line", "")
            end_line = finding.get("end_line", "")
            if file_path and start_line and end_line:
                location = f"{file_path}:{start_line}-{end_line}"
            elif file_path and start_line:
                location = f"{file_path}:{start_line}"
            elif file_path:
                location = file_path
            else:
                location = "unknown location"
            body = str(finding.get("body", "")).strip()
            summary_lines.append(f"- [{severity}] {title} ({location})")
            if body:
                summary_lines.append(f"  {body}")

    return "\n".join(summary_lines)


def _build_failed_evaluation(
    *,
    summary: str,
    expected_head_sha: str,
    expected_base_sha: str,
    check_name: str,
    check_url: str,
    reviewed_head_sha: str = "",
    reviewed_base_sha: str = "",
    schema_version: str = "v1",
) -> GateEvaluation:
    normalized_head_sha = reviewed_head_sha or expected_head_sha
    normalized_base_sha = reviewed_base_sha or expected_base_sha
    result_payload = {
        "schema_version": schema_version,
        "status": "failed",
        "decision": "failed",
        "summary": summary,
        "reviewed_base_sha": normalized_base_sha,
        "reviewed_head_sha": normalized_head_sha,
        "findings": [],
        "reviewer_role": "independent-review",
        "reviewer_agent": "codex",
        "source_check_name": check_name,
        "source_check_url": check_url,
        "reviewed_at": _utc_now_iso(),
    }
    machine_summary = {
        "check_name": check_name,
        "decision": "failed",
        "conclusion": "failure",
        "reviewed_head_sha": normalized_head_sha,
        "expected_head_sha": expected_head_sha,
        "findings_count": 0,
        "summary": summary,
    }
    human_summary = _render_human_summary(result_payload, machine_summary)
    return GateEvaluation(
        result_payload=result_payload,
        machine_summary=machine_summary,
        human_summary=human_summary,
        exit_code=1,
    )


def evaluate_review_gate(
    *,
    input_path: Path,
    schema_path: Path,
    expected_head_sha: str,
    expected_base_sha: str,
    check_name: str = DEFAULT_CHECK_NAME,
    check_url: str = "",
) -> GateEvaluation:
    expected_head_sha = expected_head_sha.strip()
    expected_base_sha = expected_base_sha.strip()

    if not expected_head_sha:
        return _build_failed_evaluation(
            summary="Missing expected head SHA for review gate evaluation.",
            expected_head_sha="",
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    if not schema_path.exists():
        return _build_failed_evaluation(
            summary=f"Schema file is missing: {schema_path}",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    try:
        schema_payload = json.loads(schema_path.read_text())
    except json.JSONDecodeError as exc:
        return _build_failed_evaluation(
            summary=(f"Could not parse review schema JSON: {schema_path} (line {exc.lineno}, column {exc.colno})"),
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    if not isinstance(schema_payload, dict):
        return _build_failed_evaluation(
            summary=f"Schema file must contain a JSON object: {schema_path}",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    if not input_path.exists():
        return _build_failed_evaluation(
            summary=f"Missing review output artifact: {input_path}",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    try:
        raw_text = input_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _build_failed_evaluation(
            summary=f"Could not read review output artifact: {input_path} ({exc})",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    try:
        review_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return _build_failed_evaluation(
            summary=(f"Malformed JSON review output: {input_path} (line {exc.lineno}, column {exc.colno})"),
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    if not isinstance(review_payload, dict):
        return _build_failed_evaluation(
            summary="Review output must be a JSON object.",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
        )

    schema_error = _validate_against_schema(review_payload, schema_payload)
    if schema_error is not None:
        return _build_failed_evaluation(
            summary=f"Schema validation failed: {schema_error}",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
            reviewed_head_sha=str(review_payload.get("reviewed_head_sha", "")).strip(),
            reviewed_base_sha=str(review_payload.get("reviewed_base_sha", "")).strip(),
            schema_version=str(review_payload.get("schema_version", "v1")).strip() or "v1",
        )

    decision = str(review_payload.get("decision", "")).strip().lower()
    reviewed_head_sha = str(review_payload.get("reviewed_head_sha", "")).strip()
    reviewed_base_sha = str(review_payload.get("reviewed_base_sha", "")).strip()
    schema_version = str(review_payload.get("schema_version", "v1")).strip() or "v1"

    if decision not in VALID_DECISIONS:
        return _build_failed_evaluation(
            summary=f"Decision is invalid: {decision!r}",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
            reviewed_head_sha=reviewed_head_sha,
            reviewed_base_sha=reviewed_base_sha,
            schema_version=schema_version,
        )

    if reviewed_head_sha != expected_head_sha:
        return _build_failed_evaluation(
            summary=(f"Head SHA mismatch: expected {expected_head_sha}, got {reviewed_head_sha or '(empty)'}"),
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
            reviewed_head_sha=reviewed_head_sha,
            reviewed_base_sha=reviewed_base_sha,
            schema_version=schema_version,
        )

    if expected_base_sha and reviewed_base_sha and reviewed_base_sha != expected_base_sha:
        return _build_failed_evaluation(
            summary=(f"Base SHA mismatch: expected {expected_base_sha}, got {reviewed_base_sha}"),
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            check_name=check_name,
            check_url=check_url,
            reviewed_head_sha=reviewed_head_sha,
            reviewed_base_sha=reviewed_base_sha,
            schema_version=schema_version,
        )

    findings = review_payload.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    summary = str(review_payload.get("summary", "")).strip()
    if not summary:
        summary = f"Review decision: {decision}"

    result_payload = {
        "schema_version": schema_version,
        "status": decision,
        "decision": decision,
        "summary": summary,
        "reviewed_base_sha": reviewed_base_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "findings": findings,
        "reviewer_role": str(review_payload.get("reviewer_role", "independent-review")).strip() or "independent-review",
        "reviewer_agent": str(review_payload.get("reviewer_agent", "codex")).strip() or "codex",
        "source_check_name": check_name,
        "source_check_url": check_url,
        "reviewed_at": str(review_payload.get("reviewed_at", _utc_now_iso())).strip() or _utc_now_iso(),
    }

    gate_passed = decision == "approved"
    machine_summary = {
        "check_name": check_name,
        "decision": decision,
        "conclusion": "success" if gate_passed else "failure",
        "reviewed_head_sha": reviewed_head_sha,
        "expected_head_sha": expected_head_sha,
        "findings_count": len(findings),
        "summary": summary,
    }
    human_summary = _render_human_summary(result_payload, machine_summary)
    return GateEvaluation(
        result_payload=result_payload,
        machine_summary=machine_summary,
        human_summary=human_summary,
        exit_code=0 if gate_passed else 1,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_decision_gate",
        description="Validate Codex review JSON and evaluate review-decision-gate.",
    )
    parser.add_argument("--input", required=True, help="Path to raw Codex review JSON")
    parser.add_argument("--schema", required=True, help="Path to review JSON schema file")
    parser.add_argument("--expected-head-sha", required=True, help="Expected PR head SHA")
    parser.add_argument("--expected-base-sha", default="", help="Expected PR base SHA")
    parser.add_argument("--check-name", default=DEFAULT_CHECK_NAME, help="Check name to record")
    parser.add_argument("--check-url", default="", help="URL for the source check")
    parser.add_argument("--output", required=True, help="Path to write normalized review JSON")
    parser.add_argument(
        "--summary-output",
        required=True,
        help="Path to write machine-readable gate summary JSON",
    )
    parser.add_argument(
        "--job-summary-output",
        required=True,
        help="Path to write markdown summary for GitHub job summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    evaluation = evaluate_review_gate(
        input_path=Path(args.input),
        schema_path=Path(args.schema),
        expected_head_sha=str(args.expected_head_sha),
        expected_base_sha=str(args.expected_base_sha),
        check_name=str(args.check_name),
        check_url=str(args.check_url),
    )

    _write_json(Path(args.output), evaluation.result_payload)
    _write_json(Path(args.summary_output), evaluation.machine_summary)
    _write_text(Path(args.job_summary_output), evaluation.human_summary)
    print(json.dumps(evaluation.machine_summary, sort_keys=True))
    return evaluation.exit_code


if __name__ == "__main__":
    sys.exit(main())
