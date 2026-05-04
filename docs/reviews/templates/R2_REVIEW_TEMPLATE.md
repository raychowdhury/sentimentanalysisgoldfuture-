# R2 (fade seller dominance) — Checkpoint Review

> **Template — copy and fill at each R2 checkpoint transition.**
> Save filled copy as `docs/reviews/r2_n<level>_<UTC>.md`.

---

## Header

| field | value |
|---|---|
| rule | r2_seller_up |
| direction bias | long |
| baseline mean_r | 0.75 |
| checkpoint level (n=10 / n=15 / n=30) | <FILL> |
| triggered_by | <S13 notification text or manual> |
| trigger UTC | <YYYY-MM-DDTHH:MM:SSZ> |
| reviewer | <name> |
| current Flask uptime | <hours> |
| current live SDK status | <connected / silent> |

---

## Snapshot

Pull from `outputs/order_flow/.live_checkpoint_state.json` and
`realflow_outcomes_summary_ESM6_15m.json`:

| metric | value |
|---|---|
| live n | <FILL> |
| live wins / losses / flats | <W / L / F> |
| live hit_rate | <FILL> |
| live mean_r | <FILL> |
| live retention vs 0.75 | <FILL> |
| historical n | <FILL> |
| historical mean_r | <FILL> |
| historical retention | 1.17 (known strong) |
| live alerts/day | <FILL> |
| sample window | <fire_ts_min> → <fire_ts_max> |

### Session breakdown (live only)

| session | n | wins | hit_rate | mean_r |
|---|---|---|---|---|
| ETH | | | | |
| RTH_open | | | | |
| RTH_mid | | | | |
| RTH_close | | | | |

---

## Per-fire detail (all live R2 fires this checkpoint)

| fire (NY) | session | entry | stop | target | MFE | MAE | final R | outcome |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

---

## Diagnostic checklist

```
☐ entry / stop / target reasonable for ATR at fire time
☐ MFE in R — did price ever reach 1R favourable
☐ MAE in R — stopped out 1R adverse?
☐ direction sign correct (R2 = long)
☐ session distribution: ETH-heavy or balanced?
☐ cluster: adjacent fires same direction outcome?
☐ regime at fires: low-ATR / normal / high-ATR / news-driven
☐ outliers: any single fire > 3σ from mean?
☐ any HEALTH_*.flag during sample window?
☐ Live SDK uptime % during sample window
```

---

## R2-specific failure modes (check explicitly)

```
☐ Symmetric break — does R2 decay correlate with R1 recovery (regime flip)?
☐ Session dependence — ETH-heavy sample may not generalize to RTH
☐ Threshold over-fit — RULE_ABSORPTION_DELTA_REAL=0.20 too tight on live?
☐ Trend chase — R2 fires during sustained sell-offs that don't reverse?
☐ Volume reconciliation — losses align with high vol_match mismatch bars?
☐ ATR regime — low-ATR sessions where 1R stop is too tight?
```

R2 historical retention is 1.17 (strong). Default expectation is OK at every
checkpoint. WARN should trigger urgent investigation rather than wait-and-see.

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

R2 from OK → WARN = regression. Document immediately, do not wait for n=30
to investigate.

---

## Statistical sanity (n=30 only)

```
☐ 95% CI on mean_r (bootstrap on 30 R values)
☐ CI lower bound:    <FILL>
☐ Is CI lower bound > 0?  yes / no
☐ Time-decay check: rolling 10-bar mean_r trend (up / flat / down)
☐ Compare CI to historical baseline 0.75 — overlapping or diverging?
```

---

## Decision

Pick one:

| option | when to choose |
|---|---|
| ☐ KEEP | CI lower > 0 AND retention ≥ 0.8 AND hit ≥ 0.55 |
| ☐ KEEP-WITH-CAVEAT | CI lower > 0 BUT retention 0.5-0.8; monitor at n=60 |
| ☐ REVERT | mean_r ≤ 0 — propose `OF_REAL_THRESHOLDS_ENABLED=0` (manual env edit) |
| ☐ RETUNE | one session breaking it; document but do NOT change config at n=30 |
| ☐ INVESTIGATE | ambiguous — propose Phase 2C iteration |

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
n=10 / n=15:
  ☐ Do NOT change thresholds
  ☐ Do NOT change rules
  ☐ Do NOT pause R2 production firing
  ☐ Do NOT auto-revert

n=30:
  ☐ Do NOT auto-revert (env flag flip is manual)
  ☐ Do NOT change R2 thresholds individually (revert is all-or-nothing)
  ☐ Do NOT promote R7 shadow (separate review)
  ☐ Do NOT cross-asset port before recording verdict
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
| n=10 WARN | n=15 in ~2 days | regression — investigate immediately |
| n=15 OK convergent | n=30 in ~5 days | prepare verdict template |
| n=30 KEEP | n=60 in ~10 days | continue monitoring |
| n=30 REVERT | post-revert n=10 baseline | observe proxy-mode outcomes |

---

_Filled by reviewer at checkpoint transition. No code changes are made by
filling this file. Decision implementation is separate per standing
instruction (manual env edit only)._
