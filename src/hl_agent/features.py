"""Feature engineering for the LightGBM second-opinion model.

Pure functions only — no I/O, no global state, no logging. Each function
takes a list of `Candle` (most-recent-last by convention) plus a small
number of scalars and returns a single float feature value. The whole
module is trivially unit-testable, and the no-lookahead-leakage property
of each feature can be proved by adding a future bar to the input and
asserting the value doesn't change.

Conventions
-----------
- All candle lists are ordered OLDEST → NEWEST. The "current" bar is the
  last element. Functions that take `candles_15m`/`candles_1h`/etc. use
  the last bar as the reference point.
- Functions return `float('nan')` when there aren't enough bars to
  compute the feature. The caller (`tree_model.py`) decides how to
  handle NaN — typically by skipping the cycle or imputing with
  training-set median.
- "Lookahead" means using bar[t+k] (for k>0) to compute the feature
  value at time t. Every function here uses only bars[:t+1] (where t is
  the index of the current bar). The `tests/test_features.py` proves
  this per-feature by adding a future bar to the input.

The feature set (~25 features) covers: multi-horizon returns, realized
volatility, trend, momentum, mean-reversion, volume regime, perp-specific
microstructure (funding + OI), and cyclical time features.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .market_data import AssetSnapshot, Candle, MarketSnapshot


# --- Primitives -----------------------------------------------------------


def _closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def _highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def _lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]


def _volumes(candles: list[Candle]) -> list[float]:
    return [c.volume for c in candles]


def _mean(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)  # sample std
    return math.sqrt(var)


def _pct_change(now: float, then: float) -> float:
    if then == 0 or math.isnan(then) or math.isnan(now):
        return float("nan")
    return (now - then) / then


# --- Returns (4) ----------------------------------------------------------


def return_15m(candles_15m: list[Candle]) -> float:
    """Most recent 15m bar's return: (close - open) / open."""
    if not candles_15m:
        return float("nan")
    c = candles_15m[-1]
    return _pct_change(c.close, c.open)


def return_1h(candles_15m: list[Candle]) -> float:
    """Return over the last 1 hour = last 4 × 15m bars.

    Uses 15m candles (not 1h) so the computation is consistent with the
    other rolling features and depends on a single source-of-truth bar
    series. With <4 bars returns NaN.
    """
    if len(candles_15m) < 5:
        return float("nan")
    now = candles_15m[-1].close
    then = candles_15m[-5].close  # 4 bars back = 1h ago
    return _pct_change(now, then)


def return_4h(candles_15m: list[Candle]) -> float:
    """Return over the last 4 hours = last 16 × 15m bars."""
    if len(candles_15m) < 17:
        return float("nan")
    now = candles_15m[-1].close
    then = candles_15m[-17].close
    return _pct_change(now, then)


def return_24h(candles_15m: list[Candle]) -> float:
    """Return over the last 24 hours = last 96 × 15m bars."""
    if len(candles_15m) < 97:
        return float("nan")
    now = candles_15m[-1].close
    then = candles_15m[-97].close
    return _pct_change(now, then)


# --- Realized volatility (3) ---------------------------------------------


def realized_vol_4h(candles_15m: list[Candle]) -> float:
    """Sample std of the last 16 × 15m simple returns. NaN if <17 bars."""
    if len(candles_15m) < 17:
        return float("nan")
    closes = _closes(candles_15m[-17:])
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]
    return _std(rets)


def realized_vol_24h(candles_15m: list[Candle]) -> float:
    """Sample std of the last 96 × 15m simple returns. NaN if <97 bars."""
    if len(candles_15m) < 97:
        return float("nan")
    closes = _closes(candles_15m[-97:])
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]
    return _std(rets)


def atr_14(candles_1h: list[Candle]) -> float:
    """Average True Range over last 14 × 1h candles.

    TR for a bar = max(high - low, |high - prev_close|, |low - prev_close|).
    ATR = mean of last 14 TRs. Normalized by the most recent close so it
    becomes a unitless % volatility measure.
    """
    if len(candles_1h) < 15:  # need 14 TRs → need 15 candles
        return float("nan")
    trs: list[float] = []
    for i in range(len(candles_1h) - 14, len(candles_1h)):
        cur = candles_1h[i]
        prev = candles_1h[i - 1]
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    atr = _mean(trs)
    last_close = candles_1h[-1].close
    if last_close == 0:
        return float("nan")
    return atr / last_close


