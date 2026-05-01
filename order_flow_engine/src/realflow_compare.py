"""
Phase-1 proxy-vs-real comparison.

Builds the order-flow feature pipeline twice on the same OHLCV bars,
once with CLV/proxy buy-sell split and once with `buy_vol_real` /
`sell_vol_real` injected from the live Databento tail. Reports rule
fire counts, label distribution, sign agreement, and per-label trade
expectancy at the same anchor TF.

Single-TF only — multi-TF context (delta_ratio_1h etc.) is intentionally
skipped so the diff isolates the orderflow proxy vs real signal.

Run:
    python -m order_flow_engine.src.realflow_compare \\
        --symbol ESM6 --tf 15m
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY_TZ = ZoneInfo("America/New_York")
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

from order_flow_engine.src import (
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    label_generator,
    realflow_loader,
    rule_engine,
)
from order_flow_engine.src import predictor as _pred
from order_flow_engine.src.backtester import _direction_for_row


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
    elif out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    return out.sort_index()


def _build_pipeline(df_bars: pd.DataFrame, tf: str) -> pd.DataFrame:
    """OHLCV → flow features → rules → labels. Single-TF, no merge."""
    feat = fe.build_features_for_tf(df_bars, tf)
    feat = rule_engine.apply_rules(feat)
    feat["label"] = label_generator.generate_labels(feat, tf)
    return feat


def _expectancy_by_label(df: pd.DataFrame, tf: str) -> dict:
    """Mean forward R per non-normal label using rule_engine direction."""
    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    atr_safe = df["atr"].replace(0, np.nan)
    fwd = df["Close"].shift(-horizon) - df["Close"]
    fwd_r = (fwd / atr_safe).fillna(0.0)

    out: dict[str, dict] = {}
    for label in of_cfg.LABEL_CLASSES:
        if label == "normal_behavior":
            continue
        rows = df[df["label"] == label]
        if rows.empty:
            out[label] = {"count": 0, "mean_r": None}
            continue
        dirs = np.array([_direction_for_row(label, r) for _, r in rows.iterrows()])
        signed = fwd_r.loc[rows.index].to_numpy() * dirs
        out[label] = {
            "count":  int(len(rows)),
            "mean_r": round(float(signed.mean()), 4),
        }
    return out


def _rule_counts(df: pd.DataFrame) -> dict:
    return {r: int(df[r].fillna(False).sum()) for r in rule_engine.ALL_RULE_COLS}


def _label_dist(df: pd.DataFrame) -> dict:
    return {c: int((df["label"] == c).sum()) for c in of_cfg.LABEL_CLASSES}


# ── Phase 1B diagnostic helpers ─────────────────────────────────────────────

def _series_stats(s: pd.Series) -> dict:
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


def _frac_above(s: pd.Series, threshold: float) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return round(float((s.abs() >= threshold).mean()), 4)


def _ratio_distribution(proxy_feat: pd.DataFrame, real_feat: pd.DataFrame) -> dict:
    """delta_ratio / cvd_z / buy_share distributions per source."""
    out: dict[str, dict] = {}
    for name, src_df in (("proxy", proxy_feat), ("real", real_feat)):
        dr = src_df["delta_ratio"]
        out[name] = {
            "delta_ratio": {
                **_series_stats(dr),
                "frac_abs_ge_dominance": _frac_above(
                    dr, of_cfg.RULE_DELTA_DOMINANCE),
                "frac_abs_ge_absorption": _frac_above(
                    dr, of_cfg.RULE_ABSORPTION_DELTA),
                "frac_abs_ge_trap": _frac_above(
                    dr, of_cfg.RULE_TRAP_DELTA),
            },
            "cvd_z": _series_stats(src_df["cvd_z"]),
            "buy_share": _series_stats((1 + src_df["clv"]) / 2)
                          if name == "proxy"
                          else _series_stats(
                              src_df["buy_vol"] /
                              src_df[["buy_vol", "sell_vol"]].sum(axis=1).replace(0, np.nan)
                          ),
        }
    return out


def _per_bar_trace(
    proxy_feat: pd.DataFrame,
    real_feat:  pd.DataFrame,
    real_bars:  pd.DataFrame,
    raw_bars:   pd.DataFrame,
) -> list[dict]:
    """Bar-by-bar side-by-side trace for the joined window."""
    rows: list[dict] = []
    for ts in proxy_feat.index:
        rp = proxy_feat.loc[ts]
        rr = real_feat.loc[ts]
        cache_vol_raw = raw_bars.loc[ts, "Volume"] if ts in raw_bars.index else np.nan
        cache_vol = float(cache_vol_raw) if pd.notna(cache_vol_raw) else float("nan")
        real_buy_raw  = real_bars.loc[ts, "buy_vol_real"]
        real_sell_raw = real_bars.loc[ts, "sell_vol_real"]
        real_buy  = float(real_buy_raw)  if pd.notna(real_buy_raw)  else None
        real_sell = float(real_sell_raw) if pd.notna(real_sell_raw) else None
        real_total = (real_buy + real_sell) if (real_buy is not None and real_sell is not None) else None
        proxy_fired = [c for c in rule_engine.ALL_RULE_COLS if bool(rp.get(c, False))]
        real_fired  = [c for c in rule_engine.ALL_RULE_COLS if bool(rr.get(c, False))]

        real_buy_share = (round(real_buy / real_total, 4)
                          if real_total and real_total > 0 else None)
        vol_match = (round(real_total / cache_vol, 4)
                     if (real_total is not None and cache_vol and cache_vol > 0) else None)

        rows.append({
            "ts": str(ts),
            "Open":  round(float(rp["Open"]), 4),
            "High":  round(float(rp["High"]), 4),
            "Low":   round(float(rp["Low"]),  4),
            "Close": round(float(rp["Close"]), 4),
            "cache_volume": (round(cache_vol, 2) if pd.notna(cache_vol) else None),
            "clv":               round(float(rp.get("clv", 0.0)), 4),
            "proxy_buy_share":   round(float((1 + rp.get("clv", 0.0)) / 2), 4),
            "real_buy_share":    real_buy_share,
            "proxy_delta_ratio": round(float(rp["delta_ratio"]), 4),
            "real_delta_ratio":  round(float(rr["delta_ratio"]), 4),
            "proxy_cvd_z":       round(float(rp["cvd_z"]), 4),
            "real_cvd_z":        round(float(rr["cvd_z"]), 4),
            "real_buy_vol":      (round(real_buy, 2)  if real_buy  is not None else None),
            "real_sell_vol":     (round(real_sell, 2) if real_sell is not None else None),
            "real_total_vol":    (round(real_total, 2) if real_total is not None else None),
            "vol_match_pct":     vol_match,
            "proxy_rules_fired": proxy_fired,
            "real_rules_fired":  real_fired,
        })
    return rows


def _volume_recon(trace: list[dict]) -> dict:
    matches = [r["vol_match_pct"] for r in trace if r["vol_match_pct"] is not None]
    if not matches:
        return {"mean": None, "min": None, "max": None,
                "n_bars": 0, "n_mismatch_5pct": 0}
    arr = np.array(matches)
    return {
        "mean": round(float(arr.mean()), 4),
        "min":  round(float(arr.min()),  4),
        "max":  round(float(arr.max()),  4),
        "n_bars":           len(matches),
        "n_mismatch_5pct":  int(((arr < 0.95) | (arr > 1.05)).sum()),
    }


def _threshold_sensitivity(
    proxy_feat: pd.DataFrame,
    real_feat:  pd.DataFrame,
    grid: list[float],
) -> dict:
    """
    Recompute R1/R2 fire counts at a sweep of dominance thresholds without
    actually re-running the rule engine. R1 = (dr > thr) & (fwd_atr < -0.3);
    R2 = (dr < -thr) & (fwd_atr > 0.3). Forward-move clause stays fixed at
    its current value so the diff isolates the threshold knob.
    """
    out: dict[str, dict] = {}
    for src_name, src in (("proxy", proxy_feat), ("real", real_feat)):
        atr_frac = (src["atr_pct"] / 100).replace(0, np.nan)
        fwd_atr = (src["fwd_ret_1"] / atr_frac).fillna(0.0)
        dr = src["delta_ratio"]
        per_thr: dict[str, dict] = {}
        for thr in grid:
            r1 = ((dr >  thr) & (fwd_atr < -0.3)).fillna(False).sum()
            r2 = ((dr < -thr) & (fwd_atr >  0.3)).fillna(False).sum()
            per_thr[f"{thr:.2f}"] = {
                "r1_buyer_down": int(r1),
                "r2_seller_up":  int(r2),
            }
        out[src_name] = per_thr
    return out


def _top_diff_bars(trace: list[dict], n: int = 5) -> list[dict]:
    sortable = [
        (abs(r["proxy_delta_ratio"] - r["real_delta_ratio"]), r)
        for r in trace
    ]
    sortable.sort(reverse=True, key=lambda x: x[0])
    return [{
        "ts": r["ts"],
        "proxy_delta_ratio": r["proxy_delta_ratio"],
        "real_delta_ratio":  r["real_delta_ratio"],
        "abs_diff":          round(diff, 4),
        "proxy_rules_fired": r["proxy_rules_fired"],
        "real_rules_fired":  r["real_rules_fired"],
        "vol_match_pct":     r["vol_match_pct"],
    } for diff, r in sortable[:n]]


# ── Regenerate-button status (cheap, no full diagnose) ─────────────────────

def peek_button_status(symbol: str, tf: str,
                       output_dir: Path | None = None) -> dict:
    """
    Cheap status check for the dashboard's Regenerate button.

    Compares the last regenerated diagnostic JSON (`generated.ts_utc`) with
    the current live tail's max ts. Used by an AJAX poll so the page can
    show whether clicking Regenerate would produce new data.
    """
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    json_path = out_dir / f"realflow_diagnostic_{symbol}_{tf}.json"

    last_regen = None
    if json_path.exists():
        try:
            saved = json.loads(json_path.read_text())
            ts = saved.get("generated", {}).get("ts_utc")
            if ts:
                last_regen = pd.Timestamp(ts)
                if last_regen.tzinfo is None:
                    last_regen = last_regen.tz_localize("UTC")
        except Exception:
            last_regen = None

    # Pick the freshest live source: same-tf file first, then 1m fallback.
    tf_ts,  tf_src  = _peek_live_tail_max(symbol, tf)
    one_ts, one_src = _peek_live_tail_max(symbol, "1m")
    if tf_ts is None:
        live_ts, live_src = one_ts, one_src
    elif one_ts is None:
        live_ts, live_src = tf_ts, tf_src
    else:
        live_ts, live_src = (one_ts, one_src) if one_ts > tf_ts else (tf_ts, tf_src)

    now_utc = pd.Timestamp.now(tz="UTC")
    tf_min  = TF_MINUTES.get(tf, 15)

    bars_since = 0
    if live_ts is not None and last_regen is not None:
        delta_min = (live_ts - last_regen).total_seconds() / 60.0
        bars_since = max(0, int(delta_min // tf_min))

    lag_minutes = (round((now_utc - live_ts).total_seconds() / 60.0, 1)
                   if live_ts is not None else None)

    if bars_since > 0:
        state = "new_data"
        label = f"Regenerate ({bars_since} new bar{'s' if bars_since != 1 else ''})"
    elif (lag_minutes is not None and lag_minutes > 2 * tf_min):
        state = "stale"
        label = "Regenerate (stale data)"
    else:
        state = "up_to_date"
        label = "Regenerate (up to date)"

    return {
        "symbol":               symbol,
        "timeframe":            tf,
        "state":                state,
        "label":                label,
        "bars_since_regen":     int(bars_since),
        "lag_minutes_now":      lag_minutes,
        "last_regen":           _fmt_ts(last_regen),
        "current_live_max":     {**_fmt_ts(live_ts), "source": live_src},
        "tf_period_min":        tf_min,
    }


# ── Phase 1E monitoring ─────────────────────────────────────────────────────

# ESM6 RTH window (CME E-mini S&P): 13:30 ≤ UTC < 20:00.
RTH_START_HOUR = 13
RTH_START_MIN  = 30
RTH_END_HOUR   = 20

# Phase 2 readiness gates (Phase 1D plan).
GATE_BAR_COUNT_MIN     = 500
GATE_VOL_MEDIAN_LOW    = 0.95
GATE_VOL_MEDIAN_HIGH   = 1.05
GATE_VOL_MISMATCH_MAX  = 0.25
GATE_DRIFT_MAX         = 0.10
GATE_REAL_FIRES_MIN    = 10
GATE_FIRE_RULES        = ("r1_buyer_down", "r2_seller_up", "r7_cvd_divergence")


def _is_rth(ts: pd.Timestamp) -> bool:
    t = ts.tz_convert("UTC") if ts.tzinfo else ts
    minutes = t.hour * 60 + t.minute
    return (RTH_START_HOUR * 60 + RTH_START_MIN) <= minutes < (RTH_END_HOUR * 60)


def _vol_match_stats(matches: list[float]) -> dict:
    if not matches:
        return {"median": None, "iqr": [None, None], "mismatch_5pct_rate": None}
    arr = np.array(matches, dtype=float)
    return {
        "median": round(float(np.median(arr)), 4),
        "iqr": [
            round(float(np.quantile(arr, 0.25)), 4),
            round(float(np.quantile(arr, 0.75)), 4),
        ],
        "mismatch_5pct_rate":
            round(float(((arr < 0.95) | (arr > 1.05)).mean()), 4),
    }


def _delta_ratio_drift(real_feat: pd.DataFrame) -> dict:
    """Rolling 100-bar std drift over last 5 windows."""
    s = pd.to_numeric(real_feat["delta_ratio"], errors="coerce")
    window = 100
    if len(s) < window:
        return {
            "drift_pct": None,
            "rolling_window": window,
            "n_rolling_points": 0,
            "note": f"need >={window} bars; have {len(s)}",
        }
    rolling = s.rolling(window=window, min_periods=window).std().dropna()
    if len(rolling) < 5:
        return {
            "drift_pct": None,
            "rolling_window": window,
            "n_rolling_points": int(len(rolling)),
            "note": "need >=5 rolling-std points",
        }
    last = rolling.iloc[-5:]
    mean = float(last.mean())
    drift = float((last.max() - last.min()) / mean) if mean else None
    return {
        "drift_pct":          round(drift, 4) if drift is not None else None,
        "rolling_window":     window,
        "n_rolling_points":   int(len(rolling)),
        "rolling_std_last5":  [round(float(v), 4) for v in last.values],
    }


def _session_split(trace: list[dict]) -> dict:
    """Split vol_match by RTH vs ETH; report median + count per session."""
    rth, eth = [], []
    for r in trace:
        if r["vol_match_pct"] is None:
            continue
        ts = pd.Timestamp(r["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        bucket = rth if _is_rth(ts) else eth
        bucket.append(r["vol_match_pct"])

    def _summary(vals):
        if not vals:
            return {"n_bars": 0, "vol_match_median": None, "mismatch_5pct_rate": None}
        arr = np.array(vals)
        return {
            "n_bars": int(len(arr)),
            "vol_match_median": round(float(np.median(arr)), 4),
            "mismatch_5pct_rate":
                round(float(((arr < 0.95) | (arr > 1.05)).mean()), 4),
        }
    return {"RTH": _summary(rth), "ETH": _summary(eth)}


def _bars_added_today(common: pd.Index) -> int:
    today = pd.Timestamp.now(tz="UTC").date()
    return int(sum(1 for ts in common if ts.date() == today))


def _real_fires_cumulative(real_feat: pd.DataFrame) -> dict:
    return {r: int(real_feat[r].fillna(False).sum()) for r in rule_engine.ALL_RULE_COLS}


def _live_nan_rows(real_raw: pd.DataFrame) -> int:
    """NaN count in the live tail's real-flow columns (joined or full)."""
    cols = [c for c in ("buy_vol_real", "sell_vol_real") if c in real_raw.columns]
    if not cols:
        return 0
    return int(real_raw[cols].isna().any(axis=1).sum())


