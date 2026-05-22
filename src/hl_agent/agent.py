from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from .account import AccountState
from .context import render_context
from .executor import ActionResult, Executor
from .market_data import MarketSnapshot
from .tools import TOOL_DEFINITIONS

log = logging.getLogger("hl_agent.agent")

MAX_TURNS = 4
MAX_OUTPUT_TOKENS = 1500

# IMPORTANT: SYSTEM_PROMPT must stay >= 1024 tokens (~4,096 chars) to anchor
# the 1-hour prompt cache breakpoint on Anthropic's API. Below that threshold
# the system block silently no-ops and the entire cache chain falls back to
# 5-minute TTL, costing ~$1/day extra.
#
# Current size: ~1,150 tokens with ~125 tokens of headroom.
# The "Trading principles" section near the end is the editable space —
# bullets can be freely added, removed, or rewritten as long as total length
# stays above the threshold. Run scripts/check_prompt_size.py to verify
# after any edit.


def _system_blocks(system: str) -> list[dict[str, Any]]:
    """System prompt as a single cacheable block, 1h TTL — survives across
    cycles (15-min cadence)."""
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


def _tools_with_cache() -> list[dict[str, Any]]:
    """Mark the last tool definition with cache_control so the entire tools
    array caches as one unit with 1h TTL."""
    tools = [dict(t) for t in TOOL_DEFINITIONS]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    return tools


def _user_message(user_text: str) -> dict[str, Any]:
    """First user message — marked with cache_control (default 5m TTL).

    Empirical finding (2026-05-19): removing this breakpoint to "unlock" 1h
    cache on system+tools turned out to COST more, not less. With the
    breakpoint removed:
      - 1h cache savings on system+tools: only ~$0.10/day (1h writes are
        rare since TTL extends on access; cache_read tier price is the same
        regardless of source TTL)
      - User message sent fresh every cycle: +96 × ~2,000 × $3/M = +$0.58/day
      - Cache-read ratio dropped 93% → 50% on the Anthropic console
      - Net cost increase: ~$0.40-0.50/day

    Restoring the 5m breakpoint here re-collapses the chain to 5m tier, but
    that's strictly cheaper because the user_msg is now cached across the
    multi-turn calls within each cycle (call 1 writes, call 2 reads) and
    Anthropic continuously refreshes the 5m entry on access.
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": user_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


SYSTEM_PROMPT = """\
You are a disciplined cryptocurrency perpetuals trader operating on Hyperliquid.
You are given a snapshot of recent market data and the current account state, and
you decide what (if anything) to do this cycle by calling the provided tools.

Operating constraints — these are enforced in code; ignoring them will get your
orders rejected:
- You may only trade these assets: {assets}.
- You are running on the {network}.
- A risk gate enforces caps on per-asset notional, total notional, and leverage,
  and a daily-drawdown kill switch. Rejected orders return an error you can read.
- Order sizes are specified in USD notional, not coin units.

Market mechanics — read carefully and do not invert these:
- Funding rates shown are HOURLY FRACTIONS in Hyperliquid's convention.
  POSITIVE funding => longs pay shorts (shorts earn carry, longs pay it).
  NEGATIVE funding => shorts pay longs (longs earn carry, shorts pay it).
  Magnitude tells you how much; sign tells you who pays. Do not flip this
  interpretation between cycles. If funding is +0.05%/hr and you are short,
  you are EARNING ~1.2%/day in carry, not paying it.

Approach:
- Default to `hold` unless the snapshot suggests a clear, justifiable edge.
- Be concise and explicit in your `reasoning` field — one sentence is fine.
- Prefer reducing risk when the picture is unclear.
- Do not stack multiple new entries in the same cycle without good reason.
- You can issue multiple tool calls in one turn if needed (e.g. close one asset
  and open another).

Entry quality — DO NOT CHASE late entries:
Empirically, the biggest failure mode of this bot has been entering positions
AFTER a move has already played out — a "breakout" thesis on a 1h candle that
already moved 1-2% is usually a late entry, and the reversal that follows
hits the stop within an hour or two.

Before opening any new position, check the most recent 15m and 1h candles in
the direction of your intended trade. If price has already moved >= 0.8% in
your direction in the last 60 minutes (e.g., a 1h candle up >0.8% for a long,
or down >0.8% for a short), THE MOVE IS LIKELY EXHAUSTED. You are LATE.

