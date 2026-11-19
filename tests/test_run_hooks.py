"""Tests for project-configured run lifecycle hooks (``[hooks]`` in ``.spec.toml``)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from spec_runtime import orchestrator as orch
from spec_runtime import run_hooks
from spec_runtime.config import SpecConfigError, load_repo_spec_runtime_config

# Appends one JSON line per call to hooks.jsonl beside itself and fails for an
# event when a file named fail-<event> exists there.
HOOK_SCRIPT = """\
import json, os, pathlib, sys
here = pathlib.Path(__file__).parent
env = {k: v for k, v in os.environ.items() if k.startswith("SPEC_HOOK_")}
event = env["SPEC_HOOK_EVENT"]
entry = {
    "argv": sys.argv[1:],
    "env": env,
    "message": pathlib.Path(env["SPEC_HOOK_MESSAGE_FILE"]).read_text(encoding="utf-8"),
    "cwd": os.getcwd(),
}
with (here / "hooks.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")
if (here / f"fail-{event}").exists():
    print("another owner already holds this", file=sys.stderr)
    sys.exit(3)
"""


def write_hook_config(repo: Path, events: tuple[str, ...] = run_hooks.EVENTS, *, extra_args: tuple[str, ...] = ()):
    script = repo / "hook.py"
    script.write_text(HOOK_SCRIPT, encoding="utf-8")
    lines = ["[specs]", "require_merged_spec = false", "", "[hooks]"]
    for event in events:
        argv = [sys.executable, str(script), "{event}", "{spec_id}", "{run_id}", "{branch}", *extra_args]
        lines.append(f"run_{event} = {json.dumps(argv)}")
    (repo / ".spec.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_spec(repo: Path, spec_id: str = "feature", frontmatter: str = "issue: TRACK-7\n") -> None:
    path = repo / "specs" / f"{spec_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {spec_id}\narea: backend\n{frontmatter}description: {spec_id} spec\n---\n\n"
        "## Goal\nDeliver it.\n\n## Acceptance Criteria\n- [ ] works\n",
        encoding="utf-8",
    )


def hook_log(repo: Path) -> list[dict]:
    log = repo / "hooks.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def events(repo: Path) -> list[str]:
    return [entry["env"]["SPEC_HOOK_EVENT"] for entry in hook_log(repo)]


def implement_args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(**{"spec": "feature", "agent": "codex", "base": "origin/main", **overrides})


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    write_spec(tmp_path)
    write_hook_config(tmp_path)
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: tmp_path)
    return tmp_path


def stub_workflow(monkeypatch: pytest.MonkeyPatch, result: str, **fields) -> list[orch.RunState]:
    """Replace the phase loop with one that stops the run in ``result``."""
    seen: list[orch.RunState] = []

    def phases(run, repo_root, *, retry_cap):
        run.status = result
        for name, value in fields.items():
            setattr(run, name, value)
        run.save(repo_root)
        seen.append(run)
        return result

    monkeypatch.setattr(orch, "_run_workflow_phases", phases)
    return seen


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_no_hooks_table_means_no_hooks():
    assert not run_hooks.parse_hooks_section(None).enabled


def test_parse_accepts_command_strings_and_argv_arrays():
    config = run_hooks.parse_hooks_section(
        {"run_start": "tracker claim --id '{frontmatter_issue}'", "run_finished": ["tracker", "done"], "timeout_seconds": 5}
    )
    assert config.commands == {"start": ("tracker", "claim", "--id", "{frontmatter_issue}"), "finished": ("tracker", "done")}
    assert config.timeout_seconds == 5


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"run_started": "x"}, "hooks.run_started is not a hook"),
        ({"run_start": 3}, "must be a command string or an array of strings"),
        ({"run_start": "echo 'unclosed"}, "could not be parsed"),
        ({"timeout_seconds": 0}, "must be a positive number"),
        ("echo", "hooks must be a table"),
    ],
)
def test_parse_rejects_malformed_tables(payload, message):
    with pytest.raises(run_hooks.HookConfigError, match=message):
        run_hooks.parse_hooks_section(payload)


def test_spec_toml_loads_hooks_and_rejects_unknown_events(tmp_path: Path):
    (tmp_path / ".spec.toml").write_text('[hooks]\nrun_blocked = "notify blocked"\n', encoding="utf-8")
    assert load_repo_spec_runtime_config(tmp_path).hooks.commands == {"blocked": ("notify", "blocked")}
    (tmp_path / ".spec.toml").write_text('[hooks]\non_start = "x"\n', encoding="utf-8")
    with pytest.raises(SpecConfigError, match="hooks.on_start is not a hook"):
        load_repo_spec_runtime_config(tmp_path)


def test_lint_accepts_frontmatter_keys_that_hooks_read(tmp_path: Path):
    from spec_runtime.spec_lint import lint_specs

    write_spec(tmp_path, frontmatter="issue: TRACK-7\nticket: T-1\nstray: x\n")
    (tmp_path / ".spec.toml").write_text(
        "[hooks]\n"
        'run_start = "tracker claim {frontmatter_issue}"\n'
        """run_finished = ["sh", "-c", 'tracker done "$SPEC_HOOK_FRONTMATTER_TICKET"']\n""",
        encoding="utf-8",
    )
    warnings = lint_specs(tmp_path, merged_spec_ids=set())[0].warnings
    assert "unknown frontmatter key: stray" in warnings
    assert not any("issue" in warning or "ticket" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# Running one hook
# ---------------------------------------------------------------------------


def test_hook_receives_arguments_environment_and_message_file(tmp_path: Path):
    write_hook_config(tmp_path, ("needs_input",), extra_args=("{frontmatter_issue}", "${HOME}", "{print}"))
    config = load_repo_spec_runtime_config(tmp_path).hooks
    values = run_hooks.hook_values(
        event="needs_input",
        spec_id="feature",
        run_id="feature-1",
        branch="code/feature--1",
        state="waiting-for-input",
        phase="implement",
        run_mode="spec",
        frontmatter={"issue": "TRACK-7", "depends_on": ["a"], "obsolete": False, "Review-Owner": "sam"},
    )
    result = run_hooks.run_hook(
        config, "needs_input", values, cwd=tmp_path, message="token=abc which one?", redact=lambda text: text.replace("abc", "<R>")
    )
    assert result == run_hooks.HookResult(True, "")
    (entry,) = hook_log(tmp_path)
    # Unknown brace text is left for the command; frontmatter scalars are passed through.
    assert entry["argv"] == ["needs_input", "feature", "feature-1", "code/feature--1", "TRACK-7", "${HOME}", "{print}"]
    assert entry["env"]["SPEC_HOOK_STATE"] == "waiting-for-input"
    assert entry["env"]["SPEC_HOOK_PHASE"] == "implement"
    assert entry["env"]["SPEC_HOOK_RUN_MODE"] == "spec"
    assert entry["env"]["SPEC_HOOK_FRONTMATTER_ISSUE"] == "TRACK-7"
    assert entry["env"]["SPEC_HOOK_FRONTMATTER_REVIEW_OWNER"] == "sam"
    assert "SPEC_HOOK_FRONTMATTER_DEPENDS_ON" not in entry["env"]
    assert "SPEC_HOOK_FRONTMATTER_OBSOLETE" not in entry["env"]
    assert entry["message"] == "token=<R> which one?\n"
    assert Path(entry["cwd"]).resolve() == tmp_path.resolve()
    assert not Path(entry["env"]["SPEC_HOOK_MESSAGE_FILE"]).exists()


def test_unconfigured_event_runs_nothing(tmp_path: Path):
    write_hook_config(tmp_path, ("start",))
    config = load_repo_spec_runtime_config(tmp_path).hooks
    assert run_hooks.run_hook(config, "finished", {"event": "finished"}, cwd=tmp_path) is None


def test_hook_failures_are_reported_not_raised(tmp_path: Path):
    write_hook_config(tmp_path, ("start",), extra_args=("{frontmatter_issue}",))
    config = load_repo_spec_runtime_config(tmp_path).hooks
    values = run_hooks.hook_values(
        event="start", spec_id="feature", run_id="r", branch="b", state="pending", phase="bootstrap", run_mode="spec"
    )
    missing = run_hooks.run_hook(config, "start", values, cwd=tmp_path)
    assert missing == run_hooks.HookResult(False, "run_start: the spec's frontmatter does not set issue")
    assert hook_log(tmp_path) == []

    (tmp_path / "fail-start").touch()
    refused = run_hooks.run_hook(config, "start", {**values, "frontmatter_issue": "TRACK-7"}, cwd=tmp_path)
    assert refused == run_hooks.HookResult(False, "run_start exited 3: another owner already holds this")

    absent = run_hooks.RunHooksConfig(commands={"start": (str(tmp_path / "no-such-command"),)})
    assert "run_start could not start" in run_hooks.run_hook(absent, "start", values, cwd=tmp_path).detail

    slow = run_hooks.RunHooksConfig(
        commands={"start": (sys.executable, "-c", "import time; time.sleep(30)")}, timeout_seconds=0.2
    )
    assert run_hooks.run_hook(slow, "start", values, cwd=tmp_path).detail == "run_start timed out after 0.2s"


# ---------------------------------------------------------------------------
# Lifecycle wiring
# ---------------------------------------------------------------------------


def test_start_hook_runs_before_the_run_exists_then_outcome_is_announced(repo: Path, monkeypatch):
    seen = stub_workflow(monkeypatch, "waiting-for-input", input_question="Which database?")
    assert orch.cmd_run(implement_args()) == 2
    (run,) = seen
    start, needs_input = hook_log(repo)
    assert start["argv"] == ["start", "feature", run.run_id, run.branch]
    assert start["env"]["SPEC_HOOK_FRONTMATTER_ISSUE"] == "TRACK-7"
    assert start["env"]["SPEC_HOOK_STATE"] == "pending"
    assert needs_input["argv"][0] == "needs_input"
    assert needs_input["env"]["SPEC_HOOK_STATE"] == "waiting-for-input"
    assert "needs input: Which database?" in needs_input["message"]


def test_failing_start_hook_stops_the_run_before_any_work(repo: Path, monkeypatch, capsys):
    seen = stub_workflow(monkeypatch, "passed")
    (repo / "fail-start").touch()
    assert orch.cmd_run(implement_args()) == 1
    assert seen == []
    assert events(repo) == ["start"]
    assert "Run refused by the start hook: run_start exited 3: another owner already holds this" in capsys.readouterr().err
    assert orch.RunState.list_for_spec(repo, "feature") == []

    # Nothing was left behind, so the next attempt asks again and can start.
    (repo / "fail-start").unlink()
    assert orch.cmd_run(implement_args()) == 0
    assert events(repo) == ["start", "start", "finished"]


def test_start_hook_needing_missing_frontmatter_refuses_the_run(repo: Path, monkeypatch, capsys):
    write_hook_config(repo, ("start",), extra_args=("{frontmatter_issue}",))
    write_spec(repo, frontmatter="")
    seen = stub_workflow(monkeypatch, "passed")
    assert orch.cmd_run(implement_args()) == 1
    assert seen == []
    assert "the spec's frontmatter does not set issue" in capsys.readouterr().err


def test_later_hook_failures_are_reported_and_keep_the_run(repo: Path, monkeypatch, capsys):
    stub_workflow(monkeypatch, "blocked", last_error="tests need a database")
    (repo / "fail-blocked").touch()
    assert orch.cmd_run(implement_args()) == 2
    assert events(repo) == ["start", "blocked"]
    assert "is blocked: tests need a database" in hook_log(repo)[-1]["message"]
    assert "warning: lifecycle hook failed: run_blocked exited 3" in capsys.readouterr().err
    (run,) = orch.RunState.list_for_spec(repo, "feature")
    assert run.status == "blocked"


def test_resumed_fires_when_a_paused_run_continues(repo: Path, monkeypatch):
    stub_workflow(monkeypatch, "blocked", last_error="stuck")
    assert orch.cmd_run(implement_args()) == 2
    assert events(repo) == ["start", "blocked"]

    stub_workflow(monkeypatch, "passed")
    assert orch.cmd_run(implement_args()) == 0
    assert events(repo) == ["start", "blocked", "resumed", "finished"]
    run_ids = {entry["env"]["SPEC_HOOK_RUN_ID"] for entry in hook_log(repo)}
    assert len(run_ids) == 1


def test_superseded_run_is_abandoned_before_the_new_run_starts(repo: Path, monkeypatch):
    stub_workflow(monkeypatch, "failed")
    assert orch.cmd_run(implement_args()) == 1
    stub_workflow(monkeypatch, "passed")
    assert orch.cmd_run(implement_args(run="new")) == 0
    first, abandoned, second, finished = hook_log(repo)
    assert [entry["env"]["SPEC_HOOK_EVENT"] for entry in (first, abandoned, second, finished)] == [
        "start",
        "abandoned",
        "start",
        "finished",
    ]
    old, new = first["env"]["SPEC_HOOK_RUN_ID"], second["env"]["SPEC_HOOK_RUN_ID"]
    assert old != new
    assert abandoned["env"]["SPEC_HOOK_RUN_ID"] == old
    assert f"superseded by run {new}" in abandoned["message"]
    assert finished["env"]["SPEC_HOOK_RUN_ID"] == new


def test_abandoned_fires_once_and_only_for_announced_unfinished_runs(repo: Path):
    runs = []
    for run_id, marker in (("feature-a", "start"), ("feature-b", "finished"), ("feature-c", "")):
        run = orch.RunState(run_id=run_id, spec_id="feature", branch=f"code/{run_id}", status="failed")
        run.save(repo)
        if marker:
            run_hooks.record_event(repo / ".spec-state" / "runs" / run_id, marker)
        runs.append(run)
    orch.fire_abandoned_run_hooks(repo, runs, "cleaned up without merging")
    orch.fire_abandoned_run_hooks(repo, runs, "cleaned up without merging")
    (entry,) = hook_log(repo)
    assert entry["argv"] == ["abandoned", "feature", "feature-a", "code/feature-a"]
    assert "was abandoned: cleaned up without merging" in entry["message"]


def test_refused_task_run_is_marked_abandoned(repo: Path, monkeypatch, capsys):
    monkeypatch.setattr(orch, "require_host_agent_available", lambda agent: None)
    seen = stub_workflow(monkeypatch, "passed")
    (repo / "fail-start").touch()
    assert orch.cmd_task(argparse.Namespace(agent="codex", base="origin/main")) == 1
    assert seen == []
    assert "Run refused by the start hook" in capsys.readouterr().err
    (entry,) = hook_log(repo)
    assert entry["env"]["SPEC_HOOK_RUN_MODE"] == "task"
    (state,) = (repo / ".spec-state" / "runs").glob("*.json")
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "abandoned"


def test_repositories_without_hooks_record_nothing(repo: Path, monkeypatch):
    (repo / ".spec.toml").write_text("[specs]\nrequire_merged_spec = false\n", encoding="utf-8")
    stub_workflow(monkeypatch, "passed")
    assert orch.cmd_run(implement_args()) == 0
    assert hook_log(repo) == []
    assert not list((repo / ".spec-state" / "runs").rglob("lifecycle-event"))
