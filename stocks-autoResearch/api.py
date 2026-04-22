"""
FastAPI service exposing the stocks autoresearch status endpoint.

Read-only over run_log.md + program.md + the model registry. Runs alongside
the orchestrator in the same docker-compose stack.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings

app = FastAPI(title="Stocks AutoResearch API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


CYCLE_BLOCK_RE = re.compile(
    r"## Cycle (\d+) — (\S+)\n(.*?)(?=\n## |\Z)", re.DOTALL
)
BULLET_RE = re.compile(r"- \*\*([^*]+)\*\*:\s*(.+)")


def _parse_cycle_block(cycle_n: int, ts: str, body: str) -> dict:
    bullets: dict[str, str] = {}
    for m in BULLET_RE.finditer(body):
        bullets[m.group(1).strip().lower()] = m.group(2).strip()
    return {
        "cycle":     cycle_n,
        "timestamp": ts,
        "bullets":   bullets,
        "raw":       body.strip(),
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
    return entries[-n:][::-1]


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
        "objective":       obj.group(1).strip() if obj else None,
        "next_experiment": nxt.group(1).strip() if nxt else None,
    }


@app.get("/api/stocks-research/status")
def research_status() -> dict[str, Any]:
    from models.model_registry import registry

    production = registry.production_metadata()
    program = _program_snapshot()
    cycles = _last_n_cycles(5)

    return {
        "program":   program,
        "production_model": (
            {
                "version":        production.version,
                "accuracy":       production.accuracy,
                "sharpe":         production.sharpe,
                "max_drawdown":   production.max_drawdown,
                "created_at":     production.created_at,
                "per_ticker_acc": production.per_ticker_acc,
            }
            if production is not None
            else None
        ),
        "recent_cycles": cycles,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
