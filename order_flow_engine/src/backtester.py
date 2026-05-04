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
        # Prefer the precomputed reversal_direction (set in rule_engine):
        # handles r7-only fires where delta_ratio is uninformative. Falls
        # back to delta_ratio sign for older rows lacking the column.
        rd = row.get("reversal_direction", 0)
        try:
            rd = int(rd)
        except (TypeError, ValueError):
            rd = 0
        if rd != 0:
            return rd
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


# Labels whose firing rule(s) are confirmation-only (need fwd_ret_1 from
# the next bar). For these, the alert is only known one bar AFTER the
# signal-bar close — so the earliest fill is two bars after the signal
# index (`entry_offset = 2`). Causal/mixed labels use `entry_offset = 1`.
_CONFIRMATION_LABELS: set[str] = {"buyer_absorption", "seller_absorption"}

# Stop loss assumed for the backtest (informational — trade still exits at
# horizon close for PnL accounting). Set to 1.0×ATR to match a typical
# 1R risk unit.
BACKTEST_STOP_ATR_MULT: float = 1.0


def build_trades(
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    min_conf: int | None = None,
    allowed_labels: set[str] | None = None,
) -> dict:
    """
    Trader-facing trade list. Each alert that passes gating is one trade:
    entry at close of the first executable bar after the signal, exit
    `horizon` bars later.

    Per-label entry alignment:
      - Causal labels (bullish_trap / bearish_trap / possible_reversal) →
        entry at close(t+1). Rule fires at close(t).
      - Confirmation labels (buyer/seller_absorption) → entry at close(t+2).
        Rule needs fwd_ret_1 = close(t+1) - close(t); alert is only available
        after close(t+1), so the trader fills at close(t+2). Earlier offsets
        would be lookahead.

    Returns: {
        "symbol", "timeframe", "point_value",
        "trades": [
            {signal_time, entry_time, entry_price, stop_loss, atr,
             exit_time, exit_price, side, buy_price, sell_price,
             buy_time, sell_time, label, pass_type, entry_offset_bars,
             rules, confidence,
             max_favorable_pts, max_adverse_pts,
             max_favorable_atr, max_adverse_atr,
             would_stop_hit_atr, stop_hit_bar,
             pnl_pts, pnl_usd, pnl_r, result},
            ...
        ],
        "summary": {count, wins, losses, hit_rate, sum_pts, sum_usd,
                    mean_r, expectancy_usd, stop_hit_count},
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
    n_rows = len(joined.index)
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

        # Per-label entry offset: confirmation labels need an extra bar to
        # avoid lookahead (rule consumes close(t+1)).
        entry_offset = 2 if label in _CONFIRMATION_LABELS else 1
        pass_type = "confirm" if label in _CONFIRMATION_LABELS else "causal"

        entry_idx = i + entry_offset
        exit_idx  = i + entry_offset + horizon
        if exit_idx >= n_rows:
            skipped["no_bars"] += 1
            continue
        ep = float(joined.iloc[entry_idx]["Close"])
        xp = float(joined.iloc[exit_idx]["Close"])

        direction = _direction_for_row(label, row)
        if direction == 0:
            continue
        last_emit_idx[label] = i
        side = "long" if direction > 0 else "short"
        entry_ts = joined.index[entry_idx]
        exit_ts  = joined.index[exit_idx]
        if side == "long":
            buy_price, sell_price = ep, xp
            buy_time, sell_time = entry_ts, exit_ts
        else:
            sell_price, buy_price = ep, xp
            sell_time, buy_time = entry_ts, exit_ts

        pnl_pts = (xp - ep) * direction
        pnl_usd = pnl_pts * pt_value
        atr_v   = float(atr_safe.loc[ts]) if ts in atr_safe.index and not pd.isna(atr_safe.loc[ts]) else np.nan
        pnl_r   = round(pnl_pts / atr_v, 3) if atr_v and not np.isnan(atr_v) else None

        # Stop loss: 1×ATR from entry on the loss side.
        stop_loss = (
            round(ep - direction * BACKTEST_STOP_ATR_MULT * atr_v, 4)
            if atr_v and not np.isnan(atr_v) else None
        )

        # MAE / MFE — scan the held window (entry bar through exit bar inclusive)
        # and find the worst adverse move and best favorable move.
        seg = joined.iloc[entry_idx:exit_idx + 1]
        if direction > 0:
            mfe_px = float(seg["High"].max())
            mae_px = float(seg["Low"].min())
            mfe = mfe_px - ep
            mae = mae_px - ep
            bars_to_fav = int(seg["High"].values.argmax())
            bars_to_adv = int(seg["Low"].values.argmin())
        else:
            mfe_px = float(seg["Low"].min())
            mae_px = float(seg["High"].max())
            mfe = ep - mfe_px
            mae = ep - mae_px
            bars_to_fav = int(seg["Low"].values.argmin())
            bars_to_adv = int(seg["High"].values.argmax())

        mae_atr = round(mae / atr_v, 3) if atr_v and not np.isnan(atr_v) else None
        mfe_atr = round(mfe / atr_v, 3) if atr_v and not np.isnan(atr_v) else None
        would_stop_hit = bool(
            atr_v and not np.isnan(atr_v) and mae <= -BACKTEST_STOP_ATR_MULT * atr_v
        )

        # ── Stop-honored PnL ────────────────────────────────────────────
        # Walk bars from entry through exit; if Low (long) or High (short)
        # crosses the stop level, close at the stop price and stop here.
        # Otherwise hold to horizon. This produces the realistic exit a
        # trader running a hard stop would have experienced.
        stop_exit_idx   = None
        stop_exit_price = None
        stop_exit_time  = None
        exit_reason     = "horizon"
        stop_pnl_pts    = pnl_pts
        if atr_v and not np.isnan(atr_v):
            stop_dist = BACKTEST_STOP_ATR_MULT * atr_v
            stop_px   = ep - direction * stop_dist  # below for long, above for short
            for k in range(len(seg)):
                row_seg = seg.iloc[k]
                if direction > 0:
                    if float(row_seg["Low"]) <= stop_px:
                        stop_exit_idx = entry_idx + k
                        break
                else:
                    if float(row_seg["High"]) >= stop_px:
                        stop_exit_idx = entry_idx + k
                        break
            if stop_exit_idx is not None:
                stop_exit_price = stop_px
                stop_exit_time  = joined.index[stop_exit_idx]
                stop_pnl_pts    = direction * (stop_exit_price - ep)
                exit_reason     = "stop"
        stop_pnl_usd = stop_pnl_pts * pt_value
        stop_pnl_r   = round(stop_pnl_pts / atr_v, 3) if atr_v and not np.isnan(atr_v) else None
        stop_result  = "win" if stop_pnl_pts > 0 else ("loss" if stop_pnl_pts < 0 else "flat")

        trades.append({
            "signal_time": _iso(ts),
            "entry_time":  _iso(entry_ts),
            "entry_price": round(ep, 4),
            "exit_time":   _iso(exit_ts),
            "exit_price":  round(xp, 4),
            "side":        side,
            "buy_price":   round(buy_price, 4),
            "sell_price":  round(sell_price, 4),
            "buy_time":    _iso(buy_time),
            "sell_time":   _iso(sell_time),
            "label":       label,
            "pass_type":   pass_type,
            "entry_offset_bars": entry_offset,
            "rules":       [r for r in row.get("rule_hit_codes", "").split(";") if r],
            "confidence":  conf,
            "atr":         round(atr_v, 4) if atr_v and not np.isnan(atr_v) else None,
            "stop_loss":   stop_loss,
            "stop_atr_mult":      BACKTEST_STOP_ATR_MULT,
            "max_favorable_pts":  round(float(mfe), 4),
            "max_adverse_pts":    round(float(mae), 4),
            "max_favorable_atr":  mfe_atr,
            "max_adverse_atr":    mae_atr,
            "max_favorable_price": round(float(mfe_px), 4),
            "max_adverse_price":   round(float(mae_px), 4),
            "bars_to_max_favorable": bars_to_fav,
            "bars_to_max_adverse":   bars_to_adv,
            "would_stop_hit_atr":  would_stop_hit,
            "stop_hit_bar":        bars_to_adv if would_stop_hit else None,
            "pnl_pts":     round(pnl_pts, 4),
            "pnl_usd":     round(pnl_usd, 2),
            "pnl_r":       pnl_r,
            "result":      "win" if pnl_pts > 0 else ("loss" if pnl_pts < 0 else "flat"),
            # Stop-honored exit: realistic PnL when 1×ATR stop is enforced.
            "exit_reason":      exit_reason,
            "stop_exit_time":   _iso(stop_exit_time),
            "stop_exit_price":  round(stop_exit_price, 4) if stop_exit_price is not None else None,
            "stop_pnl_pts":     round(stop_pnl_pts, 4),
            "stop_pnl_usd":     round(stop_pnl_usd, 2),
            "stop_pnl_r":       stop_pnl_r,
            "stop_result":      stop_result,
        })

    wins     = sum(1 for t in trades if t["result"] == "win")
    losses   = sum(1 for t in trades if t["result"] == "loss")
    n        = len(trades)
    sum_pts  = sum(t["pnl_pts"] for t in trades)
    sum_usd  = sum(t["pnl_usd"] for t in trades)
    rs       = [t["pnl_r"] for t in trades if t["pnl_r"] is not None]
    mean_r   = round(float(np.mean(rs)), 3) if rs else 0.0

    # Stop-honored aggregates — realistic outcomes with a 1×ATR hard stop.
    stop_wins   = sum(1 for t in trades if t["stop_result"] == "win")
    stop_losses = sum(1 for t in trades if t["stop_result"] == "loss")
    stop_sum_pts = sum(t["stop_pnl_pts"] for t in trades)
    stop_sum_usd = sum(t["stop_pnl_usd"] for t in trades)
    stop_rs = [t["stop_pnl_r"] for t in trades if t["stop_pnl_r"] is not None]
    stop_mean_r = round(float(np.mean(stop_rs)), 3) if stop_rs else 0.0
    stopped_out = sum(1 for t in trades if t["exit_reason"] == "stop")

    stop_hits = sum(1 for t in trades if t.get("would_stop_hit_atr"))
    return {
        "symbol":      symbol,
        "timeframe":   timeframe,
        "lookback_days": lookback,
        "min_conf":    threshold,
        "allowed_labels": sorted(labels_ok) if labels_ok else [],
        "horizon_bars": horizon,
        # Mixed alignment: causal=1, confirmation=2. Per-trade in `entry_offset_bars`.
        "entry_offset_bars_causal":      1,
        "entry_offset_bars_confirmation": 2,
        "cooldown_bars": cooldown_bars,
        "volume_gate_pctl": of_cfg.VOLUME_GATE_PCTL,
        "stop_atr_mult": BACKTEST_STOP_ATR_MULT,
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
            "stop_hit_count": stop_hits,
        },
        "summary_with_stop": {
            "count":          n,
            "stopped_out":    stopped_out,
            "wins":           stop_wins,
            "losses":         stop_losses,
            "hit_rate":       round(stop_wins / n, 3) if n else 0.0,
            "sum_pts":        round(stop_sum_pts, 2),
            "sum_usd":        round(stop_sum_usd, 2),
            "mean_r":         stop_mean_r,
            "expectancy_usd": round(stop_sum_usd / n, 2) if n else 0.0,
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


def param_sweep(
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    horizons: list[int] | None = None,
    stop_atr_mults: list[float] | None = None,
    volume_gate_pctls: list[float] | None = None,
    delta_dominance_levels: list[float] | None = None,
    min_conf: int = 40,
    output_dir: Path | None = None,
) -> dict:
    """
    Grid-sweep four key knobs and rank by stop-honored expectancy × √sample.

    Knobs swept:
      - horizon (forward bars to exit at)
      - stop_atr_mult (1×ATR vs wider)
      - volume_gate_pctl (gate strictness; 0 = disabled)
      - rule_delta_dominance (R1/R2 directional-flow threshold)

    Score: stop_expectancy_usd × n^0.5 (sample penalty: needs >= 5 trades to mean
    much). Top N configs returned with full per-label breakdown.
    """
    global BACKTEST_STOP_ATR_MULT
    symbol    = symbol or of_cfg.OF_SYMBOL
    timeframe = timeframe or of_cfg.OF_ANCHOR_TF
    lookback  = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    out_dir   = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    horizons         = horizons         or [12, 24, 36]
    stop_atr_mults   = stop_atr_mults   or [1.0, 1.5, 2.0]
    volume_gate_pctls = volume_gate_pctls or [0.0, 0.20]
    delta_dominance_levels = delta_dominance_levels or [0.25, 0.40]

    orig_horizon  = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    orig_stop     = BACKTEST_STOP_ATR_MULT
    orig_vol_pctl = of_cfg.VOLUME_GATE_PCTL
    orig_delta    = of_cfg.RULE_DELTA_DOMINANCE

    results: list[dict] = []
    try:
        for h in horizons:
            of_cfg.OF_FORWARD_BARS[timeframe] = h
            for sm in stop_atr_mults:
                BACKTEST_STOP_ATR_MULT = float(sm)
                for vp in volume_gate_pctls:
                    of_cfg.VOLUME_GATE_PCTL = float(vp)
                    for dd in delta_dominance_levels:
                        of_cfg.RULE_DELTA_DOMINANCE = float(dd)
                        bt = build_trades(
                            symbol=symbol, timeframe=timeframe,
                            lookback_days=lookback, min_conf=min_conf,
                            allowed_labels=set(),
                        )
                        ws = bt["summary_with_stop"]
                        h_sum = bt["summary"]
                        n = ws["count"]
                        exp_usd = ws["expectancy_usd"]
                        score = exp_usd * (n ** 0.5) if n else 0.0
                        results.append({
                            "horizon":     h,
                            "stop_atr":    sm,
                            "vol_pctl":    vp,
                            "delta_dom":   dd,
                            "n":           n,
                            "stopped_out": ws["stopped_out"],
                            "stop_wins":   ws["wins"],
                            "stop_losses": ws["losses"],
                            "stop_hit_rate":  ws["hit_rate"],
                            "stop_exp_usd":   exp_usd,
                            "stop_mean_r":    ws["mean_r"],
                            "horizon_exp_usd": h_sum["expectancy_usd"],
                            "horizon_mean_r":  h_sum["mean_r"],
                            "score":          round(score, 4),
                        })
    finally:
        of_cfg.OF_FORWARD_BARS[timeframe] = orig_horizon
        BACKTEST_STOP_ATR_MULT = orig_stop
        of_cfg.VOLUME_GATE_PCTL = orig_vol_pctl
        of_cfg.RULE_DELTA_DOMINANCE = orig_delta

    results.sort(key=lambda r: r["score"], reverse=True)
    out = {
        "symbol":       symbol,
        "timeframe":    timeframe,
        "lookback_days": lookback,
        "min_conf":     min_conf,
        "grid_size":    len(results),
        "ranked":       results,
        "best":         results[0] if results else None,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "param_sweep.json").write_text(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=of_cfg.OF_SYMBOL)
    ap.add_argument("--tf", default=of_cfg.OF_ANCHOR_TF)
    ap.add_argument("--sweep", action="store_true",
                    help="run threshold sweep instead of single backtest")
    ap.add_argument("--param-sweep", action="store_true",
                    help="grid-sweep horizon × stop × volume gate × delta dominance")
    args = ap.parse_args()
    if args.param_sweep:
        print(json.dumps(param_sweep(symbol=args.symbol, timeframe=args.tf),
                         indent=2, default=str))
    elif args.sweep:
        print(json.dumps(threshold_sweep(symbol=args.symbol, timeframe=args.tf),
                         indent=2, default=str))
    else:
        print(json.dumps(run(symbol=args.symbol, timeframe=args.tf),
                         indent=2, default=str))
