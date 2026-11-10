"""Spec hold: ``spec/hold/<id>`` tags keep a spec out of dispatch.

All tests use a local bare repository as ``origin``; nothing touches the
network or a model provider.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runtime import autopilot, cli
from spec_runtime import orchestrator as orch
from spec_runtime.config import load_repo_spec_runtime_config
from spec_runtime.spec_hold_tags import (
    NO_REASON_RECORDED,
    HoldTag,
    parse_hold_message,
    read_hold_tag,
    read_hold_tags,
    refresh_hold_tags,
)
from spec_runtime.spec_status import VALID_STATUSES, collect_git_spec_state, get_spec_status

# Hold commands, refreshes and dependency checks run ``git fetch`` through the
# process supervisor, which inspects launched processes with ``ps``.
pytestmark = pytest.mark.requires_ps


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def write_spec(root: Path, spec_id: str, *, depends_on: tuple[str, ...] = (), extra: str = "", subdir: str = "specs"):
    deps = ", ".join(depends_on)
    path = root / subdir / f"{spec_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {spec_id}\ndepends_on: [{deps}]\narea: backend\n{extra}"
        f"description: {spec_id} spec\n---\n\n## Goal\nDeliver it.\n\n## Acceptance Criteria\n1. works\n",
        encoding="utf-8",
    )


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    return remote


@pytest.fixture()
def repo(tmp_path: Path, origin: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "remote", "add", "origin", str(origin))
    (work / ".spec.toml").write_text('base_ref = "origin/main"\npr_base_branch = "main"\n', encoding="utf-8")
    write_spec(work, "alpha")
    write_spec(work, "beta", depends_on=("alpha",))
    write_spec(work, "gamma")
    write_spec(work, "old", extra="obsolete: true\n")
    write_spec(work, "chore", subdir="specs/tasks")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    monkeypatch.setenv("SPEC_ACTOR", "hold-tester")
    monkeypatch.setattr(orch, "BASE_REF", "origin/main")
    monkeypatch.setattr(orch, "PR_BASE_BRANCH", "main")
    monkeypatch.setattr(orch, "get_forge_adapter", lambda: SimpleNamespace(is_spec_merged=lambda *a, **kw: False))
    return work


@pytest.fixture()
def use_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI handlers at ``repo``."""
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_resolve_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "_lazy_config", lambda: load_repo_spec_runtime_config(repo))
    monkeypatch.setattr(orch, "resolve_repo_root", lambda: repo)
    return repo


def hold(spec_id: str, reason: str) -> int:
    return cli._cmd_hold(argparse.Namespace(spec_id=spec_id, reason=reason))


def unhold(spec_id: str) -> int:
    return cli._cmd_unhold(argparse.Namespace(spec_id=spec_id))


def remote_tags(origin: Path) -> str:
    return git(origin, "tag", "--list")


