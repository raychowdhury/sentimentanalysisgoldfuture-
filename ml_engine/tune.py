"""Grid-search XGBoost hyperparams using purged walk-forward CV.

Selects params that maximize mean test AUC (long+short averaged).

Usage:
    python -m ml_engine.tune ES --schema ohlcv-1h --macro
"""
import argparse
import itertools
import json

from ml_engine import config
from ml_engine.cv import cv

GRID = {
    "max_depth":   [3, 5, 7],
    "eta":         [0.03, 0.05, 0.10],
    "min_child_weight": [1, 5, 20],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--macro", action="store_true")
    args = ap.parse_args()

    keys = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    print(f"[tune] {len(combos)} combos × {args.folds} folds")

    base = dict(config.XGB_PARAMS)
    results = []
    for i, vals in enumerate(combos):
        p = dict(zip(keys, vals))
        config.XGB_PARAMS.update(p)
        s = cv(args.symbol, args.schema, args.folds, include_macro=args.macro)
        score = (s["auc_long_mean"] + s["auc_short_mean"]) / 2
        results.append({**p, "score": score,
                        "auc_long": s["auc_long_mean"],
                        "auc_short": s["auc_short_mean"]})
        print(f"[{i+1}/{len(combos)}] {p} -> auc {score:.4f}")
        config.XGB_PARAMS.update(base)

    results.sort(key=lambda r: -r["score"])
    print("\n=== TOP 5 ===")
    for r in results[:5]:
        print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
