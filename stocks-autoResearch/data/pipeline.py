"""
Multi-ticker data pipeline.

Fetches OHLCV for the full universe plus SPY/VIX/DXY/sector-ETFs/FRED macro,
builds per-ticker technical features and per-date macro/breadth features,
stacks everything into a single long-format DataFrame with one row per
(date, ticker). Ticker is carried as a categorical column so a single pooled
classifier can share signal across names while still differentiating them.

The final column `y_next_dir` is the binary target (1 if next-day close >
today's close for that ticker). `y_next_ret` is the signed forward return
used for Sharpe / drawdown.
"""
from __future__ import annotations

import io
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

from config.settings import settings

# Import the parent project's universe so "top 20" stays in one place.
_PARENT_ROOT = settings.root_dir.parent
if str(_PARENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PARENT_ROOT))

from stocks.stock_universe import UNIVERSE, tickers  # noqa: E402

logger = logging.getLogger(__name__)

CACHE_NAME = "feature_matrix.pkl"

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_SERIES = {
    "FED":     "DFF",
    "REAL10Y": "DFII10",
    "CPI":     "CPIAUCSL",
}
MARKET_SYMBOLS = {
    "SPY": "SPY",
    "VIX": "^VIX",
    "DXY": "DX-Y.NYB",
}
SECTOR_ETFS = {
    "Technology":             "XLK",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Financials":             "XLF",
    "Health Care":            "XLV",
    "Energy":                 "XLE",
    "Consumer Staples":       "XLP",
}


# ── Fetch helpers ────────────────────────────────────────────────────────────

