"""
Walk-forward backtest of the pooled XGBoost classifier (item 1).

Replaces the single 60-day holdout with a rolling expanding-window walk-forward:
- Anchor window: first 252 trading days (≈1y) for initial fit
- Step: retrain every 60 days, predict the next 60 days OOS
- Aggregate per-fold mean-acc, Sharpe, max-DD, calibration

Cheaper than full sliding window (re-fits N times), gives honest OOS metrics
that the production single-holdout overstates.

Outputs:
  outputs/stocks/_walkforward.json — per-fold + aggregate metrics
  console — fold table

Usage:  python -m research.walk_forward
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from data.pipeline import load_cached_frame
from agents.training_agent import _encode_categoricals, _feature_columns

ROOT = Path(__file__).resolve().parent.parent.parent
OUT  = ROOT / "outputs" / "stocks" / "_walkforward.json"

INITIAL_WINDOW = 252
STEP_DAYS      = 60
MIN_FOLDS      = 3


def _fold_metrics(holdout: pd.DataFrame, y_pred: np.ndarray) -> dict:
    per_ticker = {}
    h = holdout[["y_next_dir", "ticker", "y_next_ret"]].copy()
    h["pred"]    = y_pred
    h["correct"] = (h["pred"] == h["y_next_dir"]).astype(int)
    for t, g in h.groupby("ticker", observed=True):
        per_ticker[str(t)] = float(g["correct"].mean())
    mean_acc = float(np.mean(list(per_ticker.values()))) if per_ticker else 0.0

    h["signal"] = np.where(h["pred"] == 1, 1.0, -1.0)
    h["pnl"]    = h["signal"] * h["y_next_ret"]
    daily = h.groupby("y_next_ret").size()  # placeholder to keep groupby usage
    # Use date for daily aggregation
    h["date"] = holdout["date"].values
    daily = h.groupby("date")["pnl"].mean()
    sharpe = 0.0; max_dd = 0.0
    if not daily.empty and daily.std() > 0:
        sharpe = float(daily.mean() / (daily.std() + 1e-9) * np.sqrt(252))
        eq = (1 + daily).cumprod()
        max_dd = float((eq / eq.cummax() - 1).min() * -1)
    return {
        "n":           int(len(h)),
        "mean_acc":    round(mean_acc, 4),
        "sharpe":      round(sharpe, 4),
        "max_dd":      round(max_dd, 4),
        "pred_up_rate": round(float(y_pred.mean()), 4),
    }


def main() -> None:
    df = load_cached_frame()
    if df is None:
        raise SystemExit("no cached feature matrix")

    dates = sorted(df["date"].unique())
    if len(dates) < INITIAL_WINDOW + STEP_DAYS * MIN_FOLDS:
        raise SystemExit(f"need at least {INITIAL_WINDOW + STEP_DAYS * MIN_FOLDS} dates, "
                         f"have {len(dates)}")

    feature_cols = _feature_columns(df)

    folds = []
    fold_idx = 0
    cursor = INITIAL_WINDOW
    while cursor + STEP_DAYS <= len(dates):
        train_dates    = dates[:cursor]
        holdout_dates  = dates[cursor:cursor + STEP_DAYS]
        train          = df[df["date"].isin(train_dates)].copy()
        holdout        = df[df["date"].isin(holdout_dates)].copy()

        X_tr, tmap, smap = _encode_categoricals(train, feature_cols)
        y_tr = train["y_next_dir"].to_numpy()
        X_va, _, _       = _encode_categoricals(holdout, feature_cols, tmap, smap)

        clf = xgb.XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.1,
            tree_method="hist", verbosity=0, n_jobs=-1,
        )
        clf.fit(X_tr, y_tr)
        y_pred = clf.predict(X_va)

        m = _fold_metrics(holdout, y_pred)
        m.update({
            "fold":            fold_idx,
            "train_start":     str(pd.Timestamp(train_dates[0]).date()),
            "train_end":       str(pd.Timestamp(train_dates[-1]).date()),
            "holdout_start":   str(pd.Timestamp(holdout_dates[0]).date()),
            "holdout_end":     str(pd.Timestamp(holdout_dates[-1]).date()),
        })
        folds.append(m)
        print(f"fold {fold_idx:>2d} train [{m['train_start']}→{m['train_end']}] "
              f"holdout [{m['holdout_start']}→{m['holdout_end']}] "
              f"acc={m['mean_acc']:.4f} sharpe={m['sharpe']:+.2f} dd={m['max_dd']:.3f}")
        fold_idx += 1
        cursor += STEP_DAYS

    if not folds:
        raise SystemExit("no folds produced")

    agg = {
        "n_folds":         len(folds),
        "mean_acc_avg":    round(float(np.mean([f["mean_acc"] for f in folds])), 4),
        "mean_acc_median": round(float(np.median([f["mean_acc"] for f in folds])), 4),
        "mean_acc_std":    round(float(np.std([f["mean_acc"] for f in folds])), 4),
        "sharpe_avg":      round(float(np.mean([f["sharpe"] for f in folds])), 4),
        "sharpe_median":   round(float(np.median([f["sharpe"] for f in folds])), 4),
        "dd_max":          round(float(np.max([f["max_dd"] for f in folds])), 4),
        "win_rate":        round(float(np.mean([f["mean_acc"] > 0.5 for f in folds])), 4),
    }
    print("\n── walk-forward aggregate ──")
    for k, v in agg.items():
        print(f"  {k:>20s}: {v}")

    OUT.write_text(json.dumps({
        "folds":     folds,
        "aggregate": agg,
        "config": {
            "initial_window": INITIAL_WINDOW,
            "step_days":      STEP_DAYS,
        },
    }, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
