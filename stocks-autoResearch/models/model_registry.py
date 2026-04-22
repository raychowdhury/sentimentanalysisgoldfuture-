"""
Versioned model registry (pooled stock classifier).

Mirrors gold-autoResearch registry — pickled artefact + sidecar JSON metadata
+ `production.pkl` symlink to the promoted model. Promotion is gated on
mean per-ticker accuracy (vs incumbent) plus Sharpe / drawdown guard-rails.
"""
from __future__ import annotations

import json
import logging
import pickle
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

PRODUCTION_LINK = "production.pkl"


@dataclass
class ModelMetadata:
    version:        str
    created_at:     str
    accuracy:       float       # mean per-ticker directional accuracy
    sharpe:         float       # pooled equal-weight long/short Sharpe
    max_drawdown:   float       # pooled equity curve drawdown
    features:       list[str]
    hyperparams:    dict[str, Any]
    notes:          str = ""
    pred_up_rate:   float = 0.5
    per_ticker_acc: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.per_ticker_acc is None:
            object.__setattr__(self, "per_ticker_acc", {})


class ModelRegistry:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or settings.models_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ── Save / load ──────────────────────────────────────────────────────────

    def save(self, model: Any, metadata: ModelMetadata) -> Path:
        pkl_path  = self.base_dir / f"{metadata.version}.pkl"
        meta_path = self.base_dir / f"{metadata.version}.json"
        with open(pkl_path, "wb") as f:
            pickle.dump(model, f)
        with open(meta_path, "w") as f:
            json.dump(asdict(metadata), f, indent=2)
        logger.info("model saved → %s", pkl_path.name)
        return pkl_path

    def load(self, version: str) -> tuple[Any, ModelMetadata]:
        with open(self.base_dir / f"{version}.pkl", "rb") as f:
            model = pickle.load(f)
        with open(self.base_dir / f"{version}.json") as f:
            raw = json.load(f)
        known = {f.name for f in ModelMetadata.__dataclass_fields__.values()}
        metadata = ModelMetadata(**{k: v for k, v in raw.items() if k in known})
        return model, metadata

    # ── Promotion ────────────────────────────────────────────────────────────

    def production_metadata(self) -> ModelMetadata | None:
        link = self.base_dir / PRODUCTION_LINK
        if not link.exists():
            return None
        version = link.resolve().stem
        try:
            _, meta = self.load(version)
            return meta
        except FileNotFoundError:
            return None

    # Pooled classifier whose predictions collapse to nearly constant across
    # all tickers is rejected — it is learning the base rate, not signal.
    PRED_UP_RATE_MIN = 0.25
    PRED_UP_RATE_MAX = 0.75

    def promote(
        self,
        candidate: ModelMetadata,
        incumbent_current_accuracy: float | None = None,
    ) -> bool:
        incumbent = self.production_metadata()
        is_seed = incumbent is None
        dd_limit = 0.60 if is_seed else settings.max_drawdown_limit
        sharpe_floor = -5.0 if is_seed else settings.sharpe_target * 0.8

        if not (self.PRED_UP_RATE_MIN <= candidate.pred_up_rate <= self.PRED_UP_RATE_MAX):
            logger.info("promotion blocked — pred_up_rate %.2f outside [%.2f, %.2f]",
                        candidate.pred_up_rate,
                        self.PRED_UP_RATE_MIN, self.PRED_UP_RATE_MAX)
            return False
        if candidate.sharpe < sharpe_floor:
            logger.info("promotion blocked — Sharpe %.2f below floor %.2f (seed=%s)",
                        candidate.sharpe, sharpe_floor, is_seed)
            return False
        if candidate.max_drawdown > dd_limit:
            logger.info("promotion blocked — drawdown %.2f above limit %.2f (seed=%s)",
                        candidate.max_drawdown, dd_limit, is_seed)
            return False
        if incumbent is not None:
            baseline = (incumbent_current_accuracy
                        if incumbent_current_accuracy is not None
                        else incumbent.accuracy)
            if candidate.accuracy <= baseline:
                logger.info("promotion blocked — candidate mean_acc %.4f ≤ baseline %.4f",
                            candidate.accuracy, baseline)
                return False

        link = self.base_dir / PRODUCTION_LINK
        target = self.base_dir / f"{candidate.version}.pkl"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target.name)
        logger.info("model promoted — %s is now production", candidate.version)
        return True

    @staticmethod
    def new_version() -> str:
        return f"s{int(time.time())}"


registry = ModelRegistry()
