"""Infrastructure backoff and circuit breaker for autopilot dispatch.

Autopilot relaunches a failed spec as soon as it is ready again. When the
failure is infrastructure (container image build, missing toolchain, expired
credentials, push or CI-wait trouble), every relaunch fails the same way within
seconds; on one host that produced thousands of relaunches in a day. This module
holds the two controls that stop that:

* Per spec and failure signature, jittered exponential backoff before the next
  dispatch.
* Per backend and failure signature, a circuit breaker. After ``threshold``
  consecutive failures with one signature the breaker opens: affected work is
  held as *blocked-on-infrastructure* and one probe dispatch is allowed per
  cooldown. A success, or a failure that proves the environment works (a code
  failure), closes it.

Breaker state is persisted to ``<state_dir>/autopilot/infra-breaker.json`` so
``spec status``, ``spec watch``/web and ``spec analytics`` can show it and an
autopilot restart does not silently resume the storm. Per-spec backoff is
in-memory: after a restart, the first relaunch per spec is a free probe.

Failure classification itself lives in ``orchestrator.classify_failure``; this
module only consumes ``(signature, kind, scope)``.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from spec_runtime.platform_fs import atomic_write_text

INFRA_BREAKER_FILENAME = "infra-breaker.json"
BLOCKED_ON_INFRASTRUCTURE = "blocked-on-infrastructure"
SCOPE_BACKEND = "backend"
SCOPE_SPEC = "spec"
_ERROR_EXCERPT_CHARS = 600


def infra_breaker_state_path(state_root: Path) -> Path:
    """``state_root`` is the repo's ``.spec-state`` directory."""
    return state_root / "autopilot" / INFRA_BREAKER_FILENAME


