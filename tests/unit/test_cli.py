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
