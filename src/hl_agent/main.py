from __future__ import annotations

import logging
import signal
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic
from apscheduler.schedulers.blocking import BlockingScheduler

from . import account as account_mod
from . import market_data
from . import monitor
from . import runtime
from .agent import run_cycle
from .executor import Executor
from .hl_client import build_client, initialize_position_leverage
from .settings import Settings, load_settings
from .storage import Storage

log = logging.getLogger("hl_agent")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_one_cycle(settings: Settings, *, dry_run: bool = False) -> None:
    cycle_id = f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    storage = Storage(settings.storage_path)

    if runtime.is_paused(storage):
        log.info("cycle %s skipped — agent is paused", cycle_id)
        return

    log.info("cycle %s start (dry_run=%s)", cycle_id, dry_run)

    client = build_client(settings)

    snapshot = market_data.get_snapshot(
        client,
        settings.config.assets,
        candles_15m=settings.config.market_data.candles_15m,
        candles_1h=settings.config.market_data.candles_1h,
        candles_4h=settings.config.market_data.candles_4h,
    )
    account = account_mod.get_state(client)
    storage.log_equity(cycle_id=cycle_id, account=account)

    starting_equity = storage.starting_equity_today() or account.equity_usd
    risk_config = runtime.effective_risk(settings, storage)

    executor = Executor(
        client=client,
        storage=storage,
        risk_config=risk_config,
        allowed_assets=settings.config.assets,
        cycle_id=cycle_id,
        dry_run=dry_run,
    )

    anthropic = Anthropic(api_key=settings.secrets.anthropic_api_key)
    result = run_cycle(
        anthropic=anthropic,
        model=settings.config.model,
        network=settings.config.network,
        allowed_assets=settings.config.assets,
        snapshot=snapshot,
        account=account,
        starting_equity_usd=starting_equity,
        executor=executor,
    )

    executed = [asdict(a) for a in result.actions if a.accepted]
    rejected = [asdict(a) for a in result.actions if not a.accepted]
    storage.log_decision(
        cycle_id=cycle_id,
        model=settings.config.model,
        network=settings.config.network,
        reasoning=result.reasoning,
        raw_tool_calls=result.raw_tool_calls,
        executed_actions=executed,
        rejected_actions=rejected,
    )

    u = result.usage
    if u:
        cw_5m = u.get("cache_write_5m_input_tokens", 0)
        cw_1h = u.get("cache_write_1h_input_tokens", 0)
        # If the per-tier split isn't reported, attribute the total to 5m
        # (default TTL when no `ttl` is requested).
        cw_total = u.get("cache_creation_input_tokens", 0)
        if cw_5m + cw_1h == 0 and cw_total > 0:
            cw_5m = cw_total
        storage.log_token_usage(
            cycle_id=cycle_id,
            model=settings.config.model,
            input_tokens=u.get("input_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_write_5m_tokens=cw_5m,
            cache_write_1h_tokens=cw_1h,
            output_tokens=u.get("output_tokens", 0),
        )

    log.info(
        "cycle %s done — equity=%.2f executed=%d rejected=%d",
        cycle_id,
        account.equity_usd,
        len(executed),
        len(rejected),
    )
    if result.reasoning:
        log.info("reasoning: %s", result.reasoning)
    for a in result.actions:
        verdict = "OK" if a.accepted else "REJECT"
        log.info("  %s %s args=%s reason=%s", verdict, a.tool, a.args, a.reason)


def compute_next_run_time(settings: Settings) -> datetime:
    """Decide when the scheduler's first cycle should fire on startup.

    If the most recent logged decision is younger than the cadence, wait the
    remainder so we don't fire a cycle right after a restart. Otherwise (no
    prior cycles or the last one was more than cadence ago) fire immediately.
    """
    storage = Storage(settings.storage_path)
    now = datetime.now(tz=timezone.utc)
    last = storage.last_cycle_ts_utc()
    if not last:
        return now
    last_dt = datetime.fromisoformat(last)
    next_dt = last_dt + timedelta(minutes=settings.config.cadence_minutes)
    return next_dt if next_dt > now else now


def _initialize_exchange(settings: Settings) -> None:
    """One-time exchange-side setup that must happen before cycles start —
    currently just the per-asset leverage. Safe to call repeatedly. Honors
    any runtime overrides so UI changes survive a restart."""
    try:
        client = build_client(settings)
    except RuntimeError as e:
        log.warning("skipping leverage init: %s", e)
        return
    storage = Storage(settings.storage_path)
    leverage = runtime.effective_position_leverage(settings, storage)
    is_cross = runtime.effective_position_margin_cross(settings, storage)
    statuses = initialize_position_leverage(
        client,
        settings.config.assets,
        leverage,
        is_cross=is_cross,
    )
    log.info(
        "position leverage set to %dx (cross=%s): %s",
        leverage,
        is_cross,
        statuses,
    )


def main() -> None:
    _setup_logging()
    settings = load_settings()
    _initialize_exchange(settings)

    next_run = compute_next_run_time(settings)
    delay = (next_run - datetime.now(tz=timezone.utc)).total_seconds()
    if delay > 1:
        log.info(
            "next cycle scheduled at %s (%.0fs from now — respecting last cycle)",
            next_run.isoformat(),
            delay,
        )
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        lambda: _safe_cycle(settings),
        "interval",
        minutes=settings.config.cadence_minutes,
        next_run_time=next_run,
        id="trade_cycle",
        max_instances=1,
        coalesce=True,
    )
    tp_on = settings.config.take_profit.enabled and settings.config.take_profit.pct > 0
    sl_on = settings.config.stop_loss.enabled and settings.config.stop_loss.pct > 0
    if tp_on or sl_on:
        # Both share the same monitor tick; use TP's interval as the cadence
        # (or SL's if TP is disabled).
        interval = (
            settings.config.take_profit.check_interval_seconds if tp_on
            else settings.config.stop_loss.check_interval_seconds
        )
        sched.add_job(
            lambda: monitor.safe_check(settings),
            "interval",
            seconds=interval,
            id="auto_tp_sl_monitor",
            max_instances=1,
            coalesce=True,
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

    def _shutdown(signum, _frame):
        log.info("signal %s received — shutting scheduler down", signum)
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "starting scheduler: every %d min on %s, model=%s, assets=%s",
        settings.config.cadence_minutes,
        settings.config.network,
        settings.config.model,
        settings.config.assets,
    )
    sched.start()


def _safe_cycle(settings: Settings) -> None:
    try:
        run_one_cycle(settings)
    except Exception as e:
        log.exception("cycle raised — continuing scheduler")
        # Persist the failure so the dashboard can surface it.
        try:
            Storage(settings.storage_path).log_cycle_error(
                cycle_id=None,
                component="agent_cycle",
                error_type=type(e).__name__,
                error_message=str(e),
            )
        except Exception:
            log.exception("also failed to record cycle error")


if __name__ == "__main__":
    main()
