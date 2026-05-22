"""Print current account state and recent decisions from the local log."""
from __future__ import annotations

import argparse
import json

from hl_agent.account import get_state
from hl_agent.hl_client import build_client
from hl_agent.settings import load_settings
from hl_agent.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="Recent decisions to show.")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip live account state fetch (only show stored history).",
    )
    args = parser.parse_args()

    settings = load_settings()
    storage = Storage(settings.storage_path)

    if not args.no_network:
        client = build_client(settings)
        account = get_state(client)
        print(f"=== Account ({settings.config.network}) ===")
        print(f"address:        {account.address}")
        print(f"equity:         ${account.equity_usd:,.2f}")
        print(f"free margin:    ${account.free_margin_usd:,.2f}")
        print(f"total notional: ${account.total_notional_usd:,.2f}")
        print(f"margin used:    ${account.margin_used_usd:,.2f}")
        print()

        if account.positions:
            print("Positions:")
            for p in account.positions:
                side = "long " if p.size > 0 else "short"
                print(
                    f"  {p.asset:5} {side} sz={abs(p.size):g} entry={p.entry_px:g} "
                    f"notional=${p.position_value_usd:,.2f} uPnL=${p.unrealized_pnl_usd:+,.2f} "
                    f"lev={p.leverage:g}x"
                )
        else:
            print("Positions: none")

        if account.open_orders:
            print("Open orders:")
            for o in account.open_orders:
                print(
                    f"  oid={o.oid} {o.asset} {o.side} sz={o.size:g} px={o.limit_px:g} "
                    f"reduce_only={o.reduce_only}"
                )
        else:
            print("Open orders: none")

        print()

    print(f"=== Last {args.limit} decisions ===")
    for d in storage.recent_decisions(args.limit):
        print(f"\n[{d['ts_utc']}] cycle={d['cycle_id']} model={d['model']}")
        if d["reasoning"]:
            print(f"  reasoning: {d['reasoning']}")
        executed = json.loads(d["executed_actions"])
        rejected = json.loads(d["rejected_actions"])
        for a in executed:
            print(f"  OK     {a['tool']} args={a['args']}")
        for a in rejected:
            print(f"  REJECT {a['tool']} args={a['args']} reason={a['reason']}")


if __name__ == "__main__":
    main()
