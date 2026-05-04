"""
Phase 2A — diagnostic threshold sweep (no detector edits).

Walks a 5×4×4=80-cell grid of candidate real-flow thresholds for R1/R2,
R3/R4, R5/R6 over the joined ESM6@15m window, splits 70/30 train/test,
and ranks cells by composite expectancy score.

Does NOT call rule_engine.apply_rules with custom thresholds — the
production thresholds in `RULE_DELTA_DOMINANCE` etc. are not modified.
Rule firing is reproduced locally in this script using the candidate
thresholds, against the joined frame from realflow_compare._load_pair.

Reads:
  outputs/order_flow/realflow_diagnostic_<sym>_<tf>.json (not required)
  joined frame via realflow_compare._load_pair

Writes:
  outputs/order_flow/realflow_threshold_sweep_<sym>_<tf>.json
  outputs/order_flow/realflow_threshold_sweep_<sym>_<tf>.md

No edits to:
  rule_engine, config, predictor, backtester, ingest, alert_engine,
  feature_engineering, label_generator, model_trainer, ml_engine.
"""

from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    config as of_cfg,
    realflow_compare as rfc,
    rule_engine,
)


# Per-rule fade direction (matches rule_engine.reversal_direction logic).
RULE_DIR: dict[str, int] = {
    "r1_buyer_down":            -1,
    "r2_seller_up":             +1,
    "r3_absorption_resistance": -1,
    "r4_absorption_support":    +1,
    "r5_bull_trap":             -1,
    "r6_bear_trap":             +1,
}

# Sweep grid (Phase 2A — finer lower-dominance grid).
DOMINANCE_GRID  = [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]
ABSORPTION_GRID = [0.15, 0.18, 0.20, 0.22, 0.25]
TRAP_GRID       = [0.08, 0.10, 0.12, 0.14, 0.16]

TRAIN_FRAC = 0.70


# ── rule firing under candidate thresholds (local; no detector edit) ────────

def _apply_thresholds(
    df: pd.DataFrame,
    dom_thr:    float,
    abs_thr:    float,
    trap_thr:   float,
    sr_atr_mult:    float = None,
    abs_ret_cap:    float = None,
) -> pd.DataFrame:
    """
    Reproduce the boolean masks for r1..r6 with the supplied thresholds.

    R1 = (dr >  dom_thr) & (fwd_atr < -0.3)
    R2 = (dr < -dom_thr) & (fwd_atr >  0.3)
    R3 = near_high & (dr >  abs_thr) & |fwd_atr| < abs_ret_cap
    R4 = near_low  & (dr < -abs_thr) & |fwd_atr| < abs_ret_cap
    R5 = (High > recent_high) & (Close < recent_high) & (dr >  trap_thr)
    R6 = (Low  < recent_low)  & (Close > recent_low)  & (dr < -trap_thr)

    abs_ret_cap, sr_atr_mult default to current production values from
    of_cfg so they don't drift inside the sweep.
    """
    sr_atr_mult = sr_atr_mult if sr_atr_mult is not None else of_cfg.RULE_SR_ATR_MULT
    abs_ret_cap = abs_ret_cap if abs_ret_cap is not None else of_cfg.RULE_ABSORPTION_RET_CAP_ATR_PCT

    dr = df["delta_ratio"]
    atr_frac = (df["atr_pct"] / 100).replace(0, np.nan)
    fwd_atr = (df["fwd_ret_1"] / atr_frac).fillna(0.0)

    near_high = df["dist_to_recent_high_atr"] < sr_atr_mult
    near_low  = df["dist_to_recent_low_atr"]  < sr_atr_mult
    small_move = fwd_atr.abs() < abs_ret_cap

    out = pd.DataFrame(index=df.index)
    out["r1_buyer_down"] = (dr >  dom_thr) & (fwd_atr < -0.3)
    out["r2_seller_up"]  = (dr < -dom_thr) & (fwd_atr >  0.3)
    out["r3_absorption_resistance"] = near_high & (dr >  abs_thr) & small_move
    out["r4_absorption_support"]    = near_low  & (dr < -abs_thr) & small_move
    out["r5_bull_trap"] = (
        (df["High"]  > df["recent_high"]) &
        (df["Close"] < df["recent_high"]) &
        (dr >  trap_thr)
    ).fillna(False)
    out["r6_bear_trap"] = (
        (df["Low"]   < df["recent_low"]) &
        (df["Close"] > df["recent_low"]) &
        (dr < -trap_thr)
    ).fillna(False)
    for c in out.columns:
        out[c] = out[c].fillna(False).astype(bool)
    return out


# ── scoring ─────────────────────────────────────────────────────────────────

