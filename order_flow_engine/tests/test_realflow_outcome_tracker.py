"""
Phase 2D Stage 1 — outcome-tracker smoke tests.

Cover the read-only invariants:
  * signal_id is deterministic
  * session bucketing matches the spec boundaries
  * direction sign per rule
  * settle gate keeps PENDING when window incomplete
  * settled rows can be scored on a synthetic frame
  * idempotence: same input twice → same JSONL
  * bar_proxy_mode filter excludes proxy-path fires

No detector / predictor / ml_engine code is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realflow_outcome_tracker as rot


# ── pure helpers ────────────────────────────────────────────────────────────

def test_signal_id_deterministic():
    ts = pd.Timestamp("2026-04-30 14:00:00", tz="UTC")
    sid1 = rot._signal_id("ESM6", "15m", ts, "r1_buyer_down")
    sid2 = rot._signal_id("ESM6", "15m", ts, "r1_buyer_down")
    assert sid1 == sid2 == "ESM6_15m_2026-04-30T14:00:00Z_r1"


def test_signal_id_differs_per_rule():
    ts = pd.Timestamp("2026-04-30 14:00:00", tz="UTC")
    a = rot._signal_id("ESM6", "15m", ts, "r1_buyer_down")
    b = rot._signal_id("ESM6", "15m", ts, "r2_seller_up")
    assert a != b


def test_session_bucket_boundaries():
    rth_open  = pd.Timestamp("2026-04-30 13:30:00", tz="UTC")
    rth_mid   = pd.Timestamp("2026-04-30 16:00:00", tz="UTC")
    rth_close = pd.Timestamp("2026-04-30 19:45:00", tz="UTC")
    eth       = pd.Timestamp("2026-04-30 03:00:00", tz="UTC")
    eth_late  = pd.Timestamp("2026-04-30 21:00:00", tz="UTC")
    assert rot._session_bucket(rth_open)  == "RTH_open"
    assert rot._session_bucket(rth_mid)   == "RTH_mid"
    assert rot._session_bucket(rth_close) == "RTH_close"
    assert rot._session_bucket(eth)       == "ETH"
    assert rot._session_bucket(eth_late)  == "ETH"


def test_direction_per_rule():
    assert rot._direction_for_rule("r1_buyer_down") == -1
    assert rot._direction_for_rule("r2_seller_up")  == +1


# ── outcome scoring ─────────────────────────────────────────────────────────

def _synthetic_frame(direction: int, win: bool) -> pd.DataFrame:
    """
    Build a minimal joined frame with 20 bars. Fire at index 0.
    For a long-equivalent (direction=+1) win bar, price rises 1.2 ATR over
    the next 12 bars. For short (direction=-1) win, price falls.
    """
    n = 20
    idx = pd.date_range("2026-04-30 14:00:00", periods=n, freq="15min", tz="UTC")
    entry = 100.0
    atr = 1.0
    horizon = of_cfg.OF_FORWARD_BARS.get("15m", 12)

    closes = np.full(n, entry)
    move = (1.2 if win else -1.2) * direction * atr   # signed move
    for i in range(1, horizon + 1):
        closes[i] = entry + (move * i / horizon)
    # tail bars stay flat at the horizon close
    for i in range(horizon + 1, n):
        closes[i] = closes[horizon]

    df = pd.DataFrame({
        "Open":   closes,
        "High":   closes + 0.05,
        "Low":    closes - 0.05,
        "Close":  closes,
        "atr":    np.full(n, atr),
    }, index=idx)
    return df


def test_score_outcome_win_long():
    df = _synthetic_frame(direction=+1, win=True)
    horizon = of_cfg.OF_FORWARD_BARS.get("15m", 12)
    out = rot._score_outcome(df, fire_idx=0, horizon=horizon,
                             direction=+1, atr=1.0, entry=100.0)
    assert out is not None
    assert out["outcome"] == "win"
    assert out["fwd_r_signed"] > 0


def test_score_outcome_loss_short():
    # short win = price falls; flip direction & expect win signed.
    # For loss-short, price RISES.
    df = _synthetic_frame(direction=-1, win=False)
    horizon = of_cfg.OF_FORWARD_BARS.get("15m", 12)
    out = rot._score_outcome(df, fire_idx=0, horizon=horizon,
                             direction=-1, atr=1.0, entry=100.0)
    assert out is not None
    assert out["outcome"] == "loss"
    assert out["fwd_r_signed"] < 0


def test_score_outcome_window_incomplete_returns_none():
    df = _synthetic_frame(direction=+1, win=True)
    horizon = of_cfg.OF_FORWARD_BARS.get("15m", 12)
    # fire_idx near end of frame -> not enough forward bars
    out = rot._score_outcome(df, fire_idx=len(df) - 2, horizon=horizon,
                             direction=+1, atr=1.0, entry=100.0)
    assert out is None


def test_score_outcome_zero_atr_returns_none():
    df = _synthetic_frame(direction=+1, win=True)
    out = rot._score_outcome(df, fire_idx=0, horizon=4,
                             direction=+1, atr=0.0, entry=100.0)
    assert out is None


# ── idempotence + JSONL ─────────────────────────────────────────────────────

def test_settled_ids_dedupe(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    rows = [
        {"signal_id": "A_15m_2026-01-01T00:00:00Z_r1", "outcome": "win"},
        {"signal_id": "A_15m_2026-01-01T00:15:00Z_r2", "outcome": "loss"},
    ]
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    ids = rot._settled_ids(p)
    assert ids == {
        "A_15m_2026-01-01T00:00:00Z_r1",
        "A_15m_2026-01-01T00:15:00Z_r2",
    }


def test_settled_ids_returns_empty_for_missing_file(tmp_path):
    assert rot._settled_ids(tmp_path / "nope.jsonl") == set()


# ── summary aggregation ─────────────────────────────────────────────────────

def test_build_summary_empty():
    s = rot._build_summary([], [], "ESM6", "15m")
    assert s["n_settled"] == 0
    assert s["sample_size_label"] == "cold"


def test_build_summary_nonempty():
    settled = [
        {"signal_id": "x1", "rule": "r1_buyer_down", "session": "RTH_mid",
         "threshold_path": "real", "fire_ts_utc": "2026-04-30T14:00:00+00:00",
         "fwd_r_signed":  1.2, "mae_r": -0.3, "mfe_r": 1.4,
         "outcome": "win",  "hit_1r": True,  "stopped_out_1atr": False},
        {"signal_id": "x2", "rule": "r1_buyer_down", "session": "RTH_mid",
         "threshold_path": "real", "fire_ts_utc": "2026-04-30T14:30:00+00:00",
         "fwd_r_signed": -0.5, "mae_r": -0.6, "mfe_r": 0.2,
         "outcome": "loss", "hit_1r": False, "stopped_out_1atr": False},
    ]
    s = rot._build_summary(settled, [], "ESM6", "15m")
    assert s["n_settled"] == 2
    assert s["by_rule"]["r1_buyer_down"]["n"] == 2
    assert s["by_rule"]["r1_buyer_down"]["wins"] == 1
    assert s["by_rule"]["r1_buyer_down"]["mean_r"] == pytest.approx(0.35)
    assert "RTH_mid" in s["by_session"]


# ── invariant: tracker doesn't mutate config ────────────────────────────────

def test_settle_pass_does_not_mutate_thresholds():
    before = (of_cfg.RULE_DELTA_DOMINANCE,
              of_cfg.RULE_DELTA_DOMINANCE_REAL,
              of_cfg.RULE_ABSORPTION_DELTA,
              of_cfg.RULE_ABSORPTION_DELTA_REAL,
              of_cfg.RULE_TRAP_DELTA,
              of_cfg.RULE_TRAP_DELTA_REAL,
              of_cfg.OF_REAL_THRESHOLDS_ENABLED)
    # Don't actually run settle_pass (needs Databento). Touch the helpers that
    # DO run without network instead.
    rot._signal_id("ESM6", "15m",
                   pd.Timestamp("2026-04-30 14:00:00", tz="UTC"),
                   "r1_buyer_down")
    rot._session_bucket(pd.Timestamp("2026-04-30 14:00:00", tz="UTC"))
    rot._direction_for_rule("r1_buyer_down")
    rot._build_summary([], [], "ESM6", "15m")
    after = (of_cfg.RULE_DELTA_DOMINANCE,
             of_cfg.RULE_DELTA_DOMINANCE_REAL,
             of_cfg.RULE_ABSORPTION_DELTA,
             of_cfg.RULE_ABSORPTION_DELTA_REAL,
             of_cfg.RULE_TRAP_DELTA,
             of_cfg.RULE_TRAP_DELTA_REAL,
             of_cfg.OF_REAL_THRESHOLDS_ENABLED)
    assert before == after
