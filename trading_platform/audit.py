"""Append-only audit log for the trading platform.

Every signal accept/reject, order create/fill/cancel, position open/close,
risk-breach, kill-switch toggle, and ARM/DISARM event lands here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
AUDIT_LOG = PROJECT / "outputs/trading_platform/audit.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event_type: str, payload: dict) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now(), "event_type": event_type, **payload}
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def tail(n: int = 100) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    out: list[dict] = []
    with AUDIT_LOG.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-n:]
