"""Tests for signals/signal_engine — scoring, mapping, veto, gates."""

import pytest

import config
from signals import signal_engine


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(config, "LONG_ONLY",    False)
    monkeypatch.setattr(config, "SMA200_GATE",  False)
    monkeypatch.setattr(config, "SCORE_WEIGHTS", {
        "sentiment": 1.0, "dxy": 1.0, "yield": 1.0, "gold": 1.0,
        "vix": 1.0, "vwap": 1.0, "volume_profile": 1.0,
    })


def test_sentiment_score_buckets():
    assert signal_engine.sentiment_score(None)   == 0
    assert signal_engine.sentiment_score(0.0)    == 0
    assert signal_engine.sentiment_score(0.10)   == 1
    assert signal_engine.sentiment_score(0.20)   == 2
    assert signal_engine.sentiment_score(-0.10)  == -1
    assert signal_engine.sentiment_score(-0.20)  == -2


def test_map_total_thresholds():
    # total >=6 with strong gold → STRONG_BUY
    assert signal_engine._map_total(6, gold_score=2)  == "STRONG_BUY"
    # total >=6 but gold weak → downgraded to BUY
    assert signal_engine._map_total(6, gold_score=1)  == "BUY"
    assert signal_engine._map_total(3, gold_score=0)  == "BUY"
    assert signal_engine._map_total(0, gold_score=0)  == "HOLD"
    assert signal_engine._map_total(-3, gold_score=0) == "SELL"
    assert signal_engine._map_total(-6, gold_score=-2) == "STRONG_SELL"
    assert signal_engine._map_total(-6, gold_score=-1) == "SELL"


def test_veto_blocks_buy_against_down_gold():
    # gold negative should veto BUY
    out = signal_engine.run(
        avg_sentiment=0.1, dxy_score=0, yield_score=0,
        gold_score=-1, vix_score=1, vwap_score=2, vp_score=2,
    )
    assert out["raw_signal"] in ("BUY", "STRONG_BUY")
    assert out["signal"] == "HOLD"
    assert out["veto_applied"] is True


def test_long_only_gate_blocks_sell(monkeypatch):
    monkeypatch.setattr(config, "LONG_ONLY", True)
    out = signal_engine.run(
        avg_sentiment=-0.2, dxy_score=2, yield_score=-2,
        gold_score=-2, vix_score=0, vwap_score=-2, vp_score=-2,
    )
    # raw would be SELL or worse, but long-only strips it down
    assert out["signal"] == "HOLD"


def test_sma200_gate_blocks_buy_when_below(monkeypatch):
    monkeypatch.setattr(config, "SMA200_GATE", True)
    out = signal_engine.run(
        avg_sentiment=0.1, dxy_score=0, yield_score=0,
        gold_score=2, vix_score=1, vwap_score=2, vp_score=2,
        macro_bullish=False,
    )
    assert out["raw_signal"] in ("BUY", "STRONG_BUY")
    assert out["signal"] == "HOLD"


def test_sma200_gate_allows_buy_when_above(monkeypatch):
    monkeypatch.setattr(config, "SMA200_GATE", True)
    out = signal_engine.run(
        avg_sentiment=0.1, dxy_score=0, yield_score=0,
        gold_score=2, vix_score=1, vwap_score=2, vp_score=2,
        macro_bullish=True,
    )
    assert out["signal"] in ("BUY", "STRONG_BUY")


def test_weighted_total_uses_config(monkeypatch):
    monkeypatch.setattr(config, "SCORE_WEIGHTS", {
        "sentiment": 0.5, "dxy": 1.0, "yield": 1.0,
        "gold": 2.0, "vix": 0.0, "vwap": 0.0, "volume_profile": 0.0,
    })
    out = signal_engine.run(
        avg_sentiment=None, dxy_score=0, yield_score=0,
        gold_score=3, vix_score=1, vwap_score=2, vp_score=2,
    )
    # only gold contributes: 3 × 2.0 = 6.0
    assert out["total_score"] == 6.0
