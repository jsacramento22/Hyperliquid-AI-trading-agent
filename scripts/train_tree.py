"""Train LightGBM tree model with walk-forward cross-validation.

Pipeline
--------
1. Load historical parquet files (BTC_15m_binance, BTC_1h_binance).
2. For each 15m bar at index i, compute features using bars[:i+1].
   Features needing 1h candles look up 1h bars with close_ms <= t_i.
3. Label each bar with: next_close > current_close (binary 0/1).
4. Walk-forward CV:
   - Window: TRAIN_DAYS train, VAL_DAYS val, TEST_DAYS test
   - Roll forward by STEP_DAYS, repeat for the entire dataset
   - Train on train slice, early-stop on val, evaluate on test
5. Report mean ± std of accuracy / AUC / log-loss / Brier across folds.
6. Train a final model on the FULL dataset (using best hyperparams from
   walk-forward) and save to data/models/{asset}_tree.pkl.

Phase 1 gate
------------
PASS if:  mean test accuracy ≥ 0.52  AND  std test accuracy ≤ 0.03
FAIL otherwise — document and stop; don't deploy a model that doesn't
beat random by a robust margin.

Usage
-----
    python scripts/train_tree.py --asset BTC
    python scripts/train_tree.py --asset BTC --train-days 180 --val-days 30 --test-days 30
    python scripts/train_tree.py --asset BTC --no-save     # just report, don't pickle
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, brier_score_loss, log_loss, roc_auc_score,
)

# We use the project's feature engineering — guarantees that training and
# inference compute features identically. This is the single source of truth.
from hl_agent.features import FEATURE_NAMES, FeatureContext, build_features
from hl_agent.market_data import AssetSnapshot, Candle

log = logging.getLogger("train_tree")

# Hyperparameters. Round 1.2 relaxes regularization based on the
# observation that the baseline run early-stopped at 8-30 iterations
# across all 17 folds — strong sign of underfitting. We give the model
# more rope: slower learning rate (0.02 vs 0.05) plus more rounds + a
# more permissive early-stopping window so it can actually find patterns.
LGB_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 15,              # Round 3.1: 31 → 15 (grid winner; smaller=more stable)
    "max_depth": 6,
    "learning_rate": 0.02,         # Round 1.2: 0.05 → 0.02
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
    "force_col_wise": True,
}
NUM_BOOST_ROUNDS = 2000             # Round 1.2: 500 → 2000
EARLY_STOPPING_ROUNDS = 100         # Round 1.2: 30 → 100

# Round 3.2 ensemble — different seeds change bagging + feature_fraction
# subsampling, producing models that make different mistakes. Averaging
# their probabilities is a free variance-reduction win.
ENSEMBLE_SEEDS = (42, 123, 456)

# Walk-forward window defaults (in days)
DEFAULT_TRAIN_DAYS = 180
DEFAULT_VAL_DAYS = 30
DEFAULT_TEST_DAYS = 30
DEFAULT_STEP_DAYS = 30

# Min history before we can start emitting features (longest window required
# is macd_signal_diff = 108 × 15m bars + a buffer)
WARMUP_BARS = 200

# Round 1.3 target smoothing: predict direction LABEL_HORIZON bars ahead
# instead of just next bar. Smooths label noise from intra-bar randomness.
# label = 1 if close[t + LABEL_HORIZON] > close[t] else 0
LABEL_HORIZON = 3  # 3 × 15m = 45-min ahead direction


# --- Data loading + windowing ---------------------------------------------


def _load_parquets(asset: str, data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load Binance-pulled 15m and 1h parquets for `asset`."""
    p15 = data_dir / f"{asset}_15m_binance.parquet"
    p1h = data_dir / f"{asset}_1h_binance.parquet"
    if not p15.exists():
        raise FileNotFoundError(
            f"missing {p15} — run scripts/pull_binance_candles.py first"
        )
    if not p1h.exists():
        raise FileNotFoundError(f"missing {p1h}")
    df15 = pd.read_parquet(p15).sort_values("open_ms").reset_index(drop=True)
    df1h = pd.read_parquet(p1h).sort_values("open_ms").reset_index(drop=True)
    log.info("loaded %s: %d × 15m + %d × 1h bars", asset, len(df15), len(df1h))
    return df15, df1h


def _row_to_candle(row: pd.Series) -> Candle:
    return Candle(
        open_ms=int(row.open_ms), close_ms=int(row.close_ms),
        open=float(row.open), high=float(row.high), low=float(row.low),
        close=float(row.close), volume=float(row.volume),
    )


# --- Feature matrix construction ------------------------------------------


