"""
Residual agent: per-ticker calibration on top of the pooled classifier.

For each ticker we train a small logistic regression whose inputs are
(pooled_logit, vol_20d, rsi_14, ret_5d, ret_20d). The goal isn't to
replace the pooled model — it's to nudge predictions using ticker-local
context without the overfit risk of a fully per-ticker tree ensemble.

Promotion is gated per-ticker: residual must beat pooled on this ticker's
holdout accuracy. Otherwise the residual is discarded and the pooled
prediction stays the source of truth.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from agents.training_agent import FWD_RET_COL, TARGET_COL, _train_valid_split
from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import registry

logger = logging.getLogger(__name__)

RESIDUAL_FEATURES = ["pooled_logit", "vol_20d", "rsi_14", "ret_5d", "ret_20d"]
MIN_TRAIN_ROWS = 200
MIN_VALID_ROWS = 20
LOGIT_CLIP = 1e-6

try:
    from sklearn.linear_model import LogisticRegression  # type: ignore
    _HAS_SKLEARN = True
except Exception:
    _HAS_SKLEARN = False


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, LOGIT_CLIP, 1.0 - LOGIT_CLIP)
    return np.log(p / (1.0 - p))


class TickerResidual:
    """Wrapper that remembers its feature list so inference is a one-liner."""

    def __init__(self, model: Any, features: list[str]) -> None:
        self.model = model
        self.features = features

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(X[self.features])[:, 1]

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(X) >= 0.5).astype(int)


def train_residual_for_ticker(
    train: pd.DataFrame,
    valid: pd.DataFrame,
) -> tuple[TickerResidual | None, dict]:
    """
    Fit a logistic residual on one ticker's train/valid split.

    Both frames must include RESIDUAL_FEATURES + TARGET_COL. Returns
    (residual, metrics). When the train slice is too thin or the target
    has only one class, returns (None, metrics_with_nones).
    """
    n_train, n_valid = len(train), len(valid)
    metrics: dict = {
        "residual_acc": None,
        "pooled_acc":   None,
        "n_train":      n_train,
        "n_valid":      n_valid,
    }

    if n_train < MIN_TRAIN_ROWS or n_valid < MIN_VALID_ROWS:
        return None, metrics
    if not _HAS_SKLEARN:
        logger.warning("[residual_agent] sklearn unavailable — skipping residual")
        return None, metrics

    y_tr = train[TARGET_COL].to_numpy()
    if len(np.unique(y_tr)) < 2:
        return None, metrics

    X_tr = train[RESIDUAL_FEATURES]
    X_va = valid[RESIDUAL_FEATURES]
    y_va = valid[TARGET_COL].to_numpy()

    try:
        model = LogisticRegression(C=1.0, max_iter=500, random_state=42)
        model.fit(X_tr, y_tr)
    except Exception as exc:
        logger.warning("[residual_agent] fit failed: %s", exc)
        return None, metrics

    residual = TickerResidual(model, RESIDUAL_FEATURES)
    y_pred = residual.predict(X_va)
    residual_acc = float((y_pred == y_va).mean())

    # Pooled baseline on same holdout slice: p >= 0.5 ⇔ logit >= 0.
    pooled_pred = (valid["pooled_logit"].to_numpy() >= 0).astype(int)
    pooled_acc = float((pooled_pred == y_va).mean())

    metrics["residual_acc"] = round(residual_acc, 4)
    metrics["pooled_acc"]   = round(pooled_acc, 4)
    return residual, metrics


async def run(pooled_ensemble: Any | None = None) -> dict:
    """
    Train + attempt promotion for each ticker's residual.

    Uses the production pooled classifier if no ensemble is passed.
    Returns a {ticker: metrics_dict} mapping; empty when preconditions
    aren't met (no cached matrix, no production pooled, etc.).
    """
    df = load_cached_frame()
    if df is None:
        logger.info("[residual_agent] no cached feature matrix — skip")
        return {}

    if pooled_ensemble is None:
        meta = registry.production_metadata()
        if meta is None:
            logger.info("[residual_agent] no production pooled — skip")
            return {}
        pooled_ensemble, _ = registry.load(meta.version)

    proba = pooled_ensemble.predict_proba(df[pooled_ensemble.features])
    df = df.copy()
    df["pooled_logit"] = _logit(np.asarray(proba))

    try:
        train, valid = _train_valid_split(df, settings.holdout_days)
    except RuntimeError as exc:
        logger.warning("[residual_agent] cannot split: %s", exc)
        return {}

    results: dict = {}
    for ticker in sorted(df["ticker"].astype(str).unique()):
        t_train = train[train["ticker"].astype(str) == ticker]
        t_valid = valid[valid["ticker"].astype(str) == ticker]

        residual, metrics = train_residual_for_ticker(t_train, t_valid)
        if residual is None:
            logger.info("[residual_agent] %s skipped (n_train=%d n_valid=%d)",
                        ticker, metrics["n_train"], metrics["n_valid"])
            results[ticker] = {**metrics, "promoted": False}
            continue

        promoted = registry.promote_residual(
            ticker, residual,
            metrics["residual_acc"], metrics["pooled_acc"],
        )
        results[ticker] = {**metrics, "promoted": promoted}
        logger.info(
            "[residual_agent] %s residual_acc=%.4f pooled_acc=%.4f promoted=%s",
            ticker, metrics["residual_acc"], metrics["pooled_acc"], promoted,
        )

    return results
