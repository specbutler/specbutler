from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "spec-pr-policy.yml"
REPO_ROOT = WORKFLOW_PATH.parent.parent.parent


def _workflow_jobs() -> dict:
    payload = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict)
    return jobs


def _spec_pr_policy_script() -> str:
    steps = _workflow_jobs()["spec-pr-policy"].get("steps", [])
    for step in steps:
        if isinstance(step, dict) and step.get("name") == "Validate spec PR policy":
            script = step.get("run")
            assert isinstance(script, str) and script.strip()
            return script
    raise AssertionError("spec-pr-policy step is missing its run script")


def _spec_pr_policy_python() -> str:
    script = _spec_pr_policy_script()
    match = re.search(
        r"PYTHONPATH=trusted/src python3 - <<'PY'\n(?P<code>.*)\nPY\s*$",
        script,
        flags=re.DOTALL,
    )
    assert match is not None, "spec-pr-policy step is missing its embedded Python script"
    return match.group("code")


def _run_authoring_policy(
    tmp_path: Path,
    changed_paths: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> subprocess.CompletedProcess[str]:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "body": "",
                    "base": {
                        "sha": "origin/main",
                    },
                }
            }
        )
    )

    def fake_check_output(cmd: list[str], *, text: bool = False, **_kwargs: object) -> str:
        assert text is True
        assert cmd[:2] == ["git", "diff"]
        return "\n".join(changed_paths)

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("BASE_SHA", "base-sha")
    monkeypatch.setenv("GITHUB_HEAD_REF", "spec/my-feature")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("HEAD_SHA", "head-sha")
    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    stdout = io.StringIO()
    stderr = io.StringIO()
    returncode = 0
    globals_dict = {
        "__name__": "__main__",
    }
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exec(
                "from pathlib import Path\nimport sys\nsys.path.insert(0, str(Path('src').resolve()))\n"
                + _spec_pr_policy_python(),
                globals_dict,
            )
        except SystemExit as exc:
            code = exc.code
            returncode = 0 if code is None else int(code)

    return subprocess.CompletedProcess(
        args=["spec-pr-policy"],
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


class TestSpecAuthoringPathAllowlist:
    def test_allows_only_spec_and_prompt_directory_prefixes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        result = _run_authoring_policy(
            tmp_path,
            [
                "specs/my-feature.md",
                "prompts/spec-template.md",
            ],
            monkeypatch,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "only spec/prompt files changed" in result.stdout

    @pytest.mark.parametrize(
        "changed_path",
        [
            pytest.param(
                ".github/workflows/spec-pr-policy.yml",
                id="policy-workflow-is-trusted-base-only",
            ),
            pytest.param(
                ".github/workflows/spec-pr-policy.yml.backup",
                id="workflow-backup-prefix-is-rejected",
            ),
            pytest.param(
                ".github/workflows/spec-pr-policy.yml/extra",
                id="workflow-subpath-prefix-is-rejected",
            ),
        ],
    )
    def test_rejects_files_that_only_share_the_exact_file_prefix(
        self,
        tmp_path: Path,
        changed_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        result = _run_authoring_policy(tmp_path, [changed_path], monkeypatch)

        assert result.returncode == 1
        assert changed_path in result.stderr
        assert "touches non-spec files" in result.stderr
