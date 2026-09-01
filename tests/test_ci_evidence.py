from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tools" / "ci_evidence.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        """[project]
name = "evidence-fixture"
version = "0.0.0"
classifiers = ["Operating System :: Microsoft :: Windows"]
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI Evidence Test"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci-evidence@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Create evidence source"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8").strip()
    return source, revision


def _github_env(revision: str, *, job: str, runner_os: str) -> dict[str, str]:
    return {
        **os.environ,
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "specbutler/specbutler",
        "GITHUB_WORKFLOW": "ci",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": job,
        "GITHUB_SHA": revision,
        "GITHUB_EVENT_NAME": "pull_request",
        "RUNNER_OS": runner_os,
        "RUNNER_ARCH": "X64",
    }


def _junit(
    path: Path,
    *,
    names: tuple[str, ...] = ("test_behavior",),
    failure: bool = False,
    skipped_reason: str | None = None,
) -> None:
    cases = []
    for index, name in enumerate(names):
        child = ""
        if failure and index == 0:
            child = '<failure message="failed">trace</failure>'
        elif skipped_reason and index == len(names) - 1:
            child = f'<skipped message="{skipped_reason}" />'
        cases.append(f'<testcase classname="tests" name="{name}">{child}</testcase>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<testsuite tests="{len(cases)}">{"".join(cases)}</testsuite>\n',
        encoding="utf-8",
    )


def _record(
    source: Path,
    revision: str,
    output: Path,
    *,
    kind: str,
    job: str,
    runner_os: str,
    extra: list[str] | None = None,
    env_revision: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "record",
            "--kind",
            kind,
            "--source-root",
            str(source),
            "--output",
            str(output),
            *(extra or []),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_github_env(env_revision or revision, job=job, runner_os=runner_os),
    )


def _complete_fragments(
    tmp_path: Path,
    source: Path,
    revision: str,
    *,
    portable_names: tuple[str, ...] | None = None,
) -> Path:
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    junit = fragments / "suite.xml"
    focused = fragments / "focused.xml"
    cli = fragments / "cli.xml"
    _junit(
        junit,
        names=portable_names
        or (
            "test_lifecycle",
            "test_web",
            "test_autopilot",
            "test_tui",
            "test_windows_docs_state_exact_supported_tier_and_exclusions",
            "test_windows_real_provider_proof_is_separately_marked_and_one_command",
        ),
    )
    _junit(
        focused,
        names=("test_native_process", "test_installed_artifact_cli_matrix"),
        skipped_reason="run once in the Python 3.12 wheel job",
    )
    _junit(cli, names=("test_installed_artifact_cli_matrix",))

    calls = [
        _record(
            source,
            revision,
            fragments / "lint.json",
            kind="lint",
            job="lint",
            runner_os="Linux",
        )
    ]
    for version in ("3.11", "3.12", "3.13"):
        calls.append(
            _record(
                source,
                revision,
                fragments / f"linux-{version}.json",
                kind="linux-test",
                job="test",
                runner_os="Linux",
                extra=["--python-version", version, "--junit", str(junit)],
            )
        )
    calls.append(
        _record(
            source,
            revision,
            fragments / "macos.json",
            kind="macos-test",
            job="macos-test",
            runner_os="macOS",
            extra=["--python-version", "3.12", "--junit", str(junit)],
        )
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "fixture-0.0.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "fixture-0.0.0.tar.gz").write_bytes(b"sdist")
    calls.append(
        _record(
            source,
            revision,
            fragments / "windows-package.json",
            kind="windows-package",
            job="windows-package",
            runner_os="Windows",
            extra=["--dist-dir", str(dist)],
        )
    )
    for version in ("3.11", "3.12", "3.13"):
        extra = [
            "--python-version",
            version,
            "--distribution",
            "wheel",
            "--full-suite",
            "--junit",
            str(junit),
            "--focused-junit",
            str(focused),
        ]
        if version == "3.12":
            extra.extend(["--cli-matrix", "--cli-junit", str(cli)])
        calls.append(
            _record(
                source,
                revision,
                fragments / f"windows-{version}-wheel.json",
                kind="windows-probe",
                job="windows-probe",
                runner_os="Windows",
                extra=extra,
            )
        )
    calls.append(
        _record(
            source,
            revision,
            fragments / "windows-3.12-sdist.json",
            kind="windows-probe",
            job="windows-probe",
            runner_os="Windows",
            extra=["--python-version", "3.12", "--distribution", "sdist"],
        )
    )
    assert all(call.returncode == 0 for call in calls), [call.stderr for call in calls]
    return fragments


def _aggregate(source: Path, revision: str, fragments: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "aggregate",
            "--source-root",
            str(source),
            "--input",
            str(fragments),
            "--output",
            str(output),
            "--expected-revision",
            revision,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_complete_exact_revision_matrix_emits_acceptance_reports(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    fragments = _complete_fragments(tmp_path, source, revision)
    output = tmp_path / "evidence"

    result = _aggregate(source, revision, fragments, output)

    assert result.returncode == 0, result.stderr
    expected = {
        "ci-fragments",
        "cross-platform-lifecycle-result.json",
        "cross-platform-web-result.json",
        "documentation-audit-result.json",
        "hosted-ci-evidence-index.json",
        "hosted-windows-ci-result.json",
        "hosted-windows-smoke-result.json",
        "package-release-result.json",
        "test-coverage-result.json",
    }
    assert {path.name for path in output.iterdir()} == expected
    for path in output.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert payload["source_revision"] == revision
    windows = json.loads((output / "hosted-windows-ci-result.json").read_text())
    assert windows["supported_python_versions_tested"] == 3
    assert windows["windows_integration_skipped_for_server"] is False
    smoke = json.loads((output / "hosted-windows-smoke-result.json").read_text())
    assert smoke["path_has_spaces_and_unicode"] is True
    assert smoke["commands"][-3:] == ["auto-adopt", "auto-stop", "cleanup"]
    coverage = json.loads((output / "test-coverage-result.json").read_text())
    assert coverage["hermetic_lifecycle"] is True
    assert coverage["real_provider_separately_marked"] is True
    documentation = json.loads((output / "documentation-audit-result.json").read_text())
    assert documentation["surfaces"] == ["README", "INSTALL", "troubleshooting", "support-matrix"]


def test_incomplete_matrix_fails_without_partial_reports(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    fragments = _complete_fragments(tmp_path, source, revision)
    (fragments / "windows-3.13-wheel.json").unlink()
    output = tmp_path / "evidence"

    result = _aggregate(source, revision, fragments, output)

    assert result.returncode == 1
    assert "expected 10 CI evidence fragments" in result.stderr
    assert not output.exists()


def test_missing_required_coverage_test_fails_without_reports(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    fragments = _complete_fragments(
        tmp_path,
        source,
        revision,
        portable_names=(
            "test_lifecycle",
            "test_windows_real_provider_proof_is_separately_marked_and_one_command",
        ),
    )
    output = tmp_path / "evidence"

    result = _aggregate(source, revision, fragments, output)

    assert result.returncode == 1
    assert "lacks required tests" in result.stderr
    assert "test_windows_docs_state_exact_supported_tier_and_exclusions" in result.stderr
    assert not output.exists()


def test_failed_junit_or_mismatched_sha_never_writes_fragment(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    failed = tmp_path / "failed.xml"
    _junit(failed, failure=True)
    output = tmp_path / "fragment.json"

    result = _record(
        source,
        revision,
        output,
        kind="linux-test",
        job="test",
        runner_os="Linux",
        extra=["--python-version", "3.12", "--junit", str(failed)],
    )
    assert result.returncode == 1
    assert not output.exists()

    result = _record(
        source,
        revision,
        output,
        kind="lint",
        job="lint",
        runner_os="Linux",
        env_revision="2" * 40,
    )
    assert result.returncode == 1
    assert "does not match tested checkout" in result.stderr
    assert not output.exists()


def test_windows_server_skip_reason_blocks_probe_fragment(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    suite = tmp_path / "suite.xml"
    focused = tmp_path / "focused.xml"
    _junit(suite)
    _junit(focused, skipped_reason="native Windows probe; skipped on Windows Server")
    output = tmp_path / "fragment.json"

    result = _record(
        source,
        revision,
        output,
        kind="windows-probe",
        job="windows-probe",
        runner_os="Windows",
        extra=[
            "--python-version",
            "3.11",
            "--distribution",
            "wheel",
            "--full-suite",
            "--junit",
            str(suite),
            "--focused-junit",
            str(focused),
        ],
    )

    assert result.returncode == 1
    assert "skipped for its platform" in result.stderr
    assert not output.exists()


def test_aggregation_rejects_changed_retained_junit(tmp_path: Path) -> None:
    source, revision = _source_repo(tmp_path)
    fragments = _complete_fragments(tmp_path, source, revision)
    (fragments / "suite.xml").write_text("<testsuite />\n", encoding="utf-8")
    output = tmp_path / "evidence"

    result = _aggregate(source, revision, fragments, output)

    assert result.returncode == 1
    assert "retained JUnit artifact is missing or has changed" in result.stderr
    assert not output.exists()


def test_ci_workflow_aggregates_and_requires_hosted_evidence() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    evidence_job = jobs["ci-evidence"]
    final_job = jobs["ci"]
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "ci-evidence" in final_job["needs"]
    assert "CI_EVIDENCE_RESULT: ${{ needs.ci-evidence.result }}" in source
    assert '[ "$CI_EVIDENCE_RESULT" != "success" ]' in source
    assert "pattern: ci-evidence-*" in source
    assert "if-no-files-found: error" in source
    assert "tools/ci_evidence.py aggregate" in source
    assert "hosted-ci-acceptance-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in source
    for report in (
        "hosted-windows-ci-result.json",
        "hosted-windows-smoke-result.json",
        "package-release-result.json",
        "cross-platform-lifecycle-result.json",
        "cross-platform-web-result.json",
        "test-coverage-result.json",
        "documentation-audit-result.json",
    ):
        assert report in HELPER.read_text(encoding="utf-8")
    assert evidence_job["needs"] == [
        "skip-check",
        "lint",
        "test",
        "macos-test",
        "windows-package",
        "windows-probe",
    ]
