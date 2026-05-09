# R2 Paper-Simulation MVP — Specification & Schema Freeze (P1)

**Status:** P1 SPECIFICATION (schema-frozen at `schema_version=1`). P2 engine code now exists at `order_flow_engine/src/paper_sim_engine.py` (dry-replay only; no live wiring, no dashboard, no config edits, no broker).

This document is the authoritative spec for the R2 forward paper-simulation MVP. It supersedes nothing — see `docs/PAPER_TRADING_PLAN.md` for the broader paper-trading roadmap; this file narrows that roadmap to R2-only and freezes file schemas.

> **Per standing instruction:** filling this file does not modify code, does not modify `config.py`, does not change live behavior, does not promote R2 into trading.

### Changelog

- **2026-05-08T18:00Z** — P1 spec authored. Schema v1 frozen.
- **2026-05-08T19:00Z** — P1 open questions resolved in `docs/paper_sim_mvp_p1_resolutions.md` (entry fill = close-of-fire-bar; `time_stop_bars` reads `OF_FORWARD_BARS[tf]`; per-trade R divergence vs outcome tracker is by-design).
- **2026-05-08T20:00Z** — P2 dry-replay engine built at `order_flow_engine/src/paper_sim_engine.py`. Smoke OK over ESM6 15m joined frame.
- **2026-05-08T20:30Z** — §7 reconciliation rewritten to reflect by-design divergence; §9 P2 gate replaced (per-trade reproducibility + mfe/mae paired-fire match within ±0.05R). Schemas unchanged. `schema_version` remains 1.
- **2026-05-08T21:00Z** — P3 partial: engine `incremental_pass()` + `hydrate_from_disk()` + signal_id dedup added; CLI controls `paper_sim_enable.py` / `paper_sim_disable.py` / `paper_sim_close.py` shipped under `scripts/`; offline tests T1–T4 in `tests/test_paper_sim_engine.py` pass. No live wiring yet at this point.
- **2026-05-08T21:30Z** — P3 minimal wiring: `monitor_loop._step` gains a 4th try/except calling `paper_sim_engine.incremental_pass(symbol, tf)` against `_forward_out_dir()` = `outputs/order_flow/` (top-level, per spec §2 literal). Engine remains mechanically inert until `scripts/paper_sim_enable.py` flips `state.enabled=true`. No other files touched.
- **2026-05-08T22:00Z** — First live paper-sim trial (review at `docs/reviews/paper_sim_first_live_trial_20260508T220000Z.md`): 3 trades settled (overshoot from intended 1), final equity +1.69R, no engine errors, disable + force-close paths exercised. Trial surfaced two doc gaps: (a) §7/§9 mfe/mae gate broke on early exits; (b) no per-iteration trade cap. Both addressed below.
- **2026-05-08T22:30Z** — §7 mfe/mae gate refined to **window-aware** (subset-bound for early exits, ±0.05R direct for time-stopped). §9 P2 gate row (b) updated to match. New §12 documents per-iteration trade-cap design options (α: explicit cap field + CLI; β: batch-level scope re-frame); decision deferred. No code change. No schema bump. `schema_version` remains 1.
- **2026-05-08T23:00Z** — Approved Option α + Option β. Implemented: engine field `state.run_cap`, counter `fires_skipped_run_cap`, on_fire `skip_run_cap` path, per-pass baseline. New CLI `scripts/paper_sim_set_run_cap.py`. Tests T5a–T5e added; T1–T4 regression confirmed. §4 risk controls + §5 manual controls + §12 updated. `engine_version` bumped 0.1.0 → 0.1.1. `schema_version` remains 1 (additive optional fields). No changes to: rules, thresholds, config.py, ml_engine, predictor, alert_engine, ingest, outcome scoring, horizon, R7 paths, broker, dashboard, monitor_loop.

---

## 0. Invariants (load-bearing)

