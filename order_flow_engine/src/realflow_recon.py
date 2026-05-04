"""
Phase 1G volume reconciliation investigation — diagnostic only.

Loads joined ESM6@15m frames from realflow_compare's pipeline, then
analyzes per-bar `real_total / cache_volume` ratio to find why ~half of
joined bars mismatch >5% even though the median is at parity.

Reads:
  outputs/order_flow/realflow_diagnostic_<sym>_<tf>.json (not required)
  order_flow_engine/data/processed/{sym}_{tf}_live.parquet
  order_flow_engine/data/processed/{sym}_{tf}_realflow_history.parquet
  Databento OHLCV cache (via data_loader.fetch_ohlcv)

Writes:
  outputs/order_flow/realflow_volume_recon_<sym>_<tf>.json
  outputs/order_flow/realflow_volume_recon_<sym>_<tf>.md

No rule / threshold / model changes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realflow_compare as rfc
from order_flow_engine.src import realflow_loader


def _is_rth(ts: pd.Timestamp) -> bool:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    minutes = ts.hour * 60 + ts.minute
    return (rfc.RTH_START_HOUR * 60 + rfc.RTH_START_MIN) <= minutes < (rfc.RTH_END_HOUR * 60)


def _build_recon_frame(symbol: str, tf: str) -> pd.DataFrame:
    """Joined frame with ratio + per-bar source label."""
    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)

    # Identify which bars come from live vs history. Live precedence is the
    # merge rule, so a ts present in the live file (or 1m-resampled live)
    # is "live"; otherwise "historical".
    live_p = realflow_loader._live_path(symbol, tf)
    live_idx: pd.DatetimeIndex
    if live_p.exists():
        df_live = pd.read_parquet(live_p)
        if not isinstance(df_live.index, pd.DatetimeIndex):
            df_live.index = pd.to_datetime(df_live.index, utc=True)
        elif df_live.index.tz is None:
            df_live.index = df_live.index.tz_localize("UTC")
        live_idx = df_live.index
    else:
        # 1m resample fallback — recompute the resampled index for membership.
        one_min_p = realflow_loader._live_path(symbol, "1m")
        if one_min_p.exists():
            df_1m = pd.read_parquet(one_min_p)
            if not isinstance(df_1m.index, pd.DatetimeIndex):
                df_1m.index = pd.to_datetime(df_1m.index, utc=True)
            elif df_1m.index.tz is None:
                df_1m.index = df_1m.index.tz_localize("UTC")
            df_resampled = realflow_loader.resample_to_tf(df_1m, tf)
            live_idx = df_resampled.index
        else:
            live_idx = pd.DatetimeIndex([], tz="UTC")

    rows = []
    for ts in common:
        cache_vol = float(raw.loc[ts, "Volume"]) if ts in raw.index else float("nan")
        real_buy_v  = real.loc[ts, "buy_vol_real"]
        real_sell_v = real.loc[ts, "sell_vol_real"]
        real_buy  = float(real_buy_v)  if pd.notna(real_buy_v)  else None
        real_sell = float(real_sell_v) if pd.notna(real_sell_v) else None
        if real_buy is None or real_sell is None:
            continue
        real_total = real_buy + real_sell
        if not (cache_vol and cache_vol > 0):
            continue
        ratio = real_total / cache_vol
        source = "live" if ts in live_idx else "historical"
        rows.append({
            "ts":          ts,
            "ratio":       ratio,
            "abs_dev":     abs(ratio - 1.0),
            "cache_vol":   cache_vol,
            "real_total":  real_total,
            "real_buy":    real_buy,
            "real_sell":   real_sell,
            "session":     "RTH" if _is_rth(ts) else "ETH",
            "source":      source,
            "hour_utc":    int(ts.hour),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.set_index("ts").sort_index()
    return df


# ── analyses ────────────────────────────────────────────────────────────────

def _q(s: pd.Series, q: float) -> float:
    return round(float(s.quantile(q)), 4)


def _analyze_distribution(df: pd.DataFrame) -> dict:
    s = df["abs_dev"]
    n = len(df)
    return {
        "n_bars": int(n),
        "abs_dev_quantiles": {
            "p50": _q(s, 0.50), "p75": _q(s, 0.75), "p90": _q(s, 0.90),
            "p95": _q(s, 0.95), "p99": _q(s, 0.99),
        },
        "fraction_above": {
            "5pct":  round(float((s >= 0.05).mean()), 4),
            "10pct": round(float((s >= 0.10).mean()), 4),
            "20pct": round(float((s >= 0.20).mean()), 4),
            "50pct": round(float((s >= 0.50).mean()), 4),
        },
        "count_above": {
            "5pct":  int((s >= 0.05).sum()),
            "10pct": int((s >= 0.10).sum()),
            "20pct": int((s >= 0.20).sum()),
            "50pct": int((s >= 0.50).sum()),
        },
    }


def _analyze_by_session(df: pd.DataFrame) -> dict:
    out: dict[str, dict] = {}
    for sess in ("RTH", "ETH"):
        sub = df[df["session"] == sess]
        if sub.empty:
            out[sess] = {"n_bars": 0}
            continue
        out[sess] = {
            "n_bars":            int(len(sub)),
            "ratio_median":      round(float(sub["ratio"].median()), 4),
            "abs_dev_median":    round(float(sub["abs_dev"].median()), 4),
            "abs_dev_p95":       _q(sub["abs_dev"], 0.95),
            "mismatch_5pct":     round(float((sub["abs_dev"] >= 0.05).mean()), 4),
            "mismatch_20pct":    round(float((sub["abs_dev"] >= 0.20).mean()), 4),
        }
    return out


def _analyze_direction(df: pd.DataFrame) -> dict:
    high = df[df["ratio"] > 1.05]
    low  = df[df["ratio"] < 0.95]
    return {
        "high_side": {
            "n_bars": int(len(high)),
            "fraction_of_total": round(float(len(high) / len(df)), 4) if len(df) else 0.0,
            "median_ratio": round(float(high["ratio"].median()), 4) if len(high) else None,
            "max_ratio":    round(float(high["ratio"].max()), 4)    if len(high) else None,
        },
        "low_side": {
            "n_bars": int(len(low)),
            "fraction_of_total": round(float(len(low) / len(df)), 4) if len(df) else 0.0,
            "median_ratio": round(float(low["ratio"].median()), 4) if len(low) else None,
            "min_ratio":    round(float(low["ratio"].min()), 4)    if len(low) else None,
        },
        "asymmetry":
            "high-skewed" if len(high) > 1.2 * len(low) else
            "low-skewed"  if len(low)  > 1.2 * len(high) else
            "balanced",
    }


def _analyze_hour_clustering(df: pd.DataFrame) -> dict:
    by_hour = df.groupby("hour_utc")
    out: dict[int, dict] = {}
    for hour, sub in by_hour:
        out[int(hour)] = {
            "n_bars":         int(len(sub)),
            "abs_dev_median": round(float(sub["abs_dev"].median()), 4),
            "mismatch_5pct":  round(float((sub["abs_dev"] >= 0.05).mean()), 4),
            "mismatch_20pct": round(float((sub["abs_dev"] >= 0.20).mean()), 4),
        }
    return out


def _top_worst(df: pd.DataFrame, n: int = 20) -> list[dict]:
    worst = df.sort_values("abs_dev", ascending=False).head(n)
    return [
        {
            "ts":         str(ts),
            "ratio":      round(float(r["ratio"]), 4),
            "abs_dev":    round(float(r["abs_dev"]), 4),
            "cache_vol":  round(float(r["cache_vol"]), 2),
            "real_total": round(float(r["real_total"]), 2),
            "session":    r["session"],
            "source":     r["source"],
            "hour_utc":   int(r["hour_utc"]),
        }
        for ts, r in worst.iterrows()
    ]


def _analyze_by_source(df: pd.DataFrame) -> dict:
    out: dict[str, dict] = {}
    for src in ("historical", "live"):
        sub = df[df["source"] == src]
        if sub.empty:
            out[src] = {"n_bars": 0}
            continue
        out[src] = {
            "n_bars":         int(len(sub)),
            "ratio_median":   round(float(sub["ratio"].median()), 4),
            "abs_dev_median": round(float(sub["abs_dev"].median()), 4),
            "abs_dev_p95":    _q(sub["abs_dev"], 0.95),
            "mismatch_5pct":  round(float((sub["abs_dev"] >= 0.05).mean()), 4),
            "mismatch_20pct": round(float((sub["abs_dev"] >= 0.20).mean()), 4),
        }
    return out


def _analyze_volume_buckets(df: pd.DataFrame) -> dict:
    # Tercile cache volumes.
    qs = df["cache_vol"].quantile([0.33, 0.66]).tolist()
    edges = [-np.inf, qs[0], qs[1], np.inf]
    labels = ["low", "mid", "high"]
    df = df.copy()
    df["bucket"] = pd.cut(df["cache_vol"], bins=edges, labels=labels)
    out: dict[str, dict] = {
        "tercile_edges": [round(float(qs[0]), 2), round(float(qs[1]), 2)],
    }
    for b in labels:
        sub = df[df["bucket"] == b]
        if sub.empty:
            out[b] = {"n_bars": 0}
            continue
        out[b] = {
            "n_bars":          int(len(sub)),
            "cache_vol_range": [round(float(sub["cache_vol"].min()), 2),
                                round(float(sub["cache_vol"].max()), 2)],
            "abs_dev_median":  round(float(sub["abs_dev"].median()), 4),
            "abs_dev_p95":     _q(sub["abs_dev"], 0.95),
            "mismatch_5pct":   round(float((sub["abs_dev"] >= 0.05).mean()), 4),
            "mismatch_20pct":  round(float((sub["abs_dev"] >= 0.20).mean()), 4),
        }
    return out


def _analyze_bar_boundary(df: pd.DataFrame) -> dict:
    """
    Detect 1-bar shift/split signature: when bar t mismatches high (ratio > 1.10)
    AND adjacent bar t±1 mismatches low (ratio < 0.90), a piece of one bar's
    flow may have leaked into the neighbor.
    """
    if len(df) < 3:
        return {"n_bars": int(len(df)), "note": "too few bars"}
    s = df["ratio"]
    prev_low_curr_high  = ((s.shift(1) < 0.90) & (s > 1.10)).sum()
    prev_high_curr_low  = ((s.shift(1) > 1.10) & (s < 0.90)).sum()
    next_low_curr_high  = ((s.shift(-1) < 0.90) & (s > 1.10)).sum()
    next_high_curr_low  = ((s.shift(-1) > 1.10) & (s < 0.90)).sum()
    suspect_pairs = int(prev_low_curr_high + prev_high_curr_low)
    return {
        "n_bars":        int(len(df)),
        "shift_signature": {
            "prev_low_curr_high":  int(prev_low_curr_high),
            "prev_high_curr_low":  int(prev_high_curr_low),
            "next_low_curr_high":  int(next_low_curr_high),
            "next_high_curr_low":  int(next_high_curr_low),
        },
        "suspect_consecutive_pairs": suspect_pairs,
        "interpretation":
            "high count of opposite-sign consecutive pairs hints at "
            "intra-period flow leakage between adjacent bars; low count "
            "rules out the 1-bar-shift hypothesis.",
    }


# ── orchestrator + writers ──────────────────────────────────────────────────

def run(symbol: str, tf: str, output_dir: Path | None = None) -> dict:
    out_dir = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _build_recon_frame(symbol, tf)
    if df.empty:
        report = {
            "symbol": symbol, "timeframe": tf,
            "error": "no joined bars with valid ratio",
        }
        (out_dir / f"realflow_volume_recon_{symbol}_{tf}.json").write_text(
            json.dumps(report, indent=2, default=str)
        )
        return report

    report = {
        "symbol":    symbol,
        "timeframe": tf,
        "n_bars":    int(len(df)),
        "window": {
            "start": str(df.index.min()),
            "end":   str(df.index.max()),
        },
        "ratio_summary": {
            "median": round(float(df["ratio"].median()), 4),
            "iqr":    [round(float(df["ratio"].quantile(0.25)), 4),
                       round(float(df["ratio"].quantile(0.75)), 4)],
        },
        "distribution":   _analyze_distribution(df),
        "by_session":     _analyze_by_session(df),
        "direction":      _analyze_direction(df),
        "hour_utc":       _analyze_hour_clustering(df),
        "top_worst_20":   _top_worst(df, n=20),
        "by_source":      _analyze_by_source(df),
        "volume_buckets": _analyze_volume_buckets(df),
        "bar_boundary":   _analyze_bar_boundary(df),
    }

    json_path = out_dir / f"realflow_volume_recon_{symbol}_{tf}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))

    md_path = out_dir / f"realflow_volume_recon_{symbol}_{tf}.md"
    md_path.write_text(_render_md(report))

    report["_json_path"] = str(json_path)
    report["_md_path"]   = str(md_path)
    return report


def _render_md(r: dict) -> str:
    L: list[str] = []
    L.append(f"# Volume Reconciliation — {r['symbol']} @ {r['timeframe']}\n")
    L.append(f"- Bars analyzed: **{r['n_bars']}**")
    L.append(f"- Window: `{r['window']['start']}` → `{r['window']['end']}`")
    L.append(f"- ratio median: **{r['ratio_summary']['median']}** "
             f"· IQR {r['ratio_summary']['iqr']}\n")

    d = r["distribution"]
    L.append("## 1. Mismatch distribution")
    L.append(f"- abs_dev quantiles: p50={d['abs_dev_quantiles']['p50']} · "
             f"p75={d['abs_dev_quantiles']['p75']} · p90={d['abs_dev_quantiles']['p90']} · "
             f"p95={d['abs_dev_quantiles']['p95']} · p99={d['abs_dev_quantiles']['p99']}")
    L.append(f"- fraction ≥5%: {d['fraction_above']['5pct']} ({d['count_above']['5pct']} bars)")
    L.append(f"- fraction ≥10%: {d['fraction_above']['10pct']} ({d['count_above']['10pct']} bars)")
    L.append(f"- fraction ≥20%: {d['fraction_above']['20pct']} ({d['count_above']['20pct']} bars)")
    L.append(f"- fraction ≥50%: {d['fraction_above']['50pct']} ({d['count_above']['50pct']} bars)\n")

    L.append("## 2. By session")
    for sess in ("RTH", "ETH"):
        s = r["by_session"].get(sess, {})
        if s.get("n_bars"):
            L.append(f"- **{sess}** (n={s['n_bars']}): ratio_med={s['ratio_median']} · "
                     f"abs_dev_med={s['abs_dev_median']} · p95={s['abs_dev_p95']} · "
                     f"≥5%={s['mismatch_5pct']} · ≥20%={s['mismatch_20pct']}")
    L.append("")

    dirn = r["direction"]
    L.append("## 3. Direction of mismatch")
    L.append(f"- high-side (ratio > 1.05): n={dirn['high_side']['n_bars']} "
             f"({dirn['high_side']['fraction_of_total']}) · median={dirn['high_side']['median_ratio']} "
             f"· max={dirn['high_side']['max_ratio']}")
    L.append(f"- low-side (ratio < 0.95): n={dirn['low_side']['n_bars']} "
             f"({dirn['low_side']['fraction_of_total']}) · median={dirn['low_side']['median_ratio']} "
             f"· min={dirn['low_side']['min_ratio']}")
    L.append(f"- asymmetry: **{dirn['asymmetry']}**\n")

    L.append("## 4. By hour UTC (mismatch ≥5% rate)")
    L.append("| hour | n | abs_dev_med | ≥5% | ≥20% |")
    L.append("|------|---|-------------|-----|------|")
    for hour in sorted(r["hour_utc"].keys()):
        h = r["hour_utc"][hour]
        L.append(f"| {hour:02d} | {h['n_bars']} | {h['abs_dev_median']} | "
                 f"{h['mismatch_5pct']} | {h['mismatch_20pct']} |")
    L.append("")

    L.append("## 5. By source")
    for src in ("historical", "live"):
        s = r["by_source"].get(src, {})
        if s.get("n_bars"):
            L.append(f"- **{src}** (n={s['n_bars']}): ratio_med={s['ratio_median']} · "
                     f"abs_dev_med={s['abs_dev_median']} · p95={s['abs_dev_p95']} · "
                     f"≥5%={s['mismatch_5pct']} · ≥20%={s['mismatch_20pct']}")
    L.append("")

    vb = r["volume_buckets"]
    L.append("## 6. Volume buckets (terciles by cache_volume)")
    L.append(f"- tercile edges: {vb['tercile_edges']}")
    for b in ("low", "mid", "high"):
        s = vb.get(b, {})
        if s.get("n_bars"):
            L.append(f"- **{b}** (n={s['n_bars']}, range {s['cache_vol_range']}): "
                     f"abs_dev_med={s['abs_dev_median']} · p95={s['abs_dev_p95']} · "
                     f"≥5%={s['mismatch_5pct']} · ≥20%={s['mismatch_20pct']}")
    L.append("")

    bb = r["bar_boundary"]
    L.append("## 7. Bar-boundary check")
    L.append(f"- prev_low_curr_high: {bb['shift_signature']['prev_low_curr_high']}")
    L.append(f"- prev_high_curr_low: {bb['shift_signature']['prev_high_curr_low']}")
    L.append(f"- suspect consecutive pairs (either direction): "
             f"**{bb['suspect_consecutive_pairs']}**")
    L.append(f"- {bb['interpretation']}\n")

    L.append("## Top 20 worst mismatch bars")
    L.append("| ts | ratio | abs_dev | cache_vol | real_total | session | source | hr |")
    L.append("|----|-------|---------|-----------|------------|---------|--------|----|")
    for w in r["top_worst_20"]:
        L.append(f"| {w['ts']} | {w['ratio']} | {w['abs_dev']} | "
                 f"{w['cache_vol']} | {w['real_total']} | {w['session']} | "
                 f"{w['source']} | {w['hour_utc']} |")

    return "\n".join(L) + "\n"


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Phase 1G volume recon investigation.")
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf",     default="15m")
    args = ap.parse_args()
    out = run(args.symbol, args.tf)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
