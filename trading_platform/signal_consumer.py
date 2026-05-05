"""Signal consumer — read pending fires JSON; route ARMED rules to OMS.

Two read modes:
  - pending mode: poll outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json
  - settled mode: tail outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl
                  (used for backfill — settled outcomes inform paper close)

Dedup by signal_id. Persists processed IDs in
outputs/trading_platform/processed_signals.json.

Does NOT change rule_engine.py / outcome_tracker.py / pending JSON / settled
JSONL. Read-only on those files. Append-only on platform JSONLs.
"""

from __future__ import annotations

import json
from pathlib import Path

from trading_platform import audit, oms, strategy_registry

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
PENDING_FILE = PROJECT / "outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json"
SETTLED_FILE = PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl"
PROCESSED_FILE = PROJECT / "outputs/trading_platform/processed_signals.json"


def _load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_FILE.read_text()))
    except Exception:
        return set()


def _save_processed(ids: set[str]) -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps(sorted(ids), indent=2))


def _read_pending() -> list[dict]:
    if not PENDING_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_FILE.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _read_settled_tail(n: int = 200) -> list[dict]:
    if not SETTLED_FILE.exists():
        return []
    out: list[dict] = []
    with SETTLED_FILE.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-n:]


def consume_pending() -> dict:
    processed = _load_processed()
    fires = _read_pending()
    armed_count = 0
    skipped_disarmed = 0
    skipped_dupe = 0
    routed: list[dict] = []
    for f in fires:
        sid = f.get("signal_id")
        if not sid or sid in processed:
            skipped_dupe += 1
            continue
        rule = f.get("rule", "")
        if not strategy_registry.is_armed(rule):
            skipped_disarmed += 1
            audit.log("signal_skipped_disarmed",
                      {"signal_id": sid, "rule": rule})
            processed.add(sid)
            continue
        rec = oms.place_paper_order(f)
        routed.append(rec)
        armed_count += 1
        processed.add(sid)
    _save_processed(processed)
    return {
        "fires_seen": len(fires),
        "armed_routed": armed_count,
        "skipped_disarmed": skipped_disarmed,
        "skipped_dupe": skipped_dupe,
        "routed": routed,
    }


def consume_settled_tail(n: int = 200) -> dict:
    """Backfill: route any settled fires whose rule is currently ARMED.
    Useful first time a rule is armed — replays history from settled JSONL.
    Skips already-processed signal_ids."""
    processed = _load_processed()
    fires = _read_settled_tail(n=n)
    armed_count = 0
    routed: list[dict] = []
    for f in fires:
        sid = f.get("signal_id")
        if not sid or sid in processed:
            continue
        rule = f.get("rule", "")
        if not strategy_registry.is_armed(rule):
            processed.add(sid)
            continue
        rec = oms.place_paper_order(f)
        routed.append(rec)
        armed_count += 1
        processed.add(sid)
    _save_processed(processed)
    return {"fires_seen": len(fires), "armed_routed": armed_count,
            "routed": routed}
