#!/usr/bin/env python3
"""
Replay historical Databento trades through the live tape detectors
(sweep / block / iceberg), compute outcomes against forward 1m bars,
and grade each detector kind by win rate / R-multiple.

Usage:
    python scripts/backtest_tape_detectors.py --symbol GCM6 --days 1
    python scripts/backtest_tape_detectors.py --symbol ESM6 --days 7
    python scripts/backtest_tape_detectors.py --symbol ALL --days 1

Output:
    outputs/order_flow/tape_backtest_<sym>_<start>_to_<end>.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import databento as db  # noqa: E402

# ── detector params (mirror realtime_databento_live.py) ────────────────────
SWEEP_WINDOW_S       = 1.0
SWEEP_MIN_COUNT      = 4
BLOCK_LOOKBACK       = 200
BLOCK_SIGMA          = 3.0
ICEBERG_WINDOW_S     = 30.0
ICEBERG_MIN_COUNT    = 5
ICEBERG_MIN_DUR_S    = 10.0
COOLDOWN_S           = 45
TARGET_ATR           = 1.0
STOP_ATR             = 1.0

TICK_SIZE = {
    "ESM6": 0.25, "ESH6": 0.25, "ESU6": 0.25,
    "NQM6": 0.25, "NQH6": 0.25,
    "GCM6": 0.10, "GCJ6": 0.10, "GCQ6": 0.10,
    "CLM6": 0.01, "CLN6": 0.01, "CLQ6": 0.01,
}

DATASET = "GLBX.MDP3"
_AVAIL_END_RE = re.compile(r"available up to '([^']+)'")
_RAW_RE       = re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$")


def _client():
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        sys.exit("DATABENTO_API_KEY missing")
    return db.Historical(key)


def _resolve_front(client, parent: str, on_date: datetime.date) -> str | None:
    end   = datetime.combine(on_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=18)
    start = end - timedelta(hours=4)
    try:
        df = client.timeseries.get_range(
            dataset=DATASET, symbols=parent, stype_in="parent",
            schema="ohlcv-1m", start=start.isoformat(), end=end.isoformat(),
        ).to_df()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    single = df[df["symbol"].astype(str).str.match(_RAW_RE)]
    if single.empty:
        return None
    top = single.groupby("symbol")["volume"].sum().sort_values(ascending=False)
    return str(top.index[0])


def _fetch_trades(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    try:
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=symbol, stype_in="raw_symbol",
            schema="trades", start=start.isoformat(), end=end.isoformat(),
        )
        return data.to_df()
    except Exception as e:
        m = _AVAIL_END_RE.search(str(e))
        if not m:
            raise
        avail = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
        return client.timeseries.get_range(
            dataset=DATASET, symbols=symbol, stype_in="raw_symbol",
            schema="trades", start=start.isoformat(), end=avail.isoformat(),
        ).to_df()


def _fetch_bars_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    try:
        df = client.timeseries.get_range(
            dataset=DATASET, symbols=symbol, stype_in="raw_symbol",
            schema="ohlcv-1m", start=start.isoformat(), end=end.isoformat(),
        ).to_df()
    except Exception as e:
        m = _AVAIL_END_RE.search(str(e))
        if not m:
            raise
        avail = pd.Timestamp(m.group(1)).tz_convert("UTC").to_pydatetime()
        df = client.timeseries.get_range(
            dataset=DATASET, symbols=symbol, stype_in="raw_symbol",
            schema="ohlcv-1m", start=start.isoformat(), end=avail.isoformat(),
        ).to_df()
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index).tz_convert("UTC")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _classify_side_tickrule(prices: list[float]) -> list[str]:
    """Lee-Ready tick rule. Sign carried forward."""
    sides = []
    last_sign = +1
    for i, p in enumerate(prices):
        if i == 0:
            sides.append("buy")
            continue
        diff = p - prices[i-1]
        if diff > 0:
            last_sign = +1
        elif diff < 0:
            last_sign = -1
        sides.append("buy" if last_sign > 0 else "sell")
    return sides


def replay_detectors(symbol: str, trades_df: pd.DataFrame) -> list[dict]:
    """Replay sweep/block/iceberg detectors over trades. Returns list of fires."""
    if trades_df.empty:
        return []
    df = trades_df[["price", "size"]].copy()
    df["price"] = df["price"].astype(float)
    df["size"]  = df["size"].astype(float)
    df.index = pd.to_datetime(df.index).tz_convert("UTC")

    # Side classification (uses 'side' from databento if present, else tick rule)
    if "side" in trades_df.columns:
        raw = trades_df["side"].astype(str).str.upper()
        df["side"] = raw.map({"B": "buy", "A": "sell"}).fillna("buy")
    else:
        df["side"] = _classify_side_tickrule(df["price"].tolist())

    tick = TICK_SIZE.get(symbol, 0.25)
    fires: list[dict] = []
    last_emit: dict[tuple, float] = {}

    # Sliding state
    win5: list[dict] = []         # last 5s
    iceberg_state: dict[tuple, list[float]] = {}

    for ts, row in df.iterrows():
        t = ts.timestamp()
        price = row["price"]; size = row["size"]; side = row["side"]
        win5.append({"t": t, "price": price, "size": size, "side": side})
        cutoff5 = t - 5.0
        while win5 and win5[0]["t"] < cutoff5:
            win5.pop(0)

        # SWEEP
        sweep_cut = t - SWEEP_WINDOW_S
        last1 = [w for w in win5 if w["t"] >= sweep_cut]
        if last1:
            run_side = None; run_len = 0; max_run = 0; max_side = None
            for w in last1:
                if w["side"] == run_side: run_len += 1
                else: run_side = w["side"]; run_len = 1
                if run_len > max_run: max_run = run_len; max_side = run_side
            if max_run >= SWEEP_MIN_COUNT and max_side:
                key = (symbol, "sweep", max_side)
                if t - last_emit.get(key, 0) >= COOLDOWN_S:
                    last_emit[key] = t
                    sw_vol = sum(w["size"] for w in last1 if w["side"] == max_side)
                    fires.append({"ts": ts.isoformat(), "kind": "sweep",
                                  "side": max_side, "price": price,
                                  "metrics": {"count": max_run, "vol": sw_vol}})

        # BLOCK
        if len(win5) >= 30:
            sizes = [w["size"] for w in win5[-BLOCK_LOOKBACK:]]
            mean = sum(sizes) / len(sizes)
            var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
            sd = var ** 0.5 or 1.0
            thr = mean + BLOCK_SIGMA * sd
            if size > thr and size > 5:
                key = (symbol, "block", side)
                if t - last_emit.get(key, 0) >= COOLDOWN_S:
                    last_emit[key] = t
                    fires.append({"ts": ts.isoformat(), "kind": "block",
                                  "side": side, "price": price,
                                  "metrics": {"size": size, "mean": round(mean, 2),
                                              "sd": round(sd, 2), "thr": round(thr, 2)}})

        # ICEBERG
        p_bin = round(price / tick) * tick
        s_bin = round(size)
        if s_bin > 0:
            ikey = (round(p_bin, 4), s_bin, side)
            bucket = iceberg_state.setdefault(ikey, [])
            bucket.append(t)
            ic_cutoff = t - ICEBERG_WINDOW_S
            while bucket and bucket[0] < ic_cutoff:
                bucket.pop(0)
            if len(bucket) >= ICEBERG_MIN_COUNT:
                duration = bucket[-1] - bucket[0]
                if duration >= ICEBERG_MIN_DUR_S:
                    gaps = [bucket[i] - bucket[i-1] for i in range(1, len(bucket))]
                    if gaps:
                        gs = sorted(gaps)
                        median = gs[len(gs) // 2] or 0.001
                        gm = sum(gaps) / len(gaps)
                        gv = sum((g - gm) ** 2 for g in gaps) / len(gaps)
                        gsd = gv ** 0.5
                        if gsd <= max(2.0, median * 1.5):
                            key = (symbol, "iceberg", side)
                            if t - last_emit.get(key, 0) >= COOLDOWN_S:
                                last_emit[key] = t
                                fires.append({"ts": ts.isoformat(), "kind": "iceberg",
                                              "side": side, "price": p_bin,
                                              "metrics": {"size": s_bin, "count": len(bucket),
                                                          "duration_s": round(duration, 1),
                                                          "gap_median_s": round(median, 2)}})
                                bucket.clear()
    return fires


def compute_outcomes(fires: list[dict], bars: pd.DataFrame,
                     horizon_min: int = 30) -> list[dict]:
    """Apply target/stop ATR exit logic to each fire using forward 1m bars."""
    if bars.empty or not fires:
        return fires
    # Pre-compute rolling ATR(14) on bars
    bars = bars.copy()
    bars["prev_close"] = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - bars["prev_close"]).abs(),
        (bars["low"]  - bars["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    bars["atr14"] = tr.rolling(14, min_periods=1).mean()

    for f in fires:
        ts = pd.Timestamp(f["ts"])
        # ATR snapshot from most recent completed bar
        prior = bars[bars.index <= ts]
        if prior.empty:
            f["outcome"] = "skipped_no_atr"; continue
        atr = float(prior["atr14"].iloc[-1] or 0)
        if atr <= 0:
            f["outcome"] = "skipped_no_atr"; continue
        f["atr"] = round(atr, 4)
        entry = f["price"]
        direction = +1 if f["side"] == "buy" else -1
        target = entry + direction * TARGET_ATR * atr
        stop   = entry - direction * STOP_ATR   * atr
        cutoff = ts + pd.Timedelta(minutes=horizon_min)
        forward = bars[(bars.index > ts) & (bars.index <= cutoff)]
        if forward.empty:
            f["outcome"] = "no_forward"; continue
        hit_t, hit_s = None, None
        for i, (bts, brow) in enumerate(forward.iterrows()):
            hi = float(brow["high"]); lo = float(brow["low"])
            if direction > 0:
                if hi >= target and hit_t is None: hit_t = i
                if lo <= stop   and hit_s is None: hit_s = i
            else:
                if lo <= target and hit_t is None: hit_t = i
                if hi >= stop   and hit_s is None: hit_s = i
            if hit_t is not None and hit_s is not None: break
        if hit_t is not None and (hit_s is None or hit_t < hit_s):
            f["outcome"] = "win"; f["r"] = TARGET_ATR
            f["exit_price"] = round(target, 4)
            f["exit_ts"]    = forward.index[hit_t].isoformat()
            f["bars_held"]  = hit_t + 1
        elif hit_s is not None and (hit_t is None or hit_s < hit_t):
            f["outcome"] = "loss"; f["r"] = -STOP_ATR
            f["exit_price"] = round(stop, 4)
            f["exit_ts"]    = forward.index[hit_s].isoformat()
            f["bars_held"]  = hit_s + 1
        else:
            close = float(forward["close"].iloc[-1])
            f["outcome"] = "neutral"
            f["r"] = direction * (close - entry) / atr
            f["exit_price"] = round(close, 4)
            f["exit_ts"]    = forward.index[-1].isoformat()
            f["bars_held"]  = len(forward)
        f["entry"]  = round(entry, 4)
        f["target"] = round(target, 4)
        f["stop"]   = round(stop, 4)
    return fires


def summarize(fires: list[dict]) -> dict:
    by_kind: dict = {}
    for f in fires:
        if f.get("outcome") not in ("win", "loss", "neutral"):
            continue
        key = f"{f['kind']}_{f['side']}"
        agg = by_kind.setdefault(key, {"count": 0, "wins": 0, "losses": 0,
                                       "neutral": 0, "total_R": 0.0})
        agg["count"] += 1
        bucket = {"win": "wins", "loss": "losses", "neutral": "neutral"}[f["outcome"]]
        agg[bucket] += 1
        agg["total_R"] += float(f.get("r") or 0)
    for k, v in by_kind.items():
        decisive = v["wins"] + v["losses"]
        v["win_rate"]       = round(v["wins"] / decisive, 4) if decisive else 0.0
        v["expectancy_R"]   = round(v["total_R"] / v["count"], 4) if v["count"] else 0.0
        v["total_R"]        = round(v["total_R"], 4)
    return by_kind


def run_one(client, symbol_input: str, days: int) -> dict:
    end   = datetime.now(timezone.utc) - timedelta(minutes=15)
    start = end - timedelta(days=days)
    sym = symbol_input
    if sym.endswith(".FUT"):
        resolved = _resolve_front(client, sym, end.date())
        if not resolved:
            return {"error": f"could not resolve {sym}", "symbol": sym}
        sym = resolved
    print(f"[{sym}] fetching trades {start.isoformat()} → {end.isoformat()}", flush=True)
    trades = _fetch_trades(client, sym, start, end)
    print(f"[{sym}] trades: {len(trades)} rows", flush=True)
    if trades.empty:
        return {"symbol": sym, "fires": [], "summary": {}}
    print(f"[{sym}] replaying detectors…", flush=True)
    fires = replay_detectors(sym, trades)
    print(f"[{sym}] fires: {len(fires)}", flush=True)
    if not fires:
        return {"symbol": sym, "trades": len(trades), "fires": [], "summary": {}}
    bars_end = end + timedelta(minutes=45)
    print(f"[{sym}] fetching 1m bars for outcome compute…", flush=True)
    bars = _fetch_bars_1m(client, sym, start, bars_end)
    fires = compute_outcomes(fires, bars)
    summary = summarize(fires)
    return {"symbol": sym, "trades": len(trades), "bars": len(bars),
            "start": start.isoformat(), "end": end.isoformat(),
            "fires": fires, "summary": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GCM6", help="raw contract or ALL")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    client = _client()
    syms_all = ["ESM6", "GCM6", "NQM6", "CLM6"]
    syms = syms_all if args.symbol.upper() == "ALL" else [args.symbol]
    out_dir = ROOT / "outputs" / "order_flow"
    out_dir.mkdir(parents=True, exist_ok=True)
    combined: dict = {"per_symbol": {}, "combined_summary": {}}
    for sym in syms:
        report = run_one(client, sym, args.days)
        combined["per_symbol"][sym] = report
        for kind_side, agg in report.get("summary", {}).items():
            comb = combined["combined_summary"].setdefault(
                kind_side, {"count": 0, "wins": 0, "losses": 0, "neutral": 0, "total_R": 0.0},
            )
            comb["count"]   += agg["count"]
            comb["wins"]    += agg["wins"]
            comb["losses"]  += agg["losses"]
            comb["neutral"] += agg["neutral"]
            comb["total_R"] += agg["total_R"]
    for ks, comb in combined["combined_summary"].items():
        d = comb["wins"] + comb["losses"]
        comb["win_rate"]     = round(comb["wins"] / d, 4) if d else 0.0
        comb["expectancy_R"] = round(comb["total_R"] / comb["count"], 4) if comb["count"] else 0.0
        comb["total_R"]      = round(comb["total_R"], 4)

    fname = args.out or f"tape_backtest_{args.symbol}_{args.days}d.json"
    out_path = out_dir / fname
    with out_path.open("w") as f:
        json.dump(combined, f, indent=2, default=str)

    # CLI summary
    print()
    print("=" * 60)
    print(f"BACKTEST · {args.symbol} · {args.days}d")
    print("=" * 60)
    print(f"{'kind_side':<20} {'n':>5} {'W':>4} {'L':>4} {'N':>4} {'win%':>6} {'Σ R':>8} {'E[R]':>7}")
    print("-" * 60)
    for ks, c in sorted(combined["combined_summary"].items(),
                        key=lambda x: -x[1]["total_R"]):
        print(f"{ks:<20} {c['count']:>5} {c['wins']:>4} {c['losses']:>4} "
              f"{c['neutral']:>4} {c['win_rate']*100:>5.0f}% "
              f"{c['total_R']:>+8.2f} {c['expectancy_R']:>+7.3f}")
    print("=" * 60)
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
