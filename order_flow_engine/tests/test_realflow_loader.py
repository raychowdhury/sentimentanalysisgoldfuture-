"""Smoke tests for the realflow_loader module.

Covers schema validation and 1m → 15m resample arithmetic. No network /
no parquet I/O — tests exercise resample_to_tf with synthetic frames.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from order_flow_engine.src import realflow_loader as rfl


def _synthetic_1m(n: int = 30) -> pd.DataFrame:
    """30 minutes of 1m bars with deterministic OHLCV + real-flow split."""
    idx = pd.date_range("2026-04-30 00:00:00", periods=n, freq="1min", tz="UTC")
    close = 100.0 + np.arange(n)
    return pd.DataFrame(
        {
            "Open":          close - 0.25,
            "High":          close + 0.50,
            "Low":           close - 0.50,
            "Close":         close,
            "Volume":        np.full(n, 100.0),
            "buy_vol_real":  np.full(n, 60.0),
            "sell_vol_real": np.full(n, 40.0),
        },
        index=idx,
    )


def test_resample_15m_aggregations():
    df = _synthetic_1m(30)
    out = rfl.resample_to_tf(df, "15m")

    # 30 1-min bars → 2 15-min buckets (00:00–00:14, 00:15–00:29).
    assert len(out) == 2
    assert list(out.columns) == [
        "Open", "High", "Low", "Close",
        "Volume", "buy_vol_real", "sell_vol_real",
    ]

    first = out.iloc[0]
    # Open = first bar's Open of the bucket; Close = last bar's Close.
    assert first["Open"]  == pytest.approx(99.75)
    assert first["Close"] == pytest.approx(114.0)
    # High/Low across the 15 bars in the bucket.
    assert first["High"] == pytest.approx(114.5)
    assert first["Low"]  == pytest.approx(99.5)
    # Sums.
    assert first["Volume"]        == pytest.approx(1500.0)
    assert first["buy_vol_real"]  == pytest.approx(900.0)
    assert first["sell_vol_real"] == pytest.approx(600.0)


def test_resample_unsupported_tf():
    with pytest.raises(ValueError, match="Unsupported tf"):
        rfl.resample_to_tf(_synthetic_1m(5), "13m")


def test_required_cols_constant():
    # Guard against accidental schema drift.
    assert "buy_vol_real" in rfl.REQUIRED_COLS
    assert "sell_vol_real" in rfl.REQUIRED_COLS
    assert all(c in rfl.REQUIRED_COLS
               for c in ("Open", "High", "Low", "Close", "Volume"))


def test_load_realflow_missing(tmp_path, monkeypatch):
    # Point OF_PROCESSED_DIR at an empty tmp dir so neither file exists.
    from order_flow_engine.src import config as of_cfg
    monkeypatch.setattr(of_cfg, "OF_PROCESSED_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        rfl.load_realflow("ZZZ6", "15m")
