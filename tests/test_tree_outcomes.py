"""Tests for the Phase 3 outcome-backfill logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_agent.market_data import AssetSnapshot, Candle, MarketSnapshot
from hl_agent.storage import Storage
from hl_agent.tree_outcomes import (
    _CLOSE_MATCH_TOL_MS,
    backfill_from_snapshot,
    find_realized_close,
    score_prediction,
)


# --- Pure scorer ---------------------------------------------------------


def test_score_up_correct():
    rd, ok = score_prediction(
        mid_price=100.0, predicted_direction="up", realized_close=101.0,
    )
    assert rd == "up"
    assert ok is True


def test_score_up_wrong():
    rd, ok = score_prediction(
        mid_price=100.0, predicted_direction="up", realized_close=99.0,
    )
    assert rd == "down"
    assert ok is False


def test_score_down_correct():
    rd, ok = score_prediction(
        mid_price=100.0, predicted_direction="down", realized_close=99.0,
    )
    assert rd == "down"
    assert ok is True


def test_score_equal_counts_as_down():
    # Tie-break: realized_close == mid → not strictly greater → "down".
    # A down-prediction wins, an up-prediction loses. Documented quirk;
    # ties are vanishingly rare on 15m BTC anyway.
    rd, ok_up = score_prediction(
        mid_price=100.0, predicted_direction="up", realized_close=100.0,
    )
    assert rd == "down"
    assert ok_up is False


# --- find_realized_close -------------------------------------------------


def _c(open_ms: int, close: float, bar_ms: int = 15 * 60 * 1000) -> Candle:
    return Candle(
        open_ms=open_ms,
        close_ms=open_ms + bar_ms,
        open=close, high=close, low=close, close=close, volume=1.0,
    )


def test_find_realized_close_exact_match():
    candles = [_c(i * 15 * 60 * 1000, 100 + i) for i in range(10)]
    # Target = close_ms of bar index 5
    target = candles[5].close_ms
    assert find_realized_close(candles, target) == 105.0


def test_find_realized_close_within_tolerance():
    candles = [_c(i * 15 * 60 * 1000, 100 + i) for i in range(10)]
    target = candles[5].close_ms + 60_000  # 1 minute later
    assert find_realized_close(candles, target) == 105.0


def test_find_realized_close_out_of_tolerance_returns_none():
    candles = [_c(i * 15 * 60 * 1000, 100 + i) for i in range(10)]
    # 30 min beyond the last close — well past the 8-min tolerance and
    # past the boundary of the nearest candle (the last one).
    last_close = candles[-1].close_ms
    target = last_close + 30 * 60 * 1000
    assert find_realized_close(candles, target) is None


def test_find_realized_close_empty_returns_none():
    assert find_realized_close([], 1234) is None


def test_tolerance_boundary_inclusive():
    # The tolerance check uses <=, so exactly _CLOSE_MATCH_TOL_MS apart
    # must match — this guards against a regression flipping it to <.
    c = _c(0, 100.0)
    assert find_realized_close([c], c.close_ms + _CLOSE_MATCH_TOL_MS) == 100.0


# --- backfill_from_snapshot wired against a real Storage -----------------


def _snapshot_with_history(now_ms: int, bars: int = 12) -> MarketSnapshot:
    """Build a snapshot whose candles_15m ends at `now_ms` and stretches
    `bars` × 15min backward. Prices ramp up so direction-check is
    deterministic per bar.
    """
    bar_ms = 15 * 60 * 1000
    candles_15m = []
    for i in range(bars):
        # Most recent bar last: close_ms = now_ms, open_ms = now_ms - bar_ms
        close_ms = now_ms - (bars - 1 - i) * bar_ms
        open_ms = close_ms - bar_ms
        # Price ramps up by 100 per bar — predictable for up/down scoring
        close_px = 60_000 + i * 100
        candles_15m.append(
            Candle(open_ms, close_ms, close_px, close_px, close_px, close_px, 1.0)
        )
    asnap = AssetSnapshot(
        asset="BTC",
        mid=candles_15m[-1].close,
        mark=candles_15m[-1].close,
        funding_hourly=0.0,
        open_interest=0.0,
        day_volume_usd=0.0,
        sz_decimals=5,
        candles_15m=candles_15m,
        candles_1h=[],
        candles_4h=[],
    )
    return MarketSnapshot(timestamp_ms=now_ms, assets={"BTC": asnap})


def _force_ts(storage: Storage, ts_iso: str) -> None:
    """Override ts_utc on every row — public log API stamps 'now', so to
    test horizon math we need to age the rows backward by hand."""
    import sqlite3 as _sql
    with _sql.connect(storage.path) as c:
        c.execute("UPDATE tree_predictions SET ts_utc = ?", (ts_iso,))
        c.commit()


def test_backfill_scores_pending_prediction(tmp_path):
    storage = Storage(tmp_path / "test.db")
    now = datetime.now(tz=timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    # Predict 3 bars (45min) ago, expecting UP at mid=60_500. The bar
    # sequence in the snapshot ramps up by 100/bar, so the realised close
    # 3 bars later is +300 → UP → prediction correct.
    pred_ts = now - timedelta(minutes=45)
    storage.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.55, predicted_direction="up", confidence="medium",
        model_version="v1", horizon_bars=3, mid_price=60_500.0,
    )
    _force_ts(storage, pred_ts.isoformat())

    snap = _snapshot_with_history(now_ms, bars=12)
    scored = backfill_from_snapshot(storage, snap)
    assert scored == 1

    r = storage.recent_tree_predictions(limit=1)[0]
    assert r["realized_close"] is not None
    assert r["realized_direction"] == "up"
    assert r["correct"] == 1


def test_backfill_skips_when_horizon_not_elapsed(tmp_path):
    storage = Storage(tmp_path / "test.db")
    now = datetime.now(tz=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    # Predict 10 minutes ago, horizon 3 bars (45min) → not elapsed.
    pred_ts = now - timedelta(minutes=10)
    storage.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.55, predicted_direction="up", confidence="medium",
        model_version="v1", horizon_bars=3, mid_price=60_500.0,
    )
    _force_ts(storage, pred_ts.isoformat())
    snap = _snapshot_with_history(now_ms, bars=12)
    assert backfill_from_snapshot(storage, snap) == 0
    # And the row remains unscored
    r = storage.recent_tree_predictions(limit=1)[0]
    assert r["realized_close"] is None


def test_backfill_idempotent(tmp_path):
    """Re-running backfill must not double-score already-scored rows."""
    storage = Storage(tmp_path / "test.db")
    now = datetime.now(tz=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    pred_ts = now - timedelta(minutes=45)
    storage.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.55, predicted_direction="up", confidence="medium",
        model_version="v1", horizon_bars=3, mid_price=60_500.0,
    )
    _force_ts(storage, pred_ts.isoformat())

    snap = _snapshot_with_history(now_ms, bars=12)
    assert backfill_from_snapshot(storage, snap) == 1
    # Second invocation: the now-scored prediction is excluded from
    # tree_predictions_needing_backfill, so 0 new rows.
    assert backfill_from_snapshot(storage, snap) == 0


def test_accuracy_summary_after_backfill(tmp_path):
    storage = Storage(tmp_path / "test.db")
    now = datetime.now(tz=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    pred_ts = now - timedelta(minutes=45)

    # Two predictions: one will be correct, one wrong, after backfill
    storage.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.55, predicted_direction="up", confidence="medium",
        model_version="v1", horizon_bars=3, mid_price=60_500.0,
    )
    storage.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.40, predicted_direction="down", confidence="medium",
        model_version="v1", horizon_bars=3, mid_price=60_500.0,
    )
    _force_ts(storage, pred_ts.isoformat())
    snap = _snapshot_with_history(now_ms, bars=12)
    backfill_from_snapshot(storage, snap)
    s = storage.tree_accuracy_summary(hours=24)
    assert s["scored_count"] == 2
    assert s["correct_count"] == 1     # only the UP was right
    assert s["accuracy"] == 0.5


def test_migration_idempotent(tmp_path):
    """Storage init runs the migration; running it twice (two Storage
    instantiations) must not error."""
    db = tmp_path / "test.db"
    Storage(db)
    Storage(db)  # second open re-runs migration — must be a no-op
    # And the writes still work
    s = Storage(db)
    s.log_tree_prediction(
        cycle_id="c1", asset="BTC",
        prob_up=0.5, predicted_direction="up", confidence="low",
        model_version="v1", horizon_bars=3, mid_price=60_000.0,
    )
    assert len(s.recent_tree_predictions()) == 1
