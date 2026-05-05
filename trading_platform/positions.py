"""Position tracker — paper.

Reads positions.json (open) and computes from fills.jsonl + outcome
window. Each open position carries entry_price, atr (for stop calc),
horizon_eta, fire metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
POSITIONS_FILE = PROJECT / "outputs/trading_platform/positions.json"


def _load() -> list[dict]:
    if not POSITIONS_FILE.exists():
        return []
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except Exception:
        return []


def _save(positions: list[dict]) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


def open_positions() -> list[dict]:
    return _load()


def add(position: dict) -> None:
    p = _load()
    p.append(position)
    _save(p)


def remove(position_id: str) -> dict | None:
    p = _load()
    out = None
    keep: list[dict] = []
    for x in p:
        if x.get("position_id") == position_id:
            out = x
        else:
            keep.append(x)
    _save(keep)
    return out


def find_by_signal_id(signal_id: str) -> dict | None:
    for p in _load():
        if p.get("signal_id") == signal_id:
            return p
    return None
