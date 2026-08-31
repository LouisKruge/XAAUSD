"""Symbol discovery across broker naming conventions.

Hardcoding a symbol name is how a bot fails silently on a new broker. Guessing between
two plausible names is how it trades the wrong instrument. The rule is: rank, and REFUSE
on ambiguity.
"""

from __future__ import annotations

import pytest

from xauusd.execution.symbol_discovery import (
    SymbolResolutionError,
    rank_candidates,
    resolve_symbol,
    sanity_check_quote,
)


def sym(name: str, **kw: object) -> dict:
    base = {
        "name": name,
        "description": "Gold vs US Dollar",
        "path": "Forex\\Metals",
        "currency_profit": "USD",
        "currency_base": "XAU",
        "trade_mode": 4,
        "digits": 2,
        "spread": 25,
        "visible": True,
    }
    base.update(kw)
    return base


class TestRealBrokerLayouts:
    @pytest.mark.parametrize(
        "broker,symbols,expected",
        [
            ("plain", [sym("XAUUSD"), sym("EURUSD", currency_base="EUR")], "XAUUSD"),
            (
                "with silver and crosses",
                [
                    sym("XAUUSD"),
                    sym("XAGUSD", description="Silver"),
                    sym("XAUEUR", currency_profit="EUR"),
                ],
                "XAUUSD",
            ),
            (
                "suffix variants alongside the primary",
                [sym("XAUUSDm", spread=30), sym("XAUUSD", spread=18)],
                "XAUUSD",
            ),
            ("named GOLD", [sym("GOLD"), sym("GOLD.spot", spread=40)], "GOLD"),
            ("suffix only", [sym("XAUUSDm")], "XAUUSDm"),
            (
                "primary disabled, variant tradable",
                [sym("XAUUSD", trade_mode=0), sym("XAUUSD.pro")],
                "XAUUSD.pro",
            ),
        ],
    )
    def test_resolves(self, broker: str, symbols: list, expected: str) -> None:
        assert resolve_symbol(symbols).name == expected

    def test_refuses_when_two_variants_are_too_close(self) -> None:
        """Ambiguity is never resolved silently — it would mean trading the wrong book."""
        with pytest.raises(SymbolResolutionError, match="ambiguous"):
            resolve_symbol([sym("XAUUSD.a"), sym("XAUUSD.b")])

    def test_refuses_when_no_gold_symbol_exists(self) -> None:
        with pytest.raises(SymbolResolutionError, match="no tradable"):
            resolve_symbol([sym("EURUSD", currency_base="EUR")])


class TestFiltering:
    def test_non_usd_quoted_gold_is_excluded(self) -> None:
        ranked = rank_candidates([sym("XAUEUR", currency_profit="EUR"), sym("XAUUSD")])
        assert [c.name for c in ranked] == ["XAUUSD"]

    def test_untradable_symbols_are_excluded(self) -> None:
        assert rank_candidates([sym("XAUUSD", trade_mode=0)]) == []

    @pytest.mark.parametrize("name", ["XAUXAG", "XAGUSD", "GOLD_INDEX", "XAUUSD2026", "XAUUSD_FUT"])
    def test_lookalikes_are_excluded(self, name: str) -> None:
        assert not [c for c in rank_candidates([sym(name)]) if c.name == name]

    def test_tighter_spread_ranks_higher_among_equals(self) -> None:
        ranked = rank_candidates([sym("XAUUSD.a", spread=60), sym("XAUUSD.b", spread=12)])
        assert ranked[0].name == "XAUUSD.b"


class TestOverride:
    def test_a_configured_override_is_still_validated(self) -> None:
        """A configured name that is not tradable is an error, not an instruction."""
        with pytest.raises(SymbolResolutionError, match="not fully tradable"):
            resolve_symbol([sym("XAUUSD", trade_mode=0)], override="XAUUSD")

    def test_an_override_the_broker_does_not_offer_fails(self) -> None:
        with pytest.raises(SymbolResolutionError, match="not offered"):
            resolve_symbol([sym("XAUUSD")], override="XAUUSD.nonexistent")

    def test_a_valid_override_wins(self) -> None:
        chosen = resolve_symbol([sym("XAUUSD"), sym("XAUUSD.pro")], override="XAUUSD.pro")
        assert chosen.name == "XAUUSD.pro"
        assert "override" in " ".join(chosen.notes)


class TestQuoteSanity:
    def test_a_plausible_gold_price_passes(self) -> None:
        sanity_check_quote(2650.0, "XAUUSD")

    @pytest.mark.parametrize("price", [1.0855, 150.0, 45_000.0])
    def test_an_implausible_price_is_rejected(self, price: float) -> None:
        """Catches a symbol that matched the pattern but is not spot gold."""
        with pytest.raises(SymbolResolutionError, match="plausible gold range"):
            sanity_check_quote(price, "XAUUSD")
