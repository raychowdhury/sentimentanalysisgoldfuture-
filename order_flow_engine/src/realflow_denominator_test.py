"""
Phase 1H — denominator switch test (diagnostic only).

Compares three `delta_ratio` formulas on the same joined window:

  proxy   : CLV-based (current detector path when no real flow)
  real_v1 : (buy_real − sell_real) / cache_Volume      (current real-flow path)
  real_v2 : (buy_real − sell_real) / (buy_real + sell_real)   (proposed)

For each formula:
  - distribution of delta_ratio
  - R1/R2/R3/R4 fire counts at threshold sweep
  - sign agreement vs other formulas
  - per-rule mean forward R (signed by rule direction)
  - boundary-hour fires (RTH open 13Z, RTH close 20Z) — Phase 1G hot-spots

R5/R6 are price-pattern only — unaffected. R7 uses cvd_z, not delta_ratio —
unaffected at the rule-firing level (note: cvd_z is held constant across
formulas in this quick test). cvd_z recomputation under v2 is left to a
follow-up if needed.

Reads:
  joined frame from realflow_compare._load_pair  (uses live + history merge)

Writes:
  outputs/order_flow/realflow_denominator_test_<sym>_<tf>.json
  outputs/order_flow/realflow_denominator_test_<sym>_<tf>.md

No production code paths touched. No edits to rules, thresholds, features,
labels, models, ml_engine, ingest, predictor.
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
    rule_engine,
)

# Per-rule fade direction for signed forward R.
RULE_DIR: dict[str, int] = {
    "r1_buyer_down":            -1,  # R1 = buyer dominance → fade short
    "r2_seller_up":             +1,  # R2 = seller dominance → fade long
    "r3_absorption_resistance": -1,
    "r4_absorption_support":    +1,
    "r5_bull_trap":             -1,
    "r6_bear_trap":             +1,
}

THRESHOLD_GRID = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
BOUNDARY_HOURS_UTC = (13, 20)


# ── builders ────────────────────────────────────────────────────────────────

def _build_three_frames(symbol: str, tf: str) -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, dict
]:
    """
    Returns (frame_proxy, frame_v1, frame_v2, meta).

    frame_proxy comes from the proxy pipeline (CLV-derived buy/sell).
    frame_v1 / frame_v2 are copies of the real-flow pipeline with
    delta_ratio rewritten under each denominator. Then apply_rules is
    re-run on each so r1..r7 booleans reflect the swapped delta_ratio.
    """
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)

    # frame_proxy already has rules applied via _build_pipeline → apply_rules.
    # frame_v1 same — but we rebuild rules anyway from a clean copy so
    # comparison is symmetric.
    feat_v1 = real_feat.copy()
    # delta_ratio in real_feat already = (buy_real − sell_real) / cache_Volume.
    # Rebuild rule columns from this delta_ratio.
    feat_v1 = _reapply_rules(feat_v1)

    # v2: (buy − sell) / (buy + sell). Use buy_vol/sell_vol that
    # add_orderflow_proxies already populated from real columns.
    feat_v2 = real_feat.copy()
    buy  = pd.to_numeric(feat_v2["buy_vol"],  errors="coerce")
    sell = pd.to_numeric(feat_v2["sell_vol"], errors="coerce")
    total = buy + sell
    feat_v2["delta_ratio"] = np.where(total > 0, (buy - sell) / total, 0.0)
    feat_v2 = _reapply_rules(feat_v2)

    # frame_proxy already has rules applied, but to keep the pipeline
    # identical we drop existing rule columns and re-apply.
    feat_proxy = proxy_feat.copy()
    feat_proxy = _reapply_rules(feat_proxy)

    meta = {
        "joined_n_bars":   int(len(common)),
        "joined_start":    str(common.min()),
        "joined_end":      str(common.max()),
    }
    return feat_proxy, feat_v1, feat_v2, meta


def _reapply_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Drop existing rule cols then re-run apply_rules on the frame."""
    cols_to_drop = [c for c in rule_engine.ALL_RULE_COLS if c in df.columns]
    cols_to_drop += [c for c in ("rule_hit_count", "rule_hit_count_causal",
                                 "rule_hit_codes", "reversal_direction",
                                 "cvd_price_corr") if c in df.columns]
    df = df.drop(columns=cols_to_drop, errors="ignore")
    return rule_engine.apply_rules(df)


# ── analyses ────────────────────────────────────────────────────────────────

