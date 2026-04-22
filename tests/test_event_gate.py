"""Tests for event calendar + blackout gate."""

from __future__ import annotations

from datetime import date

import pytest

import config
from events.blackout import is_blackout
from events.calendar import _first_friday, get_events


def test_first_friday_rule():
    # 2026-04 first Friday is the 3rd.
    assert _first_friday(2026, 4) == date(2026, 4, 3)
    # 2025-08-01 is a Friday itself.
    assert _first_friday(2025, 8) == date(2025, 8, 1)


def test_get_events_returns_sorted_range():
    events = get_events(date(2026, 4, 1), date(2026, 4, 30))
    assert events, "expected at least one April 2026 event"
    assert all(date(2026, 4, 1) <= e.date <= date(2026, 4, 30) for e in events)
    assert events == sorted(events, key=lambda e: (e.date, e.kind))


def test_get_events_kinds_present():
    events = get_events(date(2026, 1, 1), date(2026, 12, 31))
    kinds = {e.kind for e in events}
    assert {"FOMC", "CPI", "NFP", "PCE"}.issubset(kinds)


def test_blackout_disabled_returns_clear(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", False)
    blocked, reason = is_blackout(date(2026, 4, 29))
    assert blocked is False
    assert reason is None


def test_blackout_on_fomc_day(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_TYPES", ["FOMC", "CPI", "NFP", "PCE"])

    blocked, reason = is_blackout(date(2026, 4, 29))
    assert blocked is True
    assert "FOMC" in reason
    assert "2026-04-29" in reason


def test_blackout_day_before(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1)
    blocked, reason = is_blackout(date(2026, 4, 28))
    assert blocked is True
    assert reason.startswith("pre-FOMC")


def test_blackout_day_after(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1)
    blocked, reason = is_blackout(date(2026, 4, 30))
    assert blocked is True
    assert reason.startswith("post-FOMC")


def test_blackout_outside_window(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1)
    # 2026-04-20 — sits between CPI (Apr 14) and FOMC (Apr 29). Both windows
    # end/start well outside this date, so should be clear.
    blocked, reason = is_blackout(date(2026, 4, 20))
    assert blocked is False
    assert reason is None


def test_blackout_type_filter(monkeypatch):
    monkeypatch.setattr(config, "EVENT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1)
    monkeypatch.setattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1)
    # Only allow CPI — FOMC day should pass through clear.
    monkeypatch.setattr(config, "EVENT_BLACKOUT_TYPES", ["CPI"])
    blocked, _ = is_blackout(date(2026, 4, 29))
    assert blocked is False


def test_signal_engine_event_gate_downgrades(monkeypatch):
    """End-to-end: event_blackout_reason forces HOLD after all other gates pass."""
    from signals import signal_engine

    # Clean BUY-side scoring that does not trip the veto (dxy/yield mild, not -2).
    result = signal_engine.run(
        avg_sentiment=0.2,
        dxy_score=-1,
        yield_score=-1,
        gold_score=3,
        vix_score=2,
        vwap_score=2,
        vp_score=2,
        macro_bullish=True,
        event_blackout_reason="pre-FOMC (2026-04-29)",
    )
    assert result["signal"] == "HOLD"
    assert result["event_gated"] is True
    assert result["event_blackout_reason"] == "pre-FOMC (2026-04-29)"
    assert result["raw_signal"] in ("STRONG_BUY", "BUY")


def test_signal_engine_no_blackout_clears(monkeypatch):
    from signals import signal_engine
    result = signal_engine.run(
        avg_sentiment=0.2,
        dxy_score=-1,
        yield_score=-1,
        gold_score=3,
        vix_score=2,
        vwap_score=2,
        vp_score=2,
        macro_bullish=True,
        event_blackout_reason=None,
    )
    assert result["event_gated"] is False
    assert result["signal"] in ("BUY", "STRONG_BUY")
