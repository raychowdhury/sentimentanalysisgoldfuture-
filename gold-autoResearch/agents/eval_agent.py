"""
Eval agent: backtest the current production model on the last N trading days.

Returns a plain dict so orchestrator + report_agent can serialize freely.
When no production model exists (cold start), returns accuracy=None and
the orchestrator treats that as "below threshold" to force a first train.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import registry

logger = logging.getLogger(__name__)

TARGET_COL = "y_next_dir"
FWD_RET_COL = "y_next_ret"


def _metrics(y_true: pd.Series, y_pred: np.ndarray, fwd_rets: np.ndarray) -> dict:
    accuracy = float((y_pred == y_true.values).mean())
    signal   = np.where(y_pred == 1, 1.0, -1.0)
    pnl      = signal * fwd_rets
    sharpe   = float(pnl.mean() / (pnl.std() + 1e-9) * np.sqrt(252))
    equity   = np.cumprod(1 + pnl)
    drawdown = float((equity / np.maximum.accumulate(equity) - 1).min() * -1)
    return {
        "accuracy":     round(accuracy, 4),
        "sharpe":       round(sharpe, 4),
        "max_drawdown": round(drawdown, 4),
        "pred_up_rate": round(float(y_pred.mean()), 4),
        "n_samples":    int(len(y_true)),
    }


async def run(version: str | None = None) -> dict:
    df = load_cached_frame()
    if df is None:
        return {"accuracy": None, "reason": "no cached feature matrix"}

    holdout = df.iloc[-settings.holdout_days:]
    if len(holdout) < 10:
        return {"accuracy": None, "reason": "holdout too small"}

    if version is None:
        meta = registry.production_metadata()
        if meta is None:
            logger.info("[eval_agent] no production model — returning sentinel")
            return {"accuracy": None, "reason": "no production model"}
        version = meta.version

    model, meta = registry.load(version)
    y_true   = holdout[TARGET_COL]
    fwd_rets = holdout[FWD_RET_COL].values
    y_pred   = model.predict(holdout)

    out = _metrics(y_true, y_pred, fwd_rets)
    out.update({"version": version, "evaluated_at_rows": len(df)})
    logger.info("[eval_agent] %s → acc=%.4f sharpe=%.2f dd=%.2f",
                version, out["accuracy"], out["sharpe"], out["max_drawdown"])
    return out
