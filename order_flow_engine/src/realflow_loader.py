"""
Real-flow loader — read locally cached Databento-derived live tails (and
optional historical-backfill files) and return a bar frame with
`buy_vol_real` / `sell_vol_real` populated.

Source files:
  {SYMBOL}_{tf}_live.parquet              — written by realtime_databento_live
  {SYMBOL}_{tf}_realflow_history.parquet  — written by realflow_history_backfill

Schema (both): Open/High/Low/Close/Volume/buy_vol_real/sell_vol_real,
indexed by `ts` (tz-aware UTC). History file may carry a `source` column.

Merge rule: when both files exist, concatenate, dedupe by index, prefer
live rows on overlap (live is the production-of-record).

If the requested timeframe has no live file but a 1m live file exists,
this module resamples 1m → target TF (sum volumes, OHLC from first/max/
min/last). No network calls.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from order_flow_engine.src import config as of_cfg

REQUIRED_COLS = ("Open", "High", "Low", "Close",
                 "Volume", "buy_vol_real", "sell_vol_real")

_TF_TO_PANDAS = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
}


def _live_path(symbol: str, tf: str) -> Path:
    sym = symbol.replace("=", "_").replace("/", "_")
    return Path(of_cfg.OF_PROCESSED_DIR) / f"{sym}_{tf}_live.parquet"


def _history_path(symbol: str, tf: str) -> Path:
    sym = symbol.replace("=", "_").replace("/", "_")
    return Path(of_cfg.OF_PROCESSED_DIR) / f"{sym}_{tf}_realflow_history.parquet"


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def _merge_history_and_live(
    history: pd.DataFrame | None,
    live: pd.DataFrame,
) -> pd.DataFrame:
    """Concat history first, live second; dedupe by index keeping last (live)."""
    if history is None or history.empty:
        return live
    # Tag missing source column on live so concat keeps schema consistent.
    if "source" in history.columns and "source" not in live.columns:
        live = live.copy()
        live["source"] = "live"
    combined = pd.concat([history, live])
    combined = combined.sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined


def resample_to_tf(df_1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate 1m bars to the target TF preserving real-flow sums."""
    rule = _TF_TO_PANDAS.get(tf)
    if rule is None:
        raise ValueError(f"Unsupported tf for resample: {tf}")
    agg = {
        "Open":          "first",
        "High":          "max",
        "Low":           "min",
        "Close":         "last",
        "Volume":        "sum",
        "buy_vol_real":  "sum",
        "sell_vol_real": "sum",
    }
    out = df_1m.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def load_realflow(symbol: str, tf: str) -> pd.DataFrame:
    """
    Return a real-flow bar frame for (symbol, tf).

    Priority:
      1. Same-tf live file (production-of-record).
      2. 1m live file resampled to tf (fallback if same-tf file absent).
    Then merged with the historical backfill file (if present), live wins
    on overlap.

    Raises FileNotFoundError if no source (live or history) exists.
    """
    history_p = _history_path(symbol, tf)
    history = _read(history_p) if history_p.exists() else None

    live: pd.DataFrame | None = None
    direct = _live_path(symbol, tf)
    if direct.exists():
        live = _read(direct)
    else:
        one_min = _live_path(symbol, "1m")
        if one_min.exists():
            live = resample_to_tf(_read(one_min), tf)

    if live is None and history is None:
        raise FileNotFoundError(
            f"No real-flow source for {symbol}@{tf}: looked at "
            f"{history_p}, {direct}, and {_live_path(symbol, '1m')}"
        )

    if live is None:
        return history
    return _merge_history_and_live(history, live)
