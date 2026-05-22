"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtLocal, fmtRelative } from "@/lib/format";
import { Panel } from "./Panel";
import type { Decision } from "@/lib/types";

type Filter = "all" | "actions" | "rejected";

function isActionCycle(d: Decision): boolean {
  return (
    d.executed_actions.some((a) => a.tool !== "hold") ||
    d.rejected_actions.length > 0
  );
}

function isRejectedCycle(d: Decision): boolean {
  return d.rejected_actions.length > 0;
}

export function DecisionsTable({ limit = 50 }: { limit?: number }) {
  const q = useQuery({
    queryKey: ["decisions", limit],
    queryFn: () => api.decisions(limit),
  });

  const [filter, setFilter] = useState<Filter>("all");
  const [openIds, setOpenIds] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const all = q.data?.decisions ?? [];
  const counts = {
    all: all.length,
    actions: all.filter(isActionCycle).length,
    rejected: all.filter(isRejectedCycle).length,
  };
  const filtered = all.filter((d) => {
    if (filter === "all") return true;
    if (filter === "actions") return isActionCycle(d);
    if (filter === "rejected") return isRejectedCycle(d);
    return true;
  });

  return (
    <Panel
      title={`Decisions (${filtered.length}/${counts.all})`}
      right={
        <div className="flex gap-1 text-xs">
          <FilterPill
            active={filter === "all"}
            onClick={() => setFilter("all")}
          >
            All ({counts.all})
          </FilterPill>
          <FilterPill
            active={filter === "actions"}
            onClick={() => setFilter("actions")}
          >
            Actions ({counts.actions})
          </FilterPill>
          <FilterPill
            active={filter === "rejected"}
            onClick={() => setFilter("rejected")}
          >
            Rejected ({counts.rejected})
          </FilterPill>
        </div>
      }
    >
      {filtered.length === 0 ? (
        <div className="text-sm text-[var(--muted)]">
          {all.length === 0
            ? "No decisions yet."
            : `No decisions match the "${filter}" filter.`}
        </div>
      ) : (
        <div className="space-y-1 max-h-[480px] overflow-y-auto pr-1">
          {filtered.map((d) => (
            <DecisionRow
              key={d.id}
              d={d}
              open={openIds.has(d.id)}
              onToggle={() => toggle(d.id)}
            />
          ))}
        </div>
      )}
    </Panel>
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

function DecisionRow({
  d,
  open,
  onToggle,
}: {
  d: Decision;
  open: boolean;
  onToggle: () => void;
}) {
  const totalActions = d.executed_actions.length + d.rejected_actions.length;
  const summaryAction =
    d.executed_actions[0]?.tool ?? d.rejected_actions[0]?.tool ?? "—";

  return (
    <div className="border-t border-[var(--panel-border)] first:border-t-0">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between py-2 text-sm text-left hover:bg-[var(--panel-border)]/30 px-2 rounded"
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-[var(--muted)] text-xs font-mono w-20 shrink-0">
            {fmtRelative(d.ts_utc)}
          </span>
          <span className="font-mono text-xs">{summaryAction}</span>
          {d.executed_actions.length > 0 && (
            <span className="text-[var(--accent)] text-xs">
              ✓ {d.executed_actions.length}
            </span>
          )}
          {d.rejected_actions.length > 0 && (
            <span className="text-[var(--danger)] text-xs">
              ✗ {d.rejected_actions.length}
            </span>
          )}
          <span className="truncate text-[var(--muted)]">
            {d.reasoning.split("\n")[0]}
          </span>
        </div>
        <span className="text-[var(--muted)] text-xs ml-2">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="px-2 pb-3 text-sm space-y-2">
          <div className="text-xs text-[var(--muted)] font-mono">
            {fmtLocal(d.ts_utc)} · cycle {d.cycle_id} · {d.model} ·{" "}
            {d.network}
          </div>
          {d.reasoning && (
            <div className="whitespace-pre-wrap text-[var(--foreground)] bg-[var(--background)] rounded p-3 border border-[var(--panel-border)] text-[13px] leading-snug">
              {d.reasoning}
            </div>
          )}
          {totalActions > 0 && (
            <div className="space-y-1">
              {d.executed_actions.map((a, i) => (
                <ActionLine key={`e${i}`} status="ok" tool={a.tool} args={a.args} reason={a.reason} />
              ))}
              {d.rejected_actions.map((a, i) => (
                <ActionLine
                  key={`r${i}`}
                  status="reject"
                  tool={a.tool}
                  args={a.args}
                  reason={a.reason}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ActionLine({
  status,
  tool,
  args,
  reason,
}: {
  status: "ok" | "reject";
  tool: string;
  args: Record<string, unknown>;
  reason: string;
}) {
  return (
    <div className="text-xs font-mono flex gap-2">
      <span
        className={
          status === "ok"
            ? "text-[var(--accent)]"
            : "text-[var(--danger)]"
        }
      >
        {status === "ok" ? "OK    " : "REJECT"}
      </span>
      <span className="shrink-0">{tool}</span>
      <span className="text-[var(--muted)] truncate">
        {JSON.stringify(args)}
      </span>
      {reason && (
        <span className="text-[var(--muted)] truncate">— {reason}</span>
      )}
    </div>
  );
}
