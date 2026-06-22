"""Phase 4 — drift monitor for the LightGBM tree advisor.

Reads scored predictions from the live SQLite store, compares the rolling
accuracy against the backtest baseline stored in the model's meta.json,
and emits a single-screen verdict suitable for cron output / paging.

Exit codes (so this can be cron'd with simple alerting):
    0  OK / INSUFFICIENT   — within tolerance, or not enough scored data
    1  WARN                 — accuracy is 1.5σ–2.5σ below backtest
    2  ALERT                — accuracy is >2.5σ below backtest; retrain

The σ used here is the combined standard error of:
    - the backtest mean (walk-forward std across folds in meta.json), and
    - the live point estimate (binomial std on N scored predictions).

The combined SE prevents false positives on a small live sample (the
binomial variance dominates) and false negatives on a noisy backtest
(the backtest std widens the tolerance band).

Usage:
    python scripts/drift_check.py
    python scripts/drift_check.py --hours 720 --min-samples 200
    python scripts/drift_check.py --asset BTC --json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# Repo bootstrap so this script runs without `pip install -e .` plumbing
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hl_agent.settings import load_settings  # noqa: E402
from hl_agent.storage import Storage  # noqa: E402

log = logging.getLogger("drift_check")


# Verdict thresholds. Picked from the Phase 1.5 backtest variability —
# at 2.5σ below backtest mean with a binomial-corrected SE, we expect
# < ~1% false-positive rate when nothing is actually wrong. 1.5σ is the
# noisier early-warning band.
_ALERT_Z = 2.5
_WARN_Z = 1.5


@dataclass(frozen=True)
class Verdict:
    code: str           # "OK" | "WARN" | "ALERT" | "INSUFFICIENT"
    exit_code: int      # what to return to the shell
    reason: str
    live_n: int
    live_correct: int
    live_accuracy: float
    backtest_mean: float
    backtest_std: float
    z_score: float | None    # None when INSUFFICIENT


def compute_verdict(
    *,
    live_n: int,
    live_correct: int,
    backtest_mean: float,
    backtest_std: float,
    min_samples: int = 100,
    alert_z: float = _ALERT_Z,
    warn_z: float = _WARN_Z,
) -> Verdict:
    """Pure function — no I/O. Tested standalone in test_drift_check.py."""
    live_accuracy = (live_correct / live_n) if live_n > 0 else 0.0
    if live_n < min_samples:
        return Verdict(
            code="INSUFFICIENT",
            exit_code=0,
            reason=f"only {live_n}/{min_samples} scored predictions",
            live_n=live_n,
            live_correct=live_correct,
            live_accuracy=live_accuracy,
            backtest_mean=backtest_mean,
            backtest_std=backtest_std,
            z_score=None,
        )

    binomial_var = live_accuracy * (1 - live_accuracy) / live_n
    combined_se = math.sqrt(backtest_std**2 + binomial_var)
    if combined_se <= 0:
        # Degenerate — backtest std=0 and live accuracy is 0 or 1. Treat
        # as OK; the user is on synthetic data.
        return Verdict(
            code="OK",
            exit_code=0,
            reason="no measurable variance",
            live_n=live_n,
            live_correct=live_correct,
            live_accuracy=live_accuracy,
            backtest_mean=backtest_mean,
            backtest_std=backtest_std,
            z_score=0.0,
        )

    z = (live_accuracy - backtest_mean) / combined_se

    if z < -alert_z:
        return Verdict(
            code="ALERT",
            exit_code=2,
            reason=(
                f"live accuracy {live_accuracy:.4f} is {-z:.1f}σ below "
                f"backtest {backtest_mean:.4f} — retrain recommended"
            ),
            live_n=live_n,
            live_correct=live_correct,
            live_accuracy=live_accuracy,
            backtest_mean=backtest_mean,
            backtest_std=backtest_std,
            z_score=z,
        )
    if z < -warn_z:
        return Verdict(
            code="WARN",
            exit_code=1,
            reason=(
                f"live accuracy {live_accuracy:.4f} is {-z:.1f}σ below "
                f"backtest {backtest_mean:.4f}"
            ),
            live_n=live_n,
            live_correct=live_correct,
            live_accuracy=live_accuracy,
            backtest_mean=backtest_mean,
            backtest_std=backtest_std,
            z_score=z,
        )
    return Verdict(
        code="OK",
        exit_code=0,
        reason=(
            f"live accuracy {live_accuracy:.4f} within tolerance "
            f"({z:+.1f}σ from backtest {backtest_mean:.4f})"
        ),
        live_n=live_n,
        live_correct=live_correct,
        live_accuracy=live_accuracy,
        backtest_mean=backtest_mean,
        backtest_std=backtest_std,
        z_score=z,
    )


def _load_baseline(meta_path: Path) -> tuple[float, float, str]:
    """Returns (mean, std, model_version) from the saved meta.json. Errors
    loudly if the file/fields are missing — drift_check is useless without
    a baseline so we don't soft-fail."""
    meta = json.loads(meta_path.read_text())
    summary = meta.get("walk_forward_summary") or {}
    acc = summary.get("accuracy") or {}
    if "mean" not in acc or "std" not in acc:
        raise SystemExit(
            f"meta.json at {meta_path} missing walk_forward_summary.accuracy "
            "— retrain the model so the new meta includes walk-forward stats"
        )
    trained_at = meta.get("trained_at_utc", "?")
    best_iter = meta.get("best_iteration", "?")
    version = f"iter{best_iter}_{trained_at[:10]}"
    return float(acc["mean"]), float(acc["std"]), version


