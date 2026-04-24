"""
TradingView webhook ingest.

TradingView (free + paid) lets you post alerts to any URL when a chart event
fires. We expose `/api/order-flow/tv/<secret>` where TV POSTs a JSON body.
The body is parsed, normalized, and shipped to ingest.ingest_bar() — same
path as the IBKR/Alpaca adapters.

Why secret in the URL? TV cannot add custom HTTP headers, so we authenticate
via a path component. Secret comes from env var OF_TV_SECRET; auto-generated
at module import if missing so dev still works without setup.

Recommended TradingView alert message (paste exactly):

    {
      "symbol":    "{{ticker}}",
      "exchange":  "{{exchange}}",
      "timeframe": "{{interval}}",
      "timestamp": "{{time}}",
      "open":      {{open}},
      "high":      {{high}},
      "low":       {{low}},
      "close":     {{close}},
      "volume":    {{volume}}
    }

TV interval values are minutes-as-strings ("1", "5", "15", "60", "240")
or "D"/"W"/"M". We normalize to engine TF strings ("1m", "5m", ...).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from order_flow_engine.src import config as of_cfg, ingest

# ── secret ───────────────────────────────────────────────────────────────────

_DEFAULT_SECRET_FILE = of_cfg.OF_OUTPUT_DIR / "tv_secret.txt"


def _load_or_create_secret() -> str:
    env = os.getenv("OF_TV_SECRET", "").strip()
    if env:
        return env
    if _DEFAULT_SECRET_FILE.exists():
        try:
            s = _DEFAULT_SECRET_FILE.read_text().strip()
            if s:
                return s
        except Exception:
            pass
    s = secrets.token_urlsafe(24)
    try:
        _DEFAULT_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEFAULT_SECRET_FILE.write_text(s)
    except Exception:
        pass
    return s


SECRET: str = _load_or_create_secret()

# ── interval normalization ───────────────────────────────────────────────────

# TradingView interval string → engine TF string
TV_INTERVAL_MAP: dict[str, str] = {
    "1":   "1m",  "3":   "3m",  "5":   "5m",
    "15":  "15m", "30":  "30m",
    "45":  "45m", "60":  "1h",  "120": "2h",
    "180": "3h",  "240": "4h",
    "D":   "1d",  "1D":  "1d",
    "W":   "1w",  "1W":  "1w",
    "M":   "1M",  "1M":  "1M",
}


def normalize_interval(tv_value: Any) -> str:
    if tv_value is None:
        return of_cfg.OF_ANCHOR_TF
    s = str(tv_value).strip()
    if not s:
        return of_cfg.OF_ANCHOR_TF
    # If user already passed engine format (e.g. "15m"), keep as is.
    if s in TV_INTERVAL_MAP.values():
        return s
    return TV_INTERVAL_MAP.get(s, s)


# ── recent-hits log (in-memory + jsonl) ──────────────────────────────────────

_HITS: deque[dict] = deque(maxlen=100)
_lock = threading.Lock()
_HITS_LOG_PATH = of_cfg.OF_OUTPUT_DIR / "tv_hits.jsonl"


def _log_hit(payload: dict, status: str, alert_id: str | None, error: str | None) -> None:
    rec = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "status":      status,
        "alert_id":    alert_id,
        "error":       error,
        "payload":     payload,
    }
    with _lock:
        _HITS.append(rec)
    try:
        _HITS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _HITS_LOG_PATH.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def recent_hits(limit: int = 50) -> list[dict]:
    with _lock:
        return list(_HITS)[-limit:]


# ── handler ──────────────────────────────────────────────────────────────────

def handle(payload: Any) -> tuple[int, dict]:
    """
    Parse a TradingView webhook body and route it to ingest.

    Returns (http_status, response_body).
    Always logs the hit, success or failure, so /tv/recent shows everything.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception as e:
            _log_hit({"raw": payload}, "error", None, f"json parse: {e}")
            return 400, {"error": "body is not valid JSON"}

    if not isinstance(payload, dict):
        _log_hit({"raw": payload}, "error", None, "body not an object")
        return 400, {"error": "body must be a JSON object"}

    try:
        symbol    = str(payload.get("symbol", of_cfg.OF_SYMBOL)).strip()
        timeframe = normalize_interval(payload.get("timeframe") or
                                       payload.get("interval"))
        timestamp = payload.get("timestamp") or datetime.now(timezone.utc).isoformat()
        open_  = float(payload["open"])
        high   = float(payload["high"])
        low    = float(payload["low"])
        close  = float(payload["close"])
        volume = float(payload.get("volume", 0) or 0)
    except KeyError as e:
        _log_hit(payload, "error", None, f"missing field: {e}")
        return 400, {"error": f"missing field: {e}"}
    except (ValueError, TypeError) as e:
        _log_hit(payload, "error", None, f"bad value: {e}")
        return 400, {"error": f"bad numeric value: {e}"}

    try:
        alert = ingest.ingest_bar(
            symbol=symbol, timeframe=timeframe, timestamp=timestamp,
            open_=open_, high=high, low=low, close=close, volume=volume,
        )
    except Exception as e:
        _log_hit(payload, "error", None, f"ingest crash: {e}")
        return 500, {"error": str(e)}

    if alert is None:
        _log_hit(payload, "ok-no-alert", None, None)
        return 200, {"ok": True, "alert": None}

    _log_hit(payload, "ok-alert", alert.get("id"), None)
    return 200, {"ok": True, "alert": alert}


# ── setup helpers ────────────────────────────────────────────────────────────

def webhook_url(public_host: str | None = None, port: int = 5001) -> str:
    """Build the canonical webhook URL TV should POST to."""
    host = public_host or os.getenv("OF_PUBLIC_HOST") or f"http://localhost:{port}"
    return f"{host.rstrip('/')}/api/order-flow/tv/{SECRET}"


def example_payload() -> str:
    return (
        "{\n"
        '  "symbol":    "{{ticker}}",\n'
        '  "exchange":  "{{exchange}}",\n'
        '  "timeframe": "{{interval}}",\n'
        '  "timestamp": "{{time}}",\n'
        '  "open":      {{open}},\n'
        '  "high":      {{high}},\n'
        '  "low":       {{low}},\n'
        '  "close":     {{close}},\n'
        '  "volume":    {{volume}}\n'
        "}"
    )
