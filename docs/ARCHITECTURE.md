# ARCHITECTURE — RFM ES Real-Flow Monitor

> **Status:** Documentation only. Snapshot of the system as it actually
> runs (not aspirational). Reflects the local Mac deployment, ESM6 15m,
> Phase 2A active.

---

## Single-machine deployment

Everything runs on one Mac. No cloud, no broker, no execution. macOS
launchd schedules background jobs. Flask serves the dashboard.

```
┌────────────────────────────────────────────────────────────────────┐
│                       MAC (single host)                            │
│                                                                    │
│  ┌──────────┐   ┌────────────────────────┐   ┌──────────────┐      │
│  │ launchd  │──▶│ com.rfm.cache-refresh   │──▶│ scripts/     │      │
│  │          │   │   (hourly)              │   │  cache_      │      │
│  │          │   │                         │   │  refresh.sh  │      │
│  │          │──▶│ com.rfm.health-monitor  │──▶│ scripts/     │      │
│  │          │   │   (every 5 min)         │   │  health_     │      │
│  └──────────┘   └────────────────────────┘   │  monitor.py  │      │
│                                              └──────┬───────┘      │
│                                                     │              │
│                                                     ▼              │
│                                              ┌──────────────┐      │
│                                              │ macOS        │      │
│                                              │ osascript    │      │
│                                              │ notification │      │
│                                              └──────────────┘      │
│                                                                    │
│  ┌──────────┐                                                      │
│  │ Flask    │                                                      │
│  │ app.py   │                                                      │
│  │ :5001    │◀───── browser ───┐                                   │
│  │          │                  │                                   │
│  │ ┌──────┐ │                  │  http://localhost:5001/...        │
│  │ │ Live │ │                                                      │
│  │ │ SDK  │ │◀──── trade prints ──── Databento WebSocket ──┐       │
│  │ │ thread│ │                                              │       │
│  │ └──┬───┘ │                                               │       │
│  │    │     │                                               │       │
│  │    ▼     │                                               │       │
│  │ ┌──────┐ │                                               │       │
│  │ │ingest│ │── 1m bars ─▶ ESM6_1m_live.parquet (every 25)  │       │
│  │ └──────┘ │                                               │       │
│  └──────────┘                                               │       │
│                                                             │       │
│  ┌──────────────┐                                          │       │
│  │ monitor_loop │──── every 15min ─────┐                   │       │
│  │   process    │                      │                   │       │
│  └──────────────┘                      ▼                   │       │
│                                  ┌──────────┐              │       │
│                                  │ outcome  │              │       │
│                                  │ tracker  │              │       │
│                                  └────┬─────┘              │       │
│                                       │                    │       │
│                                       ▼                    │       │
│                              ┌──────────────────┐          │       │
│                              │ outputs/order_   │          │       │
│                              │ flow/*.jsonl     │          │       │
│                              │      *.json      │          │       │
│                              │      *.md        │          │       │
│                              │      *.log       │          │       │
│                              └──────────────────┘          │       │
└────────────────────────────────────────────────────────────┼───────┘
                                                             │
                                                             ▼
                                                     ┌──────────────┐
                                                     │  Databento   │
                                                     │  Historical  │
                                                     │  + Live SDK  │
                                                     │  GLBX.MDP3   │
                                                     └──────────────┘
```

---

## Process inventory

| process | started by | role | persistence |
|---|---|---|---|
| `app.py` (Flask) | manual `nohup python app.py` | dashboard server, Live SDK auto-start, ingest | runs until killed |
| Live SDK thread | started inside Flask via `realtime_databento_live` | consumes Databento WebSocket trade prints, emits 1m bars to ingest | dies with Flask |
| `monitor_loop` | manual `python -m order_flow_engine.src.monitor_loop` | every 15 min: `diagnose()` + `settle_pass()` + `r7_shadow_pass()` | runs until killed |
| `cache-refresh` (launchd) | `~/Library/LaunchAgents/com.rfm.cache-refresh.plist` | hourly raw OHLCV refresh via `data_loader --no-cache` | persists across reboots |
| `health-monitor` (launchd) | `~/Library/LaunchAgents/com.rfm.health-monitor.plist` | every 5 min: 13 probes + osascript notify | persists across reboots |

