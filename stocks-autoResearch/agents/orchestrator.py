"""
Orchestrator: reads program.md, drives the stocks autoresearch loop.

Thin by design — all thresholds live in program.md (parsed here) + settings.
Agents interact asynchronously so future I/O-heavy upgrades don't block.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from agents import (
    data_agent,
    eval_agent,
    meta_optimizer,
    report_agent,
    residual_agent,
    training_agent,
)
from config.settings import settings
from models.model_registry import registry

logger = logging.getLogger(__name__)


@dataclass
class ProgramConfig:
    objective:       str
    accuracy_target: float
    retrain_floor:   float
    sharpe_target:   float
    loop_hours:      int
    meta_every:      int


# ── program.md parsing ──────────────────────────────────────────────────────

_NUM_RE = re.compile(r"([-+]?\d*\.?\d+)")


def _first_number_after(text: str, anchor: str, default: float) -> float:
    idx = text.find(anchor)
    if idx < 0:
        return default
    match = _NUM_RE.search(text, idx)
    return float(match.group(1)) if match else default


def parse_program(path: Path = settings.program_path) -> ProgramConfig:
    text = path.read_text() if path.exists() else ""

    obj_match = re.search(r"##\s*1\.\s*Objective\s*\n+(.+?)(?:\n##|\Z)",
                          text, re.DOTALL)
    objective = (obj_match.group(1).strip() if obj_match
                 else "Predict next-day direction for top 20 S&P 500 names.")

    return ProgramConfig(
        objective=objective,
        accuracy_target=_first_number_after(
            text, "directional accuracy", settings.primary_accuracy_target
        ),
        retrain_floor=_first_number_after(
            text, "below **0.", settings.retrain_accuracy_floor
        ) if "below **0." in text else settings.retrain_accuracy_floor,
        sharpe_target=_first_number_after(
            text, "Sharpe", settings.sharpe_target
        ),
        loop_hours=int(_first_number_after(
            text, "every 24", settings.loop_interval_hours
        )),
        meta_every=int(_first_number_after(
            text, "every 10", settings.meta_optimize_every
        )),
    )


# ── State persistence ──────────────────────────────────────────────────────

_state_path = settings.root_dir / ".orchestrator_state.json"


def _load_state() -> dict:
    if not _state_path.exists():
        return {"cycle": 0, "stale_streak": 0, "last_accuracy": None,
                "flag_for_human_review": False}
    import json
    return json.loads(_state_path.read_text())


def _save_state(state: dict) -> None:
    import json
    _state_path.write_text(json.dumps(state, indent=2))


# ── One cycle ──────────────────────────────────────────────────────────────

async def run_cycle(cycle: int, cfg: ProgramConfig) -> dict:
    logger.info("── cycle %d starting ──", cycle)

    data_out = await data_agent.run()
    eval_before = await eval_agent.run()

    training_out = None
    promoted = False

    need_train = (
        eval_before.get("accuracy") is None
        or eval_before["accuracy"] < cfg.retrain_floor
    )
    if need_train:
        ensemble, meta = await training_agent.run(experiment_note=f"cycle-{cycle}")
        training_out = {
            "version":      meta.version,
            "accuracy":     meta.accuracy,
            "sharpe":       meta.sharpe,
            "max_drawdown": meta.max_drawdown,
            "pred_up_rate": meta.pred_up_rate,
            "hyperparams":  meta.hyperparams,
            "notes":        meta.notes,
        }
        promoted = registry.promote(
            meta,
            incumbent_current_accuracy=eval_before.get("accuracy"),
        )

    eval_after = await eval_agent.run() if promoted else eval_before

    # Per-ticker residuals use whatever's now in production (newly promoted
    # candidate or incumbent). Skipped when no production pooled exists.
    try:
        residuals_out = await residual_agent.run()
    except Exception as exc:
        logger.exception("residual_agent failed: %s", exc)
        residuals_out = {}

    return {
        "data":         data_out,
        "eval_before":  eval_before,
        "eval_after":   eval_after,
        "training":     training_out,
        "promoted":     promoted,
        "residuals":    residuals_out,
        "flags":        [],
    }


# ── Main loop ──────────────────────────────────────────────────────────────

async def main_loop() -> None:
    state = _load_state()
    while True:
        cfg = parse_program()
        cycle = state["cycle"] + 1

        try:
            payload = await run_cycle(cycle, cfg)
        except Exception as exc:
            logger.exception("cycle %d failed: %s — will retry next tick", cycle, exc)
            await asyncio.sleep(max(60, cfg.loop_hours * 3600))
            continue

        acc_now = (payload["eval_after"] or {}).get("accuracy")
        last_acc = state.get("last_accuracy")
        if acc_now is not None and last_acc is not None and acc_now <= last_acc:
            state["stale_streak"] += 1
        elif acc_now is not None:
            state["stale_streak"] = 0
        state["last_accuracy"] = acc_now

        if state["stale_streak"] >= settings.stale_run_streak_limit:
            state["flag_for_human_review"] = True
            payload["flags"].append("human_review")
            logger.warning("%d consecutive runs without improvement — "
                           "flagged for human review", state["stale_streak"])

        await report_agent.run(cycle, payload)

        if cycle % cfg.meta_every == 0:
            try:
                suggestion = await meta_optimizer.run()
                logger.info("meta_optimizer suggestion: %s", suggestion.get("finding", ""))
            except Exception as exc:
                logger.exception("meta_optimizer failed: %s", exc)

        state["cycle"] = cycle
        _save_state(state)

        sleep_seconds = max(60, cfg.loop_hours * 3600)
        logger.info("cycle %d done — sleeping %d s", cycle, sleep_seconds)
        await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(main_loop())
