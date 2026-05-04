"""
Telegram subscriber registry — sqlite-backed list of chat IDs that receive
alert fan-outs.

Coexists with the env-var TG_CHAT_ID: that one is always notified (seed
admin), plus every chat that has sent /subscribe to the bot.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from order_flow_engine.src import config as of_cfg

_DB_NAME = "tg_subscribers.sqlite"


def _db_path(output_dir: Path | None = None) -> Path:
    base = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / _DB_NAME


@contextmanager
def _conn(output_dir: Path | None = None):
    c = sqlite3.connect(str(_db_path(output_dir)))
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db(output_dir: Path | None = None) -> None:
    with _conn(output_dir) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                added_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                active     INTEGER DEFAULT 1
            )
        """)


def subscribe(chat_id: int, username: str = "", first_name: str = "",
              output_dir: Path | None = None) -> bool:
    """Add or reactivate a chat. Returns True if newly added."""
    init_db(output_dir)
    with _conn(output_dir) as c:
        existing = c.execute(
            "SELECT chat_id, active FROM subscribers WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if existing and existing["active"]:
            return False
        c.execute("""
            INSERT INTO subscribers (chat_id, username, first_name, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                active=1
        """, (chat_id, username or "", first_name or ""))
    return True


def unsubscribe(chat_id: int, output_dir: Path | None = None) -> bool:
    init_db(output_dir)
    with _conn(output_dir) as c:
        c.execute(
            "UPDATE subscribers SET active=0 WHERE chat_id=?",
            (chat_id,),
        )
        return c.total_changes > 0


def all_active(output_dir: Path | None = None) -> list[int]:
    """All active chat IDs PLUS the env-var TG_CHAT_ID (deduped)."""
    init_db(output_dir)
    with _conn(output_dir) as c:
        rows = c.execute(
            "SELECT chat_id FROM subscribers WHERE active=1"
        ).fetchall()
    ids = {int(r["chat_id"]) for r in rows}
    seed = os.getenv("TG_CHAT_ID", "").strip()
    if seed:
        try:
            ids.add(int(seed))
        except ValueError:
            pass
    return sorted(ids)


def stats(output_dir: Path | None = None) -> dict:
    init_db(output_dir)
    with _conn(output_dir) as c:
        rows = c.execute(
            "SELECT chat_id, username, first_name, active, added_at "
            "FROM subscribers ORDER BY added_at DESC"
        ).fetchall()
    return {
        "active": sum(1 for r in rows if r["active"]),
        "total":  len(rows),
        "subscribers": [dict(r) for r in rows],
    }
