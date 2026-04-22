"""
Meta-optimizer: after every N cycles, ask Claude what to try next.

Reads run_log.md, sends it to Claude, parses a JSON answer, writes the
suggestion back into program.md under `## Next Experiment`, and persists a
hparam overlay at `config/overrides.json` that training_agent honours on
the next cycle.

Prompt-cached system block keeps subsequent calls cheap.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "You are a quantitative research assistant reviewing a pooled classifier "
    "that predicts next-day direction for the top 20 S&P 500 stocks. Analyze "
    "the run log and identify: what feature or hyperparameter change had the "
    "biggest positive impact on MEAN per-ticker directional accuracy? What "
    "should the next experiment be? Respond in JSON: "
    "{ finding: string, next_experiment: string, config_change: object }"
)


# ── LLM call ────────────────────────────────────────────────────────────────

def _client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "anthropic SDK is required for meta_optimizer — pip install anthropic"
        ) from exc
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _call_claude(run_log_text: str) -> dict:
    client = _client()
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": run_log_text}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "text", None))
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in LLM response: {text[:200]}…")
    return json.loads(match.group(0))


# ── program.md patching ─────────────────────────────────────────────────────

_NEXT_EXP_RE = re.compile(
    r"(## Next Experiment\n)(.*?)(\Z|\n## )", re.DOTALL
)


def _write_suggestion_to_program(suggestion: dict, path: Path = settings.program_path) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    block = (
        f"## Next Experiment\n\n"
        f"_Updated {now} by meta_optimizer._\n\n"
        f"**Finding.** {suggestion.get('finding', '').strip()}\n\n"
        f"**Next experiment.** {suggestion.get('next_experiment', '').strip()}\n\n"
        f"**Proposed config change.**\n\n"
        f"```json\n{json.dumps(suggestion.get('config_change', {}), indent=2)}\n```\n"
    )
    if not path.exists():
        path.write_text(block)
        return

    text = path.read_text()
    if _NEXT_EXP_RE.search(text):
        text = _NEXT_EXP_RE.sub(lambda m: block + (m.group(3) or ""), text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block
    path.write_text(text)


# ── Config overlay ──────────────────────────────────────────────────────────

OVERRIDES_PATH = settings.root_dir / "config" / "overrides.json"


def _extract_grid_overrides(cc: dict) -> dict[str, list]:
    raw: dict = {}
    if isinstance(cc.get("xgb_hparam_grid"), dict):
        raw = cc["xgb_hparam_grid"]
    elif isinstance(cc.get("xgb"), dict):
        raw = {k: ([v] if not isinstance(v, list) else v) for k, v in cc["xgb"].items()}

    valid_keys = {"max_depth", "learning_rate", "n_estimators"}
    return {k: v for k, v in raw.items()
            if k in valid_keys and isinstance(v, list) and v}


def _apply_config_change(suggestion: dict) -> dict:
    cc = suggestion.get("config_change") or {}
    xgb_grid = _extract_grid_overrides(cc)
    features_added = cc.get("features_added") or []

    overlay = {
        "updated_at":       datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "xgb_hparam_grid":  xgb_grid,
        "features_added":   features_added,
        "features_applied": False,
    }
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(json.dumps(overlay, indent=2))
    logger.info("[meta_optimizer] overlay written → %s (xgb keys=%s)",
                OVERRIDES_PATH.name, list(xgb_grid.keys()))
    return overlay


def _append_suggestion_to_log(suggestion: dict) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"## Meta-Optimizer — {now}",
        f"- **Finding**: {suggestion.get('finding', '').strip()}",
        f"- **Next experiment**: {suggestion.get('next_experiment', '').strip()}",
        f"- **Config change**: `{json.dumps(suggestion.get('config_change', {}))}`",
        "",
    ]
    with open(settings.run_log_path, "a") as f:
        f.write("\n".join(lines) + "\n")


# ── Public entry ────────────────────────────────────────────────────────────

async def run() -> dict:
    log_path = settings.run_log_path
    if not log_path.exists():
        logger.info("[meta_optimizer] run_log.md missing — skipping")
        return {}
    run_log_text = log_path.read_text()
    if len(run_log_text.strip()) < 50:
        logger.info("[meta_optimizer] run_log.md too small — skipping")
        return {}

    suggestion = _call_claude(run_log_text)
    _write_suggestion_to_program(suggestion)
    _append_suggestion_to_log(suggestion)
    _apply_config_change(suggestion)
    logger.info("[meta_optimizer] suggestion recorded")
    return suggestion
