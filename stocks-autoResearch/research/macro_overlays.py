"""
Cross-asset + momentum overlays for the SPX composite.

Exposes two live features not previously in the composite:

  spy_trend(end_date)   → SPY 20d return z-score  (trend/momentum)
  credit_zscore(end_date) → HYG/IEF ratio 60d z-score (risk appetite)

Also provides backtest-friendly series builders that return a full
Date-indexed Series so composite_backtest can vectorize over history.

Free via yfinance. Values clipped to reasonable bounds to stop outliers
from swamping the composite score.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_close(symbol: str, start: date, end: date) -> pd.Series:
    import yfinance as yf
    df = yf.download(
        symbol, start=str(start), end=str(end + timedelta(days=1)),
        progress=False, auto_adjust=False, threads=False,
    )
    if df is None or df.empty:
        return pd.Series(dtype=float, name=symbol)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    s = df["Close"].copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s.name = symbol
    return s


# ── Trend (SPY 20d) ──────────────────────────────────────────────────────────

def spy_trend_series(start: date, end: date, window: int = 20) -> pd.Series:
    """Series of SPY 20d return z-scores over [start, end]."""
    pad = timedelta(days=window * 3 + 30)
    spy = _fetch_close("SPY", start - pad, end)
    if spy.empty:
        return pd.Series(dtype=float, name="spy_trend_z")
    ret = spy.pct_change(window)
    # Rolling mean/std of the 20d-return series itself (60d window).
    mu  = ret.rolling(60).mean()
    sd  = ret.rolling(60).std()
    z   = (ret - mu) / sd
    z   = z.clip(-3, 3)
    z.name = "spy_trend_z"
    return z.loc[str(start):str(end)]


def spy_trend(end_date: date) -> Optional[dict]:
    """Live trend snapshot: latest 20d-return z-score + raw 20d return."""
    try:
        s = spy_trend_series(end_date - timedelta(days=180), end_date)
        if s.empty or s.dropna().empty:
            return None
        z = float(s.dropna().iloc[-1])
        spy = _fetch_close("SPY", end_date - timedelta(days=50), end_date)
        ret20 = float(spy.pct_change(20).dropna().iloc[-1]) if not spy.empty else None
        return {"trend_z": round(z, 3), "ret_20d": round(ret20, 4) if ret20 else None}
    except Exception as e:
        logger.warning(f"spy_trend failed: {e}")
        return None


# ── Credit (HYG/IEF) ─────────────────────────────────────────────────────────

def credit_ratio_series(start: date, end: date, window: int = 60) -> pd.Series:
    """Series of HYG/IEF ratio z-scores over [start, end]."""
    pad = timedelta(days=window * 3 + 30)
    hyg = _fetch_close("HYG", start - pad, end)
    ief = _fetch_close("IEF", start - pad, end)
    if hyg.empty or ief.empty:
        return pd.Series(dtype=float, name="credit_z")
    idx = hyg.index.intersection(ief.index)
    ratio = (hyg.loc[idx] / ief.loc[idx])
    mu = ratio.rolling(window).mean()
    sd = ratio.rolling(window).std()
    z  = (ratio - mu) / sd
    z  = z.clip(-3, 3)
    z.name = "credit_z"
    return z.loc[str(start):str(end)]


def credit_zscore(end_date: date) -> Optional[dict]:
    """Live credit snapshot: latest HYG/IEF z-score (positive = risk-on)."""
    try:
        s = credit_ratio_series(end_date - timedelta(days=240), end_date)
        if s.empty or s.dropna().empty:
            return None
        z = float(s.dropna().iloc[-1])
        return {"credit_z": round(z, 3)}
    except Exception as e:
        logger.warning(f"credit_zscore failed: {e}")
        return None


# ── VIX term slope (VIX / VIX3M) ─────────────────────────────────────────────

def vix_slope_series(start: date, end: date) -> pd.Series:
    """Series of VIX/VIX3M ratios. <1 = contango (normal), >1 = backwardation (stress)."""
    pad = timedelta(days=60)
    vix   = _fetch_close("^VIX",   start - pad, end)
    vix3m = _fetch_close("^VIX3M", start - pad, end)
    if vix.empty or vix3m.empty:
        return pd.Series(dtype=float, name="vix_slope")
    idx = vix.index.intersection(vix3m.index)
    r = (vix.loc[idx] / vix3m.loc[idx])
    r.name = "vix_slope"
    return r.loc[str(start):str(end)]


def vix_slope(end_date: date) -> Optional[dict]:
    """Live VIX term slope. Positive component = high slope = stress."""
    try:
        s = vix_slope_series(end_date - timedelta(days=120), end_date)
        if s.empty or s.dropna().empty:
            return None
        v = float(s.dropna().iloc[-1])
        # Normalize around 0.90 (typical contango). Deviation in z-like units.
        mu = float(s.dropna().rolling(60).mean().iloc[-1])
        sd = float(s.dropna().rolling(60).std().iloc[-1])
        z  = (v - mu) / sd if sd and not pd.isna(sd) and sd > 1e-6 else 0.0
        z  = max(-3.0, min(3.0, z))
        return {"ratio": round(v, 3), "slope_z": round(z, 3)}
    except Exception as e:
        logger.warning(f"vix_slope failed: {e}")
        return None


# ── Event / seasonality features ─────────────────────────────────────────────

def turn_of_month_flag(d: date) -> float:
    """1.0 if date is within last 3 or first 3 trading days of month (~6-day window)."""
    # Approximate: day-of-month ≤ 5 or ≥ 26. Ignoring exact trading-day count.
    dom = d.day
    return 1.0 if (dom <= 5 or dom >= 26) else 0.0


def day_of_week_feat(d: date) -> float:
    """Day of week encoded [-1..+1]. Mon=-1, Fri=+1 (reflects the well-known Mon/Fri asymmetry)."""
    wd = d.weekday()  # 0=Mon, 4=Fri
    if wd >= 5:
        return 0.0
    return (wd - 2) / 2.0


def fomc_proximity(days_away: Optional[int]) -> float:
    """FOMC proximity feature. 1.0 on FOMC day, decays linearly over 5 days, 0 beyond."""
    if days_away is None:
        return 0.0
    if days_away < 0 or days_away > 5:
        return 0.0
    return 1.0 - days_away / 5.0


# ── Component mapping: z-score → ±100 component ──────────────────────────────

def z_to_component(z: float, scale: float = 33.0) -> float:
    """Map a z-score (~[-3,+3]) to a composite component in ±100.

    scale=33 → z=±3 saturates at ±100. z=±1 maps to ±33.
    """
    if z is None or pd.isna(z):
        return 0.0
    return float(max(-100.0, min(100.0, z * scale)))
