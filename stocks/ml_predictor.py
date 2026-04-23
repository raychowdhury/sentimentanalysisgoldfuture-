"""
Inference bridge — consume the pooled classifier + per-ticker residuals
trained by stocks-autoResearch and expose a single predict(ticker) call.

Reads the cached feature matrix that the autoresearch loop maintains
daily; falls back silently when artifacts are missing so the Flask scan
keeps running without an ML signal.

Implementation note: stocks-autoResearch has its own `config` package
that collides with this project's top-level `config.py`. We swap
sys.modules["config"] during import so autoresearch modules bind their
own settings, then restore main's config afterwards. The autoresearch
modules cache their settings reference at import time, so the swap is
only needed once per process.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np

_AUTORES_ROOT = Path(__file__).resolve().parent.parent / "stocks-autoResearch"

logger = logging.getLogger(__name__)

_STATE: dict[str, Any] = {"loaded": False}
_LOGIT_CLIP = 1e-6


def _load() -> dict:
    if _STATE.get("loaded"):
        return _STATE
    _STATE["loaded"] = True  # mark early so failures don't re-trigger

    if not _AUTORES_ROOT.exists():
        logger.info("[ml_predictor] autoresearch dir missing — disabled")
        return _STATE

    root = str(_AUTORES_ROOT)
    saved_config          = sys.modules.pop("config", None)
    saved_config_settings = sys.modules.pop("config.settings", None)
    inserted = root not in sys.path
    if inserted:
        sys.path.insert(0, root)

    try:
        from data.pipeline import load_cached_frame
        from models.model_registry import registry
        import agents.training_agent  # noqa: F401  — unpickling dep
        import agents.residual_agent  # noqa: F401  — unpickling dep

        frame = load_cached_frame()
        meta = registry.production_metadata()
        pooled = None
        if meta is not None:
            pooled, _ = registry.load(meta.version)
        _STATE.update({
            "frame":    frame,
            "pooled":   pooled,
            "registry": registry,
        })
        logger.info(
            "[ml_predictor] loaded (frame=%s, pooled=%s)",
            "ok" if frame is not None else "missing",
            meta.version if meta is not None else "missing",
        )
    except Exception as exc:
        logger.warning("[ml_predictor] load failed: %s — disabled", exc)
    finally:
        if inserted:
            try:
                sys.path.remove(root)
            except ValueError:
                pass
        if saved_config is not None:
            sys.modules["config"] = saved_config
        if saved_config_settings is not None:
            sys.modules["config.settings"] = saved_config_settings
        else:
            sys.modules.pop("config.settings", None)

    return _STATE


def _logit(p: float) -> float:
    p = float(np.clip(p, _LOGIT_CLIP, 1.0 - _LOGIT_CLIP))
    return float(np.log(p / (1.0 - p)))


def predict(ticker: str) -> dict | None:
    """
    Return {prob_up, pooled_prob, source, asof_date} for the given ticker,
    or None when the ML stack isn't available (no models, no cache, or
    ticker missing from the feature matrix).
    """
    state = _load()
    pooled = state.get("pooled")
    frame  = state.get("frame")
    if pooled is None or frame is None:
        return None

    ticker = ticker.upper()
    t_rows = frame[frame["ticker"].astype(str) == ticker]
    if t_rows.empty:
        return None
    row = t_rows.iloc[[-1]]

    try:
        pooled_prob = float(pooled.predict_proba(row[pooled.features])[0])
    except Exception as exc:
        logger.warning("[ml_predictor] %s pooled inference failed: %s", ticker, exc)
        return None

    prob = pooled_prob
    source = "pooled"
    registry = state.get("registry")
    if registry is not None:
        residual = registry.load_residual(ticker)
        if residual is not None:
            try:
                inp = row.copy()
                inp["pooled_logit"] = _logit(pooled_prob)
                prob = float(residual.predict_proba(inp)[0])
                source = "hybrid"
            except Exception as exc:
                logger.warning("[ml_predictor] %s residual failed: %s", ticker, exc)

    asof = str(row["date"].iloc[0])[:10]
    return {
        "prob_up":     round(prob, 4),
        "pooled_prob": round(pooled_prob, 4),
        "source":      source,
        "asof_date":   asof,
    }
