"""
Databento futures fetcher.

Pulls daily OHLCV from Databento Historical API for CME-listed futures
(GC gold, ES, NQ, CL, etc.). Returns a DataFrame shaped like yfinance
output (Open/High/Low/Close/Volume, DatetimeIndex) so downstream
indicators and scoring code treat it identically.

A Databento source with the same field name in `DATABENTO_SYMBOLS`
overrides yfinance and FRED entries of the same name — same pattern as
fred_fetcher.

Auth: DATABENTO_API_KEY env var (loaded from .env).
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd

from utils.logger import setup_logger

logger = setup_logger(__name__)

_DATASET_DEFAULT = "GLBX.MDP3"  # CME Globex MDP3 — covers GC, ES, NQ, CL, ZN
_SCHEMA_DAILY = "ohlcv-1d"

# Engine timeframe -> (Databento schema, optional pandas resample rule).
# Databento publishes 1m and 1h directly; everything else resamples from 1m.
_TF_MAP: dict[str, tuple[str, str | None]] = {
    "1m":  ("ohlcv-1m", None),
    "5m":  ("ohlcv-1m", "5min"),
    "15m": ("ohlcv-1m", "15min"),
    "30m": ("ohlcv-1m", "30min"),
    "1h":  ("ohlcv-1h", None),
    "1d":  ("ohlcv-1d", None),
}


def _client():
    """Lazy-init Databento client. Returns None if key missing or pkg absent."""
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        logger.warning("DATABENTO_API_KEY not set — skipping Databento fetch")
        return None
    try:
        import databento as db
    except ImportError:
        logger.warning("databento package not installed — skipping")
        return None
    return db.Historical(key)


def fetch_front_month_daily(
    parent: str,
    lookback_days: int,
    *,
    dataset: str = _DATASET_DEFAULT,
) -> pd.DataFrame | None:
    """
    Pull `parent` ohlcv-1d, drop spreads, and per-day keep only the
    highest-volume single contract (the rolling front month).

    Use when the dataset doesn't expose a usable `continuous` stype
    (e.g. ICE IFUS.IMPACT for DX where `DX.c.0` returns empty).
    """
    from datetime import date, timedelta

    client = _client()
    if client is None:
        return None
    end = date.today()
    start = end - timedelta(days=lookback_days + 10)

    def _do(s, e):
        return client.timeseries.get_range(
            dataset=dataset, symbols=parent, stype_in="parent",
            schema=_SCHEMA_DAILY,
            start=s.isoformat(), end=e.isoformat(),
        ).to_df()

    try:
        df = _do(start, end)
    except Exception as e:
        # Some datasets (e.g. ICE IFUS) cap free access to ~24h delayed.
        # Both error patterns (`available up to 'X'` and
        # `Try again with an end time before X`) include an ISO timestamp.
        import re as _re
        m = (_re.search(r"available up to '([^']+)'", str(e))
             or _re.search(r"end time before ([0-9T:.\-+Z]+)", str(e)))
        if not m:
            logger.warning(f"Databento parent {parent}: {e}")
            return None
        try:
            avail_end = pd.Timestamp(m.group(1)).tz_convert("UTC").date()
            df = _do(start, avail_end)
        except Exception as e2:
            logger.warning(f"Databento parent retry {parent}: {e2}")
            return None
    if df is None or df.empty:
        return None

    # Drop calendar spreads — outright contracts have no `-` in symbol.
    df = df[~df["symbol"].astype(str).str.contains("-", regex=False)]
    if df.empty:
        return None

    # Per ts, keep row with highest volume = front-month outright.
    df = df.sort_values("volume", ascending=False)
    df = df[~df.index.duplicated(keep="first")].sort_index()

    df.index = pd.to_datetime(df.index).tz_localize(None)
    out = pd.DataFrame({
        "Open":   df["open"].astype(float),
        "High":   df["high"].astype(float),
        "Low":    df["low"].astype(float),
        "Close":  df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    }).sort_index()
    if len(out) > lookback_days:
        out = out.iloc[-lookback_days:]
    logger.info(
        f"  Databento {parent} (front-month parent, {dataset}): "
        f"{len(out)} bars, latest {out.index[-1].date()}"
    )
    return out


def fetch_series(
    symbol: str,
    lookback_days: int,
    *,
    dataset: str = _DATASET_DEFAULT,
    stype_in: str = "continuous",
) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV for one Databento symbol.

    Default `stype_in="continuous"` → pass e.g. `GC.c.0` for the rolling
    front-month series (auto-rolls on calendar). Use `stype_in="raw_symbol"`
    + e.g. `GCM6` for a specific contract.

    Returns a DataFrame with Open/High/Low/Close/Volume and a tz-naive
    DatetimeIndex, or None on any failure.
    """
    client = _client()
    if client is None:
        return None

    end = date.today()
    start = end - timedelta(days=lookback_days + 10)

    try:
        data = client.timeseries.get_range(
            dataset=dataset,
            symbols=symbol,
            stype_in=stype_in,
            schema=_SCHEMA_DAILY,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        df = data.to_df()
    except Exception as e:
        logger.warning(f"Databento {symbol}: {e}")
        return None

    if df is None or df.empty:
        logger.warning(f"Databento {symbol}: empty result")
        return None

    df.index = pd.to_datetime(df.index).tz_localize(None)
    out = pd.DataFrame(
        {
            "Open":   df["open"].astype(float),
            "High":   df["high"].astype(float),
            "Low":    df["low"].astype(float),
            "Close":  df["close"].astype(float),
            "Volume": df["volume"].astype(float),
        }
    ).sort_index()

    out = out[~out.index.duplicated(keep="last")]

    if len(out) > lookback_days:
        out = out.iloc[-lookback_days:]

    logger.info(
        f"  Databento {symbol} ({stype_in}): {len(out)} bars, "
        f"latest {out.index[-1].date()}"
    )
    return out


def fetch_intraday(
    symbol: str,
    timeframe: str,
    period_days: int,
    *,
    dataset: str = _DATASET_DEFAULT,
    stype_in: str = "raw_symbol",
) -> pd.DataFrame | None:
    """
    Fetch intraday OHLCV bars for a Databento symbol shaped to match
    `market.data_fetcher.fetch_intraday` output (Open/High/Low/Close/Volume,
    UTC DatetimeIndex).

    Default `stype_in="raw_symbol"` since engine backtests target a single
    contract (e.g. ESM6) — for parent tokens (`ES.FUT`) pass stype_in="parent"
    and post-filter, or use `realtime_databento._resolve_front_month` first.
    """
    from datetime import datetime, timedelta, timezone

    if timeframe not in _TF_MAP:
        logger.warning(f"unsupported timeframe {timeframe!r} for Databento")
        return None
    schema, resample_rule = _TF_MAP[timeframe]

    client = _client()
    if client is None:
        return None

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)

    def _do(s, e):
        return client.timeseries.get_range(
            dataset=dataset, symbols=symbol, stype_in=stype_in,
            schema=schema, start=s.isoformat(), end=e.isoformat(),
        ).to_df()

    try:
        df = _do(start, end)
    except Exception as e:
        # 422 dataset cutoff: extract available_end from message and retry.
        import re as _re
        m = _re.search(r"available up to '([^']+)'", str(e))
        if not m:
            logger.warning(f"Databento intraday {symbol}@{timeframe}: {e}")
            return None
        try:
            avail_end = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
            df = _do(start, avail_end)
        except Exception as e2:
            logger.warning(f"Databento intraday retry {symbol}@{timeframe}: {e2}")
            return None

    if df is None or df.empty:
        logger.warning(f"Databento intraday {symbol}@{timeframe}: empty")
        return None

    df.index = pd.to_datetime(df.index).tz_convert("UTC").tz_localize(None)
    out = pd.DataFrame({
        "Open":   df["open"].astype(float),
        "High":   df["high"].astype(float),
        "Low":    df["low"].astype(float),
        "Close":  df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    }).sort_index()
    out = out[~out.index.duplicated(keep="last")]

    if resample_rule is not None:
        out = out.resample(resample_rule, label="right", closed="right").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

    logger.info(
        f"  Databento {symbol} {timeframe}: {len(out)} bars, "
        f"latest {out.index[-1]}"
    )
    return out
