"""
Train ES volatility / range forecasters across 1m / 5m / 15m horizons.

Usage:
    python scripts/train_vol_forecast.py                     # ES, all 3 TFs
    python scripts/train_vol_forecast.py --symbol ESM6 --tf 15m
    python scripts/train_vol_forecast.py --tf 1m --tf 5m

Outputs go to:
    outputs/order_flow/vol_forecast/<version>_{predictions,report,feature_importance}.*
    order_flow_engine/models/<version>.pkl + .json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_flow_engine.src import vol_forecaster as vf


DEFAULT_SYMBOL = "ESM6"
DEFAULT_TFS    = ["1m", "5m", "15m"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Train ES vol/range forecaster.")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--tf", action="append",
                    help="Timeframe (repeatable). Default: 1m 5m 15m")
    ap.add_argument("--horizon", type=int, default=1,
                    help="Forecast next-N bars range (default 1)")
    args = ap.parse_args()

    tfs = args.tf or DEFAULT_TFS
    summaries = []
    for tf in tfs:
        try:
            meta = vf.train_and_save(args.symbol, tf, horizon=args.horizon)
        except Exception as e:
            print(f"[{tf}] FAILED: {e}", file=sys.stderr)
            continue
        s = meta["fold_summary"]
        print(f"[{tf}] mae={s['mae_bps_mean']:.2f}bps "
              f"corr={s['corr_mean']:.3f} "
              f"cover={s['band_cover_mean']:.2f} "
              f"rows={meta['rows_total']}")
        summaries.append({
            "tf":      tf,
            "version": meta["version"],
            **s,
        })

    print(json.dumps(summaries, indent=2))
    return 0 if summaries else 1


if __name__ == "__main__":
    raise SystemExit(main())
