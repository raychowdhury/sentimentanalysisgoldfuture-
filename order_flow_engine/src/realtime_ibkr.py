"""
IBKR real-time adapter (stub + docs).

Wire this to `ingest.ingest_bar()` for true tick-accurate real-time. Requires
  - IBKR TWS or IB Gateway running (paper or live)
  - `pip install ib_insync`

Usage:
    python -m order_flow_engine.src.realtime_ibkr --symbol ES --tf 15m

Connects to local IB Gateway on 127.0.0.1:7497 (paper) / 7496 (live). Each
completed 15-minute bar is forwarded to the engine. Outside this module the
rest of the pipeline is unchanged — rules, model, alerts all fire the same
way whether bars arrive from IBKR or from the yfinance poller.

If `ib_insync` is not installed the CLI prints install instructions and
exits cleanly; this file never blocks app startup.
"""

from __future__ import annotations

import argparse

from order_flow_engine.src import config as of_cfg, ingest
from utils.logger import setup_logger

logger = setup_logger(__name__)


TF_TO_IB_BAR = {
    "5m":  "5 mins",
    "15m": "15 mins",
    "1h":  "1 hour",
    "1d":  "1 day",
}


def run(symbol: str, timeframe: str, host: str, port: int, client_id: int) -> None:
    try:
        from ib_insync import IB, Future, util
    except ImportError:
        print(
            "ib_insync not installed. Install with:\n"
            "    pip install ib_insync\n"
            "and run IB Gateway / TWS before launching this adapter."
        )
        return

    bar_size = TF_TO_IB_BAR.get(timeframe)
    if not bar_size:
        print(f"unsupported timeframe for IBKR: {timeframe}")
        return

    ib = IB()
    ib.connect(host, port, clientId=client_id)
    # ES continuous front-month futures contract
    contract = Future(symbol=symbol, exchange="CME", currency="USD")
    ib.qualifyContracts(contract)

    bars = ib.reqRealTimeBars(contract, 5, "TRADES", False)

    last_bucket = None
    bucket_o = bucket_h = bucket_l = bucket_c = None
    bucket_v = 0.0

    # IB reqRealTimeBars emits 5-second bars; aggregate into the anchor TF.
    bar_seconds = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}[timeframe]

    def on_update(bars_, has_new_bar):  # noqa: ARG001 — ib_insync signature
        nonlocal last_bucket, bucket_o, bucket_h, bucket_l, bucket_c, bucket_v
        b = bars_[-1]
        ts = int(b.time.timestamp())
        bucket = ts - (ts % bar_seconds)
        if last_bucket is None:
            last_bucket = bucket
            bucket_o = b.open_
            bucket_h = b.high
            bucket_l = b.low
            bucket_c = b.close
            bucket_v = b.volume
            return
        if bucket != last_bucket:
            # prior bar closed — ship it
            ingest.ingest_bar(
                symbol=symbol, timeframe=timeframe,
                timestamp=util.formatIBDatetime(last_bucket),
                open_=bucket_o, high=bucket_h, low=bucket_l,
                close=bucket_c, volume=bucket_v,
            )
            last_bucket = bucket
            bucket_o = b.open_
            bucket_h = b.high
            bucket_l = b.low
            bucket_c = b.close
            bucket_v = b.volume
        else:
            bucket_h = max(bucket_h, b.high)
            bucket_l = min(bucket_l, b.low)
            bucket_c = b.close
            bucket_v += b.volume

    bars.updateEvent += on_update
    logger.info(f"IBKR adapter live on {symbol} {timeframe}")
    ib.run()


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="IBKR real-time adapter")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)  # 7497 paper, 7496 live
    ap.add_argument("--client-id", type=int, default=42)
    args = ap.parse_args()
    run(args.symbol, args.tf, args.host, args.port, args.client_id)
