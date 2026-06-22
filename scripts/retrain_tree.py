"""Phase 4 — one-shot retrain.

Archives the current model, optionally pulls fresh Binance candles,
runs walk-forward training, and prints a before/after diff so you can
decide whether to keep the new model. The new model overwrites
`data/models/{asset}_tree.pkl` in place — train_tree.py's normal
output path — and the previous one is moved to
`data/models/archive/{asset}_tree_{timestamp}.pkl` so rollback is a
single `mv`.

This script intentionally stays a thin orchestrator: it calls the
existing `pull_binance_candles.py` and `train_tree.py` as subprocesses
rather than importing them, so each keeps its own CLI surface and
module-level state. That keeps blast radius small if either evolves.

Usage:
    # standard retrain — uses whatever parquet is already on disk
    python scripts/retrain_tree.py

    # also refresh data first (2 years of 15m + 1h Binance candles)
    python scripts/retrain_tree.py --refresh-data --years 2

    # ETH instead of BTC
    python scripts/retrain_tree.py --asset ETH --refresh-data

    # dry run — just print what would happen
    python scripts/retrain_tree.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("retrain_tree")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_summary(meta_path: Path) -> dict | None:
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        return meta.get("walk_forward_summary")
    except Exception:
        log.exception("could not parse %s — treating as missing", meta_path)
        return None


def _fmt_summary(s: dict | None) -> str:
    if not s:
        return "(no prior model)"
    acc = s.get("accuracy", {})
    auc = s.get("auc", {})
    return (
        f"acc={acc.get('mean', 0):.4f}±{acc.get('std', 0):.4f}  "
        f"auc={auc.get('mean', 0):.4f}±{auc.get('std', 0):.4f}  "
        f"(n_folds={acc.get('n', 0)})"
    )


def _archive(model_path: Path, meta_path: Path, archive_dir: Path) -> Path | None:
    """Move the existing model + meta into archive_dir under a timestamp
    suffix. Returns the archive path, or None if nothing existed to archive."""
    if not model_path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = model_path.stem  # e.g. "btc_tree"
    archived_pkl = archive_dir / f"{base}_{ts}.pkl"
    archived_meta = archive_dir / f"{base}_{ts}.meta.json"
    shutil.move(str(model_path), archived_pkl)
    if meta_path.exists():
        shutil.move(str(meta_path), archived_meta)
    return archived_pkl


def _run(cmd: list[str], *, dry_run: bool) -> int:
    log.info("$ %s", " ".join(cmd))
    if dry_run:
        return 0
    return subprocess.call(cmd, cwd=REPO_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC")
    parser.add_argument(
        "--model-dir", default="data/models",
        help="Where the model pickle + meta live (and archive/)",
    )
    parser.add_argument(
        "--refresh-data", action="store_true",
        help="Re-pull Binance Spot candles before training",
    )
    parser.add_argument(
        "--years", type=int, default=2,
        help="Years of history when --refresh-data (default 2)",
    )
    parser.add_argument(
        "--no-archive", action="store_true",
        help="Skip archiving the current model — overwrite in place",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the steps without running anything",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    model_dir = REPO_ROOT / args.model_dir
    model_path = model_dir / f"{args.asset.lower()}_tree.pkl"
    meta_path = model_dir / f"{args.asset.lower()}_tree.meta.json"
    archive_dir = model_dir / "archive"

    # 1. Snapshot the "before" stats so we can print a diff at the end
    before = _read_summary(meta_path)
    log.info("before: %s", _fmt_summary(before))

    # 2. Optionally refresh data
    if args.refresh_data:
        py = sys.executable
        rc = _run(
            [py, "scripts/pull_binance_candles.py",
             "--asset", args.asset, "--years", str(args.years)],
            dry_run=args.dry_run,
        )
        if rc != 0:
            log.error("data refresh failed (exit %d) — aborting before train", rc)
            return rc

    # 3. Archive the current model so a bad retrain can be rolled back
    if not args.no_archive and not args.dry_run:
        archived = _archive(model_path, meta_path, archive_dir)
        if archived:
            log.info("archived previous model → %s", archived.relative_to(REPO_ROOT))
        else:
            log.info("no prior model to archive (clean slate)")
    elif args.no_archive:
        log.warning("--no-archive: previous model will be overwritten in place")

    # 4. Retrain (train_tree.py writes the new pickle + meta to model-dir)
    py = sys.executable
    rc = _run(
        [py, "scripts/train_tree.py",
         "--asset", args.asset,
         "--out-dir", str(model_dir.relative_to(REPO_ROOT))],
        dry_run=args.dry_run,
    )
    if rc != 0:
        log.error("training failed (exit %d)", rc)
        return rc

    if args.dry_run:
        log.info("dry run complete — no files changed")
        return 0

    # 5. Print before/after diff
    after = _read_summary(meta_path)
    print()
    print("=" * 60)
    print(f"Retrain complete · {args.asset}")
    print("=" * 60)
    print(f"  before:  {_fmt_summary(before)}")
    print(f"  after:   {_fmt_summary(after)}")
    if before and after:
        d_acc = (after["accuracy"]["mean"] - before["accuracy"]["mean"]) * 100
        d_auc = (after["auc"]["mean"] - before["auc"]["mean"]) * 100
        print(f"  Δ acc:   {d_acc:+.2f}pp")
        print(f"  Δ auc:   {d_auc:+.2f}pp")
        if d_acc < -0.5:
            print()
            print("  WARN: new model is meaningfully worse on walk-forward.")
            print(f"  To roll back: mv {archive_dir.relative_to(REPO_ROOT)}/"
                  f"{model_path.stem}_*.pkl {model_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
