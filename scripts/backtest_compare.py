"""Compare engine backtest expectancy: yfinance ES=F vs Databento ESM6."""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from order_flow_engine.src import backtester, config as of_cfg


def run_one(symbol: str, lookback: int, label: str) -> dict:
    print(f"\n=== Backtesting {label}: symbol={symbol}, lookback={lookback}d ===")
    out_dir = of_cfg.OF_OUTPUT_DIR / f"backtest_{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = backtester.run(symbol=symbol, lookback_days=lookback, output_dir=out_dir)
    return res


def diff_table(yf_res: dict, db_res: dict) -> list[dict]:
    rows = []
    keys = sorted(set(yf_res) | set(db_res))
    for k in keys:
        yf = yf_res.get(k, {}) or {}
        db = db_res.get(k, {}) or {}
        rows.append({
            "label": k,
            "yf_count":      yf.get("count", 0),
            "db_count":      db.get("count", 0),
            "yf_mean_r":     yf.get("mean_r"),
            "db_mean_r":     db.get("mean_r"),
            "yf_hit_rate":   yf.get("hit_rate"),
            "db_hit_rate":   db.get("hit_rate"),
            "delta_mean_r":  (db.get("mean_r") or 0) - (yf.get("mean_r") or 0)
                              if isinstance(db.get("mean_r"), (int, float))
                              and isinstance(yf.get("mean_r"), (int, float)) else None,
        })
    return rows


def main():
    # Databento intraday history depth — keep modest to control cost.
    db_lookback = 30
    yf_lookback = 60   # yfinance 5m/15m caps at 60d anyway

    yf_res = run_one("ES=F", yf_lookback, "yfinance_ESF")
    db_res = run_one("ESM6", db_lookback, "databento_ESM6")

    diff = diff_table(yf_res, db_res)
    out_path = of_cfg.OF_OUTPUT_DIR / "backtest_compare.json"
    with out_path.open("w") as f:
        json.dump({"yfinance": yf_res, "databento": db_res, "diff": diff},
                  f, indent=2, default=str)

    print("\n=== DIFF (per-label expectancy) ===")
    print(f"{'label':<22}{'yf_n':>6}{'db_n':>6}{'yf_R':>10}{'db_R':>10}"
          f"{'Δmean_R':>10}{'yf_hit':>9}{'db_hit':>9}")
    for row in diff:
        def fmt(v, w, p=4):
            if v is None: return f"{'-':>{w}}"
            if isinstance(v, float): return f"{v:>{w}.{p}f}"
            return f"{v:>{w}}"
        print(
            f"{row['label']:<22}"
            f"{fmt(row['yf_count'], 6)}"
            f"{fmt(row['db_count'], 6)}"
            f"{fmt(row['yf_mean_r'], 10)}"
            f"{fmt(row['db_mean_r'], 10)}"
            f"{fmt(row['delta_mean_r'], 10)}"
            f"{fmt(row['yf_hit_rate'], 9)}"
            f"{fmt(row['db_hit_rate'], 9)}"
        )
    print(f"\nFull report saved: {out_path}")


if __name__ == "__main__":
    main()
