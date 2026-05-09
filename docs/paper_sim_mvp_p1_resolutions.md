# R2 Paper-Sim MVP — P1 Open-Question Resolutions

**Status:** Read-only investigation. No code changed. No spec changed. This document resolves the open questions blocking P1 sign-off in `docs/paper_sim_mvp.md` §10 / §11. Spec remains schema-frozen at version 1.

Source of truth: `order_flow_engine/src/realflow_outcome_tracker.py` and `order_flow_engine/src/config.py`.

---

## Q1 (§10.1) — Entry fill convention

**Outcome tracker behavior:**

`order_flow_engine/src/realflow_outcome_tracker.py:339-340`:

```python
row   = df.loc[fire_ts]
entry = float(row["Close"])
```

`entry` = **close of the fire bar itself**, not next-bar-open.

`_score_outcome` then computes `fwd_r_signed = ((fwd_close - entry) * direction) / atr` where `fwd_close` is the close of the bar at `fire_idx + horizon` (i.e. the 12th bar AFTER the fire bar on 15m).

**Resolution for paper-sim MVP:**

- **Adopt close-of-fire-bar as entry fill.** Matches outcome tracker exactly.
- `paper_sim_mvp.md` §2.3 already specifies `"fill_assumption": "close_of_fire_bar"`. **No spec change required.**
- Honest caveat: real-paper-trader executing at close-of-bar requires a market-on-close-style fill at the bar boundary; for ESM6 15m bars this is realistic but not free. Slippage/cost modeling stays out of scope (§8).

**Status: RESOLVED. No spec edit needed.**

---

## Q3 (§10.3) — Time-stop window

**Outcome tracker behavior:**

`order_flow_engine/src/realflow_outcome_tracker.py:305`:

```python
horizon = of_cfg.OF_FORWARD_BARS.get(tf, 1)
```

`order_flow_engine/src/config.py:71-76`:

```python
OF_FORWARD_BARS: dict[str, int] = {
    "5m":  12,   # ~1 hour
    "15m": 12,   # ~3 hours (was 8)
    "1h":  4,    # ~4 hours
    "1d":  1,    # next session
}
```

Forward window for 15m = **12 bars** (~3h). The window in `_score_outcome` is `bars [fire_idx+1 .. fire_idx+horizon]` (i.e. the 12 bars AFTER the fire bar). `fwd_close` is the close of bar `fire_idx + horizon` = the 12th forward bar's close.

**Resolution for paper-sim MVP:**

- **Adopt time_stop_bars = 12.** Matches `OF_FORWARD_BARS["15m"]`.
- `paper_sim_mvp.md` §2.2 + §3 + §4 already specify `time_stop_bars = 12`. **No spec change required.**
- Forward-compat: if `OF_FORWARD_BARS["15m"]` is ever retuned (was 8 → now 12 per the inline comment), paper-sim engine MUST re-read this config at startup, NOT hardcode 12. Add to P2 as an implementation note (engine reads `OF_FORWARD_BARS[tf]` at construct time).

**Status: RESOLVED. No spec edit needed; one P2 implementation note added below.**

---

## Q-NEW — Mechanics divergence: outcome tracker vs paper-sim engine

**Surfaced during this investigation. Not in the original §10 list.** Worth recording before P2 because reconciliation (§7) depends on it.

### Outcome tracker semantics (the existing `fwd_r_signed`)

`_score_outcome` (lines 134-205) computes the recorded R as:

```
fwd_r_signed = ((Close[fire_idx + 12] - entry) * direction) / atr
```

This is **fixed-horizon close-to-close R**. The trade is effectively "held to bar 12 regardless of intra-window touches." There is no stop, no target, no early exit. The `hit_1r` and `stopped_out_1atr` flags are informational only — they do NOT short-circuit `fwd_r_signed`.

Within the window, per-bar first-touch logic at lines 173-186 checks `up_r` BEFORE `dn_r`:

```python
if up_r >= 1.0 and not stopped:
    hit_1r = True
    break
if dn_r <= -1.0 and not hit_1r:
    stopped = True
    break
```

So when a single bar's high ≥ entry+1 ATR AND low ≤ entry-1 ATR, outcome tracker tags it `hit_1r=True` (target-first within bar). Again, this affects only the flag, not the recorded R.

### Paper-sim MVP engine semantics (per §3 of frozen spec)

Paper-sim engine **closes the trade at first touch**:

