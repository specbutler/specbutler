"""Tests for autopilot dispatch discipline (spec: autopilot-dispatch-discipline).

Covers the three observed dispatch-race shapes:
  * Back off exponentially on lock contention and surface the owner.
  * Never supersede a run whose branch has commits ahead of base.
  * Yield to a non-autopilot actor mid-flight (operator grace).

Pure-unit: git operations run in tmp_path repos, no network or subprocesses
beyond git and the process-identity probe.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spec_runtime import autopilot
from spec_runtime import orchestrator as orch
from spec_runtime.control_plane import save_run_lease
from spec_runtime.control_plane.lease import build_lease, lease_actor
from spec_runtime.spec_metadata import iter_spec_metadata


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "specs").mkdir(exist_ok=True)
    remote = work.parent / "remote.git"
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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    _init_repo(work)
    return work


# ---------------------------------------------------------------------------
# AC1/AC5 — exponential backoff scheduling on lock contention
# ---------------------------------------------------------------------------


class TestLockContentionBackoff:
    def test_exponential_backoff_schedule(self):
        tracker = autopilot.LockContentionTracker(base_backoff=30.0, max_backoff=1800.0)

        first = tracker.record_locked("spec-a", "pid-100", "pid=100", now=0.0)
        assert first.should_log is True  # first observation -> one log line
        assert first.backoff_seconds == 30.0

        # Re-observing inside the window neither escalates nor re-logs.
        same_window = tracker.record_locked("spec-a", "pid-100", "pid=100", now=10.0)
        assert same_window.escalated is False
        assert same_window.should_log is False

        # After the window elapses the backoff doubles: 30 -> 60 -> 120.
        second = tracker.record_locked("spec-a", "pid-100", "pid=100", now=30.0)
        assert second.backoff_seconds == 60.0
        assert second.should_log is False  # escalation is not a new state change
        third = tracker.record_locked("spec-a", "pid-100", "pid=100", now=90.0)
        assert third.backoff_seconds == 120.0

    def test_backoff_is_capped(self):
        tracker = autopilot.LockContentionTracker(base_backoff=30.0, max_backoff=100.0)
        now = 0.0
        last = tracker.record_locked("spec-a", "pid-1", "pid=1", now=now)
        for _ in range(10):
            now = last.backoff_until
            last = tracker.record_locked("spec-a", "pid-1", "pid=1", now=now)
        assert last.backoff_seconds == 100.0

    def test_new_owner_resets_schedule_and_relogs(self):
        tracker = autopilot.LockContentionTracker(base_backoff=30.0)
        tracker.record_locked("spec-a", "pid-1", "pid=1", now=0.0)
        tracker.record_locked("spec-a", "pid-1", "pid=1", now=30.0)  # escalate to 60
        changed = tracker.record_locked("spec-a", "pid-2", "pid=2", now=45.0)
        assert changed.should_log is True  # owner changed -> state change
        assert changed.backoff_seconds == 30.0

    def test_is_backing_off_window(self):
        tracker = autopilot.LockContentionTracker(base_backoff=30.0)
        tracker.record_locked("spec-a", "pid-1", "pid=1", now=0.0)
        assert tracker.is_backing_off("spec-a", now=10.0) is True
        assert tracker.is_backing_off("spec-a", now=31.0) is False


# ---------------------------------------------------------------------------
# AC1/AC5 — owner surfacing + recovery when the lock holder exits
# ---------------------------------------------------------------------------


class TestLockOwnerSurfacing:
    def test_spec_lock_records_and_reads_owner(self, repo: Path):
        assert orch.read_spec_lock_owner(repo, "my-feature") is None
        with orch.SpecLock(repo, "my-feature"):
            owner = orch.read_spec_lock_owner(repo, "my-feature")
            assert owner is not None
            assert owner.pid > 0
            assert "pid=" in owner.describe()
        # Recovery: once the holder exits (context released) the lock is free.
        assert orch.read_spec_lock_owner(repo, "my-feature") is None

    def test_contention_error_names_owner(self, repo: Path):
        with orch.SpecLock(repo, "my-feature"):
            with pytest.raises(RuntimeError) as excinfo:
                with orch.SpecLock(repo, "my-feature"):
                    pass
        assert "Lock contention" in str(excinfo.value)
        assert "held by" in str(excinfo.value)

    def test_render_queue_surfaces_locked_owner(self):
        candidate = autopilot.DispatchCandidate(
            spec_id="my-feature",
            agent="codex",
            area="backend",
            priority=10,
            unlock_count=0,
            status="not-started",
            lock_owner_pid=4321,
            lock_owner="pid=4321 started=Mon Jul 13 command=spec implement",
        )
        output = autopilot.render_queue(
            [candidate],
            lock_backoff_until={"my-feature": autopilot.time.monotonic() + 60},
        )
        assert "locked by pid=4321" in output
        assert "backoff=" in output

    def test_lock_tracker_recovers_when_holder_exits(self):
        tracker = autopilot.LockContentionTracker(base_backoff=30.0)
        tracker.record_locked("spec-a", "pid-1", "pid=1", now=0.0)
        assert tracker.is_tracked("spec-a") is True
        # Holder gone: the loop observes the lock is free and clears state.
        assert tracker.record_free("spec-a") is True
        assert tracker.is_tracked("spec-a") is False
        assert tracker.record_free("spec-a") is False


# ---------------------------------------------------------------------------
# AC2/AC5 — supersede refusal when commits exist ahead of base
# ---------------------------------------------------------------------------


class TestSupersedeRefusal:
    def _make_branch_with_commits(self, repo: Path, branch: str, commits: int) -> None:
        _run_git("checkout", "-b", branch, cwd=repo)
        for i in range(commits):
            (repo / f"impl-{i}.py").write_text(f"work {i}\n")
            _run_git("add", f"impl-{i}.py", cwd=repo)
            _run_git("commit", "-m", f"implement {i}", cwd=repo)
        _run_git("checkout", "master", cwd=repo)

    def test_branch_commits_ahead_counts(self, repo: Path):
        self._make_branch_with_commits(repo, "code/my-feature--run", commits=2)
        assert orch._branch_commits_ahead_of_base(repo, "code/my-feature--run", "master") == 2
        assert orch._branch_commits_ahead_of_base(repo, "code/my-feature--run", "origin/master") == 2

    def test_missing_branch_counts_zero(self, repo: Path):
        assert orch._branch_commits_ahead_of_base(repo, "code/absent--run", "master") == 0

    def test_failed_run_with_commits_is_resumed_not_superseded(self, repo: Path):
        branch = "code/my-feature--20260629T010544"
        self._make_branch_with_commits(repo, branch, commits=1)
        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            # An implement failure with a non-retryable message would otherwise
            # return None (supersede path) — but the branch holds committed work.
            phase="implement",
            status="failed",
            base_ref="master",
            last_error="deterministic non-retryable failure",
            last_failure_retryable=False,
        )
        run.save(repo)
        # The failed run carries committed work, so dispatch must resume it
        # (same run id) rather than return None and trigger a supersede.
        selected = orch._select_default_run(repo, "my-feature", ensure_identity=False)
        assert selected is not None
        assert selected.run_id == run.run_id

    def test_failed_container_run_finds_unpushed_commits_in_isolated_clone(self, repo: Path):
        branch = "code/my-feature--20260629T010544"
        workspace = repo.parent / "container-source"
        _run_git("clone", str(repo), str(workspace), cwd=repo.parent)
        _run_git("config", "user.email", "test@test.com", cwd=workspace)
        _run_git("config", "user.name", "Test", cwd=workspace)
        _run_git("checkout", "-b", branch, cwd=workspace)
        (workspace / "implementation.py").write_text("container-only work\n")
        _run_git("add", "implementation.py", cwd=workspace)
        _run_git("commit", "-m", "implement in isolated clone", cwd=workspace)

        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            worktree_path=str(workspace),
            phase="verify",
            status="failed",
            base_ref="origin/master",
            last_error="stopped by user",
            last_failure_retryable=False,
        )
        run.save(repo)

        # The orchestration checkout cannot see the unpushed container branch.
        assert orch._branch_commits_ahead_of_base(repo, branch, "origin/master") == 0
        assert orch._run_branch_has_committed_work(repo, run) is True
        selected = orch._select_default_run(repo, "my-feature", ensure_identity=False)
        assert selected is not None
        assert selected.run_id == run.run_id

    def test_failed_run_without_commits_still_supersedable(self, repo: Path):
        branch = "code/my-feature--empty"
        # Branch exists but points at base — no committed implementation work.
        _run_git("branch", branch, cwd=repo)
        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            phase="implement",
            status="failed",
            base_ref="master",
            last_error="deterministic non-retryable failure",
            last_failure_retryable=False,
        )
        run.save(repo)
        selected = orch._select_default_run(repo, "my-feature", ensure_identity=False)
        assert selected is None  # supersede path remains available


# ---------------------------------------------------------------------------
# AC3/AC5 — operator-grace yielding and reclaim
# ---------------------------------------------------------------------------


class TestOperatorGrace:
    def test_lease_records_actor(self):
        lease = build_lease(run_id="r1", spec_id="s1", phase="implement", actor="alice")
        assert lease_actor(lease) == "alice"

    def test_yields_within_grace_window(self):
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
        decision = autopilot.evaluate_operator_grace(
            actor="alice",
            touched_at=(now - timedelta(seconds=60)).isoformat(),
            now=now,
            grace_seconds=600.0,
            process_alive=True,
            lease_held=True,
        )
        assert decision.yield_to_operator is True
        assert decision.reason == "operator-grace"

    def test_autopilot_actor_never_grants_grace(self):
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
        decision = autopilot.evaluate_operator_grace(
            actor="autopilot",
            touched_at=now.isoformat(),
            now=now,
            grace_seconds=600.0,
            process_alive=True,
            lease_held=True,
        )
        assert decision.yield_to_operator is False

    def test_three_way_race_resolves_in_operator_favor(self):
        """Operator resume vs autopilot dispatch vs stale process.

        The operator just resumed (fresh touch), the operator process is alive
        and holds the run lease. Autopilot must yield — a separate stale process
        is irrelevant because it does not own the operator's fresh lease.
        """
        now = datetime(2026, 7, 11, 12, 0, 5, tzinfo=UTC)
        decision = autopilot.evaluate_operator_grace(
            actor="alice",
            touched_at=(now - timedelta(seconds=5)).isoformat(),
            now=now,
            grace_seconds=600.0,
            process_alive=True,
            lease_held=True,
        )
        assert decision.yield_to_operator is True

    def test_reclaims_when_operator_exited_and_lease_released(self):
        """Recovery: operator process gone AND lease not held -> reclaim."""
        now = datetime(2026, 7, 11, 12, 30, 0, tzinfo=UTC)
        decision = autopilot.evaluate_operator_grace(
            actor="alice",
            touched_at=(now - timedelta(seconds=30)).isoformat(),
            now=now,
            grace_seconds=600.0,
            process_alive=False,
            lease_held=False,
        )
        assert decision.yield_to_operator is False

    def test_holds_grace_while_lease_still_held_after_window(self):
        # Even past the window, an actively lease-held operator run is not stolen.
        now = datetime(2026, 7, 11, 13, 0, 0, tzinfo=UTC)
        decision = autopilot.evaluate_operator_grace(
            actor="alice",
            touched_at=(now - timedelta(seconds=3600)).isoformat(),
            now=now,
            grace_seconds=600.0,
            process_alive=True,
            lease_held=True,
        )
        assert decision.yield_to_operator is True

    def test_build_queue_annotates_operator_grace(self, repo: Path):
        """A live operator lease surfaces operator-grace in the dispatch queue."""
        (repo / "specs" / "my-feature.md").write_text(
            "---\nid: my-feature\ndepends_on: []\narea: backend\n"
            "description: A test spec\n---\n\n## Acceptance Criteria\n- [ ] x\n"
        )
        run = orch.RunState(
            run_id="my-feature-20260711T120000",
            spec_id="my-feature",
            branch="code/my-feature--20260711T120000",
            phase="implement",
            status="blocked",
            base_ref="master",
        )
        run.save(repo)
        runs_dir = autopilot.runs_dir(repo)
        lease = build_lease(
            run_id=run.run_id,
            spec_id=run.spec_id,
            phase="implement",
            actor="alice",
            process_pid=1,  # is_pid_alive gets patched below
        )
        save_run_lease(runs_dir, lease)

        now = datetime.now(UTC)
        from unittest.mock import patch

        with patch("spec_runtime.autopilot.is_pid_alive", return_value=True):
            queue = autopilot.build_dispatch_queue(
                repo,
                now=now,
                operator_grace_seconds=600.0,
            )
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        assert candidate.operator_grace is True
        assert candidate.operator_actor == "alice"

    def _spec_and_failed_run(self, repo: Path) -> orch.RunState:
        (repo / "specs" / "my-feature.md").write_text(
            "---\nid: my-feature\ndepends_on: []\narea: backend\n"
            "description: A test spec\n---\n\n## Acceptance Criteria\n- [ ] x\n"
        )
        run = orch.RunState(
            run_id="my-feature-20260711T120000",
            spec_id="my-feature",
            branch="code/my-feature--20260711T120000",
            phase="implement",
            status="failed",
            base_ref="master",
            last_error="agent crashed mid-implement",
        )
        run.save(repo)
        # A stale autopilot lease keeps the run dispatch-eligible while ensuring
        # the lease-actor grace path grants nothing (actor=autopilot).
        lease = build_lease(
            run_id=run.run_id,
            spec_id=run.spec_id,
            phase="implement",
            actor="autopilot",
            process_pid=999_999,
        )
        save_run_lease(autopilot.runs_dir(repo), lease)
        return run

    def test_build_queue_yields_after_operator_steer(self, repo: Path):
        """`spec steer` grants operator grace even though steering never touches
        the run lease. The run carries a stale ``actor=autopilot`` lease — so the
        lease-actor grace check grants nothing — yet an active operator steering
        inside the window must still make autopilot yield (AC3, the named
        ``spec steer`` trigger that keying grace on the lease alone would miss).
        """
        from unittest.mock import patch

        run = self._spec_and_failed_run(repo)
        now = datetime(2026, 7, 11, 12, 0, 30, tzinfo=UTC)
        steering = orch.OperatorSteering(
            message="focus on the parser",
            provided_by="alice",
            provided_at=(now - timedelta(seconds=15)).isoformat(),
            status="active",
        )
        steering.save(repo, run.run_id)

        with patch("spec_runtime.autopilot.is_pid_alive", return_value=False):
            queue = autopilot.build_dispatch_queue(
                repo, now=now, operator_grace_seconds=600.0
            )
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        assert candidate.operator_grace is True
        assert candidate.operator_actor == "alice"

    def test_consumed_or_stale_steer_does_not_yield(self, repo: Path):
        """A steering that autopilot already consumed (or that fell outside the
        grace window) must not grant grace — otherwise the run would be pinned
        forever after a single steer.
        """
        from unittest.mock import patch

        run = self._spec_and_failed_run(repo)
        now = datetime(2026, 7, 11, 12, 30, 0, tzinfo=UTC)
        # Active but well outside the grace window.
        orch.OperatorSteering(
            message="old guidance",
            provided_by="alice",
            provided_at=(now - timedelta(seconds=1200)).isoformat(),
            status="active",
        ).save(repo, run.run_id)

        with patch("spec_runtime.autopilot.is_pid_alive", return_value=False):
            queue = autopilot.build_dispatch_queue(
                repo, now=now, operator_grace_seconds=600.0
            )
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        assert candidate.operator_grace is False

        # Fresh touch, but already consumed by an autopilot attempt -> no grace.
        orch.OperatorSteering(
            message="applied guidance",
            provided_by="alice",
            provided_at=(now - timedelta(seconds=15)).isoformat(),
            status="consumed",
        ).save(repo, run.run_id)
        with patch("spec_runtime.autopilot.is_pid_alive", return_value=False):
            queue = autopilot.build_dispatch_queue(
                repo, now=now, operator_grace_seconds=600.0
            )
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        assert candidate.operator_grace is False


# ---------------------------------------------------------------------------
# AC2/AC5 — un-resumable run with committed work surfaces needs-attention
# (never supersede committed work; "if resume is not possible, the spec
#  surfaces needs-attention explaining what work exists and where")
# ---------------------------------------------------------------------------


class TestStrandedCommittedWork:
    def _make_branch_with_commits(self, repo: Path, branch: str, commits: int) -> None:
        _run_git("checkout", "-b", branch, cwd=repo)
        for i in range(commits):
            (repo / f"impl-{i}.py").write_text(f"work {i}\n")
            _run_git("add", f"impl-{i}.py", cwd=repo)
            _run_git("commit", "-m", f"implement {i}", cwd=repo)
        _run_git("checkout", "master", cwd=repo)

    def test_decision_no_commits_grants_no_attention(self):
        decision = autopilot.evaluate_stranded_committed_work(
            resumable=False,
            commits_ahead=0,
            run_id="r1",
            branch="code/x--1",
            base_ref="master",
            status="abandoned",
        )
        assert decision.needs_attention is False

    def test_decision_resumable_run_is_not_stranded(self):
        # Committed work that can still be resumed is handled by the resume
        # path, not surfaced as needs-attention.
        decision = autopilot.evaluate_stranded_committed_work(
            resumable=True,
            commits_ahead=3,
            run_id="r1",
            branch="code/x--1",
            base_ref="master",
            status="failed",
        )
        assert decision.needs_attention is False

    def test_decision_unresumable_with_commits_surfaces_work_location(self):
        decision = autopilot.evaluate_stranded_committed_work(
            resumable=False,
            commits_ahead=2,
            run_id="my-feature-20260629T010544",
            branch="code/my-feature--20260629T010544",
            base_ref="master",
            status="abandoned",
        )
        assert decision.needs_attention is True
        # Explains *what work exists and where* (AC2).
        assert "my-feature-20260629T010544" in decision.detail
        assert "code/my-feature--20260629T010544" in decision.detail
        assert "2 commit" in decision.detail
        assert "master" in decision.detail

    def test_abandoned_run_with_commits_precondition(self, repo: Path):
        """The un-resumable-with-commits state the guard protects against."""
        branch = "code/my-feature--20260629T010544"
        self._make_branch_with_commits(repo, branch, commits=1)
        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            phase="implement",
            status="abandoned",
            base_ref="master",
        )
        run.save(repo)
        # Not resumable (supersede path) yet carries committed work — exactly the
        # shape that must NOT be superseded.
        assert orch._select_default_run(repo, "my-feature", ensure_identity=False) is None
        assert orch._run_branch_has_committed_work(repo, run) is True

    def test_build_queue_surfaces_stranded_committed_work(self, repo: Path):
        """An abandoned run with commits ahead surfaces needs-attention in the
        dispatch queue instead of dispatching a fresh (superseding) run."""
        (repo / "specs" / "my-feature.md").write_text(
            "---\nid: my-feature\ndepends_on: []\narea: backend\n"
            "description: A test spec\n---\n\n## Acceptance Criteria\n- [ ] x\n"
        )
        branch = "code/my-feature--20260629T010544"
        self._make_branch_with_commits(repo, branch, commits=1)
        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            phase="implement",
            status="abandoned",
            base_ref="master",
        )
        run.save(repo)

        queue = autopilot.build_dispatch_queue(repo, now=datetime.now(UTC))
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        # No resume run selected -> would otherwise be a fresh, superseding run.
        assert candidate.run_id == ""
        assert candidate.stranded_commits_detail
        assert run.run_id in candidate.stranded_commits_detail
        assert branch in candidate.stranded_commits_detail

    def test_build_queue_resumes_failed_run_with_committed_work(self, repo: Path):
        """A failed run whose branch holds committed work is resumable via
        ``_select_default_run`` even though autopilot's ``resolve_resume_run``
        does not pick it (generic, non-handshake error). Dispatch must resume it
        (same run id, existing branch) rather than emit a fresh, superseding run.

        The failed run carries a (stale) autopilot lease so it projects as
        ``retryable`` and stays dispatch-eligible — the exact shape that would
        otherwise be queued as a fresh RUN=new that strands the commits.
        """
        from unittest.mock import patch

        (repo / "specs" / "my-feature.md").write_text(
            "---\nid: my-feature\ndepends_on: []\narea: backend\n"
            "description: A test spec\n---\n\n## Acceptance Criteria\n- [ ] x\n"
        )
        branch = "code/my-feature--20260629T010544"
        self._make_branch_with_commits(repo, branch, commits=1)
        run = orch.RunState(
            run_id="my-feature-20260629T010544341323",
            spec_id="my-feature",
            branch=branch,
            phase="implement",
            status="failed",
            base_ref="master",
            last_error="agent crashed mid-implement",
        )
        run.save(repo)
        runs_dir = autopilot.runs_dir(repo)
        # A leftover autopilot lease (dead process) keeps the run dispatch-eligible
        # without granting operator grace.
        lease = build_lease(
            run_id=run.run_id,
            spec_id=run.spec_id,
            phase="implement",
            actor="autopilot",
            process_pid=999_999,
        )
        save_run_lease(runs_dir, lease)

        # Precondition: resumable via the default-run selector, yet not selected
        # by autopilot's own resume resolver (which would leave run_id empty and
        # dispatch a superseding fresh run).
        assert orch._select_default_run(repo, "my-feature", ensure_identity=False) is not None
        spec_meta = next(s for s in iter_spec_metadata(repo) if s.spec_id == "my-feature")
        assert autopilot.resolve_resume_run(repo, spec_meta)[0] == ""

        with patch("spec_runtime.autopilot.is_pid_alive", return_value=False):
            queue = autopilot.build_dispatch_queue(
                repo, now=datetime.now(UTC), operator_grace_seconds=600.0
            )
        candidate = next((item for item in queue if item.spec_id == "my-feature"), None)
        assert candidate is not None
        # Resumed, not superseded: run id points at the existing run/branch and
        # no stranded needs-attention is raised.
        assert candidate.run_id == run.run_id
        assert candidate.reason.startswith("resume")
        assert candidate.stranded_commits_detail == ""

    def test_render_queue_surfaces_stranded_committed_work(self):
        candidate = autopilot.DispatchCandidate(
            spec_id="my-feature",
            agent="codex",
            area="backend",
            priority=10,
            unlock_count=0,
            status="not-started",
            stranded_commits_detail="run r1 has 1 commit on code/my-feature--1 ahead of master",
        )
        output = autopilot.render_queue([candidate])
        assert "needs-attention(stranded-commits)" in output
