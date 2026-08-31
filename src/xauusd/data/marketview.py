"""MarketView — the point-in-time boundary.

This is the single most important class in the system. Every analyser reads the world
through it, and it is PHYSICALLY INCAPABLE of returning data timestamped after the
evaluation instant. Look-ahead bias is prevented structurally, not by discipline.

Two rules it enforces:

  1. A bar is visible only when it has CLOSED at or before `now`. The bar currently
     forming is never returned: its high and low are not yet known, and a backtest
     that peeks at them manufactures an edge that does not survive contact with a live
     market.
  2. Exogenous data (macro, calendar actuals, news) is filtered on when it was
     PUBLISHED, not when it refers to. Handled by the repositories; MarketView passes
     `now` through so they can.

The same class serves live and backtest. In live it wraps a cache over the broker/DB;
in backtest it wraps a preloaded store and advances a cursor. Nothing downstream can
tell the difference — which is the point.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np

from xauusd.data.series import BarSeries
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar, Quote


class LookAheadError(RuntimeError):
    """Raised on any attempt to read data the evaluation instant cannot see.

    This is a programming error, not a data problem. It fails loudly on purpose.
    """


class BarSource(Protocol):
    def series(self, symbol: str, tf: Timeframe) -> BarSeries: ...


class InMemoryBarSource:
    """Backtest source: the full history, from which the view exposes only the past."""

    def __init__(self, data: Mapping[Timeframe, BarSeries] | None = None) -> None:
        self._data: dict[Timeframe, BarSeries] = dict(data or {})

    def set(self, tf: Timeframe, series: BarSeries) -> None:
        self._data[tf] = series

    def set_bars(self, tf: Timeframe, bars: list[Bar]) -> None:
        self._data[tf] = BarSeries.from_bars(tf, bars)

    def series(self, symbol: str, tf: Timeframe) -> BarSeries:
        return self._data.get(tf) or BarSeries.empty(tf)

    def timeframes(self) -> list[Timeframe]:
        return list(self._data)

    def full_range(self, tf: Timeframe) -> tuple[datetime, datetime] | None:
        s = self._data.get(tf)
        if not s or not len(s):
            return None
        return (
            datetime.fromtimestamp(int(s.ts[0]), UTC),
            datetime.fromtimestamp(int(s.ts[-1]), UTC),
        )


class MarketView:
    """A read-only window on the market as of exactly one instant."""

    __slots__ = ("_cache", "_now", "_quote", "_source", "_strict", "_symbol")

    def __init__(
        self,
        source: BarSource,
        symbol: str,
        now: datetime,
        quote: Quote | None = None,
        strict: bool = True,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("MarketView.now must be timezone-aware (UTC)")
        self._source = source
        self._symbol = symbol
        self._now = now.astimezone(UTC)
        self._quote = quote
        self._strict = strict
        self._cache: dict[tuple[Timeframe, int], BarSeries] = {}

    # -- identity ----------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def symbol(self) -> str:
        return self._symbol

    def at(self, now: datetime, quote: Quote | None = None) -> MarketView:
        """A new view at a later instant. Views are never mutated in place."""
        return MarketView(self._source, self._symbol, now, quote or self._quote, self._strict)

    # -- bars --------------------------------------------------------------------------

    def _visible(self, tf: Timeframe) -> BarSeries:
        """Every bar CLOSED at or before `now`. The forming bar is excluded."""
        key = (tf, -1)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        full = self._source.series(self._symbol, tf)
        if not len(full):
            self._cache[key] = full
            return full
        # A bar opening at ts closes at ts + tf.seconds. Visible iff close <= now.
        cutoff = int(self._now.timestamp()) - tf.seconds
        n_visible = int(np.searchsorted(full.ts, cutoff, side="right"))
        view = full.slice(0, n_visible)
        self._cache[key] = view
        return view

    def bars(self, tf: Timeframe, count: int | None = None) -> BarSeries:
        """The last `count` closed bars. Never returns anything not yet closed."""
        vis = self._visible(tf)
        if count is None or count >= len(vis):
            return vis
        key = (tf, count)
        cached = self._cache.get(key)
        if cached is None:
            cached = vis.tail(count)
            self._cache[key] = cached
        return cached

    def bar_count(self, tf: Timeframe) -> int:
        return len(self._visible(tf))

    def has_bars(self, tf: Timeframe, minimum: int) -> bool:
        return self.bar_count(tf) >= minimum

    def last_bar(self, tf: Timeframe) -> Bar | None:
        vis = self._visible(tf)
        return vis.last if len(vis) else None

    def last_close(self, tf: Timeframe = Timeframe.M5) -> float | None:
        vis = self._visible(tf)
        return float(vis.close[-1]) if len(vis) else None

    def bars_between(
        self, tf: Timeframe, start: datetime, end: datetime | None = None
    ) -> BarSeries:
        """Closed bars in [start, end]. `end` is clamped to `now` — it cannot exceed it."""
        end = min(end or self._now, self._now)
        if self._strict and end > self._now:
            raise LookAheadError(f"requested bars to {end} from a view at {self._now}")
        vis = self._visible(tf)
        if not len(vis):
            return vis
        i0 = int(np.searchsorted(vis.ts, int(start.timestamp()), side="left"))
        i1 = int(np.searchsorted(vis.ts, int(end.timestamp()), side="right"))
        return vis.slice(i0, i1)

    # -- the guard rails ---------------------------------------------------------------

    def future_bars(self, tf: Timeframe, count: int) -> BarSeries:
        """Deliberately unavailable.

        This method exists ONLY so that an attempt to reach forward fails loudly with an
        explanatory message instead of quietly working somewhere in a refactor.
        """
        raise LookAheadError(
            "MarketView cannot see the future. If a backtest needs forward bars to "
            "label outcomes, use the labelling module, which operates outside the "
            "decision path and never feeds a feature."
        )

    def assert_not_future(self, ts: datetime, what: str = "timestamp") -> None:
        """Guard for any externally supplied timestamp entering the analysis path."""
        if ts > self._now:
            raise LookAheadError(
                f"{what} {ts.isoformat()} is after the view instant {self._now.isoformat()}"
            )

    # -- quote -------------------------------------------------------------------------

    @property
    def quote(self) -> Quote | None:
        return self._quote

    def price(self) -> float:
        """Best available current price: live quote mid, else last closed M5/M1 close."""
        if self._quote is not None:
            return self._quote.mid
        for tf in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1):
            c = self.last_close(tf)
            if c is not None:
                return c
        raise LookAheadError("no price available at this instant")

    def quote_age_seconds(self) -> float:
        if self._quote is None:
            return float("inf")
        return (self._now - self._quote.ts).total_seconds()

    def bar_age_seconds(self, tf: Timeframe = Timeframe.M5) -> float:
        """Seconds since the last closed bar of `tf` finished. Staleness detector."""
        vis = self._visible(tf)
        if not len(vis):
            return float("inf")
        return (self._now - vis.close_time(len(vis) - 1)).total_seconds()

    # -- misc --------------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self._symbol,
            "now": self._now.isoformat(),
            "bars": {
                str(tf): self.bar_count(tf) for tf in Timeframe.ordered() if self.bar_count(tf)
            },
            "quote_age_s": self.quote_age_seconds(),
        }

    def __repr__(self) -> str:
        return f"MarketView({self._symbol} @ {self._now.isoformat()})"
