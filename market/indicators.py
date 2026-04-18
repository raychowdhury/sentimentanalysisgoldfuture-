"""
Technical indicators derived from daily OHLCV DataFrames.
All public functions return plain dicts of scalar values.

Indicators computed:
  EMA (short / long), n-day return, rolling high/low  — original
  ATR (Average True Range)                             — volatility / stop sizing
  VWAP (rolling Volume-Weighted Average Price)         — institutional reference
  Volume Profile (POC, VAH, VAL)                       — high-volume price zones
  TPO Profile (TPO POC)                                — time-at-price zones
"""

import numpy as np
import pandas as pd

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


# ── Private helpers ───────────────────────────────────────────────────────────

def _ema(series: pd.Series, window: int) -> float:
    if len(series) < 2:
        return float(series.iloc[-1])
    return float(series.ewm(span=window, adjust=False).mean().iloc[-1])


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range over `period` bars (Wilder EMA smoothing).
    True Range = max(H-L, |H-PrevC|, |L-PrevC|)
    """
    if len(df) < 2:
        return float(df["High"].iloc[-1] - df["Low"].iloc[-1])

    high  = df["High"]
    low   = df["Low"]
    prev  = df["Close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - prev).abs(),
        (low  - prev).abs(),
    ], axis=1).max(axis=1)

    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def _vwap(df: pd.DataFrame) -> float | None:
    """
    Rolling VWAP over the entire DataFrame window.
    Typical price = (H + L + C) / 3.
    Returns None when volume data is absent or all zero.
    """
    if "Volume" not in df.columns:
        return None

    vol = df["Volume"].replace(0, np.nan)
    if vol.isna().all():
        return None

    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    total_vol = vol.sum(skipna=True)
    if total_vol == 0:
        return None

    return float((tp * vol).sum(skipna=True) / total_vol)


def _volume_profile(
    df: pd.DataFrame,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> tuple[float | None, float | None, float | None]:
    """
    Build a volume profile histogram from daily OHLCV.

    Each bar distributes its volume evenly across the bins it spans
    (Low → High).  Returns (POC, VAH, VAL) or (None, None, None).

    POC = price bin with the most accumulated volume
    Value Area = smallest contiguous set of bins around POC
                 that contains value_area_pct of total volume
    VAH / VAL  = top / bottom of that value area
    """
    if "Volume" not in df.columns or len(df) < 5:
        return None, None, None

    vol = df["Volume"].fillna(0)
    if vol.sum() == 0:
        return None, None, None

    price_min = float(df["Low"].min())
    price_max = float(df["High"].max())
    if price_max <= price_min:
        return None, None, None

    bin_size = (price_max - price_min) / bins
    volumes  = np.zeros(bins)

    for i in range(len(df)):
        lo  = float(df["Low"].iloc[i])
        hi  = float(df["High"].iloc[i])
        v   = float(vol.iloc[i])
        if v <= 0:
            continue
        lo_bin = int((lo - price_min) / bin_size)
        hi_bin = int((hi - price_min) / bin_size)
        lo_bin = max(0, min(lo_bin, bins - 1))
        hi_bin = max(0, min(hi_bin, bins - 1))
        n = hi_bin - lo_bin + 1
        volumes[lo_bin : hi_bin + 1] += v / n

    poc_bin = int(np.argmax(volumes))
    poc     = price_min + (poc_bin + 0.5) * bin_size

    # Expand outward from POC until value_area_pct is covered
    total = volumes.sum()
    target = total * value_area_pct
    lo_idx = hi_idx = poc_bin
    accumulated = volumes[poc_bin]

    while accumulated < target:
        can_go_lo = lo_idx > 0
        can_go_hi = hi_idx < bins - 1
        if not can_go_lo and not can_go_hi:
            break
        lo_vol = volumes[lo_idx - 1] if can_go_lo else 0.0
        hi_vol = volumes[hi_idx + 1] if can_go_hi else 0.0
        # Prefer the larger neighbor; fall back to whichever side is still open.
        go_hi = can_go_hi and (not can_go_lo or hi_vol >= lo_vol)
        if go_hi:
            hi_idx += 1
            accumulated += volumes[hi_idx]
        else:
            lo_idx -= 1
            accumulated += volumes[lo_idx]

    val = price_min + lo_idx * bin_size
    vah = price_min + (hi_idx + 1) * bin_size

    return round(poc, 4), round(vah, 4), round(val, 4)


def _tpo_profile(
    df: pd.DataFrame,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> float | None:
    """
    Time Price Opportunity profile.
    Each daily bar contributes 1 TPO unit distributed evenly across its
    High-Low range (equal weight, ignoring volume).

    Returns TPO POC (price level with most time spent).
    """
    if len(df) < 5:
        return None

    price_min = float(df["Low"].min())
    price_max = float(df["High"].max())
    if price_max <= price_min:
        return None

    bin_size = (price_max - price_min) / bins
    tpo_counts = np.zeros(bins)

    for i in range(len(df)):
        lo  = float(df["Low"].iloc[i])
        hi  = float(df["High"].iloc[i])
        lo_bin = int((lo - price_min) / bin_size)
        hi_bin = int((hi - price_min) / bin_size)
        lo_bin = max(0, min(lo_bin, bins - 1))
        hi_bin = max(0, min(hi_bin, bins - 1))
        n = hi_bin - lo_bin + 1
        tpo_counts[lo_bin : hi_bin + 1] += 1.0 / n

    poc_bin = int(np.argmax(tpo_counts))
    return round(price_min + (poc_bin + 0.5) * bin_size, 4)


# ── Public API ────────────────────────────────────────────────────────────────

def compute(df: pd.DataFrame | None, name: str = "", tf: dict | None = None) -> dict | None:
    """
    Compute all indicators from a daily OHLCV DataFrame.

    tf  – optional timeframe profile dict from config.TIMEFRAME_PROFILES.

    Returns a dict with:
      EMA / momentum / high-low:
        current, ema20, ema50, return_5d_pct, abs_change_5d,
        recent_high_14d, recent_low_14d
      ATR:
        atr          – Average True Range (absolute)
        atr_pct      – ATR as % of current price
      VWAP:
        vwap         – rolling Volume-Weighted Avg Price (None if no volume)
      Volume Profile:
        vol_poc      – Point of Control (highest-volume price)
        vah          – Value Area High  (top of 70% value area)
        val          – Value Area Low   (bottom of 70% value area)
      TPO:
        tpo_poc      – Time POC (price where most time was spent)

    Returns None if data is missing or too short to be useful.
    """
    if df is None or len(df) < 5:
        if name:
            logger.warning(f"{name}: insufficient data for indicators")
        return None

    ema_short     = tf["ema_short"]       if tf else config.EMA_SHORT
    ema_long      = tf["ema_long"]        if tf else config.EMA_LONG
    return_window = tf["return_window"]   if tf else config.RETURN_WINDOW_DAYS
    hl_window     = tf["high_low_window"] if tf else 14

    close   = df["Close"]
    current = float(close.iloc[-1])

    ema20 = _ema(close, ema_short)
    ema50 = _ema(close, min(ema_long, len(close)))
    # SMA200 — macro regime reference. Computed over whatever history is
    # available; returns None when < 50 bars (too little signal).
    sma200 = float(close.rolling(min(200, len(close))).mean().iloc[-1]) if len(close) >= 50 else None

    n    = min(return_window, len(close) - 1)
    base = float(close.iloc[-(n + 1)])
    return_pct = ((current - base) / base * 100) if base != 0 else 0.0
    abs_change = current - base

    w       = min(hl_window, len(df))
    high_nd = float(df["High"].iloc[-w:].max())
    low_nd  = float(df["Low"].iloc[-w:].min())

    # ATR
    atr     = _atr(df, config.ATR_PERIOD)
    atr_pct = (atr / current * 100) if current != 0 else 0.0

    # VWAP
    vwap = _vwap(df)

    # Volume Profile
    vol_poc, vah, val = _volume_profile(df, config.VP_BINS, config.VP_VALUE_AREA_PCT)

    # TPO
    tpo_poc = _tpo_profile(df, config.VP_BINS, config.VP_VALUE_AREA_PCT)

    return {
        # ── original ──────────────────────────────
        "current":          round(current,    4),
        "ema20":            round(ema20,      4),
        "ema50":            round(ema50,      4),
        "sma200":           round(sma200,     4) if sma200 is not None else None,
        "return_5d_pct":    round(return_pct, 4),
        "abs_change_5d":    round(abs_change, 4),
        "recent_high_14d":  round(high_nd,   4),
        "recent_low_14d":   round(low_nd,    4),
        # ── ATR ───────────────────────────────────
        "atr":              round(atr,        4),
        "atr_pct":          round(atr_pct,    4),
        # ── VWAP ──────────────────────────────────
        "vwap":             round(vwap, 4) if vwap is not None else None,
        # ── Volume Profile ─────────────────────────
        "vol_poc":          vol_poc,
        "vah":              vah,
        "val":              val,
        # ── TPO ───────────────────────────────────
        "tpo_poc":          tpo_poc,
    }
