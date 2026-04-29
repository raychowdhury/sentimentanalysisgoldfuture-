"""
Databento intraday adapter — feeds CME futures (GC gold, ES, CL, etc.)
into the order-flow ingest pipeline.

Approach: poll Databento Historical `ohlcv-1m` (or 5m/15m) on a fixed
cadence, dedupe by timestamp, and push each newly-sealed bar to
`ingest.ingest_bar`. The Historical API has a typical 1-5 min data lag,
so we always pull a small window ending a couple minutes back and skip
the newest bar until it has settled.

Why poll instead of Live: simpler, stateless, and 1m bars are cents per
fetch. Live SDK can replace this later if sub-minute latency is needed.

Symbol convention: `raw_symbol` stype with the explicit front-month
contract (e.g. `GCM6`). `continuous/GC.c.0` was tested and emits very
sparse bars under thin overnight liquidity. `parent/GC.FUT` returns
every contract+spread and would need post-fetch filtering.

CLI:
    python -m order_flow_engine.src.realtime_databento --symbol GCM6 --tf 1m
"""

from __future__ import annotations

import argparse
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pandas as pd

from order_flow_engine.src import ingest
from utils.logger import setup_logger

logger = setup_logger(__name__)

_DATASET_DEFAULT = "GLBX.MDP3"
_TF_TO_SCHEMA = {"1m": "ohlcv-1m", "5m": "ohlcv-1m", "15m": "ohlcv-1m",
                 "30m": "ohlcv-1m", "1h": "ohlcv-1h"}
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

# Skip bars newer than this many seconds — Databento Historical lag.
# Empirically ~5-7 min for GLBX.MDP3 ohlcv on the standard tier, but trades
# schema typically settles in 60-120s. Tunable via env.
_SETTLE_LAG_S        = int(os.getenv("OF_DATABENTO_SETTLE_LAG_S", "120"))
_SETTLE_LAG_TRADES_S = int(os.getenv("OF_DATABENTO_TRADES_LAG_S", "90"))

# Opt-in: also fetch the `trades` schema and classify aggressor side via tick
# rule, then pass real buy_vol/sell_vol into ingest. Lifts the engine out of
# proxy_mode for these contracts. Off by default — adds Databento bandwidth.
_REAL_FLOW = bool(int(os.getenv("OF_DATABENTO_REAL_FLOW", "0")))

# Per-feed state, keyed by f"{symbol}@{tf}" so multiple symbols run in parallel.
_states: dict[str, dict] = {}
_threads: dict[str, threading.Thread] = {}
_stops: dict[str, threading.Event] = {}
_lock = threading.Lock()

# Live trade tape — last N raw prints per symbol. Filled as a side-effect of
# `_fetch_real_flow` so no extra Databento bandwidth is used.
_TAPE_LEN = 5000
_trade_tapes: dict[str, deque] = {}
_last_tape_ts: dict[str, pd.Timestamp] = {}
_tape_lock = threading.Lock()


def get_tape(symbol: str, n: int = 50) -> list[dict]:
    with _tape_lock:
        tape = _trade_tapes.get(symbol)
        if not tape:
            return []
        n = max(1, min(int(n), len(tape)))
        return list(tape)[-n:]


def _key(symbol: str, tf: str) -> str:
    return f"{symbol}@{tf}"


def _client():
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        return None
    try:
        import databento as db
    except ImportError:
        return None
    return db.Historical(key)


