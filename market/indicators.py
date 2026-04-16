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


def compute(df: pd.DataFrame | None, name: str = "", tf: dict | None = None) -> dict | None:
    """
    Compute key indicators from a daily OHLCV DataFrame.

    tf  – optional timeframe profile dict from config.TIMEFRAME_PROFILES.
          When provided, its ema_short / ema_long / return_window /
          high_low_window values override the config constants.

    Returns a dict with:
        current          – latest close
        ema20            – short EMA of close  (window = tf["ema_short"])
        ema50            – long  EMA of close  (window = tf["ema_long"])
        return_5d_pct    – percentage return over return_window bars
        abs_change_5d    – absolute change over return_window bars (yield)
        recent_high_14d  – rolling high over high_low_window bars
        recent_low_14d   – rolling low  over high_low_window bars
    Returns None if data is missing or too short to be useful.
    """
    if df is None or len(df) < 5:
        if name:
            logger.warning(f"{name}: insufficient data for indicators")
        return None

    ema_short     = tf["ema_short"]     if tf else config.EMA_SHORT
    ema_long      = tf["ema_long"]      if tf else config.EMA_LONG
    return_window = tf["return_window"] if tf else config.RETURN_WINDOW_DAYS
    hl_window     = tf["high_low_window"] if tf else 14

    close   = df["Close"]
    current = float(close.iloc[-1])

    ema20 = _ema(close, ema_short)
    ema50 = _ema(close, min(ema_long, len(close)))

    # n-day return
    n = min(return_window, len(close) - 1)
    base = float(close.iloc[-(n + 1)])
    return_pct = ((current - base) / base * 100) if base != 0 else 0.0
    abs_change = current - base   # absolute level change (meaningful for yield)

    # rolling high / low
    w = min(hl_window, len(df))
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
