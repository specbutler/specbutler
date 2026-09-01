#!/usr/bin/env python3
"""Fail-closed audit of retained Windows release-proof evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REVISION_RE = re.compile(r"[0-9a-f]{40}")
CRITERION_RE = re.compile(r"^(\d+)\.\s+(.*)$")


class AuditInputError(ValueError):
    """The manifest or evidence is structurally invalid."""


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: str
    artifact: str | None
    detail: str


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"cannot read JSON {path}: {exc}") from exc


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _spec_criteria(path: Path) -> tuple[str, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuditInputError(f"cannot read spec {path}: {exc}") from exc
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not frontmatter:
        raise AuditInputError(f"spec has no YAML frontmatter: {path}")
    id_match = re.search(r"^id:\s*(\S+)\s*$", frontmatter.group(1), re.MULTILINE)
    if not id_match:
        raise AuditInputError(f"spec has no id: {path}")

    section = re.search(
        r"^## Acceptance Criteria\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        raise AuditInputError(f"spec has no Acceptance Criteria section: {path}")
    criteria: list[str] = []
    current: list[str] | None = None
    expected_number = 1
    for line in section.group(1).splitlines():
        match = CRITERION_RE.match(line)
        if match:
            number = int(match.group(1))
            if number != expected_number:
                raise AuditInputError(
                    f"non-contiguous criterion numbering in {path}: expected {expected_number}, found {number}"
                )
            if current is not None:
                criteria.append(_normalized(" ".join(current)))
            current = [match.group(2)]
            expected_number += 1
        elif current is not None:
            current.append(line)
    if current is not None:
        criteria.append(_normalized(" ".join(current)))
    if not criteria:
        raise AuditInputError(f"spec has no numbered acceptance criteria: {path}")
    return id_match.group(1), criteria


def _validate_manifest(manifest: Any, source_root: Path) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise AuditInputError("manifest schema_version must be 1")
    specs = manifest.get("specs")
    entries = manifest.get("criteria")
    if not isinstance(specs, list) or not specs:
        raise AuditInputError("manifest specs must be a non-empty list")
    if not isinstance(entries, list) or not entries:
        raise AuditInputError("manifest criteria must be a non-empty list")

    expected: dict[str, tuple[str, int, str]] = {}
    declared_spec_ids: set[str] = set()
    for declared in specs:
        if not isinstance(declared, dict):
            raise AuditInputError("each manifest spec must be an object")
        spec_id = declared.get("id")
        relative_path = declared.get("path")
        count = declared.get("criteria_count")
        if not isinstance(spec_id, str) or not isinstance(relative_path, str):
            raise AuditInputError("manifest spec id and path must be strings")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise AuditInputError(f"unsafe spec path: {relative_path}")
        actual_id, actual_criteria = _spec_criteria(source_root / path)
        if actual_id != spec_id:
            raise AuditInputError(f"manifest spec id {spec_id!r} does not match {actual_id!r} in {relative_path}")
        if count != len(actual_criteria):
            raise AuditInputError(f"manifest count for {spec_id} is {count!r}; spec has {len(actual_criteria)}")
        if spec_id in declared_spec_ids:
            raise AuditInputError(f"duplicate manifest spec id: {spec_id}")
        declared_spec_ids.add(spec_id)
        for number, text in enumerate(actual_criteria, start=1):
            expected[f"{spec_id}.{number}"] = (spec_id, number, text)

    actual_ids: set[str] = set()
    assertion_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AuditInputError("each manifest criterion must be an object")
        criterion_id = entry.get("id")
        if not isinstance(criterion_id, str) or criterion_id not in expected:
            raise AuditInputError(f"unknown criterion id: {criterion_id!r}")
        if criterion_id in actual_ids:
            raise AuditInputError(f"duplicate criterion id: {criterion_id}")
        actual_ids.add(criterion_id)
        spec_id, number, text = expected[criterion_id]
        if entry.get("spec_id") != spec_id or entry.get("number") != number:
            raise AuditInputError(f"criterion metadata mismatch: {criterion_id}")
        if _normalized(str(entry.get("text", ""))) != text:
            raise AuditInputError(f"criterion text drift: {criterion_id}")
        checks = entry.get("checks")
        if not isinstance(checks, list) or not checks:
            raise AuditInputError(f"criterion has no concrete checks: {criterion_id}")
        for check in checks:
            if not isinstance(check, dict):
                raise AuditInputError(f"invalid check in {criterion_id}")
            check_id = check.get("id")
            if not isinstance(check_id, str) or not check_id:
                raise AuditInputError(f"check has no id in {criterion_id}")
            if check_id in assertion_ids:
                raise AuditInputError(f"duplicate assertion id: {check_id}")
            assertion_ids.add(check_id)
            operation = check.get("op")
            allowed = {
                "artifact_nonempty",
                "json_equals",
                "json_at_least",
                "json_in",
                "text_contains",
                "text_not_contains",
                "all_other_criteria_passed",
            }
            if operation not in allowed:
                raise AuditInputError(f"unsupported operation {operation!r} in {check_id}")
            if operation != "all_other_criteria_passed":
                artifact = check.get("artifact")
                if not isinstance(artifact, str) or not artifact:
                    raise AuditInputError(f"check has no artifact: {check_id}")
                artifact_path = Path(artifact)
                if artifact_path.is_absolute() or ".." in artifact_path.parts:
                    raise AuditInputError(f"unsafe artifact path in {check_id}: {artifact}")
            if operation.startswith("json_") and not isinstance(check.get("pointer"), str):
                raise AuditInputError(f"JSON check has no pointer: {check_id}")
            if operation not in {"artifact_nonempty", "all_other_criteria_passed"}:
                if "expected" not in check:
                    raise AuditInputError(f"check has no expected value: {check_id}")
            if operation in {"text_contains", "text_not_contains"} and not isinstance(check.get("expected"), str):
                raise AuditInputError(f"text check expected value is not a string: {check_id}")
            if operation == "json_at_least" and (
                not isinstance(check.get("expected"), (int, float)) or isinstance(check.get("expected"), bool)
            ):
                raise AuditInputError(f"minimum is not numeric: {check_id}")
            if operation == "json_in" and not isinstance(check.get("expected"), list):
                raise AuditInputError(f"json_in expected value is not a list: {check_id}")

    missing = set(expected) - actual_ids
    extra = actual_ids - set(expected)
    if missing or extra:
        raise AuditInputError(f"manifest criterion set mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    return entries


def _artifact_path(evidence_root: Path, relative: str) -> Path:
    candidate = (evidence_root / relative).resolve()
    root = evidence_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise AuditInputError(f"artifact escapes evidence root: {relative}")
    return candidate


def _read_text(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeError:
            continue
    raise AuditInputError(f"cannot decode text artifact: {path}")


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise AuditInputError(f"invalid JSON pointer: {pointer}")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _resolve_expected(value: Any, source_revision: str) -> Any:
    return source_revision if value == "${SOURCE_REVISION}" else value


def _source_checkout_checks(
    *, source_root: Path, manifest_path: Path, source_revision: str, manifest: dict[str, Any]
) -> list[CheckResult]:
    try:
        root = source_root.resolve(strict=True)
        manifest_relative = manifest_path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        return [
            CheckResult(
                "global.source-checkout-revision",
                "failed",
                None,
                f"manifest and source root do not identify one checkout: {exc}",
            )
        ]
    contract_paths = [manifest_relative, *[Path(spec["path"]) for spec in manifest["specs"]]]
    try:
        revision_run = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return [
            CheckResult(
                "global.source-checkout-revision",
                "failed",
                None,
                f"cannot execute Git for source checkout: {exc}",
            )
        ]
    actual_revision = revision_run.stdout.strip().lower()
    revision_passed = revision_run.returncode == 0 and actual_revision == source_revision
    revision_result = CheckResult(
        "global.source-checkout-revision",
        "passed" if revision_passed else "failed",
        None,
        "source checkout HEAD matches the proof revision"
        if revision_passed
        else f"source checkout HEAD was {actual_revision or '(unavailable)'}",
    )

    path_args = [path.as_posix() for path in contract_paths]
    tracked_run = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", *path_args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    diff_run = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", *path_args],
        check=False,
    )
    clean = tracked_run.returncode == 0 and diff_run.returncode == 0
    contract_result = CheckResult(
        "global.source-contract-at-revision",
        "passed" if clean else "failed",
        None,
        "manifest and spec contracts are tracked and unchanged at source HEAD"
        if clean
        else "manifest or spec contracts are untracked or differ from source HEAD",
    )
    return [revision_result, contract_result]


def _evaluate_check(check: dict[str, Any], evidence_root: Path, source_revision: str) -> CheckResult:
    check_id = str(check["id"])
    operation = str(check["op"])
    artifact_name = check.get("artifact")
    if operation == "all_other_criteria_passed":
        return CheckResult(check_id, "deferred", None, "evaluated after all evidence checks")

    artifact = _artifact_path(evidence_root, str(artifact_name))
    if not artifact.is_file() or artifact.stat().st_size == 0:
        return CheckResult(check_id, "missing", str(artifact_name), "artifact is missing or empty")
    if operation == "artifact_nonempty":
        return CheckResult(check_id, "passed", str(artifact_name), "artifact exists and is non-empty")

    try:
        if operation in {"text_contains", "text_not_contains"}:
            text = _read_text(artifact)
            expected = str(_resolve_expected(check.get("expected"), source_revision))
            matched = expected in text
            passed = matched if operation == "text_contains" else not matched
            verb = "contains" if operation == "text_contains" else "does not contain"
            return CheckResult(
                check_id,
                "passed" if passed else "failed",
                str(artifact_name),
                f"artifact {verb} the required text" if passed else f"artifact violates {verb} assertion",
            )

        document = json.loads(_read_text(artifact))
        pointer = str(check["pointer"])
        try:
            actual = _json_pointer(document, pointer)
        except KeyError:
            return CheckResult(check_id, "failed", str(artifact_name), f"JSON pointer is absent: {pointer}")
        expected = _resolve_expected(check.get("expected"), source_revision)
        if operation == "json_equals":
            passed = actual == expected
        elif operation == "json_at_least":
            passed = (
                isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and actual >= expected
            )
        elif operation == "json_in":
            passed = isinstance(expected, list) and actual in expected
        else:  # pragma: no cover - manifest validation makes this unreachable
            raise AuditInputError(f"unsupported operation: {operation}")
        return CheckResult(
            check_id,
            "passed" if passed else "failed",
            str(artifact_name),
            f"{pointer} matched" if passed else f"{pointer} was {actual!r}, expected {expected!r}",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return CheckResult(check_id, "failed", str(artifact_name), f"cannot evaluate artifact: {exc}")


def audit(
    *, manifest_path: Path, source_root: Path, evidence_root: Path, source_revision: str
) -> tuple[dict[str, Any], int]:
    if not REVISION_RE.fullmatch(source_revision):
        raise AuditInputError("expected source revision must be an exact lowercase 40-character Git SHA")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    entries = _validate_manifest(manifest, source_root)

    provenance_path = evidence_root / "source-provenance.json"
    provenance_checks = [
        {
            "id": "global.source-revision.configured",
            "op": "json_equals",
            "artifact": "source-provenance.json",
            "pointer": "/configured_revision",
            "expected": "${SOURCE_REVISION}",
        },
        {
            "id": "global.source-revision.staged",
            "op": "json_equals",
            "artifact": "source-provenance.json",
            "pointer": "/staged_revision",
            "expected": "${SOURCE_REVISION}",
        },
    ]
    global_results = [_evaluate_check(check, evidence_root, source_revision) for check in provenance_checks]
    # Every machine-readable result named by the evidence contract must attest
    # the exact source revision.  Keeping this invariant here (instead of relying
    # on each criterion author to remember an extra check) prevents a current
    # provenance file from being combined with stale passing result JSON.
    result_artifacts = sorted(
        {
            str(check["artifact"])
            for entry in entries
            for check in entry["checks"]
            if str(check.get("artifact", "")).endswith(".json")
        }
    )
    global_results.extend(
        _evaluate_check(
            {
                "id": f"global.artifact-revision.{artifact}",
                "op": "json_equals",
                "artifact": artifact,
                "pointer": "/source_revision",
                "expected": "${SOURCE_REVISION}",
            },
            evidence_root,
            source_revision,
        )
        for artifact in result_artifacts
    )
    global_results.extend(
        _source_checkout_checks(
            source_root=source_root,
            manifest_path=manifest_path,
            source_revision=source_revision,
            manifest=manifest,
        )
    )

    criterion_results: list[dict[str, Any]] = []
    provisional: dict[str, str] = {}
    for entry in entries:
        evaluated = [
            _evaluate_check(check, evidence_root, source_revision)
            for check in entry["checks"]
            if check["op"] != "all_other_criteria_passed"
        ]
        statuses = {result.status for result in evaluated}
        if "failed" in statuses:
            status = "failed"
        elif "missing" in statuses:
            status = "unproven"
        else:
            status = "passed"
        provisional[entry["id"]] = status
        criterion_results.append(
            {
                "id": entry["id"],
                "spec_id": entry["spec_id"],
                "number": entry["number"],
                "status": status,
                "text": entry["text"],
                "checks": [result.__dict__ for result in evaluated],
            }
        )

    for entry, result in zip(entries, criterion_results, strict=True):
        deferred = [check for check in entry["checks"] if check["op"] == "all_other_criteria_passed"]
        for check in deferred:
            other_statuses = {
                criterion_id: status for criterion_id, status in provisional.items() if criterion_id != entry["id"]
            }
            passed = all(status == "passed" for status in other_statuses.values())
            check_result = CheckResult(
                str(check["id"]),
                "passed" if passed else "missing",
                None,
                "all other criteria passed" if passed else "one or more other criteria are not passed",
            )
            result["checks"].append(check_result.__dict__)
            if not passed and result["status"] == "passed":
                result["status"] = "unproven"
        provisional[entry["id"]] = result["status"]

    global_passed = all(item.status == "passed" for item in global_results)
    counts = {
        status: sum(result["status"] == status for result in criterion_results)
        for status in ("passed", "unproven", "failed")
    }
    overall_passed = global_passed and counts["passed"] == len(criterion_results)
    report = {
        "schema_version": 1,
        "status": "passed" if overall_passed else "failed",
        "source_revision": source_revision,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_criteria_count": len(entries),
        "counts": counts,
        "global_checks": [result.__dict__ for result in global_results],
        "criteria": criterion_results,
    }
    # Keep this explicit reference so source-provenance is visibly part of the
    # audit contract even when it is missing.
    report["source_provenance_artifact"] = provenance_path.name
    return report, 0 if overall_passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report, exit_code = audit(
            manifest_path=args.manifest,
            source_root=args.source_root,
            evidence_root=args.evidence_root,
            source_revision=args.expected_revision,
        )
    except (AuditInputError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "failed",
            "error": str(exc),
            "source_revision": args.expected_revision,
        }
        exit_code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