---

## Module structure

```
order_flow_engine/
  src/
    config.py                       # thresholds, env flags, paths
    data_loader.py                  # raw OHLCV fetch via Databento Historical
    realtime_databento.py           # Databento parent-symbol resolve, real-flow fetch
    realtime_databento_live.py      # Live SDK WebSocket consumer (1m bars)
    ingest.py                       # bar-level append + persistence (every 25 bars)
    rule_engine.py                  # R1, R2, R3-R6, R7 detection (NOT touched in current phase)
    feature_engineering.py          # per-bar features (NOT touched)
    label_generator.py              # historical labels for ML (NOT touched)
    realflow_compare.py             # diagnose() — proxy vs real comparison + joined window
    realflow_loader.py              # _merge_history_and_live() (live wins on overlap)
    realflow_history_backfill.py    # manual backfill of 18d historical real-flow
    realflow_outcome_tracker.py     # Phase 2D Stage 1: per-fire scoring + JSONL append
    realflow_r7_shadow.py           # Phase 2B shadow tracker (-0.20)
    realflow_threshold_sweep.py     # Phase 2A calibration sweep
    realflow_r7_sweep.py            # Phase 2B R7 sweep
    realflow_recon.py               # Phase 1G volume mismatch investigation
    realflow_denominator_test.py    # Phase 1H no-op denominator test
    monitor_loop.py                 # 15min cadence: diagnose + settle + shadow
    dashboard.py                    # Flask routes
    alert_engine.py                 # NOT touched
    predictor.py                    # NOT touched
  data/
    raw/<sym>_<tf>.parquet          # OHLCV cache (refreshed hourly)
    processed/
      <sym>_<tf>_realflow_history.parquet  # Phase 1F historical real-flow
      <sym>_<tf>_live.parquet              # Live tail (every 25 bars)
      <sym>_1m_live.parquet                # 1m live tail

scripts/
  cache_refresh_esm6.sh             # hourly cache refresh wrapper
  realflow_backfill.sh              # 8h scheduled realflow_history backfill wrapper
  health_monitor.py                 # 16 probes + osascript notify
  health_monitor.sh                 # venv wrapper

templates/
  realflow_diagnostic.html          # main 4-layer dashboard

outputs/order_flow/
  realflow_outcomes_<sym>_<tf>.jsonl              # append-only R1/R2 settled outcomes
  realflow_r7_shadow_outcomes_<sym>_<tf>.jsonl    # append-only R7 shadow outcomes
  realflow_outcomes_pending_<sym>_<tf>.json       # rewrite-each-iter R1/R2 pending
  realflow_r7_shadow_pending_<sym>_<tf>.json      # rewrite-each-iter R7 shadow pending
  realflow_outcomes_summary_<sym>_<tf>.json       # aggregated summary
  realflow_r7_shadow_summary_<sym>_<tf>.json      # aggregated shadow summary
  realflow_diagnostic_<sym>_<tf>.json             # diagnose() output (overwritten)
  health_monitor.log                              # append-only probe log
  cache_refresh.log                               # append-only refresh log
  realflow_backfill.log                           # append-only backfill log
  monitor_loop.log                                # append-only monitor log
  .health_state.json                              # per-probe transition state
  .live_checkpoint_state.json                     # per-rule per-level checkpoint state
  .pending_snapshot.json                          # s16 pending-disappearance state
  HEALTH_*.flag                                   # sticky transition flags
  cache_refresh_FAILED.flag                       # 3+ consecutive cache failure
  realflow_backfill_FAILED.flag                   # 3+ consecutive backfill failure
  latest_status.md                                # last diagnostic snapshot

~/Library/LaunchAgents/
  com.rfm.cache-refresh.plist
  com.rfm.health-monitor.plist
  com.rfm.realflow-backfill.plist
```

