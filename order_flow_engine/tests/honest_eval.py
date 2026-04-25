"""
Honest forward-PnL eval.

Labels embed forward bars; the basic backtester consequently reports
tautological hit-rates. Here we:

  1. Build features/rules as usual. Rules are causal — they fire at close(t).
  2. Treat the signal as filling at bar t+1 close (earliest a runtime system
     could realistically act).
  3. Measure forward return from close(t+1) to close(t+1+h), in ATR units.
  4. Report per-rule hit rate, mean R, expectancy. No label involvement.

Run: python -m order_flow_engine.tests.honest_eval --tf 5m
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    rule_engine,
)

RULE_DIR = {
    "r1_buyer_down":            -1,   # buyers absorbed → fade long = short
    "r2_seller_up":             +1,   # sellers absorbed → fade short = long
    "r3_absorption_resistance": -1,   # strong buy failed at R → short
    "r4_absorption_support":    +1,
    "r5_bull_trap":             -1,
    "r6_bear_trap":             +1,
    "r7_cvd_divergence":         0,   # direction from delta sign
}


def eval_tf(symbol: str, tf: str, lookback: int) -> dict:
    multi = data_loader.load_multi_tf(symbol=symbol,
                                      timeframes=of_cfg.OF_TIMEFRAMES,
                                      lookback_days=lookback, use_cache=True)
    featured = {t: fe.build_features_for_tf(df, t) for t, df in multi.items()}
    joined = fe.build_feature_matrix(featured, anchor_tf=tf)
    joined = rule_engine.apply_rules(joined)

    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    # Signal at close(t) ⇒ fill at close(t+1), exit at close(t+1+h).
    entry = joined["Close"].shift(-1)
    exit_ = joined["Close"].shift(-1 - horizon)
    atr = joined["atr"].replace(0, np.nan)
    joined["pnl_r"] = ((exit_ - entry) / atr).fillna(0.0)

    results: dict[str, dict] = {}
    for rule, fixed_dir in RULE_DIR.items():
        mask = joined[rule].fillna(False) & entry.notna() & exit_.notna()
        rows = joined[mask]
        if rows.empty:
            results[rule] = {"count": 0}
            continue
        if fixed_dir == 0:
            dirs = np.where(rows["delta_ratio"] > 0, -1, 1)
        else:
            dirs = np.full(len(rows), fixed_dir)
        signed = rows["pnl_r"].to_numpy() * dirs
        results[rule] = {
            "count":        int(len(rows)),
            "hit_rate":     round(float((signed > 0).mean()), 3),
            "mean_r":       round(float(signed.mean()), 3),
            "median_r":     round(float(np.median(signed)), 3),
            "expectancy_r": round(float(signed.mean()), 3),
            "win_loss_r":   round(
                float(signed[signed > 0].mean() if (signed > 0).any() else 0.0)
                / max(abs(float(signed[signed < 0].mean()) if (signed < 0).any() else 1), 1e-9),
                2,
            ),
        }

    # Also: any rule fired + confidence proxy >= threshold. Approximate conf
    # by number of rules hit (proxy used live is similar — see predictor).
    any_hit = joined["rule_hit_count"] >= 1
    rows = joined[any_hit & entry.notna() & exit_.notna()]
    if not rows.empty:
        # direction = majority rule direction, ties → delta sign
        def _dir(r):
            votes = sum(RULE_DIR[c] for c in RULE_DIR if r.get(c, False) and RULE_DIR[c] != 0)
            if votes != 0:
                return 1 if votes > 0 else -1
            return -1 if r["delta_ratio"] > 0 else 1
        dirs = np.array([_dir(r) for _, r in rows.iterrows()])
        signed = rows["pnl_r"].to_numpy() * dirs
        results["_any_rule"] = {
            "count":        int(len(rows)),
            "hit_rate":     round(float((signed > 0).mean()), 3),
            "mean_r":       round(float(signed.mean()), 3),
            "expectancy_r": round(float(signed.mean()), 3),
        }

    return {"symbol": symbol, "tf": tf, "horizon_bars": horizon, "results": results}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES=F")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--lookback", type=int, default=of_cfg.OF_LOOKBACK_DAYS)
    args = ap.parse_args()
    print(json.dumps(eval_tf(args.symbol, args.tf, args.lookback),
                     indent=2, default=str))
