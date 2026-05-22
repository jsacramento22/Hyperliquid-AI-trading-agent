from __future__ import annotations

import pytest

from hl_agent.account import AccountState, OpenOrder, Position
from hl_agent.risk import (
    check_cancel,
    check_close,
    check_open_or_increase,
)
from hl_agent.settings import RiskConfig


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig(
        max_leverage=2.0,
        max_position_pct_per_asset=0.25,
        max_total_notional_pct=0.50,
        daily_drawdown_kill_switch_pct=-0.10,
        min_order_usd=10.0,
    )


def _account(equity: float = 1000.0, positions=None, orders=None) -> AccountState:
    return AccountState(
        address="0xabc",
        equity_usd=equity,
        free_margin_usd=equity,
        total_notional_usd=sum(p.position_value_usd for p in (positions or [])),
        margin_used_usd=0.0,
        positions=positions or [],
        open_orders=orders or [],
    )


def _pos(asset: str, size: float, entry: float) -> Position:
    return Position(
        asset=asset,
        size=size,
        entry_px=entry,
        position_value_usd=abs(size * entry),
        unrealized_pnl_usd=0.0,
        leverage=1.0,
        liquidation_px=None,
        margin_used_usd=0.0,
    )


def test_disallowed_asset(risk_cfg):
    res = check_open_or_increase(
        account=_account(),
        starting_equity_usd=1000,
        asset="DOGE",
        side="buy",
        usd_size=50,
        allowed_assets=["BTC", "ETH"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "not in allowed list" in res.reason


def test_below_min_order_size(risk_cfg):
    res = check_open_or_increase(
        account=_account(),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=5,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "min_order_usd" in res.reason


def test_per_asset_cap(risk_cfg):
    # equity 1000, cap 25% = 250 per asset; 300 should fail.
    res = check_open_or_increase(
        account=_account(),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=300,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "per-asset cap" in res.reason


def test_per_asset_cap_pass_at_boundary(risk_cfg):
    res = check_open_or_increase(
        account=_account(),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=250,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert res.ok, res.reason


def test_total_notional_cap(risk_cfg):
    # equity 1000, total cap 50% = 500. ETH has 250, asking 300 BTC -> 550 > 500.
    eth = _pos("ETH", size=0.1, entry=2500)  # notional 250
    res = check_open_or_increase(
        account=_account(positions=[eth]),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=300,
        allowed_assets=["BTC", "ETH"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "total notional" in res.reason or "per-asset cap" in res.reason


def test_dd_kill_switch_blocks_new_open(risk_cfg):
    # equity dropped from 1000 -> 850 = -15% > 10% kill switch.
    res = check_open_or_increase(
        account=_account(equity=850),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=50,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "kill switch" in res.reason


def test_dd_kill_switch_does_not_block_close(risk_cfg):
    pos = _pos("BTC", size=0.001, entry=60000)
    res = check_close(account=_account(equity=850, positions=[pos]), asset="BTC")
    assert res.ok


def test_close_with_no_position_fails(risk_cfg):
    res = check_close(account=_account(), asset="BTC")
    assert not res.ok


def test_cancel_with_no_orders_fails(risk_cfg):
    res = check_cancel(account=_account(), asset="BTC")
    assert not res.ok


def test_cancel_with_orders_passes(risk_cfg):
    o = OpenOrder(asset="BTC", oid=1, side="buy", size=0.001, limit_px=60000, reduce_only=False)
    res = check_cancel(account=_account(orders=[o]), asset="BTC")
    assert res.ok


def test_zero_equity_blocked(risk_cfg):
    res = check_open_or_increase(
        account=_account(equity=0),
        starting_equity_usd=1000,
        asset="BTC",
        side="buy",
        usd_size=50,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert not res.ok
    assert "equity" in res.reason


def test_reducing_position_skips_per_asset_cap(risk_cfg):
    # We're long BTC at 300 notional (over cap). Selling 100 reduces it — still
    # over the cap in absolute terms but not increasing, so per-asset check skips.
    pos = _pos("BTC", size=300 / 60000, entry=60000)
    res = check_open_or_increase(
        account=_account(equity=1000, positions=[pos]),
        starting_equity_usd=1000,
        asset="BTC",
        side="sell",
        usd_size=100,
        allowed_assets=["BTC"],
        risk=risk_cfg,
    )
    assert res.ok, res.reason
