"""
Feature engineering — OHLCV-proxy order-flow metrics plus price-movement
and support/resistance features.

When no tick data is available every "buy volume" / "sell volume" number
is a *proxy* derived from candle shape. Proxies are:

  CLV split (primary, Chaikin 1966 close-location value):
      clv      = ((C - L) - (H - C)) / (H - L)
      buy_vol  = V * (1 + clv) / 2
      sell_vol = V * (1 - clv) / 2

  Tick-rule fallback (when H==L or CLV undefined):
      sign     = +1 if C_t > C_{t-1}, -1 if <, 0 if ==
      buy_vol  = V if sign>0 else V/2 if sign==0 else 0

CVD is the cumulative sum of (buy_vol - sell_vol). All outputs flag
`proxy_mode=True` so downstream consumers can discount accordingly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg


# ── Order-flow proxies ───────────────────────────────────────────────────────

def compute_clv(df: pd.DataFrame) -> pd.Series:
    """Close-Location Value in [-1, +1]. NaN where High == Low."""
    rng = df["High"] - df["Low"]
    clv = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / rng
    # Mask zero-range bars (H==L) so caller can fall back to tick rule.
    return clv.where(rng > 0)


def add_orderflow_proxies(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add buy_vol, sell_vol, delta, delta_ratio, cvd, cvd_z.

    If real per-bar buy/sell volume is provided via columns `buy_vol_real`
    and `sell_vol_real` (e.g. from the Binance aggTrade adapter), use those
    directly — that's TRUE order flow, not a proxy. Otherwise fall back to
    CLV-weighted split with tick-rule on zero-range bars.

    Guarantees: buy_vol + sell_vol == Volume (up to float error) for every
    row where Volume > 0. Zero-volume bars get all-zero flow columns.
    """
    out = df.copy()

    clv = compute_clv(out)
    vol = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)

    has_real = ("buy_vol_real" in out.columns and
                "sell_vol_real" in out.columns)

    # Tick-rule fallback for bars where CLV is NaN (H == L).
    sign = np.sign(out["Close"].diff().fillna(0.0))
    tick_buy_share = pd.Series(0.5, index=out.index)
    tick_buy_share[sign > 0] = 1.0
    tick_buy_share[sign < 0] = 0.0

    clv_buy_share = (1 + clv) / 2       # [0, 1] where clv defined
    buy_share = clv_buy_share.fillna(tick_buy_share)

    proxy_buy  = vol * buy_share
    proxy_sell = vol * (1.0 - buy_share)

    if has_real:
        real_buy  = pd.to_numeric(out["buy_vol_real"],  errors="coerce")
        real_sell = pd.to_numeric(out["sell_vol_real"], errors="coerce")
        # Per-bar mask: use real where both columns present, else proxy
        mask = real_buy.notna() & real_sell.notna()
        out["buy_vol"]  = real_buy.where(mask, proxy_buy)
        out["sell_vol"] = real_sell.where(mask, proxy_sell)
        out["bar_proxy_mode"] = (~mask).astype(int)
    else:
        out["buy_vol"]  = proxy_buy
        out["sell_vol"] = proxy_sell
        out["bar_proxy_mode"] = 1

    out["delta"]    = out["buy_vol"] - out["sell_vol"]

    # delta_ratio is undefined when volume is zero — use 0 (neutral).
    with np.errstate(divide="ignore", invalid="ignore"):
        out["delta_ratio"] = np.where(vol > 0, out["delta"] / vol, 0.0)

    out["cvd"] = out["delta"].cumsum()

    roll = out["cvd"].rolling(50, min_periods=10)
    mean = roll.mean()
    std  = roll.std().replace(0, np.nan)
    out["cvd_z"] = ((out["cvd"] - mean) / std).fillna(0.0)

    out["clv"] = clv.fillna(0.0)
    return out


# ── Price-movement features ──────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR as a Series (not just a scalar like market.indicators._atr)."""
    high = df["High"]
    low  = df["Low"]
    prev = df["Close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def add_price_features(
    df: pd.DataFrame,
    atr_period: int = 14,
    forward_bars: int = 1,
) -> pd.DataFrame:
    """
    Add candle-shape + forward-return features.

    forward_bars controls fwd_ret_n / fwd_atr_move so the same helper can
    be reused for different TFs with different label horizons.
    """
    out = df.copy()
    rng = out["High"] - out["Low"]

    out["atr"]     = _atr(out, atr_period)
    out["atr_pct"] = np.where(out["Close"] > 0, out["atr"] / out["Close"] * 100, 0.0)

    body = (out["Close"] - out["Open"]).abs()
    upper_wick = out["High"] - out[["Close", "Open"]].max(axis=1)
    lower_wick = out[["Close", "Open"]].min(axis=1) - out["Low"]
    out["body"]       = body
    out["upper_wick"] = upper_wick.clip(lower=0)
    out["lower_wick"] = lower_wick.clip(lower=0)
    out["body_atr"]   = np.where(out["atr"] > 0, body / out["atr"], 0.0)

    # Forward returns (next-bar and N-bar) — used for labels, NOT features.
    # Columns are prefixed fwd_* so the leakage guard can strip them.
    out["fwd_ret_1"] = out["Close"].shift(-1) / out["Close"] - 1
    out["fwd_ret_n"] = out["Close"].shift(-forward_bars) / out["Close"] - 1
    out["fwd_atr_move"] = (out["Close"].shift(-forward_bars) - out["Close"]) / out["atr"].replace(0, np.nan)
    out["fwd_atr_move"] = out["fwd_atr_move"].fillna(0.0)

    return out


