from __future__ import annotations

import json

from hl_agent.trades import compute_trades


def _fill(
    fid: int,
    asset: str,
    side: str,
    size: float,
    px: float,
    ts: str,
    requested_usd: float | None = None,
) -> dict:
    return {
        "id": fid,
        "ts_utc": ts,
        "asset": asset,
        "side": side,
        "requested_usd": requested_usd,
        "raw_response": json.dumps(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": str(size), "avgPx": str(px), "oid": fid}}
                        ]
                    },
                },
            }
        ),
    }


def test_simple_long_round_trip():
    fills = [
        _fill(1, "BTC", "buy", 0.001, 60_000, "2026-05-06T10:00:00+00:00"),
        _fill(2, "BTC", "close", 0.001, 61_000, "2026-05-06T11:00:00+00:00"),
    ]
    [t] = compute_trades(fills)
    assert t.asset == "BTC"
    assert t.side == "long"
    assert t.size == 0.001
    assert t.avg_entry_px == 60_000
    assert t.exit_px == 61_000
    # PnL: 0.001 * (61000 - 60000) = 1
    assert t.realized_pnl_usd == 1.0
    assert t.duration_seconds == 3600


def test_simple_short_round_trip():
    fills = [
        _fill(1, "ETH", "sell", 0.1, 2_400, "2026-05-06T10:00:00+00:00"),
        _fill(2, "ETH", "close", 0.1, 2_350, "2026-05-06T11:00:00+00:00"),
    ]
    [t] = compute_trades(fills)
    assert t.side == "short"
    # short PnL: 0.1 * (2400 - 2350) = 5
    assert t.realized_pnl_usd == 5.0


def test_averaged_long_with_two_opens():
    fills = [
        _fill(1, "BTC", "buy", 0.001, 60_000, "2026-05-06T10:00:00+00:00"),
        _fill(2, "BTC", "buy", 0.0005, 62_000, "2026-05-06T10:30:00+00:00"),
        _fill(3, "BTC", "close", 0.0015, 63_000, "2026-05-06T11:00:00+00:00"),
    ]
    [t] = compute_trades(fills)
    # Weighted avg: (0.001*60000 + 0.0005*62000) / 0.0015 = 60667 (rounded)
    assert abs(t.avg_entry_px - 60_666.666_666_666_67) < 0.01
    # PnL: 0.0015 * (63000 - 60667) = 0.0015 * 2333 = ~3.50
    assert abs(t.realized_pnl_usd - 3.5) < 0.01
    assert t.fill_count == 3


def test_two_assets_independent():
    fills = [
        _fill(1, "BTC", "buy", 0.001, 60_000, "2026-05-06T10:00:00+00:00"),
        _fill(2, "ETH", "sell", 0.1, 2_400, "2026-05-06T10:05:00+00:00"),
        _fill(3, "BTC", "close", 0.001, 61_000, "2026-05-06T11:00:00+00:00"),
        _fill(4, "ETH", "close", 0.1, 2_350, "2026-05-06T11:05:00+00:00"),
    ]
    trades = compute_trades(fills)
    assert len(trades) == 2
    assert {t.asset for t in trades} == {"BTC", "ETH"}


def test_sorted_newest_first():
    fills = [
        _fill(1, "BTC", "buy", 0.001, 60_000, "2026-05-06T10:00:00+00:00"),
        _fill(2, "BTC", "close", 0.001, 60_500, "2026-05-06T11:00:00+00:00"),
        _fill(3, "BTC", "buy", 0.001, 61_000, "2026-05-06T12:00:00+00:00"),
        _fill(4, "BTC", "close", 0.001, 62_000, "2026-05-06T13:00:00+00:00"),
    ]
    trades = compute_trades(fills)
    assert len(trades) == 2
    # Newest first
    assert trades[0].close_ts_utc == "2026-05-06T13:00:00+00:00"
    assert trades[1].close_ts_utc == "2026-05-06T11:00:00+00:00"


def test_close_without_open_ignored():
    fills = [
        _fill(1, "ETH", "close", 0.05, 2_400, "2026-05-06T10:00:00+00:00"),
    ]
    assert compute_trades(fills) == []


