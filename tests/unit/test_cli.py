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
