"""
Eval agent: backtest the currently-promoted pooled classifier on the last N
trading days. Reports mean per-ticker accuracy + pooled Sharpe / drawdown.
Cold-start returns accuracy=None so orchestrator forces the first train.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

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
    y_pred = model.predict(holdout[model.features])

    out = _metrics(holdout, y_pred)
    out.update({"version": version, "evaluated_at_rows": len(df)})
    logger.info("[eval_agent] %s → mean_acc=%.4f sharpe=%.2f dd=%.2f",
                version, out["accuracy"], out["sharpe"], out["max_drawdown"])
    return out
