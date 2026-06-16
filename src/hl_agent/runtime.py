"""Runtime-mutable state shared between the headless cycle, the API server,
and the UI. Lives in the SQLite `runtime_state` table so all readers see the
same values without needing IPC."""
from __future__ import annotations

from .settings import RiskConfig, Settings, StopLossConfig, TakeProfitConfig
from .storage import Storage

KEY_PAUSED = "paused"
KEY_RISK_OVERRIDES = "risk_overrides"
KEY_POSITION_LEVERAGE = "position_leverage"
KEY_POSITION_MARGIN_CROSS = "position_margin_cross"
KEY_MODEL = "model"
KEY_MODEL_PROVIDER = "model_provider"
KEY_TP_OVERRIDES = "take_profit_overrides"
KEY_SL_OVERRIDES = "stop_loss_overrides"

# Only these TP/SL fields are runtime-mutable from the UI. The operational
# knobs (check_interval_seconds, require_consecutive_checks, close_slippage)
# stay YAML-only — they affect scheduler timing and need a restart anyway.
_MONITOR_FIELDS = {"enabled", "pct"}

# Model allowlist for the live UI switch. Each entry pairs a model
# identifier with its required provider, so the UI can't ask for a
# DeepSeek model on Anthropic's API. The model string here MUST match a
# key in cost.PRICING for cost calc to work; the provider string MUST
# match Settings.AppConfig.model_provider's Literal options.
SUPPORTED_MODELS: dict[str, str] = {
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5-20251001": "anthropic",
    "claude-opus-4-7": "anthropic",
    "deepseek/deepseek-chat-v3.1": "openrouter",
    # V3.2 GA: Alpha Arena S1.5 winner vs V3.1 on Hyperliquid. Cheaper
    # output, agentic tool-use trained in. V3.1 stays in the list as a
    # one-click rollback if V3.2 misbehaves.
    "deepseek/deepseek-v3.2": "openrouter",
}


def provider_for_model(model: str) -> str | None:
    """Lookup the required provider for a supported model. None if not in
    the allowlist (caller should reject)."""
    return SUPPORTED_MODELS.get(model)

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


def get_provider_override(storage: Storage) -> str | None:
    val = storage.get_runtime_value(KEY_MODEL_PROVIDER)
    if not isinstance(val, str):
        return None
    return val if val in {"anthropic", "openrouter"} else None


def set_model_override(storage: Storage, model: str) -> None:
    """Set both the model override AND its required provider in one shot.
    Storing provider alongside the model means the runtime stays
    consistent — you can never have model='deepseek/...' with
    provider='anthropic', which would 404 the API call."""
    if model not in SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported model {model!r}; choose one of {list(SUPPORTED_MODELS)}"
        )
    provider = SUPPORTED_MODELS[model]
    storage.set_runtime_value(KEY_MODEL, model)
    storage.set_runtime_value(KEY_MODEL_PROVIDER, provider)


def clear_model_override(storage: Storage) -> None:
    storage.set_runtime_value(KEY_MODEL, None)
    storage.set_runtime_value(KEY_MODEL_PROVIDER, None)


def effective_model(settings: Settings, storage: Storage) -> str:
    """Override > config. Used by every cycle so a UI flip takes effect on
    the next scheduled run without a restart. Switching invalidates the
    Anthropic prompt cache (cache keys include the model), so expect a
    one-cycle cost bump after each change."""
    override = get_model_override(storage)
    return override if override else settings.config.model


def effective_provider(settings: Settings, storage: Storage) -> str:
    """Provider that should be used to reach the effective_model. Override
    takes precedence; if no override is set but a model override is, we
    derive the provider from SUPPORTED_MODELS. Falls back to the YAML
    config's model_provider only when nothing is overridden."""
    p_override = get_provider_override(storage)
    if p_override:
        return p_override
    m_override = get_model_override(storage)
    if m_override:
        # Belt-and-braces: model override exists without paired provider
        # override (shouldn't happen post-fix, but old DBs might).
        return SUPPORTED_MODELS.get(m_override, settings.config.model_provider)
    return settings.config.model_provider


# --- TP/SL runtime controls -----------------------------------------------
# Only the enabled flag and the pct threshold are runtime-mutable; the rest
# of the TakeProfit/StopLoss config stays YAML-only.

def _clean_monitor_overrides(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "pct" in raw:
        try:
            out["pct"] = float(raw["pct"])
        except (TypeError, ValueError):
            pass
    return out


def get_tp_overrides(storage: Storage) -> dict:
    return _clean_monitor_overrides(storage.get_runtime_value(KEY_TP_OVERRIDES))


def set_tp_overrides(storage: Storage, overrides: dict) -> None:
    storage.set_runtime_value(
        KEY_TP_OVERRIDES,
        {**get_tp_overrides(storage), **_clean_monitor_overrides(overrides)},
    )


def clear_tp_overrides(storage: Storage) -> None:
    storage.set_runtime_value(KEY_TP_OVERRIDES, {})


def get_sl_overrides(storage: Storage) -> dict:
    return _clean_monitor_overrides(storage.get_runtime_value(KEY_SL_OVERRIDES))


def set_sl_overrides(storage: Storage, overrides: dict) -> None:
    storage.set_runtime_value(
        KEY_SL_OVERRIDES,
        {**get_sl_overrides(storage), **_clean_monitor_overrides(overrides)},
    )


def clear_sl_overrides(storage: Storage) -> None:
    storage.set_runtime_value(KEY_SL_OVERRIDES, {})


def effective_take_profit(
    settings: Settings, storage: Storage
) -> TakeProfitConfig:
    base = settings.config.take_profit.model_dump()
    return TakeProfitConfig(**{**base, **get_tp_overrides(storage)})


def effective_stop_loss(
    settings: Settings, storage: Storage
) -> StopLossConfig:
    base = settings.config.stop_loss.model_dump()
    return StopLossConfig(**{**base, **get_sl_overrides(storage)})
