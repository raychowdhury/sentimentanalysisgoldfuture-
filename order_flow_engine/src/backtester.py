"""
Simple forward-PnL backtest of flagged events.

For each non-normal alert/label emits, compute the forward return over the
label's horizon and compare against the bar's ATR to normalize ("R-multiple"
style). Reports per-label hit rate, mean R, and expectancy.

This is a research aid, not a trading simulation — no slippage, no fills,
no position sizing.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    label_generator,
    rule_engine,
)


# Trading direction implied by each label. Absorption/traps are "fade the
# failed move"; possible_reversal follows the delta sign.
LABEL_TRADE_DIRECTION = {
    "buyer_absorption":  -1,
    "seller_absorption": +1,
    "bullish_trap":      -1,
    "bearish_trap":      +1,
    "possible_reversal":  0,  # direction determined per-row from delta_ratio
    "normal_behavior":    0,
}


def _direction_for_row(label: str, row: pd.Series) -> int:
    fixed = LABEL_TRADE_DIRECTION.get(label, 0)
    if fixed != 0:
        return fixed
    if label == "possible_reversal":
        # fade the flow direction
        return -1 if row.get("delta_ratio", 0) > 0 else 1
    return 0


def run(
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    output_dir: Path | None = None,
) -> dict:
    symbol    = symbol or of_cfg.OF_SYMBOL
    timeframe = timeframe or of_cfg.OF_ANCHOR_TF
    lookback  = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    out_dir   = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR

    multi = data_loader.load_multi_tf(symbol=symbol, timeframes=of_cfg.OF_TIMEFRAMES,
                                      lookback_days=lookback, use_cache=True)
    featured = {tf: fe.build_features_for_tf(df, tf) for tf, df in multi.items()}
    joined = fe.build_feature_matrix(featured, anchor_tf=timeframe)
    joined = rule_engine.apply_rules(joined)
    joined["label"] = label_generator.generate_labels(joined, timeframe)

    horizon = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    fwd_move = joined["Close"].shift(-horizon) - joined["Close"]
    atr_safe = joined["atr"].replace(0, np.nan)
    joined["fwd_move"] = fwd_move
    joined["fwd_r"]    = (fwd_move / atr_safe).fillna(0.0)

    results: dict[str, dict] = {}
    for label in of_cfg.LABEL_CLASSES:
        if label == "normal_behavior":
            continue
        mask = joined["label"] == label
        rows = joined[mask]
        if rows.empty:
            results[label] = {"count": 0}
            continue
        dirs = np.array([_direction_for_row(label, r) for _, r in rows.iterrows()])
        signed_r = rows["fwd_r"].to_numpy() * dirs
        hits = (signed_r > 0).sum()
        results[label] = {
            "count": int(len(rows)),
            "mean_r": round(float(signed_r.mean()), 4),
            "median_r": round(float(np.median(signed_r)), 4),
            "hit_rate": round(float(hits / len(rows)), 4),
            "expectancy_r": round(float(signed_r.mean()), 4),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "backtest_summary.json"
    with path.open("w") as f:
        json.dump({"symbol": symbol, "timeframe": timeframe, "results": results},
                  f, indent=2, default=str)
    return results


def threshold_sweep(
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    grid: list[int] | None = None,
    output_dir: Path | None = None,
) -> dict:
    """
    Sweep OF_ALERT_MIN_CONF over grid; for each label report expectancy at
    each threshold and pick the threshold maximizing expectancy×count^0.25
    (small Bayesian-ish penalty for tiny samples). Output saved JSON for
    operator to copy into config.
    """
    grid = grid or [40, 50, 55, 60, 65, 70, 75, 80, 85]
    symbol    = symbol or of_cfg.OF_SYMBOL
    timeframe = timeframe or of_cfg.OF_ANCHOR_TF
    lookback  = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    out_dir   = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR

    # Build the same labelled, rule-tagged frame backtester.run uses, but
    # also need a confidence proxy per bar. Rule-only confidence is fine
    # here — gives a uniform pre-model baseline to tune the gate.
    from order_flow_engine.src import predictor as _pred
    multi = data_loader.load_multi_tf(symbol=symbol, timeframes=of_cfg.OF_TIMEFRAMES,
                                      lookback_days=lookback, use_cache=True)
    featured = {tf: fe.build_features_for_tf(df, tf) for tf, df in multi.items()}
    joined = fe.build_feature_matrix(featured, anchor_tf=timeframe)
    joined = rule_engine.apply_rules(joined)
    joined["label"] = label_generator.generate_labels(joined, timeframe)
    joined["conf_rule"] = joined.apply(_pred.rule_only_confidence, axis=1)

    horizon = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    fwd = joined["Close"].shift(-horizon) - joined["Close"]
    atr = joined["atr"].replace(0, np.nan)
    joined["fwd_r"] = (fwd / atr).fillna(0.0)

    sweep: dict[str, dict] = {}
    best_per_label: dict[str, dict] = {}

    for label in of_cfg.LABEL_CLASSES:
        if label == "normal_behavior":
            continue
        rows_all = joined[joined["label"] == label]
        if rows_all.empty:
            sweep[label] = {}
            continue
        per_thr = {}
        for thr in grid:
            rows = rows_all[rows_all["conf_rule"] >= thr]
            n = int(len(rows))
            if n == 0:
                per_thr[str(thr)] = {"count": 0, "expectancy_r": 0.0, "score": 0.0}
                continue
            dirs = np.array([LABEL_TRADE_DIRECTION.get(label, 0) or
                             (-1 if r.get("delta_ratio", 0) > 0 else 1)
                             for _, r in rows.iterrows()])
            signed = rows["fwd_r"].to_numpy() * dirs
            exp_r = float(signed.mean())
            score = exp_r * (n ** 0.25)   # small-sample penalty
            per_thr[str(thr)] = {
                "count": n,
                "expectancy_r": round(exp_r, 4),
                "hit_rate":     round(float((signed > 0).mean()), 4),
                "score":        round(score, 4),
            }
        # pick best threshold
        best_thr, best_meta = max(
            per_thr.items(), key=lambda kv: kv[1].get("score", 0.0)
        )
        best_per_label[label] = {"threshold": int(best_thr), **best_meta}
        sweep[label] = per_thr

    out = {
        "symbol":    symbol,
        "timeframe": timeframe,
        "grid":      grid,
        "sweep":     sweep,
        "best":      best_per_label,
        "global_best_threshold": int(round(np.median(
            [v["threshold"] for v in best_per_label.values()] or [70]
        ))),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "threshold_sweep.json").write_text(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=of_cfg.OF_SYMBOL)
    ap.add_argument("--tf", default=of_cfg.OF_ANCHOR_TF)
    ap.add_argument("--sweep", action="store_true",
                    help="run threshold sweep instead of single backtest")
    args = ap.parse_args()
    if args.sweep:
        print(json.dumps(threshold_sweep(symbol=args.symbol, timeframe=args.tf),
                         indent=2, default=str))
    else:
        print(json.dumps(run(symbol=args.symbol, timeframe=args.tf),
                         indent=2, default=str))
