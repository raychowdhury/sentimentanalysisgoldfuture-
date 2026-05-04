"""
Report agent: append a structured, human-readable entry to run_log.md.

Each entry is a fenced block beginning with `## Cycle N — <ISO timestamp>`
so the API layer and meta_optimizer can parse cycles back out cheaply.
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


def _fmt_per_ticker(per_ticker: dict | None) -> str:
    if not per_ticker:
        return ""
    items = sorted(per_ticker.items(), key=lambda kv: kv[1], reverse=True)
    head = ", ".join(f"{t}:{a:.2f}" for t, a in items[:5])
    tail = ", ".join(f"{t}:{a:.2f}" for t, a in items[-3:])
    return f"top=[{head}], bottom=[{tail}]"


async def run(cycle: int, payload: dict) -> None:
    body = [
        _header(cycle),
        f"- **Dataset**: rows={payload['data']['rows']}, "
        f"cols={payload['data']['cols']}, "
        f"last={payload['data']['last_date']}, "
        f"tickers={payload['data']['tickers']}",
    ]

    ev = payload.get("eval_before") or {}
    if ev:
        body.append(
            f"- **Eval (production)**: mean_acc={ev.get('accuracy')}, "
            f"sharpe={ev.get('sharpe')}, dd={ev.get('max_drawdown')}, "
            f"version={ev.get('version')}"
        )
        pta = _fmt_per_ticker(ev.get("per_ticker_acc"))
        if pta:
            body.append(f"- **Per-ticker**: {pta}")

    tr = payload.get("training")
    if tr:
        body.append(
            f"- **Candidate**: version={tr.get('version')}, "
            f"mean_acc={tr.get('accuracy')}, sharpe={tr.get('sharpe')}, "
            f"dd={tr.get('max_drawdown')}"
        )
        body.append(f"- **Hyperparams**: `{json.dumps(tr.get('hyperparams', {}))}`")
        if tr.get("notes"):
            body.append(f"- **Experiment note**: {tr['notes']}")

    body.append(f"- **Promoted**: {'yes' if payload.get('promoted') else 'no'}")

    residuals = payload.get("residuals") or {}
    if residuals:
        promoted_now = sorted(t for t, r in residuals.items() if r.get("promoted"))
        trained_count = sum(1 for r in residuals.values() if r.get("residual_acc") is not None)
        body.append(
            f"- **Residuals**: trained={trained_count}/{len(residuals)}, "
            f"promoted_this_cycle={len(promoted_now)}"
        )
        body.append("  | ticker | residual_acc | pooled_acc | promoted |")
        body.append("  |--------|--------------|------------|----------|")
        for ticker in sorted(residuals):
            r = residuals[ticker]
            r_acc = r.get("residual_acc")
            p_acc = r.get("pooled_acc")
            r_fmt = f"{r_acc:.4f}" if r_acc is not None else "—"
            p_fmt = f"{p_acc:.4f}" if p_acc is not None else "—"
            body.append(
                f"  | {ticker} | {r_fmt} | {p_fmt} | {'yes' if r.get('promoted') else 'no'} |"
            )
    if ev.get("hybrid_accuracy") is not None:
        body.append(
            f"- **Hybrid eval**: hybrid_acc={ev['hybrid_accuracy']}, "
            f"pooled_acc={ev['accuracy']}, "
            f"residuals_active={len(ev.get('residuals_applied') or [])}"
        )

    flags = payload.get("flags") or []
    if flags:
        body.append(f"- **Flags**: {', '.join(flags)}")

    body.append("")
    text = "\n".join(body) + "\n"
    settings.run_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.run_log_path, "a") as f:
        f.write(text)
    logger.info("[report_agent] wrote cycle %d to %s", cycle, settings.run_log_path)
