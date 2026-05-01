"""Replay trained ES model over the held-out tail of history.

For each bar where p_long >= WIN_THRESHOLD or p_short >= WIN_THRESHOLD:
    open trade at Close, ATR-sized TP/SL, walk forward HORIZON_BARS bars.
    First-touch decides outcome. R-multiple = +TP_ATR_MULT or -SL_ATR_MULT.

Reports:
    n trades, win rate, gross / net R, avg R, max DD (in R), Sharpe (per trade).

Usage:
    python -m ml_engine.backtest ES --schema ohlcv-1h
"""
import argparse
import json

import numpy as np
import pandas as pd
import xgboost as xgb

from ml_engine import config
from ml_engine.data_loader import load
from ml_engine.features.builder import build as build_features
from ml_engine.labels.builder import build as build_labels


def _load_models(symbol, schema):
    tag = schema.replace("ohlcv-", "")
    base = config.ARTIFACTS_DIR / f"{symbol}_{tag}"
    meta = json.loads((base / "meta.json").read_text())
    bL = xgb.Booster(); bL.load_model(str(base / "long.xgb"))
    bS = xgb.Booster(); bS.load_model(str(base / "short.xgb"))
    return meta, bL, bS


def _trade_outcome(side, entry, atr, fwd_high, fwd_low):
    """Walk forward bar by bar. Return R-multiple. When both barriers hit
    in same bar, fall back to conservative SL. Caller should prefer the
    intra-bar version when finer bars are available."""
    tp_mult, sl_mult = config.TP_ATR_MULT, config.SL_ATR_MULT
    if side == "long":
        tp = entry + tp_mult * atr
        sl = entry - sl_mult * atr
        for h, l in zip(fwd_high, fwd_low):
            hit_tp = h >= tp
            hit_sl = l <= sl
            if hit_tp and hit_sl:
                return -sl_mult
            if hit_tp:
                return tp_mult
            if hit_sl:
                return -sl_mult
        return 0.0
    else:
        tp = entry - tp_mult * atr
        sl = entry + sl_mult * atr
        for h, l in zip(fwd_high, fwd_low):
            hit_tp = l <= tp
            hit_sl = h >= sl
            if hit_tp and hit_sl:
                return -sl_mult
            if hit_tp:
                return tp_mult
            if hit_sl:
                return -sl_mult
        return 0.0


def _trade_outcome_intra(side, entry, atr, ts_start, horizon_bars,
                         coarse_min, fine_bars):
    """Use finer-grained bars to break ties when both barriers touched same coarse bar.
    fine_bars: DataFrame with High/Low indexed by timestamp, must cover the window.
    Walk fine bars in order: first to touch wins.
    """
    tp_mult, sl_mult = config.TP_ATR_MULT, config.SL_ATR_MULT
    end_ts = ts_start + pd.Timedelta(minutes=coarse_min * horizon_bars)
    window = fine_bars.loc[(fine_bars.index > ts_start) & (fine_bars.index <= end_ts)]
    if window.empty:
        return 0.0
    if side == "long":
        tp = entry + tp_mult * atr
        sl = entry - sl_mult * atr
        for _, row in window.iterrows():
            hit_tp = row["High"] >= tp
            hit_sl = row["Low"] <= sl
            if hit_tp and not hit_sl: return tp_mult
            if hit_sl and not hit_tp: return -sl_mult
            if hit_tp and hit_sl:
                # Both touched same fine bar — assume SL (still conservative
                # but applied at much finer granularity, ~15m vs 1h)
                return -sl_mult
        return 0.0
    else:
        tp = entry - tp_mult * atr
        sl = entry + sl_mult * atr
        for _, row in window.iterrows():
            hit_tp = row["Low"] <= tp
            hit_sl = row["High"] >= sl
            if hit_tp and not hit_sl: return tp_mult
            if hit_sl and not hit_tp: return -sl_mult
            if hit_tp and hit_sl:
                return -sl_mult
        return 0.0


_SCHEMA_TO_MIN = {"ohlcv-1m": 1, "ohlcv-15m": 15, "ohlcv-1h": 60}
_FINER = {"ohlcv-1h": "ohlcv-1m", "ohlcv-15m": "ohlcv-1m"}


