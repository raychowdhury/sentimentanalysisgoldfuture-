"""
Read-only monitor loop. Every N seconds:

  1. realflow_compare.diagnose(symbol, tf)        → refreshes diagnostic JSON
  2. realflow_outcome_tracker.settle_pass(symbol, tf) → settles new outcomes

Logs one JSON line per iteration to stdout AND (optionally) an append-only
file. Failures in either step are caught and logged; loop continues.

Hard invariants:
  * No detector edits.
  * No trades.
  * No threshold / rule / model / ml_engine changes.
  * No Flask coupling — runs in its own process.
  * Append-only on disk.
  * Tracker remains idempotent across iterations (signal_id dedupe).

Run inside tmux:

    python -m order_flow_engine.src.monitor_loop \\
        --symbol ESM6 --tf 15m --interval 900 \\
        --log outputs/order_flow/monitor_loop.log

Ctrl-C exits within ≤1s (sleep is sliced).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from order_flow_engine.src import realflow_compare as rfc
from order_flow_engine.src import realflow_outcome_tracker as rot
from order_flow_engine.src import realflow_r7_shadow as r7s


_stop = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _stop
        _stop = True
        # second signal escalates
        signal.signal(signum, signal.SIG_DFL)
    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step(symbol: str, tf: str) -> dict:
    """One iteration: diagnose then settle. Either may fail; both attempted."""
    t0 = time.time()
    rec: dict = {
        "ts":     _now_iso(),
        "symbol": symbol,
        "tf":     tf,
    }

    try:
        d = rfc.diagnose(symbol, tf)
        rec["diagnose"] = {
            "ok":          True,
            "joined_n":    (d.get("joined") or {}).get("n_bars"),
            "load_error":  d.get("load_error"),
        }
    except Exception as e:
        rec["diagnose"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        s = rot.settle_pass(symbol, tf)
        rec["settle_pass"] = {
            "ok":              True,
            "n_new_settled":   s.get("n_new_settled"),
            "n_pending":       s.get("n_pending"),
            "n_total_settled": s.get("n_total_settled"),
        }
    except Exception as e:
        rec["settle_pass"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        sh = r7s.shadow_pass(symbol, tf)
        rec["r7_shadow"] = {
            "ok":              True,
            "n_new_settled":   sh.get("n_new_settled"),
            "n_pending":       sh.get("n_pending"),
            "n_total_settled": sh.get("n_total_settled"),
        }
    except Exception as e:
        rec["r7_shadow"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    rec["elapsed_s"] = round(time.time() - t0, 2)
    return rec


def _emit(rec: dict, log_path: Path | None) -> None:
    line = json.dumps(rec, default=str)
    print(line, flush=True)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(line + "\n")


def _sleep_with_check(seconds: int) -> None:
    """1-second slices so Ctrl-C feels snappy."""
    for _ in range(int(seconds)):
        if _stop:
            return
        time.sleep(1)


def run(symbol: str, tf: str, interval_s: int,
        log_path: Path | None, max_iterations: int | None) -> dict:
    _install_signal_handlers()
    iters = 0
    consecutive_failures = 0
    while not _stop:
        rec = _step(symbol, tf)
        # consecutive-failure flag (informational; no auto-action)
        if (not rec.get("diagnose", {}).get("ok")
                and not rec.get("settle_pass", {}).get("ok")):
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if consecutive_failures == 5:
            rec["five_consecutive_failures"] = True
        _emit(rec, log_path)

        iters += 1
        if max_iterations is not None and iters >= max_iterations:
            break
        _sleep_with_check(interval_s)

    final = {"ts": _now_iso(), "stopped": True, "iterations_run": iters}
    print(json.dumps(final), flush=True)
    if log_path is not None:
        with log_path.open("a") as f:
            f.write(json.dumps(final) + "\n")
    return final


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(description="Read-only RFM monitor loop.")
    ap.add_argument("--symbol",   default="ESM6")
    ap.add_argument("--tf",       default="15m")
    ap.add_argument("--interval", type=int, default=900,
                    help="seconds between iteration starts (default 900 = 15m)")
    ap.add_argument("--log",      default="outputs/order_flow/monitor_loop.log",
                    help="append-only log path (set empty to disable)")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="exit after N iterations (default: run forever)")
    args = ap.parse_args()
    log_path = Path(args.log) if args.log else None
    try:
        run(args.symbol, args.tf, args.interval, log_path, args.max_iterations)
    except KeyboardInterrupt:
        # Belt-and-suspenders — signal handler should already have set _stop.
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
