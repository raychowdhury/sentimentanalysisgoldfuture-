"""
FastAPI service exposing the autoresearch status endpoint.

Designed to run alongside the orchestrator in the same docker-compose stack.
It reads run_log.md + program.md from the shared volume; no database access
is required for this read-only surface.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings

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
        "recent_cycles": cycles,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
