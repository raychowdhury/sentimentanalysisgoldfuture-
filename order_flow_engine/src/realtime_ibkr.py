"""
IBKR real-time adapter — tick-by-tick trades + NBBO → Lee-Ready classified bars.

For each trade, side is inferred from the quote midpoint:
  price > mid → buy (aggressor lifted the offer)
  price < mid → sell (aggressor hit the bid)
  price = mid → tick rule (up/down from prior trade)

Bars are bucketed per `timeframe` and shipped via `ingest.ingest_bar()` with
real buy_vol/sell_vol, so alerts carry proxy_mode=false.

Requirements:
  - IB Gateway or TWS running (paper port 7497, live 7496)
  - `pip install ib_insync`
  - CME-Level-1 market data bundle on the account (~$1.50/mo)
  - ES-equivalent contract permissions

Usage:
    python -m order_flow_engine.src.realtime_ibkr --symbol ES --tf 15m

Thread helpers (`start_thread` / `status` / `stop`) mirror the Alpaca adapter
so the dashboard can control this adapter the same way.
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime, timezone

from order_flow_engine.src import ingest
from utils.logger import setup_logger

logger = setup_logger(__name__)


TF_SECONDS = {
    "1m":   60,
    "5m":   300,
    "15m":  900,
    "1h":   3600,
    "1d":   86400,
}


class QuoteTickAggregator:
    """
    Tick-by-tick trade aggregator with quote-aware Lee-Ready side classification.

    Thread-safe. Holds the current best bid/ask from NBBO updates; each trade is
    classified against the midpoint at trade time. Falls back to tick rule when
    the quote is unavailable or trade price equals mid.
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

        self.bid: float | None = None
        self.ask: float | None = None
        self.last_price: float | None = None

        self.trade_count = 0
        self.last_trade_ts: float | None = None
        self.lock = threading.Lock()

    # ── quote tracking ──────────────────────────────────────────────
    def set_quote(self, bid: float | None, ask: float | None) -> None:
        with self.lock:
            if bid is not None and bid > 0:
                self.bid = float(bid)
            if ask is not None and ask > 0:
                self.ask = float(ask)

    # ── trades ──────────────────────────────────────────────────────
    def add_trade(self, ts_seconds: float, price: float, size: float) -> None:
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

            side = self._classify(price)
            if side > 0:
                self.buy_v += size
            elif side < 0:
                self.sell_v += size
            else:
                # no prior reference — split
                self.buy_v  += size / 2
                self.sell_v += size / 2

            self.last_price = price
            self.trade_count += 1
            self.last_trade_ts = ts_seconds

    def _classify(self, price: float) -> int:
        """Quote rule with tick-rule fallback. Returns +1 buy, -1 sell, 0 unknown."""
        if self.bid is not None and self.ask is not None and self.ask > self.bid:
            mid = (self.bid + self.ask) / 2.0
            if price > mid:
                return +1
            if price < mid:
                return -1
            # price == mid (rare on tick-sized futures) → tick rule
        # tick-rule fallback
        if self.last_price is None:
            return 0
        if price > self.last_price:
            return +1
        if price < self.last_price:
            return -1
        return 0  # zero-tick with no quote → split 50/50 upstream

    # ── bucket lifecycle ────────────────────────────────────────────
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


# ── module-level adapter state ──────────────────────────────────────
_state = {
    "running":  False,
    "thread":   None,
    "symbol":   None,
    "tf":       None,
    "host":     None,
    "port":     None,
    "agg":      None,
    "started_at": None,
    "ib":       None,
    "stop":     False,
    "error":    None,
}