# --- Trend (4) ------------------------------------------------------------


def ma_cross_5_20(candles_15m: list[Candle]) -> float:
    """+1 if MA(5) > MA(20), -1 if below, 0 if equal. NaN if <20 bars."""
    if len(candles_15m) < 20:
        return float("nan")
    closes = _closes(candles_15m)
    ma5 = _mean(closes[-5:])
    ma20 = _mean(closes[-20:])
    if ma5 > ma20:
        return 1.0
    if ma5 < ma20:
        return -1.0
    return 0.0


def ma_cross_12_50(candles_15m: list[Candle]) -> float:
    """+1 if MA(12) > MA(50), -1 if below. NaN if <50 bars."""
    if len(candles_15m) < 50:
        return float("nan")
    closes = _closes(candles_15m)
    ma12 = _mean(closes[-12:])
    ma50 = _mean(closes[-50:])
    if ma12 > ma50:
        return 1.0
    if ma12 < ma50:
        return -1.0
    return 0.0


def _linreg_slope(ys: list[float]) -> float:
    """OLS slope of ys vs x=0..n-1. Normalized by mean(y) so it's a
    unitless % slope per bar. NaN if <2 points or mean=0."""
    n = len(ys)
    if n < 2:
        return float("nan")
    xs = list(range(n))
    mean_x = (n - 1) / 2.0
    mean_y = _mean(ys)
    if mean_y == 0:
        return float("nan")
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return float("nan")
    return (num / den) / mean_y


def slope_4h(candles_15m: list[Candle]) -> float:
    """OLS slope of last 16 × 15m closes, normalized by mean close."""
    if len(candles_15m) < 16:
        return float("nan")
    return _linreg_slope(_closes(candles_15m[-16:]))


def slope_24h(candles_15m: list[Candle]) -> float:
    """OLS slope of last 96 × 15m closes, normalized by mean close."""
    if len(candles_15m) < 96:
        return float("nan")
    return _linreg_slope(_closes(candles_15m[-96:]))


# --- Momentum (3) --------------------------------------------------------


def rsi_14(candles_15m: list[Candle]) -> float:
    """Classic 14-period RSI on 15m closes. Range 0-100. NaN if <15 bars."""
    if len(candles_15m) < 15:
        return float("nan")
    closes = _closes(candles_15m[-15:])
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)
    avg_gain = _mean(gains)
    avg_loss = _mean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0  # no losses → neutral or max
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(ys: list[float], period: int) -> float:
    """Last EMA value. Seeded with SMA over first `period`. NaN if too few."""
    if len(ys) < period:
        return float("nan")
    alpha = 2.0 / (period + 1)
    ema = _mean(ys[:period])
    for y in ys[period:]:
        ema = alpha * y + (1 - alpha) * ema
    return ema


# EMA's alpha-weighting never fully "forgets" the first bar, which makes
# naive EMA computation depend on input length. We cap the effective
# window so the residual influence of pre-window bars is negligible
# (~3e-5 after 100 iterations with period=26). This makes MACD
# deterministic in the input length above the cap, which the
# no-lookahead-leakage tests verify.
_MACD_WINDOW = 100


def macd(candles_15m: list[Candle]) -> float:
    """MACD line = EMA(12) - EMA(26) on the LAST 100 × 15m closes.
    Normalized by last close so unitless. NaN if <26 bars.

    The 100-bar cap is intentional: EMAs are path-dependent, and
    truncating beyond ~100 iterations makes the first bar's weight
    effectively zero. Without this cap, MACD would silently drift with
    the input length — a subtle leakage path."""
    if len(candles_15m) < 26:
        return float("nan")
    closes = _closes(candles_15m[-_MACD_WINDOW:])
    last = closes[-1]
    if last == 0:
        return float("nan")
    return (_ema(closes, 12) - _ema(closes, 26)) / last


