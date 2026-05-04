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
ATR_PCT_COL = "atr_pct_14"
RANDOM_SEED = 42
TRAIN_WINDOW = 750  # recent-regime cap; older bars exceed this window are dropped
WF_FOLD_SIZE = 60   # walk-forward: each validation fold covers this many sessions
WF_N_FOLDS   = 3    # walk-forward: number of contiguous validation windows
# Drop training/validation rows whose next-day |return| is smaller than
# k × ATR_pct — these moves are noise that bias the model toward coin-flip
# behavior. k=0 disables the filter (backward-compatible with older caches
# that don't have the atr_pct_14 column).
ATR_THRESHOLD_K = float(os.getenv("ATR_THRESHOLD_K", "0.25"))

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


def _significant_mask(df: pd.DataFrame, k: float) -> pd.Series:
    """
    True where |next-day return| is at least k × ATR_pct. Rows failing the
    threshold are treated as noise and excluded from train + validation. If
    the cache predates ATR features or k=0, every row qualifies.
    """
    if k <= 0 or ATR_PCT_COL not in df.columns:
        return pd.Series(True, index=df.index)
    return df[FWD_RET_COL].abs() >= k * df[ATR_PCT_COL]


def _fit_ensemble(
    X_tr: pd.DataFrame, y_tr: pd.Series,
    features: list[str], xgb_hp: dict, lstm_hp: dict,
) -> "GoldDirectionEnsemble":
    """Fit one XGBoost + LSTM ensemble on the given train slice."""
    xgb_model = None
    if _HAS_XGB:
        n_pos = int(y_tr.sum())
        n_neg = int(len(y_tr) - n_pos)
        spw   = (n_neg / n_pos) if n_pos > 0 else 1.0
        xgb_model = XGBClassifier(
            **xgb_hp, eval_metric="logloss",
            scale_pos_weight=spw,
            n_jobs=2, random_state=RANDOM_SEED,
        )
        xgb_model.fit(X_tr, y_tr)

    try:
        lstm_model = _train_lstm(X_tr, y_tr, lstm_hp)
    except Exception as exc:
        logger.warning("[training_agent] LSTM training failed (%s) — "
                       "falling back to XGB-only ensemble", exc)
        lstm_model = None

    if xgb_model is None and lstm_model is None:
        logger.warning("[training_agent] no ML backend available — using sign baseline")
        xgb_model = _SignClassifier().fit(X_tr, y_tr)

    return GoldDirectionEnsemble(xgb_model, lstm_model, features)


def _fold_metrics(
    ensemble: "GoldDirectionEnsemble",
    valid: pd.DataFrame, features: list[str],
) -> dict:
    X_va, y_va = valid[features], valid[TARGET_COL]
    y_pred = ensemble.predict(X_va)
    accuracy = float((y_pred == y_va.values).mean())
    pred_up_rate = float(y_pred.mean())
    fwd_rets = valid[FWD_RET_COL].values
    signal   = np.where(y_pred == 1, 1.0, -1.0)
    pnl      = signal * fwd_rets
    sharpe   = float(pnl.mean() / (pnl.std() + 1e-9) * np.sqrt(252))
    equity   = np.cumprod(1 + pnl)
    max_dd   = float((equity / np.maximum.accumulate(equity) - 1).min() * -1)
    return {
        "accuracy": accuracy, "sharpe": sharpe, "max_dd": max_dd,
        "pred_up_rate": pred_up_rate, "n": int(len(y_va)),
    }


