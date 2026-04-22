"""
SPY top-20 holdings fetcher + price/volume enrichment.

Two public entrypoints:
  - fetch_holdings(top_n) -> list[dict] from SSGA daily XLSX
  - enrich_with_prices(holdings) -> same list with price/day_pct/vol_ratio/
    contrib_bps/sector filled in from yfinance

No API keys. SSGA XLSX is a public URL; yfinance is used elsewhere in the
repo for OHLCV and .info lookups.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import openpyxl
import requests
import yfinance as yf

from utils.logger import setup_logger

logger = setup_logger(__name__)

SSGA_SPY_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
HTTP_TIMEOUT = 15
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 NewsSentimentScanner"}

# SSGA uses "BRK.B" / "BF.B"; yfinance uses "BRK-B" / "BF-B".
_YF_TICKER_FIXUP = str.maketrans({".": "-"})

# Sector rarely changes; caching per-ticker avoids a yfinance .info call on
# every 30-second refresh (halves the API calls per enrich).
_SECTOR_CACHE: dict[str, str | None] = {}


def _yf_ticker(ssga_ticker: str) -> str:
    return ssga_ticker.translate(_YF_TICKER_FIXUP)


def _parse_holdings_xlsx(content: bytes, top_n: int) -> list[dict[str, Any]]:
    """Parse SSGA SPY holdings XLSX. Returns top_n rows by weight desc."""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active

    header_idx = None
    rows: list[tuple] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if header_idx is None:
            if row and "Ticker" in row and "Weight" in row:
                header_idx = {col: j for j, col in enumerate(row) if col}
            continue
        if not row or row[header_idx["Ticker"]] is None:
            continue
        rows.append(row)

    if header_idx is None:
        raise ValueError("SSGA XLSX: header row with Ticker/Weight not found")

    name_i, tkr_i, wt_i = header_idx["Name"], header_idx["Ticker"], header_idx["Weight"]

    holdings = []
    for row in rows:
        ticker = row[tkr_i]
        weight = row[wt_i]
        if not ticker or weight is None:
            continue
        try:
            weight_pct = float(weight)
        except (TypeError, ValueError):
            continue
        holdings.append({
            "ticker":     str(ticker).strip(),
            "name":       str(row[name_i]).strip() if row[name_i] else "",
            "weight_pct": weight_pct,
        })

    holdings.sort(key=lambda h: h["weight_pct"], reverse=True)
    return holdings[:top_n]


def fetch_holdings(top_n: int = 20) -> list[dict[str, Any]]:
    """
    Fetch SPY top-N holdings from SSGA. Raises on HTTP / parse failure so the
    service layer can decide what to surface to the UI.
    """
    r = requests.get(SSGA_SPY_URL, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
    r.raise_for_status()
    holdings = _parse_holdings_xlsx(r.content, top_n)
    if len(holdings) < top_n:
        raise ValueError(f"SSGA XLSX: got {len(holdings)} holdings, expected {top_n}")
    logger.info(f"SSGA holdings fetched: {len(holdings)} rows")
    return holdings


def _fetch_sector(ssga_ticker: str) -> str | None:
    """Return sector for a ticker, using an in-process cache."""
    if ssga_ticker in _SECTOR_CACHE:
        return _SECTOR_CACHE[ssga_ticker]
    try:
        info = yf.Ticker(_yf_ticker(ssga_ticker)).info or {}
        sector = info.get("sector")
    except Exception as e:
        logger.warning(f"sector fetch failed for {ssga_ticker}: {e}")
        sector = None
    _SECTOR_CACHE[ssga_ticker] = sector
    return sector


def _fetch_one_price(ssga_ticker: str) -> dict[str, Any]:
    """
    One-ticker price+volume+sector pull. Returns a dict of enrichment fields
    (partial allowed — caller merges; missing fields become None).
    """
    yt = _yf_ticker(ssga_ticker)
    out: dict[str, Any] = {
        "price": None, "day_pct": None, "vol_ratio": None, "sector": None,
    }
    try:
        t = yf.Ticker(yt)
        hist = t.history(period="30d", interval="1d", auto_adjust=True)
        if hist is not None and len(hist) >= 2:
            close = hist["Close"]
            vol   = hist["Volume"]
            last, prev = float(close.iloc[-1]), float(close.iloc[-2])
            out["price"]   = last
            out["day_pct"] = (last - prev) / prev * 100 if prev else None
            vol_today = float(vol.iloc[-1])
            vol_avg20 = float(vol.iloc[-21:-1].mean()) if len(vol) >= 21 else float(vol.iloc[:-1].mean())
            out["vol_ratio"] = vol_today / vol_avg20 if vol_avg20 else None
    except Exception as e:
        logger.warning(f"price fetch failed for {ssga_ticker}: {e}")

    out["sector"] = _fetch_sector(ssga_ticker)
    return out


def enrich_with_prices(holdings: list[dict[str, Any]], workers: int = 8) -> list[dict[str, Any]]:
    """
    Add price, day_pct, vol_ratio, sector, contrib_bps to each holding.
    Network failures are tolerated per-ticker — missing fields become None.
    """
    tickers = [h["ticker"] for h in holdings]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_fetch_one_price, tickers))

    out = []
    for h, r in zip(holdings, results):
        merged = {**h, **r}
        wt, dp = merged.get("weight_pct"), merged.get("day_pct")
        merged["contrib_bps"] = (wt * dp) if (wt is not None and dp is not None) else None
        out.append(merged)
    return out
