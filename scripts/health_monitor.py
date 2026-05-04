"""Health monitor — probes 12 signals, logs JSON, notifies on transitions.

Phase 1: probes + logging + local notification only. No self-healing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT   = Path("/Users/ray/Dev/Sentiment analysis projtect")
LOG       = PROJECT / "outputs/order_flow/health_monitor.log"
STATE     = PROJECT / "outputs/order_flow/.health_state.json"
FLAG_DIR  = PROJECT / "outputs/order_flow"
FLASK_LOG = Path("/tmp/flask.log")
NY_TZ     = zoneinfo.ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log_record(record: dict) -> None:
    record["ts"] = now_utc().isoformat()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def notify(signal: str, status: str, msg: str) -> None:
    title = f"RFM Health: {signal} {status}"
    body  = msg.replace('"', "'")[:200]
    cmd = ["osascript", "-e",
           f'display notification "{body}" with title "{title}"']
    try:
        subprocess.run(cmd, check=False, timeout=5,
                       capture_output=True)
    except Exception:
        pass


def market_open() -> bool:
    """CME Globex approximation: closed Fri 22:00Z → Sun 22:00Z."""
    n = now_utc()
    wd, hr = n.weekday(), n.hour
    if wd == 5:                 # Saturday
        return False
    if wd == 6 and hr < 22:     # Sunday before 22:00 UTC
        return False
    if wd == 4 and hr >= 22:    # Friday after 22:00 UTC
        return False
    return True


def probe_s1_flask_proc():
    out = subprocess.run(["pgrep", "-f", r"python.*app\.py"],
                         capture_output=True, text=True)
    pids = [p for p in out.stdout.strip().split("\n") if p]
    if pids:
        return "healthy", {"pids": pids}
    return "unhealthy", {"reason": "no python app.py process"}


def probe_s2_flask_http():
    try:
        with urllib.request.urlopen("http://localhost:5001/", timeout=3) as r:
            code = r.getcode()
        if code == 200:
            return "healthy", {"http": code}
        return "unhealthy", {"http": code}
    except Exception as e:
        return "unhealthy", {"error": str(e)[:120]}


def _latest_tape_alert_utc() -> datetime | None:
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


def probe_s3_live_sdk_stream():
    if not market_open():
        return "skipped", {"reason": "market closed"}
    last = _latest_tape_alert_utc()
    if last is None:
        return "unhealthy", {"reason": "no TAPE ALERT in /tmp/flask.log tail"}
    age = (now_utc() - last).total_seconds() / 60
    if age < 15:
        return "healthy", {"latest_alert_age_min": round(age, 1)}
    return "unhealthy", {"latest_alert_age_min": round(age, 1)}


def _last_bar_age_min(parquet_path: Path) -> float | None:
    if not parquet_path.exists():
        return None
    try:
        df = pd.read_parquet(parquet_path)
    except Exception:
        return None
    if len(df) == 0:
        return None
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return (now_utc() - idx.max().to_pydatetime()).total_seconds() / 60


def probe_s4_live_1m_freshness():
    """1m parquet persists every 25 bars via _LIVE_PERSIST_EVERY=25, so
    last_bar can legitimately trail wall-clock by up to ~25 min. Use 30 min
    threshold (25 + 5 buffer) to avoid false positives between persist
    snapshots."""
    if not market_open():
        return "skipped", {"reason": "market closed"}
    p = PROJECT / "order_flow_engine/data/processed/ESM6_1m_live.parquet"
    age = _last_bar_age_min(p)
    if age is None:
        return "unhealthy", {"reason": "ESM6_1m_live.parquet missing or unreadable"}
    if age < 30:
        return "healthy", {"last_bar_age_min": round(age, 1)}
    return "unhealthy", {"last_bar_age_min": round(age, 1)}


def probe_s5_15m_parquet_absent():
    p = PROJECT / "order_flow_engine/data/processed/ESM6_15m_live.parquet"
    if p.exists():
        return "unhealthy", {
            "reason": "ESM6_15m_live.parquet present — rename workaround needed",
        }
    return "healthy", {}


def probe_s6_raw_freshness():
    p = PROJECT / "order_flow_engine/data/raw/ESM6_15m.parquet"
    age = _last_bar_age_min(p)
    if age is None:
        return "unhealthy", {"reason": "raw ESM6_15m.parquet missing or unreadable"}
    if age < 90:
        return "healthy", {"last_bar_age_min": round(age, 1)}
    return "unhealthy", {"last_bar_age_min": round(age, 1)}


def probe_s7_monitor_loop_proc():
    out = subprocess.run(["pgrep", "-f", "order_flow_engine.src.monitor_loop"],
                         capture_output=True, text=True)
    pids = [p for p in out.stdout.strip().split("\n") if p]
    if pids:
        return "healthy", {"pids": pids}
    return "unhealthy", {"reason": "no monitor_loop process"}


def probe_s8_launchd_cache_refresh():
    out = subprocess.run(["launchctl", "list"],
                         capture_output=True, text=True)
    if "com.rfm.cache-refresh" in out.stdout:
        return "healthy", {}
    return "unhealthy", {"reason": "com.rfm.cache-refresh not loaded"}


def probe_s9_cache_refresh_log():
    p = PROJECT / "outputs/order_flow/cache_refresh.log"
    if not p.exists():
        return "unhealthy", {"reason": "cache_refresh.log missing"}
    age_min = (time.time() - p.stat().st_mtime) / 60
    if age_min < 90:
        return "healthy", {"log_mtime_age_min": round(age_min, 1)}
    return "unhealthy", {"log_mtime_age_min": round(age_min, 1)}


def probe_s10_failed_flag():
    p = PROJECT / "outputs/order_flow/cache_refresh_FAILED.flag"
    if p.exists():
        return "unhealthy", {"reason": "cache_refresh_FAILED.flag present"}
    return "healthy", {}


_PENDING_SNAPSHOT = PROJECT / "outputs/order_flow/.pending_snapshot.json"

# Watched (pending_path, settled_jsonl_path, label) tuples
_PENDING_SOURCES = [
    (
        PROJECT / "outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json",
        PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl",
        "r1r2",
    ),
    (
        PROJECT / "outputs/order_flow/realflow_r7_shadow_pending_ESM6_15m.json",
        PROJECT / "outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl",
        "r7_shadow",
    ),
]


def _read_pending_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        rows = json.loads(path.read_text())
    except Exception:
        return set()
    if not isinstance(rows, list):
        return set()
    return {r.get("signal_id") for r in rows if isinstance(r, dict) and r.get("signal_id")}


def _read_settled_ids(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = d.get("signal_id")
                if sid:
                    out.add(sid)
    except Exception:
        pass
    return out


def _load_pending_snapshot() -> dict[str, list[str]]:
    if not _PENDING_SNAPSHOT.exists():
        return {}
    try:
        return json.loads(_PENDING_SNAPSHOT.read_text())
    except Exception:
        return {}


def _save_pending_snapshot(snapshot: dict[str, list[str]]) -> None:
    _PENDING_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PENDING_SNAPSHOT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    tmp.replace(_PENDING_SNAPSHOT)


def probe_s16_pending_disappearance():
    """Detect silent loss of pending fires across passes.

    Pending JSON files are rewritten each settle pass by the trackers. After
    a realflow_history backfill, pending fires whose recomputed correlations
    no longer cross the trigger threshold can disappear from pending without
    ever being settled to JSONL. This probe diffs the previous snapshot of
    pending IDs against the current, then verifies any disappeared IDs
    appear in the corresponding settled JSONL. Disappeared-and-not-settled =
    silent loss → unhealthy.

    Visibility-only. No scoring change. Updates the snapshot file in place.
    """
    prev = _load_pending_snapshot()
    current_snapshot: dict[str, list[str]] = {}
    silent_losses: list[dict] = []

    for pending_path, settled_path, label in _PENDING_SOURCES:
        cur_ids = _read_pending_ids(pending_path)
        prev_ids = set(prev.get(label, []))
        if prev_ids:
            disappeared = prev_ids - cur_ids
            if disappeared:
                settled_ids = _read_settled_ids(settled_path)
                lost = sorted(disappeared - settled_ids)
                if lost:
                    silent_losses.append({"source": label, "lost_ids": lost})
        current_snapshot[label] = sorted(cur_ids)

    _save_pending_snapshot(current_snapshot)

    if silent_losses:
        return "unhealthy", {"silent_losses": silent_losses}
    return "healthy", {"watched_sources": [s for _, _, s in _PENDING_SOURCES]}


_DEMOTION_SOURCES = [
    PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl",
    PROJECT / "outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl",
]
DEMOTION_WINDOW_N = 50              # last N settled fires (combined across sources)
DEMOTION_RATIO_THRESHOLD = 0.30     # warn if true_demotion / candidate_live > 30%
LIVE_TAIL_CAP_HOURS = 8.3           # 1m tail = TAIL_LEN(500) bars × 1m / 60

# A fire is a "candidate live fire" if its discovery gap is within the live
# tail cap — Live SDK *should* have seen it as live. Anything beyond the cap
# is backfill rediscovery, correctly tagged historical, NOT a silent demotion.


def _read_recent_outcomes(path: Path, n: int) -> list[dict]:
    """Read last n outcome rows from a JSONL file. Returns list (may be < n)."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return out[-n:] if n else out


