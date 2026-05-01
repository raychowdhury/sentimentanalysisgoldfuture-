"""
Phase 2D Stage 1 — read-only R1/R2 outcome tracker.

For each R1/R2 fire on the real-flow path (bar_proxy_mode == 0), record a
PENDING row. Once `now_utc - settle_eta >= grace`, score the outcome from
the joined frame's forward bars and append a SETTLED row to JSONL.

Hard invariants:
  * NEVER imports predictor / alert_engine / ingest / ml_engine.
  * NEVER modifies threshold or rule config.
  * NEVER places trades.
  * Outcomes JSONL is append-only.
  * settled rows are deduped by deterministic signal_id.
  * idempotent: running settle_pass twice produces the same JSONL.

Outputs (all under outputs/order_flow/):
  realflow_outcomes_pending_<sym>_<tf>.json   — current pending list
  realflow_outcomes_<sym>_<tf>.jsonl          — append-only settled rows
  realflow_outcomes_summary_<sym>_<tf>.json   — rebuilt aggregate per pass
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realflow_compare as rfc
from order_flow_engine.src.realflow_compare import NY_TZ, TF_MINUTES


TARGET_RULES = ("r1_buyer_down", "r2_seller_up")
HORIZON_GRACE_S = 30   # wait this many seconds beyond the horizon ETA

# Phase 2A test baseline (from outputs/order_flow/realflow_threshold_sweep_*).
# Used only for vs_baseline drift display in the summary; never gates anything.
BASELINE_TEST_MEAN_R = {
    "r1_buyer_down": 1.18,
    "r2_seller_up":  0.75,
}


# ── path helpers ────────────────────────────────────────────────────────────

def _out_dir() -> Path:
    return Path(of_cfg.OF_OUTPUT_DIR)


def _pending_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_outcomes_pending_{symbol}_{tf}.json"


def _outcomes_jsonl_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_outcomes_{symbol}_{tf}.jsonl"


def _summary_path(symbol: str, tf: str) -> Path:
    return _out_dir() / f"realflow_outcomes_summary_{symbol}_{tf}.json"


# ── small utilities ─────────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _signal_id(symbol: str, tf: str, fire_ts: pd.Timestamp, rule: str) -> str:
    """Deterministic ID — same fire always yields the same ID."""
    if fire_ts.tzinfo is None:
        fire_ts = fire_ts.tz_localize("UTC")
    ts_str = fire_ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    short_rule = rule.split("_")[0]   # r1 / r2
    return f"{symbol}_{tf}_{ts_str}_{short_rule}"


def _direction_for_rule(rule: str) -> int:
    return -1 if rule == "r1_buyer_down" else +1


def _session_bucket(ts: pd.Timestamp) -> str:
    """RTH_open 13:30-14:00 / RTH_mid 14:00-19:30 / RTH_close 19:30-20:00 / ETH."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    minutes = ts.hour * 60 + ts.minute
    if (13 * 60 + 30) <= minutes < (14 * 60):
        return "RTH_open"
    if (14 * 60) <= minutes < (19 * 60 + 30):
        return "RTH_mid"
    if (19 * 60 + 30) <= minutes < (20 * 60):
        return "RTH_close"
    return "ETH"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _settled_ids(jsonl_path: Path) -> set[str]:
    return {r.get("signal_id") for r in _read_jsonl(jsonl_path)
            if r.get("signal_id")}


# ── outcome scoring ─────────────────────────────────────────────────────────

