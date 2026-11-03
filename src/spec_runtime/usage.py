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


def _session_rows(launches: list[dict]) -> list[dict]:
    rows = [{**launch, **receipt, "recorded_at": launch.get("recorded_at", "")}
            for launch in launches for receipt in launch.get("transcript_receipts", [launch])]
    for row in rows:
        row.pop("transcript_receipts", None)
    return rows


def effective_usage(launches: list[dict]) -> list[dict]:
    """Use the latest Claude whole-session snapshot, preserving launch history."""
    result: list[dict] = []
    seen: set[tuple] = set()
    for launch in reversed(_session_rows(launches)):
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
    rows = effective_usage(launches)
    if key == "recorded_at":
        rows = []
        previous: dict[tuple, dict] = {}
        for launch in sorted(_session_rows(launches), key=lambda row: row.get("recorded_at", "")):
            row = dict(launch)
            session = (row.get("provider"), row.get("session_id"))
            if session[0] == "claude" and session[1] and row.get("usage_available"):
                prior = previous.get(session, {})
                for field in (*TOKEN_FIELDS, "estimated_cost_usd"):
                    current = number(row.get(field))
                    if current is not None:
                        row[field] = max(0, current - (number(prior.get(field)) or 0))
                previous[session] = launch
            # Period increments are independent, not whole-session snapshots.
            row.pop("session_id", None)
            rows.append(row)
    for row in rows:
        value = str(row.get(key) or "unknown")
        if key == "recorded_at":
            value = value[:7]
        buckets.setdefault(value, []).append(row)
    return {name: totals(rows) for name, rows in sorted(buckets.items())}


def transcript_snapshot(provider, cwd, env):
    """Snapshot transcript sizes before an attached launch, without reading credentials."""
    from pathlib import Path
    home = Path(env.get("HOME") or Path.home())
    if provider == "claude":
        root = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
        import re
        root = root / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
    else:
        root = Path(env.get("CODEX_HOME") or home / ".codex") / "sessions"
    sizes = {}
    try:
        for path in root.rglob("*.jsonl"):
            if not path.is_symlink():
                sizes[path] = path.stat().st_size
    except OSError:
        pass
    return root, sizes


def collect_transcript_usage(launch, cwd, snapshot):
    """Read changed provider transcripts after exit; never consume another cwd's rollout."""
    import json
    root, before = snapshot
    provider = launch["provider"]
    try:
        paths = list(root.rglob("*.jsonl"))
    except OSError:
        return
    changed_sessions = set()
    if provider == "claude":
        for path in paths:
            try:
                if path.stat().st_size != before.get(path, 0):
                    changed_sessions.add(path.stem if path.parent == root else path.relative_to(root).parts[0])
            except OSError:
                continue
    for path in paths:
        try:
            if path.is_symlink():
                continue
            changed = path.stat().st_size != before.get(path, 0)
            parent_session = path.relative_to(root).parts[0]
            if not changed and not (provider == "claude" and parent_session in changed_sessions):
                continue
            events = []
            for line in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except ValueError:
                    continue
        except OSError:
            continue
        if provider == "codex":
            if not any(e.get("type") == "session_meta" and
                       e.get("payload", {}).get("cwd") == str(cwd) for e in events):
                continue
            # Rollout token_count totals are cumulative. Attribute only appended usage.
            old = None
            latest = None
            offset = 0
            for line in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True):
                offset += len(line.encode("utf-8", "surrogateescape"))
                try:
                    e = json.loads(line)
                    info = e.get("payload", {}).get("info") or {}
                    usage = info.get("total_token_usage")
                    if usage:
                        latest = usage
                        if offset <= before.get(path, 0):
                            old = usage
                except (ValueError, AttributeError):
                    continue
            if latest:
                delta = {k: max(0, v - (old or {}).get(k, 0)) for k, v in latest.items()
                         if isinstance(v, (int, float))}
                update_launch(launch, parse_event(provider, {"type": "turn.completed", "usage": delta}))
        else:
            # Each assistant message contains API usage. Deduplicate streamed message updates.
            messages = {}
            for e in events:
                message = e.get("message") or {}
                if e.get("type") == "assistant" and message.get("usage"):
                    messages[message.get("id") or e.get("uuid")] = message
            if not messages:
                continue
            models = {}
            cost = 0.0
            known = True
            for message in messages.values():
                model = message.get("model", "")
                usage = message["usage"]
                bucket = models.setdefault(model, {})
                for k, v in usage.items():
                    if number(v) is not None:
                        bucket[k] = bucket.get(k, 0) + v
                # Standard API list prices, USD per million tokens:
                # https://platform.claude.com/docs/en/about-claude/pricing
                rates = {"claude-sonnet-4": (3, 15), "claude-haiku-4-5": (1, 5),
                         "claude-opus-4-5": (5, 25), "claude-opus-4-6": (5, 25)}
                rate = next((v for k, v in rates.items() if model.startswith(k)), None)
                if rate is None:
                    known = False
                else:
                    cost += (usage.get("input_tokens", 0) * rate[0] +
                             usage.get("output_tokens", 0) * rate[1] +
                             usage.get("cache_read_input_tokens", 0) * rate[0] * .1 +
                             usage.get("cache_creation_input_tokens", 0) * rate[0] * 1.25) / 1e6
            receipt = new_launch(launch["role"], provider, launch.get("model"), launch["phase"])
            update_launch(receipt, parse_event(provider, {"type": "result", "session_id": path.stem,
                          "modelUsage": models, "total_cost_usd": cost if known else None}))
            # Multiple files include subagents; preserve each whole-session snapshot separately.
            launch.setdefault("transcript_receipts", []).append(receipt)
    if provider == "claude" and launch.get("transcript_receipts"):
        receipts = launch["transcript_receipts"]
        launch.update(totals(receipts))
        launch["usage_available"] = True
        launch["session_id"] = next((r["session_id"] for r in receipts
                                      if not r["session_id"].startswith("agent-")), receipts[0]["session_id"])