```
1. Paper-only. No broker. No order routing. No real money. Ever.
2. No edits to config.py. Engine enable/disable lives in paper_sim_state.json only.
3. No edits to R2 rule logic, R2 thresholds, OF_REAL_THRESHOLDS_ENABLED.
4. No edits to R1, R7 production, R7 shadow code paths.
5. No reuse of existing paper_journal_replay.py outputs (paper_journal*.json/.md)
   — those are retrospective replay; this MVP is forward simulation.
6. No reuse of existing paper_orders.jsonl / paper_positions.json / paper_risk.json
   — those are an unrelated prior feature. Paper-sim namespace is paper_sim_*.
7. Single rule scope: r2_seller_up only. R1 / R7 not in scope.
8. Single position max. No sizing logic. 1 unit per trade.
9. No telegram, no webhook, no phone alerts. CLI + dashboard read-only only.
10. Auto-promote to live trading is forbidden at every level. Live promotion
    requires a separate review with its own gates (bootstrap CI, RTH coverage,
    slippage modeling) outside this MVP.
```

---

## 1. Goal

Build a forward paper-simulation layer for r2_seller_up that:

1. Opens a simulated paper position when R2 fires live (going forward).
2. Marks-to-market on each subsequent 15m bar.
3. Closes at first stop / target / time-stop hit.
4. Persists the paper book, the order log, and a per-bar equity curve.
5. Surfaces the running state through a read-only dashboard card (deferred to P4).

**Not in scope:** retrospective replay (already covered by `scripts/paper_journal_replay.py`), execution-cost modeling, slippage modeling, sizing rules, multi-rule portfolio, broker integration, real money.

---

## 2. File schemas (FROZEN at P1)

All paths under `outputs/order_flow/`. Write atomically (tmp + rename) where applicable.

### 2.1 `paper_sim_state.json`

Single small JSON. Engine reads on every bar; rewrites only on state transition.

```json
{
  "schema_version": 1,
  "enabled": false,
  "enabled_reason": "P1 spec — engine not yet built",
  "enabled_changed_ts": "2026-05-08T18:00:00Z",
  "last_bar_processed_ts": null,
  "sequence": 0,
  "auto_pause": {
    "active": false,
    "reason": null,
    "tripped_ts": null,
    "tripped_metric": null,
    "tripped_value": null
  },
  "counters": {
    "trades_opened_total": 0,
    "trades_closed_total": 0,
    "consecutive_losses": 0,
    "trades_today": 0,
    "today_date": null
  },
  "watermarks": {
    "max_realized_R": 0.0,
    "max_drawdown_R": 0.0,
    "equity_R_running": 0.0
  },
  "last_updated_ts": "2026-05-08T18:00:00Z"
}
```

**Field rules:**
- `enabled`: only mutated by manual flip (CLI script or auto-pause). Default false until P5.
- `enabled_reason` and `enabled_changed_ts`: written every time `enabled` changes; reason is freeform string ≤ 200 chars.
- `auto_pause.active=true` forces engine to skip on-fire and on-bar logic regardless of `enabled`. Resume requires explicit manual reset that clears `auto_pause` AND sets `enabled=true`.
- `counters.trades_today` resets when current UTC date ≠ `counters.today_date`.
- `sequence` is monotonic increment per orders.jsonl write (used to correlate book ↔ orders).
- `schema_version` bumps on any breaking change to this file.

### 2.2 `paper_sim_book.json`

Open positions. MVP holds 0 or 1 position.

```json
{
  "schema_version": 1,
  "as_of_ts": "2026-05-08T18:00:00Z",
  "positions": [
    {
      "trade_id": "r2_2026-05-08T13:30:00Z_001",
      "rule": "r2_seller_up",
      "direction": 1,
      "entry_ts": "2026-05-08T13:30:00Z",
      "entry_bar_ts": "2026-05-08T13:30:00Z",
      "entry_px": 7261.25,
      "atr_at_entry": 5.96,
      "stop_px": 7255.29,
      "target_px": 7273.17,
      "stop_R_distance": 1.0,
      "target_R_distance": 2.0,
      "time_stop_bars": 12,
      "bars_held": 0,
      "current_px": 7261.25,
      "unrealized_R": 0.0,
      "mfe_R_seen": 0.0,
      "mae_R_seen": 0.0,
      "session_at_entry": "ETH",
      "sequence_open": 1
    }
  ]
}
```

