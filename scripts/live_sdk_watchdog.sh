#!/bin/bash
# Live SDK watchdog wrapper — Stage 1 read-only.
# Evaluates restart criteria; logs decision; never restarts anything.

set -uo pipefail

PROJECT="/Users/ray/Dev/Sentiment analysis projtect"
PY="$PROJECT/scripts/live_sdk_watchdog.py"
LOG="$PROJECT/outputs/order_flow/live_sdk_watchdog.log"

cd "$PROJECT" || exit 1
source .venv/bin/activate 2>/dev/null

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
err() { echo "[$(ts)] WRAPPER_ERROR: $*" >> "$LOG"; }

if [ ! -f "$PY" ]; then
  err "watchdog python module missing at $PY"
  exit 1
fi

python "$PY" >/dev/null 2>>"$LOG" || {
  err "python exit non-zero"
  exit 0
}

exit 0
