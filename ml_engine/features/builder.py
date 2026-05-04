"""OHLCV-derived features. No leakage: only past bars.

Set include_macro=True to add daily macro overlays (DFII10, VIX, DXY).
"""
import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(span=n, adjust=False, min_periods=n).mean()
    dn = (-d).clip(lower=0).ewm(span=n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def build(df: pd.DataFrame, include_macro: bool = False,
          symbol: str | None = None) -> pd.DataFrame:
    """Return feature frame aligned with df.index. Drops warmup NaNs at caller.

    include_macro: add DFII10/VIX/DXY + COT z-score (when symbol given).
    """
    f = pd.DataFrame(index=df.index)
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]

    # Returns over multiple lookbacks
    for n in (1, 3, 5, 10, 20):
        f[f"ret_{n}"] = c.pct_change(n)

    # EMA slopes / distance
    for n in (10, 20, 50):
        ema = _ema(c, n)
        f[f"ema{n}_dist"] = (c - ema) / c
        f[f"ema{n}_slope"] = ema.diff(3) / c

    # Volatility
    atr14 = _atr(df, 14)
    f["atr_14"] = atr14
    f["atr_pct"] = atr14 / c
    f["range_pct"] = (h - l) / c
    f["body_pct"] = (c - df["Open"]).abs() / c

    # Momentum
    f["rsi_14"] = _rsi(c, 14)

    # Volume regime
    vma = v.rolling(20, min_periods=20).mean()
    f["vol_z"] = (v - vma) / v.rolling(20, min_periods=20).std()

    # Higher-high / lower-low context
    f["hh_20"] = (h == h.rolling(20, min_periods=20).max()).astype(int)
    f["ll_20"] = (l == l.rolling(20, min_periods=20).min()).astype(int)

    # Session / time features
    idx = df.index
    f["hour"] = idx.hour if hasattr(idx, "hour") else 0
    f["dow"] = idx.dayofweek if hasattr(idx, "dayofweek") else 0

    if include_macro:
        from ml_engine.features.macro import build as build_macro
        m = build_macro(df.index)
        for col in m.columns:
            f[col] = m[col].values
        if symbol:
            from ml_engine.features.cot import build as build_cot
            c = build_cot(symbol, df.index)
            for col in c.columns:
                f[col] = c[col].values

    return f
