"""Economic calendar filter and the blackout state machine.

Two things here are not the obvious implementation, and both matter:

1. **Our own impact mapping.** Provider "impact" ratings are inconsistent and are not
   about gold. We map a normalised event key to (impact, gold_relevance), so FOMC and
   CPI are CRITICAL for gold regardless of how a provider stars them, and a euro-area
   consumer-confidence print is not.

2. **Post-event re-entry is a STATE, not a timer.** Waiting "30 minutes" and then
   trading is how a bot gets filled into a re-priced market on a spread three times
   normal. Re-entry additionally requires spread and volatility to have normalised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from xauusd.config.settings import NewsConfig
from xauusd.domain.enums import EventImpact

# --------------------------------------------------------------------------------------
# Normalisation: provider event names -> our key -> (impact, gold relevance 0..10)
# --------------------------------------------------------------------------------------

_PATTERNS: list[tuple[str, str]] = [
    (r"fomc.*(rate|decision|statement)|federal funds rate", "FOMC_RATE"),
    (r"fomc.*(press|conference)|powell.*(press|conference)", "FOMC_PRESSER"),
    (r"fomc.*minutes", "FOMC_MINUTES"),
    (r"(powell|fed chair).*(speak|testimony|remarks)", "FED_CHAIR_SPEAKS"),
    (r"non.?farm|nfp|nonfarm payroll", "US_NFP"),
    (r"unemployment rate", "US_UNEMPLOYMENT"),
    (r"average hourly earnings", "US_AHE"),
    (r"(initial )?jobless claims", "US_JOBLESS"),
    (r"core cpi", "US_CPI_CORE"),
    (r"\bcpi\b|consumer price index", "US_CPI"),
    (r"core pce", "US_PCE_CORE"),
    (r"\bpce\b", "US_PCE"),
    (r"\bppi\b|producer price", "US_PPI"),
    (r"ism.*manufacturing", "US_ISM_MFG"),
    (r"ism.*(services|non.?manufacturing)", "US_ISM_SVC"),
    (r"\bgdp\b", "US_GDP"),
    (r"retail sales", "US_RETAIL_SALES"),
    (r"(michigan|consumer sentiment)", "US_SENTIMENT"),
    (r"jolts", "US_JOLTS"),
    (r"ecb.*(rate|decision)", "ECB_RATE"),
    (r"boe.*(rate|decision)", "BOE_RATE"),
    (r"boj.*(rate|decision)", "BOJ_RATE"),
]

# (impact, gold relevance). Gold relevance is OUR judgement, not a provider's stars.
_KEY_IMPACT: dict[str, tuple[EventImpact, int]] = {
    "FOMC_RATE": (EventImpact.CRITICAL, 10),
    "FOMC_PRESSER": (EventImpact.CRITICAL, 10),
    "US_CPI": (EventImpact.CRITICAL, 10),
    "US_CPI_CORE": (EventImpact.CRITICAL, 10),
    "US_NFP": (EventImpact.CRITICAL, 9),
    "US_PCE_CORE": (EventImpact.CRITICAL, 9),
    "FED_CHAIR_SPEAKS": (EventImpact.HIGH, 8),
    "US_PCE": (EventImpact.HIGH, 8),
    "FOMC_MINUTES": (EventImpact.HIGH, 7),
    "US_PPI": (EventImpact.HIGH, 7),
    "US_UNEMPLOYMENT": (EventImpact.HIGH, 7),
    "US_AHE": (EventImpact.HIGH, 6),
    "US_GDP": (EventImpact.HIGH, 6),
    "US_ISM_MFG": (EventImpact.HIGH, 6),
    "US_ISM_SVC": (EventImpact.HIGH, 6),
    "US_RETAIL_SALES": (EventImpact.MEDIUM, 5),
    "US_JOBLESS": (EventImpact.MEDIUM, 5),
    "US_JOLTS": (EventImpact.MEDIUM, 4),
    "US_SENTIMENT": (EventImpact.MEDIUM, 4),
    "ECB_RATE": (EventImpact.HIGH, 6),
    "BOE_RATE": (EventImpact.MEDIUM, 4),
    "BOJ_RATE": (EventImpact.MEDIUM, 4),
}


def normalize_event(name: str, currency: str = "USD") -> str | None:
    low = name.lower()
    for pattern, key in _PATTERNS:
        if re.search(pattern, low):
            if key.startswith("US_") or key.startswith("FOMC") or key.startswith("FED"):
                return key if currency.upper() in ("USD", "") else None
            return key
    return None


def classify_event(name: str, currency: str = "USD") -> tuple[EventImpact, int, str | None]:
    """(impact, gold_relevance, normalized_key). Unknown events are LOW/0."""
    key = normalize_event(name, currency)
    if key is None:
        return EventImpact.LOW, 0, None
    impact, relevance = _KEY_IMPACT[key]
    # A non-USD event matters less to gold even when it is a big event locally.
    if currency.upper() not in ("USD", "") and impact is EventImpact.CRITICAL:
        impact = EventImpact.HIGH
    return impact, relevance, key


# --------------------------------------------------------------------------------------
# Blackout
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    ts: datetime
    name: str
    currency: str
    impact: EventImpact
    gold_relevance: int
    normalized_key: str | None = None
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None


@dataclass(frozen=True, slots=True)
class BlackoutState:
    active: bool
    reason: str | None
    until: datetime | None
    event: CalendarEvent | None
    phase: str  # PRE | EVENT | POST | STABILISING | CLEAR
    next_event: CalendarEvent | None
    minutes_to_next: float | None

    @property
    def blocks_entry(self) -> bool:
        return self.active


class CalendarFilter:
    def __init__(self, config: NewsConfig | None = None) -> None:
        self.cfg = config or NewsConfig()

    def _window(self, impact: EventImpact) -> tuple[int, int]:
        return (
            self.cfg.blackout_minutes_before.get(str(impact), 0),
            self.cfg.blackout_minutes_after.get(str(impact), 0),
        )

    def evaluate(
        self,
        now: datetime,
        events: list[CalendarEvent],
        spread_ratio: float | None = None,
        atr_ratio: float | None = None,
        min_relevance: int = 4,
    ) -> BlackoutState:
        """Blackout state at `now`.

        `spread_ratio` and `atr_ratio` are current-vs-normal. When the timer has expired
        but conditions have not normalised, the state is STABILISING and still blocks.
        """
        relevant = sorted(
            (e for e in events if e.gold_relevance >= min_relevance), key=lambda e: e.ts
        )
        upcoming = [e for e in relevant if e.ts >= now]
        next_event = upcoming[0] if upcoming else None
        minutes_to_next = (next_event.ts - now).total_seconds() / 60.0 if next_event else None

        for e in relevant:
            before, after = self._window(e.impact)
            start = e.ts - timedelta(minutes=before)
            end = e.ts + timedelta(minutes=after)
            if not (start <= now <= end):
                continue
            if now < e.ts:
                phase = "PRE"
                reason = f"{e.name} ({e.impact}) in {(e.ts - now).total_seconds() / 60:.0f} min"
            else:
                phase = "POST"
                reason = f"{e.name} ({e.impact}) released {(now - e.ts).total_seconds() / 60:.0f} min ago"
            return BlackoutState(True, reason, end, e, phase, next_event, minutes_to_next)

        # Timer expired: require conditions to have actually normalised.
        if self.cfg.require_spread_normalised_after:
            recent = [
                e
                for e in relevant
                if e.impact in (EventImpact.CRITICAL, EventImpact.HIGH)
                and 0 <= (now - e.ts).total_seconds() / 60 <= self._window(e.impact)[1] + 60
            ]
            if recent:
                latest = recent[-1]
                bad_spread = (
                    spread_ratio is not None and spread_ratio > self.cfg.post_event_spread_ratio
                )
                bad_vol = atr_ratio is not None and atr_ratio > self.cfg.post_event_atr_ratio
                if bad_spread or bad_vol:
                    why = []
                    if bad_spread:
                        why.append(f"spread {spread_ratio:.1f}x normal")
                    if bad_vol:
                        why.append(f"volatility {atr_ratio:.1f}x normal")
                    return BlackoutState(
                        True,
                        f"post-{latest.name} conditions not normalised: {', '.join(why)}",
                        None,
                        latest,
                        "STABILISING",
                        next_event,
                        minutes_to_next,
                    )

        return BlackoutState(False, None, None, None, "CLEAR", next_event, minutes_to_next)


# --------------------------------------------------------------------------------------
# Fallback schedule — the safety net when every feed fails
# --------------------------------------------------------------------------------------

FALLBACK_SCHEDULE_YAML = """\
# Curated critical-event schedule. This is the LAST line of defence: if every calendar
# feed is unavailable, blackout windows around the events that actually move gold still
# apply. Maintaining this takes a few minutes per quarter and is worth it.
#
# Times are UTC. Recurrence rules are interpreted by RecurringEventSchedule.
events:
  - key: US_NFP
    name: US Non-Farm Payrolls
    currency: USD
    rule: "first_friday"
    time: "13:30"
  - key: US_CPI
    name: US Consumer Price Index
    currency: USD
    rule: "monthly_day_range"
    day_from: 10
    day_to: 15
    time: "13:30"
  - key: US_JOBLESS
    name: US Initial Jobless Claims
    currency: USD
    rule: "weekly_thursday"
    time: "13:30"
  - key: FOMC_RATE
    name: FOMC Rate Decision
    currency: USD
    rule: "explicit"
    dates: []   # fill from the Fed's published calendar each year
    time: "19:00"
