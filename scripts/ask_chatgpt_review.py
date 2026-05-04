"""OpenAI Reviewer Bridge — gated send of brief to OpenAI Chat Completions.

Read-only on engine state. Writes only:
  outputs/order_flow/chatgpt_response.md   (latest)
  outputs/order_flow/chatgpt_history/<UTC>_response.md   (archive of prior)

Safety rails:
  - OPENAI_API_KEY env only (never CLI arg, never logged)
  - Brief size cap 50 KB
  - Response size cap 100 KB (truncated)
  - Rate limit 60s via /tmp/chatgpt_review.lockfile mtime
  - Cost ceiling $0.25/call unless --force
  - Confirmation prompt unless --yes
  - --dry-run prints preview, no network
  - No retries (avoid silent double-charge)
  - System prompt fixes reviewer scope: research-only, no rule/threshold/model code changes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BRIEF_PATH = PROJECT / "outputs/order_flow/chatgpt_brief.md"
RESPONSE_PATH = PROJECT / "outputs/order_flow/chatgpt_response.md"
HISTORY_DIR = PROJECT / "outputs/order_flow/chatgpt_history"
LOCK_PATH = Path("/tmp/chatgpt_review.lockfile")

API_URL = "https://api.openai.com/v1/chat/completions"

BRIEF_SIZE_CAP_BYTES = 50 * 1024
RESPONSE_SIZE_CAP_BYTES = 100 * 1024
RATE_LIMIT_SECONDS = 60
COST_CEILING_USD = 0.25

# USD per 1M tokens. Input / output. Update as pricing changes.
PRICING = {
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "gpt-4o":      {"in": 2.50, "out": 10.00},
    "o1-mini":     {"in": 3.00, "out": 12.00},
}

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 4000

SYSTEM_PROMPT = (
    "You are a senior quant reviewing a research-stage order-flow signal "
    "system. Assume read-only review. Do NOT propose code changes that "
    "touch rules, thresholds, models, ml_engine, predictor, alert_engine, "
    "ingest, or trading behavior. Suggest diagnostics, hypotheses, and "
    "questions only. Be concise and specific."
)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def utc_stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token. Conservative for English."""
    return max(1, len(text) // 4)


def estimate_cost_usd(model: str, in_tokens: int, out_tokens: int) -> float:
    p = PRICING.get(model)
    if p is None:
        return float("inf")
    return (in_tokens * p["in"] + out_tokens * p["out"]) / 1_000_000


def load_dotenv_key(name: str) -> str | None:
    """Read a single key from .env if not already in os.environ. No 3rd-party dep."""
    val = os.environ.get(name)
    if val:
        return val
    env_file = PROJECT / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


def check_rate_limit() -> tuple[bool, float]:
    if not LOCK_PATH.exists():
        return True, 0.0
    age = time.time() - LOCK_PATH.stat().st_mtime
    if age >= RATE_LIMIT_SECONDS:
        return True, age
    return False, age


def touch_lock() -> None:
    LOCK_PATH.touch()


def archive_existing_response() -> Path | None:
    if not RESPONSE_PATH.exists():
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    prior_mtime = datetime.fromtimestamp(
        RESPONSE_PATH.stat().st_mtime, tz=timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")
    dest = HISTORY_DIR / f"{prior_mtime}_response.md"
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = HISTORY_DIR / f"{prior_mtime}_response_{suffix}.md"
    shutil.move(str(RESPONSE_PATH), str(dest))
    return dest


def write_response_atomic(body: str) -> None:
    RESPONSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESPONSE_PATH.with_suffix(".md.tmp")
    tmp.write_text(body)
    tmp.replace(RESPONSE_PATH)


def confirm(prompt: str) -> bool:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def call_openai(api_key: str, model: str, brief: str, max_tokens: int,
                timeout_s: int = 120) -> dict:
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": brief},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview cost + system prompt + brief, no network call")
    parser.add_argument("--yes", action="store_true",
                        help="Skip interactive confirmation")
    parser.add_argument("--force", action="store_true",
                        help="Bypass cost ceiling")
    parser.add_argument("--brief", default=str(BRIEF_PATH),
                        help=f"Brief path (default {BRIEF_PATH})")
    args = parser.parse_args()

    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"ERROR: brief not found at {brief_path}", file=sys.stderr)
        return 2

    brief_bytes = brief_path.read_bytes()
    if len(brief_bytes) > BRIEF_SIZE_CAP_BYTES:
        print(f"ERROR: brief size {len(brief_bytes)} > cap {BRIEF_SIZE_CAP_BYTES}",
              file=sys.stderr)
        return 2
    brief = brief_bytes.decode("utf-8")
    brief_sha = hashlib.sha256(brief_bytes).hexdigest()[:16]

    in_tokens = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(brief)
    out_tokens = args.max_tokens
    est_cost = estimate_cost_usd(args.model, in_tokens, out_tokens)

    print(f"brief:        {brief_path}")
    print(f"brief sha:    {brief_sha}")
    print(f"brief bytes:  {len(brief_bytes)}")
    print(f"model:        {args.model}")
    print(f"in tokens:    ~{in_tokens}")
    print(f"out tokens:   <= {out_tokens}")
    print(f"est cost USD: ${est_cost:.4f} (worst case)")
    print(f"ceiling:      ${COST_CEILING_USD:.2f}")

    if est_cost > COST_CEILING_USD and not args.force:
        print(f"ERROR: estimated cost ${est_cost:.4f} exceeds ceiling "
              f"${COST_CEILING_USD:.2f}. Pass --force to override.",
              file=sys.stderr)
        return 3

    if args.dry_run:
        print("\n--- system prompt ---")
        print(SYSTEM_PROMPT)
        print("\n--- brief ---")
        print(brief)
        print("\n[dry-run] no network call made.")
        return 0

    api_key = load_dotenv_key("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set in env or .env", file=sys.stderr)
        return 4

    ok, age = check_rate_limit()
    if not ok:
        wait = RATE_LIMIT_SECONDS - age
        print(f"ERROR: rate limit. Wait {wait:.1f}s before next send.",
              file=sys.stderr)
        return 5

    if not args.yes:
        if not confirm(f"Send to OpenAI {args.model}? [y/N] "):
            print("aborted by user.")
            return 0

    touch_lock()
    request_ts = now_utc().isoformat()
    try:
        resp = call_openai(api_key, args.model, brief, args.max_tokens)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:1000]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        return 6
    except Exception as e:
        print(f"ERROR: send failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 6
    response_ts = now_utc().isoformat()

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"ERROR: unexpected response shape: {e}", file=sys.stderr)
        print(json.dumps(resp)[:1000])
        return 7

    usage = resp.get("usage", {})
    actual_in = usage.get("prompt_tokens", in_tokens)
    actual_out = usage.get("completion_tokens", 0)
    actual_cost = estimate_cost_usd(args.model, actual_in, actual_out)

    truncated = False
    if len(content.encode("utf-8")) > RESPONSE_SIZE_CAP_BYTES:
        content = content.encode("utf-8")[:RESPONSE_SIZE_CAP_BYTES].decode(
            "utf-8", errors="ignore")
        truncated = True

    archived = archive_existing_response()

    header = (
        "---\n"
        f"model: {args.model}\n"
        f"request_ts: {request_ts}\n"
        f"response_ts: {response_ts}\n"
        f"brief_sha: {brief_sha}\n"
        f"brief_path: {brief_path}\n"
        f"prompt_tokens: {actual_in}\n"
        f"completion_tokens: {actual_out}\n"
        f"actual_cost_usd: {actual_cost:.6f}\n"
        f"truncated: {truncated}\n"
        f"prior_archived: {archived if archived else 'none'}\n"
        "---\n\n"
    )
    write_response_atomic(header + content)

    print(f"\nresponse: {RESPONSE_PATH}")
    print(f"archived prior: {archived if archived else 'none'}")
    print(f"actual tokens: in={actual_in} out={actual_out} cost=${actual_cost:.6f}")
    if truncated:
        print("WARNING: response truncated to 100 KB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
