"""Immutable value objects passed between layers.

Everything here is frozen. Analysis produces new objects rather than mutating shared
state, which is what makes a decision cycle reproducible from its stored inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from xauusd.domain.enums import (
    Bias,
    Classification,
    Direction,
    ExitReason,
    FVGState,
    Killzone,
    LevelKind,
    LiquidityKind,
    MacroBias,
    NewsRisk,
    OrderBlockKind,
    OrderStatus,
    Regime,
    Session,
    StructureKind,
    SwingKind,
    SwingStrength,
    Timeframe,
    VolRegime,
    ZoneState,
)

# --------------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # bar OPEN time, always UTC, always tz-aware
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    real_volume: int = 0
    spread_points: int = 0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        """Body as a fraction of range. High values mean displacement, not indecision."""
        r = self.range
        return self.body / r if r > 0 else 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    def close_time(self, tf: Timeframe) -> datetime:
        return self.ts + timedelta(seconds=tf.seconds)


@dataclass(frozen=True, slots=True)
class Quote:
    ts: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def spread_points(self, point: float) -> float:
        return self.spread / point if point > 0 else math.inf

    def price_for(self, direction: Direction) -> float:
        """Entry price: buy at ask, sell at bid."""
        return self.ask if direction is Direction.LONG else self.bid

    def exit_price_for(self, direction: Direction) -> float:
        """Exit price: close a long at bid, close a short at ask."""
        return self.bid if direction is Direction.LONG else self.ask

    def age(self, now: datetime) -> timedelta:
        return now - self.ts


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Broker-reported symbol specification. NEVER constructed from assumptions.

    Every field here comes from MT5 SymbolInfo. The system refuses to size a position
    from a spec it did not read from the broker.
    """

    symbol: str
    digits: int
    point: float
    contract_size: float
    tick_size: float
    tick_value: float
    tick_value_profit: float
    tick_value_loss: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # in points
    freeze_level: int  # in points
    filling_modes: int = 0
    trade_mode: int = 4  # SYMBOL_TRADE_MODE_FULL
    currency_profit: str = "USD"
    currency_margin: str = "USD"
    swap_long: float = 0.0
    swap_short: float = 0.0
    commission_per_lot: float = 0.0
    spread_points: int = 0
    spread_float: bool = True

    def __post_init__(self) -> None:
        if self.tick_size <= 0:
            raise ValueError(f"{self.symbol}: tick_size must be > 0, got {self.tick_size}")
        if self.volume_step <= 0:
            raise ValueError(f"{self.symbol}: volume_step must be > 0, got {self.volume_step}")
        if self.volume_min <= 0 or self.volume_max < self.volume_min:
            raise ValueError(f"{self.symbol}: invalid volume bounds")
        if self.tick_value_loss <= 0:
            raise ValueError(f"{self.symbol}: tick_value_loss must be > 0")
        if self.point <= 0:
            raise ValueError(f"{self.symbol}: point must be > 0")

    @property
    def stops_level_price(self) -> float:
        return self.stops_level * self.point

    @property
    def freeze_level_price(self) -> float:
        return self.freeze_level * self.point

    def normalize_price(self, price: float) -> float:
        return round(price, self.digits)

    def normalize_volume(self, volume: float) -> float:
        """Floor to the volume step. ALWAYS floors, so rounding can only reduce risk."""
        if volume <= 0:
            return 0.0
        steps = math.floor(round(volume / self.volume_step, 8))
        vol = steps * self.volume_step
        # volume_step is often 0.01; keep float noise out of the value sent to the broker.
        return round(vol, 8)

    def value_per_price_unit(self, volume: float) -> float:
        """Money moved per 1.0 of price, for `volume` lots, in the profit currency."""
        return (self.tick_value_loss / self.tick_size) * volume

    def spec_hash(self) -> str:
        import hashlib

        parts = (
            f"{self.symbol}|{self.digits}|{self.point}|{self.contract_size}|{self.tick_size}"
            f"|{self.tick_value_loss}|{self.volume_min}|{self.volume_max}|{self.volume_step}"
            f"|{self.stops_level}|{self.freeze_level}|{self.currency_profit}"
        )
        return hashlib.blake2s(parts.encode(), digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class AccountState:
    login: int
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    leverage: int = 100
    trade_allowed: bool = True
    trade_expert: bool = True
    server: str = ""
    ts: datetime | None = None


# --------------------------------------------------------------------------------------
# Structural analysis objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Swing:
    ts: datetime
    index: int  # index into the bar array it was detected in
    price: float
    kind: SwingKind
    timeframe: Timeframe
    strength: SwingStrength = SwingStrength.UNTESTED
    confirmed_ts: datetime | None = None  # when the right-hand bars confirmed it

    @property
    def is_high(self) -> bool:
        return self.kind is SwingKind.HIGH


@dataclass(frozen=True, slots=True)
class StructureEvent:
    ts: datetime
    timeframe: Timeframe
    kind: StructureKind
    direction: Direction
    price: float  # the level that was broken
    break_price: float  # the close that broke it
    ref_swing_ts: datetime | None = None
    displacement_atr: float = 0.0
    body_ratio: float = 0.0
    is_internal: bool = False

    @property
    def is_bullish(self) -> bool:
        return self.direction is Direction.LONG


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    kind: LiquidityKind
    timeframe: Timeframe
    price: float
    formed_ts: datetime
    price_upper: float | None = None
    price_lower: float | None = None
    touches: int = 1
    strength: float = 0.0
    swept_ts: datetime | None = None
    sweep_quality: float = 0.0

    @property
    def is_resting(self) -> bool:
        return self.swept_ts is None

    @property
    def is_buyside(self) -> bool:
        return self.kind.is_buyside


@dataclass(frozen=True, slots=True)
class Sweep:
    """A liquidity sweep: penetration of a pool followed by rejection back through it.

    A sweep alone is NEVER a trade signal - see strategy/setups. It is the first of
    several required conditions.
    """

    ts: datetime
    timeframe: Timeframe
    pool: LiquidityPool
    direction: Direction  # direction of the REVERSAL implied (sweep of highs -> SHORT bias)
    penetration: float  # price distance beyond the pool
    penetration_atr: float
    rejection_ratio: float  # wick beyond pool / total bar range
    closed_back_inside: bool
    displacement_after_atr: float = 0.0
    bars_to_reject: int = 1

    @property
    def quality(self) -> float:
        """0..1 composite. Deep penetration with a fast, strong rejection scores highest."""
        pen = min(self.penetration_atr / 0.5, 1.0)
        rej = min(self.rejection_ratio / 0.6, 1.0)
        spd = 1.0 / max(self.bars_to_reject, 1)
        disp = min(self.displacement_after_atr / 1.0, 1.0)
        inside = 1.0 if self.closed_back_inside else 0.4
        return round((0.20 * pen + 0.30 * rej + 0.15 * spd + 0.25 * disp + 0.10 * 1.0) * inside, 4)


@dataclass(frozen=True, slots=True)
class FVG:
    timeframe: Timeframe
    direction: Direction
    formed_ts: datetime
    top: float
    bottom: float
    size: float
    size_atr: float
    displacement_atr: float
    state: FVGState = FVGState.UNMITIGATED
    mitigated_pct: float = 0.0
    first_touch_ts: datetime | None = None
    quality: float = 0.0

    @property
    def midpoint(self) -> float:
        """Consequent encroachment - the 50% level, the standard refined entry."""
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    @property
    def is_tradable(self) -> bool:
        return self.state in {FVGState.UNMITIGATED, FVGState.PARTIAL}


@dataclass(frozen=True, slots=True)
class OrderBlock:
    kind: OrderBlockKind
    timeframe: Timeframe
    direction: Direction
    formed_ts: datetime
    top: float
    bottom: float
    open_price: float
    close_price: float
    displacement_atr: float = 0.0
    caused_bos: bool = False
    swept_liquidity: bool = False
    has_fvg: bool = False
    state: ZoneState = ZoneState.FRESH
    test_count: int = 0
    quality: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    @property
    def is_tradable(self) -> bool:
        return self.state in {ZoneState.FRESH, ZoneState.TESTED}


@dataclass(frozen=True, slots=True)
class SRLevel:
    kind: LevelKind
    timeframe: Timeframe
    price: float
    band_upper: float
    band_lower: float
    formed_ts: datetime
    touches: int = 1
    last_test_ts: datetime | None = None
    rejection_strength: float = 0.0
    importance: float = 0.0

    def contains(self, price: float) -> bool:
        return self.band_lower <= price <= self.band_upper

    def distance(self, price: float) -> float:
        return abs(price - self.price)


@dataclass(frozen=True, slots=True)
class DealingRange:
    """The swing range price is currently trading inside. Defines premium/discount."""

    high: float
    low: float
    high_ts: datetime
    low_ts: datetime
    timeframe: Timeframe

    @property
    def equilibrium(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def size(self) -> float:
        return self.high - self.low

    def position_of(self, price: float) -> float:
        """0.0 at range low, 1.0 at range high. Clamped."""
        if self.size <= 0:
            return 0.5
        return max(0.0, min(1.0, (price - self.low) / self.size))

    def is_discount(self, price: float) -> bool:
        return self.position_of(price) < 0.5

    def is_premium(self, price: float) -> bool:
        return self.position_of(price) > 0.5

    def zone_label(self, price: float) -> str:
        p = self.position_of(price)
        if p < 0.25:
            return "DEEP_DISCOUNT"
        if p < 0.5:
            return "DISCOUNT"
        if p < 0.75:
            return "PREMIUM"
        return "DEEP_PREMIUM"


# --------------------------------------------------------------------------------------
# Context objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionState:
    session: Session
    killzone: Killzone
    utc_now: datetime
    london_now: datetime
    ny_now: datetime
    broker_now: datetime
    minutes_into_session: int
    is_overlap: bool
    is_weekend: bool
    is_holiday: bool
    day_of_week: int  # 0 = Monday
    asia_high: float | None = None
    asia_low: float | None = None
    session_high: float | None = None
    session_low: float | None = None


@dataclass(frozen=True, slots=True)
class VolatilityState:
    atr_d1: float
    atr_h4: float
    atr_h1: float
    atr_m15: float
    atr_m5: float
    atr_h1_percentile: float  # 0..1 against a trailing window
    realized_vol: float
    vol_regime: VolRegime
    spread_points: float
    spread_median_points: float

    @property
    def spread_ratio(self) -> float:
        return (
            self.spread_points / self.spread_median_points
            if self.spread_median_points > 0
            else 99.0
        )


@dataclass(frozen=True, slots=True)
class MacroState:
    bias: MacroBias
    dxy_level: float | None
    dxy_change_1d: float | None
    dxy_change_5d: float | None
    dxy_trend: Bias
    us10y: float | None
    us2y: float | None
    real10y: float | None
    real10y_change_5d: float | None
    breakeven10y: float | None
    yields_trend: Bias
    curve_10y2y: float | None
    as_of: datetime | None
    is_stale: bool
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def dxy_implication(self) -> Bias:
        """A falling dollar is a bullish gold input."""
        if self.dxy_trend is Bias.BULLISH:
            return Bias.BEARISH
        if self.dxy_trend is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL

    @property
    def yields_implication(self) -> Bias:
        """Rising real yields raise the opportunity cost of holding gold."""
        if self.yields_trend is Bias.BULLISH:
            return Bias.BEARISH
        if self.yields_trend is Bias.BEARISH:
            return Bias.BULLISH
        return Bias.NEUTRAL


@dataclass(frozen=True, slots=True)
class NewsState:
    risk: NewsRisk
    blackout: bool
    blackout_reason: str | None
    blackout_until: datetime | None
    next_event_name: str | None
    next_event_ts: datetime | None
    minutes_to_next_event: float | None
    directional_hint: Bias
    drivers: tuple[str, ...] = ()
    is_stale: bool = False


@dataclass(frozen=True, slots=True)
class TimeframeStructure:
    """Structural read of one timeframe at one instant."""

    timeframe: Timeframe
    bias: Bias
    last_event: StructureEvent | None
    swings: tuple[Swing, ...]
    dealing_range: DealingRange | None
    last_bos: StructureEvent | None = None
    last_choch: StructureEvent | None = None
    last_mss: StructureEvent | None = None

    @property
    def has_recent_mss(self) -> bool:
        return self.last_mss is not None


# --------------------------------------------------------------------------------------
# Snapshot: everything the engine saw at one instant
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    ts: datetime
    symbol: str
    quote: Quote
    structures: dict[Timeframe, TimeframeStructure]
    liquidity: tuple[LiquidityPool, ...]
    sweeps: tuple[Sweep, ...]
    fvgs: tuple[FVG, ...]
    order_blocks: tuple[OrderBlock, ...]
    sr_levels: tuple[SRLevel, ...]
    dealing_range: DealingRange | None
    session: SessionState
    volatility: VolatilityState
    regime: Regime
    macro: MacroState
    news: NewsState

    def bias(self, tf: Timeframe) -> Bias:
        s = self.structures.get(tf)
        return s.bias if s else Bias.NEUTRAL

    @property
    def htf_bias(self) -> Bias:
        """Aggregate D1/H4 directional bias. Disagreement resolves to NEUTRAL."""
        d1 = self.bias(Timeframe.D1).sign
        h4 = self.bias(Timeframe.H4).sign
        total = d1 * 2 + h4
        if total >= 2:
            return Bias.BULLISH
        if total <= -2:
            return Bias.BEARISH
        return Bias.NEUTRAL

    def resting_liquidity(self, buyside: bool) -> tuple[LiquidityPool, ...]:
        return tuple(p for p in self.liquidity if p.is_resting and p.is_buyside == buyside)


# --------------------------------------------------------------------------------------
# Trade plan / decision
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetLevel:
    price: float
    rr: float
    rationale: str
    liquidity_kind: LiquidityKind | None = None


@dataclass(frozen=True, slots=True)
class TradePlan:
    """A candidate trade. Produced by a strategy; not yet scored, gated, or sized."""

    strategy: str
    strategy_version: str
    direction: Direction
    entry: float
    stop_loss: float
    targets: tuple[TargetLevel, ...]
    ts: datetime
    setup_timeframe: Timeframe
    invalidation: str
    entry_zone_top: float | None = None
    entry_zone_bottom: float | None = None
    symbol: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction is Direction.LONG and self.stop_loss >= self.entry:
            raise ValueError("long stop must be below entry")
        if self.direction is Direction.SHORT and self.stop_loss <= self.entry:
            raise ValueError("short stop must be above entry")
        if not self.targets:
            raise ValueError("a trade plan must have at least one target")

    def symbol_hint(self) -> str:
        return self.symbol

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def primary_target(self) -> TargetLevel:
        return self.targets[0]

    @property
    def final_target(self) -> TargetLevel:
        return self.targets[-1]

    @property
    def rr(self) -> float:
        """RR to the FINAL target - what the trade is judged on."""
        return self.final_target.rr

    def rr_to(self, price: float) -> float:
        d = self.risk_distance
        return abs(price - self.entry) / d if d > 0 else 0.0

    def with_entry(self, entry: float, stop_loss: float | None = None) -> TradePlan:
        """Re-price the plan at execution time and recompute every RR."""
        sl = stop_loss if stop_loss is not None else self.stop_loss
        dist = abs(entry - sl)
        new_targets = tuple(
            replace(t, rr=(abs(t.price - entry) / dist if dist > 0 else 0.0)) for t in self.targets
        )
        return replace(self, entry=entry, stop_loss=sl, targets=new_targets)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    observed: Any = None
    threshold: Any = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "passed": self.passed,
            "observed": _jsonable(self.observed),
            "threshold": _jsonable(self.threshold),
            "detail": self.detail,
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 6) if math.isfinite(v) else str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "value"):
        return v.value
    return v


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    categories: dict[str, float]
    maximums: dict[str, float]
    penalties: dict[str, float]
    total: float
    strong_categories: tuple[str, ...]

    @property
    def gross(self) -> float:
        return sum(self.categories.values())

    @property
    def penalty_total(self) -> float:
        return sum(self.penalties.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "categories": {k: round(v, 3) for k, v in self.categories.items()},
            "maximums": self.maximums,
            "penalties": {k: round(v, 3) for k, v in self.penalties.items()},
            "gross": round(self.gross, 3),
            "penalty_total": round(self.penalty_total, 3),
            "total": round(self.total, 3),
            "strong_categories": list(self.strong_categories),
        }


