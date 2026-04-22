"""
CFTC COT (Commitments of Traders) fetcher — weekly gold-futures positioning.

Source:   CFTC Public Reporting Environment, Socrata JSON API (no key).
Dataset:  Disaggregated Futures-Only Reports  →  `72hh-3qpy`
Contract: Gold - Commodity Exchange Inc.      →  CFTC code `088691`
Release:  Fridays ~3:30 PM ET, reporting Tuesday's positions.

The fetcher downloads the full history once, normalizes the fields we use
(managed-money long / short / net, open interest), and appends to a local
JSONL cache at outputs/cot_gold.jsonl.

Schema (per line):
    {"date": "YYYY-MM-DD", "mm_long": int, "mm_short": int,
     "mm_net": int, "oi": int}
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import requests

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)

API_URL        = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
GOLD_CODE      = "088691"
CACHE_FILENAME = "cot_gold.jsonl"
FETCH_LIMIT    = 5000
STALE_DAYS     = 10            # weekly release; refresh after this gap
HTTP_TIMEOUT   = 30


def _path() -> str:
    return os.path.join(config.OUTPUT_DIR, CACHE_FILENAME)


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def refresh() -> int:
    """Download the full COT history for gold and overwrite the cache file."""
    params = {
        "cftc_contract_market_code": GOLD_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": FETCH_LIMIT,
    }
    try:
        r = requests.get(API_URL, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"CFTC fetch failed: {e}")
        return 0

    if not rows:
        logger.warning("CFTC returned no rows")
        return 0

    records = []
    for row in rows:
        d = (row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not d:
            continue
        long_  = _to_int(row.get("m_money_positions_long_all"))
        short_ = _to_int(row.get("m_money_positions_short_all"))
        oi     = _to_int(row.get("open_interest_all"))
        records.append({
            "date":     d,
            "mm_long":  long_,
            "mm_short": short_,
            "mm_net":   long_ - short_,
            "oi":       oi,
        })
    records.sort(key=lambda r: r["date"])

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(_path(), "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    logger.info(
        f"CFTC COT cache refreshed — {len(records)} weeks, latest {records[-1]['date']}"
    )
    return len(records)


def load() -> list[dict]:
    """Load cache as list sorted ascending by date; [] when missing."""
    p = _path()
    if not os.path.exists(p):
        return []
    out: list[dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"COT cache: skip bad line ({e})")
    out.sort(key=lambda r: r["date"])
    return out


def ensure_fresh() -> list[dict]:
    """
    Load cache. Refresh if empty or the newest record is more than STALE_DAYS
    old. Returns the (possibly updated) record list.
    """
    records = load()
    if not records:
        refresh()
        return load()
    latest = datetime.strptime(records[-1]["date"], "%Y-%m-%d").date()
    if (date.today() - latest) > timedelta(days=STALE_DAYS):
        logger.info(f"COT cache stale (latest {latest}) — refreshing")
        refresh()
        return load()
    return records
