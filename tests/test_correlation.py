"""Tests for the same-cycle correlation block in Executor.

The bot has lost money on the pattern "open BTC short and ETH short on the
same thesis in the same cycle, both lose together on the bounce." The
prompt discourages it softly; Executor._check_correlation is the hard
code-level backstop. These tests pin its behavior."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_agent.account import AccountState, Position
from hl_agent.executor import Executor
from hl_agent.market_data import AssetSnapshot, MarketSnapshot
from hl_agent.settings import RiskConfig
from hl_agent.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


@pytest.fixture
def risk() -> RiskConfig:
    # Loose-enough caps that the correlation block is the only thing that
    # could reject a $100 order at $1000 equity.
    return RiskConfig(
        max_leverage=10.0,
        max_position_pct_per_asset=0.5,
        max_total_notional_pct=1.0,
        daily_drawdown_kill_switch_pct=-0.5,
        min_order_usd=10.0,
    )


@pytest.fixture
def empty_account() -> AccountState:
    return AccountState(
        address="0xtest",
        equity_usd=1000.0,
        free_margin_usd=1000.0,
        total_notional_usd=0.0,
        margin_used_usd=0.0,
        positions=[],
        open_orders=[],
    )


@pytest.fixture
def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_ms=0,
        assets={
            "BTC": AssetSnapshot(
                asset="BTC", mid=70000.0, mark=70000.0,
                funding_hourly=0.0, open_interest=0.0, day_volume_usd=0.0,
                sz_decimals=5,
                candles_15m=[], candles_1h=[], candles_4h=[],
            ),
            "ETH": AssetSnapshot(
                asset="ETH", mid=2000.0, mark=2000.0,
                funding_hourly=0.0, open_interest=0.0, day_volume_usd=0.0,
                sz_decimals=4,
                candles_15m=[], candles_1h=[], candles_4h=[],
            ),
        },
    )


def _exec(storage: Storage, risk: RiskConfig, cycle_id: str = "T1") -> Executor:
    """Build a dry-run executor — no exchange client needed since we never
    reach the exchange call: every test here exercises a pre-trade reject
    or a dry-run accept."""
    return Executor(
        client=None,  # dry_run=True bypasses the exchange call
        storage=storage,
        risk_config=risk,
        allowed_assets=["BTC", "ETH"],
        cycle_id=cycle_id,
        dry_run=True,
    )


def _log_open_fill(storage: Storage, cycle_id: str, asset: str, side: str) -> None:
    """Record an opening fill the way the executor would, so the
    correlation check sees it via opens_in_cycle_by_side."""
    storage.log_fill(
        cycle_id=cycle_id,
        asset=asset,
        side=side,
        requested_usd=95.0,
        raw_response={"status": "ok"},
    )


# --- opens_in_cycle_by_side -----------------------------------------------


def test_opens_in_cycle_empty(storage: Storage) -> None:
    assert storage.opens_in_cycle_by_side("nope") == {}


def test_opens_in_cycle_excludes_close_fills(storage: Storage) -> None:
    _log_open_fill(storage, "T1", "BTC", "buy")
    storage.log_fill(
        cycle_id="T1", asset="ETH", side="close",
        requested_usd=None, raw_response={},
    )
    assert storage.opens_in_cycle_by_side("T1") == {"BTC": "buy"}


def test_opens_in_cycle_first_side_wins(storage: Storage) -> None:
    # Averaging in or two limits on the same asset — the FIRST logged
    # side is what the correlation check should see (they're the same
    # direction in any sane case).
    _log_open_fill(storage, "T1", "BTC", "sell")
    _log_open_fill(storage, "T1", "BTC", "sell")
    assert storage.opens_in_cycle_by_side("T1") == {"BTC": "sell"}


def test_opens_in_cycle_isolates_by_cycle(storage: Storage) -> None:
    _log_open_fill(storage, "T1", "BTC", "sell")
    _log_open_fill(storage, "T2", "ETH", "sell")
    assert storage.opens_in_cycle_by_side("T1") == {"BTC": "sell"}
    assert storage.opens_in_cycle_by_side("T2") == {"ETH": "sell"}


# --- Executor._check_correlation -----------------------------------------


def test_blocks_same_direction_same_cycle(
    storage: Storage, risk: RiskConfig, empty_account: AccountState,
    snapshot: MarketSnapshot,
) -> None:
    """The 06-04 failure: BTC short fires, then ETH short same cycle."""
    ex = _exec(storage, risk, cycle_id="T1")

    # First leg goes through (dry-run, logs fill).
    btc = ex.apply(
        "place_limit_order",
        {"asset": "BTC", "side": "sell", "usd_size": 95.0, "limit_px": 70000.0},
        account=empty_account, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert btc.accepted, btc.reason
    # Dry-run doesn't log fills via the executor itself, so simulate the
    # in-cycle state the real executor would have created.
    _log_open_fill(storage, "T1", "BTC", "sell")

    # Second leg in the same cycle, same direction, other correlated asset.
    eth = ex.apply(
        "place_limit_order",
        {"asset": "ETH", "side": "sell", "usd_size": 95.0, "limit_px": 2000.0},
        account=empty_account, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert not eth.accepted
    assert "correlated same-direction stack" in eth.reason
    assert "BTC sell" in eth.reason


def test_allows_opposite_direction_same_cycle(
    storage: Storage, risk: RiskConfig, empty_account: AccountState,
    snapshot: MarketSnapshot,
) -> None:
    """BTC short + ETH long is a hedge, not a correlated double-down."""
    _log_open_fill(storage, "T1", "BTC", "sell")
    ex = _exec(storage, risk, cycle_id="T1")
    out = ex.apply(
        "place_limit_order",
        {"asset": "ETH", "side": "buy", "usd_size": 95.0, "limit_px": 2000.0},
        account=empty_account, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert out.accepted, out.reason


def test_allows_same_asset_add_same_cycle(
    storage: Storage, risk: RiskConfig, empty_account: AccountState,
    snapshot: MarketSnapshot,
) -> None:
    """Adding to the same asset (averaging into BTC short) isn't a
    correlation issue — that's per-asset cap territory, handled elsewhere."""
    _log_open_fill(storage, "T1", "BTC", "sell")
    ex = _exec(storage, risk, cycle_id="T1")
    out = ex.apply(
        "place_market_order",
        {"asset": "BTC", "side": "sell", "usd_size": 50.0},
        account=empty_account, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert out.accepted, out.reason


def test_allows_cross_cycle_same_direction(
    storage: Storage, risk: RiskConfig, snapshot: MarketSnapshot,
) -> None:
    """BTC short opened in cycle N, ETH short attempted in cycle N+1.
    The prompt rule says wait one cycle — this is what 'one cycle later'
    looks like. Should be allowed; the LLM still has to justify it but
    the code gate lifts."""
    _log_open_fill(storage, "T1", "BTC", "sell")
    account_with_btc = AccountState(
        address="0xtest", equity_usd=1000.0, free_margin_usd=1000.0,
        total_notional_usd=95.0, margin_used_usd=19.0,
        positions=[
            Position(
                asset="BTC", size=-0.00135, entry_px=70000.0,
                position_value_usd=95.0, unrealized_pnl_usd=0.0,
                leverage=5, liquidation_px=None, margin_used_usd=19.0,
            ),
        ],
        open_orders=[],
    )
    ex_next = _exec(storage, risk, cycle_id="T2")  # new cycle_id
    out = ex_next.apply(
        "place_limit_order",
        {"asset": "ETH", "side": "sell", "usd_size": 95.0, "limit_px": 2000.0},
        account=account_with_btc, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert out.accepted, out.reason


def test_reduce_only_bypasses_correlation_check(
    storage: Storage, risk: RiskConfig, empty_account: AccountState,
    snapshot: MarketSnapshot,
) -> None:
    """A reduce-only order is closing/reducing — no new correlated risk
    is being added, so the block shouldn't apply."""
    _log_open_fill(storage, "T1", "BTC", "sell")
    ex = _exec(storage, risk, cycle_id="T1")
    out = ex.apply(
        "place_limit_order",
        {
            "asset": "ETH", "side": "sell", "usd_size": 95.0,
            "limit_px": 2000.0, "reduce_only": True,
        },
        account=empty_account, snapshot=snapshot, starting_equity_usd=1000.0,
    )
    assert out.accepted, out.reason


def test_uncorrelated_asset_bypasses_block(
    storage: Storage, risk: RiskConfig, empty_account: AccountState,
) -> None:
    """If a future config adds an asset outside CORRELATED_ASSETS, it
    shouldn't be affected by the block."""
    # SOL fill exists, BTC entry attempted — neither in the correlation
    # set means... wait, BTC IS in the set. Test the inverse: SOL would
    # bypass. We can't actually trade SOL here, but we can verify the
    # check returns None when the asset being opened isn't in the set.
    _log_open_fill(storage, "T1", "BTC", "sell")
    ex = _exec(storage, risk, cycle_id="T1")
    # Direct unit test of the helper bypasses the snapshot requirement
    # for SOL (which we don't have).
    assert ex._check_correlation("SOL", "sell", reduce_only=False) is None
