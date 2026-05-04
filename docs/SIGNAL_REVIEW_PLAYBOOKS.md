# SIGNAL_REVIEW_PLAYBOOKS

**Status:** PLANNING DOCUMENT ONLY. Reference for repeatable manual review at
each live checkpoint (n=10, n=15, n=30).

This document does not change system behavior. It defines the steps the
reviewer (user) takes when checkpoint transitions fire.

---

## Purpose

Three separate live streams (R1 buyer-down, R2 seller-up, R7 shadow CVD
divergence) each cross three checkpoints (n≥10, n≥15, n≥30). Without a
playbook, decisions drift. Without a record of what was decided, future
reviews repeat past mistakes.

These playbooks are:

- **Repeatable** — same checklist every checkpoint
- **Read-only on the engine** — review affects future decisions, not running code
- **Bounded** — each checkpoint has explicit "what NOT to decide here"
- **Recorded** — write findings to `docs/reviews/<rule>_n<level>_<UTC>.md` (reviewer creates)

---

## Common diagnostic checklist

For every fire being reviewed:

```
☐ entry / stop / target reasonable for ATR at fire time?
☐ MFE in R — did price ever reach 1R favourable?
☐ MAE in R — did price ever reach 1R adverse (stop-out)?
☐ session: ETH / RTH_open / RTH_mid / RTH_close
☐ direction sign: long for R2-seller-up, short for R1-buyer-down (sanity)
☐ outcome: win / loss / flat
☐ cluster: did adjacent fires (within 30 min) all go the same way?
☐ regime at fire: low-ATR / normal / high-ATR / news-driven?
```

For aggregate review at n=10/15/30:

```
☐ live n vs target threshold
☐ live mean_r vs baseline (R1=1.18, R2=0.75, R7sh=0.7135)
☐ live retention pct
☐ live hit_rate
☐ live vs historical mean_r — convergent or divergent?
☐ session distribution (live ETH heavy?)
☐ R1/R2 directional skew (one side losing, other winning?)
☐ uptime fraction during sample window
☐ outliers: any single fire > 3σ from mean?
```

---

## n=10 playbook — Early Warning

### Trigger

`<rule>@n10` cell flips from `NOT_REACHED` → `OK` or `WARN`.

### Inspect

1. Pull the 10 settled live fires for the rule:
   ```bash
   .venv/bin/python -c "
   import json
   recs=[json.loads(l) for l in open('outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl')]
   live=[r for r in recs if r['mode']=='live' and r['rule']=='r1_buyer_down']
   for r in live: print(r['fire_ts_ny'], r['session'], r['outcome'], r['fwd_r_signed'])
   "
   ```
2. Read the per-fire detail in the dashboard's Live Signal Trade Details card.
3. Cluster check: are losses bunched in a single regime hour (e.g. all in 06:00-08:00Z)?

### Decision options

| status | action |
|---|---|
| OK | continue, no change |
| WARN — clustered loss in one session | flag the session, reduce expectation for that session at next checkpoint |
| WARN — distributed, mean_r negative | escalate to investigate (do NOT change thresholds yet) |
| WARN — single outlier dragging mean | annotate, continue |

### Do NOT

- Do NOT change thresholds at n=10 (n is too small)
- Do NOT change rules
- Do NOT pause R1/R2 production firing
- Do NOT promote R7 shadow

### Record

`docs/reviews/<rule>_n10_<UTC>.md` with checklist + decision rationale.

---

## n=15 playbook — Soft Decision

### Trigger

`<rule>@n15` cell flips from `NOT_REACHED` → `OK` or `WARN`.

### Inspect

Repeat n=10 inspection PLUS:

1. Convergence check:
   - Did the n=11 to n=15 fires (the new 5) trend in the same direction as the n=10 baseline?
   - Did mean_r stabilize or drift further?
2. Cumulative session bias:
   - If ETH dominates, what's ETH-only mean_r?
3. Retention trend across n: did 0.5 (n=10) drift to 0.4 (n=15) or hold?

### Decision options

| condition at n=15 | recommended action |
|---|---|
| OK and matches n=10 | continue, expect n=30 verdict to confirm |
| WARN at n=15 BUT was OK at n=10 | regression — investigate, write findings |
| WARN at n=15 AND was WARN at n=10 | strong signal of decay; document, prepare n=30 reversal proposal |
| OK at n=15 BUT was WARN at n=10 | recovery — annotate, continue cautiously |

### Do NOT

- Do NOT revert thresholds at n=15
- Do NOT auto-act
- Do NOT change rules
- Do NOT promote R7 shadow even if it's OK at n=15

### Record

`docs/reviews/<rule>_n15_<UTC>.md`. Compare side-by-side to the n=10 entry.

---

## n=30 playbook — First Verdict

### Trigger

`<rule>@n30` cell flips from `NOT_REACHED` → `OK` or `WARN`.

### Inspect

Repeat n=15 inspection PLUS:

1. **Statistical significance check:**
   - 95% CI on mean_r (use bootstrap on the 30 R values)
   - Is CI lower bound > 0?
2. **Retention vs baseline:**
   - retention ≥ 0.8 → strong evidence of forward validity
   - 0.5 ≤ retention < 0.8 → ambiguous, more sample needed
   - retention < 0.5 → decay or regime break
3. **Session breakdown:**
   - Does any one session drag the aggregate?
   - Per-session hit_rate, mean_r, n
4. **Time-decay check:**
   - Plot retention over rolling 10-bar windows. Trending up, flat, or down?

### Decision tree

