"""Configuration must fail loudly, at startup, on an unsafe value."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xauusd.config.settings import (
    RiskConfig,
    ScoringWeights,
    Settings,
    StrategyThresholds,
    load_settings,
    verify_live_arming,
)
from xauusd.domain.enums import Mode


class TestRiskConfig:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"risk_pct_a": 0.05},  # above the 2% ceiling
            {"risk_pct_a": 0.0},  # zero risk is not a trade
            {"risk_pct_a": 0.02, "risk_pct_a_plus": 0.01},  # A must not exceed A+
            {"max_daily_drawdown_pct": 0.06, "max_weekly_drawdown_pct": 0.05},
            {"risk_pct_a_plus": 0.02, "max_daily_drawdown_pct": 0.015},
            {"max_concurrent_positions": 0},
        ],
    )
    def test_rejects_unsafe(self, kwargs: dict) -> None:
        with pytest.raises(Exception):
            RiskConfig(**kwargs)

    def test_defaults_match_the_specification(self) -> None:
        r = RiskConfig()
        assert r.risk_pct_a == 0.01
        assert r.risk_pct_a_plus == 0.02
        assert r.max_daily_drawdown_pct == 0.02
        assert r.max_weekly_drawdown_pct == 0.05
        assert r.max_monthly_drawdown_pct == 0.10
        assert r.max_total_open_risk_pct == 0.02

    def test_is_immutable(self) -> None:
        r = RiskConfig()
        with pytest.raises(Exception):
            r.risk_pct_a = 0.10  # type: ignore[misc]


class TestScoringWeights:
    def test_must_total_100(self) -> None:
        with pytest.raises(Exception):
            ScoringWeights(htf_bias=20.0)
        assert sum(ScoringWeights().category_maximums().values()) == pytest.approx(100.0)


class TestThresholds:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_rr": 1.5},  # below the 1:2 floor
            {"a_score_min": 80, "a_plus_score_min": 70},
            {"a_probability_min": 0.7, "a_plus_probability_min": 0.6},
            {"min_rr": 3.0, "preferred_rr": 2.0},
        ],
    )
    def test_rejects_incoherent(self, kwargs: dict) -> None:
        with pytest.raises(Exception):
            StrategyThresholds(**kwargs)


class TestSettings:
    def test_live_requires_both_mode_and_flag(self) -> None:
        with pytest.raises(Exception):
            Settings(live_trading=True)  # flag without mode
        with pytest.raises(Exception):
            Settings(mode=Mode.LIVE)  # mode without flag
        s = Settings(mode=Mode.LIVE, live_trading=True)
        assert s.mode is Mode.LIVE

    def test_default_is_not_live(self) -> None:
        s = Settings()
        assert s.live_trading is False
        assert s.mode is not Mode.LIVE

    def test_config_hash_is_stable_and_sensitive(self) -> None:
        a, b = Settings(), Settings()
        assert a.config_hash() == b.config_hash()
        c = Settings(risk=RiskConfig(risk_pct_a=0.005))
        assert c.config_hash() != a.config_hash()

    def test_loads_layered_yaml(self) -> None:
        s = load_settings(config_dir="config", env="dev")
        assert s.env == "dev"
        assert s.risk.risk_pct_a == 0.01


class TestLiveArming:
    """Two-key arming: the config flag alone must never be enough."""

    def test_flag_alone_does_not_arm(self, tmp_path: Path) -> None:
        s = Settings(
            mode=Mode.LIVE, live_trading=True, live_arming_file=str(tmp_path / "nope.json")
        )
        ok, why = verify_live_arming(s, 111)
        assert not ok and "missing" in why

    def test_wrong_account_does_not_arm(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.json"
        f.write_text(json.dumps({"account_login": 999, "acknowledged_risk": True}))
        s = Settings(mode=Mode.LIVE, live_trading=True, live_arming_file=str(f))
        ok, why = verify_live_arming(s, 111)
        assert not ok and "999" in why

    def test_unacknowledged_risk_does_not_arm(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.json"
        f.write_text(json.dumps({"account_login": 111}))
        s = Settings(mode=Mode.LIVE, live_trading=True, live_arming_file=str(f))
        assert not verify_live_arming(s, 111)[0]

    def test_both_keys_arm(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.json"
        f.write_text(json.dumps({"account_login": 111, "acknowledged_risk": True}))
        s = Settings(mode=Mode.LIVE, live_trading=True, live_arming_file=str(f))
        assert verify_live_arming(s, 111) == (True, "armed")

    def test_arming_file_without_flag_does_not_arm(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.json"
        f.write_text(json.dumps({"account_login": 111, "acknowledged_risk": True}))
        s = Settings(live_arming_file=str(f))  # live_trading defaults False
        assert not verify_live_arming(s, 111)[0]
