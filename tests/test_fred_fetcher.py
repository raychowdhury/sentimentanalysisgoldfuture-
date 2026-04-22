"""Tests for FRED CSV fetcher — network calls mocked."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
import requests

from market import fred_fetcher


class _FakeResp:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


_GOOD_CSV = (
    "observation_date,DFII10\n"
    "2025-04-10,1.95\n"
    "2025-04-11,1.98\n"
    "2025-04-14,2.01\n"
    "2025-04-15,.\n"  # FRED's "missing" marker
    "2025-04-16,2.03\n"
)


def test_fetch_series_parses_csv():
    with patch.object(fred_fetcher.requests, "get", return_value=_FakeResp(_GOOD_CSV)):
        df = fred_fetcher.fetch_series("DFII10", 30)
    assert df is not None
    assert len(df) == 4  # the "." row is dropped
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df["Close"].iloc[-1] == 2.03
    # OHLC all equal, volume zero.
    row = df.iloc[-1]
    assert row["Open"] == row["High"] == row["Low"] == row["Close"]
    assert int(row["Volume"]) == 0


def test_fetch_series_respects_lookback_cap():
    # Synthesize 50 rows, request only 10.
    dates = pd.date_range("2025-01-01", periods=50, freq="B")
    csv_text = "observation_date,DFII10\n" + "\n".join(
        f"{d.date()},{1.0 + i * 0.01:.2f}" for i, d in enumerate(dates)
    )
    with patch.object(fred_fetcher.requests, "get", return_value=_FakeResp(csv_text)):
        df = fred_fetcher.fetch_series("DFII10", 10)
    assert df is not None
    assert len(df) == 10


def test_fetch_series_http_failure_returns_none():
    def _boom(*a, **kw):
        raise requests.ConnectionError("boom")
    with patch.object(fred_fetcher.requests, "get", side_effect=_boom):
        assert fred_fetcher.fetch_series("DFII10", 30) is None


def test_fetch_series_empty_csv_returns_none():
    with patch.object(fred_fetcher.requests, "get", return_value=_FakeResp("")):
        assert fred_fetcher.fetch_series("DFII10", 30) is None


def test_fetch_series_all_missing_rows_returns_none():
    csv_text = "observation_date,DFII10\n2025-04-10,.\n2025-04-11,.\n"
    with patch.object(fred_fetcher.requests, "get", return_value=_FakeResp(csv_text)):
        assert fred_fetcher.fetch_series("DFII10", 30) is None


def test_fetch_series_400_returns_none():
    with patch.object(fred_fetcher.requests, "get", return_value=_FakeResp("x", status=500)):
        assert fred_fetcher.fetch_series("DFII10", 30) is None
