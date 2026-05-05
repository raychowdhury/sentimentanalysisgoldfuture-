"""Risk engine.

Hard caps:
  - per-trade max risk = 1R = $100 (paper unit)
  - daily loss kill = -3R = -$300
  - max concurrent positions = 2
  - max position per symbol = 1 contract
  - kill switch flag-file blocks all new orders

Pre-trade check returns (allowed, reason). If allowed=False, OMS rejects
the order and audit-logs the reject.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading_platform import audit
from trading_platform.broker import DOLLAR_PER_R

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
STATE_FILE = PROJECT / "outputs/trading_platform/risk_state.json"
KILL_FLAG = PROJECT / "outputs/trading_platform/kill_switch.flag"

MAX_TRADE_R = 1.0
DAILY_LOSS_R_STOP = -3.0
MAX_CONCURRENT_POSITIONS = 2
MAX_POSITION_PER_SYMBOL = 1


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"day": _today_utc(), "realized_r_today": 0.0,
                "trades_today": 0}
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        s = {}
    if s.get("day") != _today_utc():
        s = {"day": _today_utc(), "realized_r_today": 0.0,
             "trades_today": 0}
    return s


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def kill_switch_engaged() -> bool:
    return KILL_FLAG.exists()


def engage_kill_switch(by: str = "ui") -> None:
    KILL_FLAG.parent.mkdir(parents=True, exist_ok=True)
    KILL_FLAG.write_text(f"engaged at {datetime.now(timezone.utc).isoformat()} by {by}\n")
    audit.log("kill_switch_engage", {"by": by})


def disengage_kill_switch(by: str = "ui") -> None:
    if KILL_FLAG.exists():
        KILL_FLAG.unlink()
    audit.log("kill_switch_disengage", {"by": by})


def precheck(open_positions: list[dict], proposed_risk_r: float,
             symbol: str) -> tuple[bool, str]:
    if kill_switch_engaged():
        return False, "kill_switch_engaged"
    state = _load_state()
    if state["realized_r_today"] <= DAILY_LOSS_R_STOP:
        return False, f"daily_loss_stop_breached ({state['realized_r_today']:.2f}R)"
    if proposed_risk_r > MAX_TRADE_R:
        return False, f"per_trade_risk_exceeds_cap ({proposed_risk_r:.2f}R > {MAX_TRADE_R}R)"
    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
        return False, f"max_concurrent_positions ({len(open_positions)})"
    same_sym = [p for p in open_positions if p.get("symbol") == symbol]
    if len(same_sym) >= MAX_POSITION_PER_SYMBOL:
        return False, f"max_position_per_symbol ({symbol})"
    return True, "ok"


def realize(realized_r: float, dollars: float = None) -> dict:
    state = _load_state()
    state["realized_r_today"] = round(
        state.get("realized_r_today", 0.0) + realized_r, 4
    )
    state["trades_today"] = state.get("trades_today", 0) + 1
    _save_state(state)
    audit.log("risk_realize", {"r": realized_r, "dollars": dollars,
                               "running_r_today": state["realized_r_today"]})
    return state


def status() -> dict:
    s = _load_state()
    return {
        **s,
        "kill_switch": kill_switch_engaged(),
        "max_trade_r": MAX_TRADE_R,
        "daily_loss_r_stop": DAILY_LOSS_R_STOP,
        "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        "max_position_per_symbol": MAX_POSITION_PER_SYMBOL,
        "dollar_per_r": DOLLAR_PER_R,
    }
