from __future__ import annotations

import time
from dataclasses import dataclass

from .hl_client import HLClient


@dataclass
class Candle:
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class AssetSnapshot:
    asset: str
    mid: float
    mark: float
    funding_hourly: float          # current funding rate (per hour, fractional)
    open_interest: float           # in asset units
    day_volume_usd: float
    sz_decimals: int               # max decimal places for order size on this asset
    candles_15m: list[Candle]      # near-term — for late-entry / chase detection
    candles_1h: list[Candle]
    candles_4h: list[Candle]


@dataclass
class MarketSnapshot:
    timestamp_ms: int
    assets: dict[str, AssetSnapshot]


def _parse_candle(c: dict) -> Candle:
    return Candle(
        open_ms=int(c["t"]),
        close_ms=int(c["T"]),
        open=float(c["o"]),
        high=float(c["h"]),
        low=float(c["l"]),
        close=float(c["c"]),
        volume=float(c["v"]),
    )


def _fetch_candles(client: HLClient, asset: str, interval: str, count: int) -> list[Candle]:
    interval_ms = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
    end = int(time.time() * 1000)
    start = end - interval_ms * (count + 1)
    raw = client.info.candles_snapshot(asset, interval, start, end)
    return [_parse_candle(c) for c in raw[-count:]]


def get_snapshot(
    client: HLClient,
    assets: list[str],
    *,
    candles_15m: int = 12,
    candles_1h: int = 24,
    candles_4h: int = 14,
) -> MarketSnapshot:
    mids_raw = client.info.all_mids()
    meta, asset_ctxs = client.info.meta_and_asset_ctxs()
    universe = {entry["name"]: i for i, entry in enumerate(meta["universe"])}

    out: dict[str, AssetSnapshot] = {}
    for asset in assets:
        if asset not in universe:
            raise ValueError(f"Asset {asset!r} not in Hyperliquid perp universe")
        idx = universe[asset]
        ctx = asset_ctxs[idx]
        out[asset] = AssetSnapshot(
            asset=asset,
            mid=float(mids_raw.get(asset, ctx.get("midPx") or ctx["markPx"])),
            mark=float(ctx["markPx"]),
            funding_hourly=float(ctx.get("funding", 0.0)),
            open_interest=float(ctx.get("openInterest", 0.0)),
            day_volume_usd=float(ctx.get("dayNtlVlm", 0.0)),
            sz_decimals=int(meta["universe"][idx]["szDecimals"]),
            candles_15m=_fetch_candles(client, asset, "15m", candles_15m),
            candles_1h=_fetch_candles(client, asset, "1h", candles_1h),
            candles_4h=_fetch_candles(client, asset, "4h", candles_4h),
        )

    return MarketSnapshot(timestamp_ms=int(time.time() * 1000), assets=out)