def _discovery_gap_hours(row: dict) -> float | None:
    fts = row.get("fire_ts_utc")
    dts = row.get("discovered_at")
    if not fts or not dts:
        return None
    try:
        a = datetime.fromisoformat(fts.replace("Z", "+00:00"))
        b = datetime.fromisoformat(dts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (b - a).total_seconds() / 3600.0


def probe_s17_demotion_rate():
    """Detect TRUE silent live → historical fire demotion.

    1m live tail caps at 500 bars (~8.3h per blocker #5). Fires whose
    discovered_at exceeds that cap are correctly tagged mode=historical
    because Live SDK could not have captured them — these are backfill
    rediscoveries, NOT silent demotions.

    A "true silent demotion" is a fire whose discovery gap is WITHIN the
    8.3h cap AND yet got tagged mode=historical instead of mode=live.
    Those are the dangerous cases: Live SDK / monitor_loop should have
    caught them in real time but didn't.

    Metric: true_demotion / candidate_live where:
      candidate_live = fires with discovery gap <= LIVE_TAIL_CAP_HOURS
                       (regardless of mode tag)
      true_demotion  = subset of candidate_live tagged mode=historical

    Visibility-only. No scoring change. No write to outcomes JSONL.
    """
    combined: list[dict] = []
    for src in _DEMOTION_SOURCES:
        combined.extend(_read_recent_outcomes(src, DEMOTION_WINDOW_N))

    if not combined:
        return "skipped", {"reason": "no settled outcomes available"}

    combined.sort(key=lambda d: d.get("fire_ts_utc", ""), reverse=True)
    window = combined[:DEMOTION_WINDOW_N]

    n_total = len(window)
    n_live = sum(1 for d in window if d.get("mode") == "live")
    n_historical_total = sum(1 for d in window if d.get("mode") == "historical")

    candidate_live = []
    backfill_rediscovery = []
    for d in window:
        gap = _discovery_gap_hours(d)
        if gap is None:
            continue
        if gap <= LIVE_TAIL_CAP_HOURS:
            candidate_live.append(d)
        elif d.get("mode") == "historical":
            backfill_rediscovery.append(d)

    n_candidate_live = len(candidate_live)
    n_true_demotion = sum(1 for d in candidate_live if d.get("mode") == "historical")
    true_ratio = (round(n_true_demotion / n_candidate_live, 4)
                  if n_candidate_live else 0.0)
    raw_ratio = round(n_historical_total / n_total, 4) if n_total else 0.0

    details = {
        "window_n": n_total,
        "n_live": n_live,
        "n_historical_total": n_historical_total,
        "n_candidate_live": n_candidate_live,
        "n_true_demotion": n_true_demotion,
        "n_backfill_rediscovery": len(backfill_rediscovery),
        "true_demotion_ratio": true_ratio,
        "raw_demotion_ratio": raw_ratio,
        "threshold": DEMOTION_RATIO_THRESHOLD,
        "live_tail_cap_hours": LIVE_TAIL_CAP_HOURS,
    }
    if n_candidate_live == 0:
        return "skipped", {**details, "reason": "no fires within live tail cap window"}
    if true_ratio > DEMOTION_RATIO_THRESHOLD:
        details["reason"] = (
            f"true_demotion_ratio={true_ratio:.2f} > {DEMOTION_RATIO_THRESHOLD:.2f} "
            f"({n_true_demotion}/{n_candidate_live} fires within {LIVE_TAIL_CAP_HOURS}h "
            "tail window were silently demoted to historical)"
        )
        return "unhealthy", details
    return "healthy", details


def probe_s14_realflow_backfill_log():
    """Watch realflow_backfill.log freshness. Backfill runs every 8h via launchd.
    Warn if log mtime > 8h (skipped runs OK; missing log means schedule broken)."""
    p = PROJECT / "outputs/order_flow/realflow_backfill.log"
    if not p.exists():
        return "unhealthy", {"reason": "realflow_backfill.log missing — schedule not loaded?"}
    age_min = (time.time() - p.stat().st_mtime) / 60
    if age_min < 480:   # 8h cadence + small buffer
        return "healthy", {"log_mtime_age_min": round(age_min, 1)}
    return "unhealthy", {"log_mtime_age_min": round(age_min, 1)}


def probe_s15_realflow_backfill_failed():
    p = PROJECT / "outputs/order_flow/realflow_backfill_FAILED.flag"
    if p.exists():
        return "unhealthy", {"reason": "realflow_backfill_FAILED.flag present"}
    return "healthy", {}


def probe_s11_disk():
    usage = shutil.disk_usage(PROJECT)
    pct = usage.used / usage.total * 100
    if pct < 90:
        return "healthy", {"disk_used_pct": round(pct, 1)}
    return "unhealthy", {"disk_used_pct": round(pct, 1)}


_CHECKPOINT_STATE = PROJECT / "outputs/order_flow/.live_checkpoint_state.json"
_CHECKPOINT_BASELINES = {
    "r1_buyer_down":            1.18,
    "r2_seller_up":             0.75,
    "r7_cvd_divergence_shadow": 0.7135,
}
_CHECKPOINT_RULE_SHORT = {
    "r1_buyer_down":            "R1",
    "r2_seller_up":             "R2",
    "r7_cvd_divergence_shadow": "R7sh",
}


def _checkpoint_compute() -> tuple[dict[str, str], dict[str, dict]]:
    """Return (cells, summary). cells maps rule@nLevel → status."""
    paths = [
        PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl",
        PROJECT / "outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl",
    ]
    stats = {k: {"n": 0, "wins": 0, "rs": []} for k in _CHECKPOINT_BASELINES}
    for p in paths:
        if not p.exists():
            continue
        try:
            with p.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("mode") != "live":
                        continue
                    rule = r.get("rule")
                    if rule not in stats:
                        continue
                    stats[rule]["n"] += 1
                    if r.get("outcome") == "win":
                        stats[rule]["wins"] += 1
                    fr = r.get("fwd_r_signed")
                    if fr is not None:
                        stats[rule]["rs"].append(fr)
        except Exception:
            pass

    cells: dict[str, str] = {}
    summary: dict[str, dict] = {}
    for rule, baseline in _CHECKPOINT_BASELINES.items():
        s = stats[rule]
        n = s["n"]
        if n > 0:
            mean_r = sum(s["rs"]) / len(s["rs"]) if s["rs"] else 0.0
            hit_rate = s["wins"] / n
            retention = (mean_r / baseline) if baseline else 0.0
        else:
            mean_r = hit_rate = retention = None
        for level in (10, 15, 30):
            key = f"{rule}@n{level}"
            if n < level:
                status = "NOT_REACHED"
            else:
                warn = (mean_r is not None
                        and (mean_r <= 0 or retention < 0.5 or hit_rate < 0.45))
                status = "WARN" if warn else "OK"
            cells[key] = status
            summary[key] = {
                "rule": rule, "level": level, "n": n, "status": status,
                "mean_r": round(mean_r, 3) if mean_r is not None else None,
                "retention": round(retention, 3) if retention is not None else None,
                "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
            }
    return cells, summary


def probe_s13_checkpoints():
    """Compute checkpoint cells, compare to last persisted state, fire osascript
    notifications on transitions. Persists state for dedupe. First run is silent
    (pre-seed)."""
    cells, summary = _checkpoint_compute()

    prev: dict[str, str] = {}
    first_run = True
    if _CHECKPOINT_STATE.exists():
        try:
            saved = json.loads(_CHECKPOINT_STATE.read_text())
            prev = saved.get("cells", {}) or {}
            first_run = False
        except Exception:
            pass

    notifications: list[str] = []
    if not first_run:
        for key, status in cells.items():
            prev_status = prev.get(key, "NOT_REACHED")
            if status == prev_status:
                continue
            info = summary[key]
            short = _CHECKPOINT_RULE_SHORT[info["rule"]]
            level = info["level"]
            mr = info["mean_r"]
            hr = info["hit_rate"]
            ret = info["retention"]

            if status == "WARN":
                msg = (f"RFM Checkpoint: {short} n{level} WARN — "
                       f"mean_r={mr} hit={hr} ret={ret}")
                notify(f"{short}@n{level}", "WARN", msg)
                notifications.append(msg)
            elif status == "OK" and prev_status == "WARN":
                msg = f"RFM Checkpoint: {short} n{level} RECOVERED"
                notify(f"{short}@n{level}", "RECOVERED", msg)
                notifications.append(msg)
            elif (status == "OK" and prev_status == "NOT_REACHED"
                  and level == 30):
                msg = f"RFM Checkpoint: {short} VERDICT READY (n=30)"
                notify(f"{short}@n{level}", "VERDICT", msg)
                notifications.append(msg)

    _CHECKPOINT_STATE.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_STATE.write_text(json.dumps({
        "ts": now_utc().isoformat(),
        "cells": cells,
        "summary": summary,
    }, default=str, indent=2))

    has_warn = any(s == "WARN" for s in cells.values())
    if first_run:
        return "healthy", {"first_run": True, "cells": len(cells)}
    if has_warn:
        warn_keys = sorted(k for k, s in cells.items() if s == "WARN")
        return "unhealthy", {
            "warn": warn_keys,
            "notified_this_run": len(notifications),
        }
    return "healthy", {"warn": [], "notified_this_run": len(notifications)}


def probe_s12_pending_backlog():
    paths = [
        PROJECT / "outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json",
        PROJECT / "outputs/order_flow/realflow_r7_shadow_pending_ESM6_15m.json",
    ]
    total = 0
    for p in paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                total += len(data)
        except Exception:
            pass
    if total < 50:
        return "healthy", {"pending_count": total}
    return "unhealthy", {"pending_count": total}


PROBES = [
    ("s1_flask_proc",          probe_s1_flask_proc),
    ("s2_flask_http",          probe_s2_flask_http),
    ("s3_live_sdk_stream",     probe_s3_live_sdk_stream),
    ("s4_live_1m_freshness",   probe_s4_live_1m_freshness),
    ("s5_15m_parquet_absent",  probe_s5_15m_parquet_absent),
    ("s6_raw_freshness",       probe_s6_raw_freshness),
    ("s7_monitor_loop_proc",   probe_s7_monitor_loop_proc),
    ("s8_launchd_cache",       probe_s8_launchd_cache_refresh),
    ("s9_cache_refresh_log",   probe_s9_cache_refresh_log),
    ("s10_failed_flag",        probe_s10_failed_flag),
    ("s11_disk",               probe_s11_disk),
    ("s12_pending_backlog",    probe_s12_pending_backlog),
    ("s13_checkpoints",        probe_s13_checkpoints),
    ("s14_realflow_backfill_log",    probe_s14_realflow_backfill_log),
    ("s15_realflow_backfill_failed", probe_s15_realflow_backfill_failed),
    ("s16_pending_disappearance",    probe_s16_pending_disappearance),
    ("s17_demotion_rate",            probe_s17_demotion_rate),
]


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def main() -> int:
    state = load_state()
    new_state: dict[str, str] = {}
    summary: list[str] = []

    for name, fn in PROBES:
        try:
            status, details = fn()
        except Exception as e:
            status, details = "unhealthy", {"error": f"probe crashed: {e}"[:200]}

        new_state[name] = status
        prev = state.get(name)

        log_record({"signal": name, "status": status, "details": details})

        flag = FLAG_DIR / f"HEALTH_{name}.flag"
        if status == "unhealthy":
            if prev != "unhealthy":
                notify(name, "DOWN", json.dumps(details))
                flag.write_text(json.dumps(
                    {"ts": now_utc().isoformat(), "signal": name,
                     "status": status, "details": details},
                    default=str, indent=2,
                ))
        elif status == "healthy":
            if prev == "unhealthy":
                notify(name, "RECOVERED", "ok")
                if flag.exists():
                    flag.unlink()

        summary.append(f"{name}={status}")

    save_state(new_state)
    log_record({"signal": "_summary", "status": "ok",
                "details": " ".join(summary)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
