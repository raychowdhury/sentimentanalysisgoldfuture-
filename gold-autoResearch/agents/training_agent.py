"""
Training agent: fits an XGBoost + LSTM ensemble on the cached feature matrix.

The LSTM is optional — when torch isn't available (CI, lightweight runs) the
agent falls back to XGBoost alone. Each call returns a trained model object
and the ModelMetadata that will be written to the registry if promoted.
"""
from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from config.settings import settings
from data.pipeline import load_cached_frame
from models.model_registry import ModelMetadata, registry

logger = logging.getLogger(__name__)

TARGET_COL = "y_next_dir"
FWD_RET_COL = "y_next_ret"
RANDOM_SEED = 42
TRAIN_WINDOW = 750  # recent-regime cap; older bars exceed this window are dropped

try:
    from xgboost import XGBClassifier  # type: ignore
    _HAS_XGB = True
except Exception:  # ImportError, OSError, XGBoostError (native lib missing)
    _HAS_XGB = False

_LSTM_DISABLED = os.getenv("DISABLE_LSTM", "").lower() in {"1", "true", "yes"}

if _LSTM_DISABLED:
    _HAS_TORCH = False
else:
    try:
        import torch
        from torch import nn
        _HAS_TORCH = True
    except Exception:
        _HAS_TORCH = False


if _HAS_TORCH:
    class LSTMClassifier(nn.Module):  # module-level so pickle can find it
        def __init__(self, n_features: int, hidden: int, layers: int, dropout: float) -> None:
            super().__init__()
            self.lstm = nn.LSTM(
                n_features, hidden, layers,
                batch_first=True,
                dropout=dropout if layers > 1 else 0,
            )
            self.head = nn.Linear(hidden, 1)

        def forward(self, x):  # type: ignore[override]
            out, _ = self.lstm(x)
            return torch.sigmoid(self.head(out[:, -1, :]))


class _SignClassifier:
    """
    Tiny fallback classifier used when neither XGBoost nor Torch is available.
    Predicts "up" when the mean of the most-recent `ret_1d` is positive, else
    "down". Good enough to exercise the pipeline end-to-end.
    """

    def __init__(self) -> None:
        self._bias = 0.5

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "_SignClassifier":
        self._bias = float(y.mean())
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probs = np.full(len(X), self._bias, dtype=float)
        return np.column_stack([1 - probs, probs])


# ── Feature / split helpers ──────────────────────────────────────────────────

def _feature_columns(df: pd.DataFrame) -> list[str]:
    drop = {TARGET_COL, FWD_RET_COL,
            "Open", "High", "Low", "Close", "Adj Close", "Volume"}
    return [c for c in df.columns if c not in drop]


def _train_valid_split(df: pd.DataFrame, holdout: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if len(df) <= holdout + 30:
        raise RuntimeError("not enough rows to train with the requested holdout")
    return df.iloc[:-holdout], df.iloc[-holdout:]


# ── Ensemble wrapper — what gets pickled into the registry ───────────────────

class GoldDirectionEnsemble:
    """Blend XGBoost probability with LSTM probability (mean). Deterministic
    fallback to whichever model is present."""

    def __init__(self, xgb_model: Any | None, lstm_model: Any | None,
                 features: list[str]) -> None:
        self.xgb_model  = xgb_model
        self.lstm_model = lstm_model
        self.features   = features

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probs: list[np.ndarray] = []
        if self.xgb_model is not None:
            probs.append(self.xgb_model.predict_proba(X[self.features])[:, 1])
        if self.lstm_model is not None:
            probs.append(_lstm_proba(self.lstm_model, X[self.features]))
        if not probs:
            raise RuntimeError("ensemble has no underlying models")
        return np.mean(probs, axis=0)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(X) >= 0.5).astype(int)


# ── LSTM training helpers ────────────────────────────────────────────────────

def _train_lstm(X: pd.DataFrame, y: pd.Series, hp: dict) -> Any | None:
    if not _HAS_TORCH:
        logger.info("[training_agent] torch unavailable — skipping LSTM branch")
        return None

    model = LSTMClassifier(X.shape[1], hp["hidden"], hp["layers"], hp["dropout"])
    opt   = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.BCELoss()
    tensor_x = torch.tensor(X.values, dtype=torch.float32).unsqueeze(1)
    tensor_y = torch.tensor(y.values, dtype=torch.float32).unsqueeze(1)
    model.train()
    for _ in range(30):
        opt.zero_grad()
        pred = model(tensor_x)
        loss = loss_fn(pred, tensor_y)
        loss.backward()
        opt.step()
    model.eval()
    return model


def _lstm_proba(model: Any, X: pd.DataFrame) -> np.ndarray:
    with torch.no_grad():  # type: ignore[attr-defined]
        tensor_x = torch.tensor(X.values, dtype=torch.float32).unsqueeze(1)
        return model(tensor_x).cpu().numpy().flatten()


