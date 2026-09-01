from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB_ROOT = REPO_ROOT / "tools" / "windows-lab"
AUDITOR = LAB_ROOT / "audit_acceptance.py"
MANIFEST = LAB_ROOT / "acceptance-manifest.json"
REVISION = "1" * 40
CURRENT_REVISION = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, encoding="utf-8"
).strip()


def _run_audit(
    tmp_path: Path,
    *,
    manifest: Path = MANIFEST,
    source_root: Path = REPO_ROOT,
    evidence_root: Path | None = None,
    revision: str = CURRENT_REVISION,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    evidence = evidence_root or (tmp_path / "evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "acceptance-audit.json"
    result = subprocess.run(
        [
            sys.executable,
            str(AUDITOR),
            "--manifest",
            str(manifest),
            "--source-root",
            str(source_root),
            "--evidence-root",
            str(evidence),
            "--expected-revision",
            revision,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result, json.loads(output.read_text(encoding="utf-8"))


def _write_minimal_contract(tmp_path: Path, *, artifact: str = "evidence.json") -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    specs = source / "specs"
    specs.mkdir(parents=True)
    (specs / "proof.md").write_text(
        """---
id: proof
---

# Proof

## Acceptance Criteria

1. Exact retained evidence proves the behavior.
""",
        encoding="utf-8",
    )
    manifest = source / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "specs": [{"id": "proof", "path": "specs/proof.md", "criteria_count": 1}],
                "criteria": [
                    {
                        "id": "proof.1",
                        "spec_id": "proof",
                        "number": 1,
                        "text": "Exact retained evidence proves the behavior.",
                        "checks": [
                            {
                                "id": "proof.1.status",
                                "op": "json_equals",
                                "artifact": artifact,
                                "pointer": "/status",
                                "expected": "passed",
                            },
                            {
                                "id": "proof.1.revision",
                                "op": "json_equals",
                                "artifact": artifact,
                                "pointer": "/source_revision",
                                "expected": "${SOURCE_REVISION}",
                            },
                        ],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Audit Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "audit@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Create evidence contract"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8").strip()
    return source, manifest, revision


def _write_provenance(evidence: Path, *, configured: str, staged: str) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "source-provenance.json").write_text(
        json.dumps({"configured_revision": configured, "staged_revision": staged}) + "\n",
        encoding="utf-8",
    )


def test_production_manifest_exactly_tracks_all_windows_spec_criteria(tmp_path: Path) -> None:
    result, audit = _run_audit(tmp_path)

    assert result.returncode == 1
    assert audit["status"] == "failed"
    assert audit["manifest_criteria_count"] == 26
    assert audit["counts"] == {"failed": 0, "passed": 0, "unproven": 26}
    assert [entry["id"] for entry in audit["criteria"]] == [
        *[f"windows-native-lifecycle.{number}" for number in range(1, 11)],
        *[f"windows-web-autopilot.{number}" for number in range(1, 9)],
        *[f"windows-ci-e2e-release.{number}" for number in range(1, 9)],
    ]


def test_audit_passes_only_with_exact_revision_and_satisfied_artifact(tmp_path: Path) -> None:
    source, manifest, revision = _write_minimal_contract(tmp_path)
    evidence = tmp_path / "evidence"
    _write_provenance(evidence, configured=revision, staged=revision)
    (evidence / "evidence.json").write_text(
        json.dumps({"status": "passed", "source_revision": revision}) + "\n",
        encoding="utf-8",
    )

    result, audit = _run_audit(
        tmp_path,
        manifest=manifest,
        source_root=source,
        evidence_root=evidence,
        revision=revision,
    )

    assert result.returncode == 0
    assert audit["status"] == "passed"
    assert audit["counts"] == {"failed": 0, "passed": 1, "unproven": 0}
    assert {check["status"] for check in audit["global_checks"]} == {"passed"}


def test_missing_evidence_is_unproven_and_blocks_release(tmp_path: Path) -> None:
    source, manifest, revision = _write_minimal_contract(tmp_path)
    evidence = tmp_path / "evidence"
    _write_provenance(evidence, configured=revision, staged=revision)

    result, audit = _run_audit(
        tmp_path,
        manifest=manifest,
        source_root=source,
        evidence_root=evidence,
        revision=revision,
    )

    assert result.returncode == 1
    assert audit["counts"] == {"failed": 0, "passed": 0, "unproven": 1}
    assert {check["status"] for check in audit["criteria"][0]["checks"]} == {"missing"}


def test_contradictory_evidence_and_revision_mismatch_fail(tmp_path: Path) -> None:
    source, manifest, revision = _write_minimal_contract(tmp_path)
    evidence = tmp_path / "evidence"
    _write_provenance(evidence, configured=revision, staged="2" * 40)
    (evidence / "evidence.json").write_text(
        json.dumps({"status": "failed", "source_revision": "3" * 40}) + "\n",
        encoding="utf-8",
    )

    result, audit = _run_audit(
        tmp_path,
        manifest=manifest,
        source_root=source,
        evidence_root=evidence,
        revision=revision,
    )

    assert result.returncode == 1
    assert audit["status"] == "failed"
    assert audit["counts"] == {"failed": 1, "passed": 0, "unproven": 0}
    assert any(check["status"] == "failed" for check in audit["global_checks"])


def test_manifest_drift_and_unsafe_artifact_paths_are_schema_errors(tmp_path: Path) -> None:
    source, manifest, revision = _write_minimal_contract(tmp_path, artifact="../outside.json")

    result, audit = _run_audit(tmp_path, manifest=manifest, source_root=source, revision=revision)

    assert result.returncode == 2
    assert audit["status"] == "failed"
    assert "unsafe artifact path" in audit["error"]

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["criteria"][0]["checks"][0]["artifact"] = "evidence.json"
    payload["criteria"][0]["checks"][1]["artifact"] = "evidence.json"
    payload["criteria"][0]["text"] = "Stale criterion text."
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result, audit = _run_audit(tmp_path, manifest=manifest, source_root=source, revision=revision)
    assert result.returncode == 2
    assert "criterion text drift" in audit["error"]


def test_production_final_audit_criterion_cannot_be_self_asserted() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    final = manifest["criteria"][-1]

    assert final["id"] == "windows-ci-e2e-release.8"
    assert final["checks"] == [{"id": "release.8.all-other-criteria", "op": "all_other_criteria_passed"}]


def test_all_other_criteria_check_is_derived_from_sibling_results(tmp_path: Path) -> None:
    source, manifest, _ = _write_minimal_contract(tmp_path)
    spec_path = source / "specs" / "proof.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace(
            "1. Exact retained evidence proves the behavior.\n",
            "1. Exact retained evidence proves the behavior.\n"
            "2. The final audit is derived from every other criterion.\n",
        ),
        encoding="utf-8",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["specs"][0]["criteria_count"] = 2
    payload["criteria"].append(
        {
            "id": "proof.2",
            "spec_id": "proof",
            "number": 2,
            "text": "The final audit is derived from every other criterion.",
            "checks": [{"id": "proof.2.derived", "op": "all_other_criteria_passed"}],
        }
    )
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add derived final audit"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8").strip()
    evidence = tmp_path / "evidence"
    _write_provenance(evidence, configured=revision, staged=revision)
    artifact = evidence / "evidence.json"
    artifact.write_text(
        json.dumps({"status": "passed", "source_revision": revision}) + "\n",
        encoding="utf-8",
    )

    result, audit = _run_audit(
        tmp_path,
        manifest=manifest,
        source_root=source,
        evidence_root=evidence,
        revision=revision,
    )
    assert result.returncode == 0
    assert [criterion["status"] for criterion in audit["criteria"]] == [
        "passed",
        "passed",
    ]

    artifact.unlink()
    result, audit = _run_audit(
        tmp_path,
        manifest=manifest,
        source_root=source,
        evidence_root=evidence,
        revision=revision,
    )
    assert result.returncode == 1
    assert [criterion["status"] for criterion in audit["criteria"]] == [
        "unproven",
        "unproven",
    ]


def test_proof_records_exact_provenance_and_does_not_claim_release_passed() -> None:
    proof = (LAB_ROOT / "proof.ps1").read_text(encoding="utf-8")
    controller = (LAB_ROOT / "labctl").read_text(encoding="utf-8")

    assert "source-provenance.json" in proof
    assert "'evidence-collected'" in proof
    assert "'requires-fail-closed-audit'" in proof
    assert "acceptance-manifest.json" in proof
    assert "review-bootstrap-warning.json" in proof
    assert "--system-site-packages" in proof
    assert "--no-build-isolation --no-deps" in proof
    assert "install_command_windows = '" in proof
    assert 'install_shell_windows = "powershell"' in proof
    assert 'argv_windows = [".venv/Scripts/python.exe", "-m", "pytest", "-q"]' in proof
    assert "local_acceptance.py" in proof
    assert "installed-cli-matrix.junit.xml" in proof
    assert "'--wheel', '--sdist'" in proof
    assert "lab-controller-static-result.json" in controller
    assert "acceptance audit will report it unproven" in controller
