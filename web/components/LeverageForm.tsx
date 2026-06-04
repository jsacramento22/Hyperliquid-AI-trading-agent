"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Panel } from "./Panel";
import type { LeverageApplyResponse } from "@/lib/types";

export function LeverageForm() {
  const qc = useQueryClient();
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });

  const [leverage, setLeverage] = useState<number | null>(null);
  const [isCross, setIsCross] = useState<boolean | null>(null);
  const [lastResult, setLastResult] = useState<LeverageApplyResponse | null>(
    null,
  );

  useEffect(() => {
    const lev = runtime.data?.position_leverage;
    const cross = runtime.data?.position_margin_cross;
    if (lev && cross && leverage === null) {
      setLeverage(lev.effective);
      setIsCross(cross.effective);
    }
  }, [runtime.data, leverage]);

  const mut = useMutation({
    mutationFn: (body: { leverage?: number; is_cross?: boolean }) =>
      api.setLeverage(body),
    onSuccess: (res) => {
      setLastResult(res);
      qc.invalidateQueries({ queryKey: ["runtime"] });
      qc.invalidateQueries({ queryKey: ["account"] });
    },
  });

  if (!runtime.data) {
    return (
      <Panel title="Position leverage">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (!runtime.data.position_leverage || !runtime.data.position_margin_cross) {
    return (
      <Panel title="Position leverage">
        <div className="text-sm text-[var(--danger)]">
          Backend is out of date — restart{" "}
          <code className="font-mono">python -m hl_agent.server</code> to pick
          up the new <code className="font-mono">/api/leverage</code> endpoint.
        </div>
      </Panel>
    );
  }

  if (leverage === null || isCross === null) {
    return (
      <Panel title="Position leverage">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  const eff = runtime.data.position_leverage.effective;
  const effCross = runtime.data.position_margin_cross.effective;
  const dirty = leverage !== eff || isCross !== effCross;
  const overrideActive =
    runtime.data.position_leverage.override !== null ||
    runtime.data.position_margin_cross.override !== null;

  return (
    <Panel
      title="Position leverage"
      right={
        overrideActive ? (
          <span className="text-xs text-[var(--muted)]">
            overrides active
          </span>
        ) : null
      }
    >
      <p className="text-xs text-[var(--muted)] mb-4 max-w-xl">
        Exchange-side margin setting on Hyperliquid — controls{" "}
        <strong>liquidation distance</strong> per position. Higher leverage
        = less margin held = position dies on a smaller adverse move. Does
        NOT control how big the bot sizes trades; that's{" "}
        <span className="font-mono">Max portfolio leverage</span> below
        under Risk limits.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!dirty) return;
          const body: { leverage?: number; is_cross?: boolean } = {};
          if (leverage !== eff) body.leverage = leverage;
          if (isCross !== effCross) body.is_cross = isCross;
          mut.mutate(body);
        }}
        className="space-y-4"
      >
        <div className="grid grid-cols-[200px_1fr] gap-3 items-center">
          <label className="text-sm">Leverage</label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1}
              max={50}
              step={1}
              value={leverage}
              onChange={(e) => setLeverage(Number(e.target.value))}
              className="w-48 accent-[var(--accent)]"
            />
            <input
              type="number"
              min={1}
              max={50}
              step={1}
              value={leverage}
              onChange={(e) => setLeverage(Number(e.target.value))}
              className="w-20 bg-[var(--background)] border border-[var(--panel-border)] rounded px-2 py-1 text-sm font-mono focus:border-[var(--accent)] outline-none"
            />
            <span className="text-sm text-[var(--muted)]">×</span>
          </div>
        </div>

        <div className="grid grid-cols-[200px_1fr] gap-3 items-center">
          <label className="text-sm">Margin mode</label>
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="margin"
                checked={isCross}
                onChange={() => setIsCross(true)}
                className="accent-[var(--accent)]"
              />
              <span>Cross</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="margin"
                checked={!isCross}
                onChange={() => setIsCross(false)}
                className="accent-[var(--accent)]"
              />
              <span>Isolated</span>
            </label>
            <span className="text-xs text-[var(--muted)]">
              {isCross
                ? "Shared margin pool across all positions."
                : "Separate margin per position."}
            </span>
          </div>
        </div>

        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => {
              setLeverage(runtime.data!.position_leverage.base);
              setIsCross(runtime.data!.position_margin_cross.base);
            }}
            className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] underline"
          >
            Reset to YAML defaults ({runtime.data.position_leverage.base}×{" "}
            {runtime.data.position_margin_cross.base ? "cross" : "isolated"})
          </button>
          <button
            type="submit"
            disabled={!dirty || mut.isPending}
            className="px-4 py-2 rounded text-sm border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {mut.isPending
              ? "Applying…"
              : dirty
                ? "Apply to exchange"
                : "Up to date"}
          </button>
        </div>
      </form>

      {mut.error && (
        <div className="mt-3 text-xs text-[var(--danger)] font-mono">
          {(mut.error as Error).message}
        </div>
      )}

      {lastResult && (
        <div className="mt-4 text-xs font-mono space-y-1">
          <div className="text-[var(--muted)]">
            Last apply: {lastResult.leverage}×{" "}
            {lastResult.is_cross ? "cross" : "isolated"}
          </div>
          {Object.entries(lastResult.per_asset).map(([asset, status]) => (
            <div key={asset} className="flex gap-3">
              <span className="w-16">{asset}</span>
              <span
                className={
                  status === "ok"
                    ? "text-[var(--accent)]"
                    : "text-[var(--danger)]"
                }
              >
                {status}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