# ── Public entry point ───────────────────────────────────────────────────────

def _sample_hparams(grid: dict) -> dict:
    return {k: random.choice(v) for k, v in grid.items()}


def _effective_xgb_grid() -> dict:
    """Merge settings.xgb_hparam_grid with any overrides persisted by
    meta_optimizer. Overlay keys replace base keys; unknown keys ignored."""
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


async def run(experiment_note: str = "") -> tuple[GoldDirectionEnsemble, ModelMetadata]:
    """Train a candidate model. Caller evaluates it and decides on promotion."""
    df = load_cached_frame()
    if df is None:
        raise RuntimeError("no cached feature matrix — run data_agent first")

    features = _feature_columns(df)
    df_recent = df.iloc[-TRAIN_WINDOW:] if len(df) > TRAIN_WINDOW else df
    train, valid = _train_valid_split(df_recent, settings.holdout_days)
    X_tr, y_tr = train[features], train[TARGET_COL]
    X_va, y_va = valid[features], valid[TARGET_COL]
    base_rate = float(y_tr.mean())
    logger.info("[training_agent] train=%d valid=%d base_rate_up=%.3f",
                len(X_tr), len(X_va), base_rate)

    xgb_hp  = _sample_hparams(_effective_xgb_grid())
    lstm_hp = _sample_hparams(settings.lstm_hparam_grid)

    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    xgb_model = None
    if _HAS_XGB:
        # Training target up-rate is usually the majority class (~0.55–0.60).
        # Default scale_pos_weight=1 lets the tree collapse to "always predict
        # positive" since positive minimizes logloss under imbalance. Setting
        # sum(neg)/sum(pos) re-weights the gradient so the down class carries
        # equal total mass — restores discriminative learning.
        n_pos = int(y_tr.sum())
        n_neg = int(len(y_tr) - n_pos)
        spw   = (n_neg / n_pos) if n_pos > 0 else 1.0
        xgb_model = XGBClassifier(
            **xgb_hp, eval_metric="logloss",
            scale_pos_weight=spw,
            n_jobs=2, random_state=RANDOM_SEED,
        )
        xgb_model.fit(X_tr, y_tr)
        logger.info("[training_agent] xgb scale_pos_weight=%.3f (n_pos=%d, n_neg=%d)",
                    spw, n_pos, n_neg)

    try:
        lstm_model = _train_lstm(X_tr, y_tr, lstm_hp)
    except Exception as exc:
        # Torch native crashes (segfault on bleeding-edge Pythons, OOM, etc.)
        # shouldn't take the whole cycle down — XGB alone still produces a
        # valid candidate the promotion gate can evaluate.
        logger.warning("[training_agent] LSTM training failed (%s) — "
                       "falling back to XGB-only ensemble", exc)
        lstm_model = None

    # Neither XGB nor torch available → fall back so the pipeline still runs.
    if xgb_model is None and lstm_model is None:
        logger.warning("[training_agent] no ML backend available — using sign baseline")
        xgb_model = _SignClassifier().fit(X_tr, y_tr)

    ensemble = GoldDirectionEnsemble(xgb_model, lstm_model, features)
    y_pred = ensemble.predict(X_va)
    accuracy = float((y_pred == y_va.values).mean())
    pred_up_rate = float(y_pred.mean())
    logger.info("[training_agent] holdout_acc=%.4f pred_up_rate=%.3f base_up_rate=%.3f",
                accuracy, pred_up_rate, float(y_va.mean()))

    # Sharpe / drawdown from next-day P&L aligned to y_next_dir prediction.
    # Signal is +1 for long, -1 for short; P&L = signal * forward 1-day return.
    fwd_rets = df_recent.loc[X_va.index, FWD_RET_COL].values
    signal   = np.where(y_pred == 1, 1.0, -1.0)
    pnl      = signal * fwd_rets
    sharpe   = float(pnl.mean() / (pnl.std() + 1e-9) * np.sqrt(252))
    equity   = np.cumprod(1 + pnl)
    max_dd   = float((equity / np.maximum.accumulate(equity) - 1).min() * -1)

    meta = ModelMetadata(
        version=registry.new_version(),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        accuracy=round(accuracy, 4),
        sharpe=round(sharpe, 4),
        max_drawdown=round(max_dd, 4),
        pred_up_rate=round(pred_up_rate, 4),
        features=features,
        hyperparams={"xgb": xgb_hp, "lstm": lstm_hp},
        notes=experiment_note,
    )
    logger.info("[training_agent] trained %s — acc=%.4f sharpe=%.2f dd=%.2f",
                meta.version, meta.accuracy, meta.sharpe, meta.max_drawdown)
    registry.save(ensemble, meta)
    return ensemble, meta
