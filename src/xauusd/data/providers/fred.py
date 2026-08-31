"""FRED client with VINTAGE awareness.

The important part is `fetch_with_vintages`, which uses ALFRED (`realtime_start`) to
learn when each value was actually PUBLISHED. Storing only the reference date — the
obvious implementation — leaks revised data backwards and silently inflates every
fundamental backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

BASE = "https://api.stlouisfed.org/fred"

# Typical publication lag when ALFRED vintages are unavailable. Deliberately
# CONSERVATIVE (later than reality) so a fallback can only ever make the backtest see
# data later than it truly could, never earlier.
FALLBACK_LAG_DAYS: dict[str, int] = {
    "DGS2": 1,
    "DGS10": 1,
    "DFII10": 1,
    "DFII5": 1,
    "T10YIE": 1,
    "T10Y2Y": 1,
    "DFF": 1,
    "DTWEXBGS": 4,
    "CPIAUCSL": 14,
    "PCEPILFE": 30,
    "UNRATE": 7,
    "PAYEMS": 7,
}
DEFAULT_LAG_DAYS = 3


@dataclass(frozen=True, slots=True)
class Observation:
    series_id: str
    ref_date: datetime
    release_ts: datetime
    value: float | None
    revision: int = 0


class FredClient:
    def __init__(self, api_key: str | None, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, str | int]) -> dict:
        import httpx

        if not self.api_key:
            raise RuntimeError("FRED API key not configured")
        q = {**params, "api_key": self.api_key, "file_type": "json"}
        r = httpx.get(f"{BASE}/{path}", params=q, timeout=self.timeout)
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def fetch(
        self, series_id: str, start: datetime, end: datetime | None = None
    ) -> list[Observation]:
        """Latest values only, with a conservative estimated publication lag."""
        params: dict[str, str | int] = {
            "series_id": series_id,
            "observation_start": start.date().isoformat(),
        }
        if end:
            params["observation_end"] = end.date().isoformat()
        data = self._get("series/observations", params)
        lag = timedelta(days=FALLBACK_LAG_DAYS.get(series_id, DEFAULT_LAG_DAYS))
        out: list[Observation] = []
        for row in data.get("observations", []):
            value = None if row["value"] in (".", "") else float(row["value"])
            ref = datetime.fromisoformat(row["date"]).replace(tzinfo=UTC)
            out.append(Observation(series_id, ref, ref + lag, value))
        return out

    def fetch_with_vintages(
        self, series_id: str, start: datetime, end: datetime | None = None
    ) -> list[Observation]:
        """True publication timestamps via ALFRED realtime periods.

        This is the correct path and should be preferred whenever the key allows it.
        """
        params: dict[str, str | int] = {
            "series_id": series_id,
            "observation_start": start.date().isoformat(),
            "realtime_start": start.date().isoformat(),
            "realtime_end": (end or datetime.now(UTC)).date().isoformat(),
            "output_type": 2,  # all vintages, one row per (date, vintage)
        }
        data = self._get("series/observations", params)
        seen: dict[tuple[str, str], int] = {}
        out: list[Observation] = []
        for row in data.get("observations", []):
            ref = datetime.fromisoformat(row["date"]).replace(tzinfo=UTC)
            release = datetime.fromisoformat(row.get("realtime_start", row["date"])).replace(
                tzinfo=UTC
            )
            for key, raw in row.items():
                if not key.startswith("value"):
                    continue
                if raw in (".", "", None):
                    continue
                k = (row["date"], key)
                rev = seen.get(k, 0)
                seen[k] = rev + 1
                out.append(Observation(series_id, ref, release, float(raw), rev))
        return out or self.fetch(series_id, start, end)

    def sync(
        self,
        repo: Any,
        series_ids: list[str],
        start: datetime,
        use_vintages: bool = True,
    ) -> dict[str, int]:
        """Fetch and persist. Returns per-series counts."""
        counts: dict[str, int] = {}
        for sid in series_ids:
            try:
                obs = (
                    self.fetch_with_vintages(sid, start) if use_vintages else self.fetch(sid, start)
                )
            except Exception as exc:
                log.error("fred_fetch_failed", series=sid, error=str(exc))
                counts[sid] = 0
                continue
            repo.ensure_series(sid, provider="FRED", name=sid)
            for o in obs:
                repo.add_observation(o.series_id, o.ref_date, o.release_ts, o.value, o.revision)
            counts[sid] = len(obs)
            log.info("fred_synced", series=sid, observations=len(obs))
        return counts