---

## Data flow

### Ingest path (real-time)

```
Databento WebSocket
    │
    ▼
realtime_databento_live.py
    │ aggregates trade prints into 1m bars
    │ emits TAPE ALERTs (sweep / iceberg / block) inline
    ▼
ingest.ingest_bar(timeframe="1m", ...)
    │ appends to in-memory tail (max 500 bars)
    │ every 25 bars: writes ESM6_1m_live.parquet (atomic)
    │
    └──── (NO 15m emission — Live SDK does NOT emit 15m bars)
```

### Diagnostic path (every 15 min via monitor_loop)

```
monitor_loop iteration:
    │
    ├── diagnose(symbol, tf)
    │     ├── _load_pair():
    │     │     raw   = read raw/<sym>_<tf>.parquet
    │     │     real  = realflow_loader._merge_history_and_live()
    │     │             ├── load <sym>_<tf>_realflow_history.parquet
    │     │             ├── load <sym>_<tf>_live.parquet (or 1m resampled fallback)
    │     │             └── concat with live winning on overlap
    │     │     common = raw.index.intersection(real.index)
    │     │     ← BOTTLENECK: joined window cap = min(raw.max, real.max)
    │     ├── compute features, fire rules
    │     ├── monitoring (drift, vol_match, fires_cumulative)
    │     ├── distribution (proxy vs real)
    │     ├── threshold_sensitivity
    │     ├── top_diff_bars
    │     └── write realflow_diagnostic_<sym>_<tf>.json
    │
    ├── settle_pass(symbol, tf)
    │     for each R1/R2 fire on real-flow path:
    │       if not in settled JSONL:
    │         compute settle_eta = fire + 12 * 15min
    │         if (now - settle_eta) >= grace AND fwd bars exist in joined:
    │           score outcome (mae_r, mfe_r, fwd_r_signed, hit_1r, stopped_out_1atr)
    │           append to <sym>_<tf>.jsonl (mode tagged via _build_mode_index)
    │         else:
    │           leave pending (rewrite pending file)
    │     rebuild summary JSON
    │
    └── r7_shadow_pass(symbol, tf)
          (same as settle_pass but using -0.20 threshold for R7)
```

### Health monitor path (every 5 min via launchd)

```
health_monitor.py:
    │
    ├── for each probe in [s1_flask_proc, s2_flask_http, ..., s13_checkpoints]:
    │     status, details = probe()
    │     if transition (healthy <-> unhealthy):
    │       osascript notify
    │       write/clear HEALTH_<probe>.flag
    │
    ├── persist new state to .health_state.json
    │
    └── append _summary line to health_monitor.log
```

### Cache refresh path (hourly via launchd)

```
cache_refresh_esm6.sh:
    │
    ├── acquire mkdir lock /tmp/rfm-cache-refresh.lockdir
    │
    ├── mtime skip-check: if raw last_bar < 30 min, exit SKIP
    │
    ├── python -m order_flow_engine.src.data_loader --symbol ESM6 --no-cache
    │     ├── databento_fetcher.fetch_intraday for each TF (5m/15m/1h/1d)
    │     └── overwrite data/raw/<sym>_<tf>.parquet
    │
    ├── log OK / FAIL with raw_max
    │
    └── on 3+ consecutive failures: write cache_refresh_FAILED.flag
```

---

## Threshold paths (Phase 2A flag-controlled)

