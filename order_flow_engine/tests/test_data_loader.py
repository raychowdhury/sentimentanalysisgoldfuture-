"""Tests for data_loader: schema detection, yf-cap awareness, CSV import."""

from __future__ import annotations

import pandas as pd
import pytest

from order_flow_engine.src import config as of_cfg, data_loader


def test_detect_schema_tick():
    df = pd.DataFrame({
        "bid_size": [1, 2], "ask_size": [3, 4], "trade_side": [1, -1],
        "Close": [100, 101],
    })
    assert data_loader.detect_schema(df) == "tick"


def test_detect_schema_ohlcv():
    df = pd.DataFrame({
        "Open": [1], "High": [2], "Low": [0.5], "Close": [1.5], "Volume": [100],
    })
    assert data_loader.detect_schema(df) == "ohlcv"


def test_capped_period_5m_clipped_to_60():
    assert data_loader._capped_period("5m", 180) == 60


def test_capped_period_1h_unchanged_below_cap():
    assert data_loader._capped_period("1h", 180) == 180


def test_capped_period_daily_unrestricted():
    # Daily is not in YF_INTRADAY_CAPS so requested value passes through.
    assert data_loader._capped_period("1d", 500) == 500


def test_load_from_file_csv(tmp_path):
    csv = tmp_path / "sample.csv"
    pd.DataFrame({
        "Open": [1, 2], "High": [2, 3], "Low": [0.5, 1],
        "Close": [1.5, 2.5], "Volume": [100, 200],
    }, index=pd.to_datetime(["2026-01-01", "2026-01-02"])).to_csv(csv)
    df = data_loader.load_from_file(csv)
    assert len(df) == 2
    assert "Close" in df.columns


def test_has_usable_volume_true():
    df = pd.DataFrame({"Volume": [0, 10, 20]})
    assert data_loader.has_usable_volume(df)


def test_has_usable_volume_false_all_zero():
    df = pd.DataFrame({"Volume": [0, 0, 0]})
    assert not data_loader.has_usable_volume(df)


def test_has_usable_volume_missing_column():
    assert not data_loader.has_usable_volume(pd.DataFrame({"Close": [1]}))


def test_fetch_ohlcv_uses_cache(tmp_path, monkeypatch):
    """If a parquet cache exists, we should skip the network fetch."""
    monkeypatch.setattr(of_cfg, "OF_RAW_DIR", tmp_path)
    cache = tmp_path / "ES_F_15m.parquet"
    df = pd.DataFrame({"Close": [1.0, 2.0]},
                      index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    df.to_parquet(cache)

    def fail(*a, **kw):
        pytest.fail("network fetch should not be called when cache exists")

    monkeypatch.setattr("market.data_fetcher.fetch_series", fail)
    monkeypatch.setattr("market.data_fetcher.fetch_intraday", fail)

    result = data_loader.fetch_ohlcv("ES=F", "15m", 60)
    assert result is not None
    assert list(result["Close"]) == [1.0, 2.0]
