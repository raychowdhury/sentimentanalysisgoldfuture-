"""
Phase 2B Shadow Mode — R7 cvd-divergence threshold tracker (read-only).

Recomputes R7 locally with `RULE_CVD_CORR_THRESH_REAL_SHADOW = -0.20`,
records pending/settled outcomes in a separate JSONL, and never modifies
production R7 firing.

Hard invariants:
  * NEVER calls rule_engine.apply_rules with a custom threshold.
  * NEVER writes the shadow constant to config.py.
  * Production R7 column on the joined frame is left untouched.
  * Files are namespaced with `_shadow_` to prevent collision.
  * Signal IDs end in `_r7_shadow` (production ends in `_r7`).

Outputs (all under outputs/order_flow/):
  realflow_r7_shadow_pending_<sym>_<tf>.json
  realflow_r7_shadow_outcomes_<sym>_<tf>.jsonl     (append-only)
  realflow_r7_shadow_summary_<sym>_<tf>.json
  realflow_r7_shadow_compare_<sym>_<tf>.json       (prod vs shadow side-by-side)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realflow_compare as rfc
from order_flow_engine.src import realflow_outcome_tracker as rot
from order_flow_engine.src.realflow_compare import NY_TZ, TF_MINUTES


# Phase 2B Stage 1 — shadow constant. Calibrated on Phase 2B re-sweep
# (1289 joined bars, train/test 70/30, score_ratio 0.52).
# DO NOT add to config.py — keeping this here ensures the value can never
# accidentally be picked up by rule_engine.apply_rules.
RULE_CVD_CORR_THRESH_REAL_SHADOW = -0.20

HORIZON_GRACE_S = 30
SOURCE_RULE = "r7_cvd_divergence"


# Phase 2B re-sweep test mean_r at threshold -0.20 (used for vs_baseline display).
SHADOW_BASELINE_TEST_MEAN_R = 0.7135


# ── path helpers ────────────────────────────────────────────────────────────

def _out_dir() -> Path:
    return Path(of_cfg.OF_OUTPUT_DIR)


def _pending_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_r7_shadow_pending_{symbol}_{tf}.json"


def _outcomes_jsonl_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_r7_shadow_outcomes_{symbol}_{tf}.jsonl"


def _summary_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_r7_shadow_summary_{symbol}_{tf}.json"


def _compare_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_r7_shadow_compare_{symbol}_{tf}.json"


# ── signal ID ───────────────────────────────────────────────────────────────

def _shadow_signal_id(symbol: str, tf: str, fire_ts: pd.Timestamp) -> str:
    """Distinct from production by `_r7_shadow` suffix."""
    if fire_ts.tzinfo is None:
        fire_ts = fire_ts.tz_localize("UTC")
    ts_str = fire_ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{symbol}_{tf}_{ts_str}_r7_shadow"


# ── shadow R7 firing (local; production rule_engine NEVER called for this) ──

def _compute_shadow_r7(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """
    Reproduce R7 logic from rule_engine.apply_rules with the candidate
    threshold. Returns a DataFrame with columns:
      r7_shadow_fire (bool), r7_shadow_dir (int)

    Uses production RULE_CVD_CORR_WINDOW unchanged.
    """
    w = of_cfg.RULE_CVD_CORR_WINDOW
    close = df["Close"]
    ret = close.pct_change()
    corr = df["cvd_z"].rolling(w, min_periods=w).corr(ret)
    fires = (corr < threshold).fillna(False)
    cvd_slope = df["cvd_z"].diff(w).fillna(0.0)
    direction = np.sign(cvd_slope.to_numpy()).astype(int)
    return pd.DataFrame({
        "r7_shadow_fire": fires.astype(bool),
        "r7_shadow_dir":  direction,
    }, index=df.index)


# ── main shadow pass ────────────────────────────────────────────────────────

def shadow_pass(symbol: str = "ESM6", tf: str = "15m") -> dict:
    """
    Discover shadow R7 fires, settle ones whose horizon has elapsed.
    Idempotent. Production R7 path untouched.
    """
    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    tf_min  = TF_MINUTES.get(tf, 15)
    now_utc = pd.Timestamp.now(tz="UTC")

    # 1. Load joined frame (read-only). Same source as production tracker.
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)
    df = real_feat.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # 2. Shadow R7 fires (local, never written back to df).
    shadow = _compute_shadow_r7(df, RULE_CVD_CORR_THRESH_REAL_SHADOW)
    fires_mask = shadow["r7_shadow_fire"]
    if "bar_proxy_mode" in df.columns:
        real_mask = df["bar_proxy_mode"].fillna(1).astype(int) == 0
        fires_mask = fires_mask & real_mask

    # 3. Dedupe + mode index (reuse production helpers — read-only access).
    settled_ids = _settled_ids(_outcomes_jsonl_path(symbol, tf))
    mode_index  = rot._build_mode_index(symbol, tf)

    pending: list[dict] = []
    new_settled: list[dict] = []

    for fire_ts in df.index[fires_mask]:
        sid = _shadow_signal_id(symbol, tf, fire_ts)
        if sid in settled_ids:
            continue

        fire_idx = df.index.get_loc(fire_ts)
        row = df.loc[fire_ts]
        entry = float(row["Close"])
        atr_v = float(row["atr"]) if pd.notna(row["atr"]) else None
        direction = int(shadow.at[fire_ts, "r7_shadow_dir"])
        if direction == 0:
            # Skip ambiguous-direction fires; matches production reversal_direction
            # convention (r7_only fires use cvd slope sign — 0 means undefined).
            continue
        cvd_z       = float(row.get("cvd_z",       0.0))
        delta_ratio = float(row.get("delta_ratio", 0.0))
        settle_eta = fire_ts + pd.Timedelta(minutes=tf_min * horizon)
        mode = mode_index.get(fire_ts, "unknown")

        pending_row = {
            "signal_id":      sid,
            "symbol":         symbol,
            "tf":             tf,
            "rule":           "r7_cvd_divergence_shadow",
            "fire_ts_utc":    fire_ts.tz_convert("UTC").isoformat(),
            "fire_ts_ny":     fire_ts.tz_convert(NY_TZ).isoformat(),
            "direction":      direction,
            "session":        rot._session_bucket(fire_ts),
            "entry_close":    round(entry, 4),
            "atr":            round(float(atr_v), 4) if atr_v is not None else None,
            "delta_ratio":    round(delta_ratio, 4),
            "cvd_z":          round(cvd_z, 4),
            "threshold_path": "real_shadow",
            "shadow_threshold": RULE_CVD_CORR_THRESH_REAL_SHADOW,
            "mode":           mode,
            "horizon_bars":   int(horizon),
            "settle_eta_utc": settle_eta.tz_convert("UTC").isoformat(),
            "discovered_at":  now_utc.isoformat(),
            "is_shadow":      True,
        }

        if (now_utc - settle_eta).total_seconds() < HORIZON_GRACE_S:
            pending.append(pending_row)
            continue
        outcome = rot._score_outcome(df, fire_idx, horizon, direction, atr_v, entry) \
            if atr_v else None
        if outcome is None:
            pending.append(pending_row)
            continue
        new_settled.append({
            **pending_row,
            **outcome,
            "settled_at_utc": now_utc.isoformat(),
        })

    # 4. Append + atomic rewrites.
    jsonl_path = _outcomes_jsonl_path(symbol, tf)
    if new_settled:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a") as f:
            for r in new_settled:
                f.write(json.dumps(r, default=str) + "\n")

    rot._atomic_write(_pending_path(symbol, tf),
                      json.dumps(pending, indent=2, default=str))

    all_settled = rot._read_jsonl(jsonl_path)
    summary = _build_summary(all_settled, pending, symbol, tf)
    rot._atomic_write(_summary_path(symbol, tf),
                      json.dumps(summary, indent=2, default=str))

    # 5. Side-by-side compare with production R7.
    compare = _build_compare(symbol, tf, summary)
    rot._atomic_write(_compare_path(symbol, tf),
                      json.dumps(compare, indent=2, default=str))

    return {
        "symbol":          symbol,
        "timeframe":       tf,
        "is_shadow":       True,
        "shadow_threshold": RULE_CVD_CORR_THRESH_REAL_SHADOW,
        "n_pending":       len(pending),
        "n_new_settled":   len(new_settled),
        "n_total_settled": len(all_settled),
        "paths": {
            "pending":        str(_pending_path(symbol, tf)),
            "outcomes_jsonl": str(jsonl_path),
            "summary":        str(_summary_path(symbol, tf)),
            "compare":        str(_compare_path(symbol, tf)),
        },
    }


# ── helpers (thin wrappers around production tracker) ───────────────────────

def _settled_ids(jsonl_path: Path) -> set[str]:
    return rot._settled_ids(jsonl_path)


def _build_summary(settled: list[dict], pending: list[dict],
                   symbol: str, tf: str) -> dict:
    now = pd.Timestamp.now(tz="UTC").isoformat()
    base = {
        "as_of_utc":          now,
        "is_shadow":          True,
        "shadow_threshold":   RULE_CVD_CORR_THRESH_REAL_SHADOW,
        "symbol":             symbol,
        "tf":                 tf,
        "n_pending":          len(pending),
        "n_settled":          len(settled),
        "sample_size_label":  rot._sample_size_label(len(settled)),
    }
    if not settled:
        base.update({"by_session": {}, "by_mode": {}, "alerts_per_day": 0.0,
                     "vs_shadow_baseline": {}})
        return base
    df = pd.DataFrame(settled)
    overall = rot._agg(df)
    base.update({
        "since":          str(df["fire_ts_utc"].min()),
        "overall":        overall,
        "by_session":     {k: rot._agg(g) for k, g in df.groupby("session")},
        "by_mode":        ({k: rot._agg(g) for k, g in df.groupby("mode")}
                           if "mode" in df.columns else {}),
        "alerts_per_day": rot._alerts_per_day(df),
        "vs_shadow_baseline": {
            "baseline_test_mean_r": SHADOW_BASELINE_TEST_MEAN_R,
            "live_mean_r":          overall.get("mean_r"),
            "retention_pct":        (round(overall.get("mean_r")
                                           / SHADOW_BASELINE_TEST_MEAN_R, 4)
                                     if overall.get("mean_r") is not None
                                     and SHADOW_BASELINE_TEST_MEAN_R
                                     else None),
        },
    })
    return base


def _build_compare(symbol: str, tf: str, shadow_summary: dict) -> dict:
    """Side-by-side: production R7 outcomes vs shadow R7 outcomes."""
    prod_summary_path = (Path(of_cfg.OF_OUTPUT_DIR) /
                         f"realflow_outcomes_summary_{symbol}_{tf}.json")
    prod = {}
    if prod_summary_path.exists():
        try:
            prod = json.loads(prod_summary_path.read_text())
        except Exception:
            prod = {}
    prod_r7 = (prod.get("by_rule") or {}).get("r7_cvd_divergence", {})
    return {
        "as_of_utc":   pd.Timestamp.now(tz="UTC").isoformat(),
        "symbol":      symbol,
        "tf":          tf,
        "production": {
            "threshold": of_cfg.RULE_CVD_CORR_THRESH,
            "n":        prod_r7.get("n", 0),
            "hit_rate": prod_r7.get("hit_rate"),
            "mean_r":   prod_r7.get("mean_r"),
        },
        "shadow": {
            "threshold":  RULE_CVD_CORR_THRESH_REAL_SHADOW,
            "n":          shadow_summary.get("n_settled", 0),
            "alerts_per_day": shadow_summary.get("alerts_per_day"),
        },
        "note": ("Shadow is diagnostic-only at threshold "
                 f"{RULE_CVD_CORR_THRESH_REAL_SHADOW}. Production R7 firing "
                 f"path is unchanged. No threshold has been promoted."),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 2B Stage 1 R7 shadow tracker.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = shadow_pass(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
