# R7 Shadow (CVD divergence at -0.20) — Checkpoint Review

> **Template — copy and fill at each R7 shadow checkpoint transition.**
> Save filled copy as `docs/reviews/r7sh_n<level>_<UTC>.md`.

> R7 shadow is **shadow-only**. Production R7 stays at `RULE_CVD_CORR_THRESH=-0.50`.
> Shadow runs at -0.20 to test whether a looser threshold would be tradable.

---

## Header

| field | value |
|---|---|
| rule | r7_cvd_divergence_shadow |
| direction bias | direction = sign(cvd_slope) at fire bar |
| shadow threshold | -0.20 |
| production threshold | -0.50 (UNCHANGED — never auto-promoted) |
| baseline mean_r | 0.7135 |
| checkpoint level (n=10 / n=15 / n=30) | <FILL> |
| triggered_by | <S13 notification text or manual> |
| trigger UTC | <YYYY-MM-DDTHH:MM:SSZ> |
| reviewer | <name> |
| current Flask uptime | <hours> |

---

## Snapshot

Pull from `outputs/order_flow/.live_checkpoint_state.json` and
`realflow_r7_shadow_summary_ESM6_15m.json`:

| metric | value |
|---|---|
| live n | <FILL> |
| live wins / losses / flats | <W / L / F> |
| live hit_rate | <FILL> |
| live mean_r | <FILL> |
| live retention vs 0.7135 | <FILL> |
| historical n | <FILL> |
| historical mean_r | <FILL> |
| historical retention | 1.02 (strong shadow baseline) |
| shadow alerts/day | <FILL> |
| sample window | <fire_ts_min> → <fire_ts_max> |

### Direction breakdown (live only)

| direction | n | wins | hit_rate | mean_r |
|---|---|---|---|---|
| short (cvd slope down) | | | | |
| long (cvd slope up) | | | | |

If one direction dominates losses → **direction-sign bug** candidate (Phase 2C).

### Session breakdown (live only)

| session | n | wins | hit_rate | mean_r |
|---|---|---|---|---|
| ETH | | | | |
| RTH_open | | | | |
| RTH_mid | | | | |
| RTH_close | | | | |

---

## Per-fire detail (all live shadow fires this checkpoint)

| fire (NY) | session | dir | entry | stop | target | MFE | MAE | final R | outcome |
|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | |

---

## Diagnostic checklist

```
☐ entry / stop / target reasonable for ATR at fire time
☐ MFE in R — did price ever reach 1R favourable
☐ MAE in R — stopped out 1R adverse?
☐ direction sign matches sign(cvd_slope)?
☐ session distribution: ETH-heavy or balanced?
☐ cluster: adjacent fires same direction outcome (regime trap)?
☐ regime at fires: low-ATR / normal / high-ATR / news-driven
☐ outliers: any single fire > 3σ from mean?
☐ any HEALTH_*.flag during sample window?
☐ Live SDK uptime % during sample window
```

---

## R7 shadow specific failure modes (check explicitly)

```
☐ Direction-sign bug — should fade-the-divergence be opposite sign?
   (Phase 2C deferred work candidate)
☐ Cluster on a single trend day — all losses bunched in one regime?
☐ Window too short — RULE_CVD_CORR_WINDOW=20 may not capture divergence
   in higher-ATR regimes
☐ Threshold inversion — does -0.20 fire too early (before divergence resolves)?
☐ Production drift — has anyone manually overridden production -0.50? (No,
   per standing instruction; verify config)
☐ Volume reconciliation — losses correlate with vol_match mismatch?
```

R7 shadow LIVE is failing on small sample (mean_r negative). Default
expectation: stay shadow, do NOT promote. Promotion is a SEPARATE process
beyond this checkpoint review (Phase 2B Stage 2).

---

## Convergence vs prior checkpoint

(For n=15 / n=30 only — skip at n=10)

| metric | n=10 | n=15 | n=30 |
|---|---|---|---|
| n | | | |
| mean_r | | | |
| hit_rate | | | |
| retention | | | |
| WARN status | | | |

If WARN persists across n=10 → n=15 → n=30, the -0.20 hypothesis is
empirically rejected on live data. Stay at production -0.50.

---

## Statistical sanity (n=30 only)

```
☐ 95% CI on mean_r (bootstrap on 30 R values)
☐ CI lower bound:    <FILL>
☐ Is CI lower bound > 0?  yes / no
☐ Time-decay check: rolling 10-bar mean_r trend (up / flat / down)
☐ Direction-sign sub-analysis: split by short vs long; do both sides confirm?
```

---

## Decision

R7 shadow has its own decision space (different from R1/R2):

| option | when to choose |
|---|---|
| ☐ STAY-SHADOW | default — do not promote, keep production -0.50, continue shadow |
| ☐ STAY-SHADOW-INVESTIGATE | persistent WARN, propose Phase 2C direction-sign test |
| ☐ ABANDON-SHADOW | shadow demonstrably negative on n ≥ 30 with CI lower < 0 — stop shadow tracking |
| ☐ PROPOSE-PROMOTION | CI lower > 0 AND retention ≥ 1.0 AND hit ≥ 0.6 — schedule Phase 2B Stage 2 review (NOT auto-promote) |

**Selected:** <FILL>

**Rationale:** <FILL — 2-4 sentences>

---

## Action items (post-decision)

| item | owner | due |
|---|---|---|
| | | |

---

## What NOT to do at this checkpoint

```
At every checkpoint (n=10 / n=15 / n=30):
  ☐ Do NOT change RULE_CVD_CORR_THRESH (production stays at -0.50)
  ☐ Do NOT change RULE_CVD_CORR_THRESH_REAL_SHADOW (shadow stays at -0.20)
  ☐ Do NOT add the shadow constant to config.py
  ☐ Do NOT auto-promote
  ☐ Do NOT change R7 production threshold based on shadow data alone
  ☐ Do NOT cross-asset port shadow

At n=30:
  ☐ Even with PROPOSE-PROMOTION verdict, run a separate Phase 2B Stage 2
    review before any threshold change to production
```

---

## Open questions raised

```
1.
2.
3.
```

---

## Next checkpoint plan

| if current | next checkpoint | expected action |
|---|---|---|
| n=10 OK | n=15 in ~2 days | re-review |
| n=10 WARN | n=15 in ~2 days | investigate, document |
| n=15 OK convergent | n=30 in ~5 days | re-review with promotion-or-stay framing |
| n=15 WARN | n=30 in ~5 days | shadow rejection becoming likely |
| n=30 STAY-SHADOW | n=60 monitor | continue |
| n=30 ABANDON-SHADOW | stop tracking | propose Phase 2C investigation |
| n=30 PROPOSE-PROMOTION | Phase 2B Stage 2 review | separate process |

---

_Filled by reviewer at checkpoint transition. No code changes are made by
filling this file. Decision implementation (any threshold change) is
explicitly out of scope per standing instruction._
