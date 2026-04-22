"""
Training agent: fit a pooled XGBoost classifier on the long-format matrix.

Ticker is passed as an ordinal-encoded categorical feature so a single model
can share macro/market signal across names while still separating ticker-
specific drift. Metrics reported are the mean per-ticker directional
accuracy plus pooled Sharpe / drawdown from an equal-weight long/short
paper portfolio that goes long predicted-up names and short predicted-down.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import ModelMetadata, registry

logger = logging.getLogger(__name__)

TARGET_COL  = "y_next_dir"
FWD_RET_COL = "y_next_ret"
RANDOM_SEED = 42
# Pool has 20 tickers × ~250 rows/yr — last 3 years keeps the regime recent.
TRAIN_WINDOW_DAYS = 750

try:
    from xgboost import XGBClassifier  # type: ignore
    _HAS_XGB = True
except Exception:
    _HAS_XGB = False


# ── Fallback baseline (used when XGBoost isn't installed) ────────────────────

class _MajorityClassifier:
    """Predicts the train-set majority class for every row."""

    def __init__(self) -> None:
        self._bias = 0.5

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "_MajorityClassifier":
        self._bias = float(y.mean())
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probs = np.full(len(X), self._bias, dtype=float)
        return np.column_stack([1 - probs, probs])


# ── Feature selection / encoding ─────────────────────────────────────────────

_DROP = {
    "date", TARGET_COL, FWD_RET_COL,
    "close", "Open", "High", "Low", "Close", "Adj Close", "Volume",
}


def _feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in _DROP]


def _encode_categoricals(df: pd.DataFrame, features: list[str],
                         ticker_map: dict[str, int] | None = None,
                         sector_map: dict[str, int] | None = None,
                         ) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    """Ordinal-encode ticker + sector. Reuses maps at inference time."""
    out = df[features].copy()
    if "ticker" in out.columns:
        if ticker_map is None:
            ticker_map = {t: i for i, t in enumerate(sorted(out["ticker"].astype(str).unique()))}
        out["ticker"] = out["ticker"].astype(str).map(ticker_map).fillna(-1).astype(int)
    if "sector" in out.columns:
        if sector_map is None:
            sector_map = {s: i for i, s in enumerate(sorted(out["sector"].astype(str).unique()))}
        out["sector"] = out["sector"].astype(str).map(sector_map).fillna(-1).astype(int)
    return out, (ticker_map or {}), (sector_map or {})


def _train_valid_split(df: pd.DataFrame, holdout_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Time-based split on the date column (not row count) — the matrix has
    20 rows per day so slicing by rows alone would mix tickers across the
    boundary and leak label.
    """
    unique_dates = sorted(df["date"].unique())
    if len(unique_dates) <= holdout_days + 30:
        raise RuntimeError("not enough trading days to split with requested holdout")
    cutoff = unique_dates[-holdout_days]
    train = df[df["date"] <  cutoff]
    valid = df[df["date"] >= cutoff]
    return train, valid


# ── Ensemble wrapper ─────────────────────────────────────────────────────────

class PooledStockClassifier:
    """
    Thin wrapper around the underlying classifier that remembers the feature
    list and the categorical encoding maps, so inference is a one-liner for
    eval_agent and downstream consumers (app.py).
    """

    def __init__(
        self,
        model: Any,
        features: list[str],
        ticker_map: dict[str, int],
        sector_map: dict[str, int],
    ) -> None:
        self.model      = model
        self.features   = features
        self.ticker_map = ticker_map
        self.sector_map = sector_map

    def _encoded(self, X: pd.DataFrame) -> pd.DataFrame:
        out, _, _ = _encode_categoricals(
            X, self.features, self.ticker_map, self.sector_map,
        )
        return out

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self._encoded(X))[:, 1]

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(X) >= 0.5).astype(int)


# ── Hparam sampling with overlay ─────────────────────────────────────────────

def _sample_hparams(grid: dict) -> dict:
    return {k: random.choice(v) for k, v in grid.items()}


