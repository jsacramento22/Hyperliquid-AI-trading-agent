export type Health = {
  ok: boolean;
  network: "testnet" | "mainnet";
  model: string;
  assets: string[];
  cadence_minutes: number;
  version: string;
  started_at_utc: string;
  started_at_unix: number;
  uptime_seconds: number;
  build: {
    running: string;
    disk: string;
    stale: boolean;
    latest_source_file: string;
    latest_source_mtime_unix: number;
  };
  last_error: {
    id: number;
    ts_utc: string;
    cycle_id: string | null;
    component: string;
    error_type: string;
    error_message: string;
  } | null;
};

export type Position = {
  asset: string;
  size: number;
  entry_px: number;
  position_value_usd: number;
  unrealized_pnl_usd: number;
  leverage: number;
  liquidation_px: number | null;
  margin_used_usd: number;
};

export type OpenOrder = {
  asset: string;
  oid: number;
  side: "buy" | "sell";
  size: number;
  limit_px: number;
  reduce_only: boolean;
};

export type Account = {
  address: string;
  equity_usd: number;
  free_margin_usd: number;
  total_notional_usd: number;
  margin_used_usd: number;
  positions: Position[];
  open_orders: OpenOrder[];
};

export type EquitySnapshot = {
  ts_utc: string;
  cycle_id: string;
  equity_usd: number;
  free_margin_usd: number;
  total_notional_usd: number;
  margin_used_usd: number;
};

export type EquityResponse = {
  hours: number;
  snapshots: EquitySnapshot[];
};

export type ToolCall = { name: string; input: Record<string, unknown> };

export type Action = {
  tool: string;
  args: Record<string, unknown>;
  accepted: boolean;
  reason: string;
  response: unknown;
};

export type Decision = {
  id: number;
  ts_utc: string;
  cycle_id: string;
  model: string;
  network: string;
  reasoning: string;
  raw_tool_calls: ToolCall[];
  executed_actions: Action[];
  rejected_actions: Action[];
};

export type DecisionsResponse = { decisions: Decision[] };

export type Fill = {
  id: number;
  ts_utc: string;
  cycle_id: string;
  asset: string;
  side: string;
  requested_usd: number | null;
  raw_response: unknown;
};

export type FillsResponse = { fills: Fill[] };

export type Trade = {
  asset: string;
  side: "long" | "short";
  size: number;
  avg_entry_px: number;
  exit_px: number;
  open_ts_utc: string;
  close_ts_utc: string;
  open_notional_usd: number;
  close_notional_usd: number;
  realized_pnl_usd: number;
  realized_pnl_pct: number;
  duration_seconds: number;
  fill_count: number;
};

export type TradesSummary = {
  count: number;
  total_realized_pnl_usd: number;
  wins: number;
  losses: number;
  scratch: number;
  win_rate: number;
};

export type TradesResponse = { trades: Trade[]; summary: TradesSummary };

export type CostBreakdown = {
  input_usd: number;
  cache_read_usd: number;
  cache_write_5m_usd: number;
  cache_write_1h_usd: number;
  output_usd: number;
  total_usd: number;
};

export type TokenTotals = {
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_5m_tokens: number;
  cache_write_1h_tokens: number;
  output_tokens: number;
};

export type CostResponse = {
  hours: number;
  cycles: number;
  tokens: TokenTotals;
  cost: CostBreakdown;
  cache_hit_pct: number;
  projected_daily_usd: number;
  series: Array<{
    ts_utc: string;
    cycle_id: string;
    model: string;
    tokens: {
      input: number;
      cache_read: number;
      cache_write_5m: number;
      cache_write_1h: number;
      output: number;
    };
    cost_usd: number;
  }>;
};

export type Risk = {
  max_leverage: number;
  max_position_pct_per_asset: number;
  max_total_notional_pct: number;
  daily_drawdown_kill_switch_pct: number;
  min_order_usd: number;
};

export type LeverageState = {
  effective: number;
  base: number;
  override: number | null;
};

export type MarginCrossState = {
  effective: boolean;
  base: boolean;
  override: boolean | null;
};

export type ModelState = {
  effective: string;       // model in use right now
  base: string;            // model from config.yaml
  override: string | null; // runtime-set override, if any
  supported: string[];     // allowlist the UI may pick from
};

// Take-profit / stop-loss monitor state. Each side has independently
// runtime-mutable `enabled` and `pct`; everything else (intervals,
// streak count, slippage) stays YAML-only.
export type MonitorSide = {
  enabled: boolean;
  pct: number;            // 0.015 = 1.5%
};

export type MonitorSideState = {
  effective: MonitorSide;
  base: MonitorSide;
  overrides: Partial<MonitorSide>;
};

export type RuntimeState = {
  paused: boolean;
  risk_overrides: Partial<Risk>;
  effective_risk: Risk;
  base_risk: Risk;
  position_leverage: LeverageState;
  position_margin_cross: MarginCrossState;
  model: ModelState;
  take_profit: MonitorSideState;
  stop_loss: MonitorSideState;
};

// Body for POST /api/monitor. Only set the fields you want to change.
export type MonitorPatch = {
  tp_enabled?: boolean;
  tp_pct?: number;
  sl_enabled?: boolean;
  sl_pct?: number;
};

export type MonitorApplyResponse = {
  take_profit: { effective: MonitorSide; overrides: Partial<MonitorSide> };
  stop_loss: { effective: MonitorSide; overrides: Partial<MonitorSide> };
};

export type LeverageApplyResponse = {
  leverage: number;
  is_cross: boolean;
  per_asset: Record<string, string>;
};

export type ModelApplyResponse = {
  model: string;
  override: string | null;
  base: string;
};
