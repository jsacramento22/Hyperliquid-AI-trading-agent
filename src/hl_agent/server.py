from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import account as account_mod
from . import cost as cost_mod
from . import monitor
from . import runtime
from . import version as version_mod
from .hl_client import build_client, initialize_position_leverage
from .main import (
    _initialize_exchange,
    _safe_cycle,
    _setup_logging,
    compute_next_run_time,
)
from .settings import RiskConfig, Settings, load_settings
from .storage import Storage
from .trades import compute_trades, trades_from_user_fills

log = logging.getLogger("hl_agent")


class PauseBody(BaseModel):
    paused: bool


class RiskBody(BaseModel):
    max_leverage: float | None = Field(default=None, ge=1.0, le=50.0)
    max_position_pct_per_asset: float | None = Field(default=None, gt=0.0, le=1.0)
    max_total_notional_pct: float | None = Field(default=None, gt=0.0, le=1.0)
    daily_drawdown_kill_switch_pct: float | None = Field(default=None, gt=-1.0, le=0.0)
    min_order_usd: float | None = Field(default=None, gt=0.0)


class LeverageBody(BaseModel):
    leverage: int | None = Field(default=None, ge=1, le=50)
    is_cross: bool | None = None


def _parse_decision(row: dict) -> dict:
    return {
        "id": row["id"],
        "ts_utc": row["ts_utc"],
        "cycle_id": row["cycle_id"],
        "model": row["model"],
        "network": row["network"],
        "reasoning": row["reasoning"] or "",
        "raw_tool_calls": json.loads(row["raw_tool_calls"]),
        "executed_actions": json.loads(row["executed_actions"]),
        "rejected_actions": json.loads(row["rejected_actions"]),
    }


def _classify_decision(tool_calls: list[dict]) -> dict:
    """Reduce a cycle's tool calls to a coarse {bucket, label} pair so we can
    compare primary vs shadow side-by-side without choking on minor arg diffs.

    Buckets (mutually exclusive):
      - "hold"                  — every call is `hold`
      - "long"  / "short"       — at least one market/limit open
      - "close"                 — at least one close_position
      - "cancel"                — only cancel_all (no open/close)
      - "other"                 — anything else / mixed
    Label is a short human string for the UI.
    """
    if not tool_calls:
        return {"bucket": "hold", "label": "no-call"}

    names = [tc.get("name", "") for tc in tool_calls]
    if all(n == "hold" for n in names):
        return {"bucket": "hold", "label": "hold"}

    # First substantive (non-hold) call drives the label.
    for tc in tool_calls:
        n = tc.get("name", "")
        args = tc.get("input", {}) or {}
        asset = (args.get("asset") or args.get("coin") or "?").upper()
        side = args.get("side", "")
        if n == "place_market_order":
            bucket = "long" if side == "buy" else "short"
            return {"bucket": bucket, "label": f"market {side} {asset}"}
        if n == "place_limit_order":
            bucket = "long" if side == "buy" else "short"
            return {"bucket": bucket, "label": f"limit {side} {asset}"}
        if n == "close_position":
            return {"bucket": "close", "label": f"close {asset}"}
        if n == "cancel_all":
            return {"bucket": "cancel", "label": f"cancel {asset}"}

    return {"bucket": "other", "label": ",".join(names)}


def _parse_fill(row: dict) -> dict:
    return {
        "id": row["id"],
        "ts_utc": row["ts_utc"],
        "cycle_id": row["cycle_id"],
        "asset": row["asset"],
        "side": row["side"],
        "requested_usd": row["requested_usd"],
        "raw_response": json.loads(row["raw_response"]),
    }


def _risk_to_dict(risk: RiskConfig) -> dict[str, Any]:
    return risk.model_dump()


