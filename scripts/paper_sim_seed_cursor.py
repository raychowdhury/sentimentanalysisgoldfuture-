"""Seed paper_sim_state.json cursor before first live trial.

One-shot bootstrap. Writes paper_sim_state.json with:
  - enabled = false               (operator must still run paper_sim_enable.py)
  - last_bar_processed_ts = <cursor>
  - all counters zero, watermarks zero, no auto-pause

Refuses to run if paper_sim_state.json already exists with a non-null
cursor — prevents accidentally overwriting in-flight trial state.

Mutates ONLY paper_sim_state.json. Does NOT touch config.py, R2 rule
code, broker code, or any rule/threshold. Operator must supply --cursor
(ISO8601 UTC) and --reason.

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
ENGINE_VERSION = "0.1.0"


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


def _validate_cursor(s: str) -> str:
    """Parse ISO8601 UTC timestamp; reject naive or non-UTC."""
    try:
        ts = datetime.fromisoformat(s)
    except ValueError as e:
        raise SystemExit(
            "invalid --cursor (not ISO8601): {s} ({e})".format(s=s, e=e))
    if ts.tzinfo is None:
        raise SystemExit(
            "invalid --cursor (must include timezone offset): {s}".format(s=s))
    return ts.astimezone(timezone.utc).isoformat()


def _build_seed(cursor_iso: str, reason: str) -> dict:
    now = _now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "enabled": False,
        "enabled_reason": "seeded — operator must run paper_sim_enable.py",
        "enabled_changed_ts": now,
        "last_bar_processed_ts": cursor_iso,
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
        "seed_metadata": {
            "seeded_ts": now,
            "seed_reason": reason,
            "seed_cursor": cursor_iso,
        },
        "last_updated_ts": now,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cursor", required=True,
                    help="ISO8601 UTC timestamp to seed as last_bar_processed_ts.")
    ap.add_argument("--reason", required=True,
                    help="Operator-provided rationale for seeding.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="Directory for paper_sim_state.json.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing state.json even if cursor non-null. "
                         "DANGEROUS — only use if you know the prior trial is "
                         "fully reconciled and disabled.")
    args = ap.parse_args()

    cursor_iso = _validate_cursor(args.cursor)
    state_path = Path(args.out_dir) / "paper_sim_state.json"

    if state_path.exists() and not args.force:
        try:
            existing = json.loads(state_path.read_text())
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error": "existing_state_unreadable",
                "detail": str(e),
                "state_path": str(state_path),
                "hint": "inspect manually before re-running with --force.",
            }, indent=2))
            return 1
        if existing.get("last_bar_processed_ts") is not None:
            print(json.dumps({
                "ok": False,
                "error": "cursor_already_seeded",
                "existing_cursor": existing.get("last_bar_processed_ts"),
                "existing_enabled": existing.get("enabled"),
                "state_path": str(state_path),
                "hint": "use --force only after disabling and reconciling "
                        "the prior trial.",
            }, indent=2))
            return 2

    payload = _build_seed(cursor_iso, args.reason)
    _atomic_write_json(state_path, payload)
    print(json.dumps({
        "ok": True,
        "action": "seeded",
        "cursor": cursor_iso,
        "reason": args.reason,
        "enabled": False,
        "state_path": str(state_path),
        "next_step": "review state.json, then run scripts/paper_sim_enable.py",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
