from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "codex-review.yml"
REPO_ROOT = WORKFLOW_PATH.parents[2]
PINNED_ACTION_RE = re.compile(r"^(?P<action>[^@]+)@[0-9a-f]{40}$")


def _workflow_jobs() -> dict:
    payload = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = payload.get("jobs", {})
    assert isinstance(jobs, dict)
    return jobs


def test_workflow_reruns_when_pr_body_metadata_is_edited():
    payload = yaml.safe_load(WORKFLOW_PATH.read_text())
    on_section = payload.get("on", payload.get(True, {}))
    pull_request = on_section.get("pull_request", {})
    event_types = pull_request.get("types", [])
    assert "edited" in event_types


def test_workflow_defaults_to_read_only_permissions():
    payload = yaml.safe_load(WORKFLOW_PATH.read_text())
    assert payload["permissions"] == "read-all"


def _determine_review_routing_script() -> str:
    steps = _workflow_jobs()["determine-review-routing"].get("steps", [])
    for step in steps:
        if isinstance(step, dict) and step.get("id") == "routing":
            script = step.get("run")
            assert isinstance(script, str) and script.strip()
            return script
    raise AssertionError("determine-review-routing step is missing its run script")


def _review_routing(
    tmp_path: Path,
    pr_body: str,
    **overrides: str,
) -> dict[str, bool]:
    github_output = tmp_path / "github-output.txt"
    env = os.environ.copy()
    env.update(
        {
            "HEAD_REF": "fix/example",
            "HEAD_REPO": "example/spec",
            "PR_AUTHOR": "octocat",
            "PR_AUTHOR_TYPE": "User",
            "PR_BODY": pr_body,
            "REPO": "example/spec",
            **overrides,
        }
    )
    env["GITHUB_OUTPUT"] = str(github_output)
    result = subprocess.run(
        ["/bin/sh", "-lc", _determine_review_routing_script()],
        cwd=WORKFLOW_PATH.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    output = dict(line.partition("=")[::2] for line in github_output.read_text().splitlines())
    assert output.keys() == {"local_review_owned", "dependabot_owned"}
    return {key: value == "true" for key, value in output.items()}


def _local_review_owned_from_pr_body(tmp_path: Path, pr_body: str) -> bool:
    return _review_routing(tmp_path, pr_body)["local_review_owned"]


def _find_pinned_action_step(steps: list[dict], action: str) -> dict:
    for step in steps:
        uses = str(step.get("uses", ""))
        match = PINNED_ACTION_RE.fullmatch(uses)
        if match is not None and match.group("action") == action:
            return step
    raise AssertionError(f"missing fully pinned action step: {action}")


def test_determine_review_routing_does_not_checkout_or_import_pr_head_parser_code():
    job = _workflow_jobs()["determine-review-routing"]
    steps = job.get("steps", [])
    assert all(
        not str(step.get("uses", "")).startswith("actions/checkout@")
        for step in steps
        if isinstance(step, dict)
    )

    run_scripts = [str(step.get("run", "")) for step in steps if isinstance(step, dict) and "run" in step]
    assert run_scripts
    assert all("spec_runtime.spec_identity" not in script for script in run_scripts)


def test_local_review_seeds_pending_status_in_separate_job():
    jobs = _workflow_jobs()
    seed_job = jobs["seed-local-review-status"]
    assert "local_review_owned == 'true'" in str(seed_job.get("if", ""))

    steps = [step for step in seed_job.get("steps", []) if isinstance(step, dict)]
    pending_step = next(step for step in steps if step.get("name") == "Publish pending local-review status")
    assert "statuses/" in str(pending_step.get("run", ""))


def test_seed_local_review_guards_against_overwriting_final_status():
    jobs = _workflow_jobs()
    seed_job = jobs["seed-local-review-status"]
    steps = [step for step in seed_job.get("steps", []) if isinstance(step, dict)]
    pending_step = next(step for step in steps if step.get("name") == "Publish pending local-review status")
    script = str(pending_step.get("run", ""))
    assert "commits/" in script and "/statuses" in script
    for state in ("success", "failure", "error"):
        assert state in script
    assert "target_url" in script
    assert '"/pull/"' in script


def test_review_decision_gate_skipped_for_local_review():
    jobs = _workflow_jobs()
    job = jobs["review-decision-gate"]
    job_if = str(job.get("if", ""))
    assert "local_review_owned != 'true'" in job_if


def _review_gate_steps() -> list[dict]:
    steps = _workflow_jobs()["review-decision-gate"].get("steps", [])
    return [step for step in steps if isinstance(step, dict)]


def _step_index(steps: list[dict], predicate) -> int:
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    raise AssertionError("no matching step found")


def test_review_decision_gate_strips_forge_credentials_from_checkout():
    steps = _review_gate_steps()
    checkout = _find_pinned_action_step(steps, "actions/checkout")
    # The PR head is attacker-controlled and gets bootstrapped/reviewed, so the
    # forge token must not be persisted into the untrusted worktree.
    assert checkout.get("with", {}).get("persist-credentials") is False


def test_review_decision_gate_materializes_all_control_code_from_trusted_base():
    steps = _review_gate_steps()
    prepare = next(
        step for step in steps if step.get("name") == "Materialize trusted review code and prompt"
    )
    script = str(prepare.get("run", ""))

    assert 'git archive "$BASE_SHA"' in script
    assert 'PYTHONPATH="$TRUSTED_ROOT/src"' in script
    assert script.index('cd "$TRUSTED_ROOT"') < script.index("python3 -P -")
    assert "python3 -P - <<'PY'" in script
    assert 'trusted_root / ".github/prompts/review.md"' in script
    assert "pip install" not in script
    assert "install_command" not in script

    codex = next(step for step in steps if step.get("id") == "codex")
    codex_inputs = codex["with"]
    assert codex_inputs["prompt-file"].startswith("/tmp/spec-review-${{ github.run_id }}-")
    assert codex_inputs["output-schema-file"].startswith(
        "/tmp/spec-trusted-${{ github.run_id }}-"
    )
    assert codex_inputs["sandbox"] == "read-only"
    assert codex_inputs["safety-strategy"] == "drop-sudo"

    prepare_index = _step_index(
        steps,
        lambda step: step.get("name") == "Materialize trusted review code and prompt",
    )
    codex_index = _step_index(steps, lambda step: step.get("id") == "codex")
    assert prepare_index < codex_index


def test_sticky_comment_publisher_executes_only_trusted_base_code():
    job = _workflow_jobs()["publish-sticky-review-comment"]
    steps = [step for step in job.get("steps", []) if isinstance(step, dict)]
    checkout = _find_pinned_action_step(steps, "actions/checkout")
    checkout_config = checkout.get("with", {})

    assert checkout_config.get("ref") == "${{ github.event.pull_request.base.sha }}"
    assert checkout_config.get("persist-credentials") is False
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "write",
    }


