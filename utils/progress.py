"""
Thread-safe per-run progress counters.

Read by the dashboard's /api/status endpoint (via scheduler.get_status) to
render a "Running… N/M" label on the Run Now button while a pipeline is
in flight. Written by main.run_sentiment as articles finish.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_state: dict = {
    "current": 0,
    "total":   0,
    "stage":   "",   # free-form label, e.g. "articles", "market", "signal"
}


def reset(total: int = 0, stage: str = "articles") -> None:
    with _lock:
        _state["current"] = 0
        _state["total"]   = int(total)
        _state["stage"]   = stage


def tick(n: int = 1) -> None:
    with _lock:
        _state["current"] += n


def set_stage(stage: str) -> None:
    with _lock:
        _state["stage"] = stage


def snapshot() -> dict:
    with _lock:
        return dict(_state)
