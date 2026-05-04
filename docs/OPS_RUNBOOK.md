# OPS_RUNBOOK — RFM ES Real-Flow Monitor

> Operational reference. How to bring the system up, watch it, recover from
> common failures, and know when to do nothing.

---

## Quick reference

| URL | what |
|---|---|
| `http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m` | main dashboard |
| `http://localhost:5001/api/order-flow/realflow-diagnostic-status?symbol=ESM6&tf=15m` | button status JSON |

| file | purpose |
|---|---|
| `outputs/order_flow/latest_status.md` | last diagnostic snapshot |
| `outputs/order_flow/health_monitor.log` | append-only health probe log |
| `outputs/order_flow/.health_state.json` | current per-probe state |
| `outputs/order_flow/.live_checkpoint_state.json` | per-rule per-level checkpoint state |
| `outputs/order_flow/cache_refresh.log` | hourly cache refresh log |
| `/tmp/flask.log` | Flask + Live SDK output |
| `PROJECT_STATE.md` | source of truth |

---

## Bring system up (cold start)

```bash
cd "/Users/ray/Dev/Sentiment analysis projtect"
source .venv/bin/activate
set -a && source .env && set +a

# 1. Start Flask + Live SDK auto-start
nohup python app.py > /tmp/flask.log 2>&1 &

# 2. Wait ~60s for boot + 15m parquet seed
sleep 60

# 3. Apply known workaround: rename freshly-seeded 15m parquet
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mv order_flow_engine/data/processed/ESM6_15m_live.parquet \
   order_flow_engine/data/processed/ESM6_15m_live.parquet.stale_$STAMP

# 4. Confirm Flask up
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5001/

# 5. Run one monitor_loop to verify pipeline
python -m order_flow_engine.src.monitor_loop --symbol ESM6 --tf 15m \
    --max-iterations 1 --log outputs/order_flow/monitor_loop.log

# 6. Verify launchd jobs are loaded
launchctl list | grep com.rfm
# Expected: com.rfm.cache-refresh + com.rfm.health-monitor
```

If launchd jobs are missing, reload:
```bash
launchctl load ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
launchctl load ~/Library/LaunchAgents/com.rfm.health-monitor.plist
```

---

## Bring system down (graceful)

```bash
# 1. Kill Flask (Live SDK shuts down with it)
PID=$(pgrep -f "python.*app\.py" | head -1)
kill "$PID"

# 2. Optional: unload launchd jobs (jobs continue running; only need to
#    unload if doing maintenance)
launchctl unload ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
launchctl unload ~/Library/LaunchAgents/com.rfm.health-monitor.plist

# 3. monitor_loop is a separate process — stop it explicitly
PID=$(pgrep -f "order_flow_engine.src.monitor_loop" | head -1)
kill "$PID"
```

System leaves all output files intact. Restart at any time.

---

## Daily routine

```bash
# 1. Quick health check (terminal)
cd "/Users/ray/Dev/Sentiment analysis projtect"
grep "_summary" outputs/order_flow/health_monitor.log | tail -1

# 2. Open dashboard in browser
open "http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m"

# 3. Check checkpoint state
cat outputs/order_flow/.live_checkpoint_state.json | head -20

# 4. Inspect any HEALTH_*.flag files
ls outputs/order_flow/HEALTH_*.flag 2>/dev/null
```

---

## Recovery scenarios

### Scenario A — Flask down

**Symptom:** `lsof -iTCP:5001 -sTCP:LISTEN` empty; HTTP 000 on curl.

**Action:**
```bash
cd "/Users/ray/Dev/Sentiment analysis projtect"
source .venv/bin/activate
set -a && source .env && set +a
nohup python app.py > /tmp/flask.log 2>&1 &
sleep 60
# Re-apply 15m rename workaround (see below)
```

### Scenario B — Live SDK silent during open market

**Symptom:** No TAPE ALERT in flask.log for >15 min during open market.
Health probes S3 + S4 unhealthy.

**First: confirm market actually open.** ESM6 closes Fri 20:00Z → Sun 22:00Z.
Daily 15-min maintenance break ~20:15-20:30Z most days.

