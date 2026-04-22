"""
Stock signal engine — turn factor scores into a directional label + veto.

Mirrors the gold engine's philosophy:
  • total score maps to STRONG_BUY / BUY / HOLD / SELL / STRONG_SELL
  • a veto downgrades strong labels when trend strongly disagrees with
    the directional bias implied by sentiment (prevents "buy the news"
    calls on a clearly broken tape, and vice versa)
"""

from __future__ import annotations


LABELS = ("STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL")


def _label_from_total(total: int) -> str:
    if total >=  4: return "STRONG_BUY"
    if total >=  2: return "BUY"
    if total <= -4: return "STRONG_SELL"
    if total <= -2: return "SELL"
    return "HOLD"


def run(scores: dict) -> dict:
    """
    `scores` keys expected:
      news_sentiment, stock_trend, relative_strength,
      market_regime, volume_momentum, total
    """
    total = int(scores.get("total", 0))
    raw   = _label_from_total(total)

    trend     = int(scores.get("stock_trend", 0))
    sentiment = int(scores.get("news_sentiment", 0))

    veto     = False
    veto_msg = None

    # Buy family vetoed when the tape is clearly down (stock below both EMAs
    # with a strong negative 5d return → trend = -3). Same direction logic as
    # gold: don't buy into a broken chart even if sentiment and SPY are green.
    if raw in ("BUY", "STRONG_BUY") and trend <= -2:
        veto = True
        veto_msg = "trend strongly negative — buy vetoed"

    # Sell family vetoed when the tape is clearly up.
    if raw in ("SELL", "STRONG_SELL") and trend >= 2:
        veto = True
        veto_msg = "trend strongly positive — sell vetoed"

    # Strong-label sanity: news must not be diametrically opposed.
    if raw == "STRONG_BUY" and sentiment <= -1:
        veto = True
        veto_msg = "news sentiment negative — STRONG_BUY downgraded"
    if raw == "STRONG_SELL" and sentiment >= 1:
        veto = True
        veto_msg = "news sentiment positive — STRONG_SELL downgraded"

    final = "HOLD" if veto else raw

    return {
        "signal":       final,
        "raw_signal":   raw,
        "total_score":  total,
        "veto_applied": veto,
        "veto_reason":  veto_msg,
    }
