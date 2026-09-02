"""Configuration must fail loudly, at startup, on an unsafe value."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

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
        # ValidationError specifically: a blind `Exception` would also pass on a
        # typo'd keyword, which is the failure mode this test exists to rule out.
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
            r.risk_pct_a = 0.10  # type: ignore[misc]


class TestScoringWeights:
    def test_must_total_100(self) -> None:
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
            StrategyThresholds(**kwargs)


class TestSettings:
    def test_live_requires_both_mode_and_flag(self) -> None:
        with pytest.raises(ValidationError):
            Settings(live_trading=True)  # flag without mode
        with pytest.raises(ValidationError):
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


class TestSourcePrecedence:
    """Explicit arguments beat the environment, which beats YAML, which beats defaults.

    This was wrong in a way that produced no error at all. The merged YAML was passed as
    `Settings(**merged)`, making it init state, and init state outranks the environment
    in pydantic-settings — so every key that appeared in base.yaml silently ignored its
    XAUUSD_* variable. `database.url` is the one that mattered: an operator following
    .env.example would point at Postgres, see no complaint, and keep writing the decision
    journal for a live account to a local SQLite file.
    """

    def test_the_environment_overrides_yaml(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("database:\n  url: sqlite:///from-yaml.db\n")

        assert load_settings(config_dir=cfg).database.url == "sqlite:///from-yaml.db"

        monkeypatch.setenv("XAUUSD_DATABASE__URL", "postgresql+psycopg://u:p@h:5432/db")
        assert load_settings(config_dir=cfg).database.url == "postgresql+psycopg://u:p@h:5432/db"

    def test_an_explicit_override_beats_the_environment(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("database:\n  url: sqlite:///from-yaml.db\n")
        monkeypatch.setenv("XAUUSD_DATABASE__URL", "sqlite:///from-env.db")

        settings = load_settings(
            config_dir=cfg, overrides={"database": {"url": "sqlite:///explicit.db"}}
        )
        assert settings.database.url == "sqlite:///explicit.db"

    def test_yaml_still_applies_when_no_variable_is_set(self, tmp_path) -> None:
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: XAUUSD.a\n")
        assert load_settings(config_dir=cfg).symbol == "XAUUSD.a"

    def test_the_yaml_layer_does_not_leak_between_loads(self, tmp_path) -> None:
        """The layer is process-global, so a failed or nested load must restore it."""
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: XAUUSD.a\n")
        load_settings(config_dir=cfg)
        assert Settings().symbol == "XAUUSD", "the YAML layer outlived its load_settings call"


class TestTheEnvFileIsActuallyRead:
    """`.env` is where every document tells the operator to put their broker credentials.

    Settings had no `env_file` in its model_config, so pydantic-settings never opened
    the file. There was no error: values fell back to the config files, and the engine
    reported the simulated broker with login=0 while a correctly-filled .env sat beside
    it. Nothing in the pre-flight report hinted that the file had not been read.
    """

    def test_values_in_the_env_file_reach_settings(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "XAUUSD_BROKER__LOGIN=111958443\nXAUUSD_BROKER__SERVER=MetaQuotes-Demo\n"
        )
        settings = Settings()
        assert settings.broker.login == 111958443
        assert settings.broker.server == "MetaQuotes-Demo"

    def test_the_process_environment_still_wins_over_the_file(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("XAUUSD_BROKER__LOGIN=111111\n")
        monkeypatch.setenv("XAUUSD_BROKER__LOGIN", "222222")
        assert Settings().broker.login == 222222

    def test_the_env_file_beats_the_config_files(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: FROM-YAML\n")
        (tmp_path / ".env").write_text("XAUUSD_SYMBOL=FROM-ENV-FILE\n")
        assert load_settings(config_dir=cfg).symbol == "FROM-ENV-FILE"


class TestTheEnvFileChoosesTheConfigEnvironment:
    """Which yaml file is layered is decided before Settings exists, so it has to read
    `.env` itself. Reading only os.environ meant XAUUSD_ENV=demo in .env produced
    settings.env == "demo" while the loader still layered dev.yaml — so the broker
    stayed on the simulator and nothing explained why."""

    def test_env_file_selects_the_yaml_layer(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("XAUUSD_ENV", raising=False)
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: BASE\n")
        (cfg / "demo.yaml").write_text("symbol: FROM-DEMO-YAML\n")
        (tmp_path / ".env").write_text("XAUUSD_ENV=demo\n")

        settings = load_settings(config_dir=cfg)
        assert settings.env == "demo"
        assert settings.symbol == "FROM-DEMO-YAML", "demo.yaml was not layered"

    def test_the_process_environment_still_wins(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XAUUSD_ENV", "live")
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: BASE\n")
        (cfg / "demo.yaml").write_text("symbol: DEMO\n")
        (cfg / "live.yaml").write_text("symbol: LIVE\n")
        (tmp_path / ".env").write_text("XAUUSD_ENV=demo\n")
        assert load_settings(config_dir=cfg).symbol == "LIVE"

    def test_an_explicit_argument_beats_both(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XAUUSD_ENV", "live")
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: BASE\n")
        (cfg / "demo.yaml").write_text("symbol: DEMO\n")
        (tmp_path / ".env").write_text("XAUUSD_ENV=live\n")
        assert load_settings(config_dir=cfg, env="demo").symbol == "DEMO"

    def test_no_env_file_still_defaults_to_dev(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("XAUUSD_ENV", raising=False)
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "base.yaml").write_text("symbol: BASE\n")
        assert load_settings(config_dir=cfg).env == "dev"


class TestABlankLineMeansNotSet:
    """`.env.example` ships blanks deliberately — they show which keys exist.

    Once `.env` was actually being read, those blanks reached pydantic as "". A
    `str | None` field tolerates it; `broker.login`, an `int | None`, does not, and
    setup died on "unable to parse string as an integer" before printing anything an
    operator could act on. A blank must behave exactly like an absent line.
    """

    def test_a_stock_env_example_loads(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The file we ship must not be the file that breaks startup."""
        monkeypatch.chdir(tmp_path)
        example = Path(__file__).resolve().parents[2] / ".env.example"
        (tmp_path / ".env").write_text(example.read_text())
        (tmp_path / "config").mkdir()

        settings = load_settings(config_dir=tmp_path / "config")
        assert settings.broker.login is None

    def test_a_blank_integer_becomes_none(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("XAUUSD_BROKER__LOGIN=\n")
        assert Settings().broker.login is None

    def test_a_blank_string_becomes_the_default(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("XAUUSD_SYMBOL=\n")
        assert Settings().symbol == "XAUUSD", "a blank must not blank out a default"

    def test_a_filled_value_still_wins(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("XAUUSD_BROKER__LOGIN=112061848\n")
        assert Settings().broker.login == 112061848

    def test_sections_are_still_immutable(self) -> None:
        """The shared base must not have quietly dropped frozen=True."""
        with pytest.raises(ValidationError):
            RiskConfig().risk_pct_a = 0.10  # type: ignore[misc]
        with pytest.raises(ValidationError):
            Settings().symbol = "XAGUSD"  # type: ignore[misc]

    def test_a_zero_is_not_treated_as_blank(self, tmp_path, monkeypatch) -> None:
        """Only the empty string is 'unset'. 0 and false are real values."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("XAUUSD_LOG_JSON=false\n")
        assert Settings().log_json is False
