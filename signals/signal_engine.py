"""
Core signal engine.

Combines seven scored factors into a final directional bias. Each raw score
is multiplied by its weight in config.SCORE_WEIGHTS before summing:

  sentiment_score    (from news pipeline)  : -2 … +2
  dxy_score          (market)              : -2 … +2
  yield_score        (market)              : -2 … +2
  gold_trend_score   (market, dominant)    : -3 … +3
  vix_score          (market fear)         : -1 … +2
  vwap_score         (price vs VWAP)       : -2 … +2
  volume_profile_score (value area pos.)   : -2 … +2

Signal thresholds (on weighted total):
  total >= 6           → STRONG_BUY   (requires gold_score >= 2)
  total 2–5.9          → BUY
  total -1.9 to 1.9    → HOLD
  total -5.9 to -2     → SELL
  total <= -6          → STRONG_SELL  (requires gold_score <= -2)

Threshold 6 (vs 4) chosen after backtest: at >=4 STRONG_BUY fired near tops
and posted near-zero expectancy while plain BUY (mid-trend) carried the edge.
Lifting the bar funnels most real entries into BUY and reserves STRONG for
genuine full-alignment signals.

Macro gate: config.LONG_ONLY=True blocks all short signals (default).
Veto rules prevent contradictory signals (e.g. BUY while gold is downtrending).
"""

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def sentiment_score(avg: float | None) -> int:
    """Map average final sentiment score → integer component."""
    if avg is None:
        return 0
    if avg > 0.15:
        return 2
    if avg > 0.05:
        return 1
    if avg >= -0.05:
        return 0
    if avg >= -0.15:
        return -1
    return -2


def _map_total(total: float, gold_score: int = 0) -> str:
    """
    Map weighted total score to signal. STRONG variants require trend
    confirmation: the gold trend itself must register as strong
    (|gold_score| >= 2), otherwise the signal is downgraded to plain BUY/SELL.

    Total is now a float (weighted), so thresholds use >= comparisons.
    """
    if total >= 6:
        return "STRONG_BUY" if gold_score >= 2 else "BUY"
    if total >= 2:
        return "BUY"
    if total <= -6:
        return "STRONG_SELL" if gold_score <= -2 else "SELL"
    if total <= -2:
        return "SELL"
    return "HOLD"


def _veto(
    signal: str,
    gold_score: int,
    dxy_score: int,
    yield_score: int,
) -> tuple[str, bool]:
    """
    Apply guardrail veto rules.

    BUY / STRONG_BUY blocked when:
      - gold trend is negative
      - DXY is strongly rising (dxy_score == -2)
      - yields are strongly rising (yield_score == -2)

    SELL / STRONG_SELL blocked when:
      - gold trend is positive
      - DXY is strongly falling (dxy_score == +2)

    Blocked signals are downgraded to HOLD.
    Returns (final_signal, veto_applied).
    """
    if signal in ("BUY", "STRONG_BUY"):
        if gold_score < 0 or dxy_score == -2 or yield_score == -2:
            logger.info(
                f"BUY veto: gold_trend={gold_score} dxy={dxy_score} yield={yield_score}"
            )
            return "HOLD", True

    if signal in ("SELL", "STRONG_SELL"):
        if gold_score > 0 or dxy_score == 2:
            logger.info(
                f"SELL veto: gold_trend={gold_score} dxy={dxy_score}"
            )
            return "HOLD", True

    return signal, False


def run(
    avg_sentiment: float | None,
    dxy_score: int,
    yield_score: int,
    gold_score: int,
    vix_score: int = 0,
    vwap_score: int = 0,
    vp_score: int = 0,
    macro_bullish: bool | None = None,
) -> dict:
    """
    Compute the full signal result.

    macro_bullish – optional SMA200 regime flag (True = gold above SMA200).
        When config.SMA200_GATE is enabled AND this is False, BUY / STRONG_BUY
        are blocked. None skips the gate (backwards-compat / missing data).

    Returns a dict with all component scores, total, raw signal,
    final signal, and whether a veto was applied.
    """
    s_score = sentiment_score(avg_sentiment)

    w = config.SCORE_WEIGHTS
    total = (
        s_score     * w["sentiment"]
        + dxy_score   * w["dxy"]
        + yield_score * w["yield"]
        + gold_score  * w["gold"]
        + vix_score   * w["vix"]
        + vwap_score  * w["vwap"]
        + vp_score    * w["volume_profile"]
    )
    raw = _map_total(total, gold_score)
    final, vetoed = _veto(raw, gold_score, dxy_score, yield_score)

    # Macro gate: block short side entirely when LONG_ONLY is enabled.
    if getattr(config, "LONG_ONLY", False) and final in ("SELL", "STRONG_SELL"):
        logger.info(f"Long-only gate: {final} → HOLD")
        final = "HOLD"

    # SMA200 regime gate: block longs when gold is below its own SMA200.
    if (
        getattr(config, "SMA200_GATE", False)
        and macro_bullish is False
        and final in ("BUY", "STRONG_BUY")
    ):
        logger.info(f"SMA200 gate: {final} → HOLD (gold below SMA200)")
        final = "HOLD"

    logger.info(
        f"Scores — sentiment:{s_score:+d}  dxy:{dxy_score:+d}  yield:{yield_score:+d}  "
        f"gold:{gold_score:+d}  vix:{vix_score:+d}  vwap:{vwap_score:+d}  vp:{vp_score:+d}  "
        f"total:{total:+.2f}"
    )
    logger.info(f"Signal: {raw} → {final}  (veto={vetoed})")

    return {
        "sentiment_score":         s_score,
        "dxy_score":               dxy_score,
        "yield_score":             yield_score,
        "gold_trend_score":        gold_score,
        "vix_score":               vix_score,
        "vwap_score":              vwap_score,
        "volume_profile_score":    vp_score,
        "total_score":             round(total, 3),
        "raw_signal":              raw,
        "signal":                  final,
        "veto_applied":            vetoed,
    }