def test_review_decision_gate_never_executes_pr_head_code_before_secret_action():
    steps = _review_gate_steps()
    codex_index = _step_index(steps, lambda step: step.get("id") == "codex")
    pre_action_scripts = "\n".join(
        str(step.get("run", "")) for step in steps[:codex_index] if "run" in step
    )

    assert "pip install" not in pre_action_scripts
    assert "PYTHONPATH=src" not in pre_action_scripts
    assert "python -m" not in pre_action_scripts

    key_check = next(step for step in steps if step.get("id") == "key")
    assert key_check["env"]["REVIEW_API_KEY"] == "${{ secrets.OPENAI_API_KEY }}"
    assert "${{ secrets.OPENAI_API_KEY }}" not in str(key_check["run"])


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param(
            {
                "HEAD_REF": "dependabot/github_actions/actions-123",
                "HEAD_REPO": "example/spec",
                "PR_AUTHOR": "dependabot[bot]",
                "PR_AUTHOR_TYPE": "Bot",
                "REPO": "example/spec",
            },
            True,
            id="verified-same-repository-dependabot",
        ),
        pytest.param(
            {
                "HEAD_REF": "dependabot/github_actions/actions-123",
                "HEAD_REPO": "example/spec",
                "PR_AUTHOR": "dependabot[bot]",
                "PR_AUTHOR_TYPE": "User",
                "REPO": "example/spec",
            },
            False,
            id="user-spoofing-bot-login",
        ),
        pytest.param(
            {
                "HEAD_REF": "dependabot/github_actions/actions-123",
                "HEAD_REPO": "fork/spec",
                "PR_AUTHOR": "dependabot[bot]",
                "PR_AUTHOR_TYPE": "Bot",
                "REPO": "example/spec",
            },
            False,
            id="dependabot-fork",
        ),
        pytest.param(
            {
                "HEAD_REF": "fix/spoofed-dependabot",
                "HEAD_REPO": "example/spec",
                "PR_AUTHOR": "dependabot[bot]",
                "PR_AUTHOR_TYPE": "Bot",
                "REPO": "example/spec",
            },
            False,
            id="non-dependabot-branch",
        ),
    ],
)
def test_dependabot_review_routing_requires_immutable_bot_identity(
    tmp_path: Path,
    overrides: dict[str, str],
    expected: bool,
):
    assert _review_routing(tmp_path, "", **overrides)["dependabot_owned"] is expected


