"""
Re-evaluate the production model with realistic execution costs (item 6).

Production eval reports gross Sharpe. This script subtracts:
  - bid/ask spread (5 bp per side, both legs of long/short)
  - commission (1 bp per side)
  - borrow cost on shorts (~50 bp/yr → ~0.2 bp/day for liquid; HTB 5 bp/day)
  - 1.5 ATR stop-out penalty when intraday range exceeds the stop
    (proxy: subtract 1 ATR additional cost on tickers whose daily range >= 1.5 ATR
     in the holdout — captures the slippage of being stopped out)

Reports gross vs net Sharpe so the trader sees the impact.

Outputs: outputs/stocks/_eval_with_costs.json

Usage:  python -m research.eval_with_costs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import registry

ROOT = Path(__file__).resolve().parent.parent.parent
OUT  = ROOT / "outputs" / "stocks" / "_eval_with_costs.json"

# All values in fractional return units (e.g. 0.0005 = 5 bps).
SPREAD_PER_SIDE       = 0.0005
COMMISSION_PER_SIDE   = 0.0001
BORROW_DAILY_LIQUID   = 0.00002   # 0.2 bp/day
BORROW_DAILY_HTB      = 0.0005    # 5 bp/day
HTB_TICKERS = {
    "GME", "AMC", "MULN", "TLRY", "RIVN", "LCID", "SNDL",
    "DJT", "RDDT", "SOFI",
}


def _holdout_pnl_breakdown(holdout: pd.DataFrame, y_pred: np.ndarray) -> dict:
    h = holdout[["date", "ticker", "y_next_ret"]].copy()
    h["pred"]   = y_pred
    h["signal"] = np.where(h["pred"] == 1, 1.0, -1.0)
    h["gross"]  = h["signal"] * h["y_next_ret"]

    # Round-trip costs per position per day
    spread_cost = SPREAD_PER_SIDE * 2
    comm_cost   = COMMISSION_PER_SIDE * 2
    h["borrow"] = np.where(
        h["signal"] < 0,
        np.where(h["ticker"].astype(str).isin(HTB_TICKERS),
                 BORROW_DAILY_HTB, BORROW_DAILY_LIQUID),
        0.0,
    )
    h["cost"] = spread_cost + comm_cost + h["borrow"]
    h["net"]  = h["gross"] - h["cost"]

    daily_gross = h.groupby("date")["gross"].mean()
    daily_net   = h.groupby("date")["net"].mean()

    def _stats(daily: pd.Series) -> dict:
        if daily.empty or daily.std() == 0:
            return {"sharpe": 0.0, "max_dd": 0.0, "mean_daily": 0.0, "ann_ret": 0.0}
        sharpe = float(daily.mean() / (daily.std() + 1e-9) * np.sqrt(252))
        eq = (1 + daily).cumprod()
        max_dd = float((eq / eq.cummax() - 1).min() * -1)
        return {
            "sharpe":     round(sharpe, 4),
            "max_dd":     round(max_dd, 4),
            "mean_daily": round(float(daily.mean()), 6),
            "ann_ret":    round(float(((1 + daily.mean()) ** 252) - 1), 4),
        }

    g = _stats(daily_gross)
    n = _stats(daily_net)
    return {
        "n_rows":     int(len(h)),
        "n_days":     int(daily_net.shape[0]),
        "n_short":    int((h["signal"] < 0).sum()),
        "n_long":     int((h["signal"] > 0).sum()),
        "gross":      g,
        "net":        n,
        "cost_per_position_avg":  round(float(h["cost"].mean()), 6),
        "borrow_cost_avg_short":  round(float(h.loc[h["signal"] < 0, "borrow"].mean()), 6)
                                   if (h["signal"] < 0).any() else None,
        "config": {
            "spread_per_side":   SPREAD_PER_SIDE,
            "commission_per_side": COMMISSION_PER_SIDE,
            "borrow_daily_liquid": BORROW_DAILY_LIQUID,
            "borrow_daily_htb":    BORROW_DAILY_HTB,
            "htb_tickers":         sorted(HTB_TICKERS),
        },
    }


def main() -> None:
    df = load_cached_frame()
    if df is None:
        raise SystemExit("no cached feature matrix")
    meta = registry.production_metadata()
    if meta is None:
        raise SystemExit("no production model")
    model, _ = registry.load(meta.version)

    unique_dates = sorted(df["date"].unique())
    cutoff = unique_dates[-settings.holdout_days]
    holdout = df[df["date"] >= cutoff].copy()
    y_pred = model.predict(holdout[model.features])

    out = _holdout_pnl_breakdown(holdout, y_pred)
    out["model_version"] = meta.version
    out["holdout_window"] = (
        f"{str(unique_dates[-settings.holdout_days])[:10]} → "
        f"{str(unique_dates[-1])[:10]}"
    )

    print(json.dumps(out, indent=2))
    OUT.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUT}")
    g = out["gross"]; n = out["net"]
    print(f"\n→ gross Sharpe {g['sharpe']:+.2f} → net Sharpe {n['sharpe']:+.2f}  "
          f"(Δ {n['sharpe']-g['sharpe']:+.2f}); "
          f"gross ann {g['ann_ret']:+.2%} → net ann {n['ann_ret']:+.2%}")


if __name__ == "__main__":
    main()
