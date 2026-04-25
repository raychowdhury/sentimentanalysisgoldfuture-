"""
Order Flow Engine configuration.

Defaults live here. If the root `config.py` defines `ORDER_FLOW_*` constants
they override these, letting the rest of the repo tune the engine from one
place without importing this module.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import config as _root  # repo-level config.py
except Exception:  # pragma: no cover — engine still usable standalone
    _root = None


def _get(name: str, default):
    if _root is not None and hasattr(_root, name):
        return getattr(_root, name)
    return default


# ── Data selection ───────────────────────────────────────────────────────────
OF_SYMBOL: str        = _get("ORDER_FLOW_SYMBOL", "ES=F")
OF_TIMEFRAMES: list[str] = _get("ORDER_FLOW_TIMEFRAMES", ["5m", "15m", "1h", "1d"])
OF_LOOKBACK_DAYS: int = _get("ORDER_FLOW_LOOKBACK_DAYS", 180)
OF_ANCHOR_TF: str     = _get("ORDER_FLOW_ANCHOR_TF", "15m")

# yfinance window caps per interval — enforced by data_loader.
YF_INTRADAY_CAPS: dict[str, int] = {
    "1m":  7,
    "2m":  60,
    "5m":  60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h":  730,
    "90m": 60,
}

# Forward-window bar count per timeframe. Longer TFs need fewer forward bars
# to capture a meaningful move.
OF_FORWARD_BARS: dict[str, int] = {
    "5m":  12,   # ~1 hour
    "15m": 8,    # ~2 hours
    "1h":  4,    # ~4 hours
    "1d":  1,    # next session
}

# A forward reversal is "real" only if the move exceeds this multiple of ATR.
# Prevents labelling noise as reversals.
OF_LABEL_HORIZON_ATR: float = 0.5

# Alert gating.
# Defaults reflect honest-eval results on ES=F (180d, proxy flow):
# only r3_absorption_resistance and r6_bear_trap showed positive expectancy,
# and 15m was cleaner than 5m. Tune back via root config.py if desired.
OF_ALERT_MIN_CONF: int = _get("ORDER_FLOW_ALERT_MIN_CONF", 75)
OF_ALERT_ALLOWED_LABELS: set[str] = set(_get(
    "ORDER_FLOW_ALERT_ALLOWED_LABELS",
    {"buyer_absorption", "bearish_trap"},
))
OF_ALERT_ALLOWED_TFS: set[str] = set(_get(
    "ORDER_FLOW_ALERT_ALLOWED_TFS",
    {"15m"},
))

# Output location — shares the repo's outputs/ convention.
_REPO_ROOT = Path(__file__).resolve().parents[2]
OF_OUTPUT_SUBDIR: str = _get("ORDER_FLOW_OUTPUT_SUBDIR", "order_flow")
OF_OUTPUT_DIR = Path(_get("OUTPUT_DIR", str(_REPO_ROOT / "outputs"))) / OF_OUTPUT_SUBDIR

# Package-local paths
_PKG_ROOT = Path(__file__).resolve().parents[1]
OF_RAW_DIR        = _PKG_ROOT / "data" / "raw"
OF_PROCESSED_DIR  = _PKG_ROOT / "data" / "processed"
OF_MODELS_DIR     = _PKG_ROOT / "models"
OF_TEMPLATES_DIR  = _PKG_ROOT / "templates"

for _d in (OF_OUTPUT_DIR, OF_RAW_DIR, OF_PROCESSED_DIR, OF_MODELS_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Rule thresholds ──────────────────────────────────────────────────────────
RULE_DELTA_DOMINANCE   = 0.4   # |delta_ratio| above this = clear directional flow
RULE_ABSORPTION_DELTA  = 0.5
RULE_TRAP_DELTA        = 0.3
RULE_SR_ATR_MULT       = 0.5   # "near S/R" window in ATR multiples
RULE_ABSORPTION_RET_CAP_ATR_PCT = 0.1  # forward move must be tiny for absorption
RULE_CVD_CORR_WINDOW   = 20
RULE_CVD_CORR_THRESH   = -0.5
RULE_SR_LOOKBACK       = 50

# ── Model ────────────────────────────────────────────────────────────────────
WF_FOLD_SIZE: int = 500
WF_N_FOLDS: int   = 3
KEEP_NORMAL_FRAC: float = 0.1   # downsample majority class

XGB_PARAMS: dict = {
    "objective":      "multi:softprob",
    "n_estimators":   300,
    "max_depth":      4,
    "learning_rate":  0.05,
    "subsample":      0.8,
    "colsample_bytree": 0.8,
    "eval_metric":    "mlogloss",
    "n_jobs":         2,
    "random_state":   42,
}

RF_PARAMS: dict = {
    "n_estimators":   200,
    "max_depth":      6,
    "class_weight":   "balanced",
    "n_jobs":         2,
    "random_state":   42,
}

# ── Quality gates / cooldown ─────────────────────────────────────────────────
# Suppress same-label alerts within this many bars on the same (symbol, tf).
# Prevents flood when a regime persists across many bars.
ALERT_COOLDOWN_BARS: int = 8

# Volume gate: skip alerts on bars whose volume is below this percentile of
# the trailing window. Kills after-hours / illiquid noise where proxies break.
VOLUME_GATE_PCTL: float = 0.20
VOLUME_GATE_WINDOW: int = 200

# Sqlite alert store
ALERTS_DB_NAME: str = "alerts.sqlite"

# ── Labels ───────────────────────────────────────────────────────────────────
LABEL_CLASSES: list[str] = [
    "normal_behavior",
    "buyer_absorption",
    "seller_absorption",
    "bullish_trap",
    "bearish_trap",
    "possible_reversal",
]
