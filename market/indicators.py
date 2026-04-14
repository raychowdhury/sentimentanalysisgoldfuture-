"""
Technical indicators derived from daily OHLCV DataFrames.
All public functions return plain dicts of scalar values.
"""

import pandas as pd

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _ema(series: pd.Series, window: int) -> float:
    """Exponential moving average — returns the most recent value."""
    if len(series) < 2:
        return float(series.iloc[-1])
    return float(series.ewm(span=window, adjust=False).mean().iloc[-1])


def compute(df: pd.DataFrame | None, name: str = "") -> dict | None:
    """
    Compute key indicators from a daily OHLCV DataFrame.

    Returns a dict with:
        current          – latest close
        ema20            – 20-period EMA of close
        ema50            – 50-period EMA of close (uses available bars if < 50)
        return_5d_pct    – percentage return over RETURN_WINDOW_DAYS
        abs_change_5d    – absolute change over RETURN_WINDOW_DAYS (used for yield)
        recent_high_14d  – 14-bar rolling high
        recent_low_14d   – 14-bar rolling low
    Returns None if data is missing or too short to be useful.
    """
    if df is None or len(df) < 5:
        if name:
            logger.warning(f"{name}: insufficient data for indicators")
        return None

    close   = df["Close"]
    current = float(close.iloc[-1])

    ema20 = _ema(close, config.EMA_SHORT)
    ema50 = _ema(close, min(config.EMA_LONG, len(close)))

    # n-day return
    n = min(config.RETURN_WINDOW_DAYS, len(close) - 1)
    base = float(close.iloc[-(n + 1)])
    return_pct = ((current - base) / base * 100) if base != 0 else 0.0
    abs_change = current - base   # absolute level change (meaningful for yield)

    # 14-bar high / low
    w = min(14, len(df))
    high_14 = float(df["High"].iloc[-w:].max())
    low_14  = float(df["Low"].iloc[-w:].min())

    return {
        "current":          round(current,    4),
        "ema20":            round(ema20,      4),
        "ema50":            round(ema50,      4),
        "return_5d_pct":    round(return_pct, 4),
        "abs_change_5d":    round(abs_change, 4),
        "recent_high_14d":  round(high_14,   4),
        "recent_low_14d":   round(low_14,    4),
    }
