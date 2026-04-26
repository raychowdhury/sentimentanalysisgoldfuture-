"""
Alert outcome tracker.

For each emitted alert, fetch forward bars after a delay and compute:
  - forward returns at fixed horizons (1h, 4h)
  - max favorable / adverse excursion in ATR units
  - outcome label: win / loss / neutral

Uses Alpaca REST historical bars (free IEX feed). Runs as a background daemon
thread; reads alerts.jsonl tail, writes alert_outcomes.jsonl.

Win rule:
  outcome = "win"      if max_fav_atr  >= TARGET_ATR  AND reached before stop
            "loss"     if max_adv_atr  >= STOP_ATR    AND reached before target
            "neutral"  otherwise

Direction inference from label:
  buyer_absorption / bullish_trap / possible_reversal (bullish bias) → long
  seller_absorption / bearish_trap (bearish bias) → short
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from order_flow_engine.src import config as of_cfg
from utils.logger import setup_logger

logger = setup_logger(__name__)

REST_BASE = "https://data.alpaca.markets/v2/stocks"

# Forward horizons (minutes) at which to record return + extremes.
HORIZONS_MIN: tuple[int, ...] = (60, 240)

# Win/loss thresholds in ATR multiples.
TARGET_ATR: float = 1.0
STOP_ATR:   float = 1.0

# Long-bias labels (alert implies buyer-favored move ahead).
LONG_LABELS = {"buyer_absorption", "bullish_trap", "possible_reversal"}
SHORT_LABELS = {"seller_absorption", "bearish_trap"}

OUTCOMES_PATH = of_cfg.OF_OUTPUT_DIR / "alert_outcomes.jsonl"
ALERTS_PATH   = of_cfg.OF_OUTPUT_DIR / "alerts.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _completed_alert_ids() -> set[str]:
    return {r.get("alert_id") for r in _load_jsonl(OUTCOMES_PATH) if r.get("alert_id")}


def _fetch_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    key    = os.getenv("ALPACA_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY/ALPACA_SECRET not set")
    params = {
        "start":     start.isoformat().replace("+00:00", "Z"),
        "end":       end.isoformat().replace("+00:00", "Z"),
        "timeframe": "1Min",
        "limit":     10000,
        "feed":      "iex",
        "adjustment": "raw",
    }
    rows: list[dict] = []
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{REST_BASE}/{symbol}/bars",
            params=params,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("bars") or [])
        page_token = data.get("next_page_token")
        if not page_token:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.set_index("t").sort_index()
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                            "c": "Close", "v": "Volume"})
    return df


def _compute_outcome(alert: dict, bars: pd.DataFrame) -> dict | None:
    if bars.empty:
        return None
    entry = float(alert["price"])
    atr = float(alert.get("atr") or 0.0)
    if atr <= 0:
        return None
    direction = +1 if alert["label"] in LONG_LABELS else \
                -1 if alert["label"] in SHORT_LABELS else 0
    if direction == 0:
        return None

    alert_ts = pd.Timestamp(alert["timestamp_utc"])
    forward = bars[bars.index > alert_ts]
    if forward.empty:
        return None

    target_price = entry + direction * TARGET_ATR * atr
    stop_price   = entry - direction * STOP_ATR   * atr

    hit_target_idx = None
    hit_stop_idx   = None
    for i, (ts, row) in enumerate(forward.iterrows()):
        hi, lo = float(row["High"]), float(row["Low"])
        if direction > 0:
            if hi >= target_price and hit_target_idx is None:
                hit_target_idx = i
            if lo <= stop_price and hit_stop_idx is None:
                hit_stop_idx = i
        else:
            if lo <= target_price and hit_target_idx is None:
                hit_target_idx = i
            if hi >= stop_price and hit_stop_idx is None:
                hit_stop_idx = i
        if hit_target_idx is not None and hit_stop_idx is not None:
            break

    if hit_target_idx is not None and (hit_stop_idx is None or hit_target_idx < hit_stop_idx):
        outcome = "win"
    elif hit_stop_idx is not None and (hit_target_idx is None or hit_stop_idx < hit_target_idx):
        outcome = "loss"
    else:
        outcome = "neutral"

    horizon_returns = {}
    for h_min in HORIZONS_MIN:
        cutoff = alert_ts + pd.Timedelta(minutes=h_min)
        slice_ = forward[forward.index <= cutoff]
        if slice_.empty:
            continue
        ret = direction * (float(slice_["Close"].iloc[-1]) - entry) / entry
        horizon_returns[f"ret_{h_min}m"] = round(ret, 6)

    max_fav = direction * (forward["High" if direction > 0 else "Low"].agg(
        "max" if direction > 0 else "min") - entry)
    max_adv = direction * (forward["Low" if direction > 0 else "High"].agg(
        "min" if direction > 0 else "max") - entry)

    return {
        "alert_id":       alert["id"],
        "alert_ts":       alert["timestamp_utc"],
        "symbol":         alert["symbol"],
        "timeframe":      alert["timeframe"],
        "label":          alert["label"],
        "confidence":     alert["confidence"],
        "direction":      direction,
        "entry":          round(entry, 4),
        "atr":            round(atr, 4),
        "outcome":        outcome,
        "max_fav_atr":    round(float(max_fav) / atr, 4),
        "max_adv_atr":    round(float(max_adv) / atr, 4),
        "horizon_returns": horizon_returns,
        "computed_at":    datetime.now(timezone.utc).isoformat(),
    }


def _settle_pending(min_age_minutes: int = 240) -> int:
    """Compute outcomes for alerts older than `min_age_minutes` without one."""
    if not (os.getenv("ALPACA_KEY", "").strip()
            and os.getenv("ALPACA_SECRET", "").strip()):
        # No credentials → can't fetch forward bars. Skip silently — the loop
        # is harmless until keys appear.
        return 0
    alerts = _load_jsonl(ALERTS_PATH)
    if not alerts:
        return 0
    done = _completed_alert_ids()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    pending: list[dict] = []
    for a in alerts:
        if a.get("id") in done:
            continue
        try:
            ts = pd.Timestamp(a["timestamp_utc"]).to_pydatetime()
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts <= cutoff:
            pending.append(a)
    if not pending:
        return 0

    written = 0
    OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTCOMES_PATH.open("a") as f:
        for a in pending:
            try:
                ts = pd.Timestamp(a["timestamp_utc"])
                start = ts - pd.Timedelta(minutes=5)
                end   = ts + pd.Timedelta(minutes=max(HORIZONS_MIN) + 30)
                bars = _fetch_bars(a["symbol"], start.to_pydatetime(), end.to_pydatetime())
                outcome = _compute_outcome(a, bars)
                if outcome is None:
                    continue
                f.write(json.dumps(outcome) + "\n")
                written += 1
            except Exception as e:
                logger.warning(f"outcome compute failed {a.get('id')}: {e}")
    if written:
        logger.info(f"outcome_tracker: settled {written} alerts")
    return written


def rolling_stats(window_days: int = 7) -> dict:
    """Aggregate win rate / expectancy over the trailing window."""
    rows = _load_jsonl(OUTCOMES_PATH)
    if not rows:
        return {"count": 0, "wins": 0, "losses": 0, "neutral": 0,
                "win_rate": 0.0, "expectancy_atr": 0.0, "window_days": window_days}
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    keep = []
    for r in rows:
        try:
            ts = pd.Timestamp(r["alert_ts"]).to_pydatetime()
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            keep.append(r)
    if not keep:
        return {"count": 0, "wins": 0, "losses": 0, "neutral": 0,
                "win_rate": 0.0, "expectancy_atr": 0.0, "window_days": window_days}
    wins   = sum(1 for r in keep if r["outcome"] == "win")
    losses = sum(1 for r in keep if r["outcome"] == "loss")
    neutral = sum(1 for r in keep if r["outcome"] == "neutral")
    decisive = wins + losses
    win_rate = wins / decisive if decisive else 0.0
    # Expectancy in ATR units: wins ≈ +TARGET_ATR, losses ≈ -STOP_ATR,
    # neutral counts as terminal close-to-close.
    exp = sum(
        TARGET_ATR if r["outcome"] == "win" else
        -STOP_ATR if r["outcome"] == "loss" else
        r.get("max_fav_atr", 0) + r.get("max_adv_atr", 0)
        for r in keep
    ) / len(keep)
    return {
        "count":           len(keep),
        "wins":            wins,
        "losses":          losses,
        "neutral":         neutral,
        "win_rate":        round(win_rate, 4),
        "expectancy_atr":  round(exp, 4),
        "window_days":     window_days,
    }


# ── background loop ─────────────────────────────────────────────────────────

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop(interval_s: int) -> None:
    logger.info(f"outcome_tracker started (interval={interval_s}s)")
    while not _stop.is_set():
        try:
            _settle_pending()
        except Exception as e:
            logger.warning(f"outcome_tracker tick error: {e}")
        _stop.wait(interval_s)
    logger.info("outcome_tracker stopped")


def start_thread(interval_s: int = 300) -> bool:
    global _thread
    if _thread and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(interval_s,), daemon=True, name="outcome-tracker",
    )
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "outcomes_path": str(OUTCOMES_PATH),
        **rolling_stats(7),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Settle pending alert outcomes")
    ap.add_argument("--once", action="store_true", help="run one settle pass and exit")
    ap.add_argument("--stats", type=int, default=0, help="print rolling stats over N days")
    args = ap.parse_args()
    if args.stats > 0:
        print(json.dumps(rolling_stats(args.stats), indent=2))
    else:
        n = _settle_pending()
        print(f"settled: {n}")
        if not args.once:
            start_thread()
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                stop()
