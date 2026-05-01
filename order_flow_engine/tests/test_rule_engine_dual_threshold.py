"""
Phase 2A — dual-threshold smoke tests.

Confirms that rule_engine.apply_rules selects:
  * RULE_*_REAL thresholds for bars with bar_proxy_mode == 0
  * RULE_* (proxy) thresholds for bars with bar_proxy_mode == 1
  * proxy thresholds for all bars when bar_proxy_mode is missing
  * proxy thresholds for all bars when OF_REAL_THRESHOLDS_ENABLED is False

These guard the Phase 2A path-specific threshold branch from regressions.
No model / feature / label changes exercised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import rule_engine as re_eng


def _base_frame(n: int = 6) -> pd.DataFrame:
    """Synthetic frame carrying the columns apply_rules requires."""
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "Open":   np.full(n, 100.0),
        "High":   np.full(n, 100.5),
        "Low":    np.full(n, 99.5),
        "Close":  np.full(n, 100.0),
        "delta_ratio": [0.0]*n,    # filled by each test
        "cvd_z":  [0.0]*n,
        "fwd_ret_1": [0.0]*n,
        "atr":     [1.0]*n,
        "atr_pct": [1.0]*n,
        "recent_high": [200.0]*n,
        "recent_low":  [0.0]*n,
        "dist_to_recent_high_atr": [5.0]*n,
        "dist_to_recent_low_atr":  [5.0]*n,
    }, index=idx)


def test_real_bar_fires_at_real_threshold_proxy_does_not():
    """
    Bar with delta_ratio = 0.05 should:
      * fire R1 when bar_proxy_mode == 0 (real_thr=0.04 < 0.05)
      * NOT fire R1 when bar_proxy_mode == 1 (proxy_thr=0.30 > 0.05)
    """
    df = _base_frame(2)
    df["delta_ratio"] = 0.05
    df["fwd_ret_1"]   = -0.005   # → fwd_ret_atr ≈ -0.5, passes < -0.3
    df["bar_proxy_mode"] = [0, 1]   # row 0 real, row 1 proxy

    out = re_eng.apply_rules(df)
    assert bool(out["r1_buyer_down"].iloc[0]) is True,  "real bar should fire R1"
    assert bool(out["r1_buyer_down"].iloc[1]) is False, "proxy bar should not"


def test_proxy_threshold_used_when_column_missing():
    """No bar_proxy_mode column → all bars treated as proxy."""
    df = _base_frame(1)
    df["delta_ratio"] = 0.05
    df["fwd_ret_1"]   = -0.005
    out = re_eng.apply_rules(df)
    assert bool(out["r1_buyer_down"].iloc[0]) is False, \
        "without bar_proxy_mode, default to proxy thr (no fire)"


def test_disabled_flag_reverts_to_proxy(monkeypatch):
    """
    With OF_REAL_THRESHOLDS_ENABLED = False, real bars must use proxy
    thresholds — single env-var revert path.
    """
    monkeypatch.setattr(of_cfg, "OF_REAL_THRESHOLDS_ENABLED", False)
    df = _base_frame(1)
    df["delta_ratio"] = 0.05
    df["fwd_ret_1"]   = -0.005
    df["bar_proxy_mode"] = [0]
    out = re_eng.apply_rules(df)
    assert bool(out["r1_buyer_down"].iloc[0]) is False, \
        "disabled flag must keep proxy thr even on real bars"


def test_proxy_path_unchanged_at_proxy_threshold():
    """Proxy bar with dr just above proxy threshold still fires."""
    df = _base_frame(1)
    df["delta_ratio"] = of_cfg.RULE_DELTA_DOMINANCE + 0.01
    df["fwd_ret_1"]   = -0.005
    df["bar_proxy_mode"] = [1]   # proxy bar
    out = re_eng.apply_rules(df)
    assert bool(out["r1_buyer_down"].iloc[0]) is True


def test_real_threshold_constants_present():
    """Phase 2A constants exist with the calibrated values."""
    assert hasattr(of_cfg, "RULE_DELTA_DOMINANCE_REAL")
    assert hasattr(of_cfg, "RULE_ABSORPTION_DELTA_REAL")
    assert hasattr(of_cfg, "RULE_TRAP_DELTA_REAL")
    assert hasattr(of_cfg, "OF_REAL_THRESHOLDS_ENABLED")
    # Sanity: real thresholds must be strictly tighter than proxy.
    assert of_cfg.RULE_DELTA_DOMINANCE_REAL  < of_cfg.RULE_DELTA_DOMINANCE
    assert of_cfg.RULE_ABSORPTION_DELTA_REAL < of_cfg.RULE_ABSORPTION_DELTA
    assert of_cfg.RULE_TRAP_DELTA_REAL       < of_cfg.RULE_TRAP_DELTA
