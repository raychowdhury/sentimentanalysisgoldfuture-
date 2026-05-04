"""Macro features. Daily series fetched from FRED + yfinance,
forward-filled onto bar timestamps.

Cached to ml_engine/data/macro/ for reuse across training runs.
"""
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ml_engine import config

logger = logging.getLogger(__name__)

CACHE_DIR = config.HISTORY_DIR.parent / "macro"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_HOURS = 12


def _stale(p: Path) -> bool:
    if not p.exists():
        return True
    import time
    return (time.time() - p.stat().st_mtime) > CACHE_TTL_HOURS * 3600


def _fred(series_id: str, days: int) -> pd.Series:
    cache = CACHE_DIR / f"fred_{series_id}.parquet"
    if not _stale(cache):
        return pd.read_parquet(cache)["v"]
    try:
        from market.fred_fetcher import fetch_series
        df = fetch_series(series_id, days)
        if df is None:
            raise RuntimeError("FRED fetch returned None")
        s = df["Close"].rename("v")
        s.index = pd.to_datetime(s.index, utc=True)
        s.to_frame().to_parquet(cache)
        return s
    except Exception as e:
        logger.warning(f"FRED {series_id} failed: {e}")
        if cache.exists():
            return pd.read_parquet(cache)["v"]
        return pd.Series(dtype=float)


def _yf(ticker: str, days: int) -> pd.Series:
    cache = CACHE_DIR / f"yf_{ticker.replace('^','').replace('-','_')}.parquet"
    if not _stale(cache):
        return pd.read_parquet(cache)["v"]
    try:
        import yfinance as yf
        end = date.today()
        start = end - timedelta(days=days + 5)
        df = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                         progress=False, auto_adjust=False)
        if df.empty:
            raise RuntimeError("yfinance empty")
        s = df["Close"].squeeze().rename("v")
        s.index = pd.to_datetime(s.index, utc=True)
        s.to_frame().to_parquet(cache)
        return s
    except Exception as e:
        logger.warning(f"yfinance {ticker} failed: {e}")
        if cache.exists():
            return pd.read_parquet(cache)["v"]
        return pd.Series(dtype=float)


def build(bar_index: pd.DatetimeIndex, lookback_days: int = 800) -> pd.DataFrame:
    """Build macro feature frame aligned to bar_index. All forward-filled from
    daily values. Features:
        dfii10_level  — 10Y TIPS real yield level
        dfii10_d5     — 5-day change
        vix_level     — VIX close
        vix_z20       — VIX z-score over 20 days
        dxy_d5        — DXY 5-day pct change
    """
    dfii = _fred("DFII10", lookback_days)
    vix  = _yf("^VIX", lookback_days)
    dxy  = _yf("DX-Y.NYB", lookback_days)

    feats = pd.DataFrame(index=pd.to_datetime(bar_index))

    if not dfii.empty:
        s = dfii.sort_index()
        feats["dfii10_level"] = s.reindex(feats.index, method="ffill")
        feats["dfii10_d5"] = s.diff(5).reindex(feats.index, method="ffill")

    if not vix.empty:
        s = vix.sort_index()
        feats["vix_level"] = s.reindex(feats.index, method="ffill")
        v_mean = s.rolling(20, min_periods=10).mean()
        v_std = s.rolling(20, min_periods=10).std()
        z = (s - v_mean) / v_std
        feats["vix_z20"] = z.reindex(feats.index, method="ffill")

    if not dxy.empty:
        s = dxy.sort_index()
        feats["dxy_d5"] = s.pct_change(5).reindex(feats.index, method="ffill")

    return feats
