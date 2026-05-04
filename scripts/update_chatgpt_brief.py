"""Scaffold outputs/order_flow/chatgpt_brief.md for OpenAI Reviewer Bridge.

Read-only on engine state. Writes only chatgpt_brief.md (atomic tmp+rename).
No network. No engine mutation. No subprocess.

Brief structure: 10 fixed sections so reviewer responses stay specific to
this project. Sections render placeholders when source data is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
BRIEF_PATH = PROJECT / "outputs/order_flow/chatgpt_brief.md"

PROJECT_STATE = PROJECT / "PROJECT_STATE.md"
HEALTH_STATE = PROJECT / "outputs/order_flow/.health_state.json"
CHECKPOINT_STATE = PROJECT / "outputs/order_flow/.live_checkpoint_state.json"
OUTCOMES_SUMMARY = PROJECT / "outputs/order_flow/realflow_outcomes_summary_ESM6_15m.json"
R7_SHADOW_SUMMARY = PROJECT / "outputs/order_flow/realflow_r7_shadow_summary_ESM6_15m.json"
DIAGNOSTIC = PROJECT / "outputs/order_flow/realflow_diagnostic_ESM6_15m.json"

PROJECT_STATE_HEAD_LINES = 80


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def safe_load_json(path: Path) -> tuple[Any, str | None]:
    if not path.exists():
        return None, f"missing: {path.name}"
    try:
        return json.loads(path.read_text()), None
    except Exception as e:
        return None, f"parse failed: {e}"


def safe_read_text(path: Path, head_lines: int | None = None) -> str:
    if not path.exists():
        return f"_(missing: {path.name})_"
    try:
        text = path.read_text()
    except Exception as e:
        return f"_(read failed: {e})_"
    if head_lines is not None:
        text = "\n".join(text.splitlines()[:head_lines])
    return text


# ── section 2: market/session state ─────────────────────────────────────────

def cme_es_session_state(now: datetime) -> dict:
    """CME ES electronic session: Sun 22:00 UTC → Fri 21:00 UTC.
    Daily maintenance break 21:00-22:00 UTC Mon-Thu.
    Returns dict {open: bool, label: str, next_event_utc: str}."""
    wd = now.weekday()  # Mon=0 .. Sun=6
    h = now.hour
    if wd == 5:  # Sat
        return {"open": False, "label": "weekend (Sat)", "next_event_utc": "Sun 22:00Z reopen"}
    if wd == 6:  # Sun
        if h < 22:
            return {"open": False, "label": "weekend (Sun, pre-reopen)",
                    "next_event_utc": "Sun 22:00Z reopen"}
        return {"open": True, "label": "Sun ETH (reopened)",
                "next_event_utc": "Mon 21:00Z maintenance break"}
    if wd in (0, 1, 2, 3):  # Mon-Thu
        if h == 21:
            return {"open": False, "label": "daily maintenance (21:00-22:00Z)",
                    "next_event_utc": "22:00Z reopen"}
        return {"open": True, "label": f"weekday ETH ({['Mon','Tue','Wed','Thu'][wd]})",
                "next_event_utc": f"{['Mon','Tue','Wed','Thu'][wd]} 21:00Z maintenance break"}
    if wd == 4:  # Fri
        if h < 21:
            return {"open": True, "label": "weekday ETH (Fri)",
                    "next_event_utc": "Fri 21:00Z weekly close"}
        return {"open": False, "label": "weekend (Fri post-close)",
                "next_event_utc": "Sun 22:00Z reopen"}
    return {"open": False, "label": "unknown", "next_event_utc": "unknown"}


def render_market_section(now: datetime) -> str:
    s = cme_es_session_state(now)
    lines = [
        f"- now UTC: `{now.isoformat()}`",
        f"- CME ES session open: **{s['open']}**",
        f"- session label: {s['label']}",
        f"- next session event: {s['next_event_utc']}",
    ]
    diag, err = safe_load_json(DIAGNOSTIC)
    if diag:
        lb = diag.get("latest_live_bar", {}).get("primary", {}) or {}
        fr = diag.get("freshness", {}) or {}
        lines.append(f"- latest live bar UTC: `{lb.get('ts_utc', 'n/a')}`")
        lines.append(f"- live lag: {lb.get('lag_minutes', 'n/a')} min")
        lines.append(f"- freshness label: {fr.get('label', 'n/a')} ({fr.get('explanation', '')})")
        joined = diag.get("joined", {}) or {}
        lines.append(
            f"- joined window: {joined.get('start','?')} → {joined.get('end','?')} "
            f"(n={joined.get('n_bars','?')})"
        )
    elif err:
        lines.append(f"- diagnostic: _{err}_")
    return "\n".join(lines) + "\n"


# ── section 3: health state ─────────────────────────────────────────────────

def render_health_section() -> str:
    data, err = safe_load_json(HEALTH_STATE)
    if err:
        return f"_{err}_\n"
    rows = ["| probe | status |", "|---|---|"]
    for k, v in data.items():
        rows.append(f"| {k} | {v} |")
    return "\n".join(rows) + "\n"


# ── section 4: checkpoint table ─────────────────────────────────────────────

def render_checkpoints_section() -> str:
    data, err = safe_load_json(CHECKPOINT_STATE)
    if err:
        return f"_{err}_\n"
    summary = data.get("summary", {}) or {}
    cells = data.get("cells", {}) or {}
    if not summary:
        return f"_(no summary in checkpoint state — keys: {list(data.keys())})_\n"
    rows = ["| signal@level | n | mean_r | hit_rate | retention | status |",
            "|---|---|---|---|---|---|"]
    for key in sorted(summary.keys()):
        s = summary[key]
        cell_status = cells.get(key, s.get("status", "?"))
        rows.append(
            f"| {key} | {s.get('n','?')} | {s.get('mean_r','?')} | "
            f"{s.get('hit_rate','?')} | {s.get('retention','?')} | {cell_status} |"
        )
    return "\n".join(rows) + "\n"


# ── section 5: signal concern ───────────────────────────────────────────────

def _summary_concerns(label: str, path: Path) -> list[str]:
    data, err = safe_load_json(path)
    if err:
        return [f"- {label}: _{err}_"]
    out = []
    by_mode = data.get("by_mode", {}) or {}
    for mode, m in by_mode.items():
        n = m.get("n", 0)
        mr = m.get("mean_r")
        hr = m.get("hit_rate")
        warn = []
        if isinstance(mr, (int, float)) and mr <= 0:
            warn.append("mean_r ≤ 0")
        if isinstance(hr, (int, float)) and hr < 0.45:
            warn.append("hit_rate < 0.45")
        flag = " — ⚠ " + " / ".join(warn) if warn else ""
        out.append(f"- {label} [{mode}]: n={n} mean_r={mr} hit_rate={hr}{flag}")
    return out


def render_signal_concern_section(extra: str | None) -> str:
    lines: list[str] = []
    if extra:
        lines.append(extra.strip())
        lines.append("")
    lines.append("Auto-extracted from outcome summaries:")
    lines.extend(_summary_concerns("R1/R2 outcomes", OUTCOMES_SUMMARY))
    lines.extend(_summary_concerns("R7 shadow outcomes", R7_SHADOW_SUMMARY))
    return "\n".join(lines) + "\n"


# ── section 6: files/data already checked ──────────────────────────────────

DEFAULT_FILES_CHECKED = [
    "outputs/order_flow/.health_state.json",
    "outputs/order_flow/.live_checkpoint_state.json",
    "outputs/order_flow/realflow_diagnostic_ESM6_15m.json",
    "outputs/order_flow/realflow_outcomes_summary_ESM6_15m.json",
    "outputs/order_flow/realflow_r7_shadow_summary_ESM6_15m.json",
    "PROJECT_STATE.md",
]


def render_files_checked_section(extra: list[str]) -> str:
    seen = set()
    items: list[str] = []
    for f in DEFAULT_FILES_CHECKED + (extra or []):
        if f in seen:
            continue
        seen.add(f)
        items.append(f"- `{f}`")
    return "\n".join(items) + "\n"


# ── sections 7/8/9/10: static blocks ────────────────────────────────────────

ALLOWED_ACTIONS = """- Read-only inspection of files listed under "Files/data already checked"
- Read-only commands invoking existing scripts in `scripts/` or modules in `order_flow_engine/src/`
- Hypothesising about cause/effect relationships in the data
- Asking clarifying questions
- Suggesting additional read-only files to consult
"""

FORBIDDEN_ACTIONS = """- Editing rules / thresholds / config / models / `ml_engine/` / predictor / alert_engine / ingest
- Editing `monitor_loop`, `health_monitor`, or `cache_refresh`
- Proposing trading behavior changes
- Promoting R7 shadow to production
- Inventing commands, scripts, modules, or CLI flags that do not already exist
- Suggesting code changes of any kind
- Auto-revert / auto-retune / auto-promote of anything
"""

EXPECTED_RESPONSE_FORMAT = """Reply in this exact structure:

