"""ML engine config: paths, symbols, horizons, hyperparams."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"
HISTORY_DIR = ROOT / "data" / "history"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# Databento
DATASET = "GLBX.MDP3"
# Use continuous front-month (.c.0) — avoids parent-symbol contract merging
# that corrupts OHLCV when illiquid back-months trade at off prices.
SYMBOL_MAP = {
    "GC": "GC.c.0",
    "ES": "ES.c.0",
    "NQ": "NQ.c.0",
    "CL": "CL.c.0",
}
STYPE_IN = "continuous"

# Schemas
SCHEMA_15M = "ohlcv-15m"  # databento native 15m bars
SCHEMA_1M = "ohlcv-1m"

# Training
HORIZON_BARS = 8          # predict 8 bars ahead (= 2h on 15m)
TP_ATR_MULT = 2.0         # take profit = entry +/- 2*ATR
SL_ATR_MULT = 1.0         # stop  loss   = entry -/+ 1*ATR
MIN_RR = TP_ATR_MULT / SL_ATR_MULT
WIN_THRESHOLD = 0.55      # confidence >= this => emit prediction (path-label calibrated)

# Walk-forward
TRAIN_FRAC = 0.7
VALID_FRAC = 0.15
# remaining 0.15 = test

# XGBoost defaults (tuned via ml_engine.tune on ES 1h macro)
XGB_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "max_depth": 3,
    "eta": 0.03,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 1,
    "tree_method": "hist",
    "verbosity": 0,
}
NUM_ROUNDS = 600
EARLY_STOP = 40