def backtest(symbol, schema, test_frac=0.3, threshold: float | None = None,
             intra_bar: bool = True, conf_weight: bool = False):
    meta, bL, bS = _load_models(symbol, schema)
    df = load(symbol, schema)
    feats = build_features(df, include_macro=meta.get("include_macro", False), symbol=symbol)
    labs = build_labels(df)
    join = feats.join(labs[["atr", "entry"]], how="inner").dropna()

    feat_cols = meta["feature_cols"]
    n = len(join)
    test_start = int(n * (1 - test_frac))
    test = join.iloc[test_start:]
    bars = df.loc[test.index[0]:]

    fine_bars = None
    if intra_bar:
        finer_schema = _FINER.get(schema)
        if finer_schema:
            try:
                fine_bars = load(symbol, finer_schema)
                fine_bars = fine_bars[["High", "Low"]]
            except FileNotFoundError:
                fine_bars = None

    pL = bL.predict(xgb.DMatrix(test[feat_cols].values))
    pS = bS.predict(xgb.DMatrix(test[feat_cols].values))
    threshold = threshold if threshold is not None else config.WIN_THRESHOLD
    horizon = config.HORIZON_BARS

    trades = []
    bars_high = bars["High"].values
    bars_low = bars["Low"].values
    bars_idx = bars.index
    test_to_bar = {ts: i for i, ts in enumerate(bars_idx)}

    coarse_min = _SCHEMA_TO_MIN.get(schema, 60)

    for i, ts in enumerate(test.index):
        long_p, short_p = pL[i], pS[i]
        side = None
        if long_p >= threshold and long_p > short_p:
            side, conf = "long", long_p
        elif short_p >= threshold and short_p > long_p:
            side, conf = "short", short_p
        else:
            continue
        bi = test_to_bar.get(ts)
        if bi is None or bi + 1 + horizon > len(bars_idx):
            continue
        entry = float(test.iloc[i]["entry"])
        atr = float(test.iloc[i]["atr"])

        if fine_bars is not None:
            r = _trade_outcome_intra(side, entry, atr, ts, horizon, coarse_min, fine_bars)
        else:
            fwd_h = bars_high[bi + 1: bi + 1 + horizon]
            fwd_l = bars_low[bi + 1: bi + 1 + horizon]
            r = _trade_outcome(side, entry, atr, fwd_h, fwd_l)

        # Confidence-weighted size: scale R by (conf - threshold) / (1 - threshold)
        size = ((conf - threshold) / max(1e-6, 1 - threshold)) if conf_weight else 1.0
        trades.append({"ts": str(ts), "side": side, "conf": float(conf),
                       "entry": entry, "r": r * size, "raw_r": r, "size": size})

    if not trades:
        return {"symbol": symbol, "schema": schema, "n_trades": 0,
                "note": "No trades above threshold"}

    rs = np.array([t["r"] for t in trades])
    eq = np.cumsum(rs)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    wins = (rs > 0).sum()

    return {
        "symbol": symbol, "schema": schema,
        "threshold": threshold,
        "test_first": str(test.index[0]),
        "test_last": str(test.index[-1]),
        "n_trades": len(trades),
        "n_long":  int(sum(1 for t in trades if t["side"] == "long")),
        "n_short": int(sum(1 for t in trades if t["side"] == "short")),
        "win_rate": float(wins / len(rs)),
        "total_R": float(rs.sum()),
        "avg_R": float(rs.mean()),
        "max_dd_R": float(dd.min()),
        "sharpe_per_trade": float(rs.mean() / rs.std()) if rs.std() > 0 else float("nan"),
        "rs_first_5": rs[:5].tolist(),
        "rs_last_5": rs[-5:].tolist(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--no-intra", action="store_true", help="disable intra-bar walk")
    ap.add_argument("--conf-weight", action="store_true", help="size each trade by conf")
    args = ap.parse_args()
    print(json.dumps(backtest(args.symbol, args.schema, args.test_frac,
                              args.threshold, intra_bar=not args.no_intra,
                              conf_weight=args.conf_weight),
                     indent=2, default=str))


if __name__ == "__main__":
    main()
