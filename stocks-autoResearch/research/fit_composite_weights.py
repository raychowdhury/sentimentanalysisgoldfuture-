"""
Fit composite — Tier-3 edition.

Changes vs Tier-2:
  • XGBoost classifier replaces logistic regression (captures non-linear
    interactions between trend / credit / vix_slope / regime features).
  • Expanded feature set: + vix_slope, fomc_prox, tom (turn-of-month),
    dow (day-of-week).
  • LOO ablation: leave-one-out walk-forward log loss per feature,
    reported in output so thin/useless features get pruned.
  • Model persisted to _composite_model.ubj (XGBoost native binary) so
    live code can reload and call predict_proba.

Inputs:  outputs/stocks/_backtest_composite.csv
Outputs: outputs/stocks/_composite_weights.json  (weights + metadata)
         outputs/stocks/_composite_model.ubj     (XGBoost binary)

Weights displayed on the dashboard come from `feature_importances_`
(gain) normalised to 1, for interpretability only. p_up is computed
live via predict_proba(model).

Usage:  python -m research.fit_composite_weights
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss
from xgboost import XGBClassifier

ROOT             = Path(__file__).resolve().parent.parent.parent
CSV              = ROOT / "outputs" / "stocks" / "_backtest_composite.csv"
OUT_JSON         = ROOT / "outputs" / "stocks" / "_composite_weights.json"
OUT_MODEL        = ROOT / "outputs" / "stocks" / "_composite_model.ubj"
OUT_MODEL_LOW    = ROOT / "outputs" / "stocks" / "_composite_model_lowvix.ubj"
OUT_MODEL_HIGH   = ROOT / "outputs" / "stocks" / "_composite_model_highvix.ubj"

# Tier-3 post-LOO pruning: keep only features with positive walk-forward
# contribution. Full set tested, losers dropped from the model.
# Removed: real_yield, trend, credit, fomc_prox, tom, dow (all delta_ll < 0).
# These still appear in the live composite for display but are not fitted.
COMPONENTS = [
    "model_signal", "sector_agree", "vix_slope",
]
VIX_THRESHOLD   = 18.0
WF_TRAIN_DAYS   = 250
WF_TEST_DAYS    = 21
HORIZON_COL     = "spy_fwd_5d"
FALLBACK_TARGET = "spy_next_ret"

XGB_PARAMS = dict(
    n_estimators=40,          # was 200 — huge regularization vs 800-row dataset
    max_depth=2,              # was 3 — stumps + 1 split
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_lambda=4.0,           # was 1.0 — stronger L2
    reg_alpha=0.5,            # L1 on leaf weights
    min_child_weight=5,       # avoid tiny-leaf overfit
    objective="binary:logistic",
    eval_metric="logloss",
    n_jobs=1,
    verbosity=0,
)


def _parse_components(cell: str) -> dict:
    return json.loads(cell.replace("'", '"'))


def _walk_forward(X: np.ndarray, y: np.ndarray, feature_list: list[str]) -> dict:
    """Rolling 250d train / next 21d test. Returns LL + hit rate."""
    n = len(y)
    if n < WF_TRAIN_DAYS + WF_TEST_DAYS:
        return {"n_folds": 0, "mean_ll": None, "baseline_ll": None, "note": "too few rows"}
    base_p = float(y.mean())
    base_ll = log_loss(y, np.full_like(y, base_p, dtype=float))
    lls = []
    hits = []
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
        y_te = y[tr_hi:te_hi]
        lls.append(log_loss(y_te, p, labels=[0, 1]))
        hits.append(float(((p > 0.5).astype(int) == y_te).mean()))
        start += WF_TEST_DAYS
    return {
        "n_folds":          len(lls),
        "mean_ll":          round(float(np.mean(lls)), 4) if lls else None,
        "baseline_ll":      round(base_ll, 4),
        "mean_hit":         round(float(np.mean(hits)), 4) if hits else None,
        "edge_vs_baseline": round(base_ll - float(np.mean(lls)), 4) if lls else None,
    }


def _fit_final(X: np.ndarray, y: np.ndarray) -> XGBClassifier:
    return XGBClassifier(**XGB_PARAMS).fit(X, y)


def _importances_to_weights(importances: np.ndarray) -> dict:
    if importances.sum() < 1e-6:
        arr = np.full(len(COMPONENTS), 1.0 / len(COMPONENTS))
    else:
        arr = importances / importances.sum()
    w = {COMPONENTS[i]: float(round(arr[i], 4)) for i in range(len(COMPONENTS))}
    w.setdefault("stock_sent", 0.10)
    s = sum(w.values())
    return {k: round(v / s, 4) for k, v in w.items()}


def _loo_ablation(X: np.ndarray, y: np.ndarray) -> list[dict]:
    """Leave-one-out walk-forward LL. Positive delta = removing feature HURT."""
    base_wf = _walk_forward(X, y, COMPONENTS)
    base_ll = base_wf.get("mean_ll")
    if base_ll is None:
        return []
    rows = []
    for i, feat in enumerate(COMPONENTS):
        keep = [j for j in range(len(COMPONENTS)) if j != i]
        X_minus = X[:, keep]
        wf = _walk_forward(X_minus, y, [COMPONENTS[j] for j in keep])
        ll = wf.get("mean_ll")
        if ll is None:
            continue
        rows.append({
            "feature":   feat,
            "ll_minus":  round(ll, 4),
            "delta_ll":  round(ll - base_ll, 4),  # >0 = feature was useful
        })
    rows.sort(key=lambda r: -r["delta_ll"])
    return rows


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"missing {CSV} — run composite_backtest.py first")
    df = pd.read_csv(CSV)
    comp_df = df["components"].apply(_parse_components).apply(pd.Series)
    df = pd.concat([df.drop(columns=["components"]), comp_df], axis=1)

    # Target selection
    if HORIZON_COL in df.columns and df[HORIZON_COL].notna().sum() >= 100:
        df = df.dropna(subset=[HORIZON_COL])
        target_col = HORIZON_COL
    else:
        df = df.dropna(subset=[FALLBACK_TARGET])
        target_col = FALLBACK_TARGET

    df = df.sort_values("date").reset_index(drop=True)
    df["vix"] = df["vix"].fillna(df["vix"].median())

    # Backward compatibility: if CSV lacks any new component, fill zero.
    for c in COMPONENTS:
        if c not in df.columns:
            df[c] = 0.0

    X_all = df[COMPONENTS].astype(float).to_numpy() / 100.0
    y_all = (df[target_col].astype(float) > 0).astype(int).to_numpy()

    # ── Global walk-forward + LOO on full dataset ──
    wf_global = _walk_forward(X_all, y_all, COMPONENTS)
    print(f"global walk-forward: {wf_global}")
    loo = _loo_ablation(X_all, y_all)
    print("LOO ablation (delta_ll > 0 = feature helps):")
    for r in loo:
        print(f"  {r['feature']:>14s}: delta LL {r['delta_ll']:+.4f}  (ll_minus {r['ll_minus']:.4f})")

    # ── Regime-split fits (single model each) ──
    low_mask  = df["vix"] <  VIX_THRESHOLD
    high_mask = df["vix"] >= VIX_THRESHOLD

    regimes: dict[str, dict] = {}
    regime_model_paths = {"low_vix": OUT_MODEL_LOW, "high_vix": OUT_MODEL_HIGH}
    for name, mask in [("low_vix", low_mask), ("high_vix", high_mask)]:
        X_r = X_all[mask.values]
        y_r = y_all[mask.values]
        n_r = len(y_r)
        if n_r < 60 or y_r.sum() in (0, n_r):
            final = _fit_final(X_all, y_all)
            fallback_note = f"regime n={n_r} — used global"
            model_file = OUT_MODEL.name
        else:
            final = _fit_final(X_r, y_r)
            fallback_note = None
            final.save_model(str(regime_model_paths[name]))
            model_file = regime_model_paths[name].name
        weights = _importances_to_weights(final.feature_importances_)
        wf = _walk_forward(X_r, y_r, COMPONENTS) if n_r >= WF_TRAIN_DAYS + WF_TEST_DAYS else {"n_folds": 0}
        regimes[name] = {
            "weights":        weights,
            "model_file":     model_file,
            "n_train_rows":   int(n_r),
            "up_rate":        round(float(y_r.mean()), 4) if n_r else None,
            "wf":             wf,
            "feature_gain":   {COMPONENTS[i]: float(final.feature_importances_[i])
                               for i in range(len(COMPONENTS))},
            "fallback":       fallback_note,
        }

    # ── Fallback global model: used when regime-specific file missing ──
    final_global = _fit_final(X_all, y_all)
    final_global.save_model(str(OUT_MODEL))

    payload = {
        "schema":           "xgb_v1",
        "model_file":       OUT_MODEL.name,
        "target":           target_col,
        "target_note":      "y = cumret(t+1..t+5) > 0" if target_col == HORIZON_COL else "y = ret(t+1) > 0",
        "vix_threshold":    VIX_THRESHOLD,
        "regimes":          regimes,
        "walk_forward":     wf_global,
        "loo_ablation":     loo,
        "components_used":  COMPONENTS,
        "n_total_rows":     int(len(y_all)),
        "overall_up_rate":  round(float(y_all.mean()), 4),
        "xgb_params":       XGB_PARAMS,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2))
    print(json.dumps({k: v for k, v in payload.items() if k not in ("loo_ablation",)}, indent=2))
    print(f"\nwrote {OUT_JSON}")
    print(f"wrote {OUT_MODEL}")


if __name__ == "__main__":
    main()
