"""
Data agent: fetch fresh OHLCV for universe + market + macro, write pooled
feature matrix to disk. Returns dataset vitals for report_agent.
"""
from __future__ import annotations

import logging

from data.pipeline import build_feature_matrix

logger = logging.getLogger(__name__)


async def run(lookback_days: int = 1200) -> dict:
    logger.info("[data_agent] refreshing pooled feature matrix…")
    frame = build_feature_matrix(lookback_days=lookback_days)
    return {
        "rows":        int(len(frame)),
        "cols":        int(frame.shape[1]),
        "last_date":   str(frame["date"].max().date()),
        "tickers":     int(frame["ticker"].nunique()),
        "ticker_list": sorted(frame["ticker"].astype(str).unique().tolist()),
    }
