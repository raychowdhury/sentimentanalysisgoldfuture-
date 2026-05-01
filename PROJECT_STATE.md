# PROJECT_STATE — RFM ES Real-Flow Monitor

_Last updated: 2026-05-01 UTC_

Decision-focused snapshot. Update after every major change.

## Phase status

| phase | status | note |
|-------|--------|------|
| 1A — real-flow comparison | done | `realflow_compare.py` |
| 1B — diagnostic | done | per-bar trace + threshold sensitivity |
| 1C — dashboard view | done | `/order-flow/realflow-diagnostic` |
| 1D — readiness plan | done | 5 gates defined |
| 1E — monitoring extension | done | drift/vol/session/fires |
| 1F — historical backfill | done | `realflow_history_backfill.py`, cap=18d |
| 1G — volume recon investigation | done | RTH-open/close auction skew identified |
| 1H — denominator switch test | done | no-op confirmed; threshold retune chosen instead |
| **2A — R1/R2/R3-R6 real-flow thresholds** | **ACTIVE** | calibrated, env-flagged |
| 2B — R7 calibration | **deferred → shadow-only** | re-sweep yielded 1 borderline cell at -0.20; running shadow |
| 2C — direction-sign investigation | not started | candidate for later |
| 2D Stage 1 — outcome tracker | done | `realflow_outcome_tracker.py` |
| 2D Stage 2 — dashboard surface | done | "Phase 2A Live Outcomes" card |
| 2D Stage 3 — decision logic | NOT scoped | manual revert via env flag only |

## Current production thresholds

| constant | value | path |
|----------|-------|------|
| `RULE_DELTA_DOMINANCE` | 0.30 | proxy |
| `RULE_ABSORPTION_DELTA` | 0.50 | proxy |
| `RULE_TRAP_DELTA` | 0.30 | proxy |
| `RULE_DELTA_DOMINANCE_REAL` | **0.04** | real (Phase 2A) |
| `RULE_ABSORPTION_DELTA_REAL` | **0.20** | real (Phase 2A) |
| `RULE_TRAP_DELTA_REAL` | **0.12** | real (Phase 2A) |
| `RULE_CVD_CORR_THRESH` | -0.50 | both paths (R7) |
| `RULE_CVD_CORR_WINDOW` | 20 | both paths (R7) |

Path is selected per-bar by `bar_proxy_mode` column inside `rule_engine.apply_rules` when `OF_REAL_THRESHOLDS_ENABLED=1`.

## Shadow-only constants (NOT in config.py)

| constant | value | location |
|----------|-------|----------|
| `RULE_CVD_CORR_THRESH_REAL_SHADOW` | -0.20 | `realflow_r7_shadow.py` module-local |

Shadow tracks what R7 would do at -0.20 without firing in production.

## Active env flags

```
OF_REAL_THRESHOLDS_ENABLED=1     # Phase 2A R1/R2/R3-R6 real-flow thresholds active
OF_DATABENTO_ENABLED=1
OF_DATABENTO_LIVE=1
OF_DATABENTO_SYMBOLS=ESM6
OF_DATABENTO_TFS=1m,15m
```

Rollback: set `OF_REAL_THRESHOLDS_ENABLED=0` to revert all bars to proxy thresholds.

## Active commands

```bash
# Flask (auto-starts Databento Live SDK + outcome_tracker thread)
.venv/bin/python app.py

# Monitor loop — diagnose + settle_pass + r7_shadow every 15 min
.venv/bin/python -m order_flow_engine.src.monitor_loop \
    --symbol ESM6 --tf 15m --interval 900 \
    --log outputs/order_flow/monitor_loop.log
```

Both run inside tmux session `rfm`. Dashboard: `http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m` via SSH tunnel.

## Operational automation

Hourly raw OHLCV cache refresh is active via launchd.

```
Label:           com.rfm.cache-refresh
Script:          scripts/cache_refresh_esm6.sh
Plist:           ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
Cadence:         StartInterval=3600 (1h), RunAtLoad=true
Lock:            POSIX mkdir atomic at /tmp/rfm-cache-refresh.lockdir
mtime skip:      30 min — skip if raw last_bar < 30 min old
Log:             outputs/order_flow/cache_refresh.log
Failure flag:    outputs/order_flow/cache_refresh_FAILED.flag (after 3 consecutive fails)
Stop:            launchctl unload ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
```