def _monitoring(
    common: pd.Index,
    proxy_feat: pd.DataFrame,
    real_feat:  pd.DataFrame,
    real_raw:   pd.DataFrame,
    trace:      list[dict],
    vol_recon:  dict,
) -> dict:
    matches = [r["vol_match_pct"] for r in trace if r["vol_match_pct"] is not None]
    vm_stats = _vol_match_stats(matches)
    return {
        "today_utc":          str(pd.Timestamp.now(tz="UTC").date()),
        "bars_added_today":   _bars_added_today(common),
        "joined_bar_count":   int(len(common)),
        "live_nan_rows":      _live_nan_rows(real_raw),
        "delta_ratio_drift":  _delta_ratio_drift(real_feat),
        "vol_match":          {
            **vm_stats,
            "n_bars": int(vol_recon.get("n_bars", 0) or 0),
        },
        "session_split":      _session_split(trace),
        "real_fires_cumulative": _real_fires_cumulative(real_feat),
    }


def _phase_status_blocks(monitoring: dict, gates: dict) -> dict:
    """
    Phase 2A vs 2B vs data-readiness vs volume-warning status.

    Phase 1H found vol_match_mismatch is testing the wrong thing (denominator
    switch was a no-op; real-flow thresholds are the actual fix). We keep the
    metric visible as a warning rather than block on it.

    R7 (cvd_z divergence) uses RULE_CVD_CORR_THRESH which Phase 2A did not
    retune — Phase 2B candidate.
    """
    fires = monitoring.get("real_fires_cumulative", {})
    r1 = int(fires.get("r1_buyer_down", 0))
    r2 = int(fires.get("r2_seller_up", 0))
    r7 = int(fires.get("r7_cvd_divergence", 0))

    bar_pass   = bool(gates.get("bar_count",        {}).get("pass"))
    median_ok  = bool(gates.get("vol_match_median", {}).get("pass"))
    drift_ok   = bool(gates.get("drift",            {}).get("pass"))
    mismatch_ok = bool(gates.get("vol_match_mismatch", {}).get("pass"))

    data_readiness = bar_pass and median_ok and drift_ok
    phase2a_ok = r1 >= GATE_REAL_FIRES_MIN and r2 >= GATE_REAL_FIRES_MIN
    phase2b_ok = r7 >= GATE_REAL_FIRES_MIN

    return {
        "data_readiness": {
            "pass": bool(data_readiness),
            "label": "PASSED" if data_readiness else "INSUFFICIENT",
            "checks": {
                "bar_count_ge_500":     bar_pass,
                "vol_match_median_ok":  median_ok,
                "drift_ok":             drift_ok,
            },
        },
        "phase2a_status": {
            "pass": bool(phase2a_ok),
            "label": "PASSED" if phase2a_ok else "NEEDED",
            "scope": "R1/R2 real-flow thresholds",
            "fires_r1": r1,
            "fires_r2": r2,
            "fires_min": GATE_REAL_FIRES_MIN,
        },
        "phase2b_status": {
            "pass": bool(phase2b_ok),
            "label": "PASSED" if phase2b_ok else "NEEDED FOR R7",
            "scope": "R7 cvd-divergence threshold (RULE_CVD_CORR_THRESH)",
            "fires_r7": r7,
            "fires_min": GATE_REAL_FIRES_MIN,
        },
        "volume_warning": {
            "ok":    bool(mismatch_ok),
            "label": "OK" if mismatch_ok else "WARNING",
            "note":  ("known source mismatch / session-boundary warning — "
                      "Phase 1H found denominator switch is a no-op; "
                      "real-flow thresholds (Phase 2A) are the actual fix"),
            "actual": gates.get("vol_match_mismatch", {}).get("actual"),
        },
    }


