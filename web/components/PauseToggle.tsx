"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Panel } from "./Panel";

export function PauseToggle() {
  const qc = useQueryClient();
  const runtime = useQuery({ queryKey: ["runtime"], queryFn: api.runtime });
  const mut = useMutation({
    mutationFn: (paused: boolean) => api.pause(paused),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runtime"] }),
  });

  const paused = runtime.data?.paused ?? false;
  const busy = mut.isPending || runtime.isLoading;

  return (
    <Panel title="Scheduler">
      <div className="flex items-center justify-between gap-6">
        <div>
          <div className="text-sm">
            Status:{" "}
            <span
              className={`font-mono font-semibold ${
                paused
                  ? "text-[var(--danger)]"
                  : "text-[var(--accent)]"
              }`}
            >
              {paused ? "PAUSED" : "RUNNING"}
            </span>
          </div>
          <p className="text-xs text-[var(--muted)] mt-1 max-w-md">
            When paused, the scheduler still ticks every cycle but skips work.
            Existing positions are not closed automatically.
          </p>
        </div>
        <button
          disabled={busy}
          onClick={() => mut.mutate(!paused)}
          className={`px-4 py-2 rounded text-sm font-medium border transition-colors disabled:opacity-50 ${
            paused
              ? "border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10"
              : "border-[var(--danger)] text-[var(--danger)] hover:bg-[var(--danger)]/10"
          }`}
        >
          {busy ? "…" : paused ? "Resume" : "Pause"}
        </button>
      </div>
      {mut.error && (
        <div className="mt-3 text-xs text-[var(--danger)]">
          {(mut.error as Error).message}
        </div>
      )}
    </Panel>
  );
}