def _score_outcome(
    joined: pd.DataFrame,
    fire_idx: int,
    horizon: int,
    direction: int,
    atr: float,
    entry: float,
) -> dict | None:
    """
    Score a settled outcome from forward bars. Returns None when the window
    is incomplete (forward bars not yet present in the joined frame).

    Window = bars [fire_idx+1 .. fire_idx+horizon]. fwd_close is the close
    of the last bar in that window.
    """
    if fire_idx + horizon >= len(joined):
        return None
    window = joined.iloc[fire_idx + 1: fire_idx + 1 + horizon]
    if len(window) < horizon:
        return None
    if not (atr and atr > 0):
        return None

    fwd_close = float(joined.iloc[fire_idx + horizon]["Close"])
    fwd_high  = float(window["High"].max())
    fwd_low   = float(window["Low"].min())

    fwd_r_signed = ((fwd_close - entry) * direction) / atr

    if direction > 0:
        mfe = (fwd_high - entry) / atr
        mae = (fwd_low  - entry) / atr
    else:
        mfe = (entry - fwd_low)  / atr
        mae = (entry - fwd_high) / atr

    # First-touch hit/stop within window.
    hit_1r = False
    stopped = False
    for i in range(len(window)):
        bar = window.iloc[i]
        if direction > 0:
            up_r = (float(bar["High"]) - entry) / atr
            dn_r = (float(bar["Low"])  - entry) / atr
        else:
            up_r = (entry - float(bar["Low"]))  / atr
            dn_r = (entry - float(bar["High"])) / atr
        if up_r >= 1.0 and not stopped:
            hit_1r = True
            break
        if dn_r <= -1.0 and not hit_1r:
            stopped = True
            break

    if fwd_r_signed > 0:
        outcome_label = "win"
    elif fwd_r_signed < 0:
        outcome_label = "loss"
    else:
        outcome_label = "flat"

    return {
        "fwd_close_at_horizon": round(fwd_close, 4),
        "fwd_high_at_horizon":  round(fwd_high,  4),
        "fwd_low_at_horizon":   round(fwd_low,   4),
        "fwd_r_signed":         round(float(fwd_r_signed), 4),
        "mae_r":                round(float(mae), 4),
        "mfe_r":                round(float(mfe), 4),
        "outcome":              outcome_label,
        "hit_1r":               bool(hit_1r),
        "stopped_out_1atr":     bool(stopped),
    }


# ── main settle pass ────────────────────────────────────────────────────────

def _build_pending_row(
    symbol: str, tf: str, rule: str, fire_ts: pd.Timestamp,
    direction: int, entry: float, atr: float | None,
    delta_ratio: float, cvd_z: float,
    horizon: int, settle_eta: pd.Timestamp, now_utc: pd.Timestamp,
    mode: str = "unknown",
) -> dict:
    return {
        "signal_id":      _signal_id(symbol, tf, fire_ts, rule),
        "symbol":         symbol,
        "tf":             tf,
        "rule":           rule,
        "fire_ts_utc":    fire_ts.tz_convert("UTC").isoformat(),
        "fire_ts_ny":     fire_ts.tz_convert(NY_TZ).isoformat(),
        "direction":      int(direction),
        "session":        _session_bucket(fire_ts),
        "entry_close":    round(float(entry), 4),
        "atr":            round(float(atr), 4) if atr is not None else None,
        "delta_ratio":    round(float(delta_ratio), 4),
        "cvd_z":          round(float(cvd_z), 4),
        "threshold_path": "real",
        "mode":           mode,
        "horizon_bars":   int(horizon),
        "settle_eta_utc": settle_eta.tz_convert("UTC").isoformat(),
        "discovered_at":  now_utc.isoformat(),
    }


def _row_mode(row) -> str:
    """Map the per-bar `source` column to outcome mode."""
    src = row.get("source") if hasattr(row, "get") else None
    if src is None or (hasattr(pd, "isna") and pd.isna(src)):
        return "unknown"
    s = str(src)
    if s == "live":
        return "live"
    if s == "historical_realflow_tick_rule":
        return "historical"
    return s  # passthrough any other tag


