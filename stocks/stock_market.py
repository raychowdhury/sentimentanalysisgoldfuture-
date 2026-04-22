"""
Stock market data — yfinance OHLCV fetch + indicator computation.

Reuses market.indicators.compute() for EMA/return/high-low/ATR math so
stock factor scoring sees the same shape of dict the gold engine does.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from market.indicators import compute as compute_indicators
from utils.logger import setup_logger

logger = setup_logger(__name__)

# yfinance profile for stock mode — shorter lookback than gold swing (60d
# gives ~42 trading days, enough for EMA50 slope + 20d relative strength).
STOCK_LOOKBACK_DAYS = 60

# Stock profile for market.indicators.compute(). EMA20/50 + 5d return + 14d
# high/low match the gold swing layout so downstream consumers don't special-
# case stocks, but nothing here is tied to the gold config constants.
STOCK_PROFILE: dict = {
    "ema_short":       20,
    "ema_long":        50,
    "return_window":    5,
    "high_low_window": 14,
}


def fetch_ohlcv(ticker: str, lookback_days: int = STOCK_LOOKBACK_DAYS) -> pd.DataFrame | None:
    """Pull daily OHLCV. Returns None on any failure — caller handles."""
    try:
        hist = yf.Ticker(ticker).history(
            period=f"{lookback_days}d",
            interval="1d",
            auto_adjust=True,
        )
    except Exception as e:
        logger.warning(f"yfinance fetch failed for {ticker}: {e}")
        return None
    if hist is None or hist.empty:
        logger.warning(f"yfinance empty history for {ticker}")
        return None
    return hist


def fetch_indicators(ticker: str, lookback_days: int = STOCK_LOOKBACK_DAYS) -> dict | None:
    df = fetch_ohlcv(ticker, lookback_days)
    return compute_indicators(df, name=ticker, tf=STOCK_PROFILE)


def fetch_market_context() -> dict:
    """
    SPY + VIX snapshot used by relative-strength and regime scoring.

    Each slot is either the indicator dict from market.indicators.compute()
    or None if the fetch failed. Callers treat None as "data missing" and
    downgrade confidence accordingly.
    """
    return {
        "spy": fetch_indicators("SPY"),
        "vix": fetch_indicators("^VIX"),
    }


def volume_ratio(df: pd.DataFrame | None, window: int = 20) -> float | None:
    """Today's volume / trailing N-day mean. None when data is missing."""
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    vol = df["Volume"].dropna()
    if len(vol) < 3:
        return None
    today = float(vol.iloc[-1])
    prior = vol.iloc[-(window + 1):-1] if len(vol) > window else vol.iloc[:-1]
    if prior.empty:
        return None
    avg = float(prior.mean())
    if avg <= 0:
        return None
    return today / avg
