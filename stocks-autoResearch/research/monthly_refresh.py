"""
Monthly composite refresh: backtest → fit weights → reliability diagram.

Runs the three research scripts in order, writes a timestamp file so
the dashboard can show "last refreshed X days ago". Triggered manually
via dashboard button or cron.

Usage:  python -m research.monthly_refresh
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT  = ROOT / "outputs" / "stocks" / "_monthly_refresh.json"


def _run(module: str) -> dict:
    """Run a research submodule. Return dict {module, rc, seconds}."""
    t0 = datetime.now(timezone.utc)
    proc = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True,
    )
    sec = (datetime.now(timezone.utc) - t0).total_seconds()
    return {
        "module":  module,
        "rc":      proc.returncode,
        "seconds": round(sec, 1),
        "stderr_tail": proc.stderr[-500:] if proc.returncode != 0 else "",
    }


def main() -> None:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    steps = []
    for mod in (
        "research.composite_backtest",
        "research.fit_composite_weights",
        "research.reliability",
    ):
        result = _run(mod)
        steps.append(result)
        print(f"{mod}: rc={result['rc']}  {result['seconds']}s")
        if result["rc"] != 0:
            print(f"  stderr tail:\n{result['stderr_tail']}")
            break
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "started":  started,
        "finished": finished,
        "ok":       all(s["rc"] == 0 for s in steps),
        "steps":    steps,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
