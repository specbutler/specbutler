from __future__ import annotations

import pytest

from spec_runtime.review_feedback import normalize_review_payload
from spec_runtime.review_gate_sticky_comment import (
    extract_embedded_review_result,
    render_sticky_comment,
)


def test_sticky_comment_renders_local_review_status_as_decision() -> None:
    review_result = {
        "status": "approved",
        "summary": "Local review approved the exact PR head.",
        "findings": [],
    }

    rendered = render_sticky_comment(review_result, pr_number=8)

    assert "## Codex Review Gate: Approved" in rendered
    assert "Current decision: `approved`." in rendered
    assert "The latest `review-decision-gate` run approved this PR." in rendered
    assert extract_embedded_review_result(rendered) == review_result


def test_sticky_comment_renders_cloud_review_decision() -> None:
    review_result = {
        "status": "changes_requested",
        "decision": "changes_requested",
        "summary": "A blocking finding remains.",
        "reviewed_head_sha": "a" * 40,
        "reviewed_base_sha": "b" * 40,
        "findings": [],
    }

    rendered = render_sticky_comment(review_result)

    assert "## Codex Review Gate: Request Changes" in rendered
    assert "Current decision: `request_changes`." in rendered
    normalized = normalize_review_payload(
        review_result,
        expected_head_sha="a" * 40,
        expected_base_sha="b" * 40,
        check_run={},
    )
    assert normalized.status == "request_changes"


def test_sticky_comment_and_review_consumer_fail_closed_for_conflicting_fields() -> None:
    review_result = {
        "status": "approved",
        "decision": "failed",
        "summary": "Conflicting producer fields.",
        "reviewed_head_sha": "a" * 40,
        "reviewed_base_sha": "b" * 40,
        "findings": [],
    }
    rendered = render_sticky_comment(review_result)

    assert "## Codex Review Gate: Failed" in rendered
    assert "Current decision: `failed`." in rendered
    embedded = extract_embedded_review_result(rendered)
    assert embedded == review_result
    with pytest.raises(ValueError, match="invalid decision/status"):
        normalize_review_payload(
            embedded,
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
            check_run={},
        )


@pytest.mark.parametrize(
    "review_result",
    [
        {"status": "unknown"},
        {"decision": "approved", "status": "unknown"},
        {},
    ],
)
def test_sticky_comment_fails_closed_for_missing_or_invalid_decision_fields(
    review_result: dict[str, str],
) -> None:
    rendered = render_sticky_comment(review_result)

    assert "## Codex Review Gate: Failed" in rendered
    with pytest.raises(ValueError, match="invalid decision/status"):
        normalize_review_payload(
            review_result,
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
            check_run={},
        )