# ── Support / Resistance features ────────────────────────────────────────────

def add_sr_features(df: pd.DataFrame, lookback: int | None = None) -> pd.DataFrame:
    """
    Rolling recent-high/low with ATR-normalized distances and a near_sr_flag.
    Uses *past* bars only (shift(1)) — no lookahead.
    """
    lookback = lookback or of_cfg.RULE_SR_LOOKBACK
    out = df.copy()

    recent_high = out["High"].rolling(lookback, min_periods=5).max().shift(1)
    recent_low  = out["Low"].rolling(lookback, min_periods=5).min().shift(1)

    out["recent_high"] = recent_high
    out["recent_low"]  = recent_low

    atr_safe = out["atr"].replace(0, np.nan)
    out["dist_to_recent_high"] = (recent_high - out["Close"]).abs()
    out["dist_to_recent_low"]  = (out["Close"] - recent_low).abs()
    # Use a large finite sentinel (not inf) so downstream sklearn/xgb accept
    # the frame without special handling. 99 ATR multiples is effectively
    # "not near" without polluting numerics.
    _FAR = 99.0
    out["dist_to_recent_high_atr"] = (out["dist_to_recent_high"] / atr_safe).fillna(_FAR).replace([np.inf, -np.inf], _FAR)
    out["dist_to_recent_low_atr"]  = (out["dist_to_recent_low"]  / atr_safe).fillna(_FAR).replace([np.inf, -np.inf], _FAR)

    near_high = out["dist_to_recent_high_atr"] < of_cfg.RULE_SR_ATR_MULT
    near_low  = out["dist_to_recent_low_atr"]  < of_cfg.RULE_SR_ATR_MULT
    out["near_sr_flag"] = (near_high | near_low).astype(int)
    return out


# ── Multi-TF join ────────────────────────────────────────────────────────────

_HIGHER_TF_COLS = ["delta_ratio", "cvd_z", "atr_pct"]


def build_feature_matrix(
    multi_tf: dict[str, pd.DataFrame],
    anchor_tf: str | None = None,
) -> pd.DataFrame:
    """
    Join higher-timeframe features onto the anchor timeframe.

    Each higher-TF column is forward-filled from the last *closed* higher-TF
    bar to avoid leakage — we use merge_asof with direction='backward', which
    picks the most recent higher bar whose timestamp is <= the anchor bar.
    """
    anchor_tf = anchor_tf or of_cfg.OF_ANCHOR_TF
    if anchor_tf not in multi_tf:
        raise ValueError(f"Anchor TF {anchor_tf} missing from multi_tf dict")

    def _norm_index(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            idx = pd.to_datetime(idx, utc=True, errors="coerce")
        elif idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        # merge_asof requires identical datetime units; force ns.
        df.index = idx.astype("datetime64[ns, UTC]")
        return df.sort_index()

    base = _norm_index(multi_tf[anchor_tf])
    base["_anchor_ts"] = base.index

    for tf, higher in multi_tf.items():
        if tf == anchor_tf:
            continue
        cols = [c for c in _HIGHER_TF_COLS if c in higher.columns]
        if not cols:
            continue
        h = _norm_index(higher[cols])
        h["_higher_ts"] = h.index

        merged = pd.merge_asof(
            base.reset_index(drop=True),
            h.reset_index(drop=True),
            left_on="_anchor_ts",
            right_on="_higher_ts",
            direction="backward",
            suffixes=("", f"_{tf}"),
        )
        # Rename the pulled higher-TF columns with the tf suffix where merge
        # did not already disambiguate (happens only when the column already
        # existed on the anchor frame).
        for c in cols:
            suffixed = f"{c}_{tf}"
            if suffixed not in merged.columns and c in merged.columns:
                merged = merged.rename(columns={c: suffixed})
        base = merged.drop(columns=["_higher_ts"], errors="ignore")
        base.index = base["_anchor_ts"]

    base = base.drop(columns=["_anchor_ts"], errors="ignore")
    return base


# ── Pipeline ─────────────────────────────────────────────────────────────────

def build_features_for_tf(
    df: pd.DataFrame,
    timeframe: str,
    atr_period: int = 14,
) -> pd.DataFrame:
    """One-shot: proxies + price features + S/R features for a single TF."""
    forward_bars = of_cfg.OF_FORWARD_BARS.get(timeframe, 1)
    out = add_orderflow_proxies(df)
    out = add_price_features(out, atr_period=atr_period, forward_bars=forward_bars)
    out = add_sr_features(out)
    return out
