"""Tests for alert_engine — threshold gate, schema, JSONL append."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from order_flow_engine.src import alert_engine, config as of_cfg


def _sample_alert(**overrides) -> dict:
    base = alert_engine.build_alert(
        timestamp=datetime(2026, 4, 23, 14, 15, tzinfo=timezone.utc),
        symbol="ES=F",
        timeframe="15m",
        label="buyer_absorption",
        confidence=80,
        price=5234.25,
        atr=8.4,
        rules_fired=["r3_absorption_resistance"],
        metrics={"delta_ratio": 0.62, "cvd_z": 1.85, "clv": 0.18,
                 "dist_to_recent_high_atr": 0.31, "dist_to_recent_low_atr": 3.0},
        model_info={"version": None, "probas": {}},
        proxy_mode=True,
    )
    return {**base, **overrides}


def test_build_alert_schema():
    a = _sample_alert()
    assert set(a.keys()) >= {
        "id", "timestamp_utc", "symbol", "timeframe", "label", "confidence",
        "price", "atr", "rules_fired", "reason_codes", "metrics",
        "model", "data_quality",
    }
    assert a["data_quality"]["proxy_mode"] is True
    assert a["reason_codes"][0].startswith("Strong buying pressure")


def test_should_emit_gates():
    assert alert_engine.should_emit("buyer_absorption", 90)
    assert not alert_engine.should_emit("buyer_absorption", 10)
    assert not alert_engine.should_emit("normal_behavior", 99)


def test_emit_below_threshold_returns_none(tmp_of_output):
    weak = _sample_alert(confidence=10)
    out = alert_engine.emit(weak, output_dir=tmp_of_output)
    assert out is None
    assert not (tmp_of_output / "alerts.jsonl").exists()


def test_emit_above_threshold_appends(tmp_of_output):
    strong = _sample_alert(confidence=90)
    out = alert_engine.emit(strong, output_dir=tmp_of_output)
    assert out is not None
    lines = (tmp_of_output / "alerts.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["confidence"] == 90


def test_emit_appends_when_cooldown_disabled(tmp_of_output):
    """With cooldown_bars=0 each emit appends to JSONL."""
    a = _sample_alert(confidence=90)
    alert_engine.emit(a, output_dir=tmp_of_output, cooldown_bars=0)
    alert_engine.emit(a, output_dir=tmp_of_output, cooldown_bars=0)
    alert_engine.emit(a, output_dir=tmp_of_output, cooldown_bars=0)
    lines = (tmp_of_output / "alerts.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3


def test_volume_gate_passes_above_pctl():
    recent = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    assert alert_engine.volume_gate_passes(800, recent, pctl=0.20)


def test_volume_gate_blocks_below_pctl():
    recent = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    # 20th percentile of 100..1000 ~ 280; 100 below it
    assert not alert_engine.volume_gate_passes(100, recent, pctl=0.20)


def test_volume_gate_empty_window_passes():
    assert alert_engine.volume_gate_passes(0, [], pctl=0.20)


def test_cooldown_blocks_duplicate(tmp_of_output):
    a = _sample_alert(confidence=90)
    first = alert_engine.emit(a, output_dir=tmp_of_output)
    assert first is not None
    # Same label/symbol/tf, 1 minute later, default cooldown is many bars
    a2 = _sample_alert(confidence=92)
    a2["timestamp_utc"] = "2026-04-23T14:16:00Z"
    a2["id"] = "different_id"
    second = alert_engine.emit(a2, output_dir=tmp_of_output)
    assert second is None


def test_cooldown_allows_after_window(tmp_of_output):
    a = _sample_alert(confidence=90)
    alert_engine.emit(a, output_dir=tmp_of_output)
    # 10 bars × 15m = 150 min later (cooldown default 8 bars × 15m = 120 min)
    a2 = _sample_alert(confidence=92)
    a2["timestamp_utc"] = "2026-04-23T17:00:00Z"
    a2["id"] = "later_id"
    out = alert_engine.emit(a2, output_dir=tmp_of_output)
    assert out is not None


def test_emit_writes_to_sqlite(tmp_of_output):
    from order_flow_engine.src import alert_store
    a = _sample_alert(confidence=90)
    alert_engine.emit(a, output_dir=tmp_of_output)
    rows = alert_store.query(output_dir=tmp_of_output)
    assert len(rows) == 1
    assert rows[0]["label"] == "buyer_absorption"


def test_write_consolidated_overwrites(tmp_of_output):
    a1 = _sample_alert(confidence=90)
    a2 = _sample_alert(confidence=85, label="bullish_trap")
    path = alert_engine.write_consolidated([a1, a2], output_dir=tmp_of_output)
    data = json.loads(path.read_text())
    assert len(data) == 2
    assert data[1]["label"] == "bullish_trap"

    # Second call rewrites, not appends.
    alert_engine.write_consolidated([a1], output_dir=tmp_of_output)
    data = json.loads(path.read_text())
    assert len(data) == 1
