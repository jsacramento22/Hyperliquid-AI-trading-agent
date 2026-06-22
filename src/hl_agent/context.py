from __future__ import annotations

from datetime import datetime, timezone

from typing import TYPE_CHECKING

from .account import AccountState
from .market_data import AssetSnapshot, Candle, MarketSnapshot

if TYPE_CHECKING:
    from .tree_model import TreePrediction


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _candle_table(candles: list[Candle]) -> str:
    rows = ["time(UTC) | open | high | low | close | vol"]
    for c in candles:
        ts = datetime.fromtimestamp(c.open_ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        rows.append(
            f"{ts} | {c.open:g} | {c.high:g} | {c.low:g} | {c.close:g} | {c.volume:g}"
        )
    return "\n".join(rows)


def _asset_block(snap: AssetSnapshot) -> str:
    closes_1h = [c.close for c in snap.candles_1h]
    pct_24h = ((closes_1h[-1] / closes_1h[0]) - 1) if len(closes_1h) >= 2 else 0.0
    pct_4h = ((closes_1h[-1] / closes_1h[-5]) - 1) if len(closes_1h) >= 5 else 0.0
    pct_1h = ((closes_1h[-1] / closes_1h[-2]) - 1) if len(closes_1h) >= 2 else 0.0

    # Recent-action summary derived from 15m candles — surfaces the "is this a
    # late entry?" question without making Claude do the arithmetic. Uses 4
    # candles (60 minutes) to match the "last 60 minutes" threshold in the
    # Entry quality rule in the system prompt — keep these aligned.
    closes_15m = [c.close for c in snap.candles_15m]
    pct_60m_rolling = (
        ((closes_15m[-1] / closes_15m[-5]) - 1) if len(closes_15m) >= 5 else 0.0
    )

    # Relative volume — raw volume numbers are meaningless to an LLM without a
    # baseline. Compute the latest candle's volume as a multiple of the
    # rolling average. >=1.5x = real surge confirming a move; <=0.6x = thin /
    # dead market where any "breakout" is suspect.
    vols_1h = [c.volume for c in snap.candles_1h]
    vols_15m = [c.volume for c in snap.candles_15m]
    avg_1h_vol = sum(vols_1h) / len(vols_1h) if vols_1h else 0.0
    avg_15m_vol = sum(vols_15m) / len(vols_15m) if vols_15m else 0.0
    rel_1h_vol = (vols_1h[-1] / avg_1h_vol) if avg_1h_vol > 0 else 0.0
    rel_15m_vol = (vols_15m[-1] / avg_15m_vol) if avg_15m_vol > 0 else 0.0

    return (
        f"## {snap.asset}\n"
        f"- mid: {snap.mid:g}  mark: {snap.mark:g}\n"
        f"- 1h change: {_fmt_pct(pct_1h)}   4h change: {_fmt_pct(pct_4h)}   "
        f"24h change: {_fmt_pct(pct_24h)}\n"
        f"- last 60min move (4 × 15m, rolling): {_fmt_pct(pct_60m_rolling)}   ← late-entry check (>=0.8% = LATE per system prompt)\n"
        f"- vol: 1h={rel_1h_vol:.2f}x avg · 15m={rel_15m_vol:.2f}x avg   ← >=1.5x = surge confirming move, <=0.6x = thin/dead\n"
        f"- funding (hourly): {_fmt_pct(snap.funding_hourly)}   "
        f"OI: {snap.open_interest:g}   24h vol $: {snap.day_volume_usd:,.0f}\n"
        f"\n### {snap.asset} 15m candles (last {len(snap.candles_15m)})\n"
        f"{_candle_table(snap.candles_15m)}\n"
        f"\n### {snap.asset} 1h candles (last {len(snap.candles_1h)})\n"
        f"{_candle_table(snap.candles_1h)}\n"
        f"\n### {snap.asset} 4h candles (last {len(snap.candles_4h)})\n"
        f"{_candle_table(snap.candles_4h)}\n"
    )


def _account_block(account: AccountState) -> str:
    lines = [
        "## Account",
        f"- equity (USD): {account.equity_usd:,.2f}",
        f"- free margin (USD): {account.free_margin_usd:,.2f}",
        f"- total notional (USD): {account.total_notional_usd:,.2f}",
        f"- margin used (USD): {account.margin_used_usd:,.2f}",
    ]

    if account.positions:
        lines.append("\n### Open positions")
        lines.append("asset | side | size | entry | notional$ | uPnL$ | lev | liq")
        for p in account.positions:
            side = "long" if p.size > 0 else "short"
            liq = f"{p.liquidation_px:g}" if p.liquidation_px else "-"
            lines.append(
                f"{p.asset} | {side} | {abs(p.size):g} | {p.entry_px:g} | "
                f"{p.position_value_usd:,.2f} | {p.unrealized_pnl_usd:+,.2f} | "
                f"{p.leverage:g}x | {liq}"
            )
    else:
        lines.append("\n### Open positions: none")

    if account.open_orders:
        lines.append("\n### Open orders")
        lines.append("oid | asset | side | size | limit_px | reduce_only")
        for o in account.open_orders:
            lines.append(
                f"{o.oid} | {o.asset} | {o.side} | {o.size:g} | {o.limit_px:g} | {o.reduce_only}"
            )
    else:
        lines.append("\n### Open orders: none")

    return "\n".join(lines)


def _tree_signals_block(
    predictions: "dict[str, TreePrediction]",
) -> str:
    """Render the LightGBM tree advisor signal.

    Wired as an INFORMATIONAL side input — the prompt frames it as a
    statistical hint, not an authoritative call. The system rules in the
    SYSTEM_PROMPT (entry quality, rejection signal, correlation cap,
    invalidation thresholds) still own the decision.
    """
    lines = ["## Tree Model Signal (informational — not authoritative)"]
    # One-line context so a reader of the prompt can calibrate trust
    # without scrolling: how good is this signal vs random?
    lines.append(
        "Calibration: ~52% directional accuracy on backtest. Use as a "
        "weak Bayesian prior, not a decision. The system rules above "
        "(entry quality, rejection signal, invalidation thresholds) own "
        "the trade decision."
    )
    for p in predictions.values():
        horizon_min = p.horizon_bars * 15
        # Show p as a delta from 50% so the magnitude is immediate ("the
        # model is +3pp leaning long" reads faster than "p=0.53"). The
        # raw probability stays in the line too for transparency.
        delta_pp = (p.prob_up - 0.5) * 100
        lines.append(
            f"- {p.asset}: prob_up={p.prob_up:.3f} "
            f"({delta_pp:+.1f}pp from 50/50) → {p.predicted_direction.upper()} "
            f"over next {horizon_min}min · confidence: {p.confidence} "
            f"(model: {p.model_version})"
        )
    return "\n".join(lines)


def render_context(
    snapshot: MarketSnapshot,
    account: AccountState,
    tree_predictions: "dict[str, TreePrediction] | None" = None,
) -> str:
    ts = datetime.fromtimestamp(snapshot.timestamp_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    blocks = [f"# Market + account snapshot @ {ts}", _account_block(account), ""]
    for asset in snapshot.assets:
        blocks.append(_asset_block(snapshot.assets[asset]))
    if tree_predictions:
        blocks.append(_tree_signals_block(tree_predictions))
    return "\n".join(blocks)
