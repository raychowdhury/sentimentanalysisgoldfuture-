"""
Score gold positioning from weekly CFTC COT managed-money net data.

Method: rolling 52-week z-score of managed-money NET position
(long − short). Crowded readings fade (contrarian).

Mapping:
    z > +Z_STRONG   crowded long    → -2 (fade: bearish for gold)
    +Z_MILD < z ≤ +Z_STRONG         → -1
    -Z_MILD ≤ z ≤ +Z_MILD           →  0
    -Z_STRONG ≤ z < -Z_MILD         → +1
    z < -Z_STRONG   crowded short   → +2 (fade: bullish for gold)

Factor is 0 most of the time — only fires at statistical extremes. That's
intentional: COT is most useful as a turning-point filter, not a trend
generator.
"""

from __future__ import annotations

import statistics
from datetime import date

Z_STRONG = 2.0
Z_MILD   = 1.0
WINDOW   = 52     # weeks
MIN_SAMPLE = 10


def _parse_date(d) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def compute_zscore_at(
    records: list[dict],
    at: date | str,
    window: int = WINDOW,
) -> float | None:
    """
    z-score of managed-money net at the latest weekly record on or before `at`.
    Returns None when fewer than MIN_SAMPLE prior records exist.
    """
    if not records:
        return None
    at_d = _parse_date(at)
    history = [r for r in records if _parse_date(r["date"]) <= at_d]
    if len(history) < MIN_SAMPLE:
        return None

    sample = [r["mm_net"] for r in history[-window:]]
    if len(sample) < MIN_SAMPLE:
        return None

    mu = statistics.fmean(sample)
    sd = statistics.pstdev(sample)
    if sd == 0:
        return 0.0
    return (history[-1]["mm_net"] - mu) / sd


def score_from_zscore(z: float | None) -> int:
    if z is None:
        return 0
    if z > Z_STRONG:
        return -2
    if z > Z_MILD:
        return -1
    if z < -Z_STRONG:
        return +2
    if z < -Z_MILD:
        return +1
    return 0


def score_at(records: list[dict], at: date | str) -> int:
    """Convenience: z-score + mapping in one call."""
    return score_from_zscore(compute_zscore_at(records, at))