def test_unparseable_response_skipped():
    fills = [
        {
            "id": 1,
            "ts_utc": "2026-05-06T10:00:00+00:00",
            "asset": "BTC",
            "side": "buy",
            "requested_usd": 100,
            "raw_response": json.dumps({"status": "ok", "response": {}}),
        },
        _fill(2, "BTC", "close", 0.001, 60_000, "2026-05-06T11:00:00+00:00"),
    ]
    # First fill unparseable, close hits empty stack, no trade
    assert compute_trades(fills) == []


def _error_fill(fid: int, asset: str, side: str, ts: str) -> dict:
    """Hyperliquid response shape for a rejected order — no `filled` key."""
    return {
        "id": fid,
        "ts_utc": ts,
        "asset": asset,
        "side": side,
        "requested_usd": None,
        "raw_response": json.dumps(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [{"error": "Price too far from oracle asset=4"}]
                    },
                },
            }
        ),
    }


def test_partial_close_short_two_parts():
    """Real bug: ETH short 0.0833 closed in two partial fills."""
    fills = [
        _fill(1, "ETH", "sell", 0.0833, 2389.95, "2026-05-07T11:19:30+00:00"),
        _fill(2, "ETH", "close", 0.0572, 2399.94, "2026-05-07T11:34:28+00:00"),
        _fill(3, "ETH", "close", 0.0261, 2388.13, "2026-05-07T12:19:32+00:00"),
    ]
    [t] = compute_trades(fills)
    assert t.side == "short"
    assert t.fill_count == 3
    # Weighted avg exit: (0.0572*2399.94 + 0.0261*2388.13) / 0.0833
    expected_avg_exit = (0.0572 * 2399.94 + 0.0261 * 2388.13) / 0.0833
    assert abs(t.exit_px - expected_avg_exit) < 0.01
    # Short PnL: total_size * (entry - avg_exit)
    expected_pnl = 0.0833 * (2389.95 - expected_avg_exit)
    assert abs(t.realized_pnl_usd - expected_pnl) < 0.01
    # close ts is the LAST close
    assert t.close_ts_utc == "2026-05-07T12:19:32+00:00"


def test_partial_close_long_two_parts():
    fills = [
        _fill(1, "BTC", "buy", 0.002, 80_000, "2026-05-08T10:00:00+00:00"),
        _fill(2, "BTC", "close", 0.0005, 81_000, "2026-05-08T11:00:00+00:00"),
        _fill(3, "BTC", "close", 0.0015, 82_000, "2026-05-08T12:00:00+00:00"),
    ]
    [t] = compute_trades(fills)
    assert t.side == "long"
    expected_avg_exit = (0.0005 * 81_000 + 0.0015 * 82_000) / 0.002
    assert abs(t.exit_px - expected_avg_exit) < 0.01
    expected_pnl = 0.002 * (expected_avg_exit - 80_000)
    assert abs(t.realized_pnl_usd - expected_pnl) < 0.01


def test_partial_close_with_intermediate_error_fill():
    """ETH 5/7 13:19 case from real log: open -> partial close -> rejected
    close (error) -> final close. The error fill must be skipped."""
    fills = [
        _fill(1, "ETH", "sell", 0.0833, 2389.95, "2026-05-07T11:19:30+00:00"),
        _fill(2, "ETH", "close", 0.0572, 2399.94, "2026-05-07T11:34:28+00:00"),
        _error_fill(3, "ETH", "close", "2026-05-07T11:49:35+00:00"),
        _fill(4, "ETH", "close", 0.0261, 2388.13, "2026-05-07T12:19:32+00:00"),
    ]
    trades = compute_trades(fills)
    assert len(trades) == 1
    assert trades[0].fill_count == 3  # error fill not counted
    assert trades[0].close_ts_utc == "2026-05-07T12:19:32+00:00"


