"""Enable the R2 paper-simulation engine via state-file flag flip.

Mutates ONLY paper_sim_state.json. Does NOT touch config.py, R2 rule code,
broker code, or any rule/threshold. Operator must supply --reason.

Optional --clear-pause clears any existing auto-pause flag in the same
write so the engine actually runs on the next iteration.

Stdlib only. Atomic write (tmp + os.replace).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT / "outputs/order_flow"
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _seed_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": "0.1.0",
        "enabled": False,
        "enabled_reason": "seeded",
        "enabled_changed_ts": _now_iso(),
        "last_bar_processed_ts": None,
        "sequence": 0,
        "auto_pause": {
            "active": False, "reason": None, "tripped_ts": None,
            "tripped_metric": None, "tripped_value": None,
        },
        "counters": {
            "trades_opened_total": 0, "trades_closed_total": 0,
            "consecutive_losses": 0, "trades_today": 0, "today_date": None,
            "fires_skipped_total": 0, "fires_skipped_book_full": 0,
            "fires_skipped_daily_cap": 0, "fires_skipped_disabled": 0,
            "fires_skipped_paused": 0,
        },
        "watermarks": {
            "max_realized_R": 0.0, "max_drawdown_R": 0.0,
            "equity_R_running": 0.0, "equity_peak_R": 0.0,
        },
        "last_updated_ts": _now_iso(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reason", required=True,
                    help="Operator-provided rationale for enabling.")
    ap.add_argument("--clear-pause", action="store_true",
                    help="Also clear any existing auto-pause flag.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="Directory containing paper_sim_state.json.")
    args = ap.parse_args()

    state_path = Path(args.out_dir) / "paper_sim_state.json"
    state = _seed_state()
    if state_path.exists():
        with state_path.open() as f:
            state = json.load(f)

    state["enabled"] = True
    state["enabled_reason"] = args.reason
    state["enabled_changed_ts"] = _now_iso()
    state["last_updated_ts"] = _now_iso()

    if args.clear_pause:
        state["auto_pause"] = {
            "active": False, "reason": None, "tripped_ts": None,
            "tripped_metric": None, "tripped_value": None,
        }

    _atomic_write_json(state_path, state)
    print(json.dumps({
        "ok": True,
        "action": "enabled",
        "reason": args.reason,
        "cleared_auto_pause": bool(args.clear_pause),
        "state_path": str(state_path),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
