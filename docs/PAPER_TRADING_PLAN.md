# PAPER_TRADING_PLAN

**Status:** PLANNING DOCUMENT ONLY. Not active. No code, no broker, no execution.

This document scopes the future paper-trading phase for the RFM ES Real-Flow
Monitor. It does not authorize any change to the running system. Phases 4
work is gated behind explicit approval and the live-checkpoint verdicts at
n=10 / n=15 / n=30.

---

## 1. Goal

Validate that real-flow rules (R1, R2, R7) produce a positive expectancy in
forward time, **simulated only**. Catch decay or regime breaks before any
real-capital decision. Build a journal of paper trades that mirrors the
existing outcome tracker but with explicit position sizing and P&L.

Paper trading is a **research activity**, not a production system. It is the
bridge between:

- **Phase 2A/2D (current)**: outcome tracker counts +R / -R per rule fire
- **Phase 5 (future, deferred)**: real capital execution

Paper trading runs entirely on the local machine, against historical and
live-resampled bars. No external broker, no FIX, no order routing.

## 2. Preconditions (gating triggers)

Paper trading does not begin until ALL of the following are true:

1. R1 live settled ≥ 30 with checkpoint status @n30 = OK or WARN-but-positive-mean_r
2. R2 live settled ≥ 30 with same condition
3. R7 shadow live settled ≥ 30 (verdict: keep production -0.50 or research -0.20)
4. Volume reconciliation gate: warning only is acceptable
5. Live tail SDK uptime ≥ 95% over trailing 14 days
6. Health monitor: all critical probes (S1, S6, S7) consistently healthy
7. User explicit approval (per-trigger sign-off, not blanket)

Any single missing precondition blocks Phase 4 start. Reviewer is the user.

## 3. Scope: simulated only

Hard scope rules:

- No broker connection (IBKR, Tradestation, etc.) — explicit out of scope.
- No FIX, no REST broker order, no live execution.
- All "fills" are synthetic at the bar's close price at fire time.
- Forward P&L is the existing outcome tracker's `fwd_r_signed`, plus a
  per-trade $ allocation (TBD).
- No external market-impact model.

The paper system is functionally a journal layered on top of the existing
outcome tracker. It adds position sizing and aggregated P&L; it does not
add new market interaction.

## 4. Position sizing (placeholders)

```
Per-trade risk:                TBD
Default planning assumption:   fixed 1R per paper trade
                               (R = 1 × ATR, position = entry ± 1×ATR stop)
Daily R cap:                   TBD  (suggestion: 3-5R, never exceed 1× per-trade)
Per-rule R cap:                TBD  (suggestion: 2R per rule per session)
Account currency:              USD (placeholder, since ESM6 = $50/point)
Notional per trade:            TBD  (depends on user-chosen risk per R)
```

Reviewer fills in real values when Phase 4 is approved. Until then, the doc
treats sizing as a research parameter, not a production setting.

## 5. Entry / stop / target rules

Already encoded in the rule engine. Paper trading reuses them verbatim:

- **Entry:** bar close at fire timestamp (no slippage modeled in Phase 4A)
- **Stop:** entry ± 1 × ATR opposite to direction (per outcome tracker)
- **Target:** entry ± 1 × ATR with direction (1R take-profit reference)
- **Horizon:** 12 × 15m = 3 hours forward
- **Outcome:** existing scoring (`fwd_r_signed`, `mae_r`, `mfe_r`, `outcome`,
  `hit_1r`, `stopped_out_1atr`)

Phase 4 does not modify any of these. It consumes the existing outcome rows.

## 6. Settlement & P&L

Paper P&L per trade:

```
paper_r        = fwd_r_signed              (already computed)
paper_dollars  = paper_r × $_per_R         (TBD per Phase 4 approval)
trade_outcome  = outcome (win/loss/flat)   (already computed)
```

No new computation. Just multiplication by a chosen per-R dollar amount.

## 7. Tracking files (new, append-only)

Phase 4A would add:

```
outputs/paper/paper_journal.jsonl        — append-only per-trade record
outputs/paper/paper_daily.json           — daily aggregate (R, $)
outputs/paper/paper_session.json         — by-session aggregate
outputs/paper/paper_summary.md           — Markdown daily summary
```

All files read+append-only. No mutation of the engine's outcome tracker.
Existing files stay where they are:

- `outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl` (UNCHANGED, source-of-truth)
- `outputs/order_flow/paper_orders.jsonl` (already exists for Trader Desk; not reused here unless explicit approval)

