"""Tests for TradingView webhook handler."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from order_flow_engine.src import ingest, tv_webhook


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Per-test temp output dir + clear in-memory hit deque."""
    from order_flow_engine.src import config as of_cfg
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(of_cfg, "OF_OUTPUT_DIR", out)
    tv_webhook._HITS.clear()
    monkeypatch.setattr(tv_webhook, "_HITS_LOG_PATH", out / "tv_hits.jsonl")
    yield


def test_normalize_interval_minute_codes():
    assert tv_webhook.normalize_interval("1") == "1m"
    assert tv_webhook.normalize_interval("5") == "5m"
    assert tv_webhook.normalize_interval("15") == "15m"
    assert tv_webhook.normalize_interval("60") == "1h"
    assert tv_webhook.normalize_interval("240") == "4h"


def test_normalize_interval_daily_weekly():
    assert tv_webhook.normalize_interval("D") == "1d"
    assert tv_webhook.normalize_interval("1D") == "1d"
    assert tv_webhook.normalize_interval("W") == "1w"


def test_normalize_interval_passthrough_engine_format():
    assert tv_webhook.normalize_interval("15m") == "15m"
    assert tv_webhook.normalize_interval("1h") == "1h"


def test_normalize_interval_none_uses_anchor():
    from order_flow_engine.src import config as of_cfg
    assert tv_webhook.normalize_interval(None) == of_cfg.OF_ANCHOR_TF


def test_handle_valid_payload_returns_200():
    payload = {
        "symbol": "ES1!", "timeframe": "15", "timestamp": "2026-04-23T22:00:00Z",
        "open": 5234.5, "high": 5240.0, "low": 5232.0, "close": 5238.5, "volume": 12000,
    }
    with patch.object(ingest, "ingest_bar", return_value=None) as m:
        status, body = tv_webhook.handle(payload)
    assert status == 200
    assert body["ok"] is True
    assert body["alert"] is None
    # interval was normalized
    assert m.call_args.kwargs["timeframe"] == "15m"
    assert m.call_args.kwargs["symbol"] == "ES1!"


def test_handle_string_body_parses_json():
    body_str = json.dumps({
        "symbol": "SPY", "timeframe": "1", "timestamp": "2026-04-23T22:00:00Z",
        "open": 444.0, "high": 444.5, "low": 443.5, "close": 444.2, "volume": 100,
    })
    with patch.object(ingest, "ingest_bar", return_value=None):
        status, body = tv_webhook.handle(body_str)
    assert status == 200


def test_handle_missing_field_400():
    payload = {"symbol": "ES1!", "timeframe": "5"}  # no OHLC
    status, body = tv_webhook.handle(payload)
    assert status == 400
    assert "missing field" in body["error"]


def test_handle_bad_value_400():
    payload = {
        "symbol": "ES1!", "timeframe": "5", "timestamp": "x",
        "open": "NaN-like", "high": 1, "low": 1, "close": 1, "volume": 1,
    }
    status, body = tv_webhook.handle(payload)
    assert status == 400


def test_recent_hits_logs_and_returns():
    payload = {
        "symbol": "ES1!", "timeframe": "5", "timestamp": "2026-04-23T22:00:00Z",
        "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100,
    }
    with patch.object(ingest, "ingest_bar", return_value=None):
        tv_webhook.handle(payload)
    hits = tv_webhook.recent_hits()
    assert len(hits) == 1
    assert hits[0]["status"].startswith("ok")
    assert hits[0]["payload"]["symbol"] == "ES1!"


def test_handle_alert_emitted_returns_alert():
    payload = {
        "symbol": "ES1!", "timeframe": "15", "timestamp": "2026-04-23T22:00:00Z",
        "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100,
    }
    fake_alert = {"id": "of_xyz", "label": "bullish_trap", "confidence": 90}
    with patch.object(ingest, "ingest_bar", return_value=fake_alert):
        status, body = tv_webhook.handle(payload)
    assert status == 200
    assert body["alert"]["id"] == "of_xyz"


def test_secret_is_persisted(tmp_path, monkeypatch):
    """Secret survives across module reloads via tv_secret.txt."""
    from order_flow_engine.src import config as of_cfg
    monkeypatch.setattr(of_cfg, "OF_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(tv_webhook, "_DEFAULT_SECRET_FILE", tmp_path / "tv_secret.txt")
    s1 = tv_webhook._load_or_create_secret()
    s2 = tv_webhook._load_or_create_secret()
    assert s1 == s2
    assert (tmp_path / "tv_secret.txt").exists()
