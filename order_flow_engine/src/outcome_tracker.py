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
import re
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

# CME single-contract pattern (e.g. ESM6, GCM6) — route to Databento.
_FUT_RAW_RE = re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$")
# yfinance futures symbol (e.g. ES=F). For these we can back-resolve the
# alert-time front-month contract via Databento parent volume.
_YF_FUT_RE = re.compile(r"^([A-Z]{1,3})=F$")
# yfinance root -> Databento parent token + dataset.
_YF_ROOT_TO_PARENT: dict[str, tuple[str, str]] = {
    "ES": ("ES.FUT", "GLBX.MDP3"),
    "NQ": ("NQ.FUT", "GLBX.MDP3"),
    "GC": ("GC.FUT", "GLBX.MDP3"),
    "SI": ("SI.FUT", "GLBX.MDP3"),
    "CL": ("CL.FUT", "GLBX.MDP3"),
    "ZN": ("ZN.FUT", "GLBX.MDP3"),
    "ZB": ("ZB.FUT", "GLBX.MDP3"),
    "YM": ("YM.FUT", "GLBX.MDP3"),
    "RTY": ("RTY.FUT", "GLBX.MDP3"),
}
_RAW_CONTRACT_RE = re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$")
# Per-alert-date resolution cache: (parent, date_iso) -> raw_contract
_alert_front_cache: dict[tuple[str, str], str] = {}
# Symbols/dates we've logged a "no resolve" message for, to avoid spam.
_skip_logged: set[str] = set()


def _resolve_front_at(parent: str, dataset: str, on_date) -> str | None:
    """
    Resolve the most-active outright contract for `parent` on a given date.

    Uses ohlcv-1d on the parent symbol for a 1-day window centered on the
    alert date and picks the single contract (no `-` spread, raw-contract
    regex match) with the highest volume.
    """
    import pandas as _pd
    key = (parent, on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date))
    cached = _alert_front_cache.get(key)
    if cached:
        return cached

    key_db = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key_db:
        return None
    try:
        import databento as _db
    except ImportError:
        return None
    client = _db.Historical(key_db)

    start = on_date - timedelta(days=1)
    end = on_date + timedelta(days=1)
    try:
        df = client.timeseries.get_range(
            dataset=dataset, symbols=parent, stype_in="parent",
            schema="ohlcv-1d",
            start=start.isoformat(), end=end.isoformat(),
        ).to_df()
    except Exception as e:
        logger.warning(f"front-month back-resolve {parent}@{on_date}: {e}")
        return None
    if df is None or df.empty or "symbol" not in df.columns:
        return None
    single = df[df["symbol"].astype(str).str.match(_RAW_CONTRACT_RE)]
    if single.empty:
        return None
    raw = str(single.groupby("symbol")["volume"].sum().idxmax())
    _alert_front_cache[key] = raw
    return raw

# Forward horizons (minutes) at which to record return + extremes.
HORIZONS_MIN: tuple[int, ...] = (60, 240)

# Win/loss thresholds in ATR multiples.
TARGET_ATR: float = 1.0
STOP_ATR:   float = 1.0

# Long-bias labels (alert implies buyer-favored move ahead).
LONG_LABELS = {
    "buyer_absorption", "bullish_trap", "possible_reversal",
    "sweep_buy", "block_buy", "iceberg_buy",
}
SHORT_LABELS = {
    "seller_absorption", "bearish_trap",
    "sweep_sell", "block_sell", "iceberg_sell",
}

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


