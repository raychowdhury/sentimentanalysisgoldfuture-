"""Tests for backtest/engine._simulate — stop/TP/partial/TIME outcomes."""

import pandas as pd
import pytest

import config
from backtest import engine


def _make_df(rows):
    """rows = list of (open, high, low, close) starting on 2025-01-02."""
    idx = pd.date_range("2025-01-02", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000
    return df


def _setup(entry=100.0, stop=95.0, tp=115.0, atr=1.0):
    return {
        "entry_price": entry,
        "stop_loss":   stop,
        "take_profit": tp,
        "level2":      {"atr": atr},
    }


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setattr(config, "TRAIL_ENABLED",       False)
    monkeypatch.setattr(config, "PARTIAL_TP_ENABLED",  False)
    monkeypatch.setattr(config, "PARTIAL_TP_R",        1.5)
    monkeypatch.setattr(config, "PARTIAL_TP_FRACTION", 0.5)


def test_buy_hits_tp():
    # bar1 rips above TP (115)
    df = _make_df([
        (100, 100, 100, 100),
        (100, 120, 100, 118),
    ])
    exit_rec, idx = engine._simulate(df, 0, _setup(), "BUY", max_hold=5)
    assert exit_rec["exit_reason"] == "TP"
    assert exit_rec["pnl"] == pytest.approx(15.0)
    assert idx == 1


def test_buy_hits_stop():
    df = _make_df([
        (100, 100, 100, 100),
        (100, 101, 94, 96),   # low 94 < stop 95
    ])
    exit_rec, _ = engine._simulate(df, 0, _setup(), "BUY", max_hold=5)
    assert exit_rec["exit_reason"] == "STOP"
    assert exit_rec["pnl"] == pytest.approx(-5.0)


def test_buy_time_exit_when_neither_hits():
    df = _make_df([
        (100, 100, 100, 100),
        (100, 108, 98, 105),
        (105, 110, 100, 108),
        (108, 112, 104, 110),
    ])
    exit_rec, idx = engine._simulate(df, 0, _setup(), "BUY", max_hold=3)
    assert exit_rec["exit_reason"] == "TIME"
    assert idx == 3
    assert exit_rec["exit_price"] == pytest.approx(110.0)


def test_stop_takes_priority_when_both_hit_same_bar():
    # Low 94 and high 120 on the same bar — engine is pessimistic.
    df = _make_df([
        (100, 100, 100, 100),
        (100, 120, 94, 100),
    ])
    exit_rec, _ = engine._simulate(df, 0, _setup(), "BUY", max_hold=5)
    assert exit_rec["exit_reason"] == "STOP"


def test_sell_hits_tp():
    # direction=SELL, entry=100, stop=105, tp=85
    df = _make_df([
        (100, 100, 100, 100),
        (100, 100, 80, 82),   # low 80 <= tp 85
    ])
    setup = _setup(entry=100, stop=105, tp=85)
    exit_rec, _ = engine._simulate(df, 0, setup, "SELL", max_hold=5)
    assert exit_rec["exit_reason"] == "TP"
    assert exit_rec["pnl"] == pytest.approx(15.0)


def test_partial_tp_banks_half_and_exits_at_be_on_reversal(monkeypatch):
    monkeypatch.setattr(config, "PARTIAL_TP_ENABLED", True)
    # risk=5, partial_r=1.5 → partial_level = 107.5
    # bar1 tags 110 (partial taken), bar2 drops below entry stop moved to BE.
    df = _make_df([
        (100, 100, 100, 100),
        (100, 110, 100, 108),
        (108, 108,  99, 99),   # low 99 <= BE stop 100
    ])
    exit_rec, _ = engine._simulate(df, 0, _setup(), "BUY", max_hold=5)
    assert exit_rec["exit_reason"] == "PARTIAL+BE"
    # realized = (107.5 - 100) * 0.5 = 3.75 ; remainder (100-100)*0.5 = 0
    assert exit_rec["pnl"] == pytest.approx(3.75)


def test_partial_tp_then_final_tp(monkeypatch):
    monkeypatch.setattr(config, "PARTIAL_TP_ENABLED", True)
    df = _make_df([
        (100, 100, 100, 100),
        (100, 110, 101, 108),   # tags 107.5 → partial taken (BE stop = 100)
        (108, 120, 108, 118),   # tags tp 115 → final exit
    ])
    exit_rec, _ = engine._simulate(df, 0, _setup(), "BUY", max_hold=5)
    assert exit_rec["exit_reason"] == "PARTIAL+TP"
    # realized = 7.5 * 0.5 = 3.75 ; final = 15 * 0.5 = 7.5 ; total 11.25
    assert exit_rec["pnl"] == pytest.approx(11.25)


def test_regime_classification():
    # flat series
    df_flat = _make_df([(100, 100, 100, 100)] * 70)
    assert engine._regime(df_flat, 65) == "flat"

    # bull: index 0 = 100 → index 65 = 110 → +10 % > 5 %
    rows = [(100, 100, 100, 100)] * 70
    rows[65] = (110, 110, 110, 110)
    df_bull = _make_df(rows)
    assert engine._regime(df_bull, 65) == "bull"

    # bear
    rows[65] = (90, 90, 90, 90)
    df_bear = _make_df(rows)
    assert engine._regime(df_bear, 65) == "bear"