**Field rules:**
- `positions` is a list (forward-compat) but length ≤ 1 in MVP.
- `trade_id` format: `<rule>_<entry_bar_ts_iso>_<3-digit-counter>`. Counter is `counters.trades_opened_total + 1` zero-padded.
- `direction = +1` always for r2_seller_up (long bias by rule design). Field is explicit for forward-compat with R1 (-1) etc.
- `stop_px = entry_px - direction * stop_R_distance * atr_at_entry`.
- `target_px = entry_px + direction * target_R_distance * atr_at_entry`.
- `unrealized_R = (current_px - entry_px) * direction / atr_at_entry`.
- `mfe_R_seen` / `mae_R_seen` are running per-trade extremes from each bar's high/low, in R units, sign convention: MFE positive, MAE negative.
- `session_at_entry`: one of `ETH | RTH_open | RTH_mid | RTH_close`.
- File rewritten atomically on every position change AND on every mtm bar.

### 2.3 `paper_sim_orders.jsonl`

Append-only fill log. One JSON object per line. Two record types: `open`, `close`.

#### Open record

```json
{
  "schema_version": 1,
  "type": "open",
  "sequence": 1,
  "trade_id": "r2_2026-05-08T13:30:00Z_001",
  "rule": "r2_seller_up",
  "direction": 1,
  "fire_bar_ts": "2026-05-08T13:30:00Z",
  "entry_ts": "2026-05-08T13:30:00Z",
  "entry_bar_ts": "2026-05-08T13:30:00Z",
  "entry_px": 7261.25,
  "atr_at_entry": 5.96,
  "stop_px": 7255.29,
  "target_px": 7273.17,
  "session_at_entry": "ETH",
  "fill_assumption": "close_of_fire_bar",
  "size_units": 1,
  "engine_version": "0.1.0",
  "written_ts": "2026-05-08T13:30:01.123456Z"
}
```

#### Close record

```json
{
  "schema_version": 1,
  "type": "close",
  "sequence": 2,
  "trade_id": "r2_2026-05-08T13:30:00Z_001",
  "rule": "r2_seller_up",
  "direction": 1,
  "exit_ts": "2026-05-08T15:30:00Z",
  "exit_bar_ts": "2026-05-08T15:30:00Z",
  "exit_px": 7273.17,
  "exit_reason": "target",
  "bars_held": 8,
  "realized_R": 2.0,
  "mfe_R_seen": 2.05,
  "mae_R_seen": -0.21,
  "tie_break_applied": false,
  "engine_version": "0.1.0",
  "written_ts": "2026-05-08T15:30:01.456789Z"
}
```

**Field rules:**
- `exit_reason ∈ { stop, target, time_stop, manual, auto_pause_force_close }`.
- `tie_break_applied=true` if same bar low ≤ stop_px AND high ≥ target_px (stop-first rule applied).
- `realized_R` computed as `(exit_px - entry_px) * direction / atr_at_entry` regardless of exit reason.
- File is append-only. Never rewrite. Never delete lines. Reset operation rotates file (see §6).

### 2.4 `paper_sim_equity.jsonl`

Append-only per-bar mark-to-market snapshots. One JSON object per line.

```json
{
  "schema_version": 1,
  "bar_ts": "2026-05-08T13:30:00Z",
  "realized_R_cumulative": 2.0,
  "unrealized_R": 0.0,
  "equity_R_total": 2.0,
  "open_positions": 0,
  "max_drawdown_R_running": 0.0,
  "trade_count_to_date": 1,
  "written_ts": "2026-05-08T13:30:01.789012Z"
}
```

