from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from spec_runtime.dependabot_review import DependabotPolicyError, main, validate_dependabot_update


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workflow Test",
        "GIT_AUTHOR_EMAIL": "workflow@example.invalid",
        "GIT_COMMITTER_NAME": "Workflow Test",
        "GIT_COMMITTER_EMAIL": "workflow@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    return repo


def test_accepts_only_same_action_sha_updates_and_writes_review(tmp_path: Path):
    repo = _repo(tmp_path)
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@1111111111111111111111111111111111111111 # v4\n"
        "  - name: Upload\n"
        "    uses: actions/upload-artifact@3333333333333333333333333333333333333333 # v4\n"
    )
    base_sha = _commit(repo, "base")
    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@2222222222222222222222222222222222222222 # v7.0.1\n"
        "  - name: Upload\n"
        "    uses: actions/upload-artifact@4444444444444444444444444444444444444444 # v7.0.1\n"
    )
    head_sha = _commit(repo, "update pin")
    output = repo / "review.json"

    assert main(
        [
            "--repo",
            str(repo),
            "--base-sha",
            base_sha,
            "--head-sha",
            head_sha,
            "--output",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text())
    assert payload["decision"] == "approved"
    assert payload["reviewer_agent"] == "dependabot"
    assert payload["reviewed_base_sha"] == base_sha
    assert payload["reviewed_head_sha"] == head_sha


def test_rejects_non_pin_workflow_changes(tmp_path: Path):
    repo = _repo(tmp_path)
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@1111111111111111111111111111111111111111 # v4\n"
    )
    base_sha = _commit(repo, "base")
    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@2222222222222222222222222222222222222222 # v7.0.1\n"
        "  - run: untrusted-command\n"
    )
    head_sha = _commit(repo, "tamper")

    with pytest.raises(DependabotPolicyError, match="existing action pins"):
        validate_dependabot_update(repo, base_sha, head_sha)


def test_accepts_constraint_only_pyproject_update(tmp_path: Path):
    repo = _repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        '[build-system]\nrequires = ["setuptools>=70"]\n\n'
        '[project]\nname = "demo"\ndependencies = ["starlette>=0.40; python_version >= \'3.11\'"]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n'
    )
    base_sha = _commit(repo, "base")
    pyproject.write_text(
        '[build-system]\nrequires = ["setuptools>=71"]\n\n'
        '[project]\nname = "demo"\ndependencies = ["starlette>=0.41; python_version >= \'3.11\'"]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8.4"]\n'
    )
    head_sha = _commit(repo, "update dependencies")

    assert validate_dependabot_update(repo, base_sha, head_sha) == ("pyproject.toml",)


def test_rejects_dependency_identity_change(tmp_path: Path):
    repo = _repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\ndependencies = ["starlette>=0.40"]\n')
    base_sha = _commit(repo, "base")
    pyproject.write_text('[project]\nname = "demo"\ndependencies = ["malware>=1"]\n')
    head_sha = _commit(repo, "replace dependency")

    with pytest.raises(DependabotPolicyError, match="dependency identities changed"):
        validate_dependabot_update(repo, base_sha, head_sha)


def test_rejects_direct_reference_source_change(tmp_path: Path):
    repo = _repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\ndependencies = ["starlette>=0.40"]\n')
    base_sha = _commit(repo, "base")
    pyproject.write_text(
        '[project]\nname = "demo"\n'
        'dependencies = ["starlette @ https://example.invalid/starlette.whl"]\n'
    )
    head_sha = _commit(repo, "change dependency source")

    with pytest.raises(DependabotPolicyError, match="registry version constraints"):
        validate_dependabot_update(repo, base_sha, head_sha)
