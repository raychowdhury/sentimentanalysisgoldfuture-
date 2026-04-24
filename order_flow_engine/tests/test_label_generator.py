"""Tests for label_generator — class assignment, leakage guard."""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_engine.src import label_generator as lg


def _bar_frame(rows: list[dict]) -> pd.DataFrame:
    """Enrich per-row dict with default rule booleans and required columns."""
    default_rules = {
        "r1_buyer_down": False, "r2_seller_up": False,
        "r3_absorption_resistance": False, "r4_absorption_support": False,
        "r5_bull_trap": False, "r6_bear_trap": False,
        "r7_cvd_divergence": False,
    }
    filled = []
    for r in rows:
        row = {
            "Open": 100.0, "Close": 100.0,
            "High": 101.0, "Low": 99.0,
            "recent_high": 105.0, "recent_low": 95.0,
            "atr": 1.0, "atr_pct": 1.0,
            "fwd_ret_n": 0.0,
            "rule_hit_count": 0,
            **default_rules,
            **r,
        }
        filled.append(row)
    idx = pd.date_range("2026-01-01", periods=len(filled), freq="15min")
    return pd.DataFrame(filled, index=idx)


def test_normal_label_when_no_rules_and_small_move():
    df = _bar_frame([{"fwd_ret_n": 0.0005}])  # 0.05% move, atr_pct 1%
    labels = lg.generate_labels(df, "15m")
    assert labels.iloc[0] == "normal_behavior"


def test_buyer_absorption_label():
    # 20 rows: last one is the absorption bar; the following 8 (horizon) drop.
    rows = [{} for _ in range(20)]
    # Bar index 10 is the absorption setup; subsequent closes need to drop.
    rows[10] = {
        "r3_absorption_resistance": True,
        "rule_hit_count": 1,
        "Open": 100, "Close": 100,
        "High": 101, "Low": 99.5,
        "atr": 1.0, "atr_pct": 1.0,
        "fwd_ret_n": -0.02,
    }
    df = _bar_frame(rows)
    # Synthesize forward price drop of ~2*ATR over 8 bars
    closes = df["Close"].to_numpy().astype(float)
    for i in range(11, 11 + 8):
        if i < len(closes):
            closes[i] = 100 - (i - 10) * 0.3
    df["Close"] = closes
    labels = lg.generate_labels(df, "15m")
    assert labels.iloc[10] == "buyer_absorption"


def test_bullish_trap_label():
    n = 10
    rows = [{} for _ in range(n)]
    rows[3] = {
        "r5_bull_trap": True, "rule_hit_count": 1,
        "High": 106.0, "Close": 104.5,
        "recent_high": 105.0, "fwd_ret_n": -0.01,
    }
    df = _bar_frame(rows)
    # Force next 3 closes < recent_high(105)
    closes = df["Close"].to_numpy().astype(float)
    for i in range(4, 7):
        closes[i] = 103.0
    df["Close"] = closes
    labels = lg.generate_labels(df, "15m")
    assert labels.iloc[3] == "bullish_trap"


def test_feature_columns_strips_leakage():
    df = pd.DataFrame({
        "delta_ratio":   [0.1, 0.2],
        "fwd_ret_1":     [0.01, 0.02],
        "fwd_ret_n":     [0.01, 0.02],
        "fwd_atr_move":  [1.0, 1.0],
        "label":         ["x", "y"],
        "Close":         [100, 101],
        "rule_hit_codes":["", "r1"],
        "cvd_z":         [0.1, 0.2],
        "recent_high":   [110, 111],
    })
    cols = lg.feature_columns(df)
    assert "delta_ratio" in cols
    assert "cvd_z" in cols
    # Leakage and raw OHLCV are stripped
    for bad in ["fwd_ret_1", "fwd_ret_n", "fwd_atr_move",
                "label", "Close", "rule_hit_codes", "recent_high"]:
        assert bad not in cols


def test_label_distribution_includes_all_classes():
    labels = pd.Series(["normal_behavior", "normal_behavior", "bullish_trap"])
    dist = lg.label_distribution(labels)
    assert set(dist.keys()) == set(lg.LABEL_CLASSES)
    assert dist["normal_behavior"] == 2
    assert dist["bullish_trap"] == 1
    assert dist["buyer_absorption"] == 0
