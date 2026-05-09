"""
Print current price + range forecasts (1m / 15m / 3h) for ESM6.

Picks the most recent model file for each (tf, horizon) combination from
order_flow_engine/models/. Loads latest bars from disk and reports the
P10 / P50 / P90 next-bar range in basis points and absolute price units
($ per index point on ES = 50/contract; we just show $ on the index level).
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import vol_forecaster as vf


SYMBOL = "ESM6"
HORIZONS = [
    ("1m",  "1m",  1,  "next 1m"),
    ("15m", "15m", 1,  "next 15m"),
    ("3h",  "15m", 12, "next 3h (12×15m)"),
]


def _latest_model(tf: str, horizon: int) -> Path | None:
    pat = f"vol_{SYMBOL}_{tf}_h{horizon}_*.pkl"
    files = sorted(of_cfg.OF_MODELS_DIR.glob(pat))
    return files[-1] if files else None


def main() -> int:
    bars_cache: dict[str, "pd.DataFrame"] = {}
    rows = []
    last_price = None
    last_ts    = None

    for label, tf, horizon, desc in HORIZONS:
        pkl = _latest_model(tf, horizon)
        if pkl is None:
            rows.append((label, desc, None, "model missing"))
            continue
        with pkl.open("rb") as f:
            pkg = pickle.load(f)

        if tf not in bars_cache:
            bars_cache[tf] = vf.load_bars(SYMBOL, tf)
        bars = bars_cache[tf]

        try:
            pred = vf.predict_latest(pkg, bars)
        except Exception as e:
            rows.append((label, desc, None, f"err: {e}"))
            continue

        # Use the most-granular feed (1m if available) for current price
        price_src = bars_cache["1m"] if "1m" in bars_cache else bars
        cur_price = float(price_src["Close"].iloc[-1])
        cur_ts    = str(price_src.index[-1])
        if last_price is None:
            last_price, last_ts = cur_price, cur_ts

        # Convert bps to absolute index points.
        p10_pts = cur_price * pred["p10_bps"] / 1e4
        p50_pts = cur_price * pred["p50_bps"] / 1e4
        p90_pts = cur_price * pred["p90_bps"] / 1e4
        rows.append((label, desc, pred, (p10_pts, p50_pts, p90_pts)))

    print(f"\nESM6 — current {last_price:.2f}  @ {last_ts}\n")
    print(f"{'horizon':<8}{'desc':<22}{'P10 bps':>10}{'P50 bps':>10}{'P90 bps':>10}"
          f"{'P10 pts':>10}{'P50 pts':>10}{'P90 pts':>10}")
    print("-" * 90)
    for label, desc, pred, extra in rows:
        if pred is None:
            print(f"{label:<8}{desc:<22}{'—':>10}{'—':>10}{'—':>10}  ({extra})")
            continue
        p10p, p50p, p90p = extra
        print(f"{label:<8}{desc:<22}"
              f"{pred['p10_bps']:>10.2f}{pred['p50_bps']:>10.2f}{pred['p90_bps']:>10.2f}"
              f"{p10p:>10.2f}{p50p:>10.2f}{p90p:>10.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
