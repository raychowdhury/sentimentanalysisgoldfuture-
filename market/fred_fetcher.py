"""
FRED (Federal Reserve Economic Data) fetcher.

Uses FRED's public CSV download endpoint — no API key required. Returns a
synthetic OHLCV DataFrame (O=H=L=C=value, V=0) so existing indicators and
scoring code treat it identically to yfinance-sourced series.

Primary use: US 10Y TIPS real yield (DFII10) — gold's true discount-rate
proxy. Nominal 10Y (DGS10) is also available from the same endpoint if a
breakeven-inflation factor is added later.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import requests

from utils.logger import setup_logger

logger = setup_logger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_TIMEOUT = 15


def fetch_series(series_id: str, lookback_days: int) -> pd.DataFrame | None:
    """
    Fetch a FRED series as a synthetic daily OHLCV DataFrame.
    Returns None on any failure — caller falls back to neutral scoring.
    """
    start = (date.today() - timedelta(days=lookback_days + 10)).isoformat()
    params = {"id": series_id, "cosd": start}

    try:
        r = requests.get(FRED_CSV_URL, params=params, timeout=FRED_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"FRED {series_id} HTTP error: {e}")
        return None

    try:
        raw = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        logger.warning(f"FRED {series_id} parse error: {e}")
        return None

    if raw.empty or raw.shape[1] < 2:
        logger.warning(f"FRED {series_id}: empty or malformed CSV")
        return None

    date_col, val_col = raw.columns[0], raw.columns[1]
    raw[date_col] = pd.to_datetime(raw[date_col], errors="coerce")
    raw[val_col] = pd.to_numeric(raw[val_col], errors="coerce")
    raw = raw.dropna().set_index(date_col).sort_index()

    if raw.empty:
        logger.warning(f"FRED {series_id}: no usable rows after clean")
        return None

    v = raw[val_col]
    out = pd.DataFrame(
        {
            "Open":   v,
            "High":   v,
            "Low":    v,
            "Close":  v,
            "Volume": 0,
        },
        index=raw.index,
    )

    if len(out) > lookback_days:
        out = out.iloc[-lookback_days:]

    logger.info(f"  FRED {series_id}: {len(out)} bars, latest {out.index[-1].date()}")
    return out
