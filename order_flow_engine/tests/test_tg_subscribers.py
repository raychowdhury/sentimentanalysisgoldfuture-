"""Tests for Telegram subscriber registry."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from order_flow_engine.src import tg_subscribers


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from order_flow_engine.src import config as of_cfg
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(of_cfg, "OF_OUTPUT_DIR", out)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    yield


def test_subscribe_returns_true_first_time():
    assert tg_subscribers.subscribe(123, "alice", "Alice") is True


def test_subscribe_returns_false_for_existing_active():
    tg_subscribers.subscribe(123, "alice", "Alice")
    assert tg_subscribers.subscribe(123, "alice", "Alice") is False


def test_unsubscribe_then_resubscribe():
    tg_subscribers.subscribe(123, "alice", "Alice")
    tg_subscribers.unsubscribe(123)
    # Inactive → resubscribe should return True
    assert tg_subscribers.subscribe(123, "alice", "Alice") is True


def test_all_active_returns_subscribed_chats():
    tg_subscribers.subscribe(111)
    tg_subscribers.subscribe(222)
    tg_subscribers.subscribe(333)
    tg_subscribers.unsubscribe(222)
    assert set(tg_subscribers.all_active()) == {111, 333}


def test_all_active_includes_env_seed(monkeypatch):
    monkeypatch.setenv("TG_CHAT_ID", "999")
    tg_subscribers.subscribe(111)
    assert set(tg_subscribers.all_active()) == {111, 999}


def test_all_active_dedupes_env_seed():
    os.environ["TG_CHAT_ID"] = "111"
    try:
        tg_subscribers.subscribe(111)
        assert tg_subscribers.all_active() == [111]
    finally:
        del os.environ["TG_CHAT_ID"]


def test_stats_returns_counts():
    tg_subscribers.subscribe(1); tg_subscribers.subscribe(2)
    tg_subscribers.unsubscribe(1)
    s = tg_subscribers.stats()
    assert s["active"] == 1
    assert s["total"] == 2
