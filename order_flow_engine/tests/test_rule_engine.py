"""Tests for rule_engine — one crafted frame per rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from order_flow_engine.src import feature_engineering as fe
from order_flow_engine.src import rule_engine as re_eng


def _scenario_frame(**overrides) -> pd.DataFrame:
    """Pre-featured frame with the columns rule_engine expects."""
    base = {
        "Close":                    100.0,
        "High":                     101.0,
        "Low":                      99.0,
        "delta_ratio":              0.0,
        "cvd_z":                    0.0,
        "fwd_ret_1":                0.0,
        "atr":                      1.0,
        "atr_pct":                  1.0,   # 1%
        "recent_high":              105.0,
        "recent_low":               95.0,
        "dist_to_recent_high_atr":  5.0,
        "dist_to_recent_low_atr":   5.0,
    }
    rows = []
    for o in overrides.get("rows", [{}]):
        rows.append({**base, **o})
    return pd.DataFrame(rows, index=pd.date_range("2026-01-01", periods=len(rows), freq="15min"))


def test_r1_buyer_down_hits():
    df = _scenario_frame(rows=[{"delta_ratio": 0.7, "fwd_ret_1": -0.02, "atr_pct": 1.0}])
    out = re_eng.apply_rules(df)
    assert out["r1_buyer_down"].iloc[0]


def test_r2_seller_up_hits():
    df = _scenario_frame(rows=[{"delta_ratio": -0.7, "fwd_ret_1": 0.02, "atr_pct": 1.0}])
    out = re_eng.apply_rules(df)
    assert out["r2_seller_up"].iloc[0]


def test_r3_absorption_resistance():
    df = _scenario_frame(rows=[{
        "delta_ratio": 0.7, "fwd_ret_1": 0.00005, "atr_pct": 1.0,
        "dist_to_recent_high_atr": 0.2,
    }])
    out = re_eng.apply_rules(df)
    assert out["r3_absorption_resistance"].iloc[0]


def test_r4_absorption_support():
    df = _scenario_frame(rows=[{
        "delta_ratio": -0.7, "fwd_ret_1": -0.00005, "atr_pct": 1.0,
        "dist_to_recent_low_atr": 0.2,
    }])
    out = re_eng.apply_rules(df)
    assert out["r4_absorption_support"].iloc[0]


def test_r5_bull_trap():
    df = _scenario_frame(rows=[{
        "High": 106.0, "Close": 104.0, "recent_high": 105.0,
        "delta_ratio": 0.5,
    }])
    out = re_eng.apply_rules(df)
    assert out["r5_bull_trap"].iloc[0]


def test_r6_bear_trap():
    df = _scenario_frame(rows=[{
        "Low": 94.0, "Close": 96.0, "recent_low": 95.0,
        "delta_ratio": -0.5,
    }])
    out = re_eng.apply_rules(df)
    assert out["r6_bear_trap"].iloc[0]


def test_r7_cvd_divergence():
    n = 30
    # price rising, cvd_z falling → strong negative correlation
    close = np.linspace(100, 120, n)
    cvd_z = np.linspace(2.0, -2.0, n)
    df = pd.DataFrame({
        "Close": close, "High": close + 1, "Low": close - 1,
        "delta_ratio": [0.0]*n, "cvd_z": cvd_z, "fwd_ret_1": [0.0]*n,
        "atr": [1.0]*n, "atr_pct": [1.0]*n,
        "recent_high": [200]*n, "recent_low": [0]*n,
        "dist_to_recent_high_atr": [5.0]*n, "dist_to_recent_low_atr": [5.0]*n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="15min"))
    out = re_eng.apply_rules(df)
    # Last bar should see the full 20-bar correlation window and hit.
    assert out["r7_cvd_divergence"].iloc[-1]


def test_rule_hit_aggregations():
    df = _scenario_frame(rows=[
        {"delta_ratio": 0.7,  "fwd_ret_1": -0.02, "atr_pct": 1.0},  # R1
        {"delta_ratio": -0.7, "fwd_ret_1": 0.02,  "atr_pct": 1.0},  # R2
        {},                                                          # no hit
    ])
    out = re_eng.apply_rules(df)
    assert out["rule_hit_count"].tolist()[:2] == [1, 1]
    assert "r1_buyer_down" in out["rule_hit_codes"].iloc[0]
    assert "r2_seller_up"  in out["rule_hit_codes"].iloc[1]
    assert out["rule_hit_count"].iloc[2] == 0


def test_rules_for_label_mapping():
    assert "r3_absorption_resistance" in re_eng.rules_for_label("buyer_absorption")
    assert re_eng.rules_for_label("normal_behavior") == []
