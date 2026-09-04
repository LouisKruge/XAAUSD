"""Canonical enumerations. Every string that crosses a module boundary is one of these."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str-subclass enum: compares equal to its value, serialises as its value."""

    def __str__(self) -> str:
        return str(self.value)


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"
    MN1 = "MN1"

    @property
    def seconds(self) -> int:
        return _TF_SECONDS[self]

    @property
    def rank(self) -> int:
        """Higher rank == higher timeframe. Used for HTF/LTF comparisons."""
        return _TF_ORDER.index(self)

    @classmethod
    def ordered(cls) -> tuple[Timeframe, ...]:
        return _TF_ORDER


_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
    Timeframe.MN1: 2592000,
}
_TF_ORDER: tuple[Timeframe, ...] = (
    Timeframe.M1,
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.W1,
    Timeframe.MN1,
)


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Bias(StrEnum):
    """Directional bias of a timeframe. NEUTRAL is a real answer, not a failure."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def sign(self) -> int:
        return {Bias.BULLISH: 1, Bias.BEARISH: -1, Bias.NEUTRAL: 0}[self]

    def agrees_with(self, direction: Direction) -> bool:
        return self.sign == direction.sign

    def conflicts_with(self, direction: Direction) -> bool:
        return self.sign == -direction.sign


class Classification(StrEnum):
    NO_TRADE = "NO_TRADE"
    A = "A"
    A_PLUS = "A_PLUS"
    # The short-duration tier. Separate from A/A+ rather than a lower grade of it:
    # it has its own score, its own gates and its own risk fraction, and mixing it
    # into the A ladder would let a scalp inherit an A's 1% by accident.
    SCALP = "SCALP"


class StructureKind(StrEnum):
    HH = "HH"
    HL = "HL"
    LH = "LH"
    LL = "LL"
    BOS = "BOS"
    CHOCH = "CHOCH"
    MSS = "MSS"


class SwingKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class SwingStrength(StrEnum):
    """A high is STRONG if price failed to take it and instead broke structure the other way."""

    STRONG = "STRONG"
    WEAK = "WEAK"
    UNTESTED = "UNTESTED"


class LiquidityKind(StrEnum):
    BSL = "BSL"
    SSL = "SSL"
    EQH = "EQH"
    EQL = "EQL"
    PDH = "PDH"
    PDL = "PDL"
    PWH = "PWH"
    PWL = "PWL"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    RANGE_HIGH = "RANGE_HIGH"
    RANGE_LOW = "RANGE_LOW"

    @property
    def is_buyside(self) -> bool:
        return self in _BUYSIDE

    @property
    def is_sellside(self) -> bool:
        return self in _SELLSIDE


_BUYSIDE = frozenset(
    {
        LiquidityKind.BSL,
        LiquidityKind.EQH,
        LiquidityKind.PDH,
        LiquidityKind.PWH,
        LiquidityKind.SESSION_HIGH,
        LiquidityKind.RANGE_HIGH,
    }
)
_SELLSIDE = frozenset(
    {
        LiquidityKind.SSL,
        LiquidityKind.EQL,
        LiquidityKind.PDL,
        LiquidityKind.PWL,
        LiquidityKind.SESSION_LOW,
        LiquidityKind.RANGE_LOW,
    }
)


class FVGState(StrEnum):
    UNMITIGATED = "UNMITIGATED"
    PARTIAL = "PARTIAL"
    MITIGATED = "MITIGATED"
    INVERTED = "INVERTED"
    INVALIDATED = "INVALIDATED"


class OrderBlockKind(StrEnum):
    BULL_OB = "BULL_OB"
    BEAR_OB = "BEAR_OB"
    BULL_BREAKER = "BULL_BREAKER"
    BEAR_BREAKER = "BEAR_BREAKER"
    MITIGATION = "MITIGATION"


class ZoneState(StrEnum):
    FRESH = "FRESH"
    TESTED = "TESTED"
    MITIGATED = "MITIGATED"
    INVALIDATED = "INVALIDATED"


class LevelKind(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"
    SUPPLY = "SUPPLY"
    DEMAND = "DEMAND"


class Session(StrEnum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP = "OVERLAP"
    OFF = "OFF"


class Killzone(StrEnum):
    ASIA_KZ = "ASIA_KZ"
    LONDON_KZ = "LONDON_KZ"
    NY_AM_KZ = "NY_AM_KZ"
    NY_PM_KZ = "NY_PM_KZ"
    NONE = "NONE"


class Regime(StrEnum):
    STRONG_BULL = "STRONG_BULL"
    MODERATE_BULL = "MODERATE_BULL"
    RANGE = "RANGE"
    MODERATE_BEAR = "MODERATE_BEAR"
    STRONG_BEAR = "STRONG_BEAR"
    NEWS_DRIVEN = "NEWS_DRIVEN"
    ABNORMAL = "ABNORMAL"

    @property
    def is_trending(self) -> bool:
        return self in {
            Regime.STRONG_BULL,
            Regime.MODERATE_BULL,
            Regime.MODERATE_BEAR,
            Regime.STRONG_BEAR,
        }

    @property
    def is_tradable(self) -> bool:
        """ABNORMAL is never tradable; everything else is subject to strategy whitelists."""
        return self is not Regime.ABNORMAL


class VolRegime(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class MacroBias(StrEnum):
    STRONGLY_BULLISH = "STRONGLY_BULLISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    STRONGLY_BEARISH = "STRONGLY_BEARISH"
    UNKNOWN = "UNKNOWN"

    @property
    def score(self) -> int:
        return {
            MacroBias.STRONGLY_BULLISH: 2,
            MacroBias.BULLISH: 1,
            MacroBias.NEUTRAL: 0,
            MacroBias.BEARISH: -1,
            MacroBias.STRONGLY_BEARISH: -2,
            MacroBias.UNKNOWN: 0,
        }[self]

    @property
    def is_known(self) -> bool:
        return self is not MacroBias.UNKNOWN


class NewsRisk(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    EXTREME = "EXTREME"

    @property
    def level(self) -> int:
        return {
            NewsRisk.LOW: 0,
            NewsRisk.MODERATE: 1,
            NewsRisk.HIGH: 2,
            NewsRisk.EXTREME: 3,
        }[self]

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, NewsRisk):
            return self.level <= other.level
        return NotImplemented


class EventImpact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"

    @property
    def is_real_money(self) -> bool:
        return self is Mode.LIVE


class OrderStatus(StrEnum):
    INTENT = "INTENT"
    SENT = "SENT"
    RECONCILING = "RECONCILING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.ABANDONED,
        }


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TP1 = "TP1"
    TP2 = "TP2"
    TRAIL = "TRAIL"
    BREAK_EVEN = "BREAK_EVEN"
    TIME_STOP = "TIME_STOP"
    INVALIDATION = "INVALIDATION"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"
    WEEKEND_FLAT = "WEEKEND_FLAT"
    END_OF_TEST = "END_OF_TEST"


class KillSwitchReason(StrEnum):
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"
    WEEKLY_DRAWDOWN = "WEEKLY_DRAWDOWN"
    MONTHLY_DRAWDOWN = "MONTHLY_DRAWDOWN"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"
    STALE_DATA = "STALE_DATA"
    SPREAD_ABNORMAL = "SPREAD_ABNORMAL"
    SLIPPAGE_EXCESSIVE = "SLIPPAGE_EXCESSIVE"
    NEWS_EXTREME = "NEWS_EXTREME"
    STATE_DIVERGENCE = "STATE_DIVERGENCE"
    DUPLICATE_POSITION = "DUPLICATE_POSITION"
    SPEC_CHANGED = "SPEC_CHANGED"
    RISK_INVARIANT = "RISK_INVARIANT"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    MANUAL = "MANUAL"

    @property
    def auto_clearable(self) -> bool:
        """Which conditions may clear themselves when the underlying condition resolves."""
        return self in {
            KillSwitchReason.DAILY_DRAWDOWN,
            KillSwitchReason.BROKER_UNREACHABLE,
            KillSwitchReason.STALE_DATA,
            KillSwitchReason.SPREAD_ABNORMAL,
            KillSwitchReason.NEWS_EXTREME,
        }


class ValidationStatus(StrEnum):
    """Gate on live routing. Enforced in code, not convention."""

    DEV = "DEV"
    IN_SAMPLE_PASSED = "IN_SAMPLE_PASSED"
    OOS_PASSED = "OOS_PASSED"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"

    @property
    def live_eligible(self) -> bool:
        return self in {
            ValidationStatus.OOS_PASSED,
            ValidationStatus.PAPER,
            ValidationStatus.DEMO,
            ValidationStatus.LIVE,
        }