def tag_message(root: Path, tag: str) -> dict:
    return json.loads(git(root, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}"))


def merge_tag(root: Path, spec_id: str) -> None:
    git(root, "tag", f"spec/merged/{spec_id}", "main")
    git(root, "push", "origin", f"refs/tags/spec/merged/{spec_id}")
    git(root, "fetch", "origin")


def write_run(root: Path, spec_id: str, *, status: str, phase: str = "implement", run_id: str = "") -> str:
    run_id = run_id or f"{spec_id}-20260101T000000000000"
    runs = root / ".spec-state" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "spec_id": spec_id,
                "branch": f"code/{spec_id}--abc",
                "status": status,
                "phase": phase,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return run_id


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


class TestHoldTagParsing:
    def test_tolerant_parse_of_non_json_message(self):
        assert parse_hold_message("alpha", "waiting on vendor") == HoldTag(spec_id="alpha")
        assert parse_hold_message("alpha", "[1, 2]").display_reason == NO_REASON_RECORDED

    def test_lightweight_and_non_json_tags_hold_without_reason(self, repo: Path):
        git(repo, "tag", "spec/hold/alpha")
        git(repo, "tag", "-a", "spec/hold/gamma", "-m", "plain text, not json")
        holds = read_hold_tags(repo)
        assert set(holds) == {"alpha", "gamma"}
        assert holds["alpha"].display_reason == NO_REASON_RECORDED
        assert holds["gamma"].display_reason == NO_REASON_RECORDED
        assert get_spec_status(repo, "alpha") == "held"


# ---------------------------------------------------------------------------
# spec hold / spec unhold
# ---------------------------------------------------------------------------


class TestHoldCommands:
    def test_hold_creates_and_pushes_annotated_json_tag(self, use_repo: Path, origin: Path, capsys):
        assert hold("alpha", "waiting on the billing switch") == 0
        out = capsys.readouterr().out
        assert "alpha" in out and "waiting on the billing switch" in out and "spec/hold/alpha" in out

        payload = tag_message(use_repo, "spec/hold/alpha")
        assert payload["schema_version"] == 1
        assert payload["spec_id"] == "alpha"
        assert payload["reason"] == "waiting on the billing switch"
        assert payload["actor"] == "hold-tester"
        assert payload["timestamp"].endswith("Z")
        assert git(use_repo, "cat-file", "-t", "refs/tags/spec/hold/alpha").strip() == "tag"
        assert git(use_repo, "rev-parse", "spec/hold/alpha^{commit}") == git(use_repo, "rev-parse", "origin/main")
        assert "spec/hold/alpha" in remote_tags(origin)

    def test_hold_replaces_existing_hold(self, use_repo: Path, origin: Path, capsys):
        assert hold("alpha", "first reason") == 0
        capsys.readouterr()
        assert hold("alpha", "second reason") == 0
        out = capsys.readouterr().out
        assert "first reason" in out
        assert tag_message(use_repo, "spec/hold/alpha")["reason"] == "second reason"
        assert json.loads(git(origin, "for-each-ref", "--format=%(contents)", "refs/tags/spec/hold/alpha"))[
            "reason"
        ] == "second reason"

    @pytest.mark.parametrize(
        ("spec_id", "message"),
        [("nope", "Unknown spec"), ("chore", "task spec"), ("old", "obsolete")],
    )
    def test_hold_refuses_unknown_task_and_obsolete(self, use_repo: Path, origin: Path, capsys, spec_id, message):
        assert hold(spec_id, "reason") != 0
        assert message in capsys.readouterr().err
        assert read_hold_tag(use_repo, spec_id) is None
        assert "spec/hold/" not in remote_tags(origin)

    def test_hold_refuses_merged_spec(self, use_repo: Path, origin: Path, capsys):
        merge_tag(use_repo, "alpha")
        assert hold("alpha", "reason") != 0
        assert "already merged" in capsys.readouterr().err
        assert read_hold_tag(use_repo, "alpha") is None

    def test_hold_requires_non_blank_reason(self, use_repo: Path, capsys):
        assert hold("alpha", "   ") != 0
        assert "--reason" in capsys.readouterr().err
        assert read_hold_tag(use_repo, "alpha") is None

    def test_hold_push_failure_keeps_local_tag(self, use_repo: Path, origin: Path, monkeypatch, capsys):
        real_run_git = cli.run_git

        def failing_push(args, **kwargs):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="remote rejected")
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(cli, "run_git", failing_push)
        assert hold("alpha", "reason") != 0
        err = capsys.readouterr().err
        assert "not shared yet" in err and "git push origin" in err
        assert read_hold_tag(use_repo, "alpha") is not None
        assert "spec/hold/alpha" not in remote_tags(origin)

    def test_unhold_deletes_remote_then_local(self, use_repo: Path, origin: Path, capsys):
        assert hold("alpha", "reason") == 0
        assert unhold("alpha") == 0
        assert read_hold_tag(use_repo, "alpha") is None
        assert "spec/hold/alpha" not in remote_tags(origin)

    def test_unhold_is_noop_when_not_held(self, use_repo: Path, capsys):
        assert unhold("alpha") == 0
        assert "not held" in capsys.readouterr().out

    def test_unhold_keeps_local_tag_when_remote_delete_fails(self, use_repo: Path, monkeypatch, capsys):
        assert hold("alpha", "reason") == 0
        real_run_git = cli.run_git

        def failing_push(args, **kwargs):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(cli, "run_git", failing_push)
        assert unhold("alpha") != 0
        assert read_hold_tag(use_repo, "alpha") is not None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_drops_hold_removed_by_another_clone(self, use_repo: Path, origin: Path, tmp_path, monkeypatch):
        assert hold("alpha", "reason") == 0
        git(use_repo, "tag", "local-only-tag")
        git(use_repo, "tag", "spec/merged/local-only")

        other = tmp_path / "other"
        git(tmp_path, "clone", str(origin), str(other))
        git(other, "config", "user.email", "o@example.com")
        git(other, "config", "user.name", "Other")
        assert read_hold_tag(other, "alpha") is not None
        monkeypatch.setattr(cli, "_resolve_repo_root", lambda: other)
        monkeypatch.setattr(cli, "_lazy_config", lambda: load_repo_spec_runtime_config(other))
        assert unhold("alpha") == 0

        # A plain --tags --prune fetch does not prune tags.
        git(use_repo, "fetch", "--quiet", "--tags", "--prune", "origin")
        assert read_hold_tag(use_repo, "alpha") is not None

        assert autopilot.refresh_runtime_git_refs(use_repo) == (True, "")
        assert read_hold_tag(use_repo, "alpha") is None
        tags = git(use_repo, "tag", "--list").split()
        assert "local-only-tag" in tags and "spec/merged/local-only" in tags

    def test_refresh_failure_is_reported(self, repo: Path):
        git(repo, "remote", "remove", "origin")
        ok, error = refresh_hold_tags(repo)
        assert not ok
        assert "hold tags could not be fetched" in error

    def test_list_refresh_picks_up_remote_hold(self, use_repo: Path, origin: Path, tmp_path, monkeypatch, capsys):
        other = tmp_path / "other"
        git(tmp_path, "clone", str(origin), str(other))
        git(other, "tag", "-a", "spec/hold/gamma", "-m", json.dumps({"reason": "elsewhere"}))
        git(other, "push", "origin", "refs/tags/spec/hold/gamma")
        assert cli._cmd_list(argparse.Namespace(all=False)) == 0
        assert "gamma: elsewhere" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_held_is_a_valid_status(self):
        assert "held" in VALID_STATUSES

    def test_git_state_carries_holds(self, use_repo: Path):
        assert hold("alpha", "vendor switch") == 0
        state = collect_git_spec_state(use_repo)
        assert state.held_specs == frozenset({"alpha"})
        assert state.hold_for("alpha").reason == "vendor switch"
        assert get_spec_status(use_repo, "alpha", git_state=state) == "held"

    @pytest.mark.parametrize("run_status", ["running", "failed"])
    def test_held_outranks_in_progress_and_needs_attention(self, use_repo: Path, run_status: str):
        write_run(use_repo, "alpha", status=run_status)
        assert get_spec_status(use_repo, "alpha") in {"in-progress", "needs-attention", "retryable"}
        assert hold("alpha", "reason") == 0
        assert get_spec_status(use_repo, "alpha") == "held"
        assert get_spec_status(use_repo, "alpha", git_state=collect_git_spec_state(use_repo)) == "held"

    def test_held_outranks_needs_attention_from_intake(self, use_repo: Path):
        write_spec(use_repo, "intake-spec", extra="intake:\n  required: true\n  questions:\n    - id: q\n      prompt: q?\n")
        git(use_repo, "add", ".")
        git(use_repo, "commit", "-m", "intake spec")
        git(use_repo, "push", "origin", "main")
        before = get_spec_status(use_repo, "intake-spec")
        assert hold("intake-spec", "reason") == 0
        assert get_spec_status(use_repo, "intake-spec") == "held"
        assert before != "held"

    def test_merged_and_obsolete_outrank_held(self, use_repo: Path):
        git(use_repo, "tag", "spec/hold/old")
        assert get_spec_status(use_repo, "old") == "obsolete"
        assert hold("alpha", "reason") == 0
        merge_tag(use_repo, "alpha")
        assert get_spec_status(use_repo, "alpha") == "merged"

    def test_spec_list_shows_held_status_and_section(self, use_repo: Path, capsys):
        assert hold("gamma", "manual build in progress") == 0
        capsys.readouterr()
        assert cli._cmd_list(argparse.Namespace(all=False)) == 0
        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if line.startswith("gamma "))
        assert " held " in row
        assert "Held (not dispatched):" in out
        section = out.split("Held (not dispatched):", 1)[1]
        assert "gamma: manual build in progress (by hold-tester at " in section

    def test_spec_status_prints_hold_line(self, use_repo: Path, capsys):
        assert hold("alpha", "waiting on legal") == 0
        capsys.readouterr()
        assert orch.cmd_status(argparse.Namespace(spec="alpha", run="")) == 0
        out = capsys.readouterr().out
        assert "Status: held" in out
        assert "Hold: waiting on legal (by hold-tester at " in out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dry_run_args(repo: Path) -> argparse.Namespace:
    return argparse.Namespace(
        repo_root=str(repo),
        concurrency=1,
        poll_interval=1,
        notify=[],
        notify_backends=[],
        notify_success=False,
        dry_run=True,
        agent="",
    )