def _effective_xgb_grid() -> dict:
    base = {k: list(v) for k, v in settings.xgb_hparam_grid.items()}
    overlay_path = settings.root_dir / "config" / "overrides.json"
    if not overlay_path.exists():
        return base
    try:
        overlay = json.loads(overlay_path.read_text()).get("xgb_hparam_grid") or {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[training_agent] could not read overrides.json: %s", exc)
        return base
    for k, v in overlay.items():
        if isinstance(v, list) and v and k in base:
            base[k] = v
    return base


# ── Metric helpers ───────────────────────────────────────────────────────────

def _per_ticker_accuracy(df_valid: pd.DataFrame, y_pred: np.ndarray) -> dict[str, float]:
    tmp = df_valid[[TARGET_COL, "ticker"]].copy()
    tmp["pred"] = y_pred
    tmp["correct"] = (tmp["pred"] == tmp[TARGET_COL]).astype(int)
    return {str(t): float(g["correct"].mean()) for t, g in tmp.groupby("ticker", observed=True)}


def _pooled_pnl(df_valid: pd.DataFrame, y_pred: np.ndarray) -> tuple[float, float]:
    """
    Equal-weight long/short daily PnL: for each trading day, long the set of
    tickers predicted up, short the set predicted down, average across names.
    Returns (annualized Sharpe, max drawdown on compounded equity).
    """
    tmp = df_valid[["date", FWD_RET_COL]].copy()
    tmp["pred"] = y_pred
    tmp["signal"] = np.where(tmp["pred"] == 1, 1.0, -1.0)
    tmp["pnl_contrib"] = tmp["signal"] * tmp[FWD_RET_COL]
    daily = tmp.groupby("date")["pnl_contrib"].mean()
    if daily.empty or daily.std() == 0:
        return 0.0, 0.0
    sharpe = float(daily.mean() / (daily.std() + 1e-9) * np.sqrt(252))
    equity = (1 + daily).cumprod()
    max_dd = float((equity / equity.cummax() - 1).min() * -1)
    return sharpe, max_dd


# ── Public entry ─────────────────────────────────────────────────────────────

async def run(experiment_note: str = "") -> tuple[PooledStockClassifier, ModelMetadata]:
    df = load_cached_frame()
    if df is None:
        raise RuntimeError("no cached feature matrix — run data_agent first")

    # Recent-regime cap on trading days (not rows).
    unique_dates = sorted(df["date"].unique())
    if len(unique_dates) > TRAIN_WINDOW_DAYS:
        cutoff = unique_dates[-TRAIN_WINDOW_DAYS]
        df = df[df["date"] >= cutoff]

    features = _feature_columns(df)
    train, valid = _train_valid_split(df, settings.holdout_days)

    X_tr_raw, y_tr = train[features], train[TARGET_COL]
    X_va_raw, y_va = valid[features], valid[TARGET_COL]
    X_tr, ticker_map, sector_map = _encode_categoricals(X_tr_raw, features)
    X_va, _, _ = _encode_categoricals(X_va_raw, features, ticker_map, sector_map)

    base_rate = float(y_tr.mean())
    logger.info("[training_agent] train=%d valid=%d base_rate_up=%.3f n_tickers=%d",
                len(X_tr), len(X_va), base_rate, len(ticker_map))

    xgb_hp = _sample_hparams(_effective_xgb_grid())
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    if _HAS_XGB:
        n_pos = int(y_tr.sum())
        n_neg = int(len(y_tr) - n_pos)
        spw   = (n_neg / n_pos) if n_pos > 0 else 1.0
        model = XGBClassifier(
            **xgb_hp, eval_metric="logloss",
            scale_pos_weight=spw,
            n_jobs=2, random_state=RANDOM_SEED,
        )
        model.fit(X_tr, y_tr)
        logger.info("[training_agent] xgb trained (scale_pos_weight=%.3f)", spw)
    else:
        logger.warning("[training_agent] xgboost unavailable — majority baseline")
        model = _MajorityClassifier().fit(X_tr, y_tr)

    ensemble = PooledStockClassifier(model, features, ticker_map, sector_map)

    # Holdout evaluation — feed the raw (un-encoded) validation rows so the
    # wrapper applies the persisted ticker/sector maps consistently.
    y_pred = ensemble.predict(X_va_raw)
    per_ticker = _per_ticker_accuracy(valid, y_pred)
    mean_acc   = float(np.mean(list(per_ticker.values()))) if per_ticker else 0.0
    pred_up_rate = float(y_pred.mean())
    sharpe, max_dd = _pooled_pnl(valid, y_pred)
    logger.info("[training_agent] mean_acc=%.4f pred_up_rate=%.3f sharpe=%.2f dd=%.2f",
                mean_acc, pred_up_rate, sharpe, max_dd)

    meta = ModelMetadata(
        version=registry.new_version(),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        accuracy=round(mean_acc, 4),
        sharpe=round(sharpe, 4),
        max_drawdown=round(max_dd, 4),
        pred_up_rate=round(pred_up_rate, 4),
        features=features,
        hyperparams={"xgb": xgb_hp, "ticker_map_size": len(ticker_map)},
        notes=experiment_note,
        per_ticker_acc={k: round(v, 4) for k, v in per_ticker.items()},
    )
    logger.info("[training_agent] trained %s — mean_acc=%.4f sharpe=%.2f dd=%.2f",
                meta.version, meta.accuracy, meta.sharpe, meta.max_drawdown)
    registry.save(ensemble, meta)
    return ensemble, meta
