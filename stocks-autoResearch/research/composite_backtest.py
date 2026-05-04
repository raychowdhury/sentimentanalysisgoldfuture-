"""
Composite SPX score backtest (item 4).

For every historical date in the cached feature matrix:
  1. Run production pooled model → mean P(up), breadth, sectors
  2. Compute composite components that are knowable historically
     (model_signal, breadth_signal, real_yield, sector_agree, vix_regime)
     — stock_sent skipped (no historical per-ticker sentiment).
  3. Apply weights, get score
  4. Look up actual SPY next-day return
  5. Bucket scores → tabulate hit-rate + mean fwd return per bucket

Writes outputs/stocks/_backtest_composite.json. Used by:
  - fit_composite_weights.py (item 2) for component weights regression
  - calibration.py (item 3) for reliability diagram
  - dashboard for "track record" panel

Usage:  python -m research.composite_backtest
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import registry
from research.macro_overlays import (
    credit_ratio_series,
    spy_trend_series,
    vix_slope_series,
    turn_of_month_flag,
    day_of_week_feat,
    fomc_proximity,
    z_to_component,
)

# Optional FOMC history — hand-keyed list of meeting dates for proximity feature.
# Only used historically in the backtest. Live code reads from the event gate.
FOMC_DATES_HIST = [
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29",
]


def _spy_returns() -> tuple[pd.Series, pd.Series]:
    """Daily SPY t+1 return and cumulative t+1..t+5 return (both anchored at t)."""
    spy = yf.download("SPY", period="5y", progress=False,
                      auto_adjust=False, threads=False)
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.get_level_values(0)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    close = spy["Close"]
    ret_1d = close.pct_change().shift(-1)
    # 5-day forward cumulative return: close[t+5]/close[t] - 1
    ret_5d = (close.shift(-5) / close - 1.0)
    ret_1d.name = "spy_next_ret"
    ret_5d.name = "spy_fwd_5d"
    return ret_1d, ret_5d


def _vix_size_mult(vix: float) -> float:
    if pd.isna(vix): return 1.0
    if vix >= 35: return 0.0
    if vix >= 25: return 0.5
    return 1.0


def _components(group: pd.DataFrame, sector_col: str = "sector") -> dict:
    proba = group["proba"].to_numpy()
    mean_p  = float(proba.mean())
    breadth = float((proba >= 0.5).mean())
    sectors = group.groupby(sector_col, observed=True)["proba"].mean()
    sec_long  = int((sectors >= 0.5).sum())
    sec_short = len(sectors) - sec_long
    sector_sig = (sec_long - sec_short) / len(sectors) * 100 if len(sectors) else 0.0
    real10y = group["real10y"].iloc[0] if "real10y" in group.columns else None
    return {
        "model_signal":   max(-100, min(100, (mean_p  - 0.5) * 200)),
        "sector_agree":   sector_sig,
        "real10y":        float(real10y) if real10y is not None and not pd.isna(real10y) else None,
        "vix":            float(group["vix"].iloc[0]) if "vix" in group.columns else None,
        "mean_p":         mean_p,
        "breadth":        breadth,
        "n_tickers":      len(group),
    }


def main() -> None:
    df = load_cached_frame()
    if df is None:
        raise SystemExit("no cached feature matrix — run data_agent first")

    meta = registry.production_metadata()
    if meta is None:
        raise SystemExit("no production model")
    model, _ = registry.load(meta.version)

    print(f"backtesting composite on {df['date'].nunique()} dates "
          f"× {df['ticker'].nunique()} tickers (model {meta.version})…")

    # Predict in one shot, then split by date
    df = df.copy()
    df["proba"] = model.predict_proba(df[model.features])

    # Real10y_chg_5d: compute from real10y series per date
    daily_real10y = df.groupby("date")["real10y"].first().sort_index()
    real10y_chg5  = daily_real10y.diff(5)

    spy, spy5 = _spy_returns()

    # Tier-1/3 cross-asset + term-slope series.
    dates = pd.to_datetime(df["date"].unique())
    dmin, dmax = dates.min().date(), dates.max().date()
    print(f"fetching trend + credit + vix_slope series {dmin}..{dmax}…")
    trend_series     = spy_trend_series(dmin, dmax)
    credit_series    = credit_ratio_series(dmin, dmax)
    vix_slope_series_ = vix_slope_series(dmin, dmax)
    # Rolling z-score of vix_slope for stationarity.
    if not vix_slope_series_.empty:
        mu = vix_slope_series_.rolling(60).mean()
        sd = vix_slope_series_.rolling(60).std()
        vix_slope_z = ((vix_slope_series_ - mu) / sd).clip(-3, 3)
    else:
        vix_slope_z = pd.Series(dtype=float)

    # FOMC history → proximity feature
    fomc_dates = [pd.Timestamp(s) for s in FOMC_DATES_HIST]

    rows = []
    for d, g in df.groupby("date", observed=True):
        comps = _components(g)
        chg5 = real10y_chg5.get(d, np.nan)
        d_ts = pd.Timestamp(d)
        trend_z   = float(trend_series.get(d_ts, np.nan))
        credit_z  = float(credit_series.get(d_ts, np.nan))
        vslope_z  = float(vix_slope_z.get(d_ts, np.nan)) if not vix_slope_z.empty else np.nan
        # FOMC proximity: days to next FOMC meeting from d_ts
        fomc_days = min(
            ((fd - d_ts).days for fd in fomc_dates if fd >= d_ts),
            default=None,
        )
        d_py = pd.Timestamp(d).date()
        components = {
            "model_signal":   round(comps["model_signal"], 2),
            "sector_agree":   round(comps["sector_agree"], 2),
            "real_yield":     round(max(-100, min(100, -float(chg5) * 1000)), 2)
                              if not pd.isna(chg5) else 0.0,
            "trend":          round(z_to_component(trend_z), 2),
            "credit":         round(z_to_component(credit_z), 2),
            # Tier-3 additions
            "vix_slope":      round(z_to_component(vslope_z), 2),
            "fomc_prox":      round(fomc_proximity(fomc_days) * 100.0, 2),
            "tom":            round(turn_of_month_flag(d_py) * 100.0, 2),
            "dow":            round(day_of_week_feat(d_py)   * 100.0, 2),
        }
        # Default weights matching production defaults (stock_sent missing in backtest)
        weights = {
            "model_signal": 0.40,
            "sector_agree": 0.15,
            "real_yield":   0.10,
            "trend":        0.15,
            "credit":       0.10,
            "vix_slope":    0.05,
            "fomc_prox":    0.02,
            "tom":          0.02,
            "dow":          0.01,
        }
        wsum = sum(weights.values())
        weights = {k: v / wsum for k, v in weights.items()}
        score = sum(components[k] * weights[k] for k in components)
        score *= _vix_size_mult(comps["vix"])
        score = max(-100, min(100, score))

        nxt_ret  = float(spy.get(d, np.nan))  if not pd.isna(spy.get(d, np.nan))  else None
        fwd5_ret = float(spy5.get(d, np.nan)) if not pd.isna(spy5.get(d, np.nan)) else None
        rows.append({
            "date":          str(pd.Timestamp(d).date()),
            "score":         round(float(score), 2),
            "components":    components,
            "mean_p":        round(comps["mean_p"], 4),
            "breadth":       round(comps["breadth"], 4),
            "vix":           comps["vix"],
            "spy_next_ret":  round(nxt_ret,  6) if nxt_ret  is not None else None,
            "spy_fwd_5d":    round(fwd5_ret, 6) if fwd5_ret is not None else None,
        })

    out_df = pd.DataFrame(rows)
    out_df = out_df.dropna(subset=["spy_next_ret"])

    # Bucket score → hit-rate + mean SPY return
    bins   = [-101, -50, -30, -10, 10, 30, 50, 101]
    labels = ["<-50", "-50..-30", "-30..-10", "-10..10", "10..30", "30..50", ">50"]
    out_df["bucket"] = pd.cut(out_df["score"], bins=bins, labels=labels)
    summary = (
        out_df.groupby("bucket", observed=True)
        .agg(n=("spy_next_ret", "size"),
             hit_rate=("spy_next_ret", lambda s: float((s > 0).mean())),
             mean_ret=("spy_next_ret", "mean"),
             median_ret=("spy_next_ret", "median"))
        .round(4)
        .reset_index()
    )

    # Overall summary
    pos_mask = out_df["score"] >= 10
    neg_mask = out_df["score"] <= -10
    overall = {
        "n_days":           int(len(out_df)),
        "long_signals":     int(pos_mask.sum()),
        "short_signals":    int(neg_mask.sum()),
        "long_hit_rate":    round(float((out_df.loc[pos_mask, "spy_next_ret"] > 0).mean()), 4)
                            if pos_mask.any() else None,
        "short_hit_rate":   round(float((out_df.loc[neg_mask, "spy_next_ret"] < 0).mean()), 4)
                            if neg_mask.any() else None,
        "long_mean_ret":    round(float(out_df.loc[pos_mask, "spy_next_ret"].mean()), 6)
                            if pos_mask.any() else None,
        "short_mean_ret":   round(float(out_df.loc[neg_mask, "spy_next_ret"].mean()), 6)
                            if neg_mask.any() else None,
    }

    print("\nbucket summary:")
    print(summary.to_string(index=False))
    print("\noverall:")
    for k, v in overall.items():
        print(f"  {k:>15s}: {v}")

    out_path = settings.root_dir.parent / "outputs" / "stocks" / "_backtest_composite.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "model_version":  meta.version,
        "generated_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary_buckets": summary.to_dict(orient="records"),
        "overall":         overall,
    }, indent=2, default=str))
    print(f"\nwrote {out_path}")

    # Companion long-format CSV for downstream regression / calibration
    csv_path = settings.root_dir.parent / "outputs" / "stocks" / "_backtest_composite.csv"
    out_df.drop(columns=["bucket"]).to_csv(csv_path, index=False)
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
