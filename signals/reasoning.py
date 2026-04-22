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

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vw = signal_result.get("vwap_score", 0)
    if vw == 2:
        reasons.append("Gold is well above its rolling VWAP — strong institutional bullish bias")
    elif vw == 1:
        reasons.append("Gold is above its rolling VWAP — mild bullish institutional bias")
    elif vw == -1:
        reasons.append("Gold is below its rolling VWAP — mild bearish institutional bias")
    elif vw == -2:
        reasons.append("Gold is well below its rolling VWAP — strong institutional selling pressure")
    else:
        reasons.append("Gold is near its rolling VWAP — no clear institutional directional bias")

    # ── Volume Profile ────────────────────────────────────────────────────────
    vp = signal_result.get("volume_profile_score", 0)
    if vp == 2:
        reasons.append("Gold is trading above the Value Area High — bullish breakout from accepted range")
    elif vp == 1:
        reasons.append("Gold is above the Point of Control — price in upper value area, mild bullish")
    elif vp == -1:
        reasons.append("Gold is below the Point of Control — price in lower value area, mild bearish")
    elif vp == -2:
        reasons.append("Gold is below the Value Area Low — bearish breakdown from accepted range")

    # ── VIX ───────────────────────────────────────────────────────────────────
    v = signal_result.get("vix_score", 0)
    if v == 2:
        reasons.append("VIX is elevated (≥ 30) — high market fear driving safe-haven demand for gold")
    elif v == 1:
        reasons.append("VIX is above normal (≥ 20) — mild fear supporting gold as a safe haven")
    elif v == -1:
        reasons.append("VIX is very low (< 15) — market complacency signals risk-on, mild headwind for gold")
    else:
        reasons.append("VIX is in normal range — neutral impact on gold")

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

    w_total = data_quality.get("weighting_total")
    w_min   = data_quality.get("weighting_min")
    if w_total is not None and w_min is not None and float(w_total) < float(w_min):
        reasons.append(
            f"Weighted sentiment support is thin (Σ={float(w_total):.2f} < {float(w_min):.2f}) "
            "— articles were mostly stale, off-topic, or from low-tier sources, "
            "so the headline score carries less weight than usual"
        )

    return reasons
