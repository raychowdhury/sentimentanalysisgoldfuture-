# HANDOFF — for new Claude session

> Read this first. Then if you need detail, follow the pointers at the
> bottom. PROJECT_STATE.md is the live source of truth; this file is a
> session-bridge summary as of 2026-05-03 22:43Z.

---

## What this project is in one paragraph

Local research pipeline on a single Mac. Streams real-time CME E-mini S&P
500 futures (ESM6 contract, 15m timeframe) via Databento Live SDK. Detects
three reversal patterns (R1 fade-buyer-dom = short, R2 fade-seller-dom =
long, R7 CVD divergence). Tracks per-rule outcome quality (mean_r, hit
rate, retention vs calibration baseline). **Not a trading system.** No
broker, no execution, no auto-action. The current question being answered:
do calibrated real-flow rule thresholds hold up forward in live data, or
do they decay?

## Current phase

```
Phase 1A-1H:    DONE   (calibration, diagnostic, dashboards, monitoring extension)
Phase 2A:       ACTIVE (R1/R2/R3-R6 real-flow thresholds, env-flagged)
Phase 2B:       DEFERRED → SHADOW only (R7 at -0.20 shadow, production stays -0.50)
Phase 2C:       NOT STARTED (direction-sign investigation candidate)
Phase 2D:       Stage 1+2 done (outcome tracker + dashboard surface)
Phase 2D Stage 3: NOT scoped (decision logic — manual revert via env only)
Phase 3:        ACTIVE (operational scaffolding, dashboards, docs)
Phase 4:        NOT STARTED (paper trading planning doc only)
Phase 5:        NOT IN SCOPE (real capital)
```

## Standing instruction (do NOT violate)

```
NEVER edit:
  rules / thresholds / config.py / models / ml_engine/
  predictor.py / alert_engine.py / ingest.py / outcome scoring

NEVER:
  place trades  / promote R7 / auto-revert
  retrain models / cross-asset port before ESM6 verdict
  bypass health_monitor / cache_refresh
  add anything that mutates engine state from a "research" tool

ALL trackers are read+append-only. Dashboard, daily report, health monitor,
and review templates are operational scaffolding only.
```

---

## What's running right now

```
Flask app.py            UP    PID 16126   port 5001    HTTP 200
Live SDK                SUBSCRIBED to ESM6/NQM6/GCM6/CLM6   (TAPE ALERTs flowing)
launchd cache-refresh   loaded, hourly, last fire 22:41Z   (raw_max 22:30Z)
launchd health-monitor  loaded, every 5 min, last fire 22:41Z
monitor_loop            runs every 15 min — independently started by user
                        (NOT a launchd job; if not visible in pgrep, restart manually)

health probes:          13/13 healthy, 0 flags
ESM6_15m_live.parquet:  ABSENT (renamed workaround applied — correct state)
ESM6_1m_live.parquet:   present, updates every ~25 min via _LIVE_PERSIST_EVERY=25
```

## Live checkpoint state (snapshot)

```
RULE                   n   mean_r   retention   hit_rate   status@n10
r1_buyer_down          5   -2.342   -1.985      0.6        NOT_REACHED  (trending WARN)
r2_seller_up           7   +1.093   +1.457      0.571      NOT_REACHED  (trending OK)
r7_cvd_divergence_shadow  9   -3.559   -4.988      0.333    NOT_REACHED  (trending WARN, ~1 fire from n=10)
```

R7 shadow is closest to firing the n=10 transition notification (1 more fire).
S13 health probe will fire macOS osascript when transitions happen.

## Trader View dashboard (current state)

```
SYSTEM:        Healthy
LIVE DATA:     Connected
R1:            Watching (n=5)
R2:            Watching (n=7)
R7:            Shadow only (n=9)
ACTION:        Keep monitoring
```

## URLs

```
http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m   (main dashboard, 4-layer)
http://localhost:5001/                                                     (root)
http://localhost:5001/trader-desk                                          (trader desk)
```

---

## What we built across the session (in order)

1. **Hourly raw OHLCV cache refresh** via launchd (`com.rfm.cache-refresh`) +
   `scripts/cache_refresh_esm6.sh` (POSIX mkdir lock; flock unavailable on macOS).
