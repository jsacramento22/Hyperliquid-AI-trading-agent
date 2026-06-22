"""Backfill outcomes for tree_predictions.

A prediction logged at time t with horizon H bars (each 15min) is
scored against the 15m candle whose close lands at approximately
t + H × 15min. We compare the realised close to the mid_price recorded
at prediction time:
    realized_direction = "up" if realized_close > mid_price else "down"
    correct = (realized_direction == predicted_direction)

Runs every cycle right after the snapshot fetch — the snapshot's own
candles_15m (typically the last 12 bars = 3 hours) carry enough history
to score any prediction made in the last few cycles. Predictions older
than that fall outside the snapshot window and are left unscored;
storage.tree_predictions_needing_backfill caps the search at 24h so the
backlog stays bounded.

This module is intentionally framework-free — no Storage, no HL client —
so it can be unit-tested with synthetic inputs. The wrapper at the
bottom plumbs Storage + MarketSnapshot in.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from .market_data import Candle, MarketSnapshot
from .storage import Storage

log = logging.getLogger("hl_agent.tree_outcomes")

# How far the candle's close_ms is allowed to drift from the prediction's
# target time. Cycles run a few seconds into the bar so the worst case
# is a sub-minute drift; 8 minutes (about half a bar) gives plenty of
# safety without grabbing the wrong bar.
_CLOSE_MATCH_TOL_MS = 8 * 60 * 1000

_BAR_15M_MS = 15 * 60 * 1000


def _parse_iso_to_ms(ts_iso: str) -> int:
    # ts_utc rows are written via datetime.now(tz=utc).isoformat(), so the
    # string always carries an explicit offset — no naive-datetime case.
    return int(datetime.fromisoformat(ts_iso).timestamp() * 1000)


def find_realized_close(
    candles_15m: Iterable[Candle], target_ms: int,
) -> float | None:
    """Return the close price of the candle whose close_ms is closest to
    target_ms, or None when no candle in the input is within tolerance."""
    candles = list(candles_15m)
    if not candles:
        return None
    closest = min(candles, key=lambda c: abs(c.close_ms - target_ms))
    if abs(closest.close_ms - target_ms) <= _CLOSE_MATCH_TOL_MS:
        return float(closest.close)
    return None


def score_prediction(
    *,
    mid_price: float,
    predicted_direction: str,
    realized_close: float,
) -> tuple[str, bool]:
    """Pure scoring logic. Returns (realized_direction, correct)."""
    realized_direction = "up" if realized_close > mid_price else "down"
    return realized_direction, realized_direction == predicted_direction


def backfill_from_snapshot(
    storage: Storage, snapshot: MarketSnapshot,
) -> int:
    """Score every pending prediction whose target time falls inside the
    snapshot's 15m candle history. Returns count of newly scored rows.
    Per-row failures are logged but never raised — the call site treats
    backfill as best-effort."""
    pending = storage.tree_predictions_needing_backfill()
    if not pending:
        return 0

    # One candle list per asset, looked up by asset symbol. Predictions
    # for an asset not in the current snapshot are skipped (the bot may
    # have dropped an asset from config between predict and score).
    by_asset = {
        asset: snap.candles_15m for asset, snap in snapshot.assets.items()
    }

    scored = 0
    for row in pending:
        asset = row["asset"]
        candles = by_asset.get(asset)
        if not candles:
            continue
        try:
            pred_ts_ms = _parse_iso_to_ms(row["ts_utc"])
            target_ms = pred_ts_ms + int(row["horizon_bars"]) * _BAR_15M_MS
            # Guard: target_ms must be in the past relative to the snapshot,
            # else the horizon hasn't elapsed yet and we'd score against
            # an in-progress bar.
            if target_ms > snapshot.timestamp_ms:
                continue
            realized_close = find_realized_close(candles, target_ms)
            if realized_close is None:
                continue
            realized_dir, correct = score_prediction(
                mid_price=float(row["mid_price"]),
                predicted_direction=row["predicted_direction"],
                realized_close=realized_close,
            )
            storage.update_tree_outcome(
                prediction_id=int(row["id"]),
                realized_close=realized_close,
                realized_direction=realized_dir,
                correct=correct,
            )
            scored += 1
        except Exception:
            log.exception("failed to score prediction id=%s", row.get("id"))

    if scored:
        log.info("tree_outcomes: scored %d prediction(s)", scored)
    return scored