**Field rules:**
- One row per bar processed by the engine, regardless of whether anything happened on that bar.
- `equity_R_total = realized_R_cumulative + unrealized_R`.
- `max_drawdown_R_running` is the largest peak-to-trough drop in `equity_R_total` since engine start, expressed as a non-positive number? **Convention: stored as a non-negative magnitude** (e.g. 3.5 means a 3.5R drop). Documented here, do not change.

### 2.5 Sequence integrity

`sequence` is a monotonic int shared between `paper_sim_state.json.sequence` and `paper_sim_orders.jsonl[].sequence`. Each orders write increments by 1. Reader can detect dropped writes by gap detection.

---

## 3. Paper trade lifecycle (specification)

```
on_fire(bar B, fire_ts, atr_B, close_B, session_B):
    if not state.enabled:                      return  # skip
    if state.auto_pause.active:                return  # skip
    if len(book.positions) >= 1:               return  # skip (single-position MVP)
    if state.counters.trades_today >= 4:       return  # skip (daily cap)
    open new trade per §2.2 schema
    append open record per §2.3
    state.sequence += 1
    state.counters.trades_opened_total += 1
    state.counters.trades_today += 1
    write book.json + state.json atomically

on_bar(bar B, high_B, low_B, close_B):
    if not state.enabled:                      return
    if state.auto_pause.active:                return
    for each open position p:
        p.bars_held += 1
        update p.mfe_R_seen, p.mae_R_seen from high_B, low_B
        stop_hit   = (low_B  <= p.stop_px)   if p.direction == +1 else (high_B >= p.stop_px)
        target_hit = (high_B >= p.target_px) if p.direction == +1 else (low_B  <= p.target_px)
        time_stop  = (p.bars_held >= p.time_stop_bars)
        if stop_hit and target_hit:
            close at stop_px, exit_reason="stop", tie_break_applied=true     # conservative
        elif stop_hit:
            close at stop_px, exit_reason="stop"
        elif target_hit:
            close at target_px, exit_reason="target"
        elif time_stop:
            close at close_B, exit_reason="time_stop"
        else:
            p.current_px = close_B
            p.unrealized_R updated
            continue
        on close:
            append close record per §2.3
            update state.counters.trades_closed_total
            update state.counters.consecutive_losses (reset on win, increment on loss)
            update state.watermarks
            check risk caps (§4); if any tripped → auto_pause
            remove p from book
    append equity row per §2.4
    write book.json + state.json atomically
```

**Stop-first-on-tie:** documented assumption. Logged via `tie_break_applied` for later audit against tick-level data.

---

## 4. Risk controls (frozen)

```
hard caps (auto-pause when tripped):
  max_open_positions          = 1
  max_trades_per_day          = 4
  max_consecutive_losses      = 5
  max_paper_drawdown_R        = 15.0
  time_stop_bars              = 12   (3h on 15m)

soft caps (skip-fire when tripped, no auto-pause):
  run_cap                     = null (default; per-pass open cap, optional)

advisory (logged, not pausing):
  open_position_MAE_below     = -2.0R
  flat_equity_window_trades   = 30
```

Auto-pause sets `state.auto_pause.active=true` and `state.enabled=false`. Resume is manual only — clears `auto_pause`, sets `enabled=true`, writes new `enabled_reason` + `enabled_changed_ts`.

`run_cap` is a per-pass open cap (default null = no cap). When set to a positive integer N, `incremental_pass` anchors a baseline at start; `on_fire` returns `skip_run_cap` once `(trades_opened_total - baseline) >= N` within that pass. The cap persists across iterations until cleared. Tighter than `max_trades_per_day`; both apply (whichever is tighter wins, per sequential check). Mutated only via `scripts/paper_sim_set_run_cap.py`. Engine `run_cap` is NOT enforced in `replay()` mode (per-pass baseline is `None`); only `incremental_pass` honors it.

---

