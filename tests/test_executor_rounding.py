from __future__ import annotations

from hl_agent.executor import _round_price, _round_size


def test_size_rounded_to_sz_decimals_btc():
    # BTC szDecimals=5 — float precision must collapse to 5 places.
    assert _round_size(0.002430561885143798, 5) == 0.00243


def test_size_rounded_to_sz_decimals_eth():
    # ETH szDecimals=4
    assert _round_size(0.0827541, 4) == 0.0828


def test_price_btc_integer_only():
    # BTC: max_decimals = 6-5 = 1, but 5 sig figs at $80k means integer.
    out = _round_price(82356.7, sz_decimals=5)
    assert out == 82357.0  # rounded to integer for sig-fig cap
    assert out == int(out)


def test_price_eth_two_decimals_kept():
    # ETH: max_decimals = 6-4 = 2; 5 sig figs at $2400 allows 1 decimal.
    out = _round_price(2416.83, sz_decimals=4)
    # Must satisfy both constraints — 5 sig figs at this magnitude allows 1 decimal.
    assert out == 2416.8


def test_price_handles_integer_prices():
    assert _round_price(60000.0, sz_decimals=5) == 60000.0
    assert _round_price(3000.0, sz_decimals=4) == 3000.0


def test_price_strips_trailing_decimal_when_integer():
    # 82357.0 -> integer is allowed, so should come out as 82357.0
    assert _round_price(82357.0, sz_decimals=5) == 82357.0
