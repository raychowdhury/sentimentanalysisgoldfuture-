"""
Walk-forward backtest engine.

Replays the live signal pipeline over historical OHLCV bars:
  1. Fetch gold / DXY / yield / VIX once for the full window.
  2. For each bar t (after warmup), slice every series to rows[:t+1].
     This mirrors what the live algo would have seen at the close of bar t.
  3. Compute indicators, score, generate signal + trade setup.
  4. Forward-simulate the trade on bars t+1 .. t+max_hold.
  5. Return a list of closed-trade records.

Overlap rule: at most one open position at a time. After a trade enters at
bar t and exits at bar x, new signals on bars t+1 .. x are ignored.

Regime tag: each trade is labeled "bull" / "bear" / "flat" based on the
gold 60-bar slope at entry. Metrics can be grouped by regime downstream.

Sentiment is pulled from sentiment/cache.py per-bar by date. Bars that have
no cached entry fall back to neutral (None → 0). RSS-based backfill is not
possible, so early backtests remain market-data-only; coverage grows as the
live pipeline runs.
"""

from __future__ import annotations

import pandas as pd

import config
from market import data_fetcher, indicators, trend_scoring
from sentiment import cache as sentiment_cache
from signals import signal_engine, trade_setup
from utils.logger import setup_logger

logger = setup_logger(__name__)


WARMUP_BARS = 60            # min history before first signal (covers EMA50)
MAX_HOLD_BARS_DEFAULT = 20  # forced exit horizon
REGIME_WINDOW = 60          # bars used to classify bull/bear/flat
REGIME_THRESHOLD_PCT = 5.0  # ±5% over REGIME_WINDOW marks a trending regime


def _slice(df: pd.DataFrame | None, end_idx: int) -> pd.DataFrame | None:
    if df is None:
        return None
    return df.iloc[: end_idx + 1]


def _score_all(inds: dict, tf: dict | None) -> dict:
    return {
        "dxy":   trend_scoring.score_dxy(inds.get("dxy"), tf),
        "yield": trend_scoring.score_yield(inds.get("yield_10y"), tf),
        "gold":  trend_scoring.score_gold(inds.get("gold"), tf),
        "vix":   trend_scoring.score_vix(inds.get("vix")),
        "vwap":  trend_scoring.score_vwap(inds.get("gold")),
        "vp":    trend_scoring.score_volume_profile(inds.get("gold")),
    }


def _regime(gold_df: pd.DataFrame, idx: int) -> str:
    start = max(0, idx - REGIME_WINDOW)
    base  = float(gold_df["Close"].iloc[start])
    now   = float(gold_df["Close"].iloc[idx])
    if base == 0:
        return "flat"
    chg = (now - base) / base * 100
    if chg >  REGIME_THRESHOLD_PCT:
        return "bull"
    if chg < -REGIME_THRESHOLD_PCT:
        return "bear"
    return "flat"


