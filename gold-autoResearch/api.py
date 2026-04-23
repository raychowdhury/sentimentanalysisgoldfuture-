"""
FastAPI service exposing the autoresearch status endpoint.

Designed to run alongside the orchestrator in the same docker-compose stack.
It reads run_log.md + program.md from the shared volume; no database access
is required for this read-only surface.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Gold AutoResearch API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── run_log.md parsing ──────────────────────────────────────────────────────

CYCLE_BLOCK_RE = re.compile(
    r"## Cycle (\d+) — (\S+)\n(.*?)(?=\n## |\Z)", re.DOTALL
)
BULLET_RE = re.compile(r"- \*\*([^*]+)\*\*:\s*(.+)")


def _parse_cycle_block(cycle_n: int, ts: str, body: str) -> dict:
    bullets: dict[str, str] = {}
    for m in BULLET_RE.finditer(body):
        bullets[m.group(1).strip().lower()] = m.group(2).strip()
    return {
        "cycle":      cycle_n,
        "timestamp":  ts,
        "bullets":    bullets,
        "raw":        body.strip(),
    }


def _last_n_cycles(n: int) -> list[dict]:
    path = settings.run_log_path
    if not path.exists():
        return []
    text = path.read_text()
    entries = [
        _parse_cycle_block(int(m.group(1)), m.group(2), m.group(3))
        for m in CYCLE_BLOCK_RE.finditer(text)
    ]
    return entries[-n:][::-1]  # newest first


# ── program.md extraction ───────────────────────────────────────────────────

OBJECTIVE_RE = re.compile(
    r"##\s*1\.\s*Objective\s*\n+(.+?)(?=\n## |\n---|\Z)", re.DOTALL
)
NEXT_EXP_RE = re.compile(
    r"##\s*Next Experiment\s*\n+(.+?)(?=\n## |\n---|\Z)", re.DOTALL
)


def _program_snapshot() -> dict[str, Any]:
    path = settings.program_path
    if not path.exists():
        return {"objective": None, "next_experiment": None}
    text = path.read_text()
    obj = OBJECTIVE_RE.search(text)
    nxt = NEXT_EXP_RE.search(text)
    return {
        "objective":        obj.group(1).strip() if obj else None,
        "next_experiment":  nxt.group(1).strip() if nxt else None,
    }


# ── Live signal ─────────────────────────────────────────────────────────────

def _current_signal() -> dict[str, Any] | None:
    """
    Run the production model on the most-recent feature row and return the
    next-day direction. Prefers `live_row.parquet` (features valid, target
    NaN — i.e. tomorrow's call); falls back to the training cache only when
    the live sidecar is missing. Returns None when no model is promoted or
    prediction fails — the dashboard renders an empty state rather than
    surface a stale or fake signal.
    """
    import pandas as pd

    from data.pipeline import LIVE_ROW_NAME, load_cached_frame
    from models.model_registry import registry

    meta = registry.production_metadata()
    if meta is None:
        return None

    live_path = settings.data_dir / LIVE_ROW_NAME
    if live_path.exists():
        df = pd.read_parquet(live_path)
    else:
        df = load_cached_frame()
    if df is None or df.empty:
        return None

    try:
        model, _ = registry.load(meta.version)
        last = df.iloc[[-1]]
        proba_up = float(model.predict_proba(last)[0])
    except Exception as exc:
        logger.warning("current_signal failed: %s", exc)
        return None
    return {
        "direction":     "LONG" if proba_up >= 0.5 else "SHORT",
        "proba_up":      proba_up,
        "asof":          last.index[-1].isoformat(),
        "model_version": meta.version,
    }


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/research/status")
def research_status() -> dict[str, Any]:
    """
    Last 5 experiment results + the currently-active program objective and
    the AI-suggested next experiment.
    """
    from models.model_registry import registry

    production = registry.production_metadata()
    program = _program_snapshot()
    cycles = _last_n_cycles(5)

    return {
        "program":   program,
        "production_model": (
            {
                "version":      production.version,
                "accuracy":     production.accuracy,
                "sharpe":       production.sharpe,
                "max_drawdown": production.max_drawdown,
                "created_at":   production.created_at,
            }
            if production is not None
            else None
        ),
        "recent_cycles":  cycles,
    }


@app.get("/api/research/signal")
def research_signal() -> dict[str, Any]:
    """Live next-day direction from the production model. Empty payload when
    no model is promoted or the cached feature matrix is missing."""
    sig = _current_signal()
    return sig if sig is not None else {}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
