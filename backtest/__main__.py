"""
CLI entry point for the backtest harness.

Usage:
  python -m backtest                               # swing, 2 years, max-hold 20
  python -m backtest --timeframe day --days 365
  python -m backtest --timeframe swing --days 1095 --max-hold 30 --out outputs/bt.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import config
from backtest import engine, metrics


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest the gold bias signal engine.")
    p.add_argument("--timeframe", choices=["swing", "day"], default="swing")
    p.add_argument("--days",     type=int, default=730, help="lookback days to fetch")
    p.add_argument("--max-hold", type=int, default=engine.MAX_HOLD_BARS_DEFAULT,
                   help="max bars a trade stays open")
    p.add_argument("--out",      type=str, default=None,
                   help="path to write trades+report JSON (default: outputs/backtest_<ts>.json)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    trades = engine.run(
        timeframe=args.timeframe,
        lookback_days=args.days,
        max_hold=args.max_hold,
    )
    metrics.print_report(trades)

    out_path = args.out or os.path.join(
        config.OUTPUT_DIR,
        f"backtest_{args.timeframe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "params": {
            "timeframe": args.timeframe,
            "days":      args.days,
            "max_hold":  args.max_hold,
        },
        "report": metrics.report(trades),
        "trades": trades,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
