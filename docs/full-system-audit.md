# NewsSentimentScanner — Full System Audit

Forensic reverse-engineering of the Gold/XAUUSD bias engine in this repository.
All findings grounded in real file paths and code. Each statement marked as:

- **[Confirmed]** — read directly in the code
- **[Inferred]** — derived from code structure / imports
- **[Gap]** — not found / needs verification / missing

Repo root: `/Users/ray/Dev/Sentiment analysis projtect`
Branch: `feature/day-trading` (working tree has modifications in
`backtest/engine.py`, `config.py`, `main.py`, `market/data_fetcher.py`,
`signals/signal_engine.py`, `templates/index.html` plus untracked `events/`,
`market/fred_fetcher.py`, `positioning/`, `tests/test_cot.py`,
`tests/test_event_gate.py`, `tests/test_fred_fetcher.py`).

---

## 1. Executive Summary

This is a **rules-based, transparent, gold-only decision-support pipeline**,
not a machine-learning trading system. There is no trained model, no labeled
dataset, no gradient-descent loop. It combines:

1. News sentiment (VADER + FinBERT + optional LLM persona panel)
2. Daily OHLCV from yfinance (gold `GC=F`, DXY `DX-Y.NYB`, VIX `^VIX`)
3. FRED real-yield series (`DFII10`, public CSV endpoint, no key)
4. CFTC COT positioning (`088691` gold, public Socrata JSON, no key)
5. Scheduled event calendar (FOMC / CPI / NFP / PCE, hardcoded dates)

Each factor is mapped to an integer score in `-3…+3`, weighted via
`config.SCORE_WEIGHTS`, summed, and bucketed to `STRONG_BUY … STRONG_SELL`.
Three gating layers (veto, long-only / SMA200, event blackout) can force the
final signal to `HOLD`. A trade-setup layer computes entry / stop / take-profit
using ATR-scaled stops and Volume-Profile-aware targets, then rejects setups
that miss the timeframe's minimum RR (1:3 swing, 1:1.5 day).

A walk-forward backtest (`backtest/engine.py`) replays the same pipeline over
historical bars with slice-to-bar-t indicators. A sentiment JSONL cache
(`outputs/sentiment_cache.jsonl`) is the only way historical sentiment is
reintroduced — it is **forward-only** because RSS cannot be backfilled.

Strengths: clean modular decomposition, deterministic scoring, transparent
reasoning output, real backtest with partial-TP/trailing/regime breakdown.

Weaknesses: no statistical validation (Sharpe, Sortino, bootstrap CI, deflated
Sharpe, walk-forward stability, cross-validation). No experiment tracking. No
reproducibility (yfinance and Google News are live and non-deterministic). The
sentiment factor is **structurally broken for historical backtests** because
pre-deployment history has no cached scores and falls back to 0. Event calendar
is a hardcoded list, not a live feed. Short side is effectively disabled
(`LONG_ONLY=True`) after the short backtest showed zero edge.

---

## 2. What This System Actually Is

- **Purpose [Confirmed, README.md:3-7]**: local decision-support for gold bias,
  explicitly *not* financial advice, *not* an automated executor.
- **Form factor [Confirmed]**: Python CLI (`main.py`, `python -m backtest`)
  plus a single-page Flask dashboard (`app.py`, port `5001`,
  `templates/index.html` ~1894 lines) and an APScheduler `BackgroundScheduler`
  (`scheduler.py`) for interval-based auto-runs.
