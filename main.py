#!/usr/bin/env python3
"""
NewsSentimentScanner — Gold/XAUUSD Sentiment + Bias Signal Engine

Usage:
    python main.py                                        # sentiment only
    python main.py --mode title --model vader --limit 10  # fast test
    python main.py --signal                               # + market signal
    python main.py --signal --trade-setup                 # + trade levels
    python main.py --mode combined --model both --signal --trade-setup
"""

import argparse
import os
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import config
from news.article_scraper import scrape_article
from news.dedup import deduplicate
from news.rss_fetcher import fetch_articles, fetch_feeds
from sentiment.aggregator import aggregate
from sentiment.vader_analyzer import analyze as vader_analyze
from sentiment.weighting import (
    relevance_score,
    source_tier_weight,
    time_decay_weight,
    weighted_mean_score,
)
from utils import progress
from utils.io_helpers import print_signal_summary, print_summary, save_csv, save_json
from utils.logger import setup_logger
from utils.text_cleaner import clean_text

logger = setup_logger("main")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gold/XAUUSD news sentiment + bias signal engine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--mode", choices=["title", "body", "combined"],
        default=config.DEFAULT_TEXT_MODE,
        help="Text input mode for sentiment analysis",
    )
    p.add_argument(
        "--model", choices=["vader", "finbert", "both"],
        default="both",
        help="Sentiment model(s) to run",
    )
    p.add_argument(
        "--limit", type=int, default=config.MAX_ARTICLES,
        help="Max articles to analyze per run",
    )
    p.add_argument(
        "--output-dir", default=config.OUTPUT_DIR,
        help="Directory for output files",
    )
    p.add_argument(
        "--signal", action="store_true",
        help="Fetch market data and compute gold bias signal",
    )
    p.add_argument(
        "--trade-setup", action="store_true",
        help="Compute trade entry / stop / take-profit levels (requires --signal)",
    )
    p.add_argument(
        "--timeframe", choices=["swing", "day"], default="swing",
        help="Trading timeframe: 'swing' (multi-day, default) or 'day' (intraday bias)",
    )
    p.add_argument(
        "--stocks", action="store_true",
        help="Run the stock sentiment scanner instead of the gold pipeline",
    )
    p.add_argument(
        "--ticker", default=None,
        help="When used with --stocks, scan only the given ticker (e.g. AAPL)",
    )
    return p.parse_args()


# ── Text construction ─────────────────────────────────────────────────────────

def _build_text(title: str, body: str, mode: str) -> tuple[str, str]:
    """Build analysis text; returns (text, actual_mode_used)."""
    if mode == "title":
        return title, "title"
    if mode == "body":
        return (body, "body") if body else (title, "title_fallback")
    # combined
    parts = [p for p in [title, body] if p]
    actual = "combined" if body else "title_fallback"
    return " ".join(parts), actual


# ── Sentiment pipeline ────────────────────────────────────────────────────────

