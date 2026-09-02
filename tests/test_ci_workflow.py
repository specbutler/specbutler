from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
REPO_ROOT = WORKFLOWS_DIR.parent.parent
WORKFLOW_PATH = WORKFLOWS_DIR / "ci.yml"
GITLEAKS_CONFIG_PATH = REPO_ROOT / ".gitleaks.toml"


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def _workflow_jobs() -> dict:
    payload = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict)
    return jobs


def test_workflows_default_to_read_only_permissions():
    for workflow_name in ("ci.yml", "codex-review.yml", "spec-pr-policy.yml", "version.yml"):
        assert _load_workflow(workflow_name)["permissions"] == "read-all"


def test_version_workflow_creates_idempotent_github_release_for_tag():
    workflow = _load_workflow("version.yml")
    job = workflow["jobs"]["tag-release"]
    release_step = next(
        step for step in job["steps"] if step.get("name") == "Create tag for merged version bump"
    )
    script = "\n".join(
        str(step.get("run", ""))
        for step in job["steps"]
        if isinstance(step, dict)
    )

    assert job["permissions"]["contents"] == "write"
    assert release_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gh release view" in script
    assert "gh release create" in script
    assert "--verify-tag" in script
    assert "--generate-notes" in script
    assert 'if [ -z "$parent_version" ] || [ "$version" = "$parent_version" ]' in script
    assert "Packaged version did not change" in script


def test_version_bump_is_dormant_until_release_credentials_are_enabled():
    workflow = _load_workflow("version.yml")
    condition = str(workflow["jobs"]["bump-pr"]["if"])

    assert "vars.SPEC_BUTLER_AUTOMATED_RELEASES == 'true'" in condition


def test_version_workflow_uses_rest_for_release_pr_lifecycle():
    workflow = _load_workflow("version.yml")
    bump_job = workflow["jobs"]["bump-pr"]
    script = "\n".join(
        str(step.get("run", ""))
        for step in bump_job["steps"]
        if isinstance(step, dict)
    )

    assert 'gh_api_retry -X GET "repos/${repo}/pulls"' in script
    assert 'gh api -X POST "repos/${repo}/pulls"' in script
    assert 'gh_api_retry -X PATCH "repos/${repo}/pulls/${existing_pr_number}"' in script
    assert 'gh api -X PUT "repos/${repo}/pulls/${pr_number}/merge"' in script
    assert "gh_api_retry()" in script
    assert 'existing_pr_number="$(find_open_release_pr)"' in script
    assert "checking for a created PR" in script
    assert 'gh_api_retry "repos/${repo}/pulls/${pr_number}"' in script
    assert "merge_method=rebase" in script
    assert "waiting for required checks" in script
    assert "gh pr list" not in script
    assert "gh pr view" not in script
    assert "gh pr create" not in script
    assert "gh pr edit" not in script
    assert "gh pr merge" not in script


def test_version_workflow_reconciles_release_creation_before_retry():
    workflow = _load_workflow("version.yml")
    jobs = workflow["jobs"]
    bump_script = str(
        next(
            step["run"]
            for step in jobs["bump-pr"]["steps"]
            if step.get("name") == "Create or update release PR"
        )
    )
    tag_script = str(
        next(
            step["run"]
            for step in jobs["tag-release"]["steps"]
            if step.get("name") == "Create tag for merged version bump"
        )
    )

    post_index = bump_script.index('gh api -X POST "repos/${repo}/pulls"')
    reconcile_index = bump_script.index('pr_number="$(find_open_release_pr)"', post_index)
    retry_sleep_index = bump_script.index('sleep "$((attempt * 5))"', reconcile_index)
    assert post_index < reconcile_index < retry_sleep_index
    assert "for attempt in $(seq 1 6)" in tag_script
    assert 'gh release view "$tag_name"' in tag_script
    assert 'gh release create "$tag_name"' in tag_script