def _build_mode_index(symbol: str, tf: str) -> dict:
    """
    Map ts → mode by peeking the live + history parquets directly. Live wins
    on overlap (mirrors realflow_loader._merge_history_and_live behavior).
    """
    from order_flow_engine.src import realflow_loader as rfl
    out: dict = {}
    hist_p = rfl._history_path(symbol, tf)
    if hist_p.exists():
        try:
            df = pd.read_parquet(hist_p)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            for ts in df.index:
                out[ts] = "historical"
        except Exception:
            pass
    live_p = rfl._live_path(symbol, tf)
    if not live_p.exists():
        # 1m-resampled fallback path — treat resampled bars as live.
        one_min_p = rfl._live_path(symbol, "1m")
        if one_min_p.exists():
            try:
                df_1m = pd.read_parquet(one_min_p)
                if df_1m.index.tz is None:
                    df_1m.index = df_1m.index.tz_localize("UTC")
                else:
                    df_1m.index = df_1m.index.tz_convert("UTC")
                resampled = rfl.resample_to_tf(df_1m, tf)
                for ts in resampled.index:
                    out[ts] = "live"
            except Exception:
                pass
    else:
        try:
            df = pd.read_parquet(live_p)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            for ts in df.index:
                out[ts] = "live"   # live overrides historical on overlap
        except Exception:
            pass
    return out


def settle_pass(symbol: str = "ESM6", tf: str = "15m") -> dict:
    """
    Discover R1/R2 fires on the real-flow path, settle those whose horizon
    has elapsed, leave the rest pending. Idempotent.
    """
    horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
    tf_min  = TF_MINUTES.get(tf, 15)
    now_utc = pd.Timestamp.now(tz="UTC")

    # Load joined frame (read-only). Same source as diagnose / sweep.
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)
    df = real_feat.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    settled_ids = _settled_ids(_outcomes_jsonl_path(symbol, tf))
    mode_index  = _build_mode_index(symbol, tf)

    pending: list[dict] = []
    new_settled: list[dict] = []

    for rule in TARGET_RULES:
        if rule not in df.columns:
            continue
        fires_mask = df[rule].fillna(False).astype(bool)
        if "bar_proxy_mode" in df.columns:
            real_mask = df["bar_proxy_mode"].fillna(1).astype(int) == 0
            fires_mask = fires_mask & real_mask

        for fire_ts in df.index[fires_mask]:
            sid = _signal_id(symbol, tf, fire_ts, rule)
            if sid in settled_ids:
                continue

            fire_idx = df.index.get_loc(fire_ts)
            row = df.loc[fire_ts]
            entry = float(row["Close"])
            atr_v = float(row["atr"]) if pd.notna(row["atr"]) else None
            direction = _direction_for_rule(rule)
            settle_eta = fire_ts + pd.Timedelta(minutes=tf_min * horizon)
            delta_ratio = float(row.get("delta_ratio", 0.0))
            cvd_z       = float(row.get("cvd_z",       0.0))

            pending_row = _build_pending_row(
                symbol, tf, rule, fire_ts, direction,
                entry, atr_v, delta_ratio, cvd_z,
                horizon, settle_eta, now_utc,
                mode=mode_index.get(fire_ts, _row_mode(row)),
            )

            # Settle gate — enough wall-clock time elapsed?
            if (now_utc - settle_eta).total_seconds() < HORIZON_GRACE_S:
                pending.append(pending_row)
                continue

            # And do we actually have enough forward bars in the frame?
            outcome = _score_outcome(df, fire_idx, horizon, direction, atr_v, entry) \
                if atr_v else None
            if outcome is None:
                pending.append(pending_row)
                continue

            new_settled.append({
                **pending_row,
                **outcome,
                "settled_at_utc": now_utc.isoformat(),
            })

    # Append-only JSONL — never overwrite.
    jsonl_path = _outcomes_jsonl_path(symbol, tf)
    if new_settled:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a") as f:
            for r in new_settled:
                f.write(json.dumps(r, default=str) + "\n")

    # Pending file — always rewritten (atomic).
    _atomic_write(_pending_path(symbol, tf),
                  json.dumps(pending, indent=2, default=str))

    # Summary — rebuilt from the full JSONL.
    all_settled = _read_jsonl(jsonl_path)
    summary = _build_summary(all_settled, pending, symbol, tf)
    _atomic_write(_summary_path(symbol, tf),
                  json.dumps(summary, indent=2, default=str))

    return {
        "symbol":          symbol,
        "timeframe":       tf,
        "n_pending":       len(pending),
        "n_new_settled":   len(new_settled),
        "n_total_settled": len(all_settled),
        "paths": {
            "pending":        str(_pending_path(symbol, tf)),
            "outcomes_jsonl": str(jsonl_path),
            "summary":        str(_summary_path(symbol, tf)),
        },
    }