def macd_signal_diff(candles_15m: list[Candle]) -> float:
    """MACD - 9-period EMA of MACD (the "signal" line), normalized by
    last close. NaN if <34 bars (26 + 9 - 1).

    Uses the LAST 108 bars internally (100 for MACD stability + 8 prior
    to build 9 MACD values for the signal EMA). Same path-dependence
    fix as macd()."""
    if len(candles_15m) < 34:
        return float("nan")
    closes = _closes(candles_15m[-(_MACD_WINDOW + 8):])
    macd_series: list[float] = []
    for end in range(len(closes) - 8, len(closes) + 1):
        window = closes[:end]
        ema12 = _ema(window, 12)
        ema26 = _ema(window, 26)
        macd_series.append(ema12 - ema26)
    signal = _ema(macd_series, 9)
    cur_macd = macd_series[-1]
    last = closes[-1]
    if last == 0:
        return float("nan")
    return (cur_macd - signal) / last


# --- Mean reversion (2) ---------------------------------------------------


def distance_from_vwap_24h(candles_15m: list[Candle]) -> float:
    """(close - VWAP_24h) / VWAP_24h. NaN if <96 bars or sum-of-vol is 0.

    VWAP = sum(typical_price * volume) / sum(volume) over the window.
    typical_price = (high + low + close) / 3.
    """
    if len(candles_15m) < 96:
        return float("nan")
    window = candles_15m[-96:]
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in window)
    den = sum(c.volume for c in window)
    if den == 0:
        return float("nan")
    vwap = num / den
    if vwap == 0:
        return float("nan")
    return (candles_15m[-1].close - vwap) / vwap


def bollinger_position(candles_15m: list[Candle]) -> float:
    """Position of last close within Bollinger Bands (20-period, 2-std).

    Returns a value where:
      0   = at lower band
      0.5 = at middle band (SMA20)
      1   = at upper band
    Can extend outside [0, 1] when price is beyond the bands.
    NaN if <20 bars or std=0.
    """
    if len(candles_15m) < 20:
        return float("nan")
    closes = _closes(candles_15m[-20:])
    mid = _mean(closes)
    sd = _std(closes)
    if sd == 0:
        return float("nan")
    upper = mid + 2 * sd
    lower = mid - 2 * sd
    if upper == lower:
        return float("nan")
    return (candles_15m[-1].close - lower) / (upper - lower)


# --- Volume (2) -----------------------------------------------------------


def volume_z_score_24h(candles_15m: list[Candle]) -> float:
    """Z-score of the most recent 15m bar's volume vs the prior 96 bars.

    Uses the PRIOR window (not including current) so the current bar
    doesn't bias the mean/std it's being compared against. NaN if <97
    bars or std=0."""
    if len(candles_15m) < 97:
        return float("nan")
    prior = _volumes(candles_15m[-97:-1])  # 96 bars before current
    cur_vol = candles_15m[-1].volume
    mu = _mean(prior)
    sd = _std(prior)
    if sd == 0:
        return float("nan")
    return (cur_vol - mu) / sd


def volume_ratio_4h(candles_15m: list[Candle]) -> float:
    """Current 15m volume / mean volume of prior 16 bars. NaN if <17
    bars or prior mean is 0."""
    if len(candles_15m) < 17:
        return float("nan")
    prior = _volumes(candles_15m[-17:-1])  # 16 bars before current
    cur = candles_15m[-1].volume
    mean_prior = _mean(prior)
    if mean_prior == 0:
        return float("nan")
    return cur / mean_prior


# --- Microstructure (3) — perps-only --------------------------------------


def funding_z_24h(
    funding_now: float, funding_history_hourly: list[float]
) -> float:
    """Z-score of current funding vs last 24 hourly funding observations.

    funding_history_hourly is expected to be a list of ~24 prior hourly
    funding values (NOT including the current). The caller is responsible
    for collecting this history; for now we accept whatever the snapshot
    has and degrade gracefully when there isn't enough.

    Returns NaN if <2 history points or std=0.
    """
    if len(funding_history_hourly) < 2:
        return float("nan")
    mu = _mean(funding_history_hourly)
    sd = _std(funding_history_hourly)
    if sd == 0:
        return float("nan")
    return (funding_now - mu) / sd


