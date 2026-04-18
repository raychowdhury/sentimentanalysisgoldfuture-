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


def score_dxy(ind: dict | None, tf: dict | None = None) -> int:
    """
    DXY rising → bearish for gold (negative score).
    DXY falling → bullish for gold (positive score).

    tf – optional timeframe profile; overrides config thresholds when provided.
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
    strong      = tf["dxy_strong_pct"] if tf else config.DXY_STRONG_MOVE_PCT
    mild        = tf["dxy_mild_pct"]   if tf else config.DXY_MILD_MOVE_PCT

    if above_ema20 and above_ema50 and ret > strong:
        return -2
    if above_ema20 and ret > mild:
        return -1
    if not above_ema20 and not above_ema50 and ret < -strong:
        return +2
    if not above_ema20 and ret < -mild:
        return +1
    return 0


def score_yield(ind: dict | None, tf: dict | None = None) -> int:
    """
    Yields rising → bearish for gold (negative score).
    Uses absolute change in yield level (percentage points / basis-point proxy).

    tf – optional timeframe profile; overrides config thresholds when provided.
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
    strong      = tf["yield_strong"] if tf else config.YIELD_STRONG_MOVE
    mild        = tf["yield_mild"]   if tf else config.YIELD_MILD_MOVE

    if above_ema20 and above_ema50 and chg > strong:
        return -2
    if above_ema20 and chg > mild:
        return -1
    if not above_ema20 and not above_ema50 and chg < -strong:
        return +2
    if not above_ema20 and chg < -mild:
        return +1
    return 0


def score_vwap(ind: dict | None) -> int:
    """
    Gold price vs. rolling VWAP.

    Above VWAP = institutional bullish bias (+).
    Below VWAP = institutional bearish bias (−).

    Returns: -2 … 0 … +2
    """
    if ind is None or ind.get("vwap") is None:
        logger.warning("VWAP unavailable — defaulting to 0")
        return 0

    current = ind["current"]
    vwap    = ind["vwap"]
    if vwap == 0:
        return 0

    dev_pct = (current - vwap) / vwap * 100   # % deviation from VWAP

    strong = config.VWAP_DEVIATION_STRONG
    mild   = config.VWAP_DEVIATION_MILD

    if dev_pct >= strong:
        return +2
    if dev_pct >= mild:
        return +1
    if dev_pct <= -strong:
        return -2
    if dev_pct <= -mild:
        return -1
    return 0


def score_volume_profile(ind: dict | None) -> int:
    """
    Current price position relative to the Volume Profile value area.

    Above VAH → breakout above accepted range  → +2 (strong bullish)
    POC–VAH   → upper value area               → +1
    VAL–POC   → lower value area               → -1
    Below VAL → breakdown below accepted range → -2 (strong bearish)

    Returns: -2 … 0 … +2
    """
    if ind is None or ind.get("vol_poc") is None:
        logger.warning("Volume Profile unavailable — defaulting to 0")
        return 0

    current = ind["current"]
    poc     = ind["vol_poc"]
    vah     = ind.get("vah")
    val     = ind.get("val")

    if vah is None or val is None:
        return 0

    if current > vah:
        return +2
    if current > poc:
        return +1
    if current > val:
        return -1
    return -2


def score_vix(ind: dict | None) -> int:
    """
    VIX (CBOE Volatility Index) score.

    High VIX = market fear = safe-haven demand → bullish for gold (+).
    Low VIX  = complacency = risk-on environment → mildly bearish (−).

    Scoring is level-based (current value vs. fixed thresholds):
        VIX >= VIX_FEAR_STRONG  → +2
        VIX >= VIX_FEAR_MILD    → +1
        VIX >= VIX_CALM         →  0  (normal range)
        VIX <  VIX_CALM         → -1  (complacency)

    Returns: -1 … 0 … +2
    """
    if ind is None:
        logger.warning("VIX indicators unavailable — defaulting to 0")
        return 0

    level = ind["current"]

    if level >= config.VIX_FEAR_STRONG:
        return +2
    if level >= config.VIX_FEAR_MILD:
        return +1
    if level >= config.VIX_CALM:
        return 0
    return -1


def score_gold(ind: dict | None, tf: dict | None = None) -> int:
    """
    Gold trend score — the dominant factor.

    tf – optional timeframe profile; overrides config thresholds when provided.
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
    strong      = tf["gold_strong_pct"] if tf else config.GOLD_STRONG_MOVE_PCT
    mild        = tf["gold_mild_pct"]   if tf else config.GOLD_MILD_MOVE_PCT

    if above_ema20 and above_ema50 and ret > strong:
        return +3
    if above_ema20 and ret > mild:
        return +1
    if not above_ema20 and not above_ema50 and ret < -strong:
        return -3
    if not above_ema20 and ret < -mild:
        return -1
    return 0
