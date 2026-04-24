"""
Market data fetcher.

Pulls daily OHLCV for the market factors. Primary source is yfinance
(gold, DXY, VIX). FRED (public CSV endpoint, no key) supplies real-yield
series; FRED sources override same-named entries in MARKET_SYMBOLS.
"""

import pandas as pd
import yfinance as yf

import config
from market import fred_fetcher
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


def fetch_intraday(
    symbol: str,
    interval: str,
    period_days: int,
) -> pd.DataFrame | None:
    """
    Fetch intraday OHLCV bars at the given interval.

    Yfinance caps window by interval — caller should pass a period_days that
    respects those caps (1m=7d, 5m/15m=60d, 1h=730d, 1d=unbounded). Exceeding
    the cap returns empty.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            period=f"{period_days}d",
            interval=interval,
            auto_adjust=True,
        )
        if df is None or df.empty:
            logger.warning(f"No intraday data for {symbol} @ {interval}/{period_days}d")
            return None
        logger.info(
            f"  {symbol} {interval}: {len(df)} bars, "
            f"latest {df.index[-1]}"
        )
        return df
    except Exception as e:
        logger.warning(f"Failed intraday fetch {symbol}@{interval}: {e}")
        return None


def fetch_all(lookback_days: int | None = None) -> dict[str, pd.DataFrame | None]:
    """
    Fetch market data for all configured instruments.

    - yfinance sources: config.MARKET_SYMBOLS
    - FRED sources:     config.FRED_SYMBOLS (CSV endpoint, no key)

    Entries sharing a name in FRED_SYMBOLS override the yfinance entry. That
    lets us swap e.g. the yield_10y slot from nominal (^TNX) to real (DFII10)
    without touching any downstream scoring code.
    """
    days = lookback_days or config.MARKET_LOOKBACK_DAYS
    result: dict[str, pd.DataFrame | None] = {}

    fred_symbols = getattr(config, "FRED_SYMBOLS", {})

    for name, symbol in config.MARKET_SYMBOLS.items():
        if name in fred_symbols:
            continue  # FRED wins
        logger.info(f"Fetching {name} ({symbol})")
        result[name] = fetch_series(symbol, days)

    for name, series_id in fred_symbols.items():
        logger.info(f"Fetching {name} (FRED {series_id})")
        result[name] = fred_fetcher.fetch_series(series_id, days)

    return result