def oi_change_24h_pct(oi_now: float, oi_24h_ago: float) -> float:
    """Percent change in open interest vs 24h ago. NaN if either is 0
    or NaN."""
    if oi_now == 0 or oi_24h_ago == 0:
        return float("nan")
    if math.isnan(oi_now) or math.isnan(oi_24h_ago):
        return float("nan")
    return (oi_now - oi_24h_ago) / oi_24h_ago


# --- Intra-bar shape (4) — Round 2 ----------------------------------------
# These look at the SHAPE of the most recent bar (where it opened, where
# it closed, where it tagged its highs and lows) which the existing OHLCV-
# average features mostly throw away. All four use only the last bar →
# no leakage risk (window = 1).


def bar_body_ratio(candles_15m: list[Candle]) -> float:
    """|close - open| / (high - low) of the last bar.
    Range [0, 1]. High = strong directional conviction; low = doji."""
    if not candles_15m:
        return float("nan")
    c = candles_15m[-1]
    rng = c.high - c.low
    if rng <= 0:
        return float("nan")
    return abs(c.close - c.open) / rng


def upper_wick_ratio(candles_15m: list[Candle]) -> float:
    """(high - max(open,close)) / (high - low). Range [0, 1].
    High = price tested up but was rejected (sellers defended)."""
    if not candles_15m:
        return float("nan")
    c = candles_15m[-1]
    rng = c.high - c.low
    if rng <= 0:
        return float("nan")
    return (c.high - max(c.open, c.close)) / rng


def lower_wick_ratio(candles_15m: list[Candle]) -> float:
    """(min(open,close) - low) / (high - low). Range [0, 1].
    High = price tested down but was rejected (buyers defended)."""
    if not candles_15m:
        return float("nan")
    c = candles_15m[-1]
    rng = c.high - c.low
    if rng <= 0:
        return float("nan")
    return (min(c.open, c.close) - c.low) / rng


def close_position_in_range(candles_15m: list[Candle]) -> float:
    """(close - low) / (high - low). Range [0, 1].
    0 = closed at low (bearish), 1 = closed at high (bullish)."""
    if not candles_15m:
        return float("nan")
    c = candles_15m[-1]
    rng = c.high - c.low
    if rng <= 0:
        return float("nan")
    return (c.close - c.low) / rng


# --- Volume-weighted (2) — Round 2 ----------------------------------------


def vol_weighted_return_4h(candles_15m: list[Candle]) -> float:
    """Σ(return_i × volume_z_score_i) over last 16 × 15m bars.

    Returns are weighted by how unusual the bar's volume was — moves on
    high relative volume count more than moves on thin tape. The z-score
    is computed against the same 16-bar window so this is self-contained.
    NaN if <17 bars or zero volume variance."""
    if len(candles_15m) < 17:
        return float("nan")
    window = candles_15m[-17:]  # 16 prior + current = 17 to get 16 returns
    rets: list[float] = []
    vols: list[float] = []
    closes = _closes(window)
    for i in range(1, len(closes)):
        if closes[i - 1] == 0:
            continue
        rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
        vols.append(window[i].volume)
    if not rets:
        return float("nan")
    mu = _mean(vols)
    sd = _std(vols)
    if sd == 0:
        return float("nan")
    z_scores = [(v - mu) / sd for v in vols]
    return sum(r * z for r, z in zip(rets, z_scores))


def signed_volume_imbalance_4h(candles_15m: list[Candle]) -> float:
    """Σ(volume × sign(close - open)) / Σ(volume) over last 16 bars.
    Range [-1, 1]. +1 = every bar bullish, -1 = every bar bearish.
    Proxy for net buy/sell pressure. NaN if <16 bars or zero total volume."""
    if len(candles_15m) < 16:
        return float("nan")
    window = candles_15m[-16:]
    signed = 0.0
    total_vol = 0.0
    for c in window:
        sign = 1.0 if c.close > c.open else (-1.0 if c.close < c.open else 0.0)
        signed += c.volume * sign
        total_vol += c.volume
    if total_vol == 0:
        return float("nan")
    return signed / total_vol


# --- Multi-bar momentum (3) — Round 2 -------------------------------------


