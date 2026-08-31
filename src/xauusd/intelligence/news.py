"""News and geopolitical risk.

**Hard constraint, enforced structurally: news never places a trade.**

This module's only outputs are

  1. a RISK LEVEL that can veto or downgrade a setup, and
  2. a small, BOUNDED contribution to fundamental alignment, capped by
     `NewsConfig.max_news_score_contribution` so it can never move a candidate across a
     classification threshold on its own.

There is no code path from a headline to an order. An LLM assessment is a structured
opinion stored in the database with the timestamp it was produced, and it is FROZEN:
historical bars are never re-assessed with a later model, because that would leak
hindsight into a backtest.

Failure behaviour is deliberately asymmetric. An unavailable feed degrades to MODERATE,
never LOW, because absence of news is not evidence of calm — and defaulting to LOW would
let a broken feed unlock A+ classification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from xauusd.config.settings import NewsConfig
from xauusd.domain.enums import Bias, NewsRisk
from xauusd.domain.types import NewsState
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)


class NewsCategory:
    WAR = "WAR"
    MILITARY = "MILITARY"
    CENTRAL_BANK = "CENTRAL_BANK"
    SANCTIONS = "SANCTIONS"
    TRADE_CONFLICT = "TRADE_CONFLICT"
    FINANCIAL_CRISIS = "FINANCIAL_CRISIS"
    BANKING = "BANKING"
    SOVEREIGN_DEBT = "SOVEREIGN_DEBT"
    RISK_OFF = "RISK_OFF"
    OTHER = "OTHER"


# Rules-based pre-filter: cheap, deterministic, always runs, and works when the LLM
# layer is disabled or unavailable.
_RULES: list[tuple[str, str, int, int, Bias]] = [
    # pattern, category, importance, gold relevance, likely direction for gold
    (
        r"\b(nuclear|invasion|invades|declares war|air ?strikes?|missile attack)\b",
        NewsCategory.WAR,
        10,
        9,
        Bias.BULLISH,
    ),
    (
        r"\b(war|conflict escalat|military (action|strike|operation)|troops deployed)\b",
        NewsCategory.MILITARY,
        8,
        8,
        Bias.BULLISH,
    ),
    (
        r"\b(emergency (rate|meeting)|unscheduled (meeting|cut)|intermeeting)\b",
        NewsCategory.CENTRAL_BANK,
        10,
        10,
        Bias.BULLISH,
    ),
    (
        r"\b(rate (cut|hike)|hawkish|dovish|quantitative (easing|tightening))\b",
        NewsCategory.CENTRAL_BANK,
        7,
        8,
        Bias.NEUTRAL,
    ),
    (
        r"\b(sanction|embargo|asset freeze|export controls)\b",
        NewsCategory.SANCTIONS,
        6,
        6,
        Bias.BULLISH,
    ),
    (r"\b(tariff|trade war|trade dispute)\b", NewsCategory.TRADE_CONFLICT, 6, 5, Bias.BULLISH),
    (
        r"\b(bank (failure|collapse|run)|bailout|insolven|contagion)\b",
        NewsCategory.BANKING,
        9,
        9,
        Bias.BULLISH,
    ),
    (
        r"\b(default|debt ceiling|downgrade[sd]? .*(rating|debt)|credit rating cut)\b",
        NewsCategory.SOVEREIGN_DEBT,
        8,
        8,
        Bias.BULLISH,
    ),
    (
        r"\b(market (crash|plunge|selloff)|circuit breaker|flash crash)\b",
        NewsCategory.RISK_OFF,
        8,
        7,
        Bias.BULLISH,
    ),
    (
        r"\b(central bank(s)? (buy|bought|purchas)|reserve diversification)\b",
        NewsCategory.CENTRAL_BANK,
        6,
        8,
        Bias.BULLISH,
    ),
    (
        r"\b(ceasefire|peace (deal|talks|agreement)|de-?escalat)\b",
        NewsCategory.WAR,
        7,
        7,
        Bias.BEARISH,
    ),
]

_GOLD_TERMS = re.compile(
    r"\b(gold|xau|bullion|precious metal|federal reserve|fomc|inflation|treasury|"
    r"yield|dollar|dxy|safe.?haven|central bank)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NewsItem:
    published_ts: datetime
    headline: str
    source: str
    body: str = ""
    url: str = ""

    @property
    def content_hash(self) -> str:
        return hashlib.blake2s(
            f"{self.source}|{self.headline}".encode(), digest_size=16
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class Assessment:
    category: str
    importance: int  # 0..10
    gold_relevance: int  # 0..10
    direction: Bias
    uncertainty: int  # 0..10
    assessor: str
    assessed_at: datetime
    rationale: str = ""

    @property
    def weight(self) -> float:
        """Contribution to aggregate risk: importance x relevance, normalised."""
        return (self.importance / 10.0) * (self.gold_relevance / 10.0)


def is_gold_relevant(item: NewsItem) -> bool:
    """Cheap pre-filter so the LLM only ever sees items that could matter."""
    text = f"{item.headline} {item.body[:400]}"
    if _GOLD_TERMS.search(text):
        return True
    return any(re.search(p, text, re.IGNORECASE) for p, *_ in _RULES)


def assess_by_rules(item: NewsItem, now: datetime | None = None) -> Assessment:
    """Deterministic classification. Always runs; the LLM layer only refines it."""
    text = f"{item.headline}. {item.body[:600]}"
    best: tuple[str, int, int, Bias] | None = None
    for pattern, category, importance, relevance, direction in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            if best is None or importance > best[1]:
                best = (category, importance, relevance, direction)
    if best is None:
        return Assessment(
            NewsCategory.OTHER,
            1,
            1,
            Bias.NEUTRAL,
            5,
            "rules_v1",
            now or datetime.now(UTC),
            "no rule matched",
        )
    category, importance, relevance, direction = best
    return Assessment(
        category,
        importance,
        relevance,
        direction,
        uncertainty=3 if direction is not Bias.NEUTRAL else 6,
        assessor="rules_v1",
        assessed_at=now or datetime.now(UTC),
        rationale=f"matched rule for {category}",
    )


class LLMAssessor(Protocol):
    def assess(self, item: NewsItem) -> Assessment | None: ...


class ClaudeAssessor:
    """Optional LLM refinement, with a strict schema and a fail-safe default.

    Anything unparseable is treated as UNCERTAIN, which RAISES risk rather than lowering
    it. A model that returns garbage must never make the system more willing to trade.
    """

    SCHEMA_PROMPT = """You are a macro risk analyst. Classify this news item's effect on GOLD (XAUUSD).

