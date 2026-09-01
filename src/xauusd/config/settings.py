"""Layered, validated configuration.

Load order (later overrides earlier):
    config/base.yaml  ->  config/{env}.yaml  ->  config/local.yaml  ->  environment vars

Validation is strict and happens at startup. A nonsensical risk limit fails the process
immediately rather than at 2 a.m. with money on the line.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from xauusd.domain.enums import Mode, Regime, Session, Timeframe


class RiskConfig(BaseModel):
    """Hard risk limits. These are invariants, not preferences."""

    model_config = {"frozen": True}

    risk_pct_a: float = Field(0.01, gt=0, le=0.02)
    risk_pct_a_plus: float = Field(0.02, gt=0, le=0.02)
    global_risk_cap_pct: float = Field(
        0.02, gt=0, le=0.02, description="Absolute ceiling applied on top of class caps."
    )
    max_daily_drawdown_pct: float = Field(0.02, gt=0, le=0.10)
    max_weekly_drawdown_pct: float = Field(0.05, gt=0, le=0.20)
    max_monthly_drawdown_pct: float = Field(0.10, gt=0, le=0.40)
    max_total_open_risk_pct: float = Field(0.02, gt=0, le=0.06)
    max_concurrent_positions: int = Field(1, ge=1, le=5)
    max_trades_per_day: int = Field(3, ge=1, le=20)
    max_consecutive_losses_lockout: int = Field(4, ge=2, le=20)
    drawdown_from_peak: bool = Field(
        True, description="Measure drawdown from period peak equity, not starting equity."
    )
    daily_reset_hour_broker: int = Field(0, ge=0, le=23)
    weekly_lockout_needs_manual_clear: bool = True
    monthly_lockout_needs_manual_clear: bool = True
    margin_safety_factor: float = Field(0.30, gt=0, le=1.0)
    sizing_cross_check_tolerance: float = Field(
        0.02, gt=0, le=0.25, description="Max relative disagreement with broker's own P/L maths."
    )
    risk_overshoot_tolerance: float = Field(0.005, ge=0, le=0.05)
    commission_per_lot: float = Field(7.0, ge=0)
    slippage_points_estimate: float = Field(15.0, ge=0)

    @model_validator(mode="after")
    def _check_ordering(self) -> RiskConfig:
        if self.risk_pct_a > self.risk_pct_a_plus:
            raise ValueError("risk_pct_a must not exceed risk_pct_a_plus")
        if not (
            self.max_daily_drawdown_pct
            <= self.max_weekly_drawdown_pct
            <= self.max_monthly_drawdown_pct
        ):
            raise ValueError("drawdown limits must satisfy daily <= weekly <= monthly")
        if self.risk_pct_a_plus > self.max_daily_drawdown_pct:
            raise ValueError("a single A+ trade may not risk more than the daily drawdown limit")
        return self


class ExecutionConfig(BaseModel):
    model_config = {"frozen": True}

    max_spread_points: float = Field(50.0, gt=0)
    max_spread_ratio: float = Field(2.5, gt=1.0, description="vs rolling median spread")
    max_quote_age_seconds: float = Field(10.0, gt=0)
    max_bar_age_seconds: float = Field(420.0, gt=0)
    max_entry_drift_r: float = Field(
        0.15, gt=0, description="Abandon if price drifts this fraction of R from signal price."
    )
    max_slippage_points: int = Field(25, gt=0)
    max_send_retries: int = Field(2, ge=0, le=5)
    reconcile_timeout_seconds: float = Field(30.0, gt=0)
    reconcile_interval_seconds: float = Field(60.0, gt=0)
    require_server_side_stop: bool = True
    break_even_at_r: float = Field(1.0, gt=0)
    break_even_offset_r: float = Field(0.05, ge=0)
    partial_tp_enabled: bool = False
    partial_tp_fraction: float = Field(0.5, gt=0, lt=1)
    trail_enabled: bool = True
    trail_activate_r: float = Field(1.5, gt=0)
    time_stop_bars: int = Field(48, ge=0, description="0 disables the time stop.")
    time_stop_min_r: float = Field(0.3, description="Exit at time stop only if below this R.")
    invalidation_exit_enabled: bool = True
    flat_before_weekend: bool = True
    weekend_flat_minutes_before_close: int = Field(90, ge=0)


class StrategyThresholds(BaseModel):
    """Classification thresholds. Set from validation output, never guessed live."""

    model_config = {"frozen": True}

    a_score_min: float = Field(70.0, ge=0, le=100)
    a_plus_score_min: float = Field(85.0, ge=0, le=100)
    a_probability_min: float = Field(0.55, gt=0, lt=1)
    a_plus_probability_min: float = Field(0.65, gt=0, lt=1)
    a_strong_categories_min: int = Field(5, ge=1, le=10)
    a_plus_strong_categories_min: int = Field(7, ge=1, le=10)
    strong_category_fraction: float = Field(
        0.7, gt=0, le=1.0, description="Fraction of a category's max that counts as 'strong'."
    )
    min_rr: float = Field(2.0, ge=2.0, description="Hard floor. Never below 2.0.")
    preferred_rr: float = Field(3.0, ge=2.0)
    require_probability_model: bool = Field(
        False, description="If false, degrade to score-only in A-only mode when no model."
    )

    @model_validator(mode="after")
    def _check(self) -> StrategyThresholds:
        if self.a_plus_score_min < self.a_score_min:
            raise ValueError("a_plus_score_min must be >= a_score_min")
        if self.a_plus_probability_min < self.a_probability_min:
            raise ValueError("a_plus_probability_min must be >= a_probability_min")
        if self.a_plus_strong_categories_min < self.a_strong_categories_min:
            raise ValueError("a_plus_strong_categories_min must be >= a_strong_categories_min")
        if self.preferred_rr < self.min_rr:
            raise ValueError("preferred_rr must be >= min_rr")
        return self


class ScoringWeights(BaseModel):
    """The 100-point allocation. Optimised in validation, not assumed correct.

    NOTE: the weights given as an example in the original brief total 95, not 100
    (15+15+15+10+10+10+5+5+5+5). The missing 5 points are allocated to
    entry_confirmation, on the grounds that the execution trigger is what converts a
    context into a fill and deserves parity with support_resistance. The validator
    enforces the 100 total so this class of error cannot recur silently.
    """

    model_config = {"frozen": True}

    htf_bias: float = 15.0
    market_structure: float = 15.0
    liquidity: float = 15.0
    fvg_ob: float = 10.0
    support_resistance: float = 10.0
    fundamentals: float = 10.0
    dxy_yields: float = 5.0
    session: float = 5.0
    volatility_regime: float = 5.0
    entry_confirmation: float = 10.0
    # Penalties (subtracted)
    penalty_news_risk: float = 15.0
    penalty_fundamental_conflict: float = 10.0
    penalty_poor_volatility: float = 8.0
    penalty_wide_spread: float = 8.0
    penalty_weak_session: float = 5.0
    penalty_opposing_liquidity: float = 10.0
    penalty_stale_data: float = 6.0

    @model_validator(mode="after")
    def _sums_to_100(self) -> ScoringWeights:
        total = (
            self.htf_bias
            + self.market_structure
            + self.liquidity
            + self.fvg_ob
            + self.support_resistance
            + self.fundamentals
            + self.dxy_yields
            + self.session
            + self.volatility_regime
            + self.entry_confirmation
        )
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"scoring category weights must total 100, got {total}")
        return self

    def category_maximums(self) -> dict[str, float]:
        return {
            "htf_bias": self.htf_bias,
            "market_structure": self.market_structure,
            "liquidity": self.liquidity,
            "fvg_ob": self.fvg_ob,
            "support_resistance": self.support_resistance,
            "fundamentals": self.fundamentals,
            "dxy_yields": self.dxy_yields,
            "session": self.session,
            "volatility_regime": self.volatility_regime,
            "entry_confirmation": self.entry_confirmation,
        }


class StructureConfig(BaseModel):
    """Objective definitions for the market-structure engine. See docs/specs/."""

    model_config = {"frozen": True}

    swing_lookback: int = Field(2, ge=1, le=10, description="Bars either side of a fractal.")
    swing_min_atr: float = Field(
        0.25, ge=0, description="Min swing leg size in ATR to count as structural."
    )
    bos_min_displacement_atr: float = Field(0.5, ge=0)
    bos_min_body_ratio: float = Field(0.5, ge=0, le=1)
    bos_require_body_close: bool = True
    mss_min_displacement_atr: float = Field(0.75, ge=0)
    internal_swing_lookback: int = Field(1, ge=1, le=5)
    atr_period: int = Field(14, ge=2)
    max_swings_tracked: int = Field(40, ge=4)


class LiquidityConfig(BaseModel):
    model_config = {"frozen": True}

    equal_level_tolerance_atr: float = Field(0.10, gt=0, le=1.0)
    min_equal_touches: int = Field(2, ge=2)
    sweep_min_penetration_atr: float = Field(0.05, ge=0)
    sweep_max_penetration_atr: float = Field(1.5, gt=0)
    sweep_min_rejection_ratio: float = Field(0.35, gt=0, le=1)
    sweep_max_bars_to_reject: int = Field(3, ge=1)
    sweep_require_close_back_inside: bool = True
    sweep_lookback_bars: int = Field(120, ge=10)
    pool_max_age_bars: int = Field(500, ge=10)
    displacement_after_sweep_atr: float = Field(0.5, ge=0)


class FVGConfig(BaseModel):
    model_config = {"frozen": True}

    min_size_atr: float = Field(0.15, gt=0)
    min_displacement_atr: float = Field(0.5, ge=0)
    mitigation_threshold: float = Field(
        0.5, gt=0, le=1.0, description="Fill fraction above which an FVG counts as mitigated."
    )
    invalidate_on_full_fill: bool = True
    max_age_bars: int = Field(200, ge=10)
    prefer_consequent_encroachment: bool = True


class OrderBlockConfig(BaseModel):
    model_config = {"frozen": True}

    require_bos: bool = True
    min_displacement_atr: float = Field(0.6, ge=0)
    max_lookback_bars: int = Field(50, ge=5)
    invalidate_on_body_close_through: bool = True
    max_tests_before_stale: int = Field(2, ge=1)
    use_wick_extremes: bool = True


class SRConfig(BaseModel):
    model_config = {"frozen": True}

    cluster_tolerance_atr: float = Field(0.35, gt=0)
    min_touches: int = Field(2, ge=1)
    lookback_bars: dict[str, int] = Field(default_factory=lambda: {"D1": 250, "H4": 400, "H1": 500})
    recency_halflife_bars: int = Field(200, ge=10)


class SessionConfig(BaseModel):
    model_config = {"frozen": True}

    asia_start_utc_hour: int = 23
    asia_end_utc_hour: int = 7
    london_local_start: str = "08:00"
    london_local_end: str = "16:30"
    newyork_local_start: str = "08:00"
    newyork_local_end: str = "17:00"
    london_killzone_local: tuple[str, str] = ("07:00", "10:00")
    ny_am_killzone_local: tuple[str, str] = ("08:30", "11:00")
    ny_pm_killzone_local: tuple[str, str] = ("13:30", "16:00")
    asia_killzone_utc: tuple[str, str] = ("00:00", "03:00")
    allowed_sessions: list[Session] = Field(
        default_factory=lambda: [Session.LONDON, Session.NEW_YORK, Session.OVERLAP]
    )
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    block_first_minutes_of_week: int = Field(60, ge=0)
    block_last_minutes_of_week: int = Field(120, ge=0)


class RegimeConfig(BaseModel):
    model_config = {"frozen": True}

    adx_period: int = 14
    strong_trend_adx: float = 30.0
    moderate_trend_adx: float = 20.0
    vol_percentile_low: float = 0.25
    vol_percentile_high: float = 0.75
    vol_percentile_extreme: float = 0.95
    vol_window_bars: int = 250
    abnormal_atr_multiple: float = Field(
        3.0, gt=1, description="ATR this many times its median is not a market we understand."
    )
    abnormal_spread_multiple: float = Field(4.0, gt=1)
    allowed_regimes: list[Regime] = Field(
        default_factory=lambda: [
            Regime.STRONG_BULL,
            Regime.MODERATE_BULL,
            Regime.RANGE,
            Regime.MODERATE_BEAR,
            Regime.STRONG_BEAR,
        ]
    )


class NewsConfig(BaseModel):
    model_config = {"frozen": True}

    blackout_minutes_before: dict[str, int] = Field(
        default_factory=lambda: {"CRITICAL": 60, "HIGH": 30, "MEDIUM": 15, "LOW": 0}
    )
    blackout_minutes_after: dict[str, int] = Field(
        default_factory=lambda: {"CRITICAL": 30, "HIGH": 15, "MEDIUM": 10, "LOW": 0}
    )
    require_spread_normalised_after: bool = True
    post_event_spread_ratio: float = Field(1.5, gt=1)
    post_event_atr_ratio: float = Field(2.0, gt=1)
    max_news_age_minutes: int = Field(30, ge=1)
    max_macro_age_days: int = Field(3, ge=1)
    news_risk_blocks_a_plus: str = "HIGH"
    news_risk_blackout: str = "EXTREME"
    max_news_score_contribution: float = Field(
        4.0, ge=0, le=10, description="News can never move a classification on its own."
    )
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-5"
    calendar_file: str | None = Field(
        None,
        description=(
            "Path to the file written by the MQL5 calendar relay EA (see mql5/). "
            "This is the preferred primary calendar source: free, already installed, "
            "and on the broker's own clock."
        ),
    )


class DataConfig(BaseModel):
    model_config = {"frozen": True}

    symbol_override: str | None = None
    symbol_patterns: list[str] = Field(default_factory=lambda: [r"^XAU", r"^GOLD"])
    analysis_timeframes: list[Timeframe] = Field(
        default_factory=lambda: [
            Timeframe.M5,
            Timeframe.M15,
            Timeframe.H1,
            Timeframe.H4,
            Timeframe.D1,
            Timeframe.W1,
            Timeframe.MN1,
        ]
    )
    bars_to_load: dict[str, int] = Field(
        default_factory=lambda: {
            "M1": 1500,
            "M5": 1500,
            "M15": 1000,
            "H1": 800,
            "H4": 500,
            "D1": 400,
            "W1": 200,
            "MN1": 120,
        }
    )
    lake_path: str = "data/lake"
    fred_api_key: str | None = None
    synthetic_dxy: bool = True
    dxy_symbols: dict[str, str] = Field(
        default_factory=lambda: {
            "EURUSD": "EURUSD",
            "USDJPY": "USDJPY",
            "GBPUSD": "GBPUSD",
            "USDCAD": "USDCAD",
            "USDSEK": "USDSEK",
            "USDCHF": "USDCHF",
        }
    )


class BrokerConfig(BaseModel):
    model_config = {"frozen": True}

    kind: Literal["mt5_grpc", "mt5_direct", "sim", "paper"] = "sim"
    bridge_address: str = "127.0.0.1:50551"
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None
    magic: int = 20260831
    health_timeout_seconds: float = 5.0
    max_health_failures: int = 3


class DatabaseConfig(BaseModel):
    model_config = {"frozen": True}

    url: str = "sqlite:///data/xauusd.db"
    echo: bool = False
    pool_size: int = 5


class AlertConfig(BaseModel):
    model_config = {"frozen": True}

    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    email_enabled: bool = False
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    min_level: str = "WARNING"


class DashboardConfig(BaseModel):
    """The dashboard is the only component with a network listener and two write paths
    (halt, flatten). Both properties make it the attack surface, so the bind address and
    the token are validated together rather than left to the operator to get right."""

    model_config = {"frozen": True}

    host: str = "127.0.0.1"
    port: int = 8000
    auth_token: str | None = None
    # Read by the engine to reach the dashboard's publish endpoint.
    url: str = "http://127.0.0.1:8000"
    command_poll_seconds: int = 5

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    @model_validator(mode="after")
    def _remote_bind_requires_a_token(self) -> DashboardConfig:
        # A token is optional on loopback, where the OS is the boundary. Off loopback
        # there is no boundary, and /api/commands/flatten is a POST away — so refuse to
        # start rather than serve an open control surface.
        if not self.is_loopback and not self.auth_token:
            raise ValueError(
                f"dashboard.host={self.host!r} is not loopback and no auth_token is set. "
                "The dashboard can halt the engine and flatten positions; it must not be "
                "reachable off-host without a token. Set XAUUSD_DASHBOARD__AUTH_TOKEN, or "
                "keep host=127.0.0.1 and reach it over WireGuard or an SSH tunnel."
            )
        if self.auth_token is not None and len(self.auth_token) < 16:
            raise ValueError("dashboard.auth_token must be at least 16 characters")
        return self


# The merged YAML layer, published for the settings source below. It is process-global
# because pydantic-settings constructs its sources from the class, not the call.
_YAML_LAYER: dict[str, Any] = {}


class YamlSettingsSource(PydanticBaseSettingsSource):
    """The layered YAML files, as a settings SOURCE rather than constructor arguments.

    This distinction is the whole point. Passing the merged YAML as `Settings(**merged)`
    made it init state, and init state outranks the environment in pydantic-settings —
    so every key that happened to appear in base.yaml silently ignored its XAUUSD_*
    variable. `XAUUSD_DATABASE__URL` was the dangerous one: an operator following
    .env.example would point at Postgres and keep journalling to local SQLite, with no
    error to say otherwise.

    As a source it can be ordered properly: explicit arguments win, then the
    environment, then these files, then the defaults.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return _YAML_LAYER.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(_YAML_LAYER)


