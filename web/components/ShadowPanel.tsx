"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtLocal, fmtPct, fmtRelative, fmtUsd } from "@/lib/format";
import { Panel, Stat } from "./Panel";
import type { ShadowBucket, ShadowPair, ShadowSide } from "@/lib/types";

const WINDOWS: { label: string; hours: number }[] = [
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "3d", hours: 24 * 3 },
  { label: "7d", hours: 24 * 7 },
];

// Tailwind-friendly tint per bucket so primary/shadow side-by-side reads at a glance.
const BUCKET_STYLES: Record<ShadowBucket, string> = {
  hold: "text-[var(--muted)]",
  long: "text-[var(--accent)]",
  short: "text-[var(--danger)]",
  close: "text-[#facc15]",
  cancel: "text-[#a78bfa]",
  other: "text-[var(--foreground)]",
};

export function ShadowPanel() {
  const [hours, setHours] = useState(24);
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const q = useQuery({
    queryKey: ["shadow", hours],
    queryFn: () => api.shadow(hours, 100),
  });

  const toggle = (cycleId: string) =>
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(cycleId)) next.delete(cycleId);
      else next.add(cycleId);
      return next;
    });

  if (q.isLoading) {
    return (
      <Panel title="Shadow A/B">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (q.error) {
    return (
      <Panel title="Shadow A/B">
        <div className="text-[var(--danger)] text-sm">
          {(q.error as Error).message}
        </div>
      </Panel>
    );
  }

  const d = q.data!;

  if (!d.enabled) {
    return (
      <Panel title="Shadow A/B (disabled)">
        <div className="text-sm text-[var(--muted)]">
          Enable in <code className="font-mono">config.yaml</code> under{" "}
          <code className="font-mono">shadow.enabled</code> and restart the
          server to start logging the comparison model.
        </div>
      </Panel>
    );
  }

  return (
    <Panel
      title={`Shadow A/B · primary vs ${d.model} · last ${d.hours}h`}
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
      {d.cycles_compared === 0 ? (
        <div className="text-sm text-[var(--muted)]">
          No paired cycles yet in this window. The shadow runs alongside each
          primary cycle (~every {15} min); pairs will populate here.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6 mb-5">
            <Stat
              label="Agreement"
              value={fmtPct(d.agreement.same_bucket_pct, 1)}
              sub={`${d.cycles_compared} paired cycle${
                d.cycles_compared === 1 ? "" : "s"
              }`}
            />
            <Stat
              label="Both held"
              value={fmtPct(d.agreement.both_hold_pct, 1)}
              sub="no-action overlap"
            />
            <Stat
              label="Same direction"
              value={fmtPct(d.agreement.same_direction_pct, 1)}
              sub="long/short/hold align"
            />
            <Stat
              label="Shadow projected /day"
              value={fmtUsd(d.cost.projected_daily_usd, 2)}
              sub={`${fmtUsd(d.cost.total_usd, 4)} over ${d.hours}h`}
            />
          </div>

          <div className="space-y-1 max-h-[480px] overflow-y-auto pr-1">
            {d.pairs.map((p) => (
              <PairRow
                key={p.cycle_id}
                p={p}
                open={openIds.has(p.cycle_id)}
                onToggle={() => toggle(p.cycle_id)}
              />
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function PairRow({
  p,
  open,
  onToggle,
}: {
  p: ShadowPair;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="border-t border-[var(--panel-border)] first:border-t-0">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 py-2 px-2 text-sm text-left hover:bg-[var(--panel-border)]/30 rounded"
      >
        <span className="text-[var(--muted)] text-xs font-mono w-20 shrink-0">
          {fmtRelative(p.ts_utc)}
        </span>
        <SideChip side={p.primary} who="P" />
        <span className="text-[var(--muted)] text-xs">vs</span>
        <SideChip side={p.shadow} who="S" />
        <span
          className={`ml-auto text-xs ${
            p.agree ? "text-[var(--accent)]" : "text-[var(--danger)]"
          }`}
        >
          {p.agree ? "agree" : "differ"}
        </span>
        <span className="text-[var(--muted)] text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="px-2 pb-3 text-sm space-y-2">
          <div className="text-xs text-[var(--muted)] font-mono">
            {fmtLocal(p.ts_utc)} · cycle {p.cycle_id}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <SidePanel label="Primary" side={p.primary} />
            <SidePanel label="Shadow" side={p.shadow} />
          </div>
        </div>
      )}
    </div>
  );
}

function SideChip({ side, who }: { side: ShadowSide; who: "P" | "S" }) {
  return (
    <span className="flex items-center gap-1.5 text-xs font-mono min-w-0">
      <span className="text-[var(--muted)]">{who}:</span>
      <span className={`${BUCKET_STYLES[side.bucket]} truncate`}>
        {side.label}
      </span>
    </span>
  );
}

function SidePanel({ label, side }: { label: string; side: ShadowSide }) {
  return (
    <div className="bg-[var(--background)] rounded p-3 border border-[var(--panel-border)]">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs uppercase tracking-wide text-[var(--muted)]">
          {label}
        </span>
        <span className="text-[10px] text-[var(--muted)] font-mono">
          {side.model}
        </span>
      </div>
      <div className={`font-mono text-sm mb-2 ${BUCKET_STYLES[side.bucket]}`}>
        {side.label}
      </div>
      {side.reasoning ? (
        <div className="whitespace-pre-wrap text-[13px] leading-snug">
          {side.reasoning}
        </div>
      ) : (
        <div className="text-[var(--muted)] text-xs italic">no reasoning</div>
      )}
    </div>
  );
}
