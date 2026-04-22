# Stocks AutoResearch — Program Specification

Single source of truth read by `orchestrator.py` at the start of every cycle.
Edit values here (not agents) to change how the research loop behaves.

---

## 1. Objective

Predict the **next-day direction** (up / down) for each of the **top 20
S&P 500 names by index influence** with a pooled model whose **mean
per-ticker directional accuracy ≥ 0.55** on a held-out 60-day window.

Universe = `stocks/stock_universe.py` (AAPL, MSFT, NVDA, AMZN, GOOGL, GOOG,
META, BRK-B, TSLA, LLY, AVGO, JPM, V, XOM, UNH, MA, COST, JNJ, PG, HD).

---

## 2. Metrics

| Role       | Metric                               | Threshold |
|------------|--------------------------------------|-----------|
| primary    | Mean per-ticker directional accuracy | ≥ 0.55    |
| secondary  | Pooled Sharpe (equal-weight long/short, daily, ann.) | ≥ 1.00 |
| guard-rail | Max drawdown (pooled equity curve)   | ≤ 20%     |

Training agent triggered whenever live mean accuracy falls below **0.53**
over the most recent evaluation window.

---

## 3. Data Sources

| Source        | Symbols / Series                                   | Refresh |
|---------------|----------------------------------------------------|---------|
| Yahoo Finance | 20 tickers (universe) OHLCV                        | daily   |
| Yahoo Finance | `SPY` (market), `^VIX` (vol), `DX-Y.NYB` (DXY)     | daily   |
| Yahoo Finance | Sector ETFs: XLK, XLY, XLC, XLF, XLV, XLE, XLP     | daily   |
| FRED          | `DFF` (Fed funds), `DFII10` (real 10Y), `CPIAUCSL` | daily   |
| Parent project| `outputs/sentiment_cache.jsonl` (gold sentiment, used as risk proxy) | on-read |

---

## 4. Agent Roles

- **data_agent** — fetch all tickers + macro + sector ETFs, build pooled
  long-format feature matrix (one row per ticker per date), write parquet.
- **training_agent** — retrain single pooled XGBoost classifier with
  ticker as categorical feature; explores one hyperparameter change per cycle.
- **eval_agent** — backtest active model on last 60 trading days; emit
  per-ticker accuracy + mean + pooled Sharpe + drawdown.
- **report_agent** — append structured entry to `run_log.md` summarising
  cycle decisions, metrics, per-ticker accuracy.
- **meta_optimizer** — every 10 cycles, ask Claude what to change next.

---

## 5. Loop Frequency

- Cadence: **every 24 hours**. Container cron at **00:10 UTC** (offset from
  gold at 00:05 to avoid Yahoo rate-limit collisions). Orchestrator also
  contains asyncio loop for local continuous runs.

---

## 6. Experiment Rules

- **One variable per cycle.** Feature set, lookback, hyperparameter, or split.
- **Promotion gate.** Candidate promoted only if mean per-ticker accuracy
  beats incumbent's re-measured accuracy on current holdout AND respects
  Sharpe / drawdown guard-rails.
- **Degenerate-classifier gate.** Pooled pred_up_rate must stay in [0.25, 0.75].
- **Reproducibility.** Every promoted run stores config, seed, feature list.

---

## 7. Stopping Conditions

- If **3 consecutive runs** produce no improvement in mean accuracy, loop
  sets `flag_for_human_review = true` and pauses further promotions until cleared.

---

## 8. Meta-Optimization

- After **every 10 cycles**, `meta_optimizer.py` reads `run_log.md` and asks
  Claude to identify (a) the change with biggest positive impact and
  (b) single highest-value next experiment. Suggestion written back under
  **Next Experiment** heading and logged to `run_log.md`.

---

## Next Experiment

_No experiments run yet._
