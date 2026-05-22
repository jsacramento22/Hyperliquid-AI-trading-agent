"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";

function fmtAge(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return m === 0 ? `${h}h` : `${h}h${m}m`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return h === 0 ? `${d}d` : `${d}d${h}h`;
}

function fmtIso(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function VersionBadge() {
  const q = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
  });
  const [open, setOpen] = useState(false);

  if (!q.data) return null;
  const h = q.data;

  // Backend predates the version endpoint — show a friendly hint instead of crashing.
  if (!h.build) {
    return (
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 text-xs font-mono px-2 py-1 rounded border border-[var(--danger)] text-[var(--danger)] hover:bg-[var(--danger)]/10"
        title="Backend predates the version endpoint"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-[var(--danger)] animate-pulse" />
        <span>backend out of date — restart</span>
      </button>
    );
  }

  const stale = h.build.stale;
  const diskMtime = new Date(h.build.latest_source_mtime_unix * 1000);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-2 text-xs font-mono px-2 py-1 rounded border transition-colors ${
          stale
            ? "border-[var(--danger)] text-[var(--danger)] hover:bg-[var(--danger)]/10"
            : "border-[var(--panel-border)] text-[var(--muted)] hover:text-[var(--foreground)]"
        }`}
        title={stale ? "Code on disk is newer than the running process" : "Build status"}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            stale ? "bg-[var(--danger)] animate-pulse" : "bg-[var(--accent)]"
          }`}
        />
        <span>build {h.build.running}</span>
        <span className="text-[var(--muted)]">·</span>
        <span>up {fmtAge(h.uptime_seconds)}</span>
      </button>

      {open && (
        <div
          className="absolute right-0 mt-2 w-80 bg-[var(--panel)] border border-[var(--panel-border)] rounded-lg shadow-xl z-30 p-4 text-xs space-y-2"
          onClick={(e) => e.stopPropagation()}
        >
          {stale && (
            <div className="text-[var(--danger)] font-medium border border-[var(--danger)] rounded px-2 py-1.5 mb-2">
              ⚠ Disk has newer code than running process. Restart{" "}
              <code>python -m hl_agent.server</code> to apply.
            </div>
          )}
          <Row label="Version" value={`v${h.version}`} />
          <Row
            label="Build (running)"
            value={<code className="font-mono">{h.build.running}</code>}
          />
          <Row
            label="Build (disk)"
            value={
              <code
                className={`font-mono ${stale ? "text-[var(--danger)]" : ""}`}
              >
                {h.build.disk}
              </code>
            }
          />
          <Row
            label="Latest source"
            value={
              <code className="font-mono">{h.build.latest_source_file}</code>
            }
          />
          <Row label="Source mtime" value={fmtIso(diskMtime.toISOString())} />
          <Row label="Started at" value={fmtIso(h.started_at_utc)} />
          <Row label="Uptime" value={fmtAge(h.uptime_seconds)} />
          <Row label="Network" value={h.network} />
          <Row label="Model" value={h.model} />
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-[var(--muted)]">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  );
}
