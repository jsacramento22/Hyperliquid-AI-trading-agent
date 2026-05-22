"""Dump the exact payload that would be sent to Claude on the next cycle.

Useful for debugging context quality — see what the LLM actually receives
without burning an API call or making a trade. Hits the Hyperliquid API
to fetch live snapshot + account state (no cost) but never calls Claude.

Usage:
    python scripts/dump_payload.py              # human-readable, all 3 parts
    python scripts/dump_payload.py --user       # just the rendered user message
    python scripts/dump_payload.py --json       # raw API payload as JSON
    python scripts/dump_payload.py --save out.txt
"""
from __future__ import annotations

import argparse
import json
import sys

from hl_agent.account import get_state
from hl_agent.agent import (
    MAX_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    _system_blocks,
    _tools_with_cache,
    _user_message,
)
from hl_agent.context import render_context
from hl_agent.hl_client import build_client
from hl_agent.market_data import get_snapshot
from hl_agent.settings import load_settings
from hl_agent.tools import TOOL_DEFINITIONS


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true",
                   help="emit the raw API payload as JSON (what literally hits Anthropic)")
    p.add_argument("--user", action="store_true",
                   help="print only the rendered user message")
    p.add_argument("--system", action="store_true",
                   help="print only the system prompt")
    p.add_argument("--save", default=None,
                   help="write output to this file instead of stdout")
    args = p.parse_args()

    settings = load_settings()
    client = build_client(settings)

    snapshot = get_snapshot(
        client,
        settings.config.assets,
        candles_1h=settings.config.market_data.candles_1h,
        candles_4h=settings.config.market_data.candles_4h,
    )
    account = get_state(client)

    system_text = SYSTEM_PROMPT.format(
        assets=", ".join(settings.config.assets),
        network=settings.config.network,
    )
    user_text = render_context(snapshot, account)

    out = sys.stdout if args.save is None else open(args.save, "w")
    try:
        if args.json:
            payload = {
                "model": settings.config.model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "system": _system_blocks(system_text),
                "tools": _tools_with_cache(),
                "messages": [_user_message(user_text)],
                "betas": ["extended-cache-ttl-2025-04-11"],
            }
            json.dump(payload, out, indent=2, default=str)
            out.write("\n")
        elif args.user:
            out.write(user_text)
            out.write("\n")
        elif args.system:
            out.write(system_text)
            out.write("\n")
        else:
            # Human-readable summary of all three parts
            sep = "=" * 80
            out.write(f"{sep}\nPayload for {settings.config.model} on {settings.config.network}\n")
            out.write(f"assets: {settings.config.assets}\n{sep}\n\n")

            out.write("### SYSTEM PROMPT\n\n")
            out.write(system_text)
            out.write(f"\n\n{sep}\n### TOOLS ({len(TOOL_DEFINITIONS)} defined)\n\n")
            for t in TOOL_DEFINITIONS:
                out.write(f"- {t['name']}\n")
                desc = t["description"].strip().replace("\n", " ")
                out.write(f"    {desc[:140]}{'...' if len(desc) > 140 else ''}\n")
                params = list(t["input_schema"].get("properties", {}).keys())
                out.write(f"    params: {', '.join(params)}\n")

            out.write(f"\n{sep}\n### USER MESSAGE (rendered snapshot)\n\n")
            out.write(user_text)
            out.write(f"\n\n{sep}\n### SIZES (rough, chars/4)\n")
            out.write(f"  system: ~{len(system_text) // 4:,} tokens\n")
            out.write(f"  user:   ~{len(user_text) // 4:,} tokens\n")
            tools_size = sum(len(json.dumps(t)) for t in TOOL_DEFINITIONS)
            out.write(f"  tools:  ~{tools_size // 4:,} tokens\n")
            out.write(f"  total:  ~{(len(system_text) + len(user_text) + tools_size) // 4:,} tokens\n")
    finally:
        if args.save:
            out.close()
            print(f"wrote payload to {args.save}", file=sys.stderr)


if __name__ == "__main__":
    main()