def _per_rule_stats(rules_df: pd.DataFrame, fwd_r: pd.Series) -> dict:
    out: dict[str, dict] = {}
    for rule, direction in RULE_DIR.items():
        mask = rules_df[rule]
        n = int(mask.sum())
        if n == 0:
            out[rule] = {"count": 0, "mean_r": None, "hit_rate": None}
            continue
        signed = fwd_r[mask].to_numpy() * direction
        out[rule] = {
            "count":    n,
            "mean_r":   round(float(signed.mean()), 4),
            "hit_rate": round(float((signed > 0).mean()), 4),
        }
    return out


def _composite_score(stats: dict) -> float:
    """Score = Σ_rule (mean_r × √n_fires). Phase 1B convention."""
    score = 0.0
    for rule, s in stats.items():
        n = s["count"]
        mr = s["mean_r"]
        if n > 0 and mr is not None:
            score += mr * (n ** 0.5)
    return round(float(score), 4)


def _evaluate_cell(
    df: pd.DataFrame, fwd_r: pd.Series,
    dom: float, ab: float, tr: float,
) -> dict:
    rules = _apply_thresholds(df, dom, ab, tr)
    stats = _per_rule_stats(rules, fwd_r)
    return {
        "thresholds": {"dominance": dom, "absorption": ab, "trap": tr},
        "stats":      stats,
        "score":      _composite_score(stats),
        "n_bars":     int(len(df)),
    }


# ── orchestrator ────────────────────────────────────────────────────────────

def _build_frame(symbol: str, tf: str) -> pd.DataFrame:
    """
    Joined real-flow frame with delta_ratio, atr/atr_pct/fwd_ret_1, S/R cols,
    plus forward R (signed by horizon).
    """
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)
    df = real_feat.copy()
    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    atr_safe = df["atr"].replace(0, np.nan)
    df["fwd_r"] = ((df["Close"].shift(-horizon) - df["Close"]) / atr_safe).fillna(0.0)
    return df