**If market open and silence > 15 min:**
```bash
# Restart Flask (Live SDK auto-starts)
PID=$(pgrep -f "python.*app\.py" | head -1) && kill "$PID" && sleep 3
nohup python app.py > /tmp/flask.log 2>&1 &
sleep 60
# Re-apply 15m rename
```

### Scenario C — 15m parquet present after Flask boot

**Symptom:** `outputs/order_flow/HEALTH_s5_15m_parquet_absent.flag` present
OR `ls order_flow_engine/data/processed/ESM6_15m_live.parquet` returns a file.

**Action:**
```bash
cd "/Users/ray/Dev/Sentiment analysis projtect"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mv order_flow_engine/data/processed/ESM6_15m_live.parquet \
   order_flow_engine/data/processed/ESM6_15m_live.parquet.stale_$STAMP
```

### Scenario D — Raw cache stale

**Symptom:** `HEALTH_s6_raw_freshness.flag` present; raw cache last_bar > 90 min.

**Action:**
```bash
# Manual cache refresh
bash "/Users/ray/Dev/Sentiment analysis projtect/scripts/cache_refresh_esm6.sh"

# If lockdir is stuck:
rmdir /tmp/rfm-cache-refresh.lockdir
bash "/Users/ray/Dev/Sentiment analysis projtect/scripts/cache_refresh_esm6.sh"
```

### Scenario E — cache_refresh.log idle for hours during open market

**Symptom:** `HEALTH_s9_cache_refresh_log.flag` AND market open AND
launchd thinks cache-refresh is loaded.

**Likely cause:** Mac is sleeping. macOS power management throttles launchd
in idle state.

**Action:**
```bash
# Re-enable caffeinate (prevents sleep)
caffeinate -dimsu &

# Force one cache refresh now
bash "/Users/ray/Dev/Sentiment analysis projtect/scripts/cache_refresh_esm6.sh"
```

### Scenario F — Pending fires not settling

**Symptom:** `realflow_outcomes_pending_ESM6_15m.json` has fires whose
`settle_eta_utc` is in the past, but they don't settle.

**Diagnose:**
```bash
# Check raw cache last_bar vs pending settle ETAs
cd "/Users/ray/Dev/Sentiment analysis projtect"
.venv/bin/python -c "
import pandas as pd
df = pd.read_parquet('order_flow_engine/data/raw/ESM6_15m.parquet')
df.index = pd.to_datetime(df.index)
print('raw last_bar:', df.index.max())
"
```

If raw last_bar < pending settle ETA: raw cache is stale. Run Scenario D.

### Scenario G — Checkpoint notification fired

**Symptom:** macOS notification: "RFM Checkpoint: <rule> n<level> WARN/OK/RECOVERED".

**Action:**

1. Open the appropriate review template:
   `docs/reviews/templates/<R1|R2|R7_SHADOW>_REVIEW_TEMPLATE.md`
2. Copy to `docs/reviews/<rule>_n<level>_<UTC>.md`
3. Fill in snapshot, per-fire detail, decision
4. Do NOT take action until review is complete

### Scenario H — Disk space low

**Symptom:** `HEALTH_s11_disk.flag` present; disk usage > 90%.

**Action:**
```bash
# 1. Find largest directories
du -sh outputs/order_flow/* | sort -rh | head -10

# 2. Archive old log files (manual, careful)
gzip outputs/order_flow/health_monitor.log
mv outputs/order_flow/health_monitor.log.gz \
   outputs/order_flow/archives/

# 3. Truncate /tmp/flask.log (Flask reopens it next write)
> /tmp/flask.log
```

### Scenario I — Health monitor process not firing

**Symptom:** `health_monitor.log` mtime > 10 min, no new _summary entries.

**Action:**
```bash
# Re-load launchd job
launchctl unload ~/Library/LaunchAgents/com.rfm.health-monitor.plist
launchctl load ~/Library/LaunchAgents/com.rfm.health-monitor.plist
launchctl list | grep com.rfm.health-monitor
```

### Scenario J — Dashboard shows old data

**Symptom:** Page renders but data is from yesterday.

**Action:**
1. Hard refresh browser (Cmd+Shift+R)
2. Click "Regenerate" button
3. If still old, run manual diagnostic:
   ```bash
   .venv/bin/python -m order_flow_engine.src.monitor_loop \
       --symbol ESM6 --tf 15m --max-iterations 1 \
       --log outputs/order_flow/monitor_loop.log
   ```
