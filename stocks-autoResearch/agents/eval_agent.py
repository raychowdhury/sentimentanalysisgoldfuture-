"""
Eval agent: backtest the currently-promoted pooled classifier on the last N
trading days. Reports mean per-ticker accuracy + pooled Sharpe / drawdown.
Cold-start returns accuracy=None so orchestrator forces the first train.

When per-ticker residual models are promoted, the eval also reports
hybrid metrics: for tickers with a promoted residual, the residual's
prediction replaces pooled; other tickers keep pooled.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from agents.residual_agent import RESIDUAL_FEATURES, _logit
from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import registry

logger = logging.getLogger(__name__)

TARGET_COL  = "y_next_dir"
FWD_RET_COL = "y_next_ret"


def _metrics(df_holdout: pd.DataFrame, y_pred: np.ndarray) -> dict:
    per_ticker = {}
    tmp = df_holdout[[TARGET_COL, "ticker"]].copy()
    tmp["pred"] = y_pred
    tmp["correct"] = (tmp["pred"] == tmp[TARGET_COL]).astype(int)
    for t, g in tmp.groupby("ticker", observed=True):
        per_ticker[str(t)] = round(float(g["correct"].mean()), 4)
    mean_acc = float(np.mean(list(per_ticker.values()))) if per_ticker else 0.0

    pnl_df = df_holdout[["date", FWD_RET_COL]].copy()
    pnl_df["signal"] = np.where(y_pred == 1, 1.0, -1.0)
    pnl_df["pnl"]    = pnl_df["signal"] * pnl_df[FWD_RET_COL]
    daily = pnl_df.groupby("date")["pnl"].mean()
    if daily.empty or daily.std() == 0:
        sharpe, max_dd = 0.0, 0.0
    else:
        sharpe = float(daily.mean() / (daily.std() + 1e-9) * np.sqrt(252))
        equity = (1 + daily).cumprod()
        max_dd = float((equity / equity.cummax() - 1).min() * -1)

    return {
        "accuracy":       round(mean_acc, 4),
        "sharpe":         round(sharpe, 4),
        "max_drawdown":   round(max_dd, 4),
        "pred_up_rate":   round(float(y_pred.mean()), 4),
        "n_samples":      int(len(df_holdout)),
        "per_ticker_acc": per_ticker,
    }


def _apply_residuals(
    holdout: pd.DataFrame,
    pooled_model,
    y_pred_pooled: np.ndarray,
) -> tuple[np.ndarray, list[str]]:
    """
    Replace pooled predictions with residual predictions for any ticker
    that has a promoted residual model on disk. Returns (y_pred_hybrid,
    list of tickers whose residuals were applied).
    """
    pooled_proba = pooled_model.predict_proba(holdout[pooled_model.features])
    work = holdout.copy()
    work["pooled_logit"] = _logit(np.asarray(pooled_proba))

    y_pred = y_pred_pooled.copy()
    applied: list[str] = []
    for ticker in sorted(work["ticker"].astype(str).unique()):
        residual = registry.load_residual(ticker)
        if residual is None:
            continue
        mask = (work["ticker"].astype(str) == ticker).to_numpy()
        if not mask.any():
            continue
        y_pred[mask] = residual.predict(work.loc[mask, RESIDUAL_FEATURES])
        applied.append(ticker)
    return y_pred, applied


async def run(version: str | None = None) -> dict:
    df = load_cached_frame()
    if df is None:
        return {"accuracy": None, "reason": "no cached feature matrix"}

    unique_dates = sorted(df["date"].unique())
    if len(unique_dates) < settings.holdout_days + 5:
        return {"accuracy": None, "reason": "not enough trading days for holdout"}

    cutoff = unique_dates[-settings.holdout_days]
    holdout = df[df["date"] >= cutoff].copy()

    if version is None:
        meta = registry.production_metadata()
        if meta is None:
            logger.info("[eval_agent] no production model — returning sentinel")
            return {"accuracy": None, "reason": "no production model"}
        version = meta.version

    model, _meta = registry.load(version)
    y_pred_pooled = model.predict(holdout[model.features])

    out = _metrics(holdout, y_pred_pooled)
    out.update({"version": version, "evaluated_at_rows": len(df)})

    y_pred_hybrid, residuals_applied = _apply_residuals(holdout, model, y_pred_pooled)
    if residuals_applied:
        hybrid = _metrics(holdout, y_pred_hybrid)
        out["hybrid_accuracy"]       = hybrid["accuracy"]
        out["hybrid_sharpe"]         = hybrid["sharpe"]
        out["hybrid_max_drawdown"]   = hybrid["max_drawdown"]
        out["hybrid_per_ticker_acc"] = hybrid["per_ticker_acc"]
        out["residuals_applied"]     = residuals_applied
    else:
        out["residuals_applied"] = []

    logger.info(
        "[eval_agent] %s → pooled_acc=%.4f sharpe=%.2f dd=%.2f residuals=%d",
        version, out["accuracy"], out["sharpe"], out["max_drawdown"],
        len(residuals_applied),
    )
    if residuals_applied:
        logger.info(
            "[eval_agent] hybrid_acc=%.4f (pooled_acc=%.4f, residuals=%s)",
            out["hybrid_accuracy"], out["accuracy"], ",".join(residuals_applied),
        )
    return out