def _phase2_gates(monitoring: dict) -> dict:
    n = monitoring["joined_bar_count"]
    vm = monitoring["vol_match"]
    drift = monitoring["delta_ratio_drift"]
    fires = monitoring["real_fires_cumulative"]

    g_bars = n >= GATE_BAR_COUNT_MIN
    g_med  = (vm["median"] is not None
              and GATE_VOL_MEDIAN_LOW <= vm["median"] <= GATE_VOL_MEDIAN_HIGH)
    g_mis  = (vm["mismatch_5pct_rate"] is not None
              and vm["mismatch_5pct_rate"] <= GATE_VOL_MISMATCH_MAX)
    g_drift = (drift["drift_pct"] is not None
               and drift["drift_pct"] <= GATE_DRIFT_MAX)
    fires_actual = {r: int(fires.get(r, 0)) for r in GATE_FIRE_RULES}
    g_fires = all(v >= GATE_REAL_FIRES_MIN for v in fires_actual.values())

    return {
        "bar_count": {
            "target": f">= {GATE_BAR_COUNT_MIN}",
            "actual": n,
            "pass":   bool(g_bars),
        },
        "vol_match_median": {
            "target": f"[{GATE_VOL_MEDIAN_LOW}, {GATE_VOL_MEDIAN_HIGH}]",
            "actual": vm["median"],
            "pass":   bool(g_med),
        },
        "vol_match_mismatch": {
            "target": f"<= {GATE_VOL_MISMATCH_MAX}",
            "actual": vm["mismatch_5pct_rate"],
            "pass":   bool(g_mis),
        },
        "drift": {
            "target": f"<= {GATE_DRIFT_MAX}",
            "actual": drift["drift_pct"],
            "pass":   bool(g_drift),
            "note":   drift.get("note"),
        },
        "real_fires_min10": {
            "target": f">= {GATE_REAL_FIRES_MIN} each ({', '.join(GATE_FIRE_RULES)})",
            "actual": fires_actual,
            "pass":   bool(g_fires),
        },
        "all_pass": bool(g_bars and g_med and g_mis and g_drift and g_fires),
    }


