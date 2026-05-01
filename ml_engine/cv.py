"""Purged walk-forward cross-validation.

Splits time series into K folds. For each fold:
    train = bars before fold start, minus a purge gap of HORIZON_BARS
    test  = bars in fold

Validates AUC isn't a fluke from a single split.

Usage:
    python -m ml_engine.cv ES --schema ohlcv-1h --folds 5
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
from ml_engine.labels.path_builder import build as build_path_labels


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    order = np.argsort(p)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos = pos.sum(); n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def cv(symbol: str, schema: str, folds: int = 5, embargo: int | None = None,
       include_macro: bool = False, path_labels: bool = False) -> dict:
    df = load(symbol, schema)
    feats = build_features(df, include_macro=include_macro, symbol=symbol)
    labs = build_path_labels(symbol, schema) if path_labels else build_labels(df)
    join = feats.join(labs[["y_long", "y_short"]], how="inner").dropna()
    feat_cols = list(feats.columns)
    X = join[feat_cols].values
    y_long = join["y_long"].values
    y_short = join["y_short"].values
    n = len(join)
    embargo = embargo or config.HORIZON_BARS
    fold_size = n // (folds + 1)  # first fold is initial train

    results = []
    for k in range(folds):
        test_start = (k + 1) * fold_size
        test_end = test_start + fold_size if k < folds - 1 else n
        train_end = test_start - embargo
        if train_end < fold_size // 2:
            continue
        Xtr, Xte = X[:train_end], X[test_start:test_end]
        yL_tr, yL_te = y_long[:train_end], y_long[test_start:test_end]
        yS_tr, yS_te = y_short[:train_end], y_short[test_start:test_end]

        bL = xgb.train(config.XGB_PARAMS, xgb.DMatrix(Xtr, label=yL_tr),
                       num_boost_round=config.NUM_ROUNDS,
                       evals=[(xgb.DMatrix(Xte, label=yL_te), "test")],
                       early_stopping_rounds=config.EARLY_STOP, verbose_eval=False)
        bS = xgb.train(config.XGB_PARAMS, xgb.DMatrix(Xtr, label=yS_tr),
                       num_boost_round=config.NUM_ROUNDS,
                       evals=[(xgb.DMatrix(Xte, label=yS_te), "test")],
                       early_stopping_rounds=config.EARLY_STOP, verbose_eval=False)
        pL = bL.predict(xgb.DMatrix(Xte))
        pS = bS.predict(xgb.DMatrix(Xte))
        results.append({
            "fold": k + 1,
            "train_n": int(train_end),
            "test_n": int(test_end - test_start),
            "test_first": str(join.index[test_start]),
            "test_last": str(join.index[test_end - 1]),
            "auc_long": _auc(yL_te, pL),
            "auc_short": _auc(yS_te, pS),
            "base_long": float(yL_te.mean()),
            "base_short": float(yS_te.mean()),
        })

    aucs_l = [r["auc_long"] for r in results if not np.isnan(r["auc_long"])]
    aucs_s = [r["auc_short"] for r in results if not np.isnan(r["auc_short"])]
    summary = {
        "symbol": symbol, "schema": schema, "folds": folds, "embargo": embargo,
        "auc_long_mean":  float(np.mean(aucs_l)) if aucs_l else float("nan"),
        "auc_long_std":   float(np.std(aucs_l))  if aucs_l else float("nan"),
        "auc_short_mean": float(np.mean(aucs_s)) if aucs_s else float("nan"),
        "auc_short_std":  float(np.std(aucs_s))  if aucs_s else float("nan"),
        "fold_results": results,
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--macro", action="store_true")
    ap.add_argument("--path-labels", action="store_true")
    args = ap.parse_args()
    out = cv(args.symbol, args.schema, args.folds, include_macro=args.macro,
             path_labels=args.path_labels)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