def _simulate(
    gold_df: pd.DataFrame,
    entry_idx: int,
    setup: dict,
    direction: str,
    max_hold: int,
) -> tuple[dict, int]:
    """
    Walk bars entry_idx+1 .. entry_idx+max_hold.
    Returns (exit_record, exit_bar_idx).

    Baseline:
      BUY : stop if Low <= stop_loss ; TP if High >= take_profit
      SELL: stop if High >= stop_loss ; TP if Low  <= take_profit

    Trailing stop (when config.TRAIL_ENABLED):
      After unrealized move >= TRAIL_ACTIVATE_R × initial_risk, the stop is
      pulled to peak − TRAIL_ATR_MULT × ATR. Stop only moves in the trade's
      favor.

    Partial TP / scale-out (when config.PARTIAL_TP_ENABLED):
      When unrealized move reaches PARTIAL_TP_R × initial_risk, close
      PARTIAL_TP_FRACTION of the position at that level and move the stop on
      the remainder to breakeven (entry). Remaining fraction continues to the
      original take-profit. pnl returned is the blended result across both
      portions, so R-multiples remain comparable to non-partial trades.

    If both stop and TP hit on the same bar → pessimistic: stop assumed first.
    """
    entry = setup["entry_price"]
    stop  = setup["stop_loss"]
    tp    = setup["take_profit"]
    atr   = (setup.get("level2") or {}).get("atr") or 0.0

    initial_risk = abs(entry - stop)
    trail_on     = getattr(config, "TRAIL_ENABLED", False) and atr > 0
    activate_r   = getattr(config, "TRAIL_ACTIVATE_R", 1.0)
    atr_mult     = getattr(config, "TRAIL_ATR_MULT",   1.5)

    partial_on   = getattr(config, "PARTIAL_TP_ENABLED", False)
    partial_r    = getattr(config, "PARTIAL_TP_R", 1.5)
    partial_frac = getattr(config, "PARTIAL_TP_FRACTION", 0.5)
    partial_taken    = False
    realized_pnl     = 0.0           # pnl already banked from scale-out
    remaining_frac   = 1.0

    if direction == "BUY":
        partial_level = entry + partial_r * initial_risk
    else:
        partial_level = entry - partial_r * initial_risk

    peak_favor = 0.0
    last_idx = min(entry_idx + max_hold, len(gold_df) - 1)

    for i in range(entry_idx + 1, last_idx + 1):
        bar = gold_df.iloc[i]
        high, low = float(bar["High"]), float(bar["Low"])

        if direction == "BUY":
            peak_favor = max(peak_favor, high - entry)
            if trail_on and peak_favor >= activate_r * initial_risk:
                new_stop = (entry + peak_favor) - atr_mult * atr
                if new_stop > stop:
                    stop = new_stop
            # Partial TP fires before stop/TP logic — price reached +partial_r R.
            if partial_on and not partial_taken and high >= partial_level:
                realized_pnl += (partial_level - entry) * partial_frac
                remaining_frac = 1.0 - partial_frac
                partial_taken = True
                stop = max(stop, entry)  # lock breakeven on remainder
            hit_stop = low  <= stop
            hit_tp   = high >= tp
        else:
            peak_favor = max(peak_favor, entry - low)
            if trail_on and peak_favor >= activate_r * initial_risk:
                new_stop = (entry - peak_favor) + atr_mult * atr
                if new_stop < stop:
                    stop = new_stop
            if partial_on and not partial_taken and low <= partial_level:
                realized_pnl += (entry - partial_level) * partial_frac
                remaining_frac = 1.0 - partial_frac
                partial_taken = True
                stop = min(stop, entry)
            hit_stop = high >= stop
            hit_tp   = low  <= tp

        if hit_stop:
            base_reason = "TRAIL" if stop != setup["stop_loss"] and stop != entry else "STOP"
            if partial_taken and stop == entry:
                base_reason = "PARTIAL+BE"
            elif partial_taken:
                base_reason = f"PARTIAL+{base_reason}"
            return _close_blended(
                entry, stop, base_reason, i, gold_df, direction,
                realized_pnl, remaining_frac,
            ), i
        if hit_tp:
            reason = "PARTIAL+TP" if partial_taken else "TP"
            return _close_blended(
                entry, tp, reason, i, gold_df, direction,
                realized_pnl, remaining_frac,
            ), i

    exit_px = float(gold_df.iloc[last_idx]["Close"])
    reason = "PARTIAL+TIME" if partial_taken else "TIME"
    return _close_blended(
        entry, exit_px, reason, last_idx, gold_df, direction,
        realized_pnl, remaining_frac,
    ), last_idx


def _close_blended(
    entry: float, exit_px: float, reason: str,
    exit_idx: int, gold_df: pd.DataFrame, direction: str,
    realized_pnl: float, remaining_frac: float,
) -> dict:
    """Close the remaining fraction and blend with any partial already banked."""
    per_unit = (exit_px - entry) if direction == "BUY" else (entry - exit_px)
    total_pnl = realized_pnl + per_unit * remaining_frac
    return {
        "exit_price":  round(exit_px, 4),
        "exit_reason": reason,
        "exit_date":   gold_df.index[exit_idx].date().isoformat(),
        "exit_idx":    exit_idx,
        "pnl":         round(total_pnl, 4),
    }


def _resolve_profile(timeframe: str | dict) -> tuple[str, dict]:
    """Accept either a profile name (looked up in config) or a raw dict."""
    if isinstance(timeframe, dict):
        return timeframe.get("_name", "custom"), timeframe
    return timeframe, config.TIMEFRAME_PROFILES[timeframe]


