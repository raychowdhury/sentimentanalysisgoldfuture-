"""
Retune divergence-rule thresholds against Databento ESM6 history.

Sweeps each rule's threshold one at a time, runs backtester.run() per value,
records per-label expectancy + sample count, and writes the best value
maximising expectancy_R × sqrt(count) — small-sample penalty so a 1-trade
config can't win.

Run:
    PYTHONPATH=. .venv/bin/python scripts/threshold_retune.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

from order_flow_engine.src import backtester, config as of_cfg

SYMBOL = "ESM6"
LOOKBACK_DAYS = 30
TF = "15m"
OUT = of_cfg.OF_OUTPUT_DIR / "threshold_retune.json"


@dataclass
class Sweep:
    cfg_attr: str
    grid: list[float]
    affects_labels: list[str]


SWEEPS = [
    Sweep("RULE_DELTA_DOMINANCE",
          [0.20, 0.25, 0.30, 0.35, 0.40],
          ["possible_reversal"]),
    Sweep("RULE_ABSORPTION_DELTA",
          [0.40, 0.50, 0.60, 0.70],
          ["buyer_absorption", "seller_absorption"]),
    Sweep("RULE_TRAP_DELTA",
          [0.20, 0.30, 0.40, 0.50],
          ["bullish_trap", "bearish_trap"]),
    Sweep("RULE_CVD_CORR_THRESH",
          [-0.30, -0.40, -0.50, -0.60, -0.70],
          ["possible_reversal"]),
    Sweep("RULE_SR_ATR_MULT",
          [0.30, 0.50, 0.75, 1.00],
          ["buyer_absorption", "seller_absorption"]),
]


def _score(per_label: dict, label: str) -> float:
    rec = per_label.get(label) or {}
    n = rec.get("count", 0) or 0
    r = rec.get("mean_r", 0) or 0
    if n < 3:        # too few trades — not credible
        return 0.0
    return float(r) * (n ** 0.5)


def run_sweep(s: Sweep) -> dict:
    orig = getattr(of_cfg, s.cfg_attr)
    rows: list[dict] = []
    for v in s.grid:
        setattr(of_cfg, s.cfg_attr, v)
        try:
            res = backtester.run(symbol=SYMBOL, timeframe=TF,
                                 lookback_days=LOOKBACK_DAYS)
        except Exception as e:
            res = {"_error": str(e)}
        row = {"value": v}
        for lbl in s.affects_labels:
            rec = res.get(lbl) or {}
            row[f"{lbl}_n"]    = rec.get("count", 0)
            row[f"{lbl}_R"]    = rec.get("mean_r")
            row[f"{lbl}_hit"]  = rec.get("hit_rate")
            row[f"{lbl}_score"] = round(_score(res, lbl), 4)
        rows.append(row)
    setattr(of_cfg, s.cfg_attr, orig)

    # pick best per affected label
    best = {}
    for lbl in s.affects_labels:
        ranked = sorted(rows, key=lambda r: r[f"{lbl}_score"], reverse=True)
        if ranked and ranked[0][f"{lbl}_score"] > 0:
            best[lbl] = {
                "value": ranked[0]["value"],
                "score": ranked[0][f"{lbl}_score"],
                "n":     ranked[0][f"{lbl}_n"],
                "mean_R": ranked[0][f"{lbl}_R"],
                "hit_rate": ranked[0][f"{lbl}_hit"],
            }
    return {
        "attr":   s.cfg_attr,
        "grid":   s.grid,
        "current": orig,
        "rows":   rows,
        "best":   best,
    }


def main():
    print(f"Retuning thresholds on {SYMBOL} ({TF}, {LOOKBACK_DAYS}d)\n")
    full = []
    for s in SWEEPS:
        print(f"= {s.cfg_attr}: sweeping {s.grid} ===")
        result = run_sweep(s)
        full.append(result)
        for lbl, b in result["best"].items():
            cur = result["current"]
            print(f"  → best {lbl:<22} @ {s.cfg_attr}={b['value']:>6.2f} "
                  f"(was {cur:>6.2f}) | n={b['n']:>3} R={b['mean_R']:+.3f} hit={b['hit_rate']:.0%}")
        if not result["best"]:
            print(f"  → no credible best (sample sizes < 3 across grid)")
        print()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(full, indent=2, default=str))
    print(f"\nFull report: {OUT}")


if __name__ == "__main__":
    main()
