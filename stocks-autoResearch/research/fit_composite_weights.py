"""
Fit composite component weights via logistic regression on SPY direction (item 2).

Inputs:  outputs/stocks/_backtest_composite.csv  (from composite_backtest.py)
Output:  outputs/stocks/_composite_weights.json  (consumed by predict_next_session.py)

Each row provides component values + realized SPY next-day direction.
LR predicts direction; coefficients are normalized + clipped to [0, 0.6] so no
single component dominates. If the regression has no edge (CV LL ≈ baseline),
weights default to uniform — rather than spurious tuning.

Usage:  python -m research.fit_composite_weights
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss

ROOT = Path(__file__).resolve().parent.parent.parent
CSV  = ROOT / "outputs" / "stocks" / "_backtest_composite.csv"
OUT  = ROOT / "outputs" / "stocks" / "_composite_weights.json"

COMPONENTS = ["model_signal", "breadth_signal", "sector_agree", "real_yield"]


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"missing {CSV} — run composite_backtest.py first")
    df = pd.read_csv(CSV)
    # Components stored as JSON dict in the CSV column
    comp_df = df["components"].apply(lambda s: pd.Series(json.loads(s.replace("'", '"'))))
    X = comp_df[COMPONENTS].astype(float).to_numpy() / 100.0  # rescale ±1
    y = (df["spy_next_ret"].astype(float) > 0).astype(int).to_numpy()

    if len(y) < 60 or y.sum() in (0, len(y)):
        raise SystemExit(f"insufficient data: n={len(y)}, ups={y.sum()}")

    # 5-fold time-series CV log loss vs baseline (predict mean rate)
    tscv = TimeSeriesSplit(n_splits=5)
    base_p = float(y.mean())
    base_ll  = log_loss(y, np.full_like(y, base_p, dtype=float))

    cv_lls = []
    for tr, va in tscv.split(X):
        m = LogisticRegression(C=1.0, max_iter=500).fit(X[tr], y[tr])
        p = m.predict_proba(X[va])[:, 1]
        cv_lls.append(log_loss(y[va], p, labels=[0, 1]))
    cv_ll = float(np.mean(cv_lls))

    fit = LogisticRegression(C=1.0, max_iter=500).fit(X, y)
    coefs = fit.coef_[0]
    # Convert coefs to weights: take absolute, normalize, clip
    abs_coefs = np.abs(coefs)
    if abs_coefs.sum() < 1e-6:
        weights = {k: 1.0 / len(COMPONENTS) for k in COMPONENTS}
        verdict = "uniform (degenerate fit)"
    elif cv_ll >= base_ll * 0.999:
        weights = {k: 1.0 / len(COMPONENTS) for k in COMPONENTS}
        verdict = f"uniform (CV LL {cv_ll:.4f} ≥ baseline {base_ll:.4f})"
    else:
        # Apply sign of coefficient by aligning component to its expected polarity:
        # we want each weight positive, so flip the component if coef sign disagrees
        # with the convention "positive component → bullish". The composite math
        # already matches that, so just take |coef| / sum normalized, clipped.
        normalized = abs_coefs / abs_coefs.sum()
        clipped = np.minimum(normalized, 0.60)
        clipped = clipped / clipped.sum()
        weights = {COMPONENTS[i]: float(round(clipped[i], 4)) for i in range(len(COMPONENTS))}
        verdict = f"fitted (CV LL {cv_ll:.4f} < baseline {base_ll:.4f})"

    # Always include stock_sent at a small reserved weight for live use
    weights.setdefault("stock_sent", 0.10)
    # Renormalize
    s = sum(weights.values())
    weights = {k: round(v / s, 4) for k, v in weights.items()}

    payload = {
        "verdict":       verdict,
        "weights":       weights,
        "raw_coefs":     {COMPONENTS[i]: float(coefs[i]) for i in range(len(COMPONENTS))},
        "intercept":     float(fit.intercept_[0]),
        "n_train_rows":  int(len(y)),
        "base_log_loss": round(base_ll, 4),
        "cv_log_loss":   round(cv_ll, 4),
        "components_used": COMPONENTS,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