Respond with ONLY a JSON object, no prose:
{
  "category": "WAR|MILITARY|CENTRAL_BANK|SANCTIONS|TRADE_CONFLICT|FINANCIAL_CRISIS|BANKING|SOVEREIGN_DEBT|RISK_OFF|OTHER",
  "importance": 0-10,
  "gold_relevance": 0-10,
  "direction": "BULLISH|BEARISH|NEUTRAL|UNCERTAIN",
  "uncertainty": 0-10,
  "rationale": "one sentence"
}

Guidance: "direction" is the likely effect on the GOLD price. Escalating geopolitical
risk and easing monetary policy are typically bullish gold; de-escalation and tightening
are typically bearish. If you are not confident, say UNCERTAIN - do not guess."""

    def __init__(
        self, model: str = "claude-sonnet-5", api_key: str | None = None, timeout: float = 20.0
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._cache: dict[str, Assessment] = {}

    def assess(self, item: NewsItem) -> Assessment | None:
        if not self.api_key:
            return None
        cached = self._cache.get(item.content_hash)
        if cached is not None:
            return cached
        try:
            import httpx

            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 300,
                    "temperature": 0,
                    "system": self.SCHEMA_PROMPT,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"HEADLINE: {item.headline}\n\nBODY: {item.body[:1500]}",
                        }
                    ],
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            text = r.json()["content"][0]["text"]
            a = self._parse(text, item)
        except Exception as exc:
            log.warning("llm_assessment_failed", error=str(exc), headline=item.headline[:80])
            return None
        self._cache[item.content_hash] = a
        return a

    def _parse(self, text: str, item: NewsItem) -> Assessment:
        try:
            start, end = text.index("{"), text.rindex("}") + 1
            data = json.loads(text[start:end])
            direction_raw = str(data.get("direction", "UNCERTAIN")).upper()
            direction = (
                Bias.BULLISH
                if direction_raw == "BULLISH"
                else Bias.BEARISH
                if direction_raw == "BEARISH"
                else Bias.NEUTRAL
            )
            uncertainty = int(data.get("uncertainty", 5))
            if direction_raw == "UNCERTAIN":
                uncertainty = max(uncertainty, 8)
            return Assessment(
                category=str(data.get("category", NewsCategory.OTHER)),
                importance=max(0, min(10, int(data.get("importance", 0)))),
                gold_relevance=max(0, min(10, int(data.get("gold_relevance", 0)))),
                direction=direction,
                uncertainty=max(0, min(10, uncertainty)),
                assessor=f"llm:{self.model}@v1",
                assessed_at=datetime.now(UTC),
                rationale=str(data.get("rationale", ""))[:500],
            )
        except Exception:
            # Unparseable output raises risk; it never lowers it.
            return Assessment(
                NewsCategory.OTHER,
                5,
                5,
                Bias.NEUTRAL,
                10,
                f"llm:{self.model}@v1-unparseable",
                datetime.now(UTC),
                "model output could not be parsed - treated as uncertain",
            )


class NewsEngine:
    """Aggregates assessments into the single risk level the engine consumes."""

    def __init__(self, config: NewsConfig | None = None, llm: LLMAssessor | None = None) -> None:
        self.cfg = config or NewsConfig()
        self.llm = llm

    def assess(self, item: NewsItem, now: datetime | None = None) -> Assessment:
        base = assess_by_rules(item, now)
        if self.cfg.llm_enabled and self.llm is not None and is_gold_relevant(item):
            refined = self.llm.assess(item)
            if refined is not None:
                return refined
        return base

    def aggregate(
        self,
        assessments: list[tuple[NewsItem, Assessment]],
        now: datetime,
        calendar_blackout: bool = False,
        calendar_reason: str | None = None,
        calendar_until: datetime | None = None,
        next_event: tuple[str, datetime] | None = None,
        feed_age_minutes: float | None = None,
    ) -> NewsState:
        """Combine everything into one NewsState."""
        window = timedelta(hours=12)
        recent = [
            (i, a)
            for i, a in assessments
            if now - window <= i.published_ts <= now and a.gold_relevance >= 4
        ]

        stale = feed_age_minutes is None or feed_age_minutes > self.cfg.max_news_age_minutes

        score = 0.0
        drivers: list[str] = []
        directional = 0.0
        for item, a in recent:
            # Decay: a 10-hour-old headline is context, not a live risk.
            age_h = max(0.0, (now - item.published_ts).total_seconds() / 3600.0)
            decay = 0.5 ** (age_h / 4.0)
            contribution = a.weight * decay
            score += contribution
            # High uncertainty raises risk but contributes nothing directional.
            certainty = 1.0 - a.uncertainty / 10.0
            directional += a.direction.sign * contribution * certainty
            if contribution > 0.25:
                drivers.append(f"{a.category}: {item.headline[:70]}")

        if score >= 2.0:
            risk = NewsRisk.EXTREME
        elif score >= 1.0:
            risk = NewsRisk.HIGH
        elif score >= 0.35:
            risk = NewsRisk.MODERATE
        else:
            risk = NewsRisk.LOW

        if stale and risk.level < NewsRisk.MODERATE.level:
            # Absence of news is not evidence of calm.
            risk = NewsRisk.MODERATE
            drivers.append("news feed stale — risk floored at MODERATE")

        if calendar_blackout and risk.level < NewsRisk.HIGH.level:
            risk = NewsRisk.HIGH

        blackout = calendar_blackout or risk is NewsRisk.EXTREME
        reason = calendar_reason
        if risk is NewsRisk.EXTREME and not calendar_reason:
            reason = f"extreme news risk (score {score:.2f})"

        hint = Bias.NEUTRAL
        if directional > 0.3:
            hint = Bias.BULLISH
        elif directional < -0.3:
            hint = Bias.BEARISH

        return NewsState(
            risk=risk,
            blackout=blackout,
            blackout_reason=reason,
            blackout_until=calendar_until,
            next_event_name=next_event[0] if next_event else None,
            next_event_ts=next_event[1] if next_event else None,
            minutes_to_next_event=(
                (next_event[1] - now).total_seconds() / 60.0 if next_event else None
            ),
            directional_hint=hint,
            drivers=tuple(drivers[:6]),
            is_stale=stale,
        )

    def score_contribution(self, state: NewsState, direction) -> float:  # type: ignore[no-untyped-def]
        """Bounded contribution to the fundamentals score.

        Capped by config so that news can never, on its own, push a candidate across a
        classification threshold. This is the structural guarantee that a headline
        cannot become a trade.
        """
        cap = self.cfg.max_news_score_contribution
        if state.directional_hint is Bias.NEUTRAL or state.is_stale:
            return 0.0
        aligned = state.directional_hint.agrees_with(direction)
        return cap * (1.0 if aligned else -1.0) * 0.5
