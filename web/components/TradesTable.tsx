"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtLocal, fmtNum, fmtPct, fmtUsd } from "@/lib/format";
import { Panel, Stat } from "./Panel";
import type { Trade } from "@/lib/types";

type Filter = "all" | "wins" | "losses";
type AssetFilter = "all" | "BTC" | "ETH";
type Period = "24h" | "7d" | "mtd" | "all";

const PERIOD_LABELS: Record<Period, string> = {
  "24h": "24h",
  "7d": "7d",
  mtd: "MTD",
  all: "All time",
};

/** Cutoff (epoch ms) below which trades are excluded. null = no cutoff.
 * `close_ts_utc` (ISO) is what we compare against. */
function periodCutoffMs(period: Period): number | null {
  const now = Date.now();
  if (period === "24h") return now - 24 * 3600 * 1000;
  if (period === "7d") return now - 7 * 86400 * 1000;
  if (period === "mtd") {
    const d = new Date(now);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
  }
  return null;
}

function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return m === 0 ? `${h}h` : `${h}h${m}m`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return h === 0 ? `${d}d` : `${d}d${h}h`;
}

export function TradesTable({ limit = 100 }: { limit?: number }) {
  const q = useQuery({
    queryKey: ["trades", limit],
    queryFn: () => api.trades(limit),
  });

  const [period, setPeriod] = useState<Period>("all");
  const [filter, setFilter] = useState<Filter>("all");
  const [assetFilter, setAssetFilter] = useState<AssetFilter>("all");

  if (q.isLoading) {
    return (
      <Panel title="Trade history">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (q.error) {
    return (
      <Panel title="Trade history">
        <div className="text-[var(--danger)]">
          {(q.error as Error).message}
        </div>
      </Panel>
    );
  }

  const all = q.data!.trades;

  // Period filter is the "outer" filter: it drives the Realized PnL summary
  // AND the trades table (so the asset / win-loss pills below are scoped to
  // the chosen period).
  const cutoff = periodCutoffMs(period);
  const inPeriod = all.filter(
    (t) => cutoff === null || new Date(t.close_ts_utc).getTime() >= cutoff,
  );

  // Summary stats computed from the period-filtered set so they actually
  // reflect what the user asked for. The backend's summary is whole-history
  // and isn't used here.
  const periodSummary = {
    count: inPeriod.length,
    total_realized_pnl_usd: inPeriod.reduce(
      (s, t) => s + t.realized_pnl_usd,
      0,
    ),
    wins: inPeriod.filter((t) => t.realized_pnl_usd > 0).length,
    losses: inPeriod.filter((t) => t.realized_pnl_usd < 0).length,
  };
  const periodScratch =
    periodSummary.count - periodSummary.wins - periodSummary.losses;
  const periodWinRate =
    periodSummary.count > 0 ? periodSummary.wins / periodSummary.count : 0;

  // Asset / win-loss filters applied on top of the period set, for the
  // table view below.
  const filtered = inPeriod.filter((t) => {
    if (assetFilter !== "all" && t.asset !== assetFilter) return false;
    if (filter === "wins") return t.realized_pnl_usd > 0;
    if (filter === "losses") return t.realized_pnl_usd < 0;
    return true;
  });

  const filteredPnl = filtered.reduce((s, t) => s + t.realized_pnl_usd, 0);

  return (
    <div className="space-y-6">
      <Panel
        title={`Realized PnL summary · ${PERIOD_LABELS[period]}`}
        right={
          <div className="flex gap-1 text-xs">
            {(Object.keys(PERIOD_LABELS) as Period[]).map((p) => (
              <FilterPill
                key={p}
                active={period === p}
                onClick={() => setPeriod(p)}
              >
                {PERIOD_LABELS[p]}
              </FilterPill>
            ))}
          </div>
        }
      >
        <div className="grid grid-cols-2 md:grid-cols-5 gap-6">
          <Stat label="Total trades" value={fmtNum(periodSummary.count, 0)} />
          <Stat
            label="Total PnL"
            value={
              <span
                className={
                  periodSummary.total_realized_pnl_usd >= 0
                    ? "text-[var(--accent)]"
                    : "text-[var(--danger)]"
                }
              >
                {fmtUsd(periodSummary.total_realized_pnl_usd)}
              </span>
            }
          />
          <Stat
            label="Wins"
            value={
              <span className="text-[var(--accent)]">
                {fmtNum(periodSummary.wins, 0)}
              </span>
            }
          />
          <Stat
            label="Losses"
            value={
              <span className="text-[var(--danger)]">
                {fmtNum(periodSummary.losses, 0)}
              </span>
            }
          />
          <Stat
            label="Win rate"
            value={fmtPct(periodWinRate, 1)}
            sub={periodScratch > 0 ? `${periodScratch} scratch` : undefined}
          />
        </div>
      </Panel>

      <Panel
        title={`Trades (${filtered.length}/${inPeriod.length} in ${PERIOD_LABELS[period]}) · filtered PnL ${fmtUsd(filteredPnl)}`}
        right={
          <div className="flex flex-wrap gap-1 text-xs">
            <FilterPill
              active={assetFilter === "all"}
              onClick={() => setAssetFilter("all")}
            >
              All assets
            </FilterPill>
            <FilterPill
              active={assetFilter === "BTC"}
              onClick={() => setAssetFilter("BTC")}
            >
              BTC
            </FilterPill>
            <FilterPill
              active={assetFilter === "ETH"}
              onClick={() => setAssetFilter("ETH")}
            >
              ETH
            </FilterPill>
            <span className="w-1" />
            <FilterPill
              active={filter === "all"}
              onClick={() => setFilter("all")}
            >
              All
            </FilterPill>
            <FilterPill
              active={filter === "wins"}
              onClick={() => setFilter("wins")}
            >
              Wins
            </FilterPill>
            <FilterPill
              active={filter === "losses"}
              onClick={() => setFilter("losses")}
            >
              Losses
            </FilterPill>
          </div>
        }
      >
        {filtered.length === 0 ? (
          <div className="text-sm text-[var(--muted)]">
            {all.length === 0
              ? "No completed trades yet."
              : "No trades match the current filter."}
          </div>
        ) : (
          <div className="overflow-x-auto overflow-y-auto max-h-[28rem]">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[var(--panel)] z-10">
                <tr className="text-[var(--muted)] text-xs uppercase tracking-wide">
                  <th className="py-1 pr-3 text-left">Closed</th>
                  <th className="py-1 pr-3 text-left">Asset</th>
                  <th className="py-1 pr-3 text-left">Side</th>
                  <th className="py-1 pr-3 text-right">Size</th>
                  <th className="py-1 pr-3 text-right">Entry</th>
                  <th className="py-1 pr-3 text-right">Exit</th>
                  <th className="py-1 pr-3 text-right">Notional</th>
                  <th className="py-1 pr-3 text-right">PnL $</th>
                  <th className="py-1 pr-3 text-right">PnL %</th>
                  <th className="py-1 pr-3 text-right">Duration</th>
                  <th className="py-1 text-right">Fills</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {filtered.map((t, i) => (
                  <TradeRow key={`${t.close_ts_utc}-${i}`} t={t} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function TradeRow({ t }: { t: Trade }) {
  const win = t.realized_pnl_usd > 0;
  const loss = t.realized_pnl_usd < 0;
  const pnlColor = win
    ? "text-[var(--accent)]"
    : loss
      ? "text-[var(--danger)]"
      : "text-[var(--muted)]";

  return (
    <tr className="border-t border-[var(--panel-border)] hover:bg-[var(--panel-border)]/30">
      <td className="py-1.5 pr-3 text-[var(--muted)] whitespace-nowrap">
        {fmtLocal(t.close_ts_utc)}
      </td>
      <td className="py-1.5 pr-3">{t.asset}</td>
      <td className="py-1.5 pr-3">
        <span
          className={
            t.side === "long"
              ? "text-[var(--accent)]"
              : "text-[var(--danger)]"
          }
        >
          {t.side}
        </span>
      </td>
      <td className="py-1.5 pr-3 text-right">{fmtNum(t.size, 6)}</td>
      <td className="py-1.5 pr-3 text-right">
        {fmtNum(t.avg_entry_px, 2)}
      </td>
      <td className="py-1.5 pr-3 text-right">{fmtNum(t.exit_px, 2)}</td>
      <td className="py-1.5 pr-3 text-right">
        {fmtUsd(t.open_notional_usd)}
      </td>
      <td className={`py-1.5 pr-3 text-right ${pnlColor}`}>
        {fmtUsd(t.realized_pnl_usd)}
      </td>
      <td className={`py-1.5 pr-3 text-right ${pnlColor}`}>
        {fmtPct(t.realized_pnl_pct, 2)}
      </td>
      <td className="py-1.5 pr-3 text-right text-[var(--muted)]">
        {fmtDuration(t.duration_seconds)}
      </td>
      <td className="py-1.5 text-right text-[var(--muted)]">
        {t.fill_count}
      </td>
    </tr>
  );
}

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2 py-0.5 rounded border transition-colors ${
        active
          ? "border-[var(--accent)] text-[var(--accent)] bg-[var(--accent)]/10"
          : "border-[var(--panel-border)] text-[var(--muted)] hover:text-[var(--foreground)]"
      }`}
    >
      {children}
    </button>
  );
}
