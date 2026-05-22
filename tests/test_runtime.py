from __future__ import annotations

from pathlib import Path

import pytest

from hl_agent import runtime
from hl_agent.settings import (
    AppConfig,
    MarketDataConfig,
    RiskConfig,
    Secrets,
    Settings,
    StorageConfig,
)
from hl_agent.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        config=AppConfig(
            network="testnet",
            model="claude-sonnet-4-6",
            assets=["BTC", "ETH"],
            cadence_minutes=15,
            risk=RiskConfig(
                max_leverage=2.0,
                max_position_pct_per_asset=0.25,
                max_total_notional_pct=0.50,
                daily_drawdown_kill_switch_pct=-0.10,
                min_order_usd=10.0,
            ),
            market_data=MarketDataConfig(),
            storage=StorageConfig(path="data/test.db"),
        ),
        secrets=Secrets(),
    )


def test_paused_defaults_false(storage):
    assert runtime.is_paused(storage) is False


def test_paused_round_trip(storage):
    runtime.set_paused(storage, True)
    assert runtime.is_paused(storage) is True
    runtime.set_paused(storage, False)
    assert runtime.is_paused(storage) is False


def test_risk_overrides_default_none(storage):
    assert runtime.get_risk_overrides(storage) is None


def test_risk_overrides_round_trip(storage):
    runtime.set_risk_overrides(storage, {"max_leverage": 1.5})
    assert runtime.get_risk_overrides(storage) == {"max_leverage": 1.5}


def test_risk_overrides_only_known_fields(storage):
    runtime.set_risk_overrides(
        storage,
        {"max_leverage": 1.5, "bogus_field": 42, "another": "x"},
    )
    out = runtime.get_risk_overrides(storage)
    assert out == {"max_leverage": 1.5}


def test_effective_risk_falls_through_to_yaml(storage, settings):
    eff = runtime.effective_risk(settings, storage)
    assert eff.max_leverage == 2.0
    assert eff.max_position_pct_per_asset == 0.25


def test_effective_risk_applies_overrides(storage, settings):
    runtime.set_risk_overrides(
        storage, {"max_leverage": 3.0, "min_order_usd": 25.0}
    )
    eff = runtime.effective_risk(settings, storage)
    assert eff.max_leverage == 3.0
    assert eff.min_order_usd == 25.0
    # Non-overridden fields keep YAML values.
    assert eff.max_position_pct_per_asset == 0.25
    assert eff.max_total_notional_pct == 0.50


def test_effective_risk_clear_overrides(storage, settings):
    runtime.set_risk_overrides(storage, {"max_leverage": 5.0})
    runtime.clear_risk_overrides(storage)
    eff = runtime.effective_risk(settings, storage)
    assert eff.max_leverage == 2.0