Purpose: refresh raw ESM6 15m OHLCV cache hourly so joined window stays aligned with live tail and pending outcomes can settle (joined-window cap = `raw.index ∩ real.index`; raw is the bottleneck since live SDK already streams `real` forward in real-time).

Notes:
- Uses `data_loader --symbol ESM6 --no-cache`
- Does NOT run `realflow_history_backfill` (manual only — heavy trades fetch)
- Does NOT change rules, thresholds, models, ml_engine, predictor, alert_engine, ingest, or trading behavior
- Read+append only on output files

## Standing instruction

- No detector behavior changes.
- No edits to rules / thresholds / labels / models / `ml_engine/` / predictor / alert_engine / ingest.
- No trades.
- No auto-revert / auto-promote.
- All trackers read+append-only.
- Phase 2A active. Phase 2B deferred (shadow-tracked at -0.20).
- Manual dashboard checks by user.

## Next checkpoint

| metric | target | current |
|--------|--------|---------|
| R1 live settled | ≥ 30 | 2 |
| R2 live settled | ≥ 30 | 2 |
| R7 shadow live settled | ≥ 30 | 3 |

When R1+R2 live ≥ 30 each, run Phase 2D verdict (keep / retune / continue).
When R7 shadow live ≥ 30, evaluate Phase 2B Stage 2 readiness.

## Known blockers

1. ~~**Live SDK in Databento 422 error.**~~ **RESOLVED 2026-05-01.** `data_schema_not_fully_available` no longer observed. Live SDK subscribes raw front-month (ESM6/GCM6/CLM6/NQM6) cleanly. Tape alerts streaming. Residual `data_end_after_available_end` 422 from outcome_tracker is harmless settle-lag against historical dataset publication (~1-3min behind wall-clock).
2. **Raw OHLCV cache stale risk.** `data/raw/ESM6_15m.parquet` does not auto-refresh in current Flask session. Joined window caps at raw `last_bar`, which blocks live fires from settling once their forward-horizon timestamps trail past it. **Workaround:** periodic `python -m order_flow_engine.src.data_loader --symbol ESM6 --no-cache` refreshes raw + downstream realflow_history. No code change. Candidate for scheduled cadence.
3. **R1 retention 0.13 in 18d historical.** Below 0.5 gate but mean_r still positive. Per recommendation logic: continue monitoring on live-only sample.
4. **Vol_match mismatch 0.48 (gate ≤0.25).** Phase 1H showed denominator switch is no-op; gate is informational only. Not a blocker.

## Active triggers (ping when any fires)

1. `phase2_gates.all_pass == true`
2. `joined_bar_count ≥ 200` AND `vol_match_mismatch_5pct > 0.40`
3. `delta_ratio_drift.drift_pct != null` AND `drift_pct > 0.30`
4. `live_nan_rows / joined_bar_count > 0.05`
5. `joined_bar_count ≥ 500` ✅ already cleared
6. `joined_bar_count ≥ 1000` AND `r7_cvd_divergence ≥ 30`
7. R1 settled ≥ 100 AND R2 settled ≥ 100 ✅ already cleared
8. R1 live ≥ 30 AND R2 live ≥ 30 → Phase 2D live verdict
9. R7 shadow live ≥ 30 → Phase 2B Stage 2 review

## Symbols & data

- Primary: ESM6 (E-mini S&P June 2026), 15m timeframe.
- Cache: `order_flow_engine/data/raw/ESM6_15m.parquet` (~7869 bars, 6 months).
- Live tail: `order_flow_engine/data/processed/ESM6_15m_live.parquet` + 1m equivalent.
- Historical real-flow: `order_flow_engine/data/processed/ESM6_15m_realflow_history.parquet` (18-day window, ~920 bars, all `historical_realflow_tick_rule`).
- Joined window: 1289 bars (cache ∩ real merged).

## Coordination files

- [PROJECT_STATE.md](PROJECT_STATE.md) — this file (decision snapshot)
- [NEXT_ACTIONS.md](NEXT_ACTIONS.md) — open work and proposed next moves
- [RUNBOOK.md](RUNBOOK.md) — common commands and recovery
- [outputs/order_flow/latest_status.md](outputs/order_flow/latest_status.md) — last diagnostic snapshot
