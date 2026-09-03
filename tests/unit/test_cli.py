"""The CLI surface.

Thin delegation, but it is what the runbook tells an operator to type, so the contract
worth pinning is: every advertised subcommand exists, and a bad configuration produces a
readable explanation rather than a pydantic traceback.
"""

from __future__ import annotations

import pytest

from xauusd.cli import main


class TestEveryAdvertisedCommandExists:
    """`validate` was named in --help for a while without being implemented."""

    @pytest.mark.parametrize(
        "command",
        [
            "doctor",
            "run",
            "dashboard",
            "bridge",
            "backtest",
            "harvest",
            "validate",
            "explain",
            "rejections",
            "arm-live",
        ],
    )
    def test_the_subcommand_parses(self, command: str) -> None:
        with pytest.raises(SystemExit) as exc:
            main([command, "--help"])
        assert exc.value.code == 0, f"{command} is advertised but does not parse"

    def test_an_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["definitely-not-a-command"])
        assert exc.value.code != 0


class TestABadConfigExplainsItself:
    def test_an_invalid_setting_prints_the_reason_not_a_traceback(
        self, monkeypatch, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        # A dashboard exposed with no token is refused at construction, which is exactly
        # the kind of thing `doctor` is run to discover.
        monkeypatch.setenv("XAUUSD_DASHBOARD__HOST", "0.0.0.0")
        monkeypatch.delenv("XAUUSD_DASHBOARD__AUTH_TOKEN", raising=False)

        assert main(["doctor"]) == 2
        err = capsys.readouterr().err
        assert "configuration is invalid" in err
        assert "dashboard" in err
        assert "not loopback" in err
        assert "Traceback" not in err

    def test_a_risk_limit_breach_is_reported_the_same_way(self, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("XAUUSD_RISK__RISK_PCT_A", "0.05")  # above the 2% ceiling

        assert main(["doctor"]) == 2
        err = capsys.readouterr().err
        assert "configuration is invalid" in err
        assert "risk" in err


class TestTheBridgeReadsTheConfiguredAccount:
    """.env.example asks for XAUUSD_BROKER__LOGIN / PASSWORD / SERVER / TERMINAL_PATH.

    The bridge is launched with no flags by the Start shortcut, so if it only read
    argparse defaults those four values reached nothing — and it would still appear to
    work, by attaching to whichever account the terminal happened to have open. That is
    the worst possible version of "connected": plausible, and potentially the wrong
    account.
    """

    def test_credentials_come_from_config_when_no_flags_are_given(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("XAUUSD_BROKER__LOGIN", "12345678")
        monkeypatch.setenv("XAUUSD_BROKER__PASSWORD", "s3cret")
        monkeypatch.setenv("XAUUSD_BROKER__SERVER", "ICMarketsSC-Demo")
        monkeypatch.setenv("XAUUSD_BROKER__TERMINAL_PATH", r"C:\MT5\terminal64.exe")

        seen: dict[str, object] = {}
        import xauusd.execution.mt5_bridge as bridge

        monkeypatch.setattr(bridge, "serve", lambda **kw: seen.update(kw))

        from xauusd.cli import main

        main(["bridge"])
        assert seen["login"] == 12345678
        assert seen["password"] == "s3cret"
        assert seen["server"] == "ICMarketsSC-Demo"
        assert seen["terminal_path"] == r"C:\MT5\terminal64.exe"

    def test_an_explicit_flag_still_wins(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("XAUUSD_BROKER__LOGIN", "11111111")
        seen: dict[str, object] = {}
        import xauusd.execution.mt5_bridge as bridge

        monkeypatch.setattr(bridge, "serve", lambda **kw: seen.update(kw))

        from xauusd.cli import main

        main(["bridge", "--login", "22222222"])
        assert seen["login"] == 22222222

    def test_no_configured_login_is_allowed_but_announced(self, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
        """Attaching to the terminal's open session is legitimate — it just must not be
        silent, because 'connected to the wrong account' is a bad thing to discover late."""
        monkeypatch.delenv("XAUUSD_BROKER__LOGIN", raising=False)
        seen: dict[str, object] = {}
        import xauusd.execution.mt5_bridge as bridge

        monkeypatch.setattr(bridge, "serve", lambda **kw: seen.update(kw))

        from xauusd.cli import main

        assert main(["bridge"]) == 0
        assert seen["login"] is None
        combined = capsys.readouterr()
        assert "bridge_no_login_configured" in (combined.out + combined.err)


class TestTheSpecIsCheckedForCoherence:
    """A one-tick move on one lot is worth contract_size * tick_size. Every position
    size derives from tick_value, so a broker whose own numbers disagree with that
    arithmetic cannot be sized against — and no operator spots it by eye."""

    def test_a_coherent_spec_passes(self, capsys) -> None:  # type: ignore[no-untyped-def]
        from xauusd.cli import main

        assert main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "consistent: 1 tick on 1 lot" in out

    def test_the_metaquotes_demo_spec_is_flagged_and_fails(self, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
        """The exact spec a MetaQuotes demo reported: Digits 2, Contract 100,
        Tick size 0.01, Tick value 0.1. Its own numbers are ten times apart, and sizing
        reads the wrong one — a 1% risk would have been placed as 10%."""
        import xauusd.cli as cli
        from xauusd.domain.types import SymbolSpec

        real = cli.build_broker

        def broker_with_bad_spec(settings):  # type: ignore[no-untyped-def]
            b = real(settings)
            bad = SymbolSpec(
                settings.symbol,
                2,
                0.01,
                100.0,
                0.01,
                0.1,
                0.1,
                0.1,  # tick values ten times too small
                0.01,
                50.0,
                0.01,
                10,
                5,
            )
            monkeypatch.setattr(b, "symbol_spec", lambda _s: bad)
            return b

        monkeypatch.setattr(cli, "build_broker", broker_with_bad_spec)
        assert cli.main(["doctor"]) == 1, "an unsizeable spec must not report READY"
        out = capsys.readouterr().out
        assert "MISMATCH" in out
        assert "Do not trade this symbol" in out
        assert "10x apart" in out


class TestTheInstrumentIsProvedByItsPrice:
    """A symbol can be named GOLD, described "SPOT Gold Ounce vs US Dollar", and carry
    an ISIN, an NYSE listing and a "Basic Materials" sector — because it is a gold
    mining company's shares. Contract size 100, tick size 0.01 and tick value 1 are all
    perfectly reasonable for that too. The price is the only thing that separates a
    $2,600 ounce of bullion from a $21 share, and the engine checks it at startup.
    """

    def test_a_mining_stock_price_fails_the_preflight(self, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime

        import xauusd.cli as cli
        from xauusd.config.settings import load_settings
        from xauusd.domain.types import Quote

        settings = load_settings()
        real_build = cli.build_broker

        def broker_serving_a_stock(s):  # type: ignore[no-untyped-def]
            b = real_build(settings)  # a sim broker, so no bridge is needed
            b.raw_symbols = lambda p, o: [
                {
                    "name": "GOLD",
                    "description": "SPOT Gold Ounce vs US Dollar",
                    "path": "Stocks\\GOLD",
                    "currency_profit": "USD",
                    "trade_mode": 4,
                    "digits": 2,
                    "spread": 3,
                    "visible": True,
                }
            ]
            b.quote = lambda _s: Quote(datetime.now(UTC), 21.38, 21.42)
            return b

        monkeypatch.setattr(cli, "build_broker", broker_serving_a_stock)
        # The check is for real brokers; the simulator legitimately quotes nothing.
        monkeypatch.setenv("XAUUSD_BROKER__KIND", "mt5_grpc")
        monkeypatch.setattr(
            cli,
            "load_settings",
            lambda **kw: settings.model_copy(
                update={"broker": settings.broker.model_copy(update={"kind": "mt5_grpc"})}
            ),
        )

        assert cli.main(["doctor"]) == 1, "a $21 instrument must not pass as spot gold"
        out = capsys.readouterr().out
        assert "NOT GOLD" in out
        assert "mining company" in out

    def test_a_real_gold_price_passes(self) -> None:
        from xauusd.execution.symbol_discovery import sanity_check_quote

        sanity_check_quote(2648.35, "GOLD")  # must not raise