def run_sentiment(
    mode: str,
    models: list[str],
    limit: int,
    output_dir: str,
    timestamp: str,
    timeframe: str = "swing",
) -> tuple[list[dict], dict]:
    """
    Run the news sentiment pipeline.
    Returns (article_results, summary_dict).
    """
    os.makedirs(output_dir, exist_ok=True)

    # Load FinBERT once — expensive, do it up front
    finbert = None
    if "finbert" in models:
        logger.info("Loading FinBERT model (first run downloads ~440 MB)...")
        from sentiment.finbert_analyzer import FinBERTAnalyzer
        finbert = FinBERTAnalyzer()

    # Agent panel (LLM multi-persona). Opt-in via config.AGENT_PANEL_ENABLED.
    agent_panel = None
    if getattr(config, "AGENT_PANEL_ENABLED", False):
        from sentiment.agent_panel import AgentPanel
        agent_panel = AgentPanel()
        if not agent_panel.ready:
            agent_panel = None

    # Fetch RSS — direct feeds first (FinancialJuice, MarketWatch) so they
    # appear at the top of the article list and win dedup ties over Google
    # News aggregator entries (better provenance, earlier timestamps).
    logger.info("Fetching gold/XAUUSD news from RSS feeds...")
    direct_feeds = getattr(config, "RSS_FEEDS", [])
    raw: list[dict] = []
    if direct_feeds:
        raw += fetch_feeds(
            direct_feeds,
            max_per_feed=getattr(config, "MAX_PER_FEED", 40),
            filter_keywords=getattr(config, "GOLD_FILTER_KEYWORDS", []),
        )
    raw += fetch_articles(config.RSS_QUERIES, max_per_query=config.MAX_PER_QUERY)
    total_fetched = len(raw)
    logger.info(f"Total fetched: {total_fetched}")

    # Deduplicate + cap
    articles  = deduplicate(raw)[:limit]
    total_unique = len(articles)
    logger.info(f"Processing {total_unique} unique article(s)")

    progress.reset(total=total_unique, stage="articles")

    def _process_article(idx_article):
        idx, article = idx_article
        title = clean_text(article.get("title", ""))
        url   = article.get("url", "")
        logger.info(f"[{idx}/{total_unique}] {title[:80]}…")

        scrape = scrape_article(url, timeout=config.SCRAPE_TIMEOUT, retries=config.SCRAPE_RETRIES)
        body   = scrape["body"]
        ok     = scrape["extraction_success"]
        if not ok:
            logger.info(f"  → scrape failed for [{idx}], using title-only")

        text, actual_mode = _build_text(title, body, mode)

        vader_result   = vader_analyze(text) if "vader" in models and text else None
        finbert_result = finbert.analyze(text) if finbert and text else None
        panel_result   = agent_panel.analyze(title, body) if agent_panel and text else None
        agg            = aggregate(vader_result, finbert_result, actual_mode, panel_result)

        # Pillar 1: per-article static weights (relevance + source tier).
        # Time decay is applied at aggregation time using the profile's τ.
        source_name = article.get("source", "")
        relevance   = round(relevance_score(title), 4)
        src_tier    = round(source_tier_weight(source_name), 4)

        return idx, ok, {
            "title":              title,
            "source":             source_name,
            "published":          article.get("published", ""),
            "url":                url,
            "query":              article.get("query", ""),
            "body_length":        len(body),
            "extraction_success": ok,
            "dedup_key":          article.get("dedup_key", ""),
            "relevance":          relevance,
            "source_tier":        src_tier,
            "vader_label":        vader_result["label"] if vader_result else "",
            "vader_score":        vader_result["score"] if vader_result else "",
            "finbert_label":      finbert_result["label"] if finbert_result else "",
            "finbert_confidence": finbert_result.get("confidence", "") if finbert_result else "",
            "panel_label":        panel_result["label"] if panel_result else "",
            "panel_score":        panel_result["score"] if panel_result else "",
            "panel_variance":     panel_result.get("variance", "") if panel_result else "",
            "panel_rationale":    panel_result["rationale"] if panel_result else "",
            "final_label":        agg["final_label"],
            "final_score":        agg["final_score"],
            "text_mode":          agg["text_mode_used"],
            "models_used":        ",".join(agg["models_used"]),
        }

    from concurrent.futures import ThreadPoolExecutor
    max_workers = getattr(config, "PIPELINE_WORKERS", 6)
    indexed: list[tuple[int, bool, dict]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for item in ex.map(_process_article, enumerate(articles, 1)):
            indexed.append(item)
            progress.tick()

    # Preserve original article order (map already keeps order, but sort for safety)
    indexed.sort(key=lambda x: x[0])
    total_scraped = sum(1 for _, ok, _ in indexed if ok)
    total_failed  = len(indexed) - total_scraped
    results: list[dict] = [row for _, _, row in indexed]

    # Pillar 1: attach time-decay + combined weight per article using the
    # timeframe's τ. Done here (not in worker) so τ is resolved once.
    tau_map = getattr(config, "SENTIMENT_TAU_HOURS", {"swing": 48.0, "day": 12.0})
    tau_hours = float(tau_map.get(timeframe, 48.0))
    for r in results:
        dec = time_decay_weight(r.get("published", ""), tau_hours)
        rel = float(r.get("relevance", 0) or 0)
        src = float(r.get("source_tier", 0) or 0)
        r["time_decay"]     = round(dec, 4)
        r["weight_combined"] = round(rel * src * dec, 4)

    # Save sentiment output
    csv_path  = os.path.join(output_dir, f"sentiment_{timestamp}.csv")
    json_path = os.path.join(output_dir, f"sentiment_{timestamp}.json")

    summary = _build_sentiment_summary(
        results, total_fetched, total_unique, total_scraped, total_failed, mode, models,
        timeframe=timeframe,
    )
    save_csv(results, csv_path)
    save_json({"summary": summary, "articles": results}, json_path)
    logger.info(f"Sentiment CSV  → {csv_path}")
    logger.info(f"Sentiment JSON → {json_path}")

    # Append this run to the sentiment cache so future backtests can
    # replay today's score instead of falling back to neutral 0.
    from sentiment import cache as sentiment_cache
    sentiment_cache.append(
        avg_score=summary.get("average_final_score"),
        n_articles=summary.get("total_analyzed", 0),
        weighted=True,
        weighting_total=summary.get("weighting_total"),
        timeframe=timeframe,
    )

    print_summary(summary)
    return results, summary


def _build_sentiment_summary(
    results, total_fetched, total_unique, total_scraped, total_failed, mode, models,
    timeframe: str = "swing",
) -> dict:
    scores: list[float] = []
    dist:   dict        = {}

    for r in results:
        for m in ["vader", "finbert"]:
            lbl = r.get(f"{m}_label", "")
            if lbl:
                dist.setdefault(m, {"positive": 0, "neutral": 0, "negative": 0})
                dist[m][lbl] = dist[m].get(lbl, 0) + 1
        try:
            fs = r.get("final_score", "")
            if fs != "":
                scores.append(float(fs))
        except (ValueError, TypeError):
            pass

    final_dist: dict = {"positive": 0, "neutral": 0, "negative": 0}
    for r in results:
        lbl = r.get("final_label", "neutral")
        final_dist[lbl] = final_dist.get(lbl, 0) + 1
    dist["final"] = final_dist

    def _safe_score(r):
        try:
            return float(r.get("final_score") or 0)
        except (ValueError, TypeError):
            return 0.0

    top_pos = sorted(
        [r for r in results if r.get("final_label") == "positive"],
        key=_safe_score, reverse=True,
    )[:5]
    top_neg = sorted(
        [r for r in results if r.get("final_label") == "negative"],
        key=_safe_score,
    )[:5]

    panel_variances = [
        float(r["panel_variance"])
        for r in results
        if r.get("panel_variance") not in ("", None)
    ]
    avg_panel_variance = (
        round(sum(panel_variances) / len(panel_variances), 4)
        if panel_variances else None
    )

    # Pillar 1: weighted mean replaces plain mean as the headline score.
    # Keep plain mean alongside for dashboards / A/B comparison.
    tau_map = getattr(config, "SENTIMENT_TAU_HOURS", {"swing": 48.0, "day": 12.0})
    tau = float(tau_map.get(timeframe, 48.0))
    weighted_avg, total_weight = weighted_mean_score(results, tau_hours=tau)
    plain_avg = round(sum(scores) / len(scores), 4) if scores else None

    # Low-conviction floor — expose so the dashboard can flag thin batches.
    weight_min_map = getattr(config, "SENTIMENT_WEIGHT_MIN", {"swing": 1.0, "day": 0.6})
    weighting_min = float(weight_min_map.get(timeframe, 1.0))

    # Which articles dominated the weighted mean. Front-end shows these as
    # "drove the score" callouts.
    def _weight(row: dict) -> float:
        try:
            return float(row.get("weight_combined") or 0)
        except (ValueError, TypeError):
            return 0.0
    top_weighted = sorted(results, key=_weight, reverse=True)[:3]
    top_weighted_rows = [
        {
            "title":      r.get("title", ""),
            "source":     r.get("source", ""),
            "final_label": r.get("final_label", ""),
            "final_score": r.get("final_score", ""),
            "relevance":   r.get("relevance"),
            "source_tier": r.get("source_tier"),
            "time_decay":  r.get("time_decay"),
            "weight_combined": r.get("weight_combined"),
        }
        for r in top_weighted
    ]

    return {
        "total_fetched":           total_fetched,
        "total_unique":            total_unique,
        "total_analyzed":          len(results),
        "total_scraped":           total_scraped,
        "total_failed":            total_failed,
        "text_mode":               mode,
        "models_used":             models,
        "sentiment_distribution":  dist,
        "average_final_score":     weighted_avg if weighted_avg is not None else plain_avg,
        "average_final_score_plain": plain_avg,
        "weighting_tau_hours":     tau,
        "weighting_total":         total_weight,
        "weighting_min":           weighting_min,
        "weighting_thin":          total_weight is not None and float(total_weight) < weighting_min,
        "weighting_timeframe":     timeframe,
        "top_weighted":            top_weighted_rows,
        "average_panel_variance":  avg_panel_variance,
        "panel_articles_scored":   len(panel_variances),
        "top_positive":            [r["title"] for r in top_pos],
        "top_negative":            [r["title"] for r in top_neg],
        "neutral_count":           final_dist.get("neutral", 0),
    }


# ── Signal engine ─────────────────────────────────────────────────────────────

def run_signal(
    sentiment_summary: dict,
    output_dir: str,
    timestamp: str,
    include_trade: bool,
    timeframe: str = "swing",
) -> dict:
    """
    Fetch market data, compute scores, generate bias signal.
    Optionally compute trade setup levels.
    Returns the full signal output dict.
    """
    from events.blackout import is_blackout
    from market.data_fetcher import fetch_all
    from market.indicators import compute as compute_ind
    from market.trend_scoring import score_dxy, score_gold, score_vix, score_volume_profile, score_vwap, score_yield
    from positioning import cot_fetcher, cot_scoring
    from signals import confidence as conf_mod
    from signals import reasoning as reason_mod
    from signals import signal_engine, trade_setup as ts_mod
    from signals.risk_management import validate as rr_validate

    tf = config.TIMEFRAME_PROFILES[timeframe]
    avg_score = sentiment_summary.get("average_final_score")

    # Data quality context passed to confidence + reasoning
    weight_min_map = getattr(config, "SENTIMENT_WEIGHT_MIN", {"swing": 1.0, "day": 0.6})
    data_quality = {
        "articles_fetched":     sentiment_summary["total_fetched"],
        "unique_articles":      sentiment_summary["total_unique"],
        "successfully_scraped": sentiment_summary["total_scraped"],
        "failed_scrapes":       sentiment_summary["total_failed"],
        "text_mode_used":       sentiment_summary["text_mode"],
        "market_data_failures": 0,
        "panel_disagreement":   sentiment_summary.get("average_panel_variance"),
        "panel_articles_scored": sentiment_summary.get("panel_articles_scored", 0),
        "weighting_total":      sentiment_summary.get("weighting_total"),
        "weighting_min":        float(weight_min_map.get(timeframe, 1.0)),
    }

    # ── Fetch market data ─────────────────────────────────────────────────────
    logger.info(f"Fetching market data (DXY, US10Y, Gold) — timeframe={timeframe}…")
    raw_market = fetch_all(lookback_days=tf["lookback_days"])

    gold_ind  = compute_ind(raw_market.get("gold"),      name="gold",      tf=tf)
    dxy_ind   = compute_ind(raw_market.get("dxy"),       name="dxy",       tf=tf)
    yield_ind = compute_ind(raw_market.get("yield_10y"), name="yield_10y", tf=tf)
    vix_ind   = compute_ind(raw_market.get("vix"),       name="vix",       tf=tf)

    for name, ind in [("gold", gold_ind), ("dxy", dxy_ind), ("yield_10y", yield_ind), ("vix", vix_ind)]:
        if ind is None:
            data_quality["market_data_failures"] += 1
            logger.warning(f"{name}: no indicators — score defaults to 0")

    # ── Score each factor ─────────────────────────────────────────────────────
    dxy_score   = score_dxy(dxy_ind,              tf=tf)
    yld_score   = score_yield(yield_ind,          tf=tf)
    gold_score  = score_gold(gold_ind,            tf=tf)
    vix_score   = score_vix(vix_ind)
    vwap_score  = score_vwap(gold_ind)
    vp_score    = score_volume_profile(gold_ind)

    # Macro regime flag: gold above/below its SMA200.
    macro_bullish = None
    if gold_ind and gold_ind.get("sma200") is not None:
        macro_bullish = gold_ind["current"] > gold_ind["sma200"]

    # Event gate — block entries inside blackout window around FOMC/CPI/NFP/PCE.
    # Toggled per timeframe profile; swing is off by default (see config).
    if tf.get("event_gate", False):
        _, event_reason = is_blackout(datetime.now().date())
    else:
        event_reason = None

    # COT positioning — weekly CFTC managed-money net z-score, contrarian fade.
    # Per-profile toggle: swing on, day off (weekly cadence too stale for daily).
    if tf.get("cot_enabled", True):
        cot_records = cot_fetcher.ensure_fresh()
        cot_score = cot_scoring.score_at(cot_records, datetime.now().date())
    else:
        cot_score = 0

    # ── Signal + veto ─────────────────────────────────────────────────────────
    sig = signal_engine.run(
        avg_sentiment=avg_score,
        dxy_score=dxy_score,
        yield_score=yld_score,
        gold_score=gold_score,
        vix_score=vix_score,
        vwap_score=vwap_score,
        vp_score=vp_score,
        cot_score=cot_score,
        macro_bullish=macro_bullish,
        event_blackout_reason=event_reason,
    )

    confidence = conf_mod.compute(sig, data_quality)
    reasoning  = reason_mod.build(sig, data_quality)

    # Market snapshot (saved to JSON for reference)
    market_snapshot = {
        "gold":      gold_ind,
        "dxy":       dxy_ind,
        "yield_10y": yield_ind,
        "vix":       vix_ind,
    }

    output = {
        **sig,
        "timeframe":        timeframe,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "data_quality":     data_quality,
        "market_snapshot":  market_snapshot,
    }

    # ── Trade setup (optional) ────────────────────────────────────────────────
    if include_trade:
        setup = ts_mod.compute(sig["signal"], gold_ind, tf=tf)
        setup = rr_validate(setup, tf=tf)
        output["trade_setup"] = setup

    # ── Save ──────────────────────────────────────────────────────────────────
    signal_path = os.path.join(output_dir, f"signal_{timestamp}.json")
    save_json(output, signal_path)
    logger.info(f"Signal JSON    → {signal_path}")

    print_signal_summary(output)
    return output


# ── Entry point ───────────────────────────────────────────────────────────────

def run_stocks_cli(args: argparse.Namespace) -> None:
    """Stock sentiment scan — separate from the gold pipeline."""
    from stocks.stock_pipeline import scan_universe
    from stocks.stock_universe import is_known

    tickers: list[str] | None = None
    if args.ticker:
        t = args.ticker.upper()
        if not is_known(t):
            raise SystemExit(f"Unknown ticker: {args.ticker} — not in stocks/stock_universe.py")
        tickers = [t]

    overview = scan_universe(tickers=tickers, text_mode=args.mode)

    # Terminal summary
    sep = "─" * 62
    print(f"\n{sep}")
    print("  STOCK SENTIMENT SCAN — OVERVIEW")
    print(sep)
    ms = overview["market_summary"]
    print(f"  Tickers scanned : {overview['universe']['size']}")
    print(f"  Bullish         : {ms['bullish_count']}")
    print(f"  Bearish         : {ms['bearish_count']}")
    print(f"  Neutral         : {ms['neutral_count']}")
    print(f"  Strongest bull  : {ms['strongest_bullish']}")
    print(f"  Strongest bear  : {ms['strongest_bearish']}")
    print(f"  Avg sentiment   : {ms['average_sentiment']}")
    print(f"  Elapsed         : {overview['elapsed_sec']}s")
    print(sep)
    for row in overview["stocks"]:
        print(f"  {row['ticker']:6s}  {row['signal']:12s} "
              f"conf={row['confidence']:6s} sent={row.get('sentiment_label', '-'):8s} "
              f"n={row.get('article_count', 0):>3}  err={row.get('error') or '-'}")
    print(f"{sep}\n")


if __name__ == "__main__":
    args      = parse_args()

    # Stock scanner is a distinct pipeline — run it and exit before the
    # gold path touches any shared state (FinBERT, sentiment cache, etc.).
    if args.stocks:
        run_stocks_cli(args)
        raise SystemExit(0)

    models    = ["vader", "finbert"] if args.model == "both" else [args.model]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results, summary = run_sentiment(
        mode=args.mode,
        models=models,
        limit=args.limit,
        output_dir=args.output_dir,
        timestamp=timestamp,
        timeframe=args.timeframe,
    )

    if args.signal:
        run_signal(
            sentiment_summary=summary,
            output_dir=args.output_dir,
            timestamp=timestamp,
            include_trade=args.trade_setup,
            timeframe=args.timeframe,
        )
