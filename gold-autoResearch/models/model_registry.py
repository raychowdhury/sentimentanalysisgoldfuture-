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
    # Fraction of predictions that were "up". Extreme values (all-up / all-down)
    # reveal a degenerate classifier even when accuracy ≈ base rate.
    pred_up_rate: float = 0.5


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
        # Tolerate older sidecars that predate newer fields (e.g. pred_up_rate).
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

    # Any candidate whose predictions are nearly constant (all-up or all-down)
    # is rejected regardless of accuracy — it's learning the base rate, not a
    # real signal. Applies in seed and incumbent paths.
    PRED_UP_RATE_MIN = 0.25
    PRED_UP_RATE_MAX = 0.75

    # Seed gate: a cold-start model becomes "production" for the dashboard, so
    # the floors matter. Prior code accepted Sharpe down to -5 and DD up to 0.60
    # to "plant a baseline" — in practice it planted junk and blocked better
    # candidates from replacing it. Seed now requires a real signal.
    SEED_SHARPE_FLOOR  = 0.0
    SEED_DD_LIMIT      = 0.30
    SEED_ACCURACY_MIN  = 0.52

    def promote(
        self,
        candidate: ModelMetadata,
        incumbent_current_accuracy: float | None = None,
    ) -> bool:
        """
        Replace the production pointer with `candidate` only when it clears
        every gate: pred-up-rate, Sharpe, drawdown, and accuracy vs incumbent
        (or a seed-accuracy floor when no incumbent exists).

        `incumbent_current_accuracy` is the incumbent's accuracy re-measured on
        the current holdout (via eval_agent). When provided, it is used for the
        comparison instead of the incumbent's stale stored accuracy — otherwise
        candidates are judged against a holdout the incumbent never saw.
        """
        incumbent = self.production_metadata()
        is_seed = incumbent is None
        dd_limit     = self.SEED_DD_LIMIT     if is_seed else settings.max_drawdown_limit
        sharpe_floor = self.SEED_SHARPE_FLOOR if is_seed else settings.sharpe_target * 0.8

        if not (self.PRED_UP_RATE_MIN <= candidate.pred_up_rate <= self.PRED_UP_RATE_MAX):
            logger.info("promotion blocked — pred_up_rate %.2f outside [%.2f, %.2f] "
                        "(degenerate classifier)",
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
        if is_seed:
            if candidate.accuracy < self.SEED_ACCURACY_MIN:
                logger.info("promotion blocked — seed accuracy %.4f below floor %.2f",
                            candidate.accuracy, self.SEED_ACCURACY_MIN)
                return False
        else:
            baseline = (incumbent_current_accuracy
                        if incumbent_current_accuracy is not None
                        else incumbent.accuracy)
            if candidate.accuracy <= baseline:
                logger.info("promotion blocked — candidate accuracy %.4f ≤ baseline %.4f",
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
        return f"m{int(time.time())}"

    # ── Demotion ─────────────────────────────────────────────────────────────

    def demote_if_unfit(
        self,
        current_accuracy: float | None,
        current_sharpe:   float | None,
        current_drawdown: float | None,
    ) -> bool:
        """
        Unlink `production.pkl` when the live model's re-measured metrics fall
        below hard floors. Prevents a bad seed from locking production forever:
        once demoted, the next cycle's seed path can install a fresh candidate
        under the tightened seed gate. Returns True if demoted.
        """
        link = self.base_dir / PRODUCTION_LINK
        if not link.exists():
            return False
        fails: list[str] = []
        if current_accuracy is not None and current_accuracy < self.SEED_ACCURACY_MIN:
            fails.append(f"acc={current_accuracy:.4f}<{self.SEED_ACCURACY_MIN}")
        if current_sharpe is not None and current_sharpe < self.SEED_SHARPE_FLOOR:
            fails.append(f"sharpe={current_sharpe:.2f}<{self.SEED_SHARPE_FLOOR}")
        if current_drawdown is not None and current_drawdown > self.SEED_DD_LIMIT:
            fails.append(f"dd={current_drawdown:.2f}>{self.SEED_DD_LIMIT}")
        if not fails:
            return False
        link.unlink()
        logger.warning("production demoted — unfit on re-eval: %s", ", ".join(fails))
        return True


registry = ModelRegistry()
