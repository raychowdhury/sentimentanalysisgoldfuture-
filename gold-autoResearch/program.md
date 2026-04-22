# Gold Futures AutoResearch — Program Specification

This document is the **single source of truth** read by `orchestrator.py` at the
start of every cycle. Edit the values here (not the agents) to change how the
research loop behaves.

---

## 1. Objective

Predict the **next-day direction** (up / down) of front-month gold futures
(`GC=F`) with **directional accuracy ≥ 60%** on a held-out 90-day window.

---

## 2. Metrics

| Role       | Metric                   | Threshold |
|------------|--------------------------|-----------|
| primary    | Directional accuracy     | ≥ 0.60    |
| secondary  | Sharpe ratio (daily, ann.) | ≥ 1.20 |
| guard-rail | Max drawdown             | ≤ 15%     |

The training agent is triggered whenever live directional accuracy falls
below **0.58** over the most recent evaluation window.

---

## 3. Data Sources

| Source           | Symbols / Series                     | Refresh   |
|------------------|--------------------------------------|-----------|
| Yahoo Finance    | `GC=F` (OHLCV)                       | daily     |
| Yahoo Finance    | `DX-Y.NYB` (DXY), `^VIX`             | daily     |
| FRED             | `CPIAUCSL` (CPI), `DFF` (Fed funds), `DFII10` (real 10Y) | daily |

---

## 4. Agent Roles

- **data_agent** — fetch latest OHLCV + macro series, run feature engineering,
  write a clean feature matrix to disk.
- **training_agent** — retrain the ensemble (XGBoost + LSTM) when accuracy
  drops below the retrain threshold; explores one hyperparameter change per
  cycle.
- **eval_agent** — backtest the active and candidate models on the last
  90 trading days; emit directional accuracy, Sharpe, max drawdown.
- **report_agent** — append a structured entry to `run_log.md` summarising
  the cycle's decisions and metrics.

---

## 5. Loop Frequency

- Cadence: **every 24 hours**, triggered at **00:05 UTC** by the container
  cron job. The orchestrator itself also contains an asyncio loop so it can
  run continuously during development.

---

## 6. Experiment Rules

- **One variable per cycle.** Each cycle may change exactly one of:
  feature set, lookback window, model hyperparameter, or train/valid split.
- **Promotion gate.** A newly trained candidate is only promoted to
  production if it **beats the current production model's directional
  accuracy on the holdout set** and does not violate the Sharpe / drawdown
  guard-rails.
- **Reproducibility.** Every promoted run stores its training config,
  random seed, and feature list alongside the model artefact.

---

## 7. Stopping Conditions

- If **3 consecutive runs** produce no improvement in directional accuracy
  on the holdout, the loop sets `flag_for_human_review = true` in
  `run_log.md` and pauses further promotions until cleared.

---

## 8. Meta-Optimization

- After **every 10 cycles**, `meta_optimizer.py` reads `run_log.md` and asks
  a Claude model to identify (a) the change with the biggest positive
  impact so far, and (b) the single highest-value next experiment.
- The suggestion is written back into this file under the **Next
  Experiment** heading below, and logged to `run_log.md` with a timestamp.

---

## Next Experiment

_Updated 2026-04-22T03:24:20+00:00 by meta_optimizer._

**Finding.** RSI-14 and ema_gap features provided the biggest positive impact with ~3% improvement in directional accuracy. Hyperparameter variations in cycles 1-3 showed no measurable performance differences.

**Next experiment.** Add 10-day rolling sentiment z-score feature to complement RSI-14 and ema_gap, using max_depth=7 as suggested by meta-optimizer

**Proposed config change.**

```json
{
  "xgb": {
    "max_depth": 7,
    "learning_rate": 0.1,
    "n_estimators": 400
  },
  "features_added": [
    "sentiment_z10"
  ],
  "focus": "feature_engineering_over_hyperparams"
}
```
