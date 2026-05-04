"""Path-correct triple-barrier labels.

Walks fine 1m bars in order to find first-touch. Same logic as backtest,
so training labels match backtest reality.

For each coarse bar t with ATR_t and Close_t:
    Long  TP = entry + TP_ATR_MULT * ATR
    Long  SL = entry - SL_ATR_MULT * ATR
    Walk fine bars [t+1, t+horizon*coarse_min] in order.
    First fine bar with H>=TP and not L<=SL  -> y_long = 1
    First fine bar with L<=SL and not H>=TP  -> y_long = 0
    Both same fine bar                        -> y_long = 0 (conservative)
    No touch                                  -> y_long = 0
"""
import numpy as np
import pandas as pd

from ml_engine import config
from ml_engine.data_loader import load


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False, min_periods=n).mean()


_SCHEMA_TO_MIN = {"ohlcv-1m": 1, "ohlcv-15m": 15, "ohlcv-1h": 60}


def build(symbol: str, schema: str) -> pd.DataFrame:
    """Path-correct labels using 1m bars for first-touch detection."""
    coarse = load(symbol, schema)
    fine = load(symbol, "ohlcv-1m")

    coarse_min = _SCHEMA_TO_MIN[schema]
    horizon = config.HORIZON_BARS
    window_min = coarse_min * horizon

    atr = _atr(coarse, 14)
    entry = coarse["Close"]
    tp_long = entry + config.TP_ATR_MULT * atr
    sl_long = entry - config.SL_ATR_MULT * atr
    tp_short = entry - config.TP_ATR_MULT * atr
    sl_short = entry + config.SL_ATR_MULT * atr

    # Convert fine bars to numpy for fast slicing
    fine_idx = fine.index.values  # datetime64[ns]
    fine_high = fine["High"].values
    fine_low = fine["Low"].values

    coarse_idx = coarse.index.values
    n_coarse = len(coarse_idx)

    y_long = np.full(n_coarse, np.nan)
    y_short = np.full(n_coarse, np.nan)

    win_ns = np.timedelta64(int(window_min * 60 * 1e9), "ns")
    one_ns = np.timedelta64(0, "ns")

    # Pointer for fine_idx — advances as coarse_idx advances (both sorted)
    j_start = 0
    for i in range(n_coarse):
        if not np.isfinite(atr.iloc[i]):
            continue
        t = coarse_idx[i]
        end_t = t + win_ns
        # Advance j_start until fine_idx[j_start] > t
        while j_start < len(fine_idx) and fine_idx[j_start] <= t:
            j_start += 1
        # Find end pointer
        j_end = j_start
        while j_end < len(fine_idx) and fine_idx[j_end] <= end_t:
            j_end += 1
        if j_end <= j_start:
            continue

        h_slice = fine_high[j_start:j_end]
        l_slice = fine_low[j_start:j_end]

        TPL, SLL = tp_long.iloc[i], sl_long.iloc[i]
        TPS, SLS = tp_short.iloc[i], sl_short.iloc[i]

        # Long: first fine bar where H>=TP or L<=SL
        long_tp_hit = h_slice >= TPL
        long_sl_hit = l_slice <= SLL
        long_any = long_tp_hit | long_sl_hit
        if long_any.any():
            k = np.argmax(long_any)  # first True index
            if long_tp_hit[k] and not long_sl_hit[k]:
                y_long[i] = 1.0
            else:
                y_long[i] = 0.0
        else:
            y_long[i] = 0.0

        short_tp_hit = l_slice <= TPS
        short_sl_hit = h_slice >= SLS
        short_any = short_tp_hit | short_sl_hit
        if short_any.any():
            k = np.argmax(short_any)
            if short_tp_hit[k] and not short_sl_hit[k]:
                y_short[i] = 1.0
            else:
                y_short[i] = 0.0
        else:
            y_short[i] = 0.0

    out = pd.DataFrame({
        "y_long": y_long,
        "y_short": y_short,
        "atr": atr.values,
        "entry": entry.values,
        "tp_long": tp_long.values,
        "sl_long": sl_long.values,
        "tp_short": tp_short.values,
        "sl_short": sl_short.values,
    }, index=coarse.index)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--schema", default=config.SCHEMA_15M)
    args = ap.parse_args()
    out = build(args.symbol, args.schema)
    print(f"rows: {len(out)}, valid y_long: {out.y_long.notna().sum()}, "
          f"y_long_mean={out.y_long.mean():.3f}, y_short_mean={out.y_short.mean():.3f}")
