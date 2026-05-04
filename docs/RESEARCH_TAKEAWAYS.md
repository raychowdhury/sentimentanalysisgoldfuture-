# Research Takeaways — Practical Checklist

Status: ACTIVE — research methodology + verdict-acceleration playbook
Scope: ES Real-Flow Monitor (ESM6, 15m). Documentation only. No rule / threshold / model / ml_engine / outcome scoring / horizon / R7 promotion / trading behavior change.

---

## 1. Event-count validation framing

The verdict for any rule is a function of **event count**, not calendar time. The system fires sparsely; each event is a sample. Statistical confidence depends on n, not on hours waited.

```
calendar time = waiting
event count   = evidence
verdict       = function(event count, hit_rate, mean_r, drawdown, diversity)
```

Implications:
- A rule that fires twice a day reaches verdict in ~15 days (n=30)
- A rule that fires twice a week reaches verdict in ~15 weeks (n=30)
- Cannot accelerate by waiting harder; only by widening the candidate set or accepting smaller n
- Soft verdicts at n=15 trade confidence for time

---

## 2. Time-to-30 vs time-to-decision

Two distinct latencies:

| metric | meaning | current target |
|---|---|---|
| **time to n=30** | wall-clock until 30 settled live fires for any rule | R7 shadow ~6w · R2 ~9w · R1 ~14w |
| **time to decision** | wall-clock until reviewer issues a verdict | n=10 (early warning) · n=15 (soft) · n=30 (hard) |

A rule can be **decided** at n=15 even though n=30 is far away. Calling the game early is allowed if soft-verdict gates are met. Reverse is also true: even at n=30 the verdict can be DEFER if diversity gates fail.

---

## 3. Approved acceleration methods

These do NOT change signal logic. All have been built (or are scheduled) in this project.

| method | mechanism | files |
|---|---|---|
| Keep realflow_history fresh | 8h scheduled backfill prevents joined-window stall | `scripts/realflow_backfill.sh`, `com.rfm.realflow-backfill.plist` |
| Hourly raw OHLCV refresh | raw cache stays within 1h of wall-clock | `scripts/cache_refresh_esm6.sh`, `com.rfm.cache-refresh.plist` |
| Detect pending disappearance | s16 probe diffs pending JSON snapshots | `scripts/health_monitor.py:probe_s16_pending_disappearance` |
| Detect TRUE live→historical demotion | s17 probe filters backfill rediscovery | `scripts/health_monitor.py:probe_s17_demotion_rate` |
| Single monitor_loop process | duplicates risk JSONL race + double-notification | operational hygiene; check `ps -ef \| grep monitor_loop` |
| n=10 early-warning reviews | pre-staged review files at n=9+1pending | `docs/reviews/r2_n10_<UTC>.md`, `r7sh_n10_<UTC>.md` |
| n=15 soft-verdict reviews | call game early on convergent signal | use `R2_REVIEW_TEMPLATE.md` at n=15 transition |
| Stop-aware paper journal | realistic risk-managed equity curves | `scripts/paper_journal_replay.py --stop-r 1.0` |
| Stop-aware R2 validation | honest verdict reflecting real stop logic | `scripts/r2_validation_report.py --stop-r 1.0` |

---

## 4. Forbidden acceleration methods

Any of these would invalidate the sample, reset the checkpoint clock, or violate standing instruction.

| forbidden method | reason |
|---|---|
| Lower fire thresholds (e.g. relax `RULE_DELTA_DOMINANCE_REAL`) | invalidates sample; threshold change must be done via env flag flip after n=30 verdict only |
| Shorten 12-bar horizon | outcome scoring contract; would silently change every existing settled outcome |
| Combine R1/R2/R7 counts | each rule has independent verdict; mixing dilutes signal and conceals weak rules |
| Promote R7 shadow before n=30 | shadow is research; production -0.50 is structurally protected |
| Retune R1 mid-sample | "rescue tuning" defeats purpose of out-of-sample test |
| Retrain models in `ml_engine/` | frozen during Phase 2A; any retrain restarts validation clock |
| Modify `ml_engine/` directory | out of scope per standing instruction |
| Add broker / live-trade execution | Phase 5 deferred, OUT OF SCOPE |
| Cross-asset port (NQ / CL / GC) | adds samples but mixes asset edges; finish ESM6 first |
| Change TF (e.g. 5m parallel) | rule engine is 15m-anchored; would need full recalibration |

