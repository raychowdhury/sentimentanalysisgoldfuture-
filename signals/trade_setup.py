"""
Trade setup computation.

Entry   : current gold price (market order)
Stop    : invalidation level − (ATR_STOP_MULT × ATR)
           Invalidation = min(EMA20, recent_low)  for BUY
                        = max(EMA20, recent_high) for SELL
           ATR provides a dynamic buffer sized to recent volatility.

Take Profit:
  BUY  → target VAH (Value Area High) when it clears the MIN_RR hurdle,
          otherwise Entry + MIN_RR × Risk
  SELL → target VAL (Value Area Low)  when it clears the MIN_RR hurdle,
          otherwise Entry − MIN_RR × Risk

Level 2 key levels returned alongside the setup:
  vwap, vol_poc, vah, val, tpo_poc, atr
"""

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def compute(signal: str, gold_ind: dict | None, tf: dict | None = None) -> dict:
    """
    Compute trade setup for the given signal and gold indicators.

    tf – optional timeframe profile from config.TIMEFRAME_PROFILES.
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
    Invalidation : min(EMA20, recent_low)
    Stop Loss    : Invalidation − ATR_STOP_MULT × ATR
    Take Profit  : VAH when it clears MIN_RR, else Entry + MIN_RR × Risk
    """
    atr_mult = tf["atr_stop_mult"] if tf else 1.0
    min_rr   = tf["min_rr"]        if tf else config.MIN_RR
    atr      = ind.get("atr", 0) or 0

    invalidation = min(ind["ema20"], ind["recent_low_14d"])
    stop = invalidation - atr_mult * atr

    risk = entry - stop
    if risk <= 0:
        return _no_trade(entry, "Entry is at or below the invalidation level — BUY not valid", tf)

    risk = max(risk, entry * config.MIN_RISK_PCT)
    stop = entry - risk

    # TP: prefer VAH → TPO POC → MIN_RR fallback
    vah = ind.get("vah")
    tpo = ind.get("tpo_poc")
    if vah and vah > entry and (vah - entry) / risk >= min_rr:
        tp     = vah
        tp_src = "VAH (Value Area High)"
    elif tpo and tpo > entry and (tpo - entry) / risk >= min_rr:
        tp     = tpo
        tp_src = "TPO POC (Time at Price)"
    else:
        tp     = entry + min_rr * risk
        tp_src = f"MIN_RR ({min_rr}×R)"

    rr = (tp - entry) / risk
    logger.info(
        f"BUY  entry={entry:.2f}  stop={stop:.2f}  tp={tp:.2f}  "
        f"rr={rr:.2f}  atr={atr:.2f}  tp_src={tp_src}"
    )
    return _fmt(entry, stop, tp, risk, min_rr, tp_src, ind)


# ── SELL setup ────────────────────────────────────────────────────────────────

def _sell(entry: float, ind: dict, tf: dict | None) -> dict:
    """
    Invalidation : max(EMA20, recent_high)
    Stop Loss    : Invalidation + ATR_STOP_MULT × ATR
    Take Profit  : VAL when it clears MIN_RR, else Entry − MIN_RR × Risk
    """
    atr_mult = tf["atr_stop_mult"] if tf else 1.0
    min_rr   = tf["min_rr"]        if tf else config.MIN_RR
    atr      = ind.get("atr", 0) or 0

    invalidation = max(ind["ema20"], ind["recent_high_14d"])
    stop = invalidation + atr_mult * atr

    risk = stop - entry
    if risk <= 0:
        return _no_trade(entry, "Entry is at or above the invalidation level — SELL not valid", tf)

    risk = max(risk, entry * config.MIN_RISK_PCT)
    stop = entry + risk

    # TP: prefer VAL → TPO POC (when below entry) → MIN_RR fallback
    val = ind.get("val")
    tpo = ind.get("tpo_poc")
    if val and val < entry and (entry - val) / risk >= min_rr:
        tp     = val
        tp_src = "VAL (Value Area Low)"
    elif tpo and tpo < entry and (entry - tpo) / risk >= min_rr:
        tp     = tpo
        tp_src = "TPO POC (Time at Price)"
    else:
        tp     = entry - min_rr * risk
        tp_src = f"MIN_RR ({min_rr}×R)"

    rr = (entry - tp) / risk
    logger.info(
        f"SELL entry={entry:.2f}  stop={stop:.2f}  tp={tp:.2f}  "
        f"rr={rr:.2f}  atr={atr:.2f}  tp_src={tp_src}"
    )
    return _fmt(entry, stop, tp, risk, min_rr, tp_src, ind)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(
    entry: float, stop: float, tp: float,
    risk: float, min_rr: float, tp_source: str, ind: dict,
) -> dict:
    reward   = abs(tp - entry)
    rr_raw   = reward / risk
    rr       = round(rr_raw, 4)          # use rounded value for comparison
    valid    = rr >= min_rr              # floating-point safe
    note     = None if valid else (
        f"R:R {round(rr, 2):.2f} does not meet the minimum {min_rr} requirement"
    )
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
        "tp_source":           tp_source,
        "setup_note":          note,
        # Level 2 key levels embedded in the setup for dashboard display
        "level2": {
            "atr":     round(ind.get("atr")     or 0, 2),
            "atr_pct": round(ind.get("atr_pct") or 0, 3),
            "vwap":    round(ind["vwap"], 2)    if ind.get("vwap")    else None,
            "vol_poc": round(ind["vol_poc"], 2) if ind.get("vol_poc") else None,
            "vah":     round(ind["vah"], 2)     if ind.get("vah")     else None,
            "val":     round(ind["val"], 2)     if ind.get("val")     else None,
            "tpo_poc": round(ind["tpo_poc"], 2) if ind.get("tpo_poc") else None,
        },
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
        "tp_source":           None,
        "setup_note":          reason,
        "level2":              None,
    }
