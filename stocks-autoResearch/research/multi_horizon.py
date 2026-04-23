"""
Train pooled classifiers at multiple horizons (item 12).

Production model targets next-day direction (1d). Slow features (real10y_chg_5d,
sector_rel_5d, ema_gap) are wasted on a 1d horizon — better signal-to-noise on
5d / 20d windows. This script trains 5d and 20d direction classifiers on the
same feature matrix and reports holdout metrics so the trader can pick horizon.

Outputs:
  outputs/stocks/_multi_horizon_5d.pkl  + _5d_metrics.json
  outputs/stocks/_multi_horizon_20d.pkl + _20d_metrics.json

Usage:  python -m research.multi_horizon
"""
from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from data.pipeline import load_cached_frame
from agents.training_agent import _encode_categoricals, _feature_columns

ROOT = Path(__file__).resolve().parent.parent.parent
HORIZONS = (5, 20)

HOLDOUT_DAYS = 90  # larger holdout for longer-horizon noise


def _build_targets(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Add y_next_ret_h / y_next_dir_h columns for a multi-day horizon."""
    out = df.copy()
    fwd_ret = (
        out.groupby("ticker", observed=True)["close"]
        .transform(lambda s: s.shift(-horizon) / s - 1.0)
    )
    out[f"y_next_ret_{horizon}"] = fwd_ret
    out[f"y_next_dir_{horizon}"] = (fwd_ret > 0).astype("float32")
    return out


def _train_one(df: pd.DataFrame, horizon: int) -> dict:
    target_dir = f"y_next_dir_{horizon}"
    target_ret = f"y_next_ret_{horizon}"
    work = _build_targets(df, horizon).dropna(subset=[target_dir, target_ret]).copy()
    work[target_dir] = work[target_dir].astype(int)

    dates = sorted(work["date"].unique())
    cutoff = dates[-HOLDOUT_DAYS]
    train = work[work["date"] <  cutoff]
    valid = work[work["date"] >= cutoff]

    feature_cols = _feature_columns(work)  # same exclusion logic as production
    # Drop the production 1d target (it would leak through y_next_dir / y_next_ret)
    feature_cols = [c for c in feature_cols
                    if c not in ("y_next_dir", "y_next_ret",
                                 target_dir, target_ret)]

    X_tr, tmap, smap = _encode_categoricals(train, feature_cols)
    y_tr = train[target_dir].to_numpy()
    X_va, _, _ = _encode_categoricals(valid, feature_cols, tmap, smap)
    y_va = valid[target_dir].to_numpy()

    clf = xgb.XGBClassifier(
        n_estimators=400, max_depth=5, learning_rate=0.05,
        tree_method="hist", verbosity=0, n_jobs=-1,
    )
    clf.fit(X_tr, y_tr)

    p_va = clf.predict_proba(X_va)[:, 1]
    pred = (p_va >= 0.5).astype(int)
    acc  = float((pred == y_va).mean())

    # Per-ticker mean accuracy + Sharpe on horizon-aligned forward return
    valid_eval = valid[["date", "ticker", target_ret]].copy()
    valid_eval["pred"]    = pred
    valid_eval["correct"] = (valid_eval["pred"] == y_va).astype(int)
    per_ticker = valid_eval.groupby("ticker", observed=True)["correct"].mean()
    mean_acc = float(per_ticker.mean()) if len(per_ticker) else 0.0

    valid_eval["signal"] = np.where(pred == 1, 1.0, -1.0)
    valid_eval["pnl"]    = valid_eval["signal"] * valid_eval[target_ret]
    daily = valid_eval.groupby("date")["pnl"].mean()
    if not daily.empty and daily.std() > 0:
        # Annualized Sharpe scaled by horizon (returns overlap)
        sharpe = float(daily.mean() / daily.std() * np.sqrt(252 / horizon))
        eq = (1 + daily).cumprod()
        max_dd = float((eq / eq.cummax() - 1).min() * -1)
    else:
        sharpe, max_dd = 0.0, 0.0

    importance = dict(zip(feature_cols, clf.feature_importances_.tolist()))
    meta = {
        "horizon_days":   horizon,
        "trained_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_train":        int(len(train)),
        "n_valid":        int(len(valid)),
        "valid_window":   f"{str(pd.Timestamp(dates[-HOLDOUT_DAYS]).date())} → "
                          f"{str(pd.Timestamp(dates[-1]).date())}",
        "accuracy":       round(acc, 4),
        "mean_per_ticker_acc": round(mean_acc, 4),
        "sharpe":         round(sharpe, 4),
        "max_drawdown":   round(max_dd, 4),
        "pred_up_rate":   round(float(pred.mean()), 4),
        "base_rate":      round(float(y_va.mean()), 4),
        "n_features":     len(feature_cols),
        "feature_importance_top10": dict(
            sorted(importance.items(), key=lambda x: -x[1])[:10]
        ),
    }

    pkl_path  = ROOT / "outputs" / "stocks" / f"_multi_horizon_{horizon}d.pkl"
    meta_path = ROOT / "outputs" / "stocks" / f"_multi_horizon_{horizon}d_metrics.json"
    with open(pkl_path, "wb") as f:
        pickle.dump({"model": clf, "features": feature_cols,
                     "ticker_map": tmap, "sector_map": smap,
                     "horizon": horizon}, f)
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"\n── horizon {horizon}d ──")
    print(json.dumps(meta, indent=2))
    print(f"wrote {pkl_path.name}, {meta_path.name}")
    return meta


def main() -> None:
    df = load_cached_frame()
    if df is None:
        raise SystemExit("no cached feature matrix")
    out = {h: _train_one(df, h) for h in HORIZONS}

    summary = {h: {"acc": v["accuracy"], "mean_ticker_acc": v["mean_per_ticker_acc"],
                   "sharpe": v["sharpe"], "max_dd": v["max_drawdown"]}
               for h, v in out.items()}
    print("\n── horizon comparison ──")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