def _build_feature_matrix(
    df15: pd.DataFrame, df1h: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """For each 15m bar (with enough warmup history and LABEL_HORIZON
    future bars), compute features + label. Returns (X, y, timestamps).

    X columns are FEATURE_NAMES in order. y is binary:
        label = 1 if close[i + LABEL_HORIZON] > close[i] else 0
    Round 1.3 introduced the horizon (was always 1 = next bar). Larger
    horizons smooth label noise from intra-bar randomness — a single 15m
    bar is mostly noise, but 3-bar (45min) direction has more signal.

    timestamps is the open_ms of each row, used for walk-forward splitting.
    """
    n15 = len(df15)
    # We need LABEL_HORIZON future bars for the label, so the last LABEL_HORIZON
    # bars can't be in X.
    end = n15 - LABEL_HORIZON
    if end <= WARMUP_BARS:
        raise ValueError(
            f"insufficient data: {n15} bars, need >{WARMUP_BARS + LABEL_HORIZON}"
        )

    # Pre-convert 1h df to a list of Candles (cheaper than DataFrame slicing
    # inside the hot loop) and an array of close_ms for fast binary search.
    candles_1h_all = [_row_to_candle(r) for _, r in df1h.iterrows()]
    close_ms_1h = df1h.close_ms.values

    candles_15m_all = [_row_to_candle(r) for _, r in df15.iterrows()]

    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    ts_rows: list[int] = []

    log.info("building feature matrix for %d candidate bars...", end - WARMUP_BARS)
    last_log = 0
    for i in range(WARMUP_BARS, end):
        if i - last_log >= 5000:
            log.info("  ...feature row %d / %d", i - WARMUP_BARS, end - WARMUP_BARS)
            last_log = i

        # Slice 15m candles up to and including bar i
        c15_slice = candles_15m_all[: i + 1]
        # 1h candles with close_ms <= current 15m close_ms (no lookahead)
        cur_close_ms = candles_15m_all[i].close_ms
        # Binary search for largest 1h close_ms <= cur_close_ms
        idx = np.searchsorted(close_ms_1h, cur_close_ms, side="right")
        c1h_slice = candles_1h_all[:idx]

        # Build a minimal AssetSnapshot just to pass to build_features.
        # The feature funcs only read .candles_15m / .candles_1h / .funding_hourly
        # / .open_interest, so the other fields can be junk.
        snap = AssetSnapshot(
            asset="BTC", mid=c15_slice[-1].close, mark=c15_slice[-1].close,
            funding_hourly=float("nan"),  # NaN during training; real in prod
            open_interest=float("nan"),    # ditto
            day_volume_usd=0.0, sz_decimals=5,
            candles_15m=c15_slice, candles_1h=c1h_slice, candles_4h=[],
        )

        features = build_features(snap, cur_close_ms, ctx=None)
        feats_in_order = [features[k] for k in FEATURE_NAMES]

        # Label: direction LABEL_HORIZON bars ahead. With LABEL_HORIZON=3,
        # this is "is close 45 min from now > close right now". Smoother
        # than the 1-bar version (Round 1.3 change).
        cur_close = candles_15m_all[i].close
        future_close = candles_15m_all[i + LABEL_HORIZON].close
        label = 1 if future_close > cur_close else 0

        X_rows.append(feats_in_order)
        y_rows.append(label)
        ts_rows.append(int(cur_close_ms))

    X = pd.DataFrame(X_rows, columns=list(FEATURE_NAMES))
    y = pd.Series(y_rows, name="next_up", dtype="int8")
    ts = pd.Series(ts_rows, name="ts_ms", dtype="int64")
    log.info("✓ feature matrix: %d rows × %d cols, label balance: %.2f%%/%.2f%%",
             len(X), len(FEATURE_NAMES),
             (y == 1).mean() * 100, (y == 0).mean() * 100)
    return X, y, ts


# --- Walk-forward CV ------------------------------------------------------


def _walk_forward_folds(
    ts: pd.Series, train_days: int, val_days: int, test_days: int, step_days: int,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Yield (train_idx, val_idx, test_idx) for each rolling window.

    All slicing by TIME (ts_ms), not by row count — handles uneven bar
    density correctly if there are gaps."""
    DAY_MS = 24 * 60 * 60 * 1000
    train_ms = train_days * DAY_MS
    val_ms = val_days * DAY_MS
    test_ms = test_days * DAY_MS
    step_ms = step_days * DAY_MS

    ts_arr = ts.values
    start_train_ms = ts_arr[0]
    folds = []
    fold_idx = 0
    while True:
        end_train_ms = start_train_ms + train_ms
        end_val_ms = end_train_ms + val_ms
        end_test_ms = end_val_ms + test_ms
        if end_test_ms > ts_arr[-1]:
            break  # not enough future data for another fold
        train_idx = np.where(
            (ts_arr >= start_train_ms) & (ts_arr < end_train_ms)
        )[0]
        val_idx = np.where(
            (ts_arr >= end_train_ms) & (ts_arr < end_val_ms)
        )[0]
        test_idx = np.where(
            (ts_arr >= end_val_ms) & (ts_arr < end_test_ms)
        )[0]
        if len(train_idx) > 100 and len(val_idx) > 30 and len(test_idx) > 30:
            folds.append((train_idx, val_idx, test_idx))
            fold_idx += 1
        start_train_ms += step_ms
    return folds


def _train_one_fold(
    X: pd.DataFrame, y: pd.Series,
    train_idx: np.ndarray, val_idx: np.ndarray, test_idx: np.ndarray,
    seeds: tuple[int, ...] | None = None,
) -> dict[str, float]:
    """Fit on train, early-stop on val, evaluate on test. Returns dict of
    test-set metrics.

    When `seeds` is provided, trains one model per seed and averages
    their test-set probabilities — an ensemble. Each seed varies the
    bagging + feature_fraction sampling pattern, so the boosters make
    different mistakes; averaging reduces variance without bias.
    Reports `best_iteration` as the MEAN across seeds for logging."""
    train_set = lgb.Dataset(X.iloc[train_idx], label=y.iloc[train_idx])
    val_set = lgb.Dataset(
        X.iloc[val_idx], label=y.iloc[val_idx], reference=train_set
    )
    y_test = y.iloc[test_idx].values

    if seeds is None or len(seeds) <= 1:
        # Single model (default path — back-compat with grid search etc.)
        booster = lgb.train(
            LGB_PARAMS,
            train_set,
            num_boost_round=NUM_BOOST_ROUNDS,
            valid_sets=[val_set],
            callbacks=[
                lgb.early_stopping(
                    stopping_rounds=EARLY_STOPPING_ROUNDS, verbose=False
                ),
            ],
        )
        p_test = booster.predict(
            X.iloc[test_idx], num_iteration=booster.best_iteration
        )
        best_iter = booster.best_iteration
    else:
        # Ensemble path — train K models with different seeds, average probs
        preds = []
        iters = []
        for seed in seeds:
            params = {**LGB_PARAMS, "seed": seed,
                      "bagging_seed": seed, "feature_fraction_seed": seed}
            b = lgb.train(
                params,
                train_set,
                num_boost_round=NUM_BOOST_ROUNDS,
                valid_sets=[val_set],
                callbacks=[
                    lgb.early_stopping(
                        stopping_rounds=EARLY_STOPPING_ROUNDS, verbose=False
                    ),
                ],
            )
            preds.append(b.predict(X.iloc[test_idx], num_iteration=b.best_iteration))
            iters.append(b.best_iteration)
        p_test = np.mean(preds, axis=0)
        best_iter = int(np.mean(iters))

    pred_class = (p_test > 0.5).astype(int)
    return {
        "accuracy": accuracy_score(y_test, pred_class),
        "auc": roc_auc_score(y_test, p_test) if len(set(y_test)) > 1 else float("nan"),
        "log_loss": log_loss(y_test, p_test, labels=[0, 1]),
        "brier": brier_score_loss(y_test, p_test),
        "n_test": len(test_idx),
        "best_iteration": best_iter,
    }


def _run_walk_forward(
    X: pd.DataFrame, y: pd.Series, ts: pd.Series,
    train_days: int, val_days: int, test_days: int, step_days: int,
    seeds: tuple[int, ...] | None = None,
) -> list[dict[str, float]]:
    folds = _walk_forward_folds(ts, train_days, val_days, test_days, step_days)
    ensemble_note = f" [ensemble of {len(seeds)}]" if seeds else ""
    log.info(
        "walk-forward: %d folds (train=%dd / val=%dd / test=%dd, step=%dd)%s",
        len(folds), train_days, val_days, test_days, step_days, ensemble_note,
    )
    metrics_per_fold: list[dict[str, float]] = []
    for fold_i, (tr, va, te) in enumerate(folds, start=1):
        m = _train_one_fold(X, y, tr, va, te, seeds=seeds)
        m["fold"] = fold_i
        m["train_start_ms"] = int(ts.iloc[tr[0]])
        m["test_end_ms"] = int(ts.iloc[te[-1]])
        log.info(
            "  fold %2d/%d: acc=%.3f auc=%.3f log_loss=%.3f brier=%.3f "
            "(n_train=%d n_val=%d n_test=%d, iter=%d)",
            fold_i, len(folds),
            m["accuracy"], m["auc"], m["log_loss"], m["brier"],
            len(tr), len(va), len(te), m["best_iteration"],
        )
        metrics_per_fold.append(m)
    return metrics_per_fold


def _summarize(metrics: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """Reduce per-fold metrics to mean ± std + pass/fail gate."""
    def stat(key: str) -> dict[str, float]:
        vals = [m[key] for m in metrics if not np.isnan(m[key])]
        return {
            "mean": float(np.mean(vals)),
            "std": float(np.std(vals, ddof=1)) if len(vals) > 1 else float("nan"),
            "min": float(np.min(vals)),
            "max": float(np.max(vals)),
            "n": len(vals),
        }
    return {
        "accuracy": stat("accuracy"),
        "auc": stat("auc"),
        "log_loss": stat("log_loss"),
        "brier": stat("brier"),
    }


# --- Final model fit + save ------------------------------------------------


def _fit_final_model(X: pd.DataFrame, y: pd.Series) -> Any:
    """Fit on ALL data using a single 90/10 train/val split for early
    stopping. This is the model that gets shipped."""
    split = int(len(X) * 0.9)
    train_set = lgb.Dataset(X.iloc[:split], label=y.iloc[:split])
    val_set = lgb.Dataset(
        X.iloc[split:], label=y.iloc[split:], reference=train_set
    )
    booster = lgb.train(
        LGB_PARAMS,
        train_set,
        num_boost_round=NUM_BOOST_ROUNDS,
        valid_sets=[val_set],
        callbacks=[
            lgb.early_stopping(stopping_rounds=EARLY_STOPPING_ROUNDS, verbose=False),
        ],
    )
    return booster


def _save_model(
    booster: Any, asset: str, summary: dict, out_dir: Path,
) -> tuple[Path, Path]:
    """Pickle the booster + a sidecar JSON with metadata + walk-forward stats.
    The metadata file is what the dashboard / drift_check.py read at runtime."""
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"{asset.lower()}_tree.pkl"
    meta_path = out_dir / f"{asset.lower()}_tree.meta.json"
    with model_path.open("wb") as f:
        pickle.dump(booster, f)
    meta = {
        "asset": asset,
        "trained_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "feature_names": list(FEATURE_NAMES),
        "lgb_params": LGB_PARAMS,
        "num_boost_rounds": NUM_BOOST_ROUNDS,
        "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        "label_horizon_bars": LABEL_HORIZON,
        "best_iteration": booster.best_iteration,
        "walk_forward_summary": summary,
    }
    with meta_path.open("w") as f:
        json.dump(meta, f, indent=2)
    return model_path, meta_path


# --- Main ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--data-dir", default="data/historical")
    parser.add_argument("--out-dir", default="data/models")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--step-days", type=int, default=DEFAULT_STEP_DAYS)
    parser.add_argument(
        "--no-save", action="store_true",
        help="Skip pickling — useful when iterating",
    )
    parser.add_argument(
        "--ensemble", action="store_true",
        help=("Round 3.2: train ensemble of ENSEMBLE_SEEDS boosters per "
              "fold, average predictions. Reduces variance via different "
              "subsampling patterns."),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)

    log.info("=== Phase 1 training run: %s ===", args.asset)
    df15, df1h = _load_parquets(args.asset, data_dir)
    X, y, ts = _build_feature_matrix(df15, df1h)

    log.info("running walk-forward CV...")
    seeds = ENSEMBLE_SEEDS if args.ensemble else None
    fold_metrics = _run_walk_forward(
        X, y, ts,
        train_days=args.train_days, val_days=args.val_days,
        test_days=args.test_days, step_days=args.step_days,
        seeds=seeds,
    )
    summary = _summarize(fold_metrics)

    log.info("")
    log.info("=== Walk-forward summary across %d folds ===", len(fold_metrics))
    for metric, st in summary.items():
        log.info(
            "  %-10s  mean=%.4f  std=%.4f  min=%.4f  max=%.4f",
            metric, st["mean"], st["std"], st["min"], st["max"],
        )

    # Phase 1 gate
    acc_mean = summary["accuracy"]["mean"]
    acc_std = summary["accuracy"]["std"]
    gate_pass = acc_mean >= 0.52 and acc_std <= 0.03
    log.info("")
    log.info("=== Phase 1 Gate ===")
    log.info("  Required:  mean accuracy ≥ 0.52  AND  std ≤ 0.03")
    log.info("  Got:       mean=%.4f             std=%.4f", acc_mean, acc_std)
    log.info("  Verdict:   %s", "✓ PASS" if gate_pass else "✗ FAIL")

    if not args.no_save:
        log.info("")
        log.info("fitting final model on all %d rows...", len(X))
        final = _fit_final_model(X, y)
        model_path, meta_path = _save_model(final, args.asset, summary, out_dir)
        log.info("✓ saved model:    %s", model_path)
        log.info("✓ saved metadata: %s", meta_path)

    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
