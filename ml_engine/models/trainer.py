"""Train two XGBoost binary classifiers (long, short) per symbol+timeframe.

Walk-forward split: train [0..0.7], valid [0.7..0.85], test [0.85..1].
Saves model + feature column order + metadata.

Usage:
    python -m ml_engine.models.trainer GC --schema ohlcv-15m
"""
import argparse
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import xgboost as xgb

from ml_engine import config
from ml_engine.data_loader import load
from ml_engine.features.builder import build as build_features
from ml_engine.labels.builder import build as build_labels
from ml_engine.labels.path_builder import build as build_path_labels


def _split(n: int) -> tuple[slice, slice, slice]:
    a = int(n * config.TRAIN_FRAC)
    b = int(n * (config.TRAIN_FRAC + config.VALID_FRAC))
    return slice(0, a), slice(a, b), slice(b, n)


def _train_one(X: pd.DataFrame, y: pd.Series, side: str, out_path) -> dict:
    n = len(X)
    tr, va, te = _split(n)
    dtr = xgb.DMatrix(X.iloc[tr].values, label=y.iloc[tr].values)
    dva = xgb.DMatrix(X.iloc[va].values, label=y.iloc[va].values)
    dte = xgb.DMatrix(X.iloc[te].values, label=y.iloc[te].values)

    booster = xgb.train(
        config.XGB_PARAMS,
        dtr,
        num_boost_round=config.NUM_ROUNDS,
        evals=[(dtr, "train"), (dva, "valid")],
        early_stopping_rounds=config.EARLY_STOP,
        verbose_eval=False,
    )
    p_te = booster.predict(dte)
    y_te = y.iloc[te].values
    # Metrics
    auc = _auc(y_te, p_te)
    acc = float(((p_te >= 0.5) == (y_te >= 0.5)).mean())
    base = float(y_te.mean())
    booster.save_model(str(out_path))
    return {
        "side": side,
        "n_train": int(tr.stop - tr.start),
        "n_valid": int(va.stop - va.start),
        "n_test": int(te.stop - te.start),
        "test_auc": auc,
        "test_acc": acc,
        "test_base_rate": base,
        "best_iteration": int(booster.best_iteration),
    }


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    order = np.argsort(p)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos = pos.sum()
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def train(symbol: str, schema: str = config.SCHEMA_15M, include_macro: bool = False,
          path_labels: bool = False) -> dict:
    df = load(symbol, schema)
    feats = build_features(df, include_macro=include_macro, symbol=symbol)
    labs = build_path_labels(symbol, schema) if path_labels else build_labels(df)

    join = feats.join(labs[["y_long", "y_short", "atr", "entry"]], how="inner").dropna()
    feat_cols = [c for c in feats.columns]
    X = join[feat_cols]

    tag = schema.replace("ohlcv-", "")
    base = config.ARTIFACTS_DIR / f"{symbol}_{tag}"
    base.mkdir(exist_ok=True)

    long_meta = _train_one(X, join["y_long"], "long", base / "long.xgb")
    short_meta = _train_one(X, join["y_short"], "short", base / "short.xgb")

    meta = {
        "symbol": symbol,
        "schema": schema,
        "include_macro": include_macro,
        "path_labels": path_labels,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(join)),
        "first_ts": str(join.index[0]),
        "last_ts": str(join.index[-1]),
        "horizon_bars": config.HORIZON_BARS,
        "tp_atr_mult": config.TP_ATR_MULT,
        "sl_atr_mult": config.SL_ATR_MULT,
        "feature_cols": feat_cols,
        "long": long_meta,
        "short": short_meta,
    }
    with open(base / "meta.json", "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(json.dumps(meta, indent=2, default=str))
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    ap.add_argument("--macro", action="store_true", help="include DFII10/VIX/DXY features")
    ap.add_argument("--path-labels", action="store_true", help="use 1m path-correct labels")
    args = ap.parse_args()
    train(args.symbol, args.schema, include_macro=args.macro, path_labels=args.path_labels)


if __name__ == "__main__":
    main()
