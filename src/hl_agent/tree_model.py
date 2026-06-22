"""LightGBM tree predictor for Mode A advisor signal.

Loads a pickled LightGBM model + meta.json saved by scripts/train_tree.py,
maintains a small rolling state per asset (funding history, OI history)
so the per-cycle FeatureContext can be reconstructed, and returns a
TreePrediction for each supported asset on each cycle.

The prediction is informational only — wired into the LLM context as a
side signal, never used to gate or size trades directly (Mode A).

State persistence:
  - Funding history (last 24h hourly readings) and OI history (last ~25h
    of cycle snapshots) are kept in-memory on the TreePredictor instance.
  - On process restart the buffers reset; the two affected features
    (funding_z_24h, oi_change_24h_pct) return NaN for ~24h until the
    buffers refill. LightGBM handles NaN natively. Per feature-importance
    inspection in Phase 1.5, both features were already near-dead, so the
    cold-start gap costs near zero predictive power.
"""
from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .features import FeatureContext, build_features
from .market_data import MarketSnapshot

log = logging.getLogger("hl_agent.tree_model")

# Confidence buckets keyed off |prob_up - 0.5|. Thresholds picked from
# Phase 1.5 walk-forward stats: median |p-0.5| across all val folds was
# ~0.02, p90 was ~0.05. So "high" = top decile, "medium" = next quartile.
_CONF_HIGH = 0.05
_CONF_MEDIUM = 0.02

# How long to keep OI snapshots in the rolling buffer (slightly more than
# 24h so we always have a reading within ±90min of the 24h-ago target).
_OI_BUFFER_MS = 25 * 3600 * 1000
_OI_TARGET_LAG_MS = 24 * 3600 * 1000
_OI_TARGET_TOL_MS = 90 * 60 * 1000

# Funding readings are sampled per cycle (every ~15 min). We need the last
# 24h for funding_z_24h's mean/std; cap the deque so the buffer doesn't
# grow unbounded over weeks of uptime.
_FUNDING_KEEP = 24 * 4   # 24h × 4 cycles/hour at 15m cadence


@dataclass(frozen=True)
class TreePrediction:
    """One asset's prediction at one cycle.

    `prob_up` is the raw LightGBM probability that close in LABEL_HORIZON
    bars exceeds the current close (45-min horizon for the R3.1 model).
    `predicted_direction` is the >0.5 threshold call, `confidence` is a
    coarse bucket used to colour the dashboard, and `model_version`
    identifies which pickle produced this output for drift attribution.
    """
    asset: str
    prob_up: float
    predicted_direction: str   # "up" | "down"
    confidence: str            # "low" | "medium" | "high"
    model_version: str
    horizon_bars: int          # number of 15m bars the label spans


def _bucket_confidence(prob_up: float) -> str:
    delta = abs(prob_up - 0.5)
    if delta >= _CONF_HIGH:
        return "high"
    if delta >= _CONF_MEDIUM:
        return "medium"
    return "low"


