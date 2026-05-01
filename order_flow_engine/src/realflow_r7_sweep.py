"""
Phase 2B Stage 1 — diagnostic R7 threshold sweep.

Tests whether `RULE_CVD_CORR_THRESH` can be calibrated for real-flow mode.
1-D sweep over candidate `RULE_CVD_CORR_THRESH_REAL` values; window held
fixed at the production value.

Reproduces R7 firing locally — does NOT call rule_engine.apply_rules with a
custom threshold. Production thresholds in config remain unchanged.

Walk-forward 70/30 split (matches Phase 2A methodology).

Reads:
  joined real-flow frame via realflow_compare._load_pair

Writes:
  outputs/order_flow/realflow_r7_sweep_<sym>_<tf>.json
  outputs/order_flow/realflow_r7_sweep_<sym>_<tf>.md

Untouched: rule_engine, config, predictor, backtester, ingest, alert_engine,
feature_engineering, label_generator, model_trainer, ml_engine.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    config as of_cfg,
    realflow_compare as rfc,
)


# Sweep grid — Phase 2B plan.
THRESH_GRID = [-0.20, -0.25, -0.30, -0.35, -0.40, -0.45, -0.50, -0.55, -0.60]

TRAIN_FRAC = 0.70
DIRECTION_EPS = 1e-9   # below this magnitude, skip the fire (no clear bias)


# ── R7 firing under candidate threshold (local; no detector edit) ───────────

def _build_frame(symbol: str, tf: str) -> pd.DataFrame:
    """
    Joined real-flow frame with cvd_z, Close, atr, plus rolling cvd-return
    correlation under the production window, plus forward R.
    """
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)
    df = real_feat.copy()

    w = of_cfg.RULE_CVD_CORR_WINDOW
    ret = df["Close"].pct_change()
    df["cvd_corr"]  = df["cvd_z"].rolling(w, min_periods=w).corr(ret)
    df["cvd_slope"] = df["cvd_z"].diff(w)
    df["r7_dir"]    = np.sign(df["cvd_slope"].fillna(0.0)).astype(int)

    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    atr_safe = df["atr"].replace(0, np.nan)
    df["fwd_r"] = ((df["Close"].shift(-horizon) - df["Close"]) / atr_safe).fillna(0.0)
    return df


def _r7_stats(df: pd.DataFrame, thresh: float) -> dict:
    """
    Apply candidate threshold, count fires, compute hit rate + mean signed R.
    Direction = sign(cvd_z.diff(window)). When direction == 0 (slope ≈ 0),
    skip the fire to avoid contaminating mean_r with zeros.
    """
    fires_mask = (df["cvd_corr"] < thresh).fillna(False)
    valid = fires_mask & (df["cvd_slope"].abs() > DIRECTION_EPS)
    n = int(valid.sum())
    if n == 0:
        return {
            "n_fires": 0,
            "hit_rate": None,
            "mean_r":  None,
            "score":   0.0,
        }
    direction = df.loc[valid, "r7_dir"].to_numpy()
    fwd_r     = df.loc[valid, "fwd_r"].to_numpy()
    signed = fwd_r * direction
    mean_r = float(signed.mean())
    return {
        "n_fires":  n,
        "hit_rate": round(float((signed > 0).mean()), 4),
        "mean_r":   round(mean_r, 4),
        "score":    round(mean_r * (n ** 0.5), 4),
    }


def _evaluate_cell(train: pd.DataFrame, test: pd.DataFrame, thresh: float) -> dict:
    tr = _r7_stats(train, thresh)
    te = _r7_stats(test,  thresh)
    ratio = (round(te["score"] / tr["score"], 4)
             if tr["score"] not in (0.0, None) else None)
    return {
        "threshold":   thresh,
        "train":       tr,
        "test":        te,
        "score_ratio": ratio,
    }


# ── orchestrator ────────────────────────────────────────────────────────────

def run(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _build_frame(symbol, tf)
    n = len(df)
    cut = int(n * TRAIN_FRAC)
    train = df.iloc[:cut]
    test  = df.iloc[cut:]

    cells: list[dict] = []
    for thr in THRESH_GRID:
        cells.append(_evaluate_cell(train, test, thr))

    # Production baseline at current threshold.
    prod = _evaluate_cell(train, test, of_cfg.RULE_CVD_CORR_THRESH)

    # Rank by train score; require score_ratio >= 0.5 AND test n_fires >= 10
    # AND test mean_r >= 0 to qualify (Phase 2B acceptance criteria).
    qualified = []
    for c in cells:
        sr = c["score_ratio"]
        if sr is None or sr < 0.5:
            continue
        if c["test"]["n_fires"] < 10:
            continue
        mr = c["test"]["mean_r"]
        if mr is None or mr < 0:
            continue
        qualified.append(c)
    qualified_sorted = sorted(qualified,
                              key=lambda c: c["train"]["score"], reverse=True)
    headline = qualified_sorted[0] if qualified_sorted else None

    notes: list[str] = []
    if headline is None:
        notes.append("No cell meets Phase 2B acceptance criteria "
                     "(test fires >= 10 AND test mean_r >= 0 AND ratio >= 0.5).")
        notes.append("Fallback recommendation: disable R7 on real-flow path "
                     "by setting RULE_CVD_CORR_THRESH_REAL = -1.0 (never fires) "
                     "until more bars accumulate.")
    if min(c["test"]["n_fires"] for c in cells) < 10:
        notes.append("Some cells produced fewer than 10 R7 fires on TEST — "
                     "sample size is the limiting factor.")
    if all(c["train"]["n_fires"] < 10 for c in cells):
        notes.append("ALL cells produced fewer than 10 R7 fires on TRAIN — "
                     "R7 may be structurally weak on real-flow at this "
                     "horizon. Consider disabling.")

    report = {
        "symbol":     symbol,
        "timeframe":  tf,
        "joined": {
            "n_total": int(n),
            "n_train": int(len(train)),
            "n_test":  int(len(test)),
            "train_window": [str(train.index.min()), str(train.index.max())],
            "test_window":  [str(test.index.min()),  str(test.index.max())],
        },
        "production_threshold": of_cfg.RULE_CVD_CORR_THRESH,
        "production_window":    of_cfg.RULE_CVD_CORR_WINDOW,
        "production_baseline":  prod,
        "grid":                 THRESH_GRID,
        "cells":                cells,
        "qualified_count":      len(qualified),
        "headline":             headline,
        "promotion_rule":       ("a candidate qualifies if test_score / "
                                 "train_score >= 0.5 AND test n_fires >= 10 "
                                 "AND test mean_r >= 0"),
        "notes":                notes,
    }

    json_path = out_dir / f"realflow_r7_sweep_{symbol}_{tf}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path = out_dir / f"realflow_r7_sweep_{symbol}_{tf}.md"
    md_path.write_text(_render_md(report))

    report["_json_path"] = str(json_path)
    report["_md_path"]   = str(md_path)
    return report


def _render_md(r: dict) -> str:
    L: list[str] = []
    L.append(f"# Real-Flow R7 Threshold Sweep — {r['symbol']} @ {r['timeframe']}\n")
    j = r["joined"]
    L.append(f"- bars: total={j['n_total']} · train={j['n_train']} · test={j['n_test']}")
    L.append(f"- train: `{j['train_window'][0]}` → `{j['train_window'][1]}`")
    L.append(f"- test:  `{j['test_window'][0]}` → `{j['test_window'][1]}`")
    L.append(f"- production threshold: {r['production_threshold']}")
    L.append(f"- production window: {r['production_window']} bars\n")

    p = r["production_baseline"]
    L.append("## Production baseline at current threshold\n")
    L.append(f"- TRAIN: n_fires={p['train']['n_fires']} · "
             f"mean_r={p['train']['mean_r']} · "
             f"hit_rate={p['train']['hit_rate']} · "
             f"score={p['train']['score']}")
    L.append(f"- TEST:  n_fires={p['test']['n_fires']} · "
             f"mean_r={p['test']['mean_r']} · "
             f"hit_rate={p['test']['hit_rate']} · "
             f"score={p['test']['score']}")
    L.append(f"- score_ratio: {p['score_ratio']}\n")

    L.append("## All candidate cells (1-D sweep over RULE_CVD_CORR_THRESH_REAL)\n")
    L.append("| threshold | train n / R / hit / score | test n / R / hit / score | ratio |")
    L.append("|-----------|---------------------------|--------------------------|-------|")
    for c in r["cells"]:
        tr = c["train"]
        te = c["test"]
        L.append(
            f"| {c['threshold']} | "
            f"{tr['n_fires']} / {tr['mean_r']} / {tr['hit_rate']} / {tr['score']} | "
            f"{te['n_fires']} / {te['mean_r']} / {te['hit_rate']} / {te['score']} | "
            f"{c['score_ratio']} |"
        )
    L.append("")

    L.append(f"## Headline (qualified count: {r['qualified_count']})\n")
    if r["headline"] is None:
        L.append("**No cell met Phase 2B acceptance criteria.**\n")
    else:
        h = r["headline"]
        L.append(f"- **threshold = {h['threshold']}**")
        L.append(f"- train: n={h['train']['n_fires']} mean_r={h['train']['mean_r']} "
                 f"hit={h['train']['hit_rate']}")
        L.append(f"- test:  n={h['test']['n_fires']} mean_r={h['test']['mean_r']} "
                 f"hit={h['test']['hit_rate']}")
        L.append(f"- score_ratio: {h['score_ratio']}\n")
    L.append(f"_{r['promotion_rule']}_\n")

    if r["notes"]:
        L.append("## Notes\n")
        for n in r["notes"]:
            L.append(f"- {n}")
        L.append("")

    L.append("**Sweep is diagnostic only.** No production threshold has been "
             "modified. Decide whether to promote (Stage 2) or disable R7 "
             "on real-flow path after reviewing the table above.")
    return "\n".join(L) + "\n"


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 2B Stage 1 R7 sweep.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = run(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
