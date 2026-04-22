"""
Confidence scoring for stock signals.

Rules (per spec):
  < 3 unique articles                     → LOW
  3-4 unique articles                     → downgrade one level
  > 60% scrape failures                   → downgrade one level
  missing stock OHLCV                     → LOW
  missing SPY or VIX                      → downgrade one level
  factor agreement < 2 of 5 with signal   → LOW
"""

from __future__ import annotations


_LEVELS = ["HIGH", "MEDIUM", "LOW"]


def _downgrade(level: str, steps: int = 1) -> str:
    idx = _LEVELS.index(level)
    return _LEVELS[min(len(_LEVELS) - 1, idx + steps)]


def _sign(n: int) -> int:
    if n > 0: return  1
    if n < 0: return -1
    return 0


def _agreement_count(scores: dict, signal: str) -> int:
    """
    Count factors whose sign matches the signal direction.
    HOLD gets factor count of non-zero factors / 2 (unused — we only gate
    non-HOLD confidence on agreement, HOLD stays at whatever level the
    degradation chain produced).
    """
    if signal in ("BUY", "STRONG_BUY"):
        want = 1
    elif signal in ("SELL", "STRONG_SELL"):
        want = -1
    else:
        return 99  # HOLD bypasses the agreement floor
    keys = ("news_sentiment", "stock_trend", "relative_strength",
            "market_regime", "volume_momentum")
    return sum(1 for k in keys if _sign(int(scores.get(k, 0))) == want)


def compute(
    signal: str,
    scores: dict,
    unique_articles: int,
    total_scrapes: int,
    failed_scrapes: int,
    stock_ok: bool,
    spy_ok: bool,
    vix_ok: bool,
) -> str:
    """
    Start HIGH; apply downgrades until one of the LOW rules fires or the
    chain finishes. Hard-LOW conditions short-circuit.
    """
    # Hard-LOW conditions
    if not stock_ok:
        return "LOW"
    if unique_articles < 3:
        return "LOW"
    if _agreement_count(scores, signal) < 2:
        return "LOW"

    level = "HIGH"

    if 3 <= unique_articles <= 4:
        level = _downgrade(level)

    if total_scrapes > 0:
        failure_rate = failed_scrapes / total_scrapes
        if failure_rate > 0.60:
            level = _downgrade(level)

    if not spy_ok or not vix_ok:
        level = _downgrade(level)

    return level
