"""
Stock factor scoring — five factors, each returning an integer score.

Factor ranges (mirrors spec):
  news_sentiment    : -2..+2
  stock_trend       : -3..+3
  relative_strength : -2..+2   (vs SPY, 5d return delta)
  market_regime     : -2..+2   (SPY trend + VIX level)
  volume_momentum   : -1..+1

Every scorer tolerates missing inputs by returning 0. That way one bad
data fetch degrades conviction (via confidence) rather than crashing the
signal path.
"""

from __future__ import annotations


# ── News sentiment (-2..+2) ───────────────────────────────────────────────────

def score_sentiment(avg_score: float | None) -> int:
    """Map article-average sentiment score to -2..+2 bucket."""
    if avg_score is None:
        return 0
    try:
        s = float(avg_score)
    except (TypeError, ValueError):
        return 0
    if s >=  0.15: return  2
    if s >=  0.05: return  1
    if s <= -0.15: return -2
    if s <= -0.05: return -1
    return 0


# ── Stock trend (-3..+3) ──────────────────────────────────────────────────────

def score_trend(ind: dict | None) -> int:
    """
    EMA20 / EMA50 position + 5d return bucket. Dominant factor — same
    -3..+3 range gold uses so the stock engine inherits the "trend must
    agree for STRONG signals" property.
    """
    if not ind:
        return 0
    price = ind.get("current")
    e20   = ind.get("ema20")
    e50   = ind.get("ema50")
    ret   = ind.get("return_5d_pct")
    if None in (price, e20, e50, ret):
        return 0

    above20 = price > e20
    above50 = price > e50
    below20 = price < e20
    below50 = price < e50

    if above20 and above50 and ret >  1.5: return  3
    if above20           and ret >  0.5: return  1
    if below20 and below50 and ret < -1.5: return -3
    if below20           and ret < -0.5: return -1
    return 0


# ── Relative strength vs SPY (-2..+2) ─────────────────────────────────────────

def score_relative_strength(stock_ind: dict | None, spy_ind: dict | None) -> int:
    """
    Delta of 5-day return vs SPY. Captures whether the stock is outperforming
    or lagging the tape on the same horizon.
    """
    if not stock_ind or not spy_ind:
        return 0
    s = stock_ind.get("return_5d_pct")
    m = spy_ind.get("return_5d_pct")
    if s is None or m is None:
        return 0
    delta = s - m
    if delta >=  3.0: return  2
    if delta >=  1.0: return  1
    if delta <= -3.0: return -2
    if delta <= -1.0: return -1
    return 0


# ── Market regime (-2..+2) ────────────────────────────────────────────────────

def score_regime(spy_ind: dict | None, vix_ind: dict | None) -> int:
    """
    Broad risk-on / risk-off read from SPY trend + VIX level.

    SPY above EMA50 + low VIX  = risk-on, score positive.
    SPY below EMA50 + high VIX = risk-off, score negative.
    Elevated VIX always caps a positive score.
    """
    spy_above = None
    if spy_ind and spy_ind.get("current") is not None and spy_ind.get("ema50") is not None:
        spy_above = spy_ind["current"] > spy_ind["ema50"]

    vix = vix_ind.get("current") if vix_ind else None

    if spy_above is None and vix is None:
        return 0

    # High VIX dominates — panic regime is bearish for individual longs
    if vix is not None and vix >= 30:
        return -2
    if spy_above is True and vix is not None and vix < 18:
        return 2
    if spy_above is True and (vix is None or vix < 25):
        return 1
    if spy_above is False and vix is not None and vix >= 22:
        return -1
    if spy_above is False:
        return -1
    return 0


# ── Volume / momentum confirmation (-1..+1) ───────────────────────────────────

def score_volume_momentum(vol_ratio: float | None, return_1d_pct: float | None) -> int:
    """
    Volume surge confirms the day's direction. Quiet tape returns 0.
    """
    if vol_ratio is None or return_1d_pct is None:
        return 0
    if vol_ratio < 1.5:
        return 0
    if return_1d_pct >  0.3: return  1
    if return_1d_pct < -0.3: return -1
    return 0


# ── Combined ─────────────────────────────────────────────────────────────────

def score_all(
    sentiment_avg: float | None,
    stock_ind:     dict | None,
    spy_ind:       dict | None,
    vix_ind:       dict | None,
    vol_ratio:     float | None,
    return_1d_pct: float | None,
) -> dict:
    """Returns dict of per-factor scores plus their unweighted sum."""
    scores = {
        "news_sentiment":    score_sentiment(sentiment_avg),
        "stock_trend":       score_trend(stock_ind),
        "relative_strength": score_relative_strength(stock_ind, spy_ind),
        "market_regime":     score_regime(spy_ind, vix_ind),
        "volume_momentum":   score_volume_momentum(vol_ratio, return_1d_pct),
    }
    scores["total"] = sum(scores.values())
    return scores
