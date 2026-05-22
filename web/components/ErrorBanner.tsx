"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtLocal, fmtRelative } from "@/lib/format";

export function ErrorBanner() {
  const q = useQuery({ queryKey: ["health"], queryFn: api.health });
  const [dismissed, setDismissed] = useState<string | null>(null);

  const err = q.data?.last_error ?? null;
  if (!err) return null;

  // Allow dismissing a specific error by id (won't reappear until a new one is logged)
  if (dismissed === `${err.id}`) return null;

  return (
    <div className="sticky top-[57px] z-10 bg-[var(--danger)]/15 border-b border-[var(--danger)] text-[var(--danger)]">
      <div className="max-w-6xl mx-auto px-6 py-2.5 flex items-start gap-3 text-sm">
        <span className="font-mono shrink-0 mt-0.5">⚠</span>
        <div className="flex-1 min-w-0">
          <div className="font-medium">
            Cycle failure: <span className="font-mono">{err.error_type}</span>{" "}
            in <span className="font-mono">{err.component}</span>
            <span className="text-[var(--muted)] font-normal">
              {" "}
              · {fmtRelative(err.ts_utc)} ({fmtLocal(err.ts_utc)})
            </span>
          </div>
          <div className="text-xs mt-0.5 break-words text-[var(--danger)]/90">
            {err.error_message}
          </div>
        </div>
        <button
          type="button"
          onClick={() => setDismissed(`${err.id}`)}
          className="shrink-0 text-xs px-2 py-1 rounded border border-[var(--danger)]/60 hover:bg-[var(--danger)]/10"
          title="Hide this banner until the next cycle failure"
        >
          dismiss
        </button>
      </div>
    </div>
  );
}