def _fetch_bars_databento(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Pull 1m OHLCV for a CME contract from Databento Historical."""
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set")
    try:
        import databento as db
    except ImportError:
        raise RuntimeError("databento package not installed")
    client = db.Historical(key)
    data = client.timeseries.get_range(
        dataset="GLBX.MDP3", symbols=symbol, stype_in="raw_symbol",
        schema="ohlcv-1m",
        start=start.isoformat(), end=end.isoformat(),
    )
    df = data.to_df()
    if df is None or df.empty:
        return pd.DataFrame()
    df.index = pd.to_datetime(df.index).tz_convert("UTC")
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                            "close": "Close", "volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def _fetch_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    # Route by symbol shape so futures don't get slammed against Alpaca.
    if _FUT_RAW_RE.match(symbol):
        return _fetch_bars_databento(symbol, start, end)
    m = _YF_FUT_RE.match(symbol)
    if m:
        root = m.group(1)
        parent_info = _YF_ROOT_TO_PARENT.get(root)
        if not parent_info:
            return pd.DataFrame()
        parent, dataset = parent_info
        # Back-resolve the contract that was front-month on the alert date.
        on_date = start.date()
        raw = _resolve_front_at(parent, dataset, on_date)
        if not raw:
            return pd.DataFrame()
        return _fetch_bars_databento(raw, start, end)

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
    skipped_unsupported = 0
    OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTCOMES_PATH.open("a") as f:
        for a in pending:
            sym = a.get("symbol", "")
            aid = a.get("id", "")
            try:
                ts = pd.Timestamp(a["timestamp_utc"])
                start = ts - pd.Timedelta(minutes=5)
                end   = ts + pd.Timedelta(minutes=max(HORIZONS_MIN) + 30)
                bars = _fetch_bars(sym, start.to_pydatetime(), end.to_pydatetime())
                outcome = _compute_outcome(a, bars)
                if outcome is None:
                    continue
                f.write(json.dumps(outcome) + "\n")
                written += 1
            except Exception as e:
                logger.warning(f"outcome compute failed {aid}: {e}")
    if written:
        logger.info(f"outcome_tracker: settled {written} alerts")
    if skipped_unsupported:
        logger.info(
            f"outcome_tracker: skipped {skipped_unsupported} legacy futures alerts"
        )
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


def stats_by_label(window_days: int = 30) -> dict:
    """Group outcomes by label, compute per-detector win rate / expectancy / R-curve."""
    rows = _load_jsonl(OUTCOMES_PATH)
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

    by_label: dict[str, list] = {}
    for r in keep:
        by_label.setdefault(r.get("label", "?"), []).append(r)

    out = {"window_days": window_days, "labels": {}}
    for label, group in by_label.items():
        wins   = sum(1 for r in group if r["outcome"] == "win")
        losses = sum(1 for r in group if r["outcome"] == "loss")
        neutral = sum(1 for r in group if r["outcome"] == "neutral")
        decisive = wins + losses
        win_rate = wins / decisive if decisive else 0.0
        exp = sum(
            TARGET_ATR if r["outcome"] == "win" else
            -STOP_ATR  if r["outcome"] == "loss" else
            r.get("max_fav_atr", 0) + r.get("max_adv_atr", 0)
            for r in group
        ) / len(group) if group else 0
        out["labels"][label] = {
            "count": len(group), "wins": wins, "losses": losses, "neutral": neutral,
            "win_rate": round(win_rate, 4),
            "expectancy_atr": round(exp, 4),
            "total_R": round(wins * TARGET_ATR - losses * STOP_ATR, 2),
        }
    return out


def equity_curve(window_days: int = 30) -> dict:
    """Cumulative R-multiple curve assuming 1R risk per trade. Win=+TARGET_ATR,
    loss=-STOP_ATR, neutral=close-to-close in ATR."""
    rows = _load_jsonl(OUTCOMES_PATH)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    series: list[dict] = []
    cum = 0.0
    for r in rows:
        try:
            ts = pd.Timestamp(r["alert_ts"]).to_pydatetime()
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        if r["outcome"] == "win":
            r_pnl = TARGET_ATR
        elif r["outcome"] == "loss":
            r_pnl = -STOP_ATR
        else:
            r_pnl = float(r.get("max_fav_atr", 0)) + float(r.get("max_adv_atr", 0))
        cum += r_pnl
        series.append({
            "ts":    r["alert_ts"],
            "label": r.get("label"),
            "sym":   r.get("symbol"),
            "r":     round(r_pnl, 4),
            "cum":   round(cum, 4),
            "outcome": r["outcome"],
        })
    return {"window_days": window_days, "trades": series, "final_R": round(cum, 4)}


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