2. **Health monitor Phase 1** via launchd (`com.rfm.health-monitor`) +
   `scripts/health_monitor.py` — 12 probes, 5-min cadence, osascript notifications.
3. **Diagnosed live 15m parquet freeze bug**: Live SDK only emits 1m bars.
   15m parquet gets startup-seeded then frozen. **Workaround:** rename
   `ESM6_15m_live.parquet` to `*.stale_<UTC>` after each Flask boot. This
   forces 1m→15m resample fallback in `realflow_loader` + `outcome_tracker`.
   **Recurring step required after every Flask restart.**
4. **Trader-friendly dashboard redesign** — 4-layer collapsible structure:
   - Layer 1 always visible: Trader View + Live Signal Trade Details
   - Layer 2 collapsed: Phase 2A outcomes + R7 shadow + Daily Metrics + Joined Window
   - Layer 3 collapsed: Status, Phase 2 Gate, Distribution, Vol recon, Threshold sens, Top diff bars, Per-bar trace, Generated/Freshness
   - Layer 4 collapsed: Help / glossary
5. **Live Checkpoint Tracker** (Phase 2D Stage 1 extension) — per-rule per-level
   classifications at n=10/15/30. Backed by `.live_checkpoint_state.json` and
   health probe `s13_checkpoints` (now 13 probes total). Notifies on transition.
6. **Daily Research Report** — REMOVED on 2026-05-04. Was a manual Markdown summary script + Flask viewer.
7. **Expected Move (Advisory)** — REMOVED on 2026-05-04. Was a probabilistic price-range envelope tool + Flask viewer.
8. **Planning Documents:**
   - `docs/PAPER_TRADING_PLAN.md` (Phase 4 simulated-only plan)
   - `docs/SIGNAL_REVIEW_PLAYBOOKS.md` (n=10/15/30 review process)
9. **Checkpoint Review Templates:**
   - `docs/reviews/templates/R1_REVIEW_TEMPLATE.md`
   - `docs/reviews/templates/R2_REVIEW_TEMPLATE.md`
   - `docs/reviews/templates/R7_SHADOW_REVIEW_TEMPLATE.md`
   - `docs/reviews/templates/CROSS_RULE_REVIEW_TEMPLATE.md`
10. **Investor One-Pager + Ops Runbook:**
    - `docs/INVESTOR_ONE_PAGER.md`
    - `docs/OPS_RUNBOOK.md`
11. **Architecture + Data Quality:**
    - `docs/ARCHITECTURE.md`
    - `docs/DATA_QUALITY_CHECKLIST.md`
12. **Regenerate button JS fix** — `window.location.href = url` → `window.location.reload()`.

---

## What's risky / known caveats

| risk | mitigation |
|---|---|
| R1 historical retention 0.07 (weak edge) | live likely confirms; n=30 may revert R1 |
| R7 shadow live mean_r -3.56 (failing badly) | will stay shadow; never auto-promote |
| 15m parquet freeze bug | manual rename after each Flask restart (in OPS_RUNBOOK Scenario C) |
| Live SDK abort on weekend boot | "no resolvable symbols" — needs Flask restart on next market open |
| 1m parquet 25-bar persist cadence | up to 25 min lag normal; not a bug |
| 1m tail rolling 8.3h cap | fires older than 8.3h tagged "historical" at settle |
| Mac sleeping → launchd throttled | weekend OK; weekday → use `caffeinate -dimsu &` |
| volume reconciliation 0.48 mismatch | informational only (Phase 1G investigated, ETH thinness) |

---

## Recurring operational gotchas

These bite EVERY Flask restart. Document in PROJECT_STATE if not already.

1. **After Flask restart**, rename freshly-seeded 15m parquet:
   ```bash
   STAMP=$(date -u +%Y%m%dT%H%M%SZ)
   mv "/Users/ray/Dev/Sentiment analysis projtect/order_flow_engine/data/processed/ESM6_15m_live.parquet" \
      "/Users/ray/Dev/Sentiment analysis projtect/order_flow_engine/data/processed/ESM6_15m_live.parquet.stale_$STAMP"
   ```
2. **After Flask restart during weekend close**, Live SDK will abort. Wait
   for market reopen Sun 22:00Z, then restart Flask again.
