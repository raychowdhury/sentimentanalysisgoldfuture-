"""Tests for Alpaca tick-rule aggregator."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from order_flow_engine.src import ingest
from order_flow_engine.src.realtime_alpaca import TickRuleAggregator, _parse_ts


def test_tick_rule_uptick_is_buy():
    agg = TickRuleAggregator("SPY", "1m")
    t0 = 1_700_000_000
    agg.add_trade(t0,     price=444.0, size=100)
    agg.add_trade(t0 + 1, price=444.5, size=200)   # uptick → buy
    assert agg.buy_v == pytest.approx(300)         # first (no prior) + uptick both buy
    assert agg.sell_v == 0


def test_tick_rule_downtick_is_sell():
    agg = TickRuleAggregator("SPY", "1m")
    t0 = 1_700_000_000
    agg.add_trade(t0,     price=444.0, size=100)
    agg.add_trade(t0 + 1, price=443.5, size=200)   # downtick → sell
    assert agg.buy_v == 100
    assert agg.sell_v == 200


def test_tick_rule_zero_tick_split():
    agg = TickRuleAggregator("SPY", "1m")
    t0 = 1_700_000_000
    agg.add_trade(t0,     price=444.0, size=100)
    agg.add_trade(t0 + 1, price=444.0, size=200)   # zero-tick → 50/50 split
    assert agg.buy_v == pytest.approx(100 + 100)
    assert agg.sell_v == pytest.approx(100)


def test_aggregator_ships_on_minute_rollover():
    agg = TickRuleAggregator("SPY", "1m")
    t0 = 1_700_000_000
    captured = []
    with patch.object(ingest, "ingest_bar", side_effect=lambda **kw: captured.append(kw)):
        agg.add_trade(t0,        price=444.0, size=100)
        agg.add_trade(t0 + 70,   price=445.0, size=200)   # next minute
    assert len(captured) == 1
    bar = captured[0]
    assert bar["symbol"] == "SPY"
    assert bar["close"] == 444.0
    assert bar["volume"] == 100
    assert bar["buy_vol"] == 100   # first trade defaults to buy (no prior)
    assert bar["sell_vol"] == 0


def test_parse_ts_handles_ns_iso():
    s = "2026-04-23T19:45:00.123456789Z"
    epoch = _parse_ts(s)
    assert isinstance(epoch, float)
    # crude bound: should be close to ts of 2026-04-23 19:45 UTC
    assert 1_776_000_000 < epoch < 1_900_000_000