def run(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _build_frame(symbol, tf)
    n = len(df)
    cut = int(n * TRAIN_FRAC)
    train = df.iloc[:cut]
    test  = df.iloc[cut:]
    fwd_r_train = train["fwd_r"]
    fwd_r_test  = test["fwd_r"]

    # Sweep all cells on TRAIN first.
    cells: list[dict] = []
    for dom, ab, tr in product(DOMINANCE_GRID, ABSORPTION_GRID, TRAP_GRID):
        train_eval = _evaluate_cell(train, fwd_r_train, dom, ab, tr)
        cells.append(train_eval)

    # Rank by train score, then evaluate top-10 on TEST for stability.
    cells_sorted = sorted(cells, key=lambda c: c["score"], reverse=True)
    top_10 = cells_sorted[:10]
    enriched: list[dict] = []
    for c in top_10:
        thr = c["thresholds"]
        test_eval = _evaluate_cell(test, fwd_r_test,
                                   thr["dominance"], thr["absorption"], thr["trap"])
        c2 = {
            "thresholds":   thr,
            "train_score":  c["score"],
            "test_score":   test_eval["score"],
            "score_ratio":  (round(test_eval["score"] / c["score"], 4)
                             if c["score"] not in (0.0,) else None),
            "train_stats":  c["stats"],
            "test_stats":   test_eval["stats"],
        }
        enriched.append(c2)

    # Headline: top by combined train+test score where test/train ratio >= 0.5.
    qualified = [c for c in enriched
                 if c["score_ratio"] is not None and c["score_ratio"] >= 0.5]
    headline = qualified[0] if qualified else None

    # Reference current-prod thresholds on the same data.
    prod_train = _evaluate_cell(train, fwd_r_train,
                                of_cfg.RULE_DELTA_DOMINANCE,
                                of_cfg.RULE_ABSORPTION_DELTA,
                                of_cfg.RULE_TRAP_DELTA)
    prod_test  = _evaluate_cell(test,  fwd_r_test,
                                of_cfg.RULE_DELTA_DOMINANCE,
                                of_cfg.RULE_ABSORPTION_DELTA,
                                of_cfg.RULE_TRAP_DELTA)

    report = {
        "symbol":     symbol,
        "timeframe":  tf,
        "joined": {
            "n_total":  int(n),
            "n_train":  int(len(train)),
            "n_test":   int(len(test)),
            "train_window": [str(train.index.min()), str(train.index.max())],
            "test_window":  [str(test.index.min()),  str(test.index.max())],
        },
        "horizon_bars":     of_cfg.OF_FORWARD_BARS.get(tf, 1),
        "production_thresholds": {
            "dominance":  of_cfg.RULE_DELTA_DOMINANCE,
            "absorption": of_cfg.RULE_ABSORPTION_DELTA,
            "trap":       of_cfg.RULE_TRAP_DELTA,
        },
        "production_baseline": {
            "train": prod_train,
            "test":  prod_test,
        },
        "grid": {
            "dominance":  DOMINANCE_GRID,
            "absorption": ABSORPTION_GRID,
            "trap":       TRAP_GRID,
            "n_cells":    len(cells),
        },
        "top_10":          enriched,
        "headline":        headline,
        "qualified_count": len(qualified),
        "promotion_rule":  ("a candidate qualifies if test_score / train_score >= 0.5; "
                            "headline = highest train_score among qualified cells"),
    }

    json_path = out_dir / f"realflow_threshold_sweep_{symbol}_{tf}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path = out_dir / f"realflow_threshold_sweep_{symbol}_{tf}.md"
    md_path.write_text(_render_md(report))

    report["_json_path"] = str(json_path)
    report["_md_path"]   = str(md_path)
    return report


def _fmt_stats(stats: dict, rules: tuple = ("r1_buyer_down", "r2_seller_up")) -> str:
    parts = []
    for r in rules:
        s = stats.get(r, {})
        n = s.get("count", 0)
        mr = s.get("mean_r")
        hr = s.get("hit_rate")
        if n == 0:
            parts.append(f"{r[:6]}: 0/—/—")
        else:
            parts.append(f"{r[:6]}: {n}/{mr}/{hr}")
    return " · ".join(parts)


def _render_md(r: dict) -> str:
    L: list[str] = []
    L.append(f"# Real-Flow Threshold Sweep — {r['symbol']} @ {r['timeframe']}\n")
    j = r["joined"]
    L.append(f"- bars: total={j['n_total']} · train={j['n_train']} · test={j['n_test']}")
    L.append(f"- train: `{j['train_window'][0]}` → `{j['train_window'][1]}`")
    L.append(f"- test:  `{j['test_window'][0]}` → `{j['test_window'][1]}`")
    L.append(f"- horizon: {r['horizon_bars']} bars")
    L.append(f"- prod thresholds: dom={r['production_thresholds']['dominance']} · "
             f"abs={r['production_thresholds']['absorption']} · "
             f"trap={r['production_thresholds']['trap']}")
    L.append(f"- grid cells: {r['grid']['n_cells']}\n")

    L.append("## Production baseline at current thresholds\n")
    L.append(f"- TRAIN score: {r['production_baseline']['train']['score']}")
    L.append(f"- TEST  score: {r['production_baseline']['test']['score']}")
    L.append(f"- TRAIN per-rule (R1/R2): "
             f"{_fmt_stats(r['production_baseline']['train']['stats'])}")
    L.append(f"- TEST  per-rule (R1/R2): "
             f"{_fmt_stats(r['production_baseline']['test']['stats'])}")
    L.append("")

    L.append("## Top 10 candidate cells (ranked by TRAIN score)\n")
    L.append("| rank | dom | abs | trap | train_score | test_score | ratio | "
             "train R1/R2 | test R1/R2 |")
    L.append("|------|-----|-----|------|-------------|------------|-------|"
             "-------------|------------|")
    for i, c in enumerate(r["top_10"], 1):
        thr = c["thresholds"]
        L.append(
            f"| {i} | {thr['dominance']} | {thr['absorption']} | {thr['trap']} | "
            f"{c['train_score']} | {c['test_score']} | {c['score_ratio']} | "
            f"{_fmt_stats(c['train_stats'])} | {_fmt_stats(c['test_stats'])} |"
        )
    L.append("")

    L.append(f"## Headline (qualified count: {r['qualified_count']})\n")
    if r["headline"] is None:
        L.append("**No cell met the test/train ≥ 0.5 stability bar.**")
        L.append("\nDo NOT promote. Either:")
        L.append("- accumulate more bars (current train n=" +
                 str(r['joined']['n_train']) + " → expectancy CIs too wide), or")
        L.append("- relax the stability rule, or")
        L.append("- reconsider whether per-rule expectancy is the right objective.")
    else:
        h = r["headline"]
        L.append(f"- **dominance = {h['thresholds']['dominance']}**")
        L.append(f"- **absorption = {h['thresholds']['absorption']}**")
        L.append(f"- **trap = {h['thresholds']['trap']}**")
        L.append(f"- train_score = {h['train_score']} · test_score = "
                 f"{h['test_score']} · ratio = {h['score_ratio']}")
        L.append(f"- TRAIN per-rule: {_fmt_stats(h['train_stats'])}")
        L.append(f"- TEST  per-rule: {_fmt_stats(h['test_stats'])}")
    L.append(f"\n_{r['promotion_rule']}_")
    L.append("\n**Sweep is diagnostic only.** No production threshold has been "
             "modified. Decide whether to promote after reviewing the table above.")
    return "\n".join(L) + "\n"


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 2A diagnostic threshold sweep.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = run(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
