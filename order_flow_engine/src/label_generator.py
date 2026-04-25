"""
Label generation for supervised training.

Takes a frame already enriched by feature_engineering and rule_engine and
assigns one of six class labels per bar by combining rule hits at t with
what happened over the next H bars.

A reversal counts only if the forward move exceeds OF_LABEL_HORIZON_ATR
multiples of ATR — this filters noise from genuine regime shifts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg

LABEL_CLASSES = of_cfg.LABEL_CLASSES


def generate_labels(df: pd.DataFrame, timeframe: str) -> pd.Series:
    """
    Return a Series of label strings aligned with df.index.

    Expects columns: Close, recent_high, recent_low, atr, atr_pct,
    fwd_ret_n, r1..r7 rule hits.
    """
    horizon = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    close = df["Close"]

    fwd_ret_n = df["fwd_ret_n"].fillna(0.0)
    atr = df["atr"].replace(0, np.nan)
    # Magnitude of forward move expressed in ATR multiples — orientation-free
    # so the same gate works for buyer_absorption (expect drop) and
    # seller_absorption (expect rise).
    fwd_move_atr = ((fwd_ret_n.abs() * close) / atr).fillna(0.0)
    fwd_real_reversal = fwd_move_atr > of_cfg.OF_LABEL_HORIZON_ATR

    atr_pct_frac = (df["atr_pct"] / 100).fillna(0.0)

    # Post-bar close sequence check for trap confirmation — the next 3 bars
    # must stay on the trap side.
    stays_below_high = pd.concat(
        [close.shift(-i) < df["recent_high"] for i in (1, 2, 3)],
        axis=1,
    ).all(axis=1).fillna(False)
    stays_above_low = pd.concat(
        [close.shift(-i) > df["recent_low"] for i in (1, 2, 3)],
        axis=1,
    ).all(axis=1).fillna(False)

    labels = pd.Series("normal_behavior", index=df.index, dtype="object")

    bull_trap = df["r5_bull_trap"].fillna(False) & stays_below_high
    bear_trap = df["r6_bear_trap"].fillna(False) & stays_above_low
    buyer_abs = df["r3_absorption_resistance"].fillna(False) & (fwd_ret_n < 0) & fwd_real_reversal
    seller_abs = df["r4_absorption_support"].fillna(False) & (fwd_ret_n > 0) & fwd_real_reversal

    reversal = (
        (df["r1_buyer_down"].fillna(False) |
         df["r2_seller_up"].fillna(False)  |
         df["r7_cvd_divergence"].fillna(False)) &
        fwd_real_reversal
    )

    # Precedence: traps > absorption > reversal > normal. Traps are the most
    # specific pattern; absorption is the most specific flow signal; reversal
    # is the catch-all for non-specific divergence.
    labels = labels.mask(reversal, "possible_reversal")
    labels = labels.mask(buyer_abs, "buyer_absorption")
    labels = labels.mask(seller_abs, "seller_absorption")
    labels = labels.mask(bull_trap, "bullish_trap")
    labels = labels.mask(bear_trap, "bearish_trap")

    # Force "normal" back on rows where rule_hit_count==0 AND forward move is
    # tiny. This prevents accidentally labelling coincidental reversals.
    tiny_move = df["fwd_ret_n"].abs() < (0.3 * atr_pct_frac)
    no_rules  = df["rule_hit_count"].fillna(0) == 0
    labels = labels.mask(no_rules & tiny_move, "normal_behavior")

    return labels


def label_distribution(labels: pd.Series) -> dict[str, int]:
    """Count each label, ensuring all six classes appear (0-filled)."""
    counts = labels.value_counts().to_dict()
    return {cls: int(counts.get(cls, 0)) for cls in LABEL_CLASSES}


# Columns that must NOT be fed to the model — they leak future info.
# r1..r4 (CONFIRMATION_RULES) internally consume fwd_ret_1 and are therefore
# leaky as features. r5/r6/r7 are causal and safe.
_LEAKAGE_PREFIXES = ("fwd_",)
_LEAKAGE_NAMES = {"label", "rule_hit_codes", "recent_high", "recent_low",
                  "dist_to_recent_high", "dist_to_recent_low",
                  "cvd_price_corr",
                  "r1_buyer_down", "r2_seller_up",
                  "r3_absorption_resistance", "r4_absorption_support",
                  # total rule_hit_count sums r1..r4 too → leaky; the
                  # rule_hit_count_causal alias is the safe feature.
                  "rule_hit_count"}
# Raw OHLCV excluded to keep model from memorizing absolute price.
_DROP_RAW = {"Open", "High", "Low", "Close", "Volume", "Adj Close",
             "Dividends", "Stock Splits"}


def feature_columns(df: pd.DataFrame) -> list[str]:
    """Columns safe to use as features after leakage/raw-price scrub."""
    keep = []
    for c in df.columns:
        if c in _LEAKAGE_NAMES or c in _DROP_RAW:
            continue
        if any(c.startswith(p) for p in _LEAKAGE_PREFIXES):
            continue
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        keep.append(c)
    return keep
