"""
Per-article weighting for sentiment aggregation (Pillar 1).

Three independent multipliers combine into a single article weight:

    weight = relevance * source_tier * time_decay

- relevance    : topic fit — direct gold/XAU mention scores higher than a
                 macro-only headline. Derived from keyword hits in the title.
- source_tier  : publisher quality — Reuters/Bloomberg/FT/WSJ > MW/CNBC/Kitco
                 > Yahoo/Fortune > aggregators > unknown.
- time_decay   : exponential decay exp(-age_hours / tau). τ varies by
                 timeframe — day mode wants fresher news than swing.

The weighted sentiment mean is then
    avg = Σ(score * weight) / Σ(weight)

This replaces the unweighted mean used previously.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import config

# ── Relevance ────────────────────────────────────────────────────────────────

# Direct metals terms — a hit implies the article is *about* gold, not merely
# relevant to it via macro drivers.
_TIER_A_KEYWORDS: tuple[str, ...] = (
    "gold", "xau", "xauusd", "bullion", "precious metal", "precious metals",
    "comex", "silver", "platinum", "palladium",
)

# Compiled once at import. All GOLD_FILTER_KEYWORDS not in TIER_A become TIER_B.
_TIER_A_RE = [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in _TIER_A_KEYWORDS]


def _tier_b_patterns() -> list[re.Pattern]:
    a_set = {k.lower() for k in _TIER_A_KEYWORDS}
    all_kw = getattr(config, "GOLD_FILTER_KEYWORDS", [])
    return [
        re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE)
        for k in all_kw if k.lower() not in a_set
    ]


_TIER_B_RE = _tier_b_patterns()


def relevance_score(title: str) -> float:
    """
    Map a headline to [0.1, 1.0]:
      direct gold mention   → 0.6 .. 1.0 (scales with extra hits)
      macro-only mention    → 0.3 .. 0.6
      no keyword hits       → 0.1

    Only the title is scored; body text is optional and not always extracted.
    """
    if not title:
        return 0.1
    a_hits = sum(1 for p in _TIER_A_RE if p.search(title))
    b_hits = sum(1 for p in _TIER_B_RE if p.search(title))

    if a_hits >= 1:
        return min(1.0, 0.6 + 0.1 * (a_hits - 1) + 0.05 * b_hits)
    if b_hits >= 1:
        return min(0.6, 0.3 + 0.1 * b_hits)
    return 0.1


# ── Source tier ──────────────────────────────────────────────────────────────

def source_tier_weight(source: str) -> float:
    """
    Substring match against config.SOURCE_TIERS (lowercase keys). First match
    wins; unknown sources fall back to SOURCE_TIER_DEFAULT.
    """
    if not source:
        return float(getattr(config, "SOURCE_TIER_DEFAULT", 0.4))
    low = source.lower()
    tiers: dict[str, float] = getattr(config, "SOURCE_TIERS", {})
    for key, weight in tiers.items():
        if key in low:
            return float(weight)
    return float(getattr(config, "SOURCE_TIER_DEFAULT", 0.4))


# ── Time decay ───────────────────────────────────────────────────────────────

def _parse_published(published: str) -> datetime | None:
    if not published:
        return None
    try:
        dt = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_hours(published: str, now: datetime | None = None) -> float | None:
    dt = _parse_published(published)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return (ref - dt).total_seconds() / 3600.0


def time_decay_weight(published: str, tau_hours: float, now: datetime | None = None) -> float:
    """
    exp(-age/τ), clamped to [exp(-5), 1]. Unparseable timestamps → 0.5 fallback
    (treat as moderately fresh — better than dropping the article).
    """
    age = age_hours(published, now=now)
    if age is None:
        return 0.5
    if age < 0:  # future-dated, treat as fresh
        return 1.0
    decay = math.exp(-age / max(tau_hours, 1.0))
    return max(decay, math.exp(-5.0))  # floor so stale items still count a little


# ── Combined + weighted mean ─────────────────────────────────────────────────

def article_weight(
    title: str,
    source: str,
    published: str,
    tau_hours: float,
    now: datetime | None = None,
) -> dict:
    """Returns the three component weights plus their product."""
    rel = relevance_score(title)
    src = source_tier_weight(source)
    dec = time_decay_weight(published, tau_hours, now=now)
    return {
        "relevance":   round(rel, 4),
        "source_tier": round(src, 4),
        "time_decay":  round(dec, 4),
        "combined":    round(rel * src * dec, 4),
    }


def weighted_mean_score(
    results: list[dict],
    tau_hours: float,
    now: datetime | None = None,
) -> tuple[float | None, float]:
    """
    Compute Σ(score * weight) / Σ(weight) over analyzed articles.
    Returns (weighted_avg, total_weight). Falls back to plain mean when total
    weight is zero (shouldn't happen with the 0.1 relevance floor).

    `relevance` and `source_tier` are expected to already be attached to each
    row (computed once in the pipeline). Time decay is recomputed here so the
    caller can pass any τ.
    """
    num = 0.0
    den = 0.0
    plain_scores: list[float] = []

    for r in results:
        try:
            score = float(r.get("final_score") or 0)
        except (ValueError, TypeError):
            continue
        plain_scores.append(score)

        rel = r.get("relevance")
        src = r.get("source_tier")
        if rel is None or src is None:
            continue
        dec = time_decay_weight(r.get("published", ""), tau_hours, now=now)
        w = float(rel) * float(src) * dec
        num += score * w
        den += w

    if den <= 0:
        if not plain_scores:
            return None, 0.0
        return round(sum(plain_scores) / len(plain_scores), 4), 0.0

    return round(num / den, 4), round(den, 4)