def run(
    timeframe: str | dict = "swing",
    lookback_days: int = 730,
    max_hold: int | None = None,
    series: dict[str, pd.DataFrame | None] | None = None,
    allow_overlap: bool = False,
) -> list[dict]:
    """
    Run a walk-forward backtest.

    timeframe      – profile name ("swing" / "day") or raw profile dict.
    lookback_days  – calendar days of history to fetch (ignored if series provided).
    max_hold       – max bars a trade stays open. If None, uses the profile's
                     own max_hold, falling back to MAX_HOLD_BARS_DEFAULT.
    series         – pre-fetched {name: DataFrame} to avoid re-downloading.
    allow_overlap  – if False (default), skip signals while a trade is open.
    """
    tf_name, tf = _resolve_profile(timeframe)
    if max_hold is None:
        max_hold = tf.get("max_hold", MAX_HOLD_BARS_DEFAULT)

    logger.info(f"Backtest starting — timeframe={tf_name} lookback={lookback_days}d")
    if series is None:
        series = data_fetcher.fetch_all(lookback_days)
    gold_df = series.get("gold")
    if gold_df is None or len(gold_df) < WARMUP_BARS + 5:
        logger.error("Not enough gold history for backtest")
        return []

    trades: list[dict] = []
    n = len(gold_df)
    logger.info(f"Walking forward over {n - WARMUP_BARS} bars")

    # Pre-load sentiment cache once (may be empty on first runs).
    sent_cache = sentiment_cache.load()
    if sent_cache:
        logger.info(f"Sentiment cache: {len(sent_cache)} day(s) available")

    next_available_idx = WARMUP_BARS

    for t in range(WARMUP_BARS, n - 1):
        if not allow_overlap and t < next_available_idx:
            continue

        sliced = {k: _slice(v, t) for k, v in series.items()}
        inds = {
            "gold":      indicators.compute(sliced["gold"],      "gold",      tf),
            "dxy":       indicators.compute(sliced["dxy"],       "dxy",       tf),
            "yield_10y": indicators.compute(sliced["yield_10y"], "yield_10y", tf),
            "vix":       indicators.compute(sliced["vix"],       "vix",       tf),
        }
        if inds["gold"] is None:
            continue

        scores = _score_all(inds, tf)
        bar_date  = gold_df.index[t].date().isoformat()
        avg_sent  = sent_cache.get(bar_date)  # None when uncovered → neutral
        gold_ind  = inds["gold"]
        macro_bullish = (
            gold_ind["current"] > gold_ind["sma200"]
            if gold_ind.get("sma200") is not None else None
        )
        sig = signal_engine.run(
            avg_sentiment=avg_sent,
            dxy_score=scores["dxy"],
            yield_score=scores["yield"],
            gold_score=scores["gold"],
            vix_score=scores["vix"],
            vwap_score=scores["vwap"],
            vp_score=scores["vp"],
            macro_bullish=macro_bullish,
        )
        signal = sig["signal"]
        if signal not in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"):
            continue

        setup = trade_setup.compute(signal, inds["gold"], tf)
        if not setup.get("trade_valid"):
            continue

        direction = "BUY" if "BUY" in signal else "SELL"
        result, exit_idx = _simulate(gold_df, t, setup, direction, max_hold)
        next_available_idx = exit_idx + 1

        trades.append({
            "entry_date":   gold_df.index[t].date().isoformat(),
            "timeframe":    tf_name,
            "regime":       _regime(gold_df, t),
            "signal":       signal,
            "raw_signal":   sig["raw_signal"],
            "veto_applied": sig["veto_applied"],
            "total_score":  sig["total_score"],
            "direction":    direction,
            "entry_price":  setup["entry_price"],
            "stop_loss":    setup["stop_loss"],
            "take_profit":  setup["take_profit"],
            "risk":         setup["risk_amount"],
            "planned_rr":   setup["risk_reward_ratio"],
            "tp_source":    setup["tp_source"],
            **result,
        })

    logger.info(f"Backtest complete — {len(trades)} trades")
    return trades
