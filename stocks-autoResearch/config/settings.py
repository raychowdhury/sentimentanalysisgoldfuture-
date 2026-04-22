"""
Central configuration for stocks-autoResearch.

All tunables live here so agents stay pure. Values are read from environment
variables (docker-compose injects them from .env). Defaults are safe for
local development.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # ── Paths ────────────────────────────────────────────────────────────────
    root_dir:     Path = ROOT_DIR
    program_path: Path = ROOT_DIR / "program.md"
    run_log_path: Path = ROOT_DIR / "run_log.md"
    data_dir:     Path = ROOT_DIR / "data" / "cache"
    models_dir:   Path = ROOT_DIR / "models" / "registry"

    # ── External APIs ────────────────────────────────────────────────────────
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    fred_api_key:      str = os.getenv("FRED_API_KEY", "")
    database_url:      str = os.getenv(
        "DATABASE_URL",
        "postgresql://autoresearch:autoresearch@postgres:5432/autoresearch",
    )

    # ── Loop ─────────────────────────────────────────────────────────────────
    loop_interval_hours:  int = int(os.getenv("LOOP_INTERVAL_HOURS", "24"))
    meta_optimize_every:  int = int(os.getenv("META_OPTIMIZE_EVERY", "10"))

    # ── Metric thresholds (defaults; program.md may override at runtime) ─────
    # Stocks are noisier than gold — lower thresholds than the gold loop.
    primary_accuracy_target: float = 0.55
    retrain_accuracy_floor:  float = 0.53
    sharpe_target:           float = 1.00
    max_drawdown_limit:      float = 0.20

    # ── Stopping ─────────────────────────────────────────────────────────────
    stale_run_streak_limit: int = 3

    # ── Model / training ─────────────────────────────────────────────────────
    # Shorter holdout than gold: per-ticker rows are the same length but
    # we have 20× the row count, so 60 days ×20 = 1,200 rows is plenty.
    holdout_days:     int = 60
    default_lookback: int = 20
    xgb_hparam_grid: dict = field(default_factory=lambda: {
        "max_depth":     [4, 6, 8],
        "learning_rate": [0.05, 0.1],
        "n_estimators":  [300, 500],
    })

    # ── Claude / LLM ─────────────────────────────────────────────────────────
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
