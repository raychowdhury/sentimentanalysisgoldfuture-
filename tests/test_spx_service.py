"""Tests for SPX service cache + error handling."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from market import spx_service


@pytest.fixture(autouse=True)
def _reset():
    spx_service.reset_cache()
    yield
    spx_service.reset_cache()


def _fake_rows(day_pcts: list[float]) -> list[dict]:
    rows = []
    for i, dp in enumerate(day_pcts):
        rows.append({
            "ticker":      f"T{i}",
            "name":        f"N{i}",
            "weight_pct":  5.0,
            "price":       100.0,
            "day_pct":     dp,
            "vol_ratio":   1.0,
            "sector":      "Tech",
        })
    return rows


def test_get_top_influencers_caches_within_ttl():
    call_count = {"n": 0}
    def _fake_build(top_n):
        call_count["n"] += 1
        return {"as_of": "t", "top_n": top_n, "rows": [], "breadth": {"up":0,"down":0,"total":0}, "net_bps": 0.0, "error": None}

    with patch.object(spx_service, "_build_payload", side_effect=_fake_build):
        spx_service.get_top_influencers()
        spx_service.get_top_influencers()
        spx_service.get_top_influencers()

    assert call_count["n"] == 1


def test_get_top_influencers_force_refresh_bypasses_cache():
    call_count = {"n": 0}
    def _fake_build(top_n):
        call_count["n"] += 1
        return {"as_of": "t", "top_n": top_n, "rows": [], "breadth": {"up":0,"down":0,"total":0}, "net_bps": 0.0, "error": None}

    with patch.object(spx_service, "_build_payload", side_effect=_fake_build):
        spx_service.get_top_influencers()
        spx_service.get_top_influencers(force_refresh=True)

    assert call_count["n"] == 2


def test_build_payload_computes_breadth_and_net_bps():
    holdings = [
        {"ticker": "A", "name": "A", "weight_pct": 5.0},
        {"ticker": "B", "name": "B", "weight_pct": 3.0},
        {"ticker": "C", "name": "C", "weight_pct": 1.0},
    ]
    enriched = [
        {**holdings[0], "price": 100.0, "day_pct":  2.0, "vol_ratio": 1.0, "sector": "T", "contrib_bps": 10.0},
        {**holdings[1], "price":  50.0, "day_pct": -1.0, "vol_ratio": 1.0, "sector": "F", "contrib_bps": -3.0},
        {**holdings[2], "price":  25.0, "day_pct":  0.0, "vol_ratio": 1.0, "sector": "H", "contrib_bps":  0.0},
    ]
    with patch.object(spx_service.spx_fetcher, "fetch_holdings", return_value=holdings), \
         patch.object(spx_service.spx_fetcher, "enrich_with_prices", return_value=enriched):
        payload = spx_service._build_payload(top_n=3)

    assert payload["breadth"] == {"up": 1, "down": 1, "total": 3}
    assert payload["net_bps"] == pytest.approx(7.0)
    assert payload["error"] is None
    assert len(payload["rows"]) == 3


@pytest.mark.parametrize("iso,expected", [
    # 2026-04-21 is a Tuesday (weekday).
    ("2026-04-21T14:00:00+00:00", True),   # 10:00 ET — open
    ("2026-04-21T20:30:00+00:00", False),  # 16:30 ET — just closed
    ("2026-04-21T13:29:00+00:00", False),  # 09:29 ET — pre-open
    ("2026-04-21T13:30:00+00:00", True),   # 09:30 ET — open bell
    # 2026-04-18 is a Saturday.
    ("2026-04-18T14:00:00+00:00", False),
])
def test_is_market_open(iso, expected):
    now = datetime.fromisoformat(iso)
    assert spx_service.is_market_open(now) is expected


def test_build_payload_includes_market_open_flag():
    holdings = [{"ticker": "A", "name": "A", "weight_pct": 1.0}]
    enriched = [{**holdings[0], "price": 100.0, "day_pct": 1.0, "vol_ratio": 1.0, "sector": "T", "contrib_bps": 1.0}]
    with patch.object(spx_service.spx_fetcher, "fetch_holdings", return_value=holdings), \
         patch.object(spx_service.spx_fetcher, "enrich_with_prices", return_value=enriched):
        payload = spx_service._build_payload(top_n=1)
    assert "market_open" in payload
    assert isinstance(payload["market_open"], bool)


def test_holdings_cached_across_payload_refreshes():
    holdings_call_count = {"n": 0}
    def _fake_holdings(top_n):
        holdings_call_count["n"] += 1
        return [{"ticker": "A", "name": "A", "weight_pct": 1.0}]

    def _fake_enrich(h):
        return [{**h[0], "price": 100.0, "day_pct": 0.0, "vol_ratio": 1.0, "sector": "T", "contrib_bps": 0.0}]

    with patch.object(spx_service.spx_fetcher, "fetch_holdings", side_effect=_fake_holdings), \
         patch.object(spx_service.spx_fetcher, "enrich_with_prices", side_effect=_fake_enrich):
        spx_service.get_top_influencers(force_refresh=True)
        spx_service.get_top_influencers(force_refresh=True)
        spx_service.get_top_influencers(force_refresh=True)

    # Holdings pulled once, prices re-enriched each time (force_refresh bypasses payload cache).
    assert holdings_call_count["n"] == 1


def test_get_top_influencers_returns_error_payload_on_fetch_failure():
    with patch.object(spx_service, "_build_payload", side_effect=RuntimeError("boom")):
        payload = spx_service.get_top_influencers()
    assert payload["error"] is not None
    assert "boom" in payload["error"]
    assert payload["rows"] == []
