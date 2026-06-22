"""Print LightGBM feature importance for a saved tree model.

Importance is reported using LightGBM's "gain" metric — the total
reduction in loss attributable to splits on each feature. Higher gain
means the feature is doing more predictive work; near-zero gain means
the model effectively ignores the feature and we could drop it.

Use this output to inform Round 2 of Phase 1.5: which features to vary,
augment, or remove.

Usage:
    python scripts/inspect_feature_importance.py
    python scripts/inspect_feature_importance.py --model data/models/btc_tree.pkl
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default="data/models/btc_tree.pkl",
        help="Path to the pickled LightGBM booster",
    )
    parser.add_argument(
        "--meta", default=None,
        help="Path to the .meta.json sidecar (auto-derived if not set)",
    )
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"ERROR: {model_path} not found — train a model first", file=sys.stderr)
        return 1

    with model_path.open("rb") as f:
        booster = pickle.load(f)

    meta_path = Path(args.meta) if args.meta else model_path.with_suffix(".meta.json")
    meta = {}
    if meta_path.exists():
        with meta_path.open() as f:
            meta = json.load(f)

    feature_names = booster.feature_name()
    gain = booster.feature_importance(importance_type="gain")
    split = booster.feature_importance(importance_type="split")
    total_gain = sum(gain) or 1.0
    total_split = sum(split) or 1.0

    rows = list(zip(feature_names, gain, split))
    rows.sort(key=lambda r: r[1], reverse=True)

    print(f"Model:           {model_path}")
    if meta:
        print(f"Trained at:      {meta.get('trained_at_utc', '?')}")
        print(f"Best iteration:  {meta.get('best_iteration', '?')}")
        wf = meta.get("walk_forward_summary", {})
        if wf:
            acc = wf.get("accuracy", {})
            print(
                f"Walk-forward:    acc mean={acc.get('mean'):.4f}  "
                f"std={acc.get('std'):.4f}  n_folds={acc.get('n')}"
            )
    print(f"Total trees:     {booster.num_trees()}")
    print(f"Features:        {len(feature_names)}")
    print()

    # Identify thresholds
    dead = [r for r in rows if r[1] / total_gain < 0.005]  # <0.5% of total gain
    dominant = [r for r in rows if r[1] / total_gain > 0.10]  # >10%

    header = f"{'rank':>4}  {'feature':<28}  {'gain':>10}  {'gain%':>6}  {'split':>6}  {'split%':>6}"
    print(header)
    print("-" * len(header))
    for i, (name, g, s) in enumerate(rows, 1):
        flag = ""
        if g / total_gain < 0.005:
            flag = "  ← dead"
        elif g / total_gain > 0.10:
            flag = "  ← dominant"
        print(
            f"{i:>4}  {name:<28}  {g:>10.0f}  {g/total_gain*100:>5.1f}%  "
            f"{s:>6.0f}  {s/total_split*100:>5.1f}%{flag}"
        )

    print()
    print(f"Dead features (gain <0.5% of total):  {len(dead)}")
    if dead:
        for name, _, _ in dead:
            print(f"  - {name}")
    print(f"Dominant features (gain >10% of total): {len(dominant)}")
    if dominant:
        for name, g, _ in dominant:
            print(f"  - {name} ({g/total_gain*100:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
