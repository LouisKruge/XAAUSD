"""Drawdown protection and the lockout state machine.

Design points that matter:

* Drawdown is measured from the period's PEAK equity (a high-water mark), not from its
  starting equity. This is the stricter definition and matches how funded-account rules
  work: making 3% then giving back 3% is a drawdown, not break-even.
* Daily periods anchor to BROKER midnight, because that is what the broker's own daily
  bar and any prop-firm rule use, not UTC midnight.
* A daily lockout clears when the period rolls. Weekly and monthly lockouts require a
  human to clear them: a weekly breach deserves someone looking at why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from xauusd.config.settings import RiskConfig


def period_start(
    now: datetime, period: str, broker_offset_seconds: int = 0, reset_hour: int = 0
) -> datetime:
    """Start of the containing period, anchored in BROKER time then returned as UTC."""
    broker_now = now + timedelta(seconds=broker_offset_seconds)
    if period == "DAY":
        start = broker_now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if broker_now < start:
            start -= timedelta(days=1)
    elif period == "WEEK":
        days_back = (broker_now.weekday() - 0) % 7  # Monday
        start = (broker_now - timedelta(days=days_back)).replace(
            hour=reset_hour, minute=0, second=0, microsecond=0
        )
    elif period == "MONTH":
        start = broker_now.replace(day=1, hour=reset_hour, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period {period}")
    return start - timedelta(seconds=broker_offset_seconds)


@dataclass(slots=True)
class PeriodState:
    period: str
    start: datetime
    starting_equity: float
    peak_equity: float
    current_equity: float
    limit_pct: float
    realised_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    locked: bool = False
    locked_at: datetime | None = None
    lock_reason: str | None = None
    needs_manual_clear: bool = False

    @property
    def drawdown_pct(self) -> float:
        """From the high-water mark. Never negative."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity)

    @property
    def drawdown_from_start_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return max(0.0, (self.starting_equity - self.current_equity) / self.starting_equity)

    @property
    def remaining_budget_pct(self) -> float:
        """How much risk the period can still absorb before hitting the limit."""
        return max(0.0, self.limit_pct - self.drawdown_pct)

    @property
    def breached(self) -> bool:
        return self.drawdown_pct >= self.limit_pct


class DrawdownGuard:
    """Tracks day/week/month equity and issues lockouts."""

    def __init__(self, config: RiskConfig | None = None, broker_offset_seconds: int = 0) -> None:
        self.cfg = config or RiskConfig()
        self.broker_offset = broker_offset_seconds
        self.periods: dict[str, PeriodState] = {}
        self.events: list[tuple[datetime, str, str]] = []

    def _limit(self, period: str) -> float:
        return {
            "DAY": self.cfg.max_daily_drawdown_pct,
            "WEEK": self.cfg.max_weekly_drawdown_pct,
            "MONTH": self.cfg.max_monthly_drawdown_pct,
        }[period]

    def _manual(self, period: str) -> bool:
        return {
            "DAY": False,
            "WEEK": self.cfg.weekly_lockout_needs_manual_clear,
            "MONTH": self.cfg.monthly_lockout_needs_manual_clear,
        }[period]

    def update(self, now: datetime, equity: float) -> dict[str, PeriodState]:
        """Advance every period, rolling over as needed. Returns the current states."""
        for period in ("DAY", "WEEK", "MONTH"):
            start = period_start(now, period, self.broker_offset, self.cfg.daily_reset_hour_broker)
            state = self.periods.get(period)
            if state is None or state.start != start:
                # Roll over. A manual-clear lockout SURVIVES the roll.
                carry_lock = bool(state and state.locked and state.needs_manual_clear)
                self.periods[period] = PeriodState(
                    period=period,
                    start=start,
                    starting_equity=equity,
                    peak_equity=equity,
                    current_equity=equity,
                    limit_pct=self._limit(period),
                    locked=carry_lock,
                    locked_at=state.locked_at if carry_lock and state else None,
                    lock_reason=(
                        f"{state.lock_reason} (carried across period roll; needs manual clear)"
                        if carry_lock and state
                        else None
                    ),
                    needs_manual_clear=carry_lock,
                )
                if state is not None and not carry_lock:
                    self.events.append((now, period, "ROLLED"))
                continue
            state.current_equity = equity
            state.peak_equity = max(state.peak_equity, equity)
            if state.breached and not state.locked:
                state.locked = True
                state.locked_at = now
                state.needs_manual_clear = self._manual(period)
                state.lock_reason = (
                    f"{period} drawdown {state.drawdown_pct:.2%} reached the limit "
                    f"{state.limit_pct:.2%}"
                )
                self.events.append((now, period, state.lock_reason))
        return dict(self.periods)

    def record_trade(self, now: datetime, r_multiple: float) -> None:
        for state in self.periods.values():
            state.trades += 1
            if r_multiple < -0.05:
                state.consecutive_losses += 1
            elif r_multiple > 0.05:
                state.consecutive_losses = 0
        day = self.periods.get("DAY")
        if (
            day
            and day.consecutive_losses >= self.cfg.max_consecutive_losses_lockout
            and not day.locked
        ):
            day.locked = True
            day.locked_at = now
            day.lock_reason = (
                f"{day.consecutive_losses} consecutive losses reached the lockout threshold"
            )
            self.events.append((now, "DAY", day.lock_reason))

    def clear(self, period: str, by: str, now: datetime) -> bool:
        """Manually clear a lockout. Recorded with who cleared it."""
        state = self.periods.get(period)
        if state is None or not state.locked:
            return False
        state.locked = False
        state.needs_manual_clear = False
        state.lock_reason = None
        self.events.append((now, period, f"CLEARED by {by}"))
        return True

    # -- queries used by the risk gate -------------------------------------------------

    @property
    def any_locked(self) -> bool:
        return any(s.locked for s in self.periods.values())

    def locked_periods(self) -> list[str]:
        return [p for p, s in self.periods.items() if s.locked]

    def remaining_budget_pct(self) -> float:
        """The BINDING constraint across all periods.

        This is what caps position size as drawdown accumulates: if the day has only
        0.4% of its 2% budget left, a 1% A-trade is sized down to 0.4%, not taken at 1%.
        """
        if not self.periods:
            return 1.0
        return min(s.remaining_budget_pct for s in self.periods.values())

    def summary(self) -> dict[str, dict[str, float | bool | str | None]]:
        return {
            p: {
                "drawdown_pct": round(s.drawdown_pct, 6),
                "limit_pct": s.limit_pct,
                "remaining_pct": round(s.remaining_budget_pct, 6),
                "peak_equity": round(s.peak_equity, 2),
                "current_equity": round(s.current_equity, 2),
                "trades": s.trades,
                "consecutive_losses": s.consecutive_losses,
                "locked": s.locked,
                "reason": s.lock_reason,
                "needs_manual_clear": s.needs_manual_clear,
            }
            for p, s in self.periods.items()
        }
