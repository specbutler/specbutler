"""Canonical review-decision parsing shared by producers and consumers."""

from __future__ import annotations

from typing import Any

REVIEW_DECISION_VALUES = ("approved", "request_changes", "blocked", "failed")

_REVIEW_DECISION_ALIASES = {
    "approve": "approved",
    "approved": "approved",
    "changes_requested": "request_changes",
    "request_changes": "request_changes",
    "request-changes": "request_changes",
    "blocked": "blocked",
    "failed": "failed",
}


def normalize_review_decision(raw: object) -> str:
    """Return the canonical decision for one field, or an empty invalid value."""
    value = str(raw or "").strip().lower()
    return _REVIEW_DECISION_ALIASES.get(value, "")


def review_payload_decision(payload: dict[str, Any]) -> str:
    """Return one consistent canonical payload decision, failing closed to empty.

    Local-review payloads use ``status`` while cloud-gate payloads include
    ``decision`` (and normally a matching ``status``). Every field that is
    present must be valid, and aliases must resolve to the same decision.
    """
    decisions: list[str] = []
    for field_name in ("status", "decision"):
        if field_name not in payload:
            continue
        decision = normalize_review_decision(payload[field_name])
        if not decision:
            return ""
        decisions.append(decision)
    if not decisions or any(decision != decisions[0] for decision in decisions[1:]):
        return ""
    return decisions[0]
