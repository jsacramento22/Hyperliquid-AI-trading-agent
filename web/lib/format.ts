export function fmtUsd(n: number, frac = 2): string {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: frac,
    maximumFractionDigits: frac,
  });
}

export function fmtPct(n: number, frac = 2): string {
  return `${(n * 100).toFixed(frac)}%`;
}

export function fmtNum(n: number, frac = 4): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: frac });
}

export function fmtLocal(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}
