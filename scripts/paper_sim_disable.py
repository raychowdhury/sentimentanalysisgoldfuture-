"""Disable the R2 paper-simulation engine via state-file flag flip.

Mutates ONLY paper_sim_state.json. Does NOT touch config.py, R2 rule code,
or broker code. Operator must supply --reason.

Disabling does NOT close any open paper position — use scripts/paper_sim_close.py
for that. Disable only stops the engine from opening new trades or
mark-to-market on subsequent bars.

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
    ap.add_argument("--reason", required=True,
                    help="Operator-provided rationale for disabling.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="Directory containing paper_sim_state.json.")
    args = ap.parse_args()

    state_path = Path(args.out_dir) / "paper_sim_state.json"
    if not state_path.exists():
        print(json.dumps({
            "ok": False,
            "error": "state_file_missing",
            "state_path": str(state_path),
            "hint": "run paper_sim_enable.py first to seed the state file, "
                    "or run incremental_pass once to seed it.",
        }, indent=2))
        return 1

    with state_path.open() as f:
        state = json.load(f)

    was_enabled = bool(state.get("enabled", False))
    state["enabled"] = False
    state["enabled_reason"] = args.reason
    state["enabled_changed_ts"] = _now_iso()
    state["last_updated_ts"] = _now_iso()

    _atomic_write_json(state_path, state)
    print(json.dumps({
        "ok": True,
        "action": "disabled",
        "was_enabled": was_enabled,
        "reason": args.reason,
        "state_path": str(state_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