def _fetch_yf(symbol: str, lookback_days: int) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance not installed")
    end = datetime.utcnow().date()
    start = end - timedelta(days=lookback_days)
    df = yf.download(
        symbol, start=start, end=end,
        progress=False, auto_adjust=False, threads=False,
    )
    if df.empty:
        raise RuntimeError(f"empty yfinance response for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _fetch_fred(series_id: str) -> pd.Series:
    url = FRED_CSV.format(series=series_id)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"].rename(series_id)


# ── Feature engineering ──────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _per_ticker_features(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Price-derived features for one ticker's OHLCV frame."""
    out = pd.DataFrame(index=df.index)
    out["close"]    = df["Close"]
    out["ret_1d"]   = df["Close"].pct_change()
    out["ret_5d"]   = df["Close"].pct_change(5)
    out["ret_20d"]  = df["Close"].pct_change(20)
    out["vol_20d"]  = out["ret_1d"].rolling(lookback).std()
    out["ema_10"]   = df["Close"].ewm(span=10).mean()
    out["ema_50"]   = df["Close"].ewm(span=50).mean()
    out["ema_gap"]  = (out["ema_10"] - out["ema_50"]) / out["ema_50"]
    out["rsi_14"]   = _rsi(df["Close"], 14)
    out["vol_z"]    = (
        (df["Volume"] - df["Volume"].rolling(20).mean())
        / (df["Volume"].rolling(20).std() + 1e-9)
    )
    return out.drop(columns=["ema_10", "ema_50"])


def _market_features(
    spy: pd.DataFrame, vix: pd.DataFrame, dxy: pd.DataFrame,
) -> pd.DataFrame:
    out = pd.DataFrame(index=spy.index)
    out["spy_ret_1d"] = spy["Close"].pct_change()
    out["spy_ret_5d"] = spy["Close"].pct_change(5)
    out["vix"]        = vix["Close"]
    out["vix_ret_5d"] = vix["Close"].pct_change(5)
    out["dxy_ret_5d"] = dxy["Close"].pct_change(5)
    return out


def _macro_features(fred_df: pd.DataFrame) -> pd.DataFrame:
    # FRED series have mixed frequencies (daily/weekly/monthly) on a union
    # index — ffill so momentum calcs don't collapse to NaN across gaps.
    filled = fred_df.ffill()
    out = pd.DataFrame(index=filled.index)
    out["real10y"]        = filled["REAL10Y"]
    out["real10y_chg_5d"] = filled["REAL10Y"].diff(5)
    out["fed"]            = filled["FED"]
    out["cpi_yoy"]        = filled["CPI"].pct_change(252)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def build_feature_matrix(
    lookback_days: int = 1200,
    feature_lookback: int | None = None,
) -> pd.DataFrame:
    """
    Assemble a pooled long-format training matrix.

    Rows: one per (date, ticker). Columns: per-ticker price features,
    market/breadth features, sector-relative strength, macro, ticker
    categorical, and the target `y_next_dir`.
    """
    feature_lookback = feature_lookback or settings.default_lookback
    logger.info("fetching %d tickers + market + macro (lookback=%sd)",
                len(tickers()), lookback_days)

    spy = _fetch_yf(MARKET_SYMBOLS["SPY"], lookback_days)
    vix = _fetch_yf(MARKET_SYMBOLS["VIX"], lookback_days)
    dxy = _fetch_yf(MARKET_SYMBOLS["DXY"], lookback_days)
    mkt = _market_features(spy, vix, dxy)

    sector_frames: dict[str, pd.Series] = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            s = _fetch_yf(etf, lookback_days)["Close"].pct_change(5).rename(f"sector_ret_5d_{etf}")
            sector_frames[sector] = s
        except Exception as exc:
            logger.warning("sector ETF %s fetch failed: %s", etf, exc)

    fred_frames = {name: _fetch_fred(sid) for name, sid in FRED_SERIES.items()}
    fred_df = pd.concat(fred_frames.values(), axis=1)
    fred_df.columns = list(fred_frames.keys())
    macro = _macro_features(fred_df)

    per_ticker: list[pd.DataFrame] = []
    for stock in UNIVERSE:
        try:
            ohlcv = _fetch_yf(stock.ticker, lookback_days)
        except Exception as exc:
            logger.warning("ticker %s fetch failed: %s — skipping", stock.ticker, exc)
            continue
        feat = _per_ticker_features(ohlcv, feature_lookback)
        feat["ticker"] = stock.ticker
        feat["sector"] = stock.sector
        # Sector-relative 5d return: ticker 5d minus its sector ETF 5d.
        sector_ret = sector_frames.get(stock.sector)
        if sector_ret is not None:
            feat["sector_rel_5d"] = feat["ret_5d"] - sector_ret.reindex(feat.index)
        else:
            feat["sector_rel_5d"] = 0.0
        feat = feat.join(mkt, how="left").join(macro, how="left")
        feat["y_next_ret"] = feat["close"].shift(-1) / feat["close"] - 1.0
        feat["y_next_dir"] = (feat["y_next_ret"] > 0).astype(int)
        per_ticker.append(feat)

    if not per_ticker:
        raise RuntimeError("no tickers fetched — check network / yfinance")

    long = pd.concat(per_ticker, axis=0)
    long.index.name = "date"
    long = long.reset_index().sort_values(["date", "ticker"])
    # Forward-fill macro across weekends / holidays before dropping NaNs so
    # the price-feature warm-up is what actually trims the early rows.
    macro_cols = list(macro.columns) + ["fed", "real10y"]
    for c in macro_cols:
        if c in long.columns:
            long[c] = long.groupby("ticker")[c].ffill()
    long = long.dropna().copy()
    # Ticker categorical — XGBoost handles via ordinal encoding below; store
    # as a category so training_agent can cast cheaply.
    long["ticker"] = long["ticker"].astype("category")
    long["sector"] = long["sector"].astype("category")

    cache_path = settings.data_dir / CACHE_NAME
    long.to_pickle(cache_path)
    logger.info("pooled feature matrix cached → %s (%d rows, %d cols, %d tickers)",
                cache_path, len(long), long.shape[1], long["ticker"].nunique())
    return long


def load_cached_frame() -> pd.DataFrame | None:
    path: Path = settings.data_dir / CACHE_NAME
    if not path.exists():
        return None
    return pd.read_pickle(path)
