"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { fmtUsd } from "@/lib/format";
import { Panel } from "./Panel";

export function EquityChart({ hours = 24 }: { hours?: number }) {
  const q = useQuery({
    queryKey: ["equity", hours],
    queryFn: () => api.equity(hours),
  });
  // Defer chart mount until the parent has layout — Recharts'
  // ResponsiveContainer otherwise logs a width(-1) warning on first paint.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  if (q.isLoading) {
    return (
      <Panel title={`Equity (last ${hours}h)`}>
        <div className="h-64 flex items-center justify-center text-[var(--muted)]">
          loading…
        </div>
      </Panel>
    );
  }

  const data = (q.data?.snapshots ?? []).map((s) => ({
    t: new Date(s.ts_utc).getTime(),
    equity: s.equity_usd,
  }));

  if (data.length < 2) {
    return (
      <Panel title={`Equity (last ${hours}h)`}>
        <div className="h-64 flex items-center justify-center text-[var(--muted)] text-sm">
          waiting for more data points… ({data.length} so far)
        </div>
      </Panel>
    );
  }

  const equities = data.map((d) => d.equity);
  const min = Math.min(...equities);
  const max = Math.max(...equities);
  const pad = Math.max((max - min) * 0.1, 0.01);

  return (
    <Panel title={`Equity (last ${hours}h, ${data.length} points)`}>
      <div className="h-64 min-w-0">
        {mounted ? (
          <ResponsiveContainer width="100%" height={256} minWidth={1}>
            <LineChart
              data={data}
              margin={{ top: 8, right: 16, bottom: 8, left: 16 }}
            >
            <CartesianGrid stroke="var(--panel-border)" strokeDasharray="3 3" />
            <XAxis
              dataKey="t"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t: number) =>
                new Date(t).toLocaleTimeString("en-US", {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              }
              stroke="var(--muted)"
              fontSize={11}
            />
            <YAxis
              dataKey="equity"
              domain={[min - pad, max + pad]}
              tickFormatter={(v: number) => `$${v.toFixed(0)}`}
              stroke="var(--muted)"
              fontSize={11}
              width={70}
            />
            <Tooltip
              contentStyle={{
                background: "var(--panel)",
                border: "1px solid var(--panel-border)",
                borderRadius: 6,
                fontSize: 12,
              }}
              labelStyle={{ color: "var(--muted)" }}
              labelFormatter={(label) =>
                new Date(Number(label)).toLocaleString("en-US")
              }
              formatter={(value) => [fmtUsd(Number(value)), "Equity"]}
            />
            <Line
              type="monotone"
              dataKey="equity"
              stroke="var(--accent)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            </LineChart>
          </ResponsiveContainer>
        ) : null}
      </div>
    </Panel>
  );
}
