"""Micro market structure: the same engine, read on M1 and M5.

Deliberately not a second implementation. `StructureEngine` already walks swings and
emits BOS/CHOCH/MSS in confirmation order, and it is entirely driven by
`StructureConfig`. What differs on a fast timeframe is what counts as a swing and how
much displacement is meaningful, and both are already numbers in that config.

A second BOS detector would be a second thing to keep correct, a second thing to keep
look-ahead-safe, and the parity test only guards one of them.

What this module adds is the *assembly*: a snapshot of micro state at one instant,
built from the same `MarketView` the A/A+ snapshot uses, so nothing here can see a bar
the rest of the system cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from xauusd.config.settings import MicroStructureConfig, Settings
from xauusd.core.fair_value_gap import FVGEngine
from xauusd.core.liquidity import LiquidityEngine
from xauusd.core.order_blocks import OrderBlockEngine
from xauusd.core.structure import StructureEngine, atr_last, detect_swings
from xauusd.data.marketview import MarketView
from xauusd.data.series import BarSeries
from xauusd.domain.enums import Direction, StructureKind, Timeframe
from xauusd.domain.types import (
    FVG,
    LiquidityPool,
    OrderBlock,
    StructureEvent,
    Sweep,
)

# The fast pair. M1 gives the trigger, M5 gives the structure a stop can rest behind —
# the cost model rules out stops small enough to sit on M1 structure alone.
TRIGGER_TF = Timeframe.M1
STRUCTURE_TF = Timeframe.M5


@dataclass(slots=True)
class MicroSnapshot:
    """Micro state at one instant. Built from the same MarketView as MarketSnapshot."""

    ts: datetime
    atr_m1: float
    atr_m5: float
    events_m1: tuple[StructureEvent, ...] = ()
    events_m5: tuple[StructureEvent, ...] = ()
    sweeps: tuple[Sweep, ...] = ()
    fvgs: tuple[FVG, ...] = ()
    order_blocks: tuple[OrderBlock, ...] = ()
    pools: tuple[LiquidityPool, ...] = ()
    m1: BarSeries | None = None
    m5: BarSeries | None = None
    degraded: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """Warm-up is not an error, but nothing may be traded during it.

        ATR is NaN until the period fills, and every structural threshold is scaled by
        ATR — so a missing ATR does not mean 'use a default', it means the structure
        thresholds are undefined and no judgement is available.
        """
        return (
            self.atr_m1 == self.atr_m1
            and self.atr_m5 == self.atr_m5
            and self.atr_m1 > 0
            and self.atr_m5 > 0
            and not self.degraded
        )

    def last_event(self, tf: Timeframe, kind: StructureKind) -> StructureEvent | None:
        events = self.events_m1 if tf is TRIGGER_TF else self.events_m5
        return next((e for e in reversed(events) if e.kind is kind), None)

    def recent_shift(
        self, direction: Direction, within_seconds: float, tf: Timeframe = TRIGGER_TF
    ) -> StructureEvent | None:
        """The most recent CHOCH or MSS in `direction`, if it is recent enough.

        Recency is in wall-clock rather than bars so the same threshold means the same
        thing on M1 and M5, and so a data gap cannot silently make a stale event look
        fresh by bar count.
        """
        events = self.events_m1 if tf is TRIGGER_TF else self.events_m5
        cutoff = self.ts.timestamp() - within_seconds
        for e in reversed(events):
            if e.kind not in (StructureKind.CHOCH, StructureKind.MSS):
                continue
            if e.ts.timestamp() < cutoff:
                return None  # events are ordered; nothing newer qualifies
            if e.direction is direction:
                return e
        return None


class MicroAnalyzer:
    """Assembles a MicroSnapshot from a MarketView, at the view's own instant."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        cfg: MicroStructureConfig = self.settings.micro_structure
        self.structure = StructureEngine(cfg)
        self.liquidity = LiquidityEngine(self.settings.liquidity)
        self.fvg = FVGEngine(self.settings.fvg)
        self.order_blocks = OrderBlockEngine(self.settings.order_block)
        self.cfg = cfg

    def analyze(self, view: MarketView, m1_bars: int = 600, m5_bars: int = 400) -> MicroSnapshot:
        degraded: list[str] = []

        m1 = self._series(view, TRIGGER_TF, m1_bars, degraded)
        m5 = self._series(view, STRUCTURE_TF, m5_bars, degraded)
        if m1 is None or m5 is None:
            return MicroSnapshot(
                ts=view.now,
                atr_m1=float("nan"),
                atr_m5=float("nan"),
                degraded=tuple(degraded),
            )

        atr_m1 = atr_last(m1, self.cfg.atr_period)
        atr_m5 = atr_last(m5, self.cfg.atr_period)

        swings_m1 = self._swings(m1, atr_m1)
        swings_m5 = self._swings(m5, atr_m5)
        events_m1 = (
            self.structure.detect_events(m1, swings_m1, atr_m1)
            if swings_m1 or atr_m1 == atr_m1
            else []
        )
        events_m5 = (
            self.structure.detect_events(m5, swings_m5, atr_m5)
            if swings_m5 or atr_m5 == atr_m5
            else []
        )

        # Liquidity, gaps and blocks come from M5: they need a structure a stop can sit
        # behind, and an M1 order block is noise at this instrument's spread.
        pools: tuple[LiquidityPool, ...] = ()
        sweeps: tuple[Sweep, ...] = ()
        fvgs: tuple[FVG, ...] = ()
        obs: tuple[OrderBlock, ...] = ()
        if atr_m5 == atr_m5 and atr_m5 > 0:
            found_pools, found_sweeps = self.liquidity.analyze(m5, swings_m5)
            pools, sweeps = tuple(found_pools), tuple(found_sweeps)
            fvgs = tuple(self.fvg.detect(m5, atr_m5))
            obs = tuple(self.order_blocks.detect(m5, list(events_m5), atr_m5, list(fvgs)))

        return MicroSnapshot(
            ts=view.now,
            atr_m1=atr_m1,
            atr_m5=atr_m5,
            events_m1=tuple(events_m1),
            events_m5=tuple(events_m5),
            sweeps=sweeps,
            fvgs=fvgs,
            order_blocks=obs,
            pools=pools,
            m1=m1,
            m5=m5,
            degraded=tuple(degraded),
        )

    def _series(
        self, view: MarketView, tf: Timeframe, count: int, degraded: list[str]
    ) -> BarSeries | None:
        try:
            bars = view.bars(tf, count)
        except Exception as exc:
            degraded.append(f"{tf}: {type(exc).__name__}")
            return None
        if len(bars) < self.cfg.atr_period + 5:
            degraded.append(f"{tf}: only {len(bars)} bars, warming up")
            return None
        return BarSeries.from_bars(tf, bars)

    def _swings(self, series: BarSeries, atr_value: float) -> list:  # list[RawSwing]
        if atr_value != atr_value or atr_value <= 0:
            return []
        return detect_swings(series, self.cfg.swing_lookback, self.cfg.swing_min_atr, atr_value)
