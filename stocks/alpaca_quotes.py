"""
Alpaca real-time last-trade fetch for Trader Desk live prices.

Uses Alpaca's free IEX feed via REST. Keys read from env (ALPACA_KEY,
ALPACA_SECRET). Server-side only — never exposed to browser.

Short TTL cache (1s) dedupes concurrent dashboard polls.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Iterable

import requests

logger = logging.getLogger(__name__)

_BASE = "https://data.alpaca.markets/v2/stocks/trades/latest"
_TTL_SEC = 1.0
_TIMEOUT = 4.0

_cache: Dict[str, dict] = {}
_cache_ts: float = 0.0
_cache_key: str = ""
_lock = threading.Lock()


def _headers() -> dict | None:
    key = os.getenv("ALPACA_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        return None
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def get_last_trades(symbols: Iterable[str]) -> Dict[str, dict]:
    """Fetch latest trade per symbol. Returns {SYM: {price, ts}}.

    Empty dict if keys missing or request fails — caller falls back to
    stored aggregate price.
    """
    global _cache, _cache_ts, _cache_key

    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not syms:
        return {}
    cache_key = ",".join(syms)

    with _lock:
        now = time.time()
        if cache_key == _cache_key and (now - _cache_ts) < _TTL_SEC:
            return dict(_cache)

    headers = _headers()
    if headers is None:
        logger.debug("alpaca keys missing — skipping live quote fetch")
        return {}

    try:
        resp = requests.get(
            _BASE,
            params={"symbols": cache_key, "feed": "iex"},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except requests.RequestException as e:
        logger.warning(f"alpaca quote fetch failed: {e}")
        return {}

    out: Dict[str, dict] = {}
    trades = payload.get("trades") or {}
    for sym, trade in trades.items():
        price = trade.get("p")
        ts = trade.get("t")
        if price is None:
            continue
        out[sym.upper()] = {"price": float(price), "ts": ts}

    with _lock:
        _cache = out
        _cache_ts = time.time()
        _cache_key = cache_key

    return out
