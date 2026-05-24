"""Round-trip trade construction.

Two reconstruction paths:

1. `trades_from_user_fills` — preferred. Takes the raw response from
   Hyperliquid's `info.user_fills_by_time(address, start_ms)` and walks the
   authoritative exchange fill stream. Uses the per-fill `dir` field
   ("Open Long" / "Close Short" / etc.) and `closedPnl` so PnL already
   reflects fees + funding settlements. Sees every fill the exchange saw —
   including limit orders that filled after the bot wrote a "resting"
   placement record.

2. `compute_trades` — legacy. Builds trades from our local `fills` table
   (what the bot tried to place). Kept for the /api/fills view and for
   debugging the bot's own placement record, but NOT used for /api/trades:
   limit orders that go to "resting" never produce a follow-up "filled"
   record locally, so any trade entered via limit + closed later shows up
   as an orphaned close that this function silently drops."""
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


# --- Exchange-sourced reconstruction --------------------------------------

# Tolerance for "position closed" — Hyperliquid sizes are quoted in coin units
# with up to 6 decimals, so a closed position can carry residual floating-point
# noise around 1e-10 to 1e-12.
_EXCHANGE_POSITION_EPS = 1e-7


def _ms_to_iso(ms: int | float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).isoformat()


@dataclass
class _ExchangeFill:
    """Parsed view of one row from info.user_fills_by_time."""
    coin: str
    dir: str           # "Open Long" | "Open Short" | "Close Long" | "Close Short" | "Long > Short" (flip) etc.
    px: float
    sz: float
    closed_pnl: float
    time_ms: int

    @property
    def is_open(self) -> bool:
        return self.dir.startswith("Open")

    @property
    def is_close(self) -> bool:
        return self.dir.startswith("Close")

    @property
    def direction(self) -> str | None:
        """'long' or 'short' for plain Open/Close fills; None for flip dirs
        which we treat as a close-then-open pair (rare for this bot)."""
        if self.dir.endswith("Long"):
            return "long"
        if self.dir.endswith("Short"):
            return "short"
        return None


