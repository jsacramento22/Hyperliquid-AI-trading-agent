"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtNum, fmtUsd } from "@/lib/format";
import { Panel } from "./Panel";
import type { Position } from "@/lib/types";

export function PositionsTable() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["account"], queryFn: api.account });
  const positions = q.data?.positions ?? [];
  const orders = q.data?.open_orders ?? [];

  // The position currently in the "are you sure?" modal. null = no modal open.
  const [pending, setPending] = useState<Position | null>(null);

  const mut = useMutation({
    mutationFn: (asset: string) => api.closePosition(asset),
    onSuccess: () => {
      // Account refresh, decision/fill/trade panels all pick up the close.
      qc.invalidateQueries({ queryKey: ["account"] });
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["fills"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      setPending(null);
    },
  });

  return (
    <Panel title="Positions">
      {positions.length === 0 ? (
        <div className="text-sm text-[var(--muted)]">No open positions.</div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[var(--muted)] text-xs uppercase tracking-wide">
              <th className="py-1 pr-3">Asset</th>
              <th className="py-1 pr-3">Side</th>
              <th className="py-1 pr-3 text-right">Size</th>
              <th className="py-1 pr-3 text-right">Entry</th>
              <th className="py-1 pr-3 text-right">Notional</th>
              <th className="py-1 pr-3 text-right">uPnL</th>
              <th className="py-1 pr-3 text-right">Lev</th>
              <th className="py-1 pr-3 text-right">Liq</th>
              <th className="py-1 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {positions.map((p) => {
              const long = p.size > 0;
              return (
                <tr
                  key={p.asset}
                  className="border-t border-[var(--panel-border)]"
                >
                  <td className="py-1.5 pr-3">{p.asset}</td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={
                        long
                          ? "text-[var(--accent)]"
                          : "text-[var(--danger)]"
                      }
                    >
                      {long ? "long" : "short"}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {fmtNum(Math.abs(p.size))}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {fmtNum(p.entry_px, 2)}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {fmtUsd(p.position_value_usd)}
                  </td>
                  <td
                    className={`py-1.5 pr-3 text-right ${
                      p.unrealized_pnl_usd >= 0
                        ? "text-[var(--accent)]"
                        : "text-[var(--danger)]"
                    }`}
                  >
                    {fmtUsd(p.unrealized_pnl_usd)}
                  </td>
                  <td className="py-1.5 pr-3 text-right">{p.leverage}x</td>
                  <td className="py-1.5 pr-3 text-right">
                    {p.liquidation_px ? fmtNum(p.liquidation_px, 2) : "—"}
                  </td>
                  <td className="py-1.5 text-right">
                    <button
                      type="button"
                      onClick={() => setPending(p)}
                      disabled={mut.isPending}
                      className="px-2 py-0.5 text-xs rounded border border-[var(--danger)] text-[var(--danger)] hover:bg-[var(--danger)]/10 disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      Close
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {orders.length > 0 && (
        <>
          <div className="mt-5 mb-2 text-xs uppercase tracking-wide text-[var(--muted)]">
            Open orders
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[var(--muted)] text-xs uppercase tracking-wide">
                <th className="py-1 pr-3">OID</th>
                <th className="py-1 pr-3">Asset</th>
                <th className="py-1 pr-3">Side</th>
                <th className="py-1 pr-3 text-right">Size</th>
                <th className="py-1 pr-3 text-right">Limit</th>
                <th className="py-1 text-right">RO</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {orders.map((o) => (
                <tr
                  key={o.oid}
                  className="border-t border-[var(--panel-border)]"
                >
                  <td className="py-1.5 pr-3">{o.oid}</td>
                  <td className="py-1.5 pr-3">{o.asset}</td>
                  <td className="py-1.5 pr-3">{o.side}</td>
                  <td className="py-1.5 pr-3 text-right">{fmtNum(o.size)}</td>
                  <td className="py-1.5 pr-3 text-right">
                    {fmtNum(o.limit_px, 2)}
                  </td>
                  <td className="py-1.5 text-right">
                    {o.reduce_only ? "yes" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {pending && (
        <CloseConfirmModal
          position={pending}
          busy={mut.isPending}
          error={mut.error as Error | null}
          onCancel={() => {
            if (!mut.isPending) setPending(null);
          }}
          onConfirm={() => mut.mutate(pending.asset)}
        />
      )}
    </Panel>
  );
}

function CloseConfirmModal({
  position,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  position: Position;
  busy: boolean;
  error: Error | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const long = position.size > 0;
  const pnlPos = position.unrealized_pnl_usd >= 0;

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50"
      onClick={onCancel}
    >
      <div
        className="bg-[var(--panel)] border border-[var(--panel-border)] rounded-lg p-6 max-w-md w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold mb-2">
          Close {position.asset} {long ? "long" : "short"}?
        </h3>
        <p className="text-sm text-[var(--muted)] mb-4">
          Sends a market-close order to Hyperliquid with 0.5% slippage.
          This is final — locks in the current uPnL as realized.
        </p>
        <table className="w-full text-sm font-mono mb-4">
          <tbody>
            <Row label="Asset" value={position.asset} />
            <Row
              label="Side"
              value={
                <span
                  className={long ? "text-[var(--accent)]" : "text-[var(--danger)]"}
                >
                  {long ? "long" : "short"}
                </span>
              }
            />
            <Row
              label="Size"
              value={`${fmtNum(Math.abs(position.size))} ${position.asset}`}
            />
            <Row label="Entry" value={fmtNum(position.entry_px, 2)} />
            <Row
              label="Notional"
              value={fmtUsd(position.position_value_usd)}
            />
            <Row
              label="Unrealized PnL"
              value={
                <span
                  className={
                    pnlPos ? "text-[var(--accent)]" : "text-[var(--danger)]"
                  }
                >
                  {fmtUsd(position.unrealized_pnl_usd)}
                </span>
              }
            />
          </tbody>
        </table>
        {error && (
          <div className="text-xs text-[var(--danger)] mb-3 font-mono">
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
            className="px-4 py-2 text-sm rounded border border-[var(--danger)] text-[var(--danger)] hover:bg-[var(--danger)]/10 disabled:opacity-50"
          >
            {busy ? "Closing…" : "Confirm market close"}
          </button>
        </div>
      </div>
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
    <tr className="border-t border-[var(--panel-border)]">
      <td className="py-1 pr-3 text-[var(--muted)] text-xs uppercase tracking-wide">
        {label}
      </td>
      <td className="py-1 text-right">{value}</td>
    </tr>
  );
}
