"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { Panel } from "./Panel";
import type { Risk } from "@/lib/types";

const FIELDS: {
  key: keyof Risk;
  label: string;
  step: number;
  format: "pct" | "num";
  hint: string;
}[] = [
  {
    key: "max_leverage",
    label: "Max portfolio leverage",
    step: 0.1,
    format: "num",
    hint:
      "Pre-trade cap on TOTAL open notional ÷ equity. Stops the bot from " +
      "sizing trades too large. Independent of exchange margin — doesn't " +
      "affect liquidation distance.",
  },
  {
    key: "max_position_pct_per_asset",
    label: "Max % per asset",
    step: 0.01,
    format: "pct",
    hint: "Largest single-asset position as a fraction of equity.",
  },
  {
    key: "max_total_notional_pct",
    label: "Max total %",
    step: 0.01,
    format: "pct",
    hint: "Largest combined notional across all positions.",
  },
  {
    key: "daily_drawdown_kill_switch_pct",
    label: "Daily DD kill switch",
    step: 0.01,
    format: "pct",
    hint: "Equity drop from start-of-day at which only closes are allowed.",
  },
  {
    key: "min_order_usd",
    label: "Min order $",
    step: 1,
    format: "num",
    hint: "Smallest notional the bot may submit.",
  },
];

export function RiskForm() {
  const qc = useQueryClient();
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });

  const [draft, setDraft] = useState<Partial<Risk> | null>(null);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (runtime.data && draft === null) {
      setDraft({ ...runtime.data.effective_risk });
    }
  }, [runtime.data, draft]);

  const mut = useMutation({
    mutationFn: (overrides: Partial<Risk>) => api.setRisk(overrides),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runtime"] });
      setConfirming(false);
    },
  });

  if (!runtime.data || draft === null) {
    return (
      <Panel title="Risk limits">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  const eff = runtime.data.effective_risk;

  const diff = (Object.keys(draft) as (keyof Risk)[]).filter(
    (k) => Number(draft[k]) !== Number(eff[k]),
  );
  const hasChanges = diff.length > 0;

  return (
    <Panel
      title="Risk limits"
      right={
        Object.keys(runtime.data.risk_overrides).length > 0 ? (
          <span className="text-xs text-[var(--muted)]">
            overrides active
          </span>
        ) : null
      }
    >
      <p className="text-xs text-[var(--muted)] mb-4 max-w-xl">
        Pre-trade caps the bot enforces in code. Orders that would breach
        any of these are rejected before being sent. These are independent
        from exchange-side margin (Position leverage above), which controls
        liquidation distance, not order sizing.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (hasChanges) setConfirming(true);
        }}
        className="space-y-3"
      >
        {FIELDS.map((f) => (
          <div key={f.key} className="grid grid-cols-[200px_1fr] gap-3 items-center">
            <label className="text-sm">{f.label}</label>
            <div>
              <input
                type="number"
                step={f.step}
                value={Number(draft[f.key] ?? 0)}
                onChange={(e) =>
                  setDraft({ ...draft, [f.key]: parseFloat(e.target.value) })
                }
                className="w-32 bg-[var(--background)] border border-[var(--panel-border)] rounded px-2 py-1 text-sm font-mono focus:border-[var(--accent)] outline-none"
              />
              <span className="ml-3 text-xs text-[var(--muted)]">
                {f.hint}
              </span>
            </div>
          </div>
        ))}
        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => setDraft({ ...runtime.data!.base_risk })}
            className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] underline"
          >
            Reset to YAML defaults
          </button>
          <button
            type="submit"
            disabled={!hasChanges}
            className="px-4 py-2 rounded text-sm border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Review changes ({diff.length})
          </button>
        </div>
      </form>

      {confirming && (
        <ConfirmModal
          diffKeys={diff}
          before={eff}
          after={draft}
          busy={mut.isPending}
          error={mut.error as Error | null}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            const overrides: Partial<Risk> = {};
            diff.forEach((k) => {
              overrides[k] = Number(draft[k]);
            });
            mut.mutate(overrides);
          }}
        />
      )}
    </Panel>
  );
}

function ConfirmModal({
  diffKeys,
  before,
  after,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  diffKeys: (keyof Risk)[];
  before: Risk;
  after: Partial<Risk>;
  busy: boolean;
  error: Error | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50"
      onClick={onCancel}
    >
      <div
        className="bg-[var(--panel)] border border-[var(--panel-border)] rounded-lg p-6 max-w-md w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold mb-3">Confirm risk changes</h3>
        <p className="text-sm text-[var(--muted)] mb-4">
          These will apply to the next cycle. The bot will reject orders that
          violate the new caps.
        </p>
        <table className="w-full text-sm font-mono mb-5">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-[var(--muted)]">
              <th className="py-1 pr-3">Field</th>
              <th className="py-1 pr-3 text-right">Before</th>
              <th className="py-1 text-right">After</th>
            </tr>
          </thead>
          <tbody>
            {diffKeys.map((k) => {
              const isPct = k.includes("pct");
              const fmt = (n: number) =>
                isPct ? fmtPct(n) : String(n);
              return (
                <tr
                  key={k}
                  className="border-t border-[var(--panel-border)]"
                >
                  <td className="py-1.5 pr-3 text-xs">{k}</td>
                  <td className="py-1.5 pr-3 text-right text-[var(--muted)]">
                    {fmt(Number(before[k]))}
                  </td>
                  <td className="py-1.5 text-right text-[var(--accent)]">
                    {fmt(Number(after[k]))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {error && (
          <div className="text-xs text-[var(--danger)] mb-3">
            {error.message}
          </div>
        )}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-4 py-2 text-sm rounded border border-[var(--panel-border)] hover:bg-[var(--panel-border)]/30 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="px-4 py-2 text-sm rounded border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
