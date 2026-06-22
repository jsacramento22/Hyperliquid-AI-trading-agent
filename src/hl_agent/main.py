from __future__ import annotations

import logging
import signal
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from . import account as account_mod
from . import market_data
from . import monitor
from . import runtime
from . import tree_outcomes
from .agent import run_cycle
from .executor import Executor
from .hl_client import build_client, initialize_position_leverage
from .llm_provider import build_provider
from .settings import Settings, load_settings
from .storage import Storage
from .tree_model import TreePredictor, try_build_predictor

log = logging.getLogger("hl_agent")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_one_cycle(
    settings: Settings,
    *,
    dry_run: bool = False,
    tree_predictor: TreePredictor | None = None,
) -> None:
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

    # Score any tree predictions whose horizon has elapsed. Best-effort —
    # the snapshot's candles_15m carries enough history to catch anything
    # made in the last few cycles. Failures here are non-fatal.
    if tree_predictor is not None:
        try:
            tree_outcomes.backfill_from_snapshot(storage, snapshot)
        except Exception:
            log.exception("tree_outcomes.backfill_from_snapshot failed — continuing")

    starting_equity = storage.starting_equity_today() or account.equity_usd
    risk_config = runtime.effective_risk(settings, storage)
    model = runtime.effective_model(settings, storage)
    provider_name = runtime.effective_provider(settings, storage)

    executor = Executor(
        client=client,
        storage=storage,
        risk_config=risk_config,
        allowed_assets=settings.config.assets,
        cycle_id=cycle_id,
        dry_run=dry_run,
    )

    provider = build_provider(
        provider=provider_name,
        anthropic_api_key=settings.secrets.anthropic_api_key,
        openrouter_api_key=settings.secrets.openrouter_api_key,
    )
    result = run_cycle(
        provider=provider,
        model=model,
        network=settings.config.network,
        allowed_assets=settings.config.assets,
        snapshot=snapshot,
        account=account,
        starting_equity_usd=starting_equity,
        executor=executor,
        tree_predictor=tree_predictor,
        storage=storage,
        cycle_id=cycle_id,
    )

    executed = [asdict(a) for a in result.actions if a.accepted]
    rejected = [asdict(a) for a in result.actions if not a.accepted]
    storage.log_decision(
        cycle_id=cycle_id,
        model=model,
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
            model=model,
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

    # Built once at startup so the in-memory rolling state (funding history,
    # OI history) survives across cycles. Returns None when the model
    # files aren't on disk → bot runs in pre-Phase-2 LLM-only mode.
    tree_predictor = try_build_predictor(model_dir=Path("data/models"))

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
        lambda: _safe_cycle(settings, tree_predictor=tree_predictor),
        "interval",
        minutes=settings.config.cadence_minutes,
        next_run_time=next_run,
        id="trade_cycle",
        max_instances=1,
        coalesce=True,
    )
    # Always register the monitor job (gated only on cadence > 0). Whether
    # TP/SL actually fire is decided per-tick by check_and_close reading
    # the effective config — so flipping the YAML enabled flags or the UI
    # toggles takes effect without a restart. If both YAML flags are off
    # the tick does ~1ms of DB reads and returns; harmless.
    interval = (
        settings.config.take_profit.check_interval_seconds
        or settings.config.stop_loss.check_interval_seconds
    )
    if interval > 0:
        sched.add_job(
            lambda: monitor.safe_check(settings),
            "interval",
            seconds=interval,
            id="auto_tp_sl_monitor",
            max_instances=1,
            coalesce=True,
        )
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

    def _shutdown(signum, _frame):
        log.info("signal %s received — shutting scheduler down", signum)
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    storage = Storage(settings.storage_path)
    log.info(
        "starting scheduler: every %d min on %s, model=%s via %s, assets=%s",
        settings.config.cadence_minutes,
        settings.config.network,
        runtime.effective_model(settings, storage),
        runtime.effective_provider(settings, storage),
        settings.config.assets,
    )
    sched.start()


def _safe_cycle(
    settings: Settings,
    *,
    tree_predictor: TreePredictor | None = None,
) -> None:
    try:
        run_one_cycle(settings, tree_predictor=tree_predictor)
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