def create_app(
    settings: Settings | None = None,
    *,
    start_scheduler: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    _setup_logging()

    storage = Storage(settings.storage_path)
    scheduler = AsyncIOScheduler(
        timezone="UTC",
        executors={"default": APSThreadPoolExecutor(max_workers=1)},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            _initialize_exchange(settings)
            next_run = compute_next_run_time(settings)
            delay = (next_run - datetime.now(tz=timezone.utc)).total_seconds()
            scheduler.add_job(
                lambda: _safe_cycle(settings),
                "interval",
                minutes=settings.config.cadence_minutes,
                next_run_time=next_run,
                id="trade_cycle",
                max_instances=1,
                coalesce=True,
            )
            tp_on = (
                settings.config.take_profit.enabled
                and settings.config.take_profit.pct > 0
            )
            sl_on = (
                settings.config.stop_loss.enabled
                and settings.config.stop_loss.pct > 0
            )
            if tp_on or sl_on:
                interval = (
                    settings.config.take_profit.check_interval_seconds if tp_on
                    else settings.config.stop_loss.check_interval_seconds
                )
                scheduler.add_job(
                    lambda: monitor.safe_check(settings),
                    "interval",
                    seconds=interval,
                    id="auto_tp_sl_monitor",
                    max_instances=1,
                    coalesce=True,
                )
            scheduler.start()
            log.info(
                "server scheduler started: every %d min on %s, model=%s, assets=%s",
                settings.config.cadence_minutes,
                settings.config.network,
                settings.config.model,
                settings.config.assets,
            )
            if tp_on:
                log.info(
                    "auto take-profit: every %ds at +%.2f%% gain "
                    "(req=%d ticks, slippage=%.2f%%)",
                    interval,
                    settings.config.take_profit.pct * 100,
                    settings.config.take_profit.require_consecutive_checks,
                    settings.config.take_profit.close_slippage * 100,
                )
            if sl_on:
                log.info(
                    "auto stop-loss:   every %ds at -%.2f%% loss "
                    "(req=%d ticks, slippage=%.2f%%)",
                    interval,
                    settings.config.stop_loss.pct * 100,
                    settings.config.stop_loss.require_consecutive_checks,
                    settings.config.stop_loss.close_slippage * 100,
                )
            if delay > 1:
                log.info(
                    "next cycle at %s (%.0fs from now — respecting last cycle)",
                    next_run.isoformat(),
                    delay,
                )
        try:
            yield
        finally:
            if start_scheduler:
                scheduler.shutdown(wait=False)
                log.info("server scheduler stopped")

    app = FastAPI(title="hl-agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        last_err = storage.last_cycle_error(within_seconds=3600)
        return {
            "ok": True,
            "network": settings.config.network,
            "model": settings.config.model,
            "assets": settings.config.assets,
            "cadence_minutes": settings.config.cadence_minutes,
            **version_mod.status(),
            "last_error": last_err,
        }

    @app.get("/api/account")
    def account():
        client = build_client(settings)
        state = account_mod.get_state(client)
        return {
            "address": state.address,
            "equity_usd": state.equity_usd,
            "free_margin_usd": state.free_margin_usd,
            "total_notional_usd": state.total_notional_usd,
            "margin_used_usd": state.margin_used_usd,
            "positions": [asdict(p) for p in state.positions],
            "open_orders": [asdict(o) for o in state.open_orders],
        }

    @app.get("/api/equity")
    def equity(hours: int = 24):
        if hours <= 0 or hours > 24 * 30:
            raise HTTPException(400, "hours must be in (0, 720]")
        return {"hours": hours, "snapshots": storage.equity_snapshots_since(hours)}

    @app.get("/api/decisions")
    def decisions(limit: int = 50):
        if limit <= 0 or limit > 500:
            raise HTTPException(400, "limit must be in (0, 500]")
        return {"decisions": [_parse_decision(d) for d in storage.recent_decisions(limit)]}

    @app.get("/api/fills")
    def fills(limit: int = 50):
        if limit <= 0 or limit > 500:
            raise HTTPException(400, "limit must be in (0, 500]")
        return {"fills": [_parse_fill(f) for f in storage.recent_fills(limit)]}

    @app.get("/api/cost")
    def get_cost(hours: int = 24):
        if hours <= 0 or hours > 24 * 30:
            raise HTTPException(400, "hours must be in (0, 720]")
        rows = storage.token_usage_since(hours)
        agg = cost_mod.aggregate(rows)

        # Token totals
        totals = {
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
            "cache_write_5m_tokens": sum(r["cache_write_5m_tokens"] for r in rows),
            "cache_write_1h_tokens": sum(r["cache_write_1h_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
        }
        all_input = (
            totals["input_tokens"]
            + totals["cache_read_tokens"]
            + totals["cache_write_5m_tokens"]
            + totals["cache_write_1h_tokens"]
        )
        cache_hit_pct = (
            (totals["cache_read_tokens"] / all_input) if all_input > 0 else 0.0
        )

        # Per-cycle series for charting
        series = []
        for r in rows:
            c = cost_mod.cost_for_row(r)
            series.append(
                {
                    "ts_utc": r["ts_utc"],
                    "cycle_id": r["cycle_id"],
                    "model": r["model"],
                    "tokens": {
                        "input": r["input_tokens"],
                        "cache_read": r["cache_read_tokens"],
                        "cache_write_5m": r["cache_write_5m_tokens"],
                        "cache_write_1h": r["cache_write_1h_tokens"],
                        "output": r["output_tokens"],
                    },
                    "cost_usd": c.total_usd,
                }
            )

        return {
            "hours": hours,
            "cycles": len(rows),
            "tokens": totals,
            "cost": agg.to_dict(),
            "cache_hit_pct": cache_hit_pct,
            "projected_daily_usd": agg.total_usd * 24 / hours if hours > 0 else 0.0,
            "series": series,
        }

    @app.get("/api/trades")
    def trades(limit: int = 100, days: int = 30):
        """Source of truth = Hyperliquid's user_fills API (not our local fills
        table). The local table records what the bot TRIED to place; it
        misses fills that happened later — e.g. limit orders that went
        `resting` at placement and matched on the book minutes/hours later.
        Querying the exchange directly fixes that.

        `days` bounds the window; default 30d covers anything sensible for a
        bot running at 15-min cadence.
        """
        if limit <= 0 or limit > 1000:
            raise HTTPException(400, "limit must be in (0, 1000]")
        if days <= 0 or days > 365:
            raise HTTPException(400, "days must be in (0, 365]")

        try:
            client = build_client(settings)
            since_ms = int(
                (datetime.now(tz=timezone.utc).timestamp() - days * 86400) * 1000
            )
            raw = client.info.user_fills_by_time(client.account_address, since_ms)
        except Exception as e:
            log.exception("user_fills_by_time failed")
            raise HTTPException(502, f"exchange fills query failed: {e}")

        completed = trades_from_user_fills(raw or [])[:limit]

        rows = [asdict(t) for t in completed]
        total_pnl = sum(t.realized_pnl_usd for t in completed)
        wins = sum(1 for t in completed if t.realized_pnl_usd > 0)
        losses = sum(1 for t in completed if t.realized_pnl_usd < 0)
        scratch = len(completed) - wins - losses
        return {
            "trades": rows,
            "summary": {
                "count": len(completed),
                "total_realized_pnl_usd": total_pnl,
                "wins": wins,
                "losses": losses,
                "scratch": scratch,
                "win_rate": (wins / len(completed)) if completed else 0.0,
            },
        }

    @app.get("/api/shadow")
    def shadow(hours: int = 24, limit: int = 50):
        """Side-by-side primary vs shadow decisions for the A/B comparison.

        Returns:
          - enabled / model: from config
          - cycles: paired rows {cycle_id, ts_utc, primary, shadow} where
            each side has {bucket, label, reasoning}
          - agreement: how often shadow matched primary (% same bucket,
            % both-hold, % same direction)
          - cost: total shadow $ over the window + projected daily $
        """
        if hours <= 0 or hours > 24 * 30:
            raise HTTPException(400, "hours must be in (0, 720]")
        if limit <= 0 or limit > 500:
            raise HTTPException(400, "limit must be in (0, 500]")

        shadow_rows = storage.shadow_decisions_since(hours)
        # Pull a wide window of primary decisions, then build a lookup.
        primary_rows = storage.recent_decisions(max(limit * 4, len(shadow_rows) * 2 + 50))
        primary_by_cycle = {r["cycle_id"]: r for r in primary_rows}

        paired: list[dict] = []
        agreements = {"same_bucket": 0, "same_direction": 0, "both_hold": 0}
        considered = 0

        for srow in shadow_rows:
            prow = primary_by_cycle.get(srow["cycle_id"])
            if prow is None:
                # Primary row aged out of our lookup window — skip rather than
                # show a half-paired row.
                continue

            primary_calls = json.loads(prow["raw_tool_calls"])
            shadow_calls = json.loads(srow["raw_tool_calls"])
            primary_cls = _classify_decision(primary_calls)
            shadow_cls = _classify_decision(shadow_calls)

            considered += 1
            if primary_cls["bucket"] == shadow_cls["bucket"]:
                agreements["same_bucket"] += 1
                if primary_cls["bucket"] == "hold":
                    agreements["both_hold"] += 1
            # "Direction" = long/short bucket overlap, ignoring asset.
            if (
                primary_cls["bucket"] in ("long", "short")
                and primary_cls["bucket"] == shadow_cls["bucket"]
            ):
                agreements["same_direction"] += 1
            elif primary_cls["bucket"] == "hold" and shadow_cls["bucket"] == "hold":
                agreements["same_direction"] += 1

            paired.append(
                {
                    "cycle_id": srow["cycle_id"],
                    "ts_utc": srow["ts_utc"],
                    "primary": {
                        "model": prow["model"],
                        "bucket": primary_cls["bucket"],
                        "label": primary_cls["label"],
                        "reasoning": prow["reasoning"] or "",
                    },
                    "shadow": {
                        "model": srow["model"],
                        "bucket": shadow_cls["bucket"],
                        "label": shadow_cls["label"],
                        "reasoning": srow["reasoning"] or "",
                    },
                    "agree": primary_cls["bucket"] == shadow_cls["bucket"],
                }
            )

        paired = paired[:limit]

        def _pct(n: int) -> float:
            return (n / considered) if considered else 0.0

        # Shadow cost over the same window
        token_rows = storage.shadow_token_usage_since(hours)
        agg = cost_mod.aggregate(token_rows)
        projected_daily = (agg.total_usd * 24 / hours) if hours > 0 else 0.0

        return {
            "enabled": settings.config.shadow.enabled,
            "model": settings.config.shadow.model,
            "hours": hours,
            "cycles_compared": considered,
            "agreement": {
                "same_bucket_pct": _pct(agreements["same_bucket"]),
                "same_direction_pct": _pct(agreements["same_direction"]),
                "both_hold_pct": _pct(agreements["both_hold"]),
            },
            "cost": {
                "total_usd": agg.total_usd,
                "projected_daily_usd": projected_daily,
            },
            "pairs": paired,
        }

    @app.get("/api/runtime")
    def get_runtime():
        return {
            "paused": runtime.is_paused(storage),
            "risk_overrides": runtime.get_risk_overrides(storage) or {},
            "effective_risk": _risk_to_dict(runtime.effective_risk(settings, storage)),
            "base_risk": _risk_to_dict(settings.config.risk),
            "position_leverage": {
                "effective": runtime.effective_position_leverage(settings, storage),
                "base": settings.config.position_leverage,
                "override": runtime.get_position_leverage_override(storage),
            },
            "position_margin_cross": {
                "effective": runtime.effective_position_margin_cross(settings, storage),
                "base": settings.config.position_margin_cross,
                "override": runtime.get_position_margin_cross_override(storage),
            },
        }

    @app.post("/api/pause")
    def post_pause(body: PauseBody):
        runtime.set_paused(storage, body.paused)
        return {"paused": runtime.is_paused(storage)}

    @app.post("/api/leverage")
    def post_leverage(body: LeverageBody):
        if body.leverage is None and body.is_cross is None:
            raise HTTPException(400, "must set at least one of leverage, is_cross")
        if body.leverage is not None:
            runtime.set_position_leverage_override(storage, body.leverage)
        if body.is_cross is not None:
            runtime.set_position_margin_cross_override(storage, body.is_cross)
        leverage = runtime.effective_position_leverage(settings, storage)
        is_cross = runtime.effective_position_margin_cross(settings, storage)
        try:
            client = build_client(settings)
        except RuntimeError as e:
            raise HTTPException(500, f"client unavailable: {e}")
        statuses = initialize_position_leverage(
            client, settings.config.assets, leverage, is_cross=is_cross
        )
        return {
            "leverage": leverage,
            "is_cross": is_cross,
            "per_asset": statuses,
        }

    @app.post("/api/risk")
    def post_risk(body: RiskBody):
        overrides = {k: v for k, v in body.model_dump().items() if v is not None}
        # Sanity: total cap should not be smaller than per-asset cap.
        merged_base = settings.config.risk.model_dump()
        merged = {**merged_base, **(runtime.get_risk_overrides(storage) or {}), **overrides}
        if merged["max_total_notional_pct"] < merged["max_position_pct_per_asset"]:
            raise HTTPException(
                400,
                "max_total_notional_pct must be >= max_position_pct_per_asset",
            )
        runtime.set_risk_overrides(
            storage,
            {**(runtime.get_risk_overrides(storage) or {}), **overrides},
        )
        return {
            "risk_overrides": runtime.get_risk_overrides(storage) or {},
            "effective_risk": _risk_to_dict(runtime.effective_risk(settings, storage)),
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "hl_agent.server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