def run(symbol: str, timeframe: str, host: str, port: int, client_id: int) -> None:
    # ib_insync expects an asyncio event loop in whatever thread it runs in.
    # When launched via threading.Thread there isn't one — create it first,
    # BEFORE importing ib_insync (its __init__ probes the loop).
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    _state["error"] = None
    try:
        from ib_insync import IB, Future
    except ImportError as e:
        msg = f"ib_insync not installed: {e}. pip install ib_insync"
        logger.warning(msg)
        _state["error"] = msg
        return

    agg = QuoteTickAggregator(symbol, timeframe)
    _state.update({
        "running": True, "symbol": symbol, "tf": timeframe,
        "host": host, "port": port, "agg": agg,
        "started_at": datetime.now(timezone.utc).isoformat(), "stop": False,
    })

    ib = IB()
    _state["ib"] = ib
    logger.info(f"connecting IB gateway {host}:{port} clientId={client_id}")
    try:
        ib.connect(host, port, clientId=client_id)
    except Exception as e:
        msg = (f"IB connect failed ({e}). Is IB Gateway/TWS running on "
               f"{host}:{port} with API enabled? (paper=7497, live=7496)")
        logger.warning(msg)
        _state.update({"running": False, "error": msg, "ib": None})
        return

    # Continuous front-month futures contract.
    contract = Future(symbol=symbol, exchange="CME", currency="USD",
                      includeExpired=False)
    ib.qualifyContracts(contract)
    logger.info(f"qualified: {contract.localSymbol} (conId={contract.conId})")

    # Quote subscription (NBBO) — needed for Lee-Ready classification.
    mkt_ticker = ib.reqMktData(contract, "", False, False)

    def _on_quote(ticker):
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        if bid or ask:
            agg.set_quote(bid, ask)

    mkt_ticker.updateEvent += _on_quote

    # Tick-by-tick trade subscription.
    tbt = ib.reqTickByTickData(contract, "AllLast", 0, False)

    def _on_tick(ticker):
        for t in ticker.tickByTicks:
            ts = t.time.timestamp() if hasattr(t.time, "timestamp") else float(t.time)
            agg.add_trade(ts_seconds=ts, price=float(t.price), size=float(t.size))
        ticker.tickByTicks.clear()

    tbt.updateEvent += _on_tick

    # Periodic force_close so the final bar of a quiet window still ships.
    def _heartbeat():
        while not _state["stop"]:
            ib.sleep(5)
            try:
                now = time.time()
                if agg.bucket is not None and now - agg.bucket >= agg.bar_seconds:
                    agg.force_close()
            except Exception as e:
                logger.warning(f"heartbeat: {e}")

    hb = threading.Thread(target=_heartbeat, daemon=True, name="ibkr-hb")
    hb.start()

    logger.info(f"IBKR adapter live on {symbol} {timeframe} (tick-by-tick + NBBO)")
    try:
        ib.run()
    except Exception as e:
        _state["error"] = f"runtime: {e}"
        logger.warning(f"IBKR runtime error: {e}")
    finally:
        try: ib.disconnect()
        except Exception: pass
        _state.update({"running": False, "ib": None, "stop": True})


# ── thread-friendly start/status helpers ────────────────────────────
def start_thread(symbol: str = "ES", tf: str = "15m",
                 host: str = "127.0.0.1", port: int = 7497,
                 client_id: int = 42) -> bool:
    """Kick off the adapter in a daemon thread. Idempotent."""
    if _state["running"]:
        return False
    t = threading.Thread(
        target=run,
        kwargs={"symbol": symbol, "timeframe": tf, "host": host,
                "port": port, "client_id": client_id},
        daemon=True, name="ibkr-adapter",
    )
    _state["thread"] = t
    t.start()
    return True


def status() -> dict:
    agg = _state.get("agg")
    return {
        "running":      bool(_state.get("running")),
        "symbol":       _state.get("symbol"),
        "tf":           _state.get("tf"),
        "host":         _state.get("host"),
        "port":         _state.get("port"),
        "started_at":   _state.get("started_at"),
        "error":        _state.get("error"),
        "trade_count":  agg.trade_count if agg else 0,
        "last_trade_ts": (datetime.fromtimestamp(agg.last_trade_ts, tz=timezone.utc).isoformat()
                          if agg and agg.last_trade_ts else None),
        "bid":          agg.bid if agg else None,
        "ask":          agg.ask if agg else None,
        "bucket_buy":   agg.buy_v if agg else 0.0,
        "bucket_sell":  agg.sell_v if agg else 0.0,
    }


def stop() -> None:
    _state["stop"] = True
    ib = _state.get("ib")
    if ib is not None:
        try: ib.disconnect()
        except Exception: pass


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="IBKR real-time adapter — tick + NBBO")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)  # 7497 paper, 7496 live
    ap.add_argument("--client-id", type=int, default=42)
    args = ap.parse_args()
    run(args.symbol, args.tf, args.host, args.port, args.client_id)
