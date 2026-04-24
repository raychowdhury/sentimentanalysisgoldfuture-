"""Tests for missing-data resilience."""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_engine.src import feature_engineering as fe


def test_no_volume_column_fallback_to_tick_rule():
    df = pd.DataFrame({
        "Open":  [100, 101, 102, 101],
        "High":  [101, 102, 103, 102],
        "Low":   [99,  100, 101, 100],
        "Close": [101, 102, 101, 100],   # up, up, down, down → tick-rule signs
    })
    # Engineer a Volume column but keep it all zero → CLV math gives 50/50
    # buy_share but should return zeros (no volume to distribute).
    df["Volume"] = 0.0
    out = fe.add_orderflow_proxies(df)
    assert not out["delta_ratio"].isna().any()
    # With zero volume, buy_vol + sell_vol == 0.
    assert (out["buy_vol"] + out["sell_vol"] == 0).all()


def test_all_zero_range_bars_use_tick_rule():
    # H == L for every bar; CLV is NaN so tick-rule path activates.
    df = pd.DataFrame({
        "Open":   [100, 100, 100, 100],
        "High":   [100, 100, 100, 100],
        "Low":    [100, 100, 100, 100],
        "Close":  [100, 101, 100, 99],
        "Volume": [10.0, 10.0, 10.0, 10.0],
    })
    out = fe.add_orderflow_proxies(df)
    # With H==L the CLV is NaN; tick rule on Close diff determines split.
    # Second bar closes up vs first → all volume buy-side.
    assert out["buy_vol"].iloc[1] == 10.0
    assert out["sell_vol"].iloc[1] == 0.0
    # Fourth bar closes down vs prior → all volume sell-side.
    assert out["buy_vol"].iloc[3] == 0.0
    assert out["sell_vol"].iloc[3] == 10.0


def test_nan_warmup_rows_dont_crash_sr_features():
    """First bars have NaN ATR (warmup) — S/R features must not blow up."""
    rng = np.random.default_rng(0)
    n = 30
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "Open":  close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
        "Volume": rng.integers(100, 500, n).astype(float),
    }, index=pd.date_range("2026-01-01", periods=n, freq="15min"))
    out = fe.build_features_for_tf(df, "15m")
    # No crash; expected cols present; some NaN allowed on warmup but no infinities
    # outside what we intentionally use (dist_to_recent_*_atr sentinel).
    assert "cvd" in out.columns
    # fwd_atr_move may be 0 on warmup NaN ATR rows (filled with 0)
    assert out["fwd_atr_move"].iloc[0] == 0.0 or np.isfinite(out["fwd_atr_move"].iloc[0])
