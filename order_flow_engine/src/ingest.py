"""
Real-time bar ingestion.

Two entry points:
  ingest_bar()  — single-bar push (webhook/IBKR/polling all land here)
  poll_yf()     — background worker that calls ingest_bar() on each poll

Flow for one bar:
  1. Append to per-(symbol,tf) tail buffer (last N bars kept in memory
     and mirrored to the parquet cache so restarts don't lose state).
  2. Rebuild features on the tail + higher-TF context.
  3. Run rule_engine + predictor on the newest bar only.
  4. Build alert, emit (threshold gate), append to alerts.jsonl, rewrite
     alerts.json tail.
  5. Publish alert to the in-process pub/sub so SSE subscribers get it.

Keeps the existing batch CLI intact — this module is purely additive.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market import data_fetcher
from order_flow_engine.src import (
    alert_engine,
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    label_generator,
    rule_engine,
)
from order_flow_engine.src import predictor
from utils.logger import setup_logger

logger = setup_logger(__name__)

# Rolling tail per (symbol, tf). Length covers enough bars to compute all
# rolling features (CVD-50, correlation-20, S/R-50, ATR-14 with ewm spin-up).
# 500 bars is overkill for every feature at 15m.
TAIL_LEN = 500

# Tails are keyed by (symbol, tf). Access is serialized via a single lock —
# ingest is low-throughput (one bar per TF per interval), not a hot path.
_tails: dict[tuple[str, str], deque[dict]] = defaultdict(lambda: deque(maxlen=TAIL_LEN))
_lock = threading.RLock()

# Subscribers for the SSE pub/sub. Each subscriber is a queue; the route
# iterates until the client disconnects.
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()

# Polling worker control.
_poll_thread: threading.Thread | None = None
_poll_stop = threading.Event()
_poll_state: dict = {"last_tick": None, "last_alert": None, "running": False}


# ── pub/sub ──────────────────────────────────────────────────────────────────

def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=100)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def _broadcast(event: dict) -> None:
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


# ── tail seeding + bar append ────────────────────────────────────────────────

def _seed_tail_from_cache(symbol: str, tf: str) -> None:
    """Lazy-seed the tail from the parquet cache on first touch."""
    key = (symbol, tf)
    if _tails[key]:
        return
    cache = data_loader._cache_path(symbol, tf)
    if not cache.exists():
        return
    try:
        df = pd.read_parquet(cache)
    except Exception as e:
        logger.warning(f"cache read for seed failed {cache}: {e}")
        return
    df = df.tail(TAIL_LEN)
    for ts, row in df.iterrows():
        bar = {
            "ts": pd.Timestamp(ts).tz_convert("UTC") if pd.Timestamp(ts).tzinfo
                   else pd.Timestamp(ts).tz_localize("UTC"),
            "Open":   float(row["Open"]),
            "High":   float(row["High"]),
            "Low":    float(row["Low"]),
            "Close":  float(row["Close"]),
            "Volume": float(row.get("Volume", 0) or 0),
        }
        if "buy_vol_real" in df.columns and pd.notna(row.get("buy_vol_real")):
            bar["buy_vol_real"]  = float(row["buy_vol_real"])
            bar["sell_vol_real"] = float(row["sell_vol_real"])
        _tails[key].append(bar)


def _tail_to_frame(symbol: str, tf: str) -> pd.DataFrame:
    key = (symbol, tf)
    rows = list(_tails[key])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    return df


# ── single-bar ingest ────────────────────────────────────────────────────────

def ingest_bar(
    *,
    symbol: str,
    timeframe: str,
    timestamp: Any,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 0.0,
    buy_vol: float | None = None,
    sell_vol: float | None = None,
) -> dict | None:
    """
    Accept a newly-closed bar, run rules + predictor, emit alert if any.

    If buy_vol and sell_vol are supplied (e.g. from the Binance aggTrade
    adapter where each trade carries an aggressor flag), they bypass the
    OHLCV proxy and the bar is treated as TRUE order flow. proxy_mode flips
    to False on the resulting alert.

    Returns the alert dict if one was emitted, else None.
    """
    with _lock:
        _seed_tail_from_cache(symbol, timeframe)
        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        bar_payload = {
            "ts": ts,
            "Open":   float(open_),
            "High":   float(high),
            "Low":    float(low),
            "Close":  float(close),
            "Volume": float(volume or 0),
        }
        if buy_vol is not None and sell_vol is not None:
            bar_payload["buy_vol_real"]  = float(buy_vol)
            bar_payload["sell_vol_real"] = float(sell_vol)

        key = (symbol, timeframe)
        tail = _tails[key]
        if tail and tail[-1]["ts"] == ts:
            tail[-1] = bar_payload
        else:
            tail.append(bar_payload)

        _poll_state["last_tick"] = ts.isoformat()

        # Build anchor features + higher-TF context from whatever tails we hold.
        anchor_df = _tail_to_frame(symbol, timeframe)
        if len(anchor_df) < 60:
            # Not enough history for stable features — just buffer.
            return None

        # If the latest bar carries true buy/sell volumes, this alert is NOT
        # proxy-mode. Older buffered bars may still be proxy — we mark per-alert.
        bar_is_proxy = "buy_vol_real" not in anchor_df.columns or \
                       pd.isna(anchor_df["buy_vol_real"].iloc[-1])

        multi_tf = {timeframe: anchor_df}
        # Best-effort pull of higher-TF tails for context. If they're empty
        # the join still works but columns will be NaN/filled.
        for other_tf in of_cfg.OF_TIMEFRAMES:
            if other_tf == timeframe:
                continue
            _seed_tail_from_cache(symbol, other_tf)
            other = _tail_to_frame(symbol, other_tf)
            if not other.empty:
                multi_tf[other_tf] = other

        featured = {tf: fe.build_features_for_tf(df, tf) for tf, df in multi_tf.items()}
        joined = fe.build_feature_matrix(featured, anchor_tf=timeframe)
        joined = rule_engine.apply_rules(joined)

        proxy_mode = bar_is_proxy

        # Pass 1 — confirmation rules (r1-r4) on the PRIOR bar. fwd_ret_1 for
        # bar t-1 becomes defined once bar t closes.
        confirm_alert = None
        if len(joined) >= 2:
            confirm_alert = _score_and_emit(
                joined=joined, row_idx=-2,
                allowed_rules=rule_engine.CONFIRMATION_RULES,
                pass_type="confirm",
                symbol=symbol, timeframe=timeframe,
                proxy_mode=proxy_mode, anchor_df=anchor_df,
            )

        # Pass 2 — causal rules (r5/r6/r7) on the newest bar.
        causal_alert = _score_and_emit(
            joined=joined, row_idx=-1,
            allowed_rules=rule_engine.CAUSAL_RULES,
            pass_type="causal",
            symbol=symbol, timeframe=timeframe,
            proxy_mode=proxy_mode, anchor_df=anchor_df,
        )

        # Prefer the fresher alert for the return value; both are persisted.
        return causal_alert if causal_alert is not None else confirm_alert


def _score_and_emit(
    *,
    joined: pd.DataFrame,
    row_idx: int,
    allowed_rules: list[str],
    pass_type: str,
    symbol: str,
    timeframe: str,
    proxy_mode: bool,
    anchor_df: pd.DataFrame,
) -> dict | None:
    """Score `joined.iloc[row_idx]` against `allowed_rules`, emit if gated."""
    row = joined.iloc[row_idx]
    ts = joined.index[row_idx]
    if not isinstance(ts, pd.Timestamp):
        ts = pd.Timestamp(ts)
    rules_fired = [c for c in allowed_rules if bool(row.get(c, False))]

    model_path = predictor._latest_model()
    pred_label = None
    confidence = 0
    model_payload: dict = {"version": None, "probas": {}}

    if model_path is not None and rules_fired:
        try:
            bundle = predictor._load_model(model_path)
            model = bundle["model"]
            feat_names = bundle["feature_names"]
            reverse_map = bundle.get("class_index_map", {
                i: i for i in range(len(of_cfg.LABEL_CLASSES))
            })
            X = joined.reindex(columns=feat_names).fillna(0.0).iloc[[row_idx]]
            dense = model.predict_proba(X.values)[0]
            full = np.zeros(len(of_cfg.LABEL_CLASSES))
            for di, oi in reverse_map.items():
                full[int(oi)] = dense[int(di)]
            pred_idx = int(full.argmax())
            pred_label = of_cfg.LABEL_CLASSES[pred_idx]
            confidence = predictor.blended_confidence(full, pred_label, row, proxy_mode)
            model_payload = {
                "version": model_path.stem,
                "probas": {c: round(float(full[i]), 4)
                           for i, c in enumerate(of_cfg.LABEL_CLASSES)},
            }
        except Exception as e:
            logger.warning(f"ingest model path failed ({e}); rule-only")

    if pred_label is None:
        if not rules_fired:
            return None
        pred_label = predictor._rule_only_label(row)
        # Per-pass confidence: count only rules allowed for this pass.
        confidence = min(100, 40 + 10 * len(rules_fired))

    if not alert_engine.should_emit(pred_label, confidence, tf=timeframe):
        return None

    if proxy_mode:
        recent_vol = anchor_df["Volume"].tail(of_cfg.VOLUME_GATE_WINDOW)
        if not alert_engine.volume_gate_passes(
            float(row.get("Volume", 0) or 0), recent_vol,
        ):
            return None

    alert = alert_engine.build_alert(
        timestamp=ts.to_pydatetime(),
        symbol=symbol,
        timeframe=timeframe,
        label=pred_label,
        confidence=confidence,
        price=float(row.get("Close", 0.0)),
        atr=float(row.get("atr", 0.0) or 0.0),
        rules_fired=rules_fired,
        metrics={
            "delta_ratio": row.get("delta_ratio"),
            "cvd_z":       row.get("cvd_z"),
            "clv":         row.get("clv"),
            "dist_to_recent_high_atr": row.get("dist_to_recent_high_atr"),
            "dist_to_recent_low_atr":  row.get("dist_to_recent_low_atr"),
        },
        model_info=model_payload,
        proxy_mode=proxy_mode,
        pass_type=pass_type,
    )

    emitted = alert_engine.emit(alert, output_dir=of_cfg.OF_OUTPUT_DIR)
    if emitted is None:
        return None
    _rewrite_consolidated()
    _poll_state["last_alert"] = alert
    _broadcast({"type": "alert", "alert": alert})
    logger.info(f"REALTIME alert: {pred_label} conf={confidence} {ts.isoformat()}")
    return alert


def _rewrite_consolidated(tail: int = 500) -> None:
    """Rewrite the consolidated alerts.json from the tail of alerts.jsonl."""
    jsonl = of_cfg.OF_OUTPUT_DIR / "alerts.jsonl"
    if not jsonl.exists():
        return
    lines = jsonl.read_text().splitlines()[-tail:]
    data = []
    for ln in lines:
        try:
            data.append(json.loads(ln))
        except Exception:
            continue
    alert_engine.write_consolidated(data, output_dir=of_cfg.OF_OUTPUT_DIR)


# ── polling worker ───────────────────────────────────────────────────────────

def _poll_once(symbol: str, timeframe: str) -> dict | None:
    """Pull a small yfinance window and ingest only the newest closed bar."""
    cap = of_cfg.YF_INTRADAY_CAPS.get(timeframe, 1)
    df = data_fetcher.fetch_intraday(symbol, timeframe, min(cap, 2))
    if df is None or df.empty:
        return None

    # Drop the last row if it represents an unclosed bar — yfinance returns
    # the in-progress bar for intraday intervals. Heuristic: if its timestamp
    # is within one bar-width of now, it's still forming.
    unit = timeframe[-1]
    unit_minutes = {"m": 1, "h": 60, "d": 1440}.get(unit)
    if unit_minutes is None:
        bar_min = 15
    else:
        try:
            bar_min = int(timeframe[:-1]) * unit_minutes
        except Exception:
            bar_min = 15
    now = pd.Timestamp.now(tz="UTC")
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts_utc = last_ts.tz_localize("UTC")
    else:
        last_ts_utc = last_ts.tz_convert("UTC")
    if (now - last_ts_utc).total_seconds() < bar_min * 60:
        df = df.iloc[:-1]
    if df.empty:
        return None

    last = df.iloc[-1]
    last_ts = df.index[-1]
    return ingest_bar(
        symbol=symbol, timeframe=timeframe, timestamp=last_ts,
        open_=float(last["Open"]), high=float(last["High"]),
        low=float(last["Low"]), close=float(last["Close"]),
        volume=float(last.get("Volume", 0) or 0),
    )


def _poll_loop(symbol: str, timeframe: str, interval_s: int):
    logger.info(f"poll worker started for {symbol}@{timeframe} every {interval_s}s")
    _poll_state["running"] = True
    try:
        while not _poll_stop.is_set():
            try:
                _poll_once(symbol, timeframe)
            except Exception as e:
                logger.warning(f"poll error: {e}")
            _poll_stop.wait(interval_s)
    finally:
        _poll_state["running"] = False
        logger.info("poll worker stopped")


def backfill_tail(
    symbol: str,
    timeframe: str,
    lookback_days: int | None = None,
) -> int:
    """
    Pre-load the in-memory tail by fetching historical OHLCV via yfinance
    and pushing each bar through the bar buffer (no scoring). Use this
    before starting a real-time adapter so the engine has enough context
    to score the very first live bar.
    """
    days = lookback_days or {"1m": 5, "5m": 30, "15m": 30,
                             "30m": 60, "1h": 60}.get(timeframe, 30)
    df = data_fetcher.fetch_intraday(symbol, timeframe, days)
    if df is None or df.empty:
        logger.warning(f"backfill: no data for {symbol}@{timeframe}")
        return 0
    with _lock:
        key = (symbol, timeframe)
        _tails[key].clear()
        for ts, row in df.iterrows():
            ts_p = pd.Timestamp(ts)
            if ts_p.tzinfo is None:
                ts_p = ts_p.tz_localize("UTC")
            else:
                ts_p = ts_p.tz_convert("UTC")
            _tails[key].append({
                "ts": ts_p,
                "Open":   float(row["Open"]),
                "High":   float(row["High"]),
                "Low":    float(row["Low"]),
                "Close":  float(row["Close"]),
                "Volume": float(row.get("Volume", 0) or 0),
            })
    logger.info(f"backfill: seeded {symbol}@{timeframe} with {len(df)} bars")
    return int(len(df))


def start_polling(
    symbol: str | None = None,
    timeframe: str | None = None,
    interval_s: int = 60,
) -> bool:
    """Start a background polling worker. Idempotent."""
    global _poll_thread
    if _poll_thread and _poll_thread.is_alive():
        return False
    _poll_stop.clear()
    _poll_thread = threading.Thread(
        target=_poll_loop,
        args=(symbol or of_cfg.OF_SYMBOL, timeframe or of_cfg.OF_ANCHOR_TF, interval_s),
        daemon=True,
    )
    _poll_thread.start()
    return True


def stop_polling() -> None:
    _poll_stop.set()


def poll_status() -> dict:
    return {
        "running":    bool(_poll_thread and _poll_thread.is_alive()),
        "last_tick":  _poll_state.get("last_tick"),
        "last_alert": (_poll_state.get("last_alert") or {}).get("timestamp_utc"),
        "subscribers": len(_subscribers),
    }
