"""
Data pipeline: OHLCV + macro fetch, feature engineering, persistence.

The pipeline returns a single pandas DataFrame indexed by date whose final
column is the binary target `y_next_dir` (1 if next-day close > today's
close, else 0). All other columns are features.
"""
from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:  # surfaced clearly at runtime
    yf = None  # type: ignore

from config.settings import settings

logger = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_SERIES = {
    "CPI":      "CPIAUCSL",
    "FED":      "DFF",
    "REAL10Y":  "DFII10",
}
YF_SYMBOLS = {
    "GOLD": "GC=F",
    "DXY":  "DX-Y.NYB",
    "VIX":  "^VIX",
}

CACHE_NAME = "feature_matrix.parquet"
LIVE_ROW_NAME = "live_row.parquet"
TARGET_COLS = ["y_next_ret", "y_next_dir"]

def _sentiment_cache_path() -> Path:
    """Resolve sentiment cache path. Env var wins; else container mount; else
    parent-project outputs/ (the local-dev layout)."""
    override = os.getenv("SENTIMENT_CACHE_PATH")
    if override:
        return Path(override)
    container = Path("/app/external/sentiment_cache.jsonl")
    if container.exists():
        return container
    return settings.root_dir.parent / "outputs" / "sentiment_cache.jsonl"


def _load_sentiment_series() -> pd.Series:
    """
    Read the parent project's sentiment JSONL (mounted read-only). Latest
    entry per date wins. Missing file or empty cache returns an empty Series
    so callers fillna(0) cleanly and historical bars remain usable.
    """
    path = _sentiment_cache_path()
    if not path.exists():
        logger.info("sentiment cache not mounted at %s — feature will be zeros", path)
        return pd.Series(dtype=float, name="sent_score")
    by_date: dict[str, float] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                by_date[rec["date"]] = float(rec["avg_score"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    if not by_date:
        return pd.Series(dtype=float, name="sent_score")
    ser = pd.Series(by_date, name="sent_score")
    ser.index = pd.to_datetime(ser.index)
    return ser.sort_index()


# ── Fetch ────────────────────────────────────────────────────────────────────

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
    # Newer yfinance returns a MultiIndex (field, ticker); collapse to single level.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _fetch_fred(series_id: str) -> pd.Series:
    """FRED's public CSV endpoint — no API key required for the daily series."""
    url = FRED_CSV.format(series=series_id)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"].rename(series_id)


# ── Feature engineering ──────────────────────────────────────────────────────

def _add_price_features(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    out = df.copy()
    out["ret_1d"]  = out["Close"].pct_change()
    out["ret_5d"]  = out["Close"].pct_change(5)
    out["vol_20d"] = out["ret_1d"].rolling(lookback).std()
    out["ema_10"]  = out["Close"].ewm(span=10).mean()
    out["ema_50"]  = out["Close"].ewm(span=50).mean()
    out["ema_gap"] = (out["ema_10"] - out["ema_50"]) / out["ema_50"]
    out["rsi_14"]  = _rsi(out["Close"], 14)
    out["atr_14"]     = _atr(out["High"], out["Low"], out["Close"], 14)
    out["atr_pct_14"] = out["atr_14"] / out["Close"]
    return out


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average True Range — Wilder smoothing. Scale for vol-aware labelling."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── Public API ───────────────────────────────────────────────────────────────

def build_feature_matrix(
    lookback_days: int = 400,
    feature_lookback: int | None = None,
) -> pd.DataFrame:
    """
    Assemble a training-ready feature matrix. Caches to parquet for the API
    layer to read without re-running the full fetch.
    """
    feature_lookback = feature_lookback or settings.default_lookback
    logger.info("fetching gold / macro data (lookback=%sd)", lookback_days)

    gold = _fetch_yf(YF_SYMBOLS["GOLD"], lookback_days)
    dxy  = _fetch_yf(YF_SYMBOLS["DXY"],  lookback_days)["Close"].rename("dxy")
    vix  = _fetch_yf(YF_SYMBOLS["VIX"],  lookback_days)["Close"].rename("vix")

    fred_frames = {name: _fetch_fred(sid) for name, sid in FRED_SERIES.items()}
    fred_df = pd.concat(fred_frames.values(), axis=1)
    fred_df.columns = list(fred_frames.keys())

    feat = _add_price_features(gold, feature_lookback)
    feat = feat.join(dxy).join(vix).join(fred_df, how="left")
    feat[list(FRED_SERIES)] = feat[list(FRED_SERIES)].ffill()

    # Macro momentum — next-day direction responds to flow, not level.
    feat["dxy_ret_5d"]     = feat["dxy"].pct_change(5)
    feat["vix_ret_5d"]     = feat["vix"].pct_change(5)
    feat["real10y_chg_5d"] = feat["REAL10Y"].diff(5)
    feat["cpi_yoy"]        = feat["CPI"].pct_change(252)

    # Sentiment feature — parent project's live cache. Historical bars
    # predate the cache, so fillna(0) treats them as neutral. As the cache
    # grows forward, sentiment_z10 gains signal for future holdout windows.
    sent = _load_sentiment_series()
    feat["sent_score"]    = sent.reindex(feat.index).fillna(0.0)
    feat["sentiment_z10"] = (
        (feat["sent_score"] - feat["sent_score"].rolling(10, min_periods=3).mean())
        / (feat["sent_score"].rolling(10, min_periods=3).std() + 1e-9)
    ).fillna(0.0)

    feat["y_next_ret"] = feat["Close"].shift(-1) / feat["Close"] - 1.0
    feat["y_next_dir"] = (feat["y_next_ret"] > 0).astype(int)

    # Snapshot rows with valid features but unknown next-day target — these are
    # the live-inference rows. dropna() below removes them from the training
    # cache; the API reads this sidecar to predict tomorrow's direction.
    feature_cols = [c for c in feat.columns if c not in TARGET_COLS]
    live_mask = (
        feat[feature_cols].notna().all(axis=1)
        & feat[TARGET_COLS].isna().any(axis=1)
    )
    live_row = feat.loc[live_mask, feature_cols]
    if not live_row.empty:
        live_row.to_parquet(settings.data_dir / LIVE_ROW_NAME)

    feat = feat.dropna().copy()

    cache_path = settings.data_dir / CACHE_NAME
    feat.to_parquet(cache_path)
    logger.info("feature matrix cached → %s (%d rows, %d cols); live rows=%d",
                cache_path, len(feat), feat.shape[1], len(live_row))
    return feat


def load_cached_frame() -> pd.DataFrame | None:
    path: Path = settings.data_dir / CACHE_NAME
    if not path.exists():
        return None
    return pd.read_parquet(path)
