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


def test_deepseek_v3_2_is_supported(storage):
    """The V3.2 entry must be in SUPPORTED_MODELS and routed via
    openrouter — otherwise the UI dropdown can't expose it and the
    set_model_override path would reject it."""
    assert "deepseek/deepseek-v3.2" in runtime.SUPPORTED_MODELS
    assert runtime.SUPPORTED_MODELS["deepseek/deepseek-v3.2"] == "openrouter"


def test_set_model_override_atomically_pairs_v3_2_with_openrouter(storage):
    """Selecting V3.2 must atomically set both the model AND the
    provider — the runtime must never expose model='deepseek/...' with
    provider='anthropic', which would 404 the API call."""
    runtime.set_model_override(storage, "deepseek/deepseek-v3.2")
    assert runtime.get_model_override(storage) == "deepseek/deepseek-v3.2"
    assert runtime.get_provider_override(storage) == "openrouter"
