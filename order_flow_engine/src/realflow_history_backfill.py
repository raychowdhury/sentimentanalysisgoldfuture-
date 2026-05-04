"""
One-shot historical real-flow backfill.

Pulls OHLCV bars + trade-level prints from Databento Historical for the
requested window, classifies aggressor side via the Lee–Ready tick rule
(reusing `realtime_databento._fetch_real_flow`), and writes a parquet
file the realflow loader can merge alongside live tails.

Output schema matches `*_live.parquet`:
    Open, High, Low, Close, Volume, buy_vol_real, sell_vol_real
plus a `source` column = "historical_realflow_tick_rule" for traceability.

Hard-capped at 7 days lookback. Never overwrites live parquet files.

Run:
    python -m order_flow_engine.src.realflow_history_backfill \\
        --symbol ESM6 --tf 15m --lookback-days 7
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from market import databento_fetcher
from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realtime_databento as rd
from utils.logger import setup_logger

logger = setup_logger(__name__)

LOOKBACK_DAYS_CAP = 18
SOURCE_LABEL      = "historical_realflow_tick_rule"


def _history_path(symbol: str, tf: str) -> Path:
    sym = symbol.replace("=", "_").replace("/", "_")
    return Path(of_cfg.OF_PROCESSED_DIR) / f"{sym}_{tf}_realflow_history.parquet"


def _live_path(symbol: str, tf: str) -> Path:
    sym = symbol.replace("=", "_").replace("/", "_")
    return Path(of_cfg.OF_PROCESSED_DIR) / f"{sym}_{tf}_live.parquet"


def _fetch_ohlcv(symbol: str, tf: str, lookback_days: int) -> pd.DataFrame:
    """OHLCV via Databento Historical. Bypasses cache so window matches request."""
    df = databento_fetcher.fetch_intraday(symbol, tf, lookback_days)
    if df is None or df.empty:
        raise RuntimeError(f"No OHLCV from Databento for {symbol}@{tf}")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    return df[keep].sort_index()


def _fetch_realflow_window(symbol: str, tf: str, lookback_days: int) -> pd.DataFrame:
    """Real flow via Lee–Ready tick rule on `trades` schema."""
    client = rd._client()
    if client is None:
        raise RuntimeError("Databento client unavailable (set DATABENTO_API_KEY)")
    end   = datetime.now(timezone.utc) - timedelta(seconds=15)
    start = end - timedelta(days=lookback_days)
    flow = rd._fetch_real_flow(client, symbol, tf, start, end)
    if flow is None or flow.empty:
        raise RuntimeError(f"No real-flow trades for {symbol}@{tf} window")
    if not isinstance(flow.index, pd.DatetimeIndex):
        flow.index = pd.to_datetime(flow.index, utc=True)
    elif flow.index.tz is None:
        flow.index = flow.index.tz_localize("UTC")
    return flow.sort_index()


def _verify_alignment(history: pd.DataFrame, symbol: str, tf: str) -> dict:
    """Compare first 5 timestamps of history vs live tail (if present)."""
    live = _live_path(symbol, tf)
    info = {"live_file": str(live), "live_exists": live.exists()}
    if not live.exists():
        info["note"] = "no live tail to align against"
        return info
    try:
        df_live = pd.read_parquet(live)
        if not isinstance(df_live.index, pd.DatetimeIndex):
            df_live.index = pd.to_datetime(df_live.index, utc=True)
        elif df_live.index.tz is None:
            df_live.index = df_live.index.tz_localize("UTC")
        overlap = history.index.intersection(df_live.index)
        info.update({
            "live_n_bars":      int(len(df_live)),
            "live_idx_min":     str(df_live.index.min()),
            "live_idx_max":     str(df_live.index.max()),
            "history_idx_min":  str(history.index.min()),
            "history_idx_max":  str(history.index.max()),
            "overlap_n_bars":   int(len(overlap)),
            "overlap_first_5":  [str(t) for t in overlap[:5]],
        })
    except Exception as e:
        info["error"] = f"verify failed: {e}"
    return info


def backfill(symbol: str, tf: str, lookback_days: int) -> dict:
    if lookback_days > LOOKBACK_DAYS_CAP:
        logger.warning(
            f"lookback {lookback_days} > cap {LOOKBACK_DAYS_CAP}; clipping"
        )
        lookback_days = LOOKBACK_DAYS_CAP

    logger.info(f"Fetching OHLCV {symbol}@{tf} for {lookback_days}d…")
    ohlcv = _fetch_ohlcv(symbol, tf, lookback_days)
    logger.info(f"  OHLCV: {len(ohlcv)} bars, "
                f"{ohlcv.index.min()} → {ohlcv.index.max()}")

    logger.info(f"Fetching real-flow trades {symbol}@{tf} for {lookback_days}d…")
    flow = _fetch_realflow_window(symbol, tf, lookback_days)
    logger.info(f"  Real flow: {len(flow)} bars, "
                f"{flow.index.min()} → {flow.index.max()}")

    # Inner-join: only bars where both OHLCV and real flow are available.
    common = ohlcv.index.intersection(flow.index)
    if len(common) == 0:
        raise RuntimeError(
            "No timestamp overlap between OHLCV and real flow — "
            "bar-boundary convention mismatch. Aborting backfill."
        )
    out = ohlcv.loc[common].copy()
    out["buy_vol_real"]  = flow.loc[common, "buy_vol_real"]
    out["sell_vol_real"] = flow.loc[common, "sell_vol_real"]
    out["source"]        = SOURCE_LABEL

    # Drop rows with NaN OHLC (degraded Databento days).
    before = len(out)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    dropped_ohlc = before - len(out)

    # Drop rows with NaN real flow (no trades in that window).
    before = len(out)
    out = out.dropna(subset=["buy_vol_real", "sell_vol_real"])
    dropped_flow = before - len(out)

    align = _verify_alignment(out, symbol, tf)

    out_path = _history_path(symbol, tf)
    if out_path.exists():
        # Don't blow away an existing history file silently — keep a backup.
        backup = out_path.with_suffix(".parquet.bak")
        out_path.rename(backup)
        logger.info(f"existing history moved to {backup.name}")
    out.to_parquet(out_path)

    summary = {
        "symbol":        symbol,
        "timeframe":     tf,
        "lookback_days": lookback_days,
        "source":        SOURCE_LABEL,
        "rows_written":  int(len(out)),
        "dropped_ohlc_nan": int(dropped_ohlc),
        "dropped_flow_nan": int(dropped_flow),
        "idx_min":       str(out.index.min()) if len(out) else None,
        "idx_max":       str(out.index.max()) if len(out) else None,
        "output_path":   str(out_path),
        "alignment":     align,
    }
    logger.info(
        f"Wrote {len(out)} bars to {out_path.name} "
        f"({summary['idx_min']} → {summary['idx_max']})"
    )
    return summary


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Historical real-flow backfill.")
    ap.add_argument("--symbol",        default="ESM6")
    ap.add_argument("--tf",            default="15m")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_CAP)
    args = ap.parse_args()
    import json
    print(json.dumps(
        backfill(args.symbol, args.tf, args.lookback_days),
        indent=2, default=str,
    ))


if __name__ == "__main__":  # pragma: no cover
    main()
