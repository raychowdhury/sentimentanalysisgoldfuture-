"""
Market data fetcher using yfinance.
Fetches daily OHLCV for Gold Futures, DXY, and US 10Y Treasury Yield.
Swap the symbol dict in config.py to change providers.
"""

import pandas as pd
import yfinance as yf

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def fetch_series(symbol: str, lookback_days: int) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV for one symbol.
    Returns a DataFrame or None on any failure.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            period=f"{lookback_days}d",
            interval="1d",
            auto_adjust=True,
        )
        if df is None or df.empty:
            logger.warning(f"No data returned for {symbol}")
            return None
        logger.info(f"  {symbol}: {len(df)} bars, latest {df.index[-1].date()}")
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol}: {e}")
        return None


def fetch_all(lookback_days: int | None = None) -> dict[str, pd.DataFrame | None]:
    """
    Fetch market data for all instruments defined in config.MARKET_SYMBOLS.
    Returns a dict: { "gold": df | None, "dxy": df | None, "yield_10y": df | None }
    """
    days = lookback_days or config.MARKET_LOOKBACK_DAYS
    result: dict[str, pd.DataFrame | None] = {}
    for name, symbol in config.MARKET_SYMBOLS.items():
        logger.info(f"Fetching {name} ({symbol})")
        result[name] = fetch_series(symbol, days)
    return result
