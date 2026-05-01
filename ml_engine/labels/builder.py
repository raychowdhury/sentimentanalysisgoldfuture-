"""Triple-barrier labels: forward window touches TP first => 1, SL first => 0.

For each bar t with ATR_t:
    long_TP = Close_t + TP_ATR_MULT * ATR_t
    long_SL = Close_t - SL_ATR_MULT * ATR_t
Walk forward HORIZON_BARS bars; whichever barrier hits first decides label.
If neither hits within window: label = 1 if final close > entry else 0.

Output: DataFrame with columns: y_long, y_short, tp_long, sl_long, tp_short, sl_short.
"""
import numpy as np
import pandas as pd

from ml_engine import config


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False, min_periods=n).mean()


def build(df: pd.DataFrame) -> pd.DataFrame:
    """Triple-barrier on long + short. Vectorized with rolling max/min over future window."""
    horizon = config.HORIZON_BARS
    atr = _atr(df, 14)
    entry = df["Close"]

    tp_long = entry + config.TP_ATR_MULT * atr
    sl_long = entry - config.SL_ATR_MULT * atr
    tp_short = entry - config.TP_ATR_MULT * atr
    sl_short = entry + config.SL_ATR_MULT * atr

    # Future windows (shift -1 then rolling forward via reversed view)
    # Use rolling on reversed series for forward look.
    fut_high = df["High"].iloc[::-1].rolling(horizon, min_periods=1).max().iloc[::-1].shift(-1)
    fut_low = df["Low"].iloc[::-1].rolling(horizon, min_periods=1).min().iloc[::-1].shift(-1)

    # Approx: if fut max >= TP and fut min > SL (in window) => TP hit safely.
    # Strictly we'd need first-touch order; approximation OK for training signal.
    y_long = ((fut_high >= tp_long) & (fut_low > sl_long)).astype(float)
    # Ambiguous case (both touched) => use ratio of distance
    both_long = (fut_high >= tp_long) & (fut_low <= sl_long)
    dist_tp = (tp_long - entry).abs()
    dist_sl = (entry - sl_long).abs()
    y_long = y_long.where(~both_long, (dist_tp < dist_sl).astype(float))
    # No barrier hit => fallback to forward return sign at horizon
    fwd_close = df["Close"].shift(-horizon)
    no_hit_long = (fut_high < tp_long) & (fut_low > sl_long)
    y_long = y_long.where(~no_hit_long, (fwd_close > entry).astype(float))

    y_short = ((fut_low <= tp_short) & (fut_high < sl_short)).astype(float)
    both_short = (fut_low <= tp_short) & (fut_high >= sl_short)
    dist_tp_s = (entry - tp_short).abs()
    dist_sl_s = (sl_short - entry).abs()
    y_short = y_short.where(~both_short, (dist_tp_s < dist_sl_s).astype(float))
    no_hit_short = (fut_low > tp_short) & (fut_high < sl_short)
    y_short = y_short.where(~no_hit_short, (fwd_close < entry).astype(float))

    out = pd.DataFrame({
        "y_long": y_long,
        "y_short": y_short,
        "atr": atr,
        "entry": entry,
        "tp_long": tp_long,
        "sl_long": sl_long,
        "tp_short": tp_short,
        "sl_short": sl_short,
    }, index=df.index)
    return out
