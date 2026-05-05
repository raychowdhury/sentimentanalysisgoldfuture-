"""Order management system — paper.

State machine: NEW → WORKING → FILLED | REJECTED | CANCELLED
Persists to orders.jsonl (append-only).

Public entry points:
  - place_paper_order(fire_dict)      → routes a fire through risk + broker
  - close_position_at_horizon(pos)    → simulated horizon-bar exit
  - close_position_at_stop(pos)       → 1R adverse stop-out exit
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from trading_platform import audit, broker, positions, risk
from trading_platform.broker import (
    COMMISSION_PER_RT,
    DOLLAR_PER_R,
    SLIPPAGE_ATR,
)

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
ORDERS_LOG = PROJECT / "outputs/trading_platform/orders.jsonl"
FILLS_LOG = PROJECT / "outputs/trading_platform/fills.jsonl"
ACCOUNT_FILE = PROJECT / "outputs/trading_platform/account.json"

PAPER_BALANCE_DEFAULT = 100_000.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _account() -> dict:
    if not ACCOUNT_FILE.exists():
        a = {
            "broker": broker.broker_name(),
            "connected": broker.is_connected(),
            "paper_balance": PAPER_BALANCE_DEFAULT,
            "starting_balance": PAPER_BALANCE_DEFAULT,
            "realized_pnl": 0.0,
        }
        ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_FILE.write_text(json.dumps(a, indent=2, default=str))
        return a
    try:
        return json.loads(ACCOUNT_FILE.read_text())
    except Exception:
        return {"broker": broker.broker_name(), "connected": False,
                "paper_balance": PAPER_BALANCE_DEFAULT,
                "starting_balance": PAPER_BALANCE_DEFAULT,
                "realized_pnl": 0.0}


def _save_account(a: dict) -> None:
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(a, indent=2, default=str))


def account_status() -> dict:
    return _account()


def place_paper_order(fire: dict) -> dict:
    """Route a fire (dict from outcomes JSONL/pending JSON) to paper broker.

    Required fire fields: signal_id, symbol, rule, direction, entry_close, atr,
    horizon_bars, settle_eta_utc.
    """
    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    side = "buy" if fire.get("direction", 0) > 0 else "sell"
    qty = 1
    proposed_risk_r = 1.0

    open_pos = positions.open_positions()
    allowed, reason = risk.precheck(open_pos, proposed_risk_r, fire["symbol"])

    if not allowed:
        rec = {
            "order_id": order_id,
            "signal_id": fire["signal_id"],
            "rule": fire["rule"],
            "symbol": fire["symbol"],
            "side": side,
            "qty": qty,
            "state": "REJECTED",
            "ts": _now(),
            "reject_reason": reason,
        }
        _append(ORDERS_LOG, rec)
        audit.log("order_rejected", rec)
        return rec

    decision_price = float(fire["entry_close"])
    atr = float(fire["atr"])
    fill = broker.synth_entry_fill(order_id, fire["signal_id"],
                                   fire["symbol"], side, qty,
                                   decision_price, atr)
    rec = {
        "order_id": order_id,
        "signal_id": fire["signal_id"],
        "rule": fire["rule"],
        "symbol": fire["symbol"],
        "side": side,
        "qty": qty,
        "state": "FILLED",
        "decision_price": decision_price,
        "fill_price": fill.fill_price,
        "atr": atr,
        "ts": _now(),
        "fill_id": fill.fill_id,
        "horizon_bars": fire.get("horizon_bars", 12),
        "settle_eta_utc": fire.get("settle_eta_utc"),
    }
    _append(ORDERS_LOG, rec)
    _append(FILLS_LOG, fill.__dict__)
    audit.log("order_filled", rec)

    pos = {
        "position_id": f"pos-{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "signal_id": fire["signal_id"],
        "rule": fire["rule"],
        "symbol": fire["symbol"],
        "side": side,
        "qty": qty,
        "entry_price": fill.fill_price,
        "atr": atr,
        "stop_price": fill.fill_price - atr if side == "buy"
                                  else fill.fill_price + atr,
        "horizon_eta_utc": fire.get("settle_eta_utc"),
        "open_ts": _now(),
        "fwd_r_signed_settled": fire.get("fwd_r_signed"),
    }
    positions.add(pos)
    audit.log("position_opened", pos)
    return rec


def close_position(position_id: str, exit_price: float,
                   exit_reason: str = "horizon") -> dict | None:
    pos = positions.remove(position_id)
    if pos is None:
        return None
    side = pos["side"]
    fill = broker.synth_exit_fill(pos["order_id"], pos["signal_id"],
                                  pos["symbol"], side, pos["qty"],
                                  exit_price, pos["atr"], exit_reason)
    pnl_per_unit = (fill.fill_price - pos["entry_price"]) * (
        1 if side == "buy" else -1
    )
    realized_r = pnl_per_unit / pos["atr"]
    realized_dollars = realized_r * DOLLAR_PER_R - COMMISSION_PER_RT
    rec = {
        "position_id": position_id,
        "signal_id": pos["signal_id"],
        "rule": pos["rule"],
        "symbol": pos["symbol"],
        "side": side,
        "entry_price": pos["entry_price"],
        "exit_price": fill.fill_price,
        "atr": pos["atr"],
        "realized_r": round(realized_r, 4),
        "realized_dollars": round(realized_dollars, 2),
        "exit_reason": exit_reason,
        "fill_id": fill.fill_id,
        "close_ts": _now(),
    }
    _append(FILLS_LOG, fill.__dict__)
    audit.log("position_closed", rec)

    a = _account()
    a["realized_pnl"] = round(a.get("realized_pnl", 0.0) + realized_dollars, 2)
    a["paper_balance"] = round(a.get("paper_balance", PAPER_BALANCE_DEFAULT)
                               + realized_dollars, 2)
    _save_account(a)
    risk.realize(realized_r, realized_dollars)
    return rec