class TreePredictor:
    """Wraps a pickled LightGBM Booster behind a per-snapshot predict()."""

    def __init__(
        self,
        *,
        model_path: Path,
        meta_path: Path,
        supported_assets: tuple[str, ...] = ("BTC",),
    ):
        with open(model_path, "rb") as f:
            self._model = pickle.load(f)
        self._meta = json.loads(meta_path.read_text())
        self._feature_names: tuple[str, ...] = tuple(self._meta["feature_names"])
        self._asset = self._meta.get("asset", "BTC")
        # Version is best_iteration + trained_at — distinct enough that a
        # retrain produces a new version string without us needing semver
        # discipline. Keeps the dashboard's per-version filtering honest.
        trained_at = self._meta.get("trained_at_utc", "?")
        best_iter = self._meta.get("best_iteration", "?")
        self.model_version = f"{self._asset.lower()}_tree_iter{best_iter}_{trained_at[:10]}"
        # Horizon comes from the training config — Phase 1.5 used 3 (45min).
        self.horizon_bars = int(self._meta.get("label_horizon_bars", 3))
        self._supported = tuple(a for a in supported_assets if a == self._asset)

        # Rolling state — one buffer per asset (only BTC for now)
        self._funding: dict[str, list[float]] = {a: [] for a in self._supported}
        self._oi: dict[str, list[tuple[int, float]]] = {a: [] for a in self._supported}

        log.info(
            "TreePredictor loaded: %s (model_version=%s, horizon=%d bars, %d features)",
            self._asset, self.model_version, self.horizon_bars,
            len(self._feature_names),
        )

    @property
    def supported_assets(self) -> tuple[str, ...]:
        return self._supported

    def _update_state(self, asset: str, asnap, snapshot_ts_ms: int) -> None:
        fund = self._funding[asset]
        fund.append(float(asnap.funding_hourly))
        if len(fund) > _FUNDING_KEEP:
            del fund[: len(fund) - _FUNDING_KEEP]

        oi_buf = self._oi[asset]
        oi_buf.append((snapshot_ts_ms, float(asnap.open_interest)))
        cutoff = snapshot_ts_ms - _OI_BUFFER_MS
        # Trim from the front (oldest) — small lists, linear scan is fine
        while oi_buf and oi_buf[0][0] < cutoff:
            oi_buf.pop(0)

    def _oi_24h_ago(self, asset: str, snapshot_ts_ms: int) -> float:
        target = snapshot_ts_ms - _OI_TARGET_LAG_MS
        best: tuple[int, float] | None = None
        for ts, oi in self._oi[asset]:
            if abs(ts - target) <= _OI_TARGET_TOL_MS:
                if best is None or abs(ts - target) < abs(best[0] - target):
                    best = (ts, oi)
        return best[1] if best is not None else float("nan")

    def predict(self, snapshot: MarketSnapshot) -> dict[str, TreePrediction]:
        out: dict[str, TreePrediction] = {}
        for asset in self._supported:
            asnap = snapshot.assets.get(asset)
            if asnap is None:
                continue
            self._update_state(asset, asnap, snapshot.timestamp_ms)
            ctx = FeatureContext(
                funding_history_hourly=list(self._funding[asset]),
                oi_24h_ago=self._oi_24h_ago(asset, snapshot.timestamp_ms),
            )
            feat = build_features(asnap, snapshot.timestamp_ms, ctx)
            row = np.array(
                [[feat[k] for k in self._feature_names]], dtype=np.float64,
            )
            # LightGBM Booster.predict returns ndarray; binary objective →
            # the value IS the probability of class 1 (label=up).
            prob_up = float(self._model.predict(row)[0])
            out[asset] = TreePrediction(
                asset=asset,
                prob_up=prob_up,
                predicted_direction="up" if prob_up >= 0.5 else "down",
                confidence=_bucket_confidence(prob_up),
                model_version=self.model_version,
                horizon_bars=self.horizon_bars,
            )
        return out


def try_build_predictor(
    *,
    model_dir: Path,
    asset: str = "BTC",
) -> TreePredictor | None:
    """Best-effort loader: returns None when the model files aren't on disk
    rather than raising, so main.py can fall back to LLM-only mode in
    environments that haven't run training yet (fresh checkouts, CI).
    """
    model_path = model_dir / f"{asset.lower()}_tree.pkl"
    meta_path = model_dir / f"{asset.lower()}_tree.meta.json"
    if not model_path.exists() or not meta_path.exists():
        log.info(
            "tree model not found at %s — running LLM-only "
            "(run scripts/train_tree.py to enable)",
            model_path,
        )
        return None
    try:
        return TreePredictor(model_path=model_path, meta_path=meta_path)
    except Exception:
        log.exception("failed to load tree model — running LLM-only")
        return None
