"""
News query builder for a given stock.

Three queries per ticker keeps Google News RSS load ~60 requests per
full-universe scan. Mixes ticker-symbol and company-name phrasing so we
catch both trader ("AAPL news") and mainstream ("Apple earnings") feeds.
"""

from __future__ import annotations

from .stock_universe import Stock


def build_queries(stock: Stock) -> list[str]:
    name = stock.name
    ticker = stock.ticker
    return [
        f"{name} stock",
        f"{name} earnings",
        f"{ticker} news",
    ]
