"""
SPX top-influencers service with two-tier caching.

Tiers:
  - holdings cache (24h TTL) — SSGA XLSX only changes daily; re-downloading
    it every 30s is wasteful and risks rate-limits.
  - payload cache (30s TTL)  — prices/volumes re-enriched every 30s so the
    live page can patch tile values without thrashing Yahoo.

Public:
  get_top_influencers() -> dict payload
  reset_cache()         -> clear both tiers (tests)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from market import spx_fetcher
from utils.logger import setup_logger

_NY = ZoneInfo("America/New_York")
_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)

logger = setup_logger(__name__)

PAYLOAD_TTL_SECONDS  = 30
HOLDINGS_TTL_SECONDS = 24 * 3600
DEFAULT_TOP_N = 20

_lock = threading.Lock()
_holdings_cache: dict[str, Any] = {"ts": 0.0, "top_n": 0, "data": None}
_payload_cache:  dict[str, Any] = {"ts": 0.0, "payload": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_market_open(now: datetime | None = None) -> bool:
    """
    True during US equities regular trading hours (9:30–16:00 ET, Mon–Fri).
    US market holidays are not handled — tolerable for a polling gate since
    holiday calls just return stale data, not an error.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(_NY)
    if now.weekday() >= 5:  # Sat, Sun
        return False
    t = now.time()
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def _get_holdings(top_n: int) -> list[dict[str, Any]]:
    """Return SPY top-N holdings, using the 24h cache when fresh."""
    now = time.monotonic()
    if (
        _holdings_cache["data"] is not None
        and _holdings_cache["top_n"] == top_n
        and (now - _holdings_cache["ts"]) < HOLDINGS_TTL_SECONDS
    ):
        return _holdings_cache["data"]

    data = spx_fetcher.fetch_holdings(top_n=top_n)
    _holdings_cache["ts"] = now
    _holdings_cache["top_n"] = top_n
    _holdings_cache["data"] = data
    return data


def _build_payload(top_n: int) -> dict[str, Any]:
    holdings = _get_holdings(top_n)
    enriched = spx_fetcher.enrich_with_prices(holdings)

    up = sum(1 for r in enriched if (r.get("day_pct") or 0) > 0)
    dn = sum(1 for r in enriched if (r.get("day_pct") or 0) < 0)
    net_bps = sum(r["contrib_bps"] for r in enriched if r.get("contrib_bps") is not None)

    return {
        "as_of":        _now_iso(),
        "top_n":        top_n,
        "rows":         enriched,
        "breadth":      {"up": up, "down": dn, "total": len(enriched)},
        "net_bps":      round(net_bps, 2),
        "market_open":  is_market_open(),
        "error":        None,
    }


def get_top_influencers(top_n: int = DEFAULT_TOP_N, force_refresh: bool = False) -> dict[str, Any]:
    """Returns cached payload; refreshes on miss or when TTL expires."""
    now = time.monotonic()
    with _lock:
        if (
            not force_refresh
            and _payload_cache["payload"] is not None
            and (now - _payload_cache["ts"]) < PAYLOAD_TTL_SECONDS
            and _payload_cache["payload"].get("top_n") == top_n
        ):
            return _payload_cache["payload"]

    try:
        payload = _build_payload(top_n)
    except Exception as e:
        logger.warning(f"SPX service refresh failed: {e}")
        stale = _payload_cache["payload"]
        if stale is not None:
            return {**stale, "error": f"refresh failed: {e}"}
        return {
            "as_of":        _now_iso(),
            "top_n":        top_n,
            "rows":         [],
            "breadth":      {"up": 0, "down": 0, "total": 0},
            "net_bps":      0.0,
            "market_open":  is_market_open(),
            "error":        f"SPX data unavailable: {e}",
        }

    with _lock:
        _payload_cache["ts"] = now
        _payload_cache["payload"] = payload
    return payload


def reset_cache() -> None:
    """Test helper — clears both cache tiers."""
    with _lock:
        _holdings_cache["ts"] = 0.0
        _holdings_cache["top_n"] = 0
        _holdings_cache["data"] = None
        _payload_cache["ts"] = 0.0
        _payload_cache["payload"] = None
