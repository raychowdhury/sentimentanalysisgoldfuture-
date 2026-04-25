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

    # R1/R2 — opposite directional reaction on the next bar (~0.3×ATR move).
    out["r1_buyer_down"] = (dr >  of_cfg.RULE_DELTA_DOMINANCE) & (fwd_ret_atr < -0.3)
    out["r2_seller_up"]  = (dr < -of_cfg.RULE_DELTA_DOMINANCE) & (fwd_ret_atr >  0.3)

    # R3/R4 — near S/R with heavy flow but negligible forward move.
    near_high = out["dist_to_recent_high_atr"] < of_cfg.RULE_SR_ATR_MULT
    near_low  = out["dist_to_recent_low_atr"]  < of_cfg.RULE_SR_ATR_MULT
    small_move = fwd_ret_atr.abs() < of_cfg.RULE_ABSORPTION_RET_CAP_ATR_PCT
    out["r3_absorption_resistance"] = near_high & (dr >  of_cfg.RULE_ABSORPTION_DELTA) & small_move
    out["r4_absorption_support"]    = near_low  & (dr < -of_cfg.RULE_ABSORPTION_DELTA) & small_move

    # R5/R6 — failed breakout / breakdown. High pokes above recent_high but
    # close comes back inside; mirror for lows.
    out["r5_bull_trap"] = (
        (out["High"]  > out["recent_high"]) &
        (close < out["recent_high"]) &
        (dr >  of_cfg.RULE_TRAP_DELTA)
    ).fillna(False)
    out["r6_bear_trap"] = (
        (out["Low"]   < out["recent_low"]) &
        (close > out["recent_low"]) &
        (dr < -of_cfg.RULE_TRAP_DELTA)
    ).fillna(False)

    # R7 — rolling correlation between CVD z and price. Strongly negative
    # means flow disagrees with trend.
    w = of_cfg.RULE_CVD_CORR_WINDOW
    corr = out["cvd_z"].rolling(w, min_periods=w).corr(close)
    out["cvd_price_corr"] = corr
    out["r7_cvd_divergence"] = (corr < of_cfg.RULE_CVD_CORR_THRESH).fillna(False)

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