class Settings(BaseSettings):
    """Root configuration object."""

    model_config = SettingsConfigDict(
        env_prefix="XAUUSD_",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
        # Without env_file, pydantic-settings never opens `.env` at all — and `.env` is
        # where every piece of documentation tells the operator to put their broker
        # credentials. Its absence was silent: settings simply fell back to the config
        # files, so the engine reported the simulated broker and login=0 while a
        # correctly-filled .env sat next to it.
        env_file=".env",
        env_file_encoding="utf-8",
    )

    env: str = "dev"
    mode: Mode = Mode.PAPER
    live_trading: bool = Field(
        False, description="Key 1 of 2. The arming file is key 2. Both are required."
    )
    live_arming_file: str = "config/live_arming.json"
    symbol: str = "XAUUSD"
    timezone_london: str = "Europe/London"
    timezone_newyork: str = "America/New_York"
    log_level: str = "INFO"
    log_json: bool = True
    decision_interval_seconds: int = 300

    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    thresholds: StrategyThresholds = Field(default_factory=StrategyThresholds)
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    structure: StructureConfig = Field(default_factory=StructureConfig)
    liquidity: LiquidityConfig = Field(default_factory=LiquidityConfig)
    fvg: FVGConfig = Field(default_factory=FVGConfig)
    order_block: OrderBlockConfig = Field(default_factory=OrderBlockConfig)
    sr: SRConfig = Field(default_factory=SRConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    enabled_strategies: list[str] = Field(default_factory=lambda: ["sweep_mss_fvg"])

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first: explicit arguments, environment, .env, YAML, secrets."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _live_requires_mode(self) -> Settings:
        if self.live_trading and self.mode is not Mode.LIVE:
            raise ValueError("live_trading=true requires mode=LIVE")
        if self.mode is Mode.LIVE and not self.live_trading:
            raise ValueError("mode=LIVE requires live_trading=true (two-key arming)")
        return self

    def config_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
        return hashlib.blake2s(payload.encode(), digest_size=12).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_env_name(env_file: str | Path = ".env") -> str:
    """Which config environment to load: dev, demo or live.

    This has to consider `.env` as well as the process environment. Everything else
    reaches Settings through pydantic, which reads both — but WHICH yaml file to layer
    is decided here, before Settings exists. Reading only os.environ meant an operator
    who set XAUUSD_ENV=demo in .env got demo in `settings.env` while the loader still
    layered dev.yaml, so the broker stayed on the simulator and nothing said why.
    """
    from_process = os.environ.get("XAUUSD_ENV")
    if from_process:
        return from_process
    path = Path(env_file)
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("XAUUSD_ENV="):
                value = line.partition("=")[2].strip().strip("\"'")
                if value:
                    return value
    return "dev"


def load_settings(
    config_dir: str | Path = "config",
    env: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load layered configuration. Raises immediately on an invalid value."""
    cfg_dir = Path(config_dir)
    env = env or _resolve_env_name()
    merged: dict[str, Any] = {}
    for name in ("base.yaml", f"{env}.yaml", "local.yaml"):
        path = cfg_dir / name
        if path.exists():
            loaded = yaml.safe_load(path.read_text()) or {}
            merged = _deep_merge(merged, loaded)
    merged.setdefault("env", env)

    global _YAML_LAYER
    previous = _YAML_LAYER
    _YAML_LAYER = merged
    try:
        # `overrides` stays init state: an explicit argument outranks the environment,
        # which is what a caller passing one means.
        return Settings(**(overrides or {}))
    finally:
        _YAML_LAYER = previous


def verify_live_arming(settings: Settings, account_login: int) -> tuple[bool, str]:
    """Key 2 of the two-key arming.

    Live trading requires BOTH `live_trading: true` in config AND an arming file whose
    account number matches the connected account. A config edit alone cannot arm live
    trading, and an arming file copied to a different machine will not match.
    """
    if not settings.live_trading:
        return False, "live_trading flag is false"
    path = Path(settings.live_arming_file)
    if not path.exists():
        return False, f"arming file missing: {path}"
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return False, f"arming file unreadable: {exc}"
    armed_login = data.get("account_login")
    if armed_login is None:
        return False, "arming file has no account_login"
    if int(armed_login) != int(account_login):
        return False, (
            f"arming file is for account {armed_login}, connected account is {account_login}"
        )
    if not data.get("acknowledged_risk"):
        return False, "arming file missing acknowledged_risk=true"
    return True, "armed"
