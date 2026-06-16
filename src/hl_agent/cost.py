"""Pricing tables and cost computation for token usage."""
from __future__ import annotations

from dataclasses import dataclass

# All values in USD per million tokens. Source: anthropic.com/pricing.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "output": 15.00,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "cache_read": 0.10,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "output": 5.00,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "cache_read": 1.50,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.00,
        "output": 75.00,
    },
    # Non-Anthropic models. cache_* fields are kept at $0 because most
    # OpenAI-compatible providers either don't expose prompt caching or
    # apply it transparently with no separate price tier. If a provider's
    # caching becomes meaningful later, split out the entry per-provider.
    "deepseek/deepseek-chat-v3.1": {
        "input": 0.20,
        "cache_read": 0.0,
        "cache_write_5m": 0.0,
        "cache_write_1h": 0.0,
        "output": 0.80,
    },
    # V3.2 GA (released 2025-12-01): cheaper output ($0.34/M vs $0.80/M for
    # V3.1), agentic tool-use trained in, won Nof1 Alpha Arena S1.5 vs V3.1
    # on Hyperliquid perps. Must be paired with reasoning.enabled=false in
    # the request body — V3.2 defaults reasoning ON, which breaks structured
    # tool output (vllm #41132, vercel/ai #10778).
    "deepseek/deepseek-v3.2": {
        "input": 0.23,
        "cache_read": 0.0,
        "cache_write_5m": 0.0,
        "cache_write_1h": 0.0,
        "output": 0.34,
    },
}


@dataclass
class CostBreakdown:
    input_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_write_5m_usd: float = 0.0
    cache_write_1h_usd: float = 0.0
    output_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return (
            self.input_usd
            + self.cache_read_usd
            + self.cache_write_5m_usd
            + self.cache_write_1h_usd
            + self.output_usd
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "input_usd": self.input_usd,
            "cache_read_usd": self.cache_read_usd,
            "cache_write_5m_usd": self.cache_write_5m_usd,
            "cache_write_1h_usd": self.cache_write_1h_usd,
            "output_usd": self.output_usd,
            "total_usd": self.total_usd,
        }


def cost_for_row(row: dict) -> CostBreakdown:
    """Compute the cost of one token_usage row. Falls back to the Sonnet 4.6
    table for unknown models so values are conservative rather than zero."""
    model = row.get("model", "")
    rates = PRICING.get(model) or PRICING["claude-sonnet-4-6"]

    def usd(tokens: int, rate: float) -> float:
        return (tokens or 0) * rate / 1_000_000.0

    return CostBreakdown(
        input_usd=usd(row.get("input_tokens", 0), rates["input"]),
        cache_read_usd=usd(row.get("cache_read_tokens", 0), rates["cache_read"]),
        cache_write_5m_usd=usd(
            row.get("cache_write_5m_tokens", 0), rates["cache_write_5m"]
        ),
        cache_write_1h_usd=usd(
            row.get("cache_write_1h_tokens", 0), rates["cache_write_1h"]
        ),
        output_usd=usd(row.get("output_tokens", 0), rates["output"]),
    )


def aggregate(rows: list[dict]) -> CostBreakdown:
    out = CostBreakdown()
    for row in rows:
        c = cost_for_row(row)
        out.input_usd += c.input_usd
        out.cache_read_usd += c.cache_read_usd
        out.cache_write_5m_usd += c.cache_write_5m_usd
        out.cache_write_1h_usd += c.cache_write_1h_usd
        out.output_usd += c.output_usd
    return out
