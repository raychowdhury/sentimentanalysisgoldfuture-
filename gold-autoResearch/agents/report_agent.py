"""
Report agent: append a structured, human-readable entry to run_log.md.

Each entry is a fenced markdown block that the FastAPI status endpoint and
meta_optimizer can parse back out with minimal state (we keep parsing simple
by starting every block with `## Cycle N — <ISO timestamp>`).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from config.settings import settings

logger = logging.getLogger(__name__)


def _header(cycle: int) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"## Cycle {cycle} — {now}\n"


async def run(cycle: int, payload: dict) -> None:
    """
    `payload` keys expected: data, eval_before, eval_after, training,
    promoted (bool), flags (list[str]).
    """
    body = [
        _header(cycle),
        f"- **Dataset**: rows={payload['data']['rows']}, "
        f"cols={payload['data']['cols']}, "
        f"last={payload['data']['last_date']}",
    ]

    ev = payload.get("eval_before") or {}
    if ev:
        body.append(f"- **Eval (production)**: acc={ev.get('accuracy')}, "
                    f"sharpe={ev.get('sharpe')}, dd={ev.get('max_drawdown')}, "
                    f"version={ev.get('version')}")

    tr = payload.get("training")
    if tr:
        body.append(
            f"- **Candidate**: version={tr.get('version')}, "
            f"acc={tr.get('accuracy')}, sharpe={tr.get('sharpe')}, "
            f"dd={tr.get('max_drawdown')}"
        )
        body.append(f"- **Hyperparams**: `{json.dumps(tr.get('hyperparams', {}))}`")
        if tr.get("notes"):
            body.append(f"- **Experiment note**: {tr['notes']}")

    body.append(f"- **Promoted**: {'yes' if payload.get('promoted') else 'no'}")

    flags = payload.get("flags") or []
    if flags:
        body.append(f"- **Flags**: {', '.join(flags)}")

    body.append("")  # trailing newline

    text = "\n".join(body) + "\n"
    settings.run_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.run_log_path, "a") as f:
        f.write(text)
    logger.info("[report_agent] wrote cycle %d to %s", cycle, settings.run_log_path)
