"""Economic calendar providers, in the layered order from 04-data-sources.md.

  1. the MT5 terminal's own calendar, relayed by the bridge   (free, broker clock)
  2. a commercial API                                          (optional)
  3. a curated recurring schedule                              (the safety net)

`LayeredCalendarProvider` tries each in order and reports which one answered, so the
dashboard can show that the system is running on the fallback rather than silently
degrading.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from xauusd.intelligence.economic_calendar import (
    CalendarEvent,
    RecurringEventSchedule,
    classify_event,
)
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

# MT5 CalendarEvent importance: 0 none, 1 low, 2 moderate, 3 high
_MT5_IMPORTANCE = {0: "LOW", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}


class CalendarProvider(Protocol):
    name: str

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class BridgeCalendarProvider:
    """The MT5 terminal's built-in calendar, via the bridge.

    Free, already installed, and on the broker's clock. Note the Python binding does
    not expose the calendar on every build; when it does not, the bridge returns an
    empty list and the next layer takes over.
    """

    name = "mt5_terminal"

    def __init__(self, broker) -> None:  # type: ignore[no-untyped-def]
        self.broker = broker

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        try:
            rows = (
                self.broker.transport.call(
                    "calendar",
                    {"from_ts": start.timestamp(), "to_ts": end.timestamp()},
                    timeout=30.0,
                )
                or []
            )
        except Exception as exc:
            log.warning("bridge_calendar_failed", error=str(exc))
            return []
        out: list[CalendarEvent] = []
        for r in rows:
            name = str(r.get("name", ""))
            currency = str(r.get("currency", ""))
            if not name:
                continue
            impact, relevance, key = classify_event(name, currency)
            out.append(
                CalendarEvent(
                    ts=datetime.fromtimestamp(float(r["time"]), UTC),
                    name=name,
                    currency=currency,
                    impact=impact,
                    gold_relevance=relevance,
                    normalized_key=key,
                    actual=r.get("actual"),
                    forecast=r.get("forecast"),
                    previous=r.get("previous"),
                )
            )
        return sorted(out, key=lambda e: e.ts)


class RepositoryCalendarProvider:
    """Events already persisted, read POINT-IN-TIME.

    `known_at` filters on first_seen_at so a backtest cannot see an event row that had
    not been published yet — and actuals are masked separately by `mask_future_actuals`.
    """

    name = "database"

    def __init__(self, repo, as_of: datetime | None = None) -> None:  # type: ignore[no-untyped-def]
        self.repo = repo
        self.as_of = as_of

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        rows = (
            self.repo.known_at(self.as_of, start, end)
            if self.as_of
            else self.repo.events_between(start, end)
        )
        out: list[CalendarEvent] = []
        for r in rows:
            from xauusd.domain.enums import EventImpact

            out.append(
                CalendarEvent(
                    ts=r.scheduled_ts
                    if r.scheduled_ts.tzinfo
                    else r.scheduled_ts.replace(tzinfo=UTC),
                    name=r.name,
                    currency=r.currency,
                    impact=EventImpact(r.impact),
                    gold_relevance=r.gold_relevance,
                    normalized_key=r.normalized_key,
                    actual=r.actual,
                    forecast=r.forecast,
                    previous=r.previous,
                )
            )
        return out


class FallbackCalendarProvider:
    """Curated recurring schedule. Never fails, never needs a network."""

    name = "curated_fallback"

    def __init__(self, schedule: RecurringEventSchedule | None = None) -> None:
        self.schedule = schedule or RecurringEventSchedule()

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return self.schedule.events_between(start, end)


class LayeredCalendarProvider:
    """Try providers in order; report which one answered."""

    name = "layered"

    def __init__(self, providers: list[CalendarProvider]) -> None:
        self.providers = providers
        self.last_source: str = "none"

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        for p in self.providers:
            try:
                evs = p.events(start, end)
            except Exception as exc:
                log.warning("calendar_provider_failed", provider=p.name, error=str(exc))
                continue
            if evs:
                self.last_source = p.name
                if p.name == "curated_fallback":
                    log.warning(
                        "calendar_on_fallback",
                        detail="live calendar feeds unavailable; using the curated schedule",
                    )
                return evs
        self.last_source = "none"
        return []


def mask_future_actuals(events: list[CalendarEvent], as_of: datetime) -> list[CalendarEvent]:
    """Strip actual values from events that had not yet been released at `as_of`.

    A calendar row that already knows Friday's NFP print is the single most direct way
    to leak the future into a backtest.
    """
    from dataclasses import replace

    return [e if e.ts <= as_of else replace(e, actual=None) for e in events]
