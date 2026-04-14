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

import config
from news.article_scraper import scrape_article
from news.dedup import deduplicate
from news.rss_fetcher import fetch_articles
from sentiment.aggregator import aggregate
from sentiment.vader_analyzer import analyze as vader_analyze
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

    # Fetch RSS
    logger.info("Fetching gold/XAUUSD news from RSS feeds...")
    raw = fetch_articles(config.RSS_QUERIES, max_per_query=config.MAX_PER_QUERY)
    total_fetched = len(raw)
    logger.info(f"Total fetched: {total_fetched}")

    # Deduplicate + cap
    articles  = deduplicate(raw)[:limit]
    total_unique = len(articles)
    logger.info(f"Processing {total_unique} unique article(s)")

    results: list[dict] = []
    total_scraped = total_failed = 0

    for idx, article in enumerate(articles, 1):
        title = clean_text(article.get("title", ""))
        url   = article.get("url", "")
        logger.info(f"[{idx}/{total_unique}] {title[:80]}…")

        scrape = scrape_article(url, timeout=config.SCRAPE_TIMEOUT, retries=config.SCRAPE_RETRIES)
        body   = scrape["body"]
        ok     = scrape["extraction_success"]
        if ok:
            total_scraped += 1
        else:
            total_failed += 1
            logger.info("  → scrape failed, using title-only")

        text, actual_mode = _build_text(title, body, mode)

        vader_result   = vader_analyze(text) if "vader" in models and text else None
        finbert_result = finbert.analyze(text) if finbert and text else None
        agg            = aggregate(vader_result, finbert_result, actual_mode)

        results.append({
            "title":              title,
            "source":             article.get("source", ""),
            "published":          article.get("published", ""),
            "url":                url,
            "query":              article.get("query", ""),
            "body_length":        len(body),
            "extraction_success": ok,
            "dedup_key":          article.get("dedup_key", ""),
            "vader_label":        vader_result["label"] if vader_result else "",
            "vader_score":        vader_result["score"] if vader_result else "",
            "finbert_label":      finbert_result["label"] if finbert_result else "",
            "finbert_confidence": finbert_result.get("confidence", "") if finbert_result else "",
            "final_label":        agg["final_label"],
            "final_score":        agg["final_score"],
            "text_mode":          agg["text_mode_used"],
            "models_used":        ",".join(agg["models_used"]),
        })

    # Save sentiment output
    csv_path  = os.path.join(output_dir, f"sentiment_{timestamp}.csv")
    json_path = os.path.join(output_dir, f"sentiment_{timestamp}.json")

    summary = _build_sentiment_summary(
        results, total_fetched, total_unique, total_scraped, total_failed, mode, models
    )
    save_csv(results, csv_path)
    save_json({"summary": summary, "articles": results}, json_path)
    logger.info(f"Sentiment CSV  → {csv_path}")
    logger.info(f"Sentiment JSON → {json_path}")

    print_summary(summary)
    return results, summary


def _build_sentiment_summary(
    results, total_fetched, total_unique, total_scraped, total_failed, mode, models
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

    return {
        "total_fetched":           total_fetched,
        "total_unique":            total_unique,
        "total_analyzed":          len(results),
        "total_scraped":           total_scraped,
        "total_failed":            total_failed,
        "text_mode":               mode,
        "models_used":             models,
        "sentiment_distribution":  dist,
        "average_final_score":     round(sum(scores) / len(scores), 4) if scores else None,
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
) -> dict:
    """
    Fetch market data, compute scores, generate bias signal.
    Optionally compute trade setup levels.
    Returns the full signal output dict.
    """
    from market.data_fetcher import fetch_all
    from market.indicators import compute as compute_ind
    from market.trend_scoring import score_dxy, score_gold, score_yield
    from signals import confidence as conf_mod
    from signals import reasoning as reason_mod
    from signals import signal_engine, trade_setup as ts_mod
    from signals.risk_management import validate as rr_validate

    avg_score = sentiment_summary.get("average_final_score")

    # Data quality context passed to confidence + reasoning
    data_quality = {
        "articles_fetched":     sentiment_summary["total_fetched"],
        "unique_articles":      sentiment_summary["total_unique"],
        "successfully_scraped": sentiment_summary["total_scraped"],
        "failed_scrapes":       sentiment_summary["total_failed"],
        "text_mode_used":       sentiment_summary["text_mode"],
        "market_data_failures": 0,
    }

    # ── Fetch market data ─────────────────────────────────────────────────────
    logger.info("Fetching market data (DXY, US10Y, Gold)…")
    raw_market = fetch_all()

    gold_ind  = compute_ind(raw_market.get("gold"),      name="gold")
    dxy_ind   = compute_ind(raw_market.get("dxy"),       name="dxy")
    yield_ind = compute_ind(raw_market.get("yield_10y"), name="yield_10y")

    for name, ind in [("gold", gold_ind), ("dxy", dxy_ind), ("yield_10y", yield_ind)]:
        if ind is None:
            data_quality["market_data_failures"] += 1
            logger.warning(f"{name}: no indicators — score defaults to 0")

    # ── Score each factor ─────────────────────────────────────────────────────
    dxy_score   = score_dxy(dxy_ind)
    yld_score   = score_yield(yield_ind)
    gold_score  = score_gold(gold_ind)

    # ── Signal + veto ─────────────────────────────────────────────────────────
    sig = signal_engine.run(
        avg_sentiment=avg_score,
        dxy_score=dxy_score,
        yield_score=yld_score,
        gold_score=gold_score,
    )

    confidence = conf_mod.compute(sig, data_quality)
    reasoning  = reason_mod.build(sig, data_quality)

    # Market snapshot (saved to JSON for reference)
    market_snapshot = {
        "gold":      gold_ind,
        "dxy":       dxy_ind,
        "yield_10y": yield_ind,
    }

    output = {
        **sig,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "data_quality":     data_quality,
        "market_snapshot":  market_snapshot,
    }

    # ── Trade setup (optional) ────────────────────────────────────────────────
    if include_trade:
        setup = ts_mod.compute(sig["signal"], gold_ind)
        setup = rr_validate(setup)
        output["trade_setup"] = setup

    # ── Save ──────────────────────────────────────────────────────────────────
    signal_path = os.path.join(output_dir, f"signal_{timestamp}.json")
    save_json(output, signal_path)
    logger.info(f"Signal JSON    → {signal_path}")

    print_signal_summary(output)
    return output


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args      = parse_args()
    models    = ["vader", "finbert"] if args.model == "both" else [args.model]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results, summary = run_sentiment(
        mode=args.mode,
        models=models,
        limit=args.limit,
        output_dir=args.output_dir,
        timestamp=timestamp,
    )

    if args.signal:
        run_signal(
            sentiment_summary=summary,
            output_dir=args.output_dir,
            timestamp=timestamp,
            include_trade=args.trade_setup,
        )
