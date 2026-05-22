"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmtNum, fmtUsd } from "@/lib/format";
import { Panel } from "./Panel";

export function PositionsTable() {
  const q = useQuery({ queryKey: ["account"], queryFn: api.account });
  const positions = q.data?.positions ?? [];
  const orders = q.data?.open_orders ?? [];

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
              <th className="py-1 text-right">Liq</th>
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
                  <td className="py-1.5 text-right">
                    {p.liquidation_px ? fmtNum(p.liquidation_px, 2) : "—"}
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
    </Panel>
  );
}
