"""Set or clear the paper-sim per-pass run cap.

Mutates ONLY paper_sim_state.json. Does NOT touch config.py, R2 rule
code, broker code, or any rule/threshold. Operator must supply --reason.

Cap semantics: each call to incremental_pass anchors a baseline at start;
on_fire returns skip_run_cap once (trades_opened_total - baseline) >= cap.
Cap persists across iterations until cleared via --clear.

Cap is independent of max_trades_per_day (daily cap); whichever is
tighter wins.

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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--cap", type=int,
                     help="Set per-pass open cap to this value (>=1).")
    grp.add_argument("--clear", action="store_true",
                     help="Clear the cap (set to null = no cap).")
    ap.add_argument("--reason", required=True,
                    help="Operator-provided rationale for the cap change.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="Directory containing paper_sim_state.json.")
    args = ap.parse_args()

    if args.cap is not None and args.cap < 1:
        print(json.dumps({
            "ok": False, "error": "invalid_cap",
            "hint": "cap must be >= 1 or use --clear.",
        }, indent=2))
        return 2

    state_path = Path(args.out_dir) / "paper_sim_state.json"
    if not state_path.exists():
        print(json.dumps({
            "ok": False,
            "error": "state_file_missing",
            "state_path": str(state_path),
            "hint": "seed first via paper_sim_seed_cursor.py "
                    "or paper_sim_enable.py.",
        }, indent=2))
        return 1

    with state_path.open() as f:
        state = json.load(f)

    prior_cap = state.get("run_cap")
    new_cap = None if args.clear else int(args.cap)
    state["run_cap"] = new_cap
    state["last_updated_ts"] = _now_iso()
    # Append a small audit field; engine ignores unknown keys.
    state.setdefault("run_cap_audit", []).append({
        "ts": _now_iso(),
        "prior": prior_cap,
        "new": new_cap,
        "reason": args.reason,
    })

    _atomic_write_json(state_path, state)
    print(json.dumps({
        "ok": True,
        "action": "cleared" if args.clear else "set",
        "prior_cap": prior_cap,
        "new_cap": new_cap,
        "reason": args.reason,
        "state_path": str(state_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
