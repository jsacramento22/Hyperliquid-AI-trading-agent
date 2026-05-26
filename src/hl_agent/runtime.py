"""Runtime-mutable state shared between the headless cycle, the API server,
and the UI. Lives in the SQLite `runtime_state` table so all readers see the
same values without needing IPC."""
from __future__ import annotations

from .settings import RiskConfig, Settings
from .storage import Storage

KEY_PAUSED = "paused"
KEY_RISK_OVERRIDES = "risk_overrides"
KEY_POSITION_LEVERAGE = "position_leverage"
KEY_POSITION_MARGIN_CROSS = "position_margin_cross"
KEY_MODEL = "model"

# Allowlist for the live model switch. Must match keys in cost.PRICING so
# cost calculations stay correct. Adding a new model here AND a pricing
# entry in cost.py is all that's needed to make it selectable.
SUPPORTED_MODELS = (
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-7",
)

_RISK_FIELDS = {
    "max_leverage",
    "max_position_pct_per_asset",
    "max_total_notional_pct",
    "daily_drawdown_kill_switch_pct",
    "min_order_usd",
}


def is_paused(storage: Storage) -> bool:
    val = storage.get_runtime_value(KEY_PAUSED)
    return bool(val) if val is not None else False


def set_paused(storage: Storage, paused: bool) -> None:
    storage.set_runtime_value(KEY_PAUSED, bool(paused))


def get_risk_overrides(storage: Storage) -> dict | None:
    val = storage.get_runtime_value(KEY_RISK_OVERRIDES)
    if not isinstance(val, dict):
        return None
    return {k: v for k, v in val.items() if k in _RISK_FIELDS}


def set_risk_overrides(storage: Storage, overrides: dict) -> None:
    cleaned = {k: float(v) for k, v in overrides.items() if k in _RISK_FIELDS}
    storage.set_runtime_value(KEY_RISK_OVERRIDES, cleaned)


def clear_risk_overrides(storage: Storage) -> None:
    storage.set_runtime_value(KEY_RISK_OVERRIDES, {})


def effective_risk(settings: Settings, storage: Storage) -> RiskConfig:
    base = settings.config.risk.model_dump()
    overrides = get_risk_overrides(storage) or {}
    merged = {**base, **overrides}
    return RiskConfig(**merged)


def get_position_leverage_override(storage: Storage) -> int | None:
    val = storage.get_runtime_value(KEY_POSITION_LEVERAGE)
    return int(val) if val is not None else None


def set_position_leverage_override(storage: Storage, leverage: int) -> None:
    storage.set_runtime_value(KEY_POSITION_LEVERAGE, int(leverage))


def get_position_margin_cross_override(storage: Storage) -> bool | None:
    val = storage.get_runtime_value(KEY_POSITION_MARGIN_CROSS)
    return bool(val) if val is not None else None


def set_position_margin_cross_override(storage: Storage, is_cross: bool) -> None:
    storage.set_runtime_value(KEY_POSITION_MARGIN_CROSS, bool(is_cross))


def effective_position_leverage(settings: Settings, storage: Storage) -> int:
    return get_position_leverage_override(storage) or settings.config.position_leverage


def effective_position_margin_cross(settings: Settings, storage: Storage) -> bool:
    override = get_position_margin_cross_override(storage)
    return settings.config.position_margin_cross if override is None else override


def get_model_override(storage: Storage) -> str | None:
    val = storage.get_runtime_value(KEY_MODEL)
    if not isinstance(val, str):
        return None
    return val if val in SUPPORTED_MODELS else None


def set_model_override(storage: Storage, model: str) -> None:
    if model not in SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported model {model!r}; choose one of {list(SUPPORTED_MODELS)}"
        )
    storage.set_runtime_value(KEY_MODEL, model)


def clear_model_override(storage: Storage) -> None:
    storage.set_runtime_value(KEY_MODEL, None)


def effective_model(settings: Settings, storage: Storage) -> str:
    """Override > config. Used by every cycle so a UI flip takes effect on
    the next scheduled run without a restart. Switching invalidates the
    Anthropic prompt cache (cache keys include the model), so expect a
    one-cycle cost bump after each change."""
    override = get_model_override(storage)
    return override if override else settings.config.model
