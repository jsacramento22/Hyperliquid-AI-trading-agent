"""Pull historical OHLCV candles from Binance Spot.

Hyperliquid's public candles API caps at the most recent ~5000 bars
regardless of startTime. For LightGBM walk-forward training we need
multiple years of data — Binance's public klines endpoint serves the
full history with proper pagination.

Domain note
-----------
Binance spot BTC/USDT differs slightly from Hyperliquid BTC-PERP:
  - Spot has no funding rate (perps do)
  - Cross-exchange basis is typically <0.5% in normal conditions
  - Order-book depth and microstructure differ
For LightGBM features (price action, volume, vol regime), the
substantial signal is the underlying BTC price action which is
arbitraged tight across venues. Features computed on Binance data
will generalize to Hyperliquid prediction acceptably.

The funding_rate feature is unavailable in Binance training data —
training rows will have NaN, which LightGBM handles natively. In
production, the live snapshot includes funding so the model sees a
real value at inference time.

Usage
-----
    python scripts/pull_binance_candles.py --asset BTC --years 2
    python scripts/pull_binance_candles.py --asset ETH --years 2 --intervals 15m 1h

Output
------
    data/historical/{ASSET}_{INTERVAL}_binance.parquet
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

log = logging.getLogger("pull_binance_candles")

BINANCE_API = "https://api.binance.com/api/v3/klines"
PAGE_SIZE = 1000  # Binance max bars per call

_INTERVAL_MS = {
    "1m": 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def _http_get_json(url: str, retries: int = 2) -> list:
    """Read Binance klines, with one retry on transient failures."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hl-agent-puller/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"unreachable: {last_err}")


def _pull_chunked(
    asset_pair: str, interval: str, start_ms: int, end_ms: int
) -> list[list]:
    """Walk forward in PAGE_SIZE chunks. Binance returns up to 1000 bars
    per call; we step forward by exactly that many bars to maximize
    coverage. Duplicates at boundaries are removed in the DataFrame
    conversion."""
    interval_ms = _INTERVAL_MS[interval]
    chunk_span_ms = PAGE_SIZE * interval_ms

    out: list[list] = []
    cur = start_ms
    call_idx = 0
    total_calls_est = max(1, (end_ms - start_ms) // chunk_span_ms + 1)

    while cur < end_ms:
        chunk_end = min(cur + chunk_span_ms, end_ms)
        params = {
            "symbol": asset_pair,
            "interval": interval,
            "startTime": cur,
            "endTime": chunk_end,
            "limit": PAGE_SIZE,
        }
        url = f"{BINANCE_API}?{urlencode(params)}"
        call_idx += 1
        try:
            chunk = _http_get_json(url)
        except Exception as e:
            log.error(
                "call %d/%d failed permanently at %s: %s",
                call_idx, total_calls_est,
                datetime.fromtimestamp(cur / 1000, tz=timezone.utc).date(), e,
            )
            return out  # return what we have; partial is better than nothing

        if chunk:
            out.extend(chunk)
            # Next request starts right after the last received bar
            cur = int(chunk[-1][0]) + interval_ms
        else:
            log.warning(
                "call %d empty at %s — advancing past gap",
                call_idx,
                datetime.fromtimestamp(cur / 1000, tz=timezone.utc).date(),
            )
            cur += chunk_span_ms

        if call_idx % 10 == 0 or cur >= end_ms:
            log.info(
                "  call %d/~%d: cum %d bars (cursor: %s)",
                call_idx, total_calls_est, len(out),
                datetime.fromtimestamp(cur / 1000, tz=timezone.utc).date(),
            )
        time.sleep(0.05)  # well under Binance's 20 RPS public limit
    return out


def _to_dataframe(bars: list[list]) -> pd.DataFrame:
    """Binance kline shape:
        [openTime, open, high, low, close, volume,
         closeTime, quoteVolume, numTrades,
         takerBuyBase, takerBuyQuote, ignored]
    Strings for numerics — explicitly cast to float."""
    rows = [
        {
            "open_ms": int(b[0]),
            "close_ms": int(b[6]),
            "open": float(b[1]),
            "high": float(b[2]),
            "low": float(b[3]),
            "close": float(b[4]),
            "volume": float(b[5]),
        }
        for b in bars
    ]
    df = pd.DataFrame(rows)
    return (
        df.sort_values("open_ms")
        .drop_duplicates("open_ms")
        .reset_index(drop=True)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC", help="Coin name e.g. BTC, ETH")
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument(
        "--intervals", nargs="+", default=["15m", "1h", "4h"],
        choices=sorted(_INTERVAL_MS.keys()),
    )
    parser.add_argument("--out-dir", default="data/historical")
    parser.add_argument(
        "--quote", default="USDT",
        help="Quote asset for the Binance pair (USDT default; some assets need USD)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    pair = f"{args.asset}{args.quote}"
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=365 * args.years)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for interval in args.intervals:
        log.info(
            "pulling Binance %s %s from %s to %s (%d days)",
            pair, interval, start.date(), now.date(), 365 * args.years,
        )
        raw = _pull_chunked(pair, interval, start_ms, end_ms)
        df = _to_dataframe(raw)
        out_path = out_dir / f"{args.asset}_{interval}_binance.parquet"
        df.to_parquet(out_path, index=False)
        expected = (end_ms - start_ms) // _INTERVAL_MS[interval]
        coverage = (len(df) / expected * 100) if expected else 0
        log.info(
            "✓ %s: %d bars → %s (%.1f%% of theoretical max)",
            interval, len(df), out_path, coverage,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
