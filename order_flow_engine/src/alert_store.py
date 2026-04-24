"""
Sqlite alert store — queryable history without parsing JSONL.

Schema:
  alerts(id PK, ts_utc, symbol, tf, label, confidence, price, atr,
         rules, reason_codes, metrics_json, model_json, proxy_mode,
         created_at)

The sqlite file lives at outputs/order_flow/alerts.sqlite. Writers (alert_engine)
upsert by id; readers (dashboard, queries) hit the same file. JSONL stream
remains for SSE consumers — sqlite is the source of truth for history.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from order_flow_engine.src import config as of_cfg


def _db_path(output_dir: Path | None = None) -> Path:
    base = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / of_cfg.ALERTS_DB_NAME


@contextmanager
def _conn(output_dir: Path | None = None):
    conn = sqlite3.connect(str(_db_path(output_dir)))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(output_dir: Path | None = None) -> None:
    with _conn(output_dir) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id            TEXT PRIMARY KEY,
                ts_utc        TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                tf            TEXT NOT NULL,
                label         TEXT NOT NULL,
                confidence    INTEGER NOT NULL,
                price         REAL,
                atr           REAL,
                rules         TEXT,
                reason_codes  TEXT,
                metrics_json  TEXT,
                model_json    TEXT,
                proxy_mode    INTEGER,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_alerts_ts    ON alerts(ts_utc)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_alerts_label ON alerts(label)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_alerts_sym   ON alerts(symbol, tf)")


def upsert(alert: dict, output_dir: Path | None = None) -> None:
    init_db(output_dir)
    with _conn(output_dir) as c:
        c.execute("""
            INSERT INTO alerts (id, ts_utc, symbol, tf, label, confidence, price, atr,
                                rules, reason_codes, metrics_json, model_json, proxy_mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                confidence=excluded.confidence,
                rules=excluded.rules,
                reason_codes=excluded.reason_codes,
                metrics_json=excluded.metrics_json,
                model_json=excluded.model_json
        """, (
            alert["id"],
            alert["timestamp_utc"],
            alert["symbol"],
            alert["timeframe"],
            alert["label"],
            int(alert["confidence"]),
            alert.get("price"),
            alert.get("atr"),
            ",".join(alert.get("rules_fired", [])),
            json.dumps(alert.get("reason_codes", [])),
            json.dumps(alert.get("metrics", {}), default=str),
            json.dumps(alert.get("model", {}), default=str),
            int(bool(alert.get("data_quality", {}).get("proxy_mode", True))),
        ))


def query(
    output_dir: Path | None = None,
    symbol: str | None = None,
    label: str | None = None,
    min_confidence: int | None = None,
    limit: int = 500,
) -> list[dict]:
    init_db(output_dir)
    sql = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND symbol = ?"; params.append(symbol)
    if label:
        sql += " AND label = ?";  params.append(label)
    if min_confidence is not None:
        sql += " AND confidence >= ?"; params.append(int(min_confidence))
    sql += " ORDER BY ts_utc DESC LIMIT ?"
    params.append(int(limit))

    with _conn(output_dir) as c:
        rows = c.execute(sql, params).fetchall()

    out = []
    for r in rows:
        out.append({
            "id":            r["id"],
            "timestamp_utc": r["ts_utc"],
            "symbol":        r["symbol"],
            "timeframe":     r["tf"],
            "label":         r["label"],
            "confidence":    r["confidence"],
            "price":         r["price"],
            "atr":           r["atr"],
            "rules_fired":   r["rules"].split(",") if r["rules"] else [],
            "reason_codes":  json.loads(r["reason_codes"] or "[]"),
            "metrics":       json.loads(r["metrics_json"] or "{}"),
            "model":         json.loads(r["model_json"] or "{}"),
            "data_quality":  {"proxy_mode": bool(r["proxy_mode"])},
        })
    # Return ascending so existing JSON-array consumers don't have to flip.
    return list(reversed(out))


def latest(output_dir: Path | None = None) -> dict | None:
    init_db(output_dir)
    with _conn(output_dir) as c:
        row = c.execute(
            "SELECT * FROM alerts ORDER BY ts_utc DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return query(output_dir=output_dir, limit=1)[-1]


def count(output_dir: Path | None = None) -> int:
    init_db(output_dir)
    with _conn(output_dir) as c:
        return int(c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


def label_distribution(output_dir: Path | None = None) -> dict[str, int]:
    init_db(output_dir)
    with _conn(output_dir) as c:
        rows = c.execute(
            "SELECT label, COUNT(*) AS n FROM alerts GROUP BY label"
        ).fetchall()
    return {r["label"]: int(r["n"]) for r in rows}


def last_alert_for(symbol: str, tf: str, label: str,
                   output_dir: Path | None = None) -> dict | None:
    init_db(output_dir)
    with _conn(output_dir) as c:
        row = c.execute(
            """SELECT * FROM alerts WHERE symbol=? AND tf=? AND label=?
               ORDER BY ts_utc DESC LIMIT 1""",
            (symbol, tf, label),
        ).fetchone()
    if not row:
        return None
    return {
        "ts_utc":     row["ts_utc"],
        "label":      row["label"],
        "confidence": row["confidence"],
    }