def _stats(s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {k: None for k in
                ("mean", "std", "min", "p25", "p50", "p75", "max")}
    return {
        "mean": round(float(s.mean()), 6),
        "std":  round(float(s.std()), 6),
        "min":  round(float(s.min()), 6),
        "p25":  round(float(s.quantile(0.25)), 6),
        "p50":  round(float(s.quantile(0.50)), 6),
        "p75":  round(float(s.quantile(0.75)), 6),
        "max":  round(float(s.max()), 6),
    }


def _frac_above(s: pd.Series, t: float) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return round(float((s.abs() >= t).mean()), 4)


def _distribution_block(frames: dict[str, pd.DataFrame]) -> dict:
    out: dict[str, dict] = {}
    for name, df in frames.items():
        dr = df["delta_ratio"]
        out[name] = {
            "stats": _stats(dr),
            "frac_abs_ge": {
                f"{t:.2f}": _frac_above(dr, t) for t in THRESHOLD_GRID
            },
        }
    return out


def _threshold_sensitivity(frames: dict[str, pd.DataFrame]) -> dict:
    """R1/R2/R3/R4 fire counts at each threshold."""
    out: dict[str, dict] = {}
    for name, df in frames.items():
        dr = df["delta_ratio"]
        atr_frac = (df["atr_pct"] / 100).replace(0, np.nan)
        fwd_atr = (df["fwd_ret_1"] / atr_frac).fillna(0.0)
        per: dict[str, dict] = {}
        for t in THRESHOLD_GRID:
            r1 = ((dr >  t) & (fwd_atr < -0.3)).fillna(False).sum()
            r2 = ((dr < -t) & (fwd_atr >  0.3)).fillna(False).sum()
            # R3/R4 also use a small forward move + near S/R; here we just
            # count the dominance condition for clarity. Full R3/R4 stay as
            # actual rule_engine output below.
            per[f"{t:.2f}"] = {"r1_buyer_down": int(r1), "r2_seller_up": int(r2)}
        out[name] = per
    return out


def _sign_agreement(frames: dict[str, pd.DataFrame]) -> dict:
    s_proxy = np.sign(frames["proxy"]["delta_ratio"].fillna(0).to_numpy())
    s_v1    = np.sign(frames["real_v1"]["delta_ratio"].fillna(0).to_numpy())
    s_v2    = np.sign(frames["real_v2"]["delta_ratio"].fillna(0).to_numpy())
    pair = lambda a, b: round(float((a == b).mean()), 4) if len(a) else None
    three = round(float(((s_proxy == s_v1) & (s_v1 == s_v2)).mean()), 4)
    return {
        "proxy_vs_v1":  pair(s_proxy, s_v1),
        "proxy_vs_v2":  pair(s_proxy, s_v2),
        "v1_vs_v2":     pair(s_v1, s_v2),
        "three_way":    three,
    }


def _per_rule_expectancy(frame: pd.DataFrame, tf: str) -> dict:
    """Mean signed forward R per rule using fixed direction map."""
    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    atr_safe = frame["atr"].replace(0, np.nan)
    fwd = frame["Close"].shift(-horizon) - frame["Close"]
    fwd_r = (fwd / atr_safe).fillna(0.0)

    out: dict[str, dict] = {}
    for rule, direction in RULE_DIR.items():
        if rule not in frame.columns:
            out[rule] = {"count": 0, "mean_r": None}
            continue
        mask = frame[rule].fillna(False).astype(bool)
        n = int(mask.sum())
        if n == 0:
            out[rule] = {"count": 0, "mean_r": None}
            continue
        signed = fwd_r[mask].to_numpy() * direction
        out[rule] = {
            "count":    n,
            "mean_r":   round(float(signed.mean()), 4),
            "hit_rate": round(float((signed > 0).mean()), 4),
        }
    return out


def _expectancy_block(frames: dict[str, pd.DataFrame], tf: str) -> dict:
    return {name: _per_rule_expectancy(df, tf) for name, df in frames.items()}


def _boundary_hours(frames: dict[str, pd.DataFrame]) -> dict:
    """Rule fires at RTH open (13Z) and close (20Z) — Phase 1G hot-spots."""
    out: dict[str, dict] = {}
    for name, df in frames.items():
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        hours = idx.hour
        for h in BOUNDARY_HOURS_UTC:
            mask = hours == h
            sub = df[mask]
            key = f"{name}@H{h:02d}Z"
            out[key] = {
                "n_bars": int(mask.sum()),
                "fires": {r: int(sub[r].fillna(False).sum())
                          for r in rule_engine.ALL_RULE_COLS
                          if r in sub.columns},
            }
    return out


def _recommendation(distribution: dict, expectancy: dict) -> dict:
    """
    Suggest a v2 RULE_DELTA_DOMINANCE that matches the proxy fire-rate.
    Heuristic: pick the smallest threshold where v2's |dr|≥t fraction
    is within ±20% of the proxy's fraction at the current default 0.30.
    """
    proxy_frac_at_default = distribution["proxy"]["frac_abs_ge"].get(
        f"{of_cfg.RULE_DELTA_DOMINANCE:.2f}", None
    )
    suggestion = None
    rationale  = "no proxy reference fraction"
    if proxy_frac_at_default is not None and proxy_frac_at_default > 0:
        target_low  = 0.8 * proxy_frac_at_default
        target_high = 1.2 * proxy_frac_at_default
        for t_str, frac in distribution["real_v2"]["frac_abs_ge"].items():
            if target_low <= frac <= target_high:
                suggestion = float(t_str)
                rationale = (f"v2 |dr|≥{t_str} → {frac} bars · "
                             f"matches proxy@{of_cfg.RULE_DELTA_DOMINANCE} = "
                             f"{proxy_frac_at_default}")
                break
        if suggestion is None:
            rationale = (f"no v2 threshold matches proxy fire rate "
                         f"({proxy_frac_at_default}) within ±20%")

    # Compare per-rule expectancy: v2 vs v1 on R1/R2.
    summary = {}
    for rule in ("r1_buyer_down", "r2_seller_up"):
        v1 = expectancy["real_v1"].get(rule, {})
        v2 = expectancy["real_v2"].get(rule, {})
        summary[rule] = {
            "v1": {"count": v1.get("count", 0), "mean_r": v1.get("mean_r")},
            "v2": {"count": v2.get("count", 0), "mean_r": v2.get("mean_r")},
        }
    return {
        "proposed_v2_dominance_threshold": suggestion,
        "rationale": rationale,
        "v1_vs_v2_per_rule": summary,
        "notes": [
            "cvd_z held constant across formulas in this quick test; R7 not "
            "directly exercised by the swap.",
            "denominator switch decouples rule firing from cache_volume "
            "mismatch documented in Phase 1G — boundary-hour bars no longer "
            "skew the ratio.",
        ],
    }


# ── orchestrator ────────────────────────────────────────────────────────────

def run(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    feat_proxy, feat_v1, feat_v2, meta = _build_three_frames(symbol, tf)
    frames = {"proxy": feat_proxy, "real_v1": feat_v1, "real_v2": feat_v2}

    distribution = _distribution_block(frames)
    threshold    = _threshold_sensitivity(frames)
    sign_agree   = _sign_agreement(frames)
    expectancy   = _expectancy_block(frames, tf)
    boundary     = _boundary_hours(frames)
    rec          = _recommendation(distribution, expectancy)

    report = {
        "symbol":       symbol,
        "timeframe":    tf,
        "joined":       meta,
        "thresholds_in_use": {
            "RULE_DELTA_DOMINANCE":  of_cfg.RULE_DELTA_DOMINANCE,
            "RULE_ABSORPTION_DELTA": of_cfg.RULE_ABSORPTION_DELTA,
            "RULE_TRAP_DELTA":       of_cfg.RULE_TRAP_DELTA,
        },
        "distribution":          distribution,
        "threshold_sensitivity": threshold,
        "sign_agreement":        sign_agree,
        "per_rule_expectancy":   expectancy,
        "boundary_hours_fires":  boundary,
        "recommendation":        rec,
    }

    json_path = out_dir / f"realflow_denominator_test_{symbol}_{tf}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path = out_dir / f"realflow_denominator_test_{symbol}_{tf}.md"
    md_path.write_text(_render_md(report))

    report["_json_path"] = str(json_path)
    report["_md_path"]   = str(md_path)
    return report


def _render_md(r: dict) -> str:
    L: list[str] = []
    L.append(f"# Denominator Switch Test — {r['symbol']} @ {r['timeframe']}\n")
    L.append(f"- Joined bars: **{r['joined']['joined_n_bars']}**")
    L.append(f"- Window: `{r['joined']['joined_start']}` → "
             f"`{r['joined']['joined_end']}`")
    L.append(f"- Current thresholds: dom={r['thresholds_in_use']['RULE_DELTA_DOMINANCE']} "
             f"abs={r['thresholds_in_use']['RULE_ABSORPTION_DELTA']} "
             f"trap={r['thresholds_in_use']['RULE_TRAP_DELTA']}\n")

    L.append("## Distribution of delta_ratio")
    L.append("| formula | mean | std | min | p25 | p50 | p75 | max |")
    L.append("|---------|------|-----|-----|-----|-----|-----|-----|")
    for name in ("proxy", "real_v1", "real_v2"):
        s = r["distribution"][name]["stats"]
        L.append(f"| {name} | {s['mean']} | {s['std']} | {s['min']} | "
                 f"{s['p25']} | {s['p50']} | {s['p75']} | {s['max']} |")
    L.append("")

    L.append("## fraction |dr| ≥ threshold")
    L.append("| threshold | proxy | real_v1 | real_v2 |")
    L.append("|-----------|-------|---------|---------|")
    for t in THRESHOLD_GRID:
        k = f"{t:.2f}"
        p  = r["distribution"]["proxy"]["frac_abs_ge"][k]
        v1 = r["distribution"]["real_v1"]["frac_abs_ge"][k]
        v2 = r["distribution"]["real_v2"]["frac_abs_ge"][k]
        L.append(f"| {k} | {p} | {v1} | {v2} |")
    L.append("")

    L.append("## R1/R2 fire counts at threshold")
    L.append("| threshold | proxy R1 | proxy R2 | v1 R1 | v1 R2 | v2 R1 | v2 R2 |")
    L.append("|-----------|----------|----------|-------|-------|-------|-------|")
    for t in THRESHOLD_GRID:
        k = f"{t:.2f}"
        p = r["threshold_sensitivity"]["proxy"][k]
        v1 = r["threshold_sensitivity"]["real_v1"][k]
        v2 = r["threshold_sensitivity"]["real_v2"][k]
        L.append(f"| {k} | {p['r1_buyer_down']} | {p['r2_seller_up']} | "
                 f"{v1['r1_buyer_down']} | {v1['r2_seller_up']} | "
                 f"{v2['r1_buyer_down']} | {v2['r2_seller_up']} |")
    L.append("")

    L.append("## Sign agreement")
    sa = r["sign_agreement"]
    L.append(f"- proxy vs v1: **{sa['proxy_vs_v1']}**")
    L.append(f"- proxy vs v2: **{sa['proxy_vs_v2']}**")
    L.append(f"- v1    vs v2: **{sa['v1_vs_v2']}**")
    L.append(f"- three-way agree: **{sa['three_way']}**\n")

    L.append("## Per-rule expectancy (signed forward R, current thresholds)")
    L.append("| rule | proxy n / R / hit | v1 n / R / hit | v2 n / R / hit |")
    L.append("|------|-------------------|----------------|----------------|")
    for rule in RULE_DIR:
        cols = []
        for name in ("proxy", "real_v1", "real_v2"):
            e = r["per_rule_expectancy"][name].get(rule, {})
            n = e.get("count", 0)
            mr = e.get("mean_r")
            hr = e.get("hit_rate")
            cols.append(f"{n} / {mr} / {hr}" if n else "0 / — / —")
        L.append(f"| {rule} | {cols[0]} | {cols[1]} | {cols[2]} |")
    L.append("")

    L.append("## Boundary-hour fires (Phase 1G hot-spots)")
    L.append("| key | n bars | r1 | r2 | r3 | r4 | r5 | r6 | r7 |")
    L.append("|-----|--------|----|----|----|----|----|----|----|")
    for k in sorted(r["boundary_hours_fires"]):
        b = r["boundary_hours_fires"][k]
        f = b["fires"]
        L.append(f"| {k} | {b['n_bars']} | "
                 f"{f.get('r1_buyer_down', 0)} | "
                 f"{f.get('r2_seller_up', 0)} | "
                 f"{f.get('r3_absorption_resistance', 0)} | "
                 f"{f.get('r4_absorption_support', 0)} | "
                 f"{f.get('r5_bull_trap', 0)} | "
                 f"{f.get('r6_bear_trap', 0)} | "
                 f"{f.get('r7_cvd_divergence', 0)} |")
    L.append("")

    rec = r["recommendation"]
    L.append("## Recommendation")
    L.append(f"- Proposed v2 RULE_DELTA_DOMINANCE: "
             f"**{rec['proposed_v2_dominance_threshold']}**")
    L.append(f"- {rec['rationale']}\n")
    L.append("v1 vs v2 (R1/R2):")
    for rule, p in rec["v1_vs_v2_per_rule"].items():
        L.append(f"- **{rule}**: v1 n={p['v1']['count']} R={p['v1']['mean_r']} → "
                 f"v2 n={p['v2']['count']} R={p['v2']['mean_r']}")
    L.append("")
    for n in rec["notes"]:
        L.append(f"- _{n}_")

    return "\n".join(L) + "\n"


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 1H denominator switch test.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = run(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
