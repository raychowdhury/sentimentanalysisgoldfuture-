# NewsSentimentScanner — Research & Engineering Upgrade Roadmap

**Companion document to:** `docs/full-system-audit.md`
**Scope:** prioritised, implementation-oriented roadmap for turning the current
rule-based decision-support engine into a research-grade, reproducible,
statistically-validated trading-signal platform.
**Prioritisation scheme:**

- **Level 1 — Hygiene & correctness.** Fixes for things that are wrong, drifting,
  or silently failing in the *current* code. Must be done before any research
  conclusion can be trusted. Low-to-medium effort, high immediate payoff.
- **Level 2 — Research foundations.** Additions required to move from "we
  eyeballed the numbers and they look good" to "we can defend this to a
  reviewer." Statistical validation, data reproducibility, cost models,
  position sizing, experiment tracking.
- **Level 3 — Advanced capability.** Larger programmes of work: intraday
  engine, feature store, paper-trading bridge, short-side rework, ML/ensemble
  layer.

Each item lists: **Why it matters**, **Complexity** (S/M/L/XL), **Affected
files**, and **Implementation sketch**. Complexity is a rough order of
magnitude — S = under a day, M = 2–5 days, L = 1–3 weeks, XL = multi-week
programme.

---

## Table of Contents

- [Level 1 — Hygiene & Correctness](#level-1--hygiene--correctness)
  - [L1.1 Update README to match code](#l11-update-readme-to-match-code)
  - [L1.2 Event calendar expiry guard](#l12-event-calendar-expiry-guard)
  - [L1.3 Flask hardening](#l13-flask-hardening)
  - [L1.4 Log rotation](#l14-log-rotation)
  - [L1.5 Grid-search global-state cleanup](#l15-grid-search-global-state-cleanup)
  - [L1.6 Unify RR validation](#l16-unify-rr-validation)
  - [L1.7 Missing unit tests](#l17-missing-unit-tests)
  - [L1.8 SMA200 clamp disclosure](#l18-sma200-clamp-disclosure)
  - [L1.9 Timeframe parity across CLI](#l19-timeframe-parity-across-cli)
  - [L1.10 Floating-point RR parity live vs backtest](#l110-floating-point-rr-parity-live-vs-backtest)
  - [L1.11 Partial-TP same-bar ordering](#l111-partial-tp-same-bar-ordering)
- [Level 2 — Research Foundations](#level-2--research-foundations)
  - [L2.1 Historical sentiment ingest (GDELT)](#l21-historical-sentiment-ingest-gdelt)
  - [L2.2 Cost model (commission + slippage)](#l22-cost-model-commission--slippage)
  - [L2.3 Position sizing module](#l23-position-sizing-module)
  - [L2.4 Sharpe, Sortino, profit-factor](#l24-sharpe-sortino-profit-factor)
  - [L2.5 Bootstrap CI on expectancy](#l25-bootstrap-ci-on-expectancy)
  - [L2.6 Deflated Sharpe for grid search](#l26-deflated-sharpe-for-grid-search)
  - [L2.7 Walk-forward cross-validation](#l27-walk-forward-cross-validation)
  - [L2.8 Ablation harness](#l28-ablation-harness)
  - [L2.9 Null-hypothesis benchmark](#l29-null-hypothesis-benchmark)
  - [L2.10 Experiment registry (SQLite)](#l210-experiment-registry-sqlite)
  - [L2.11 Data snapshotting per run](#l211-data-snapshotting-per-run)
  - [L2.12 Live data drift monitor](#l212-live-data-drift-monitor)
  - [L2.13 Calibrated confidence](#l213-calibrated-confidence)
  - [L2.14 Factor correlation / VIF audit](#l214-factor-correlation--vif-audit)
  - [L2.15 Formal event-study around CPI/FOMC](#l215-formal-event-study-around-cpifomc)
- [Level 3 — Advanced Capability](#level-3--advanced-capability)
  - [L3.1 Intraday engine](#l31-intraday-engine)
  - [L3.2 Feature store](#l32-feature-store)
  - [L3.3 Short-side asymmetry diagnostic](#l33-short-side-asymmetry-diagnostic)
  - [L3.4 Paper-trading bridge](#l34-paper-trading-bridge)
  - [L3.5 Benchmark suite](#l35-benchmark-suite)
  - [L3.6 LLM panel robustness](#l36-llm-panel-robustness)
  - [L3.7 ML layer on top of rule engine](#l37-ml-layer-on-top-of-rule-engine)
  - [L3.8 Multi-asset generalisation](#l38-multi-asset-generalisation)
  - [L3.9 Explainability / factor attribution](#l39-explainability--factor-attribution)
- [Sequencing](#sequencing)

---

## Level 1 — Hygiene & Correctness

### L1.1 Update README to match code

**Why it matters.** The current `README.md` describes thresholds `+4 / +2 / -2 /
-4` and a sentiment-first architecture. The current `config.SIGNAL_THRESHOLDS`
uses `+6 / +2 / -2 / -6`, and the signal engine now consumes eight weighted
factors (sentiment, DXY, real-yield, gold, VIX, VWAP, volume profile, COT)
plus four gates (veto, LONG\_ONLY, SMA200, event blackout). Anyone onboarding
— reviewer, reader, or future-you — reads fiction.

**Complexity.** S.

**Affected files.** `README.md` only.

**Implementation sketch.**

1. Replace the signal-mapping section with the actual thresholds from
   `signals/signal_engine.py:52-68`.
2. Add a section describing each factor and its weight, pulled verbatim from
   `config.SCORE_WEIGHTS`.
3. Document the four gates (`_veto`, `LONG_ONLY`, `SMA200_GATE`,
   `EVENT_GATE_ENABLED`) with a one-line rationale each.
4. Replace the "trade setup = fixed 2R" paragraph with the real logic: ATR
   stop, volume-profile target priority, `MIN_RR` gate per profile.
5. Include the two timeframe profiles from `config.TIMEFRAME_PROFILES`.
6. Add a one-screen diagram of the live flow (news → sentiment → market data
   → signal → trade setup → confidence → dashboard).

---

### L1.2 Event calendar expiry guard

**Why it matters.** `events/calendar.py` hardcodes FOMC, CPI, NFP, PCE dates
through Dec 2026. After that, `is_event_day()` silently returns `False` for
every date, the blackout gate stops firing, and nobody notices until a
scheduled run walks straight into a CPI release. The day-trade profile
depends heavily on this gate (per `memory/event_gate_finding.md`: +50%
expectancy, −65% drawdown).

**Complexity.** S to add the guard, M to replace with a live feed.

**Affected files.** `events/calendar.py`, `events/blackout.py`, new test in
`tests/test_event_gate.py`.

**Implementation sketch (minimum viable).**

1. Add an assertion or warning in `events/blackout.py` on import: if
   `max(calendar dates) - today() < 90 days`, log a `WARNING` and optionally
   raise in strict mode (`config.STRICT_CALENDAR = False` default).
2. Add a unit test that parameterises the current date and fails when the
   horizon shrinks below 90 days — a CI reminder, not a runtime block.

**Implementation sketch (live feed).**

1. Replace hardcoded list with a daily pull from a free source (Federal
   Reserve RSS for FOMC, BLS release calendar for CPI/NFP, BEA for PCE).
2. Cache pulls under `outputs/event_cache.json` with a `fetched_at` field
   and fall back to the hardcoded list if the pull fails.

---

### L1.3 Flask hardening

**Why it matters.** `app.py:313` starts Flask with `debug=True`, which enables
the Werkzeug interactive debugger. If the dashboard is ever bound to a
non-loopback interface (reverse-proxied, tunnelled, cloud-deployed), the
debugger is remote-code-execution. Additionally `POST /api/run` has no auth
— anyone who can reach the port can trigger a run.

**Complexity.** S.

**Affected files.** `app.py`, `config.py`, `scheduler.py`.

**Implementation sketch.**

1. Gate `debug=True` on an env var (`FLASK_DEBUG=1`); default `False`.
2. Add an `API_TOKEN` env var and a small decorator that checks
   `request.headers['X-Auth']` on mutating endpoints. Localhost-only bind
   (`host="127.0.0.1"`) is already implicit via default but make it
   explicit.
3. Document in `README.md` the two modes: local-dev vs exposed.

---

### L1.4 Log rotation

**Why it matters.** `outputs/flask.log` and `outputs/grid_run.log` are
append-only files. Over months of scheduler runs they grow unbounded. Disk
pressure silently kills the scheduler.

**Complexity.** S.

**Affected files.** `utils/logger.py`, `app.py`, `scheduler.py`.

**Implementation sketch.**

1. Replace `logging.FileHandler` with `logging.handlers.RotatingFileHandler`
   (10 MB × 5 backups) in `utils/logger.py`.
2. Make the rotation config-driven in `config.py`.

---

### L1.5 Grid-search global-state cleanup

**Why it matters.** `backtest/grid_search.py` mutates module-level globals
(`config.TRAIL_MODE`, `config.PARTIAL_TP_*`) per iteration with a
try/finally restore. This works for a single-process sequential grid but:
(a) leaks state if an exception kills the worker between set/restore,
(b) is hostile to parallelisation (multiprocessing would copy state but
results become non-deterministic if any other module reads the globals
during import), (c) makes unit-testing the engine around a grid-search
config impossible without setup/teardown gymnastics.

**Complexity.** M.

**Affected files.** `backtest/engine.py`, `backtest/grid_search.py`,
`config.py`, all call sites that currently read `config.TRAIL_*` and
`config.PARTIAL_TP_*` (grep surface: ~6 call sites).

**Implementation sketch.**

1. Introduce a `BacktestParams` dataclass in `backtest/engine.py` holding
   `trail_mode`, `trail_atr_mult`, `partial_tp_enabled`, `partial_tp_frac`,
   `partial_tp_r`, `move_to_be_on_partial`, `min_rr`, `warmup_bars`,
   `max_hold_days`.
2. Default it from `config.*` so the live path stays unchanged.
3. Thread the instance through `run()` and `_simulate()`.
4. Grid search constructs per-iteration instances — no module mutation.

---

### L1.6 Unify RR validation

**Why it matters.** `signals/risk_management.validate()` contains the
canonical "downgrade to `no_trade` if `rr < min_rr`" logic. The backtest
engine doesn't call it; it reads `setup["trade_valid"]` directly, which is
set inside `trade_setup._buy/_sell` using a near-identical but duplicated
check. Two copies of the same rule drift: one uses `<`, one might use
`<=`, and floating-point `rr == min_rr` becomes undefined behaviour.

**Complexity.** S.

**Affected files.** `signals/risk_management.py`, `signals/trade_setup.py`,
`backtest/engine.py`, `main.py`, `tests/`.

**Implementation sketch.**

1. Extract one `validate_rr(setup: dict, min_rr: float) -> dict` function.
2. Call from `trade_setup.compute()` at the end and from
   `backtest.engine._open_trade` (new wrapper) before position entry.
3. Add a unit test for the edge case `rr == min_rr` and pick a convention
   (recommend `>=` — strict inequality as currently used is fine but
   document it).

---

### L1.7 Missing unit tests

**Why it matters.** Test coverage is ~619 lines across 8 files but has
conspicuous gaps in the highest-risk components: sentiment aggregator,
indicator formulas, factor scoring, trade-setup construction, confidence
label, Flask routes. Any refactor touches these blindly.

**Complexity.** M.

**Affected files.** add `tests/test_aggregator.py`, `tests/test_indicators.py`,
`tests/test_trend_scoring.py`, `tests/test_trade_setup.py`,
`tests/test_confidence.py`, `tests/test_app_routes.py`.

**Implementation sketch.**

1. `test_aggregator.py`: weight-majority, tie-break order, empty input,
   single-engine input, NaN safety.
2. `test_indicators.py`: ATR = 0 when range 0, VWAP single-bar equal to
   close, VAL/VAH straddle 70% volume, TPO POC on a synthetic ladder.
3. `test_trend_scoring.py`: edge-case moves exactly at threshold boundaries
   (e.g. DXY +1.0% 5-day).
4. `test_trade_setup.py`: buy/sell symmetry, fallback to `MIN_RR × risk`
   target when VP targets absent, `trade_valid=False` when `rr < MIN_RR`.
5. `test_confidence.py`: LOW when factors contradict gold direction, HIGH
   when 4+ factors aligned.
6. `test_app_routes.py`: GET `/` renders, GET `/api/status` returns JSON,
   POST `/api/run` 202s without actually kicking a pipeline (mock
   `scheduler.trigger_run`).

---

### L1.8 SMA200 clamp disclosure

**Why it matters.** `market/indicators.py` computes SMA200 via
`close.tail(min(200, len(close))).mean()`. For shorter history this returns
a shorter-window mean labelled as SMA200 — the `macro_bullish` gate then
uses the clamped value as if it were a true 200-day average. On thinly-
backtested windows this silently lies.

**Complexity.** S.

**Affected files.** `market/indicators.py`, `app.py` (`_macro_bullish` uses
it), `signals/signal_engine.py` (SMA200 gate).

**Implementation sketch.**

1. Add `is_true_sma200: bool` to the indicator dict (True iff
   `len(close) >= 200`).
2. In `_macro_bullish` and the SMA200 gate, if `is_true_sma200 is False`,
   log a `WARNING` and fall back to no-macro-gate (or use EMA50 as
   secondary trend proxy, explicitly).
3. Display in the dashboard status strip.

---

### L1.9 Timeframe parity across CLI

**Why it matters.** `main.py --timeframe day|swing` affects `run_signal` but
does nothing for `run_sentiment`. The scheduler's `SCHEDULER_TIMEFRAME` is
read only in signal paths. A user kicking `python main.py --timeframe day`
for a sentiment-only run gets swing-profile behaviour silently.

**Complexity.** S.

**Affected files.** `main.py`, `scheduler.py`.

**Implementation sketch.**

1. If `--signal` is not requested, ignore `--timeframe` with an INFO log.
2. Document in `--help` that `--timeframe` is a signal-engine flag.

---

### L1.10 Floating-point RR parity live vs backtest

**Why it matters.** If `trade_setup.compute()` reports `rr=3.0000000001` and
`risk_management.validate()` uses `rr >= 3.0`, the live path accepts, the
backtest path (which reuses `trade_setup` internally via the same call) also
accepts — but small refactors drift easily. A single `round(rr, 2)` can
change accept/reject for thousands of backtest bars.

**Complexity.** S. Subsumed by [L1.6](#l16-unify-rr-validation) once fixed.

**Affected files.** `signals/trade_setup.py:155`, `signals/risk_management.py`.

**Implementation sketch.** Use the same `rr` float through both checks; add
a unit test that constructs a setup with `rr = min_rr + 1e-12` and asserts
`trade_valid == True` at both layers.

---

### L1.11 Partial-TP same-bar ordering

**Why it matters.** In `backtest/engine._simulate`, partial-TP is checked
before the main stop/TP block on the same bar. Intraday, if price gaps
through both the partial-TP and the full-stop in one bar, the current
logic books the partial first and then stops out at the full stop — a
blended outcome that is slightly optimistic vs the pessimistic "stop first"
rule applied everywhere else in the engine.

**Complexity.** S.

**Affected files.** `backtest/engine.py`.

**Implementation sketch.**

1. Swap the order: check stop first on each bar. If stop hit, close full
   position (partial tranche included) at the stop price.
2. If stop not hit and partial-TP hit, book the partial.
3. Then check main TP.
4. Add a unit test with a synthetic gap bar to regression-protect.

---

## Level 2 — Research Foundations

### L2.1 Historical sentiment ingest (GDELT)

**Why it matters.** The sentiment cache is **forward-only** — only dates
that have been through a live run carry real sentiment. Every pre-deployment
bar in the 2–5 year backtest has `sentiment_score = 0`. This means every
published finding about sentiment weight, COT ablations, event-gate wins,
etc. is computed on a dataset where sentiment is structurally zero.
Fixing this unlocks everything downstream.

**Complexity.** L.

**Affected files.** new `sentiment/history_gdelt.py`, `sentiment/cache.py`,
`backtest/engine.py`, `tests/`.

**Implementation sketch.**

1. **Source.** GDELT 2.0 events/GKG API is free, minute-level, covers 2015+.
   Query with themes `ECON_*`, `WB_*`, currency codes `USD`, `XAU`, entity
   `FED`. Document quota and fetch pattern (chunk by month).
2. **Normalisation.** Convert GDELT tone scores (`AvgTone`) to the
   project's [-1, +1] convention. Compare on a recent week against the live
   VADER/FinBERT output to calibrate the mapping.
3. **Backfill.** One-off script `scripts/backfill_sentiment.py` that
   populates `outputs/sentiment_cache.jsonl` with per-date rows tagged
   `source=gdelt_backfill`. Preserve existing live rows — latest-wins is
   the current convention; change to first-wins by date so backfill never
   overwrites live data (flag `BACKFILL_PRESERVE_LIVE=True`).
4. **Provenance.** Every cache row gets `source` + `ingested_at` +
   `gdelt_query_hash`.
5. **Contamination audit.** Add a test that picks a date, rolls back time
   to `date - 1`, asserts the backfilled sentiment for that date doesn't
   leak into the bar-t computation.
6. **License/TOS note.** GDELT data is released under a public-benefit
   license; note it in README and in the dataset snapshot.

**Alternate / premium sources** (if GDELT noise is unacceptable): RavenPack,
Bloomberg News archive, Reuters StarMine. These are paid — defer unless
budget changes.

---

### L2.2 Cost model (commission + slippage)

**Why it matters.** Backtest reports gross R — zero friction. A 2R winner
after a realistic spread + commission + slippage can become a 1.6R winner,
and a signal whose expectancy is 0.3R gross becomes negative net. The
engine's top-line "5-year proof" is structurally optimistic.

**Complexity.** M.

**Affected files.** `backtest/engine.py`, `backtest/metrics.py`, `config.py`.

**Implementation sketch.**

1. Add `COMMISSION_PER_ROUNDTRIP` (dollars or bps) and `SLIPPAGE_BPS_MARKET`
   / `SLIPPAGE_BPS_STOP` / `SLIPPAGE_BPS_TP` to `config.py`.
2. In `_simulate`, on entry: entry\_price *= (1 + slip\_bps/1e4) for buy,
   (1 − slip\_bps/1e4) for sell. On stop fill: apply `SLIPPAGE_BPS_STOP`
   *adverse* to the position. TPs can be limit orders and slip less or 0.
3. Deduct commission from the R calc (convert dollars-per-contract to bps
   of entry price).
4. Metrics table should report `net_expectancy_R` alongside
   `gross_expectancy_R`.
5. Parameter sweep: regenerate the existing 5-year backtest at three
   friction settings (low / mid / high) and publish all three in
   `outputs/backtest_summary.json`.

---

### L2.3 Position sizing module

**Why it matters.** The engine outputs a signal and a stop, but no size.
Without size, expectancy in R doesn't translate to expectancy in dollars,
the user can't set a loss budget, and no kill-switch or drawdown-based
de-risking is possible. This is the single biggest gap between
"decision-support tool" and "trading system."

**Complexity.** M for fixed-fractional, L if equity-curve-aware.

**Affected files.** new `risk/position_sizing.py`, `backtest/engine.py`,
`main.py`, `app.py`, `templates/index.html`.

**Implementation sketch.**

1. **Fixed-fractional.** `size = (account_equity × risk_pct) / stop_distance`.
   Config: `ACCOUNT_EQUITY`, `RISK_PCT_PER_TRADE` (default 0.5%).
2. **Kelly fraction.** Compute empirical win rate and avg win/loss from
   rolling 250-bar backtest window. `kelly = win − (1−win)/R`. Apply a
   half-Kelly cap.
3. **Volatility targeting.** Size so each trade's expected daily P&L
   standard deviation equals a target (e.g. 10 bps of equity).
4. **Dashboard.** Show `suggested_size_units` and
   `suggested_dollar_risk` on the trade card.
5. **Backtest.** Accept an `account_equity` start, compound it, report
   `final_equity`, `cagr`, `max_drawdown_pct`, `max_drawdown_dollars`.

---

### L2.4 Sharpe, Sortino, profit-factor

**Why it matters.** `backtest/metrics.py` reports expectancy, win rate, max
drawdown. None of these are risk-adjusted in the industry sense. Sharpe,
Sortino, profit factor, Calmar, and max-adverse-excursion are the minimum
set any external reviewer expects.

**Complexity.** S.

**Affected files.** `backtest/metrics.py`.

**Implementation sketch.**

1. Convert trade-log R to daily equity returns (requires position sizing,
   see L2.3) or use per-trade R time series directly.
2. `sharpe = mean(r) / std(r) × sqrt(252)`; `sortino` uses downside std;
   `profit_factor = sum(wins) / abs(sum(losses))`; `calmar = cagr / abs(max_dd)`.
3. Add to `metrics.summary` output and to the dashboard backtest card.

---

### L2.5 Bootstrap CI on expectancy

**Why it matters.** A 5-year backtest with 80 trades produces one number:
"expectancy = 0.42R". Resampling the trade order (block bootstrap to
preserve autocorrelation) gives a distribution; the 95% CI might be
`[−0.05R, +0.85R]`. The point estimate alone is not a claim.

**Complexity.** M.

**Affected files.** new `backtest/bootstrap.py`, `backtest/metrics.py`.

**Implementation sketch.**

1. Block bootstrap with block size ≈ sqrt(n\_trades); N=10\,000 resamples.
2. Report `expectancy_mean`, `expectancy_p5`, `expectancy_p95`,
   `pct_positive` (share of bootstraps with mean > 0).
3. Repeat for Sharpe, profit factor.
4. Emit CI on the dashboard trade card and in the JSON artifact.

---

### L2.6 Deflated Sharpe for grid search

**Why it matters.** `backtest/grid_search.py` evaluates 4×3×3 = 36 (or 4×2×2
= 16) parameter combinations and reports the top performer. Multiple-
hypothesis bias inflates Sharpe ratios substantially. Deflated Sharpe
(Lopez de Prado) accounts for `n_trials`, the variance of Sharpes across
trials, and `n_samples`, producing a de-inflated estimate.

**Complexity.** M.

**Affected files.** `backtest/grid_search.py`, new `backtest/bootstrap.py`
(shared).

**Implementation sketch.**

1. After grid run, compute `sharpe_i` for each profile.
2. `DSR = ((sharpe_max − E[sharpe_trials]) / σ_sharpe_trials) × correction`
   (closed form in Lopez de Prado 2014).
3. Report `deflated_sharpe`, `p_value_deflated`.
4. Fail-loud warning if top-ranked profile has `p_deflated > 0.05` — the
   grid found noise, not signal.

---

### L2.7 Walk-forward cross-validation

**Why it matters.** The grid search currently tunes on the full history and
reports the best in-sample. A walk-forward harness trains on years 1–3,
tests on year 4, rolls forward, and reports the *out-of-sample* performance
of the *in-sample-optimal* parameters. Param-stability is usually worse
than the headline.

**Complexity.** L.

**Affected files.** new `experiments/walk_forward.py`,
`backtest/grid_search.py` refactor.

**Implementation sketch.**

1. Slice history into N folds of one year each.
2. For each fold `i`: grid-search on `[0..i)`, evaluate on `[i]`, record
   `(in_sample_best_params, out_of_sample_metrics)`.
3. Report **param stability**: what fraction of folds picked the same
   top-3 profile? If <50%, the model is unstable.
4. Report **out-of-sample expectancy CI** aggregated across folds — this
   is the honest number.

---

### L2.8 Ablation harness

**Why it matters.** `memory/*_finding.md` notes (event gate, real yields,
COT) are hand-run ablations recorded in commit messages. These should be a
single reproducible command. Without a harness, future changes break
ablation invariants silently.

**Complexity.** M.

**Affected files.** new `experiments/ablation.py`, `backtest/engine.py`
(add factor-disable knobs).

**Implementation sketch.**

1. Add `disabled_factors: set[str]` to the backtest params (L1.5 dataclass).
2. When a factor is disabled, set its score to 0 before weighting.
3. `experiments/ablation.py` loops over {∅, {sentiment}, {dxy}, {yield},
   {gold}, {vix}, {vwap}, {vp}, {cot}} (and the gate variants for
   `event_gate_enabled` and `cot_enabled`). Report a table.
4. Optionally all pairwise ablations (can quantify interactions).

---

### L2.9 Null-hypothesis benchmark

**Why it matters.** "Our engine's expectancy is 0.42R" is meaningless
without a baseline distribution. A Bernoulli(empirical\_BUY\_rate) engine
evaluated over the same bars with the same stop/TP geometry produces a
null distribution; the engine must beat it at p<0.05 to claim edge.

**Complexity.** M.

**Affected files.** new `experiments/null_bench.py`.

**Implementation sketch.**

1. Count BUY / SELL / HOLD frequencies in the real backtest: `p_B`, `p_S`,
   `p_H`.
2. Synthetic run: replace the signal each bar with a draw from the
   multinomial `(p_B, p_S, p_H)`.
3. Apply identical stop/TP geometry (same ATR, same VP targets) from
   `trade_setup`.
4. Run 10k synthetic backtests; compare engine expectancy to the null
   distribution; emit `p_value_vs_null`.

---

### L2.10 Experiment registry (SQLite)

**Why it matters.** Today's backtest emits a JSON file under `outputs/` keyed
by timestamp. Over hundreds of runs, comparing "configuration X from
yesterday" to "configuration Y from last week" requires filesystem archaeology.
A 5-column SQLite table solves it.

**Complexity.** S.

**Affected files.** new `experiments/registry.py`, `backtest/engine.py`
(call at end of run), optional dashboard view.

**Implementation sketch.**

1. `runs(run_id TEXT PK, git_sha TEXT, config_hash TEXT, params JSON,
   metrics JSON, started_at TEXT, finished_at TEXT, notes TEXT)`.
2. `config_hash = sha256(sorted(kv-pairs of config))`.
3. On every backtest, call `registry.log(run_id, git_sha, config_hash,
   params, metrics)`.
4. Small CLI: `python -m experiments.registry list`, `... diff RUN_A RUN_B`.
5. Dashboard: add `/api/runs` + a table view.

---

### L2.11 Data snapshotting per run

**Why it matters.** yfinance, FRED, and CFTC are all live sources. Two
runs of the same "historical" backtest can produce different trade lists
because yfinance revised a split, FRED revised a print, or CFTC is late.
Research requires replayable inputs.

**Complexity.** M.

**Affected files.** `market/data_fetcher.py`, `market/fred_fetcher.py`,
`positioning/cot_fetcher.py`, `backtest/engine.py`, new `data/snapshots/`.

**Implementation sketch.**

1. Introduce `snapshot_id` (timestamp) per backtest run.
2. After each live pull, persist the raw dataframe as parquet under
   `data/snapshots/<snapshot_id>/<source>.parquet`.
3. The fetchers gain an optional `snapshot_id` kwarg — if provided, read
   from disk instead of live.
4. Register `snapshot_id` in the experiment registry (L2.10).
5. Archive older snapshots on a retention policy (keep last 20 or
   > 6 months old are deletable).

---

### L2.12 Live data drift monitor

**Why it matters.** Intermittent yfinance revisions, FRED print delays, and
CFTC data-publishing errors silently change backtest inputs. A daily diff
against the previous snapshot catches these.

**Complexity.** S once L2.11 is in place.

**Affected files.** new `experiments/drift_monitor.py`, `scheduler.py`.

**Implementation sketch.**

1. After the daily snapshot, compare to the previous day's snapshot.
2. For each series, compute `max_abs_diff_pct` on the 30-day overlap.
3. If > 0.1% (tune per series), log a WARNING and write a diff artifact.

---

### L2.13 Calibrated confidence

**Why it matters.** HIGH/MEDIUM/LOW confidence labels are heuristic (count of
factors aligned + quality adjustments). They have never been tied to
empirical hit rates. A calibrated version would report
"HIGH = historically 63% win rate, MEDIUM = 48%, LOW = 31%" and let the
user size by confidence.

**Complexity.** S to compute, M to expose and validate.

**Affected files.** `signals/confidence.py`, `backtest/metrics.py`,
`templates/index.html`.

**Implementation sketch.**

1. In metrics, bucket trades by `confidence_at_entry` and report hit rate,
   expectancy, count per bucket.
2. Add to dashboard and to the trade card (`HIGH (hist. 63%)`).
3. Over time, fit an isotonic regression mapping the raw confidence
   formula to an empirical probability.

---

### L2.14 Factor correlation / VIF audit

**Why it matters.** DXY and real-yields move together much of the time. If
both get positive weight, the signal is effectively double-counting the
same macro. The current weights (dxy=1.00, yield=0.80) were trimmed by
hand. A correlation matrix and variance-inflation-factor (VIF) calc makes
this explicit.

**Complexity.** S.

**Affected files.** new `experiments/correlation_audit.py`.

**Implementation sketch.**

1. Compute per-bar factor scores over the full history.
2. Pearson correlation matrix (plot as heatmap).
3. VIF for each factor vs the others; flag VIF > 5.
4. Suggest weight adjustments; ideally re-fit weights with a regularised
   regression (ridge) on forward returns as ground truth.

---

### L2.15 Formal event-study around CPI/FOMC

**Why it matters.** The event-gate finding ("day +50% exp, swing −0.06 R")
is compelling but coarse. An event-study shows the distribution of returns
in the T−3..T+3 window around each event type, separating by surprise
(actual − consensus). This turns "gate on" into a defensible choice.

**Complexity.** M.

**Affected files.** new `experiments/event_study.py`.

**Implementation sketch.**

1. For each event type (FOMC, CPI, NFP, PCE), collect the event dates.
2. For each date, extract the ±3-day return panel for gold and for each
   factor score.
3. Report mean return per offset, dispersion, skew; plot.
4. Compare pre-gate vs post-gate engine R in the same windows (paired
   test).

---

## Level 3 — Advanced Capability

### L3.1 Intraday engine

**Why it matters.** The "day" profile today is daily bars with tighter
thresholds — not an intraday engine. True intraday would use 5-min or
15-min bars, session-aware gates (London open, NY open, Asia), and a
volume-profile computed on an intraday window. This is the main claim in
the project name ("day trading") that is not actually backed by the code.

**Complexity.** XL.

**Affected files.** almost everything:
`market/data_fetcher.py` (needs a minute-bar source — yfinance is limited
to 60 days of 1-min; Polygon/Databento paid),
`market/indicators.py` (window recompute on intraday cadence),
`backtest/engine.py` (bar granularity + session handling),
`events/blackout.py` (intraday event timing instead of daily).

**Implementation sketch.**

1. Pick a data source. Polygon has good 1-min for futures and ETFs;
   Databento is better quality; IBKR data feed if the user has access.
2. Store intraday snapshots under `data/snapshots/<run_id>/intraday/`.
3. Rework `indicators.compute` to operate on any bar size and take a
   `lookback_bars` rather than `lookback_days`.
4. Rework `trade_setup` stops/targets to use intraday ATR and an
   intraday-appropriate VP window (session or rolling N bars).
5. Add session classifier (Asia / London / NY / overlap) and allow the
   gates to depend on session.
6. Intraday backtest cost model (L2.2) will dominate — spreads and
   commissions matter per trade.

---

### L3.2 Feature store

**Why it matters.** Every backtest bar recomputes all indicators from
scratch from the price panel. A 5-year backtest recomputes EMA/ATR/VWAP/VP
for each of ~1250 bars. Caching indicators keyed by `(symbol, date,
indicator_config_hash)` makes grids, ablations, and walk-forward
tractable.

**Complexity.** L.

**Affected files.** new `market/feature_store.py`, `market/indicators.py`.

**Implementation sketch.**

1. Choose backend: Parquet files partitioned by symbol + month, or
   SQLite/DuckDB.
2. Keyed schema: `(symbol, date, indicator, config_hash) → value`.
3. `indicators.compute` checks cache first, writes back on miss.
4. `clear_cache()` command for safety during schema changes.

---

### L3.3 Short-side asymmetry diagnostic

**Why it matters.** `LONG_ONLY = True` is a band-aid that acknowledges the
short side lost money in the backtest without explaining why. A
research-grade fix investigates:
(a) is it gold's 2015–2025 upward drift?
(b) are the thresholds symmetric but the returns positively skewed?
(c) is there a factor that only moves symmetrically during risk-on
(e.g. CB buying that supports gold floor)?

**Complexity.** L.

**Affected files.** new `experiments/short_side_audit.py`.

**Implementation sketch.**

1. Run the engine with `LONG_ONLY=False`. Collect only SELL trades.
2. Bin by market regime (bull / bear / flat per `backtest.engine._regime`).
   In the bear regime, does SELL expectancy recover? If yes, the fix is
   a regime-conditional short gate, not a total block.
3. Analyse the factor distribution at SELL entries vs successful-SELL
   entries; look for a factor that separates.
4. Test asymmetric thresholds (e.g. require `total ≤ −3` for SELL, still
   `≥ +2` for BUY).

---

### L3.4 Paper-trading bridge

**Why it matters.** Between "backtest says it works" and "live trading" sits
"paper trading" — run the live signal every day, book a synthetic trade,
track PnL, measure calibration. Today the scheduler runs a signal and
prints — no synthetic book.

**Complexity.** L.

**Affected files.** new `trading/paper_broker.py`, `scheduler.py`, dashboard.

**Implementation sketch.**

1. `paper_broker.Book` persists open positions and closed-trade log to
   `outputs/paper_book.json`.
2. On each scheduler run, if a new BUY/SELL signal appears with
   `trade_valid=True`, open a paper trade at next day's open (avoid same-
   bar entry bias).
3. Close on stop, TP, or max-hold.
4. Daily PnL email/slack/dashboard.
5. Compare live paper vs backtest-predicted distribution monthly.

---

### L3.5 Benchmark suite

**Why it matters.** Today the dashboard's "5-year proof" is unanchored. A
simple benchmark suite — buy-and-hold gold, 50/200 MA cross, random-entry
with identical geometry — contextualises the engine's R.

**Complexity.** M.

**Affected files.** new `experiments/benchmarks.py`, `backtest/metrics.py`.

**Implementation sketch.**

1. Buy-and-hold gold return over the same window.
2. Moving-average crossover (50/200) with identical stop/TP geometry.
3. Random-entry with identical geometry, 1000 resamples.
4. Report all four (engine + 3 benchmarks) in a single table in the
   dashboard and in JSON.

---

### L3.6 LLM panel robustness

**Why it matters.** The agent panel currently supports Ollama or Anthropic
backends but their behaviour is subtly different. Default is Ollama with
`qwen2.5:3b` — a small model whose outputs are terse and occasionally
off-format. Production-grade would test across backends and ensure
stable behaviour.

**Complexity.** M.

**Affected files.** `sentiment/agent_panel.py`, `tests/`.

**Implementation sketch.**

1. Golden-output test with fixtures for each persona and both backends.
2. JSON-schema validation on panel responses; reject malformed.
3. Backend fallback chain (Ollama → Anthropic → skip panel) with logged
   reason.
4. Panel weight in `AGENT_PANEL_WEIGHTS` should degrade to 0 if the panel
   failed, with `vader`/`finbert` re-normalised.

---

### L3.7 ML layer on top of rule engine

**Why it matters.** Once historical sentiment (L2.1) and the feature store
(L3.2) are in place, training a supervised model — say, gradient-boosted
trees — on the same factor panel with forward-N-day returns as target is a
small-effort, high-insight experiment. The rule engine is interpretable but
likely suboptimal; the ML layer benchmarks how much signal the rules leave
on the table.

**Complexity.** L.

**Affected files.** new `experiments/ml_bench.py`.

**Implementation sketch.**

1. Features: the eight factor scores per bar, plus raw indicator values.
2. Target: forward 5-day return (swing) or 1-day return (day).
3. Walk-forward CV, hyperparameter tune via optuna, report SHAP for
   feature importance.
4. If ML model beats rule engine out-of-sample: either retire the rule
   engine or use the ML prediction as an additional factor.
5. If ML model doesn't beat the rule engine: that's a strong endorsement
   of the rules.

---

### L3.8 Multi-asset generalisation

**Why it matters.** The engine is XAUUSD-specific: hardcoded symbols,
gold-specific factor weights, gold-specific event calendar (PCE, CPI are
general; gold-specific factors like COT-gold and DXY-vs-gold are not).
Generalising to silver, platinum, copper, or broader commodities
multiplies testable universe and statistical power.

**Complexity.** XL.

**Affected files.** `config.py` (split per-asset), `market/`, `signals/`,
`positioning/` (COT per commodity).

**Implementation sketch.**

1. Extract all hardcoded gold refs into an `Asset` dataclass
   (`primary_symbol`, `cot_code`, `macro_factors`, `event_calendar`,
   `weights`).
2. Backtest each asset in isolation with its own calibrated weights.
3. Optional cross-asset correlation dashboard.

---

### L3.9 Explainability / factor attribution

**Why it matters.** Reasoning bullets are deterministic but equally
weighted in presentation. "Which factor moved the needle most this run?"
is unanswered. Shapley-style attribution over the weighted-sum signal is
cheap and informative.

**Complexity.** M.

**Affected files.** `signals/signal_engine.py`, `templates/index.html`.

**Implementation sketch.**

1. For each factor `i`, compute contribution = `score_i × weight_i`.
2. Sort factors by absolute contribution; show "top 3 drivers" on the
   dashboard with sign and magnitude.
3. In the JSON artifact, include the full factor-contribution vector.
4. Over time, show "rolling 60-day contribution share" per factor — a
   monitoring view of whether a factor has gone silent or dominant.

---

## Sequencing

A reasonable delivery order that respects dependencies:

1. **Sprint 1 (hygiene, 1–2 weeks).** L1.1–L1.11 in parallel.
2. **Sprint 2 (reproducibility, 2 weeks).** L2.10 (registry) → L2.11 (snapshots)
   → L2.12 (drift monitor).
3. **Sprint 3 (data unblock, 2–3 weeks).** L2.1 (GDELT backfill). Without
   this, the following sprints compute on a zeroed sentiment column.
4. **Sprint 4 (realism, 2 weeks).** L2.2 (costs) + L2.3 (position sizing) +
   L2.4 (Sharpe/Sortino/PF). Unlocks money-terms metrics.
5. **Sprint 5 (validation, 3 weeks).** L2.5 (bootstrap CI) + L2.6 (deflated
   Sharpe) + L2.7 (walk-forward) + L2.9 (null bench) + L2.8 (ablation).
6. **Sprint 6 (insight, 1–2 weeks).** L2.13 (calibrated confidence) + L2.14
   (correlation/VIF) + L2.15 (event study).
7. **Sprint 7+ (advanced).** L3.2 feature store → L3.1 intraday → L3.7 ML
   layer. L3.3/L3.4/L3.5/L3.6/L3.8/L3.9 can be interleaved as standalone
   tracks.

**Hard dependencies.**

- L2.1 (sentiment backfill) precedes every sentiment-touching ablation and
  the ML layer. Skip it and downstream findings are structurally invalid.
- L2.11 (snapshots) precedes L2.7 (walk-forward) — without replayable data,
  walk-forward results drift between runs.
- L1.5 (grid-search globals) precedes L2.7 (walk-forward) and L2.8
  (ablation) — both rely on per-iteration parameter isolation.
- L2.3 (position sizing) precedes L2.4 (Sharpe/Sortino) — risk-adjusted
  metrics are dollar-denominated.
- L3.2 (feature store) precedes L3.1 (intraday) and L3.7 (ML) — both
  explode compute without it.
- L3.4 (paper trading) precedes any live-execution work (intentionally out
  of current roadmap scope).

**Parallelisable.** Any single Level-1 item; L3.3 (short-side audit), L3.5
(benchmarks), L3.9 (attribution) can run independently once Level-1 is
done.

---

*End of roadmap.*
