"""
Trade setup computation.

Given a directional signal and gold market indicators, computes:
  entry_price, stop_loss, take_profit,
  risk_amount, reward_amount, risk_reward_ratio,
  trade_valid, trade_decision

Rules:
  BUY  setup: stop below min(EMA20, 14d_low)  with a 0.5% buffer
  SELL setup: stop above max(EMA20, 14d_high) with a 0.5% buffer
  Take profit is placed to satisfy exactly the minimum RR.
  If the geometry is invalid (entry already past invalidation), returns NO_TRADE.

This module computes levels only. It does not place orders.
"""

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def compute(signal: str, gold_ind: dict | None, tf: dict | None = None) -> dict:
    """Compute trade setup for the given signal and gold indicators.

    tf – optional timeframe profile from config.TIMEFRAME_PROFILES.
         Provides stop_buffer_pct and min_rr overrides.
    """
    if gold_ind is None:
        return _no_trade(None, "Gold market data unavailable", tf)

    entry = gold_ind["current"]

    if signal in ("STRONG_BUY", "BUY"):
        return _buy(entry, gold_ind, tf)
    if signal in ("STRONG_SELL", "SELL"):
        return _sell(entry, gold_ind, tf)

    return _no_trade(entry, f"Signal is {signal} — no directional setup", tf)


# ── BUY setup ─────────────────────────────────────────────────────────────────

def _buy(entry: float, ind: dict, tf: dict | None) -> dict:
    """
    Entry  : current gold price
    Stop   : below min(EMA20, recent_low) with stop_buffer_pct cushion
    Target : entry + min_rr * risk
    """
    stop_buf = tf["stop_buffer_pct"] if tf else config.STOP_BUFFER_PCT
    min_rr   = tf["min_rr"]          if tf else config.MIN_RR

    invalidation = min(ind["ema20"], ind["recent_low_14d"])
    stop = invalidation * (1.0 - stop_buf)
    risk = entry - stop

    if risk <= 0:
        return _no_trade(entry, "Entry is at or below the invalidation level — BUY not valid", tf)

    risk = max(risk, entry * config.MIN_RISK_PCT)
    stop = entry - risk

    tp = entry + min_rr * risk
    rr = (tp - entry) / risk

    logger.info(f"BUY  entry={entry:.2f}  stop={stop:.2f}  tp={tp:.2f}  rr={rr:.2f}")
    return _fmt(entry, stop, tp, risk, min_rr)


# ── SELL setup ────────────────────────────────────────────────────────────────

def _sell(entry: float, ind: dict, tf: dict | None) -> dict:
    """
    Entry  : current gold price
    Stop   : above max(EMA20, recent_high) with stop_buffer_pct cushion
    Target : entry - min_rr * risk
    """
    stop_buf = tf["stop_buffer_pct"] if tf else config.STOP_BUFFER_PCT
    min_rr   = tf["min_rr"]          if tf else config.MIN_RR

    invalidation = max(ind["ema20"], ind["recent_high_14d"])
    stop = invalidation * (1.0 + stop_buf)
    risk = stop - entry

    if risk <= 0:
        return _no_trade(entry, "Entry is at or above the invalidation level — SELL not valid", tf)

    risk = max(risk, entry * config.MIN_RISK_PCT)
    stop = entry + risk

    tp = entry - min_rr * risk
    rr = (entry - tp) / risk

    logger.info(f"SELL entry={entry:.2f}  stop={stop:.2f}  tp={tp:.2f}  rr={rr:.2f}")
    return _fmt(entry, stop, tp, risk, min_rr)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(entry: float, stop: float, tp: float, risk: float, min_rr: float) -> dict:
    reward = abs(tp - entry)
    rr     = reward / risk
    valid  = rr >= min_rr
    return {
        "trade_decision":      "TRADE" if valid else "NO_TRADE",
        "entry_price":         round(entry,  2),
        "stop_loss":           round(stop,   2),
        "take_profit":         round(tp,     2),
        "risk_amount":         round(risk,   2),
        "reward_amount":       round(reward, 2),
        "risk_reward_ratio":   round(rr,     2),
        "minimum_required_rr": min_rr,
        "trade_valid":         valid,
        "setup_note":          None,
    }


def _no_trade(entry: float | None, reason: str, tf: dict | None) -> dict:
    min_rr = tf["min_rr"] if tf else config.MIN_RR
    logger.info(f"NO_TRADE: {reason}")
    return {
        "trade_decision":      "NO_TRADE",
        "entry_price":         round(entry, 2) if entry is not None else None,
        "stop_loss":           None,
        "take_profit":         None,
        "risk_amount":         None,
        "reward_amount":       None,
        "risk_reward_ratio":   None,
        "minimum_required_rr": min_rr,
        "trade_valid":         False,
        "setup_note":          reason,
    }