def consecutive_up_count(candles_15m: list[Candle]) -> float:
    """Number of consecutive up-closes ending at the current bar.
    Capped at 10 to avoid extreme outliers dominating tree splits.
    NaN if <2 bars."""
    if len(candles_15m) < 2:
        return float("nan")
    count = 0
    for i in range(len(candles_15m) - 1, 0, -1):
        if candles_15m[i].close > candles_15m[i - 1].close:
            count += 1
            if count >= 10:
                break
        else:
            break
    return float(count)


def acceleration_4h(candles_15m: list[Candle]) -> float:
    """Slope of last 8 bars MINUS slope of bars 9-16. Positive = trend
    accelerating; negative = decelerating. Catches momentum changes
    before they show up in raw return numbers. NaN if <16 bars."""
    if len(candles_15m) < 16:
        return float("nan")
    closes = _closes(candles_15m)
    recent = closes[-8:]
    earlier = closes[-16:-8]
    return _linreg_slope(recent) - _linreg_slope(earlier)


def range_expansion(candles_15m: list[Candle], candles_1h: list[Candle]) -> float:
    """ATR(5) / ATR(20) using 1h candles. >1 = volatility expanding,
    <1 = contracting, =1 = stable. NaN if insufficient 1h history."""
    if len(candles_1h) < 21:  # need 21 for ATR(20) which uses 20 TRs
        return float("nan")
    atr_short = atr_n(candles_1h, n=5)
    atr_long = atr_n(candles_1h, n=20)
    if math.isnan(atr_short) or math.isnan(atr_long) or atr_long == 0:
        return float("nan")
    return atr_short / atr_long


def atr_n(candles_1h: list[Candle], n: int) -> float:
    """Generic ATR helper for arbitrary n. Same shape as atr_14 but
    parameterized; used internally by range_expansion."""
    if len(candles_1h) < n + 1:
        return float("nan")
    trs: list[float] = []
    for i in range(len(candles_1h) - n, len(candles_1h)):
        cur = candles_1h[i]
        prev = candles_1h[i - 1]
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    return _mean(trs)


# --- Time (cyclical encoding, 2 features each) ----------------------------


def hour_of_day_sin_cos(timestamp_ms: int) -> tuple[float, float]:
    """Encode hour-of-day (UTC) as (sin, cos) so the model sees Sunday-
    23h as close to Monday-00h. Range [-1, 1] each."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60.0  # fractional hour for 15m bars
    angle = 2 * math.pi * hour / 24.0
    return math.sin(angle), math.cos(angle)


def day_of_week_sin_cos(timestamp_ms: int) -> tuple[float, float]:
    """Encode day-of-week as (sin, cos). Monday=0. Range [-1, 1]."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    angle = 2 * math.pi * dt.weekday() / 7.0
    return math.sin(angle), math.cos(angle)


# --- Public assembly ------------------------------------------------------


# Feature names in stable order. The trained LightGBM model expects
# features in this exact order — DO NOT REORDER without retraining.
# Round 2 added 9 new features APPENDED at the end so existing models
# can still be loaded (they'll just lack the new columns).
FEATURE_NAMES: tuple[str, ...] = (
    # Returns (4)
    "r_15m", "r_1h", "r_4h", "r_24h",
    # Realized vol (3)
    "realized_vol_4h", "realized_vol_24h", "atr_14",
    # Trend (4)
    "ma_cross_5_20", "ma_cross_12_50", "slope_4h", "slope_24h",
    # Momentum (3)
    "rsi_14", "macd", "macd_signal_diff",
    # Mean reversion (2)
    "distance_from_vwap_24h", "bollinger_position",
    # Volume (2)
    "volume_z_score_24h", "volume_ratio_4h",
    # Microstructure (3)
    "funding_rate", "funding_z_24h", "oi_change_24h_pct",
    # Time (4 from 2 cyclical encodings)
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    # Round 2: intra-bar shape (4)
    "bar_body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "close_position_in_range",
    # Round 2: volume-weighted (2)
    "vol_weighted_return_4h", "signed_volume_imbalance_4h",
    # Round 2: multi-bar momentum (3)
    "consecutive_up_count", "acceleration_4h", "range_expansion",
)


