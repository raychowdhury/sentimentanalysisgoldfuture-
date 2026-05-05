#!/usr/bin/env python
"""Live SDK watchdog — Stage 1 read-only mode.

Evaluates whether Flask + Live SDK would need a restart based on:
  - Flask process presence
  - Flask HTTP 200
  - latest TAPE ALERT age in /tmp/flask.log
  - latest 1m live parquet bar age
  - 15m live parquet stale-file presence
  - CME market-open guard

Logs a decision per tick to outputs/order_flow/live_sdk_watchdog.log.
NEVER restarts anything. NEVER kills processes. NEVER spawns processes.
READ-ONLY observation only. Stage 2 will add the restart action.

Standing instruction: no rule / threshold / config / model / ml_engine /
predictor / alert_engine / ingest / outcome scoring / horizon / R7
promotion / trading behavior change.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path("/Users/ray/Dev/Sentiment analysis projtect")
FLASK_LOG = Path("/tmp/flask.log")
PARQUET_1M = PROJECT / "order_flow_engine/data/processed/ESM6_1m_live.parquet"
PARQUET_15M = PROJECT / "order_flow_engine/data/processed/ESM6_15m_live.parquet"
WATCHDOG_LOG = PROJECT / "outputs/order_flow/live_sdk_watchdog.log"

NY_TZ = ZoneInfo("America/New_York")
TAPE_AGE_RESTART_MIN = 30.0
PARQUET_AGE_RESTART_MIN = 30.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def market_open() -> bool:
    """CME Globex approximation: closed Fri 22:00Z → Sun 22:00Z."""
    n = now_utc()
    wd, hr = n.weekday(), n.hour
    if wd == 5:
        return False
    if wd == 6 and hr < 22:
        return False
    if wd == 4 and hr >= 22:
        return False
    return True


def latest_tape_alert_utc() -> datetime | None:
    if not FLASK_LOG.exists():
        return None
    try:
        with FLASK_LOG.open("rb") as f:
            size = f.seek(0, 2)
            chunk = min(size, 200_000)
            f.seek(-chunk, 2)
            data = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    last = None
    for line in data.splitlines():
        if "TAPE ALERT" not in line:
            continue
        if not (line.startswith("[") and len(line) > 21):
            continue
        try:
            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
            last = ts.replace(tzinfo=NY_TZ).astimezone(timezone.utc)
        except Exception:
            continue
    return last


def last_bar_age_min(parquet_path: Path) -> float | None:
    if not parquet_path.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(parquet_path)
    except Exception:
        return None
    if len(df) == 0:
        return None
    import pandas as pd
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return (now_utc() - idx.max().to_pydatetime()).total_seconds() / 60


def flask_pid() -> str | None:
    out = subprocess.run(
        ["pgrep", "-f", r"python.*app\.py"],
        capture_output=True, text=True,
    )
    pids = [p for p in out.stdout.strip().split("\n") if p]
    return pids[0] if pids else None


def flask_http_ok() -> tuple[bool, int | str]:
    try:
        with urllib.request.urlopen("http://localhost:5001/", timeout=3) as r:
            code = r.getcode()
        return code == 200, code
    except Exception as e:
        return False, str(e)[:120]


def evaluate() -> dict:
    ts = now_utc().isoformat()
    is_open = market_open()

    if not is_open:
        return {
            "ts": ts,
            "decision": "SKIP_MARKET_CLOSED",
            "market_open": False,
            "would_restart": False,
            "mode": "read_only_stage1",
        }

    pid = flask_pid()
    http_ok, http_detail = flask_http_ok()
    tape_ts = latest_tape_alert_utc()
    tape_age = (
        (now_utc() - tape_ts).total_seconds() / 60
        if tape_ts is not None else None
    )
    p1m_age = last_bar_age_min(PARQUET_1M)
    p15m_present = PARQUET_15M.exists()

    tape_stale = tape_age is None or tape_age > TAPE_AGE_RESTART_MIN
    p1m_stale = p1m_age is None or p1m_age > PARQUET_AGE_RESTART_MIN

    would_restart = (
        is_open
        and pid is not None
        and http_ok
        and tape_stale
        and p1m_stale
    )

    if would_restart:
        decision = "WOULD_RESTART"
        reason = "tape_alert_age and 1m_parquet_age both exceed 30 min while Flask HTTP 200"
    elif not pid:
        decision = "FLASK_DOWN"
        reason = "no python app.py PID — manual investigation only (out of scope for watchdog)"
    elif not http_ok:
        decision = "FLASK_HTTP_DOWN"
        reason = f"Flask HTTP not 200: {http_detail} — manual only"
    elif tape_stale and not p1m_stale:
        decision = "PARTIAL_TAPE_STALE"
        reason = f"tape stale ({tape_age}) but 1m parquet fresh ({p1m_age}) — log rotation or quiet tape"
    elif p1m_stale and not tape_stale:
        decision = "PARTIAL_1M_STALE"
        reason = f"1m parquet stale ({p1m_age}) but tape fresh ({tape_age}) — persist gap"
    else:
        decision = "HEALTHY"
        reason = "all probes within thresholds"

    return {
        "ts": ts,
        "decision": decision,
        "would_restart": bool(would_restart),
        "market_open": True,
        "flask_pid": pid,
        "flask_http_200": http_ok,
        "flask_http_detail": http_detail if not http_ok else 200,
        "tape_alert_age_min": (
            round(tape_age, 1) if tape_age is not None else None
        ),
        "parquet_1m_age_min": (
            round(p1m_age, 1) if p1m_age is not None else None
        ),
        "parquet_15m_present": p15m_present,
        "tape_stale": tape_stale,
        "parquet_1m_stale": p1m_stale,
        "thresholds": {
            "tape_age_restart_min": TAPE_AGE_RESTART_MIN,
            "parquet_age_restart_min": PARQUET_AGE_RESTART_MIN,
        },
        "reason": reason,
        "mode": "read_only_stage1",
    }


def append_log(record: dict) -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WATCHDOG_LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main() -> int:
    record = evaluate()
    append_log(record)
    print(json.dumps(record, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
