"""
Core signal engine.

Combines four scored factors into a final directional bias:
  sentiment_score  (from news pipeline)  : -2 … +2
  dxy_score        (market)              : -2 … +2
  yield_score      (market)              : -2 … +2
  gold_trend_score (market, dominant)    : -3 … +3

Total possible range: -9 … +9.

Signal thresholds:
  total >= 4           → STRONG_BUY
  total 2–3            → BUY
  total -1 to 1        → HOLD
  total -3 to -2       → SELL
  total <= -4          → STRONG_SELL

Veto rules prevent contradictory signals (e.g. BUY while gold is downtrending).
"""

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


def _map_total(total: int) -> str:
    if total >= 4:
        return "STRONG_BUY"
    if total >= 2:
        return "BUY"
    if total <= -4:
        return "STRONG_SELL"
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
) -> dict:
    """
    Compute the full signal result.

    Returns a dict with all component scores, total, raw signal,
    final signal, and whether a veto was applied.
    """
    s_score = sentiment_score(avg_sentiment)
    total   = s_score + dxy_score + yield_score + gold_score
    raw     = _map_total(total)
    final, vetoed = _veto(raw, gold_score, dxy_score, yield_score)

    logger.info(
        f"Scores — sentiment:{s_score:+d}  dxy:{dxy_score:+d}  "
        f"yield:{yield_score:+d}  gold:{gold_score:+d}  total:{total:+d}"
    )
    logger.info(f"Signal: {raw} → {final}  (veto={vetoed})")

    return {
        "sentiment_score":  s_score,
        "dxy_score":        dxy_score,
        "yield_score":      yield_score,
        "gold_trend_score": gold_score,
        "total_score":      total,
        "raw_signal":       raw,
        "signal":           final,
        "veto_applied":     vetoed,
    }
