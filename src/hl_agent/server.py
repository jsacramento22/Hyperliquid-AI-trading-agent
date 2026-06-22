from __future__ import annotations

import json
import logging
import uuid
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


class ModelBody(BaseModel):
    # Validated against runtime.SUPPORTED_MODELS in the handler; pydantic
    # only enforces shape here (must be a non-empty string).
    model: str = Field(min_length=1)


class MonitorBody(BaseModel):
    """Live-edit take-profit and stop-loss. All fields optional — only the
    ones provided are applied. `pct` is the magnitude of the move (e.g.
    0.015 = 1.5%). pct=0 effectively pauses that side, but the `enabled`
    flag is the cleaner switch."""
    tp_enabled: bool | None = None
    tp_pct: float | None = Field(default=None, gt=0.0, lt=1.0)
    sl_enabled: bool | None = None
    sl_pct: float | None = Field(default=None, gt=0.0, lt=1.0)


class ClosePositionBody(BaseModel):
    """Manually flatten one position via market_close. Slippage default 0.5%
    matches the auto take-profit / stop-loss monitor. Asset is uppercased
    server-side and validated against the allowed-assets list before any
    exchange call."""
    asset: str = Field(min_length=1, max_length=10)
    slippage: float = Field(default=0.005, gt=0.0, lt=0.5)


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
            # Always register the monitor job so the UI toggle can re-enable
            # TP/SL without a server restart. Per-tick gating in
            # monitor.check_and_close handles the disabled case.
            interval = (
                settings.config.take_profit.check_interval_seconds
                or settings.config.stop_loss.check_interval_seconds
            )
            if interval > 0:
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
                "server scheduler started: every %d min on %s, model=%s via %s, assets=%s",
                settings.config.cadence_minutes,
                settings.config.network,
                runtime.effective_model(settings, storage),
                runtime.effective_provider(settings, storage),
                settings.config.assets,
            )
            if interval > 0:
                tp = settings.config.take_profit
                sl = settings.config.stop_loss
                log.info(
                    "auto take-profit: every %ds, YAML %s at +%.2f%% (live-editable)",
                    interval,
                    "ON" if tp.enabled and tp.pct > 0 else "OFF",
                    tp.pct * 100,
                )
                log.info(
                    "auto stop-loss:   every %ds, YAML %s at -%.2f%% (live-editable)",
                    interval,
                    "ON" if sl.enabled and sl.pct > 0 else "OFF",
                    sl.pct * 100,
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
            "model": runtime.effective_model(settings, storage),
            "provider": runtime.effective_provider(settings, storage),
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

    @app.get("/api/tree")
    def tree(hours: int = 168, history_limit: int = 30):
        """Phase 3: LightGBM tree advisor signal + outcome accuracy.

        Returns the latest prediction per asset, rolling accuracy over
        the last `hours` (default 7 days), and a list of the most recent
        `history_limit` predictions for the history table. The frontend
        renders this as a Mode-A panel — informational only, never used
        for live trading decisions."""
        if hours <= 0 or hours > 24 * 90:
            raise HTTPException(400, "hours must be in (0, 2160]")
        if history_limit <= 0 or history_limit > 500:
            raise HTTPException(400, "history_limit must be in (0, 500]")

        latest = storage.latest_tree_prediction_per_asset()
        rolling = storage.tree_accuracy_summary(hours=hours)
        # Per-window breakdown lets the panel surface "is the model
        # drifting?" without the user picking a window manually — short
        # windows are noisier but flag fresh problems faster.
        windows = {
            f"{w}h": storage.tree_accuracy_summary(hours=w)
            for w in (24, 72, 168)
        }
        history = storage.recent_tree_predictions(limit=history_limit)
        return {
            "hours": hours,
            "latest": latest,
            "rolling": rolling,
            "windows": windows,
            "history": history,
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

    @app.get("/api/runtime")
    def get_runtime():
        eff_tp = runtime.effective_take_profit(settings, storage)
        eff_sl = runtime.effective_stop_loss(settings, storage)
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
            "model": {
                "effective": runtime.effective_model(settings, storage),
                "base": settings.config.model,
                "override": runtime.get_model_override(storage),
                "provider": runtime.effective_provider(settings, storage),
                "base_provider": settings.config.model_provider,
                "supported": runtime.SUPPORTED_MODELS,  # {model: provider} map
            },
            "take_profit": {
                "effective": {
                    "enabled": eff_tp.enabled,
                    "pct": eff_tp.pct,
                },
                "base": {
                    "enabled": settings.config.take_profit.enabled,
                    "pct": settings.config.take_profit.pct,
                },
                "overrides": runtime.get_tp_overrides(storage),
            },
            "stop_loss": {
                "effective": {
                    "enabled": eff_sl.enabled,
                    "pct": eff_sl.pct,
                },
                "base": {
                    "enabled": settings.config.stop_loss.enabled,
                    "pct": settings.config.stop_loss.pct,
                },
                "overrides": runtime.get_sl_overrides(storage),
            },
        }

    @app.post("/api/pause")
    def post_pause(body: PauseBody):
        runtime.set_paused(storage, body.paused)
        return {"paused": runtime.is_paused(storage)}

    @app.post("/api/model")
    def post_model(body: ModelBody):
        """Live-switch which LLM the next cycle uses. Takes effect on the
        next scheduled cycle (no restart needed). The provider is derived
        from the model lookup in SUPPORTED_MODELS — selecting a DeepSeek
        model automatically routes via OpenRouter, etc.

        Switching invalidates any prompt cache for one cycle (cache keys
        include the model name) — expect a small one-time cost bump on
        the cycle after a change."""
        try:
            runtime.set_model_override(storage, body.model)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "model": runtime.effective_model(settings, storage),
            "provider": runtime.effective_provider(settings, storage),
            "override": runtime.get_model_override(storage),
            "base": settings.config.model,
        }

    @app.post("/api/close_position")
    def post_close_position(body: ClosePositionBody):
        """Manually flatten a position via market_close. Logged to the
        decisions table with model='manual-close' so it shows up in the
        dashboard's decision log alongside agent and auto-close actions.

        Returns 400 for unknown asset, 404 when no open position exists for
        the asset, 502 when the exchange call itself fails."""
        asset = body.asset.upper()
        if asset not in settings.config.assets:
            raise HTTPException(
                400, f"asset {asset!r} not in allowed list {settings.config.assets}"
            )

        try:
            client = build_client(settings)
        except RuntimeError as e:
            raise HTTPException(500, f"client unavailable: {e}")

        state = account_mod.get_state(client)
        pos = next(
            (p for p in state.positions if p.asset == asset and p.size != 0),
            None,
        )
        if pos is None:
            raise HTTPException(404, f"no open position for {asset}")

        side = "long" if pos.size > 0 else "short"
        cycle_id = (
            f"MANUAL-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:6]}"
        )

        try:
            resp = client.exchange.market_close(
                coin=asset, slippage=body.slippage
            )
        except Exception as e:
            log.exception("manual close failed for %s", asset)
            raise HTTPException(502, f"market_close failed: {e}")

        storage.log_fill(
            cycle_id=cycle_id,
            asset=asset,
            side="close",
            requested_usd=None,
            raw_response=resp,
        )
        action_record = {
            "tool": "close_position",
            "args": {"asset": asset, "reason": "manual"},
            "accepted": True,
            "reason": (
                f"Manual close via UI ({side}, uPnL ${pos.unrealized_pnl_usd:.2f}, "
                f"slippage {body.slippage * 100:.2f}%)"
            ),
            "response": resp,
        }
        storage.log_decision(
            cycle_id=cycle_id,
            model="manual-close",
            network=settings.config.network,
            reasoning=(
                f"Manually closed {asset} {side} via dashboard. "
                f"uPnL at close: ${pos.unrealized_pnl_usd:.2f}."
            ),
            raw_tool_calls=[
                {
                    "name": "close_position",
                    "input": {"asset": asset, "reason": "manual"},
                }
            ],
            executed_actions=[action_record],
            rejected_actions=[],
        )

        log.info(
            "manual close: %s %s (uPnL $%.2f) — cycle %s",
            asset,
            side,
            pos.unrealized_pnl_usd,
            cycle_id,
        )

        return {
            "asset": asset,
            "side": side,
            "size": abs(pos.size),
            "upnl_usd": pos.unrealized_pnl_usd,
            "cycle_id": cycle_id,
            "response": resp,
        }

    @app.post("/api/monitor")
    def post_monitor(body: MonitorBody):
        """Live-edit the take-profit / stop-loss monitor. Each side has an
        independent enabled flag and pct threshold. Changes take effect on
        the next 60s monitor tick, no restart needed.

        Either side can be flipped off without affecting the other — pausing
        only TP leaves SL armed and vice versa."""
        tp_patch = {}
        if body.tp_enabled is not None:
            tp_patch["enabled"] = body.tp_enabled
        if body.tp_pct is not None:
            tp_patch["pct"] = body.tp_pct
        if tp_patch:
            runtime.set_tp_overrides(storage, tp_patch)

        sl_patch = {}
        if body.sl_enabled is not None:
            sl_patch["enabled"] = body.sl_enabled
        if body.sl_pct is not None:
            sl_patch["pct"] = body.sl_pct
        if sl_patch:
            runtime.set_sl_overrides(storage, sl_patch)

        eff_tp = runtime.effective_take_profit(settings, storage)
        eff_sl = runtime.effective_stop_loss(settings, storage)
        return {
            "take_profit": {
                "effective": {"enabled": eff_tp.enabled, "pct": eff_tp.pct},
                "overrides": runtime.get_tp_overrides(storage),
            },
            "stop_loss": {
                "effective": {"enabled": eff_sl.enabled, "pct": eff_sl.pct},
                "overrides": runtime.get_sl_overrides(storage),
            },
        }

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
