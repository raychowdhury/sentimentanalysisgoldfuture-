#!/bin/bash
# Hourly raw OHLCV cache refresh for ESM6.
# Operational only — no detector / config / model edits.

set -uo pipefail

PROJECT="/Users/ray/Dev/Sentiment analysis projtect"
LOG="$PROJECT/outputs/order_flow/cache_refresh.log"
FAIL_FLAG="$PROJECT/outputs/order_flow/cache_refresh_FAILED.flag"
FAIL_COUNT="$PROJECT/outputs/order_flow/.cache_refresh_fail_count"
RAW_PARQUET="$PROJECT/order_flow_engine/data/raw/ESM6_15m.parquet"

cd "$PROJECT" || exit 1
source .venv/bin/activate
set -a; source .env; set +a

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# Acquire lock — POSIX mkdir is atomic; portable across macOS without Homebrew.
LOCK_DIR="/tmp/rfm-cache-refresh.lockdir"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "SKIP: lock dir exists (prior run active or stale)"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

# mtime skip-check via python: parse latest bar in parquet
SKIP=$(python -c "
import pandas as pd, sys
from pathlib import Path
p = Path('$RAW_PARQUET')
if not p.exists():
    print('NO'); sys.exit()
df = pd.read_parquet(p)
df.index = pd.to_datetime(df.index)
if df.index.tz is None: df.index = df.index.tz_localize('UTC')
last = df.index.max()
now  = pd.Timestamp.now(tz='UTC')
age_min = (now - last).total_seconds() / 60
print('YES' if age_min < 30 else 'NO')
" 2>/dev/null)

if [ "$SKIP" = "YES" ]; then
  log "SKIP: raw last_bar within 30 min"
  exit 0
fi

# Run refresh
if python -m order_flow_engine.src.data_loader --symbol ESM6 --no-cache >> "$LOG" 2>&1; then
  RAW_MAX=$(python -c "
import pandas as pd
df = pd.read_parquet('$RAW_PARQUET')
df.index = pd.to_datetime(df.index)
print(df.index.max())
" 2>/dev/null)
  log "OK: data_loader raw_max=$RAW_MAX"
  echo 0 > "$FAIL_COUNT"
  rm -f "$FAIL_FLAG"
else
  log "FAIL: data_loader exit non-zero"
  COUNT=$(cat "$FAIL_COUNT" 2>/dev/null || echo 0)
  COUNT=$((COUNT + 1))
  echo "$COUNT" > "$FAIL_COUNT"
  if [ "$COUNT" -ge 3 ]; then
    echo "$(ts) — 3+ consecutive failures" > "$FAIL_FLAG"
    log "FAIL_FLAG written ($COUNT consecutive)"
  fi
fi
