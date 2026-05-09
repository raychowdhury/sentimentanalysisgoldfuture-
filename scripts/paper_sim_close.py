"""Force-close the open paper position.

Writes a `close` record to paper_sim_orders.jsonl with
exit_reason="manual", clears paper_sim_book.json, and updates
paper_sim_state.json counters/watermarks. Mutates ONLY paper_sim_*
files. Does NOT touch config, broker, rule code, or live tape.

The exit price defaults to the position's `current_px` from book.json.
Operator MAY override with --exit-px for an explicit close price.

Operator must supply --reason and --confirm to avoid accidental flushes.

Stdlib only. Atomic write for state.json + book.json. Append for orders.jsonl.
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


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reason", required=True,
                    help="Operator-provided rationale for the manual close.")
    ap.add_argument("--confirm", action="store_true", required=False,
                    help="Required acknowledgement; pass --confirm to proceed.")
    ap.add_argument("--exit-px", type=float, default=None,
                    help="Override exit price; default = current_px in book.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="Directory containing paper_sim_*.json/.jsonl.")
    args = ap.parse_args()

    if not args.confirm:
        print(json.dumps({
            "ok": False,
            "error": "missing_confirm",
            "hint": "re-run with --confirm to proceed.",
        }, indent=2))
        return 2

    out_dir = Path(args.out_dir)
    state_path = out_dir / "paper_sim_state.json"
    book_path = out_dir / "paper_sim_book.json"
    orders_path = out_dir / "paper_sim_orders.jsonl"

    if not (state_path.exists() and book_path.exists()):
        print(json.dumps({
            "ok": False,
            "error": "state_or_book_missing",
            "state_path": str(state_path),
            "book_path": str(book_path),
        }, indent=2))
        return 1

    with state_path.open() as f:
        state = json.load(f)
    with book_path.open() as f:
        book = json.load(f)

    positions = book.get("positions", []) or []
    if not positions:
        print(json.dumps({
            "ok": True,
            "action": "noop_no_open_position",
            "reason": args.reason,
        }, indent=2))
        return 0

    p = positions[0]
    entry_px = float(p["entry_px"])
    atr = float(p["atr_at_entry"])
    direction = int(p["direction"])
    exit_px = float(args.exit_px) if args.exit_px is not None else \
        float(p.get("current_px", entry_px))
    realized_R = (exit_px - entry_px) * direction / atr

    now = _now_iso()
    seq = int(state.get("sequence", 0)) + 1

    _append_jsonl(orders_path, {
        "schema_version": SCHEMA_VERSION,
        "type": "close",
        "sequence": seq,
        "trade_id": p["trade_id"],
        "rule": p["rule"],
        "direction": direction,
        "exit_ts": now,
        "exit_bar_ts": now,
        "exit_px": round(exit_px, 4),
        "exit_reason": "manual",
        "bars_held": int(p.get("bars_held", 0)),
        "realized_R": round(float(realized_R), 4),
        "mfe_R_seen": round(float(p.get("mfe_R_seen", 0.0)), 4),
        "mae_R_seen": round(float(p.get("mae_R_seen", 0.0)), 4),
        "tie_break_applied": False,
        "engine_version": ENGINE_VERSION,
        "manual_reason": args.reason,
        "written_ts": now,
    })

    # Update state counters + watermarks (mirroring engine.on_bar close path).
    counters = state.setdefault("counters", {})
    watermarks = state.setdefault("watermarks", {})
    counters["trades_closed_total"] = int(counters.get("trades_closed_total", 0)) + 1
    if realized_R < 0:
        counters["consecutive_losses"] = int(counters.get("consecutive_losses", 0)) + 1
    else:
        counters["consecutive_losses"] = 0

    eq_running = float(watermarks.get("equity_R_running", 0.0)) + float(realized_R)
    eq_peak = max(float(watermarks.get("equity_peak_R", 0.0)), eq_running)
    drawdown = eq_peak - eq_running
    watermarks["equity_R_running"] = eq_running
    watermarks["equity_peak_R"] = eq_peak
    watermarks["max_realized_R"] = max(float(watermarks.get("max_realized_R", 0.0)),
                                       eq_running)
    watermarks["max_drawdown_R"] = max(float(watermarks.get("max_drawdown_R", 0.0)),
                                       float(drawdown))
    state["sequence"] = seq
    state["last_updated_ts"] = now

    book["positions"] = []
    book["as_of_ts"] = now

    _atomic_write_json(book_path, book)
    _atomic_write_json(state_path, state)

    print(json.dumps({
        "ok": True,
        "action": "manual_close",
        "trade_id": p["trade_id"],
        "exit_px": round(exit_px, 4),
        "realized_R": round(float(realized_R), 4),
        "reason": args.reason,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
