"""Quick check that SYSTEM_PROMPT is large enough to anchor the 1-hour
prompt cache breakpoint on Anthropic's API.

Anthropic silently demotes the 1h cache breakpoint on a system block under
1024 tokens — the entire cache chain falls back to 5-minute TTL. This script
prints the current size + headroom so you can verify after editing the prompt.

Usage:
    python scripts/check_prompt_size.py
"""
from __future__ import annotations

from hl_agent.agent import SYSTEM_PROMPT

THRESHOLD_TOKENS = 1024


def main() -> None:
    formatted = SYSTEM_PROMPT.format(assets="BTC, ETH", network="testnet")
    chars = len(formatted)
    words = len(formatted.split())
    # English text is ~4 chars/token. Real count via Anthropic API would
    # differ by a few percent but this is close enough for the threshold check.
    est_tokens = chars // 4
    headroom = est_tokens - THRESHOLD_TOKENS

    print(f"SYSTEM_PROMPT size:")
    print(f"  chars:           {chars:,}")
    print(f"  words:           {words:,}")
    print(f"  est. tokens:     ~{est_tokens:,}")
    print(f"  threshold:       {THRESHOLD_TOKENS:,} (1h cache TTL anchor)")
    print(f"  headroom:        {headroom:+,} tokens")
    print()
    if headroom < 0:
        print(
            "  ⚠ BELOW THRESHOLD — 1h cache TTL will silently downgrade "
            "to 5m, costing ~$1/day extra. Add ~125 tokens of useful "
            "content to the 'Trading principles' section."
        )
    elif headroom < 50:
        print(
            "  ⚠ Tight margin. A small future edit could drop you below the "
            "threshold. Consider adding 50-100 more tokens of buffer."
        )
    else:
        print("  ✓ Comfortable margin. Cache anchor is safe.")


if __name__ == "__main__":
    main()
