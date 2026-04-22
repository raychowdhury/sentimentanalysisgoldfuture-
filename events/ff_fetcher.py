"""
ForexFactory calendar fetcher — uses the public Faireconomy JSON export
(same data FF publishes, CDN-hosted, no Cloudflare, no auth).

Endpoint returns the current week's events with fields:
  title, country, date (ISO-8601 with ET offset), impact, forecast, previous

Cached to disk with a short TTL so repeated pipeline runs don't re-fetch.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import config
from events.calendar import Event
from utils.logger import setup_logger

logger = setup_logger(__name__)

_IMPACT_RANK = {"Low": 0, "Medium": 1, "High": 2, "Holiday": -1}
_DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_DEFAULT_CACHE = "outputs/ff_calendar_cache.json"
_DEFAULT_TTL_SECONDS = 3600

# Ordered: first matching substring wins. Case-insensitive. Only applied to
# USD events so a UK/EU CPI doesn't get kind="CPI".
_TITLE_KIND_RULES_USD: list[tuple[str, str]] = [
    ("fomc",              "FOMC"),
    ("federal funds",     "FOMC"),
    ("fed chair",         "FOMC"),
    ("fed chairman",      "FOMC"),
    ("core pce",          "PCE"),
    ("pce price",         "PCE"),
    ("cpi",               "CPI"),
    ("non-farm payrolls", "NFP"),
    ("non-farm",          "NFP"),
    ("nonfarm payrolls",  "NFP"),
    ("nfp",               "NFP"),
]

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def _cache_path() -> Path:
    return Path(getattr(config, "FF_CALENDAR_CACHE", _DEFAULT_CACHE))


def _cache_is_fresh(path: Path, ttl_seconds: int) -> bool:
    if not path.exists():
        return False
    age = datetime.now(tz=timezone.utc).timestamp() - path.stat().st_mtime
    return age < ttl_seconds


def fetch_raw(force: bool = False) -> list[dict]:
    """
    Return the raw event list. Served from disk cache unless stale or `force`.
    Network failure falls back to cached copy if one exists.
    """
    url = getattr(config, "FF_CALENDAR_URL", _DEFAULT_URL)
    ttl = int(getattr(config, "FF_CALENDAR_TTL_SECONDS", _DEFAULT_TTL_SECONDS))
    path = _cache_path()

    if not force and _cache_is_fresh(path, ttl):
        try:
            with path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"FF cache unreadable ({e}), re-fetching")

    try:
        req = Request(url, headers={"User-Agent": _BROWSER_UA})
        with urlopen(req, timeout=10) as r:
            data = json.load(r)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(data, f)
        logger.info(f"FF calendar: {len(data)} events fetched")
        return data
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.warning(f"FF fetch failed: {e}")
        if path.exists():
            logger.info("Using stale FF cache")
            try:
                with path.open() as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []


def _event_date(iso_str: str) -> date | None:
    try:
        return datetime.fromisoformat(iso_str).date()
    except (ValueError, TypeError):
        return None


def _classify_kind(title: str, country: str) -> str:
    """
    Map a FF event title to an existing kind (FOMC/CPI/PCE/NFP) when possible.
    Only USD events are classified — other currencies stay "FF" so a UK CPI
    doesn't trigger the US CPI blackout rules.
    """
    if country != "USD":
        return "FF"
    low = title.lower()
    for needle, kind in _TITLE_KIND_RULES_USD:
        if needle in low:
            return kind
    return "FF"


def get_events(start: date, end: date) -> list[Event]:
    """
    Return FF-sourced Event records in [start, end] that pass the impact +
    country filter. USD events are classified into existing kinds
    (FOMC/CPI/NFP/PCE) so they dedup against events/calendar.py; everything
    else carries kind="FF".
    """
    if not getattr(config, "FF_CALENDAR_ENABLED", False):
        return []

    min_impact = getattr(config, "FF_IMPACT_MIN", "High")
    min_rank = _IMPACT_RANK.get(min_impact, 2)
    countries = set(getattr(config, "FF_COUNTRIES", ["USD"]))

    raw = fetch_raw()
    out: list[Event] = []
    for row in raw:
        country = row.get("country", "")
        if country not in countries:
            continue
        if _IMPACT_RANK.get(row.get("impact", ""), -1) < min_rank:
            continue
        ev_date = _event_date(row.get("date", ""))
        if ev_date is None or not (start <= ev_date <= end):
            continue
        title = row.get("title", "?")
        kind  = _classify_kind(title, country)
        label = f"{title} ({country})"
        out.append(Event(ev_date, kind, label))

    out.sort(key=lambda e: (e.date, e.kind))
    return out