def _walk_forward_eval(
    df_recent: pd.DataFrame, features: list[str],
    xgb_hp: dict, lstm_hp: dict,
) -> tuple[list[dict], pd.DataFrame]:
    """
    Run N expanding-window walk-forward folds. Each fold fits a fresh ensemble
    on data strictly before the fold and evaluates on a contiguous validation
    window. Returns per-fold metrics plus the train slice of the final fold,
    which becomes the training set for the production candidate.
    """
    total_holdout = WF_FOLD_SIZE * WF_N_FOLDS
    if len(df_recent) <= total_holdout + 30:
        # Not enough rows for walk-forward — fall back to single split so the
        # loop can still produce candidates during the cold-start period.
        train, valid = _train_valid_split(df_recent, settings.holdout_days)
        train_f = train[_significant_mask(train, ATR_THRESHOLD_K)]
        valid_f = valid[_significant_mask(valid, ATR_THRESHOLD_K)]
        ensemble = _fit_ensemble(train_f[features], train_f[TARGET_COL],
                                 features, xgb_hp, lstm_hp)
        return [_fold_metrics(ensemble, valid_f, features)], train_f

    folds: list[dict] = []
    final_train: pd.DataFrame | None = None
    for k in range(WF_N_FOLDS):
        valid_end   = len(df_recent) - k * WF_FOLD_SIZE
        valid_start = valid_end - WF_FOLD_SIZE
        train = df_recent.iloc[:valid_start]
        valid = df_recent.iloc[valid_start:valid_end]
        # ATR-significance filter: drop noise rows to stop the classifier
        # learning "coin flip" on sub-ATR moves. Applied symmetrically so the
        # validation metric reflects performance on tradeable setups.
        train = train[_significant_mask(train, ATR_THRESHOLD_K)]
        valid = valid[_significant_mask(valid, ATR_THRESHOLD_K)]
        if len(train) < 30 or len(valid) < 10:
            logger.warning("[training_agent] fold %d skipped — insufficient "
                           "rows after ATR filter (train=%d valid=%d)",
                           k, len(train), len(valid))
            continue
        ensemble = _fit_ensemble(train[features], train[TARGET_COL],
                                 features, xgb_hp, lstm_hp)
        folds.append(_fold_metrics(ensemble, valid, features))
        if final_train is None:
            # First usable fold defines the production-training slice so the
            # shipped model isn't re-trained on any validation window.
            final_train = train
    if not folds or final_train is None:
        raise RuntimeError("walk-forward produced no usable folds — "
                           "ATR filter may be too aggressive for this cache")
    return folds, final_train


async def run(experiment_note: str = "") -> tuple[GoldDirectionEnsemble, ModelMetadata]:
    """Train a candidate model. Caller evaluates it and decides on promotion."""
    df = load_cached_frame()
    if df is None:
        raise RuntimeError("no cached feature matrix — run data_agent first")

    features = _feature_columns(df)
    df_recent = df.iloc[-TRAIN_WINDOW:] if len(df) > TRAIN_WINDOW else df

    xgb_hp  = _sample_hparams(_effective_xgb_grid())
    lstm_hp = _sample_hparams(settings.lstm_hparam_grid)

    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    folds, final_train = _walk_forward_eval(df_recent, features, xgb_hp, lstm_hp)

    # Aggregate metrics across folds — mean reflects expected out-of-sample
    # behavior far better than a single 90-row window.
    accuracy     = float(np.mean([f["accuracy"]     for f in folds]))
    sharpe       = float(np.mean([f["sharpe"]       for f in folds]))
    max_dd       = float(np.mean([f["max_dd"]       for f in folds]))
    pred_up_rate = float(np.mean([f["pred_up_rate"] for f in folds]))
    logger.info("[training_agent] walk-forward folds=%d (fold_size=%d) "
                "acc=%.4f sharpe=%.2f dd=%.2f pred_up=%.3f",
                len(folds), WF_FOLD_SIZE, accuracy, sharpe, max_dd, pred_up_rate)

    # Production candidate = fresh fit on the final-fold train slice so the
    # shipped model was never trained on any of the walk-forward validation data.
    ensemble = _fit_ensemble(final_train[features], final_train[TARGET_COL],
                             features, xgb_hp, lstm_hp)

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
