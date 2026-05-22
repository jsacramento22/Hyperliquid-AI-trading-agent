from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "place_market_order",
        "description": (
            "Open or add to a position with a market order. Size is given in USD "
            "notional and converted to coin units at the current mid price. The order "
            "uses an IOC limit with the configured slippage tolerance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Perp ticker, e.g. BTC or ETH."},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "usd_size": {
                    "type": "number",
                    "description": "Notional size in USD. Must be >= the configured min_order_usd.",
                },
                "reduce_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, the order can only reduce an existing position.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One-sentence rationale for this trade.",
                },
            },
            "required": ["asset", "side", "usd_size", "reasoning"],
        },
    },
    {
        "name": "place_limit_order",
        "description": (
            "Place a resting limit order. Size in USD notional, price in quote (USD)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "usd_size": {"type": "number"},
                "limit_px": {"type": "number"},
                "tif": {
                    "type": "string",
                    "enum": ["Gtc", "Ioc", "Alo"],
                    "default": "Gtc",
                    "description": "Gtc=good-til-cancelled, Ioc=immediate-or-cancel, Alo=add-liquidity-only.",
                },
                "reduce_only": {"type": "boolean", "default": False},
                "reasoning": {"type": "string"},
            },
            "required": ["asset", "side", "usd_size", "limit_px", "reasoning"],
        },
    },
    {
        "name": "cancel_all",
        "description": "Cancel all open orders for an asset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["asset", "reasoning"],
        },
    },
    {
        "name": "close_position",
        "description": (
            "Market-close the entire open position for an asset. Always allowed even when "
            "the daily-drawdown kill switch has fired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["asset", "reasoning"],
        },
    },
    {
        "name": "hold",
        "description": "Take no action this cycle. Use when no opportunity is worth acting on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
            },
            "required": ["reasoning"],
        },
    },
]