@dataclass
class FeatureContext:
    """Optional historical state for features that need data not in a
    single snapshot. The agent loop maintains this in-process across
    cycles; for offline training the script populates it from the
    historical parquet rows."""
    funding_history_hourly: list[float] = None  # type: ignore
    oi_24h_ago: float = float("nan")

    def __post_init__(self):
        if self.funding_history_hourly is None:
            self.funding_history_hourly = []


def build_features(
    asset_snap: AssetSnapshot,
    timestamp_ms: int,
    ctx: FeatureContext | None = None,
) -> dict[str, float]:
    """Compute all features for one asset at one point in time.

    Inputs:
      asset_snap: the AssetSnapshot — provides candles_15m / candles_1h /
        candles_4h, funding_hourly, open_interest.
      timestamp_ms: the "current" time. Time features (hour, day-of-week)
        are derived from this. Typically this is asset_snap.candles_15m[-1].close_ms.
      ctx: optional historical state (funding history, OI 24h ago) that
        can't be derived from a single snapshot. When None, the features
        that need it return NaN.

    Returns dict keyed by FEATURE_NAMES. Values are float (may be NaN
    when there's insufficient history). LightGBM handles NaN natively —
    no imputation needed at this layer.
    """
    if ctx is None:
        ctx = FeatureContext()
    c15 = asset_snap.candles_15m
    c1h = asset_snap.candles_1h

    hour_sin, hour_cos = hour_of_day_sin_cos(timestamp_ms)
    dow_sin, dow_cos = day_of_week_sin_cos(timestamp_ms)

    return {
        "r_15m": return_15m(c15),
        "r_1h": return_1h(c15),
        "r_4h": return_4h(c15),
        "r_24h": return_24h(c15),
        "realized_vol_4h": realized_vol_4h(c15),
        "realized_vol_24h": realized_vol_24h(c15),
        "atr_14": atr_14(c1h),
        "ma_cross_5_20": ma_cross_5_20(c15),
        "ma_cross_12_50": ma_cross_12_50(c15),
        "slope_4h": slope_4h(c15),
        "slope_24h": slope_24h(c15),
        "rsi_14": rsi_14(c15),
        "macd": macd(c15),
        "macd_signal_diff": macd_signal_diff(c15),
        "distance_from_vwap_24h": distance_from_vwap_24h(c15),
        "bollinger_position": bollinger_position(c15),
        "volume_z_score_24h": volume_z_score_24h(c15),
        "volume_ratio_4h": volume_ratio_4h(c15),
        "funding_rate": asset_snap.funding_hourly,
        "funding_z_24h": funding_z_24h(
            asset_snap.funding_hourly, ctx.funding_history_hourly
        ),
        "oi_change_24h_pct": oi_change_24h_pct(
            asset_snap.open_interest, ctx.oi_24h_ago
        ),
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "dow_sin": dow_sin,
        "dow_cos": dow_cos,
        # Round 2 additions
        "bar_body_ratio": bar_body_ratio(c15),
        "upper_wick_ratio": upper_wick_ratio(c15),
        "lower_wick_ratio": lower_wick_ratio(c15),
        "close_position_in_range": close_position_in_range(c15),
        "vol_weighted_return_4h": vol_weighted_return_4h(c15),
        "signed_volume_imbalance_4h": signed_volume_imbalance_4h(c15),
        "consecutive_up_count": consecutive_up_count(c15),
        "acceleration_4h": acceleration_4h(c15),
        "range_expansion": range_expansion(c15, c1h),
    }


def build_features_from_snapshot(
    snapshot: MarketSnapshot,
    contexts: dict[str, FeatureContext] | None = None,
) -> dict[str, dict[str, float]]:
    """Convenience: compute features for every asset in the snapshot.
    Returns asset → feature dict.

    `contexts` is a per-asset dict of FeatureContext. Pass None to get
    NaN on the history-dependent features (acceptable during the very
    first cycles after process start)."""
    contexts = contexts or {}
    out: dict[str, dict[str, float]] = {}
    for asset, snap in snapshot.assets.items():
        # Use the latest candle's close time as the "current" timestamp;
        # falls back to snapshot.timestamp_ms if no candles.
        if snap.candles_15m:
            ts_ms = snap.candles_15m[-1].close_ms
        else:
            ts_ms = snapshot.timestamp_ms
        out[asset] = build_features(snap, ts_ms, contexts.get(asset))
    return out
