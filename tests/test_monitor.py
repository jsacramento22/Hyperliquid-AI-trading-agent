from __future__ import annotations

from unittest.mock import patch

import pytest

from hl_agent import monitor
from hl_agent.account import AccountState, Position
from hl_agent.monitor import _upnl_pct_vs_entry, check_and_close, reset_streaks
from hl_agent.settings import (
    AppConfig,
    MarketDataConfig,
    RiskConfig,
    Secrets,
    Settings,
    StopLossConfig,
    StorageConfig,
    TakeProfitConfig,
)


def _pos(asset: str, size: float, entry: float, upnl: float) -> Position:
    return Position(
        asset=asset,
        size=size,
        entry_px=entry,
        position_value_usd=abs(size * entry),  # close enough; mark price unknown
        unrealized_pnl_usd=upnl,
        leverage=1.0,
        liquidation_px=None,
        margin_used_usd=0.0,
    )


def test_upnl_pct_long_winner():
    # Long 0.001 BTC @ 60000, currently +$1.50 = 1.50/60.00 = 2.5%
    p = _pos("BTC", 0.001, 60_000, 1.50)
    assert abs(_upnl_pct_vs_entry(p) - 0.025) < 1e-6


def test_upnl_pct_short_winner():
    # Short 0.1 ETH @ 2400, +$3 = 3/240 = 1.25%
    p = _pos("ETH", -0.1, 2_400, 3.0)
    assert abs(_upnl_pct_vs_entry(p) - 0.0125) < 1e-6


def test_upnl_pct_loser():
    p = _pos("ETH", -0.1, 2_400, -1.20)
    assert _upnl_pct_vs_entry(p) < 0


def test_upnl_pct_zero_position_safe():
    p = _pos("ETH", 0.0, 2_400, 0.0)
    assert _upnl_pct_vs_entry(p) == 0.0


# ─── Streak / consecutive-checks behavior ──────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_streak_state():
    reset_streaks()
    yield
    reset_streaks()


def _settings(
    *,
    threshold=0.015,
    required=2,
    slippage=0.005,
    paused=False,
    tp_enabled=True,
    sl_enabled=False,
    sl_threshold=0.015,
    sl_required=2,
) -> Settings:
    return Settings(
        config=AppConfig(
            network="testnet",
            assets=["BTC", "ETH"],
            risk=RiskConfig(),
            market_data=MarketDataConfig(),
            storage=StorageConfig(path="data/test_monitor.db"),
            take_profit=TakeProfitConfig(
                enabled=tp_enabled,
                pct=threshold,
                require_consecutive_checks=required,
                close_slippage=slippage,
            ),
            stop_loss=StopLossConfig(
                enabled=sl_enabled,
                pct=sl_threshold,
                require_consecutive_checks=sl_required,
                close_slippage=slippage,
            ),
        ),
        secrets=Secrets(),
    )


def _account(*positions: Position) -> AccountState:
    return AccountState(
        address="0xabc",
        equity_usd=1000.0,
        free_margin_usd=900.0,
        total_notional_usd=sum(p.position_value_usd for p in positions),
        margin_used_usd=100.0,
        positions=list(positions),
    )


class _FakeExchange:
    def __init__(self):
        self.calls: list[dict] = []

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append({"coin": coin, "slippage": slippage})
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"totalSz": "0.1", "avgPx": "2400", "oid": 1}}
                    ]
                },
            },
        }


class _FakeClient:
    def __init__(self):
        self.exchange = _FakeExchange()
        self.account_address = "0xabc"


@pytest.fixture
def fake_client():
    return _FakeClient()


def _patch(account_state, fake_client):
    """Patch the network/storage functions used by check_and_close."""
    return [
        patch("hl_agent.monitor.build_client", return_value=fake_client),
        patch("hl_agent.monitor.account_mod.get_state", return_value=account_state),
        patch("hl_agent.monitor.runtime.is_paused", return_value=False),
        patch("hl_agent.monitor.Storage"),  # avoid touching real DB
    ]


def test_first_check_above_threshold_does_not_fire(fake_client):
    """With require_consecutive_checks=2, first observation should pend, not fire."""
    s = _settings(required=2)
    # Long with +2% gain (above 1.5% threshold)
    pos = _pos("BTC", 0.001, 60_000, 1.20)  # +$1.20 / $60 = 2%
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert closes == []
    assert fake_client.exchange.calls == []  # no market_close call


def test_two_consecutive_checks_fire(fake_client):
    s = _settings(required=2)
    pos = _pos("BTC", 0.001, 60_000, 1.20)
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        check_and_close(s)  # first tick — pending
        closes = check_and_close(s)  # second tick — fire
    finally:
        for p in patches:
            p.stop()
    assert len(closes) == 1
    assert closes[0].asset == "BTC"
    assert len(fake_client.exchange.calls) == 1
    # Slippage was passed through correctly
    assert fake_client.exchange.calls[0]["slippage"] == 0.005


def test_dropping_below_threshold_resets_streak(fake_client):
    s = _settings(required=2)
    above = _pos("BTC", 0.001, 60_000, 1.20)   # +2%
    below = _pos("BTC", 0.001, 60_000, 0.50)   # +0.83%, below threshold
    patches_above = _patch(_account(above), fake_client)
    patches_below = _patch(_account(below), fake_client)
    for p in patches_above:
        p.start()
    try:
        check_and_close(s)  # streak = 1
    finally:
        for p in patches_above:
            p.stop()
    for p in patches_below:
        p.start()
    try:
        check_and_close(s)  # below threshold — streak reset to 0
    finally:
        for p in patches_below:
            p.stop()
    for p in patches_above:
        p.start()
    try:
        closes = check_and_close(s)  # streak back to 1, NOT firing
    finally:
        for p in patches_above:
            p.stop()
    assert closes == []
    assert fake_client.exchange.calls == []


