"""Unit tests for IBKR QuoteTickAggregator — side classification + bucketing."""

from __future__ import annotations

from unittest.mock import patch

from order_flow_engine.src.realtime_ibkr import QuoteTickAggregator


def test_quote_rule_classifies_side():
    agg = QuoteTickAggregator("ES", "5m")
    agg.set_quote(bid=5000.00, ask=5000.50)  # mid = 5000.25
    t0 = 1_700_000_000

    # Trade at ask → buy
    agg.add_trade(t0, price=5000.50, size=2)
    assert agg.buy_v == 2 and agg.sell_v == 0

    # Trade at bid → sell
    agg.add_trade(t0 + 1, price=5000.00, size=3)
    assert agg.buy_v == 2 and agg.sell_v == 3

    # Trade above ask → buy
    agg.add_trade(t0 + 2, price=5001.00, size=1)
    assert agg.buy_v == 3 and agg.sell_v == 3


def test_tick_rule_fallback_without_quote():
    agg = QuoteTickAggregator("ES", "5m")
    t0 = 1_700_000_000
    # First trade: no prior price → split 50/50
    agg.add_trade(t0, price=5000.00, size=2)
    assert agg.buy_v == 1 and agg.sell_v == 1
    # Uptick → buy
    agg.add_trade(t0 + 1, price=5000.25, size=4)
    assert agg.buy_v == 5 and agg.sell_v == 1
    # Downtick → sell
    agg.add_trade(t0 + 2, price=5000.00, size=2)
    assert agg.buy_v == 5 and agg.sell_v == 3


def test_bar_bucketing_ships_on_boundary_cross():
    agg = QuoteTickAggregator("ES", "5m")
    agg.set_quote(5000.00, 5000.50)
    # Two trades in same bucket, then one in next bucket.
    t0 = 1_700_000_000  # bar_seconds=300 → bucket = t0 - (t0 % 300)
    with patch("order_flow_engine.src.realtime_ibkr.ingest.ingest_bar") as m:
        agg.add_trade(t0,     price=5000.50, size=1)  # buy
        agg.add_trade(t0 + 60, price=5000.00, size=2)  # sell (same bucket)
        # Cross into next 5-min bucket → first bucket ships
        agg.add_trade(t0 + 360, price=5000.50, size=1)
        assert m.call_count == 1
        kwargs = m.call_args.kwargs
        assert kwargs["buy_vol"] == 1
        assert kwargs["sell_vol"] == 2
        assert kwargs["volume"] == 3
        assert kwargs["open_"] == 5000.50
        assert kwargs["high"]  == 5000.50
        assert kwargs["low"]   == 5000.00


def test_force_close_ships_partial_bar():
    agg = QuoteTickAggregator("ES", "15m")
    agg.set_quote(5000, 5000.5)
    with patch("order_flow_engine.src.realtime_ibkr.ingest.ingest_bar") as m:
        agg.add_trade(1_700_000_000, price=5000.5, size=5)
        agg.force_close()
        assert m.call_count == 1
        assert m.call_args.kwargs["buy_vol"] == 5


def test_bad_timeframe_raises():
    import pytest
    with pytest.raises(ValueError):
        QuoteTickAggregator("ES", "2m")