def test_two_independent_round_trips_same_asset():
    fills = [
        _fill(1, "BTC", "buy", 0.001, 60_000, "2026-05-06T10:00:00+00:00"),
        _fill(2, "BTC", "close", 0.001, 61_000, "2026-05-06T11:00:00+00:00"),
        _fill(3, "BTC", "buy", 0.002, 62_000, "2026-05-06T12:00:00+00:00"),
        _fill(4, "BTC", "close", 0.002, 63_000, "2026-05-06T13:00:00+00:00"),
    ]
    trades = compute_trades(fills)
    assert len(trades) == 2


def test_unfinished_position_excluded():
    """Open without close should not produce a trade row."""
    fills = [
        _fill(1, "ETH", "sell", 0.05, 2_400, "2026-05-06T10:00:00+00:00"),
    ]
    assert compute_trades(fills) == []


def test_opposite_fill_closes_position_implicitly():
    """If a sell fill arrives with the long position still open and exactly
    matches the open size, treat it as the close. (The bot uses close_position
    rather than flips, but defensive handling ensures we don't get stuck.)"""
    fills = [
        _fill(1, "ETH", "buy", 0.05, 2_400, "2026-05-06T10:00:00+00:00"),
        _fill(2, "ETH", "sell", 0.05, 2_410, "2026-05-06T11:00:00+00:00"),
    ]
    [t] = compute_trades(fills)
    assert t.side == "long"
    assert abs(t.realized_pnl_usd - 0.50) < 1e-6  # 0.05 * (2410 - 2400)


# -- trades_from_user_fills (exchange-sourced reconstruction) --------------

from hl_agent.trades import trades_from_user_fills


def _ufill(
    coin: str,
    dir_: str,
    sz: float,
    px: float,
    ts_ms: int,
    closed_pnl: float = 0.0,
) -> dict:
    """Mirror Hyperliquid's user_fills row shape."""
    return {
        "coin": coin,
        "dir": dir_,
        "sz": str(sz),
        "px": str(px),
        "closedPnl": str(closed_pnl),
        "time": ts_ms,
        "side": "B" if "Long" in dir_ and dir_.startswith("Open") else "A",
        "oid": ts_ms,
        "hash": "0x0",
        "crossed": False,
        "startPosition": "0",
    }


def test_ufills_simple_long():
    fills = [
        _ufill("ETH", "Open Long", 0.05, 2_400, 1_700_000_000_000),
        _ufill("ETH", "Close Long", 0.05, 2_450, 1_700_001_000_000, closed_pnl=2.5),
    ]
    [t] = trades_from_user_fills(fills)
    assert t.asset == "ETH"
    assert t.side == "long"
    assert t.size == 0.05
    assert t.avg_entry_px == 2_400
    assert t.exit_px == 2_450
    assert t.realized_pnl_usd == 2.5  # from exchange's closedPnl, not recomputed
    assert t.fill_count == 2
    assert t.duration_seconds == 1_000


def test_ufills_orphan_close_creates_trade():
    """The bug today's trade hit: limit order filled on exchange but we have
    no Open record for it (because our local fills table stops at "resting").
    With user_fills as source of truth, the Close fill alone produces a trade
    rather than being silently dropped."""
    fills = [
        # Only a Close — Open happened before query window / was an unobserved fill.
        _ufill("ETH", "Close Long", 0.0451, 2_093.4, 1_700_100_000_000, closed_pnl=1.20),
    ]
    [t] = trades_from_user_fills(fills)
    assert t.asset == "ETH"
    assert t.side == "long"
    assert t.size == 0.0451
    assert t.realized_pnl_usd == 1.20
    # Entry back-derived from closedPnl: 2093.4 - 1.20/0.0451 ≈ 2066.79
    assert abs(t.avg_entry_px - (2_093.4 - 1.20 / 0.0451)) < 1e-6
    assert t.exit_px == 2_093.4


