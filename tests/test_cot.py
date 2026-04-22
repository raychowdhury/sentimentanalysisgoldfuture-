"""Tests for CFTC COT fetcher + scoring."""

from __future__ import annotations

import json
import os
from datetime import date
from unittest.mock import patch

import pytest

import config
from positioning import cot_fetcher, cot_scoring


@pytest.fixture
def tmp_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_score_mapping_buckets():
    # z=0 neutral
    assert cot_scoring.score_from_zscore(0.0) == 0
    # mild long → -1 (fade)
    assert cot_scoring.score_from_zscore(1.5) == -1
    # extreme long → -2
    assert cot_scoring.score_from_zscore(2.5) == -2
    # mild short → +1
    assert cot_scoring.score_from_zscore(-1.5) == +1
    # extreme short → +2
    assert cot_scoring.score_from_zscore(-2.5) == +2
    # None → 0
    assert cot_scoring.score_from_zscore(None) == 0


def test_zscore_insufficient_history_returns_none():
    recs = [{"date": f"2025-01-0{i+1}", "mm_net": i * 100, "mm_long": 0, "mm_short": 0, "oi": 0}
            for i in range(5)]
    assert cot_scoring.compute_zscore_at(recs, date(2025, 2, 1)) is None


def test_zscore_constant_returns_zero():
    recs = [{"date": f"2025-{m:02d}-01", "mm_net": 50000, "mm_long": 0, "mm_short": 0, "oi": 0}
            for m in range(1, 13)]
    recs += [{"date": f"2026-{m:02d}-01", "mm_net": 50000, "mm_long": 0, "mm_short": 0, "oi": 0}
             for m in range(1, 6)]
    assert cot_scoring.compute_zscore_at(recs, date(2026, 6, 1)) == 0.0


def test_zscore_detects_extreme_long():
    # 20 weeks at net=10000, then spike to net=100000.
    recs = [{"date": f"2025-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d}",
             "mm_net": 10000, "mm_long": 0, "mm_short": 0, "oi": 0}
            for i in range(20)]
    recs.append({"date": "2025-06-01", "mm_net": 100000,
                 "mm_long": 0, "mm_short": 0, "oi": 0})
    z = cot_scoring.compute_zscore_at(recs, date(2025, 6, 1))
    assert z is not None and z > 2.0
    assert cot_scoring.score_from_zscore(z) == -2  # fade crowded long


def test_zscore_detects_extreme_short():
    recs = [{"date": f"2025-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d}",
             "mm_net": 10000, "mm_long": 0, "mm_short": 0, "oi": 0}
            for i in range(20)]
    recs.append({"date": "2025-06-01", "mm_net": -80000,
                 "mm_long": 0, "mm_short": 0, "oi": 0})
    z = cot_scoring.compute_zscore_at(recs, date(2025, 6, 1))
    assert z is not None and z < -2.0
    assert cot_scoring.score_from_zscore(z) == +2  # fade crowded short


def test_score_at_uses_latest_record_before_date():
    recs = [
        {"date": "2025-01-07", "mm_net": 50000, "mm_long": 0, "mm_short": 0, "oi": 0},
        {"date": "2025-01-14", "mm_net": 55000, "mm_long": 0, "mm_short": 0, "oi": 0},
        {"date": "2025-01-21", "mm_net": 90000, "mm_long": 0, "mm_short": 0, "oi": 0},
    ]
    # Query between 14th and 21st — picks the 14th row, sample too small → None → 0.
    assert cot_scoring.score_at(recs, date(2025, 1, 18)) == 0


def test_fetcher_load_empty_when_missing(tmp_output_dir):
    assert cot_fetcher.load() == []


def test_fetcher_load_round_trip(tmp_output_dir):
    recs = [
        {"date": "2025-01-07", "mm_long": 100, "mm_short": 50, "mm_net": 50, "oi": 1000},
        {"date": "2025-01-14", "mm_long": 120, "mm_short": 40, "mm_net": 80, "oi": 1100},
    ]
    path = os.path.join(tmp_output_dir, cot_fetcher.CACHE_FILENAME)
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    loaded = cot_fetcher.load()
    assert len(loaded) == 2
    assert loaded[0]["date"] == "2025-01-07"
    assert loaded[-1]["mm_net"] == 80


def test_refresh_http_failure_returns_zero(tmp_output_dir):
    import requests
    def _boom(*a, **kw):
        raise requests.ConnectionError("nope")
    with patch.object(cot_fetcher.requests, "get", side_effect=_boom):
        assert cot_fetcher.refresh() == 0


def test_signal_engine_cot_adds_to_total():
    from signals import signal_engine

    # Baseline: all zeros, BUT use a mild bullish gold to make a non-HOLD raw total.
    result_zero = signal_engine.run(
        avg_sentiment=0.0, dxy_score=0, yield_score=0, gold_score=0,
        vix_score=0, vwap_score=0, vp_score=0, cot_score=0,
    )
    result_cot  = signal_engine.run(
        avg_sentiment=0.0, dxy_score=0, yield_score=0, gold_score=0,
        vix_score=0, vwap_score=0, vp_score=0, cot_score=+2,
    )
    assert result_cot["total_score"] > result_zero["total_score"]
    assert result_cot["cot_score"] == +2
