"""
Databento Live SDK adapter — sub-second streaming of CME futures trades.

Replaces the polling path in `realtime_databento` for symbols that should
run on the Live tier. Aggregates trade prints into 1m OHLCV bars on the
fly (right-labeled, start-of-minute timestamp matching the Historical
convention) and pushes each closed bar through `ingest.ingest_bar`. Also
populates the same `_trade_tapes` ring buffer that the polling adapter uses,
so the existing `/api/futures/tape` endpoint stays unchanged.

Toggle via env: OF_DATABENTO_LIVE=1
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from order_flow_engine.src import alert_engine, ingest
from order_flow_engine.src import realtime_databento as rd
from utils.logger import setup_logger

logger = setup_logger(__name__)

_DATASET_DEFAULT = "GLBX.MDP3"

_lock = threading.Lock()
_thread: threading.Thread | None = None
_flush_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict = {
    "running": False, "subscriptions": [], "trades_seen": 0,
    "bars_emitted": 0, "last_record_ts": None,
}

# In-progress 1m bar accumulator: {symbol: {"ts","open","high","low","close",
# "volume","buy_vol","sell_vol"}}
_pending_bars: dict[str, dict] = {}
_pending_lock = threading.Lock()

# Per-symbol short-window trade buffer for sweep/block detection (last ~5s)
_tape_window: dict[str, list[dict]] = {}
_tape_alert_last: dict[tuple, float] = {}  # (sym, kind, side) -> last emit unix
_TAPE_ALERT_COOLDOWN_S = 45
_SWEEP_WINDOW_S = 1.0
_SWEEP_MIN_COUNT = 4
_BLOCK_LOOKBACK = 200    # trades for size baseline
_BLOCK_SIGMA    = 3.0

# Best bid/ask per symbol (from mbp-1 schema). Updated tick-by-tick.
_best_quote: dict[str, dict] = {}
_quote_lock = threading.Lock()


def get_best_quote(symbol: str) -> dict | None:
    with _quote_lock:
        return dict(_best_quote.get(symbol, {})) or None


# Iceberg state: per symbol → {(price_tick, size_bucket, side): [ts1, ts2, ...]}
_iceberg_state: dict[str, dict] = {}
_ICEBERG_WINDOW_S = 30.0
_ICEBERG_MIN_COUNT = 5
_ICEBERG_MIN_DURATION_S = 10.0
# CME tick sizes (front-month). Default 0.25 for unknown.
_TICK_SIZE = {
    "ESM6": 0.25, "ESH6": 0.25, "ESU6": 0.25,
    "NQM6": 0.25, "NQH6": 0.25,
    "GCM6": 0.10, "GCJ6": 0.10, "GCQ6": 0.10,
    "CLM6": 0.01, "CLN6": 0.01, "CLQ6": 0.01,
}


def _resolve_to_raw(symbols: list[str]) -> list[str]:
    out: list[str] = []
    client = rd._client()
    for s in symbols:
        if s.endswith(".FUT"):
            if not client:
                logger.warning(f"cannot resolve {s} — no Historical client for front-month")
                continue
            r = rd._resolve_front_month(client, s)
            if r:
                out.append(r)
        else:
            out.append(s)
    return out


def _trade_price(record) -> float:
    """Databento `trades` schema price → float. Library returns int64 fixed-point
    with 1e-9 precision; the `pretty_price` accessor exists in newer SDKs."""
    p = getattr(record, "pretty_price", None)
    if p is not None:
        return float(p)
    raw = float(getattr(record, "price", 0))
    # Fixed-point heuristic: anything above 1e6 is unrealistic as a futures
    # price → assume nano-fixed-point and divide.
    return raw / 1e9 if raw > 1e6 else raw


def _trade_side(record) -> str | None:
    """Aggressor side: 'buy' (lifted offer), 'sell' (hit bid), or None."""
    s = getattr(record, "side", None)
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("ascii", "ignore")
    if isinstance(s, int):
        s = chr(s)
    if s == "B":
        return "buy"
    if s == "A":
        return "sell"
    return None


def _ts_from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def _atr_from_tail(symbol: str, tf: str = "1m", n: int = 14) -> float:
    """ATR from in-memory tail. Returns 0 if not enough data."""
    try:
        bars = ingest.get_recent_bars(symbol, tf, n + 1)
    except Exception:
        return 0.0
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["High"]); lo = float(bars[i]["Low"])
        pc = float(bars[i-1]["Close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _emit_tape_alert(symbol: str, kind: str, side: str, ts: datetime,
                     price: float, metrics: dict) -> None:
    """Bypass standard `should_emit` gate (sweep/block aren't ML labels).
    Writes JSONL, broadcasts SSE, fans out to notifiers, feeds outcome_tracker."""
    key = (symbol, kind, side)
    now = time.time()
    last = _tape_alert_last.get(key, 0)
    if now - last < _TAPE_ALERT_COOLDOWN_S:
        return
    _tape_alert_last[key] = now
    label = f"{kind}_{side}"
    atr = _atr_from_tail(symbol)
    alert = alert_engine.build_alert(
        timestamp=ts, symbol=symbol, timeframe="1m", label=label,
        confidence=100, price=price, atr=atr,
        rules_fired=[f"live_tape_{kind}"], metrics=metrics,
        proxy_mode=False, pass_type="tape",
    )
    try:
        from order_flow_engine.src import alert_store
        alert_engine.append_jsonl(alert)
        try: alert_store.upsert(alert)
        except Exception: pass
        try:
            from order_flow_engine.src import notifier
            notifier.fanout(alert)
        except Exception: pass
        try:
            ingest._broadcast({"type": "alert", "alert": alert})
        except Exception: pass
        logger.info(f"TAPE ALERT: {label} {symbol} @ {price} {metrics}")
    except Exception as e:
        logger.warning(f"emit_tape_alert {symbol}/{kind}: {e}")


def _detect_iceberg(symbol: str, ts: datetime, price: float,
                    size: float, side: str) -> None:
    """Iceberg = repeated identical (price-tick, size, side) prints with
    regular gaps over ≥10s. Reset bucket on emit to avoid spam (cooldown
    also gates downstream)."""
    tick = _TICK_SIZE.get(symbol, 0.25)
    p_bin = round(price / tick) * tick
    s_bin = round(size)
    if s_bin <= 0:
        return
    key = (round(p_bin, 4), s_bin, side)
    state = _iceberg_state.setdefault(symbol, {})
    bucket = state.setdefault(key, [])
    bucket.append(ts.timestamp())
    cutoff = ts.timestamp() - _ICEBERG_WINDOW_S
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) < _ICEBERG_MIN_COUNT:
        return
    duration = bucket[-1] - bucket[0]
    if duration < _ICEBERG_MIN_DURATION_S:
        return
    gaps = [bucket[i] - bucket[i-1] for i in range(1, len(bucket))]
    if not gaps:
        return
    gaps_sorted = sorted(gaps)
    median = gaps_sorted[len(gaps_sorted) // 2] or 0.001
    gap_mean = sum(gaps) / len(gaps)
    gap_var = sum((g - gap_mean) ** 2 for g in gaps) / len(gaps)
    gap_sd = gap_var ** 0.5
    # Regularity: sd within 1.5x median = consistent reload pulse
    if gap_sd > max(2.0, median * 1.5):
        return
    _emit_tape_alert(
        symbol, "iceberg", side, ts, p_bin,
        metrics={
            "size": s_bin, "count": len(bucket),
            "duration_s": round(duration, 1),
            "gap_median_s": round(median, 2),
            "price": round(p_bin, 4),
        },
    )
    bucket.clear()


def _detect_sweep_block(symbol: str, ts: datetime, price: float,
                        size: float, side: str) -> None:
    """Append to per-symbol window, detect sweep / block, emit alerts."""
    win = _tape_window.setdefault(symbol, [])
    win.append({"ts": ts, "price": price, "size": size, "side": side})
    # Trim window to last 5s
    cutoff = ts.timestamp() - 5.0
    while win and win[0]["ts"].timestamp() < cutoff:
        win.pop(0)
    # SWEEP — last 1s window, count consecutive same-side from end
    sweep_cut = ts.timestamp() - _SWEEP_WINDOW_S
    last1 = [t for t in win if t["ts"].timestamp() >= sweep_cut]
    run_side = None; run_len = 0; max_run = 0; max_side = None
    for t in last1:
        if t["side"] == run_side:
            run_len += 1
        else:
            run_side = t["side"]; run_len = 1
        if run_len > max_run:
            max_run = run_len; max_side = run_side
    if max_run >= _SWEEP_MIN_COUNT and max_side:
        sweep_size = sum(t["size"] for t in last1 if t["side"] == max_side)
        _emit_tape_alert(
            symbol, "sweep", max_side, ts, price,
            metrics={"count": max_run, "vol": sweep_size, "window_s": _SWEEP_WINDOW_S},
        )
    # BLOCK — current trade size > mean+3sd of last N (use 5s window's sizes)
    if len(win) >= 30:
        sizes = [t["size"] for t in win[-_BLOCK_LOOKBACK:]]
        mean = sum(sizes) / len(sizes)
        var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
        sd = var ** 0.5 or 1.0
        thr = mean + _BLOCK_SIGMA * sd
        if size > thr and size > 5:  # absolute floor — avoids tiny-print spam
            _emit_tape_alert(
                symbol, "block", side, ts, price,
                metrics={"size": size, "mean": round(mean, 2),
                         "sd": round(sd, 2), "thr": round(thr, 2)},
            )


def _on_trade(symbol: str, ts: datetime, price: float, size: float, side: str | None) -> None:
    # Tape append (real-time)
    with rd._tape_lock:
        tape = rd._trade_tapes.setdefault(symbol, deque(maxlen=rd._TAPE_LEN))
        tape.append({
            "ts": ts.isoformat(), "price": price, "size": size,
            "side": side or "buy",
        })
        rd._last_tape_ts[symbol] = ts
    _state["trades_seen"] += 1
    _state["last_record_ts"] = ts.isoformat()

    # Sweep / block / iceberg detection — emits alerts via notifier + SSE
    try:
        _detect_sweep_block(symbol, ts, price, size, side or "buy")
    except Exception as e:
        logger.warning(f"sweep/block detect {symbol}: {e}")
    try:
        _detect_iceberg(symbol, ts, price, size, side or "buy")
    except Exception as e:
        logger.warning(f"iceberg detect {symbol}: {e}")

    # 1m bar aggregation — start-of-minute timestamp (matches Historical)
    bar_ts = ts.replace(second=0, microsecond=0)
    is_buy = (side != "sell")  # default to buy if None ('N')
    with _pending_lock:
        pb = _pending_bars.get(symbol)
        if pb is None or pb["ts"] != bar_ts:
            if pb is not None and pb["ts"] < bar_ts:
                _emit_locked(symbol, pb)
            _pending_bars[symbol] = {
                "ts": bar_ts, "open": price, "high": price, "low": price,
                "close": price, "volume": size,
                "buy_vol":  size if is_buy else 0.0,
                "sell_vol": 0.0 if is_buy else size,
            }
        else:
            if price > pb["high"]:
                pb["high"] = price
            if price < pb["low"]:
                pb["low"] = price
            pb["close"]   = price
            pb["volume"] += size
            if is_buy:
                pb["buy_vol"]  += size
            else:
                pb["sell_vol"] += size


def _emit_locked(symbol: str, pb: dict) -> None:
    """Caller holds _pending_lock."""
    try:
        ingest.ingest_bar(
            symbol=symbol, timeframe="1m", timestamp=pb["ts"],
            open_=pb["open"], high=pb["high"], low=pb["low"],
            close=pb["close"], volume=pb["volume"],
            buy_vol=pb["buy_vol"], sell_vol=pb["sell_vol"],
        )
        _state["bars_emitted"] += 1
    except Exception as e:
        logger.warning(f"live emit_bar {symbol}: {e}")


def _flush_loop() -> None:
    """Force-emit pending bars whose minute boundary has passed by >2s."""
    while not _stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            cutoff = (now.replace(second=0, microsecond=0) - timedelta(seconds=0))
            with _pending_lock:
                stale = [(sym, pb) for sym, pb in _pending_bars.items()
                         if pb["ts"] < cutoff and (now - pb["ts"]).total_seconds() > 62]
                for sym, pb in stale:
                    _emit_locked(sym, pb)
                    _pending_bars.pop(sym, None)
        except Exception as e:
            logger.warning(f"live flush err: {e}")
        for _ in range(5):
            if _stop.is_set():
                return
            time.sleep(1)


def _run_loop(symbols: list[str]) -> None:
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        logger.error("Live SDK aborted — no DATABENTO_API_KEY")
        _state["running"] = False
        return

    try:
        import databento as db
    except ImportError:
        logger.error("Live SDK aborted — databento package missing")
        _state["running"] = False
        return

    raw_syms = _resolve_to_raw(symbols)
    if not raw_syms:
        logger.error("Live SDK aborted — no resolvable symbols")
        _state["running"] = False
        return

    try:
        client = db.Live(key=key, reconnect_policy="reconnect")
    except Exception as e:
        logger.error(f"Live client init failed: {e}")
        _state["running"] = False
        return

    try:
        client.subscribe(
            dataset=_DATASET_DEFAULT, schema="trades",
            stype_in="raw_symbol", symbols=raw_syms,
        )
        # MBP-1: top-of-book bid/ask updates tick-by-tick
        try:
            client.subscribe(
                dataset=_DATASET_DEFAULT, schema="mbp-1",
                stype_in="raw_symbol", symbols=raw_syms,
            )
            logger.info(f"Live SDK MBP-1 subscribed for {raw_syms}")
        except Exception as e:
            logger.warning(f"MBP-1 subscribe failed (continuing trades-only): {e}")
        # NOTE: do NOT call client.start() — iteration drives streaming.
        _state["subscriptions"] = list(raw_syms)
        logger.info(f"Live SDK started: trades+mbp-1 · symbols={raw_syms}")
    except Exception as e:
        logger.error(f"Live subscribe failed: {e}")
        _state["running"] = False
        return

    sym_by_id: dict[int, str] = {}
    try:
        for record in client:
            if _stop.is_set():
                break
            try:
                _on_record(client, record, sym_by_id)
            except Exception as e:
                logger.warning(f"Live record err: {e}")
    finally:
        try:
            client.terminate()
        except Exception:
            pass
        _state["running"] = False
        logger.info("Live SDK stopped")


def _on_record(client, record, sym_by_id: dict[int, str]) -> None:
    rtype = type(record).__name__

    # Symbology mapping records first — instrument_id → raw_symbol
    if "SymbolMapping" in rtype:
        try:
            iid = getattr(record, "instrument_id", None)
            sym = getattr(record, "stype_out_symbol", None) or \
                  getattr(record, "stype_in_symbol", None)
            if iid is not None and sym:
                sym_by_id[int(iid)] = str(sym)
        except Exception:
            pass
        return

    # MBP-1 top-of-book: pull best bid/ask from `levels[0]`
    if "Mbp1" in rtype or "MBP1" in rtype.upper():
        try:
            iid = getattr(record, "instrument_id", None)
            sym = sym_by_id.get(int(iid)) if iid is not None else None
            if not sym:
                return
            levels = getattr(record, "levels", None) or []
            if not levels:
                return
            lvl0 = levels[0]
            bid_px = getattr(lvl0, "bid_px", None) or getattr(lvl0, "bid_price", None)
            ask_px = getattr(lvl0, "ask_px", None) or getattr(lvl0, "ask_price", None)
            bid_sz = getattr(lvl0, "bid_sz", None) or getattr(lvl0, "bid_size", None)
            ask_sz = getattr(lvl0, "ask_sz", None) or getattr(lvl0, "ask_size", None)
            if bid_px is None or ask_px is None:
                return
            # Databento price scaling: int64 × 1e-9
            bid_px = float(bid_px) / 1e9 if float(bid_px) > 1e6 else float(bid_px)
            ask_px = float(ask_px) / 1e9 if float(ask_px) > 1e6 else float(ask_px)
            ts_ns = getattr(record, "ts_event", None) or getattr(record, "ts_recv", None)
            with _quote_lock:
                _best_quote[sym] = {
                    "bid":     round(bid_px, 4),
                    "ask":     round(ask_px, 4),
                    "bid_sz":  int(bid_sz or 0),
                    "ask_sz":  int(ask_sz or 0),
                    "spread":  round(ask_px - bid_px, 4),
                    "ts":      _ts_from_ns(int(ts_ns)).isoformat() if ts_ns else None,
                }
        except Exception:
            pass
        return

    # Skip non-trade records (mbo, system, error, etc.)
    if not (hasattr(record, "price") and hasattr(record, "size")):
        return

    iid = getattr(record, "instrument_id", None)
    if iid is None:
        return
    sym = sym_by_id.get(int(iid))
    if not sym:
        try:
            sym_by_id.update({int(k): str(v) for k, v in
                              (client.symbology_map or {}).items()})
        except Exception:
            pass
        sym = sym_by_id.get(int(iid))
        if not sym:
            return

    ts_ns = getattr(record, "ts_event", None) or getattr(record, "ts_recv", None)
    if ts_ns is None:
        return
    ts = _ts_from_ns(int(ts_ns))
    price = _trade_price(record)
    size  = float(getattr(record, "size", 0) or 0)
    if size <= 0 or price <= 0:
        return
    side = _trade_side(record)
    _on_trade(sym, ts, price, size, side)


def _backfill_async(symbols: list[str]) -> None:
    """Run Historical rest_backfill for all (sym, tf) pairs off the boot path.
    Live forward streaming starts immediately; tail fills in the background."""
    client = rd._client()
    for sym in symbols:
        raw = sym
        if sym.endswith(".FUT") and client:
            raw = rd._resolve_front_month(client, sym) or sym
        if raw.endswith(".FUT"):
            continue
        for tf in ("1m", "15m"):
            try:
                rd.rest_backfill(raw, tf)
            except Exception as e:
                logger.warning(f"Live backfill {raw}@{tf}: {e}")


def start(symbols: list[str], backfill: bool = True) -> bool:
    """Start the Live SDK consumer. Backfills tails via Historical in
    background — Flask boot is not blocked."""
    global _thread, _flush_thread
    with _lock:
        if _thread and _thread.is_alive():
            logger.info("Live SDK already running")
            return False
        if backfill:
            threading.Thread(
                target=_backfill_async, args=(symbols,),
                daemon=True, name="databento-live-backfill",
            ).start()
        _stop.clear()
        _state["running"] = True
        _thread = threading.Thread(
            target=_run_loop, args=(symbols,),
            daemon=True, name="databento-live",
        )
        _flush_thread = threading.Thread(
            target=_flush_loop, daemon=True, name="databento-live-flush",
        )
        _thread.start()
        _flush_thread.start()
    return True


def stop() -> None:
    _stop.set()


def status() -> dict:
    out = dict(_state)
    out["pending_bars"] = list(_pending_bars.keys())
    return out
