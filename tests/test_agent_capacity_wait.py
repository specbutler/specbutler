"""Claude usage-limit exits wait for the window instead of spending attempts.

Regression coverage for a run on 2026-11-11: once Claude hit its 5-hour
subscription limit, every implement launch printed ``You've hit your 5-hour
limit · resets 11:20am (America/Los_Angeles)`` and exited after one turn. The
worktree still held an earlier attempt's uncommitted work, so each launch was
recorded as ``no_handshake`` / ``unreported_work``, got a recovery launch into
the same refusal, and attempts 3-21 burned in about two minutes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runtime import orchestrator as orch

FIVE_HOUR_NOTICE = "You've hit your 5-hour limit · resets 11:20am (America/Los_Angeles)"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    return work


def _make_run() -> orch.RunState:
    return orch.RunState(
        run_id="my-feature-20260101T000000",
        spec_id="my-feature",
        branch="code/my-feature--20260101T000000",
        agent="claude",
    )


def _ctx(run: orch.RunState) -> orch.ImplementContext:
    return orch.ImplementContext(
        implement_reason="retry",
        objective="Implement spec",
        run_id=run.run_id,
        attempt_number=run.attempts,
        run_state_dir=f".spec-state/runs/{run.run_id}/",
        spec_path=f"specs/{run.spec_id}.md",
        spec_revision="sha256:test",
    )


def _limit_stream(*, resets_at: int | None, notice: str = FIVE_HOUR_NOTICE) -> list[str]:
    """Stream-json lines Claude Code 2.1.x prints for a refused launch."""
    lines = [{"type": "system", "subtype": "init", "session_id": "sess-1"}]
    if resets_at is not None:
        lines.append(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": resets_at,
                    "rateLimitType": "five_hour",
                    "isUsingOverage": False,
                },
                "session_id": "sess-1",
            }
        )
    lines += [
        {
            "type": "assistant",
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": notice}]},
            "error": "rate_limit",
            "session_id": "sess-1",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 1,
            "duration_ms": 400,
            "result": notice,
            "session_id": "sess-1",
        },
    ]
    return [json.dumps(line) for line in lines]


def _tracker_after(lines: list[str], run: orch.RunState, repo: Path) -> orch.AgentProgressTracker:
    tracker = orch.AgentProgressTracker(agent="claude", run_id=run.run_id, repo_root=repo)
    for line in lines:
        orch._handle_claude_stream_line(line, tracker)
    return tracker


def _dirty_outcome(tracker: orch.AgentProgressTracker) -> orch.ImplementAttemptOutcome:
    # Earlier attempts left uncommitted work behind, as in the reported run.
    return orch.ImplementAttemptOutcome(
        exit_code=1,
        head_before="abc123",
        head_after="abc123",
        has_new_commit=False,
        has_uncommitted_changes=True,
        use_stream_json=True,
        progress_tracker=tracker,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        FIVE_HOUR_NOTICE,
        "You've hit your 5-hour limit · resets 11:20am (America/Los_Angeles) · progress saved",
        "You’ve hit your weekly limit · resets Nov 14, 3pm (America/Los_Angeles)",
        "You've hit your Opus limit · resets Nov 14, 3pm (UTC)",
        "You've hit your Sonnet limit",
        "You've hit your session limit · resets 8:30pm (America/Los_Angeles)",
        "You have reached your usage limit",
        "You've hit your usage credit limit · resets Dec 1, 12am (UTC)",
        "Claude usage limit reached (five_hour) [resets_at=1762888805]",
    ],
)
def test_claude_window_notices_are_capacity(message: str):
    assert orch._is_agent_capacity_failure_message(message)
    assert orch._is_agent_capacity_failure_line(message)
    assert orch.classify_failure(message).kind == "provider_capacity"


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your monthly spend limit. /usage-credits to manage your limit.",
        "You've hit your team's shared budget. /model to switch models.",
        "You're out of usage credits. /model to switch models.",
        "API Error: 429 rate_limit_error: Number of request tokens has exceeded your per-minute rate limit",
        "HTTP 429 from example.com",
    ],
)
def test_spend_caps_and_transient_rate_limits_are_not_capacity_windows(message: str):
    assert not orch._is_agent_capacity_failure_message(message)
    assert orch._agent_capacity_retry_delay_seconds(message) is None


def test_agent_prose_quoting_the_notice_is_not_the_provider_speaking():
    quoted = f"The fixture should print '{FIVE_HOUR_NOTICE}' before the result event"
    tool_line = f"> Bash: grep -n \"{FIVE_HOUR_NOTICE}\" tests/"

    assert not orch._is_agent_capacity_failure_line(quoted)
    assert not orch._is_agent_capacity_failure_line(tool_line)

    tracker = orch.AgentProgressTracker(agent="claude")
    tracker.record(quoted)
    tracker.record(tool_line)
    assert tracker.capacity_failure_summary() == ""


# ---------------------------------------------------------------------------
# Reset time
# ---------------------------------------------------------------------------


def test_reset_clock_time_in_named_zone():
    # 09:00 PST; the window reopens at 11:20 the same morning.
    now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)

    delay = orch._agent_capacity_retry_delay_seconds(FIVE_HOUR_NOTICE, now=now)

    assert delay == 2 * 3600 + 20 * 60 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS


def test_reset_clock_time_already_passed_today_means_tomorrow():
    now = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)  # 12:00 PST

    delay = orch._agent_capacity_retry_delay_seconds(FIVE_HOUR_NOTICE, now=now)

    assert delay == 23 * 3600 + 20 * 60 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS


def test_reset_with_date_and_hour_only():
    now = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)  # Nov 13 17:00 PST

    delay = orch._agent_capacity_retry_delay_seconds(
        "You've hit your weekly limit · resets Nov 14, 3pm (America/Los_Angeles)",
        now=now,
    )

    assert delay == 22 * 3600 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS


def test_structured_reset_epoch_wins_over_the_text():
    now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    epoch = int((now + timedelta(minutes=10)).timestamp())

    delay = orch._agent_capacity_retry_delay_seconds(
        f"{FIVE_HOUR_NOTICE} [resets_at={epoch}]",
        now=now,
    )

    assert delay == 600 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS


def test_reset_in_the_past_backs_off_instead_of_spinning():
    now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    epoch = int((now - timedelta(minutes=1)).timestamp())

    delay = orch._agent_capacity_retry_delay_seconds(
        f"Claude usage limit reached (five_hour) [resets_at={epoch}]",
        now=now,
    )

    assert delay == orch.AGENT_CAPACITY_DEFAULT_BACKOFF_SECONDS


def test_weekly_reset_days_away_is_capped():
    now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    epoch = int((now + timedelta(days=4)).timestamp())

    delay = orch._agent_capacity_retry_delay_seconds(
        f"Claude usage limit reached (seven_day) [resets_at={epoch}]",
        now=now,
    )

    assert delay == orch.AGENT_CAPACITY_MAX_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# Claude stream-json
# ---------------------------------------------------------------------------


def test_stream_keeps_the_notice_after_the_result_event(repo: Path):
    run = _make_run()
    tracker = _tracker_after(_limit_stream(resets_at=1762888805), run, repo)

    _, last_event = tracker.snapshot()
    assert last_event == "session complete"
    assert tracker.capacity_failure_summary() == f"{FIVE_HOUR_NOTICE} [resets_at=1762888805]"


def test_structured_refusal_without_text_is_still_capacity(repo: Path):
    run = _make_run()
    lines = [
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 1762888805, "rateLimitType": "seven_day"},
            }
        ),
        json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0}),
    ]
    tracker = _tracker_after(lines, run, repo)

    summary = tracker.capacity_failure_summary()
    assert summary == "Claude usage limit reached (seven_day) [resets_at=1762888805]"
    assert orch._is_agent_capacity_failure_message(summary)


@pytest.mark.parametrize(
    "info",
    [
        {"status": "allowed", "resetsAt": 1762888805, "rateLimitType": "five_hour"},
        {"status": "allowed_warning", "resetsAt": 1762888805, "rateLimitType": "five_hour"},
        {"status": "rejected", "resetsAt": 1762888805, "rateLimitType": "five_hour", "isUsingOverage": True},
    ],
)
def test_rate_limit_events_that_do_not_refuse_are_ignored(repo: Path, info: dict):
    run = _make_run()
    tracker = _tracker_after([json.dumps({"type": "rate_limit_event", "rate_limit_info": info})], run, repo)

    assert tracker.capacity_failure_summary() == ""


# ---------------------------------------------------------------------------
# Implement outcome and workflow
# ---------------------------------------------------------------------------


def test_usage_limit_with_leftover_changes_is_capacity_not_unreported_work(repo: Path):
    run = _make_run()
    worktree = repo / ".worktrees" / run.spec_id
    worktree.mkdir(parents=True)
    tracker = _tracker_after(_limit_stream(resets_at=1762888805), run, repo)

    with patch.object(orch, "_attempt_no_handshake_recovery") as recovery:
        status = orch._interpret_implement_attempt_outcome(
            run, repo, worktree, _ctx(run), _dirty_outcome(tracker)
        )

    assert status == "failed"
    recovery.assert_not_called()
    assert "provider_capacity" in run.last_error
    assert "uncommitted changes" not in run.last_error
    assert FIVE_HOUR_NOTICE in run.last_error
    metadata = orch._classify_phase_result(run, "implement", "failed")
    assert metadata["failure_subtype"] == "provider_capacity"
    assert metadata["retryable"] is True
    assert orch._agent_capacity_retry_delay_seconds(run.last_error) is not None


def test_text_only_notice_with_leftover_changes_is_capacity(repo: Path):
    """Claude versions without rate_limit_event still print the notice."""
    run = _make_run()
    worktree = repo / ".worktrees" / run.spec_id
    worktree.mkdir(parents=True)
    tracker = _tracker_after(_limit_stream(resets_at=None), run, repo)

    with patch.object(orch, "_attempt_no_handshake_recovery") as recovery:
        status = orch._interpret_implement_attempt_outcome(
            run, repo, worktree, _ctx(run), _dirty_outcome(tracker)
        )

    assert status == "failed"
    recovery.assert_not_called()
    assert "provider_capacity" in run.last_error
    now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
    assert orch._agent_capacity_retry_delay_seconds(run.last_error, now=now) == (
        2 * 3600 + 20 * 60 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS
    )


def test_limited_launches_wait_for_the_window_without_spending_attempts(repo: Path):
    """Replays the reported run: refused launches, then a working one."""
    run = _make_run()
    run.attempts = 2
    run.save(repo)
    worktree = repo / ".worktrees" / run.spec_id
    worktree.mkdir(parents=True)
    resets_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    launches = 0

    def implement(r: orch.RunState, root: Path) -> str:
        nonlocal launches
        launches += 1
        if launches <= 2:
            tracker = _tracker_after(_limit_stream(resets_at=resets_at), r, root)
            return orch._interpret_implement_attempt_outcome(
                r, root, worktree, _ctx(r), _dirty_outcome(tracker)
            )
        return "passed"

    handlers = {phase: MagicMock(return_value="passed") for phase in orch.PHASES}
    handlers["implement"] = implement
    sleeps: list[float] = []

    with (
        patch.dict(orch.PHASE_HANDLERS, handlers),
        patch.object(orch, "_attempt_no_handshake_recovery") as recovery,
        patch.object(orch, "_poll_sleep", side_effect=sleeps.append),
    ):
        result = orch.run_full_workflow(run, repo)

    assert result == "passed"
    assert launches == 3
    recovery.assert_not_called()
    assert run.attempts == 2
    assert run.implement_failures == 0
    # Each refused launch waited out the hour (plus slack) in lease-refresh chunks.
    assert max(sleeps) <= orch.AGENT_CAPACITY_LEASE_REFRESH_SECONDS
    assert sum(sleeps) == pytest.approx(2 * (3600 + orch.AGENT_CAPACITY_RESET_SLACK_SECONDS), abs=10)
