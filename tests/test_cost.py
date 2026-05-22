from __future__ import annotations

from hl_agent.cost import PRICING, aggregate, cost_for_row


def test_sonnet_input_cost():
    row = {
        "model": "claude-sonnet-4-6",
        "input_tokens": 1_000_000,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 0,
    }
    c = cost_for_row(row)
    assert c.input_usd == 3.00
    assert c.total_usd == 3.00


def test_sonnet_cache_pricing():
    row = {
        "model": "claude-sonnet-4-6",
        "input_tokens": 0,
        "cache_read_tokens": 1_000_000,
        "cache_write_5m_tokens": 1_000_000,
        "cache_write_1h_tokens": 1_000_000,
        "output_tokens": 0,
    }
    c = cost_for_row(row)
    assert c.cache_read_usd == 0.30
    assert c.cache_write_5m_usd == 3.75
    assert c.cache_write_1h_usd == 6.00
    assert c.total_usd == 0.30 + 3.75 + 6.00


def test_haiku_costs():
    row = {
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 1_000_000,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": 1_000_000,
    }
    c = cost_for_row(row)
    assert c.input_usd == 1.00
    assert c.output_usd == 5.00
    assert c.total_usd == 6.00


def test_unknown_model_defaults_to_sonnet():
    row = {
        "model": "claude-future-x-9",
        "input_tokens": 1_000_000,
        "output_tokens": 0,
    }
    c = cost_for_row(row)
    assert c.input_usd == PRICING["claude-sonnet-4-6"]["input"]


def test_aggregate_sums():
    rows = [
        {"model": "claude-sonnet-4-6", "input_tokens": 500_000, "output_tokens": 100_000},
        {"model": "claude-sonnet-4-6", "input_tokens": 500_000, "output_tokens": 100_000},
    ]
    agg = aggregate(rows)
    assert agg.input_usd == 3.00          # 1M total at $3/M
    assert agg.output_usd == 3.00         # 200K total at $15/M


def test_zero_safe():
    row = {"model": "claude-sonnet-4-6"}
    c = cost_for_row(row)
    assert c.total_usd == 0.0