"""


@dataclass(frozen=True, slots=True)
class RecurringRule:
    key: str
    name: str
    currency: str
    rule: str
    time: str
    day_from: int = 1
    day_to: int = 28
    dates: tuple[str, ...] = ()


class RecurringEventSchedule:
    """Generates fallback events from recurrence rules.

    Deliberately approximate: NFP is the first Friday, CPI lands somewhere in the 10th
    to 15th. A blackout that is a day wide around the true release is far better than
    no blackout at all, which is the alternative when a feed is down.
    """

    def __init__(self, rules: list[RecurringRule] | None = None) -> None:
        self.rules = rules or self.default_rules()

    @staticmethod
    def default_rules() -> list[RecurringRule]:
        return [
            RecurringRule("US_NFP", "US Non-Farm Payrolls", "USD", "first_friday", "13:30"),
            RecurringRule(
                "US_CPI",
                "US Consumer Price Index",
                "USD",
                "monthly_day_range",
                "13:30",
                day_from=10,
                day_to=15,
            ),
            RecurringRule(
                "US_JOBLESS", "US Initial Jobless Claims", "USD", "weekly_thursday", "13:30"
            ),
        ]

    def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        day = start.date()
        last = end.date()
        while day <= last:
            for r in self.rules:
                if not self._matches(r, day):
                    continue
                hh, mm = (int(x) for x in r.time.split(":"))
                ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)
                if not (start <= ts <= end):
                    continue
                impact, relevance, _ = classify_event(r.name, r.currency)
                out.append(CalendarEvent(ts, r.name, r.currency, impact, relevance, r.key))
            day = day + timedelta(days=1)
        return sorted(out, key=lambda e: e.ts)

    @staticmethod
    def _matches(rule: RecurringRule, day: date) -> bool:
        if rule.rule == "first_friday":
            return day.weekday() == 4 and day.day <= 7
        if rule.rule == "weekly_thursday":
            return day.weekday() == 3
        if rule.rule == "monthly_day_range":
            return rule.day_from <= day.day <= rule.day_to and day.weekday() < 5
        if rule.rule == "explicit":
            return day.isoformat() in rule.dates
        return False
