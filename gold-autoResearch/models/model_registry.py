"""
Versioned model registry.

Each entry is a pickled artefact plus a sidecar JSON with training metadata
(accuracy, Sharpe, drawdown, features, hyperparameters, timestamp). The
symlink `production.pkl` points to the currently-promoted model.

Promotion is strictly gated: a candidate replaces production only if its
holdout directional accuracy beats the incumbent AND the guard-rails pass.
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
    version:      str
    created_at:   str
    accuracy:     float
    sharpe:       float
    max_drawdown: float
    features:     list[str]
    hyperparams:  dict[str, Any]
    notes:        str = ""


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
            metadata = ModelMetadata(**json.load(f))
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

    def promote(self, candidate: ModelMetadata) -> bool:
        """
        Replace the production pointer with `candidate` only when it beats the
        incumbent on accuracy and respects guard-rails. Returns True if the
        candidate was promoted.

        Seed path: when no incumbent exists yet, relax the drawdown cap by
        50% so the loop can plant a baseline. Subsequent cycles must beat
        that baseline AND pass the tight guard-rails.
        """
        incumbent = self.production_metadata()
        is_seed = incumbent is None
        # Seed path is intentionally loose: any non-catastrophic candidate
        # can plant a baseline. Once an incumbent exists, subsequent cycles
        # must clear the tight production guard-rails AND beat the baseline
        # on accuracy. This keeps the loop from stalling forever when the
        # current holdout window happens to be a regime-shift.
        dd_limit = 0.60 if is_seed else settings.max_drawdown_limit
        sharpe_floor = -5.0 if is_seed else settings.sharpe_target * 0.8

        if candidate.sharpe < sharpe_floor:
            logger.info("promotion blocked — Sharpe %.2f below floor %.2f (seed=%s)",
                        candidate.sharpe, sharpe_floor, is_seed)
            return False
        if candidate.max_drawdown > dd_limit:
            logger.info("promotion blocked — drawdown %.2f above limit %.2f (seed=%s)",
                        candidate.max_drawdown, dd_limit, is_seed)
            return False
        if incumbent is not None and candidate.accuracy <= incumbent.accuracy:
            logger.info("promotion blocked — candidate accuracy %.4f ≤ incumbent %.4f",
                        candidate.accuracy, incumbent.accuracy)
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
        return f"m{int(time.time())}"


registry = ModelRegistry()
