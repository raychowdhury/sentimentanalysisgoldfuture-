"""Flask blueprint for trading platform dashboard.

Routes:
  GET  /trading-platform                       — main dashboard
  GET  /api/trading-platform/status            — aggregate status JSON
  POST /api/trading-platform/arm/<rule>        — arm a rule
  POST /api/trading-platform/disarm/<rule>     — disarm a rule
  POST /api/trading-platform/kill/engage       — engage kill switch
  POST /api/trading-platform/kill/disengage    — disengage kill switch
  POST /api/trading-platform/consume-pending   — manually run signal consumer
  POST /api/trading-platform/close-position/<id> — manual close at last price

Mounts via order_flow-style register(app) call from app.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import jsonify, render_template, request

from trading_platform import (
    audit,
    broker,
    oms,
    positions,
    risk,
    signal_consumer,
    strategy_registry,
)

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")


def _orders_tail(n: int = 50) -> list[dict]:
    p = PROJECT / "outputs/trading_platform/orders.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-n:]


def _aggregate_status() -> dict:
    return {
        "broker": {
            "name": broker.broker_name(),
            "connected": broker.is_connected(),
        },
        "account": oms.account_status(),
        "risk": risk.status(),
        "strategies": strategy_registry.get_state(),
        "open_positions": positions.open_positions(),
        "recent_orders": _orders_tail(50),
        "recent_audit": audit.tail(50),
    }


def register(app) -> None:
    @app.route("/trading-platform")
    def trading_platform_view():
        return render_template(
            "trading_platform/dashboard.html",
            status=_aggregate_status(),
        )

    @app.route("/api/trading-platform/status")
    def trading_platform_status():
        return jsonify(_aggregate_status())

    @app.route("/api/trading-platform/arm/<rule>", methods=["POST"])
    def trading_platform_arm(rule):
        return jsonify(strategy_registry.arm(rule))

    @app.route("/api/trading-platform/disarm/<rule>", methods=["POST"])
    def trading_platform_disarm(rule):
        return jsonify(strategy_registry.disarm(rule))

    @app.route("/api/trading-platform/kill/engage", methods=["POST"])
    def trading_platform_kill_engage():
        risk.engage_kill_switch(by=request.args.get("by", "ui"))
        return jsonify({"engaged": True})

    @app.route("/api/trading-platform/kill/disengage", methods=["POST"])
    def trading_platform_kill_disengage():
        risk.disengage_kill_switch(by=request.args.get("by", "ui"))
        return jsonify({"engaged": False})

    @app.route("/api/trading-platform/consume-pending", methods=["POST"])
    def trading_platform_consume_pending():
        return jsonify(signal_consumer.consume_pending())

    @app.route("/api/trading-platform/consume-settled-tail", methods=["POST"])
    def trading_platform_consume_settled():
        n = int(request.args.get("n", "50"))
        return jsonify(signal_consumer.consume_settled_tail(n=n))

    @app.route("/api/trading-platform/manual-order", methods=["POST"])
    def trading_platform_manual_order():
        symbol = request.args.get("symbol", "ESM6")
        side = request.args.get("side", "buy")
        qty = int(request.args.get("qty", "1"))
        price = float(request.args.get("price", "0"))
        atr = float(request.args.get("atr", "1"))
        rec = oms.place_manual_order(symbol, side, qty, price, atr,
                                     note=request.args.get("note", "manual"))
        return jsonify(rec)

    @app.route("/api/trading-platform/close-position/<pos_id>",
               methods=["POST"])
    def trading_platform_close_position(pos_id):
        pos = positions.find_by_signal_id(pos_id)
        if pos is None:
            for p in positions.open_positions():
                if p.get("position_id") == pos_id:
                    pos = p
                    break
        if pos is None:
            return jsonify({"error": "position not found"}), 404
        exit_price = float(request.args.get("price", pos["entry_price"]))
        rec = oms.close_position(pos["position_id"], exit_price,
                                 exit_reason=request.args.get("reason",
                                                              "manual"))
        return jsonify(rec)
