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
    alert_engine,
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


# Point value per symbol (contract size × tick value). Defaults to $1/pt.
SYMBOL_POINT_VALUE = {
    "ES=F": 50.0,
    "NQ=F": 20.0,
    "YM=F": 5.0,
    "RTY=F": 50.0,
    "GC=F": 100.0,
    "CL=F": 1000.0,
}


def build_trades(
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    min_conf: int | None = None,
    allowed_labels: set[str] | None = None,
) -> dict:
    """
    Trader-facing trade list. Each alert that passes gating is one trade:
    entry at close of bar after signal, exit `horizon` bars later.

    Returns: {
        "symbol", "timeframe", "point_value",
        "trades": [
            {entry_time, entry_price, exit_time, exit_price, side,
             buy_price, sell_price, label, rules, confidence,
             pnl_pts, pnl_usd, pnl_r, result},
            ...
        ],
        "summary": {count, wins, losses, hit_rate, sum_pts, sum_usd, mean_r, expectancy_usd},
    }
    """
    from order_flow_engine.src import predictor as _pred

    symbol      = symbol or of_cfg.OF_SYMBOL
    timeframe   = timeframe or of_cfg.OF_ANCHOR_TF
    lookback    = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    threshold   = int(of_cfg.OF_ALERT_MIN_CONF if min_conf is None else min_conf)
    labels_ok   = allowed_labels if allowed_labels is not None else of_cfg.OF_ALERT_ALLOWED_LABELS

    multi = data_loader.load_multi_tf(symbol=symbol, timeframes=of_cfg.OF_TIMEFRAMES,
                                      lookback_days=lookback, use_cache=True)
    featured = {tf: fe.build_features_for_tf(df, tf) for tf, df in multi.items()}
    joined = fe.build_feature_matrix(featured, anchor_tf=timeframe)
    joined = rule_engine.apply_rules(joined)
    # Use rule-only label + confidence so results reflect the live rule path
    # (no lookahead from label_generator's forward-reversal filter).
    joined["label"] = joined.apply(_pred._rule_only_label, axis=1)
    joined["conf"]  = joined.apply(_pred.rule_only_confidence, axis=1)

    horizon   = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    # Rules are causal at bar t close; earliest real-world fill is close(t+1).
    # Exit `horizon` bars after entry.
    entry_offset = 1
    entry_px  = joined["Close"].shift(-entry_offset)
    exit_px   = joined["Close"].shift(-entry_offset - horizon)
    atr_safe  = joined["atr"].replace(0, np.nan)
    pt_value  = SYMBOL_POINT_VALUE.get(symbol, 1.0)

    # Cooldown + volume gate mirror the live gate so backtest numbers match
    # what the system actually would have emitted.
    cooldown_bars = of_cfg.ALERT_COOLDOWN_BARS
    last_emit_idx: dict[str, int] = {}
    # Precompute rolling volume threshold per-bar (uses trailing window as live does).
    if "Volume" in joined.columns:
        vol = joined["Volume"].fillna(0.0)
        vol_threshold = (
            vol.rolling(of_cfg.VOLUME_GATE_WINDOW, min_periods=20)
               .quantile(of_cfg.VOLUME_GATE_PCTL)
        )
    else:
        vol = None
        vol_threshold = None

    trades: list[dict] = []
    skipped = {"volume": 0, "cooldown": 0, "below_conf": 0, "label": 0, "no_bars": 0}
    for i, (ts, row) in enumerate(joined.iterrows()):
        label = row.get("label", "normal_behavior")
        if label == "normal_behavior":
            continue
        if labels_ok and label not in labels_ok:
            skipped["label"] += 1
            continue
        conf = int(row.get("conf", 0))
        if conf < threshold:
            skipped["below_conf"] += 1
            continue
        # Volume gate — rolling trailing-window threshold.
        if vol_threshold is not None and of_cfg.VOLUME_GATE_PCTL:
            thr = vol_threshold.loc[ts] if ts in vol_threshold.index else np.nan
            bar_vol = float(row.get("Volume", 0) or 0)
            if not np.isnan(thr) and bar_vol < float(thr):
                skipped["volume"] += 1
                continue
        # Per-label cooldown
        if i - last_emit_idx.get(label, -10**9) < cooldown_bars:
            skipped["cooldown"] += 1
            continue

        ep = entry_px.loc[ts] if ts in entry_px.index else np.nan
        xp = exit_px.loc[ts]  if ts in exit_px.index  else np.nan
        if pd.isna(ep) or pd.isna(xp):
            skipped["no_bars"] += 1
            continue

        direction = _direction_for_row(label, row)
        if direction == 0:
            continue
        last_emit_idx[label] = i
        side = "long" if direction > 0 else "short"
        if side == "long":
            buy_price, sell_price = float(ep), float(xp)
            buy_time  = _shift_ts(joined, ts, entry_offset)
            sell_time = _shift_ts(joined, ts, entry_offset + horizon)
        else:
            sell_price, buy_price = float(ep), float(xp)
            sell_time = _shift_ts(joined, ts, entry_offset)
            buy_time  = _shift_ts(joined, ts, entry_offset + horizon)

        pnl_pts = (float(xp) - float(ep)) * direction
        pnl_usd = pnl_pts * pt_value
        atr_v   = float(atr_safe.loc[ts]) if ts in atr_safe.index and not pd.isna(atr_safe.loc[ts]) else np.nan
        pnl_r   = round(pnl_pts / atr_v, 3) if atr_v and not np.isnan(atr_v) else None

        trades.append({
            "signal_time": _iso(ts),
            "entry_time":  _iso(_shift_ts(joined, ts, entry_offset)),
            "entry_price": round(float(ep), 4),
            "exit_time":   _iso(_shift_ts(joined, ts, entry_offset + horizon)),
            "exit_price":  round(float(xp), 4),
            "side":        side,
            "buy_price":   round(buy_price, 4),
            "sell_price":  round(sell_price, 4),
            "buy_time":    _iso(buy_time),
            "sell_time":   _iso(sell_time),
            "label":       label,
            "rules":       [r for r in row.get("rule_hit_codes", "").split(";") if r],
            "confidence":  conf,
            "pnl_pts":     round(pnl_pts, 4),
            "pnl_usd":     round(pnl_usd, 2),
            "pnl_r":       pnl_r,
            "result":      "win" if pnl_pts > 0 else ("loss" if pnl_pts < 0 else "flat"),
        })

    wins     = sum(1 for t in trades if t["result"] == "win")
    losses   = sum(1 for t in trades if t["result"] == "loss")
    n        = len(trades)
    sum_pts  = sum(t["pnl_pts"] for t in trades)
    sum_usd  = sum(t["pnl_usd"] for t in trades)
    rs       = [t["pnl_r"] for t in trades if t["pnl_r"] is not None]
    mean_r   = round(float(np.mean(rs)), 3) if rs else 0.0

    return {
        "symbol":      symbol,
        "timeframe":   timeframe,
        "lookback_days": lookback,
        "min_conf":    threshold,
        "allowed_labels": sorted(labels_ok) if labels_ok else [],
        "horizon_bars": horizon,
        "entry_offset_bars": entry_offset,
        "cooldown_bars": cooldown_bars,
        "volume_gate_pctl": of_cfg.VOLUME_GATE_PCTL,
        "point_value": pt_value,
        "skipped":     skipped,
        "trades":      trades,
        "summary": {
            "count":          n,
            "wins":           wins,
            "losses":         losses,
            "hit_rate":       round(wins / n, 3) if n else 0.0,
            "sum_pts":        round(sum_pts, 2),
            "sum_usd":        round(sum_usd, 2),
            "mean_r":         mean_r,
            "expectancy_usd": round(sum_usd / n, 2) if n else 0.0,
        },
    }


def _shift_ts(df: pd.DataFrame, ts, steps: int):
    """Return the index value `steps` bars after `ts`. None if off the end."""
    try:
        i = df.index.get_loc(ts)
    except KeyError:
        return None
    j = i + steps
    if j < 0 or j >= len(df.index):
        return None
    return df.index[j]


def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return pd.Timestamp(ts).isoformat()
    except Exception:
        return str(ts)


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