def test_spec_pr_policy_executes_trusted_base_validator_only():
    workflow = _load_workflow("spec-pr-policy.yml")
    steps = workflow["jobs"]["spec-pr-policy"]["steps"]

    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["path"] == "trusted"
    assert checkout["with"]["persist-credentials"] is False

    fetch = next(step for step in steps if step.get("name") == "Fetch untrusted PR head as data")
    assert "git -C trusted fetch" in fetch["run"]
    assert "--depth" not in fetch["run"]
    assert fetch["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"

    validator = next(step for step in steps if step.get("name") == "Validate spec PR policy")
    script = str(validator["run"])
    assert "PYTHONPATH=trusted/src" in script
    assert 'cwd="trusted"' in script
    assert "git\", \"cat-file\"" in script
    assert '".github/workflows/spec-pr-policy.yml"' not in script
    assert "Path(expected_spec_path).exists()" not in script


def test_spec_pr_policy_embedded_validator_runs_for_implementation_pr(tmp_path: Path):
    workflow = _load_workflow("spec-pr-policy.yml")
    steps = workflow["jobs"]["spec-pr-policy"]["steps"]
    validator = next(step for step in steps if step.get("name") == "Validate spec PR policy")
    wrapper = str(validator["run"])
    marker = "python3 - <<'PY'\n"
    script = wrapper.split(marker, 1)[1].rsplit("\nPY", 1)[0]

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=trusted, check=True, capture_output=True)
    (trusted / "specs").mkdir()
    (trusted / "specs" / "demo.md").write_text("# Demo\n")
    subprocess.run(["git", "add", "."], cwd=trusted, check=True, capture_output=True)
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workflow Test",
        "GIT_AUTHOR_EMAIL": "workflow@example.invalid",
        "GIT_COMMITTER_NAME": "Workflow Test",
        "GIT_COMMITTER_EMAIL": "workflow@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=trusted,
        env=commit_env,
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=trusted, text=True).strip()
    (trusted / "README.md").write_text("implementation\n")
    subprocess.run(["git", "add", "."], cwd=trusted, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "head"],
        cwd=trusted,
        env=commit_env,
        check=True,
        capture_output=True,
    )
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=trusted, text=True).strip()

    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "body": (
                        "## Spec\n\nSpec-ID: demo\n\n`specs/demo.md`\n\n"
                        "## Acceptance Criteria\n\n- [x] implemented\n\n"
                        "## Known Issues\n\nNone.\n"
                    )
                }
            }
        )
    )
    env = {
        **os.environ,
        "BASE_SHA": base_sha,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_HEAD_REF": "code/demo--abc123",
        "HEAD_SHA": head_sha,
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Spec PR policy validation passed." in result.stdout


def test_ci_workflow_has_blocking_aggregate_job():
    jobs = _workflow_jobs()
    ci_job = jobs["ci"]
    assert ci_job["if"] == "${{ always() }}"
    assert ci_job["needs"] == [
        "skip-check",
        "lint",
        "test",
        "macos-test",
        "package",
        "security",
        "windows-package",
        "windows-probe",
        "ci-evidence",
    ]

    steps = [step for step in ci_job.get("steps", []) if isinstance(step, dict)]
    enforce_step = next(step for step in steps if step.get("name") == "Enforce required job results")
    script = str(enforce_step.get("run", ""))
    assert "LINT_RESULT" in script
    assert "TEST_RESULT" in script
    assert "PACKAGE_RESULT" in script
    assert "SECURITY_RESULT" in script
    assert "WINDOWS_PACKAGE_RESULT" in script
    assert "WINDOWS_RESULT" in script
    assert "CI_EVIDENCE_RESULT" in script
    assert enforce_step["env"]["WINDOWS_PACKAGE_RESULT"] == "${{ needs.windows-package.result }}"
    assert enforce_step["env"]["WINDOWS_RESULT"] == "${{ needs.windows-probe.result }}"
    assert enforce_step["env"]["CI_EVIDENCE_RESULT"] == "${{ needs.ci-evidence.result }}"
    assert '[ "$WINDOWS_PACKAGE_RESULT" != "success" ]' in script
    assert '[ "$WINDOWS_RESULT" != "success" ]' in script
    assert '[ "$CI_EVIDENCE_RESULT" != "success" ]' in script
    assert "exit 1" in script


def test_ci_windows_job_is_a_required_product_gate_with_diagnostics():
    jobs = _workflow_jobs()
    package = jobs["windows-package"]
    windows = jobs["windows-probe"]
    package_steps = {
        step.get("name"): step
        for step in package["steps"]
        if isinstance(step, dict) and step.get("name")
    }
    parse_step = package_steps["Parse Windows lab PowerShell"]
    assert "Language.Parser]::ParseFile" in parse_step["run"]
    assert "*.ps1.template" in parse_step["run"]
    assert "Add-Type -Path tools/windows-lab/job-supervisor.cs" in parse_step["run"]
    supervisor_probe = package_steps["Run Windows PowerShell 5.1 supervisor probe"]
    assert supervisor_probe["shell"] == "powershell"
    assert "job-supervisor-selftest.ps1" in supervisor_probe["run"]
    supervisor_upload = package_steps["Upload Windows supervisor probe diagnostics"]
    assert supervisor_upload["if"] == "always()"
    assert supervisor_upload["with"]["if-no-files-found"] == "error"
    supervisor_enforcement = package_steps["Enforce Windows supervisor probe result"]
    assert supervisor_enforcement["if"] == "always()"
    assert supervisor_enforcement["shell"] == "powershell"
    assert "job-supervisor-selftest.json" in supervisor_enforcement["run"]
    assert "status -ne 'passed'" in supervisor_enforcement["run"]
    package_steps = [step for step in package["steps"] if isinstance(step, dict)]
    steps = [step for step in windows["steps"] if isinstance(step, dict)]

    assert package["runs-on"] == "windows-latest"
    assert "continue-on-error" not in windows
    assert all("continue-on-error" not in step for step in package_steps)
    assert all("continue-on-error" not in step for step in steps)

    package_commands = {
        step.get("name"): str(step.get("run", ""))
        for step in package_steps
        if step.get("name")
    }
    commands = {
        step.get("name"): str(step.get("run", ""))
        for step in steps
        if step.get("name")
    }
    assert "python -m build --wheel --sdist" in package_commands[
        "Build wheel and source distribution"
    ]
    assert "python -m twine check dist/*" in package_commands[
        "Build wheel and source distribution"
    ]
    assert "$requirement" in commands[
        "Install release distribution with all test surfaces"
    ]
    assert "-m ruff check ." in commands["Run lint"]
    assert '-m pytest -o "pythonpath=" tests -v --ignore=tests/test_windows_probe.py' in commands[
        "Run full portable test suite"
    ]
    assert '-m pytest -o "pythonpath=" tests/test_windows_probe.py -v' in commands[
        "Run Windows integration probes"
    ]
    assert (
        "tests/test_windows_probe.py::test_installed_artifact_cli_matrix"
        in commands["Run installed-artifact Windows CLI matrix"]
    )
    assert '-o "pythonpath=" --import-mode=importlib' in commands[
        "Run installed-artifact Windows CLI matrix"
    ]
    assert "Remove-Item Env:PYTHONPATH" in commands[
        "Run installed-artifact Windows CLI matrix"
    ]
    cli_matrix_step = next(
        step
        for step in steps
        if step.get("name") == "Run installed-artifact Windows CLI matrix"
    )
    assert cli_matrix_step["if"] == "${{ matrix.cli_matrix }}"
    assert cli_matrix_step["env"]["SPEC_WINDOWS_INSTALLED_CLI_MATRIX"] == "1"

    summary = next(step for step in steps if step.get("name") == "Summarize Windows gate")
    upload = next(step for step in steps if step.get("name") == "Upload Windows probe logs")
    assert summary["if"] == "${{ always() }}"
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["path"] == "artifacts/"


def test_ci_windows_matrix_covers_supported_python_and_both_distribution_types():
    jobs = _workflow_jobs()
    windows = jobs["windows-probe"]
    include = windows["strategy"]["matrix"]["include"]

    wheel_versions = {
        str(entry["python-version"])
        for entry in include
        if entry["distribution"] == "wheel" and entry["full_suite"] is True
    }
    assert wheel_versions == {"3.11", "3.12", "3.13"}
    assert any(
        entry["distribution"] == "sdist" and str(entry["python-version"]) == "3.12"
        for entry in include
    )
    cli_matrix_entries = [entry for entry in include if entry["cli_matrix"] is True]
    assert cli_matrix_entries == [
        {
            "python-version": "3.12",
            "distribution": "wheel",
            "full_suite": True,
            "cli_matrix": True,
        }
    ]
    assert windows["strategy"]["fail-fast"] is False
    assert "windows-package" in windows["needs"]


def test_ci_linux_matrix_reports_every_supported_version_failure():
    jobs = _workflow_jobs()

    assert jobs["test"]["strategy"]["fail-fast"] is False


def test_ci_windows_tests_cannot_import_checkout_via_pytest_pythonpath():
    jobs = _workflow_jobs()
    steps = [step for step in jobs["windows-probe"]["steps"] if isinstance(step, dict)]
    commands = {
        step.get("name"): str(step.get("run", ""))
        for step in steps
        if step.get("name")
    }

    provenance = commands["Verify imports resolve outside the checkout"]
    assert 'Path(os.environ["GITHUB_WORKSPACE"]).resolve()' in provenance
    assert "imported.relative_to(checkout)" in provenance
    assert "Remove-Item Env:PYTHONPATH" in commands["Run full portable test suite"]
    assert '-o "pythonpath="' in commands["Run full portable test suite"]
    assert '-o "pythonpath="' in commands["Run Windows integration probes"]


def test_ci_windows_jobs_do_not_expose_repository_credentials_to_tested_code():
    jobs = _workflow_jobs()
    for job_name in ("windows-package", "windows-probe"):
        job = jobs[job_name]
        assert "GH_TOKEN" not in job.get("env", {})
        action_steps = [step for step in job["steps"] if step.get("uses")]
        assert action_steps
        assert all(
            re.fullmatch(r"[^@]+@[0-9a-f]{40}", str(step["uses"]))
            for step in action_steps
        )
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["persist-credentials"] is False


def test_ci_installs_optional_surfaces_and_smokes_built_wheel():
    jobs = _workflow_jobs()
    test_steps = yaml.dump(jobs["test"]["steps"])
    assert ".[dev,tui,web]" in test_steps

    package_steps = yaml.dump(jobs["package"]["steps"])
    assert "python -m build" in package_steps
    assert "pip check" in package_steps
    assert "pip_audit" in package_steps
    assert "spec\" --help" in package_steps


def test_ci_lint_job_verifies_and_runs_pinned_actionlint():
    jobs = _workflow_jobs()
    steps = [step for step in jobs["lint"]["steps"] if isinstance(step, dict)]
    actionlint = next(
        step
        for step in steps
        if step.get("name") == "Validate GitHub Actions workflows with actionlint v1.7.12"
    )
    script = str(actionlint["run"])

    assert actionlint["env"]["ACTIONLINT_VERSION"] == "1.7.12"
    assert actionlint["env"]["ACTIONLINT_ARCHIVE_SHA256"] == (
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
    )
    assert "sha256sum --check --strict" in script
    assert '"$binary"' in script


def test_ci_security_job_verifies_and_runs_pinned_gitleaks():
    jobs = _workflow_jobs()
    security = jobs["security"]
    steps = [step for step in security["steps"] if isinstance(step, dict)]

    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"]["fetch-depth"] == 0

    install = next(step for step in steps if step.get("name") == "Install verified gitleaks v8.29.1")
    install_script = str(install["run"])
    assert install["env"]["GITLEAKS_VERSION"] == "8.29.1"
    assert install["env"]["GITLEAKS_ARCHIVE_SHA256"] == (
        "e4eb209d04e20339d77122a3bdf9cd41351255cfb27ebcb75e85325e04f88924"
    )
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in install_script
    assert "sha256sum --check --strict" in install_script

    scan = next(step for step in steps if step.get("name") == "Scan repository history and checked-out tree")
    scan_script = str(scan["run"])
    assert "gitleaks\" git --config .gitleaks.toml" in scan_script
    assert "gitleaks\" dir --config .gitleaks.toml" in scan_script


def test_gitleaks_allowlist_is_limited_to_audited_fixture_matches():
    with GITLEAKS_CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config["extend"] == {"useDefault": True}
    allowlists = config["allowlists"]
    assert len(allowlists) == 1
    allowlist = allowlists[0]
    assert allowlist["regexTarget"] == "match"
    assert allowlist["regexes"]
    assert "paths" not in allowlist
    assert "commits" not in allowlist


# ---------- ci.yml version-bump skip path ----------


def test_ci_skip_check_gates_product_jobs():
    """Product jobs depend on skip-check and are skipped for version bumps."""
    jobs = _workflow_jobs()
    assert "skip-check" in jobs
    for job_name in (
        "lint",
        "test",
        "package",
        "security",
        "macos-test",
        "windows-package",
        "windows-probe",
        "ci-evidence",
    ):
        job = jobs[job_name]
        assert "skip-check" in job["needs"]
        condition = str(job["if"])
        assert "is_version_bump" in condition
        assert "!= 'true'" in condition


def test_ci_aggregate_handles_version_bump():
    """ci aggregate job succeeds early when is_version_bump is true."""
    jobs = _workflow_jobs()
    ci_job = jobs["ci"]
    enforce_step = next(
        s for s in ci_job["steps"]
        if isinstance(s, dict) and s.get("name") == "Enforce required job results"
    )
    script = str(enforce_step.get("run", ""))
    assert "IS_VERSION_BUMP" in script


def test_ci_evidence_waits_for_every_required_product_gate():
    """A downloadable passing bundle cannot outlive a later package/security failure."""
    evidence_job = _workflow_jobs()["ci-evidence"]

    assert evidence_job["needs"] == [
        "skip-check",
        "lint",
        "test",
        "macos-test",
        "package",
        "security",
        "windows-package",
        "windows-probe",
    ]
    condition = str(evidence_job["if"])
    for job_name in (
        "lint",
        "test",
        "macos-test",
        "package",
        "security",
        "windows-package",
        "windows-probe",
    ):
        assert f"needs.{job_name}.result == 'success'" in condition


def test_ci_skip_check_verifies_same_repo():
    """skip-check rejects version-bump branches from forks."""
    jobs = _workflow_jobs()
    steps_text = yaml.dump(jobs["skip-check"]["steps"])
    assert "head.repo.full_name" in steps_text
    assert "github.repository" in steps_text


def test_ci_skip_check_verifies_changed_files():
    """skip-check validates that only pyproject.toml is changed."""
    jobs = _workflow_jobs()
    steps_text = yaml.dump(jobs["skip-check"]["steps"])
    assert "pyproject.toml" in steps_text


# ---------- codex-review.yml version-bump skip path ----------


def test_codex_review_version_bump_gate_publishes_status():
    """version-bump-gate publishes a success review-decision-gate status."""
    jobs = _load_workflow("codex-review.yml")["jobs"]
    assert "skip-check" in jobs
    gate = jobs["version-bump-gate"]
    assert "skip-check" in gate["needs"]
    assert "== 'true'" in str(gate["if"])
    steps_text = yaml.dump(gate["steps"])
    assert "review-decision-gate" in steps_text
    assert "success" in steps_text


def test_codex_review_skips_reviewer_for_version_bump():
    """determine-review-routing is gated behind skip-check."""
    jobs = _load_workflow("codex-review.yml")["jobs"]
    routing = jobs["determine-review-routing"]
    assert "skip-check" in routing["needs"]
    condition = str(routing["if"])
    assert "is_version_bump" in condition
    assert "!= 'true'" in condition


def test_codex_review_skip_check_verifies_same_repo():
    """skip-check in codex-review rejects version-bump branches from forks."""
    jobs = _load_workflow("codex-review.yml")["jobs"]
    steps_text = yaml.dump(jobs["skip-check"]["steps"])
    assert "head.repo.full_name" in steps_text
    assert "github.repository" in steps_text


def test_codex_review_skip_check_verifies_changed_files():
    """skip-check in codex-review validates only pyproject.toml is changed."""
    jobs = _load_workflow("codex-review.yml")["jobs"]
    steps_text = yaml.dump(jobs["skip-check"]["steps"])
    assert "pyproject.toml" in steps_text


# ---------- spec-pr-policy.yml version-bump skip ----------


def test_spec_pr_policy_skips_version_bump():
    """spec-pr-policy is skipped for release/version-bump PRs."""
    jobs = _load_workflow("spec-pr-policy.yml")["jobs"]
    condition = str(jobs["spec-pr-policy"].get("if", ""))
    assert "release/version-bump" in condition


def test_spec_pr_policy_verifies_same_repo():
    """spec-pr-policy skip condition checks the PR comes from the same repo."""
    jobs = _load_workflow("spec-pr-policy.yml")["jobs"]
    condition = str(jobs["spec-pr-policy"].get("if", ""))
    assert "head.repo.full_name" in condition
