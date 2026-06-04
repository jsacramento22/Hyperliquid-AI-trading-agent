# hl-agent — Hyperliquid AI trading agent

LLM-driven perpetuals trading bot for [Hyperliquid](https://hyperliquid.xyz).
Claude reads market snapshots every 15 minutes and decides whether to open,
close, hold, or adjust positions through structured tool calls. A
deterministic risk gate rejects orders that violate caps. Two fast monitors
(60-second tick) auto-close positions on take-profit and stop-loss thresholds
without waiting for the next agent cycle.

A Next.js dashboard provides live equity, trade history, decision log, cost
tracking, and runtime controls. All decisions, fills, errors, equity
snapshots, and per-cycle token usage are persisted to SQLite.

**Currently running on Hyperliquid mainnet** behind tighter risk caps. The
codebase still supports testnet — flip one line in `config.yaml`.

---

## Architecture

```
                       Browser (localhost:3000)
                                  │
                       ┌──────────┴──────────┐
                       │   Next.js dashboard │
                       │  (Tailwind/Recharts)│
                       └──────────┬──────────┘
                                  │ HTTP/JSON · 10s polls
                                  ▼  (optionally through nginx + basic auth)
                       ┌─────────────────────┐
                       │   FastAPI (uvicorn) │   ← localhost:8000
                       │   /api/health        /api/account       │
                       │   /api/equity        /api/decisions     │
                       │   /api/fills         /api/trades        │
                       │   /api/cost          /api/runtime       │
                       │   /api/pause         /api/risk          │
                       │   /api/leverage      /api/model         │
                       │   /api/monitor       /api/close_position│
                       └──┬───────────────┬──┘
                          │               │
                          ▼               ▼
   ┌───────────────────────────┐    ┌──────────────────────┐
   │   AsyncIO scheduler       │    │  SQLite              │
   │   ─ trade_cycle (15m)     │    │  decisions/fills/    │
   │   ─ auto_tp_sl_monitor    │    │  equity_snapshots/   │
   │     (60s)                 │    │  token_usage/        │
   └────────────┬──────────────┘    │  cycle_errors/       │
                │                   │  runtime_state       │
                ▼                   └──────────────────────┘
   ┌────────────────────────────────────────────────────┐
   │  agent cycle (every 15 min):                       │
   │    1. fetch market snapshot (mids + 1h/4h candles  │
   │       + funding) and account state                 │
   │    2. render compact text context                  │
   │    3. call Claude with system + tools (cached)     │
   │    4. tool-use loop — each tool call goes through  │
   │       risk gate → exchange.market_open/.order/...  │
   │    5. persist tokens, decisions, fills, equity     │
   │                                                    │
   │  auto-TP/SL monitor (every 60s):                   │
   │    1. fetch account state                          │
   │    2. for each open position: uPnL/(size·entry)    │
   │    3. if streak ≥ require_consecutive_checks       │
   │       and uPnL crosses ±pct, market_close at       │
   │       tight slippage and log as auto-decision      │
   └────────────────────────────────────────────────────┘
```

**Two distinct scheduler jobs in one process:**

| Job | Cadence | What it does | LLM cost |
|---|---|---|---|
| `trade_cycle` | 15 min | Full snapshot → Claude → tool-use → execute | Yes (~$0.77/day on Haiku, ~$2.70/day on Sonnet) |
| `auto_tp_sl_monitor` | 60 s | Read account state → close winners ≥ +pct / losers ≤ −pct | None |

The monitor catches fast moves between agent cycles; the agent does the
strategy. **Auto take-profit and auto stop-loss are OFF by default** —
the scheduler job is always registered (so the UI toggle works without a
restart) but each tick short-circuits unless the YAML or a runtime
override has the side enabled. The agent's prompt also encodes its own
TP/SL rules so closes still happen on the 15-min cycle. Flip the monitor
on per-side from `/settings → Auto take-profit / stop-loss`.

---

## Project layout

```
hyperliquid/
├── src/hl_agent/
│   ├── settings.py     .env + config.yaml → typed Settings (pydantic)
│   ├── hl_client.py    Builds Info + Exchange for testnet or mainnet
│   ├── market_data.py  Mids, 1h/4h candles, funding rates, sz_decimals
│   ├── account.py      Equity, free margin, positions, open orders
│   ├── context.py      Snapshot + account → compact LLM context (markdown)
│   ├── tools.py        JSON tool schemas exposed to Claude
│   ├── agent.py        Claude tool-use loop, prompt + cache wiring
│   ├── risk.py         Pure pre-trade validation (no I/O), kill switch
│   ├── executor.py     Tool calls → risk gate → Exchange, with rounding
│   ├── monitor.py      Auto TP/SL monitor (60s ticks, no LLM)
│   ├── trades.py       Round-trip trade reconstruction from fills
│   ├── cost.py         Pricing tables for Sonnet/Haiku/Opus + aggregation
│   ├── storage.py      SQLite append-only log + runtime state
│   ├── runtime.py      Pause flag + live risk/leverage overrides
│   ├── version.py      Build fingerprint (mtime hash) + startup time
│   ├── main.py         APScheduler BlockingScheduler entrypoint (headless)
│   └── server.py       FastAPI + AsyncIOScheduler (web mode)
├── scripts/
│   ├── one_shot.py            Run one decision cycle (--dry-run flag)
│   ├── show_state.py          Account + recent decisions to stdout
│   ├── dump_payload.py        Print the exact payload Claude would receive
│   │                          (no API call), with --system / --user / --json
│   ├── check_prompt_size.py   Quick SYSTEM_PROMPT size report
│   └── deploy.sh              Rsync to droplet + restart systemd
├── tests/                     71 unit tests, no network
├── web/                       Next.js 16 frontend (TypeScript + Tailwind v4)
│   ├── app/                   Routes: / (dashboard + trades), /settings
│   ├── components/            Panels: Account header, Equity chart,
│   │                          Positions (with Close button), Decisions,
│   │                          Trades (period filter), Cost, Risk form,
│   │                          Leverage form, Model switch, Monitor form
│   │                          (TP/SL), Pause toggle, Version badge,
│   │                          Error banner
│   └── lib/                   api.ts (typed fetch with optional basic
│                              auth), types.ts (mirrors backend shapes)
├── deploy/                    systemd unit + nginx config for production
│   ├── hl-agent.service
│   └── nginx-hl-agent.conf
├── data/                      SQLite DB + backups (gitignored)
├── config.yaml                All runtime config except secrets
├── .env                       Secrets only (gitignored)
├── DEPLOY.md                  Step-by-step DigitalOcean deployment
└── README.md                  This file
```

---

## Setup (local development)

For production deployment on a DigitalOcean droplet, see **[DEPLOY.md](DEPLOY.md)** —
this section covers running on your own machine.

### Prerequisites

- Python 3.10+
- Node 18+ (for the dashboard)
- A Hyperliquid account (testnet OR mainnet) with USDC in the **Perps**
  sub-account
- An Anthropic API key with credits loaded

### 1. Backend install

```bash
git clone <this-repo> hl-agent && cd hl-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### 2. Frontend install

```bash
cd web
npm install
cd ..
```

### 3. Generate a Hyperliquid agent wallet

An *agent wallet* is a separate private key that can trade on your behalf
but **cannot withdraw** — the recommended way to run a bot.

- **Testnet:** <https://app.hyperliquid-testnet.xyz> → connect your wallet
  → **Settings → API → Generate**
- **Mainnet:** <https://app.hyperliquid.xyz> → same path

Copy the private key. You won't see it again.

### 4. Configure `.env`

```bash
cp .env.example .env
```

Fill in:

```dotenv
HL_AGENT_PRIVATE_KEY=0x...      # from step 3
HL_ACCOUNT_ADDRESS=0x...        # your main wallet address (matches the UI)
ANTHROPIC_API_KEY=sk-ant-...    # from https://console.anthropic.com
```

```bash
chmod 600 .env
```

**Common gotcha:** if you use Privy / email login on Hyperliquid, the
testnet and mainnet apps generate *different* addresses for your account.
Don't paste a testnet address into a mainnet `.env` (or vice versa).

### 5. Fund the Perps sub-account

Hyperliquid has separate **Spot** and **Perps** balances. The bot reads from
Perps. After depositing USDC, open the Hyperliquid app → top of Portfolio →
**Perps ⇄ Spot** → move USDC into Perps.

If `python scripts/show_state.py` returns equity $0 but you know you
deposited, this is almost certainly the cause.

---

## Configuration (`config.yaml`)

Everything that isn't a secret. Live-editable values (risk caps, leverage,
pause) can also be changed from the dashboard without a restart.

```yaml
# === Network and model ===
network: mainnet                          # testnet | mainnet
model: claude-haiku-4-5-20251001          # live-overridable via /settings UI
                                          # supported: sonnet-4-6 / haiku-4-5 / opus-4-7

# === Trading universe ===
assets: [BTC, ETH]              # perpetuals the agent is allowed to touch
cadence_minutes: 15             # agent cycle interval

# === Per-position leverage (Hyperliquid setting) ===
position_leverage: 5            # 1-50; lower = wider liquidation distance
position_margin_cross: true     # cross margin (true) vs isolated (false)

# === Risk gate (enforced before every order) ===
risk:
  max_leverage: 2.0                       # portfolio leverage cap
  max_position_pct_per_asset: 0.10        # max notional per asset (% of equity)
  max_total_notional_pct: 0.20            # max combined notional (% of equity)
  daily_drawdown_kill_switch_pct: -0.05   # at this drawdown, only closes allowed
  min_order_usd: 10                       # smallest order the bot may submit

# === Market data snippet sent to Claude ===
market_data:
  candles_1h: 24                # last 24 hourly candles per asset
  candles_4h: 14                # last 14 four-hour candles per asset

# === Persistence ===
storage:
  path: data/hl_agent.db

# === Auto take-profit (deterministic, no LLM call) ===
take_profit:
  enabled: false                # OFF by default — flip on via /settings UI
  pct: 0.015                    # close when uPnL >= +1.5% of entry notional
  check_interval_seconds: 60
  require_consecutive_checks: 2 # filters mark-price spikes
  close_slippage: 0.005         # 0.5% max slippage (vs SDK default 5%)

# === Auto stop-loss (mirror of take-profit) ===
stop_loss:
  enabled: false                # OFF by default — flip on via /settings UI
  pct: 0.015                    # close when uPnL <= -1.5%
  check_interval_seconds: 60
  require_consecutive_checks: 2
  close_slippage: 0.005
```

`take_profit.enabled` and `stop_loss.enabled` are also runtime-mutable
from `/settings → Auto take-profit / stop-loss` — flipping a side on or
off there persists in SQLite and survives restarts without touching
`config.yaml`. Same for `pct` (the threshold).

**Recommended starting caps for mainnet** (shipping with the current config):

```yaml
position_leverage: 5
risk:
  max_position_pct_per_asset: 0.10
  max_total_notional_pct: 0.20
  daily_drawdown_kill_switch_pct: -0.05
```

These keep max single-asset notional under ~10% of equity. Loosen after a
week or two of clean operation if PnL warrants.

---

## Running

You can run either **headless** or **with the web UI**. Do not run both at the
same time — they share the same SQLite file and would double-trade.

### Headless (CLI only)

```bash
source .venv/bin/activate

# Single decision in dry-run mode (calls Claude but submits no orders):
python scripts/one_shot.py --dry-run

# Single live decision:
python scripts/one_shot.py

# Inspect account + last 5 decisions to stdout:
python scripts/show_state.py

# Run the scheduler forever (15-min cycles + 60s auto TP/SL monitor):
python -m hl_agent.main
```

### With the web UI (local dev)

**Terminal 1 — backend** (FastAPI + scheduler in one process):
```bash
source .venv/bin/activate
python -m hl_agent.server     # serves http://localhost:8000
```

Startup logs should show:
```
position leverage set to 5x (cross=True): {'BTC': 'ok', 'ETH': 'ok'}
server scheduler started: every 15 min on mainnet, model=claude-haiku-4-5-20251001, ...
auto take-profit: every 60s, YAML OFF at +1.50% (live-editable)
auto stop-loss:   every 60s, YAML OFF at -1.50% (live-editable)
next cycle at ... UTC (Xs from now — respecting last cycle)
```

**Terminal 2 — frontend** (Next.js dev server):
```bash
cd web
npm run dev                   # serves http://localhost:3000
```

Open <http://localhost:3000>.

**Security note for local dev:** the dashboard talks to FastAPI at
`localhost:8000` with **no authentication**. Do not expose port 8000 to the
public internet without auth (use **[DEPLOY.md](DEPLOY.md)** which puts nginx
+ basic auth in front).

### Production (DigitalOcean droplet)

See **[DEPLOY.md](DEPLOY.md)** — a complete step-by-step from zero to running
behind nginx + basic auth on a $6/mo droplet. ~30-40 minutes the first time.

The dashboard can stay on your laptop pointed at the droplet's IP via:
```dotenv
# web/.env.local
NEXT_PUBLIC_API_URL=http://<droplet-ip>
NEXT_PUBLIC_API_USER=admin
NEXT_PUBLIC_API_PASS=<your-htpasswd-password>
```

---

## Dashboard tour

### `/` — main dashboard

- **Sticky header:** nav, build-fingerprint badge with uptime (red dot if
  code on disk is newer than running process), error banner across the top
  if any cycle failed in the last hour
- **Account header:** equity, free margin, total notional, margin used,
  network/model/cadence/paused badges, wallet address
- **Equity chart:** last 24h equity curve from `equity_snapshots`
- **Cost panel:** last 24h LLM cost breakdown (input, cache-read,
  cache-write 5m/1h, output) + projected daily, with a 1h/6h/24h/7d
  time-window selector
- **Positions table:** all open positions with side, size, entry, notional,
  uPnL, leverage, liquidation price + any open limit orders. Each row has
  a red **Close** button that opens a confirm modal then sends a
  market_close (0.5% slippage). Manual closes are logged with
  `model="manual-close"` so they appear alongside agent / auto-TP / auto-SL
  in the decision log
- **Decisions table:** last 50 cycles with filter pills (All / Actions /
  Rejected). Each row expands to show full reasoning, executed actions,
  and rejected actions. Auto-TP, auto-SL, and manual closes appear with
  their own `model` labels
- **Trades table (bottom of dashboard):** round-trip trade history with a
  period filter (24h / 7d / MTD / All time) that drives the Realized PnL
  summary AND the table. Reconstructed from Hyperliquid's `user_fills`
  API (source of truth — picks up limit fills that the local fills table
  doesn't observe). Asset (All / BTC / ETH) and outcome (All / Wins /
  Losses) filters stack on top of the period filter

### `/settings` — live controls

- **Pause toggle:** stops the agent cycle on its next tick (positions and
  monitors continue; pause blocks new LLM-driven decisions and *also*
  blocks auto TP/SL fires). Manual close from the dashboard still works
  when paused (rescue scenario)
- **Model switch:** dropdown of supported models (Sonnet 4.6 / Haiku 4.5 /
  Opus 4.7) with per-day cost hint. Takes effect on the next 15-min
  cycle. Switching invalidates the prompt cache for one cycle (small
  one-time cost bump)
- **Auto take-profit / stop-loss:** two independently-controllable sides.
  Each has an on/off toggle + a threshold input (% of entry notional).
  Changes apply on the next 60s monitor tick. Pausing one side leaves
  the other armed
- **Position leverage form:** slider 1×–50× + cross/isolated radio.
  Persists override to SQLite *and* calls `exchange.update_leverage` on
  every allowed asset immediately
- **Risk form:** edit each cap with a "review changes" → diff modal →
  confirm. Overrides land in SQLite and take effect on the next cycle
  without restart

All overrides persist across restarts. To revert, click
**"Reset to YAML defaults"** in each form.

---

## Operations

### Watching the logs (local)

The server logs to stdout. Useful lines:

| Pattern | Meaning |
|---|---|
| `cycle ... start (dry_run=False)` | An agent cycle began |
| `cycle ... done — equity=$X executed=N rejected=N` | Cycle finished cleanly |
| `cycle tokens: in=X cache_read=Y cache_write=Z out=O` | Per-cycle token breakdown |
| `hold-only cycle — skipping confirmation call` | Optimization fired (saves an LLM call) |
| `auto take-profit FIRING on BTC short: uPnL=$X` | Monitor closed a winner |
| `auto stop-loss FIRING on ETH long: uPnL=$X` | Monitor closed a loser |
| `auto-tp pending on BTC: ... streak 1/2` | Above threshold but waiting for confirmation |
| `cycle skipped — agent is paused` | Pause toggle is on |
| `cycle raised — continuing scheduler` | An exception was caught + logged to `cycle_errors` |

For production (droplet):
```bash
sudo journalctl -u hl-agent -f
```

### Querying SQLite directly

```bash
sqlite3 data/hl_agent.db
> SELECT COUNT(*) FROM decisions;
> SELECT ts_utc, model, reasoning FROM decisions ORDER BY id DESC LIMIT 5;
> SELECT * FROM cycle_errors ORDER BY id DESC LIMIT 10;
```

Schema lives in `SCHEMA` at the top of [storage.py](src/hl_agent/storage.py).

### Restart-aware scheduler

The scheduler respects the last logged cycle: if you restart 5 min after
the last cycle, it waits 10 min instead of firing immediately. See
`compute_next_run_time()` in [main.py](src/hl_agent/main.py).

### Build / staleness detection

`/api/health` returns a build fingerprint (SHA256 of source-file mtimes)
captured at process startup, and recomputes it on each request. The
dashboard's badge shows red when the running process is older than the
code on disk, so you know whether your edits have taken effect.

### Error surfacing

`/api/health` also returns the most recent cycle failure within the last
hour. The dashboard renders this as a red banner across the top of every
page until it's dismissed or rolls off the window.

### Prompt edits

The full `SYSTEM_PROMPT` is in [agent.py](src/hl_agent/agent.py) and
reproduced under [Agent prompt](#agent-prompt) below for reference. When
you edit it, Anthropic's cache automatically invalidates on the next
cycle (cache keys include a hash of the system block), so no manual
marker is needed. Useful debug commands:

```bash
# Print the current system prompt
python scripts/dump_payload.py --system

# Print everything Claude would receive on the next cycle (no API call)
python scripts/dump_payload.py

# Quick size report
python scripts/check_prompt_size.py
```

The `check_prompt_size.py` script still warns if the prompt drops under
1024 tokens — that floor was tied to a 1h-cache breakpoint optimization
that's no longer load-bearing, but the script is still useful for
catching accidental truncation. Current size is ~2,200 tokens.

---

## Strategy & risk model

The bot opens positions based on momentum/structure analysis driven by
Claude. Closes can fire through four independent paths:

| Path | Trigger | Latency | Default |
|---|---|---|---|
| LLM decision | Claude's tool-use during 15-min cycle | 15 min worst case | always on |
| Auto take-profit | uPnL ≥ +pct for N consecutive 60s ticks | ~60–120 s | **OFF** (UI toggle) |
| Auto stop-loss | uPnL ≤ −pct for N consecutive 60s ticks | ~60–120 s | **OFF** (UI toggle) |
| Manual close | `Close` button in dashboard | instant | works even when paused |

All paths converge on `exchange.market_close()` with a tight slippage
tolerance (default 0.5%, vs the SDK default of 5%).

**The risk gate** in [risk.py](src/hl_agent/risk.py) is invoked before
every order (LLM-originated or auto-monitor):

- Per-asset notional cap
- Total portfolio notional cap
- Implied leverage cap (total notional / equity)
- Daily drawdown kill switch — blocks new opens; only `close_position`
  allowed until UTC midnight rolls the start-of-day equity

---

## Switching networks

Same code path, different URL constant + different agent wallet.

```yaml
# config.yaml
network: mainnet                # or: testnet
```

You need a **separate** agent wallet for each network (the testnet key
won't sign mainnet transactions). Update `.env` with the new
`HL_AGENT_PRIVATE_KEY` and `HL_ACCOUNT_ADDRESS`.

**Recommended sequence for going to mainnet:**

1. Run on testnet until win rate and realized PnL are reliably positive
2. Generate a mainnet agent wallet
3. Bridge $50–$200 USDC to mainnet via the Hyperliquid app
4. Move USDC from Spot → Perps in the Hyperliquid UI
5. Update `.env` + `config.yaml` for mainnet
6. **Tighten risk caps for the first week** (see "Recommended starting caps"
   in the Configuration section)
7. Optionally back up the testnet DB and start fresh:
   ```bash
   mv data/hl_agent.db data/hl_agent.testnet.db.bak
   ```
   before restarting — gives the mainnet equity curve and trade history a
   clean origin

---

## Development

```bash
# Run all tests (no network):
python -m pytest tests/

# Verify SYSTEM_PROMPT is large enough to anchor 1h prompt-cache TTL:
python scripts/check_prompt_size.py

# TypeScript check the frontend without building:
cd web && npx tsc --noEmit

# Production build the frontend:
cd web && npm run build
```

The frontend uses **Next.js 16 (App Router)**, **Tailwind v4**,
**Recharts**, and **TanStack Query** for polling. All API types live in
`web/lib/types.ts` and must be kept in sync with FastAPI response shapes
when you change them.

---

## Cost

Running on Claude Haiku 4.5 (default) with prompt caching + the
hold-only optimization, current actuals from 72h on mainnet:

| Component | $/day | Notes |
|---|---|---|
| LLM (Haiku 4.5)   | **~$0.77** | 96 cycles/day × ~$0.008 average. Switchable to Sonnet (~$2.70/day) or Opus (~$13.50/day) from `/settings`. |
| Hyperliquid trading fees | varies | ~$0.05 per fill, depending on activity |
| DigitalOcean droplet | $6/mo | If running production deployment |
| **Total fixed (Haiku)** | **~$29/month** | Plus exchange fees |

The 60-second auto TP/SL monitor uses **zero LLM tokens** — only Hyperliquid
REST calls (free, well within rate limits).

Switching primary model from Sonnet to Haiku was informed by a 3-day
shadow A/B run that measured 96.7% decision agreement between the two
on the same 180-cycle window. The shadow infrastructure has since been
removed (single-model going forward); the model switch in `/settings`
is the lightweight way to flip back if Haiku quality drops.

Cost tracking:
- `/api/cost?hours=N` returns breakdown over any window
- Dashboard's Cost panel visualizes it with 1h/6h/24h/7d toggles
- Per-cycle token rows are in the SQLite `token_usage` table

---

## Agent prompt

The full `SYSTEM_PROMPT` from [`agent.py`](src/hl_agent/agent.py). This is
the unchanging part of every Anthropic request — it gets prompt-cached
and the per-cycle market data is appended as the user message.

Verify what's actually deployed: `python scripts/dump_payload.py --system`

`{assets}` and `{network}` are interpolated at format-time from `config.yaml`.

```
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

Limit placement discipline (when fading INTO a named resistance/support
zone — distinct from the retracement case above):
Place the limit at the FAR EDGE of the named zone — the TOP of the
resistance band for shorts, the BOTTOM of the support band for longs.
If your thesis says "$62,900-$63,000 is resistance", your short limit
goes at ~63,000, not 62,950 or 62,920. Filling mid-band means you took
the trade on a weak retest before the level was properly defended,
leaving zero buffer if the level breaks. A limit that doesn't fill
because price never reached the actual level is a GOOD outcome — it
means the resistance/support wasn't tested, and you avoided a
mediocre entry. The limit price you choose must be defensible as
"this is where the level actually is", not "this is where I think
price might go".

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
      decisive break of a key level the trade was positioned against, OR
  (d) Position uPnL has reached >= 60% of the kill threshold in (a) — i.e.
      uPnL <= -0.9% when (a) is -1.5% — AND the original thesis has
      WEAKENED in any concrete way:
        * funding has lost the carry advantage that supported entry, OR
        * 1h candles in the adverse direction are continuing/accelerating
          (not just consolidating or pulling back briefly), OR
        * the named level the trade was positioned against has been
          reclaimed with volume.
      Being two-thirds of the way to your hard stop with a thesis that is
      no longer well-supported is NOT a hold. "The bounce is decelerating"
      is not enough — point at a concrete thesis-weakening signal or close.
      Taking a -0.9% loss with structure turning is strictly better than
      testing -1.5% on a thesis you no longer believe in.
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
- BTC and ETH are typically 80-90% correlated intraday. Two new positions
  in the same direction on both is a single concentrated bet, NOT
  diversification — when the macro thesis is wrong both lose together,
  and the per-asset risk caps don't protect against this. Rules:
    * Do not open new same-direction entries on BOTH BTC and ETH in the
      same cycle on the same thesis (e.g. "the 4h downtrend continues"
      does not justify shorting both).
    * If both setups are independently compelling and you would still open
      both, open the higher-conviction one this cycle and wait one cycle
      for the other asset's price action to confirm independently before
      adding the second.
    * Adding to an existing position in the same direction on a different
      asset is acceptable only when the new asset has shown its OWN
      confirming signal since the first entry — not when the new entry
      is just the old thesis rewritten.
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
```

The per-cycle user message (the live market snapshot + account state) is
rendered by `context.render_context()` in [context.py](src/hl_agent/context.py).
Inspect it with: `python scripts/dump_payload.py --user`

---

## Out of scope (deliberately)

- Backtesting framework — markets behave too differently in replay
- News / on-chain signal ingestion — would need separate plumbing
- Assets beyond BTC + ETH — easy to add, deliberately small for now
- Reinforcement-learning strategies — orthogonal to the LLM-driven design
- Built-in API auth — defer to nginx + basic auth (covered in DEPLOY.md)

---

## License

Personal project. No warranty, no guarantees. Cryptocurrency trading
involves real risk of loss; this code makes its own mistakes regularly.
