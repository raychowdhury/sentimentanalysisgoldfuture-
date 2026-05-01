# ML Engine

Standalone ML module. Lives next to the rule-based bias engine, **does not modify it**.
Predicts directional setups with entry / stop / target / confidence and renders them on
the dashboard.

## Layout

```
ml_engine/
  config.py           # symbols, horizons, hyperparams
  backfill.py         # databento -> data/history/<sym>_<tf>_history.parquet
  data_loader.py      # concat history + live parquet
  features/builder.py # OHLCV-only features (returns, ATR, RSI, EMA dist, vol z, session)
  labels/builder.py   # triple-barrier (TP_ATR_MULT × ATR vs SL_ATR_MULT × ATR over HORIZON_BARS)
  models/trainer.py   # XGBoost binary, walk-forward (70/15/15), saves to artifacts/
  models/predictor.py # latest bar -> prediction dict
  dashboard.py        # Flask routes /ml-predictions + /api/ml/predictions
  artifacts/          # trained models per symbol+timeframe
  data/history/       # backfilled parquet (gitignored, regenerable)
```

## Workflow

```bash
# 1. Backfill multi-year history
python -m ml_engine.backfill GC --years 5 --schema ohlcv-15m

# 2. Train (writes ml_engine/artifacts/GC_15m/{long.xgb,short.xgb,meta.json})
python -m ml_engine.models.trainer GC --schema ohlcv-15m

# 3. Predict on latest bar
python -m ml_engine.models.predictor GC --schema ohlcv-15m

# 4. View dashboard
python app.py    # then http://localhost:5001/ml-predictions
```

## Output schema (per symbol)

| field | meaning |
|---|---|
| `as_of` | last bar timestamp used for inference |
| `expected_window_start` / `_end` | bar at which entry triggers + horizon end |
| `horizon_minutes` | `bar_min × HORIZON_BARS` |
| `side` | `long`, `short`, or `none` (below `WIN_THRESHOLD`) |
| `confidence` | model probability for the chosen side |
| `entry` | last close |
| `target` | `entry ± TP_ATR_MULT × ATR` |
| `stop` | `entry ∓ SL_ATR_MULT × ATR` |
| `rr` | `TP_ATR_MULT / SL_ATR_MULT` |
| `p_long`, `p_short` | both raw probabilities |

## Tuning knobs (`ml_engine/config.py`)

- `HORIZON_BARS` — how many bars forward we predict.
- `TP_ATR_MULT` / `SL_ATR_MULT` — barrier widths in ATR units. Drives R:R.
- `WIN_THRESHOLD` — minimum confidence to emit a side. Lower = more signals, more noise.
- `XGB_PARAMS` / `NUM_ROUNDS` / `EARLY_STOP` — model hyperparams.

## Notes

- **No leakage**: features use only past bars; labels use only future bars; train/valid/test
  are time-ordered, no shuffling.
- **No coupling**: rule engine and ML engine read the same parquet but write to different
  artifact dirs. Disabling either does not break the other.
- **Cold start**: without a trained model, `/api/ml/predictions` returns an `error` field
  per symbol — the page renders an `ERR` card, dashboard stays up.
- **Symbols**: defined in `config.SYMBOL_MAP`. Add a Databento parent symbol to extend.
