"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtNum, fmtPct, fmtUsd } from "@/lib/format";
import { Panel, Stat } from "./Panel";

const WINDOWS: { label: string; hours: number }[] = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
];

export function CostPanel() {
  const [hours, setHours] = useState(24);
  const q = useQuery({
    queryKey: ["cost", hours],
    queryFn: () => api.cost(hours),
  });

  if (q.isLoading) {
    return (
      <Panel title="Claude API cost">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (q.error) {
    return (
      <Panel title="Claude API cost">
        <div className="text-[var(--danger)] text-sm">
          {(q.error as Error).message}
        </div>
      </Panel>
    );
  }

  const d = q.data!;
  const c = d.cost;
  const t = d.tokens;
  const totalCacheWrite = c.cache_write_5m_usd + c.cache_write_1h_usd;
  const totalCacheWriteTokens =
    t.cache_write_5m_tokens + t.cache_write_1h_tokens;

  const segments = [
    { label: "input", usd: c.input_usd, tokens: t.input_tokens, color: "var(--danger)" },
    { label: "cache write", usd: totalCacheWrite, tokens: totalCacheWriteTokens, color: "#facc15" },
    { label: "cache read", usd: c.cache_read_usd, tokens: t.cache_read_tokens, color: "var(--accent)" },
    { label: "output", usd: c.output_usd, tokens: t.output_tokens, color: "#a78bfa" },
  ];
  const totalForBar = c.total_usd || 1;

  return (
    <Panel
      title={`Claude API cost · last ${d.hours}h · ${d.cycles} cycle${d.cycles === 1 ? "" : "s"}`}
      right={
        <div className="flex gap-1 text-xs">
          {WINDOWS.map((w) => (
            <button
              key={w.hours}
              type="button"
              onClick={() => setHours(w.hours)}
              className={`px-2 py-0.5 rounded border transition-colors ${
                hours === w.hours
                  ? "border-[var(--accent)] text-[var(--accent)] bg-[var(--accent)]/10"
                  : "border-[var(--panel-border)] text-[var(--muted)] hover:text-[var(--foreground)]"
              }`}
            >
              {w.label}
            </button>
          ))}
        </div>
      }
    >
      {d.cycles === 0 ? (
        <div className="text-sm text-[var(--muted)]">
          No token usage logged yet. Once the next scheduled cycle runs, costs
          will start populating here.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6 mb-5">
            <Stat label="Total cost" value={fmtUsd(c.total_usd, 4)} />
            <Stat
              label="Per cycle"
              value={fmtUsd(c.total_usd / Math.max(1, d.cycles), 4)}
              sub={`${d.cycles} cycle${d.cycles === 1 ? "" : "s"}`}
            />
            <Stat
              label="Projected /day"
              value={fmtUsd(d.projected_daily_usd, 2)}
              sub={`extrapolated from ${d.hours}h`}
            />
            <Stat
              label="Cache hit"
              value={fmtPct(d.cache_hit_pct, 1)}
              sub="of input tokens"
            />
          </div>

          <div className="mb-5">
            <div className="flex h-2.5 w-full overflow-hidden rounded">
              {segments.map((s) => {
                const w = (s.usd / totalForBar) * 100;
                if (w < 0.5) return null;
                return (
                  <div
                    key={s.label}
                    style={{ width: `${w}%`, background: s.color }}
                    title={`${s.label}: ${fmtUsd(s.usd, 4)}`}
                  />
                );
              })}
            </div>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--muted)] text-xs uppercase tracking-wide">
                <th className="py-1 pr-3 text-left">Bucket</th>
                <th className="py-1 pr-3 text-right">Tokens</th>
                <th className="py-1 pr-3 text-right">Cost</th>
                <th className="py-1 text-right">% of total</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              <CostRow
                label="Input (no cache)"
                tokens={t.input_tokens}
                usd={c.input_usd}
                total={c.total_usd}
              />
              <CostRow
                label="Cache write 5m"
                tokens={t.cache_write_5m_tokens}
                usd={c.cache_write_5m_usd}
                total={c.total_usd}
              />
              <CostRow
                label="Cache write 1h"
                tokens={t.cache_write_1h_tokens}
                usd={c.cache_write_1h_usd}
                total={c.total_usd}
              />
              <CostRow
                label="Cache read"
                tokens={t.cache_read_tokens}
                usd={c.cache_read_usd}
                total={c.total_usd}
              />
              <CostRow
                label="Output"
                tokens={t.output_tokens}
                usd={c.output_usd}
                total={c.total_usd}
              />
            </tbody>
          </table>
        </>
      )}
    </Panel>
  );
}

function CostRow({
  label,
  tokens,
  usd,
  total,
}: {
  label: string;
  tokens: number;
  usd: number;
  total: number;
}) {
  const pct = total > 0 ? (usd / total) * 100 : 0;
  return (
    <tr className="border-t border-[var(--panel-border)]">
      <td className="py-1.5 pr-3">{label}</td>
      <td className="py-1.5 pr-3 text-right text-[var(--muted)]">
        {fmtNum(tokens, 0)}
      </td>
      <td className="py-1.5 pr-3 text-right">{fmtUsd(usd, 4)}</td>
      <td className="py-1.5 text-right text-[var(--muted)]">
        {pct.toFixed(1)}%
      </td>
    </tr>
  );
}