def test_ufills_partial_closes_aggregate():
    fills = [
        _ufill("BTC", "Open Short", 0.002, 70_000, 1_700_000_000_000),
        _ufill("BTC", "Close Short", 0.0012, 69_500, 1_700_000_500_000, closed_pnl=0.60),
        _ufill("BTC", "Close Short", 0.0008, 69_400, 1_700_000_700_000, closed_pnl=0.48),
    ]
    [t] = trades_from_user_fills(fills)
    assert t.side == "short"
    assert abs(t.size - 0.002) < 1e-9
    assert t.fill_count == 3
    assert abs(t.realized_pnl_usd - 1.08) < 1e-9  # exchange PnL summed
    # Avg exit = (0.0012*69500 + 0.0008*69400) / 0.002 = 69460
    assert abs(t.exit_px - 69_460.0) < 1e-6


def test_ufills_averaging_in_then_close():
    fills = [
        _ufill("ETH", "Open Long", 0.03, 2_400, 1_700_000_000_000),
        _ufill("ETH", "Open Long", 0.02, 2_440, 1_700_000_500_000),  # add to position
        _ufill("ETH", "Close Long", 0.05, 2_500, 1_700_000_900_000, closed_pnl=4.20),
    ]
    [t] = trades_from_user_fills(fills)
    assert t.side == "long"
    # Avg entry = (0.03*2400 + 0.02*2440) / 0.05 = 2416
    assert abs(t.avg_entry_px - 2_416.0) < 1e-6
    assert abs(t.realized_pnl_usd - 4.20) < 1e-9


def test_ufills_two_sequential_round_trips_same_coin():
    fills = [
        _ufill("ETH", "Open Long", 0.04, 2_400, 1_700_000_000_000),
        _ufill("ETH", "Close Long", 0.04, 2_420, 1_700_000_300_000, closed_pnl=0.80),
        _ufill("ETH", "Open Short", 0.05, 2_430, 1_700_000_600_000),
        _ufill("ETH", "Close Short", 0.05, 2_410, 1_700_000_900_000, closed_pnl=1.00),
    ]
    trades = trades_from_user_fills(fills)
    assert len(trades) == 2
    # Sorted newest-first
    assert trades[0].side == "short"
    assert trades[1].side == "long"
    assert trades[0].realized_pnl_usd == 1.00
    assert trades[1].realized_pnl_usd == 0.80


def test_ufills_still_open_position_excluded():
    """A position with no close yet shouldn't produce a Trade."""
    fills = [
        _ufill("ETH", "Open Long", 0.05, 2_400, 1_700_000_000_000),
    ]
    assert trades_from_user_fills(fills) == []


def test_ufills_unrecognized_row_skipped():
    fills = [
        {"this": "is not a fill"},
        _ufill("ETH", "Open Long", 0.05, 2_400, 1_700_000_000_000),
        _ufill("ETH", "Close Long", 0.05, 2_450, 1_700_001_000_000, closed_pnl=2.5),
    ]
    trades = trades_from_user_fills(fills)
    assert len(trades) == 1


def test_ufills_empty_input():
    assert trades_from_user_fills([]) == []


def test_ufills_open_fees_included_in_realized_pnl():
    """Hyperliquid reports the entry fee as a small negative closedPnl on
    Open rows (no position is closed, but the fee shows up here for account
    equity accounting). We must sum closedPnl across both opens and closes
    to capture the true net-of-fees PnL — otherwise the trade looks
    systematically better than it actually is by a few cents."""
    # Mirrors today's actual ETH trade: two opens (limit fills, tiny open
    # fees) + one close. Hyperliquid showed -0.01 + -0.00 + -0.70 = -0.71.
    fills = [
        _ufill("ETH", "Open Long", 0.0338, 2_108.0, 1_700_000_000_000, closed_pnl=-0.01),
        _ufill("ETH", "Open Long", 0.0113, 2_108.0, 1_700_000_001_000, closed_pnl=-0.00),
        _ufill("ETH", "Close Long", 0.0451, 2_093.4, 1_700_100_000_000, closed_pnl=-0.70),
    ]
    [t] = trades_from_user_fills(fills)
    assert t.side == "long"
    assert abs(t.size - 0.0451) < 1e-9
    assert abs(t.realized_pnl_usd - (-0.71)) < 1e-9  # net of all fees
    assert t.fill_count == 3