---

## 5. R2 evidence-collection plan

R2 is the load-bearing positive signal. Project MVP rides on R2 verdict.

Current state: live n=9, mean_r=+0.955, hit=0.556, retention=1.27. **OK at every gate.**

Steps to verdict:
1. Wait for 10th live R2 fire → S13 trips → fill snapshot block in pre-staged `docs/reviews/r2_n10_<UTC>.md`
2. n=15 soft verdict (~3-7 days at 0.37 fires/day): if mean_r > 0 AND hit ≥ 0.50 AND retention ≥ 0.8 → declare **MVP-soft**
3. n=30 hard verdict (~9 weeks): final KEEP / REVERT decision
4. At every checkpoint, run `scripts/r2_validation_report.py --stop-r 1.0` for stop-aware view
5. Run `scripts/paper_journal_replay.py --rules r2_seller_up --stop-r 1.0` for equity curve
6. Watch for RTH coverage: currently RTH_mid 0/2 (small but 100% loss); escalate at n=15 if RTH continues underperforming

MVP-soft trigger: R2 PASS at n=15 with --stop-r 1.0 → declare R2-only MVP. Add Flask demo card showing R2 fires + paper equity curve.

---

## 6. R1 diagnosis plan

R1 live mean_r=-1.669 at n=6. Hit=0.667 (above floor). Direction: short. Trajectory likely WARN at n=10 trip.

Investigation steps (manual, no rule edit):
1. Read every R1 fire from outcomes JSONL; group by date
2. Check direction sanity (all 9 should be direction=-1)
3. Compute trend regime at each fire (4-bar prior close drift)
4. Look for clustering (>3 R1 fires same calendar day = regime trap signature)
5. Compare R1 historical retention 0.13 vs live retention -1.41 — both bad
6. At n=15 if mean_r still negative → fill `docs/reviews/r1_n15_<UTC>.md` from `R1_REVIEW_TEMPLATE.md`
7. At n=30 if still WARN → propose ABANDON-R1 verdict (stop counting toward Phase 2A; rule keeps firing in production but no longer tracked for verdict)

NO rule retune. NO threshold change. NO production behavior change. R1 diagnosis is read-only investigation only.

Future tool deferred (separate approval): `scripts/r1_investigation_report.py` — automate session split, regime classification, MFE/MAE distribution.

---

## 7. R7 futility-assessment plan

R7 shadow live mean_r=-3.559 at n=9. Hit=0.333. **Failing on every gate.** All 9 fires clustered on single date 2026-05-01 (trend trap). Production R7 at -0.50 is structurally untouched.

Pre-staged review at `docs/reviews/r7sh_n10_<UTC>.md`. Drafted verdict: **STAY-SHADOW**.

Futility assessment steps:
1. n=10 trip (R7 shadow 1 fire away): refresh snapshot row only; verdict stands
2. n=15 review: confirm STAY-SHADOW or escalate to STAY-SHADOW-INVESTIGATE if direction asymmetry persists
3. n=30 review: if mean_r remains negative AND CI lower < 0 → **ABANDON-SHADOW**
4. Future tool deferred (separate approval): `scripts/r7_shadow_vs_production_report.py` — confirms whether production -0.50 fired on the same trend-trap days (would indicate rule itself fragile vs threshold-only fragile)

NO promotion. Production R7 stays at -0.50 indefinitely. R7 shadow is research only; abandoning shadow does not affect production firing.

If R7 shadow ABANDONED at n=30:
- Stop tracking shadow outcomes (existing JSONL preserved, no new appends)
- Document final findings in PROJECT_STATE.md
- Optionally revisit in 6 months if regime changes

---

## 8. Required diversity checks

Verdict at n=10/n=15/n=30 is INVALID without diversity. Even strong mean_r can be regime-bound.

Checklist (apply at every checkpoint review):

| diversity dimension | minimum at n=15 | minimum at n=30 | what failure looks like |
|---|---|---|---|
| **calendar days** | ≥ 3 distinct dates | ≥ 5 distinct dates | all fires on one trend day |
| **sessions** | ≥ 2 of {ETH, RTH_open, RTH_mid, RTH_close} | ≥ 3 sessions | ETH-only sample |
| **direction** | both +1 and -1 if rule allows both | both directions confirmed | 100% short bias may indicate regime-bound or sign bug |
| **clustered fires** | no single day > 50% of total | no single day > 30% of total | 9-fire cluster like R7 shadow 2026-05-01 |
| **worst day** | min(daily_R) > -3 × |1R| | min(daily_R) > -5 × |1R| | one bad day overwhelms equity curve |

