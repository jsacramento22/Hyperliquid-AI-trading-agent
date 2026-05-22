export function Panel({
  title,
  right,
  children,
}: {
  title?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg bg-[var(--panel)] border border-[var(--panel-border)] overflow-hidden">
      {(title || right) && (
        <header className="flex items-center justify-between px-4 py-2.5 border-b border-[var(--panel-border)]">
          <h2 className="text-sm font-medium text-[var(--muted)] uppercase tracking-wide">
            {title}
          </h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-[var(--muted)]">
        {label}
      </div>
      <div className="text-xl font-semibold mt-1 font-mono">{value}</div>
      {sub && <div className="text-xs text-[var(--muted)] mt-1">{sub}</div>}
    </div>
  );
}
