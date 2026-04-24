"""
External notifier — fan out new alerts to Telegram (and Discord webhook).

Works when the dashboard is closed. Configure via env vars:
    TG_BOT_TOKEN     Telegram bot token from @BotFather
    TG_CHAT_ID       Your chat ID (or channel/group)
    DISCORD_WEBHOOK  Discord channel webhook URL (optional)

Both channels skip silently when their env vars are unset.

Fan-out happens inside `alert_engine.emit()` — every emitted alert is
forwarded once. Failures are logged but never block the main pipeline.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from utils.logger import setup_logger

logger = setup_logger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
_TG_TIMEOUT = 5
_DISCORD_TIMEOUT = 5


def _direction(alert: dict) -> str:
    fixed = {
        "buyer_absorption":  "↓ DOWN",
        "seller_absorption": "↑ UP",
        "bullish_trap":      "↓ DOWN",
        "bearish_trap":      "↑ UP",
    }
    if alert["label"] in fixed:
        return fixed[alert["label"]]
    if alert["label"] == "possible_reversal":
        dr = (alert.get("metrics") or {}).get("delta_ratio", 0) or 0
        if dr > 0:
            return "↓ DOWN"
        if dr < 0:
            return "↑ UP"
    return ""


def _format(alert: dict) -> str:
    m = alert.get("metrics") or {}
    direction = _direction(alert)
    rules = ", ".join(alert.get("rules_fired") or [])
    proxy = alert.get("data_quality", {}).get("proxy_mode", True)
    src = "proxy" if proxy else "real-flow"
    label_h = alert['label'].replace('_', ' ').title()
    lines = [
        f"🚨 {label_h} {direction}",
        f"{alert['symbol']} {alert['timeframe']} · conf {alert['confidence']}",
        f"price {alert.get('price','?')}  ATR {alert.get('atr','?')}",
        f"Δ {m.get('delta_ratio','?')}  CVDz {m.get('cvd_z','?')}",
        f"rules: {rules}",
        f"src: {src} · {alert['timestamp_utc']}",
    ]
    return "\n".join(lines)


def send_telegram(alert: dict) -> bool:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat  = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        r = requests.post(
            _TG_API.format(token=token),
            data={"chat_id": chat, "text": _format(alert),
                  "disable_web_page_preview": "true"},
            timeout=_TG_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning(f"telegram send failed {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"telegram send error: {e}")
        return False


def send_discord(alert: dict) -> bool:
    url = os.getenv("DISCORD_WEBHOOK", "").strip()
    if not url:
        return False
    try:
        r = requests.post(url, json={"content": _format(alert)},
                          timeout=_DISCORD_TIMEOUT)
        if r.status_code not in (200, 204):
            logger.warning(f"discord send failed {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"discord send error: {e}")
        return False


def fanout(alert: dict) -> dict:
    """Fire all configured notifiers; return per-channel send result."""
    return {
        "telegram": send_telegram(alert),
        "discord":  send_discord(alert),
    }


def configured() -> dict:
    """Which notifier channels have credentials present."""
    return {
        "telegram": bool(os.getenv("TG_BOT_TOKEN") and os.getenv("TG_CHAT_ID")),
        "discord":  bool(os.getenv("DISCORD_WEBHOOK")),
    }
