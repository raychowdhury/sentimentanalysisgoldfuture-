"""
Read/write helpers for the stocks/ output directory.

Layout:
  outputs/stocks/
    overview.json          — last universe scan roll-up
    AAPL.json, MSFT.json … — per-ticker detail

Everything is JSON. No database, no locking — single-writer assumption.
"""

from __future__ import annotations

import json
import os
from typing import Any

OUTPUT_SUBDIR = os.path.join("outputs", "stocks")


def _path(filename: str) -> str:
    return os.path.join(OUTPUT_SUBDIR, filename)


def ensure_dir() -> None:
    os.makedirs(OUTPUT_SUBDIR, exist_ok=True)


def write_ticker(ticker: str, payload: dict) -> str:
    ensure_dir()
    path = _path(f"{ticker.upper()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    return path


def write_overview(payload: dict) -> str:
    ensure_dir()
    path = _path("overview.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    return path


def read_ticker(ticker: str) -> dict | None:
    path = _path(f"{ticker.upper()}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_overview() -> dict | None:
    path = _path("overview.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def summarize_for_overview(ticker_payload: dict) -> dict[str, Any]:
    """Compact per-ticker row used in overview.json and the overview page."""
    scores = ticker_payload.get("factor_scores") or {}
    price  = ticker_payload.get("price_summary") or {}
    return {
        "ticker":         ticker_payload.get("ticker"),
        "company_name":   ticker_payload.get("company_name"),
        "sector":         ticker_payload.get("sector"),
        "signal":         ticker_payload.get("signal"),
        "confidence":     ticker_payload.get("confidence"),
        "sentiment_label": ticker_payload.get("sentiment_label"),
        "sentiment_score": ticker_payload.get("sentiment_score"),
        "total_score":    scores.get("total"),
        "article_count":  ticker_payload.get("article_count"),
        "price":          price.get("current"),
        "return_5d_pct":  price.get("return_5d_pct"),
        "error":          ticker_payload.get("error"),
    }
