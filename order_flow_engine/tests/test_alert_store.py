"""Tests for sqlite alert store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from order_flow_engine.src import alert_engine, alert_store


def _alert(ts="2026-04-23T14:15:00Z", label="bullish_trap", conf=80, id_=None):
    a = alert_engine.build_alert(
        timestamp=datetime.fromisoformat(ts.replace("Z","+00:00")),
        symbol="ES=F", timeframe="15m", label=label, confidence=conf,
        price=5234.25, atr=8.4, rules_fired=["r5_bull_trap"],
        metrics={"delta_ratio": 0.5, "cvd_z": 1.0, "clv": 0.0,
                 "dist_to_recent_high_atr": 0.3, "dist_to_recent_low_atr": 3.0},
        model_info={"version": None, "probas": {}}, proxy_mode=True,
    )
    if id_:
        a["id"] = id_
    return a


def test_init_creates_table(tmp_of_output):
    alert_store.init_db(output_dir=tmp_of_output)
    assert (tmp_of_output / "alerts.sqlite").exists()


def test_upsert_and_query(tmp_of_output):
    alert_store.upsert(_alert(id_="a1"), output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="a2", label="bearish_trap"), output_dir=tmp_of_output)
    rows = alert_store.query(output_dir=tmp_of_output)
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"a1", "a2"}


def test_query_filters(tmp_of_output):
    alert_store.upsert(_alert(id_="a1", label="bullish_trap", conf=70), output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="a2", label="bearish_trap", conf=85), output_dir=tmp_of_output)
    bears = alert_store.query(output_dir=tmp_of_output, label="bearish_trap")
    assert len(bears) == 1 and bears[0]["confidence"] == 85
    high = alert_store.query(output_dir=tmp_of_output, min_confidence=80)
    assert len(high) == 1 and high[0]["id"] == "a2"


def test_upsert_overwrites_same_id(tmp_of_output):
    alert_store.upsert(_alert(id_="dup", conf=70), output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="dup", conf=95), output_dir=tmp_of_output)
    rows = alert_store.query(output_dir=tmp_of_output)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 95


def test_label_distribution(tmp_of_output):
    alert_store.upsert(_alert(id_="a1", label="bullish_trap"), output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="a2", label="bullish_trap"), output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="a3", label="bearish_trap"), output_dir=tmp_of_output)
    dist = alert_store.label_distribution(output_dir=tmp_of_output)
    assert dist == {"bullish_trap": 2, "bearish_trap": 1}


def test_last_alert_for(tmp_of_output):
    alert_store.upsert(_alert(id_="a1", ts="2026-04-23T10:00:00Z", label="bullish_trap"),
                       output_dir=tmp_of_output)
    alert_store.upsert(_alert(id_="a2", ts="2026-04-23T11:00:00Z", label="bullish_trap"),
                       output_dir=tmp_of_output)
    last = alert_store.last_alert_for("ES=F", "15m", "bullish_trap",
                                      output_dir=tmp_of_output)
    assert last is not None
    assert last["ts_utc"] == "2026-04-23T11:00:00Z"