## 8. Phased rollout

| phase | scope | risk |
|---|---|---|
| **4A** | manual journaling: read outcomes JSONL, multiply by 1R, write paper_journal.jsonl | very low |
| **4B** | auto-fill on signal close: subscribe to outcome tracker settle events | low |
| **4C** | sizing engine: per-trade R cap, daily R cap, per-rule cap | low |
| **4D** | drawdown gate: auto-pause paper trading if drawdown exceeds X | low |
| **4E** | reporting: emit per-day paper P&L summary to its own Markdown/JSON file | very low |

Each phase ships independently. None auto-promotes to the next.

## 9. Boundaries — do NOT do

| boundary | reason |
|---|---|
| no IBKR / Tradestation / broker | Phase 4 scope explicitly excludes |
| no FIX / order routing | Phase 4 scope explicitly excludes |
| no auto-execute | manual approval per phase |
| no cross-asset (gold / oil / NQ) | finish ESM6 verdict first |
| no sub-15m TF | rule engine is 15m-anchored |
| no leverage adjustment | not in research scope |
| no rule / threshold edits to "improve" paper P&L | violates research isolation |
| no model retraining | out of scope per standing instruction |
| no market-impact / slippage model | Phase 4A is naive close-price fills |
| no real capital | end of Phase 4 ≠ start of Phase 5 |

## 10. Promotion criteria — Phase 4 → Phase 5 (deferred)

Phase 5 (real capital) requires ALL of:

1. ≥ 60 paper trades settled across R1, R2, and R7 shadow combined
2. Aggregate paper P&L positive at 95% confidence
3. Max paper drawdown < 2 × max-historical drawdown observed in baseline
4. Zero ops incidents (Flask crash, Live SDK silence > 1h during open market) in trailing 30 days
5. User explicit per-account approval; document risk parameters
6. External code review (independent verification of fill / sizing logic)
7. Compliance / regulatory review if applicable

Phase 5 is **not in scope of this document**. Listed only to clarify why
Phase 4 is intentionally bounded.

## 11. Reverse criteria — abandon paper, return to research

Pull the plug if:

- Aggregate paper R goes negative for 2 consecutive weeks during open market
- R1 or R2 retention falls below 0.5 across n ≥ 30
- Live SDK silence > 4 hours during open market repeats > 3× in 14 days
- A code review surfaces a defect that affects paper outcomes

In any of these, stop paper trading, write findings, return to Phase 2 work.

## 12. Reporting

If/when Phase 4 ships, emit a separate `outputs/order_flow/paper_trading_summary_<sym>_<tf>.md`:

```
## Paper Trading (Phase 4)
- trades today:    N
- daily R:         +X.X
- daily $ (TBD):   $X
- equity curve R:  cumulative
- max drawdown R:  trailing 14d
```

The previous daily-research-report and expected-move tools were removed
on 2026-05-04; Phase 4 reporting is intentionally a standalone artifact.

## 13. Open questions for user (before Phase 4A starts)

1. Per-trade risk: 1R only, or scale by ATR / regime?
2. Per-R dollar amount: $50 (1 ESM6 tick × 1 contract), $100, $250, other?
3. Daily R cap: 3R, 5R, no cap?
4. Per-rule cap: 2R, 3R, no cap?
5. Session filter: trade all 4 sessions (RTH_open / RTH_mid / RTH_close / ETH)
   or restrict to ETH where R2 baseline is strongest?
6. Drawdown rule: pause at -10R, -20R, no auto-pause?
7. Slippage model: 0 (close-price fills) for Phase 4A, or 1 tick / 0.25 ATR?
8. Equity curve display: dashboard card under Layer 2, or new route `/order-flow/paper`?

Answers shape Phase 4A implementation. None of them are decided in this
document.

---

## Status as of write date

| item | state |
|---|---|
| current phase | 2A active, 2B shadow, 2D Stage 1+2 |
| Phase 4 (paper) | NOT STARTED. preconditions not met. |
| Phase 5 (real capital) | NOT IN SCOPE |
| live R1/R2/R7sh count | all < n=10 (early checkpoint) |

Earliest Phase 4 start: when all 9 checkpoint cells reach n=30 and reviewer
signs off. Estimated ≥ 2-4 weeks of continuous Flask uptime under current
fire rates, **assuming live tail does not regress**.

---

_This document is read-only research planning. No code is changed by writing
this file. Reverse by deleting it. Update via PR review when scope changes._
