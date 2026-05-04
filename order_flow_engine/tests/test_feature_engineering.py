"""Tests for feature_engineering — CLV math, proxy invariants, multi-TF join."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from order_flow_engine.src import feature_engineering as fe


def test_clv_bull_bar_near_high():
    df = pd.DataFrame({"Open": [100], "High": [110], "Low": [99],
                       "Close": [109.5], "Volume": [1000]})
    clv = fe.compute_clv(df).iloc[0]
    # closed near top of range → CLV close to +1
    assert clv > 0.8


def test_clv_zero_range_is_nan():
    df = pd.DataFrame({"Open": [100], "High": [100], "Low": [100],
                       "Close": [100], "Volume": [500]})
    clv = fe.compute_clv(df).iloc[0]
    assert np.isnan(clv)


def test_buy_sell_sum_equals_volume():
    # Non-degenerate bars — CLV path.
    rng = np.random.default_rng(1)
    n = 50
    close = 100 + rng.normal(0, 1, n)
    high = close + 1
    low  = close - 1
    df = pd.DataFrame({
        "Open": close, "High": high, "Low": low, "Close": close,
        "Volume": rng.integers(100, 1000, n).astype(float),
    })
    out = fe.add_orderflow_proxies(df)
    total = out["buy_vol"] + out["sell_vol"]
    assert np.allclose(total.to_numpy(), df["Volume"].to_numpy(), atol=1e-6)


def test_cvd_monotonic_on_all_bullish_bars():
    """All bars close at their high → every buy_vol == Volume → CVD strictly up."""
    n = 10
    df = pd.DataFrame({
        "Open":   [100.0] * n,
        "High":   [101.0] * n,
        "Low":    [99.5]  * n,
        "Close":  [101.0] * n,     # closes at high → CLV == +1
        "Volume": [1000.0] * n,
    })
    out = fe.add_orderflow_proxies(df)
    assert (out["cvd"].diff().dropna() > 0).all()


def test_zero_volume_does_not_nan():
    df = pd.DataFrame({
        "Open":   [100.0, 101.0],
        "High":   [101.0, 102.0],
        "Low":    [99.0,  100.0],
        "Close":  [100.5, 101.5],
        "Volume": [0.0,   0.0],
    })
    out = fe.add_orderflow_proxies(df)
    assert not out["delta_ratio"].isna().any()
    assert (out["delta_ratio"] == 0).all()


def test_price_features_no_negative_wicks():
    df = pd.DataFrame({
        "Open":   [100.0, 101.0, 102.0],
        "High":   [101.0, 102.0, 103.0],
        "Low":    [99.0,  100.0, 101.0],
        "Close":  [100.5, 101.5, 102.5],
        "Volume": [1000.0, 1000.0, 1000.0],
    })
    out = fe.add_price_features(df)
    assert (out["upper_wick"] >= 0).all()
    assert (out["lower_wick"] >= 0).all()


def test_multi_tf_join_no_leakage():
    """Joined higher-TF columns must come from bars at or before the anchor ts."""
    anchor = pd.DataFrame({
        "Close":       [1, 2, 3, 4, 5],
        "delta_ratio": [.1, .2, .3, .4, .5],
    }, index=pd.date_range("2026-01-01 00:00", periods=5, freq="15min", tz="UTC"))

    higher = pd.DataFrame({
        "delta_ratio": [0.9, 0.8],
        "cvd_z":       [1.5, -1.5],
        "atr_pct":     [0.2, 0.4],
    }, index=pd.date_range("2026-01-01 00:00", periods=2, freq="1h", tz="UTC"))

    merged = fe.build_feature_matrix({"15m": anchor, "1h": higher}, anchor_tf="15m")
    # First anchor bar (00:00) should pull higher[00:00] — value 0.9.
    assert merged["delta_ratio_1h"].iloc[0] == pytest.approx(0.9)
    # Anchor bars 00:15, 00:30, 00:45 should still see higher[00:00] (backward fill).
    assert merged["delta_ratio_1h"].iloc[3] == pytest.approx(0.9)
    # 01:00 anchor bar should pick up higher[01:00] == 0.8.
    assert merged["delta_ratio_1h"].iloc[4] == pytest.approx(0.8)


def test_build_features_for_tf_adds_expected_columns(synthetic_ohlcv):
    out = fe.build_features_for_tf(synthetic_ohlcv, "15m")
    expected = {
        "delta_ratio", "cvd", "cvd_z", "clv",
        "atr", "atr_pct", "body", "upper_wick", "lower_wick",
        "fwd_ret_1", "fwd_ret_n", "fwd_atr_move",
        "recent_high", "recent_low",
        "dist_to_recent_high_atr", "dist_to_recent_low_atr", "near_sr_flag",
    }
    missing = expected - set(out.columns)
    assert not missing, f"missing columns: {missing}"