def _iso(epoch: float) -> str:
    if epoch < 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def backoff_seconds(
    streak: int,
    *,
    base: float,
    maximum: float,
    rand: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with "equal jitter" for the ``streak``-th failure.

    ``base * 2**(streak-1)`` capped at ``maximum``, then drawn uniformly from
    the upper half of that window so runs that broke together do not all
    relaunch in the same instant.
    """
    streak = max(1, int(streak))
    base = max(0.0, float(base))
    maximum = max(base, float(maximum))
    # Cap the exponent before multiplying so a long streak cannot overflow.
    window = min(base * (2 ** min(streak - 1, 32)), maximum)
    draw = (rand or random.random)()
    return window / 2.0 + (window / 2.0) * max(0.0, min(1.0, draw))


@dataclass
class BreakerEntry:
    signature: str
    kind: str
    scope: str
    backend: str
    count: int = 0
    specs: list[str] = field(default_factory=list)
    error: str = ""
    first_failed_at: str = ""
    last_failed_at: str = ""
    opened_at: str = ""
    # Wall-clock epoch before which no probe dispatch is allowed while open.
    probe_after: float = 0.0
    trips: int = 0

    @property
    def is_open(self) -> bool:
        return bool(self.opened_at)

    def affects(self, spec_id: str) -> bool:
        return self.scope == SCOPE_BACKEND or spec_id in self.specs

    def describe(self) -> str:
        affected = "all specs" if self.scope == SCOPE_BACKEND else ", ".join(self.specs) or "none"
        return (
            f"{BLOCKED_ON_INFRASTRUCTURE}: {self.kind or 'infrastructure'} failure "
            f"{self.signature} on backend {self.backend or 'unknown'} after "
            f"{self.count} consecutive failures (affects {affected})"
        )

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = BLOCKED_ON_INFRASTRUCTURE if self.is_open else "backing-off"
        payload["next_probe_at"] = _iso(self.probe_after) if self.is_open else ""
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> BreakerEntry | None:
        signature = str(payload.get("signature") or "").strip()
        if not signature:
            return None
        try:
            return cls(
                signature=signature,
                kind=str(payload.get("kind") or ""),
                scope=str(payload.get("scope") or SCOPE_SPEC),
                backend=str(payload.get("backend") or ""),
                count=max(0, int(payload.get("count") or 0)),
                specs=[str(item) for item in payload.get("specs") or [] if str(item).strip()],
                error=str(payload.get("error") or ""),
                first_failed_at=str(payload.get("first_failed_at") or ""),
                last_failed_at=str(payload.get("last_failed_at") or ""),
                opened_at=str(payload.get("opened_at") or ""),
                probe_after=float(payload.get("probe_after") or 0.0),
                trips=max(0, int(payload.get("trips") or 0)),
            )
        except (TypeError, ValueError):
            return None


def read_infra_breaker_entries(path: Path) -> list[BreakerEntry]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return []
    entries = [BreakerEntry.from_payload(item) for item in raw_entries if isinstance(item, dict)]
    return [entry for entry in entries if entry is not None]


def open_breakers_for_spec(path: Path, spec_id: str) -> list[BreakerEntry]:
    return [entry for entry in read_infra_breaker_entries(path) if entry.is_open and entry.affects(spec_id)]


@dataclass(frozen=True)
class FailureOutcome:
    entry: BreakerEntry
    spec_streak: int
    backoff_seconds: float
    # True only on the failure that moved the breaker from closed to open.
    tripped: bool


@dataclass(frozen=True)
class DispatchDecision:
    allowed: bool
    # "", "backoff", "blocked" or "probe".
    reason: str = ""
    entry: BreakerEntry | None = None
    retry_in_seconds: float = 0.0


@dataclass
class _SpecBackoff:
    signature: str
    streak: int
    until: float


class InfrastructureCircuitBreaker:
    def __init__(
        self,
        *,
        backend: str,
        threshold: int = 3,
        base_backoff: float = 60.0,
        max_backoff: float = 1800.0,
        cooldown: float = 1800.0,
        state_path: Path | None = None,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self._backend = backend
        self._threshold = max(1, int(threshold))
        self._base = max(0.0, float(base_backoff))
        self._max = max(self._base, float(max_backoff))
        self._cooldown = max(0.0, float(cooldown))
        self._state_path = state_path
        self._rand = rand
        self._spec_backoff: dict[str, _SpecBackoff] = {}
        self._entries: dict[str, BreakerEntry] = {}
        if state_path is not None:
            for entry in read_infra_breaker_entries(state_path):
                if entry.backend in ("", backend):
                    self._entries[entry.signature] = entry

    @property
    def entries(self) -> list[BreakerEntry]:
        return list(self._entries.values())

    def reset(self) -> None:
        self._spec_backoff.clear()
        self._entries.clear()
        self._persist()

    def record_failure(
        self,
        spec_id: str,
        *,
        signature: str,
        kind: str,
        scope: str,
        error: str,
        now: float,
    ) -> FailureOutcome:
        spec_state = self._spec_backoff.get(spec_id)
        streak = spec_state.streak + 1 if spec_state is not None and spec_state.signature == signature else 1
        delay = backoff_seconds(streak, base=self._base, maximum=self._max, rand=self._rand)
        self._spec_backoff[spec_id] = _SpecBackoff(signature=signature, streak=streak, until=now + delay)

        entry = self._entries.get(signature)
        if entry is None:
            entry = BreakerEntry(
                signature=signature,
                kind=kind,
                scope=scope if scope in (SCOPE_BACKEND, SCOPE_SPEC) else SCOPE_SPEC,
                backend=self._backend,
                first_failed_at=_iso(now),
            )
            self._entries[signature] = entry
        entry.count += 1
        if spec_id not in entry.specs:
            entry.specs.append(spec_id)
        entry.error = (error or "")[:_ERROR_EXCERPT_CHARS]
        entry.last_failed_at = _iso(now)
        tripped = False
        if entry.is_open:
            # A failed probe: hold for another cooldown.
            entry.probe_after = now + self._cooldown
        elif entry.count >= self._threshold:
            entry.opened_at = _iso(now)
            entry.probe_after = now + self._cooldown
            entry.trips += 1
            tripped = True
        self._persist()
        return FailureOutcome(entry=entry, spec_streak=streak, backoff_seconds=delay, tripped=tripped)

    def record_progress(self, spec_id: str) -> list[BreakerEntry]:
        """The spec succeeded, or failed in a way that proves the environment works.

        Clears the spec's backoff. Backend-scoped entries close entirely; a
        spec-scoped entry forgets this spec. Returns the entries that were open
        and are now closed.
        """
        self._spec_backoff.pop(spec_id, None)
        closed: list[BreakerEntry] = []
        changed = False
        for signature, entry in list(self._entries.items()):
            if entry.scope == SCOPE_SPEC:
                if spec_id not in entry.specs:
                    continue
                entry.specs.remove(spec_id)
                changed = True
                if entry.specs:
                    continue
            del self._entries[signature]
            changed = True
            if entry.is_open:
                closed.append(entry)
        if changed:
            self._persist()
        return closed

    def check_dispatch(self, spec_id: str, *, now: float) -> DispatchDecision:
        for entry in self._entries.values():
            if not entry.is_open or not entry.affects(spec_id):
                continue
            if now < entry.probe_after:
                return DispatchDecision(
                    allowed=False,
                    reason="blocked",
                    entry=entry,
                    retry_in_seconds=entry.probe_after - now,
                )
            # Half-open: let this one dispatch through as a probe and hold
            # everyone else for another cooldown.
            entry.probe_after = now + self._cooldown
            self._persist()
            return DispatchDecision(allowed=True, reason="probe", entry=entry)
        spec_state = self._spec_backoff.get(spec_id)
        if spec_state is not None and now < spec_state.until:
            return DispatchDecision(
                allowed=False,
                reason="backoff",
                entry=self._entries.get(spec_state.signature),
                retry_in_seconds=spec_state.until - now,
            )
        return DispatchDecision(allowed=True)

    def _persist(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "updated_at": datetime.now(UTC).isoformat(),
            "entries": [entry.to_payload() for entry in self._entries.values()],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except OSError:
            # Display state only; dispatch decisions use the in-memory copy.
            pass