3. **Trader View showing Broken** when only s6_raw_freshness is unhealthy:
   trigger manual cache refresh:
   ```bash
   bash "/Users/ray/Dev/Sentiment analysis projtect/scripts/cache_refresh_esm6.sh"
   ```
4. **Browser shows old JS after template edits**: hard refresh
   (Cmd+Shift+R). Flask hot-reloads templates server-side; browser may not.
5. **Symbol/TF input field contamination**: if Trader View shows error
   path with `?` or URL pieces, user pasted into the input. Clear field,
   set to ESM6 / 15m, reload.

---

## Next reasonable action

**Default: do nothing.** Wait for live checkpoint transitions. The S13 health
probe will fire macOS notifications when:
- R7 shadow @ n=10 (1 fire away — closest)
- R2 @ n=10 (3 fires away)
- R1 @ n=10 (5 fires away)

When notification fires:
1. Open the matching review template in `docs/reviews/templates/`.
2. Copy to `docs/reviews/<rule>_n<level>_<UTC>.md`.
3. Fill in snapshot, per-fire detail, decision.
4. Do NOT change thresholds or rules at n=10 or n=15. n=30 is the first
   verdict checkpoint where reverts are considered.

ETA at current rates (assuming continuous Flask uptime):
- All three rules reach n=10: ~1-2 days
- All three reach n=15: ~5-7 days
- First n=30 verdict: ~10-14 days

---

## File pointers (drill down only if needed)

```
PROJECT_STATE.md                              live source of truth, all phases + sections
outputs/order_flow/latest_status.md            last diagnostic snapshot
outputs/order_flow/health_monitor.log          append-only probe log (tail _summary)
outputs/order_flow/.live_checkpoint_state.json  per-rule per-level checkpoint state
outputs/order_flow/cache_refresh.log           hourly cache refresh log
/tmp/flask.log                                 Flask + Live SDK runtime log

docs/ARCHITECTURE.md                           full system architecture + data flow + ASCII diagram
docs/OPS_RUNBOOK.md                            cold start / recovery scenarios A-J / status one-liners
docs/DATA_QUALITY_CHECKLIST.md                 15-section verification checklist with copy-paste commands
docs/INVESTOR_ONE_PAGER.md                     plain-English honest framing
docs/PAPER_TRADING_PLAN.md                     Phase 4 plan (simulated only, deferred)
docs/SIGNAL_REVIEW_PLAYBOOKS.md                checkpoint review process
docs/reviews/templates/                        per-rule + cross-rule review templates

scripts/health_monitor.py                      16 probes
scripts/cache_refresh_esm6.sh                  hourly raw OHLCV refresh
scripts/realflow_backfill.sh                   8h scheduled realflow_history backfill

order_flow_engine/src/dashboard.py             Flask routes (do not modify rule logic)
templates/realflow_diagnostic.html             4-layer dashboard
```

---

## Quick health sanity for the new session

Before doing anything, run:

```bash
cd "/Users/ray/Dev/Sentiment analysis projtect"
date -u
ps aux | grep "python.*app\.py" | grep -v grep
launchctl list | grep com.rfm
grep "_summary" outputs/order_flow/health_monitor.log | tail -1
ls outputs/order_flow/HEALTH_*.flag 2>/dev/null
cat outputs/order_flow/.live_checkpoint_state.json | head -20
```

Expected (healthy):
- Flask process exists
- Both `com.rfm.*` jobs loaded
- Last `_summary` is `healthy=13` or close
- No HEALTH flag files (or only s6 if cache refresh just expired)
- Checkpoint state intact

If anything is off, follow `docs/OPS_RUNBOOK.md` Scenario A-J.

---

## Caveman mode active

User runs in caveman mode (terse, fragment-OK, drop articles). Match it.
Code/commits/security: write normal. Spec docs: write normal English. The
toggle is "stop caveman" / "normal mode".

---

## Session continuity rules

- Update `PROJECT_STATE.md` after every major change.
- Update `outputs/order_flow/latest_status.md` after every diagnostic /
  outcome run.
- End every task with: "Updated PROJECT_STATE.md and latest_status.md."
- For truncated specs (mid-bash-block): propose minimum interpretation +
  list 2-4 numbered open questions, wait for "go" or "default all + your
  call" before implementing.

---

_Last updated: 2026-05-03T22:43Z. New session: read this, run the sanity
block, then proceed per user's next instruction._
