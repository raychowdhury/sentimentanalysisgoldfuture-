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
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _NY = timezone.utc

from utils.logger import setup_logger

logger = setup_logger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
_TG_TIMEOUT = 5
_DISCORD_TIMEOUT = 5


_DIRECTION_SIGN = {
    "buyer_absorption":  -1,
    "seller_absorption": +1,
    "bullish_trap":      -1,
    "bearish_trap":      +1,
}


def _direction_sign(alert: dict) -> int:
    """+1 long, -1 short, 0 flat/unknown."""
    label = alert.get("label", "")
    if label in _DIRECTION_SIGN:
        return _DIRECTION_SIGN[label]
    if label == "possible_reversal":
        dr = (alert.get("metrics") or {}).get("delta_ratio", 0) or 0
        if dr > 0:
            return -1
        if dr < 0:
            return +1
    return 0


def _direction(alert: dict) -> str:
    s = _direction_sign(alert)
    if s > 0: return "↑ UP"
    if s < 0: return "↓ DOWN"
    return ""


# Stop/target defaults in ATR multiples. Keep modest; traders can override.
_STOP_ATR_MULT   = 1.0
_TARGET_ATR_MULT = 2.0

# $/point per contract for USD conversion in the Telegram body.
_POINT_VALUE = {
    "ES=F": 50.0, "ES": 50.0, "MES": 5.0,
    "NQ=F": 20.0, "NQ": 20.0, "MNQ": 2.0,
    "YM=F": 5.0,  "YM": 5.0,  "MYM": 0.5,
    "RTY=F": 50.0, "RTY": 50.0, "M2K": 5.0,
    "GC=F": 100.0, "GC": 100.0, "MGC": 10.0,
    "CL=F": 1000.0,
}


def _trade_plan(alert: dict) -> list[str]:
    """Build BUY/SELL + stop + target lines. Empty list if direction unknown."""
    s = _direction_sign(alert)
    if s == 0:
        return []
    price = alert.get("price")
    atr   = alert.get("atr")
    if not isinstance(price, (int, float)) or not isinstance(atr, (int, float)) or atr <= 0:
        return []

    stop_dist   = _STOP_ATR_MULT   * atr
    target_dist = _TARGET_ATR_MULT * atr

    if s > 0:   # long
        action = f"BUY @ {price:.2f}"
        stop   = price - stop_dist
        target = price + target_dist
        stop_diff   = f"−{stop_dist:.2f}"
        target_diff = f"+{target_dist:.2f}"
    else:       # short
        action = f"SELL @ {price:.2f}"
        stop   = price + stop_dist
        target = price - target_dist
        stop_diff   = f"+{stop_dist:.2f}"
        target_diff = f"−{target_dist:.2f}"

    pt_val = _POINT_VALUE.get(alert.get("symbol", ""), 0.0)
    risk_usd   = stop_dist   * pt_val if pt_val else 0.0
    reward_usd = target_dist * pt_val if pt_val else 0.0
    money = ""
    if pt_val:
        money = f" (risk ${risk_usd:.0f} / reward ${reward_usd:.0f})"

    return [
        f"Entry: {action}",
        f"Stop:  {stop:.2f}  ({stop_diff} pts, {_STOP_ATR_MULT:.1f}×ATR)",
        f"Target: {target:.2f} ({target_diff} pts, {_TARGET_ATR_MULT:.1f}×ATR, ~{_TARGET_ATR_MULT/_STOP_ATR_MULT:.0f}R){money}",
    ]


def _to_ny(ts_iso: str) -> str:
    """Convert ISO UTC timestamp to NY-zone string with TZ tag."""
    if not ts_iso:
        return ""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_NY).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return ts_iso


def _format(alert: dict) -> str:
    m = alert.get("metrics") or {}
    direction = _direction(alert)
    rules = ", ".join(alert.get("rules_fired") or [])
    proxy = alert.get("data_quality", {}).get("proxy_mode", True)
    src = "proxy" if proxy else "real-flow"
    label_h = alert['label'].replace('_', ' ').title()
    ts_ny = _to_ny(alert.get("timestamp_utc", ""))
    lines = [
        f"🚨 {label_h} {direction}",
        f"{alert['symbol']} {alert['timeframe']} · conf {alert['confidence']}",
    ]
    lines.extend(_trade_plan(alert))
    lines.extend([
        f"ATR {alert.get('atr','?')} · Δ {m.get('delta_ratio','?')} · CVDz {m.get('cvd_z','?')}",
        f"rules: {rules}",
        f"src: {src} · {ts_ny}",
    ])
    return "\n".join(lines)


def send_telegram(alert: dict) -> bool:
    """Send alert to every active subscriber + the env-var seed chat.

    Telegram is restricted to ES 15m alerts only. Other symbols/timeframes
    still flow through the dashboard, JSONL, and other notifiers.
    """
    sym = str(alert.get("symbol", "")).upper()
    tf = str(alert.get("timeframe", ""))
    if not sym.startswith("ES") or tf != "15m":
        return False
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    if not token:
        return False
    try:
        from order_flow_engine.src import tg_subscribers
        chat_ids = tg_subscribers.all_active()
    except Exception:
        chat_ids = []
        seed = os.getenv("TG_CHAT_ID", "").strip()
        if seed:
            try:
                chat_ids = [int(seed)]
            except ValueError:
                pass

    if not chat_ids:
        return False

    text = _format(alert)
    sent_any = False
    for cid in chat_ids:
        try:
            r = requests.post(
                _TG_API.format(token=token),
                data={"chat_id": cid, "text": text,
                      "disable_web_page_preview": "true"},
                timeout=_TG_TIMEOUT,
            )
            if r.status_code == 200:
                sent_any = True
            else:
                logger.warning(f"telegram send to {cid} failed {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"telegram send to {cid} error: {e}")
    return sent_any


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
