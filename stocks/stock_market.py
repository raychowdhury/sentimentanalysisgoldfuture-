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


# Display ticker → yfinance symbol alias (non-equity aux like index).
_YF_ALIAS = {"SPX": "^GSPC"}


def fetch_ohlcv(ticker: str, lookback_days: int = STOCK_LOOKBACK_DAYS) -> pd.DataFrame | None:
    """Pull daily OHLCV. Returns None on any failure — caller handles."""
    yf_symbol = _YF_ALIAS.get(ticker, ticker)
    try:
        hist = yf.Ticker(yf_symbol).history(
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


def fetch_ohlcv_bulk(
    tickers: list[str],
    lookback_days: int = STOCK_LOOKBACK_DAYS,
) -> dict[str, pd.DataFrame | None]:
    """
    Batched OHLCV fetch via yf.download multi-symbol API.

    Single HTTP call replaces N per-ticker calls — critical for 500-name scans.
    Returns {ticker: DataFrame|None}. Missing/empty histories map to None so
    callers use the same None-guarded path as fetch_ohlcv().
    """
    if not tickers:
        return {}
    try:
        data = yf.download(
            tickers=tickers,
            period=f"{lookback_days}d",
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as e:
        logger.warning(f"yf.download bulk failed for {len(tickers)} tickers: {e}")
        return {t: None for t in tickers}

    out: dict[str, pd.DataFrame | None] = {}
    # yf.download returns a single-level frame when only one ticker is passed,
    # and a MultiIndex-columned frame otherwise.
    if len(tickers) == 1:
        t = tickers[0]
        out[t] = data if data is not None and not data.empty else None
        return out

    for t in tickers:
        try:
            sub = data[t]
        except KeyError:
            out[t] = None
            continue
        if sub is None or sub.empty or sub["Close"].dropna().empty:
            out[t] = None
        else:
            out[t] = sub.dropna(how="all")
    return out


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