def _resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample 1m bars to higher TF if needed."""
    if tf == "1m" or df.empty:
        return df
    rule = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[tf]
    out = df.resample(rule, label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return out


_AVAIL_END_RE = re.compile(r"available up to '([^']+)'")
# Single CME contract code: root (1-3 letters) + month (FGHJKMNQUVXZ) + year digit(s).
# Excludes spreads (contain `-`) and continuous tokens (contain `.`).
_RAW_CONTRACT_RE = re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$")

# 24-hour cache: parent_symbol -> (raw_symbol, expires_at)
_front_cache: dict[str, tuple[str, float]] = {}
_FRONT_TTL_S = 24 * 3600


def _resolve_front_month(client, parent: str, dataset: str = _DATASET_DEFAULT) -> str | None:
    """
    Resolve a Databento parent symbol (e.g. `ES.FUT`, `GC.FUT`) to the most
    actively-traded single contract over the last few hours of data.

    Cached for 24h per parent.
    """
    now = time.time()
    cached = _front_cache.get(parent)
    if cached and cached[1] > now:
        return cached[0]

    end = datetime.now(timezone.utc) - timedelta(seconds=_SETTLE_LAG_S)
    start = end - timedelta(hours=4)

    def _do(s, e):
        return client.timeseries.get_range(
            dataset=dataset, symbols=parent, stype_in="parent",
            schema="ohlcv-1m", start=s.isoformat(), end=e.isoformat(),
        ).to_df()

    try:
        df = _do(start, end)
    except Exception as e:
        m = _AVAIL_END_RE.search(str(e))
        if not m:
            logger.warning(f"front-month resolve {parent}: {e}")
            return None
        try:
            avail_end = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
            df = _do(avail_end - timedelta(hours=4), avail_end)
        except Exception as e2:
            logger.warning(f"front-month resolve retry {parent}: {e2}")
            return None

    if df is None or df.empty:
        return None

    single = df[df["symbol"].astype(str).str.match(_RAW_CONTRACT_RE)]
    if single.empty:
        return None
    top = single.groupby("symbol")["volume"].sum().sort_values(ascending=False)
    raw = str(top.index[0])
    _front_cache[parent] = (raw, now + _FRONT_TTL_S)
    logger.info(f"front-month resolved: {parent} -> {raw} (vol={int(top.iloc[0])})")
    return raw


def _fetch_window(client, symbol: str, tf: str,
                  lookback_min: int) -> pd.DataFrame:
    end = datetime.now(timezone.utc) - timedelta(seconds=_SETTLE_LAG_S)
    start = end - timedelta(minutes=lookback_min)
    schema = _TF_TO_SCHEMA[tf]

    def _do_fetch(s, e):
        data = client.timeseries.get_range(
            dataset=_DATASET_DEFAULT, symbols=symbol, stype_in="raw_symbol",
            schema=schema, start=s.isoformat(), end=e.isoformat(),
        )
        return data.to_df()

    try:
        df = _do_fetch(start, end)
    except Exception as e:
        # Dataset cutoff (overnight maintenance, post-close batch lag, etc.)
        # — extract available_end from the 422 message and retry once.
        m = _AVAIL_END_RE.search(str(e))
        if m:
            try:
                avail_end = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
                if avail_end <= start:
                    start = avail_end - timedelta(minutes=lookback_min)
                df = _do_fetch(start, avail_end)
            except Exception as e2:
                logger.warning(f"Databento retry {symbol}@{tf}: {e2}")
                return pd.DataFrame()
        else:
            logger.warning(f"Databento fetch {symbol}@{tf}: {e}")
            return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    df.index = pd.to_datetime(df.index).tz_convert("UTC")
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    return _resample(df, tf)


def _fetch_real_flow(
    client, symbol: str, tf: str,
    start: datetime, end: datetime,
) -> pd.DataFrame | None:
    """
    Pull `trades` schema for the window, apply Lee–Ready tick rule, aggregate
    aggressor-side volume per bar of the requested TF.

    Returns a DataFrame indexed at the bar boundary with columns
    `buy_vol_real`, `sell_vol_real`. None on error / empty.
    """
    schema = "trades"
    try:
        data = client.timeseries.get_range(
            dataset=_DATASET_DEFAULT, symbols=symbol, stype_in="raw_symbol",
            schema=schema, start=start.isoformat(), end=end.isoformat(),
        )
        df = data.to_df()
    except Exception as e:
        m = _AVAIL_END_RE.search(str(e))
        if not m:
            logger.warning(f"Databento trades {symbol}: {e}")
            return None
        try:
            avail = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
            data = client.timeseries.get_range(
                dataset=_DATASET_DEFAULT, symbols=symbol, stype_in="raw_symbol",
                schema=schema, start=start.isoformat(), end=avail.isoformat(),
            )
            df = data.to_df()
        except Exception as e2:
            logger.warning(f"Databento trades retry {symbol}: {e2}")
            return None
    if df is None or df.empty:
        return None

    # Tick rule: aggressor side = sign(price - prev_price); 0 carries forward.
    df = df[["price", "size"]].copy()
    df["price"] = df["price"].astype(float)
    df["size"]  = df["size"].astype(float)
    diff = df["price"].diff()
    sign = pd.Series(0, index=df.index, dtype=int)
    sign[diff > 0] = +1
    sign[diff < 0] = -1
    sign = sign.replace(0, pd.NA).ffill().fillna(+1).astype(int)
    df["buy_vol_real"]  = (df["size"] * (sign == +1)).astype(float)
    df["sell_vol_real"] = (df["size"] * (sign == -1)).astype(float)

    # Side-effect: populate live trade tape with new prints (dedup by ts).
    df.index = pd.to_datetime(df.index).tz_convert("UTC")
    with _tape_lock:
        last_seen = _last_tape_ts.get(symbol)
        new_df = df[df.index > last_seen] if last_seen is not None else df
        if not new_df.empty:
            tape = _trade_tapes.setdefault(symbol, deque(maxlen=_TAPE_LEN))
            for ts, row in new_df.iterrows():
                tape.append({
                    "ts": ts.isoformat(),
                    "price": float(row["price"]),
                    "size":  float(row["size"]),
                    "side":  "buy" if row["buy_vol_real"] > 0 else "sell",
                })
            _last_tape_ts[symbol] = new_df.index[-1]

    # Resample to the requested TF using right-closed/right-labeled bars
    # (matches `_resample` in this module).
    rule = {"1m": "1min", "5m": "5min", "15m": "15min",
            "30m": "30min", "1h": "1h"}.get(tf)
    if rule is None:
        return None
    out = df[["buy_vol_real", "sell_vol_real"]].resample(
        rule, label="right", closed="right",
    ).sum()
    out = out[(out["buy_vol_real"] > 0) | (out["sell_vol_real"] > 0)]
    return out


def rest_backfill(symbol: str, tf: str, lookback_min: int = 600) -> int:
    """Pull recent bars and seed the engine tail. Returns rows seeded."""
    client = _client()
    if client is None:
        logger.warning("Databento backfill skipped — no key/package")
        return 0
    df = _fetch_window(client, symbol, tf, lookback_min)
    if df.empty:
        return 0
    out = pd.DataFrame({
        "Open":   df["open"].astype(float),
        "High":   df["high"].astype(float),
        "Low":    df["low"].astype(float),
        "Close":  df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    }, index=df.index)

    if _REAL_FLOW:
        end = datetime.now(timezone.utc) - timedelta(seconds=_SETTLE_LAG_S)
        start = end - timedelta(minutes=lookback_min)
        flow = _fetch_real_flow(client, symbol, tf, start, end)
        if flow is not None and not flow.empty:
            out = out.join(flow, how="left")
            n_real = out["buy_vol_real"].notna().sum() if "buy_vol_real" in out.columns else 0
            logger.info(
                f"  Databento {symbol}@{tf}: {n_real}/{len(out)} bars "
                f"with real flow (tick-rule)"
            )

    return ingest.backfill_tail(symbol, tf, df=out)


def _poll_loop(symbol: str, tf: str, do_backfill: bool = False) -> None:
    k = _key(symbol, tf)
    state = _states[k]
    stop_event = _stops[k]
    client = _client()
    if client is None:
        logger.error(f"Databento poll {k} aborted — DATABENTO_API_KEY missing")
        state["running"] = False
        return

    interval = _TF_SECONDS[tf]
    window_min = max(30, interval // 60 * 5)

    if do_backfill:
        try:
            n = rest_backfill(symbol, tf)
            if n == 0:
                logger.warning(f"Databento backfill empty for {k}")
        except Exception as e:
            logger.warning(f"Databento backfill failed for {k}: {e}")

    logger.info(
        f"Databento poll started: {symbol}@{tf} every {interval}s"
        f"{' (real flow)' if _REAL_FLOW else ' (proxy flow)'}"
    )
    while not stop_event.is_set():
        df = _fetch_window(client, symbol, tf, window_min)
        if not df.empty:
            last_ts = state["last_ts"]
            new_rows = df[df.index > last_ts] if last_ts is not None else df

            real_flow = None
            if _REAL_FLOW and len(new_rows):
                end = datetime.now(timezone.utc) - timedelta(seconds=_SETTLE_LAG_S)
                start_w = new_rows.index[0].to_pydatetime() - timedelta(minutes=2)
                real_flow = _fetch_real_flow(client, symbol, tf, start_w, end)

            for ts, row in new_rows.iterrows():
                kwargs = dict(
                    symbol=symbol, timeframe=tf, timestamp=ts,
                    open_=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                if real_flow is not None and ts in real_flow.index:
                    rf = real_flow.loc[ts]
                    kwargs["buy_vol"]  = float(rf["buy_vol_real"])
                    kwargs["sell_vol"] = float(rf["sell_vol_real"])
                ingest.ingest_bar(**kwargs)
                state["last_ts"] = ts
                state["bars_ingested"] += 1
            if len(new_rows):
                tag = ""
                if real_flow is not None:
                    matched = sum(1 for ts in new_rows.index if ts in real_flow.index)
                    tag = f" (real-flow {matched}/{len(new_rows)})"
                logger.info(
                    f"Databento {symbol}@{tf}: ingested {len(new_rows)} "
                    f"new bars, latest {state['last_ts']}{tag}"
                )
        # interruptible sleep
        for _ in range(interval):
            if stop_event.is_set():
                break
            time.sleep(1)

    logger.info(f"Databento poll stopped: {symbol}@{tf}")
    state["running"] = False


def start_thread(symbol: str, tf: str, backfill: bool = True) -> bool:
    """
    Start a poll thread for one (symbol, tf). Returns True if started.

    `symbol` accepts either a raw contract (`ESM6`) or a parent token ending
    in `.FUT` (`ES.FUT`, `GC.FUT`) which is auto-resolved to the most-active
    front-month contract.

    Multi-symbol is supported — call repeatedly with different symbols and
    each gets its own thread + state bucket.
    """
    if tf not in _TF_SECONDS:
        logger.error(f"unsupported tf {tf!r} for Databento adapter")
        return False

    if symbol.endswith(".FUT"):
        client = _client()
        if client is None:
            logger.error("front-month resolve aborted — no Databento key/pkg")
            return False
        resolved = _resolve_front_month(client, symbol)
        if not resolved:
            logger.error(f"could not resolve front-month for {symbol}")
            return False
        symbol = resolved

    k = _key(symbol, tf)
    with _lock:
        existing = _threads.get(k)
        if existing and existing.is_alive():
            logger.info(f"Databento thread {k} already running; skipping")
            return False

        _states[k] = {"running": True, "symbol": symbol, "tf": tf,
                      "last_ts": None, "bars_ingested": 0}
        _stops[k] = threading.Event()
        t = threading.Thread(target=_poll_loop, args=(symbol, tf, backfill),
                             daemon=True, name=f"databento-{k}")
        _threads[k] = t
        t.start()
    return True


def start_many(specs: list[tuple[str, str]], backfill: bool = True) -> int:
    """Start multiple feeds. specs = [(symbol, tf), ...]. Returns count started."""
    started = 0
    for sym, tf in specs:
        if start_thread(sym, tf, backfill=backfill):
            started += 1
    return started


def stop_thread(symbol: str | None = None, tf: str | None = None) -> None:
    """Stop one feed (symbol+tf) or all feeds if both args omitted."""
    with _lock:
        if symbol is None and tf is None:
            for ev in _stops.values():
                ev.set()
            return
        if symbol and tf:
            ev = _stops.get(_key(symbol, tf))
            if ev:
                ev.set()


def status() -> dict:
    """Aggregate status across all running feeds."""
    feeds = []
    for k, st in _states.items():
        feeds.append({
            "key": k,
            "running": bool(st.get("running")) and bool(
                _threads.get(k) and _threads[k].is_alive()),
            "symbol": st.get("symbol"),
            "tf": st.get("tf"),
            "last_ts": str(st["last_ts"]) if st.get("last_ts") is not None else None,
            "bars_ingested": int(st.get("bars_ingested") or 0),
        })
    any_running = any(f["running"] for f in feeds)
    primary = next((f for f in feeds if f["running"]), feeds[0] if feeds else None)
    out = {
        "running": any_running,
        "feeds": feeds,
        # Back-compat single-feed view (first running, else first known).
        "symbol":        primary["symbol"]        if primary else None,
        "tf":            primary["tf"]            if primary else None,
        "last_ts":       primary["last_ts"]       if primary else None,
        "bars_ingested": primary["bars_ingested"] if primary else 0,
    }
    return out


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="ES.FUT",
                    help="comma-separated symbols, e.g. ES.FUT,GC.FUT,CLM6")
    ap.add_argument("--tf", default="1m")
    args = ap.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start_many([(s, args.tf) for s in syms])
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        stop_thread()
