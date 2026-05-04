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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import config
from market.indicators import compute as compute_indicators
from news.article_scraper import scrape_article
from news.dedup import deduplicate, _normalize_title
from news.rss_fetcher import fetch_articles
from sentiment.aggregator import aggregate
from sentiment.vader_analyzer import analyze as vader_analyze
from utils.logger import setup_logger
from utils.text_cleaner import clean_text

from . import ml_predictor
from .stock_confidence import compute as compute_confidence
from .stock_market import (
    STOCK_PROFILE,
    fetch_indicators,
    fetch_market_context,
    fetch_ohlcv,
    fetch_ohlcv_bulk,
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

# Fast-scan concurrency knobs (per-universe scan, not per-ticker)
FETCH_WORKERS_FAST  = 16  # RSS feed pulls
SCRAPE_WORKERS_FAST = 24  # article body scrapes (one per unique URL)
WRITE_WORKERS_FAST  = 8   # per-ticker aggregate + JSON write


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

        ml_signal = ml_predictor.predict(stock.ticker)

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
            "ml":               ml_signal,
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
    # Warm the ML stack BEFORE FinBERT. Unpickling XGBoost after FinBERT
    # initialises OpenMP / torch threading triggers a native crash on
    # macOS; pre-loading keeps XGBoost's runtime attached to the parent.
    try:
        ml_predictor._load()
    except Exception as e:
        logger.warning(f"ml_predictor preload failed: {e}")
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
    fast: bool = True,
    use_finbert: bool | None = None,
    articles_cap: int | None = None,
    max_per_query: int | None = None,
) -> dict:
    """
    Pipelined universe scan.

    Phases: A) RSS in parallel  B) cross-ticker dedup  C) scrape unique URLs in
    parallel  D) VADER all  E) FinBERT batched  F) bulk OHLCV  G) per-ticker
    aggregate+score+write in parallel  H) overview roll-up.

    `fast=True` caps articles/ticker to 5 and disables FinBERT (VADER-only).
    Overrides `use_finbert` / `articles_cap` / `max_per_query` take precedence.
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

    # Effective knobs ---------------------------------------------------------
    # FinBERT is fast on MPS (~30-60s for 2500 articles) and worth running
    # whenever available. Body scraping is the slow part — Google News URLs
    # need decoder calls that get 429-rate-limited at scale, so titles are
    # the practical input for sentiment regardless of mode.
    if use_finbert is None:
        use_finbert = True
    if articles_cap is None:
        articles_cap = 5 if fast else MAX_ARTICLES_PER_TICKER
    if max_per_query is None:
        max_per_query = 3 if fast else 10

    started = time.time()
    logger.info(
        f"scan_universe_fast: N={len(stocks)} fast={fast} finbert={use_finbert} "
        f"articles_cap={articles_cap} max_per_query={max_per_query}"
    )

    # ── Phase A: parallel RSS + per-ticker dedup ─────────────────────────────
    t0 = time.time()

    def _phase_a(stock: Stock) -> tuple[str, list[dict]]:
        try:
            queries = build_queries(stock)
            raw = fetch_articles(queries, max_per_query=max_per_query)
            unique = deduplicate(raw)[:articles_cap]
            return stock.ticker, unique
        except Exception as e:
            logger.warning(f"[{stock.ticker}] RSS phase failed: {e}")
            return stock.ticker, []

    per_ticker_articles: dict[str, list[dict]] = {}
    raw_counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS_FAST) as ex:
        for ticker, arts in ex.map(_phase_a, stocks):
            per_ticker_articles[ticker] = arts
            raw_counts[ticker] = len(arts)
    logger.info(f"Phase A (RSS): {time.time()-t0:.1f}s — {sum(raw_counts.values())} total articles")

    # ── Phase B: cross-ticker dedup by URL, fallback title key ───────────────
    t0 = time.time()
    unique_articles: dict[str, dict] = {}   # dedup_id → article
    ticker_article_ids: dict[str, list[str]] = {t: [] for t in per_ticker_articles}
    seen_titles: set[str] = set()

    for ticker, arts in per_ticker_articles.items():
        for a in arts:
            url = (a.get("url") or "").strip()
            title_key = a.get("dedup_key") or _normalize_title(a.get("title") or "")
            dedup_id = url or f"title::{title_key}"
            if not dedup_id:
                continue
            if dedup_id in unique_articles:
                # Seen this article under another ticker — attribute to both.
                unique_articles[dedup_id]["_seen_in"].add(ticker)
            else:
                if title_key and title_key in seen_titles and not url:
                    continue
                if title_key:
                    seen_titles.add(title_key)
                clone = dict(a)
                clone["_seen_in"] = {ticker}
                clone["_id"]      = dedup_id
                unique_articles[dedup_id] = clone
            ticker_article_ids[ticker].append(dedup_id)

    logger.info(
        f"Phase B (dedup): {time.time()-t0:.2f}s — "
        f"{sum(len(v) for v in per_ticker_articles.values())} → {len(unique_articles)} unique"
    )

    # ── Phase C: scrape every unique URL once, parallel ──────────────────────
    # In fast mode we skip scraping: Google News RSS URLs need per-URL decoder
    # calls that sleep ≥1s each, which would dominate total runtime. Title-only
    # analysis is the intended fast-mode tradeoff.
    t0 = time.time()
    scrape_by_id: dict[str, dict] = {}
    if fast:
        for dedup_id in unique_articles:
            scrape_by_id[dedup_id] = {"body": "", "extraction_success": False}
        logger.info(f"Phase C (scrape): skipped — fast mode uses titles only ({len(unique_articles)} articles)")
    else:
        def _phase_c(item: tuple[str, dict]) -> tuple[str, dict]:
            dedup_id, article = item
            url = article.get("url") or ""
            if not url:
                return dedup_id, {"body": "", "extraction_success": False}
            try:
                return dedup_id, scrape_article(
                    url,
                    timeout=config.SCRAPE_TIMEOUT,
                    retries=config.SCRAPE_RETRIES,
                )
            except Exception as e:
                logger.debug(f"scrape failed for {url[:80]}: {e}")
                return dedup_id, {"body": "", "extraction_success": False}

        with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS_FAST) as ex:
            for dedup_id, scraped in ex.map(_phase_c, list(unique_articles.items())):
                scrape_by_id[dedup_id] = scraped
        ok_count = sum(1 for s in scrape_by_id.values() if s.get("extraction_success"))
        logger.info(f"Phase C (scrape): {time.time()-t0:.1f}s — {ok_count}/{len(scrape_by_id)} succeeded")

    # ── Phase D: VADER on every unique article ───────────────────────────────
    t0 = time.time()
    texts_for_ml: list[str] = []
    ids_for_ml:   list[str] = []
    vader_by_id:  dict[str, dict] = {}

    for dedup_id, a in unique_articles.items():
        title = clean_text(a.get("title") or "")
        body  = scrape_by_id.get(dedup_id, {}).get("body") or ""
        if text_mode == "title":
            text, actual = title, "title"
        elif text_mode == "body":
            text, actual = (body, "body") if body else (title, "title_fallback")
        else:
            text   = " ".join(p for p in (title, body) if p)
            actual = "combined" if body else "title_fallback"
        a["_text"]   = text
        a["_actual"] = actual
        a["_clean_title"] = title
        vader_by_id[dedup_id] = vader_analyze(text) if text else None
        texts_for_ml.append(text)
        ids_for_ml.append(dedup_id)

    logger.info(f"Phase D (VADER): {time.time()-t0:.1f}s")

    # ── Phase E: FinBERT batched across all articles ─────────────────────────
    finbert_by_id: dict[str, dict | None] = {}
    if use_finbert:
        t0 = time.time()
        finbert = _load_finbert()
        if finbert is not None and finbert._ready:
            results = finbert.analyze_batch(texts_for_ml)
            for dedup_id, res in zip(ids_for_ml, results):
                finbert_by_id[dedup_id] = res
            logger.info(f"Phase E (FinBERT batched): {time.time()-t0:.1f}s — {len(texts_for_ml)} articles")
        else:
            logger.warning("FinBERT unavailable — skipping Phase E")
            for dedup_id in ids_for_ml:
                finbert_by_id[dedup_id] = None
    else:
        for dedup_id in ids_for_ml:
            finbert_by_id[dedup_id] = None
        logger.info("Phase E (FinBERT): skipped (fast mode / disabled)")

    # ── Phase F: bulk OHLCV for every ticker + SPY/VIX ───────────────────────
    t0 = time.time()
    all_tickers = [s.ticker for s in stocks] + ["SPY", "^VIX"]
    ohlcv_map = fetch_ohlcv_bulk(all_tickers)
    spy_df = ohlcv_map.get("SPY")
    vix_df = ohlcv_map.get("^VIX")
    spy_ind = compute_indicators(spy_df, name="SPY", tf=STOCK_PROFILE) if spy_df is not None else None
    vix_ind = compute_indicators(vix_df, name="^VIX", tf=STOCK_PROFILE) if vix_df is not None else None
    logger.info(f"Phase F (bulk OHLCV): {time.time()-t0:.1f}s — {len(all_tickers)} symbols")

    # ── Phase G: per-ticker aggregate + score + signal + write, parallel ─────
    t0 = time.time()

    def _phase_g(stock: Stock) -> dict:
        ticker = stock.ticker
        ids = ticker_article_ids.get(ticker, [])
        per_results: list[dict] = []
        for dedup_id in ids:
            a = unique_articles[dedup_id]
            scraped = scrape_by_id.get(dedup_id, {})
            vader_r   = vader_by_id.get(dedup_id)
            finbert_r = finbert_by_id.get(dedup_id)
            agg = aggregate(vader_r, finbert_r, a["_actual"], panel_result=None)
            per_results.append({
                "title":              a["_clean_title"],
                "source":             a.get("source", ""),
                "published":          a.get("published", ""),
                "url":                a.get("url", ""),
                "query":              a.get("query", ""),
                "body_length":        len(scraped.get("body", "") or ""),
                "extraction_success": bool(scraped.get("extraction_success")),
                "vader_label":        vader_r["label"] if vader_r else "",
                "vader_score":        vader_r["score"] if vader_r else "",
                "finbert_label":      finbert_r["label"] if finbert_r else "",
                "finbert_confidence": finbert_r.get("confidence", "") if finbert_r else "",
                "final_label":        agg["final_label"],
                "final_score":        agg["final_score"],
                "text_mode":          agg["text_mode_used"],
            })

        scores_list = [
            float(r["final_score"]) for r in per_results
            if r.get("final_score") not in ("", None)
        ]
        avg_score = round(sum(scores_list) / len(scores_list), 4) if scores_list else None

        df = ohlcv_map.get(ticker)
        stock_ind = compute_indicators(df, name=ticker, tf=STOCK_PROFILE) if df is not None else None
        vol_r = volume_ratio(df) if df is not None else None
        ret_1d = None
        if df is not None and len(df) >= 2:
            close = df["Close"]
            ret_1d = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)

        scores = score_all(
            sentiment_avg=avg_score,
            stock_ind=stock_ind,
            spy_ind=spy_ind,
            vix_ind=vix_ind,
            vol_ratio=vol_r,
            return_1d_pct=ret_1d,
        )
        sig = run_signal(scores)

        # In fast mode we never attempted scraping, so don't count titles as
        # scrape failures — the body absence is by design.
        if fast:
            total_scrapes = 0
            failed = 0
            skipped = len(per_results)
        else:
            total_scrapes = len(per_results)
            failed = sum(1 for r in per_results if not r.get("extraction_success"))
            skipped = 0
        confidence = compute_confidence(
            signal=sig["signal"],
            scores=scores,
            unique_articles=len(ids),
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
            for r in per_results[:10]
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
                "volume_ratio":   round(vol_r, 3) if vol_r is not None else None,
            }

        ml_signal = ml_predictor.predict(ticker)

        payload = {
            "ticker":           ticker,
            "company_name":     stock.name,
            "sector":           stock.sector,
            "industry":         getattr(stock, "industry", None),
            "run_timestamp":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "signal":           sig["signal"],
            "raw_signal":       sig["raw_signal"],
            "veto_applied":     sig["veto_applied"],
            "veto_reason":      sig["veto_reason"],
            "confidence":       confidence,
            "sentiment_label":  sentiment_label,
            "sentiment_score":  avg_score,
            "factor_scores":    scores,
            "ml":               ml_signal,
            "article_count":    len(per_results),
            "scrape_stats": {
                "fetched":     len(ids),
                "unique":      len(ids),
                "processed":   total_scrapes,
                "scraped_ok":  total_scrapes - failed,
                "failed":      failed,
                "skipped":     skipped,
                "mode":        "titles_only" if fast else "with_body",
            },
            "top_headlines":    headlines,
            "articles":         per_results,
            "price_summary":    price_summary,
            "market_context": {
                "spy_current":   spy_ind.get("current") if spy_ind else None,
                "spy_return_5d": spy_ind.get("return_5d_pct") if spy_ind else None,
                "vix":           vix_ind.get("current") if vix_ind else None,
            },
            "error": None,
        }
        write_ticker(ticker, payload)
        return payload

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WRITE_WORKERS_FAST) as ex:
        for payload in ex.map(_phase_g, stocks):
            rows.append(summarize_for_overview(payload))
    logger.info(f"Phase G (aggregate+write): {time.time()-t0:.1f}s")

    # ── Phase H: roll-up ─────────────────────────────────────────────────────
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
