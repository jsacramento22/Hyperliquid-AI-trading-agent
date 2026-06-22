"""Tests for TreePredictor — the Mode A advisor wrapper.

Covers:
- prob_up always in [0, 1]
- deterministic across repeated calls on the same snapshot
- model_version + horizon_bars surfaced from meta.json
- rolling state survives across cycles (funding history grows)
- graceful no-op when model files are missing
- predictions only emitted for the supported asset (BTC)
"""
from __future__ import annotations

import json
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from hl_agent.features import FEATURE_NAMES
from hl_agent.market_data import AssetSnapshot, Candle, MarketSnapshot
from hl_agent.tree_model import (
    TreePrediction,
    TreePredictor,
    _bucket_confidence,
    try_build_predictor,
)


# --- Helpers --------------------------------------------------------------


def _candles(
    n: int,
    *,
    start_close: float = 60_000.0,
    step: float = 5.0,
    bar_ms: int = 15 * 60 * 1000,
    base_ms: int = 1_700_000_000_000,
) -> list[Candle]:
    out = []
    px = start_close
    for i in range(n):
        out.append(
            Candle(
                open_ms=base_ms + i * bar_ms,
                close_ms=base_ms + (i + 1) * bar_ms,
                open=px,
                high=px + 20,
                low=px - 20,
                close=px + step,
                volume=1.0 + 0.001 * i,   # tiny ramp avoids std=0
            )
        )
        px += step
    return out


def _snapshot(*, ts_ms: int = 1_700_000_000_000) -> MarketSnapshot:
    # Need enough 15m bars to satisfy the longest window (96 bars for
    # 24h-window features) + the MACD warmup (100 bars internal). 200 is
    # a safe ceiling.
    c15 = _candles(200, bar_ms=15 * 60 * 1000)
    c1h = _candles(50, start_close=59_500.0, step=20.0, bar_ms=60 * 60 * 1000)
    c4h = _candles(20, start_close=59_000.0, step=80.0, bar_ms=4 * 3600 * 1000)
    asnap = AssetSnapshot(
        asset="BTC",
        mid=60_000.0,
        mark=60_010.0,
        funding_hourly=0.00005,
        open_interest=1234.5,
        day_volume_usd=1_000_000_000.0,
        sz_decimals=5,
        candles_15m=c15,
        candles_1h=c1h,
        candles_4h=c4h,
    )
    return MarketSnapshot(timestamp_ms=ts_ms, assets={"BTC": asnap})


# Fixture: build a TreePredictor against the real shipped model. Skips
# cleanly on a checkout without trained model files so CI without
# data/models stays green.
@pytest.fixture(scope="module")
def predictor() -> TreePredictor:
    repo_root = Path(__file__).resolve().parents[1]
    model_path = repo_root / "data" / "models" / "btc_tree.pkl"
    meta_path = repo_root / "data" / "models" / "btc_tree.meta.json"
    if not (model_path.exists() and meta_path.exists()):
        pytest.skip("trained model not present — run scripts/train_tree.py")
    return TreePredictor(model_path=model_path, meta_path=meta_path)


# --- Tests ---------------------------------------------------------------


def test_bucket_confidence_thresholds():
    assert _bucket_confidence(0.50) == "low"
    assert _bucket_confidence(0.51) == "low"      # delta 0.01 below medium
    assert _bucket_confidence(0.52) == "medium"   # delta 0.02 = boundary
    assert _bucket_confidence(0.54) == "medium"
    assert _bucket_confidence(0.55) == "high"     # delta 0.05 = boundary
    assert _bucket_confidence(0.95) == "high"
    # symmetric around 0.5
    assert _bucket_confidence(0.48) == "medium"
    assert _bucket_confidence(0.05) == "high"


def test_predictor_returns_btc_only(predictor):
    snap = _snapshot()
    preds = predictor.predict(snap)
    assert set(preds.keys()) == {"BTC"}


def test_prob_in_valid_range(predictor):
    preds = predictor.predict(_snapshot())
    p = preds["BTC"]
    assert 0.0 <= p.prob_up <= 1.0


def test_prediction_shape(predictor):
    p = predictor.predict(_snapshot())["BTC"]
    assert isinstance(p, TreePrediction)
    assert p.asset == "BTC"
    assert p.predicted_direction in {"up", "down"}
    assert p.confidence in {"low", "medium", "high"}
    assert p.model_version.startswith("btc_tree_iter")
    assert p.horizon_bars == 3  # Phase 1.5 R3.1


def test_deterministic_across_calls(predictor):
    snap = _snapshot()
    # Two calls with the same snapshot must produce the same probability
    # (rolling state IS mutated, but feeding the same snapshot twice in
    # a row should still hit the same lookup keys for OI 24h ago). We
    # check predict→predict on a fresh predictor instance.
    p1 = predictor.predict(snap)["BTC"].prob_up
    p2 = predictor.predict(snap)["BTC"].prob_up
    assert p1 == pytest.approx(p2, abs=1e-9)


def test_rolling_state_grows(predictor):
    snap = _snapshot()
    # Reach into the private buffer for white-box check. We accept this
    # coupling because the rolling-state behaviour IS the contract that
    # makes the cold-start NaN window finite.
    starting = len(predictor._funding.get("BTC", []))
    predictor.predict(snap)
    after_one = len(predictor._funding["BTC"])
    predictor.predict(snap)
    after_two = len(predictor._funding["BTC"])
    assert after_one == starting + 1
    assert after_two == starting + 2


def test_predictor_uses_meta_feature_names(predictor):
    # Critical invariant: the feature names in the saved meta.json must
    # be a subset of FEATURE_NAMES from features.py — otherwise build_features
    # won't return everything the model expects and predict() will KeyError.
    for name in predictor._feature_names:
        assert name in FEATURE_NAMES, (
            f"meta references unknown feature {name!r} — features.py was "
            f"renamed without retraining the model"
        )


def test_try_build_predictor_missing_returns_none(tmp_path):
    # No files in tmp_path → graceful None, not an exception
    assert try_build_predictor(model_dir=tmp_path) is None


def test_try_build_predictor_real_path():
    # Sanity: against the real shipped dir, builds (or skips if absent)
    repo_root = Path(__file__).resolve().parents[1]
    model_dir = repo_root / "data" / "models"
    if not (model_dir / "btc_tree.pkl").exists():
        pytest.skip("trained model not present")
    p = try_build_predictor(model_dir=model_dir)
    assert p is not None
    assert "BTC" in p.supported_assets
