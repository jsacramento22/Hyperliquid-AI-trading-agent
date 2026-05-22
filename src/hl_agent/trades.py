"""Round-trip trade construction from the fills log.

Walks fills per asset in chronological order. The bot's pattern is always
open/add/close (using close_position to flatten), never an opposite-side
flip — this module handles that pattern exactly and degrades safely if it
ever sees something else."""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class ParsedFill:
    id: int
    ts_utc: str
    asset: str
    side: str          # "buy" | "sell" | "close"
    size: float        # base coin units, always positive
    avg_px: float
    requested_usd: float | None


@dataclass
class Trade:
    asset: str
    side: str          # "long" | "short"
    size: float
    avg_entry_px: float
    exit_px: float
    open_ts_utc: str
    close_ts_utc: str
    open_notional_usd: float    # size * avg_entry_px
    close_notional_usd: float   # size * exit_px
    realized_pnl_usd: float
    realized_pnl_pct: float     # vs avg open notional
    duration_seconds: int
    fill_count: int             # number of fills in this round trip


def parse_fill_row(row: dict) -> ParsedFill | None:
    """Extract avg_px and size from a fill's raw_response. Returns None if the
    response shape is unrecognized."""
    raw = row.get("raw_response")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None

    try:
        statuses = raw["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None

    if not statuses or "filled" not in statuses[0]:
        return None

    f = statuses[0]["filled"]
    try:
        size = float(f["totalSz"])
        avg_px = float(f["avgPx"])
    except (KeyError, ValueError, TypeError):
        return None

    if size <= 0 or avg_px <= 0:
        return None

    return ParsedFill(
        id=int(row["id"]),
        ts_utc=row["ts_utc"],
        asset=row["asset"],
        side=row["side"],
        size=size,
        avg_px=avg_px,
        requested_usd=row.get("requested_usd"),
    )


def _ts_diff_seconds(a: str, b: str) -> int:
    from datetime import datetime
    return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())


_POSITION_EPS = 1e-9


def _make_trade(
    direction: str, opens: list[ParsedFill], closes: list[ParsedFill]
) -> Trade:
    long = direction == "long"

    open_size = sum(o.size for o in opens)
    open_cost = sum(o.size * o.avg_px for o in opens)
    avg_entry = open_cost / open_size if open_size > 0 else 0.0

    close_size = sum(c.size for c in closes)
    close_value = sum(c.size * c.avg_px for c in closes)
    avg_exit = close_value / close_size if close_size > 0 else 0.0

    # PnL uses the actual closed size (which may be slightly different from
    # the opened size due to floating point). Use the smaller to be safe.
    settled_size = min(open_size, close_size)
    if long:
        pnl = settled_size * (avg_exit - avg_entry)
    else:
        pnl = settled_size * (avg_entry - avg_exit)

    open_notional = settled_size * avg_entry
    close_notional = settled_size * avg_exit

    return Trade(
        asset=opens[0].asset,
        side=direction,
        size=settled_size,
        avg_entry_px=avg_entry,
        exit_px=avg_exit,
        open_ts_utc=opens[0].ts_utc,
        close_ts_utc=closes[-1].ts_utc,
        open_notional_usd=open_notional,
        close_notional_usd=close_notional,
        realized_pnl_usd=pnl,
        realized_pnl_pct=pnl / open_notional if open_notional > 0 else 0.0,
        duration_seconds=_ts_diff_seconds(opens[0].ts_utc, closes[-1].ts_utc),
        fill_count=len(opens) + len(closes),
    )


def compute_trades(fill_rows: list[dict]) -> list[Trade]:
    """Walk fills in ascending-id (chronological) order, grouping opens and
    closes into round-trip trades per asset.

    Tracks running position size to handle these real-world patterns:
      - Single open + single close (the common case).
      - Multiple opens (averaging into a position).
      - Multiple closes (Hyperliquid sometimes splits market_close across
        several fills if the book is thin).
      - Opposite-direction fill without explicit close ("flip"): treated as
        an implicit close of the existing position.

    Skips any fill whose raw_response can't be parsed as a successful fill
    (e.g. orders rejected with `error: "Price too far from oracle"` get
    logged but produce no realised PnL).

    Returns trades sorted newest-first by close timestamp.
    """
    parsed: dict[str, list[ParsedFill]] = {}
    for row in sorted(fill_rows, key=lambda r: r["id"]):
        pf = parse_fill_row(row)
        if pf is None:
            continue
        parsed.setdefault(pf.asset, []).append(pf)

    trades: list[Trade] = []
    for asset, fills in parsed.items():
        opens: list[ParsedFill] = []
        closes: list[ParsedFill] = []
        direction: str | None = None
        running_size: float = 0.0  # remaining open size in coin units

        for f in fills:
            if direction is None:
                # No active trade. Only an open (buy/sell) starts one.
                if f.side == "close":
                    continue  # close without prior open — ignore
                if f.side not in ("buy", "sell"):
                    continue
                direction = "long" if f.side == "buy" else "short"
                opens.append(f)
                running_size = f.size
                continue

            same_dir_open = (
                (direction == "long" and f.side == "buy")
                or (direction == "short" and f.side == "sell")
            )

            if f.side == "close" or not same_dir_open:
                # Either explicit close, or an opposite-direction fill that
                # implicitly reduces the position.
                closes.append(f)
                running_size -= f.size
            else:
                # Adding to position in same direction (averaging in).
                opens.append(f)
                running_size += f.size

            if running_size <= _POSITION_EPS:
                trades.append(_make_trade(direction, opens, closes))
                opens = []
                closes = []
                direction = None
                running_size = 0.0

        # If a position is still open at the end of the fill list, leave it
        # out — it isn't a completed round-trip yet.

    trades.sort(key=lambda t: t.close_ts_utc, reverse=True)
    return trades
