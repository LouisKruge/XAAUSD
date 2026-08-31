"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd.config.settings import Settings
from xauusd.database.session import Database
from xauusd.domain.types import SymbolSpec

UTC = UTC


@pytest.fixture
def db() -> Database:
    d = Database("sqlite://")
    d.create_all()
    yield d
    d.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def gold_spec() -> SymbolSpec:
    """A realistic broker XAUUSD spec: 100oz contract, $1 per $0.01 move per lot."""
    return SymbolSpec(
        symbol="XAUUSD",
        digits=2,
        point=0.01,
        contract_size=100.0,
        tick_size=0.01,
        tick_value=1.0,
        tick_value_profit=1.0,
        tick_value_loss=1.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        stops_level=10,
        freeze_level=5,
        currency_profit="USD",
        commission_per_lot=7.0,
    )


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