## 5. Manual controls (planned for P5; specified here)

| action | mechanism | side effects |
|---|---|---|
| enable engine | `scripts/paper_sim_enable.py --reason "<text>"` | sets state.enabled=true |
| disable engine | `scripts/paper_sim_disable.py --reason "<text>"` | sets state.enabled=false |
| force-close open paper position | `scripts/paper_sim_close.py --reason "<text>"` | writes close record, exit_reason=manual |
| reset paper book | `scripts/paper_sim_reset.py --confirm --reason "<text>"` | rotates orders.jsonl + equity.jsonl to `*.bak.<ts>`; zeros book + state counters |
| dump n=N report | `scripts/paper_sim_report.py --n 30` | read-only; produces docs/reviews-style summary |
| set per-pass cap | `scripts/paper_sim_set_run_cap.py --cap N --reason "<text>"` | sets state.run_cap=N (positive int); appends `run_cap_audit` entry |
| clear per-pass cap | `scripts/paper_sim_set_run_cap.py --clear --reason "<text>"` | sets state.run_cap=null (no cap); appends audit entry |
| seed cursor (one-shot) | `scripts/paper_sim_seed_cursor.py --cursor <ISO> --reason "<text>"` | writes state with `last_bar_processed_ts=<ISO>`, enabled=false, all counters zero; refuses overwrite without `--force` |

All scripts log to `state.json.last_manual_action` (added in P5; field reserved here as forward-compat note).

No telegram / webhook triggers. CLI-only.

---

## 6. Reset semantics

Reset is the only operation that touches `paper_sim_orders.jsonl` and `paper_sim_equity.jsonl` after they're written. Reset:

1. Refuses to run if `book.json.positions` is non-empty (must close manually first).
2. Renames `paper_sim_orders.jsonl` → `paper_sim_orders.jsonl.bak.<utc-ts>`.
3. Renames `paper_sim_equity.jsonl` → `paper_sim_equity.jsonl.bak.<utc-ts>`.
4. Rewrites `paper_sim_book.json` to empty positions list.
5. Rewrites `paper_sim_state.json` with counters zeroed and watermarks reset; preserves `schema_version`, `engine_version` history note added.

Backups never auto-delete. Manual cleanup only.

---

## 7. Reconciliation against outcome tracker

After each paper trade closes, engine MAY (P5+) cross-check against `realflow_outcomes_ESM6_15m.jsonl` for the matching R2 fire. **Divergence is expected by design** — see `docs/paper_sim_mvp_p1_resolutions.md` §Q-NEW for the full mechanics breakdown. Reconciliation does NOT validate engine correctness via per-trade R agreement; it tracks two parallel measures of the same fire stream:

- `paper_sim.realized_R` — first-touch stop/target/time-stop close, with single-position and daily-cap filters applied (so fewer trades than total fires).
- `outcomes.fwd_r_signed` — fixed close-at-horizon R over every fire (no caps, no first-touch).

Both are valid; they measure different things. Divergence will be:

- **Zero** when no stop/target was touched within the window AND close-at-horizon ≈ first-touch close.
- **Material (often > 1R per trade)** when a stop or target was touched but price recovered/reversed by the horizon close.
- **Minor (< 0.2R)** when only the same-bar tie-break differs.

Engine-correctness sub-check (paired-fire validation, NOT R agreement):

The mfe/mae paired-fire check is **window-aware** — paper-sim observes only bars `[1..bars_held]`, while outcome tracker always observes the full 12-bar window. The check splits by exit type:

- **Time-stopped paper trades** (`exit_reason == "time_stop"`, `bars_held == OF_FORWARD_BARS[tf]`): paper observation window equals outcome window. Direct match check applies — `|paper.mfe_R_seen - outcomes.mfe_r| ≤ 0.05R` AND `|paper.mae_R_seen - outcomes.mae_r| ≤ 0.05R`. Larger drift indicates an engine bug (different ATR source, different bar set, off-by-one window).

