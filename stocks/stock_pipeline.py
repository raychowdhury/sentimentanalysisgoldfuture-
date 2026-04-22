"""
Stock pipeline — per-ticker scan + universe roll-up.

Reuses the gold-era primitives (rss_fetcher / article_scraper / dedup /
VADER / FinBERT / aggregator / market.indicators) without reusing the
gold-specific orchestration in main.run_sentiment (which injects
GOLD_FILTER_KEYWORDS, sentiment-cache side effects, panel personas, etc.).

Concurrency:
  - Per-ticker article processing uses a thread pool (I/O bound).
  - Ticker scans run sequentially across the universe so FinBERT isn't
    contended by the GIL. Full universe finishes in a couple minutes on
    a warm cache; acceptable for a local, on-demand scan.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
from news.article_scraper import scrape_article
from news.dedup import deduplicate
from news.rss_fetcher import fetch_articles
from sentiment.aggregator import aggregate
from sentiment.vader_analyzer import analyze as vader_analyze
from utils.logger import setup_logger
from utils.text_cleaner import clean_text

from .stock_confidence import compute as compute_confidence
from .stock_market import (
    fetch_indicators,
    fetch_market_context,
    fetch_ohlcv,
    volume_ratio,
)
from .stock_output import (
    summarize_for_overview,
    write_overview,
    write_ticker,
)
from .stock_queries import build_queries
from .stock_scoring import score_all
from .stock_signal_engine import run as run_signal
from .stock_universe import Stock, UNIVERSE, get as get_stock

logger = setup_logger(__name__)

MAX_ARTICLES_PER_TICKER = 20
SCRAPE_WORKERS = 6


# ── Sentiment helpers ─────────────────────────────────────────────────────────

def _sentiment_label(avg_score: float | None) -> str:
    if avg_score is None:
        return "neutral"
    if avg_score >=  0.05: return "positive"
    if avg_score <= -0.05: return "negative"
    return "neutral"


def _process_article(
    article: dict,
    finbert,
    text_mode: str,
) -> dict:
    """Scrape + score a single article. Tolerates every failure mode."""
    title = clean_text(article.get("title", ""))
    url   = article.get("url", "")

    scrape = scrape_article(url, timeout=config.SCRAPE_TIMEOUT, retries=config.SCRAPE_RETRIES)
    body = scrape["body"]
    ok   = scrape["extraction_success"]

    if text_mode == "title":
        text, actual = title, "title"
    elif text_mode == "body":
        text, actual = (body, "body") if body else (title, "title_fallback")
    else:
        parts = [p for p in (title, body) if p]
        text   = " ".join(parts)
        actual = "combined" if body else "title_fallback"

    vader_result   = vader_analyze(text) if text else None
    finbert_result = finbert.analyze(text) if finbert and text else None
    agg = aggregate(vader_result, finbert_result, actual, panel_result=None)

    return {
        "title":              title,
        "source":             article.get("source", ""),
        "published":          article.get("published", ""),
        "url":                url,
        "query":              article.get("query", ""),
        "body_length":        len(body),
        "extraction_success": ok,
        "vader_label":        vader_result["label"] if vader_result else "",
        "vader_score":        vader_result["score"] if vader_result else "",
        "finbert_label":      finbert_result["label"] if finbert_result else "",
        "finbert_confidence": finbert_result.get("confidence", "") if finbert_result else "",
        "final_label":        agg["final_label"],
        "final_score":        agg["final_score"],
        "text_mode":          agg["text_mode_used"],
    }


# ── Single-ticker scan ────────────────────────────────────────────────────────

def scan_ticker(
    stock: Stock,
    finbert=None,
    market_context: dict | None = None,
    text_mode: str = "combined",
    max_per_query: int = 10,
) -> dict:
    """
    Run the full per-ticker pipeline and return a payload dict ready to
    be persisted. Never raises — errors are captured under `error` and
    the partial payload is still returned so one bad ticker doesn't
    bring down the whole universe scan.
    """
    started = time.time()
    logger.info(f"[{stock.ticker}] starting scan")

    try:
        queries = build_queries(stock)
        raw = fetch_articles(queries, max_per_query=max_per_query)
        articles = deduplicate(raw)[:MAX_ARTICLES_PER_TICKER]

        results: list[dict] = []
        if articles:
            with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as ex:
                results = list(ex.map(
                    lambda a: _process_article(a, finbert, text_mode),
                    articles,
                ))

        scores_list = [
            float(r["final_score"]) for r in results
            if r.get("final_score") not in ("", None)
        ]
        avg_score = round(sum(scores_list) / len(scores_list), 4) if scores_list else None

        # Market data
        df = fetch_ohlcv(stock.ticker)
        stock_ind = fetch_indicators(stock.ticker) if df is not None else None
        if market_context is None:
            market_context = fetch_market_context()
        spy_ind = market_context.get("spy")
        vix_ind = market_context.get("vix")

        vol_ratio = volume_ratio(df) if df is not None else None
        ret_1d = None
        if df is not None and len(df) >= 2:
            close = df["Close"]
            ret_1d = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)

        scores = score_all(
            sentiment_avg=avg_score,
            stock_ind=stock_ind,
            spy_ind=spy_ind,
            vix_ind=vix_ind,
            vol_ratio=vol_ratio,
            return_1d_pct=ret_1d,
        )

        sig = run_signal(scores)

        total_scrapes = len(results)
        failed = sum(1 for r in results if not r.get("extraction_success"))
        confidence = compute_confidence(
            signal=sig["signal"],
            scores=scores,
            unique_articles=len(articles),
            total_scrapes=total_scrapes,
            failed_scrapes=failed,
            stock_ok=stock_ind is not None,
            spy_ok=spy_ind is not None,
            vix_ok=vix_ind is not None,
        )

        sentiment_label = _sentiment_label(avg_score)

        headlines = [
            {
                "title":  r["title"],
                "source": r["source"],
                "url":    r["url"],
                "label":  r["final_label"],
                "score":  r["final_score"],
            }
            for r in results[:10]
        ]

        price_summary = None
        if stock_ind:
            price_summary = {
                "current":        stock_ind.get("current"),
                "ema20":          stock_ind.get("ema20"),
                "ema50":          stock_ind.get("ema50"),
                "return_5d_pct":  stock_ind.get("return_5d_pct"),
                "atr_pct":        stock_ind.get("atr_pct"),
                "return_1d_pct":  ret_1d,
                "volume_ratio":   round(vol_ratio, 3) if vol_ratio is not None else None,
            }

        payload = {
            "ticker":           stock.ticker,
            "company_name":     stock.name,
            "sector":           stock.sector,
            "run_timestamp":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec":      round(time.time() - started, 2),
            "signal":           sig["signal"],
            "raw_signal":       sig["raw_signal"],
            "veto_applied":     sig["veto_applied"],
            "veto_reason":      sig["veto_reason"],
            "confidence":       confidence,
            "sentiment_label":  sentiment_label,
            "sentiment_score":  avg_score,
            "factor_scores":    scores,
            "article_count":    len(results),
            "scrape_stats": {
                "fetched":     len(raw),
                "unique":      len(articles),
                "processed":   total_scrapes,
                "scraped_ok":  total_scrapes - failed,
                "failed":      failed,
            },
            "top_headlines":    headlines,
            "articles":         results,
            "price_summary":    price_summary,
            "market_context": {
                "spy_current":   spy_ind.get("current") if spy_ind else None,
                "spy_return_5d": spy_ind.get("return_5d_pct") if spy_ind else None,
                "vix":           vix_ind.get("current") if vix_ind else None,
            },
            "error": None,
        }
        return payload

    except Exception as e:
        logger.exception(f"[{stock.ticker}] pipeline failed: {e}")
        return {
            "ticker":        stock.ticker,
            "company_name":  stock.name,
            "sector":        stock.sector,
            "run_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "signal":        "HOLD",
            "confidence":    "LOW",
            "sentiment_label": "neutral",
            "sentiment_score": None,
            "factor_scores": {
                "news_sentiment": 0, "stock_trend": 0, "relative_strength": 0,
                "market_regime": 0, "volume_momentum": 0, "total": 0,
            },
            "article_count": 0,
            "scrape_stats": {},
            "top_headlines": [],
            "articles": [],
            "price_summary": None,
            "market_context": {},
            "error": str(e),
        }


# ── Universe scan ─────────────────────────────────────────────────────────────

def _load_finbert():
    """Load FinBERT once. Returns None if unavailable — pipeline still runs
    (aggregator falls back to VADER-only)."""
    try:
        from sentiment.finbert_analyzer import FinBERTAnalyzer
        logger.info("Loading FinBERT model (first run downloads ~440 MB)...")
        analyzer = FinBERTAnalyzer()
        return analyzer
    except Exception as e:
        logger.warning(f"FinBERT load failed — continuing with VADER only: {e}")
        return None


def scan_universe(
    tickers: list[str] | None = None,
    text_mode: str = "combined",
) -> dict:
    """
    Scan every ticker in the universe (or the supplied subset). Writes
    per-ticker JSON plus the overview roll-up. Returns the overview dict.
    """
    stocks: list[Stock] = []
    if tickers:
        for t in tickers:
            s = get_stock(t)
            if s:
                stocks.append(s)
            else:
                logger.warning(f"Unknown ticker skipped: {t}")
    else:
        stocks = list(UNIVERSE)

    if not stocks:
        raise ValueError("No valid tickers to scan")

    started = time.time()
    finbert = _load_finbert()
    market_context = fetch_market_context()

    rows: list[dict] = []
    for stock in stocks:
        payload = scan_ticker(
            stock,
            finbert=finbert,
            market_context=market_context,
            text_mode=text_mode,
        )
        write_ticker(stock.ticker, payload)
        rows.append(summarize_for_overview(payload))
        logger.info(
            f"[{stock.ticker}] {payload['signal']}/{payload['confidence']} "
            f"sent={payload['sentiment_label']} articles={payload['article_count']}"
        )

    # Roll-up metrics
    buy_terms    = {"BUY", "STRONG_BUY"}
    sell_terms   = {"SELL", "STRONG_SELL"}
    bullish = sum(1 for r in rows if r.get("signal") in buy_terms)
    bearish = sum(1 for r in rows if r.get("signal") in sell_terms)
    neutral = sum(1 for r in rows if r.get("signal") == "HOLD")

    scored = [r for r in rows if r.get("total_score") is not None]
    strongest_bull = max(scored, key=lambda r: r["total_score"], default=None)
    strongest_bear = min(scored, key=lambda r: r["total_score"], default=None)

    sentiment_values = [
        float(r["sentiment_score"])
        for r in rows
        if isinstance(r.get("sentiment_score"), (int, float))
    ]
    avg_sentiment = round(sum(sentiment_values) / len(sentiment_values), 4) if sentiment_values else None

    overview = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec":   round(time.time() - started, 2),
        "universe": {
            "size":    len(stocks),
            "tickers": [s.ticker for s in stocks],
        },
        "market_summary": {
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "strongest_bullish": strongest_bull["ticker"] if strongest_bull else None,
            "strongest_bearish": strongest_bear["ticker"] if strongest_bear else None,
            "average_sentiment": avg_sentiment,
        },
        "stocks": rows,
    }
    write_overview(overview)
    return overview