class TestDispatch:
    def test_held_new_run_is_not_queued(self, use_repo: Path):
        assert "gamma" in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}
        assert hold("gamma", "reason") == 0
        assert "gamma" not in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}

    def test_held_resumable_run_is_not_queued(self, use_repo: Path, monkeypatch):
        monkeypatch.setattr(
            autopilot,
            "resolve_resume_run",
            lambda _root, spec, **_kw: ("gamma-run", "resume-run") if spec.spec_id == "gamma" else ("", ""),
        )
        queue = autopilot.build_dispatch_queue(use_repo)
        assert any(item.spec_id == "gamma" and item.reason == "resume-run" for item in queue)
        assert hold("gamma", "reason") == 0
        assert "gamma" not in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}

    def test_lightweight_hold_tag_blocks_dispatch(self, use_repo: Path):
        git(use_repo, "tag", "spec/hold/gamma")
        assert "gamma" not in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}

    def test_dry_run_lists_held_specs_and_logs_skip(self, use_repo: Path, monkeypatch, capsys):
        assert hold("gamma", "manual build") == 0
        # A hand-made lightweight hold, shared through origin (the run loop's
        # refresh prunes hold tags that origin does not have).
        git(use_repo, "tag", "spec/hold/alpha")
        git(use_repo, "push", "origin", "refs/tags/spec/hold/alpha")
        capsys.readouterr()
        monkeypatch.setattr(autopilot, "resolve_repo_root", lambda _repo_root: use_repo)
        monkeypatch.setattr(autopilot, "ensure_pid_file", lambda _repo_root: None)
        monkeypatch.setattr(autopilot, "remove_pid_file", lambda _repo_root: None)
        monkeypatch.setattr(autopilot, "adopt_active_processes", lambda _repo_root: {})
        monkeypatch.setattr(autopilot, "available_memory_bytes", lambda: 8 * 1024 * 1024 * 1024)
        monkeypatch.setattr(autopilot.signal, "signal", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(autopilot, "build_dispatch_queue", lambda *_a, **_kw: [])

        assert autopilot.run_loop(dry_run_args(use_repo)) == 0
        out = capsys.readouterr().out
        assert "Queue: empty" in out
        assert "skip: gamma is held and will not be dispatched: manual build" in out
        assert f"skip: alpha is held and will not be dispatched: {NO_REASON_RECORDED}" in out
        held_section = out.split("Held (not dispatched):", 1)[1]
        assert out.index("Queue: empty") < out.index("Held (not dispatched):")
        assert "gamma: manual build" in held_section
        assert f"alpha: {NO_REASON_RECORDED}" in held_section

    def test_render_held_specs(self):
        rendered = autopilot.render_held_specs({"a": HoldTag("a", "r", "me", "2026-01-01T00:00:00Z")})
        assert rendered == "Held (not dispatched):\n  a: r (by me at 2026-01-01T00:00:00Z)"


# ---------------------------------------------------------------------------
# Dependents
# ---------------------------------------------------------------------------


class TestDependents:
    def test_dependent_of_held_spec_is_not_dispatched(self, use_repo: Path):
        assert hold("alpha", "reason") == 0
        state = collect_git_spec_state(use_repo)
        assert not autopilot.dependencies_are_merged(use_repo, ("alpha",), git_state=state)
        queue = {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}
        assert "alpha" not in queue
        assert "beta" not in queue

    def test_check_dependencies_reports_held_reason(self, use_repo: Path):
        assert hold("alpha", "vendor switch") == 0
        assert orch.check_dependencies_merged(use_repo, "beta") == ["alpha (held: vendor switch)"]
        git(use_repo, "tag", "-f", "spec/hold/alpha")
        assert orch.check_dependencies_merged(use_repo, "beta") == [f"alpha (held: {NO_REASON_RECORDED})"]

    def test_unhold_leaves_dependent_blocked_until_merge(self, use_repo: Path):
        assert hold("alpha", "reason") == 0
        assert "beta" not in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}
        assert unhold("alpha") == 0
        assert "beta" not in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}
        assert orch.check_dependencies_merged(use_repo, "beta") == ["alpha (not-started)"]
        merge_tag(use_repo, "alpha")
        assert "beta" in {item.spec_id for item in autopilot.build_dispatch_queue(use_repo)}
        assert orch.check_dependencies_merged(use_repo, "beta") == []