```
if CI lower bound > 0 AND retention ≥ 0.8:
    KEEP — rule validates, continue Phase 2A path
elif CI lower bound > 0 AND retention 0.5-0.8:
    KEEP-WITH-CAVEAT — document, monitor for regression at n=60
elif mean_r ≤ 0:
    REVERT — set OF_REAL_THRESHOLDS_ENABLED=0 (manual env edit, not auto)
elif single session is breaking it:
    RETUNE — propose session filter (planning only, NOT implemented at n=30 alone)
else:
    INVESTIGATE — write findings, propose Phase 2C iteration (sign-flip test, etc.)
```

### Do NOT

- Do NOT auto-revert (always manual env flag flip)
- Do NOT change R1/R2 thresholds (keep current 0.04 / 0.20 / 0.12 unless reverting all)
- Do NOT promote R7 shadow even at n=30 — that's a separate Phase 2B Stage 2 review
- Do NOT cross-asset port (GCM6, etc.) before recording verdict

### Record

`docs/reviews/<rule>_n30_<UTC>.md` with:

- final n
- mean_r ± CI
- retention
- session breakdown table
- decision (KEEP / REVERT / RETUNE / INVESTIGATE)
- next-checkpoint plan (n=60 monitor? abandon?)
- timestamp + reviewer name

---

## R1-specific notes — fade buyer dominance (short bias)

| dimension | observation as of write date |
|---|---|
| historical retention | 0.07 (mean_r +0.085 vs baseline 1.18 — almost zero edge) |
| hit_rate | 0.55 |
| live n=5 trend | mean_r -2.34 — already negative, trending WARN |

**Risk:** the historical retention is so weak that even an OK verdict at n=30
may not be tradable. Forward expectancy needs to be positive after
transaction costs / slippage. R1 may be the rule to revert first.

**Specific failure modes to check at any n:**

- Late fills: does R1 mostly fire near intra-bar peaks where reversal is
  already exhausted?
- Trend regime: does R1 lose specifically during persistent up-trends?
- News reaction: is there clustering near 8:30 NY ET data prints?

---

## R2-specific notes — fade seller dominance (long bias)

| dimension | observation as of write date |
|---|---|
| historical retention | 1.17 (mean_r +0.88, hit 0.66) — strong baseline |
| hit_rate | 0.66 |
| live n=7 trend | mean_r +1.09 — trending OK |

**Risk:** R2 looks healthy. Watch for regression in n=15 / n=30.

**Specific failure modes to check at any n:**

- Symmetric break: if R2 starts decaying, does it correlate with R1 recovery
  (regime flip)?
- Session dependence: ETH-heavy sample may not generalize to RTH.

---

## R7 shadow specific — CVD divergence at -0.20

| dimension | observation as of write date |
|---|---|
| historical retention vs shadow baseline | 1.02 |
| live n=9 trend | mean_r -3.56 hit 0.33 — trending WARN |

**Risk:** R7 shadow is failing badly on live. If n=30 confirms, do NOT
promote -0.20 to production. Production R7 stays at -0.50.

**Specific failure modes to check:**

- Direction-sign bug: is `sign(cvd_slope)` correct? Phase 2C deferred work
  candidate.
- Cluster on a single trend day: are the 9 losses bunched in a single trend?

---

## Cross-rule playbook — when multiple rules WARN simultaneously

If 2+ rules WARN at the same checkpoint:

1. Single regime hypothesis: are losses concentrated in one calendar window?
   (e.g. one trend day where everything counter-trend bled)
2. Common-mode hypothesis: a Live SDK / data quality issue that affected the
   whole period? Check health monitor logs for stale flags.
3. Calibration drift: did `bar_proxy_mode` flip unexpectedly during sample?
4. If neither: regime change is real, both rules need separate investigation.

Decision rule: if multi-rule WARN coincides with a known data outage, reset
counters and re-collect. Otherwise treat each rule independently.

---

## Where to record reviews

```
docs/reviews/r1_n10_<UTC>.md
docs/reviews/r1_n15_<UTC>.md
docs/reviews/r1_n30_<UTC>.md
docs/reviews/r2_n10_<UTC>.md
... (etc)
docs/reviews/r7sh_n10_<UTC>.md
... (etc)
```

Reviewer creates these files at each checkpoint transition. They are not
auto-generated by code. Health monitor's S13 probe fires the macOS
notification — the reviewer responds by writing the markdown review.

Template each review file:

```markdown
# <rule> @ n<level> — review

**Generated:** <UTC>
**Reviewer:** <name>
**Trigger:** S13 notification "<rule> n<level> WARN/OK"

## Snapshot
- live n:
- mean_r:
- retention:
- hit_rate:
- session breakdown:

## Diagnostic checklist
... (use the checklist above)

## Decision
- action: KEEP / WARN / REVERT / RETUNE / INVESTIGATE
- rationale:
- next checkpoint:

## Open questions
...
```

---

## Discipline rules

1. **Always write the review file** at every n=10 / n=15 / n=30 transition,
   even if the verdict is "no action".
2. **Never skip a checkpoint** because the previous one was OK.
3. **Never act between checkpoints** without a written review backing the
   action.
4. **Never change thresholds at n=10 or n=15** — only n=30 unlocks
   threshold-level decisions.
5. **Never promote R7 shadow at any individual checkpoint** — promotion is
   a separate Phase 2B Stage 2 process with its own review.

---

_This document is read-only research planning. No code is changed by writing
this file. Update when checkpoint review process evolves. Reverse by deleting._
