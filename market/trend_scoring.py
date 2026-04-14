"""
Convert market indicator snapshots into integer scores for the signal engine.

Score ranges:
  DXY / Yield  :  -2 to +2  (inverse relationship with gold)
  Gold trend   :  -3 to +3  (direct relationship)

Scoring philosophy:
  Both EMA position AND recent momentum must agree for a "strong" call.
  If only one condition holds, the move is "mild".
"""

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def score_dxy(ind: dict | None) -> int:
    """
    DXY rising → bearish for gold (negative score).
    DXY falling → bullish for gold (positive score).

    Returns: -2 (strong headwind) … 0 (flat) … +2 (strong tailwind)
    """
    if ind is None:
        logger.warning("DXY indicators unavailable — defaulting to 0")
        return 0

    current     = ind["current"]
    ema20       = ind["ema20"]
    ema50       = ind["ema50"]
    ret         = ind["return_5d_pct"]

    above_ema20 = current > ema20
    above_ema50 = current > ema50
    strong      = config.DXY_STRONG_MOVE_PCT
    mild        = config.DXY_MILD_MOVE_PCT

    if above_ema20 and above_ema50 and ret > strong:
        return -2
    if above_ema20 and ret > mild:
        return -1
    if not above_ema20 and not above_ema50 and ret < -strong:
        return +2
    if not above_ema20 and ret < -mild:
        return +1
    return 0


def score_yield(ind: dict | None) -> int:
    """
    Yields rising → bearish for gold (negative score).
    Uses absolute change in yield level (percentage points / basis-point proxy).

    Returns: -2 … 0 … +2
    """
    if ind is None:
        logger.warning("Yield indicators unavailable — defaulting to 0")
        return 0

    current     = ind["current"]
    ema20       = ind["ema20"]
    ema50       = ind["ema50"]
    chg         = ind["abs_change_5d"]   # e.g. +0.15 = +15 bps

    above_ema20 = current > ema20
    above_ema50 = current > ema50
    strong      = config.YIELD_STRONG_MOVE
    mild        = config.YIELD_MILD_MOVE

    if above_ema20 and above_ema50 and chg > strong:
        return -2
    if above_ema20 and chg > mild:
        return -1
    if not above_ema20 and not above_ema50 and chg < -strong:
        return +2
    if not above_ema20 and chg < -mild:
        return +1
    return 0


def score_gold(ind: dict | None) -> int:
    """
    Gold trend score — the dominant factor.

    Returns: -3 (strong downtrend) … 0 (sideways) … +3 (strong uptrend)
    """
    if ind is None:
        logger.warning("Gold indicators unavailable — defaulting to 0")
        return 0

    current     = ind["current"]
    ema20       = ind["ema20"]
    ema50       = ind["ema50"]
    ret         = ind["return_5d_pct"]

    above_ema20 = current > ema20
    above_ema50 = current > ema50
    strong      = config.GOLD_STRONG_MOVE_PCT
    mild        = config.GOLD_MILD_MOVE_PCT

    if above_ema20 and above_ema50 and ret > strong:
        return +3
    if above_ema20 and ret > mild:
        return +1
    if not above_ema20 and not above_ema50 and ret < -strong:
        return -3
    if not above_ema20 and ret < -mild:
        return -1
    return 0