If diversity fails:
- Verdict = DEFER, not KEEP / REVERT
- Document specifically which diversity gate failed
- Plan to wait for additional samples that exercise missing dimension

---

## 9. Historical + live hybrid validation rule

Historical alone is INSUFFICIENT (in-sample by construction — thresholds calibrated on this data).
Live alone is INSUFFICIENT (sample too small early; cannot verify regime stability).

Hybrid rule (encoded in `scripts/r2_validation_report.py`):

```
PASS = ALL of:
  1. historical n ≥ 100 AND mean_r > 0 AND hit_rate ≥ 0.50
  2. live n ≥ 15 AND mean_r > 0 AND hit_rate ≥ 0.50
  3. both modes show same sign mean_r (no flip)
  4. historical max_dd ≤ 8R (with --stop-r 1.0)
  5. ≥ 2 sessions covered (combined hist + live)
  6. ≥ 5 live calendar dates (no single-day cluster)
  7. live MFE_med / |MAE_med| ≥ 1.0

WAIT = at least one n-related gate failing AND no quality gate failing
       (more samples expected to resolve)

FAIL = any quality gate failing (mean_r / hit / drawdown / mfe-mae)
       (regardless of n; problem won't fix itself with more samples)
```

When in doubt, run the report:
```bash
.venv/bin/python scripts/r2_validation_report.py --stop-r 1.0
```

---

## 10. Effective lambda checklist

"Effective lambda" = the rate at which the system actually generates VALID SAMPLES toward verdict. Five infrastructure prerequisites must hold for nominal fire rate to translate into checkpoint progress:

| check | verify how | failure symptom |
|---|---|---|
| **no missed fires** (Live SDK + monitor_loop both alive during open market) | `s3_live_sdk_stream` healthy AND `s7_monitor_loop_proc` shows exactly 1 PID | tape alerts age > 30s during open market; log gaps |
| **no pending disappearance** (re-discovery doesn't drop pending entries) | `s16_pending_disappearance` healthy | s16 unhealthy with `silent_losses` detail |
| **no live→historical demotion** (real-time capture working) | `s17_demotion_rate` healthy (true_demotion_ratio < 0.30) | s17 unhealthy with elevated true_demotion_ratio |
| **fresh realflow_history** (joined window extends near wall-clock) | `s14_realflow_backfill_log` healthy AND mtime < 8h | settle_pass returns 0 new settled even though pendings have past ETAs |
| **single monitor_loop** (no race on outcomes JSONL or pending JSON) | `ps -ef \| grep monitor_loop` shows exactly 1 PID | interleaved log entries; duplicate notifications |

Run all five before drawing any conclusion from a checkpoint snapshot:

```bash
# 1-line health summary
grep "_summary" outputs/order_flow/health_monitor.log | tail -1

# monitor_loop count (should be 1)
ps -ef | grep "monitor_loop" | grep -v grep | wc -l

# realflow_history mtime (should be < 8h old)
stat -f "%Sm" -t "%Y-%m-%dT%H:%M:%SZ" \
  order_flow_engine/data/processed/ESM6_15m_realflow_history.parquet
```

If any of the five fails, the checkpoint state may be misleading. Investigate operational issue before treating verdict numbers as actionable.

---

## Cross-references

- Operational scenarios → `docs/OPS_RUNBOOK.md`
- Live data freshness checks → `docs/DATA_QUALITY_CHECKLIST.md`
- System architecture → `docs/ARCHITECTURE.md`
- Phase 4 paper trading plan → `docs/PAPER_TRADING_PLAN.md`
- Per-rule review process → `docs/SIGNAL_REVIEW_PLAYBOOKS.md`
- Per-rule review templates → `docs/reviews/templates/`
- Live decision snapshot → `PROJECT_STATE.md`

---

## Standing instruction (reinforced)

This document does not authorize any rule / threshold / model / ml_engine / outcome scoring / horizon / R7 promotion / trading behavior change. Acceleration via observation reliability and earlier soft verdicts is allowed. Acceleration via signal-logic changes is forbidden. Honest negative result is acceptable; rescue tuning is not.
