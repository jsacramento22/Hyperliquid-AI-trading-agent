from __future__ import annotations

from hl_agent.account import AccountState, OpenOrder, Position
from hl_agent.context import render_context
from hl_agent.market_data import AssetSnapshot, Candle, MarketSnapshot


def _candles(n: int, start: float, step: float = 100.0) -> list[Candle]:
    out = []
    base_ms = 1_700_000_000_000
    for i in range(n):
        px = start + i * step
        out.append(
            Candle(
                open_ms=base_ms + i * 3_600_000,
                close_ms=base_ms + (i + 1) * 3_600_000,
                open=px,
                high=px + 50,
                low=px - 50,
                close=px + 25,
                volume=1.5,
            )
        )
    return out


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_ms=1_700_000_000_000,
        assets={
            "BTC": AssetSnapshot(
                asset="BTC",
                mid=60_000,
                mark=60_010,
                funding_hourly=0.00005,
                open_interest=1234.5,
                day_volume_usd=1_000_000_000,
                sz_decimals=5,
                candles_15m=_candles(12, 59_500, step=25),
                candles_1h=_candles(24, 59_000),
                candles_4h=_candles(14, 58_000, step=400),
            ),
            "ETH": AssetSnapshot(
                asset="ETH",
                mid=3_000,
                mark=3_001,
                funding_hourly=-0.00002,
                open_interest=20000,
                day_volume_usd=500_000_000,
                sz_decimals=4,
                candles_15m=_candles(12, 2_980, step=2),
                candles_1h=_candles(24, 2_900, step=10),
                candles_4h=_candles(14, 2_800, step=40),
            ),
        },
    )


def _account_with_position() -> AccountState:
    pos = Position(
        asset="BTC",
        size=0.005,
        entry_px=60_000,
        position_value_usd=300,
        unrealized_pnl_usd=12.5,
        leverage=2.0,
        liquidation_px=45_000,
        margin_used_usd=150,
    )
    order = OpenOrder(
        asset="ETH", oid=42, side="buy", size=0.1, limit_px=2_950, reduce_only=False
    )
    return AccountState(
        address="0xabc",
        equity_usd=1000,
        free_margin_usd=850,
        total_notional_usd=300,
        margin_used_usd=150,
        positions=[pos],
        open_orders=[order],
    )


def test_render_includes_assets_and_account():
    txt = render_context(_snapshot(), _account_with_position())
    assert "## BTC" in txt
    assert "## ETH" in txt
    assert "## Account" in txt
    assert "long" in txt
    assert "Open orders" in txt
    assert "15m candles" in txt
    assert "1h candles" in txt
    assert "4h candles" in txt
    assert "late-entry check" in txt


def test_render_handles_empty_account():
    empty = AccountState(
        address="0xabc",
        equity_usd=1000,
        free_margin_usd=1000,
        total_notional_usd=0,
        margin_used_usd=0,
    )
    txt = render_context(_snapshot(), empty)
    assert "Open positions: none" in txt
    assert "Open orders: none" in txt