- **Early-exit paper trades** (`exit_reason ∈ {"stop", "target", "manual"}`, `bars_held < OF_FORWARD_BARS[tf]`): paper window is a strict subset of outcome window. Direct ±0.05R comparison is not valid — post-exit bars can extend mfe higher and mae lower. **Subset-bound check** applies instead: `paper.mfe_R_seen ≤ outcomes.mfe_r + 0.05R` AND `paper.mae_R_seen ≥ outcomes.mae_r - 0.05R`. The tolerance handles floating-point rounding only; structural inequality (paper extreme inside outcome extreme) must hold by definition. Violation indicates an engine bug.

- **Pending settlements** (outcome tracker hasn't yet settled the matching fire because forward window incomplete): defer the check until the outcome row appears.

This split was added on 2026-05-08 after the first live trial (`docs/reviews/paper_sim_first_live_trial_20260508T220000Z.md`) surfaced a stop-out trade where paper.mae was -1.41R while outcome.mae_r was -1.84R (post-exit bar reached lower) — the original ±0.05R direct check would have flagged this as a bug despite the engine being correct.

Aggregate-level reconciliation:

- Track `paper_sim mean_R` vs `outcomes mean_fwd_r_signed` separately at every checkpoint. Both are reported. Neither is the canonical "truth."

Reconciliation is informational only; does NOT auto-correct paper trades.

---

## 8. Out of scope (explicit exclusions)

```
☐ Slippage / spread / commission modeling — fill at exact stop/target/close
☐ Sizing rules — fixed 1 unit
☐ Multi-rule portfolio — R2 only
☐ R1 / R7 paper-sim — separate future MVP if their verdicts ever support it
☐ Cross-asset port — single instrument (ESM6) only
☐ Order book / depth simulation — bar high/low/close only
☐ Partial fills — full size always
☐ Re-entry on same bar after stop — no
☐ Pyramiding — no
☐ Trailing stops — no
☐ Multi-timeframe confirmation — no
☐ Live broker integration — never in this MVP
```

---

## 9. Phase plan recap

| phase | scope | gate |
|---|---|---|
| **P1 (THIS DOC)** | spec + schema freeze | user sign-off on this file |
| P2 | engine code: dry-replay over the joined real-flow frame (`realflow_compare._load_pair`) | (a) per-trade R is bit-for-bit reproducible across two runs on identical input; (b) **window-aware** mfe/mae paired-fire check per §7: time-stopped trades match outcome tracker within ±0.05R; early-exit trades satisfy the subset-bound (`paper.mfe ≤ outcome.mfe + 0.05R` AND `paper.mae ≥ outcome.mae - 0.05R`); (c) `time_stop_bars` is read from `OF_FORWARD_BARS[tf]` at runtime, not hardcoded. **Note:** equity-curve agreement with `paper_journal_R2.md` is NOT a P2 gate — see §7; mechanics differ by design. The naive ±0.05R direct-match version of (b) was retired on 2026-05-08 after the first live trial showed it false-positives on early exits. |
| P3 | hook into live bar callback (read-only on rule fires) | flag default false; no live behavior change |
| P4 | dashboard read-only card | reviewer verifies fields render |
| P5 | manual control CLI scripts + auto-pause | toggle test passes |
| P6 | enable shadow-forward for ≥10 R2 fires | reconcile vs outcomes JSONL |
| P7 | n=30 forward paper review (separate doc, mirrors verdict template) | go/no-go on broader paper-trading roadmap |

---

## 10. Open questions deferred to P2

```
1. Entry fill convention — close-of-fire-bar (current spec) vs next-bar-open?
   Verify against realflow_outcome_tracker fwd_r_signed convention before P2.
   If mismatch, update §2.3 fill_assumption + §3 lifecycle.
2. ATR source parity — engine must use the same ATR source as the rule fire
   AND the outcome tracker. Confirm in P2 against rule_engine + outcome_tracker.
3. time_stop_bars=12 — does outcome tracker use a different N? Match it
   exactly to keep reconciliation clean.
4. Multi-fire-same-bar counter — track skipped fires (book full / cap hit /
   pause). Add `state.counters.fires_skipped_total` and a per-reason
   breakdown in P2.
5. Bar replay-vs-live parity — engine code path must be identical between
   replay and live (same function, different input source). Confirm before P2.
6. Atomic write strategy — tmp file in same dir + os.replace. Confirm OS
   guarantees on macOS APFS for the dashboard reader's perspective.
7. Engine-version field — bumped how? Manual on schema break vs git SHA?
```

---

## 11. Sign-off

P1 is complete when:

```
☐ This spec is reviewed by user
☐ Open questions §10.1 (entry fill) and §10.3 (time_stop_bars) are resolved
  in writing (in this file or a referenced note) BEFORE P2 starts
☐ User issues explicit "begin P2" instruction
```

No code is written until then.

---

## 12. Per-pass open cap (`run_cap`) — IMPLEMENTED

**Decision (2026-05-08T23:00Z):** approved Option α (engine `run_cap` field) + Option β (trial-scope language reframe).

### Mechanism

- New optional state field `state.run_cap: int | null` (default null).
- New per-pass counter `state.counters.fires_skipped_run_cap: int` (default 0).
- Engine attribute `_pass_open_baseline: int | None` set at start of each `incremental_pass` to the current `trades_opened_total`; reset to `None` on pass exit.
- `on_fire` returns the new skip reason `skip_run_cap` once `(trades_opened_total - _pass_open_baseline) >= state.run_cap`, after the existing daily-cap check.
- Cap persists across iterations until cleared.
- Cap is enforced ONLY inside `incremental_pass`; `replay()` ignores it (baseline is None).
- `engine_version` bumped to `0.1.1`. `schema_version` remains 1 (additive optional fields, default-null/zero behavior; older state.json files load cleanly via `.get(key, default)`).

### CLI

```
python scripts/paper_sim_set_run_cap.py --cap 1 --reason "..."
python scripts/paper_sim_set_run_cap.py --clear --reason "..."
```

Each call appends an entry to `state.run_cap_audit` (timestamp, prior, new, reason). Engine ignores unknown keys; audit is operator-facing only.

### Trial-scope language (Option β reframe)

Trial procedure templates must explicitly state one of:

- **Single-fire trial:** seed cursor → set `run_cap=1` → enable. After the first trade closes and reconciliation is complete: clear cap, disable.
- **N-fire trial:** seed cursor → set `run_cap=N` → enable. After the Nth trade closes: clear cap, disable.
- **Batch-level trial (no cap):** seed cursor → enable. Document that the trial scope is the "next monitor_loop batch with ≥ 1 fire," not a fixed trade count.

Trials must also document: cap value at trial start; cap value at trial end (must be null); audit entries for both transitions.

### Tests

- T5a: cap=1 truncates a 3-fire batch to 1 open + 2 skipped.
- T5b: cap=null = current behavior (3 opens, 0 skipped).
- T5c: cap > fire count = no skip.
- T5d: cap is per-pass, not per-trial-lifetime.
- T5e: tighter cap (run_cap vs daily) wins via sequential check.
- T1–T4 regression: all four pre-existing tests still pass with cap=null default.

### Deferred (Option 2 from earlier comparison)

Per-pass `max_new_bars_per_run` (catch-up backpressure) NOT implemented. Stays in reserve for later if catch-up scenarios ever demand throttling beyond what `run_cap` provides.

### Rejected (Options 3 + 4)

`stop_after_first_open` (Option 3) explicitly rejected for correctness risk: cursor-vs-bars_held desync. `stop_after_first_close` (Option 4) deprioritized as narrower than Option 1 with the same code surface.

---

_P1 specification document. No code, no config, no behavior change. Schema-frozen at version 1. Future schema changes require an explicit `schema_version` bump and a migration note._