def test_dependabot_route_never_receives_openai_secret():
    steps = _review_gate_steps()
    validation = next(
        step for step in steps if step.get("name") == "Validate trusted Dependabot update"
    )
    key_check = next(step for step in steps if step.get("id") == "key")
    codex = next(step for step in steps if step.get("id") == "codex")
    failure = next(
        step
        for step in steps
        if step.get("name") == "Synthesize failed payload when reviewer cannot run"
    )

    assert "dependabot_owned == 'true'" in str(validation.get("if", ""))
    for secret_step in (key_check, codex):
        assert "dependabot_owned != 'true'" in str(secret_step.get("if", ""))
    assert "dependabot_owned != 'true'" in str(failure.get("if", ""))
    assert "OPENAI_API_KEY" not in str(validation)
    script = str(validation["run"])
    assert validation["env"]["UNTRUSTED_ROOT"] == "${{ github.workspace }}"
    assert script.index('cd "$TRUSTED_ROOT"') < script.index("-m spec_runtime.dependabot_review")
    assert 'PYTHONPATH="$TRUSTED_ROOT/src"' in script
    assert "python3 -P -m spec_runtime.dependabot_review" in script
    assert '--repo "$UNTRUSTED_ROOT"' in script
    assert "--repo ." not in script


def test_dependabot_route_cannot_import_pr_head_python(tmp_path: Path):
    validation = next(
        step
        for step in _review_gate_steps()
        if step.get("name") == "Validate trusted Dependabot update"
    )
    repo = tmp_path / "untrusted"
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@1111111111111111111111111111111111111111 # v4\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    commit_command = [
        "git",
        "-c",
        "user.name=Workflow Test",
        "-c",
        "user.email=workflow@example.invalid",
        "commit",
    ]
    subprocess.run(
        [*commit_command, "-m", "base"], cwd=repo, check=True, capture_output=True
    )
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    workflow.write_text(
        "steps:\n"
        "  - uses: actions/checkout@2222222222222222222222222222222222222222 # v7\n"
    )
    malicious_package = repo / "spec_runtime"
    malicious_package.mkdir()
    (malicious_package / "__init__.py").write_text("")
    (malicious_package / "dependabot_review.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        'Path(os.environ["MALICIOUS_SENTINEL"]).write_text("executed")\n'
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [*commit_command, "-m", "malicious head"], cwd=repo, check=True, capture_output=True
    )
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    sentinel = tmp_path / "untrusted-code-executed"
    env = {
        **os.environ,
        "BASE_SHA": base_sha,
        "HEAD_SHA": head_sha,
        "MALICIOUS_SENTINEL": str(sentinel),
        "RAW_REVIEW_PATH": str(tmp_path / "review.json"),
        "TRUSTED_ROOT": str(REPO_ROOT),
        "UNTRUSTED_ROOT": str(repo),
    }

    result = subprocess.run(
        ["bash", "-e", "-c", str(validation["run"])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "changed file is not allowed" in result.stdout
    assert not sentinel.exists()


def test_review_gate_evaluation_uses_trusted_code_and_schema():
    steps = _review_gate_steps()
    gate = next(step for step in steps if step.get("id") == "gate")
    script = str(gate.get("run", ""))

    assert 'PYTHONPATH="$TRUSTED_ROOT/src"' in script
    assert script.index('cd "$TRUSTED_ROOT"') < script.index("-m spec_runtime.review_gate")
    assert "python3 -P -m spec_runtime.review_gate" in script
    assert '--schema "$TRUSTED_ROOT/.github/schemas/codex-review.schema.json"' in script
    assert "PYTHONPATH=src" not in script


@pytest.mark.parametrize(
    ("pr_body", "expected_local_review_owned"),
    [
        pytest.param(
            "## Spec\n"
            "Spec-ID: my-feature\n"
            "[specs/my-feature.md](specs/my-feature.md)\n\n"
            "## Review\n"
            "Review-Owner: local\n",
            True,
            id="implementation-pr-with-local-review-ownership",
        ),
        pytest.param(
            "## Summary\nTask: task-docs\n\n## Review\nReview-Owner: local\n",
            True,
            id="task-pr-with-local-review-ownership",
        ),
        pytest.param(
            "## Summary\nAd-hoc fix for review docs.\n",
            False,
            id="adhoc-pr-without-local-review-ownership",
        ),
        pytest.param(
            "## Spec\nSpec-ID: review-routing\n[specs/review-routing.md](specs/review-routing.md)\n",
            False,
            id="spec-authoring-pr-without-local-review-ownership",
        ),
        pytest.param(
            "## Spec\n"
            "Spec-ID: review-routing\n"
            "[specs/review-routing.md](specs/review-routing.md)\n\n"
            "## Review\n"
            "Review-Owner: local\n",
            True,
            id="spec-authoring-pr-with-local-review-ownership",
        ),
        pytest.param(
            "",
            False,
            id="missing-ownership-metadata-falls-back-to-cloud-review",
        ),
        pytest.param(
            "## Review\nReview-Owner: local reviewer\n",
            False,
            id="malformed-ownership-metadata-falls-back-to-cloud-review",
        ),
    ],
)
def test_determine_review_routing_matches_spec_matrix(
    tmp_path: Path,
    pr_body: str,
    expected_local_review_owned: bool,
):
    assert _local_review_owned_from_pr_body(tmp_path, pr_body) is expected_local_review_owned
