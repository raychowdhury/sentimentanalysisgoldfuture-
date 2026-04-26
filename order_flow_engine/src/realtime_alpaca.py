"""
Alpaca SPY/QQQ aggTrade adapter — FREE real-time US-equity flow.

Why this works for the engine:

  Alpaca's free-tier IEX feed streams every trade printed on IEX (≈2-3% of
  total SPY volume). Each trade carries price + size + timestamp but NO
  aggressor flag. We classify side via the Lee-Ready (1991) tick rule:

      sign = +1 if trade.price > prev_price   (buyer crossed)
            -1 if trade.price < prev_price   (seller crossed)
             0 if equal — split 50/50 (paper convention)

  Tick rule is the standard academic baseline before bid/ask classification.
  Accuracy ~75% on liquid names (Lee & Ready 1991) — much better than CLV
  candle-shape proxies for this purpose. SPY tracks ES futures >95%
  correlated, so flow signals translate.

  Endpoint:  wss://stream.data.alpaca.markets/v2/iex
  Auth:      {"action":"auth","key":"...","secret":"..."}
  Subscribe: {"action":"subscribe","trades":["SPY","QQQ"]}

  Free tier: IEX feed only (no SIP). Cache hits during market hours
  09:30-16:00 ET. Outside RTH the stream is silent.

CLI:
    python -m order_flow_engine.src.realtime_alpaca --symbol SPY --tf 1m

Symbols suitable for the engine (high IEX print rate):
    SPY  QQQ  IWM  DIA  AAPL  TSLA  NVDA  AMZN
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from order_flow_engine.src import config as of_cfg, ingest
from utils.logger import setup_logger

logger = setup_logger(__name__)

WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
REST_BASE = "https://data.alpaca.markets/v2/stocks"

TF_SECONDS = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
}


class TickRuleAggregator:
    """
    Aggregates Alpaca trades into bars; classifies side via tick rule.
    Holds last_price across messages; resets per symbol.
    """

    def __init__(self, symbol: str, tf: str):
        self.symbol = symbol
        self.tf = tf
        self.bar_seconds = TF_SECONDS.get(tf)
        if self.bar_seconds is None:
            raise ValueError(f"Unsupported tf: {tf}")
        self.bucket: int | None = None
        self.o = self.h = self.l = self.c = None
        self.v = self.buy_v = self.sell_v = 0.0
        self.last_price: float | None = None
        self.lock = threading.Lock()

    def add_trade(self, ts_seconds: float, price: float, size: float):
        bucket = int(ts_seconds) - (int(ts_seconds) % self.bar_seconds)
        with self.lock:
            if self.bucket is None:
                self._open_bucket(bucket, price)
            elif bucket != self.bucket:
                self._close_and_ship()
                self._open_bucket(bucket, price)

            self.h = max(self.h, price)
            self.l = min(self.l, price)
            self.c = price
            self.v += size

            # Lee-Ready tick rule
            if self.last_price is None or price > self.last_price:
                self.buy_v += size
            elif price < self.last_price:
                self.sell_v += size
            else:
                # zero-tick → split (Lee-Ready uses prior tick direction;
                # 50/50 is a safe approximation when we lack prior context)
                self.buy_v  += size / 2
                self.sell_v += size / 2
            self.last_price = price

    def _open_bucket(self, bucket: int, price: float) -> None:
        self.bucket = bucket
        self.o = self.h = self.l = self.c = price
        self.v = self.buy_v = self.sell_v = 0.0

    def _close_and_ship(self) -> None:
        if self.bucket is None or self.o is None:
            return
        ts = datetime.fromtimestamp(self.bucket, tz=timezone.utc)
        try:
            ingest.ingest_bar(
                symbol=self.symbol,
                timeframe=self.tf,
                timestamp=ts,
                open_=self.o, high=self.h, low=self.l, close=self.c,
                volume=self.v,
                buy_vol=self.buy_v,
                sell_vol=self.sell_v,
            )
        except Exception as e:
            logger.warning(f"ingest failed: {e}")

    def force_close(self) -> None:
        with self.lock:
            self._close_and_ship()
            self.bucket = None


def _parse_ts(t_str) -> float:
    """Alpaca emits RFC3339 nanosecond timestamps; return epoch seconds."""
    if isinstance(t_str, (int, float)):
        return float(t_str)
    s = str(t_str).rstrip("Z")
    # Trim ns → us so datetime.fromisoformat copes
    if "." in s:
        head, frac = s.split(".", 1)
        frac = frac[:6]
        s = f"{head}.{frac}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def _heartbeat_loop(aggs: dict, stop_event: threading.Event, period_s: int = 30) -> None:
    """Force-close stale buckets when no trades arrive in the current bar window.

    IEX prints can stall (lunch, low-volume sessions). Without this, the bar
    never ships and rolling features desync. Closes when wall-clock has rolled
    past the bucket end.
    """
    while not stop_event.is_set():
        stop_event.wait(period_s)
        if stop_event.is_set():
            return
        now = time.time()
        for a in aggs.values():
            try:
                with a.lock:
                    if a.bucket is None:
                        continue
                    bar_end = a.bucket + a.bar_seconds
                    if now >= bar_end:
                        a._close_and_ship()
                        a.bucket = None
            except Exception as e:
                logger.warning(f"heartbeat: {e}")


def run(symbol: str, tf: str) -> None:
    try:
        import websocket
    except ImportError:
        print("websocket-client not installed. Run:\n    pip install websocket-client")
        return

    key    = os.getenv("ALPACA_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        print("Set ALPACA_KEY and ALPACA_SECRET in .env (or environment).")
        return

    sym = symbol.upper()
    aggs = {sym: TickRuleAggregator(sym, tf)}
    hb_stop = threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop, args=(aggs, hb_stop), daemon=True, name="alpaca-hb",
    )
    hb.start()
    logger.info(f"connecting Alpaca IEX WS → {sym} @ {tf}")

    def on_message(ws, message):  # noqa: ARG001
        try:
            msgs = json.loads(message)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for d in msgs:
                t = d.get("T")
                if t == "success":
                    if d.get("msg") == "authenticated":
                        ws.send(json.dumps({"action": "subscribe", "trades": [sym]}))
                        logger.info(f"authenticated; subscribed trades={sym}")
                elif t == "error":
                    logger.warning(f"alpaca error: {d}")
                elif t == "subscription":
                    logger.info(f"subscription confirmed: {d}")
                elif t == "t":
                    aggs[sym].add_trade(
                        ts_seconds=_parse_ts(d["t"]),
                        price=float(d["p"]),
                        size=float(d["s"]),
                    )
        except Exception as e:
            logger.warning(f"msg parse error: {e}")

    def on_open(ws):
        logger.info("Alpaca WS connected — sending auth")
        ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))

    def on_error(ws, error):  # noqa: ARG001
        logger.warning(f"WS error: {error}")

    def on_close(ws, code, msg):  # noqa: ARG001
        logger.info(f"WS closed code={code} msg={msg}")
        for a in aggs.values():
            a.force_close()

    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            logger.warning(f"WS loop crashed: {e}")
        logger.info("reconnecting in 3s…")
        time.sleep(3)


# ── REST trades replay backfill ─────────────────────────────────────────────


def _fetch_trades_page(
    symbol: str, start_iso: str, end_iso: str,
    page_token: str | None, key: str, secret: str,
) -> dict:
    params = {
        "start": start_iso,
        "end":   end_iso,
        "limit": 10000,
        "feed":  "iex",
    }
    if page_token:
        params["page_token"] = page_token
    r = requests.get(
        f"{REST_BASE}/{symbol}/trades",
        params=params,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _replay_trades_to_bars(symbol: str, tf: str, days: int) -> pd.DataFrame:
    """Pull historical IEX trades, classify via tick rule, aggregate to bars.

    Output schema matches the live aggregator: OHLCV + buy_vol_real +
    sell_vol_real. Used as backfill so the tail starts at proxy_mode=False
    rather than yfinance candle proxy.
    """
    key    = os.getenv("ALPACA_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY/ALPACA_SECRET not set")

    bar_seconds = TF_SECONDS.get(tf)
    if bar_seconds is None:
        raise ValueError(f"Unsupported tf: {tf}")

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=days)

    bars: dict[int, dict] = {}
    last_price: float | None = None
    page_token: str | None = None
    fetched = 0

    while True:
        data = _fetch_trades_page(
            symbol, start.isoformat().replace("+00:00", "Z"),
            end.isoformat().replace("+00:00", "Z"),
            page_token, key, secret,
        )
        trades = data.get("trades") or []
        for t in trades:
            ts = _parse_ts(t["t"])
            price = float(t["p"])
            size = float(t["s"])
            bucket = int(ts) - (int(ts) % bar_seconds)
            b = bars.get(bucket)
            if b is None:
                b = bars[bucket] = {
                    "ts":     bucket,
                    "Open":   price,
                    "High":   price,
                    "Low":    price,
                    "Close":  price,
                    "Volume": 0.0,
                    "buy_vol_real":  0.0,
                    "sell_vol_real": 0.0,
                }
            b["High"]   = max(b["High"], price)
            b["Low"]    = min(b["Low"],  price)
            b["Close"]  = price
            b["Volume"] += size
            if last_price is None or price > last_price:
                b["buy_vol_real"]  += size
            elif price < last_price:
                b["sell_vol_real"] += size
            else:
                b["buy_vol_real"]  += size / 2
                b["sell_vol_real"] += size / 2
            last_price = price
        fetched += len(trades)
        page_token = data.get("next_page_token")
        if not page_token:
            break

    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(sorted(bars.values(), key=lambda r: r["ts"]))
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("ts")
    logger.info(
        f"alpaca rest backfill {symbol}@{tf}: {len(df)} bars from {fetched} trades"
    )
    return df


def rest_backfill(symbol: str, tf: str, days: int | None = None) -> int:
    """Seed ingest tail from Alpaca REST trades. Returns bar count."""
    days_map = {"1m": 5, "5m": 10, "15m": 20, "30m": 30, "1h": 30}
    d = days if days is not None else days_map.get(tf, 10)
    df = _replay_trades_to_bars(symbol.upper(), tf, d)
    if df.empty:
        return 0
    return ingest.backfill_tail(symbol.upper(), tf, df=df)


# ── thread-friendly start/status helpers ────────────────────────────────────

_thread: threading.Thread | None = None
_active: dict = {"symbol": None, "tf": None}


def start_thread(symbol: str, tf: str, backfill: bool = True) -> bool:
    """Launch in a background daemon thread. Idempotent.

    Backfill priority:
      1. Alpaca REST trades replay (real flow, proxy_mode=False)
      2. yfinance bars fallback (proxy_mode=True)
    """
    global _thread
    if _thread and _thread.is_alive():
        return False
    _active.update(symbol=symbol, tf=tf)
    if backfill:
        try:
            n = rest_backfill(symbol, tf)
            if n == 0:
                ingest.backfill_tail(symbol, tf)
        except Exception as e:
            logger.warning(f"alpaca rest backfill failed ({e}); trying yfinance")
            try:
                ingest.backfill_tail(symbol, tf)
            except Exception as e2:
                logger.warning(f"yfinance backfill also failed: {e2}")
    _thread = threading.Thread(target=run, args=(symbol, tf), daemon=True)
    _thread.start()
    return True


def status() -> dict:
    keys_set = bool(os.getenv("ALPACA_KEY", "").strip()
                    and os.getenv("ALPACA_SECRET", "").strip())
    return {
        "running": bool(_thread and _thread.is_alive()),
        "keys_set": keys_set,
        **_active,
    }


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="Alpaca IEX trades adapter")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--tf", default="1m", choices=list(TF_SECONDS.keys()))
    args = ap.parse_args()
    run(args.symbol.upper(), args.tf)
