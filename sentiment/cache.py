"""
Per-day sentiment cache.

RSS feeds only return recent articles, so historical sentiment cannot be
backfilled from the live pipeline. Instead, each live run appends its
aggregate sentiment score to a JSONL cache. The backtest engine queries
this cache by date; when no entry exists it falls back to 0 (neutral).

File format (outputs/sentiment_cache.jsonl), one JSON object per line:
    {"date": "2026-04-16", "avg_score": 0.07, "n_articles": 34, "ts": "...",
     "weighted": true, "weighting_total": 2.81, "timeframe": "swing"}

Semantics:
  • Append-only. Multiple entries per day are allowed; the LATEST one wins
    when queried (matches live-pipeline intent — most recent read is truth).
  • Date is the local calendar date of the run (UTC would be fine too; pick
    one and stay consistent).
  • `weighted` marks Pillar-1 rows (weighted mean + per-article weighting).
    Rows written before Pillar 1 lack the field → treated as plain-mean.
    Backtest can filter or annotate to avoid mixing regimes.

This scaffold enables forward-going coverage. Historical sentiment remains
unavailable without a third-party archival feed (Bloomberg, Ravenpack, etc.).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, date

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)

CACHE_FILENAME = "sentiment_cache.jsonl"


def _path() -> str:
    return os.path.join(config.OUTPUT_DIR, CACHE_FILENAME)


def append(avg_score: float | None, n_articles: int, run_date: date | None = None) -> None:
    """Append one run's sentiment summary to the cache."""
    if avg_score is None:
        logger.info("Sentiment cache: avg_score is None — skipping append")
        return
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    entry = {
        "date":       (run_date or datetime.now().date()).isoformat(),
        "avg_score":  round(float(avg_score), 4),
        "n_articles": int(n_articles),
        "ts":         datetime.now().isoformat(timespec="seconds"),
    }
    with open(_path(), "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"Sentiment cache ← {entry['date']} avg={entry['avg_score']:+.3f}")


def load() -> dict[str, float]:
    """
    Load the cache into a {date_iso: avg_score} map. Latest entry per date wins.
    Returns an empty dict if the file does not exist.
    """
    path = _path()
    if not os.path.exists(path):
        return {}
    by_date: dict[str, float] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                by_date[rec["date"]] = float(rec["avg_score"])
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(f"Sentiment cache: skipping bad line ({e})")
    return by_date


def lookup(target: str | date, cache: dict[str, float] | None = None) -> float | None:
    """Look up a date's cached sentiment score. Returns None when absent."""
    if cache is None:
        cache = load()
    key = target.isoformat() if isinstance(target, date) else target
    return cache.get(key)
