"""
Fixed stock universe for v1 — top 20 most influential S&P 500 names.

Edit UNIVERSE below to change coverage. Ticker form follows yfinance
convention (BRK-B, not BRK.B). Company names are used both for UI display
and as news query seeds (e.g. "Apple earnings").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stock:
    ticker: str
    name: str
    sector: str


UNIVERSE: list[Stock] = [
    Stock("AAPL",  "Apple",              "Technology"),
    Stock("MSFT",  "Microsoft",          "Technology"),
    Stock("NVDA",  "Nvidia",             "Technology"),
    Stock("AMZN",  "Amazon",             "Consumer Discretionary"),
    Stock("GOOGL", "Alphabet Class A",   "Communication Services"),
    Stock("GOOG",  "Alphabet Class C",   "Communication Services"),
    Stock("META",  "Meta Platforms",     "Communication Services"),
    Stock("BRK-B", "Berkshire Hathaway", "Financials"),
    Stock("TSLA",  "Tesla",              "Consumer Discretionary"),
    Stock("LLY",   "Eli Lilly",          "Health Care"),
    Stock("AVGO",  "Broadcom",           "Technology"),
    Stock("JPM",   "JPMorgan Chase",     "Financials"),
    Stock("V",     "Visa",               "Financials"),
    Stock("XOM",   "ExxonMobil",         "Energy"),
    Stock("UNH",   "UnitedHealth",       "Health Care"),
    Stock("MA",    "Mastercard",         "Financials"),
    Stock("COST",  "Costco",             "Consumer Staples"),
    Stock("JNJ",   "Johnson & Johnson",  "Health Care"),
    Stock("PG",    "Procter & Gamble",   "Consumer Staples"),
    Stock("HD",    "Home Depot",         "Consumer Discretionary"),
]

_BY_TICKER: dict[str, Stock] = {s.ticker: s for s in UNIVERSE}


def tickers() -> list[str]:
    return [s.ticker for s in UNIVERSE]


def get(ticker: str) -> Stock | None:
    return _BY_TICKER.get(ticker.upper())


def is_known(ticker: str) -> bool:
    return ticker.upper() in _BY_TICKER
