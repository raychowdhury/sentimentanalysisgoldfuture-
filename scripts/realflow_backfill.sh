#!/usr/bin/env bash
# Scheduled wrapper for realflow_history_backfill.
#
# Behavior:
#   - POSIX mkdir atomic lock at /tmp/rfm-realflow-backfill.lockdir
#   - skip if realflow_history.parquet mtime < 4h old (override with --force)
#   - source .env to get DATABENTO_API_KEY
#   - run module, append output to log
#   - track consecutive failures; create FAILED flag after 3 in a row
#   - auto-clear FAILED flag on success
#
# Manual run:    bash scripts/realflow_backfill.sh
# Force run:     bash scripts/realflow_backfill.sh --force
# Stop schedule: launchctl unload ~/Library/LaunchAgents/com.rfm.realflow-backfill.plist
# Ack failure:   rm outputs/order_flow/realflow_backfill_FAILED.flag
#
# Standing instruction: no rule/threshold/model/ml_engine/trading change.

set -euo pipefail

PROJECT="/Users/ray/Dev/Sentiment analysis projtect"
SYMBOL="ESM6"
TF="15m"
LOOKBACK_DAYS=18
SKIP_THRESHOLD_MIN=240   # 4h
FAIL_THRESHOLD=3

LOG="$PROJECT/outputs/order_flow/realflow_backfill.log"
LOCK="/tmp/rfm-realflow-backfill.lockdir"
FAIL_FLAG="$PROJECT/outputs/order_flow/realflow_backfill_FAILED.flag"
COUNT_FILE="/tmp/rfm-realflow-backfill.failcount"
PARQUET="$PROJECT/order_flow_engine/data/processed/${SYMBOL}_${TF}_realflow_history.parquet"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

mkdir -p "$(dirname "$LOG")"

# --- lock (no-overlap) ---
if ! mkdir "$LOCK" 2>/dev/null; then
  log "SKIP: lock held by another process at $LOCK"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# --- skip-check (unless --force) ---
FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

if [[ "$FORCE" -eq 0 ]]; then
  if [[ -f "$PARQUET" ]]; then
    AGE_MIN=$(.venv/bin/python -c "
import os, time, sys
p = '$PARQUET'
print(int((time.time() - os.path.getmtime(p)) / 60))
" 2>/dev/null || echo 999)
    if [[ "$AGE_MIN" =~ ^[0-9]+$ ]] && [[ "$AGE_MIN" -lt "$SKIP_THRESHOLD_MIN" ]]; then
      log "SKIP: realflow_history mtime within ${SKIP_THRESHOLD_MIN}min (age=${AGE_MIN}min)"
      exit 0
    fi
  fi
fi

# --- run ---
cd "$PROJECT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  log "WARN: .env not found at $PROJECT/.env"
fi

if [[ -z "${DATABENTO_API_KEY:-}" ]]; then
  log "FAIL: DATABENTO_API_KEY not set"
  exit 4
fi

log "RUN: backfill $SYMBOL@$TF lookback=${LOOKBACK_DAYS}d force=$FORCE"
START_EPOCH=$(date +%s)

if .venv/bin/python -m order_flow_engine.src.realflow_history_backfill \
      --symbol "$SYMBOL" --tf "$TF" --lookback-days "$LOOKBACK_DAYS" \
      >> "$LOG" 2>&1; then
  END_EPOCH=$(date +%s)
  ELAPSED=$((END_EPOCH - START_EPOCH))
  log "OK: backfill complete elapsed=${ELAPSED}s"
  echo 0 > "$COUNT_FILE"
  if [[ -f "$FAIL_FLAG" ]]; then
    rm -f "$FAIL_FLAG"
    log "FAIL FLAG CLEARED"
  fi
  exit 0
else
  RC=$?
  COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  COUNT=$((COUNT + 1))
  echo "$COUNT" > "$COUNT_FILE"
  log "FAIL: backfill rc=$RC consecutive_fails=$COUNT"
  if [[ "$COUNT" -ge "$FAIL_THRESHOLD" ]]; then
    touch "$FAIL_FLAG"
    log "FAIL FLAG SET: ${FAIL_THRESHOLD} consecutive failures"
  fi
  exit "$RC"
fi