def test_required_one_fires_immediately(fake_client):
    """With require_consecutive_checks=1, first tick above threshold fires."""
    s = _settings(required=1)
    pos = _pos("BTC", 0.001, 60_000, 1.20)
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert len(closes) == 1


def test_closed_position_clears_state(fake_client):
    """Position vanishing from positions list should clear its streak so a
    re-opened position starts fresh."""
    s = _settings(required=2)
    pos = _pos("BTC", 0.001, 60_000, 1.20)
    patches_pos = _patch(_account(pos), fake_client)
    patches_empty = _patch(_account(), fake_client)
    for p in patches_pos:
        p.start()
    try:
        check_and_close(s)  # streak = 1
    finally:
        for p in patches_pos:
            p.stop()
    for p in patches_empty:
        p.start()
    try:
        check_and_close(s)  # no positions — streak cleared
    finally:
        for p in patches_empty:
            p.stop()
    for p in patches_pos:
        p.start()
    try:
        closes = check_and_close(s)  # back to streak 1, NOT firing
    finally:
        for p in patches_pos:
            p.stop()
    assert closes == []


# ─── Stop-loss behavior ────────────────────────────────────────────────────


def test_stop_loss_fires_after_consecutive_breaches(fake_client):
    s = _settings(tp_enabled=False, sl_enabled=True, sl_threshold=0.015, sl_required=2)
    # Long with -2% loss (below -1.5% threshold)
    pos = _pos("BTC", 0.001, 60_000, -1.20)  # -$1.20 / $60 = -2%
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        first = check_and_close(s)  # pending
        second = check_and_close(s)  # fires
    finally:
        for p in patches:
            p.stop()
    assert first == []
    assert len(second) == 1
    assert second[0].kind == "sl"
    assert second[0].asset == "BTC"
    assert second[0].upnl_usd < 0
    assert len(fake_client.exchange.calls) == 1


def test_stop_loss_short_loser_triggers(fake_client):
    """For a short, loss = price went UP, so uPnL is negative."""
    s = _settings(tp_enabled=False, sl_enabled=True, sl_required=1)
    # Short 0.1 ETH @ 2400, -$5 = -2.08% loss
    pos = _pos("ETH", -0.1, 2_400, -5.0)
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert len(closes) == 1
    assert closes[0].kind == "sl"
    assert closes[0].side == "short"


def test_stop_loss_does_not_fire_on_winner(fake_client):
    """Winner above threshold should NOT trigger SL (only TP if enabled)."""
    s = _settings(tp_enabled=False, sl_enabled=True, sl_required=1)
    pos = _pos("BTC", 0.001, 60_000, 1.20)  # +2% gain
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert closes == []


def test_stop_loss_partial_loss_does_not_fire(fake_client):
    """-0.5% loss is below the -1.5% threshold."""
    s = _settings(tp_enabled=False, sl_enabled=True, sl_required=1)
    pos = _pos("BTC", 0.001, 60_000, -0.30)  # -0.5%
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert closes == []


def test_tp_and_sl_independent_streaks(fake_client):
    """Both enabled. A position oscillating around zero should not accumulate
    a TP streak from a brief gain or an SL streak from a brief loss if the
    consecutive requirement isn't met for either."""
    s = _settings(
        tp_enabled=True, threshold=0.015, required=2,
        sl_enabled=True, sl_threshold=0.015, sl_required=2,
    )
    winner = _pos("BTC", 0.001, 60_000, 1.20)   # +2%
    loser = _pos("BTC", 0.001, 60_000, -1.20)   # -2%

    patches_w = _patch(_account(winner), fake_client)
    patches_l = _patch(_account(loser), fake_client)

    # Tick 1: above TP → tp streak=1
    for p in patches_w:
        p.start()
    try:
        check_and_close(s)
    finally:
        for p in patches_w:
            p.stop()
    # Tick 2: below SL → sl streak=1, tp streak resets to 0
    for p in patches_l:
        p.start()
    try:
        check_and_close(s)
    finally:
        for p in patches_l:
            p.stop()
    # Tick 3: above TP again → tp streak=1, sl streak resets to 0
    for p in patches_w:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches_w:
            p.stop()
    assert closes == []  # neither fired
    assert fake_client.exchange.calls == []


def test_tp_takes_priority_over_sl_when_both_enabled(fake_client):
    """If TP fires, the position is gone — SL evaluation shouldn't double-close."""
    s = _settings(
        tp_enabled=True, threshold=0.015, required=1,
        sl_enabled=True, sl_threshold=0.015, sl_required=1,
    )
    # Winner — only TP applies
    pos = _pos("BTC", 0.001, 60_000, 1.20)
    patches = _patch(_account(pos), fake_client)
    for p in patches:
        p.start()
    try:
        closes = check_and_close(s)
    finally:
        for p in patches:
            p.stop()
    assert len(closes) == 1
    assert closes[0].kind == "tp"
    assert len(fake_client.exchange.calls) == 1  # not 2
