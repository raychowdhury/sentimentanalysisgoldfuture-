"""Tests for external notifier fan-out."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from order_flow_engine.src import notifier


def _alert(**over):
    base = {
        "id": "t1", "timestamp_utc": "2026-04-24T03:00:00Z",
        "symbol": "ES=F", "timeframe": "5m", "label": "possible_reversal",
        "confidence": 75, "price": 7150.0, "atr": 3.0,
        "rules_fired": ["r1_buyer_down"], "reason_codes": ["test"],
        "metrics": {"delta_ratio": 0.6, "cvd_z": -1.2},
        "model": {}, "data_quality": {"proxy_mode": True},
    }
    base.update(over)
    return base


def test_format_includes_essentials():
    msg = notifier._format(_alert())
    assert "Possible Reversal" in msg
    assert "ES=F" in msg
    assert "75" in msg


def test_trade_plan_long_for_bearish_trap():
    # bearish_trap fires ↑ UP → BUY @ price, stop below, target above
    msg = notifier._format(_alert(label="bearish_trap", price=7000.0, atr=10.0))
    assert "BUY @ 7000.00" in msg
    assert "Stop:  6990.00" in msg       # 1×ATR below
    assert "Target: 7020.00" in msg      # 2×ATR above
    assert "risk $500" in msg            # 10pt × $50/pt
    assert "reward $1000" in msg


def test_trade_plan_short_for_buyer_absorption():
    msg = notifier._format(_alert(label="buyer_absorption", price=7000.0, atr=5.0))
    assert "SELL @ 7000.00" in msg
    assert "Stop:  7005.00" in msg       # 1×ATR above
    assert "Target: 6990.00" in msg      # 2×ATR below


def test_trade_plan_skipped_when_no_direction():
    msg = notifier._format(_alert(label="normal_behavior"))
    assert "BUY @" not in msg
    assert "SELL @" not in msg


def test_direction_buyer_absorption_is_down():
    assert notifier._direction(_alert(label="buyer_absorption")) == "↓ DOWN"


def test_direction_seller_absorption_is_up():
    assert notifier._direction(_alert(label="seller_absorption")) == "↑ UP"


def test_direction_possible_reversal_negative_delta_is_up():
    a = _alert(metrics={"delta_ratio": -0.5})
    assert notifier._direction(a) == "↑ UP"


def test_direction_possible_reversal_positive_delta_is_down():
    a = _alert(metrics={"delta_ratio": 0.5})
    assert notifier._direction(a) == "↓ DOWN"


def test_send_telegram_skips_without_env(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    assert notifier.send_telegram(_alert()) is False


def test_send_discord_skips_without_env(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    assert notifier.send_discord(_alert()) is False


def test_send_telegram_posts_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TG_CHAT_ID", "123")
    # Fresh subscriber DB so all_active() returns just the seed
    from order_flow_engine.src import config as of_cfg
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(of_cfg, "OF_OUTPUT_DIR", out)

    captured = []
    class FakeResp:
        status_code = 200
        text = "ok"
    def fake_post(url, data=None, timeout=None, json=None):
        captured.append({"url": url, "data": data})
        return FakeResp()
    monkeypatch.setattr("requests.post", fake_post)
    assert notifier.send_telegram(_alert()) is True
    assert captured, "no POST issued"
    assert "fake-token" in captured[0]["url"]
    # chat_id present in at least one POST (seed env)
    assert any(int(c["data"]["chat_id"]) == 123 for c in captured)


def test_send_discord_posts_when_configured(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.example/webhook")
    class FakeResp:
        status_code = 204
        text = ""
    def fake_post(url, json=None, timeout=None, data=None):
        return FakeResp()
    monkeypatch.setattr("requests.post", fake_post)
    assert notifier.send_discord(_alert()) is True


def test_fanout_returns_per_channel_status(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    res = notifier.fanout(_alert())
    assert res == {"telegram": False, "discord": False}


def test_configured_reflects_env(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "x"); monkeypatch.setenv("TG_CHAT_ID", "y")
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    c = notifier.configured()
    assert c == {"telegram": True, "discord": False}
