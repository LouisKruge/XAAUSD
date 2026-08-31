"""Kill switch: a persisted state machine that blocks new entries.

Trips on any of the conditions in section 25 of the brief. Some conditions can clear
themselves when the underlying cause resolves (a reconnected broker, a normalised
spread); the rest require a human, because they indicate the system's view of the world
was wrong and someone should find out why.

Tripping never closes positions by itself. Stops are always server-side at the broker,
so a halted engine leaves protected positions rather than naked ones. Flattening is a
separate, explicit action.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xauusd.domain.enums import KillSwitchReason
from xauusd.monitoring.alerts import Notifier
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    ts: datetime
    action: str  # TRIP | CLEAR
    reason: KillSwitchReason
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    cleared_by: str | None = None


class KillSwitch:
    def __init__(
        self,
        notifier: Notifier | None = None,
        on_event: Callable[[KillSwitchEvent], None] | None = None,
    ) -> None:
        self._active: dict[KillSwitchReason, KillSwitchEvent] = {}
        self.history: list[KillSwitchEvent] = []
        self.notifier = notifier
        self.on_event = on_event

    # -- state -------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def reasons(self) -> list[KillSwitchReason]:
        return list(self._active)

    @property
    def summary(self) -> str:
        if not self._active:
            return "clear"
        return "; ".join(f"{r}: {e.detail}" for r, e in self._active.items())

    def is_active(self, reason: KillSwitchReason) -> bool:
        return reason in self._active

    def blocks_entry(self) -> tuple[bool, str | None]:
        return (True, self.summary) if self._active else (False, None)

    # -- transitions -------------------------------------------------------------------

    def trip(
        self,
        reason: KillSwitchReason,
        detail: str = "",
        context: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> KillSwitchEvent:
        now = now or datetime.now(UTC)
        existing = self._active.get(reason)
        event = KillSwitchEvent(now, "TRIP", reason, detail, context or {})
        if existing is not None:
            return existing  # already tripped for this reason; do not spam
        self._active[reason] = event
        self.history.append(event)
        log.error("kill_switch_tripped", reason=str(reason), detail=detail, **(context or {}))
        if self.notifier:
            self.notifier.critical(
                "KILL_SWITCH", f"Trading halted: {reason}", detail, **(context or {})
            )
        if self.on_event:
            self.on_event(event)
        return event

    def clear(
        self,
        reason: KillSwitchReason,
        by: str = "system",
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        """Clear one reason. Non-auto-clearable reasons need force=True and a name."""
        now = now or datetime.now(UTC)
        if reason not in self._active:
            return False
        if not reason.auto_clearable and not force:
            log.warning(
                "kill_switch_clear_refused",
                reason=str(reason),
                detail="this condition requires explicit manual clearance",
            )
            return False
        del self._active[reason]
        event = KillSwitchEvent(now, "CLEAR", reason, f"cleared by {by}", cleared_by=by)
        self.history.append(event)
        log.warning("kill_switch_cleared", reason=str(reason), by=by)
        if self.notifier:
            self.notifier.warning("KILL_SWITCH", f"Cleared: {reason}", f"by {by}")
        if self.on_event:
            self.on_event(event)
        return True

    def clear_all(self, by: str, now: datetime | None = None) -> int:
        """Explicit human override. Every reason is cleared and each is logged."""
        count = 0
        for reason in list(self._active):
            if self.clear(reason, by=by, now=now, force=True):
                count += 1
        return count

    # -- automatic evaluation ----------------------------------------------------------

    def evaluate(
        self,
        now: datetime,
        *,
        broker_ok: bool = True,
        quote_age_seconds: float = 0.0,
        max_quote_age: float = 10.0,
        spread_points: float = 0.0,
        spread_median: float = 25.0,
        max_spread_ratio: float = 4.0,
        news_extreme: bool = False,
        daily_breached: bool = False,
        weekly_breached: bool = False,
        monthly_breached: bool = False,
        state_divergence: str | None = None,
        spec_changed: bool = False,
        market_open: bool = True,
    ) -> list[KillSwitchReason]:
        """Trip or auto-clear from current conditions. Returns the active reasons."""
        # --- conditions that can clear themselves ------------------------------------
        self._toggle(
            KillSwitchReason.BROKER_UNREACHABLE,
            not broker_ok,
            "broker connection or trading permission lost",
            now,
        )
        # Staleness is only meaningful while the market is open.
        stale = market_open and quote_age_seconds > max_quote_age
        self._toggle(
            KillSwitchReason.STALE_DATA,
            stale,
            f"quote is {quote_age_seconds:.1f}s old (limit {max_quote_age}s)",
            now,
        )
        ratio = spread_points / spread_median if spread_median > 0 else 0.0
        wide = market_open and ratio > max_spread_ratio
        self._toggle(
            KillSwitchReason.SPREAD_ABNORMAL,
            wide,
            f"spread {spread_points:.0f}pts is {ratio:.1f}x the median",
            now,
        )
        self._toggle(
            KillSwitchReason.NEWS_EXTREME,
            news_extreme,
            "news risk is EXTREME",
            now,
        )
        self._toggle(
            KillSwitchReason.DAILY_DRAWDOWN,
            daily_breached,
            "daily drawdown limit reached",
            now,
        )

        # --- conditions that require a human ------------------------------------------
        if weekly_breached:
            self.trip(KillSwitchReason.WEEKLY_DRAWDOWN, "weekly drawdown limit reached", now=now)
        if monthly_breached:
            self.trip(KillSwitchReason.MONTHLY_DRAWDOWN, "monthly drawdown limit reached", now=now)
        if state_divergence:
            self.trip(
                KillSwitchReason.STATE_DIVERGENCE,
                state_divergence,
                {"detail": state_divergence},
                now=now,
            )
        if spec_changed:
            self.trip(
                KillSwitchReason.SPEC_CHANGED,
                "symbol specification changed — every open risk calculation is invalidated",
                now=now,
            )
        return self.reasons

    def _toggle(
        self, reason: KillSwitchReason, condition: bool, detail: str, now: datetime
    ) -> None:
        if condition:
            self.trip(reason, detail, now=now)
        elif reason in self._active:
            self.clear(reason, by="auto (condition resolved)", now=now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reasons": [
                {
                    "reason": str(r),
                    "detail": e.detail,
                    "since": e.ts.isoformat(),
                    "auto_clearable": r.auto_clearable,
                }
                for r, e in self._active.items()
            ],
            "summary": self.summary,
        }
