# R2 Paper-Sim — First Live Trial Review (P3)

**Status:** TRIAL CLOSED. Engine disabled, no open positions, no further action pending other than the spec-gate refinement noted below.

> **Headline:** Trial intended one R2 forward fire; iteration overshoot processed 43 bars in one monitor_loop call → 4 R2 fires landed past cursor → 3 trades opened, 1 fire skipped (book full). All 3 trades closed (2 organic + 1 manual force-close). Final equity +1.69R, max drawdown 1.0R. **Reconciliation surfaced a gate bug**: §9 P2 mfe/mae±0.05R gate fails by design whenever paper-sim closes early (stop or target before bar 12).

---

## Trial window

| field | value |
|---|---|
| seed cursor | `2026-05-08T07:00:00+00:00` (joined frame's then-last bar) |
| seed_ts | `2026-05-08T16:12Z` |
| enable_ts | `2026-05-08T16:13Z` |
| disable_ts | `2026-05-08T21:37Z` |
| disable_reason | "P3 trial scope cap: end after iteration overshoot (3 trades)" |
| final cursor | `2026-05-08T18:15:00+00:00` |
| monitor_loop iterations during trial | 2 (one bootstrap @16:14 with 2-bar advance; one @21:34 with 43-bar advance) |

---

## Trades

| # | trade_id | fire_ts | session | entry | exit | reason | R | bars | mfe_R | mae_R |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | r2..._08:45_001 | 2026-05-08T08:45Z | ETH | 7390.75 | 7399.89 | **target** | +2.0 | 4 | +2.52 | -0.11 |
| 2 | r2..._11:00_002 | 2026-05-08T11:00Z | ETH | 7397.50 | 7392.88 | **stop** | -1.0 | 1 | +0.16 | -1.41 |
| 3 | r2..._16:00_003 | 2026-05-08T16:00Z | RTH_mid | 7418.25 | 7424.25 | **manual** | +0.6877 | 10 | +1.09 | -0.52 |

Plus 1 fire skipped (`fires_skipped_book_full=1`) — fire on or near 11:xx-16:xx with trade #2 already open.

**Aggregate (3 trades):** wins 2 / losses 1; sum_R +1.6877; mean_R +0.5626; hit_rate 0.667.

---

## Reconciliation against outcome tracker

| fire_ts | paper_mfe | outcome_mfe_r | Δmfe | paper_mae | outcome_mae_r | Δmae | paper_R | outcome_fwd_r | gate |
|---|---|---|---|---|---|---|---|---|---|
| 08:45Z | +2.5166 | +2.5166 | 0.0000 | -0.1094 | -0.1094 | 0.0000 | +2.0 | +1.9148 | **PASS** (by coincidence — see below) |
| 11:00Z | +0.1624 | +4.0049 | 3.8425 | -1.4071 | -1.8401 | 0.4330 | -1.0 | +2.9225 | **FAIL** (by design — see below) |
| 16:00Z | +1.0888 | n/a | n/a | -0.5157 | n/a | n/a | +0.6877 | n/a | **PEND** (frame ended 18:15Z; horizon needs +12 bars from 16:00 = 19:00Z; settle waits for full window) |

### Why trade #1 PASSED

Paper-sim exited at bar 4 (target hit). The 12-bar window's max-high and min-low both occurred at-or-before bar 4 — so paper-sim's observed mfe/mae happened to match the full-window mfe/mae exactly. **Coincidence, not a gate property.** If price had extended after bar 4 (higher high or lower low), Δmfe / Δmae would be positive.

### Why trade #2 FAILED — gate is conceptually broken

Paper-sim closed at bar 1 (stop hit immediately). Subsequent bars 2-12 saw a major recovery: outcome tracker's 12-bar window registered mfe +4.00, mae -1.84, and a final fwd_close at +2.92R. Paper-sim cannot observe bars after its first-touch exit — by spec.

The §9 P2 gate "paper_sim.mfe_R_seen / mae_R_seen must match outcome tracker mfe_r / mae_r within ±0.05R for paired fires" presumes both tools observe the same 12-bar window. They do not when paper-sim exits early. **Gate is well-defined ONLY for trades that reach time_stop_bars (i.e. paper_R = continuous, not exactly -1.0 or +2.0).**

---

## §9 P2 gate refinement (recommendation, no edit yet)

The gate as currently written should be split:

```
For trades where paper-sim exits by stop OR target (early exit):
  - paper.bars_held < 12
  - mfe/mae will diverge from outcome tracker mfe_r/mae_r whenever post-
    exit bars contain higher high OR lower low than the pre-exit window
  - direct ±0.05R comparison is NOT a valid correctness check
  - INSTEAD: validate that paper.mfe_R_seen ≤ outcome.mfe_r AND
    paper.mae_R_seen ≥ outcome.mae_r (paper window is a SUBSET of
    outcome window, so paper extremes cannot exceed outcome extremes)

For trades where paper-sim exits by time_stop:
  - paper.bars_held == 12
  - paper window == outcome window
  - direct ±0.05R comparison IS valid
  - this is the only case where the original §9 gate applies
```

Apply both checks in P5+ reconciliation logic. Current trial:

| fire | exit | new check |
|---|---|---|
| 08:45 (target, bars=4) | early | paper_mfe ≤ outcome_mfe? 2.5166 ≤ 2.5166 ✓; paper_mae ≥ outcome_mae? -0.1094 ≥ -0.1094 ✓ — **PASS** under refined gate |
| 11:00 (stop, bars=1) | early | paper_mfe ≤ outcome_mfe? 0.1624 ≤ 4.0049 ✓; paper_mae ≥ outcome_mae? -1.4071 ≥ -1.8401 ✓ — **PASS** under refined gate |
| 16:00 (manual, bars=10) | early | pending outcome settle |

Under the refined gate, both settled trades PASS the engine-correctness check. **No engine bug indicated.**

---

## Anomalies / surprises

```
1. Single monitor_loop call processed 43 bars when run after a 9-hour
   gap. Engine has no per-iteration trade-count limit; it processes the
   whole batch. This is the cause of the scope overshoot.

2. Joined frame size shrank between iterations earlier in the day
   (1289 → 1254 → 1297). Real-flow cache rewrite is in play. Did not
   affect engine correctness — cursor-based gating handled all variants.

3. Trade #2 fwd_r_signed = +2.92R but paper-sim realized -1R. This is
   the largest first-touch-vs-close-at-horizon divergence the trial
   surfaced. Worth tracking per-trade across n=10+ paper-sim trades to
   estimate the systematic gap (paper edge vs outcome-tracker edge).

4. Trade #3 RTH_mid fire — first RTH fire in the trial. Open question
   from R2 verdicts: ETH dominance vs RTH coverage. Was force-closed
   at +0.69R unrealized; outcome tracker reconciliation pending.

5. Manual force-close timestamp is the wall-clock moment, not a bar
   boundary. Spec §2.3 close-record exit_ts/exit_bar_ts both got the
   same wall-clock value. Acceptable for manual close, but flag for
   P5+: scripts/paper_sim_close.py could optionally accept --bar-ts
   to align with bar boundaries.
```

---

## Recommendation

```
1. Adopt the refined §9 P2 gate (subset-window check for early exits,
   ±0.05R only for time-stopped). Update docs/paper_sim_mvp.md §7 + §9
   in a separate doc-only edit. Do NOT change schemas. schema_version
   stays at 1.

2. Engine is correctness-validated under the refined gate. P3 minimal
   wiring is sound. No engine code change needed at this point.

3. Before any next live paper-sim trial:
   a. Decide on per-iteration trade-count cap (new control: max
      trades per single incremental_pass invocation, distinct from
      max_trades_per_day). Without this, any future "one fire only"
      scope is unenforceable when monitor_loop runs after gaps.
   b. OR: accept that scope guarantees are batch-level (one batch =
      one iteration's bars), not per-fire, and rephrase trial scopes
      accordingly.

4. Rerun trial when ready, with explicit decision on (3a) vs (3b).
   Preferred: (3a) for tightly-controlled trials; (3b) for steady-state
   forward operation.

5. Accept this trial as a successful smoke of the live wiring even
   though scope overshoot occurred — engine behavior was correct
   throughout, error isolation worked, disable + force-close worked.
```

---

## Action items

| item | owner | due |
|---|---|---|
| Update `docs/paper_sim_mvp.md` §7 + §9 with refined-gate language | reviewer | doc-only, no code change |
| Decide per-iteration trade cap design (3a vs 3b above) | reviewer | before next trial |
| Wait for outcome tracker to settle trade #3 (16:00Z fire); rerun reconciliation for completeness | reviewer | when frame extends past 19:00Z |
| Author n=60 R2 verdict review when checkpoint trips (currently n=40, OK trajectory) | reviewer | normal cadence |
| Engine code: NO change at this point | n/a | n/a |
| config.py: NO change | n/a | n/a |
| Broker code: untouched | n/a | n/a |
| Live trading: NO promotion | n/a | n/a |

---

## What did NOT change

```
☑ rules: untouched
☑ thresholds: untouched
☑ config.py: untouched
☑ ml_engine: untouched
☑ predictor: untouched
☑ alert_engine: untouched
☑ ingest: untouched
☑ outcome scoring: untouched
☑ horizon: untouched (still OF_FORWARD_BARS["15m"]=12, read at runtime)
☑ R7 production threshold: -0.50 unchanged
☑ R7 shadow threshold: -0.20 unchanged
☑ broker integration: not present, not added
☑ trading behavior: paper only, never live
☑ R2 promotion: NOT promoted to trading; remains MVP-CANDIDATE / paper-eligible
☑ live weights: do not exist; not introduced
```

---

## State at trial end (audit)

```
outputs/order_flow/paper_sim_state.json:
  enabled         = false
  enabled_reason  = "P3 trial scope cap: end after iteration overshoot (3 trades)"
  cursor          = 2026-05-08T18:15:00+00:00
  trades_opened   = 3
  trades_closed   = 3
  fires_skipped   = 1 (book full)
  equity_R_running= +1.6877
  equity_peak_R   = +2.0000
  max_drawdown_R  = 1.0000
  consec_losses   = 0
  auto_pause      = inactive

outputs/order_flow/paper_sim_book.json:
  positions       = []   (clean)

outputs/order_flow/paper_sim_orders.jsonl:
  6 lines (3 open + 3 close)

outputs/order_flow/paper_sim_equity.jsonl:
  ~45 mtm rows
```

Engine remains wired into monitor_loop but mechanically inert (`enabled=false`).