- **Runtime model**: synchronous per-article scrape + VADER/FinBERT/Ollama
  panel in a `ThreadPoolExecutor(max_workers=6)`
  ([main.py:173-179](../main.py#L173-L179)). Market fetch, scoring, signal
  build, trade-setup, save.
- **Data products**: each run produces `sentiment_<ts>.csv`,
  `sentiment_<ts>.json`, and optionally `signal_<ts>.json` in `outputs/`;
  backtests produce `backtest_<tf>_<ts>.json` and `grid_<tf>_<ts>.json`. COT
  and per-day sentiment are persistent JSONL caches in `outputs/`.

---

## 3. Full Architecture Map

```
                    ┌────────────────────────── main.py (CLI) ───────────────────────────┐
                    │                                                                       │
                    │  parse_args() ──► run_sentiment() ──► run_signal() ──► save JSON/CSV │
                    │                          │                    │                       │
                    │                          ▼                    ▼                       │
                    │                     news/*                market/* + events/*         │
                    │                     sentiment/*            positioning/*              │
                    │                                              signals/*                │
                    └───────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
                                            outputs/*.json/csv
                                    outputs/sentiment_cache.jsonl
                                    outputs/cot_gold.jsonl
                                                    │
                    ┌───────────────────────────────┴───────────────────────────────────────┐
                    │                                                                       │
                    ▼                                                                       ▼
       app.py (Flask, :5001)                                                  backtest/engine.py (walk-forward)
         - load_runs()                                                         - replays full pipeline per bar
         - render index.html                                                   - _simulate() for stop/TP/partial
         - /api/run → scheduler.trigger_run()                                  - metrics.report()
         - /api/status                                                         - grid_search.py over parameter grid
```

Top-level packages ([Confirmed] via directory listing):

| Package | Role | Key files |
|---------|------|-----------|
| `news/` | RSS ingestion + scraping + dedup | `rss_fetcher.py`, `article_scraper.py`, `dedup.py` |
| `sentiment/` | VADER / FinBERT / LLM panel + aggregation + daily JSONL cache | `vader_analyzer.py`, `finbert_analyzer.py`, `agent_panel.py`, `aggregator.py`, `cache.py` |
| `market/` | OHLCV fetch + indicators + factor scoring | `data_fetcher.py`, `fred_fetcher.py`, `indicators.py`, `trend_scoring.py` |
| `events/` | FOMC/CPI/NFP/PCE calendar + blackout gate | `calendar.py`, `blackout.py` |
| `positioning/` | CFTC COT fetcher + z-score contrarian scoring | `cot_fetcher.py`, `cot_scoring.py` |
| `signals/` | Signal engine, confidence, reasoning, trade setup, RR validate | `signal_engine.py`, `confidence.py`, `reasoning.py`, `trade_setup.py`, `risk_management.py` |
| `backtest/` | Walk-forward replay, metrics, grid search | `engine.py`, `metrics.py`, `grid_search.py`, `__main__.py` |
| `utils/` | Logger, IO helpers, progress counter, text cleaner | `logger.py`, `io_helpers.py`, `progress.py`, `text_cleaner.py` |
| `tests/` | pytest suite (8 files, ~619 lines) | `test_signal_engine.py`, `test_engine_simulate.py`, `test_event_gate.py`, `test_cot.py`, `test_fred_fetcher.py`, `test_metrics.py`, `test_sentiment_cache.py`, `conftest.py` |
| `templates/` | Dashboard | `index.html` (1894 lines, inline CSS/JS) |

---

## 4. End-to-End Execution Flow

### 4.1 Live pipeline — `python main.py --signal --trade-setup --timeframe day`

Step-by-step [Confirmed, `main.py`, `scheduler.py`, `signals/signal_engine.py`]:

1. `main.py:1-31` — boot: `dotenv.load_dotenv()`; module-level loggers via
   `utils/logger.py:4-19`.
2. `parse_args()` `main.py:36-71` — argparse reads `--mode`, `--model`,
   `--limit`, `--output-dir`, `--signal`, `--trade-setup`, `--timeframe`.
3. `run_sentiment()` `main.py:90-208`:
   - If `finbert` requested: `FinBERTAnalyzer()` constructed once (loads
     HuggingFace pipeline via `transformers.pipeline("text-classification",
     model="ProsusAI/finbert", max_length=512)`, `sentiment/finbert_analyzer.py:27-35`).
   - If `AGENT_PANEL_ENABLED` (default `True` in config, backend=`"ollama"`):
     `AgentPanel()` pings Ollama at `http://localhost:11434/api/tags` and
     verifies `qwen2.5:3b` is pulled (`sentiment/agent_panel.py:131-148`).
   - `fetch_articles()` `news/rss_fetcher.py:13-36` — loops through
     `config.RSS_QUERIES` (7 gold queries), hits
     `https://news.google.com/rss/search?q=…`, accumulates entries.
   - `deduplicate()` `news/dedup.py:8-40` — two-pass: URL set + normalized
     title (lowercase, punctuation stripped, whitespace collapsed).
   - `ThreadPoolExecutor(max_workers=6)` fans articles into `_process_article`.
     Each worker:
     - `scrape_article()` resolves Google News wrapper URLs via
       `googlenewsdecoder.gnewsdecoder()` (`news/article_scraper.py:21-44`),
       fetches with `requests.get`, `BeautifulSoup.find_all("p")` extracts
       paragraph text with retry + `1.5^attempt` backoff.
     - `_build_text()` composes input for sentiment based on mode
       (`title`, `body`, `combined`).
     - `vader_analyze()` (always instant),
       `finbert.analyze()` (if ready, truncated to `MAX_TEXT_CHARS=1800`),
       `agent_panel.analyze()` (one LLM call per article, 5 personas in a
       single JSON prompt, `sentiment/agent_panel.py:51-62`).
     - `aggregate()` `sentiment/aggregator.py:6-74` — weighted average via
       `config.AGENT_PANEL_WEIGHTS = {"vader": 0.2, "finbert": 0.4,
       "panel": 0.4}`, label via majority vote, tie-break
       `panel → finbert → vader`.
   - `_build_sentiment_summary()` counts distributions, picks top
     positive/negative by absolute `final_score`, averages panel variance.
   - `save_csv()` + `save_json()` to `outputs/sentiment_<ts>.{csv,json}`.
   - `sentiment.cache.append(avg_score, n_articles)` appends one line to
     `outputs/sentiment_cache.jsonl` for future backtests.
   - `print_summary()` to stdout.
4. `run_signal()` `main.py:281-406`:
   - Resolves `tf = config.TIMEFRAME_PROFILES[timeframe]` (swing or day).
   - `market.data_fetcher.fetch_all(lookback_days=tf["lookback_days"])` —
     yfinance for gold / dxy / vix, FRED CSV for `yield_10y` (DFII10
     overrides any same-named yfinance entry, `market/data_fetcher.py:55-65`).
   - `indicators.compute()` on each series (`market/indicators.py:186-272`) —
     EMA short/long, SMA200, n-day return, rolling high/low, ATR (Wilder EMA),
     VWAP, Volume Profile (POC/VAH/VAL), TPO POC.
   - Six `score_*` functions return integer factor scores
     (`market/trend_scoring.py`).
   - `macro_bullish = gold.current > gold.sma200`.
   - Event blackout: when `tf["event_gate"]` (True for `day`, False for
     `swing`), `events.blackout.is_blackout(today)` checks `-1/+1` day windows
     around FOMC / CPI / NFP / PCE.
   - COT positioning: when `tf["cot_enabled"]` (True for `swing`, False for
     `day`), `cot_fetcher.ensure_fresh()` refreshes `outputs/cot_gold.jsonl`
     if older than `STALE_DAYS=10`, then
     `cot_scoring.score_at(records, today)` returns a 52-week z-score fade.
   - `signal_engine.run(...)` `signals/signal_engine.py:109-195`:
     - Weighted total = `Σ score_i × SCORE_WEIGHTS[i]`.
     - `_map_total()` → STRONG variants require `|gold_score| ≥ 2`.
     - `_veto()` — BUY blocked if gold<0 or dxy==-2 or yield==-2;
       SELL blocked if gold>0 or dxy==+2.
     - `LONG_ONLY` gate strips `SELL/STRONG_SELL → HOLD`.
     - `SMA200_GATE` strips BUY side when `macro_bullish is False`.
     - Event blackout forces `HOLD` last.
   - `confidence.compute()` — count of factors aligned with gold direction,
     downgrade on data-quality failures (headline-only, <5 articles, ≥2 market
     fetch failures, panel variance ≥ 0.35).
   - `reasoning.build()` — deterministic bullet list per factor bucket.
   - `trade_setup.compute()` — invalidation = `min(EMA20, 14d_low)` (BUY) or
     `max(EMA20, 14d_high)` (SELL); stop = invalidation ± `atr_stop_mult × ATR`;
     TP prefers VAH/VAL, then TPO POC, then `MIN_RR × Risk`.
   - `risk_management.validate()` — rounds `RR` and rejects if
     `rr < tf["min_rr"]`.
   - `save_json()` → `outputs/signal_<ts>.json`.
5. Terminal prints final banner via `utils/io_helpers.py:91-160`.

### 4.2 Dashboard flow

1. `python app.py` → `Flask(__name__)` starts on `:5001` with
   `use_reloader=False`.
2. If `config.SCHEDULER_ENABLED` is `True` (default: `False`), calls
   `scheduler.init_scheduler()` which registers a
   `BackgroundScheduler`+`IntervalTrigger` at 30 min (day) or 120 min (swing).
3. GET `/` → `index()` calls `load_runs()` to pair `sentiment_*.json` and
   `signal_*.json` by shared timestamp, filters by `?tf=swing|day|all`,
   renders `templates/index.html` with `runs`, `sentiment`, `signal`,
   `trade_viz`, `scheduler`, `backtest`, `engine_cfg`, `macro_bullish`,
   `cache_days`.
4. POST `/api/run` → `scheduler.trigger_run(timeframe)` spawns a daemon thread
   running `_run_pipeline()` which invokes `main.run_sentiment` +
   `main.run_signal`. Returns 409 if a run is already in flight
   (`scheduler.py:56-60`).
5. GET `/api/status` → `scheduler.get_status()` dict including
   `progress.snapshot()` (thread-safe `current/total/stage`).

### 4.3 Backtest flow

`python -m backtest --timeframe swing --days 730`:

1. `backtest/__main__.py:32-57` parses args, calls `engine.run()`.
2. `engine.run()` `backtest/engine.py:212-336`:
   - `data_fetcher.fetch_all(lookback_days)` — same live fetcher.
   - `sentiment_cache.load()` once; `cot_fetcher.ensure_fresh()` once;
     `get_events(start, end)` once.
   - Loops `t` from `WARMUP_BARS=60` to `n-1`:
     - Skip if still inside an open trade (default `allow_overlap=False`).
     - Slice all series to `[:t+1]`, recompute indicators on the slice.
     - Lookup sentiment by `gold_df.index[t].date()`; fallback `None → 0`.
     - Build `macro_bullish`, `event_reason`, `cot_score`.
     - `signal_engine.run(...)` — same function as live.
     - If directional + `trade_valid`: `_simulate(...)` walks forward up to
       `max_hold` bars, applying trailing stop, partial TP (+BE on remainder),
       and STOP/TP/TIME exits.
   - `metrics.print_report(trades)` and JSON dump.
3. `grid_search.py` wraps `engine.run` across a 4×3×3 or 4×2×2 grid of
   `min_rr`, `atr_stop_mult`, `max_hold`, `trail_atr_mult`,
   `trail_activate_r`. Mutates module-level `config.TRAIL_*` per iteration
   (wrapped in try/finally to restore, `grid_search.py:87-117`) — **this is a
   subtle global-state mutation hazard if two backtests ever run in parallel
   in the same process**.

---

## 5. Frontend Routing and Rendering

- `app.py` registers two routes + one Jinja view [Confirmed, `app.py:242-309`]:

| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| GET | `/` | `index()` | Full SSR dashboard |
| GET | `/api/status` | `api_status()` | Scheduler + progress JSON |
| POST | `/api/run` | `api_run()` | Kick a background run |

- Template rendering is **pure server-side Jinja2**. `templates/index.html`
  is one giant file with inline `<style>` and `<script>`. No JS bundler, no
  framework, no client routing.
- Jinja filters registered at `app.py:29-66`: `fmt_score`, `fmt_price`,
  `fmt_conf`, `score_class`, `pct_change`.
- Sections rendered [Confirmed from template skim]:
  - Sticky header + timeframe tab filter (`?tf=all|swing|day`).
  - Scheduler status bar with coloured dot (`ok/running/error`) and Run-Now
    button.
  - Fetch overlay (gold spinner) toggled by the JS that polls `/api/status`.
  - Empty-state card when no runs exist.
  - Signal banner (colour-coded by `sig_class`), displaying raw signal, veto
    notice, SMA200/long-only/event-gate cause, confidence dots, panel
    disagreement.
  - **Engine status strip**: chips for `LONG-ONLY`, `SMA200 GATE`, `MACRO
    BULL/BEAR`, `PARTIAL TP`, `TRAIL`, `MIN RR`, `MAX HOLD`, `ATR STOP`,
    `SENTIMENT CACHE Nd`.
  - Backtest-proof card (reads newest `backtest_<tf>_*.json`, falls back to
    any timeframe if the current one has no file).
  - Trade-setup ladder using `_trade_viz()` pre-computed pct positions.
  - Market snapshot per instrument.
  - Sentiment summary and top positive/negative headlines.
- Client-side state [Inferred]: JS polls `/api/status` on a timer to update
  scheduler chip + progress bar; after `POST /api/run` returns `200` it shows
  the overlay until `scheduler.get_status().running` flips to `False`.
- Auth / session: **None** [Confirmed — no login, no token, no role check].
  Dashboard is bound to `localhost:5001` and `debug=True` in `app.py:315`.
  **[Gap] If ever exposed to a LAN, there is no auth.**

---

## 6. Backend Routing and Services

| Route | Handler | Inputs | Processing | Response |
|-------|---------|--------|-----------|----------|
| `GET /` | `index()` ([app.py:242-291](../app.py#L242)) | `?tf`, `?run` | `load_runs()` → pair JSONs by timestamp; `_load_json()` sentiment + signal; `_load_latest_backtest()`; `_engine_config()`; `_macro_bullish()`; `_trade_viz()` | HTML |
| `GET /api/status` | `api_status()` ([app.py:296-299](../app.py#L296)) | none | `sched.get_status()` | JSON |
| `POST /api/run` | `api_run()` ([app.py:302-309](../app.py#L302)) | `{"timeframe": "..."}` | `sched.trigger_run(timeframe)` spawns daemon thread | `{"ok": bool, "message": str}` (409 if running) |

No middleware, no auth, no validation layer beyond "is it JSON". Error handling
is try/except inside fetchers (`market/data_fetcher.py`, `market/fred_fetcher.py`,
`news/article_scraper.py`, `positioning/cot_fetcher.py`) that all log and
return `None` / empty dict / `0` on failure. Logging is stdout via
`utils/logger.py:4-19`.

Background work: APScheduler `BackgroundScheduler(daemon=True)` with a single
`IntervalTrigger` job `auto_fetch`, plus ad-hoc `threading.Thread(daemon=True)`
for `trigger_run`. Concurrency is protected by `_lock` around the shared
`_state` dict ([scheduler.py:27-37](../scheduler.py#L27)).

---

## 7. Important Functions and Modules

Ranked by operational criticality. Each row: what it does, callers, inputs,
outputs, risks.

| Function | File:Line | Callers | Inputs | Outputs | Risk |
|----------|-----------|---------|--------|---------|------|
| `signal_engine.run` | `signals/signal_engine.py:109-195` | `main.run_signal`, `backtest/engine.run` | 8 scores + macro_bullish + event reason | dict with `total_score`, `signal`, `veto_applied`, `event_gated` | **Core business logic.** Thresholds (6, 2, -2, -6) hand-tuned via grid search. Weighted total uses `config.SCORE_WEIGHTS` globals. |
| `signal_engine._map_total` | `signals/signal_engine.py:52-68` | `run()` | weighted total + gold_score | string label | **STRONG variants require |gold|≥2** — downgrade is silent (not logged with a distinct tag). |
| `signal_engine._veto` | `signals/signal_engine.py:71-106` | `run()` | signal + 3 scores | (signal, bool) | BUY veto condition on `yield_score==-2` is asymmetric — SELL side does not check yields (intentional per bias, but worth flagging). |
| `trade_setup.compute` / `_buy` / `_sell` | `signals/trade_setup.py:26-128` | `main.run_signal`, `backtest.engine.run` | signal, gold indicators, tf profile | trade dict with entry/stop/tp/RR/trade_valid | TP prefers VAH → TPO POC → `MIN_RR × Risk`. Min risk floor = `0.3% × entry` to avoid micro-stops. |
| `risk_management.validate` | `signals/risk_management.py:14-37` | `main.run_signal` only | setup dict + tf | possibly downgraded setup | Live path: applied after `trade_setup.compute` which already checks RR. In the backtest (`backtest/engine.py:309-311`) only `setup.get("trade_valid")` is checked — `validate()` is **not called** in backtest, so a marginal floating-point case could differ between live and backtest. |
| `indicators.compute` | `market/indicators.py:186-272` | live + backtest | daily OHLCV df, tf | dict (current, EMAs, SMA200, return, ATR, VWAP, VP, TPO) | `_ema` uses pandas `ewm(span=window, adjust=False)` — correct; `SMA200` uses `min(200, len)` — **bias risk when history < 200 bars**: returns a smaller-window "SMA200" that is not actually 200-day. |
| `trend_scoring.score_gold/dxy/yield` | `market/trend_scoring.py:19-210` | live + backtest | indicator dict, tf | int | Heuristic EMA-position + n-day move thresholds. All thresholds timeframe-parameterised through `tf`. |
| `trend_scoring.score_vix` | `market/trend_scoring.py:151-178` | live + backtest | indicator dict | int | Level-based (not trend); range `-1…+2` only — asymmetric factor (can never contribute bearish ≤ -2). |
| `trend_scoring.score_vwap / score_volume_profile` | `market/trend_scoring.py:85-148` | live + backtest | gold indicator | int | **`score_volume_profile` returns -2 when `current ≤ val`** (below VAL), but returns `-1` when VAL < current ≤ POC. This makes "below POC" always mildly bearish — even if price is just one tick below the POC within the value area. |
| `score_at` | `positioning/cot_scoring.py:77-79` | live + backtest | records, date | int (-2…+2) | 52-week rolling z-score, requires ≥10 samples. Returns 0 by default outside `|z|>1`. |
| `cot_fetcher.ensure_fresh` | `positioning/cot_fetcher.py:116-131` | live + backtest | none | record list | Network call with 30s timeout. No retry logic. |
| `aggregate` (sentiment) | `sentiment/aggregator.py:6-74` | `main._process_article` | vader/finbert/panel | aggregated dict | Re-normalises weights when a source is missing. Label vote falls through priority `panel → finbert → vader` on tie. |
| `AgentPanel.analyze` | `sentiment/agent_panel.py:181-250` | `main._process_article` | title, body | dict with scores + variance | One LLM call per article; Ollama default. JSON parse is regex-based (`re.search(r"\{.*\}")`), **tolerant but could match the wrong JSON** if the response contains multiple objects. Population variance (not sample variance) used — OK for n=5 personas. |
| `FinBERTAnalyzer.analyze` | `sentiment/finbert_analyzer.py:43-75` | `main._process_article` | text | dict(score/label/confidence) | Constructed once at startup; degrades silently to neutral. **FinBERT is not gold-specific and was trained on financial phrasebank** — commodity/FX context may under- or over-weight. |
| `data_fetcher.fetch_all` | `market/data_fetcher.py:41-67` | live + backtest | days | dict of dfs | FRED entries override same-named yfinance entries — side effect hard to notice without reading the function. |
| `fred_fetcher.fetch_series` | `market/fred_fetcher.py:29-79` | `fetch_all` | series_id, days | synthetic OHLCV df (O=H=L=C=value, V=0) | **VWAP, ATR, Volume Profile, TPO all degenerate for FRED series** (Volume=0 → `_vwap` returns None, VP returns all-None). Good: indicators defensively handle None. |
| `events.blackout.is_blackout` | `events/blackout.py:24-62` | live + backtest | anchor date, optional events | (bool, reason) | Daily granularity; no FOMC minutes or intraday release timestamps. |
| `backtest.engine._simulate` | `backtest/engine.py:77-202` | `engine.run` | gold_df, entry_idx, setup, direction, max_hold | (exit dict, exit_idx) | Intrabar-hit ambiguity resolved by "pessimistic assumption: stop first". Partial TP applied **before** stop/TP check on the same bar — so a bar that grazes partial and then reverses to stop banks the partial. |
| `metrics.report` | `backtest/metrics.py:79-88` | CLI, grid search, dashboard | trades | nested dict | Missing: Sharpe, Sortino, profit factor, Kelly fraction, monthly/yearly R distribution, trade-to-trade autocorrelation. Only R-multiple and equity drawdown. |
| `scheduler._run_pipeline` | `scheduler.py:44-117` | `init_scheduler`, `trigger_run` | tf overrides | void (writes state, outputs) | Swallows all exceptions → stores string in `_state["last_error"]`. No email / alert hook. |

### Orchestrator functions

`main.run_sentiment` (L90-208) and `main.run_signal` (L281-406) together are
the live orchestrator. `backtest.engine.run` (L212-336) is the historical
orchestrator and is structurally the same pipeline minus the news layer.

---

## 8. Data Sources and Information Pulling

| Source | File | Entry point | Freshness | Primary / Derived |
|--------|------|-------------|-----------|-------------------|
| Google News RSS (7 queries) | `news/rss_fetcher.py:13` | `fetch_articles()` | Live per run | **Primary** — only source of sentiment. No historical backfill. |
| Article HTML scrape | `news/article_scraper.py:47` | `scrape_article()` | Live | Primary (downstream of RSS). ~0-50% success rate — many Google News wrappers, paywalls, JS-only pages. |
| yfinance OHLCV | `market/data_fetcher.py:19` | `fetch_series()` | Live, end-of-day | **Primary** (daily bars). `GC=F`, `DX-Y.NYB`, `^VIX`. |
| FRED CSV | `market/fred_fetcher.py:29` | `fetch_series()` | Live, daily | Primary. `DFII10` real yield. No API key. Overrides yfinance entry. |
| CFTC COT Socrata | `positioning/cot_fetcher.py:50-95` | `refresh()` | Weekly (Tuesday positions, Friday release). Cached to `outputs/cot_gold.jsonl`; auto-refresh on `STALE_DAYS=10` breach. | Primary. |
| Event calendar | `events/calendar.py:28-92` | `get_events()` | **Hardcoded static lists** 2020-2026 for FOMC/CPI/PCE; NFP rule-derived. | **[Gap]** Static data; must be manually updated yearly. No test that today's date is still covered. |
| Sentiment JSONL cache | `sentiment/cache.py:40-76` | `append()` / `load()` / `lookup()` | Append-only, one entry per run | **Derived** from primary news. Only way backtests see historical sentiment. |
| Anthropic API | `sentiment/agent_panel.py:115-129` | `_call_anthropic()` | Live, per article | Primary (LLM). Optional; gated by `ANTHROPIC_API_KEY`. |
| Ollama local | `sentiment/agent_panel.py:131-148` | `_call_ollama()` | Live, per article | Primary (LLM). Default backend in config; requires `qwen2.5:3b` pulled. |

Staleness / failure handling [Confirmed]:

- All fetchers return `None` on failure and log a warning. Downstream scoring
  has `if ind is None: return 0` for every factor — **system can silently run
  on partial data and still produce a signal**. This is counted in
  `data_quality.market_data_failures` and may downgrade confidence.
- FRED and CFTC both use `requests` directly; no retry beyond `article_scraper`.
- No checksum / version stamp on cached files. A corrupted `cot_gold.jsonl`
  line is skipped with a warning (`positioning/cot_fetcher.py:108-111`).

Leakage / bias risks:

- **Survivorship bias** on news: Google News returns recency-weighted recent
  articles; deleted or reorganised articles are invisible.
- **Lookahead risk in backtest**: `sentiment_cache.load()` is keyed by the
  *run date* (local calendar), and `cache.append` timestamps with `datetime.now()`.
  A backtest on bar `t` looks up `cache[t.date()]`. If a future run appends a
  newer avg_score for that same date (the latest wins per
  `cache.py:65-75`), backtests become time-dependent on cache state. Not a
  direct lookahead, but a **reproducibility hazard**.
- **COT lookahead**: CFTC data is reported on Friday for Tuesday positions.
  `cot_scoring.score_at` filters `record.date <= at`, so it respects the
  release timing *if the record dates are the Tuesday-as-of date*. The
  Socrata field `report_date_as_yyyy_mm_dd` is the Tuesday-as-of date, so
  this is **safe for backtest use with a ~3-day report lag embedded**.
- **Event calendar backfill**: since dates are hardcoded historical-to-future,
  events known post-hoc are included — no lookahead within a retroactive
  blackout check.

---

## 9. Model / Training / Inference Analysis

- **No trained model is produced or updated by this repo.** [Confirmed]
- Three external models are *inferenced*:
  1. **VADER** — rule/lexicon-based, `vaderSentiment.SentimentIntensityAnalyzer`,
     no training in-repo. Loaded once at import time (`sentiment/vader_analyzer.py:6`).
  2. **FinBERT** — `ProsusAI/finbert` downloaded from HuggingFace on first
     run (~440 MB). Inference-only; no fine-tuning, no pinning of revision,
     no checksum. Model is **not gold-specific** (trained on Financial
     Phrasebank + FiQA).
  3. **Agent Panel LLM** — Ollama `qwen2.5:3b` (default) or Anthropic
     `claude-haiku-4-5-20251001`. Multi-persona prompt, single JSON response.
     No fine-tuning, no eval harness.
- **Signal engine is purely rule-based.** Weights in `config.SCORE_WEIGHTS`
  are hand-tuned by grid search over parameters (not learned).
- Artifacts: none in-repo. FinBERT weights cached under HuggingFace's default
  cache (`~/.cache/huggingface/`) and gitignored.
- Training data: **not in this repo**. [Gap]
- No evaluation harness for the sentiment layer per se. Only the full-stack
  *trading* performance is validated (via `backtest/metrics.py`). There is no
  per-article sentiment label ground truth in the repo.
- **Fallback logic [Confirmed]**: FinBERT load failure → returns neutral with
  zero confidence. LLM panel unavailable → skipped. VADER never fails. If all
  three fail for an article, `aggregate()` returns neutral with empty
  `models_used`.

### Inference verdict

The only "model" that drives trading behaviour is the **signal engine itself**
— a 7-factor weighted sum with bucket thresholds, vetos, and gates. It is
deterministic, explainable, and trivially auditable.

---

## 10. Training Data / Dataset Analysis

- **There is no in-repo training dataset.** [Confirmed]
- **The sentiment JSONL cache** (`outputs/sentiment_cache.jsonl`) is the
  nearest thing to a labelled time-series: `{"date", "avg_score", "n_articles",
  "ts"}`. It has no labels (just observations). Its coverage begins the day
  someone ran `main.py --signal` for the first time.
- **Historical sentiment is unobtainable** from the live pipeline [Confirmed
  from `sentiment/cache.py:6-20` docstring]. Backtests on dates before the
  cache started substitute `None → 0` (neutral) — the sentiment factor is
  therefore **structurally silent** in the multi-year backtest results that
  drive the current tuning decisions.
- **COT cache** (`outputs/cot_gold.jsonl`) **is** a valid historical dataset
  (CFTC publishes back to 2006+). `FETCH_LIMIT=5000` rows — sufficient for
  ~20 years of weekly data.
- **Event calendar** covers 2020-2026 (hardcoded) + rule-derived NFP.

### Expected dataset for research grade

The pipeline would need a retroactive sentiment dataset keyed by date:

```
date, daily_avg_sentiment_score, article_count, source_mix, polarity_stdev
```

Candidates: RavenPack, Bloomberg Terminal, GDELT (free, noisy),
Reuters/Refinitiv archive, AYLIEN News. Without one, backtest sentiment is
effectively zero and the layer cannot be validated.

---

## 11. Calculation Engine Breakdown

All formulas, with source lines.

### 11.1 Technical indicators (`market/indicators.py`)

| Quantity | Formula | Line |
|---------|---------|------|
| EMA | `series.ewm(span=window, adjust=False).mean().iloc[-1]` | `_ema:24-27` |
| ATR | Wilder EMA(`max(H-L, |H-prevC|, |L-prevC|)`, period) | `_atr:30-48` |
| SMA200 | `close.rolling(min(200, len)).mean().iloc[-1]` — **clamped** | `compute:227` |
| 5d return | `(close[-1] - close[-(n+1)]) / close[-(n+1)] * 100` | `compute:229-231` |
| 14d high/low | `df["High"].iloc[-w:].max()` / `.min()` | `compute:234-236` |
| VWAP (rolling over window) | `Σ((H+L+C)/3 × V) / ΣV` | `_vwap:51-69` |
| Volume Profile bins | 50 price bins; each bar's volume distributed evenly across bins it spans | `_volume_profile:72-144` |
| VAH/VAL | expand outward from POC bin until 70% of volume covered | `_volume_profile:120-142` |
| TPO POC | 50 bins; each bar adds `1/n_bins_spanned` time-units across its range | `_tpo_profile:147-181` |

### 11.2 Factor scoring (`market/trend_scoring.py`)

- **DXY / Yield / Gold** share the pattern:
  ```
  if above EMA20 + EMA50 AND move > strong_threshold : ±strong
  elif above EMA20 AND move > mild_threshold        : ±mild
  ...
  ```
  Thresholds timeframe-parameterised (`tf["dxy_strong_pct"]`, etc.).
- **VWAP**: `dev_pct = (current - vwap) / vwap * 100`. ≥1% → ±2, ≥0.3% → ±1.
- **Volume Profile**: current vs (VAL, POC, VAH) — four buckets mapping to
  -2, -1, +1, +2.
- **VIX**: level-based (≥30 → +2, ≥20 → +1, ≥15 → 0, <15 → -1).
- **Sentiment score**: bucketed at `±0.15` / `±0.05`
  (`signals/signal_engine.py:37-49`).
- **COT score**: `z-score > 2 → -2` (fade long); `< -2 → +2` (fade short).
  Mild bands `±1…±2`. Window=52, min sample=10
  (`positioning/cot_scoring.py:36-75`).

### 11.3 Signal total + thresholds (`signals/signal_engine.py:109-195`)

```python
total = Σ score_i × SCORE_WEIGHTS[i]          # 8 factors
# weights (post-tuning):
#   sentiment=0.75, dxy=1.00, yield=0.80, gold=1.50,
#   vix=0.75, vwap=0.75, volume_profile=0.75, cot=0.25
```

| Total | Signal |
|-------|--------|
| ≥ 6 | STRONG_BUY (requires gold_score ≥ 2, else BUY) |
| ≥ 2 | BUY |
| (-2, 2) | HOLD |
| ≤ -2 | SELL |
| ≤ -6 | STRONG_SELL (requires gold_score ≤ -2, else SELL) |

README still claims the old thresholds (4/2/-2/-4 and the older veto rules).
**[Gap] README is out of date** — thresholds, weights, COT factor, event gate,
partial TP, and timeframe profiles are all newer than the README reflects.

### 11.4 Trade setup (`signals/trade_setup.py`)

BUY:

```
invalidation = min(EMA20, recent_low_14d)
stop         = invalidation − atr_stop_mult × ATR
risk         = entry − stop        (floor: max(risk, MIN_RISK_PCT × entry))
stop         = entry − risk        (re-derived after floor)
tp = VAH if (VAH − entry)/risk ≥ min_rr and VAH > entry
   = TPO_POC if its RR clears and TPO_POC > entry
   = entry + min_rr × risk (fallback)
```

SELL is mirror-symmetric (invalidation = `max(EMA20, recent_high_14d)`).

### 11.5 Backtest accounting (`backtest/metrics.py`, `backtest/engine.py`)

- R-multiple = `pnl / risk`. Win if R>0.
- Equity curve = cumulative R per trade in chronological order.
- Max drawdown (R) = `min(eq - running_peak)`.
- Partial TP: banks `(partial_level − entry) × fraction` at trigger, moves
  stop on the remainder to breakeven, continues remainder to TP or STOP.
  Blend formula: `total_pnl = realized + per_unit × (1 − fraction)`
  (`_close_blended:188-202`).
- Regime classification: `sign(gold 60-bar % change) > ±5%` → bull/bear; else
  flat (`_regime:63-74`).

---

## 12. Risk Engine Deep Dive

This is the single most critical section for a research user.

### 12.1 What the "risk engine" actually is

It is **not a portfolio risk engine.** It is a per-trade validation + gating
layer composed of:

1. **Veto rules** (`signals/signal_engine.py:71-106`) — symmetric, cheap guard
   rails against contradictory factor combinations.
2. **LONG_ONLY / SMA200 regime gates** (`signals/signal_engine.py:152-163`) —
   macro filters that null SELLs entirely or BUYs when gold < SMA200.
3. **Event blackout** (`events/blackout.py`) — ±1 day window around
   FOMC/CPI/NFP/PCE, daily granularity.
4. **Minimum RR** (`signals/trade_setup.py:_fmt:133-155` and
   `signals/risk_management.py:14-37`) — rejects setups below `tf["min_rr"]`
   (3.0 swing, 1.5 day).
5. **Minimum risk floor** (`config.MIN_RISK_PCT = 0.003`, 0.3% of entry) —
   prevents micro-stops that would vanish in noise
   (`signals/trade_setup.py:64-65`).
6. **Confidence downgrades** (`signals/confidence.py`) — not a hard gate;
   just a label (HIGH/MEDIUM/LOW) for the user.

### 12.2 Risk inputs

| Input | Source | Role |
|-------|--------|------|
| `gold_trend_score` | `trend_scoring.score_gold` | Required for STRONG variants (≥2) and for veto |
| `dxy_score` | `trend_scoring.score_dxy` | Veto trigger at ±2 |
| `yield_score` | `trend_scoring.score_yield` | Veto trigger at -2 only (asymmetric) |
| `macro_bullish` | `gold.current > gold.sma200` | SMA200 gate |
| `event_blackout_reason` | `events.blackout.is_blackout` | Event gate |
| `ATR` | `market/indicators._atr` | Stop distance |
| `EMA20` + `recent_high/low_14d` | indicators | Invalidation level |
| `VAH/VAL/TPO_POC` | indicators | TP target preference |
| `MIN_RR` | `tf["min_rr"]` | Trade validity gate |
| `MIN_RISK_PCT` | `config.MIN_RISK_PCT` | Micro-stop floor |
| `confidence` | `signals/confidence.py` | **Labels only, not gating** |

### 12.3 What is absent (the real gaps)

- **No position sizing.** R-multiple accounting assumes unit position. There
  is no `units_to_trade = (account_equity × risk_pct) / risk_per_unit`. A
  research user cannot compute $ PnL from the current backtest output.
- **No portfolio-level risk.** No max-concurrent-positions, max-daily-loss,
  max-weekly-loss, correlation limit (trivially, there's only one instrument
  — but if additional instruments were added, no framework would exist).
- **No leverage modelling.** Futures margin, overnight holds, roll costs all
  ignored.
- **No slippage or commission.** Fills are exact high/low/close; transaction
  costs are zero. For gold futures this is a meaningful overstatement of
  expectancy, especially for tight-stop day profiles.
- **No stress / scenario analysis.** No COVID, GFC, war-spike slices.
- **No Monte Carlo / bootstrap.** Equity curves are a single deterministic
  path; no confidence interval on expectancy, no probability-of-ruin, no
  deflated Sharpe.
- **No probability model on the signal.** Confidence is `HIGH/MEDIUM/LOW`
  based on factor-count — there is no calibrated probability or expected R.
- **No risk budget across signals.** A `STRONG_BUY` is not risk-weighted
  differently from a `BUY` at entry — both trade the full unit.
- **No intraday risk.** The data is daily, the blackout is daily. A real
  intraday risk engine would need NY / London / Asia session awareness, plus
  8:30 AM / 2:00 PM ET event timestamps.
- **No regime-dependent sizing.** Backtest shows bear regimes net negative —
  no mechanism to scale down in those regimes beyond the (binary) LONG_ONLY
  switch.
- **Gate overlap is unordered.** `_veto` runs before `LONG_ONLY`, which runs
  before `SMA200_GATE`, which runs before event gate. The order matters for
  reporting (`raw_signal`, `veto_applied`, `event_gated`) but not for the
  final `HOLD` outcome — still, consumers may misread which gate actually
  fired because only `veto_applied` is a first-class flag.

### 12.4 Failure modes

1. **Silent FRED/yfinance/VIX failure** still produces a signal with
   `market_data_failures ≥ 1` and only a confidence downgrade. The engine
   does not block a trade when a required factor is missing.
2. **ATR clamp**: `atr = ind.get("atr", 0) or 0`. If ATR=0 (possible with a
   flat FRED series), stop distance collapses to just the invalidation level,
   which may yield a trivially small risk before the `MIN_RISK_PCT` floor
   kicks in.
3. **VAH below entry for BUY**: current logic falls through to TPO POC and
   then to `MIN_RR × risk`, but does not log the fallback as a warning. Could
   mask broken volume-profile calculations on synthetic series.
4. **Partial TP + STOP on the same bar**: because partial-TP is checked
   *before* the stop check on the same bar, a volatile bar that prints
   through both will bank the partial at the partial level and close the
   remainder at stop. This may double-count intrabar optimism.
5. **`risk_management.validate` vs `trade_setup.compute`**: both check RR.
   `validate` is only called in live path; backtest uses `setup.trade_valid`
   directly. Rounding in `_fmt:139` (`round(rr, 4)`) should make them
   equivalent but this has not been asserted by a test.
6. **Grid search mutates `config.TRAIL_*` globals** in-process
   (`grid_search.py:101-103`). Not thread-safe. If the scheduler fires a live
   run during a grid search in the same process, trail settings leak.

### 12.5 What a research-grade risk engine needs here

See the roadmap doc for the prioritised plan. Minimum additions:

1. Position sizing module (`risk/position.py`) parameterised by equity.
2. Transaction-cost / slippage model (`backtest/costs.py`).
3. Risk-of-ruin / bootstrap CI in metrics.
4. Calibrated signal probability (per-signal historical hit rate).
5. Drawdown-triggered kill switch (live).
6. Intraday session + release-time risk for day profile.

---

## 13. User Action → System Trace Table

| User action | Frontend | Network | Backend route | Internal calls | Persistence | Render/effect |
|-------------|----------|---------|---------------|----------------|-------------|---------------|
| Open dashboard | `GET /` | — | `index()` `app.py:242` | `load_runs`, `_load_json`, `_load_latest_backtest`, `_engine_config`, `_macro_bullish`, `_trade_viz`, `sched.get_status`, `sentiment_cache.load` | read-only | full HTML render |
| Filter by timeframe | change `.tf-tab`, navigate to `/?tf=day` | `GET /?tf=day` | same | filters `runs` | — | re-render |
| Select a run | change `<select>` → `/?run=<ts>` | `GET /?run=...` | same | picks the matching timestamp pair | — | re-render |
| Click "Run Now" | JS `fetch('/api/run', POST)` | `POST /api/run` | `api_run()` `app.py:302` | `sched.trigger_run(tf)` → `threading.Thread(_run_pipeline)` | spawns daemon thread | 200 `{"ok": true}`; JS shows overlay |
| (automatic) Scheduler tick | `BackgroundScheduler` | — | — | `_run_pipeline()` → `main.run_sentiment` → `main.run_signal` | writes `sentiment_*.{csv,json}`, `signal_*.json`, `sentiment_cache.jsonl` | overlay clears when `status.running==False` |
| Poll status | JS setInterval | `GET /api/status` | `api_status()` `app.py:296` | `sched.get_status()` + `progress.snapshot()` | — | updates chip + progress bar |
| CLI run | `python main.py --signal --trade-setup --timeframe day` | — | — | `run_sentiment` + `run_signal` directly | same files | stdout banner |
| CLI backtest | `python -m backtest --timeframe swing` | — | — | `engine.run` → `metrics.print_report` → JSON dump | `backtest_<tf>_<ts>.json` | stdout table |
| Grid search | `python -m backtest.grid_search --timeframe swing` | — | — | loops `engine.run` across grid | `grid_<tf>_<ts>.json` | stdout ranking |

---

## 14. Config / Env / Dependency Review

### 14.1 Environment variables

| Variable | Source | Used by | Default |
|----------|--------|---------|---------|
| `ANTHROPIC_API_KEY` | `.env` / shell | `sentiment/agent_panel.py:117` | unset → panel disabled for anthropic backend |

`.env.example` only contains `ANTHROPIC_API_KEY=sk-ant-...`. `.env` is
gitignored (good).

### 14.2 Hidden assumptions / fragile config

- `config.AGENT_PANEL_ENABLED = True` and `AGENT_PANEL_BACKEND = "ollama"` —
  **requires a local Ollama instance with `qwen2.5:3b` pulled**. Without it,
  the panel silently degrades (not an error) but an unsuspecting user would
  see `panel_label` empty for every article.
- `SCHEDULER_ENABLED = False` — the scheduler never starts unless explicitly
  flipped. `/api/run` manual trigger still works.
- Event calendar runs out after **2026-12-16 (FOMC)**, **2026-12-10 (CPI)**,
  **2026-12-18 (PCE)**. NFP is rule-derived and does not expire. [Gap]
- `config.LONG_ONLY = True` is tuned for the current backtest result but is a
  strong prior — any user running in a bear regime will get far fewer signals.
- `MIN_RR = 3.0` (global) vs per-tf `min_rr` (3.0 swing, 1.5 day). Most
  callers resolve through `tf["min_rr"]`; the global default is rarely used.
- `SCORE_WEIGHTS` values are hand-chosen post-grid-search. No unit test
  enforces their relative ordering; a typo (e.g. `gold: 0.15` instead of
  `1.50`) would silently wreck the engine.

### 14.3 Dependency risks

- `yfinance` — widely used but occasionally breaks with Yahoo HTML changes;
  no pinned minimum beyond `>=0.2.40`.
- `transformers` / `torch` — ~1+ GB install; cold start slow.
- `googlenewsdecoder` — third-party, small package; used for all Google News
  URL resolution.
- `anthropic`, `apscheduler`, `feedparser`, `beautifulsoup4`, `requests`,
  `vaderSentiment`, `flask`, `pandas`, `python-dotenv` — all standard.
- Python 3.10+ typing (`str | None`) means **no Python 3.9 support**.

### 14.4 Version pinning

All dependencies are `>=` lower bounds; no upper bounds. Any minor-version
regression in yfinance or transformers could break the pipeline silently.

---

## 15. Quality / Testing / Observability Review

### 15.1 Tests (~619 lines across 8 files)

| File | Covers | Depth |
|------|--------|-------|
| `test_signal_engine.py` | scoring buckets, map_total, veto, long-only, SMA200, weighted total | Good unit coverage of the core engine, monkeypatches config per test |
| `test_engine_simulate.py` | BUY/SELL TP, STOP, TIME, stop-first-on-same-bar, partial TP banking + BE, regime classification | Strong: targeted scenarios with synthetic OHLCV |
| `test_event_gate.py` | calendar lookups, blackout windows pre/on/post/outside, type filter, end-to-end gate | Good |
| `test_cot.py` | score buckets, insufficient history, constant series, extremes, round-trip load, HTTP failure, engine integration | Good |
| `test_fred_fetcher.py` | CSV parse, missing-row handling, lookback cap, HTTP error, empty CSV, 500 | Good |
| `test_metrics.py` | empty, r-multiple, zero-risk, bucketing by signal/regime, drawdown, cumulative curve | Good |
| `test_sentiment_cache.py` | append/load/lookup, latest-wins, bad-line skip | Good |

**Missing tests:**
- No test for `sentiment/aggregator.py` weight re-normalisation or tie-break.
- No test for `market/indicators.py` (ATR correctness, VP edge cases).
- No test for `market/trend_scoring.py` (scoring buckets).
- No test for `signals/trade_setup.py` (entry/stop/tp geometry, VAH fallback).
- No test for `signals/confidence.py` downgrade chain.
- No test for the Flask routes (`app.py`).
- No property-based tests (hypothesis could catch scoring edge cases).
- No integration smoke test (main.py end-to-end with mocked external calls).

### 15.2 Logging

- Every module has a named logger; output is stdout only.
- No structured logging, no correlation IDs, no log level env knob (hardcoded
  `logging.INFO`).
- `outputs/flask.log` and `outputs/grid_run.log` exist from past sessions —
  not automatically rotated.

### 15.3 Monitoring / observability

- `/api/status` is the only runtime-state endpoint. No Prometheus, no
  StatsD, no health check.
- No sentry-style exception reporter. Scheduler failures stash the error
  message in `_state["last_error"]` and log it, but nothing alerts.
- No data-quality dashboard (silent market-fetch failures flow through).

### 15.4 Reproducibility

- yfinance + FRED + CFTC + RSS: all **live**. A backtest run today and the
  same command tomorrow will differ as new bars roll in.
- No seed. LLM panel temperature is `0.2` for Ollama (almost-deterministic),
  unknown default for Anthropic messages API.
- No requirement lockfile (`requirements.txt` only has `>=` lower bounds).
- No Docker / nix / uv lock.

### 15.5 Performance

- Pipeline runtime is dominated by (a) FinBERT cold-start (~5-15s) and
  (b) Ollama LLM calls per article. With `PIPELINE_WORKERS=6` on 50 articles,
  Ollama is the bottleneck on a single GPU/CPU host.
- Backtest over 730 days ≈ 670 bars × full slice-recompute per bar. Indicator
  computation on 700-bar series is cheap (`pandas.ewm` is O(n)). Grid search
  multiplies this by |grid|, still sub-second per profile.

### 15.6 Security

- Flask runs `debug=True` on `localhost:5001`. If ever exposed beyond
  localhost, this is a remote-code-execution risk (Werkzeug debugger).
- No CSRF on `POST /api/run`. Trivial to trigger from a malicious localhost
  tab, which runs a full pipeline (not destructive, but abuse vector).
- No rate limiting.
- `ANTHROPIC_API_KEY` is loaded from `.env`; ensure `.env` is chmod 600.

---

## 16. Research Gaps

The system is solid engineering but not yet a research platform. Concretely
missing:

1. **Historical sentiment dataset.** Without it, the sentiment factor
   contributes 0 to every pre-deployment backtest bar.
2. **Statistical validation.** No Sharpe, Sortino, profit factor, bootstrap
   CI, deflated Sharpe, walk-forward stability, cross-validation, Monte Carlo
   on trade order.
3. **Out-of-sample discipline.** Grid search runs once across the full
   dataset; no train/test split, no forward-only walk, no parameter stability
   analysis.
4. **Experiment tracking.** No MLflow / Weights & Biases / simple SQLite log
   of `(config_hash, git_sha, metrics)`.
5. **Dataset versioning.** yfinance/FRED/CFTC pulls are live and unversioned;
   two runs of the same "historical" backtest can disagree.
6. **Feature store.** Indicator computations are redone from scratch on every
   bar of every backtest; no cache, no provenance.
7. **Ablation studies.** No programmatic turn-each-factor-off-in-turn
   harness. Individual findings (COT, real yields, event gate) were
   hand-run; not repeatable from a single command.
8. **Hyperparameter tracking.** Current grid reports top-ranked profile but
   not parameter stability maps, confidence intervals per profile, or
   interaction effects.
9. **Paper-trading mode.** No simulation environment between backtest and
   live. A run "executes" via printing.
10. **Explainability.** Reasoning bullets are deterministic but not scored —
    there is no "which factor moved the needle most this run" attribution.
11. **Benchmark baselines.** Buy-and-hold gold, random signals, simple
    MA crossover — no comparator metric is computed alongside engine R.
12. **Statistical validity.** No significance test against a null hypothesis
    (e.g. bootstrap of random-signal expectancy on the same bars).
13. **Data lineage / audit.** No signed snapshot of inputs per run.
14. **Intraday engine.** Current "day" profile is still daily bars with
    tighter thresholds — not true intraday.
15. **Short-side reconstruction.** Long-only is a band-aid. Research-grade
    would explain *why* the short side broke and fix the factor asymmetry.

---

## 17. Next-Level Research Roadmap

A full prioritised plan is in `docs/research-upgrade-roadmap.md`. Summary:

- **Level 1 (immediate fixes)**: README drift, event calendar refresh, missing
  unit tests, `risk_management.validate` unified path, grid-search global
  state, `SMA200` clamp warning, auth for Flask, rotating logs.
- **Level 2 (research upgrades)**: statistical validation (Sharpe, bootstrap
  CI, deflated Sharpe), historical sentiment ingest (GDELT minimum), dataset
  snapshotting + git-sha tagging per run, ablation harness, experiment log
  (SQLite), position sizing & cost model, calibrated signal probability.
- **Level 3 (advanced evolution)**: intraday engine with session-aware risk,
  feature store + provenance, walk-forward cross-validation harness, Monte
  Carlo trade-order resampling, paper-trading bridge, explainability
  attribution, short-side asymmetry diagnostic.

---

## 18. Critical Unknowns / Missing Pieces

| # | Question |
|---|---------|
| 1 | Is the historical sentiment cache ever expected to be backfilled from a third-party source? |
| 2 | Is the intent ever to go intraday, or stay daily? |
| 3 | Is position sizing deliberately omitted because this is decision-support, not execution? |
| 4 | Is the event calendar refreshed manually yearly, or is this expected to fail silently after 2026? |
| 5 | Should backtest results be cacheable / reproducible to a git-sha, or are live-data drift differences acceptable? |
| 6 | Is the LLM panel expected to be on by default, or is that a leftover? Ollama dependency is strong (requires service running). |
| 7 | How should the short side be treated long-term? Current `LONG_ONLY=True` is a work-around. |
| 8 | What risk budget is assumed per trade? Position sizing is absent, but `MIN_RISK_PCT=0.3%` implies a target. |
| 9 | Is Google News RSS sufficient, or should Reuters/FT/Bloomberg archives be integrated? |
| 10 | Is the sentiment cache's "latest wins per date" semantics the desired one, or should it be first-wins for backtest reproducibility? |

---

## 19. Appendix: File-by-File Evidence

### Entry points
- `main.py:36-71` — CLI argparser.
- `main.py:411-431` — `__main__` block; sequential sentiment → signal.
- `app.py:312-315` — Flask boot; `debug=True`, `use_reloader=False`.
- `backtest/__main__.py:32-59` — backtest CLI.
- `backtest/grid_search.py:78-150` — grid CLI.
- `scheduler.py:133-166` — `init_scheduler`, background + daemon thread.

### Core orchestrators
- `main.run_sentiment` — `main.py:90-208`.
- `main.run_signal` — `main.py:281-406`.
- `backtest.engine.run` — `backtest/engine.py:212-336`.
- `scheduler._run_pipeline` — `scheduler.py:44-117`.

### Signal engine
- `signal_engine.run` — `signals/signal_engine.py:109-195`.
- `signal_engine._map_total` — `signals/signal_engine.py:52-68`.
- `signal_engine._veto` — `signals/signal_engine.py:71-106`.
- Gates: `LONG_ONLY` L152-154, `SMA200_GATE` L157-163, event L168-171.

### Risk/setup
- `trade_setup.compute` — `signals/trade_setup.py:26-42`.
- `_buy` — `signals/trade_setup.py:47-85`.
- `_sell` — `signals/trade_setup.py:90-128`.
- `_fmt` — `signals/trade_setup.py:133-166`.
- `risk_management.validate` — `signals/risk_management.py:14-37`.
- `backtest.engine._simulate` — `backtest/engine.py:77-202`.

### Data ingestion
- RSS: `news/rss_fetcher.py:13-47`.
- Scraping: `news/article_scraper.py:21-99`.
- Dedup: `news/dedup.py:8-48`.
- yfinance: `market/data_fetcher.py:19-67`.
- FRED: `market/fred_fetcher.py:29-79`.
- COT: `positioning/cot_fetcher.py:50-131`.
- Event calendar: `events/calendar.py:28-131`.

### Sentiment stack
- VADER: `sentiment/vader_analyzer.py:1-28`.
- FinBERT: `sentiment/finbert_analyzer.py:1-76`.
- Agent panel: `sentiment/agent_panel.py:1-250`.
- Aggregator: `sentiment/aggregator.py:1-74`.
- Daily cache: `sentiment/cache.py:1-85`.

### Backtest + metrics
- Engine: `backtest/engine.py:1-336`.
- Metrics: `backtest/metrics.py:1-122`.
- Grid search: `backtest/grid_search.py:1-155`.

### Utils
- Logger: `utils/logger.py:1-19`.
- IO: `utils/io_helpers.py:1-160`.
- Progress: `utils/progress.py:1-40`.
- Text: `utils/text_cleaner.py:1-27`.

### Dashboard
- `app.py:1-316`.
- `templates/index.html:1-1894` (single file; inline CSS/JS).

### Tests (~619 lines)
- `tests/conftest.py`, `tests/test_signal_engine.py`,
  `tests/test_engine_simulate.py`, `tests/test_event_gate.py`,
  `tests/test_cot.py`, `tests/test_fred_fetcher.py`, `tests/test_metrics.py`,
  `tests/test_sentiment_cache.py`.

---

## Deliverable 1 — System Map for a Non-Technical Founder

**The product.** A local app that reads gold news, tracks the gold market and
a few related indicators (dollar, real yields, stock fear index, gold
futures positioning, central-bank and inflation event dates), and prints a
"BUY / HOLD / SELL" call for gold along with entry, stop, and target prices.

**How it decides.** Each factor gets a small integer score (−3 to +3), the
scores are weighted and added together, and the total is turned into a
signal. A few override rules can force the signal to HOLD if the factors
contradict each other, if the macro trend is against the direction, or if a
big announcement is imminent. A trade is "valid" only if the reward is at
least 3× (swing) or 1.5× (day) the risk.

**How it proves itself.** A replay engine walks through the last 2–5 years of
gold prices, asks the same engine for a decision every day, and tracks the
simulated wins and losses. That back-run is what the dashboard's "5-Year
Backtest Proof" card shows.

**What it isn't.** It is not a trader. It is not funded. It does not buy or
sell anything. It does not use machine learning in the modern sense — the
rules were tuned by hand from the replay results.

**Strengths.** Transparent — every decision has a list of reasons. Modular —
each factor can be toggled. Cached — the news, positioning, and event data
are saved locally.

**Weaknesses.** News history cannot be pulled retroactively, so sentiment is
effectively off in the long historical replay. No money-management layer —
you cannot ask it "what size should I trade?" No commission/slippage model,
so the backtest is slightly optimistic. Short-side is currently disabled
because the replay showed no edge.

---

## Deliverable 2 — Developer Action List (Priority)

### Must-do now (L1)

1. **README drift.** Thresholds moved from 4/2/-2/-4 to 6/2/-2/-6, weights
   added, COT / VIX / VWAP / Volume Profile / event gate all added since
   README was written. Update `README.md` to match current code.
2. **Event calendar expiry.** Hardcoded dates end Dec 2026. Add a test that
   fails when today's date is within 90 days of the last calendar entry. Or
   swap to a live source (ForexFactory export, Econoday CSV).
3. **Flask hardening.** Drop `debug=True` unless an env flag is set. Add a
   token gate on `POST /api/run` if bound beyond localhost.
4. **Log rotation.** `outputs/flask.log` and `outputs/grid_run.log` are
   unbounded files. Wire `RotatingFileHandler` or redirect through `logrotate`.
5. **Grid-search global mutation.** Extract trail settings from module globals
   to a per-run parameter object; current try/finally works but leaks if an
   exception in the pool kills the process.
6. **Unify RR validation.** `trade_setup.compute` and `risk_management.validate`
   both check RR. Centralise and call from both live and backtest.
7. **Missing unit tests.** Add coverage for `aggregator.py`, `indicators.py`
   (at least ATR + VP), `trend_scoring.py`, `trade_setup.py`, `confidence.py`,
   `app.py` routes.
8. **SMA200 clamp.** `compute()` silently uses `min(200, len)` — add a
   `is_true_sma200` flag in the indicator dict and respect it in the
   macro_bullish computation.
9. **CLI `--timeframe` for `run_sentiment`.** Currently unused by sentiment,
   but `scheduler.SCHEDULER_TIMEFRAME` affects only `run_signal`. Document
   that.

### Next (L2)

10. **Position sizing module** parameterised by account equity and per-trade
    risk %.
11. **Cost model** (commission + slippage per entry/exit) in `backtest/engine.py`.
12. **Sharpe / Sortino / profit-factor** in `backtest/metrics.py`.
13. **Bootstrap CI + deflated Sharpe** on expectancy.
14. **Walk-forward cross-validation harness**: split data by year, train
    (grid-search) on year 1–3, test on year 4, roll forward.
15. **Experiment log** — SQLite table of `(run_id, git_sha, config_hash,
    params, metrics)` auto-populated on every backtest.
16. **Data snapshot** — persist the raw yfinance/FRED/CFTC pulls per-run under
    `outputs/snapshots/<ts>/` so backtests are reproducible.
17. **Live data drift monitor** — compare latest pull to previous day's pull,
    alert on > X% divergence in any series.

### Later (L3)

18. **Intraday engine** — replace daily bars with 5-min or 1-hour bars; add
    session gates (London open, NY open, Asia).
19. **Feature store** — cache all indicators per (symbol, date, config_hash)
    in SQLite / Parquet.
20. **Short-side factor asymmetry fix** — diagnose why SELL expectancy is
    near zero; likely candidates: (a) gold's long-term upward drift, (b)
    symmetry of thresholds masking positive-skew, (c) missing supply-side
    factor (CB buying).
21. **Paper-trading mode** — forward-simulate live signals against a paper
    book, emit daily PnL.
22. **LLM ensemble robustness** — agree thresholds across Ollama + Anthropic;
    today panel behaviour depends on the backend switch.
23. **Replace hardcoded calendar** with a refreshed live source.

---

## Deliverable 3 — Research Action List (Priority)

1. **Acquire or synthesise historical sentiment.** GDELT v2 is free (minute-
   granularity, 2015+); RavenPack / Bloomberg paid. Without one of these,
   every conclusion about the sentiment factor is structurally unproven.
2. **Deflated Sharpe ratio** (Lopez de Prado) to correct for the grid-search
   multiple-hypothesis bias. Currently ranking on expectancy has no correction.
3. **Bootstrap trade-order confidence interval** (block bootstrap preserves
   autocorrelation) — report expectancy ± 95% CI.
4. **Walk-forward stability** — compute the proportion of years where the
   top-ranked grid profile would still rank in the top 10% if tuned only on
   earlier years. Expect a severe drop.
5. **Ablation matrix.** Programmatically run backtest with each factor
   zero'd in turn. Report marginal contribution per factor. Today this is
   implicit and lives only in commit messages.
6. **Null-hypothesis backtest.** Run the same backtest with signals drawn
   from a Bernoulli with the empirical BUY rate; compare the distribution of
   expectancies to the real engine. Need p<0.05 to claim edge.
7. **Regime-conditional expectancy.** Today `by_regime` exists but without CI.
   Add bootstrap per regime and a chi-square for regime-dependence.
8. **Benchmark suite**: buy-and-hold gold, 50/200 MA cross, random-entry
   with same stop/tp geometry. Report relative R.
9. **Kelly fraction estimate** to inform position sizing work.
10. **Feature leakage scan** on `sentiment_cache`: verify that backfilled
    entries for past dates never contaminate backtests of those dates.
11. **Intraday re-derivation** of the "day" profile. The current "day"
    profile is daily bars with tighter thresholds — not meaningfully
    different from swing minus event gating.
12. **Calibrated confidence.** Convert HIGH/MEDIUM/LOW to empirical hit
    rates from the backtest per confidence bucket and expose in JSON.
13. **Factor correlation map.** DXY ↔ yield are correlated; current
    weight trim is eyeballed. Compute and publish per-factor correlation and
    variance inflation factor.
14. **Formal event-study around CPI/FOMC.** Demonstrate the gate's
    necessity with a pre/post event-window R distribution, not just the
    5-year A/B note in code comments.

---

## Deliverable 4 — Questions to Unblock Further Improvements

1. Is this system intended to remain decision-support, or evolve toward
   paper-trading / execution? (Sizing, costs, slippage all hinge on this.)
2. What is the acceptable historical backtest window? 2 years, 5 years, 20?
3. Which sentiment-history provider is in scope if any? (GDELT / RavenPack /
   Bloomberg / Reuters / custom scrape of archived pages.)
4. Is a reproducible backtest mandatory (snapshot data per run) or is live
   drift acceptable?
5. Is the daily granularity a permanent constraint, or is intraday planned?
6. Is the short side expected to come back, and under what changed
   conditions?
7. What % per trade is the target risk budget? (needed to complete sizing).
8. Is single-instrument (XAUUSD only) the permanent scope, or will the
   engine generalise to silver, miners, or broader commodities?
9. Who is the user? Research analyst, discretionary trader, or automated
   execution? (changes UX priorities sharply)
10. What is the deployment target? Local-only forever, or eventual cloud?

---

## Deliverable 5 — Suggested Folder Refactor

The current layout is already clean. Only minor tweaks suggested:

```
newssentimentscanner/
├── src/                          # <- new: move all first-party code here
│   ├── newssentimentscanner/
│   │   ├── __init__.py
│   │   ├── cli/                  # main.py, backtest/__main__.py, grid cli
│   │   ├── config/               # split config.py by concern
│   │   │   ├── __init__.py       # exports unified namespace
│   │   │   ├── market.py         # MARKET_SYMBOLS, FRED_SYMBOLS, lookback
│   │   │   ├── scoring.py        # SCORE_WEIGHTS, thresholds
│   │   │   ├── risk.py           # MIN_RR, MIN_RISK_PCT, TRAIL, PARTIAL_TP
│   │   │   ├── profiles.py       # TIMEFRAME_PROFILES
│   │   │   └── sentiment.py      # AGENT_*, VADER_THRESHOLDS, FINBERT_*
│   │   ├── news/                 # unchanged
│   │   ├── sentiment/            # unchanged
│   │   ├── market/               # unchanged
│   │   ├── positioning/          # unchanged
│   │   ├── events/               # unchanged
│   │   ├── signals/              # unchanged
│   │   ├── risk/                 # NEW: position_sizing.py, costs.py, kill_switch.py
│   │   ├── backtest/             # unchanged
│   │   ├── experiments/          # NEW: ablation.py, walk_forward.py, bootstrap.py, registry.py
│   │   └── web/                  # app.py + templates here
│   └── templates/
├── data/                         # NEW: input datasets, pinned snapshots
│   ├── snapshots/<run_id>/
│   ├── events/                   # exported calendar JSON (replaces hardcoded list)
│   └── sentiment_history/        # optional GDELT / RavenPack local copy
├── tests/
├── docs/
│   ├── full-system-audit.md      # this file
│   └── research-upgrade-roadmap.md
├── outputs/                      # runtime outputs only
├── pyproject.toml                # replace requirements.txt; pin versions
└── README.md
```

Key reasons for the tweaks:

- `config.py` is already 286 lines and growing; splitting by domain will scale.
- A dedicated `risk/` package signals that risk is a first-class concern.
- `experiments/` formalises the bootstrap/ablation/registry work that is
  currently implicit in `grid_search.py` + commit messages.
- Moving the Flask app under `web/` clarifies that it's one of several
  entry points (CLI, backtest, scheduler).
- `data/` at the root separates *inputs* from runtime *outputs*.
