"""Pull real bar history from the broker into the database.

This is the missing half of a pair. `BarRepository.upsert_many` could always store
history and `Broker.bars` could always fetch it, but nothing ever introduced them, so
the only backtest an operator could actually run was `--synthetic` — and the test suite
asserts that synthetic data produces no trades (`test_no_edge_data_produces_no_trades`).
The result was a system that looked like it was refusing to trade on quality grounds
when it was really refusing to trade on data grounds, which are very different things.

Two rules shape everything here:

**Never invent a bar.** When the broker returns fewer bars than asked for, that is the
end of what the account can see. The report says so; the gap is not filled, smoothed,
or interpolated. A backtest over invented history is worse than no backtest.

**Never store a bar that is still forming.** `copy_rates_from` happily returns the
current, incomplete bar. Stored, it is indistinguishable from a closed one on re-read,
and every downstream consumer — resampling, ATR, structure — would treat a partial
high/low as final. That is a look-ahead defect written permanently to disk, so the
open bar is dropped at the boundary rather than trusted to be filtered later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.domain.enums import Timeframe
from xauusd.domain.types import Bar
from xauusd.execution.broker import Broker
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

# MT5 serves large requests happily, but a smaller chunk keeps the RPC well under the
# bridge's 60s timeout and lets a long harvest report progress as it goes.
CHUNK = 5000


@dataclass(frozen=True, slots=True)
class HarvestReport:
    symbol: str
    timeframe: Timeframe
    requested: int
    fetched: int
    added: int
    duplicates: int
    dropped_forming: int
    earliest: datetime | None
    latest: datetime | None
    exhausted: bool  # the broker ran out of history before we had enough

    @property
    def short(self) -> bool:
        return self.fetched < self.requested

    def summary(self) -> str:
        if self.earliest is None:
            return f"no bars available for {self.symbol} {self.timeframe}"
        span = f"{self.earliest:%Y-%m-%d} -> {self.latest:%Y-%m-%d}"
        line = (
            f"{self.symbol} {self.timeframe}: {self.added} new, "
            f"{self.duplicates} already held, {span}"
        )
        if self.dropped_forming:
            line += f" (dropped {self.dropped_forming} still-forming)"
        if self.exhausted and self.short:
            line += (
                f"\n  the broker only served {self.fetched} of the {self.requested} "
                f"requested — that is all the history this account can see"
            )
        return line


def _complete_only(bars: list[Bar], tf: Timeframe, now: datetime) -> tuple[list[Bar], int]:
    """Drop any bar whose close time has not passed yet."""
    keep = [b for b in bars if b.ts + timedelta(seconds=tf.seconds) <= now]
    return keep, len(bars) - len(keep)


def harvest(
    broker: Broker,
    db: Database,
    symbol: str,
    timeframe: Timeframe = Timeframe.M5,
    wanted: int = 60_000,
    source: str = "mt5",
    chunk: int = CHUNK,
    now: datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> HarvestReport:
    """Walk backwards from the most recent bar until `wanted` bars are held or history ends.

    Each chunk is committed as it arrives. A harvest of 60,000 M5 bars is a few minutes
    of RPC, and an operator who closes the window halfway through should keep what was
    already fetched rather than starting again from nothing.
    """
    now = now or datetime.now(UTC)
    end: datetime | None = None
    fetched = added = duplicates = dropped = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    exhausted = False

    while fetched < wanted:
        want = min(chunk, wanted - fetched)
        batch = broker.bars(symbol, timeframe, want, end)
        if not batch:
            exhausted = True
            break

        batch.sort(key=lambda b: b.ts)
        batch, forming = _complete_only(batch, timeframe, now)
        dropped += forming
        if not batch:
            exhausted = True
            break

        # No progress backwards means the broker is serving the same window again;
        # continuing would loop forever re-writing bars we already hold.
        if earliest is not None and batch[0].ts >= earliest:
            exhausted = True
            break

        with db.session() as session:
            new = Repositories(session).bars.upsert_many(symbol, timeframe, batch, source=source)
            session.commit()

        fetched += len(batch)
        added += new
        duplicates += len(batch) - new
        earliest = batch[0].ts if earliest is None else min(earliest, batch[0].ts)
        latest = batch[-1].ts if latest is None else max(latest, batch[-1].ts)
        if progress:
            progress(fetched, wanted)
        log.info(
            "harvest_chunk",
            symbol=symbol,
            timeframe=str(timeframe),
            got=len(batch),
            new=new,
            earliest=batch[0].ts.isoformat(),
        )

        # The broker served less than a full chunk: there is nothing older to ask for.
        if len(batch) + forming < want:
            exhausted = True
            break
        end = batch[0].ts

    return HarvestReport(
        symbol=symbol,
        timeframe=timeframe,
        requested=wanted,
        fetched=fetched,
        added=added,
        duplicates=duplicates,
        dropped_forming=dropped,
        earliest=earliest,
        latest=latest,
        exhausted=exhausted,
    )


def coverage(
    db: Database, symbol: str, timeframe: Timeframe = Timeframe.M5, source: str = "mt5"
) -> tuple[int, datetime | None, datetime | None]:
    """How much real history is held: (bar count, earliest, latest)."""
    with db.session() as session:
        bars = Repositories(session).bars.load(symbol, timeframe, source=source)
    if not bars:
        return 0, None, None
    return len(bars), bars[0].ts, bars[-1].ts


def stored_symbols(db: Database, timeframe: Timeframe, source: str = "mt5") -> dict[str, int]:
    """Which symbols actually have bars, and how many. Ordered most-held first."""
    from sqlalchemy import func, select

    from xauusd.database.models import BarRow

    with db.session() as session:
        rows = session.execute(
            select(BarRow.symbol, func.count())
            .where(BarRow.timeframe == str(timeframe), BarRow.source == source)
            .group_by(BarRow.symbol)
        ).all()
    return dict(sorted(((str(s), int(n)) for s, n in rows), key=lambda kv: -kv[1]))


def resolve_stored_symbol(
    db: Database, configured: str, timeframe: Timeframe, source: str = "mt5"
) -> tuple[str, str]:
    """The symbol to READ history under, given what was actually stored.

    `harvest` writes under the symbol the broker resolved to — this broker calls spot
    gold `GOLD` — while offline consumers default to the configured name. When those
    differ, a successful harvest is followed by "0 bars in the database" against a full
    table, and the operator has no way to see why. That is the same divergence that
    broke `doctor` twice, in a third place.

    Returns (symbol, note). The note is empty when the configured name was used, and
    explains the substitution otherwise, so the choice is never silent.
    """
    held = stored_symbols(db, timeframe, source)
    if held.get(configured):
        return configured, ""
    if not held:
        return configured, ""
    if len(held) == 1:
        only = next(iter(held))
        return only, (
            f"no {timeframe} history under {configured!r}; using {only!r} "
            f"({held[only]} bars), which is what the broker resolved to when harvesting"
        )
    names = ", ".join(f"{k} ({v})" for k, v in held.items())
    raise SystemExit(
        f"history is held under several symbols and none is {configured!r}: {names}.\n"
        f"Set XAUUSD_SYMBOL or XAUUSD_DATA__SYMBOL_OVERRIDE to the one you mean."
    )
