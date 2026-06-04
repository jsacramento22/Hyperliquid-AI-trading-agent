"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { Panel } from "./Panel";
import type { MonitorPatch, MonitorSideState } from "@/lib/types";

type SideKey = "tp" | "sl";
const SIDE_LABELS: Record<SideKey, { title: string; verb: string; sign: string }> = {
  tp: { title: "Take-profit", verb: "close winners", sign: "+" },
  sl: { title: "Stop-loss", verb: "close losers", sign: "−" },
};

type SideDraft = { enabled: boolean; pctPct: number };  // pctPct is in % (e.g. 1.5), not 0.015

function stateToDraft(s: MonitorSideState): SideDraft {
  return { enabled: s.effective.enabled, pctPct: s.effective.pct * 100 };
}

export function MonitorForm() {
  const qc = useQueryClient();
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });

  const [tp, setTp] = useState<SideDraft | null>(null);
  const [sl, setSl] = useState<SideDraft | null>(null);

  // Initialize draft from server state once; subsequent server polls don't
  // clobber in-progress edits.
  useEffect(() => {
    if (runtime.data?.take_profit && tp === null) {
      setTp(stateToDraft(runtime.data.take_profit));
    }
    if (runtime.data?.stop_loss && sl === null) {
      setSl(stateToDraft(runtime.data.stop_loss));
    }
  }, [runtime.data, tp, sl]);

  const mut = useMutation({
    mutationFn: (patch: MonitorPatch) => api.setMonitor(patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runtime"] });
    },
  });

  if (!runtime.data) {
    return (
      <Panel title="Auto take-profit / stop-loss">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (!runtime.data.take_profit || !runtime.data.stop_loss) {
    return (
      <Panel title="Auto take-profit / stop-loss">
        <div className="text-sm text-[var(--danger)]">
          Backend is out of date — restart{" "}
          <code className="font-mono">python -m hl_agent.server</code> to pick
          up the new <code className="font-mono">/api/monitor</code> endpoint.
        </div>
      </Panel>
    );
  }

  if (tp === null || sl === null) {
    return (
      <Panel title="Auto take-profit / stop-loss">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  const tpState = runtime.data.take_profit;
  const slState = runtime.data.stop_loss;

  const tpDirty =
    tp.enabled !== tpState.effective.enabled ||
    Math.abs(tp.pctPct / 100 - tpState.effective.pct) > 1e-9;
  const slDirty =
    sl.enabled !== slState.effective.enabled ||
    Math.abs(sl.pctPct / 100 - slState.effective.pct) > 1e-9;

  const tpOverrideActive = Object.keys(tpState.overrides ?? {}).length > 0;
  const slOverrideActive = Object.keys(slState.overrides ?? {}).length > 0;
  const anyOverride = tpOverrideActive || slOverrideActive;

  const applyTp = () =>
    mut.mutate({ tp_enabled: tp.enabled, tp_pct: tp.pctPct / 100 });
  const applySl = () =>
    mut.mutate({ sl_enabled: sl.enabled, sl_pct: sl.pctPct / 100 });

  return (
    <Panel
      title="Auto take-profit / stop-loss"
      right={
        anyOverride ? (
          <span className="text-xs text-[var(--muted)]">
            overrides active
          </span>
        ) : null
      }
    >
      <p className="text-xs text-[var(--muted)] mb-5 max-w-xl">
        Deterministic monitor that runs between LLM cycles (every 60s) and
        force-closes positions whose unrealized PnL crosses the threshold.
        No LLM call needed — instant action when triggered. Take-profit and
        stop-loss can be controlled separately.
      </p>

      <div className="space-y-6">
        <SideRow
          sideKey="tp"
          state={tpState}
          draft={tp}
          setDraft={setTp}
          dirty={tpDirty}
          busy={mut.isPending}
          onApply={applyTp}
          onReset={() => setTp(stateToDraft(tpState))}
        />

        <div className="border-t border-[var(--panel-border)]" />

        <SideRow
          sideKey="sl"
          state={slState}
          draft={sl}
          setDraft={setSl}
          dirty={slDirty}
          busy={mut.isPending}
          onApply={applySl}
          onReset={() => setSl(stateToDraft(slState))}
        />
      </div>

      {mut.error && (
        <div className="mt-4 text-xs text-[var(--danger)] font-mono">
          {(mut.error as Error).message}
        </div>
      )}
    </Panel>
  );
}

function SideRow({
  sideKey,
  state,
  draft,
  setDraft,
  dirty,
  busy,
  onApply,
  onReset,
}: {
  sideKey: SideKey;
  state: MonitorSideState;
  draft: SideDraft;
  setDraft: (d: SideDraft) => void;
  dirty: boolean;
  busy: boolean;
  onApply: () => void;
  onReset: () => void;
}) {
  const meta = SIDE_LABELS[sideKey];
  const overrideActive = Object.keys(state.overrides ?? {}).length > 0;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="text-sm font-semibold">{meta.title}</h3>
          <div className="text-xs text-[var(--muted)] mt-0.5">
            Auto-{meta.verb} when uPnL crosses {meta.sign}
            {fmtPct(state.effective.pct, 2)} of entry notional. YAML default{" "}
            {meta.sign}
            {fmtPct(state.base.pct, 2)}
            {overrideActive ? " (override active)" : ""}.
          </div>
        </div>
        <button
          type="button"
          onClick={() =>
            setDraft({ ...draft, enabled: !draft.enabled })
          }
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
            draft.enabled ? "bg-[var(--accent)]" : "bg-[var(--panel-border)]"
          }`}
          aria-label={`toggle ${meta.title}`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
              draft.enabled ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </div>

      <div className="grid grid-cols-[200px_1fr] gap-3 items-center">
        <label className="text-sm">Threshold (%)</label>
        <div className="flex items-center gap-3">
          <input
            type="number"
            min={0.1}
            max={50}
            step={0.1}
            value={draft.pctPct}
            onChange={(e) =>
              setDraft({ ...draft, pctPct: parseFloat(e.target.value) || 0 })
            }
            disabled={!draft.enabled}
            className="w-24 bg-[var(--background)] border border-[var(--panel-border)] rounded px-2 py-1 text-sm font-mono focus:border-[var(--accent)] outline-none disabled:opacity-40"
          />
          <span className="text-sm text-[var(--muted)]">% of entry</span>
        </div>
      </div>

      <div className="flex items-center justify-between pt-1">
        <button
          type="button"
          onClick={onReset}
          disabled={!dirty}
          className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] underline disabled:opacity-30 disabled:cursor-not-allowed disabled:no-underline"
        >
          Revert
        </button>
        <button
          type="button"
          onClick={onApply}
          disabled={!dirty || busy}
          className="px-4 py-2 rounded text-sm border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-30 disabled:cursor-not-allowed"
        >
          {busy ? "Applying…" : dirty ? `Apply ${meta.title}` : "Up to date"}
        </button>
      </div>
    </div>
  );
}