def _parse_exchange_fill(row: dict) -> _ExchangeFill | None:
    try:
        return _ExchangeFill(
            coin=str(row["coin"]),
            dir=str(row["dir"]),
            px=float(row["px"]),
            sz=float(row["sz"]),
            closed_pnl=float(row.get("closedPnl") or 0.0),
            time_ms=int(row["time"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def trades_from_user_fills(raw_fills: list[dict]) -> list[Trade]:
    """Build round-trip trades from Hyperliquid's user_fills response.

    Each row's `dir` field disambiguates open vs close and long vs short, so
    we don't need heuristics. PnL comes from the exchange's `closedPnl`
    (already net of fees + funding) summed across the trade's close fills.

    Walks each coin's fills in chronological order, tracking running position
    size. When size returns to ~0 we emit a Trade. Add-to-position fills in
    the same direction average into the entry price. "Flip" rows (e.g.
    `"Long > Short"`) are treated as an implicit close of the open trade
    plus a new open — rare for this bot but handled cleanly.
    """
    parsed: dict[str, list[_ExchangeFill]] = {}
    for row in raw_fills:
        pf = _parse_exchange_fill(row)
        if pf is None or pf.sz <= 0 or pf.px <= 0:
            continue
        parsed.setdefault(pf.coin, []).append(pf)

    trades: list[Trade] = []

    for coin, fills in parsed.items():
        fills.sort(key=lambda f: f.time_ms)

        # Trade-in-progress state.
        direction: str | None = None
        open_size = 0.0
        open_cost = 0.0           # Σ sz * px for opens
        close_size = 0.0
        close_value = 0.0         # Σ sz * px for closes
        realized_pnl = 0.0        # Σ closedPnl for closes (already net of fees)
        first_open_ts: int | None = None
        last_close_ts: int | None = None
        fill_count = 0

        def emit() -> None:
            nonlocal direction, open_size, open_cost, close_size, close_value
            nonlocal realized_pnl, first_open_ts, last_close_ts, fill_count
            if direction is None or first_open_ts is None or last_close_ts is None:
                return
            settled = min(open_size, close_size)
            if settled <= 0:
                return
            avg_entry = open_cost / open_size if open_size > 0 else 0.0
            avg_exit = close_value / close_size if close_size > 0 else 0.0
            open_notional = settled * avg_entry
            close_notional = settled * avg_exit
            trades.append(
                Trade(
                    asset=coin,
                    side=direction,
                    size=settled,
                    avg_entry_px=avg_entry,
                    exit_px=avg_exit,
                    open_ts_utc=_ms_to_iso(first_open_ts),
                    close_ts_utc=_ms_to_iso(last_close_ts),
                    open_notional_usd=open_notional,
                    close_notional_usd=close_notional,
                    realized_pnl_usd=realized_pnl,
                    realized_pnl_pct=(
                        realized_pnl / open_notional if open_notional > 0 else 0.0
                    ),
                    duration_seconds=int((last_close_ts - first_open_ts) / 1000),
                    fill_count=fill_count,
                )
            )
            # Reset.
            direction = None
            open_size = 0.0
            open_cost = 0.0
            close_size = 0.0
            close_value = 0.0
            realized_pnl = 0.0
            first_open_ts = None
            last_close_ts = None
            fill_count = 0

        for f in fills:
            d = f.direction  # None for flip rows ("Long > Short")

            if f.is_open and d is not None:
                if direction is not None and d != direction:
                    # Open in opposite direction with a trade in progress —
                    # treat as implicit close + new open.
                    emit()
                if direction is None:
                    direction = d
                    first_open_ts = f.time_ms
                open_size += f.sz
                open_cost += f.sz * f.px
                # Hyperliquid reports opens' closedPnl as roughly -fee (no
                # position closes, but the fee charge shows up here for
                # account-equity accounting). Sum it so total realized_pnl
                # reflects entry fees too — otherwise we systematically
                # under-count losses by a few cents per trade.
                realized_pnl += f.closed_pnl
                fill_count += 1
                continue

            if f.is_close and d is not None:
                if direction is None:
                    # Orphan close: position opened before our query window
                    # (or via a limit order whose fill we never observed in
                    # the local table). We can still back-derive the entry
                    # price from the exchange's closedPnl:
                    #   long  : pnl = sz * (px - E)  →  E = px - pnl/sz
                    #   short : pnl = sz * (E - px)  →  E = px + pnl/sz
                    # This makes the orphan trade self-consistent: it emits
                    # immediately because open_size == close_size.
                    direction = d
                    first_open_ts = f.time_ms
                    derived_entry = (
                        f.px - f.closed_pnl / f.sz
                        if d == "long"
                        else f.px + f.closed_pnl / f.sz
                    )
                    open_size += f.sz
                    open_cost += f.sz * derived_entry
                close_size += f.sz
                close_value += f.sz * f.px
                realized_pnl += f.closed_pnl
                last_close_ts = f.time_ms
                fill_count += 1
                if close_size >= open_size - _EXCHANGE_POSITION_EPS:
                    emit()
                continue

            # Flip rows ("Long > Short" / "Short > Long") — treat as close of
            # current trade. The bot doesn't flip in practice, so this branch
            # is defensive only; residual opens in the opposite direction
            # would have to come from a separate explicit Open fill.
            is_flip = ("Long > Short" in f.dir) or ("Short > Long" in f.dir)
            if direction is not None and is_flip:
                taken = min(open_size - close_size, f.sz)
                close_size += taken
                close_value += taken * f.px
                realized_pnl += f.closed_pnl
                last_close_ts = f.time_ms
                fill_count += 1
                emit()

    trades.sort(key=lambda t: t.close_ts_utc, reverse=True)
    return trades
