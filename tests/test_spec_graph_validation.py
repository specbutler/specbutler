"""Spec dependency DAG validation: unknown ``depends_on`` ids and cycles.

Covers the pure validator in ``spec_metadata`` and how autopilot dispatch and
``spec list`` surface invalid specs instead of waiting forever or crashing.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runtime import autopilot, cli
from spec_runtime.config import load_repo_spec_runtime_config
from spec_runtime.spec_metadata import SpecMetadata, iter_spec_metadata, validate_spec_graph


def _meta(spec_id: str, *depends_on: str, obsolete: bool = False) -> SpecMetadata:
    return SpecMetadata(
        spec_id=spec_id,
        obsolete=obsolete,
        depends_on=tuple(depends_on),
        description="",
        superseded_by="",
        area="backend",
        priority=50,
        intake_required=False,
        body="",
    )


def _details(problems, spec_id: str) -> list[str]:
    return [problem.detail for problem in problems.get(spec_id, ())]


class TestValidateSpecGraph:
    def test_valid_dag_has_no_problems(self):
        specs = [_meta("base"), _meta("left", "base"), _meta("right", "base"), _meta("top", "left", "right")]
        assert validate_spec_graph(specs) == {}

    def test_unknown_dependency_is_reported(self):
        problems = validate_spec_graph([_meta("a", "missing-spec")])
        assert [problem.kind for problem in problems["a"]] == ["unknown-dependency"]
        assert _details(problems, "a") == ["depends on unknown spec 'missing-spec'"]

    def test_merged_and_task_spec_dependencies_are_known(self):
        specs = [_meta("a", "merged-and-removed", "some-task")]
        problems = validate_spec_graph(
            specs,
            merged_spec_ids={"merged-and-removed"},
            external_spec_ids={"some-task"},
        )
        assert problems == {}

    def test_obsolete_dependency_is_not_unknown(self):
        specs = [_meta("old", obsolete=True), _meta("a", "old")]
        assert validate_spec_graph(specs) == {}

    def test_two_node_cycle_reports_every_member(self):
        problems = validate_spec_graph([_meta("a", "b"), _meta("b", "a"), _meta("c", "a")])
        assert _details(problems, "a") == ["dependency cycle a -> b -> a"]
        assert _details(problems, "b") == ["dependency cycle b -> a -> b"]
        # ``c`` waits on a cyclic spec but is not itself part of the cycle.
        assert "c" not in problems

    def test_self_dependency_is_a_cycle(self):
        problems = validate_spec_graph([_meta("a", "a")])
        assert _details(problems, "a") == ["dependency cycle a -> a"]

    def test_longer_cycle_uses_shortest_path(self):
        specs = [_meta("a", "b"), _meta("b", "c"), _meta("c", "a", "d"), _meta("d")]
        problems = validate_spec_graph(specs)
        assert _details(problems, "a") == ["dependency cycle a -> b -> c -> a"]
        assert set(problems) == {"a", "b", "c"}

    def test_merged_spec_breaks_cycle(self):
        problems = validate_spec_graph([_meta("a", "b"), _meta("b", "a")], merged_spec_ids={"b"})
        assert problems == {}

    def test_unknown_and_cycle_problems_combine(self):
        problems = validate_spec_graph([_meta("a", "a", "ghost")])
        assert [problem.kind for problem in problems["a"]] == ["unknown-dependency", "dependency-cycle"]


class TestComputeUnlockCounts:
    def test_cycle_does_not_recurse_forever(self):
        specs = [_meta("a", "b"), _meta("b", "a"), _meta("c", "a")]
        counts = autopilot.compute_unlock_counts(specs)
        assert counts == {"a": 2, "b": 2, "c": 0}

    def test_diamond_counts_unique_descendants(self):
        specs = [_meta("base"), _meta("left", "base"), _meta("right", "base"), _meta("top", "left", "right")]
        counts = autopilot.compute_unlock_counts(specs)
        assert counts["base"] == 3
        assert counts["left"] == 1
        assert counts["top"] == 0


# ---------------------------------------------------------------------------
# Repository-backed dispatch and listing
# ---------------------------------------------------------------------------


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "specs" / "tasks").mkdir(parents=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _run_git("init", cwd=work)
    _run_git("config", "user.email", "test@test.com", cwd=work)
    _run_git("config", "user.name", "Test", cwd=work)
    _run_git("remote", "add", "origin", str(remote), cwd=work)
    (work / "README.md").write_text("hello\n")
    _run_git("add", "README.md", cwd=work)
    _run_git("commit", "-m", "init", cwd=work)
    _run_git("branch", "-M", "master", cwd=work)
    _run_git("push", "-u", "origin", "master", cwd=work)
    return work


def _write_spec(repo: Path, spec_id: str, depends_on: list[str], *, subdir: str = "specs") -> None:
    deps = ", ".join(depends_on)
    (repo / subdir / f"{spec_id}.md").write_text(
        f"---\nid: {spec_id}\ndepends_on: [{deps}]\narea: backend\n"
        f"description: {spec_id} spec\n---\n\n## Acceptance Criteria\n1. works\n"
    )


def _write_invalid_graph(repo: Path) -> None:
    _write_spec(repo, "healthy", [])
    _write_spec(repo, "ghost-dep", ["does-not-exist"])
    _write_spec(repo, "cycle-a", ["cycle-b"])
    _write_spec(repo, "cycle-b", ["cycle-a"])
    _write_spec(repo, "on-task", ["some-task"])
    _write_spec(repo, "some-task", [], subdir="specs/tasks")


class TestDispatchSkipsInvalidGraph:
    def test_skip_reasons_cover_unknown_and_cycles(self, repo: Path):
        _write_invalid_graph(repo)
        reasons = autopilot.spec_graph_skip_reasons(repo, iter_spec_metadata(repo))
        assert reasons == {
            "ghost-dep": "depends on unknown spec 'does-not-exist'",
            "cycle-a": "dependency cycle cycle-a -> cycle-b -> cycle-a",
            "cycle-b": "dependency cycle cycle-b -> cycle-a -> cycle-b",
        }

    def test_build_dispatch_queue_skips_invalid_specs_without_crashing(self, repo: Path):
        _write_invalid_graph(repo)
        queue = autopilot.build_dispatch_queue(repo)
        spec_ids = {item.spec_id for item in queue}
        assert "healthy" in spec_ids
        assert not spec_ids & {"ghost-dep", "cycle-a", "cycle-b"}


    @pytest.mark.requires_ps
    def test_run_loop_logs_skip_reason(self, repo: Path, monkeypatch, capsys):
        _write_invalid_graph(repo)
        args = argparse.Namespace(
            repo_root=str(repo),
            concurrency=1,
            poll_interval=1,
            notify=[],
            notify_backends=[],
            notify_success=False,
            dry_run=True,
            agent="",
        )
        monkeypatch.setattr(autopilot, "resolve_repo_root", lambda _repo_root: repo)
        monkeypatch.setattr(autopilot, "ensure_pid_file", lambda _repo_root: None)
        monkeypatch.setattr(autopilot, "remove_pid_file", lambda _repo_root: None)
        monkeypatch.setattr(autopilot, "adopt_active_processes", lambda _repo_root: {})
        monkeypatch.setattr(autopilot, "refresh_runtime_git_refs", lambda _repo_root: (True, ""))
        monkeypatch.setattr(
            autopilot,
            "collect_git_spec_state",
            lambda _repo_root: SimpleNamespace(merged_specs=frozenset()),
        )
        monkeypatch.setattr(autopilot, "load_run_record_index", lambda _repo_root: autopilot.RunRecordIndex())
        monkeypatch.setattr(
            autopilot,
            "fetch_coordinator_lease_snapshot",
            lambda _repo_root: autopilot.CoordinatorLeaseSnapshot(),
        )
        monkeypatch.setattr(autopilot, "build_dispatch_queue", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(autopilot.signal, "signal", lambda *_args, **_kwargs: None)

        assert autopilot.run_loop(args) == 0

        out = capsys.readouterr().out
        assert (
            "skip: ghost-dep has invalid depends_on and will not be dispatched: "
            "depends on unknown spec 'does-not-exist'"
        ) in out
        assert "skip: cycle-a has invalid depends_on and will not be dispatched: dependency cycle" in out
        assert "skip: healthy" not in out
        assert "skip: on-task" not in out


class TestSpecListSurfacesGraphProblems:
    def test_list_prints_dependency_problems(self, repo: Path, capsys):
        _write_invalid_graph(repo)
        with (
            patch.object(cli, "_resolve_repo_root", return_value=repo),
            patch.object(cli, "_lazy_config", return_value=load_repo_spec_runtime_config(repo)),
            patch("spec_runtime.control_plane.run_git_fetch_with_timeout"),
        ):
            assert cli._cmd_list(type("Args", (), {"all": False})()) == 0

        out = capsys.readouterr().out
        assert "Dependency problems (these specs will not be dispatched):" in out
        assert "  ghost-dep: depends on unknown spec 'does-not-exist'" in out
        assert "  cycle-a: dependency cycle cycle-a -> cycle-b -> cycle-a" in out
        assert "on-task:" not in out.split("Dependency problems")[1]

    def test_list_omits_section_for_valid_graph(self, repo: Path, capsys):
        _write_spec(repo, "healthy", [])
        with (
            patch.object(cli, "_resolve_repo_root", return_value=repo),
            patch.object(cli, "_lazy_config", return_value=load_repo_spec_runtime_config(repo)),
            patch("spec_runtime.control_plane.run_git_fetch_with_timeout"),
        ):
            assert cli._cmd_list(type("Args", (), {"all": False})()) == 0

        assert "Dependency problems" not in capsys.readouterr().out