# ── summary aggregation ─────────────────────────────────────────────────────

def _agg(rows: list[dict] | pd.DataFrame) -> dict:
    df = pd.DataFrame(rows) if not isinstance(rows, pd.DataFrame) else rows
    if df.empty:
        return {"n": 0}
    r = df["fwd_r_signed"]
    return {
        "n":         int(len(df)),
        "wins":      int((df["outcome"] == "win").sum()),
        "losses":    int((df["outcome"] == "loss").sum()),
        "flats":     int((df["outcome"] == "flat").sum()),
        "hit_rate":  round(float((df["outcome"] == "win").mean()), 4),
        "mean_r":    round(float(r.mean()), 4),
        "mae_r_med": round(float(df["mae_r"].median()), 4),
        "mfe_r_med": round(float(df["mfe_r"].median()), 4),
        "hit_1r_rate":         round(float(df["hit_1r"].mean()), 4),
        "stopped_out_rate":    round(float(df["stopped_out_1atr"].mean()), 4),
    }


def _sample_size_label(n: int) -> str:
    if n < 10:  return "cold"
    if n < 30:  return "early read"
    if n < 100: return "first check"
    if n < 200: return "better read"
    return "strong read"


def _vs_baseline(by_rule: dict) -> dict:
    out: dict = {}
    for rule, base_mr in BASELINE_TEST_MEAN_R.items():
        live = by_rule.get(rule, {}).get("mean_r")
        if live is None or base_mr in (0, None):
            out[rule] = {"baseline_mean_r": base_mr, "live_mean_r": live,
                         "retention_pct": None}
            continue
        out[rule] = {
            "baseline_mean_r": base_mr,
            "live_mean_r":     live,
            "retention_pct":   round(float(live / base_mr), 4),
        }
    return out


def _alerts_per_day(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    ts = pd.to_datetime(df["fire_ts_utc"])
    span_s = (ts.max() - ts.min()).total_seconds()
    if span_s <= 0:
        return float(len(df))
    return round(float(len(df) / (span_s / 86400.0)), 4)


def _build_summary(settled: list[dict], pending: list[dict],
                   symbol: str, tf: str) -> dict:
    now = pd.Timestamp.now(tz="UTC").isoformat()
    base = {
        "as_of_utc":   now,
        "symbol":      symbol,
        "tf":          tf,
        "n_pending":   len(pending),
        "n_settled":   len(settled),
        "sample_size_label": _sample_size_label(len(settled)),
    }
    if not settled:
        base.update({"by_rule": {}, "by_session": {},
                     "by_threshold_path": {}, "alerts_per_day": 0.0,
                     "vs_baseline": {}})
        return base
    df = pd.DataFrame(settled)
    by_rule    = {k: _agg(g) for k, g in df.groupby("rule")}
    by_session = {k: _agg(g) for k, g in df.groupby("session")}
    by_path    = {k: _agg(g) for k, g in df.groupby("threshold_path")}
    by_mode    = ({k: _agg(g) for k, g in df.groupby("mode")}
                  if "mode" in df.columns else {})
    base.update({
        "since":             str(df["fire_ts_utc"].min()),
        "by_rule":           by_rule,
        "by_session":        by_session,
        "by_threshold_path": by_path,
        "by_mode":           by_mode,
        "alerts_per_day":    _alerts_per_day(df),
        "vs_baseline":       _vs_baseline(by_rule),
    })
    return base


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 2D Stage 1 outcome tracker.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = settle_pass(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
