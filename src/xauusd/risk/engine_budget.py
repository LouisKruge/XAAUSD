"""Per-engine risk budgets and drawdown (spec §15, §16, §37).

Two engines drawing on one account need three separate answers, and conflating any two
of them is how an account quietly ends up carrying more risk than anyone authorised:

    how much is the SCALP engine risking right now
    how much is the INTRADAY engine risking right now
    how much is the ACCOUNT risking right now

§37 is explicit that this must be the actual monetary amount that would be lost if every
open position hit its stop — not a count of open trades. Three positions of wildly
different size are not "three units of risk", and a system that counts them that way will
happily approve a fourth.

§15 adds the point that makes this necessary rather than tidy: every scalp is XAUUSD, so
three simultaneous longs are one directional bet sized three times. The aggregate is the
real exposure; the per-trade figure is not.

This module owns the arithmetic and nothing else. It decides no trades, sends no orders,
and cannot relax a limit — `RiskGate` remains the single choke point, and this is one of
the facts it consults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from xauusd.config.settings import Settings
from xauusd.domain.types import BrokerPosition, SymbolSpec
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

SCALP = "scalp"
INTRADAY = "intraday"


@dataclass(frozen=True, slots=True)
class EngineExposure:
    """What one engine currently has at stake, in money and as a fraction of equity."""

    engine: str
    positions: int
    risk_money: float
    risk_pct: float
    # Positions whose stop we could not read. Counted separately and never treated as
    # zero risk: a position with no visible stop is the MOST dangerous kind, and folding
    # it into the total as 0.0 would make the account look safer for being less known.
    unknown_stop: int = 0

    @property
    def has_unmeasurable_risk(self) -> bool:
        return self.unknown_stop > 0


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether an engine may add `risk_pct` more, and why not if it may not."""

    allowed: bool
    engine: str
    would_be_pct: float
    limit_pct: float
    reason: str = ""

    @property
    def headroom_pct(self) -> float:
        return max(0.0, self.limit_pct - self.would_be_pct)


@dataclass
class DailyEngineDrawdown:
    """Realised loss per engine since the broker day rolled (spec §16).

    Kept per engine because §16 disables SCALPING on a scalp drawdown breach, not all
    trading. One shared counter would take the intraday engine down with it for a reason
    that has nothing to do with it.
    """

    day: date | None = None
    realised: dict[str, float] = field(default_factory=dict)
    start_equity: float = 0.0

    def roll(self, today: date, equity: float) -> None:
        if self.day != today:
            self.day = today
            self.realised = {}
            self.start_equity = equity

    def record(self, engine: str, pnl: float) -> None:
        self.realised[engine] = self.realised.get(engine, 0.0) + pnl

    def drawdown_pct(self, engine: str) -> float:
        """Loss as a POSITIVE fraction of the day's starting equity. Profit reads 0."""
        if self.start_equity <= 0:
            return 0.0
        return max(0.0, -self.realised.get(engine, 0.0)) / self.start_equity


class EngineBudget:
    """Measures per-engine exposure and answers whether an engine may add more."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.daily = DailyEngineDrawdown()

    # -- measurement -------------------------------------------------------------------

    def engine_of(self, position: BrokerPosition) -> str | None:
        """Which engine owns this position, by its magic number (spec §36)."""
        b = self.settings.broker
        if position.magic == b.scalp_magic:
            return SCALP
        if position.magic == b.intraday_magic:
            return INTRADAY
        return None

    def exposure(
        self, engine: str, positions: list[BrokerPosition], equity: float, spec: SymbolSpec
    ) -> EngineExposure:
        """Money at risk if every one of this engine's positions hit its stop."""
        mine = [p for p in positions if self.engine_of(p) == engine]
        total = 0.0
        unknown = 0
        for p in mine:
            if not p.stop_loss:
                # No stop visible. Not zero risk — unmeasurable risk, which is worse.
                unknown += 1
                continue
            total += abs(p.entry_price - p.stop_loss) * spec.value_per_price_unit(p.volume)
        return EngineExposure(
            engine=engine,
            positions=len(mine),
            risk_money=total,
            risk_pct=(total / equity) if equity > 0 else 0.0,
            unknown_stop=unknown,
        )

    def total_exposure(
        self, positions: list[BrokerPosition], equity: float, spec: SymbolSpec
    ) -> float:
        """Account-wide open risk as a fraction of equity, both engines together."""
        return sum(self.exposure(e, positions, equity, spec).risk_pct for e in (SCALP, INTRADAY))

    # -- permission --------------------------------------------------------------------

    def may_add(
        self,
        engine: str,
        add_risk_pct: float,
        positions: list[BrokerPosition],
        equity: float,
        spec: SymbolSpec,
    ) -> BudgetVerdict:
        """May `engine` open one more position risking `add_risk_pct`?

        Three limits, checked in order of how much they protect: the engine's own
        aggregate, then the account-wide cap. The tighter of the two binds — this can
        only ever refuse, never authorise something the account cap forbids.
        """
        cfg = self.settings
        current = self.exposure(engine, positions, equity, spec)
        would_be = current.risk_pct + add_risk_pct

        # An unmeasurable position makes the total a lower bound, not a figure. Refusing
        # is the only honest response: we cannot show the limit is respected.
        if current.has_unmeasurable_risk:
            return BudgetVerdict(
                False,
                engine,
                would_be,
                0.0,
                f"{current.unknown_stop} open {engine} position(s) have no readable stop, "
                f"so open risk cannot be measured — refusing to add more",
            )

        limit = cfg.engine_risk_limit(engine)
        if would_be > limit:
            return BudgetVerdict(
                False,
                engine,
                would_be,
                limit,
                f"{engine} aggregate risk would reach {would_be:.2%}, over its {limit:.2%} budget",
            )

        total = self.total_exposure(positions, equity, spec) + add_risk_pct
        account_cap = cfg.risk.max_total_open_risk_pct
        if total > account_cap:
            return BudgetVerdict(
                False,
                engine,
                total,
                account_cap,
                f"account-wide open risk would reach {total:.2%}, over the "
                f"{account_cap:.2%} cap (this engine alone was within its budget)",
            )

        # §16: a breached daily drawdown disables THAT engine, not the account.
        dd = self.daily.drawdown_pct(engine)
        dd_limit = cfg.engine_daily_drawdown_limit(engine)
        if dd >= dd_limit:
            return BudgetVerdict(
                False,
                engine,
                would_be,
                limit,
                f"{engine} daily drawdown {dd:.2%} has reached its {dd_limit:.2%} "
                f"limit; this engine is disabled for the rest of the day",
            )

        return BudgetVerdict(True, engine, would_be, limit)

    # -- bookkeeping -------------------------------------------------------------------

    def observe_close(self, engine: str, pnl: float, now: datetime, equity: float) -> None:
        self.daily.roll(now.date(), equity)
        self.daily.record(engine, pnl)
        if pnl < 0:
            log.debug(
                "engine_daily_drawdown",
                engine=engine,
                drawdown_pct=round(self.daily.drawdown_pct(engine), 6),
                limit=self.settings.engine_daily_drawdown_limit(engine),
            )