4. Hard refresh again

---

## Weekend hibernation

ESM6 closes Fri 20:00Z → Sun 22:00Z. Expected weekend behavior:

- TAPE ALERTs go silent at ~20:00Z Friday
- 1m parquet last_bar stops advancing
- S3 (live SDK) and S4 (1m freshness) probes return "skipped" (market_open
  check)
- S6 (raw cache) eventually goes unhealthy as cache ages past 90 min
  during weekend
- macOS may sleep, throttling launchd jobs

**Do not act during weekend.** Pipeline auto-resumes Sun 22:00Z.

If you want full uptime through weekend (cosmetic only):
```bash
caffeinate -dimsu &
```

---

## What NOT to do (operationally)

```
☐ Do NOT edit any of:
   - rules
   - thresholds
   - config
   - models
   - ml_engine/
   - predictor
   - alert_engine
   - ingest

☐ Do NOT run trading commands (paper or real).

☐ Do NOT restart Flask repeatedly. Each restart re-seeds the 15m parquet
   and resets in-memory state. One restart per real outage is fine; more
   suggests something else is wrong.

☐ Do NOT delete output files (.jsonl, .json, .md, .log) without backup.
   They are append-only history. The outcome tracker depends on them.

☐ Do NOT auto-promote R7 from -0.20 shadow to production. That's a
   separate Phase 2B Stage 2 review.

☐ Do NOT modify check thresholds (n=10, n=15, n=30, retention 0.5,
   hit_rate 0.45) without explicit approval. They are the gating
   constants for live decisions.

☐ Do NOT ignore HEALTH flags. Even if you choose not to act, document why
   in the next review file.
```

---

## Status check one-liners

```bash
# Flask + ports + HTTP
lsof -iTCP:5001 -sTCP:LISTEN; curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5001/

# Latest health summary
grep "_summary" outputs/order_flow/health_monitor.log | tail -1

# Active flags
ls outputs/order_flow/HEALTH_*.flag 2>/dev/null

# launchd status
launchctl list | grep com.rfm

# Live data freshness
.venv/bin/python -c "
import pandas as pd, os
from datetime import datetime, timezone
p = 'order_flow_engine/data/processed/ESM6_1m_live.parquet'
df = pd.read_parquet(p)
df.index = pd.to_datetime(df.index)
if df.index.tz is None: df.index = df.index.tz_localize('UTC')
now = pd.Timestamp.now(tz='UTC')
print('1m last_bar age:', round((now - df.index.max().to_pydatetime()).total_seconds()/60, 1), 'min')
"

# Checkpoint progress
cat outputs/order_flow/.live_checkpoint_state.json | head -20

# Cache refresh
tail -1 outputs/order_flow/cache_refresh.log
```

---

## Logs to read in priority order

1. `/tmp/flask.log` — Live SDK + Flask + ingest activity
2. `outputs/order_flow/health_monitor.log` — every 5-min probe summary
3. `outputs/order_flow/cache_refresh.log` — every hour data_loader run
4. `outputs/order_flow/monitor_loop.log` — every 15-min diagnose + settle
5. `outputs/order_flow/health_monitor.launchd.stderr.log` — launchd errors
6. `outputs/order_flow/cache_refresh.launchd.stderr.log` — launchd errors

---

## Environment

```
.env (relevant flags):
  OF_REAL_THRESHOLDS_ENABLED=1
  OF_DATABENTO_ENABLED=1
  OF_DATABENTO_LIVE=1
  OF_DATABENTO_SYMBOL=ES.FUT
  OF_DATABENTO_SYMBOLS=ES.FUT,GC.FUT,CL.FUT,NQ.FUT
  OF_DATABENTO_TF=1m
  OF_DATABENTO_TFS=1m,15m
  OF_DATABENTO_REAL_FLOW=1
  DATABENTO_API_KEY=<set>
```

Emergency revert all real-flow rules to proxy thresholds:
```
OF_REAL_THRESHOLDS_ENABLED=0
# then restart Flask
```

---

_Update this runbook whenever a recovery scenario is added or changed.
Reverse any operational change by following the inverse procedure._
