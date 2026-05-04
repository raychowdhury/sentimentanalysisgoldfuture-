"""Validate outputs/order_flow/chatgpt_response.md for risky/invalid suggestions.

Read-only on response file and engine state. Writes only the review file
(atomic tmp+rename). Stdlib only. No network. No subprocess.

Verdict:
  SAFE = exit 0   — only allow-listed content
  FLAG = exit 1   — soft warnings (code blocks, auto-action language, missing sections)
  BLOCK = exit 2  — hard violations (invented commands, threshold/promo/model/trading edits)
  ERROR = exit 3  — missing source / parse failure

--strict promotes all FLAGs to BLOCK.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RESPONSE_PATH = PROJECT / "outputs/order_flow/chatgpt_response.md"
REVIEW_PATH = PROJECT / "outputs/order_flow/chatgpt_response_review.md"

VALIDATOR_VERSION = 1

# POSIX-ish standalone tools we accept without filesystem check
POSIX_ALLOWLIST = {
    "cat", "grep", "egrep", "fgrep", "ls", "head", "tail", "wc", "find",
    "jq", "awk", "sed", "sort", "uniq", "diff", "cut", "tr", "xargs",
    "less", "more", "file", "stat", "du", "df", "echo", "printf", "tee",
    "column", "paste", "tac", "rev", "comm", "join", "nl",
}

# Threshold-change verbs paired with threshold targets
THRESHOLD_VERBS = (
    r"change|set|update|modify|adjust|tune|loosen|tighten|"
    r"increase|decrease|raise|lower|edit|alter|patch"
)
THRESHOLD_TARGETS = (
    r"threshold|RULE_\w+|cutoff|-?0\.\d+|"
    r"RULE_DELTA_DOMINANCE(?:_REAL)?|"
    r"RULE_ABSORPTION_DELTA(?:_REAL)?|"
    r"RULE_TRAP_DELTA(?:_REAL)?|"
    r"RULE_CVD_CORR_THRESH(?:_REAL_SHADOW)?|"
    r"RULE_CVD_CORR_WINDOW"
)
RE_THRESHOLD_CHANGE = re.compile(
    rf"(?i)({THRESHOLD_VERBS}).{{0,40}}({THRESHOLD_TARGETS})"
)

RE_R7_PROMOTION = re.compile(
    r"(?i)("
    r"promot(?:e|ion|ing)"
    r"|move\s+to\s+production"
    r"|production\s+threshold"
    r"|graduate.{0,20}shadow"
    r"|shadow.{0,20}graduate"
    r"|switch.{0,20}production"
    r")"
)

ML_KEYWORDS = (
    "ml_engine", "model.fit", "retrain", "predictor.py",
    "feature_engineering", "joblib", "pickle.dump", "train_model",
    "fit_model", "model.train",
)

TRADING_KEYWORDS = (
    "place order", "submit trade", "execute trade", "live trade",
    "broker", "position size", "paper trade", "real money",
    "open position", "close position", "send to exchange",
)

RE_AUTO_ACTION = re.compile(
    r"(?i)("
    r"auto[- ]?(revert|promote|tune|retune|trade|execute|apply|fix)"
    r"|\bcron\b|\bscheduled\b|\blaunchd\b|\bsystemd\b"
    r"|automatically (?:apply|change|modify|update|run)"
    r")"
)

CONFIG_FILES = (
    "config.py", "rule_engine", "alert_engine", "ingest.py",
    "realtime_databento", "predictor.py",
)
EDIT_VERBS = ("change", "edit", "modify", "update", "add", "remove", "patch", "alter")

# Section 9 expected sub-section markers (case-insensitive substring match)
RESPONSE_SHAPE_MARKERS = [
    ("direct answer",        ["direct answer", "1."]),
    ("caveats",              ["caveat", "confidence"]),
    ("hypotheses",           ["hypothes"]),
    ("read-only checks",     ["read-only", "diagnostic check", "suggested check"]),
    ("open questions",       ["open question", "clarif"]),
    ("out-of-scope",         ["out of scope", "out-of-scope", "forbidden", "scope reminder"]),
]

# Allow-list scoring keywords
SAFE_KEYWORDS = (
    "hypothesis", "hypothesise", "hypothesize", "consider", "verify",
    "inspect", "read-only", "clarifying", "if a script existed",
    "could check", "would help", "uncertain", "depends on",
)

# Command extraction patterns
RE_PYTHON_MODULE = re.compile(
    r"(?:\.venv/bin/python|python|python3)\s+-m\s+([\w.]+)"
)
RE_PYTHON_SCRIPT = re.compile(
    r"(?:\.venv/bin/python|python|python3)\s+(scripts/[\w./-]+|[\w./-]+\.py)"
)
RE_BASH_SCRIPT = re.compile(
    r"(?:bash|sh)\s+(scripts/[\w./-]+\.sh|[\w./-]+\.sh)"
)
RE_FENCED_CODE = re.compile(
    r"^```(python|bash|sh|shell|yaml|toml|diff|sql|js|ts|json)?\s*$",
    re.MULTILINE,
)


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def strip_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter, body). Body is text after the closing `---`.
    If no front matter present, front_matter == ''."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    fm = text[4:end]
    body = text[end + len("\n---\n"):]
    return fm, body


def line_index(body: str, pos: int) -> int:
    """1-based line number for character offset `pos` in body."""
    return body.count("\n", 0, pos) + 1


def excerpt_at(body: str, pos: int, span: int = 80) -> str:
    start = max(0, pos - 10)
    end = min(len(body), pos + span)
    return body[start:end].replace("\n", " ⏎ ").strip()


# ── category checks ────────────────────────────────────────────────────────

def check_invented_commands(body: str, extra_allow: list[re.Pattern]) -> list[dict]:
    """Cat 1 — extract command candidates and verify against filesystem."""
    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def _record(line: int, kind: str, target: str, ok: bool, note: str) -> None:
        key = (line, target)
        if key in seen:
            return
        seen.add(key)
        if ok:
            return
        for ap in extra_allow:
            if ap.search(target):
                return
        findings.append({
            "category": 1,
            "name": "invented_command",
            "severity": "BLOCK",
            "line": line,
            "excerpt": f"{kind}: {target}",
            "why": note,
        })

    for m in RE_PYTHON_MODULE.finditer(body):
        mod = m.group(1)
        line = line_index(body, m.start())
        rel = mod.replace(".", "/") + ".py"
        path = PROJECT / rel
        if not path.exists():
            _record(line, "python -m", mod, False,
                    f"module path `{rel}` not found in project")

    for m in RE_PYTHON_SCRIPT.finditer(body):
        script = m.group(1)
        line = line_index(body, m.start())
        path = PROJECT / script
        if not path.exists():
            _record(line, "python", script, False,
                    f"script `{script}` not found")

    for m in RE_BASH_SCRIPT.finditer(body):
        script = m.group(1)
        line = line_index(body, m.start())
        path = PROJECT / script
        if not path.exists():
            _record(line, "bash", script, False,
                    f"script `{script}` not found")

    return findings


def check_threshold_change(body: str) -> list[dict]:
    findings = []
    for m in RE_THRESHOLD_CHANGE.finditer(body):
        line = line_index(body, m.start())
        findings.append({
            "category": 3,
            "name": "threshold_change",
            "severity": "BLOCK",
            "line": line,
            "excerpt": excerpt_at(body, m.start()),
            "why": f"verb `{m.group(1)}` near threshold target `{m.group(2)}`",
        })
    return findings


def check_r7_promotion(body: str) -> list[dict]:
    findings = []
    for m in RE_R7_PROMOTION.finditer(body):
        # Only flag if R7/cvd/shadow context is in the same paragraph (within 200 chars)
        ctx_start = max(0, m.start() - 200)
        ctx_end = min(len(body), m.end() + 200)
        ctx = body[ctx_start:ctx_end].lower()
        if "r7" in ctx or "cvd" in ctx or "shadow" in ctx:
            findings.append({
                "category": 4,
                "name": "r7_promotion",
                "severity": "BLOCK",
                "line": line_index(body, m.start()),
                "excerpt": excerpt_at(body, m.start()),
                "why": f"promotion language `{m.group(1)}` in R7/shadow context",
            })
    return findings


def check_keyword_block(body: str, keywords: tuple[str, ...], cat: int,
                       name: str, why_prefix: str) -> list[dict]:
    findings = []
    lower = body.lower()
    for kw in keywords:
        start = 0
        while True:
            idx = lower.find(kw.lower(), start)
            if idx == -1:
                break
            findings.append({
                "category": cat,
                "name": name,
                "severity": "BLOCK",
                "line": line_index(body, idx),
                "excerpt": excerpt_at(body, idx),
                "why": f"{why_prefix}: `{kw}`",
            })
            start = idx + len(kw)
    return findings


def check_auto_action(body: str) -> list[dict]:
    findings = []
    for m in RE_AUTO_ACTION.finditer(body):
        findings.append({
            "category": 7,
            "name": "auto_action",
            "severity": "FLAG",
            "line": line_index(body, m.start()),
            "excerpt": excerpt_at(body, m.start()),
            "why": f"auto-action language `{m.group(1)}`",
        })
    return findings


def check_config_edit_hints(body: str) -> list[dict]:
    findings = []
    lower = body.lower()
    for cf in CONFIG_FILES:
        for v in EDIT_VERBS:
            pat = re.compile(rf"\b{v}\b.{{0,40}}{re.escape(cf)}|{re.escape(cf)}.{{0,40}}\b{v}\b",
                             re.IGNORECASE)
            for m in pat.finditer(body):
                findings.append({
                    "category": 8,
                    "name": "config_edit_hint",
                    "severity": "BLOCK",
                    "line": line_index(body, m.start()),
                    "excerpt": excerpt_at(body, m.start()),
                    "why": f"verb `{v}` near config file `{cf}`",
                })
    return findings


def check_code_blocks(body: str) -> list[dict]:
    findings = []
    for m in RE_FENCED_CODE.finditer(body):
        lang = (m.group(1) or "plain").lower()
        if lang in {"plain", "text", "markdown", "md"}:
            continue
        findings.append({
            "category": 2,
            "name": "code_block",
            "severity": "FLAG",
            "line": line_index(body, m.start()),
            "excerpt": f"```{lang}",
            "why": f"fenced code block (`{lang}`) — read carefully before any action",
        })
    return findings


def check_response_shape(body: str) -> tuple[list[dict], list[tuple[str, bool]]]:
    findings = []
    lower = body.lower()
    presence: list[tuple[str, bool]] = []
    missing = []
    for label, markers in RESPONSE_SHAPE_MARKERS:
        present = any(mk in lower for mk in markers)
        presence.append((label, present))
        if not present:
            missing.append(label)
    if missing:
        findings.append({
            "category": 10,
            "name": "shape_conformance",
            "severity": "FLAG",
            "line": 1,
            "excerpt": "(whole response)",
            "why": f"expected sections missing: {', '.join(missing)}",
        })
    return findings, presence


def count_safe_signals(body: str) -> int:
    lower = body.lower()
    n = sum(lower.count(kw) for kw in SAFE_KEYWORDS)
    n += body.count("?")
    return n


# ── verdict + report ────────────────────────────────────────────────────────

def compute_verdict(findings: list[dict], strict: bool, body_len: int,
                    safe_signals: int) -> str:
    if body_len == 0:
        return "ERROR"
    severities = {f["severity"] for f in findings}
    if strict and "FLAG" in severities:
        severities.add("BLOCK")
    if "BLOCK" in severities:
        return "BLOCK"
    if "FLAG" in severities:
        return "FLAG"
    if not findings and safe_signals == 0:
        return "FLAG"
    return "SAFE"


def render_review(*, source_sha: str, body_len: int, verdict: str,
                  findings: list[dict], shape_presence: list[tuple[str, bool]],
                  safe_signals: int, strict: bool) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append(f"validator_version: {VALIDATOR_VERSION}")
    lines.append(f"source_response: {RESPONSE_PATH.relative_to(PROJECT)}")
    lines.append(f"source_response_sha: {source_sha}")
    lines.append(f"body_bytes: {body_len}")
    lines.append(f"generated_utc: {now_utc_iso()}")
    lines.append(f"strict_mode: {strict}")
    lines.append(f"verdict: {verdict}")
    lines.append("---\n")

    lines.append("# ChatGPT Response Review\n")
    lines.append(f"## Verdict: {verdict}\n")

    summaries = {
        "SAFE":  "Response respects all rails. Use as advisory input. No auto-apply.",
        "FLAG":  "Soft warnings present. Skim findings; ignore code blocks; act only on read-only suggestions.",
        "BLOCK": "Hard violations. Do NOT act on this response. Reject and rephrase brief, or send follow-up clarifying the ban.",
        "ERROR": "Source response empty or unreadable.",
    }
    lines.append(summaries.get(verdict, "") + "\n")

    n_block = sum(1 for f in findings if f["severity"] == "BLOCK")
    n_flag = sum(1 for f in findings if f["severity"] == "FLAG")
    lines.append(f"Counts: BLOCK={n_block}  FLAG={n_flag}  safe_signals={safe_signals}\n")

    lines.append("## Findings\n")
    if not findings:
        lines.append("_No findings._\n")
    else:
        by_cat: dict[int, list[dict]] = {}
        for f in findings:
            by_cat.setdefault(f["category"], []).append(f)
        for cat in sorted(by_cat.keys()):
            cat_findings = by_cat[cat]
            sev = cat_findings[0]["severity"]
            name = cat_findings[0]["name"]
            lines.append(f"### Category {cat} — {name} — {sev}\n")
            for f in cat_findings:
                lines.append(f"- line {f['line']}: `{f['excerpt']}` — {f['why']}")
            lines.append("")

    lines.append("## Conformance to expected response format\n")
    for label, present in shape_presence:
        mark = "[x]" if present else "[ ]"
        lines.append(f"- {mark} {label}")
    lines.append("")

    lines.append("## Recommended next action\n")
    if verdict == "BLOCK":
        lines.append("1. Do NOT apply any suggestion from this response.")
        lines.append("2. Read each BLOCK finding above and decide whether to:")
        lines.append("   - reject the response and rephrase the brief with stronger constraints")
        lines.append("   - send a follow-up brief clarifying the violated rail")
        lines.append("3. Never act on a BLOCK response without explicit human override + written rationale.")
    elif verdict == "FLAG":
        lines.append("1. Skim the FLAG findings above.")
        lines.append("2. Ignore fenced code blocks unless they are pure data examples.")
        lines.append("3. Act only on read-only diagnostic suggestions that match existing files/scripts.")
    elif verdict == "SAFE":
        lines.append("1. Response is advisory and respects all rails.")
        lines.append("2. Still no auto-apply — human decides every action.")
        lines.append("3. Cross-check any file path mentioned against the project before acting.")
    else:
        lines.append("Re-run validator after fixing source response.")
    lines.append("")

    lines.append("## Forbidden-action recap (always echoed)\n")
    lines.append("Per Brief section 8 — these are off-limits regardless of reviewer suggestion:")
    lines.append("- Editing rules / thresholds / config / models / `ml_engine/` / predictor / alert_engine / ingest")
    lines.append("- Editing `monitor_loop`, `health_monitor`, or `cache_refresh`")
    lines.append("- Proposing trading behavior changes")
    lines.append("- Promoting R7 shadow to production")
    lines.append("- Inventing commands, scripts, modules, or CLI flags that do not already exist")
    lines.append("- Suggesting code changes of any kind")
    lines.append("- Auto-revert / auto-retune / auto-promote of anything\n")

    return "\n".join(lines)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", default=str(RESPONSE_PATH),
                        help=f"Source response path (default {RESPONSE_PATH})")
    parser.add_argument("--out", default=str(REVIEW_PATH),
                        help=f"Review output path (default {REVIEW_PATH})")
    parser.add_argument("--strict", action="store_true",
                        help="Promote all FLAGs to BLOCK")
    parser.add_argument("--allow-pattern", action="append", default=[],
                        help="Regex to skip a known-false-positive command (repeatable)")
    parser.add_argument("--extra-forbidden", action="append", default=[],
                        help="Extra keyword to treat as BLOCK (repeatable)")
    args = parser.parse_args()

    src = Path(args.response)
    if not src.exists():
        print(f"ERROR: response not found at {src}", file=sys.stderr)
        return 3
    raw = src.read_text()
    fm, body = strip_front_matter(raw)
    body_len = len(body.strip())
    if body_len == 0:
        print("ERROR: response body empty", file=sys.stderr)
        return 3

    source_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    extra_allow_patterns = []
    for p in args.allow_pattern:
        try:
            extra_allow_patterns.append(re.compile(p))
        except re.error as e:
            print(f"ERROR: bad --allow-pattern {p!r}: {e}", file=sys.stderr)
            return 3

    findings: list[dict] = []
    findings += check_invented_commands(body, extra_allow_patterns)
    findings += check_threshold_change(body)
    findings += check_r7_promotion(body)
    findings += check_keyword_block(body, ML_KEYWORDS, 5, "ml_engine_touch",
                                    "ml/model keyword")
    findings += check_keyword_block(body, TRADING_KEYWORDS, 6, "trading_language",
                                    "trading keyword")
    findings += check_auto_action(body)
    findings += check_config_edit_hints(body)
    findings += check_code_blocks(body)
    if args.extra_forbidden:
        findings += check_keyword_block(
            body, tuple(args.extra_forbidden), 99, "extra_forbidden",
            "user-supplied keyword",
        )
    shape_findings, shape_presence = check_response_shape(body)
    findings += shape_findings

    safe_signals = count_safe_signals(body)
    verdict = compute_verdict(findings, args.strict, body_len, safe_signals)

    review = render_review(
        source_sha=source_sha,
        body_len=body_len,
        verdict=verdict,
        findings=findings,
        shape_presence=shape_presence,
        safe_signals=safe_signals,
        strict=args.strict,
    )
    out = Path(args.out)
    write_atomic(out, review)

    print(f"verdict: {verdict}")
    print(f"review:  {out}")
    print(f"counts:  BLOCK={sum(1 for f in findings if f['severity']=='BLOCK')} "
          f"FLAG={sum(1 for f in findings if f['severity']=='FLAG')} "
          f"safe_signals={safe_signals}")

    if verdict == "ERROR":
        return 3
    if verdict == "BLOCK":
        return 2
    if verdict == "FLAG":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
