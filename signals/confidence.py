"""
Confidence level calculation.

HIGH   — 3+ factors clearly agree with the gold trend direction
MEDIUM — 2 factors agree
LOW    — mixed, flat, or degraded by data quality

Auto-downgraded when:
  - article body scraping failed entirely (headline-only analysis)
  - fewer than 5 unique articles were available
  - 2+ market data sources could not be fetched
"""

from utils.logger import setup_logger

logger = setup_logger(__name__)


def compute(signal_result: dict, data_quality: dict) -> str:
    gold_score  = signal_result.get("gold_trend_score", 0)
    dxy_score   = signal_result.get("dxy_score",        0)
    yield_score = signal_result.get("yield_score",      0)
    sent_score  = signal_result.get("sentiment_score",  0)

    # Gold trend is the dominant factor — it defines the reference direction
    if gold_score > 0:
        direction = 1
    elif gold_score < 0:
        direction = -1
    else:
        direction = 0   # sideways gold → low base alignment

    if direction == 0:
        aligned = 0
    else:
        others  = [dxy_score, yield_score, sent_score]
        aligned = sum(
            1 for s in others
            if s != 0 and (s > 0) == (direction > 0)
        )

    if aligned >= 3:
        base = "HIGH"
    elif aligned >= 2:
        base = "MEDIUM"
    else:
        base = "LOW"

    # Downgrade for data quality issues
    scraped   = data_quality.get("successfully_scraped", 0)
    articles  = data_quality.get("unique_articles",      0)
    mkt_fails = data_quality.get("market_data_failures", 0)

    if scraped == 0 or articles < 5:
        base = _downgrade(base)
        logger.info("Confidence reduced: headline-only or too few articles")

    if mkt_fails >= 2:
        base = _downgrade(base)
        logger.info(f"Confidence reduced: {mkt_fails} market data fetch failures")

    return base


def _downgrade(level: str) -> str:
    return {"HIGH": "MEDIUM", "MEDIUM": "LOW"}.get(level, "LOW")
