"""
Data agent: fetch fresh market + macro data and write the feature matrix.

Returns a small dict that the orchestrator logs and passes downstream so
report_agent can record dataset vitals per cycle.
"""
from __future__ import annotations

import logging

from data.pipeline import build_feature_matrix

logger = logging.getLogger(__name__)


async def run(lookback_days: int = 2500) -> dict:
    logger.info("[data_agent] refreshing feature matrix…")
    frame = build_feature_matrix(lookback_days=lookback_days)
    return {
        "rows":       int(len(frame)),
        "cols":       int(frame.shape[1]),
        "last_date":  str(frame.index[-1].date()),
        "last_close": float(frame["Close"].iloc[-1]),
    }