# ---------------------------------------------------------------------------
# spec implement
# ---------------------------------------------------------------------------


class _ReachedWorkflow(RuntimeError):
    pass


def implement_args(spec_id: str, **overrides) -> argparse.Namespace:
    values = dict(
        spec=spec_id,
        agent="claude",
        review_agent="claude",
        base=None,
        run="",
        branch="",
        coordination_bypass=False,
        reset_intake=False,
        retry_cap=None,
        ignore_hold=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class TestImplement:
    @pytest.fixture()
    def stub_workflow(self, monkeypatch):
        def reached(*_args, **_kwargs):
            raise _ReachedWorkflow("reached workflow start")

        monkeypatch.setattr(orch, "SpecLock", reached)

    def test_held_spec_is_refused_without_ignore_hold(self, use_repo: Path, stub_workflow, capsys):
        assert hold("alpha", "waiting on legal") == 0
        capsys.readouterr()
        assert orch.cmd_run(implement_args("alpha")) == 1
        err = capsys.readouterr().err
        assert "held: waiting on legal" in err
        assert "spec unhold alpha" in err
        assert "--ignore-hold" in err
        assert "reached workflow start" not in err
        assert not (use_repo / ".spec-state" / "runs").exists()

    def test_held_resume_is_refused(self, use_repo: Path, stub_workflow, capsys):
        run_id = write_run(use_repo, "alpha", status="failed")
        assert hold("alpha", "reason") == 0
        capsys.readouterr()
        assert orch.cmd_run(implement_args("alpha", run=run_id)) == 1
        assert "is held" in capsys.readouterr().err

    def test_ignore_hold_starts_and_keeps_tag(self, use_repo: Path, stub_workflow, capsys):
        assert hold("alpha", "reason") == 0
        capsys.readouterr()
        assert orch.cmd_run(implement_args("alpha", ignore_hold=True)) == 1
        err = capsys.readouterr().err
        assert "reached workflow start" in err
        assert "is held" not in err
        assert read_hold_tag(use_repo, "alpha") is not None

    def test_unheld_spec_starts(self, use_repo: Path, stub_workflow, capsys):
        assert orch.cmd_run(implement_args("alpha")) == 1
        assert "reached workflow start" in capsys.readouterr().err

    def test_implement_refreshes_hold_tags_first(self, use_repo: Path, origin: Path, tmp_path, stub_workflow, capsys):
        other = tmp_path / "other"
        git(tmp_path, "clone", str(origin), str(other))
        git(other, "tag", "-a", "spec/hold/alpha", "-m", json.dumps({"reason": "set elsewhere"}))
        git(other, "push", "origin", "refs/tags/spec/hold/alpha")
        assert orch.cmd_run(implement_args("alpha")) == 1
        assert "held: set elsewhere" in capsys.readouterr().err

    def test_ignore_hold_flag_is_parsed(self, use_repo: Path, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "_cmd_implement", lambda args: seen.setdefault("ignore_hold", args.ignore_hold) and 0)
        monkeypatch.setattr(cli, "_emit_startup_update_notice", lambda *_a, **_kw: None)
        cli.main(["implement", "--spec", "alpha", "--ignore-hold"])
        assert seen["ignore_hold"] is True
