from __future__ import annotations

from dataclasses import dataclass, field

from .hl_client import HLClient


@dataclass
class Position:
    asset: str
    size: float                # signed: positive = long, negative = short
    entry_px: float
    position_value_usd: float  # absolute notional in USD
    unrealized_pnl_usd: float
    leverage: float
    liquidation_px: float | None
    margin_used_usd: float


@dataclass
class OpenOrder:
    asset: str
    oid: int
    side: str                  # "buy" | "sell"
    size: float
    limit_px: float
    reduce_only: bool


@dataclass
class AccountState:
    address: str
    equity_usd: float          # account value
    free_margin_usd: float     # withdrawable
    total_notional_usd: float
    margin_used_usd: float
    positions: list[Position] = field(default_factory=list)
    open_orders: list[OpenOrder] = field(default_factory=list)


def _parse_position(asset_pos: dict) -> Position:
    p = asset_pos["position"]
    lev = p.get("leverage") or {}
    return Position(
        asset=p["coin"],
        size=float(p["szi"]),
        entry_px=float(p.get("entryPx") or 0.0),
        position_value_usd=abs(float(p.get("positionValue") or 0.0)),
        unrealized_pnl_usd=float(p.get("unrealizedPnl") or 0.0),
        leverage=float(lev.get("value", 1)),
        liquidation_px=float(p["liquidationPx"]) if p.get("liquidationPx") else None,
        margin_used_usd=float(p.get("marginUsed") or 0.0),
    )


def _parse_open_order(o: dict) -> OpenOrder:
    return OpenOrder(
        asset=o["coin"],
        oid=int(o["oid"]),
        side="buy" if o["side"] == "B" else "sell",
        size=float(o["sz"]),
        limit_px=float(o["limitPx"]),
        reduce_only=bool(o.get("reduceOnly", False)),
    )


def get_state(client: HLClient) -> AccountState:
    raw = client.info.user_state(client.account_address)
    summary = raw.get("marginSummary", {})
    positions = [_parse_position(ap) for ap in raw.get("assetPositions", [])]
    orders_raw = client.info.open_orders(client.account_address)
    orders = [_parse_open_order(o) for o in orders_raw]

    return AccountState(
        address=client.account_address,
        equity_usd=float(summary.get("accountValue") or 0.0),
        free_margin_usd=float(raw.get("withdrawable") or 0.0),
        total_notional_usd=float(summary.get("totalNtlPos") or 0.0),
        margin_used_usd=float(summary.get("totalMarginUsed") or 0.0),
        positions=positions,
        open_orders=orders,
    )
