"""
Flask integration for the Order Flow engine.

`register(app)` wires:
  GET  /order-flow                  — HTML dashboard
  GET  /api/order-flow/alerts       — recent alerts JSON
  GET  /api/order-flow/latest       — most recent alert or null
  POST /api/order-flow/ingest       — push a single bar (TradingView/IBKR/...)
  GET  /api/order-flow/stream       — Server-Sent Events stream of new alerts
  POST /api/order-flow/poll/start   — start the yfinance polling worker
  POST /api/order-flow/poll/stop    — stop the polling worker
  GET  /api/order-flow/poll/status  — polling worker status

Reads artefacts written by predictor / alert_engine from
`outputs/order_flow/alerts.json`. The ingest path streams alerts in real time
via SSE so the dashboard never has to poll the JSON file.
"""

from __future__ import annotations

import json
import queue as _queue
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from order_flow_engine.src import alert_store, config as of_cfg, ingest


def _load_alerts(limit: int = 1000) -> list[dict]:
    """Prefer sqlite history; fall back to alerts.json if the DB is empty."""
    try:
        rows = alert_store.query(limit=limit)
        if rows:
            return rows
    except Exception:
        pass
    p = of_cfg.OF_OUTPUT_DIR / "alerts.json"
    if not p.exists():
        return []
    try:
        with p.open() as f:
            data = json.load(f)
        if isinstance(data, dict) and "alerts" in data:
            return list(data["alerts"])
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _proxy_mode_flag(alerts: list[dict]) -> bool:
    if not alerts:
        return True
    latest = alerts[-1]
    return bool(latest.get("data_quality", {}).get("proxy_mode", True))


def _source_status() -> dict:
    """Inspect which real-time adapters are running so the banner can be honest."""
    out = {"alpaca_running": False, "alpaca_symbol": None,
           "poll_running": False}
    try:
        from order_flow_engine.src import realtime_alpaca as ra
        s = ra.status()
        out["alpaca_running"] = bool(s.get("running"))
        out["alpaca_symbol"]  = s.get("symbol")
    except Exception:
        pass
    try:
        out["poll_running"] = bool(ingest.poll_status().get("running"))
    except Exception:
        pass
    return out