# ── Freshness / generated metadata ──────────────────────────────────────────

def _fmt_ts(ts: pd.Timestamp | None) -> dict:
    """Return {ts_utc, ts_ny} ISO strings or Nones."""
    if ts is None or pd.isna(ts):
        return {"ts_utc": None, "ts_ny": None}
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return {
        "ts_utc": ts.tz_convert("UTC").isoformat(),
        "ts_ny":  ts.tz_convert(NY_TZ).isoformat(),
    }


def _ceil_to_period(ts: pd.Timestamp, period_min: int) -> pd.Timestamp:
    """Round up to next period boundary in UTC."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts = ts.tz_convert("UTC")
    floor = ts.floor(f"{period_min}min")
    return floor + pd.Timedelta(minutes=period_min) if floor == ts else floor + pd.Timedelta(minutes=period_min)


def _peek_live_tail_max(symbol: str, tf: str) -> tuple[pd.Timestamp | None, str | None]:
    """Return (latest_ts, source_filename) for the live tail at given tf.
    Returns (None, None) if file absent or unreadable."""
    path = realflow_loader._live_path(symbol, tf)
    if not path.exists():
        return None, None
    try:
        df = pd.read_parquet(path)
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            idx = pd.to_datetime(idx, utc=True)
        elif idx.tz is None:
            idx = idx.tz_localize("UTC")
        if len(idx) == 0:
            return None, path.name
        return idx.max().tz_convert("UTC"), path.name
    except Exception:
        return None, path.name


def _freshness_block(symbol: str, tf: str) -> dict:
    """Generated-at, latest-live-bar (tf and 1m), lag, freshness label, next check."""
    now_utc = pd.Timestamp.now(tz="UTC")
    tf_min = TF_MINUTES.get(tf, 15)

    tf_ts,  tf_src  = _peek_live_tail_max(symbol, tf)
    one_ts, one_src = _peek_live_tail_max(symbol, "1m")

    # Use 1m if available (freshest), fall back to tf.
    primary_ts, primary_src = (one_ts, one_src) if one_ts is not None else (tf_ts, tf_src)

    if primary_ts is not None:
        lag_seconds = (now_utc - primary_ts).total_seconds()
        lag_minutes = round(lag_seconds / 60.0, 1)
        ratio = lag_minutes / tf_min if tf_min else None
        if ratio is None:
            label, expl = "unknown", "no tf period"
        elif ratio <= 1.0:
            label = "fresh"
            expl = f"latest bar {lag_minutes:.1f} min old; <= 1× the {tf} period"
        elif ratio <= 4.0:
            label = "recent"
            expl = f"latest bar {lag_minutes:.1f} min old; <= 4× the {tf} period"
        else:
            label = "stale"
            expl = f"latest bar {lag_minutes:.1f} min old; >4× the {tf} period"
    else:
        lag_minutes = None
        ratio = None
        label = "unknown"
        expl = "no live tail file"

    if primary_ts is not None:
        next_close = _ceil_to_period(primary_ts, tf_min)
        next_check = next_close + pd.Timedelta(seconds=60)
        next_rationale = f"next {tf} close + 60s grace"
        # Guarantee future-dated; if data is stale, roll forward to next period
        # boundary after now.
        if next_check <= now_utc:
            next_check = _ceil_to_period(now_utc, tf_min) + pd.Timedelta(seconds=60)
            next_rationale = (f"data stale — next {tf} boundary after now "
                              "+ 60s grace")
    else:
        next_check = _ceil_to_period(now_utc, tf_min) + pd.Timedelta(seconds=60)
        next_rationale = f"no live data — next {tf} boundary after now + 60s"

    return {
        "generated": {
            **_fmt_ts(now_utc),
            "age_seconds": 0,
        },
        "latest_live_bar": {
            "primary": {
                **_fmt_ts(primary_ts),
                "lag_minutes": lag_minutes,
                "source":      primary_src,
            },
            "tf": {
                **_fmt_ts(tf_ts),
                "source": tf_src,
            },
            "one_min": {
                **_fmt_ts(one_ts),
                "source": one_src,
            },
        },
        "freshness": {
            "label":          label,
            "tf_period_min":  tf_min,
            "lag_vs_period":  round(ratio, 2) if ratio is not None else None,
            "explanation":    expl,
        },
        "next_check": {
            **_fmt_ts(next_check),
            "rationale": next_rationale,
        },
    }


def _load_pair(symbol: str, tf: str):
    """Return (raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat)."""
    raw = data_loader.fetch_ohlcv(symbol, tf,
                                  lookback_days=of_cfg.OF_LOOKBACK_DAYS,
                                  use_cache=True)
    if raw is None or raw.empty:
        raise RuntimeError(f"No OHLCV cache for {symbol}@{tf}")
    raw = _normalize_index(raw)

    real = realflow_loader.load_realflow(symbol, tf)
    real = _normalize_index(real)

    common = raw.index.intersection(real.index)
    if len(common) == 0:
        raise RuntimeError(
            f"No timestamp overlap between OHLCV cache "
            f"({raw.index.min()}–{raw.index.max()}) and live tail "
            f"({real.index.min()}–{real.index.max()})"
        )

    proxy_bars = raw.loc[common].copy()
    real_bars  = raw.loc[common].copy()
    real_bars["buy_vol_real"]  = real.loc[common, "buy_vol_real"]
    real_bars["sell_vol_real"] = real.loc[common, "sell_vol_real"]

    proxy_feat = _build_pipeline(proxy_bars, tf)
    real_feat  = _build_pipeline(real_bars,  tf)
    return raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat


def diagnose(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    """
    Phase 1B diagnostic. Why does proxy fire rules and real-flow not?

    Emits a per-bar trace, distribution stats, volume-reconciliation,
    threshold sensitivity for R1/R2, and the top-diff bars.

    Output: outputs/order_flow/realflow_diagnostic_<symbol>_<tf>.json
    """
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    freshness = _freshness_block(symbol, tf)

    try:
        raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
            _load_pair(symbol, tf)
    except (RuntimeError, FileNotFoundError) as e:
        # Emit a degraded JSON so the dashboard still shows freshness/timing.
        degraded = {
            "symbol":    symbol,
            "timeframe": tf,
            **freshness,
            "load_error": str(e),
        }
        out_path = out_dir / f"realflow_diagnostic_{symbol}_{tf}.json"
        out_path.write_text(json.dumps(degraded, indent=2, default=str))
        degraded["_output_path"] = str(out_path)
        return degraded

    trace = _per_bar_trace(proxy_feat, real_feat, real_bars, raw)
    vol_recon = _volume_recon(trace)
    monitoring = _monitoring(common, proxy_feat, real_feat, real, trace, vol_recon)
    gates = _phase2_gates(monitoring)
    status = _phase_status_blocks(monitoring, gates)

    diagnostic = {
        "symbol":    symbol,
        "timeframe": tf,
        **freshness,
        "joined": {
            "start":  str(common.min()),
            "end":    str(common.max()),
            "n_bars": int(len(common)),
        },
        "thresholds": {
            "RULE_DELTA_DOMINANCE":  of_cfg.RULE_DELTA_DOMINANCE,
            "RULE_ABSORPTION_DELTA": of_cfg.RULE_ABSORPTION_DELTA,
            "RULE_TRAP_DELTA":       of_cfg.RULE_TRAP_DELTA,
        },
        "distribution":  _ratio_distribution(proxy_feat, real_feat),
        "volume_recon":  vol_recon,
        "threshold_sensitivity":
            _threshold_sensitivity(
                proxy_feat, real_feat,
                grid=[0.05, 0.10, 0.15, 0.20, 0.25, 0.30],
            ),
        "top_diff_bars": _top_diff_bars(trace, n=5),
        "monitoring":    monitoring,
        "phase2_gates":  gates,
        "status":        status,
        "bars":          trace,
    }

    out_path = out_dir / f"realflow_diagnostic_{symbol}_{tf}.json"
    out_path.write_text(json.dumps(diagnostic, indent=2, default=str))
    diagnostic["_output_path"] = str(out_path)
    return diagnostic


def compare(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. OHLCV cache (proxy source).
    raw = data_loader.fetch_ohlcv(symbol, tf,
                                  lookback_days=of_cfg.OF_LOOKBACK_DAYS,
                                  use_cache=True)
    if raw is None or raw.empty:
        raise RuntimeError(f"No OHLCV cache for {symbol}@{tf}")
    raw = _normalize_index(raw)

    # 2. Real-flow tail.
    real = realflow_loader.load_realflow(symbol, tf)
    real = _normalize_index(real)

    # 3. Inner-join on timestamp — bars where both sources agree.
    common = raw.index.intersection(real.index)
    if len(common) == 0:
        raise RuntimeError(
            f"No timestamp overlap between OHLCV cache "
            f"({raw.index.min()}–{raw.index.max()}) and live tail "
            f"({real.index.min()}–{real.index.max()})"
        )

    proxy_bars = raw.loc[common].copy()
    real_bars  = raw.loc[common].copy()
    real_bars["buy_vol_real"]  = real.loc[common, "buy_vol_real"]
    real_bars["sell_vol_real"] = real.loc[common, "sell_vol_real"]

    # 4. Run the pipeline twice.
    proxy_feat = _build_pipeline(proxy_bars, tf)
    real_feat  = _build_pipeline(real_bars,  tf)

    # 5. Bar-level agreement on the joined window.
    sign_proxy = np.sign(proxy_feat["delta_ratio"].fillna(0).to_numpy())
    sign_real  = np.sign(real_feat["delta_ratio"].fillna(0).to_numpy())
    agree_pct = float((sign_proxy == sign_real).mean()) if len(sign_proxy) else 0.0

    cvd_corr = float(
        proxy_feat["cvd_z"].corr(real_feat["cvd_z"])
    ) if len(proxy_feat) > 1 else float("nan")

    # 6. Rule + label diffs.
    proxy_rules = _rule_counts(proxy_feat)
    real_rules  = _rule_counts(real_feat)
    rule_diff = {
        r: {
            "proxy": proxy_rules[r],
            "real":  real_rules[r],
            "delta": real_rules[r] - proxy_rules[r],
        }
        for r in proxy_rules
    }

    proxy_labels = _label_dist(proxy_feat)
    real_labels  = _label_dist(real_feat)

    # 7. Expectancy by label (forward R at OF_FORWARD_BARS[tf]).
    proxy_exp = _expectancy_by_label(proxy_feat, tf)
    real_exp  = _expectancy_by_label(real_feat,  tf)

    notes: list[str] = []
    if len(common) < 50:
        notes.append(
            f"Joined window only {len(common)} bars — sample too small for "
            "reliable expectancy. Treat per-label R as illustrative."
        )
    if proxy_feat["fwd_ret_n"].isna().mean() > 0.3:
        notes.append("Forward-return horizon eats a large fraction of the window.")

    summary = {
        "symbol": symbol,
        "timeframe": tf,
        "proxy_window": {
            "start": str(raw.index.min()),
            "end":   str(raw.index.max()),
            "n_bars": int(len(raw)),
        },
        "real_window": {
            "start": str(real.index.min()),
            "end":   str(real.index.max()),
            "n_bars": int(len(real)),
        },
        "joined": {
            "start": str(common.min()),
            "end":   str(common.max()),
            "n_bars": int(len(common)),
        },
        "delta_ratio_sign_agree_pct": round(agree_pct, 4),
        "cvd_z_corr": round(cvd_corr, 4) if not np.isnan(cvd_corr) else None,
        "rule_fires": rule_diff,
        "label_dist": {
            "proxy": proxy_labels,
            "real":  real_labels,
        },
        "expectancy_r": {
            "horizon_bars": of_cfg.OF_FORWARD_BARS.get(tf, 1),
            "proxy": proxy_exp,
            "real":  real_exp,
        },
        "notes": notes,
    }

    out_path = out_dir / f"realflow_comparison_{symbol}_{tf}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    summary["_output_path"] = str(out_path)
    return summary


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Proxy vs real-flow comparison.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    ap.add_argument("--diagnostic", action="store_true",
                    help="Write Phase 1B diagnostic JSON instead of comparison.")
    args = ap.parse_args()
    if args.diagnostic:
        out = diagnose(args.symbol, args.tf)
    else:
        out = compare(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
