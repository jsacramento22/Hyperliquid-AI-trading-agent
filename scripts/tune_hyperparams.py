"""LightGBM hyperparameter grid search for the BTC tree model.

Builds the feature matrix once, then runs walk-forward CV for every
combination in the grid. Reports the top 5 by mean AUC (more robust
than accuracy under thin class imbalance) so we can pick the right
LGB_PARAMS to ship in scripts/train_tree.py.

Grid (54 combos):
    num_leaves        ∈ {15, 31, 63}
    max_depth         ∈ {4, 6, 8}
    min_data_in_leaf  ∈ {20, 50, 100}
    learning_rate     ∈ {0.02, 0.05}

Other params (bagging fractions, num_boost_rounds, early_stopping) are
held at the R1.2 values from train_tree.py so the comparison is apples-
to-apples vs the R2 baseline (0.5212/0.0094).

Wall clock: ~10-15 min on a laptop (54 × ~17 folds × ~1s/fold).

Usage:
    python scripts/tune_hyperparams.py --asset BTC
    python scripts/tune_hyperparams.py --asset BTC --top 10
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
import time
from pathlib import Path

import numpy as np

# Reuse train_tree's plumbing — same feature matrix, same walk-forward folds
sys.path.insert(0, str(Path(__file__).parent))
from train_tree import (  # noqa: E402
    EARLY_STOPPING_ROUNDS,
    LGB_PARAMS,
    NUM_BOOST_ROUNDS,
    _build_feature_matrix,
    _load_parquets,
    _summarize,
    _train_one_fold,
    _walk_forward_folds,
)

log = logging.getLogger("tune_hyperparams")


GRID = {
    "num_leaves": [15, 31, 63],
    "max_depth": [4, 6, 8],
    "min_data_in_leaf": [20, 50, 100],
    "learning_rate": [0.02, 0.05],
}


def _run_one_config(
    X, y, ts, folds, base_params: dict, override: dict,
) -> dict[str, float]:
    """Train walk-forward with this hyperparam combo, return summary."""
    params = {**base_params, **override}
    # Temporarily swap module-level LGB_PARAMS so _train_one_fold uses
    # the override. _train_one_fold reads the global, so we monkey-patch.
    import train_tree as tt
    saved = tt.LGB_PARAMS
    tt.LGB_PARAMS = params
    try:
        per_fold = []
        for tr, va, te in folds:
            per_fold.append(_train_one_fold(X, y, tr, va, te))
    finally:
        tt.LGB_PARAMS = saved
    s = _summarize(per_fold)
    return {
        "acc_mean": s["accuracy"]["mean"],
        "acc_std": s["accuracy"]["std"],
        "auc_mean": s["auc"]["mean"],
        "auc_std": s["auc"]["std"],
        "log_loss_mean": s["log_loss"]["mean"],
        "n_folds": s["accuracy"]["n"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--data-dir", default="data/historical")
    parser.add_argument("--top", type=int, default=10,
                        help="How many top combos to print")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--val-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    data_dir = Path(args.data_dir)
    df15, df1h = _load_parquets(args.asset, data_dir)
    X, y, ts = _build_feature_matrix(df15, df1h)

    folds = _walk_forward_folds(
        ts, args.train_days, args.val_days, args.test_days, args.step_days,
    )
    log.info("walk-forward folds: %d", len(folds))

    # Generate combos
    keys = list(GRID.keys())
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    log.info("grid: %d combos × %d folds = %d total trainings",
             len(combos), len(folds), len(combos) * len(folds))

    results: list[dict] = []
    start = time.time()
    for i, combo in enumerate(combos, 1):
        override = dict(zip(keys, combo))
        r = _run_one_config(X, y, ts, folds, LGB_PARAMS, override)
        r["params"] = override
        results.append(r)
        elapsed = time.time() - start
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(combos) - i) / rate if rate > 0 else 0
        log.info(
            "  %2d/%d: %s  acc=%.4f±%.4f  auc=%.4f±%.4f   [%.0fs elapsed, %.0fs ETA]",
            i, len(combos),
            ", ".join(f"{k}={v}" for k, v in override.items()),
            r["acc_mean"], r["acc_std"], r["auc_mean"], r["auc_std"],
            elapsed, eta,
        )

    # Sort by mean AUC descending
    results.sort(key=lambda r: r["auc_mean"], reverse=True)

    log.info("")
    log.info("=== Top %d configs by mean AUC ===", args.top)
    header = f"{'rank':>4}  {'auc':>14}  {'accuracy':>14}  {'params':<60}"
    log.info(header)
    log.info("-" * len(header))
    for i, r in enumerate(results[: args.top], 1):
        params_str = ", ".join(f"{k}={v}" for k, v in r["params"].items())
        log.info(
            "  %2d  %.4f±%.4f  %.4f±%.4f   %s",
            i,
            r["auc_mean"], r["auc_std"],
            r["acc_mean"], r["acc_std"],
            params_str,
        )

    best = results[0]
    log.info("")
    log.info("=== Winner ===")
    log.info("  params:    %s", best["params"])
    log.info("  acc:       %.4f ± %.4f", best["acc_mean"], best["acc_std"])
    log.info("  auc:       %.4f ± %.4f", best["auc_mean"], best["auc_std"])
    log.info("  log_loss:  %.4f", best["log_loss_mean"])
    log.info("")
    log.info("Compared to R2 baseline (acc=0.5212±0.0094, auc=0.5304±0.0110):")
    log.info("  Δ acc:  %+.4f  (%s)",
             best["acc_mean"] - 0.5212,
             "improvement" if best["acc_mean"] > 0.5212 else "no improvement")
    log.info("  Δ auc:  %+.4f  (%s)",
             best["auc_mean"] - 0.5304,
             "improvement" if best["auc_mean"] > 0.5304 else "no improvement")

    return 0


if __name__ == "__main__":
    sys.exit(main())
