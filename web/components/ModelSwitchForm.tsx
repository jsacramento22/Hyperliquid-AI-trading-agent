"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Panel } from "./Panel";

// Friendly labels for the small allowlist. New entries should be added in
// runtime.SUPPORTED_MODELS on the backend; this map only controls display.
const LABELS: Record<string, string> = {
  "claude-sonnet-4-6": "Sonnet 4.6",
  "claude-haiku-4-5-20251001": "Haiku 4.5",
  "claude-opus-4-7": "Opus 4.7",
  "deepseek/deepseek-chat-v3.1": "DeepSeek V3.1",
};

const COST_PER_DAY_HINT: Record<string, string> = {
  "claude-sonnet-4-6": "~$2.70/day",
  "claude-haiku-4-5-20251001": "~$0.77/day",
  "claude-opus-4-7": "~$13.50/day",
  "deepseek/deepseek-chat-v3.1": "~$0.12/day",
};

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
};

export function ModelSwitchForm() {
  const qc = useQueryClient();
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });

  // Local pending selection. Initialized once from the effective model so
  // the dropdown reflects current state but doesn't fight server polling.
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    const eff = runtime.data?.model?.effective;
    if (eff && pending === null) setPending(eff);
  }, [runtime.data, pending]);

  const mut = useMutation({
    mutationFn: (model: string) => api.setModel(model),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runtime"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });

  if (!runtime.data) {
    return (
      <Panel title="Model">
        <div className="text-[var(--muted)]">loading…</div>
      </Panel>
    );
  }

  if (!runtime.data.model) {
    return (
      <Panel title="Model">
        <div className="text-sm text-[var(--danger)]">
          Backend is out of date — restart{" "}
          <code className="font-mono">python -m hl_agent.server</code> to pick
          up the new <code className="font-mono">/api/model</code> endpoint.
        </div>
      </Panel>
    );
  }

  const m = runtime.data.model;
  const dirty = pending !== null && pending !== m.effective;
  const overrideActive = m.override !== null && m.override !== m.base;

  return (
    <Panel
      title="Model"
      right={
        overrideActive ? (
          <span className="text-xs text-[var(--muted)]">override active</span>
        ) : null
      }
    >
      <p className="text-xs text-[var(--muted)] mb-4 max-w-xl">
        Which model drives each cycle. Each model is tied to a specific
        provider (Anthropic for Claude, OpenRouter for DeepSeek), so
        switching the model automatically switches the API endpoint.
        Changes apply on the next scheduled cycle — no restart needed.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!dirty || pending === null) return;
          mut.mutate(pending);
        }}
        className="space-y-4"
      >
        <div className="grid grid-cols-[200px_1fr] gap-3 items-center">
          <label className="text-sm">Active model</label>
          <div className="flex items-center gap-3 flex-wrap">
            <select
              value={pending ?? m.effective}
              onChange={(e) => setPending(e.target.value)}
              className="bg-[var(--background)] border border-[var(--panel-border)] rounded px-2 py-1 text-sm font-mono focus:border-[var(--accent)] outline-none min-w-[220px]"
            >
              {Object.entries(m.supported).map(([opt, prov]) => (
                <option key={opt} value={opt}>
                  {LABELS[opt] ?? opt} · {PROVIDER_LABELS[prov] ?? prov}
                </option>
              ))}
            </select>
            {pending && m.supported[pending] && (
              <span className="text-xs px-2 py-0.5 rounded border border-[var(--panel-border)] text-[var(--muted)] font-mono">
                via {PROVIDER_LABELS[m.supported[pending]] ?? m.supported[pending]}
              </span>
            )}
            {pending && COST_PER_DAY_HINT[pending] && (
              <span className="text-xs text-[var(--muted)]">
                {COST_PER_DAY_HINT[pending]}
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => setPending(m.base)}
            className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] underline"
            disabled={pending === m.base}
          >
            Reset to YAML default ({LABELS[m.base] ?? m.base})
          </button>
          <button
            type="submit"
            disabled={!dirty || mut.isPending}
            className="px-4 py-2 rounded text-sm border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {mut.isPending
              ? "Switching…"
              : dirty
                ? "Switch model"
                : "In use"}
          </button>
        </div>
      </form>

      {mut.error && (
        <div className="mt-3 text-xs text-[var(--danger)] font-mono">
          {(mut.error as Error).message}
        </div>
      )}

      <div className="mt-4 text-xs font-mono text-[var(--muted)] space-y-1">
        <div>
          effective:{" "}
          <span className="text-[var(--foreground)]">{m.effective}</span>{" "}
          <span className="text-[var(--muted)]">
            via {PROVIDER_LABELS[m.provider] ?? m.provider}
          </span>
        </div>
        <div>
          base (config.yaml):{" "}
          <span className="text-[var(--foreground)]">{m.base}</span>{" "}
          <span className="text-[var(--muted)]">
            via {PROVIDER_LABELS[m.base_provider] ?? m.base_provider}
          </span>
        </div>
        <div>
          override:{" "}
          <span className="text-[var(--foreground)]">
            {m.override ?? "—"}
          </span>
        </div>
      </div>
    </Panel>
  );
}