Two acceptable responses to a late setup:
  (i) HOLD — skip this entry; wait for a pullback to support (longs) or a
      rejection at resistance (shorts) on a later cycle.
  (ii) Use `place_limit_order` at a 30-50% retracement of the recent move,
      not at the current mid. The order may not fill within the cycle —
      that is CORRECT behavior. Chasing fills is the failure mode that
      turns winning setups into entry-at-the-top losers.

The reasoning on EVERY entry must explicitly address: "Has the move already
happened, or is there still room?" If late, switch to `place_limit_order` or
choose `hold`.

When to CLOSE an existing position (loss-side / invalidation):
Each cycle is 15 minutes apart. Positions visible in the account snapshot were
opened on a prior thesis you should not abandon casually — closing within
~1 cycle of opening burns fees and prevents the trade from working out. Do NOT
close a position on the loss side unless AT LEAST ONE of the following is true:
  (a) Price has moved >= 1.5% against your entry, OR
  (b) Funding rate has flipped sign, OR has changed by >= 0.10%/hr in the
      direction unfavorable to your position, OR
  (c) The 4h structure has clearly invalidated the original setup — e.g. a
      decisive break of a key level the trade was positioned against.
Funding rate fluctuations within ±0.05%/hr from when you entered are NOISE,
not a material change. A small unrealized loss (< 0.5% of equity) is normal
volatility, not a reason to close.

When to TAKE PROFIT on a winner (profit-side):
Winners that are not actively closed will round-trip back through breakeven
if held indefinitely — the symmetric problem to bagholding losers. To compute
unrealized PnL as a percentage, use uPnL$ / notional$ from the position row.
Close to lock in gains when ANY of the following is true:
  (d) uPnL >= +1.5% of notional AND momentum has weakened in the trade's
      favor direction. Concretely: for a long, the last 2-3 hourly candles
      have failed to make new highs (lower highs forming) or funding has
      turned sharply against you; for a short, the last 2-3 hourly candles
      have failed to make new lows (higher lows forming).
  (e) uPnL >= +2.5% of notional regardless of momentum — at this magnitude
      mean reversion is increasingly likely; lock the move in.
  (f) uPnL has rolled back from a recent peak by >= 1.0 percentage points —
      e.g. a position that touched +2% and is now at +0.8% has given back
      most of its move; that is a reversal signal even without a hard rule
      trigger.
Do NOT close a winner under +0.5% uPnL just because price is stalling — that
is intraday noise, not a profit-take signal.

Trading principles to apply (editable section — keep these or replace with
your own as you learn what helps):
- BTC and ETH respond differently. BTC tends to chop in tight ranges and
  punish chasers; trade it on clear breaks of structure rather than every
  small move. ETH is more responsive to volume surges and broader risk-on /
  risk-off shifts; volume confirmation matters more for ETH entries.
- Funding payment timing on Hyperliquid: rates accrue continuously and pay
  every hour. A short paying +0.05%/hr funding earns ~$1.20/day per $100 of
  notional. Persistent positive funding is a real tailwind for shorts (and
  drag for longs), not a flat fact to ignore.
- Avoid averaging down on losers. If the original thesis is invalidated,
  close the position; do not add. Adding to a winning position in the
  direction of an intact trend is acceptable within the per-asset cap and
  only when momentum is still confirmed.
- Respect the higher timeframe. A bullish 1h candle inside a clearly bearish
  4h trend is a counter-trend bounce, not a reversal.

End the turn by **calling the `hold` tool** if you have nothing to do.
Every cycle must end with at least one tool call — never just respond with
text and no tool. If you have nothing to do, that decision must still be
expressed as `hold(reasoning=...)`. This keeps the decision log uniform and
your reasoning attached to a structured action.