- stop hit → close at `stop_px`, realized_R = -1
- target hit → close at `target_px`, realized_R = +target_R_distance (default +2)
- neither → mark to market and continue
- bar 12 reached without stop/target → close at `close_B`, realized_R = (close - entry) * dir / atr

Tie-break in §3 says **stop-first** when both touched on the same bar.

### Divergence summary

| dimension | outcome tracker | paper-sim MVP | impact |
|---|---|---|---|
| recorded R definition | fixed close-at-horizon | first-touch stop/target, else close-at-horizon | **largest divergence source** |
| stop hit behavior | informational flag only | trade closes at -1R immediately | per-trade R can differ materially |
| target hit behavior | informational flag only | trade closes at +target_R_distance | per-trade R can differ materially |
| same-bar tie-break | target-first (via up_r first check) | **stop-first** (conservative) | small per-trade divergence on tied bars |
| horizon | 12 bars (15m) | 12 bars (matches) | none |
| entry | close of fire bar | close of fire bar (matches) | none |

**Implication for reconciliation (paper_sim_mvp.md §7):**

Per-trade `paper_sim.realized_R` ≠ `outcomes.fwd_r_signed` is **expected, not a bug**. Divergence will be:

- Zero when no stop/target touched within window AND close-at-12 ≈ first-touch close.
- Material (often > 1R) when:
  - stop touched but price recovered by bar 12 → outcome tracker shows positive `fwd_r_signed`, paper-sim shows -1R.
  - target touched but price reversed by bar 12 → outcome tracker shows lower-or-negative `fwd_r_signed`, paper-sim shows +target_R_distance.
- Small (< 0.2R) when only the same-bar tie-break differs.

The §7 thresholds in the spec (> 0.5R divergence flags review, > 1.0R flags alert) need to be **interpreted as cross-check on engine correctness only**, not as expected agreement. Recommend the spec wording be relaxed in P2 prep.

### Resolution recommendations

1. **Keep paper-sim mechanics as specified** — first-touch stop/target with time-stop = 12. This is the realistic paper-trader semantics; matching outcome tracker's close-at-horizon would defeat the MVP's purpose (the whole point of paper-sim is realistic per-trade R, not fixed-window scoring).

2. **Keep stop-first tie-break in paper-sim** — conservative, pessimistic against the rule, audit-trackable via `tie_break_applied` field. Documented divergence from outcome tracker's target-first flag is acceptable.

3. **Add explicit divergence-expected note to spec §7** in P2 prep — change wording from "expected divergence sources: time-stop bar count differences, tie-break rule, fill-assumption choice" to "expected divergence: first-touch close vs fixed-horizon close (dominant), tie-break rule (minor)."

4. **Two parallel reconciliation aggregates** (P5+):
   - `paper_sim mean_R` over same n trades
   - `outcome tracker mean_fwd_r_signed` over same n trades
   - Both are valid, measuring different things. Track both for the n=30 paper-sim review.

---

## Q-NEW-2 — Engine config-source binding

Surfaced when answering Q3. Recommendation:

- Engine MUST read `OF_FORWARD_BARS[tf]` and `BASELINE_TEST_MEAN_R[rule]` at construct time, not hardcode.
- Engine MUST read ATR from the same source as the rule fire (`row["atr"]` in the joined frame produced by `realflow_compare._load_pair`).
- Engine MUST NOT import `predictor`, `alert_engine`, `ingest`, or `ml_engine` (mirrors outcome tracker invariants at lines 8-14).

These bindings are P2 implementation rules, not spec changes.

---

## P1 sign-off readiness

Per `docs/paper_sim_mvp.md` §11:

```
☑ Q1 (entry fill) resolved in writing → close-of-fire-bar, matches outcome tracker
☑ Q3 (time_stop_bars) resolved in writing → 12, matches OF_FORWARD_BARS["15m"]
☑ Q-NEW (mechanics divergence) documented → expected, not a bug
☑ Q-NEW-2 (engine config-source) documented → P2 implementation rule
☐ User issues explicit "begin P2" instruction
```

P2 may begin once the user signs off on this resolutions document.

---

## What was NOT changed

```
☑ No code changed
☑ No config changed
☑ No spec edited (paper_sim_mvp.md frozen at schema_version=1)
☑ No live behavior change
☑ No R2 / R1 / R7 production change
☑ No engine built
☑ No dashboard added
```

Pure investigation + written resolution.