def _emit_text(v: Verdict, asset: str, version: str, hours: int) -> None:
    bar = "=" * 60
    print(bar)
    print(f"Tree drift check · {asset} · model {version} · last {hours}h")
    print(bar)
    print(f"  Live:      {v.live_correct}/{v.live_n} = {v.live_accuracy:.4f}")
    print(f"  Backtest:  {v.backtest_mean:.4f} ± {v.backtest_std:.4f}")
    if v.z_score is not None:
        print(f"  Z-score:   {v.z_score:+.2f}")
    print()
    color = {
        "OK": "\033[32m",      # green
        "WARN": "\033[33m",    # yellow
        "ALERT": "\033[31m",   # red
        "INSUFFICIENT": "\033[90m",  # gray
    }.get(v.code, "")
    reset = "\033[0m" if color else ""
    print(f"  Verdict:   {color}{v.code}{reset} — {v.reason}")
    print(bar)


def _emit_json(v: Verdict, asset: str, version: str, hours: int) -> None:
    payload = {
        "asset": asset,
        "model_version": version,
        "hours": hours,
        "verdict": v.code,
        "reason": v.reason,
        "live_n": v.live_n,
        "live_correct": v.live_correct,
        "live_accuracy": v.live_accuracy,
        "backtest_mean": v.backtest_mean,
        "backtest_std": v.backtest_std,
        "z_score": v.z_score,
    }
    print(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC")
    parser.add_argument(
        "--hours", type=int, default=24 * 30,
        help="Rolling window for live accuracy (default 30 days)",
    )
    parser.add_argument(
        "--min-samples", type=int, default=100,
        help="Verdict is INSUFFICIENT below this many scored predictions",
    )
    parser.add_argument(
        "--model-dir", default="data/models",
        help="Directory containing {asset}_tree.meta.json",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of human text",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    meta_path = Path(args.model_dir) / f"{args.asset.lower()}_tree.meta.json"
    if not meta_path.exists():
        raise SystemExit(f"baseline missing: {meta_path} not found")
    backtest_mean, backtest_std, version = _load_baseline(meta_path)

    settings = load_settings()
    storage = Storage(settings.storage_path)
    summary = storage.tree_accuracy_summary(hours=args.hours, asset=args.asset)

    verdict = compute_verdict(
        live_n=summary["scored_count"],
        live_correct=summary["correct_count"],
        backtest_mean=backtest_mean,
        backtest_std=backtest_std,
        min_samples=args.min_samples,
    )

    if args.json:
        _emit_json(verdict, args.asset, version, args.hours)
    else:
        _emit_text(verdict, args.asset, version, args.hours)

    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
