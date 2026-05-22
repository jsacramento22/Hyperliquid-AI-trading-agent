"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmtUsd } from "@/lib/format";
import { Panel, Stat } from "./Panel";

export function AccountHeader() {
  const account = useQuery({ queryKey: ["account"], queryFn: api.account });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });

  if (account.isLoading || health.isLoading) {
    return (
      <Panel title="Account">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (account.error) {
    return (
      <Panel title="Account">
        <div className="text-[var(--danger)]">
          {(account.error as Error).message}
        </div>
      </Panel>
    );
  }

  // Defensive guard — covers any race condition where loading/error are both
  // false but data is still undefined (e.g., between query settle and the
  // re-render that populates `data`).
  if (!account.data || !health.data) {
    return (
      <Panel title="Account">
        <div className="text-[var(--muted)]">waiting for data…</div>
      </Panel>
    );
  }

  const a = account.data;
  const h = health.data;
  const paused = runtime.data?.paused ?? false;

  return (
    <Panel
      title="Account"
      right={
        <div className="flex items-center gap-2 text-xs">
          <Badge>{h.network}</Badge>
          <Badge>{h.model}</Badge>
          <Badge>{`every ${h.cadence_minutes}m`}</Badge>
          {paused && <Badge tone="danger">paused</Badge>}
        </div>
      }
    >
      <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
        <Stat label="Equity" value={fmtUsd(a.equity_usd)} />
        <Stat label="Free margin" value={fmtUsd(a.free_margin_usd)} />
        <Stat label="Total notional" value={fmtUsd(a.total_notional_usd)} />
        <Stat label="Margin used" value={fmtUsd(a.margin_used_usd)} />
      </div>
      <div className="mt-3 text-xs text-[var(--muted)] font-mono break-all">
        {a.address}
      </div>
    </Panel>
  );
}

function Badge({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "danger";
}) {
  const cls =
    tone === "danger"
      ? "border-[var(--danger)] text-[var(--danger)]"
      : "border-[var(--panel-border)] text-[var(--muted)]";
  return (
    <span
      className={`px-2 py-0.5 rounded border ${cls} uppercase tracking-wide`}
    >
      {children}
    </span>
  );
}
