from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RiskConfig(BaseModel):
    max_leverage: float = 2.0
    max_position_pct_per_asset: float = 0.25
    max_total_notional_pct: float = 0.50
    daily_drawdown_kill_switch_pct: float = -0.10
    min_order_usd: float = 10.0


class MarketDataConfig(BaseModel):
    candles_15m: int = 12       # ~3h of recent action for late-entry detection
    candles_1h: int = 24
    candles_4h: int = 14


class StorageConfig(BaseModel):
    path: str = "data/hl_agent.db"


class TakeProfitConfig(BaseModel):
    enabled: bool = True
    pct: float = 0.015                 # 1.5% gain on entry notional
    check_interval_seconds: int = 60   # runs between agent cycles
    require_consecutive_checks: int = 2  # # of ticks above threshold before firing
    close_slippage: float = 0.005      # max slippage on the market_close (0.5%)


class StopLossConfig(BaseModel):
    """Mirror of TakeProfitConfig but for losses. `pct` is the magnitude of
    the adverse move; `pct=0.015` means close when uPnL <= -1.5% of entry."""
    enabled: bool = True
    pct: float = 0.015                 # close when uPnL <= -1.5% of entry notional
    check_interval_seconds: int = 60   # shares the monitor tick with TP
    require_consecutive_checks: int = 2
    close_slippage: float = 0.005


class AppConfig(BaseModel):
    network: Literal["testnet", "mainnet"] = "testnet"
    # Model selected at process start; live-overridable via /api/model so a
    # UI switch doesn't require a restart.
    model: str = "claude-haiku-4-5-20251001"
    # Which API to call to reach `model`. Anthropic uses native API +
    # prompt caching; openrouter uses the OpenAI-compatible endpoint at
    # https://openrouter.ai/api/v1 and supports DeepSeek V3 etc.
    model_provider: Literal["anthropic", "openrouter"] = "anthropic"
    assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    cadence_minutes: int = 15
    position_leverage: int = 2     # per-position leverage applied on the exchange
    position_margin_cross: bool = True
    risk: RiskConfig = Field(default_factory=RiskConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    take_profit: TakeProfitConfig = Field(default_factory=TakeProfitConfig)
    stop_loss: StopLossConfig = Field(default_factory=StopLossConfig)

    @field_validator("assets")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [a.upper() for a in v]


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hl_agent_private_key: str = ""
    hl_account_address: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""


class Settings(BaseModel):
    config: AppConfig
    secrets: Secrets

    @property
    def storage_path(self) -> Path:
        p = Path(self.config.storage.path)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or PROJECT_ROOT / "config.yaml"
    with path.open("r") as f:
        raw = yaml.safe_load(f)
    return Settings(config=AppConfig(**raw), secrets=Secrets())
