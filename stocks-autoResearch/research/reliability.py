"""
Reliability diagram for the composite XGBoost model.

Reads _backtest_composite.csv + the fitted regime models, predicts p_up
on every historical row via walk-forward (rolling 250d train → 21d test
fold, then concatenate OOS predictions), bins predictions into 10
equal-width bins, reports actual hit rate + count per bin.

Output: outputs/stocks/_reliability.json
    {
      "schema": "reliability_v1",
      "generated_at": "...",
      "target": "spy_fwd_5d",
      "bins": [
        {"p_lo": 0.0, "p_hi": 0.1, "n": 12, "predicted": 0.07, "actual": 0.08},
        ...
      ],
      "brier": 0.22,
      "ece":   0.03
    }

ECE = expected calibration error (weighted mean |predicted - actual|).

Usage:  python -m research.reliability
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from research.fit_composite_weights import (
    COMPONENTS, WF_TRAIN_DAYS, WF_TEST_DAYS, XGB_PARAMS,
    HORIZON_COL, FALLBACK_TARGET,
)

ROOT = Path(__file__).resolve().parent.parent.parent
CSV  = ROOT / "outputs" / "stocks" / "_backtest_composite.csv"
OUT  = ROOT / "outputs" / "stocks" / "_reliability.json"


def _parse_components(cell: str) -> dict:
    return json.loads(cell.replace("'", '"'))


def _walk_forward_preds(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (predicted_probas, y_true) for every OOS fold row."""
    n = len(y)
    if n < WF_TRAIN_DAYS + WF_TEST_DAYS:
        return np.array([]), np.array([])
    preds, trues = [], []
    start = WF_TRAIN_DAYS
    while start + WF_TEST_DAYS <= n:
        tr_lo = start - WF_TRAIN_DAYS
        tr_hi = start
        te_hi = start + WF_TEST_DAYS
        y_tr = y[tr_lo:tr_hi]
        if y_tr.sum() in (0, len(y_tr)):
            start += WF_TEST_DAYS
            continue
        m = XGBClassifier(**XGB_PARAMS).fit(X[tr_lo:tr_hi], y_tr)
        p = m.predict_proba(X[tr_hi:te_hi])[:, 1]
        preds.append(p)
        trues.append(y[tr_hi:te_hi])
        start += WF_TEST_DAYS
    return np.concatenate(preds), np.concatenate(trues)


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"missing {CSV} — run composite_backtest.py first")
    df = pd.read_csv(CSV)
    comp_df = df["components"].apply(_parse_components).apply(pd.Series)
    df = pd.concat([df.drop(columns=["components"]), comp_df], axis=1)

    # Target
    if HORIZON_COL in df.columns and df[HORIZON_COL].notna().sum() >= 100:
        df = df.dropna(subset=[HORIZON_COL])
        target_col = HORIZON_COL
    else:
        df = df.dropna(subset=[FALLBACK_TARGET])
        target_col = FALLBACK_TARGET

    df = df.sort_values("date").reset_index(drop=True)
    for c in COMPONENTS:
        if c not in df.columns:
            df[c] = 0.0

    X = df[COMPONENTS].astype(float).to_numpy() / 100.0
    y = (df[target_col].astype(float) > 0).astype(int).to_numpy()

    preds, trues = _walk_forward_preds(X, y)
    if len(preds) == 0:
        raise SystemExit("not enough walk-forward data")

    # 10-bin equal-width reliability table.
    edges = np.linspace(0.0, 1.0, 11)
    bins = []
    ece_num, ece_den = 0.0, 0
    for i in range(10):
        lo, hi = edges[i], edges[i+1]
        if i < 9:
            mask = (preds >= lo) & (preds < hi)
        else:
            mask = (preds >= lo) & (preds <= hi)
        n = int(mask.sum())
        if n == 0:
            bins.append({"p_lo": round(float(lo), 2), "p_hi": round(float(hi), 2),
                         "n": 0, "predicted": None, "actual": None})
            continue
        pred_mean = float(preds[mask].mean())
        act_mean  = float(trues[mask].mean())
        bins.append({
            "p_lo":      round(float(lo), 2),
            "p_hi":      round(float(hi), 2),
            "n":         n,
            "predicted": round(pred_mean, 4),
            "actual":    round(act_mean, 4),
        })
        ece_num += n * abs(pred_mean - act_mean)
        ece_den += n

    brier = float(np.mean((preds - trues) ** 2))
    ece   = (ece_num / ece_den) if ece_den else None

    payload = {
        "schema":       "reliability_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target":       target_col,
        "n_oos":        int(len(preds)),
        "brier":        round(brier, 4),
        "ece":          round(ece, 4) if ece is not None else None,
        "bins":         bins,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
