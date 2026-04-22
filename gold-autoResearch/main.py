"""
Entry point.

Two modes, selected by the first CLI arg:

    python main.py loop    → continuous asyncio loop (default)
    python main.py once    → single cycle then exit (used by cron)

Cron runs `once` daily at 00:05 UTC per the Dockerfile.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from agents import orchestrator


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )


async def _run_once() -> None:
    cfg = orchestrator.parse_program()
    state = orchestrator._load_state()
    cycle = state["cycle"] + 1
    payload = await orchestrator.run_cycle(cycle, cfg)
    from agents import report_agent
    await report_agent.run(cycle, payload)
    state["cycle"] = cycle
    state["last_accuracy"] = (payload["eval_after"] or {}).get("accuracy")
    orchestrator._save_state(state)


def main() -> None:
    _configure_logging()
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if mode == "once":
        asyncio.run(_run_once())
    elif mode == "loop":
        asyncio.run(orchestrator.main_loop())
    else:
        raise SystemExit(f"unknown mode: {mode} (expected 'once' or 'loop')")


if __name__ == "__main__":
    main()
