"""Automatic XAUUSD symbol discovery.

Brokers name gold XAUUSD, XAUUSD.a, XAUUSDm, XAUUSD.raw, XAUUSD_i, XAUUSD.pro,
GOLD, GOLD.spot, XAUUSD-ECN and worse. Hardcoding a name is how a bot fails silently
on a new broker; guessing between two plausible names is how it trades the wrong
instrument. So: rank candidates, and REFUSE TO START on ambiguity rather than pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

SYMBOL_TRADE_MODE_FULL = 4
PLAUSIBLE_GOLD_RANGE = (400.0, 20_000.0)

# Things that match a gold pattern but are NOT the spot contract we want.
_EXCLUDE = re.compile(
    r"(XAUEUR|XAUGBP|XAUJPY|XAUAUD|XAUCHF|XAUXAG|XAGUSD|BASKET|INDEX|IDX|FUT|"
    r"\d{4}|MINI\b|MICRO\b)",
    re.IGNORECASE,
)


class SymbolResolutionError(RuntimeError):
    """Raised when the gold symbol cannot be determined unambiguously."""


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    description: str
    path: str
    currency_profit: str
    trade_mode: int
    digits: int
    spread: int
    visible: bool
    score: float = 0.0
    notes: tuple[str, ...] = ()


def _score(c: Candidate) -> tuple[float, tuple[str, ...]]:
    score = 0.0
    notes: list[str] = []

    if c.name.upper() in {"XAUUSD", "GOLD"}:
        score += 40
        notes.append("canonical name")
    if c.trade_mode == SYMBOL_TRADE_MODE_FULL:
        score += 25
        notes.append("fully tradable")
    if c.visible:
        score += 5
    # Shorter names are the broker's primary contract; suffixes are usually variants.
    score += max(0.0, 20.0 - 2.0 * max(0, len(c.name) - 6))
    if c.digits >= 2:
        score += 5
    if 0 < c.spread <= 60:
        score += 10 - (c.spread / 10.0)
        notes.append(f"spread {c.spread}pts")
    elif c.spread > 60:
        score -= 5
        notes.append(f"wide spread {c.spread}pts")
    if "spot" in c.description.lower() or "spot" in c.path.lower():
        score += 5
    return score, tuple(notes)


def rank_candidates(
    symbols: list[dict[str, Any]], patterns: list[str] | None = None
) -> list[Candidate]:
    pats = [re.compile(p, re.IGNORECASE) for p in (patterns or [r"^XAU", r"^GOLD"])]
    out: list[Candidate] = []
    for s in symbols:
        name = str(s.get("name", ""))
        if not any(p.search(name) for p in pats):
            continue
        if _EXCLUDE.search(name):
            continue
        if str(s.get("currency_profit", "")).upper() != "USD":
            continue
        c = Candidate(
            name=name,
            description=str(s.get("description", "")),
            path=str(s.get("path", "")),
            currency_profit=str(s.get("currency_profit", "")),
            trade_mode=int(s.get("trade_mode", 0)),
            digits=int(s.get("digits", 0)),
            spread=int(s.get("spread", 0)),
            visible=bool(s.get("visible", True)),
        )
        if c.trade_mode != SYMBOL_TRADE_MODE_FULL:
            continue
        sc, notes = _score(c)
        out.append(replace(c, score=sc, notes=notes))
    return sorted(out, key=lambda c: c.score, reverse=True)


def resolve_symbol(
    symbols: list[dict[str, Any]],
    patterns: list[str] | None = None,
    override: str | None = None,
    ambiguity_margin: float = 8.0,
) -> Candidate:
    """Pick the gold symbol, or refuse.

    `override` still goes through validation: a configured name that is not tradable
    is an error, not an instruction.
    """
    if override:
        match = next((s for s in symbols if str(s.get("name")) == override), None)
        if match is None:
            raise SymbolResolutionError(
                f"configured symbol_override '{override}' is not offered by this broker"
            )
        if int(match.get("trade_mode", 0)) != SYMBOL_TRADE_MODE_FULL:
            raise SymbolResolutionError(
                f"configured symbol_override '{override}' is not fully tradable "
                f"(trade_mode={match.get('trade_mode')})"
            )
        c = Candidate(
            name=override,
            description=str(match.get("description", "")),
            path=str(match.get("path", "")),
            currency_profit=str(match.get("currency_profit", "")),
            trade_mode=int(match.get("trade_mode", 0)),
            digits=int(match.get("digits", 0)),
            spread=int(match.get("spread", 0)),
            visible=bool(match.get("visible", True)),
            notes=("configured override",),
        )
        log.info("symbol_resolved", symbol=override, source="override")
        return c

    ranked = rank_candidates(symbols, patterns)
    if not ranked:
        raise SymbolResolutionError(
            "no tradable USD-quoted gold symbol found. Set data.symbol_override in config."
        )
    best = ranked[0]
    if len(ranked) > 1 and (best.score - ranked[1].score) < ambiguity_margin:
        names = ", ".join(f"{c.name}({c.score:.0f})" for c in ranked[:4])
        raise SymbolResolutionError(
            f"ambiguous gold symbol - candidates too close to call: {names}. "
            f"Set data.symbol_override in config. Ambiguity is never resolved silently."
        )
    log.info(
        "symbol_resolved",
        symbol=best.name,
        score=best.score,
        notes=best.notes,
        runner_up=(ranked[1].name if len(ranked) > 1 else None),
    )
    return best


def sanity_check_quote(price: float, symbol: str) -> None:
    lo, hi = PLAUSIBLE_GOLD_RANGE
    if not (lo <= price <= hi):
        raise SymbolResolutionError(
            f"{symbol} quoted at {price}, outside the plausible gold range {lo}-{hi}. "
            f"This is probably not spot gold."
        )


def resolve_broker_symbol(broker: Any, settings: Any) -> str:
    """The symbol name the engine would trade, for anything that must agree with it.

    Harvesting under a different name than the engine trades stores history nothing
    ever reads — the backtest reports "not enough bars" while the table is full. The
    engine, `doctor` and the harvester have each resolved the symbol independently
    before, and twice that divergence surfaced as a bug on a real broker, so new
    callers go through here.

    A broker with no `raw_symbols` (the simulator) has nothing to discover; the
    configured name is correct for it.
    """
    raw = getattr(broker, "raw_symbols", None)
    if raw is None:
        return str(settings.symbol)
    candidates = raw(settings.data.symbol_patterns, settings.data.symbol_override)
    return resolve_symbol(
        candidates, settings.data.symbol_patterns, settings.data.symbol_override
    ).name
