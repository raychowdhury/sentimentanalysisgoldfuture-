"""Tests for SPY holdings fetcher + price enrichment. Network calls mocked."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import openpyxl
import pandas as pd
import pytest
import requests

from market import spx_fetcher


@pytest.fixture(autouse=True)
def _clear_sector_cache():
    spx_fetcher._SECTOR_CACHE.clear()
    yield
    spx_fetcher._SECTOR_CACHE.clear()


def _make_xlsx(rows: list[tuple]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(("Fund Name:", "SPY", None, None, None, None, None, None))
    ws.append(("Ticker Symbol:", "SPY", None, None, None, None, None, None))
    ws.append(("Holdings:", "As of 2026-04-20", None, None, None, None, None, None))
    ws.append((None,) * 8)
    ws.append(("Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"))
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_FAKE_ROWS = [
    (f"COMPANY {i}", f"TKR{i}", "ID", "SEDOL", 10.0 - i * 0.2, "-", 1000, "USD")
    for i in range(25)
]


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_fetch_holdings_returns_top_20_sorted_desc():
    xlsx = _make_xlsx(_FAKE_ROWS)
    with patch.object(spx_fetcher.requests, "get", return_value=_FakeResp(xlsx)):
        holdings = spx_fetcher.fetch_holdings(top_n=20)
    assert len(holdings) == 20
    assert holdings[0]["ticker"] == "TKR0"
    assert holdings[0]["weight_pct"] == pytest.approx(10.0)
    assert holdings[-1]["ticker"] == "TKR19"
    weights = [h["weight_pct"] for h in holdings]
    assert weights == sorted(weights, reverse=True)
    for h in holdings:
        assert set(h.keys()) == {"ticker", "name", "weight_pct"}


def test_fetch_holdings_raises_when_insufficient_rows():
    xlsx = _make_xlsx(_FAKE_ROWS[:5])
    with patch.object(spx_fetcher.requests, "get", return_value=_FakeResp(xlsx)):
        with pytest.raises(ValueError, match="got 5"):
            spx_fetcher.fetch_holdings(top_n=20)


def test_fetch_holdings_http_error_propagates():
    with patch.object(spx_fetcher.requests, "get", return_value=_FakeResp(b"", status=500)):
        with pytest.raises(requests.HTTPError):
            spx_fetcher.fetch_holdings(top_n=20)


def test_fetch_holdings_skips_rows_with_missing_weight():
    rows = list(_FAKE_ROWS[:5])
    rows.append(("BAD CO", "BAD", "ID", "SEDOL", None, "-", 0, "USD"))
    xlsx = _make_xlsx(rows)
    with patch.object(spx_fetcher.requests, "get", return_value=_FakeResp(xlsx)):
        holdings = spx_fetcher.fetch_holdings(top_n=5)
    assert "BAD" not in {h["ticker"] for h in holdings}


def test_yf_ticker_translates_dots():
    assert spx_fetcher._yf_ticker("BRK.B") == "BRK-B"
    assert spx_fetcher._yf_ticker("AAPL") == "AAPL"


def test_enrich_with_prices_computes_contribution_bps():
    holdings = [
        {"ticker": "AAA", "name": "A", "weight_pct": 5.0},
        {"ticker": "BBB", "name": "B", "weight_pct": 2.0},
    ]
    # Mock _fetch_one_price: AAA returns +2% day, BBB returns -1% day.
    fake_results = {
        "AAA": {"price": 100.0, "day_pct": 2.0, "vol_ratio": 1.5, "sector": "Tech"},
        "BBB": {"price":  50.0, "day_pct": -1.0, "vol_ratio": 0.8, "sector": "Fin"},
    }
    with patch.object(spx_fetcher, "_fetch_one_price", side_effect=lambda t: fake_results[t]):
        out = spx_fetcher.enrich_with_prices(holdings)

    assert out[0]["contrib_bps"] == pytest.approx(10.0)   # 5.0 * 2.0
    assert out[1]["contrib_bps"] == pytest.approx(-2.0)   # 2.0 * -1.0
    assert out[0]["sector"] == "Tech"
    assert out[1]["price"] == 50.0


def test_enrich_with_prices_tolerates_missing_fields():
    holdings = [{"ticker": "X", "name": "X", "weight_pct": 1.0}]
    missing = {"price": None, "day_pct": None, "vol_ratio": None, "sector": None}
    with patch.object(spx_fetcher, "_fetch_one_price", return_value=missing):
        out = spx_fetcher.enrich_with_prices(holdings)
    assert out[0]["contrib_bps"] is None
    assert out[0]["day_pct"] is None


def test_fetch_sector_caches_result():
    call_count = {"n": 0}
    fake_ticker = MagicMock()
    def _side_effect(_):
        call_count["n"] += 1
        return fake_ticker
    fake_ticker.info = {"sector": "Technology"}

    with patch.object(spx_fetcher.yf, "Ticker", side_effect=_side_effect):
        s1 = spx_fetcher._fetch_sector("AAPL")
        s2 = spx_fetcher._fetch_sector("AAPL")
        s3 = spx_fetcher._fetch_sector("AAPL")

    assert s1 == s2 == s3 == "Technology"
    assert call_count["n"] == 1


def test_fetch_one_price_computes_day_pct_and_vol_ratio():
    idx = pd.date_range("2026-03-01", periods=25, freq="B")
    close_vals = [100.0] * 24 + [102.0]
    vol_vals   = [1_000_000.0] * 24 + [1_500_000.0]
    hist = pd.DataFrame({"Close": close_vals, "Volume": vol_vals}, index=idx)

    fake_ticker = MagicMock()
    fake_ticker.history.return_value = hist
    fake_ticker.info = {"sector": "Information Technology"}

    with patch.object(spx_fetcher.yf, "Ticker", return_value=fake_ticker):
        out = spx_fetcher._fetch_one_price("AAPL")

    assert out["price"] == pytest.approx(102.0)
    assert out["day_pct"] == pytest.approx(2.0)
    assert out["vol_ratio"] == pytest.approx(1.5)
    assert out["sector"] == "Information Technology"
