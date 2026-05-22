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
                       │   /api/health       │
                       │   /api/account      │
                       │   /api/equity       │
                       │   /api/decisions    │
                       │   /api/fills        │
                       │   /api/trades       │
                       │   /api/cost         │
                       │   /api/runtime      │
                       │   /api/pause        │
                       │   /api/risk         │
                       │   /api/leverage     │
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
| `trade_cycle` | 15 min | Full snapshot → Claude → tool-use → execute | Yes (~$2.40/day) |
| `auto_tp_sl_monitor` | 60 s | Read account state → close winners ≥ +1.5% / losers ≤ −1.5% | None |

The monitor catches fast moves between agent cycles; the agent does the
strategy. The agent's prompt also encodes the same TP/SL rules so closes can
fire on either path.

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
│   └── check_prompt_size.py   Verify SYSTEM_PROMPT ≥ 1024 tokens
├── tests/                     62 unit tests, no network
├── web/                       Next.js 16 frontend (TypeScript + Tailwind v4)
│   ├── app/                   Routes: /, /trades, /settings
│   ├── components/            Panels: Account header, Equity chart,
│   │                          Positions, Decisions, Trades, Cost, Risk
│   │                          form, Leverage form, Pause toggle, Version
│   │                          badge, Error banner
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
network: mainnet                # testnet | mainnet
model: claude-sonnet-4-6        # any Anthropic model id

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
  enabled: true
  pct: 0.015                    # close when uPnL >= +1.5% of entry notional
  check_interval_seconds: 60
  require_consecutive_checks: 2 # filters mark-price spikes
  close_slippage: 0.005         # 0.5% max slippage (vs SDK default 5%)

# === Auto stop-loss (mirror of take-profit) ===
stop_loss:
  enabled: true
  pct: 0.015                    # close when uPnL <= -1.5%
  check_interval_seconds: 60
  require_consecutive_checks: 2
  close_slippage: 0.005
```

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
server scheduler started: every 15 min on mainnet, model=claude-sonnet-4-6, ...
auto take-profit: every 60s at +1.50% gain (req=2 ticks, slippage=0.50%)
auto stop-loss:   every 60s at -1.50% loss (req=2 ticks, slippage=0.50%)
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
  uPnL, leverage, liquidation price + any open limit orders
- **Decisions table:** last 50 cycles with filter pills (All / Actions /
  Rejected). Each row expands to show full reasoning, executed actions,
  and rejected actions. Auto-TP and auto-SL fires appear with their own
  `model` labels.

### `/trades` — round-trip trade history

- Realized PnL summary: trade count, total PnL, wins/losses, win rate
- Sortable trades table: closed timestamp, asset, side, size, avg entry,
  avg exit, notional, PnL$, PnL%, duration, fill count
- Filter pills by asset (All / BTC / ETH) and outcome (All / Wins / Losses)
- Pairing logic in [trades.py](src/hl_agent/trades.py) handles partial
  closes, multi-fill averaging, and rejected fills correctly

### `/settings` — live controls

- **Pause toggle:** stops the agent cycle on its next tick (positions and
  monitors continue; pause blocks new LLM-driven decisions and *also*
  blocks auto TP/SL fires)
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

If you change `SYSTEM_PROMPT` in [agent.py](src/hl_agent/agent.py), bump
the `[cache-key: ...]` marker at the bottom of the prompt. This forces
Anthropic to write a fresh cache entry instead of serving from a stale
one. Run `python scripts/check_prompt_size.py` after edits — the prompt
must stay above 1024 tokens to anchor the 1h cache breakpoint.

---

## Strategy & risk model

The bot opens positions based on momentum/structure analysis driven by
Claude. Closes can fire through three independent paths:

| Path | Trigger | Latency |
|---|---|---|
| LLM decision | Claude's tool-use during 15-min cycle | 15 min worst case |
| Auto take-profit | uPnL ≥ +pct for N consecutive 60s ticks | ~60–120 s |
| Auto stop-loss | uPnL ≤ −pct for N consecutive 60s ticks | ~60–120 s |

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

Running on Claude Sonnet 4.6 with prompt caching + the hold-only
optimization, current actuals:

| Component | $/day | Notes |
|---|---|---|
| LLM (Sonnet 4.6) | **~$2.40** | 96 cycles/day × $0.025 average. 85% are single-call hold cycles. |
| Hyperliquid trading fees | varies | ~$0.05 per fill, depending on activity |
| DigitalOcean droplet | $6/mo | If running production deployment |
| **Total fixed** | **~$76/month** | Plus exchange fees |

The 60-second auto TP/SL monitor uses **zero LLM tokens** — only Hyperliquid
REST calls (free, well within rate limits).

Cost tracking:
- `/api/cost?hours=N` returns breakdown over any window
- Dashboard's Cost panel visualizes it with 1h/6h/24h/7d toggles
- Per-cycle token rows are in the SQLite `token_usage` table

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
