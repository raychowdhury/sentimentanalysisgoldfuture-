"""
Reliability diagram + Brier score for both:
  (a) per-ticker pooled model P(up) vs realized direction
  (b) composite SPX score (mapped to P) vs realized SPY direction

Inputs:
  data/cache/feature_matrix.pkl  (per-ticker historical features + targets)
  outputs/stocks/_backtest_composite.csv (per-day composite history)

Outputs:
  outputs/stocks/_calibration.json
  console: ASCII reliability table

A well-calibrated model: in 10 prob bins, predicted P should match observed
hit rate. We surface gaps so position sizing knows whether to trust 0.7 = 70%.

Usage:  python -m research.calibration
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from data.pipeline import load_cached_frame
from models.model_registry import registry

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "outputs" / "stocks" / "_calibration.json"


def reliability_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[dict]:
    """One row per probability bin: predicted-mean, observed-rate, count, gap."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.digitize(p, bins, right=False) - 1
    idx = np.clip(idx, 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        if not m.any():
            rows.append({
                "bin":             f"{bins[b]:.1f}–{bins[b+1]:.1f}",
                "n":               0,
                "predicted_mean":  None,
                "observed_rate":   None,
                "gap":             None,
            })
            continue
        pmean = float(p[m].mean())
        orate = float(y[m].mean())
        rows.append({
            "bin":             f"{bins[b]:.1f}–{bins[b+1]:.1f}",
            "n":               int(m.sum()),
            "predicted_mean":  round(pmean, 4),
            "observed_rate":   round(orate, 4),
            "gap":             round(orate - pmean, 4),
        })
    return rows


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(((p - y) ** 2).mean())


def expected_cal_error(rows: list[dict], n_total: int) -> float:
    if not n_total: return 0.0
    return float(sum(
        (r["n"] / n_total) * abs(r["gap"])
        for r in rows if r["n"] > 0
    ))


def main() -> None:
    out = {}

    # ── (a) per-ticker pooled P(up) vs realized ──
    df = load_cached_frame()
    meta = registry.production_metadata()
    if df is not None and meta is not None:
        model, _ = registry.load(meta.version)
        # Use last 252 trading days as out-of-window-ish sample
        last_dates = sorted(df["date"].unique())[-252:]
        sample = df[df["date"].isin(last_dates)].copy()
        sample["proba"] = model.predict_proba(sample[model.features])
        p = sample["proba"].to_numpy()
        y = sample["y_next_dir"].to_numpy()
        rows = reliability_table(p, y)
        out["pooled_per_ticker"] = {
            "n":             int(len(p)),
            "brier":         round(brier(p, y), 4),
            "ece":           round(expected_cal_error(rows, len(p)), 4),
            "base_rate":     round(float(y.mean()), 4),
            "rows":          rows,
            "model_version": meta.version,
            "sample_window": f"{str(last_dates[0])[:10]} → {str(last_dates[-1])[:10]}",
        }
        print(f"\n── pooled per-ticker calibration (n={len(p)}, Brier={brier(p,y):.4f}, "
              f"ECE={out['pooled_per_ticker']['ece']:.4f}) ──")
        print(f"{'bin':>10} {'n':>6} {'pred':>7} {'obs':>7} {'gap':>7}")
        for r in rows:
            n = r['n']; pm = r['predicted_mean']; o = r['observed_rate']; g = r['gap']
            if n == 0:
                print(f"{r['bin']:>10} {n:>6}  {'–':>6}  {'–':>6}  {'–':>6}")
            else:
                print(f"{r['bin']:>10} {n:>6} {pm:>7.4f} {o:>7.4f} {g:>+7.4f}")

    # ── (b) composite score → P (sigmoid of score/40) vs SPY direction ──
    csv = ROOT / "outputs" / "stocks" / "_backtest_composite.csv"
    if csv.exists():
        bdf = pd.read_csv(csv).dropna(subset=["spy_next_ret"])
        # Map score ∈ [-100, +100] → P via 1/(1+exp(-score/40))
        p = 1.0 / (1.0 + np.exp(-bdf["score"].to_numpy() / 40.0))
        y = (bdf["spy_next_ret"] > 0).astype(int).to_numpy()
        rows = reliability_table(p, y)
        out["composite_spy"] = {
            "n":         int(len(p)),
            "brier":     round(brier(p, y), 4),
            "ece":       round(expected_cal_error(rows, len(p)), 4),
            "base_rate": round(float(y.mean()), 4),
            "rows":      rows,
            "score_to_p_formula": "1 / (1 + exp(-score / 40))",
        }
        print(f"\n── composite → SPY calibration (n={len(p)}, Brier={brier(p,y):.4f}, "
              f"ECE={out['composite_spy']['ece']:.4f}) ──")
        print(f"{'bin':>10} {'n':>6} {'pred':>7} {'obs':>7} {'gap':>7}")
        for r in rows:
            n = r['n']; pm = r['predicted_mean']; o = r['observed_rate']; g = r['gap']
            if n == 0:
                print(f"{r['bin']:>10} {n:>6}  {'–':>6}  {'–':>6}  {'–':>6}")
            else:
                print(f"{r['bin']:>10} {n:>6} {pm:>7.4f} {o:>7.4f} {g:>+7.4f}")

    OUT.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
