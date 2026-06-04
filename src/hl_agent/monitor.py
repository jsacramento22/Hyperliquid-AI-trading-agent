"""Fast take-profit / stop-loss monitor that runs between agent cycles.

Polls live account state on a fixed interval (default 60s) and force-closes
any position whose unrealized PnL crosses a threshold:
  - take-profit: uPnL >= +pct of entry notional
  - stop-loss:   uPnL <= -pct of entry notional

Both are deterministic, no LLM call, no scheduler latency to the next
15-minute agent tick.

Auto-closes are logged to:
  - `fills` (so they appear in the trade history)
  - `decisions` with model="auto-take-profit" / "auto-stop-loss" (so they're
    visible in the UI decisions panel and don't surprise anyone)

The streak / require_consecutive_checks logic filters out mark-price spikes
that resolve before the actual close can execute.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from . import account as account_mod
from . import runtime
from .account import Position
from .hl_client import build_client
from .settings import Settings
from .storage import Storage

log = logging.getLogger("hl_agent.monitor")

Kind = Literal["tp", "sl"]

# Per-(kind, asset) count of consecutive monitor ticks where the threshold
# was breached. Reset to 0 on any tick that doesn't breach, or when the
# position vanishes. Lives in the process; restart resets all counts.
_streak: dict[tuple[Kind, str], int] = {}


@dataclass
class CloseResult:
    asset: str
    kind: Kind                 # "tp" | "sl"
    side: str                  # "long" | "short"
    upnl_pct: float
    upnl_usd: float
    response: object | None    # raw exchange response (None if dry-run)


# Backwards-compat alias for the older test imports / external callers.
TPCloseResult = CloseResult


def _upnl_pct_vs_entry(p: Position) -> float:
    """Profit as a fraction of entry-priced notional. Positive = winning."""
    entry_notional = abs(p.size) * p.entry_px
    if entry_notional <= 0:
        return 0.0
    return p.unrealized_pnl_usd / entry_notional


def _breached(pct: float, kind: Kind, threshold: float) -> bool:
    return pct >= threshold if kind == "tp" else pct <= -threshold


def _model_label(kind: Kind) -> str:
    return "auto-take-profit" if kind == "tp" else "auto-stop-loss"


def _cycle_prefix(kind: Kind) -> str:
    return "AUTOTP" if kind == "tp" else "AUTOSL"


def reset_streaks() -> None:
    """Test hook: clear consecutive-check state."""
    _streak.clear()


def _check_one(
    *,
    kind: Kind,
    cfg,
    p: Position,
    client,
    storage: Storage,
    settings: Settings,
    dry_run: bool,
) -> CloseResult | None:
    """Evaluate one threshold for one position. Returns a CloseResult only if
    we actually closed (or would have, in dry-run). Returns None for a no-op
    or pending streak."""
    pct = _upnl_pct_vs_entry(p)
    side = "long" if p.size > 0 else "short"
    key: tuple[Kind, str] = (kind, p.asset)
    label = _model_label(kind)
    required = max(1, int(cfg.require_consecutive_checks))

    if not _breached(pct, kind, cfg.pct):
        if _streak.pop(key, 0):
            log.debug(
                "%s streak reset on %s (uPnL %+.2f%%, threshold %.2f%%)",
                label, p.asset, pct * 100, cfg.pct * 100,
            )
        return None

    _streak[key] = _streak.get(key, 0) + 1
    streak = _streak[key]

    if streak < required:
        log.info(
            "%s pending on %s %s: uPnL=$%.2f (%+.2f%%), streak %d/%d",
            label, p.asset, side, p.unrealized_pnl_usd, pct * 100,
            streak, required,
        )
        return None

    log.info(
        "%s FIRING on %s %s: uPnL=$%.2f (%+.2f%%, %d consecutive ticks)",
        label, p.asset, side, p.unrealized_pnl_usd, pct * 100, streak,
    )

    if dry_run:
        _streak.pop(key, None)
        return CloseResult(
            asset=p.asset, kind=kind, side=side,
            upnl_pct=pct, upnl_usd=p.unrealized_pnl_usd, response=None,
        )

    try:
        resp = client.exchange.market_close(
            coin=p.asset, slippage=cfg.close_slippage
        )
    except Exception as e:
        log.exception("%s market_close failed for %s: %s", label, p.asset, e)
        # Keep the streak so a recovered exchange call resolves naturally.
        return None

    cycle_id = (
        f"{_cycle_prefix(kind)}-"
        f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    storage.log_fill(
        cycle_id=cycle_id, asset=p.asset, side="close",
        requested_usd=None, raw_response=resp,
    )
    action_record = {
        "tool": "close_position",
        "args": {"asset": p.asset, "reason": label},
        "accepted": True,
        "reason": (
            f"{label}: uPnL {pct * 100:+.2f}% "
            f"crossed {('+' if kind == 'tp' else '-')}{cfg.pct * 100:.2f}% threshold"
        ),
        "response": resp,
    }
    storage.log_decision(
        cycle_id=cycle_id,
        model=label,
        network=settings.config.network,
        reasoning=(
            f"Position {p.asset} ({side}) hit {pct * 100:+.2f}%; "
            f"closed by deterministic monitor "
            f"(threshold {('+' if kind == 'tp' else '-')}{cfg.pct * 100:.2f}%)."
        ),
        raw_tool_calls=[
            {
                "name": "close_position",
                "input": {"asset": p.asset, "reason": label},
            }
        ],
        executed_actions=[action_record],
        rejected_actions=[],
    )
    _streak.pop(key, None)
    return CloseResult(
        asset=p.asset, kind=kind, side=side,
        upnl_pct=pct, upnl_usd=p.unrealized_pnl_usd, response=resp,
    )


def check_and_close(
    settings: Settings, *, dry_run: bool = False
) -> list[CloseResult]:
    """One monitor pass: fetch account state, close any position whose uPnL
    has crossed the take-profit or stop-loss threshold for the configured
    number of consecutive ticks.

    Read TP/SL config through runtime so UI toggles (enabled / pct) take
    effect on the next tick without needing a restart. The scheduler
    interval (`check_interval_seconds`) still comes from the YAML config
    at boot time since it's wired into the scheduler job."""
    storage = Storage(settings.storage_path)
    if runtime.is_paused(storage):
        return []

    cfg_tp = runtime.effective_take_profit(settings, storage)
    cfg_sl = runtime.effective_stop_loss(settings, storage)

    tp_active = cfg_tp.enabled and cfg_tp.pct > 0
    sl_active = cfg_sl.enabled and cfg_sl.pct > 0
    if not (tp_active or sl_active):
        return []

    client = build_client(settings)
    state = account_mod.get_state(client)

    # Drop streak state for any asset that no longer has an open position.
    open_assets = {p.asset for p in state.positions if p.size != 0}
    for k in list(_streak.keys()):
        if k[1] not in open_assets:
            _streak.pop(k, None)

    closes: list[CloseResult] = []
    for p in state.positions:
        if p.size == 0:
            continue

        # Take-profit first; if it fires, the position is gone — skip SL.
        if tp_active:
            r = _check_one(
                kind="tp", cfg=cfg_tp, p=p,
                client=client, storage=storage, settings=settings,
                dry_run=dry_run,
            )
            if r is not None:
                closes.append(r)
                continue

        if sl_active:
            r = _check_one(
                kind="sl", cfg=cfg_sl, p=p,
                client=client, storage=storage, settings=settings,
                dry_run=dry_run,
            )
            if r is not None:
                closes.append(r)

    return closes


def safe_check(settings: Settings) -> None:
    """Wrapper for the scheduler — never raises."""
    try:
        check_and_close(settings)
    except Exception as e:
        log.exception("monitor cycle raised — continuing")
        try:
            Storage(settings.storage_path).log_cycle_error(
                cycle_id=None,
                component="tp_monitor",
                error_type=type(e).__name__,
                error_message=str(e),
            )
        except Exception:
            log.exception("also failed to record tp_monitor error")
