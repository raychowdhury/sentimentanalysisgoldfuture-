"""Strategy ARM/DISARM registry.

Per-rule manual toggle. Default DISARMED. Signal consumer skips fires from
DISARMED rules. ARM via UI button or by editing the JSON state file.

Used to gate signal-to-order flow. Researcher decides which rule is live;
no auto-promotion based on synthetic verdicts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading_platform import audit

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
STATE_FILE = PROJECT / "outputs/trading_platform/strategy_arm_state.json"

DEFAULT_RULES = ["r1_buyer_down", "r2_seller_up", "r7_cvd_divergence"]


def _load() -> dict:
    if not STATE_FILE.exists():
        return {r: {"armed": False, "armed_ts": None} for r in DEFAULT_RULES}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {r: {"armed": False, "armed_ts": None} for r in DEFAULT_RULES}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_state() -> dict:
    state = _load()
    for r in DEFAULT_RULES:
        state.setdefault(r, {"armed": False, "armed_ts": None})
    return state


def is_armed(rule: str) -> bool:
    return get_state().get(rule, {}).get("armed", False)


def arm(rule: str, by: str = "ui") -> dict:
    state = get_state()
    state[rule] = {
        "armed": True,
        "armed_ts": datetime.now(timezone.utc).isoformat(),
        "by": by,
    }
    _save(state)
    audit.log("strategy_arm", {"rule": rule, "by": by})
    return state[rule]


def disarm(rule: str, by: str = "ui") -> dict:
    state = get_state()
    state[rule] = {
        "armed": False,
        "armed_ts": datetime.now(timezone.utc).isoformat(),
        "by": by,
    }
    _save(state)
    audit.log("strategy_disarm", {"rule": rule, "by": by})
    return state[rule]