@dataclass(frozen=True, slots=True)
class SizingResult:
    approved: bool
    lots: float
    risk_money: float
    risk_pct: float
    risk_distance: float
    loss_per_lot: float
    commission_est: float
    slippage_est: float
    realised_risk: float
    reason: str = ""
    cross_check_delta: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "lots": self.lots,
            "risk_money": round(self.risk_money, 2),
            "risk_pct": round(self.risk_pct, 6),
            "realised_risk": round(self.realised_risk, 2),
            "loss_per_lot": round(self.loss_per_lot, 4),
            "commission_est": round(self.commission_est, 2),
            "slippage_est": round(self.slippage_est, 2),
            "reason": self.reason,
            "cross_check_delta": self.cross_check_delta,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """The complete record of one evaluation. Written whether or not it trades."""

    ts: datetime
    symbol: str
    classification: Classification
    mode: str
    plan: TradePlan | None = None
    score: float | None = None
    breakdown: ScoreBreakdown | None = None
    probability: float | None = None
    model_id: str | None = None
    model_health: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    gates: tuple[GateResult, ...] = ()
    reasons_for: tuple[str, ...] = ()
    reasons_against: tuple[str, ...] = ()
    sizing: SizingResult | None = None
    config_hash: str = ""
    git_sha: str = ""
    latency_ms: int = 0
    snapshot_id: int | None = None

    @property
    def is_trade(self) -> bool:
        return self.classification is not Classification.NO_TRADE

    @property
    def blocking_gate(self) -> str | None:
        for g in self.gates:
            if not g.passed:
                return g.name
        return None

    @property
    def all_blocking(self) -> tuple[str, ...]:
        return tuple(g.name for g in self.gates if not g.passed)

    def gate_trace(self) -> list[dict[str, Any]]:
        return [g.as_dict() for g in self.gates]

    def explain(self) -> str:
        """Human-readable answer to 'why did (or didn't) you take this trade?'."""
        lines: list[str] = []
        lines.append(f"DECISION @ {self.ts.isoformat()}  [{self.mode}]")
        lines.append(f"  Classification : {self.classification}")
        if self.plan:
            p = self.plan
            lines.append(f"  Strategy       : {p.strategy} v{p.strategy_version}")
            lines.append(f"  Direction      : {p.direction}")
            lines.append(
                f"  Entry / SL / TP: {p.entry:.2f} / {p.stop_loss:.2f} / "
                f"{p.final_target.price:.2f}   (RR {p.rr:.2f})"
            )
            lines.append(f"  Invalidation   : {p.invalidation}")
        if self.score is not None:
            lines.append(f"  Setup score    : {self.score:.1f} / 100")
        if self.probability is not None:
            lines.append(f"  Probability    : {self.probability * 100:.1f}%")
        if self.sizing:
            lines.append(
                f"  Size / risk    : {self.sizing.lots} lots, "
                f"{self.sizing.risk_pct * 100:.2f}% ({self.sizing.risk_money:.2f})"
            )
        if self.breakdown:
            lines.append("  Score breakdown:")
            for k, v in self.breakdown.categories.items():
                lines.append(f"      {k:<26} {v:5.1f} / {self.breakdown.maximums.get(k, 0):.0f}")
            for k, v in self.breakdown.penalties.items():
                if v:
                    lines.append(f"      PENALTY {k:<18} {-v:5.1f}")
        failed = [g for g in self.gates if not g.passed]
        if failed:
            lines.append("  BLOCKED BY:")
            for g in failed:
                lines.append(
                    f"      {g.name:<26} observed={g.observed!r} required={g.threshold!r}"
                    + (f"  — {g.detail}" if g.detail else "")
                )
        else:
            lines.append("  All gates passed.")
        if self.reasons_for:
            lines.append("  Reasons for:")
            lines.extend(f"      + {r}" for r in self.reasons_for)
        if self.reasons_against:
            lines.append("  Reasons against:")
            lines.extend(f"      - {r}" for r in self.reasons_against)
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    direction: Direction
    volume: float
    price: float
    stop_loss: float
    take_profit: float | None
    client_tag: str
    magic: int
    comment: str
    max_slippage_points: int = 20
    filling_mode: int | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    status: OrderStatus
    retcode: int = 0
    retcode_text: str = ""
    ticket: int | None = None
    position_ticket: int | None = None
    filled_volume: float = 0.0
    fill_price: float = 0.0
    comment: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ambiguous(self) -> bool:
        """A send whose outcome we do not know. NEVER resend on this - reconcile."""
        return self.status is OrderStatus.RECONCILING


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    magic: int = 0
    comment: str = ""
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0

    @property
    def client_tag(self) -> str:
        """Recover our idempotency tag from the broker comment field."""
        return self.comment.split(":")[-1] if ":" in self.comment else ""


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    deal: int
    position: int
    volume: float
    price: float
    commission: float = 0.0
    swap: float = 0.0
    profit: float = 0.0
    slippage_points: float = 0.0


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A completed round trip. R-multiple is the unit of account everywhere."""

    opened_at: datetime
    closed_at: datetime
    symbol: str
    direction: Direction
    strategy: str
    classification: Classification
    entry: float
    initial_sl: float
    exit_price: float
    volume: float
    risk_money: float
    gross_pnl: float
    commission: float
    swap: float
    exit_reason: ExitReason
    mae_r: float = 0.0
    mfe_r: float = 0.0
    bars_held: int = 0
    session: Session = Session.OFF
    regime: Regime = Regime.RANGE
    score: float | None = None
    probability: float | None = None
    decision_id: int | None = None
    planned_rr_at_entry: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - abs(self.commission) + self.swap

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.risk_money if self.risk_money > 0 else 0.0

    @property
    def is_win(self) -> bool:
        return self.r_multiple > 0.05

    @property
    def is_loss(self) -> bool:
        return self.r_multiple < -0.05

    @property
    def is_breakeven(self) -> bool:
        return not self.is_win and not self.is_loss

    @property
    def realised_rr(self) -> float:
        """How far price actually travelled, in units of the initial risk.

        NOT the planned RR — that is `planned_rr`, carried from the trade plan. The two
        were previously conflated, which made `avg_rr_planned` in a validation report
        show sub-1.0 values for a system with a hard 2.0 floor.
        """
        d = abs(self.entry - self.initial_sl)
        return abs(self.exit_price - self.entry) / d if d > 0 else 0.0

    @property
    def planned_rr(self) -> float:
        """The RR the plan targeted. Set at entry; 0.0 when unknown."""
        return self.planned_rr_at_entry
