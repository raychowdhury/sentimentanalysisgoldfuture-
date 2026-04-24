# Opposite Order Flow Detection Engine

A research engine that flags bars where **price moves opposite to expected
order-flow behavior** — buyer dominance with price falling, seller dominance
with price rising, absorption at support/resistance, failed breakouts, and
CVD/price divergence. Default target is **ES=F** (E-mini S&P 500 futures);
the engine works on any yfinance symbol with reliable futures-style volume.

---

## What it detects

Seven rule-based patterns (see [rule_engine.py](src/rule_engine.py)):

| Code | Pattern |
|------|---------|
| R1 | Buyer dominance, but price moved down |
| R2 | Seller dominance, but price moved up |
| R3 | Heavy buying near resistance, no upward follow-through |
| R4 | Heavy selling near support, no downward follow-through |
| R5 | Bullish trap — high pokes above prior range, close comes back in |
| R6 | Bearish trap — low pokes below prior range, close comes back in |
| R7 | CVD/price rolling-correlation divergence |

Rule hits plus forward-return behavior map to six labels:
`normal_behavior`, `buyer_absorption`, `seller_absorption`,
`bullish_trap`, `bearish_trap`, `possible_reversal`.

An optional supervised classifier (XGBoost primary, RandomForest baseline)
refines the label with probability scores and feeds a blended confidence
score in `[0, 100]`. Alerts above a configurable threshold are written to
JSON and rendered in the `/order-flow` dashboard page.

---

## Data requirements

| Mode | Inputs | Status |
|------|--------|--------|
| **OHLCV proxy** (default) | `Open, High, Low, Close, Volume` | Always available via yfinance |
| **Tick mode** (optional) | `bid_size, ask_size, trade_side` per trade | Requires user-supplied CSV/parquet |

`detect_schema()` in [data_loader.py](src/data_loader.py) picks the mode
automatically. Every output carries `data_quality.proxy_mode` so consumers
know which path produced it.

### OHLCV proxy formulas

Buy / sell volume are estimated from candle shape:

- **Primary — Close Location Value (Chaikin 1966):**
  `clv = ((C - L) - (H - C)) / (H - L)`, then
  `buy_vol = V * (1 + clv) / 2`, `sell_vol = V * (1 - clv) / 2`.
- **Fallback — tick rule (Lee & Ready 1991):** used when `H == L`.
  `sign = sign(C_t - C_{t-1})`; all volume goes to the sign's side (half
  split on flat bars).

Delta, delta_ratio, and CVD follow directly:
`delta = buy_vol − sell_vol`, `cvd = delta.cumsum()`,
`cvd_z = rolling z-score of CVD`.

**Important:** these are *proxies*. They correlate with true order flow but
are not the same. See "Limitations" below.

---

## Usage

```bash
# 1. install deps (xgboost, scikit-learn, pyarrow added to root requirements)
pip install -r requirements.txt

# 2. fetch + cache multi-TF bars
python -m order_flow_engine.src.data_loader --symbol ES=F

# 3. (optional) train a classifier
python -m order_flow_engine.src.model_trainer --symbol ES=F --tf 15m

# 4. run a prediction pass → flagged_events.csv, alerts.json, (model_predictions.csv)
python -m order_flow_engine.src.predictor --symbol ES=F --tf 15m

# 5. view the dashboard
python app.py   # visit http://localhost:5001/order-flow
```

Outputs land in `outputs/order_flow/`:

- `flagged_events.csv` — every bar with any rule hit
- `model_predictions.csv` — per-bar class probabilities (only when a model exists)
- `feature_importance.csv` — written by the trainer
- `alerts.json` — consolidated alerts above the confidence threshold
- `alerts.jsonl` — append-only stream of the same

Models live in `order_flow_engine/models/` as `of_<timestamp>.pkl` + sidecar
`.json` metadata.

---

## Configuration

All knobs live in [`order_flow_engine/src/config.py`](src/config.py). A few
are also surfaced at the repo root in `config.py` so they can be tuned from
one place:

```python
ORDER_FLOW_ENABLED        = True
ORDER_FLOW_SYMBOL         = "ES=F"
ORDER_FLOW_TIMEFRAMES     = ["5m", "15m", "1h", "1d"]
ORDER_FLOW_LOOKBACK_DAYS  = 180
ORDER_FLOW_ANCHOR_TF      = "15m"
ORDER_FLOW_ALERT_MIN_CONF = 70
```

Rule thresholds (`RULE_DELTA_DOMINANCE`, `RULE_ABSORPTION_DELTA`, etc.) and
ML hyperparameters (`XGB_PARAMS`, `RF_PARAMS`, `WF_FOLD_SIZE`) stay in the
package-local config.

---

## Confidence scoring

```
p_class      = max(predict_proba(x))
p_normal     = predict_proba(x)[idx("normal_behavior")]
rule_support = min(1, hits_for_predicted_label / required_hits)
data_quality = 0.85 if proxy_mode else 1.0
volume_ok    = 1.0 if V > 0 else 0.7
raw          = (0.6*p_class + 0.2*(1 - p_normal) + 0.2*rule_support)
               * data_quality * volume_ok
confidence   = clamp(round(100 * raw), 0, 100)
```

With no model trained yet, the engine falls back to a rule-only score:
`confidence = min(100, 40 + 10 * rule_hit_count)`.

---

## Testing

```bash
pytest order_flow_engine/tests/ -v
```

Covers feature math, proxy invariants, each rule in isolation, label
assignment, leakage guard, alert gating/schema, model training (gated by
`pytest.importorskip`), and missing-data resilience.

---

## Limitations

1. **No real order flow.** Buy/sell volumes, delta, and CVD are OHLCV
   proxies. They correlate with true flow in liquid futures but can diverge
   in gappy / thin markets. Every output flags `proxy_mode=True`.
2. **6-month intraday not achievable via yfinance.** Per-interval caps are
   1m = 7d, 5m/15m = 60d, 1h = 730d, 1d = unbounded. The engine runs
   mixed-resolution: 180d of 1d + 1h, 60d of 5m/15m. On a 15m anchor this
   gives ~5,800 labelled rows over ~60 days.
3. **No bid/ask, no trade direction.** R3/R4 absorption rules are heuristic
   — real absorption needs size-at-quote observation.
4. **Volume reliability is symbol-dependent.** `ES=F` and `GC=F` futures
   volume is good. `XAUUSD=X` spot has zero/None volume; the engine warns
   and degrades to tick-rule only.
5. **Rare-label scarcity.** Trap classes may have fewer than 50 examples
   in the available window. Inverse-frequency sample weights help but do
   not fix small-sample variance — treat rare-label confidences with
   skepticism.
6. **Batch-oriented, no real-time loop.** The engine is a CLI pass. A
   scheduler hook can be added later but is intentionally out of scope.
7. **CSV import is the only tick-data path.** If user later supplies broker
   tick data (CME Group, Alpaca, etc.), only `data_loader.detect_schema`
   plus a small `tick_to_bar.py` aggregator would be needed — the rest of
   the pipeline already accepts the tick schema.