[cache-key: v8 / 2026-05-22 — bump this string to force a fresh cache write]"""


@dataclass
class CycleResult:
    reasoning: str
    raw_tool_calls: list[dict] = field(default_factory=list)
    actions: list[ActionResult] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def run_cycle(
    *,
    anthropic: Anthropic,
    model: str,
    network: str,
    allowed_assets: list[str],
    snapshot: MarketSnapshot,
    account: AccountState,
    starting_equity_usd: float,
    executor: Executor,
) -> CycleResult:
    system = SYSTEM_PROMPT.format(
        assets=", ".join(allowed_assets), network=network
    )
    user_text = render_context(snapshot, account)
    messages: list[dict[str, Any]] = [_user_message(user_text)]

    raw_tool_calls: list[dict] = []
    actions: list[ActionResult] = []
    final_text = ""
    usage_total = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_5m_input_tokens": 0,
        "cache_write_1h_input_tokens": 0,
        "output_tokens": 0,
    }

    for _ in range(MAX_TURNS):
        # Use the beta-namespaced endpoint so the `betas` array reaches the
        # API correctly. The `extras_headers` route silently downgrades the
        # 1h cache TTL to 5m on this SDK version.
        resp = anthropic.beta.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=_system_blocks(system),
            tools=_tools_with_cache(),
            messages=messages,
            betas=["extended-cache-ttl-2025-04-11"],
        )

        u = resp.usage
        usage_total["input_tokens"] += u.input_tokens or 0
        usage_total["output_tokens"] += u.output_tokens or 0
        usage_total["cache_read_input_tokens"] += (
            getattr(u, "cache_read_input_tokens", 0) or 0
        )
        usage_total["cache_creation_input_tokens"] += (
            getattr(u, "cache_creation_input_tokens", 0) or 0
        )
        # 5m vs 1h split — only present when ttl variants are used.
        cc = getattr(u, "cache_creation", None)
        if cc is not None:
            usage_total["cache_write_5m_input_tokens"] += (
                getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
            )
            usage_total["cache_write_1h_input_tokens"] += (
                getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
            )

        text_parts = [b.text for b in resp.content if b.type == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        # Append the assistant turn back into the message history exactly as returned.
        messages.append({"role": "assistant", "content": resp.content})

        if not tool_uses:
            # Claude returned only text — no tool call. This is a "lazy hold":
            # the prompt explicitly asks for a tool call on every cycle, so a
            # text-only response means Claude is implicitly holding. Synthesize
            # a hold action so the decision log stays uniform — every cycle
            # has either a real tool call or an inferred hold.
            log.info(
                "no tool call in response — synthesizing hold from text "
                "(Claude returned text-only instead of calling hold())"
            )
            synthesized = ActionResult(
                tool="hold",
                args={"reasoning": "[inferred — Claude responded with text only, no tool call]"},
                accepted=True,
                reason="inferred hold (no tool_use in response)",
            )
            actions.append(synthesized)
            raw_tool_calls.append(
                {
                    "name": "hold",
                    "input": {"reasoning": "[inferred from text response]"},
                    "inferred": True,
                }
            )
            break

        # Hold-only optimization: if every tool call is `hold`, skip the
        # follow-up acknowledgement call. The reasoning is already in
        # `final_text`, the action is a no-op, and there's no retry possible.
        # Cuts LLM cost roughly in half on hold cycles (~85% of all cycles).
        hold_only = all(tu.name == "hold" for tu in tool_uses)

        tool_results = []
        for tu in tool_uses:
            args = dict(tu.input or {})
            raw_tool_calls.append({"name": tu.name, "input": args})

            # Refresh account state between trades only matters within a cycle if
            # we'd execute multiple sized actions; for v1 we re-use the cycle-start
            # snapshot. The risk gate's caps are computed against this snapshot.
            result = executor.apply(
                tu.name,
                args,
                account=account,
                snapshot=snapshot,
                starting_equity_usd=starting_equity_usd,
            )
            actions.append(result)

            if result.accepted:
                content = result.reason or "ok"
            else:
                content = f"REJECTED: {result.reason}"
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": content,
                }
            )

        if hold_only:
            log.info("hold-only cycle — skipping confirmation call")
            break

        messages.append({"role": "user", "content": tool_results})

        if resp.stop_reason != "tool_use":
            break

    log.info(
        "cycle tokens: in=%d cache_read=%d cache_write=%d out=%d (cache_hit_pct=%.0f%%)",
        usage_total["input_tokens"],
        usage_total["cache_read_input_tokens"],
        usage_total["cache_creation_input_tokens"],
        usage_total["output_tokens"],
        100
        * usage_total["cache_read_input_tokens"]
        / max(
            1,
            usage_total["input_tokens"]
            + usage_total["cache_read_input_tokens"]
            + usage_total["cache_creation_input_tokens"],
        ),
    )

    return CycleResult(
        reasoning=final_text,
        raw_tool_calls=raw_tool_calls,
        actions=actions,
        usage=usage_total,
    )
