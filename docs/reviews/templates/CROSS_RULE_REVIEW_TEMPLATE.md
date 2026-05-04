# Cross-Rule Multi-WARN — Review

> **Template — copy and fill when 2+ rules WARN at the same checkpoint.**
> Save filled copy as `docs/reviews/cross_rule_<UTC>.md`.

> Used when checkpoint state shows simultaneous WARN across two or more of
> {R1, R2, R7 shadow}. Designed to disambiguate single-cause vs multi-cause
> decay.

---

## Header

| field | value |
|---|---|
| trigger UTC | <YYYY-MM-DDTHH:MM:SSZ> |
| reviewer | <name> |
| rules WARN | <e.g. R1@n10, R7sh@n10> |
| checkpoint level(s) | <e.g. R1=n10, R7sh=n10> |
| sample window | <UTC range> |

---

## State at trigger

| rule | n | mean_r | hit_rate | retention | status@checkpoint |
|---|---|---|---|---|---|
| R1 (1.18 baseline) | | | | | |
| R2 (0.75 baseline) | | | | | |
| R7 shadow (0.7135 baseline) | | | | | |

---

## Hypothesis tree — pick one before deciding

```
1. SINGLE-REGIME hypothesis
   Losses are concentrated in one calendar window (one trend day, one
   news-driven session, one liquidity event).
   → expectation: per-rule sub-period analysis shows healthy mean_r in
     OTHER windows.

2. COMMON-MODE hypothesis
   A data-quality / ops issue affected the whole sample (Live SDK
   silence, bad parquet, raw cache stale, ingest gap).
   → expectation: HEALTH_*.flag entries OR cache_refresh.log gaps OR Live
     SDK gaps coincide with the sample window.

3. CALIBRATION DRIFT hypothesis
   `bar_proxy_mode` flipped unexpectedly during sample, mixing real and
   proxy paths.
   → expectation: realflow_diagnostic shows non-zero bar_proxy_mode count
     during the window when it should be 0.

4. REGIME-CHANGE hypothesis
   The forward-tradability of these rules is genuinely worse now than
   during calibration. No specific data fault.
   → expectation: per-rule sub-period analysis shows uniformly weaker
     mean_r across all sub-windows.

5. STATISTICAL NOISE hypothesis
   At small n, multiple rules going WARN simultaneously is within
   chance.
   → expectation: CI lower bounds overlap zero; n is small (n=10 or n=15);
     no other corroborating evidence.
```

**Selected hypothesis:** <FILL>

---

## Evidence checklist

```
☐ HEALTH flags raised during sample window?
   (list any HEALTH_*.flag files that overlapped)
   <FILL>

☐ Live SDK silence > 5 min during sample?
   (grep TAPE ALERT timestamps; identify gaps)
   <FILL>

☐ Cache refresh failures during sample?
   (grep cache_refresh.log for FAIL: entries)
   <FILL>

☐ Volume reconciliation flags during sample?
   (vol_match_mismatch_5pct rate during window)
   <FILL>

☐ bar_proxy_mode integrity during sample?
   (joined frame's bar_proxy_mode column should be 0 throughout)
   <FILL>

☐ Calendar overlap with known events?
   (FOMC, NFP, CPI, daily settlement, monthly expiry, quarterly roll)
   <FILL>

☐ Per-rule sub-period analysis
   (split sample window in halves or thirds; mean_r per rule per
   sub-period — uniform decay or one bad chunk?)
   <FILL>

☐ Cross-rule directional symmetry
   (R1 short losses paired with R2 long losses → trend regime in opposite
   directions; suggests regime hypothesis. R1 short losses paired with R2
   long wins → no symmetry; suggests single-rule defect.)
   <FILL>
```

---

## Per-rule fire timing alignment

Use this table to spot temporal clusters:

| time bucket (1h) | R1 fires | R1 wins | R2 fires | R2 wins | R7sh fires | R7sh wins |
|---|---|---|---|---|---|---|
| | | | | | | |

If multi-rule WARN concentrates in one or two buckets → SINGLE-REGIME.
If spread evenly → REGIME-CHANGE or NOISE.

---

## Decision

Pick one (matrix maps hypothesis → action):

| hypothesis | recommended action |
|---|---|
| SINGLE-REGIME | annotate session/event, continue monitoring; do not change config |
| COMMON-MODE | reset checkpoint counters for affected rules (manual JSONL trim is OUT-OF-SCOPE; instead document the contamination, exclude in next-checkpoint analysis) |
| CALIBRATION-DRIFT | escalate to engineering — `bar_proxy_mode` flip is a defect; no thresholds touched until repaired |
| REGIME-CHANGE | proceed to per-rule n=30 review; multi-rule decay is the most concerning, may require revert across all rules |
| STATISTICAL NOISE | wait for n=15 / n=30; do not act |

**Selected action:** <FILL>

**Rationale:** <FILL — 3-5 sentences>

---

## Action items (post-decision)

| item | owner | due |
|---|---|---|
| | | |

---

## What NOT to do regardless of hypothesis

```
☐ Do NOT change rules at any cross-rule checkpoint
☐ Do NOT change thresholds at any cross-rule checkpoint
☐ Do NOT auto-revert
☐ Do NOT trim or rewrite outcomes JSONL files
☐ Do NOT change RULE_CVD_CORR_THRESH (R7 production)
☐ Do NOT promote R7 shadow to production
☐ Do NOT add cross-asset symbols (still ESM6 only)
☐ Do NOT modify monitor_loop / health_monitor / cache_refresh
☐ Do NOT skip individual per-rule reviews; this template SUPPLEMENTS them,
  it does not replace them
```

---

## Open questions raised

```
1.
2.
3.
```

---

## Follow-ups

| follow-up | trigger condition |
|---|---|
| individual R1 review at n=10 | per R1 template |
| individual R2 review at n=10 | per R2 template |
| individual R7 shadow review at n=10 | per R7 shadow template |
| Phase 2C direction-sign test (R7 shadow) | if hypothesis = CALIBRATION-DRIFT or persistent R7sh WARN |
| Live SDK persistence audit | if hypothesis = COMMON-MODE |
| Volume reconciliation deep-dive | if vol_match_mismatch correlates with losses |

---

_Filled by reviewer when 2+ rules WARN simultaneously. Supplements
individual rule templates. No code changes. Decision implementation is
separate per standing instruction._
