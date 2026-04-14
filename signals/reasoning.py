"""
Plain-English reasoning builder.

Translates integer scores and data quality flags into a short,
transparent list of bullet-point strings for the final output.
"""


def build(signal_result: dict, data_quality: dict) -> list[str]:
    """Return a list of short reasoning strings explaining the signal."""
    reasons: list[str] = []

    # ── Sentiment ─────────────────────────────────────────────────────────────
    s = signal_result.get("sentiment_score", 0)
    if s >= 2:
        reasons.append("News sentiment is strongly bullish for gold")
    elif s == 1:
        reasons.append("News sentiment is mildly bullish for gold")
    elif s == -1:
        reasons.append("News sentiment is mildly bearish for gold")
    elif s <= -2:
        reasons.append("News sentiment is strongly bearish for gold")
    else:
        reasons.append("News sentiment is neutral")

    # ── DXY ──────────────────────────────────────────────────────────────────
    d = signal_result.get("dxy_score", 0)
    if d == -2:
        reasons.append("DXY is rising strongly — typically a headwind for gold")
    elif d == -1:
        reasons.append("DXY is rising mildly — slight headwind for gold")
    elif d == 1:
        reasons.append("DXY is falling mildly — mild tailwind for gold")
    elif d == 2:
        reasons.append("DXY is falling strongly — typically bullish for gold")
    else:
        reasons.append("DXY is flat — neutral for gold")

    # ── Yield ─────────────────────────────────────────────────────────────────
    y = signal_result.get("yield_score", 0)
    if y == -2:
        reasons.append("US 10Y yields are rising strongly — pressures non-yielding assets like gold")
    elif y == -1:
        reasons.append("US 10Y yields are rising — modest headwind for gold")
    elif y == 1:
        reasons.append("US 10Y yields are falling — supportive for gold")
    elif y == 2:
        reasons.append("US 10Y yields are falling strongly — bullish for non-yielding assets")
    else:
        reasons.append("US 10Y yields are flat — neutral for gold")

    # ── Gold trend ────────────────────────────────────────────────────────────
    g = signal_result.get("gold_trend_score", 0)
    if g == 3:
        reasons.append("Gold is in a strong uptrend — price above EMA20 and EMA50 with strong momentum")
    elif g == 1:
        reasons.append("Gold is in a mild uptrend — price above EMA20")
    elif g == -1:
        reasons.append("Gold is in a mild downtrend — price below EMA20")
    elif g == -3:
        reasons.append("Gold is in a strong downtrend — price below EMA20 and EMA50 with negative momentum")
    else:
        reasons.append("Gold price trend is sideways — no clear directional bias")

    # ── Veto notice ───────────────────────────────────────────────────────────
    if signal_result.get("veto_applied"):
        raw = signal_result.get("raw_signal", "")
        reasons.append(
            f"Signal downgraded from {raw} to HOLD — veto triggered by conflicting market conditions"
        )

    # ── Data quality caveats ──────────────────────────────────────────────────
    if data_quality.get("successfully_scraped", 0) == 0:
        reasons.append(
            "Article body scraping failed entirely — sentiment is based on headlines only, "
            "which reduces reliability"
        )

    n_mkt = data_quality.get("market_data_failures", 0)
    if n_mkt == 1:
        reasons.append("One market data source could not be fetched — its score defaulted to 0")
    elif n_mkt >= 2:
        reasons.append(
            f"{n_mkt} market data sources could not be fetched — "
            "affected scores defaulted to 0, confidence reduced"
        )

    return reasons
