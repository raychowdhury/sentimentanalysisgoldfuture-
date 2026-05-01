"""Inference: latest bar -> prediction with time / entry / exit / confidence.

Returns dict shaped for the dashboard:
    {
      "symbol": "GC",
      "schema": "ohlcv-15m",
      "as_of": "...",            # last bar timestamp
      "expected_window": "...",  # entry bar -> entry bar + horizon (in minutes)
      "side": "long" | "short" | "none",
      "confidence": 0.72,
      "entry": 4587.1,
      "stop":  4575.4,
      "target": 4610.5,
      "rr": 2.0,
      "p_long": 0.72,
      "p_short": 0.28,
      "horizon_minutes": 120,
      "model_meta": {...}
    }
"""
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import xgboost as xgb

from ml_engine import config
from ml_engine.data_loader import load
from ml_engine.features.builder import build as build_features
from ml_engine.labels.builder import build as build_labels


_SCHEMA_TO_MIN = {
    "ohlcv-1m": 1, "ohlcv-5m": 5, "ohlcv-15m": 15,
    "ohlcv-1h": 60, "ohlcv-1d": 60 * 24,
}


def _load_models(symbol: str, schema: str):
    tag = schema.replace("ohlcv-", "")
    base = config.ARTIFACTS_DIR / f"{symbol}_{tag}"
    meta_path = base / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Train first: python -m ml_engine.models.trainer {symbol}")
    meta = json.loads(meta_path.read_text())
    long_b = xgb.Booster(); long_b.load_model(str(base / "long.xgb"))
    short_b = xgb.Booster(); short_b.load_model(str(base / "short.xgb"))
    return meta, long_b, short_b


def predict(symbol: str, schema: str = config.SCHEMA_15M) -> dict:
    meta, long_b, short_b = _load_models(symbol, schema)
    df = load(symbol, schema)
    feats = build_features(df, include_macro=meta.get("include_macro", False), symbol=symbol).dropna()
    if feats.empty:
        return {"symbol": symbol, "schema": schema, "side": "none",
                "reason": "Not enough bars for features"}

    feat_cols = meta["feature_cols"]
    last_row = feats.iloc[[-1]][feat_cols]
    last_ts = feats.index[-1]

    # Compute current ATR + entry using same logic as trainer labels
    labs = build_labels(df)
    atr = float(labs.loc[last_ts, "atr"]) if last_ts in labs.index else float("nan")
    entry = float(df.loc[last_ts, "Close"])

    d = xgb.DMatrix(last_row.values)
    p_long = float(long_b.predict(d)[0])
    p_short = float(short_b.predict(d)[0])

    side = "none"
    conf = max(p_long, p_short)
    if p_long >= config.WIN_THRESHOLD and p_long > p_short:
        side = "long"
    elif p_short >= config.WIN_THRESHOLD and p_short > p_long:
        side = "short"

    if side == "long":
        target = entry + config.TP_ATR_MULT * atr
        stop = entry - config.SL_ATR_MULT * atr
        conf = p_long
    elif side == "short":
        target = entry - config.TP_ATR_MULT * atr
        stop = entry + config.SL_ATR_MULT * atr
        conf = p_short
    else:
        target = stop = float("nan")

    bar_min = _SCHEMA_TO_MIN.get(schema, 15)
    horizon_min = bar_min * config.HORIZON_BARS
    window_end = last_ts + timedelta(minutes=horizon_min)

    return {
        "symbol": symbol,
        "schema": schema,
        "as_of": str(last_ts),
        "expected_window_start": str(last_ts),
        "expected_window_end": str(window_end),
        "horizon_minutes": horizon_min,
        "side": side,
        "confidence": round(conf, 4),
        "entry": round(entry, 4),
        "stop": (round(stop, 4) if stop == stop else None),
        "target": (round(target, 4) if target == target else None),
        "rr": config.MIN_RR,
        "atr": (round(atr, 4) if atr == atr else None),
        "p_long": round(p_long, 4),
        "p_short": round(p_short, 4),
        "win_threshold": config.WIN_THRESHOLD,
        "model_trained_at": meta.get("trained_at"),
    }


def predict_all(symbols=None, schema=config.SCHEMA_15M) -> list[dict]:
    syms = symbols or list(config.SYMBOL_MAP.keys())
    out = []
    for s in syms:
        try:
            out.append(predict(s, schema))
        except FileNotFoundError as e:
            out.append({"symbol": s, "schema": schema, "side": "none", "error": str(e)})
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    args = ap.parse_args()
    if args.symbol:
        print(json.dumps(predict(args.symbol, args.schema), indent=2, default=str))
    else:
        print(json.dumps(predict_all(schema=args.schema), indent=2, default=str))