1. **Direct answer (≤3 sentences)** — answer the Question section first.
2. **Confidence and caveats** — what limits the answer (sample size, missing context, etc.).
3. **Hypotheses to consider (max 3, ranked)** — for each: hypothesis, supporting evidence already present, what additional read-only check would discriminate.
4. **Suggested read-only checks** — concrete commands using ONLY existing files/scripts. If you don't know whether a command exists, say "verify path exists" instead of inventing one.
5. **Open questions for the user** — clarifications the user could answer to make the next round more useful.
6. **Out of scope reminder** — repeat back the Forbidden Actions list to confirm you understood.
"""

NO_INVENTED_COMMANDS = """**Important:** Do NOT invent commands, scripts, module paths, or CLI flags.
Use ONLY existing project files/scripts. The Files/data already checked section
lists known-good paths. If you want to suggest a check that requires a new file
or script, instead phrase it as "if a script existed at X that did Y, it could
help" — do NOT write a fabricated command line.
"""


# ── brief assembly ──────────────────────────────────────────────────────────

def build_brief(
    question: str,
    *,
    include_project_state: bool,
    include_outcomes_json: bool,
    files_checked: list[str],
    concern_extra: str | None,
) -> str:
    now = now_utc()
    parts: list[str] = []
    parts.append("# Brief for OpenAI Reviewer\n")
    parts.append(f"_Generated: {now.isoformat()}_\n")

    parts.append("## 1. Exact question from user\n")
    parts.append(question.strip() + "\n")

    parts.append("## 2. Current market/session state\n")
    parts.append(render_market_section(now))

    parts.append("## 3. Current health state\n")
    parts.append(render_health_section())

    parts.append("## 4. Current checkpoint table\n")
    parts.append(render_checkpoints_section())

    parts.append("## 5. Current signal concern\n")
    parts.append(render_signal_concern_section(concern_extra))

    parts.append("## 6. Files/data already checked\n")
    parts.append(render_files_checked_section(files_checked))

    parts.append("## 7. Allowed actions\n")
    parts.append(ALLOWED_ACTIONS)

    parts.append("## 8. Forbidden actions\n")
    parts.append(FORBIDDEN_ACTIONS)

    parts.append("## 9. Expected response format\n")
    parts.append(EXPECTED_RESPONSE_FORMAT)

    parts.append("## 10. No invented commands\n")
    parts.append(NO_INVENTED_COMMANDS)

    if include_project_state:
        parts.append(f"## Appendix A — PROJECT_STATE.md (head {PROJECT_STATE_HEAD_LINES} lines)\n")
        parts.append("```markdown\n" +
                     safe_read_text(PROJECT_STATE, head_lines=PROJECT_STATE_HEAD_LINES) +
                     "\n```\n")

    if include_outcomes_json:
        parts.append("## Appendix B — R1/R2 outcomes summary JSON\n")
        d, e = safe_load_json(OUTCOMES_SUMMARY)
        parts.append("```json\n" + (json.dumps(d, indent=2) if d else f"// {e}") + "\n```\n")
        parts.append("## Appendix C — R7 shadow outcomes summary JSON\n")
        d, e = safe_load_json(R7_SHADOW_SUMMARY)
        parts.append("```json\n" + (json.dumps(d, indent=2) if d else f"// {e}") + "\n```\n")

    return "\n".join(parts) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True,
                        help="The exact question / topic for the reviewer")
    parser.add_argument("--concern",
                        help="Free-text describing the current signal concern (prepended to section 5)")
    parser.add_argument("--files-checked", action="append", default=[],
                        help="Additional file path you've already inspected (repeatable)")
    parser.add_argument("--include-project-state", action="store_true",
                        help=f"Append head of PROJECT_STATE.md ({PROJECT_STATE_HEAD_LINES} lines) as Appendix A")
    parser.add_argument("--include-outcomes-json", action="store_true",
                        help="Append raw outcomes summary JSON as Appendices B and C")
    parser.add_argument("--out", default=str(BRIEF_PATH),
                        help=f"Brief output path (default {BRIEF_PATH})")
    args = parser.parse_args()

    body = build_brief(
        args.question,
        include_project_state=args.include_project_state,
        include_outcomes_json=args.include_outcomes_json,
        files_checked=args.files_checked,
        concern_extra=args.concern,
    )

    out = Path(args.out)
    write_atomic(out, body)
    print(f"wrote {out} ({len(body.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
