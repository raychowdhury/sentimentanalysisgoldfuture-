# NEXT_ACTIONS — RFM ES Real-Flow Monitor

_Last updated: 2026-05-01 UTC_

Short list of open work. User decides; assistant proposes.

## Currently waiting on

- **R1 / R2 live ≥ 30 each** to evaluate Phase 2A live verdict.
- **R7 shadow live ≥ 30** to evaluate Phase 2B Stage 2 readiness.
- **Live SDK recovery** from Databento 422 errors (unblocks live accumulation).

## Open proposals (need user approval)

### Live SDK 422 recovery
- Wait & retry — Databento `data_schema_not_fully_available` typically transient.
- Optional: set raw symbol pin in `.env` to bypass parent-symbol resolution. Already partially configured.
- No code change required.

### Phase 2D Stage 3 (decision logic)
- Out of scope. Manual revert remains: `OF_REAL_THRESHOLDS_ENABLED=0`.

### Phase 2B Stage 2 (R7 dashboard surface)
- Adds `GET /api/order-flow/realflow-r7-shadow` endpoint and a "R7 Shadow (DIAGNOSTIC ONLY)" card.
- Pre-condition: shadow live ≥ 30 fires.

### Phase 2C (R7 direction-sign investigation)
- Test inverted direction (`-sign(cvd_slope)`) or alternative direction proxies.
- New diagnostic file. Not approved.

### EC2 deployment Stage 2 (systemd)
- Promote tmux session to systemd units. Not approved yet.

## Recently closed

| date | item |
|------|------|
| 2026-05-01 | R7 shadow Stage 1 (harness + tests + monitor hook) |
| 2026-05-01 | Local server-side monitor loop |
| 2026-05-01 | EC2 deployment plan + checklist (tmux only) |
| 2026-05-01 | UI wording — "fires / target" |
| 2026-05-01 | Outcome tracker mode tagging (historical/live) |
| 2026-04-30 | 18-day historical backfill |
| 2026-04-30 | Phase 2A R1/R2/R3-R6 thresholds promoted |
| 2026-04-30 | Phase 2D Stage 1+2 (tracker + dashboard surface) |
| 2026-04-30 | Phase 2B Stage 1 sweep (deferred initially, re-swept) |
| 2026-04-30 | Smart Regenerate button + auto-regen toggle |

## Hard rules

- No detector behavior changes without explicit approval.
- No `ml_engine/` edits.
- No model retraining.
- No trades.
- No auto-revert / auto-promote.
- Tracker is read+append-only.
- Shadow constants stay out of `config.py`.
