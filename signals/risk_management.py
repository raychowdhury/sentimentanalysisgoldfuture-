"""
Risk management validation layer.

Checks an existing trade setup against the minimum RR requirement
and downgrades to NO_TRADE if the threshold is not met.
"""

import config
from utils.logger import setup_logger

logger = setup_logger(__name__)


def validate(setup: dict, tf: dict | None = None) -> dict:
    """
    Validate a trade setup dict against the minimum RR.

    tf – optional timeframe profile; its min_rr overrides config.MIN_RR.
    Returns a (possibly modified) copy — the original is not mutated.
    """
    min_rr = tf["min_rr"] if tf else config.MIN_RR
    rr = setup.get("risk_reward_ratio")
    if rr is None:
        return setup   # already a no-trade setup

    if rr < min_rr:
        logger.warning(
            f"RR {rr:.2f} < required {min_rr:.1f} — downgrading to NO_TRADE"
        )
        setup = {
            **setup,
            "trade_valid":    False,
            "trade_decision": "NO_TRADE",
            "setup_note":     f"RR {rr:.2f} does not meet minimum {min_rr:.1f}",
        }

    return setup


def required_tp(entry: float, stop: float, min_rr: float | None = None) -> float:
    """
    Calculate the minimum take-profit price to satisfy min_rr.
    Works for both BUY (entry > stop) and SELL (entry < stop).
    """
    rr   = min_rr if min_rr is not None else config.MIN_RR
    risk = abs(entry - stop)
    return entry + rr * risk if entry > stop else entry - rr * risk
