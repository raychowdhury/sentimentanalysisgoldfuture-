"""
Telegram bot command poller.

Long-polls Telegram getUpdates, handles /subscribe, /unsubscribe, /status,
/help. Registers chat IDs in tg_subscribers so the notifier fans alerts
to everyone who opted in.

Runs in a background daemon thread started by app boot. No webhook needed
— pure polling, works behind NAT.
"""

from __future__ import annotations

import os
import threading
import time

import requests

from order_flow_engine.src import alert_store, config as of_cfg, notifier, tg_subscribers
from utils.logger import setup_logger

logger = setup_logger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 25      # seconds long-poll
_HTTP_TIMEOUT = _POLL_TIMEOUT + 5

_thread: threading.Thread | None = None
_stop = threading.Event()
_state = {"running": False, "last_update_id": 0, "last_msg_at": None}


def _send(token: str, chat_id: int, text: str) -> None:
    try:
        requests.post(
            _API_BASE.format(token=token) + "/sendMessage",
            data={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": "true"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"send reply failed: {e}")


def _send_recent_alerts(token: str, chat_id: int, n: int = 5) -> int:
    """
    Push the last N alerts to the chat — newest first, gated by the same
    confidence threshold the live engine uses (OF_ALERT_MIN_CONF). Returns
    count sent.
    """
    try:
        # Pull a wider window then filter — sqlite has no compound sort
        # by (conf >= thr, ts desc), so we filter in Python.
        rows = alert_store.query(limit=max(n * 5, 50),
                                 min_confidence=of_cfg.OF_ALERT_MIN_CONF)
    except Exception as e:
        logger.warning(f"recent fetch failed: {e}")
        return 0
    if not rows:
        _send(token, chat_id,
              f"No alerts ≥ conf {of_cfg.OF_ALERT_MIN_CONF} in history yet.")
        return 0
    recent = sorted(rows, key=lambda a: a["timestamp_utc"], reverse=True)[:n]
    _send(token, chat_id,
          f"📜 Last {len(recent)} alerts (newest first, conf ≥ {of_cfg.OF_ALERT_MIN_CONF}):")
    for a in recent:  # newest first — most actionable on top
        try:
            _send(token, chat_id, notifier._format(a))
            time.sleep(0.3)  # avoid Telegram rate limit
        except Exception as e:
            logger.warning(f"backfill send failed: {e}")
    return len(recent)


def _handle(token: str, msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    username = chat.get("username", "") or ""
    first    = chat.get("first_name", "") or ""

    if text in ("/start", "/subscribe"):
        is_new = tg_subscribers.subscribe(chat_id, username, first)
        if is_new:
            _send(token, chat_id,
                  "✅ Subscribed to Order Flow alerts.\n\n"
                  "You'll get a ping every time the engine fires a signal.\n"
                  "Commands:\n"
                  "  /recent — show last 5 alerts\n"
                  "  /recent 20 — show last N alerts (max 20)\n"
                  "  /unsubscribe — stop receiving\n"
                  "  /status — your subscription state")
            # Catch-up burst on first subscribe
            _send_recent_alerts(token, chat_id, n=5)
        else:
            _send(token, chat_id, "Already subscribed. Use /recent for catch-up or /unsubscribe to stop.")
    elif text == "/unsubscribe":
        tg_subscribers.unsubscribe(chat_id)
        _send(token, chat_id, "🔕 Unsubscribed. Re-subscribe with /subscribe.")
    elif text == "/status":
        active = chat_id in tg_subscribers.all_active()
        _send(token, chat_id,
              f"Subscribed: {'yes ✅' if active else 'no ❌'}\nChat ID: {chat_id}")
    elif text.startswith("/recent"):
        # /recent or /recent N
        parts = text.split()
        n = 5
        if len(parts) > 1:
            try:
                n = max(1, min(20, int(parts[1])))
            except ValueError:
                pass
        _send_recent_alerts(token, chat_id, n=n)
    elif text in ("/help", "/?"):
        _send(token, chat_id,
              "Order Flow alerts bot.\n\n"
              "/subscribe — start receiving\n"
              "/recent [N] — show last N alerts (default 5, max 20)\n"
              "/unsubscribe — stop\n"
              "/status — show your state")
    elif text.startswith("/"):
        _send(token, chat_id, "Unknown command. Try /help.")


def _poll_loop(token: str) -> None:
    logger.info("Telegram bot poller started")
    _state["running"] = True
    base = _API_BASE.format(token=token)
    try:
        while not _stop.is_set():
            try:
                r = requests.get(
                    base + "/getUpdates",
                    params={
                        "offset":  _state["last_update_id"] + 1,
                        "timeout": _POLL_TIMEOUT,
                        "allowed_updates": '["message"]',
                    },
                    timeout=_HTTP_TIMEOUT,
                )
                data = r.json()
                if not data.get("ok"):
                    logger.warning(f"getUpdates returned: {data}")
                    time.sleep(3)
                    continue
                for upd in data.get("result", []):
                    _state["last_update_id"] = upd["update_id"]
                    msg = upd.get("message")
                    if msg:
                        _state["last_msg_at"] = msg.get("date")
                        _handle(token, msg)
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                logger.warning(f"poll iteration error: {e}")
                time.sleep(3)
    finally:
        _state["running"] = False
        logger.info("Telegram bot poller stopped")


def start_thread() -> bool:
    """Start the polling thread if TG_BOT_TOKEN is set. Idempotent."""
    global _thread
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    if not token:
        logger.info("TG_BOT_TOKEN not set; bot poller skipped")
        return False
    if _thread and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, args=(token,), daemon=True)
    _thread.start()
    return True


def stop_thread() -> None:
    _stop.set()


def status() -> dict:
    s = tg_subscribers.stats()
    return {
        "poller_running": bool(_thread and _thread.is_alive()),
        "last_update_id": _state["last_update_id"],
        "active_subscribers": s["active"],
        "total_subscribers":  s["total"],
    }
