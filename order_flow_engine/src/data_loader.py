"""
Data loader — multi-timeframe OHLCV fetch plus schema auto-detect.

Engine operates in OHLCV mode here. `detect_schema()` can identify a tick
frame (`bid_size`, `ask_size`, `trade_side`) but there is no file-based
tick→bar aggregator in this module — real tick flow only reaches the engine
via the IBKR / Binance streaming adapters, which pre-aggregate per-bar
`buy_vol_real` / `sell_vol_real` and feed `ingest_bar()` directly. File-mode
inputs must be OHLCV.

Yfinance intraday caps force mixed-resolution windows. The engine honors them
silently rather than erroring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

from market import data_fetcher
from utils.logger import setup_logger

from order_flow_engine.src import config as of_cfg

logger = setup_logger(__name__)

TICK_COLS = {"bid_size", "ask_size", "trade_side"}
OHLCV_COLS = {"Open", "High", "Low", "Close", "Volume"}


def detect_schema(df: pd.DataFrame) -> Literal["tick", "ohlcv"]:
    """Return 'tick' if the frame carries trade-direction columns else 'ohlcv'."""
    cols = set(df.columns)
    if TICK_COLS.issubset(cols):
        return "tick"
    return "ohlcv"


def _capped_period(timeframe: str, requested_days: int) -> int:
    """Clip lookback to yfinance's per-interval window cap."""
    cap = of_cfg.YF_INTRADAY_CAPS.get(timeframe)
    if cap is None:
        return requested_days
    return min(requested_days, cap)


def _cache_path(symbol: str, timeframe: str) -> Path:
    safe = symbol.replace("=", "_").replace("/", "_")
    return of_cfg.OF_RAW_DIR / f"{safe}_{timeframe}.parquet"


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    lookback_days: int,
    use_cache: bool = True,
) -> pd.DataFrame | None:
    """
    Fetch a single (symbol, timeframe) window.

    Daily bars go through market.data_fetcher.fetch_series (shared with the
    gold bias pipeline). Intraday bars go through fetch_intraday with a cap-
    aware period. On success the frame is cached to parquet for re-runs.
    """
    cache = _cache_path(symbol, timeframe)
    if use_cache and cache.exists():
        try:
            return pd.read_parquet(cache)
        except Exception as e:
            logger.warning(f"Cache read failed {cache}: {e} — refetching")

    if timeframe in ("1d", "1D"):
        df = data_fetcher.fetch_series(symbol, lookback_days)
    else:
        period = _capped_period(timeframe, lookback_days)
        df = data_fetcher.fetch_intraday(symbol, timeframe, period)

    if df is None or df.empty:
        return None

    try:
        df.to_parquet(cache)
    except Exception as e:
        logger.warning(f"Cache write failed {cache}: {e}")

    return df


def load_multi_tf(
    symbol: str | None = None,
    timeframes: list[str] | None = None,
    lookback_days: int | None = None,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """Return {tf: DataFrame} for each timeframe that fetched successfully."""
    symbol        = symbol or of_cfg.OF_SYMBOL
    timeframes    = timeframes or of_cfg.OF_TIMEFRAMES
    lookback_days = lookback_days or of_cfg.OF_LOOKBACK_DAYS

    out: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        df = fetch_ohlcv(symbol, tf, lookback_days, use_cache=use_cache)
        if df is not None:
            out[tf] = df
    return out


def load_from_file(path: str | Path) -> pd.DataFrame:
    """
    Read a CSV or parquet file of bars or ticks. Column schema is not
    validated here — detect_schema() classifies the frame downstream.
    """
    p = Path(path)
    if p.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(p)
    return pd.read_csv(p, parse_dates=True, index_col=0)


def has_usable_volume(df: pd.DataFrame) -> bool:
    """
    True if the frame has a Volume column with any positive values. FX spots
    (XAUUSD=X) report None/0 volume — proxies are not meaningful there.
    """
    if "Volume" not in df.columns:
        return False
    vol = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
    return bool((vol > 0).any())


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Fetch multi-TF bars for the engine.")
    ap.add_argument("--symbol", default=of_cfg.OF_SYMBOL)
    ap.add_argument("--lookback", type=int, default=of_cfg.OF_LOOKBACK_DAYS)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    frames = load_multi_tf(
        symbol=args.symbol,
        lookback_days=args.lookback,
        use_cache=not args.no_cache,
    )
    for tf, df in frames.items():
        print(f"{tf}: {len(df)} bars, {df.index.min()} → {df.index.max()}")
