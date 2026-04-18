"""
Grid search over timeframe-profile parameters.

Fetches market data ONCE, then runs engine.run() across a parameter grid to
find settings that maximize expectancy / total R / win-rate.

Example:
    python -m backtest.grid_search --timeframe swing --days 730

Customize the grid inline (keep it small — cost = |grid| × bars).
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
from datetime import datetime

import config
from backtest import engine, metrics
from market import data_fetcher
from utils.logger import setup_logger

logger = setup_logger(__name__)


# ── Grid definition ───────────────────────────────────────────────────────────
# Keep the grid small. 4 × 3 × 3 = 36 runs is a reasonable ceiling.

GRIDS: dict[str, dict[str, list]] = {
    "swing": {
        "min_rr":            [2.5, 3.0],
        "atr_stop_mult":     [1.0, 1.5],
        "max_hold":          [40, 60],
        "trail_atr_mult":    [1.5, 2.5, 3.5, None],   # None = trailing off
        "trail_activate_r":  [1.0, 2.0],
    },
    "day": {
        "min_rr":            [1.2, 1.5, 2.0],
        "atr_stop_mult":     [0.25, 0.5, 1.0],
        "max_hold":          [5, 10, 20],
        "trail_atr_mult":    [1.0, 2.0, None],
        "trail_activate_r":  [0.5, 1.0],
    },
}


def _build_profiles(timeframe: str) -> list[dict]:
    base = copy.deepcopy(config.TIMEFRAME_PROFILES[timeframe])
    grid = GRIDS[timeframe]
    keys = list(grid.keys())
    combos = list(itertools.product(*(grid[k] for k in keys)))

    profiles = []
    for combo in combos:
        p = copy.deepcopy(base)
        for k, v in zip(keys, combo):
            p[k] = v
        p["_name"] = "+".join(f"{k}={v}" for k, v in zip(keys, combo))
        profiles.append(p)
    return profiles


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Grid search over profile parameters.")
    p.add_argument("--timeframe", choices=["swing", "day"], default="swing")
    p.add_argument("--days",      type=int, default=730)
    p.add_argument("--max-hold",  type=int, default=engine.MAX_HOLD_BARS_DEFAULT)
    p.add_argument("--rank-by",   choices=["expectancy", "total_r", "win_rate"],
                   default="expectancy")
    p.add_argument("--out",       type=str, default=None)
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    logger.info(f"Fetching market data once — {args.days}d")
    series = data_fetcher.fetch_all(args.days)

    profiles = _build_profiles(args.timeframe)
    logger.info(f"Grid size: {len(profiles)} profiles")

    # Snapshot original trail config so we can restore it per-iteration.
    orig_trail_enabled  = config.TRAIL_ENABLED
    orig_trail_mult     = config.TRAIL_ATR_MULT
    orig_trail_activate = config.TRAIL_ACTIVATE_R

    results: list[dict] = []
    try:
        for i, profile in enumerate(profiles, 1):
            logger.info(f"[{i}/{len(profiles)}] {profile['_name']}")
            max_hold      = profile.pop("max_hold", args.max_hold)
            trail_mult    = profile.pop("trail_atr_mult",   None)
            trail_activ_r = profile.pop("trail_activate_r", orig_trail_activate)

            # Trail settings are module-level globals; flip in/out per iteration.
            config.TRAIL_ENABLED    = trail_mult is not None
            config.TRAIL_ATR_MULT   = trail_mult if trail_mult is not None else orig_trail_mult
            config.TRAIL_ACTIVATE_R = trail_activ_r

            trades = engine.run(
                timeframe=profile,
                lookback_days=args.days,
                max_hold=max_hold,
                series=series,
            )
            summary = metrics.report(trades)["overall"]
            summary["profile"] = profile["_name"]
            results.append(summary)
    finally:
        config.TRAIL_ENABLED    = orig_trail_enabled
        config.TRAIL_ATR_MULT   = orig_trail_mult
        config.TRAIL_ACTIVATE_R = orig_trail_activate

    key = args.rank_by
    results.sort(key=lambda r: r.get(key, 0) if r.get("trades", 0) > 0 else -1e9,
                 reverse=True)

    print(f"\n── Grid Search Ranking (by {key}) ──────────────────")
    print(f"  {'profile':<60}  {'n':>4}  {'win%':>6}  {'exp':>7}  {'total':>8}")
    for r in results:
        n = r.get("trades", 0)
        if n == 0:
            print(f"  {r['profile']:<60}  {'0':>4}  {'—':>6}  {'—':>7}  {'—':>8}")
            continue
        print(f"  {r['profile']:<60}  {n:>4}  "
              f"{r['win_rate']:>5.1f}%  {r['expectancy']:>+7.3f}  "
              f"{r['total_r']:>+8.2f}")
    print("────────────────────────────────────────────────────\n")

    out_path = args.out or os.path.join(
        config.OUTPUT_DIR,
        f"grid_{args.timeframe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "params": {
                "timeframe": args.timeframe,
                "days":      args.days,
                "max_hold":  args.max_hold,
                "rank_by":   args.rank_by,
            },
            "ranking": results,
        }, f, indent=2, default=str)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