```
OF_REAL_THRESHOLDS_ENABLED=1   (current)
    │
    ▼
rule_engine.apply_rules(bar):
    if bar["bar_proxy_mode"] == 0 (real-flow):
        use *_REAL thresholds:
            RULE_DELTA_DOMINANCE_REAL  = 0.04
            RULE_ABSORPTION_DELTA_REAL = 0.20
            RULE_TRAP_DELTA_REAL       = 0.12
            RULE_CVD_CORR_THRESH       = -0.50  (R7 production unchanged)
    else (proxy):
        use original thresholds:
            RULE_DELTA_DOMINANCE   = 0.30
            RULE_ABSORPTION_DELTA  = 0.50
            RULE_TRAP_DELTA        = 0.30

OF_REAL_THRESHOLDS_ENABLED=0   (revert)
    │
    ▼
rule_engine.apply_rules(bar):
    always use proxy thresholds  (per-bar bar_proxy_mode ignored)
```

R7 shadow at -0.20 lives module-local in `realflow_r7_shadow.py`. Never
copied to config. Production R7 stays at -0.50.

---

## Mode tagging (live vs historical)

```
realflow_outcome_tracker._build_mode_index(symbol, tf):
    out = {}
    if history.parquet exists:
        out[ts] = "historical" for each ts in history.index
    if live.parquet exists:
        out[ts] = "live" for each ts in live.index   # overrides historical
    elif 1m.parquet exists:
        resampled = resample_to_tf(1m, tf)
        out[ts] = "live" for each ts in resampled.index   # fallback
    return out
```

Quirk: 1m parquet caps at 500 bars (~8.3h rolling). Fires older than 8.3h
get tagged "historical" at settle even if originally live.

---

## Boundaries

```
NEVER MUTATED:
  - rule_engine.py
  - config.py (Phase 2A real thresholds)
  - models / ml_engine
  - predictor.py
  - alert_engine.py
  - ingest.py
  - feature_engineering.py
  - label_generator.py
  - any production threshold

READ + APPEND ONLY:
  - all outputs/order_flow/*.jsonl
  - PROJECT_STATE.md (manual edits with audit trail)

WRITE-EACH-CYCLE (atomic):
  - realflow_diagnostic_<sym>_<tf>.json (overwritten by diagnose)
  - realflow_outcomes_pending_<sym>_<tf>.json (rewritten by settle_pass)
  - realflow_outcomes_summary_<sym>_<tf>.json (rebuilt by settle_pass)
  - .health_state.json (rewritten each probe run)
  - .live_checkpoint_state.json (rewritten each S13 run)
  - .pending_snapshot.json (rewritten each S16 run)
```

---

## Failure domains

| domain | symptom | recovery |
|---|---|---|
| Flask process | port 5001 unresponsive | restart Flask + re-rename 15m parquet |
| Live SDK | TAPE ALERT silence > 15 min during open market | restart Flask |
| ingest persistence | 1m parquet stops updating | inherent every-25-bar cadence; check Live SDK |
| 15m parquet seed | startup re-seeds stale | manual rename to .stale_<UTC> |
| raw cache | last_bar > 90 min stale | hourly launchd OR manual `data_loader --no-cache` |
| realflow_history | front edge ages | manual `realflow_history_backfill` (heavy, deferred) |
| outcome tracker | pending fires not settling | check raw + real cap; refresh either or both |
| monitor_loop | dies | manual restart |
| launchd jobs | not firing during weekend | macOS power management; caffeinate to mitigate |
| disk | low space | archive logs |

---

## Data freshness budget

| layer | acceptable lag |
|---|---|
| Live SDK trade prints | < 5 sec end-to-end |
| 1m parquet write | < 30 min (25-bar persist cadence + buffer) |
| 15m parquet (resampled) | < 15 min (per 1m roll) |
| raw OHLCV cache | < 90 min (hourly refresh + buffer) |
| realflow_history | days (manual backfill cadence) |
| outcome tracker | < 15 min after horizon expiry |
| dashboard | manual refresh OR 30s auto-poll for outcomes/r7 shadow cards |
| daily report | manual run |

---

_This document reflects the system as deployed at write date. Update when
processes / files / paths change. Reverse by deleting._
