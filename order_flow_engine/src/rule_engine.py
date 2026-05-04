"""
Seven opposite-flow detection rules. Each rule emits a per-bar boolean
column; callers aggregate via `rule_hit_count` and `rule_hit_codes`.

Thresholds live in order_flow_engine.src.config and are tuned for futures
(ES=F) at 15m resolution. Override via that module if you re-target.

Rules (see README for market rationale):
  R1 buyer dominance but price down
  R2 seller dominance but price up
  R3 buying absorption at resistance
  R4 selling absorption at support
  R5 bullish trap (failed breakout)
  R6 bearish trap (failed breakdown)
  R7 CVD / price divergence over rolling window
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg

RULE_CODES: dict[str, str] = {
    "r1_buyer_down":            "Buyer dominance but price moved down",
    "r2_seller_up":             "Seller dominance but price moved up",
    "r3_absorption_resistance": "Strong buying pressure failed to push above resistance",
    "r4_absorption_support":    "Strong selling pressure failed to break support",
    "r5_bull_trap":             "Failed breakout — high closed back inside range",
    "r6_bear_trap":             "Failed breakdown — low closed back inside range",
    "r7_cvd_divergence":        "CVD/price divergence over rolling window",
}

ALL_RULE_COLS = list(RULE_CODES.keys())

# Causal rules read only current/past bars → safe to fire on the newest bar
# at close(t). Confirmation rules require the next bar's return (close(t+1))
# so they can only be fired one bar late: evaluate bar t after close(t+1).
CAUSAL_RULES:       list[str] = ["r5_bull_trap", "r6_bear_trap", "r7_cvd_divergence"]
CONFIRMATION_RULES: list[str] = [
    "r1_buyer_down", "r2_seller_up",
    "r3_absorption_resistance", "r4_absorption_support",
]


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add rule hit columns, rule_hit_count, rule_hit_codes.

    Rule layering:
      * CAUSAL_RULES (r5/r6/r7) — read only current and past bars.
      * CONFIRMATION_RULES (r1-r4) — depend on fwd_ret_1 (next-bar return).
        Safe offline; live callers must score the PRIOR bar after the
        newest bar closes (see ingest._confirmation_pass).

    Requires columns produced by feature_engineering.build_features_for_tf:
    delta_ratio, cvd_z, fwd_ret_1, atr_pct, atr, Close, High, Low,
    recent_high, recent_low, dist_to_recent_high_atr, dist_to_recent_low_atr.
    """
    out = df.copy()
    close = out["Close"]
    dr    = out["delta_ratio"]

    # Forward-return in ATR units — used only by confirmation rules r1-r4.
    atr_frac = (out["atr_pct"] / 100).replace(0, np.nan)
    fwd_ret_atr = (out["fwd_ret_1"] / atr_frac).fillna(0.0)

    # Per-bar threshold selection. When OF_REAL_THRESHOLDS_ENABLED and the
    # bar carries real buy/sell flow (bar_proxy_mode == 0), use the *_REAL
    # values calibrated for real-flow's tighter delta_ratio distribution.
    # Bars without real flow keep the proxy-tuned thresholds.
    if (of_cfg.OF_REAL_THRESHOLDS_ENABLED
            and "bar_proxy_mode" in out.columns):
        is_real = (out["bar_proxy_mode"].fillna(1).astype(int) == 0).to_numpy()
        dom_thr  = np.where(is_real,
                            of_cfg.RULE_DELTA_DOMINANCE_REAL,
                            of_cfg.RULE_DELTA_DOMINANCE)
        abs_thr  = np.where(is_real,
                            of_cfg.RULE_ABSORPTION_DELTA_REAL,
                            of_cfg.RULE_ABSORPTION_DELTA)
        trap_thr = np.where(is_real,
                            of_cfg.RULE_TRAP_DELTA_REAL,
                            of_cfg.RULE_TRAP_DELTA)
    else:
        dom_thr  = of_cfg.RULE_DELTA_DOMINANCE
        abs_thr  = of_cfg.RULE_ABSORPTION_DELTA
        trap_thr = of_cfg.RULE_TRAP_DELTA

    # R1/R2 — opposite directional reaction on the next bar (~0.3×ATR move).
    out["r1_buyer_down"] = (dr >  dom_thr) & (fwd_ret_atr < -0.3)
    out["r2_seller_up"]  = (dr < -dom_thr) & (fwd_ret_atr >  0.3)

    # R3/R4 — near S/R with heavy flow but negligible forward move.
    near_high = out["dist_to_recent_high_atr"] < of_cfg.RULE_SR_ATR_MULT
    near_low  = out["dist_to_recent_low_atr"]  < of_cfg.RULE_SR_ATR_MULT
    small_move = fwd_ret_atr.abs() < of_cfg.RULE_ABSORPTION_RET_CAP_ATR_PCT
    out["r3_absorption_resistance"] = near_high & (dr >  abs_thr) & small_move
    out["r4_absorption_support"]    = near_low  & (dr < -abs_thr) & small_move

    # R5/R6 — failed breakout / breakdown. High pokes above recent_high but
    # close comes back inside; mirror for lows.
    out["r5_bull_trap"] = (
        (out["High"]  > out["recent_high"]) &
        (close < out["recent_high"]) &
        (dr >  trap_thr)
    ).fillna(False)
    out["r6_bear_trap"] = (
        (out["Low"]   < out["recent_low"]) &
        (close > out["recent_low"]) &
        (dr < -trap_thr)
    ).fillna(False)

    # R7 — rolling correlation between CVD-z and *bar returns* (not raw price).
    # Correlating against price levels mostly tracks the underlying trend, so
    # a strong trend with rising CVD would never fire even though CVD is just
    # following the trend. Using returns isolates per-bar agreement: negative
    # correlation = flow consistently disagrees with bar direction = real
    # divergence.
    w = of_cfg.RULE_CVD_CORR_WINDOW
    ret = close.pct_change()
    corr = out["cvd_z"].rolling(w, min_periods=w).corr(ret)
    out["cvd_price_corr"] = corr
    out["r7_cvd_divergence"] = (corr < of_cfg.RULE_CVD_CORR_THRESH).fillna(False)

    # Per-bar trade direction for possible_reversal: r1 → fade buy (-1),
    # r2 → fade sell (+1), r7-only → follow CVD slope sign (price will
    # likely revert toward the flow). Stored so backtester / outcome tracker
    # don't have to re-derive direction from delta_ratio (which is undefined
    # for r7-only fires).
    # Compose direction without boolean-mask Series assignment — live ingest
    # frames can carry duplicate timestamps which break .loc[mask] = Series
    # alignment with "cannot reindex on an axis with duplicate labels".
    r1 = out["r1_buyer_down"].fillna(False).to_numpy()
    r2 = out["r2_seller_up"].fillna(False).to_numpy()
    r7 = out["r7_cvd_divergence"].fillna(False).to_numpy()
    cvd_slope = out["cvd_z"].diff(w).fillna(0.0).to_numpy()
    r7_dir = np.sign(cvd_slope).astype(int)
    r7_only = r7 & ~r1 & ~r2
    direction = np.zeros(len(out), dtype=int)
    direction[r1] = -1
    direction[r2] = +1
    direction[r7_only] = r7_dir[r7_only]
    out["reversal_direction"] = direction

    # Aggregations
    bool_block = out[ALL_RULE_COLS].astype(bool)
    out["rule_hit_count"] = bool_block.sum(axis=1).astype(int)
    # Causal subset — leak-free; safe to feed to the model.
    out["rule_hit_count_causal"] = bool_block[CAUSAL_RULES].sum(axis=1).astype(int)
    out["rule_hit_codes"] = bool_block.apply(
        lambda row: ";".join([c for c in ALL_RULE_COLS if row[c]]),
        axis=1,
    )
    return out


def rules_for_label(label: str) -> list[str]:
    """Which rule(s) corroborate a given predicted class."""
    return {
        "buyer_absorption":  ["r3_absorption_resistance"],
        "seller_absorption": ["r4_absorption_support"],
        "bullish_trap":      ["r5_bull_trap"],
        "bearish_trap":      ["r6_bear_trap"],
        "possible_reversal": ["r1_buyer_down", "r2_seller_up", "r7_cvd_divergence"],
        "normal_behavior":   [],
    }.get(label, [])
