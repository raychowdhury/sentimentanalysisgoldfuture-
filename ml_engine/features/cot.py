"""CFTC Commitments of Traders — generic per-contract fetcher.

Returns mm-net z-score (managed-money longs minus shorts, normalized).
Cached weekly. Forward-filled to bar timestamps.

Contract codes:
    GC: 088691  (gold)
    ES: 13874A  (E-mini S&P 500)
    NQ: 209742  (E-mini Nasdaq-100)
    CL: 067651  (light sweet crude)
"""
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from ml_engine import config

logger = logging.getLogger(__name__)

CACHE_DIR = config.HISTORY_DIR.parent / "macro"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_HOURS = 24

# Two CFTC datasets:
#   gpe5-46if: Disaggregated futures (commodities — GC, CL) -> m_money_*
#   yw9f-hn96: Traders in Financial Futures (ES, NQ, bonds, FX) -> lev_money_*
CFTC_DISAGG_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_TFF_URL    = "https://publicreporting.cftc.gov/resource/yw9f-hn96.json"

CONTRACT = {
    "GC": ("088691", "disagg"),
    "CL": ("067651", "disagg"),
    "ES": ("13874A", "tff"),
    "NQ": ("209742", "tff"),
}
ZSCORE_LOOKBACK = 52  # weeks (~1 year)


def _stale(p: Path) -> bool:
    return (not p.exists()) or (time.time() - p.stat().st_mtime) > CACHE_TTL_HOURS * 3600


def _fetch(contract_code: str, kind: str) -> pd.Series | None:
    cache = CACHE_DIR / f"cot_{contract_code}.parquet"
    if not _stale(cache):
        return pd.read_parquet(cache)["mm_net"]
    try:
        url = CFTC_DISAGG_URL if kind == "disagg" else CFTC_TFF_URL
        params = {
            "cftc_contract_market_code": contract_code,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": "500",
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], utc=True)
        if kind == "disagg":
            long_col, short_col = "m_money_positions_long_all", "m_money_positions_short_all"
        else:
            long_col, short_col = "lev_money_positions_long", "lev_money_positions_short"
        df["mm_long"]  = pd.to_numeric(df.get(long_col), errors="coerce")
        df["mm_short"] = pd.to_numeric(df.get(short_col), errors="coerce")
        df["mm_net"] = df["mm_long"] - df["mm_short"]
        s = df.dropna(subset=["mm_net"]).set_index("date")["mm_net"].sort_index()
        s.to_frame().to_parquet(cache)
        return s
    except Exception as e:
        logger.warning(f"COT {contract_code} fetch failed: {e}")
        if cache.exists():
            return pd.read_parquet(cache)["mm_net"]
        return None


def build(symbol: str, bar_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Return DataFrame with cot_z (z-score) and cot_d (week-over-week diff),
    forward-filled to bar_index."""
    cfg = CONTRACT.get(symbol)
    if cfg is None:
        return pd.DataFrame(index=bar_index)
    code, kind = cfg
    s = _fetch(code, kind)
    if s is None or s.empty:
        return pd.DataFrame(index=bar_index)
    # Rolling z-score
    mu = s.rolling(ZSCORE_LOOKBACK, min_periods=10).mean()
    sd = s.rolling(ZSCORE_LOOKBACK, min_periods=10).std()
    z = (s - mu) / sd
    diff = s.diff()
    feats = pd.DataFrame({"cot_z": z, "cot_d": diff})
    return feats.reindex(pd.to_datetime(bar_index), method="ffill")
