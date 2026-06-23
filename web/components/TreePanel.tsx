"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmtLocal, fmtPct, fmtRelative } from "@/lib/format";
import type { TreePredictionRow } from "@/lib/types";
import { Panel, Stat } from "./Panel";

// Background colors keyed off prediction confidence so a glance at the
// row tells you how strongly the model leaned. Aligned with the Python
// thresholds in tree_model._bucket_confidence (low ≤2pp, medium 2-5pp,
// high ≥5pp from 50/50).
const CONFIDENCE_COLORS: Record<string, string> = {
  low: "text-[var(--muted)]",
  medium: "text-[var(--foreground)]",
  high: "text-[var(--accent)]",
};

export function TreePanel() {
  const q = useQuery({
    queryKey: ["tree"],
    queryFn: () => api.tree(168, 30),
    // Predictions only update on the agent's 15-min cadence, but outcome
    // backfill scores them as cycles complete — refetching every 60s
    // keeps the rolling accuracy fresh without hammering the API.
    refetchInterval: 60_000,
  });

  if (q.isLoading) {
    return (
      <Panel title="Tree model · Mode A advisor">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (q.error) {
    return (
      <Panel title="Tree model · Mode A advisor">
        <div className="text-[var(--danger)] text-sm">
          {(q.error as Error).message}
        </div>
      </Panel>
    );
  }

  const d = q.data!;
  const latestList = Object.values(d.latest);
  const hasHistory = d.history.length > 0;

  return (
    <Panel
      title="Tree model · Mode A advisor (informational only)"
      right={
        <div className="text-xs text-[var(--muted)]">
          LightGBM · 34 features · backtest ~52% directional
        </div>
      }
    >
      {!hasHistory ? (
        <div className="text-sm text-[var(--muted)]">
          No tree predictions logged yet. The next agent cycle will produce
          one — refresh in ~15 minutes.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6 mb-5">
            {latestList.map((p) => (
              <LatestStat key={p.asset} pred={p} />
            ))}
            <Stat
              label="24h accuracy"
              value={
                d.windows["24h"].scored_count > 0
                  ? fmtPct(d.windows["24h"].accuracy, 1)
                  : "—"
              }
              sub={`${d.windows["24h"].correct_count}/${d.windows["24h"].scored_count} scored`}
            />
            <Stat
              label="7d accuracy"
              value={
                d.windows["168h"].scored_count > 0
                  ? fmtPct(d.windows["168h"].accuracy, 1)
                  : "—"
              }
              sub={`${d.windows["168h"].correct_count}/${d.windows["168h"].scored_count} scored`}
            />
          </div>

          <div className="overflow-x-auto overflow-y-auto max-h-[28rem]">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[var(--panel)] z-10">
                <tr className="text-[var(--muted)] text-xs uppercase tracking-wide">
                  <th className="py-1 pr-3 text-left">When</th>
                  <th className="py-1 pr-3 text-left">Asset</th>
                  <th className="py-1 pr-3 text-right">Prob ↑</th>
                  <th className="py-1 pr-3 text-left">Predicted</th>
                  <th className="py-1 pr-3 text-left">Conf</th>
                  <th className="py-1 pr-3 text-right">Mid @ predict</th>
                  <th className="py-1 pr-3 text-right">Realised</th>
                  <th className="py-1 text-left">Outcome</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {d.history.map((row) => (
                  <HistoryRow key={row.id} row={row} />
                ))}
              </tbody>
            </table>
          </div>

          <div className="text-xs text-[var(--muted)] mt-3">
            Outcomes score the realised 15m close at horizon vs the mid at
            prediction time. Unscored rows are awaiting horizon elapse +
            next backfill cycle.
          </div>
        </>
      )}
    </Panel>
  );
}

function LatestStat({ pred }: { pred: TreePredictionRow }) {
  const deltaPp = (pred.prob_up - 0.5) * 100;
  const directionLabel = pred.predicted_direction === "up" ? "↑ UP" : "↓ DOWN";
  const directionColor =
    pred.predicted_direction === "up" ? "text-[var(--accent)]" : "text-[var(--danger)]";
  return (
    <Stat
      label={`${pred.asset} · latest`}
      value={
        <span className={directionColor}>
          {directionLabel}{" "}
          <span className={`text-base ${CONFIDENCE_COLORS[pred.confidence]}`}>
            {pred.prob_up.toFixed(3)}
          </span>
        </span>
      }
      sub={`${deltaPp >= 0 ? "+" : ""}${deltaPp.toFixed(1)}pp · ${pred.confidence} · ${fmtRelative(pred.ts_utc)}`}
    />
  );
}

function HistoryRow({ row }: { row: TreePredictionRow }) {
  const isScored = row.correct !== null;
  const deltaPp = (row.prob_up - 0.5) * 100;
  return (
    <tr className="border-t border-[var(--panel-border)]">
      <td className="py-1.5 pr-3 text-[var(--muted)] whitespace-nowrap">
        {fmtLocal(row.ts_utc)}
      </td>
      <td className="py-1.5 pr-3">{row.asset}</td>
      <td className="py-1.5 pr-3 text-right">
        {row.prob_up.toFixed(3)}
        <span className="text-[var(--muted)] text-xs ml-1">
          ({deltaPp >= 0 ? "+" : ""}
          {deltaPp.toFixed(1)}pp)
        </span>
      </td>
      <td
        className={`py-1.5 pr-3 ${
          row.predicted_direction === "up"
            ? "text-[var(--accent)]"
            : "text-[var(--danger)]"
        }`}
      >
        {row.predicted_direction.toUpperCase()}
      </td>
      <td
        className={`py-1.5 pr-3 ${CONFIDENCE_COLORS[row.confidence] ?? ""}`}
      >
        {row.confidence}
      </td>
      <td className="py-1.5 pr-3 text-right text-[var(--muted)]">
        {row.mid_price.toLocaleString("en-US", {
          maximumFractionDigits: 2,
        })}
      </td>
      <td className="py-1.5 pr-3 text-right text-[var(--muted)]">
        {row.realized_close === null
          ? "—"
          : row.realized_close.toLocaleString("en-US", {
              maximumFractionDigits: 2,
            })}
      </td>
      <td className="py-1.5">
        {!isScored ? (
          <span className="text-[var(--muted)]">pending</span>
        ) : row.correct === 1 ? (
          <span className="text-[var(--accent)]">✓ correct</span>
        ) : (
          <span className="text-[var(--danger)]">✗ wrong</span>
        )}
      </td>
    </tr>
  );
}
