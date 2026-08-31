"""Trading sessions, clocks and DST.

Gold trades ~23h/day and the broker's server clock is usually not UTC and shifts with
US DST. Getting this wrong silently corrupts every session statistic in the system, so
session boundaries are derived from tz-aware LOCAL times via zoneinfo and never from
fixed UTC offsets.

That means:
  * the London open moves correctly against UTC across the year;
  * the two annual weeks where UK and US DST disagree are handled automatically rather
    than being three days of quietly wrong statistics;
  * the broker's offset is MEASURED from its own timestamps, not configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from xauusd.config.settings import SessionConfig
from xauusd.domain.enums import Killzone, Session
from xauusd.domain.types import SessionState

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in_local_window(local_dt: datetime, start: str, end: str) -> bool:
    t = local_dt.time()
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    return s <= t < e if s <= e else (t >= s or t < e)


@dataclass(slots=True)
class BrokerClock:
    """Measures the broker server's offset from UTC instead of trusting configuration.

    Most brokers run GMT+2/+3 and shift with US DST, so the offset is not a constant.
    A sudden jump is a real event the engine must react to, because it means every
    session label computed from broker timestamps just changed meaning.
    """

    offset_seconds: int = 0
    samples: int = 0
    last_sample: datetime | None = None
    _last_offset: int = 0

    def observe(self, broker_time: datetime, utc_time: datetime) -> int | None:
        """Record a (broker_time, utc_time) pair. Returns a jump size if one occurred."""
        if broker_time.tzinfo is None:
            broker_time = broker_time.replace(tzinfo=UTC)
        raw = int((broker_time - utc_time).total_seconds())
        # Round to the nearest 15 minutes: brokers use whole/half/quarter-hour offsets,
        # so anything finer is quote latency, not a real offset change.
        rounded = int(round(raw / 900.0) * 900)
        previous = self.offset_seconds if self.samples else rounded
        self.offset_seconds = rounded
        self.samples += 1
        self.last_sample = utc_time
        jump = rounded - previous
        return jump if jump != 0 and self.samples > 1 else None

    def to_broker(self, utc_dt: datetime) -> datetime:
        return utc_dt + timedelta(seconds=self.offset_seconds)

    def to_utc(self, broker_dt: datetime) -> datetime:
        if broker_dt.tzinfo is None:
            broker_dt = broker_dt.replace(tzinfo=UTC)
        return broker_dt - timedelta(seconds=self.offset_seconds)

    @property
    def offset_hours(self) -> float:
        return self.offset_seconds / 3600.0


class SessionEngine:
    """Classifies an instant into session, killzone and tradability."""

    def __init__(
        self, config: SessionConfig | None = None, clock: BrokerClock | None = None
    ) -> None:
        self.cfg = config or SessionConfig()
        self.clock = clock or BrokerClock()
        self._holidays: set[date] = set()

    def add_holidays(self, days: list[date]) -> None:
        self._holidays.update(days)

    # -- session classification --------------------------------------------------------

    def session_for(self, utc_dt: datetime) -> Session:
        london = utc_dt.astimezone(LONDON)
        ny = utc_dt.astimezone(NEW_YORK)

        in_london = _in_local_window(london, self.cfg.london_local_start, self.cfg.london_local_end)
        in_ny = _in_local_window(ny, self.cfg.newyork_local_start, self.cfg.newyork_local_end)

        if in_london and in_ny:
            return Session.OVERLAP
        if in_london:
            return Session.LONDON
        if in_ny:
            return Session.NEW_YORK

        hour = utc_dt.hour
        a_start, a_end = self.cfg.asia_start_utc_hour, self.cfg.asia_end_utc_hour
        in_asia = hour >= a_start or hour < a_end if a_start > a_end else a_start <= hour < a_end
        return Session.ASIA if in_asia else Session.OFF

    def killzone_for(self, utc_dt: datetime) -> Killzone:
        london = utc_dt.astimezone(LONDON)
        ny = utc_dt.astimezone(NEW_YORK)
        if _in_local_window(london, *self.cfg.london_killzone_local):
            return Killzone.LONDON_KZ
        if _in_local_window(ny, *self.cfg.ny_am_killzone_local):
            return Killzone.NY_AM_KZ
        if _in_local_window(ny, *self.cfg.ny_pm_killzone_local):
            return Killzone.NY_PM_KZ
        if _in_local_window(utc_dt, *self.cfg.asia_killzone_utc):
            return Killzone.ASIA_KZ
        return Killzone.NONE

    def is_market_open(self, utc_dt: datetime) -> bool:
        """Gold trades Sunday 22:00 UTC to Friday 21:00 UTC, roughly, with a daily break."""
        wd = utc_dt.weekday()
        if wd == 5:  # Saturday
            return False
        if wd == 6 and utc_dt.hour < 22:  # Sunday before the open
            return False
        if wd == 4 and utc_dt.hour >= 21:  # Friday after the close
            return False
        if utc_dt.date() in self._holidays:
            return False
        return True

    def minutes_into_session(self, utc_dt: datetime, session: Session) -> int:
        if session is Session.OFF:
            return 0
        if session in (Session.LONDON, Session.OVERLAP):
            local, start = utc_dt.astimezone(LONDON), self.cfg.london_local_start
        elif session is Session.NEW_YORK:
            local, start = utc_dt.astimezone(NEW_YORK), self.cfg.newyork_local_start
        else:
            local, start = utc_dt, f"{self.cfg.asia_start_utc_hour:02d}:00"
        s = _parse_hhmm(start)
        anchor = local.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        if local < anchor:
            anchor -= timedelta(days=1)
        return int((local - anchor).total_seconds() // 60)

    def minutes_to_week_close(self, utc_dt: datetime) -> float:
        """Minutes until the Friday 21:00 UTC close. Drives weekend flattening."""
        days_ahead = (4 - utc_dt.weekday()) % 7
        close = (utc_dt + timedelta(days=days_ahead)).replace(
            hour=21, minute=0, second=0, microsecond=0
        )
        if close < utc_dt:
            close += timedelta(days=7)
        return (close - utc_dt).total_seconds() / 60.0

    def minutes_since_week_open(self, utc_dt: datetime) -> float:
        days_back = (utc_dt.weekday() - 6) % 7
        open_ts = (utc_dt - timedelta(days=days_back)).replace(
            hour=22, minute=0, second=0, microsecond=0
        )
        if open_ts > utc_dt:
            open_ts -= timedelta(days=7)
        return (utc_dt - open_ts).total_seconds() / 60.0

    def is_tradable_window(self, utc_dt: datetime) -> tuple[bool, str]:
        """Whether the calendar/session filter permits a NEW entry now."""
        if not self.is_market_open(utc_dt):
            return False, "market closed"
        if utc_dt.weekday() not in self.cfg.allowed_weekdays:
            return False, f"weekday {utc_dt.weekday()} not in allowed_weekdays"
        session = self.session_for(utc_dt)
        if session not in self.cfg.allowed_sessions:
            return False, f"session {session} not in allowed_sessions"
        if self.minutes_since_week_open(utc_dt) < self.cfg.block_first_minutes_of_week:
            return False, "inside the thin liquidity window after the weekly open"
        if self.minutes_to_week_close(utc_dt) < self.cfg.block_last_minutes_of_week:
            return False, "too close to the weekly close"
        return True, "ok"

    # -- session extremes --------------------------------------------------------------

    def session_bounds(self, utc_dt: datetime, session: Session) -> tuple[datetime, datetime]:
        """UTC start/end of the given session on the day containing utc_dt."""
        if session is Session.ASIA:
            start_h = self.cfg.asia_start_utc_hour
            start = utc_dt.replace(hour=start_h, minute=0, second=0, microsecond=0)
            if utc_dt.hour < self.cfg.asia_end_utc_hour:
                start -= timedelta(days=1)
            return start, start + timedelta(
                hours=(24 - start_h) + self.cfg.asia_end_utc_hour
                if start_h > self.cfg.asia_end_utc_hour
                else self.cfg.asia_end_utc_hour - start_h
            )
        tz = LONDON if session in (Session.LONDON, Session.OVERLAP) else NEW_YORK
        s_str = (
            self.cfg.london_local_start
            if session in (Session.LONDON, Session.OVERLAP)
            else self.cfg.newyork_local_start
        )
        e_str = (
            self.cfg.london_local_end
            if session in (Session.LONDON, Session.OVERLAP)
            else self.cfg.newyork_local_end
        )
        local = utc_dt.astimezone(tz)
        s, e = _parse_hhmm(s_str), _parse_hhmm(e_str)
        start_local = local.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        end_local = local.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
        if local < start_local:
            start_local -= timedelta(days=1)
            end_local -= timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def state(
        self,
        utc_dt: datetime,
        asia_high: float | None = None,
        asia_low: float | None = None,
        session_high: float | None = None,
        session_low: float | None = None,
    ) -> SessionState:
        session = self.session_for(utc_dt)
        return SessionState(
            session=session,
            killzone=self.killzone_for(utc_dt),
            utc_now=utc_dt,
            london_now=utc_dt.astimezone(LONDON),
            ny_now=utc_dt.astimezone(NEW_YORK),
            broker_now=self.clock.to_broker(utc_dt),
            minutes_into_session=self.minutes_into_session(utc_dt, session),
            is_overlap=session is Session.OVERLAP,
            is_weekend=not self.is_market_open(utc_dt),
            is_holiday=utc_dt.date() in self._holidays,
            day_of_week=utc_dt.weekday(),
            asia_high=asia_high,
            asia_low=asia_low,
            session_high=session_high,
            session_low=session_low,
        )
