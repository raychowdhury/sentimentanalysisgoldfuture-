# Investor One-Pager — RFM ES Real-Flow Monitor

> **Status:** RESEARCH SYSTEM. NOT a trading system. NOT seeking capital.
> This document is for transparency about what is and isn't built.

---

## What this is

A local research pipeline that monitors real-time CME E-mini S&P 500 futures
order flow (ESM6 contract, 15-minute timeframe) for three reversal patterns:

- **R1** — fade buyer dominance (short bias)
- **R2** — fade seller dominance (long bias)
- **R7** — CVD divergence (sign-of-flow disagreement with bar direction)

The system **observes and tracks** how often these patterns fire and how
they perform forward in time. It does not place trades.

## What it isn't

- ❌ Not a trading system. No execution. No broker. No order routing.
- ❌ Not a hedge fund product. No outside capital. No fund structure.
- ❌ Not a black box. Every threshold, every rule, every outcome is in
     plain JSON / Markdown files on disk.
- ❌ Not optimized. Phase 1 is read-only validation; promotion to live
     execution is not in scope.
- ❌ Not multi-asset. ESM6 only. Other contracts deferred until ESM6
     verdict is recorded.

## Why it exists

To answer one question rigorously:

> Do real order-flow signals (actual buyer vs seller intent, derived from
> tick-level trade data) produce a real, sustained edge over candle-derived
> proxy heuristics on ES futures?

A previous calibration phase (test window) showed they could. The current
phase tests whether that holds **forward in time** on live data the system
has never seen.

## Current state (as of write date)

| dimension | value |
|---|---|
| historical settled trades | ~246 (R1+R2) + ~100 R7 shadow |
| live settled trades | ~12 R1+R2 + 9 R7 shadow |
| historical R2 retention | 1.17 (strong vs calibration baseline) |
| historical R1 retention | 0.07 (weak — barely positive edge) |
| live R1 trend | early sample negative, trending WARN |
| live R2 trend | early sample positive, trending OK |
| live R7 shadow trend | early sample very negative, trending WARN |
| operational uptime | local Mac, daily duty cycle |
| trading exposure | none |

Translation: R2 looks healthy. R1 historically looks weak and live looks
worse. R7 shadow is failing on live. We are 4-7 days from the first formal
verdict checkpoint at n=30 per rule.

## How decisions are made

Three checkpoints per rule (n=10, n=15, n=30 live samples). At each:

1. Health monitor fires a notification on transition
2. Reviewer fills a Markdown review template
3. Decision is one of: KEEP / REVERT / RETUNE / INVESTIGATE
4. Implementation is **manual** — flipping an env flag, no automated promotion

This is intentionally slow and explicit. The system is designed to make
decay visible, not to be fast.

## What's working

- Real-time data pipeline (Databento Live SDK, free of vendor outages this
  session)
- Outcome tracker (every fire scored vs 1-ATR stop, 1-ATR target, 12-bar
  forward horizon)
- Health monitor (13 probes, auto notifications on failure)
- Cache refresh (hourly, automated)
- Read-only dashboard (4-layer trader/analyst/technical/help layout)
- Daily research report (Markdown summary, frontend viewer)
- Per-fire trade detail with entry/stop/target/MFE/MAE/outcome

## What's risky

| risk | mitigation |
|---|---|
| R1 retention historically thin (0.07) | live data may force REVERT verdict at n=30 |
| R7 shadow promotion hypothesis failing | will stay shadow-only; production R7 unchanged |
| Live SDK silently freezes | health monitor flags within 5 min |
| 15m parquet persistence bug at startup | manual rename workaround applied per Flask boot |
| Mac dependency for ops | weekend hibernation acceptable; EC2 deferred |
| Small live sample | gating on n=30 before any threshold change |

## What we will not do

- Place real trades. Period.
- Promote R7 from shadow to production without a separate Stage 2 review.
- Auto-revert thresholds based on live data (manual env flag only).
- Cross-asset port (gold, oil, NQ) before ESM6 verdict.
- Retrain models. The ML engine is intentionally untouched.
- Lower thresholds to fire more signals. Edge does not improve with
  signal volume.

## Realistic timeline

| milestone | ETA under continuous uptime |
|---|---|
| R7 shadow n=10 | ~1-2 days |
| R1 / R2 n=10 | ~2-4 days |
| All three n=15 | ~5-7 days |
| First verdicts at n=30 | ~10-14 days |
| Phase 4 (paper trading) | only if n=30 verdicts allow |
| Phase 5 (live capital) | not on current roadmap |

## What this would cost an outside party (informational, not a fundraise)

- Databento subscription: ~$X/month (CME GLBX.MDP3 plan dependent)
- Mac or small EC2: ~$0-50/month
- No license, no team, no broker fees (no trades placed)

The project is operationally cheap. The expensive thing is patience —
waiting 10-14 days for a single verdict, three times.

## What an investor / partner would want to know

The most likely outcome is that **at least one rule fails on live data and
gets reverted**. R2 may survive. R1 may not. R7 shadow looks dead.

If only R2 survives:

- The system becomes a single-rule long-side observer
- Forward profitability is unknown until paper trading runs
- Total investable signal frequency is ~5-7 trades/day in active sessions

If all three fail:

- The real-flow vs proxy hypothesis is empirically rejected on ES
- The honest path is to publish findings and stop
- No "let's tune until it works" — that's overfitting

If all three survive:

- Phase 4 paper trading begins (simulated, no broker)
- Phase 5 (real capital) requires an entirely separate evaluation

The system is honest about all three outcomes. It does not bias toward
"keep going."

## Contact

This is a single-person research project on a local machine. There is no
team, no fund, no investment vehicle. Communication is via the project
files. PROJECT_STATE.md is the source of truth.

---

_Last updated: 2026-05-02. Reverse by deleting this file._
