"""
Databento equities OHLCV fetcher (EQUS.MINI consolidated US tape).

Daily bars only — matches the existing yfinance-shaped output so
stocks/stock_market.py can swap in via a feature flag without touching
indicator math.

Dataset: EQUS.MINI starts 2023-03-28 (~3 years of history). For deeper
backtests yfinance is still better. Cost is fractions of a cent per
symbol-month at daily granularity.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd

from utils.logger import setup_logger

logger = setup_logger(__name__)

_DATASET = "EQUS.MINI"
_SCHEMA = "ohlcv-1d"
_BULK_CHUNK = 50  # symbols per get_range call


def _client():
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        return None
    try:
        import databento as db
    except ImportError:
        return None
    return db.Historical(key)


def _to_yfinance_shape(df: pd.DataFrame) -> pd.DataFrame:
    """Map Databento ohlcv columns to yfinance shape (Open/High/Low/Close/Volume)."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    out = pd.DataFrame({
        "Open":   df["open"].astype(float),
        "High":   df["high"].astype(float),
        "Low":    df["low"].astype(float),
        "Close":  df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    }).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _date_window(lookback_days: int) -> tuple[str, str]:
    # EQUS.MINI ends at midnight UTC of current day — querying past it 422s.
    end = date.today()
    start = end - timedelta(days=lookback_days + 5)
    return start.isoformat(), end.isoformat()


def fetch_ohlcv(ticker: str, lookback_days: int) -> pd.DataFrame | None:
    client = _client()
    if client is None:
        logger.warning("Databento equities skipped — no key/package")
        return None
    start, end = _date_window(lookback_days)
    try:
        data = client.timeseries.get_range(
            dataset=_DATASET, symbols=ticker, stype_in="raw_symbol",
            schema=_SCHEMA, start=start, end=end,
        )
        df = data.to_df()
    except Exception as e:
        logger.warning(f"Databento {ticker}: {e}")
        return None
    if df is None or df.empty:
        return None
    out = _to_yfinance_shape(df)
    if len(out) > lookback_days:
        out = out.iloc[-lookback_days:]
    return out


def fetch_ohlcv_bulk(
    tickers: list[str], lookback_days: int,
) -> dict[str, pd.DataFrame | None]:
    """Batched fetch — chunks symbols into multi-symbol get_range calls."""
    if not tickers:
        return {}
    client = _client()
    if client is None:
        return {t: None for t in tickers}
    start, end = _date_window(lookback_days)

    out: dict[str, pd.DataFrame | None] = {t: None for t in tickers}
    for i in range(0, len(tickers), _BULK_CHUNK):
        chunk = tickers[i : i + _BULK_CHUNK]
        try:
            data = client.timeseries.get_range(
                dataset=_DATASET, symbols=chunk, stype_in="raw_symbol",
                schema=_SCHEMA, start=start, end=end,
            )
            df = data.to_df()
        except Exception as e:
            logger.warning(f"Databento bulk chunk failed ({len(chunk)} syms): {e}")
            continue
        if df is None or df.empty or "symbol" not in df.columns:
            continue
        for sym, sub in df.groupby("symbol"):
            shaped = _to_yfinance_shape(sub.drop(columns=["symbol"], errors="ignore"))
            if len(shaped) > lookback_days:
                shaped = shaped.iloc[-lookback_days:]
            out[str(sym)] = shaped if not shaped.empty else None
    return out
