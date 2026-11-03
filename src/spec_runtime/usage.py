"""Provider usage normalization and subscription cost estimates.

Claude result counters cover the whole session, including subagents. Codex
turn counters cover one turn, so multiple completed turns are additive.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

ROLES = ("implement", "review", "debugger", "authoring")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def parse_event(provider: str, event: object) -> dict | None:
    if not isinstance(event, dict):
        return None
    if provider == "claude" and event.get("type") == "result":
        usage = event.get("usage") or {}
        models = event.get("modelUsage") or {}
        models = models if isinstance(models, dict) else {}
        # modelUsage includes subagents; top-level usage can omit their tokens.
        sources = [v for v in models.values() if isinstance(v, dict)] or [usage]
        def total(camel: str, snake: str) -> int:
            return sum(int(number(s.get(camel, s.get(snake))) or 0)
                       for s in sources if isinstance(s, dict))
        return {
            "input_tokens": total("inputTokens", "input_tokens"),
            "output_tokens": total("outputTokens", "output_tokens"),
            "cache_read_tokens": total("cacheReadInputTokens", "cache_read_input_tokens"),
            "cache_creation_tokens": total("cacheCreationInputTokens", "cache_creation_input_tokens"),
            "estimated_cost_usd": number(event.get("total_cost_usd")),
            "model_usage": models,
            "provider_usage": usage,
            "session_id": event.get("session_id") or "",
            "budget_exceeded": event.get("subtype") == "error_max_budget_usd",
        }
    if provider == "codex" and event.get("type") == "turn.completed":
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        return {
            "input_tokens": int(number(usage.get("input_tokens")) or 0),
            "output_tokens": int(number(usage.get("output_tokens")) or 0),
            "cache_read_tokens": int(number(usage.get("cached_input_tokens")) or 0),
            "cache_creation_tokens": 0,
            "estimated_cost_usd": None,
        }
    return None


def new_launch(role: str, provider: str, model: str, phase: str) -> dict:
    return dict(role=role, provider=provider, model=model or None, phase=phase,
                recorded_at=datetime.now(timezone.utc).isoformat(), usage_available=False,
                **{key: 0 for key in TOKEN_FIELDS}, estimated_cost_usd=None)


def update_launch(launch: dict, parsed: dict) -> None:
    launch["usage_available"] = True
    if launch["provider"] == "codex":
        for key in TOKEN_FIELDS:
            launch[key] += parsed[key]
    else:
        launch.update(parsed)
        if not launch.get("model") and parsed.get("model_usage"):
            launch["model"] = ", ".join(parsed["model_usage"])


def effective_usage(launches: list[dict]) -> list[dict]:
    """Use the latest Claude whole-session snapshot, preserving launch history."""
    result: list[dict] = []
    seen: set[tuple] = set()
    for launch in reversed(launches):
        session = launch.get("session_id")
        key = (launch.get("provider"), session)
        if launch.get("provider") == "claude" and session:
            if key in seen:
                continue
            seen.add(key)
        result.append(launch)
    return list(reversed(result))


def totals(launches: list[dict]) -> dict:
    rows = effective_usage(launches)
    costs = [row["estimated_cost_usd"] for row in rows
             if number(row.get("estimated_cost_usd")) is not None]
    return {**{key: sum(row.get(key, 0) for row in rows) for key in TOKEN_FIELDS},
            "estimated_cost_usd": sum(costs) if costs else None,
            "unknown_cost_launches": sum(row.get("estimated_cost_usd") is None for row in rows)}


def grouped(launches: list[dict], key: str) -> dict:
    buckets: dict[str, list[dict]] = {}
    for row in effective_usage(launches):
        value = str(row.get(key) or "unknown")
        if key == "recorded_at":
            value = value[:7]
        buckets.setdefault(value, []).append(row)
    return {name: totals(rows) for name, rows in sorted(buckets.items())}
