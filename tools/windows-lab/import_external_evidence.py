#!/usr/bin/env python3
"""Import independently produced exact-revision evidence into a VM proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

HOSTED_RESULTS = (
    "cross-platform-lifecycle-result.json",
    "cross-platform-web-result.json",
    "hosted-windows-ci-result.json",
    "hosted-windows-smoke-result.json",
)
REQUIRED_RESULTS = (*HOSTED_RESULTS, "linux-claude-web-result.json")
HOSTED_INDEX = "hosted-ci-evidence-index.json"
SECRET_VALUE_RE = re.compile(r"(?:github_pat_|gh[opsu]_|sk-)[A-Za-z0-9_-]{20,}")


class EvidenceImportError(ValueError):
    """The external evidence bundle is incomplete or internally inconsistent."""


def _revision(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise EvidenceImportError(f"{label} must be an exact 40-character Git SHA")
    return normalized


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceImportError(f"cannot read external evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceImportError(f"external evidence is not a JSON object: {path}")
    return payload


def _reject_secret_shapes(payload: Any, *, name: str, key: str = "") -> None:
    if isinstance(payload, dict):
        for child_key, child in payload.items():
            _reject_secret_shapes(child, name=name, key=str(child_key))
    elif isinstance(payload, list):
        for child in payload:
            _reject_secret_shapes(child, name=name, key=key)
    elif isinstance(payload, str):
        normalized_key = key.casefold().replace("-", "_")
        credential_key = any(marker in normalized_key for marker in ("api_key", "token", "secret"))
        if SECRET_VALUE_RE.search(payload) or (credential_key and len(payload) >= 16):
            raise EvidenceImportError(f"{name} contains a recognized credential shape in field {key!r}")


def _unique_file(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file()]
    if len(matches) != 1:
        raise EvidenceImportError(f"expected exactly one {name} under {root}, found {len(matches)}")
    path = matches[0].resolve()
    if path != root and root not in path.parents:
        raise EvidenceImportError(f"external evidence escapes its root: {matches[0]}")
    return path


def _github_run(payload: dict[str, Any], *, name: str, revision: str) -> tuple[Any, ...]:
    run = payload.get("github_run")
    if not isinstance(run, dict):
        raise EvidenceImportError(f"{name} has no hosted GitHub run identity")
    identity = tuple(
        run.get(key)
        for key in ("repository", "workflow", "run_id", "run_attempt", "event_name", "github_sha")
    )
    if any(value in (None, "") for value in identity) or identity[-1] != revision:
        raise EvidenceImportError(f"{name} has an incomplete or mismatched GitHub run identity")
    return identity


def import_evidence(source: Path, destination: Path, revision: str) -> dict[str, Any]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    revision = _revision(revision, label="source revision")
    if not source.is_dir():
        raise EvidenceImportError(f"external evidence root is not a directory: {source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise EvidenceImportError("external evidence root and destination must be separate")
    destination.mkdir(parents=True, exist_ok=True)

    redaction_path = destination / "_redaction-report.json"
    redaction = _load_object(redaction_path)
    if (
        redaction.get("status") != "passed"
        or redaction.get("source_revision") != revision
        or redaction.get("recognized_secret_shapes_remaining") != []
    ):
        raise EvidenceImportError("local redaction report is absent, failed, or belongs to another revision")

    index_path = _unique_file(source, HOSTED_INDEX)
    index = _load_object(index_path)
    _reject_secret_shapes(index, name=HOSTED_INDEX)
    if index.get("status") != "passed" or index.get("source_revision") != revision:
        raise EvidenceImportError("hosted CI evidence index does not attest this exact revision")
    declared_reports = index.get("reports")
    if not isinstance(declared_reports, list) or not set(HOSTED_RESULTS) <= set(declared_reports):
        raise EvidenceImportError("hosted CI evidence index does not declare every required hosted result")
    index_run = _github_run(index, name=HOSTED_INDEX, revision=revision)

    selected: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for name in REQUIRED_RESULTS:
        path = _unique_file(source, name)
        payload = _load_object(path)
        _reject_secret_shapes(payload, name=name)
        if payload.get("status") != "passed" or payload.get("source_revision") != revision:
            raise EvidenceImportError(f"{name} does not attest success at {revision}")
        if name in HOSTED_RESULTS and _github_run(payload, name=name, revision=revision) != index_run:
            raise EvidenceImportError(f"{name} came from a different hosted CI run")
        target = destination / name
        if target.exists():
            raise EvidenceImportError(f"refusing to overwrite existing local evidence: {target}")
        selected[name] = path
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix=".external-evidence-", dir=destination) as staging_name:
        staging = Path(staging_name)
        for name, path in selected.items():
            shutil.copy2(path, staging / name)
        for name in REQUIRED_RESULTS:
            (staging / name).replace(destination / name)

    report = {
        "schema_version": 1,
        "status": "passed",
        "source_revision": revision,
        "hosted_github_run": {
            key: value
            for key, value in zip(
                ("repository", "workflow", "run_id", "run_attempt", "event_name", "github_sha"),
                index_run,
                strict=True,
            )
        },
        "imported_sha256": digests,
    }
    report_path = destination / "external-evidence-import.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    redaction["files_processed"] = int(redaction.get("files_processed", 0)) + len(REQUIRED_RESULTS)
    redaction["text_files_scanned"] = int(redaction.get("text_files_scanned", 0)) + len(REQUIRED_RESULTS)
    redaction["external_results_scanned"] = list(REQUIRED_RESULTS)
    redaction_path.write_text(json.dumps(redaction, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args(argv)
    try:
        report = import_evidence(args.source, args.destination, args.expected_revision)
    except (EvidenceImportError, OSError) as exc:
        print(f"external evidence import failed: {exc}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