def register(app: Flask) -> None:
    """Attach routes to the given Flask app. Idempotent."""

    @app.route("/order-flow")
    def order_flow_view():  # pragma: no cover — exercised end-to-end
        alerts = _load_alerts()
        alerts_desc = list(reversed(alerts))[:50]
        latest = alerts_desc[0] if alerts_desc else None
        return render_template(
            "order_flow.html",
            symbol=of_cfg.OF_SYMBOL,
            anchor_tf=of_cfg.OF_ANCHOR_TF,
            alert_min_conf=of_cfg.OF_ALERT_MIN_CONF,
            alerts=alerts_desc,
            latest=latest,
            proxy_mode=_proxy_mode_flag(alerts),
            source=_source_status(),
        )

    @app.route("/api/order-flow/alerts")
    def order_flow_alerts():  # pragma: no cover
        return jsonify(_load_alerts())

    @app.route("/api/order-flow/latest")
    def order_flow_latest():  # pragma: no cover
        alerts = _load_alerts()
        return jsonify(alerts[-1] if alerts else None)

    @app.route("/api/order-flow/ingest", methods=["POST"])
    def order_flow_ingest():  # pragma: no cover
        """
        Push a single closed bar. Body JSON:
          {symbol, timeframe, timestamp, open, high, low, close, volume?}

        TradingView alert format works directly — set the alert message to:
          {"symbol":"{{ticker}}","timeframe":"{{interval}}",
           "timestamp":"{{time}}","open":{{open}},"high":{{high}},
           "low":{{low}},"close":{{close}},"volume":{{volume}}}
        """
        payload = request.get_json(force=True, silent=True) or {}
        try:
            alert = ingest.ingest_bar(
                symbol=str(payload.get("symbol", of_cfg.OF_SYMBOL)),
                timeframe=str(payload.get("timeframe", of_cfg.OF_ANCHOR_TF)),
                timestamp=payload["timestamp"],
                open_=float(payload["open"]),
                high=float(payload["high"]),
                low=float(payload["low"]),
                close=float(payload["close"]),
                volume=float(payload.get("volume", 0) or 0),
            )
        except KeyError as e:
            return jsonify({"error": f"missing field: {e}"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "alert": alert})

    @app.route("/api/order-flow/stream")
    def order_flow_stream():  # pragma: no cover
        """SSE stream of live alerts. Long-polling — keep one open per dashboard tab."""
        def gen():
            q = ingest.subscribe()
            try:
                # Emit a hello so the client can detect "connected" immediately.
                yield f"event: hello\ndata: {json.dumps({'ok': True})}\n\n"
                while True:
                    try:
                        evt = q.get(timeout=15)
                        yield f"event: {evt.get('type','message')}\ndata: {json.dumps(evt, default=str)}\n\n"
                    except _queue.Empty:
                        # Comment line as keep-alive (per SSE spec).
                        yield ": keep-alive\n\n"
            finally:
                ingest.unsubscribe(q)

        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/order-flow/poll/start", methods=["POST"])
    def order_flow_poll_start():  # pragma: no cover
        body = request.get_json(force=True, silent=True) or {}
        started = ingest.start_polling(
            symbol=body.get("symbol"),
            timeframe=body.get("timeframe"),
            interval_s=int(body.get("interval_s", 60)),
        )
        return jsonify({"started": started, **ingest.poll_status()})

    @app.route("/api/order-flow/poll/stop", methods=["POST"])
    def order_flow_poll_stop():  # pragma: no cover
        ingest.stop_polling()
        return jsonify({"stopped": True})

    @app.route("/api/order-flow/poll/status")
    def order_flow_poll_status():  # pragma: no cover
        return jsonify(ingest.poll_status())

    @app.route("/api/order-flow/alpaca/start", methods=["POST"])
    def order_flow_alpaca_start():  # pragma: no cover
        body = request.get_json(force=True, silent=True) or {}
        from order_flow_engine.src import realtime_alpaca as ra
        started = ra.start_thread(
            symbol=str(body.get("symbol", "SPY")).upper(),
            tf=str(body.get("tf", "1m")),
        )
        return jsonify({"started": started, **ra.status()})

    @app.route("/api/order-flow/alpaca/status")
    def order_flow_alpaca_status():  # pragma: no cover
        from order_flow_engine.src import realtime_alpaca as ra
        return jsonify(ra.status())

    # ── TradingView webhook ────────────────────────────────────────
    @app.route("/api/order-flow/tv/<secret>", methods=["POST"])
    def order_flow_tv_webhook(secret):  # pragma: no cover
        from order_flow_engine.src import tv_webhook as tv
        if secret != tv.SECRET:
            return jsonify({"error": "bad secret"}), 403
        # TV may send body as string or JSON; pull raw and let handler decide.
        raw = request.get_data(as_text=True)
        try:
            payload = request.get_json(force=True, silent=True)
            if payload is None:
                payload = raw
        except Exception:
            payload = raw
        status, body = tv.handle(payload)
        return jsonify(body), status

    @app.route("/api/order-flow/tv/info")
    def order_flow_tv_info():  # pragma: no cover
        from order_flow_engine.src import tv_webhook as tv
        public = request.args.get("host") or request.host_url.rstrip("/")
        return jsonify({
            "webhook_url":    f"{public}/api/order-flow/tv/{tv.SECRET}",
            "secret":         tv.SECRET,
            "payload_template": tv.example_payload(),
            "interval_map":   tv.TV_INTERVAL_MAP,
        })

    @app.route("/api/order-flow/tv/recent")
    def order_flow_tv_recent():  # pragma: no cover
        from order_flow_engine.src import tv_webhook as tv
        return jsonify(tv.recent_hits())

    @app.route("/api/order-flow/notifiers")
    def order_flow_notifiers():  # pragma: no cover
        from order_flow_engine.src import notifier
        return jsonify(notifier.configured())

    @app.route("/api/order-flow/notifiers/test", methods=["POST"])
    def order_flow_notifiers_test():  # pragma: no cover
        from order_flow_engine.src import notifier
        sample = {
            "id": "test_notify", "timestamp_utc": "2026-04-24T03:30:00Z",
            "symbol": "ES=F", "timeframe": "5m", "label": "possible_reversal",
            "confidence": 99, "price": 7150.0, "atr": 3.0,
            "rules_fired": ["test_rule"], "reason_codes": ["Test alert from dashboard"],
            "metrics": {"delta_ratio": -0.5, "cvd_z": 1.2},
            "model": {}, "data_quality": {"proxy_mode": True},
        }
        return jsonify(notifier.fanout(sample))
