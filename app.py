"""
NewsSentimentScanner — Dashboard Server

Usage:
    python app.py
    # Open http://localhost:5001
"""

import glob
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, render_template, request, send_file

import config
import scheduler as sched
from sentiment import cache as sentiment_cache

app = Flask(__name__)
OUTPUT_DIR = "outputs"

from order_flow_engine.src.dashboard import register as register_order_flow
register_order_flow(app)

from ml_engine.dashboard import register as register_ml_engine
register_ml_engine(app)


# ── Jinja2 filters ────────────────────────────────────────────────────────────

@app.template_filter("fmt_score")
def fmt_score(v):
    try:
        return f"{float(v):+.4f}"
    except (ValueError, TypeError):
        return "—"

@app.template_filter("fmt_price")
def fmt_price(v):
    try:
        return f"{float(v):,.2f}"
    except (ValueError, TypeError):
        return "—"

@app.template_filter("fmt_conf")
def fmt_conf(v):
    try:
        return f"{float(v):.3f}"
    except (ValueError, TypeError):
        return "—"

@app.template_filter("score_class")
def score_class(v):
    try:
        f = float(v)
        if f > 0.05:  return "score-pos"
        if f < -0.05: return "score-neg"
    except (ValueError, TypeError):
        pass
    return "score-neu"

@app.template_filter("pct_change")
def pct_change(v):
    try:
        f = float(v)
        return f"{f:+.2f}%"
    except (ValueError, TypeError):
        return "—"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _peek_timeframe(sig_path: str | None) -> str:
    """Read the 'timeframe' field from a signal JSON without full loading."""
    if not sig_path or not os.path.isfile(sig_path):
        return "swing"
    try:
        with open(sig_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("timeframe", "swing")
    except Exception:
        return "swing"


def load_runs() -> list[dict]:
    """
    Pair sentiment_*.json and signal_*.json by shared timestamp.
    Returns runs sorted newest-first.
    """
    sent_map = {
        os.path.basename(p).removeprefix("sentiment_").removesuffix(".json"): p
        for p in glob.glob(os.path.join(OUTPUT_DIR, "sentiment_*.json"))
    }
    sig_map = {
        os.path.basename(p).removeprefix("signal_").removesuffix(".json"): p
        for p in glob.glob(os.path.join(OUTPUT_DIR, "signal_*.json"))
    }

    all_ts = sorted(set(sent_map) | set(sig_map), reverse=True)
    runs = []
    for ts in all_ts:
        try:
            dt    = datetime.strptime(ts, "%Y%m%d_%H%M%S")
            label = dt.strftime("%b %d, %Y  %H:%M:%S")
        except ValueError:
            label = ts

        has_sent  = ts in sent_map
        has_sig   = ts in sig_map
        sig_path  = sig_map.get(ts)
        timeframe = _peek_timeframe(sig_path)
        tag = "signal+sentiment" if (has_sent and has_sig) else ("signal" if has_sig else "sentiment")

        runs.append({
            "timestamp":  ts,
            "label":      f"{label}  [{tag}]",
            "has_sent":   has_sent,
            "has_sig":    has_sig,
            "sent_path":  sent_map.get(ts),
            "sig_path":   sig_path,
            "timeframe":  timeframe,
        })
    return runs


def _load_json(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_latest_backtest(timeframe: str = "swing") -> dict | None:
    """Newest backtest_{tf}_*.json → condensed summary for the dashboard.

    Falls back to any timeframe when the requested one has no backtest yet,
    so a fresh `day` run still shows the swing backtest proof.
    """
    paths = sorted(glob.glob(os.path.join(OUTPUT_DIR, f"backtest_{timeframe}_*.json")),
                   reverse=True)
    if not paths:
        paths = sorted(glob.glob(os.path.join(OUTPUT_DIR, "backtest_*.json")),
                       reverse=True)
        if paths:
            # Update timeframe label to whatever file we actually grabbed.
            fn = os.path.basename(paths[0])
            timeframe = fn.split("_")[1] if fn.startswith("backtest_") else timeframe
    if not paths:
        return None
    data = _load_json(paths[0])
    if not data:
        return None
    rep      = data.get("report", {})
    overall  = rep.get("overall", {})
    filename = os.path.basename(paths[0])
    ts       = filename.removeprefix(f"backtest_{timeframe}_").removesuffix(".json")
    try:
        label = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%b %d, %Y %H:%M")
    except ValueError:
        label = ts
    return {
        "label":      label,
        "days":       data.get("params", {}).get("days"),
        "timeframe":  timeframe,
        "overall":    overall,
        "by_signal":  rep.get("by_signal", {}),
        "by_regime":  rep.get("by_regime", {}),
        "max_dd":     rep.get("max_drawdown_r"),
    }


def _engine_config(timeframe: str = "swing") -> dict:
    """Risk gates + weights + partial-TP settings surfaced to the template."""
    tf_profile = config.TIMEFRAME_PROFILES.get(timeframe, {})
    return {
        "long_only":          getattr(config, "LONG_ONLY",         False),
        "sma200_gate":        getattr(config, "SMA200_GATE",       False),
        "min_rr":             tf_profile.get("min_rr", getattr(config, "MIN_RR", None)),
        "max_hold":           tf_profile.get("max_hold"),
        "atr_stop_mult":      tf_profile.get("atr_stop_mult"),
        "partial_tp_enabled": getattr(config, "PARTIAL_TP_ENABLED", False),
        "partial_tp_r":       getattr(config, "PARTIAL_TP_R",       None),
        "partial_tp_frac":    getattr(config, "PARTIAL_TP_FRACTION", None),
        "trail_enabled":      getattr(config, "TRAIL_ENABLED",     False),
        "weights":            getattr(config, "SCORE_WEIGHTS",     {}),
    }


def _event_calendar(lookahead_days: int = 7, limit: int = 12) -> dict:
    """
    Upcoming events + today's blackout status for the dashboard panel.
    Sources: hardcoded FOMC/CPI/NFP/PCE + live FF feed (see events/ff_fetcher).
    """
    from events import get_events, is_blackout

    today = date.today()
    events = get_events(today, today + timedelta(days=lookahead_days))

    before = int(getattr(config, "EVENT_BLACKOUT_DAYS_BEFORE", 1))
    after  = int(getattr(config, "EVENT_BLACKOUT_DAYS_AFTER", 1))

    rows = []
    for ev in events[:limit]:
        days_until = (ev.date - today).days
        win_start  = ev.date - timedelta(days=before)
        win_end    = ev.date + timedelta(days=after)
        in_window  = win_start <= today <= win_end
        rows.append({
            "date":       ev.date.isoformat(),
            "days_until": days_until,
            "kind":       ev.kind,
            "label":      ev.label,
            "blocking":   in_window,
        })

    blocked, reason = is_blackout(today)
    return {
        "today":           today.isoformat(),
        "blackout_today":  blocked,
        "blackout_reason": reason,
        "events":          rows,
        "ff_enabled":      bool(getattr(config, "FF_CALENDAR_ENABLED", False)),
    }


def _macro_bullish(signal: dict | None) -> bool | None:
    """True when gold > SMA200, False when below, None when unknown."""
    if not signal:
        return None
    gold = (signal.get("market_snapshot") or {}).get("gold") or {}
    cur, sma = gold.get("current"), gold.get("sma200")
    if cur is None or sma is None:
        return None
    return cur > sma


def _trade_viz(trade: dict | None) -> dict | None:
    """
    Pre-compute price ladder geometry for the template.
    Returns pct positions so Jinja2 doesn't need to do division.
    """
    if not trade or not trade.get("trade_valid"):
        return None
    entry = trade.get("entry_price")
    stop  = trade.get("stop_loss")
    tp    = trade.get("take_profit")
    if None in (entry, stop, tp):
        return None

    is_buy      = stop < entry
    low         = min(stop, tp)
    high        = max(stop, tp)
    total_range = high - low
    if total_range == 0:
        return None

    entry_pct = round((entry - low) / total_range * 100, 1)
    # For BUY  : bottom=stop(red), entry line, top=tp(green) → reward above entry
    # For SELL : bottom=tp(green), entry line, top=stop(red) → reward below entry
    reward_pct = round(100 - entry_pct if is_buy else entry_pct, 1)
    risk_pct   = round(entry_pct       if is_buy else 100 - entry_pct, 1)

    bottom_label = f"{low:,.2f}"
    top_label    = f"{high:,.2f}"
    bottom_is_stop = is_buy   # True → bottom label = Stop; False → bottom label = TP

    return {
        "entry_pct":      entry_pct,
        "reward_pct":     reward_pct,
        "risk_pct":       risk_pct,
        "is_buy":         is_buy,
        "top_label":      top_label,
        "bottom_label":   bottom_label,
        "entry_label":    f"{entry:,.2f}",
        "bottom_is_stop": bottom_is_stop,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    all_runs = load_runs()
    if not all_runs:
        return render_template("index.html", runs=[], sentiment=None, signal=None,
                               trade_viz=None, selected=None, tf_filter="all",
                               scheduler=sched.get_status(),
                               scheduler_enabled=sched.is_enabled(),
                               backtest=None, engine_cfg=_engine_config("swing"),
                               macro_bullish=None, cache_days=0,
                               event_calendar=_event_calendar(), page="gold")

    # Timeframe nav filter: all / swing / day
    tf_filter = request.args.get("tf", "all")
    if tf_filter not in ("all", "swing", "day"):
        tf_filter = "all"

    runs = all_runs if tf_filter == "all" else [r for r in all_runs if r["timeframe"] == tf_filter]
    # Fall back to full list if filter yields nothing
    if not runs:
        runs = all_runs

    valid    = {r["timestamp"] for r in runs}
    selected = request.args.get("run", runs[0]["timestamp"])
    if selected not in valid:
        selected = runs[0]["timestamp"]

    run       = next(r for r in runs if r["timestamp"] == selected)
    sentiment = _load_json(run["sent_path"])
    signal    = _load_json(run["sig_path"])
    viz       = _trade_viz(signal.get("trade_setup") if signal else None)

    backtest_tf = signal.get("timeframe") if signal else "swing"
    backtest    = _load_latest_backtest(backtest_tf or "swing")
    cache_days  = len(sentiment_cache.load())

    return render_template(
        "index.html",
        runs=runs,
        sentiment=sentiment,
        signal=signal,
        trade_viz=viz,
        selected=selected,
        tf_filter=tf_filter,
        scheduler=sched.get_status(),
        scheduler_enabled=sched.is_enabled(),
        backtest=backtest,
        engine_cfg=_engine_config(signal.get("timeframe", "swing") if signal else "swing"),
        macro_bullish=_macro_bullish(signal),
        cache_days=cache_days,
        event_calendar=_event_calendar(),
        page="gold",
    )


# ── Gold AutoResearch dashboard ──────────────────────────────────────────────
# Serves the standalone dashboard.html from the gold-autoResearch sub-project.
# The page fetches live data from the FastAPI service at localhost:8000 (see
# gold-autoResearch/api.py); that must be running separately.

AUTORESEARCH_HTML = Path(__file__).parent / "gold-autoResearch" / "frontend" / "dashboard.html"


@app.route("/autoresearch")
def autoresearch_view():
    if not AUTORESEARCH_HTML.exists():
        return "AutoResearch dashboard not available.", 404
    return send_file(AUTORESEARCH_HTML)


# ── Stock sentiment scanner ───────────────────────────────────────────────────

@app.route("/stocks/overview")
def stocks_overview_view():
    """Aggregated view over the full S&P 500 universe, grouped by GICS sector."""
    from stocks.stock_output import read_overview
    from stocks.stock_universe import UNIVERSE, by_sector

    overview = read_overview()
    scanned_by_ticker = {
        row["ticker"]: row for row in (overview.get("stocks") if overview else []) or []
    }

    sectors_data = []
    for sector, stocks in by_sector().items():
        tiles = []
        bullish = bearish = neutral = scanned = 0
        for s in stocks:
            row = scanned_by_ticker.get(s.ticker)
            tile = {
                "ticker":   s.ticker,
                "name":     s.name,
                "sector":   s.sector,
                "industry": s.industry,
                "scanned":  row is not None,
            }
            if row:
                scanned += 1
                tile.update({
                    "signal":          row.get("signal"),
                    "confidence":      row.get("confidence"),
                    "sentiment_label": row.get("sentiment_label"),
                    "total_score":     row.get("total_score"),
                    "article_count":   row.get("article_count"),
                    "price":           row.get("price"),
                    "return_5d_pct":   row.get("return_5d_pct"),
                    "ml_prob_up":      row.get("ml_prob_up"),
                    "ml_source":       row.get("ml_source"),
                    "error":           row.get("error"),
                })
                sig = row.get("signal")
                if sig in ("BUY", "STRONG_BUY"):
                    bullish += 1
                elif sig in ("SELL", "STRONG_SELL"):
                    bearish += 1
                elif sig == "HOLD":
                    neutral += 1
            tiles.append(tile)

        sectors_data.append({
            "sector":   sector,
            "count":    len(stocks),
            "scanned":  scanned,
            "bullish":  bullish,
            "bearish":  bearish,
            "neutral":  neutral,
            "tiles":    tiles,
        })

    return render_template(
        "stocks_overview.html",
        overview=overview,
        sectors=sectors_data,
        universe_size=len(UNIVERSE),
        page="stocks",
    )


@app.route("/stocks/aggregate")
def stocks_aggregate_view():
    """S&P 500 next-session bias aggregate (breadth + lean) from
    stocks-autoResearch pooled model. Reads outputs/stocks/_aggregate.json
    written by stocks-autoResearch/predict_next_session.py."""
    agg_path = Path(__file__).parent / "outputs" / "stocks" / "_aggregate.json"
    aggregate = None
    if agg_path.exists():
        try:
            aggregate = json.loads(agg_path.read_text())
        except json.JSONDecodeError:
            aggregate = None
    reliab_path = Path(__file__).parent / "outputs" / "stocks" / "_reliability.json"
    reliability = None
    if reliab_path.exists():
        try:
            reliability = json.loads(reliab_path.read_text())
        except json.JSONDecodeError:
            reliability = None
    weights_path = Path(__file__).parent / "outputs" / "stocks" / "_composite_weights.json"
    weights_meta = None
    if weights_path.exists():
        try:
            weights_meta = json.loads(weights_path.read_text())
        except json.JSONDecodeError:
            weights_meta = None
    backtest_path = Path(__file__).parent / "outputs" / "stocks" / "_backtest_composite.json"
    backtest = None
    if backtest_path.exists():
        try:
            backtest = json.loads(backtest_path.read_text())
        except json.JSONDecodeError:
            backtest = None
    refresh_path = Path(__file__).parent / "outputs" / "stocks" / "_monthly_refresh.json"
    last_refresh = None
    if refresh_path.exists():
        try:
            last_refresh = json.loads(refresh_path.read_text())
        except json.JSONDecodeError:
            last_refresh = None
    from stocks.stock_output import read_ticker
    spy_detail = read_ticker("SPX")
    return render_template(
        "stocks_aggregate.html",
        aggregate=aggregate,
        reliability=reliability,
        weights_meta=weights_meta,
        backtest=backtest,
        last_refresh=last_refresh,
        spy_detail=spy_detail,
        page="stocks",
    )


# ── Aggregate refresh job (item 17) ────────────────────────────────────────
import subprocess, threading

_refresh_state = {"proc": None, "started_at": None}
_refresh_lock = threading.Lock()


def _refresh_running() -> bool:
    p = _refresh_state.get("proc")
    return bool(p and p.poll() is None)


@app.route("/api/stocks/aggregate/refresh", methods=["POST"])
def api_aggregate_refresh():
    with _refresh_lock:
        if _refresh_running():
            return jsonify({"started": False, "error": "refresh already running",
                            "started_at": _refresh_state["started_at"]}), 409
        script = Path(__file__).parent / "stocks-autoResearch" / "predict_next_session.py"
        if not script.exists():
            return jsonify({"started": False, "error": "predict script not found"}), 500
        venv_python = Path(__file__).parent / ".venv" / "bin" / "python"
        cmd = [str(venv_python) if venv_python.exists() else "python", str(script)]
        log_path = Path("/tmp") / "stocks_aggregate_refresh.log"
        log_fh = open(log_path, "w")
        proc = subprocess.Popen(
            cmd, cwd=str(script.parent),
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
        _refresh_state["proc"] = proc
        _refresh_state["started_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return jsonify({"started": True, "pid": proc.pid,
                    "log": str(log_path),
                    "started_at": _refresh_state["started_at"]})


@app.route("/api/stocks/aggregate/status")
def api_aggregate_status():
    p = _refresh_state.get("proc")
    return jsonify({
        "running":    _refresh_running(),
        "started_at": _refresh_state.get("started_at"),
        "exit_code":  None if (p is None or p.poll() is None) else p.returncode,
    })


_reliab_state = {"proc": None, "started_at": None}
_reliab_lock = threading.Lock()


def _reliab_running() -> bool:
    p = _reliab_state.get("proc")
    return bool(p and p.poll() is None)


@app.route("/api/stocks/reliability/refresh", methods=["POST"])
def api_reliability_refresh():
    """Kick off monthly composite refresh (backtest + fit + reliability)."""
    with _reliab_lock:
        if _reliab_running():
            return jsonify({"started": False, "error": "refresh already running",
                            "started_at": _reliab_state["started_at"]}), 409
        venv_python = Path(__file__).parent / ".venv" / "bin" / "python"
        cmd = [str(venv_python) if venv_python.exists() else "python",
               "-m", "research.monthly_refresh"]
        cwd = Path(__file__).parent / "stocks-autoResearch"
        log_path = Path("/tmp") / "stocks_reliability_refresh.log"
        log_fh = open(log_path, "w")
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=log_fh, stderr=subprocess.STDOUT)
        _reliab_state["proc"] = proc
        _reliab_state["started_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return jsonify({"started": True, "pid": proc.pid, "log": str(log_path),
                    "started_at": _reliab_state["started_at"]})


@app.route("/api/stocks/reliability/status")
def api_reliability_status():
    p = _reliab_state.get("proc")
    meta_path = Path(__file__).parent / "outputs" / "stocks" / "_monthly_refresh.json"
    last = None
    if meta_path.exists():
        try:
            last = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            last = None
    return jsonify({
        "running":    _reliab_running(),
        "started_at": _reliab_state.get("started_at"),
        "exit_code":  None if (p is None or p.poll() is None) else p.returncode,
        "last":       last,
    })


@app.route("/api/stocks/quotes")
def api_stocks_quotes():
    """Live last-trade prices for comma-separated ?symbols=... via Alpaca IEX.

    Returns {"quotes": {SYM: {price, ts}}, "enabled": bool}. `enabled`
    is false when ALPACA_KEY/ALPACA_SECRET are unset, letting the
    client hide the live indicator.
    """
    from stocks.alpaca_quotes import get_last_trades

    raw = request.args.get("symbols", "")
    symbols = [s for s in raw.split(",") if s.strip()]
    enabled = bool(os.getenv("ALPACA_KEY") and os.getenv("ALPACA_SECRET"))
    if not enabled or not symbols:
        return jsonify({"quotes": {}, "enabled": enabled})
    quotes = get_last_trades(symbols)
    return jsonify({"quotes": quotes, "enabled": True})


@app.route("/api/futures/snapshot")
def api_futures_snapshot():
    """Per-contract trader-view payload: last close, change, last N closes
    (for sparkline), volume sum, latest delta_ratio + buy/sell split.

    Reads engine's in-memory tail — zero extra Databento fetches.
    Query: ?symbols=ESM6,GCM6,CLM6,NQM6&tf=1m&n=60
    """
    from order_flow_engine.src import ingest

    raw = request.args.get("symbols", "")
    tf  = request.args.get("tf", "1m")
    n   = max(2, min(int(request.args.get("n", 60)), 500))
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    enabled = bool(os.getenv("DATABENTO_API_KEY"))
    out = {"enabled": enabled, "tf": tf, "snapshots": {}}
    if not enabled or not symbols:
        return jsonify(out)

    for sym in symbols:
        bars = ingest.get_recent_bars(sym, tf, n)
        if not bars:
            continue
        closes = [float(b["Close"]) for b in bars]
        vols   = [float(b.get("Volume", 0) or 0) for b in bars]
        last   = bars[-1]
        first  = bars[0]
        delta_ratio = None
        buy_vol = last.get("buy_vol_real")
        sell_vol = last.get("sell_vol_real")
        if buy_vol is not None and sell_vol is not None:
            tot = float(buy_vol) + float(sell_vol)
            if tot > 0:
                delta_ratio = (float(buy_vol) - float(sell_vol)) / tot
        out["snapshots"][sym] = {
            "last":       float(last["Close"]),
            "first":      float(first["Close"]),
            "change_pct": (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0,
            "high":       max(float(b["High"]) for b in bars),
            "low":        min(float(b["Low"]) for b in bars),
            "vol_sum":    sum(vols),
            "closes":     closes,
            "ts":         str(last.get("ts")),
            "delta_ratio": round(delta_ratio, 4) if delta_ratio is not None else None,
            "real_flow":  buy_vol is not None and sell_vol is not None,
            "n":          len(bars),
        }
    return jsonify(out)


@app.route("/api/futures/footprint")
def api_futures_footprint():
    """Per-bar buy/sell/delta/cvd series for charting.

    Query: ?symbol=GCM6&tf=1m&n=60
    Returns: {symbol, tf, real_flow, bars: [{ts, close, buy, sell, delta, cvd}]}
    """
    from order_flow_engine.src import ingest

    sym = request.args.get("symbol", "GCM6").strip().upper()
    tf  = request.args.get("tf", "1m")
    n   = max(2, min(int(request.args.get("n", 60)), 500))
    bars = ingest.get_recent_bars(sym, tf, n)
    out = {"symbol": sym, "tf": tf, "real_flow": False, "bars": []}
    if not bars:
        return jsonify(out)

    cvd = 0.0
    cum_pv, cum_v = 0.0, 0.0
    real_flow = all(b.get("buy_vol_real") is not None for b in bars)
    out["real_flow"] = real_flow
    for b in bars:
        buy  = float(b.get("buy_vol_real") or 0)
        sell = float(b.get("sell_vol_real") or 0)
        if not real_flow:
            vol = float(b.get("Volume") or 0)
            hi, lo, cl = float(b["High"]), float(b["Low"]), float(b["Close"])
            clv = ((cl - lo) - (hi - cl)) / (hi - lo) if hi > lo else 0
            buy  = vol * (1 + clv) / 2
            sell = vol * (1 - clv) / 2
        delta = buy - sell
        cvd += delta
        close = float(b["Close"])
        # session VWAP — typical price ~ close, weighted by total bar volume
        bar_vol = float(b.get("Volume") or (buy + sell))
        cum_pv += close * bar_vol
        cum_v  += bar_vol
        vwap = (cum_pv / cum_v) if cum_v > 0 else close
        out["bars"].append({
            "ts":     str(b.get("ts")),
            "close":  close,
            "high":   float(b.get("High", close)),
            "low":    float(b.get("Low", close)),
            "volume": round(bar_vol, 2),
            "buy":    round(buy, 2),
            "sell":   round(sell, 2),
            "delta":  round(delta, 2),
            "cvd":    round(cvd, 2),
            "vwap":   round(vwap, 4),
        })
    return jsonify(out)


@app.route("/api/futures/volume-profile")
def api_futures_volume_profile():
    """Horizontal volume profile: volume binned by price level.

    Query: ?symbol=GCM6&tf=1m&n=240&bins=30
    Returns: {symbol, tf, real_flow, bins:[{low,high,buy,sell,total}], poc, vah, val, last_close}
    """
    from order_flow_engine.src import ingest

    sym = request.args.get("symbol", "GCM6").strip().upper()
    tf  = request.args.get("tf", "1m")
    n     = max(2, min(int(request.args.get("n", 240)), 500))
    nbins = max(5, min(int(request.args.get("bins", 30)), 100))
    bars = ingest.get_recent_bars(sym, tf, n)
    if not bars:
        return jsonify({"symbol": sym, "tf": tf, "bins": [], "real_flow": False})

    real = all(b.get("buy_vol_real") is not None for b in bars)
    closes = [float(b["Close"]) for b in bars]
    p_lo, p_hi = min(closes), max(closes)
    if p_hi == p_lo:
        p_hi = p_lo + 1.0
    bw = (p_hi - p_lo) / nbins
    bins = [{"low": p_lo + i*bw, "high": p_lo + (i+1)*bw, "buy": 0.0, "sell": 0.0, "total": 0.0}
            for i in range(nbins)]

    for b in bars:
        cl = float(b["Close"])
        if real:
            buy  = float(b.get("buy_vol_real")  or 0)
            sell = float(b.get("sell_vol_real") or 0)
        else:
            vol = float(b.get("Volume") or 0)
            hi, lo = float(b["High"]), float(b["Low"])
            clv = ((cl - lo) - (hi - cl)) / (hi - lo) if hi > lo else 0
            buy  = vol * (1 + clv) / 2
            sell = vol * (1 - clv) / 2
        idx = min(nbins - 1, max(0, int((cl - p_lo) / bw)))
        bins[idx]["buy"]  += buy
        bins[idx]["sell"] += sell
        bins[idx]["total"] += buy + sell

    total = sum(b["total"] for b in bins) or 1
    poc_idx = max(range(nbins), key=lambda i: bins[i]["total"])
    target = total * 0.70
    acc = bins[poc_idx]["total"]
    lo_i = hi_i = poc_idx
    while acc < target and (lo_i > 0 or hi_i < nbins - 1):
        nxt_lo = bins[lo_i - 1]["total"] if lo_i > 0 else -1
        nxt_hi = bins[hi_i + 1]["total"] if hi_i < nbins - 1 else -1
        if nxt_hi >= nxt_lo:
            hi_i += 1
            acc += bins[hi_i]["total"]
        else:
            lo_i -= 1
            acc += bins[lo_i]["total"]

    return jsonify({
        "symbol": sym, "tf": tf, "real_flow": real,
        "bins": [{
            "low":   round(b["low"], 4),
            "high":  round(b["high"], 4),
            "buy":   round(b["buy"], 2),
            "sell":  round(b["sell"], 2),
            "total": round(b["total"], 2),
        } for b in bins],
        "poc":  {"idx": poc_idx, "price": round((bins[poc_idx]["low"] + bins[poc_idx]["high"]) / 2, 4)},
        "vah":  round(bins[hi_i]["high"], 4),
        "val":  round(bins[lo_i]["low"], 4),
        "last_close": round(closes[-1], 4),
    })


@app.route("/api/futures/grid")
def api_futures_grid():
    """Compact multi-symbol snapshot for the grid panel.

    Query: ?symbols=ESM6,GCM6,NQM6,CLM6&tf=1m&n=30
    Returns: {tf, snapshots: {SYM: {last, change_pct, vwap, cvd, buy_now, sell_now, closes, last_ts}}}
    """
    from order_flow_engine.src import ingest

    raw = request.args.get("symbols", "ESM6,GCM6,NQM6,CLM6")
    tf  = request.args.get("tf", "1m")
    n   = max(2, min(int(request.args.get("n", 30)), 240))
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    out: dict = {"tf": tf, "snapshots": {}}
    for sym in symbols:
        bars = ingest.get_recent_bars(sym, tf, n)
        if not bars:
            continue
        closes = [float(b["Close"]) for b in bars]
        cvd = 0.0
        pv  = 0.0
        v   = 0.0
        last_buy = last_sell = 0.0
        real = all(b.get("buy_vol_real") is not None for b in bars)
        for b in bars:
            cl = float(b["Close"])
            if real:
                buy  = float(b.get("buy_vol_real")  or 0)
                sell = float(b.get("sell_vol_real") or 0)
            else:
                vol = float(b.get("Volume") or 0)
                hi, lo = float(b["High"]), float(b["Low"])
                clv = ((cl - lo) - (hi - cl)) / (hi - lo) if hi > lo else 0
                buy  = vol * (1 + clv) / 2
                sell = vol * (1 - clv) / 2
            cvd += (buy - sell)
            bar_vol = float(b.get("Volume") or (buy + sell))
            pv += cl * bar_vol
            v  += bar_vol
            last_buy, last_sell = buy, sell
        out["snapshots"][sym] = {
            "last":       closes[-1],
            "change_pct": ((closes[-1] / closes[0]) - 1) * 100 if closes[0] else 0,
            "vwap":       round(pv / v, 4) if v > 0 else closes[-1],
            "cvd":        round(cvd, 1),
            "buy_now":    round(last_buy, 1),
            "sell_now":   round(last_sell, 1),
            "closes":     [round(c, 4) for c in closes],
            "last_ts":    str(bars[-1].get("ts")),
            "real_flow":  real,
        }
    return jsonify(out)


@app.route("/api/futures/quote")
def api_futures_quote():
    """Best bid/ask from MBP-1 (Live SDK). Falls back to last close ± 1 tick."""
    sym = request.args.get("symbol", "GCM6").strip().upper()
    try:
        from order_flow_engine.src import realtime_databento_live as rdl
        q = rdl.get_best_quote(sym)
    except Exception:
        q = None
    if q:
        return jsonify({"symbol": sym, "source": "mbp-1", **q})
    # Fallback: synthesize from last close
    from order_flow_engine.src import ingest
    bar = ingest.get_latest_bar(sym, "1m")
    if not bar:
        return jsonify({"symbol": sym, "source": "none"})
    tick = {"GCM6": 0.10, "CLM6": 0.01}.get(sym, 0.25)
    cl = float(bar["Close"])
    return jsonify({
        "symbol": sym, "source": "synth",
        "bid": round(cl - tick, 4), "ask": round(cl + tick, 4),
        "bid_sz": 0, "ask_sz": 0, "spread": tick * 2,
        "ts": str(bar.get("ts")),
    })


@app.route("/api/futures/heatmap")
def api_futures_heatmap():
    """Trade-print heatmap.

    Query: ?symbol=GCM6&minutes=10&tick=auto&time_bins=60
    Returns:
      {symbol, tick, p_lo, p_hi, last_close,
       price_bins:[{price, buy, sell, total}],          # 1D
       grid:[[vol, ...]], grid_x_secs:[t0,...], grid_p:[p0,...]} # 2D
    """
    from order_flow_engine.src import realtime_databento as rd

    sym = request.args.get("symbol", "GCM6").strip().upper()
    minutes = max(1, min(int(request.args.get("minutes", 10)), 60))
    tick_arg = request.args.get("tick", "auto")
    time_bins = max(10, min(int(request.args.get("time_bins", 60)), 240))

    tick_defaults = {"ESM6": 0.25, "NQM6": 0.25, "GCM6": 0.10, "CLM6": 0.01}
    tick = tick_defaults.get(sym, 0.25) if tick_arg == "auto" else float(tick_arg)

    trades = rd.get_tape(sym, 500)
    if not trades:
        return jsonify({"symbol": sym, "tick": tick, "price_bins": [], "grid": []})

    import datetime as _dt
    now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
    cutoff = now_ts - minutes * 60
    rows = []
    for t in trades:
        try:
            ts = _dt.datetime.fromisoformat(t["ts"]).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        rows.append((ts, float(t["price"]), float(t["size"]), t.get("side", "buy")))
    if not rows:
        return jsonify({"symbol": sym, "tick": tick, "price_bins": [], "grid": []})

    prices = [r[1] for r in rows]
    p_lo = (round(min(prices) / tick) - 1) * tick
    p_hi = (round(max(prices) / tick) + 1) * tick
    nbins_p = max(1, int(round((p_hi - p_lo) / tick)))
    bins = [{"price": round(p_lo + (i + 0.5) * tick, 4),
             "buy": 0.0, "sell": 0.0, "total": 0.0}
            for i in range(nbins_p)]
    # 2D matrix [time_bins][nbins_p]
    grid = [[0.0] * nbins_p for _ in range(time_bins)]
    t_step = (minutes * 60) / time_bins
    for ts, price, size, side in rows:
        pi = min(nbins_p - 1, max(0, int((price - p_lo) / tick)))
        ti = min(time_bins - 1, max(0, int((ts - cutoff) / t_step)))
        if side == "buy":
            bins[pi]["buy"] += size
        else:
            bins[pi]["sell"] += size
        bins[pi]["total"] += size
        grid[ti][pi] += size

    grid_p = [round(p_lo + (i + 0.5) * tick, 4) for i in range(nbins_p)]
    grid_x_secs = [int(cutoff + i * t_step) for i in range(time_bins)]

    return jsonify({
        "symbol": sym, "tick": tick, "minutes": minutes,
        "p_lo": round(p_lo, 4), "p_hi": round(p_hi, 4),
        "last_close": round(rows[-1][1], 4),
        "price_bins": [{**b, "buy": round(b["buy"], 2),
                        "sell": round(b["sell"], 2),
                        "total": round(b["total"], 2)} for b in bins],
        "grid": grid,
        "grid_p": grid_p,
        "grid_x_secs": grid_x_secs,
    })


@app.route("/api/futures/tape")
def api_futures_tape():
    """Last N raw trades for a contract from the live tape ring buffer.

    Query: ?symbol=GCM6&n=50&min_size=0
    Returns: {symbol, trades:[{ts, price, size, side}], p90_size}
    """
    from order_flow_engine.src import realtime_databento as rd

    sym = request.args.get("symbol", "GCM6").strip().upper()
    n   = max(1, min(int(request.args.get("n", 50)), 500))
    min_size = float(request.args.get("min_size", 0) or 0)

    raw = rd.get_tape(sym, n * 5 if min_size > 0 else n)
    if min_size > 0:
        raw = [t for t in raw if t["size"] >= min_size]
    raw = raw[-n:]
    p90 = 0.0
    if raw:
        sizes = sorted(t["size"] for t in raw)
        p90 = sizes[int(len(sizes) * 0.9)] if len(sizes) > 1 else sizes[0]
    return jsonify({"symbol": sym, "trades": raw, "p90_size": p90})


@app.route("/api/futures/footprint-bars")
def api_futures_footprint_bars():
    """Per-bar footprint: OHLC + per-price-level buy/sell volumes from live tape.

    Query: ?symbol=ESM6&tf=1m&n=20&tick=0.25
    Returns: {symbol, tf, tick, real_flow, bars:[{ts,o,h,l,c,buy_total,
              sell_total,delta,vol,levels:[{price,buy,sell}],poc}]}
    """
    from order_flow_engine.src import ingest, realtime_databento as rd
    import pandas as pd

    sym = request.args.get("symbol", "ESM6").strip().upper()
    tf  = request.args.get("tf", "1m")
    n   = max(2, min(int(request.args.get("n", 20)), 60))
    tick = float(request.args.get("tick", 0.25) or 0.25)

    bars = ingest.get_recent_bars(sym, tf, n)
    if not bars:
        return jsonify({"symbol": sym, "tf": tf, "tick": tick, "bars": [],
                        "real_flow": False})

    tape = rd.get_tape(sym, 500)
    trades_df = pd.DataFrame(tape) if tape else pd.DataFrame(
        columns=["ts", "price", "size", "side"]
    )
    if not trades_df.empty:
        trades_df["ts"] = pd.to_datetime(trades_df["ts"], utc=True)

    unit_min = {"m": 1, "h": 60, "d": 1440}.get(tf[-1], 1)
    try:
        bar_min = int(tf[:-1]) * unit_min
    except Exception:
        bar_min = 1
    bar_td = pd.Timedelta(minutes=bar_min)

    real_flow = all(b.get("buy_vol_real") is not None for b in bars)
    out_bars = []
    for b in bars:
        ts_end = pd.Timestamp(b["ts"])
        if ts_end.tzinfo is None:
            ts_end = ts_end.tz_localize("UTC")
        ts_start = ts_end - bar_td
        hi, lo = float(b["High"]), float(b["Low"])
        op = float(b.get("Open", b["Close"]))
        cl = float(b["Close"])

        # Aggregate trades that fall inside this bar window into price bins.
        levels: dict[float, dict] = {}
        if not trades_df.empty:
            mask = (trades_df["ts"] > ts_start) & (trades_df["ts"] <= ts_end) \
                   & (trades_df["price"] >= lo) & (trades_df["price"] <= hi)
            sub = trades_df[mask]
            for _, t in sub.iterrows():
                p_bin = round(round(float(t["price"]) / tick) * tick, 4)
                lv = levels.setdefault(p_bin, {"buy": 0.0, "sell": 0.0})
                if t["side"] == "buy":
                    lv["buy"]  += float(t["size"])
                else:
                    lv["sell"] += float(t["size"])

        # Fallback: if no tape data inside window, distribute bar buy/sell
        # evenly across the H-L range using OHLC proxy. Keeps chart usable.
        if not levels:
            n_bins = max(1, int(round((hi - lo) / tick)) + 1)
            bvol = float(b.get("buy_vol_real") or 0)
            svol = float(b.get("sell_vol_real") or 0)
            if bvol == 0 and svol == 0:
                vol = float(b.get("Volume") or 0)
                clv = ((cl - lo) - (hi - cl)) / (hi - lo) if hi > lo else 0
                bvol = vol * (1 + clv) / 2
                svol = vol * (1 - clv) / 2
            per_buy  = bvol / n_bins if n_bins else 0
            per_sell = svol / n_bins if n_bins else 0
            for i in range(n_bins):
                p_bin = round(lo + i * tick, 4)
                levels[p_bin] = {"buy": per_buy, "sell": per_sell}

        lvl_list = sorted(
            ({"price": p, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2)}
             for p, v in levels.items()),
            key=lambda r: r["price"],
        )
        buy_total  = sum(l["buy"]  for l in lvl_list)
        sell_total = sum(l["sell"] for l in lvl_list)
        poc = max(lvl_list, key=lambda r: r["buy"] + r["sell"])["price"] \
              if lvl_list else cl
        out_bars.append({
            "ts": ts_end.isoformat(),
            "o": op, "h": hi, "l": lo, "c": cl,
            "buy_total":  round(buy_total, 2),
            "sell_total": round(sell_total, 2),
            "delta":      round(buy_total - sell_total, 2),
            "vol":        round(buy_total + sell_total, 2),
            "levels":     lvl_list,
            "poc":        poc,
        })
    return jsonify({"symbol": sym, "tf": tf, "tick": tick,
                    "real_flow": real_flow, "bars": out_bars})


_RAW_TO_PARENT = {
    "ES": "ES.FUT", "NQ": "NQ.FUT", "GC": "GC.FUT", "SI": "SI.FUT",
    "CL": "CL.FUT", "ZN": "ZN.FUT", "ZB": "ZB.FUT", "YM": "YM.FUT",
    "RTY": "RTY.FUT",
}
_RAW_RE = __import__("re").compile(r"^([A-Z]{1,3})[FGHJKMNQUVXZ]\d{1,2}$")


def _fetch_historical_trades(symbol: str, start_iso: str, end_iso: str) -> list[dict]:
    """Pull raw trades from Databento Historical for [start, end] and apply
    Lee–Ready tick-rule for aggressor side. Returns list[{ts,price,size,side}].

    For windows > ~5 days, raw single-contract symbols (ESM6) miss volume from
    prior front-month contracts. Auto-switch to parent (ES.FUT) with stype_in
    parent so Databento aggregates the continuous front-month series.
    """
    import os
    import pandas as pd
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        return []
    try:
        import databento as db
    except ImportError:
        return []

    # Decide stype: parent for long windows on known contracts.
    stype = "raw_symbol"
    sym_query = symbol
    try:
        win_days = (pd.Timestamp(end_iso) - pd.Timestamp(start_iso)).days
    except Exception:
        win_days = 0
    if win_days >= 5:
        m = _RAW_RE.match(symbol)
        if m and m.group(1) in _RAW_TO_PARENT:
            sym_query = _RAW_TO_PARENT[m.group(1)]
            stype = "parent"

    # Long windows: trades schema returns millions of records → timeout.
    # Fall back to ohlcv-1m + CLV split to synthesize per-trade buckets.
    use_trades = win_days <= 2
    schema = "trades" if use_trades else "ohlcv-1m"

    import re as _re
    _AVAIL_RE = _re.compile(r"data available up to '([^']+)'")
    df = None
    try:
        client = db.Historical(key)
        data = client.timeseries.get_range(
            dataset="GLBX.MDP3", symbols=sym_query, stype_in=stype,
            schema=schema, start=start_iso, end=end_iso,
        )
        df = data.to_df()
    except Exception as e:
        msg = str(e)
        m = _AVAIL_RE.search(msg)
        if m:
            try:
                avail_end = pd.Timestamp(m.group(1)).tz_convert("UTC").isoformat()
                data = client.timeseries.get_range(
                    dataset="GLBX.MDP3", symbols=sym_query, stype_in=stype,
                    schema=schema, start=start_iso, end=avail_end,
                )
                df = data.to_df()
            except Exception as e2:
                from utils.logger import setup_logger
                setup_logger(__name__).warning(
                    f"historical {schema} {sym_query} retry: {e2}")
                return []
        else:
            from utils.logger import setup_logger
            setup_logger(__name__).warning(
                f"historical {schema} {sym_query} ({stype}): {msg}")
            return []
    if df is None or df.empty:
        return []

    # ohlcv-1m path: synthesize trades from each 1m bar via CLV proxy
    if not use_trades:
        # Drop calendar-spread instruments (e.g. "ESM6-ESU6") returned by
        # parent stype. Outright contract regex enforces single underlying.
        if stype == "parent" and "symbol" in df.columns:
            outright = df["symbol"].astype(str).str.match(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$")
            df = df[outright]
            if df.empty:
                return []
            # Per minute, prefer the contract with most volume (front-month).
            cols = {c.lower(): c for c in df.columns}
            cv = cols.get("volume")
            if cv:
                df = df.reset_index()
                ts_col = df.columns[0]
                top = df.sort_values(cv, ascending=False).drop_duplicates(
                    subset=[ts_col], keep="first")
                df = top.set_index(ts_col).sort_index()

        out = []
        cols = {c.lower(): c for c in df.columns}
        co, ch, cl, cc, cv = (cols.get("open"), cols.get("high"),
                              cols.get("low"), cols.get("close"),
                              cols.get("volume"))
        # Sanity filter — drop any bar with implausible range (>5% of price).
        # Catches spread junk ($55 lows on a $7000 contract).
        for ts, row in df.iterrows():
            o = float(row[co]); h = float(row[ch])
            lo = float(row[cl]); c = float(row[cc])
            v = float(row[cv]) if cv else 0.0
            if v <= 0 or h <= lo:
                continue
            if h <= 0 or (h - lo) / h > 0.05:
                continue
            clv = ((c - lo) - (h - c)) / (h - lo)
            buy_share  = max(0.0, min(1.0, (1 + clv) / 2))
            sell_share = 1.0 - buy_share
            ts_iso = pd.Timestamp(ts).tz_convert("UTC").isoformat()
            out.append({"ts": ts_iso, "price": (o + c) / 2,
                        "size": v * buy_share, "side": "buy"})
            out.append({"ts": ts_iso, "price": (o + c) / 2,
                        "size": v * sell_share, "side": "sell"})
            out.append({"ts": ts_iso, "price": h, "size": 0.001, "side": "buy"})
            out.append({"ts": ts_iso, "price": lo, "size": 0.001, "side": "sell"})
        return out
    if df is None or df.empty:
        return []
    df = df[["price", "size"]].copy()
    df["price"] = df["price"].astype(float)
    df["size"]  = df["size"].astype(float)
    diff = df["price"].diff()
    import pandas as pd
    sign = pd.Series(0, index=df.index, dtype=int)
    sign[diff > 0] = +1
    sign[diff < 0] = -1
    sign = sign.replace(0, pd.NA).ffill().fillna(+1).astype(int)
    sides = ["buy" if s > 0 else "sell" for s in sign.to_numpy()]
    out = []
    for i, (ts, row) in enumerate(df.iterrows()):
        out.append({
            "ts":    pd.Timestamp(ts).tz_convert("UTC").isoformat(),
            "price": float(row["price"]),
            "size":  float(row["size"]),
            "side":  sides[i],
        })
    return out


@app.route("/api/futures/range-bars")
def api_futures_range_bars():
    """Range bars (price-excursion based, not time) with per-bar footprint
    + aggregate VP. Source = live tape OR Databento historical when both
    `start` and `end` provided (ISO 8601, UTC).

    Query: ?symbol=ESM6&range=20&tick=0.25&maxbars=20&taken=2000
           [&start=2026-04-28T13:30:00Z&end=2026-04-28T20:00:00Z]
    """
    from order_flow_engine.src import realtime_databento as rd

    sym       = request.args.get("symbol", "ESM6").strip().upper()
    rng_ticks = max(1, int(request.args.get("range", 20)))
    tick      = float(request.args.get("tick", 0.25) or 0.25)
    maxbars   = max(2, min(int(request.args.get("maxbars", 20)), 200))
    offset    = max(0, int(request.args.get("offset", 0) or 0))
    taken     = max(100, min(int(request.args.get("taken", 2000)), 5000))
    start_iso = request.args.get("start", "").strip()
    end_iso   = request.args.get("end", "").strip()

    if start_iso and end_iso:
        trades = _fetch_historical_trades(sym, start_iso, end_iso)
        source = "historical"
    else:
        trades = rd.get_tape(sym, taken)
        source = "live"
    if not trades:
        return jsonify({"symbol": sym, "range": rng_ticks, "tick": tick,
                        "real_flow": False, "source": source,
                        "start": start_iso or None, "end": end_iso or None,
                        "bars": [],
                        "profile": {"levels": [], "total_vol": 0,
                                    "poc": None, "vah": None, "val": None}})

    rng_dist = rng_ticks * tick
    bars: list[dict] = []
    cur: dict | None = None

    def _init_bar(t):
        p = float(t["price"])
        return {
            "ts_open":   t["ts"], "ts_close": t["ts"],
            "o": p, "h": p, "l": p, "c": p,
            "buy_total": 0.0, "sell_total": 0.0, "trades": 0,
            "_levels": {},  # price-bin -> {buy, sell}
        }

    for t in trades:
        if cur is None:
            cur = _init_bar(t)
        p = float(t["price"]); s = float(t["size"]); side = t.get("side", "buy")
        if p > cur["h"]: cur["h"] = p
        if p < cur["l"]: cur["l"] = p
        cur["c"] = p
        cur["ts_close"] = t["ts"]
        cur["trades"] += 1
        if side == "buy":
            cur["buy_total"]  += s
        else:
            cur["sell_total"] += s
        p_bin = round(round(p / tick) * tick, 4)
        lv = cur["_levels"].setdefault(p_bin, {"buy": 0.0, "sell": 0.0})
        if side == "buy":
            lv["buy"]  += s
        else:
            lv["sell"] += s
        if (cur["h"] - cur["l"]) >= rng_dist - 1e-9:
            bars.append(cur)
            cur = None

    if cur is not None and cur["trades"] > 0:
        bars.append(cur)

    total_bars = len(bars)
    # Window into the carved bars: offset = bars to skip from the most-recent
    # end. offset=0 → latest N. offset=N → previous page.
    end_idx = total_bars - offset
    start_idx = max(0, end_idx - maxbars)
    bars = bars[start_idx:end_idx]

    def _compute_va(levels_sorted: list[dict], pct: float = 0.70):
        """Return (poc, vah, val) using expand-toward-larger-side from POC
        until cumulative volume >= pct * total."""
        if not levels_sorted:
            return None, None, None
        totals = [(r["buy"] + r["sell"]) for r in levels_sorted]
        tot = sum(totals)
        if tot <= 0:
            return None, None, None
        poc_i = max(range(len(totals)), key=lambda i: totals[i])
        target = tot * pct
        lo = hi = poc_i
        acc = totals[poc_i]
        while acc < target and (lo > 0 or hi < len(totals) - 1):
            up = totals[hi+1] if hi < len(totals) - 1 else -1
            dn = totals[lo-1] if lo > 0 else -1
            if up >= dn and hi < len(totals) - 1:
                hi += 1; acc += totals[hi]
            elif lo > 0:
                lo -= 1; acc += totals[lo]
            else:
                break
        return (levels_sorted[poc_i]["price"],
                levels_sorted[hi]["price"],
                levels_sorted[lo]["price"])

    # Format bars + aggregate profile across visible window
    profile_levels: dict[float, dict] = {}
    out_bars = []
    for b in bars:
        lvls = sorted(
            ({"price": p, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2)}
             for p, v in b["_levels"].items()),
            key=lambda r: r["price"],
        )
        for r in lvls:
            agg = profile_levels.setdefault(r["price"],
                                            {"buy": 0.0, "sell": 0.0})
            agg["buy"]  += r["buy"]
            agg["sell"] += r["sell"]
        vol = b["buy_total"] + b["sell_total"]
        delta = b["buy_total"] - b["sell_total"]
        poc, vah, val = _compute_va(lvls, 0.70)
        if poc is None:
            poc = b["c"]
        out_bars.append({
            "ts_open":    b["ts_open"], "ts_close": b["ts_close"],
            "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "buy_total":  round(b["buy_total"], 2),
            "sell_total": round(b["sell_total"], 2),
            "vol":        round(vol, 2),
            "delta":      round(delta, 2),
            "delta_pct":  round((delta / vol * 100) if vol > 0 else 0, 2),
            "trades":     b["trades"],
            "levels":     lvls,
            "poc":        poc,
            "vah":        vah,
            "val":        val,
        })

    # Aggregate VP — sort + compute POC, VA70 (70% volume around POC)
    prof_list = sorted(
        ({"price": p, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2),
          "total": round(v["buy"] + v["sell"], 2)}
         for p, v in profile_levels.items()),
        key=lambda r: r["price"],
    )
    total_vol = sum(r["total"] for r in prof_list)
    poc, vah, val = _compute_va(prof_list, 0.70)

    return jsonify({
        "symbol": sym, "range": rng_ticks, "tick": tick,
        "real_flow": True, "source": source,
        "start": start_iso or None, "end": end_iso or None,
        "offset": offset, "total_bars": total_bars,
        "has_older": start_idx > 0, "has_newer": offset > 0,
        "bars": out_bars,
        "profile": {"levels": prof_list, "total_vol": round(total_vol, 2),
                    "poc": poc, "vah": vah, "val": val},
    })


@app.route("/footprint")
def footprint_view():
    return render_template("footprint_chart.html")


@app.route("/footprint-deep")
def footprint_deep_view():
    """DeepChart-style canvas footprint chart (separate from SVG /footprint)."""
    return render_template("footprint_deep.html")


# ── Pine Script library + TV webhook receiver ───────────────────────────────
PINE_DIR = Path(__file__).resolve().parent / "pine"

PINE_FILES = {
    "of_proxy":       {"file": "of_proxy.pine",
                       "title": "OrderFlow Proxy · CVD + Δ + R-rules",
                       "desc": "CVD line, delta histogram, Δ% labels, R1/R3/R6/R7 detection rules + webhook alerts."},
    "footprint_lite": {"file": "footprint_lite.pine",
                       "title": "Footprint Lite · D-VP",
                       "desc": "Per-bar box-grid colored by per-level delta sign (CLV proxy)."},
    "va_profile":     {"file": "va_profile.pine",
                       "title": "VA Profile · POC / VAH / VAL 70%",
                       "desc": "Rolling N-bar volume profile with Value Area (70%) lines."},
}


@app.route("/pine")
def pine_index():
    return render_template("pine_library.html", files=PINE_FILES)


@app.route("/api/pine/<name>")
def api_pine_file(name):
    meta = PINE_FILES.get(name)
    if not meta:
        return jsonify({"error": "unknown script"}), 404
    path = PINE_DIR / meta["file"]
    if not path.exists():
        return jsonify({"error": "file missing"}), 500
    return path.read_text(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/pine/<name>/download")
def api_pine_download(name):
    meta = PINE_FILES.get(name)
    if not meta:
        return jsonify({"error": "unknown script"}), 404
    path = PINE_DIR / meta["file"]
    if not path.exists():
        return jsonify({"error": "file missing"}), 500
    return send_file(str(path), as_attachment=True, download_name=meta["file"],
                     mimetype="text/plain")


@app.route("/api/order-flow/tv-alert", methods=["POST"])
def api_tv_alert():
    """Receive TradingView Pine Script webhook alerts.
    Expected JSON body (matches our pine `mkAlert()` output):
      {source, symbol, tf, label, confidence, price, delta_pct, cvd}
    Persisted to outputs/order_flow/tv_alerts.jsonl + alerts.jsonl pipeline.
    """
    payload = request.get_json(force=True, silent=True) or {}
    if not payload:
        return jsonify({"error": "empty body"}), 400
    payload["received_utc"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("source", "tv_pine")

    out_dir = Path("outputs/order_flow")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Append to dedicated tv_alerts.jsonl for easy filtering
    with (out_dir / "tv_alerts.jsonl").open("a") as f:
        f.write(json.dumps(payload) + "\n")
    # Also fan into main alerts pipeline so dashboards see it
    try:
        from order_flow_engine.src import alert_engine
        alert = alert_engine.build_alert(
            timestamp=payload.get("received_utc"),
            symbol=str(payload.get("symbol", "TV")),
            timeframe=str(payload.get("tf", "1m")),
            label=str(payload.get("label", "tv_signal")),
            confidence=int(payload.get("confidence", 60)),
            price=float(payload.get("price", 0) or 0),
            atr=0.0,
            rules_fired=[payload.get("label", "tv_pine")],
            metrics={
                "delta_pct": payload.get("delta_pct"),
                "cvd":       payload.get("cvd"),
            },
            model_info={"version": "tv_pine"},
            proxy_mode=True,
            pass_type="tv",
        )
        alert_engine.append_jsonl(alert, output_dir=out_dir)
    except Exception as e:
        return jsonify({"ok": True, "fanout_error": str(e)})
    return jsonify({"ok": True, "alert": alert})


@app.route("/api/futures/time-footprint")
def api_futures_time_footprint():
    """Time-based footprint bars: same shape as range-bars but bins by
    fixed-duration windows instead of price excursion.

    Query: ?symbol=ESM6&tf=5m&n=40&tick=0.25[&start=...&end=...]
    Returns: {symbol, tf, tick, source, bars:[{ts_open,ts_close,o,h,l,c,
              buy_total,sell_total,delta,vol,trades,levels,poc,vah,val}],
              profile:{...}, total_bars, has_older, has_newer, offset}
    """
    from order_flow_engine.src import realtime_databento as rd
    import pandas as pd

    sym       = request.args.get("symbol", "ESM6").strip().upper()
    tf        = request.args.get("tf", "5m").strip()
    n_bars    = max(2, min(int(request.args.get("n", 40)), 200))
    tick      = float(request.args.get("tick", 0.25) or 0.25)
    offset    = max(0, int(request.args.get("offset", 0) or 0))
    start_iso = request.args.get("start", "").strip()
    end_iso   = request.args.get("end", "").strip()

    # Parse tf to seconds
    unit = tf[-1].lower()
    try:
        tf_sec = int(tf[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    except Exception:
        return jsonify({"error": f"bad tf: {tf}"}), 400

    if start_iso and end_iso:
        trades = _fetch_historical_trades(sym, start_iso, end_iso)
        source = "historical"
    else:
        trades = rd.get_tape(sym, 5000)
        source = "live"

    if not trades:
        return jsonify({"symbol": sym, "tf": tf, "tick": tick,
                        "source": source, "bars": [],
                        "total_bars": 0, "offset": offset,
                        "has_older": False, "has_newer": False,
                        "profile": {"levels": [], "total_vol": 0,
                                    "poc": None, "vah": None, "val": None}})

    # Group trades by floor(ts / tf_sec) bucket
    buckets: dict[int, dict] = {}
    for t in trades:
        ts_ms = pd.Timestamp(t["ts"]).value // 1_000_000  # → ms
        ts_sec = ts_ms / 1000
        key = int(ts_sec // tf_sec)
        bucket = buckets.get(key)
        p = float(t["price"]); s = float(t["size"]); side = t.get("side", "buy")
        if bucket is None:
            bucket = {
                "key": key,
                "ts_open":  key * tf_sec * 1000,
                "ts_close": (key + 1) * tf_sec * 1000,
                "o": p, "h": p, "l": p, "c": p,
                "buy_total": 0.0, "sell_total": 0.0, "trades": 0,
                "_first_ts": ts_ms,
                "_last_ts":  ts_ms,
                "_levels": {},
            }
            buckets[key] = bucket
        if ts_ms < bucket["_first_ts"]:
            bucket["_first_ts"] = ts_ms
            bucket["o"] = p
        if ts_ms >= bucket["_last_ts"]:
            bucket["_last_ts"] = ts_ms
            bucket["c"] = p
        if p > bucket["h"]: bucket["h"] = p
        if p < bucket["l"]: bucket["l"] = p
        bucket["trades"] += 1
        if side == "buy":
            bucket["buy_total"] += s
        else:
            bucket["sell_total"] += s
        p_bin = round(round(p / tick) * tick, 4)
        lv = bucket["_levels"].setdefault(p_bin, {"buy": 0.0, "sell": 0.0})
        if side == "buy":
            lv["buy"]  += s
        else:
            lv["sell"] += s

    # Reuse VA helper from range-bars endpoint via inline copy
    def _compute_va(levels_sorted, pct=0.70):
        if not levels_sorted:
            return None, None, None
        totals = [r["buy"] + r["sell"] for r in levels_sorted]
        tot = sum(totals)
        if tot <= 0:
            return None, None, None
        poc_i = max(range(len(totals)), key=lambda i: totals[i])
        target = tot * pct
        lo = hi = poc_i
        acc = totals[poc_i]
        while acc < target and (lo > 0 or hi < len(totals) - 1):
            up = totals[hi+1] if hi < len(totals) - 1 else -1
            dn = totals[lo-1] if lo > 0 else -1
            if up >= dn and hi < len(totals) - 1:
                hi += 1; acc += totals[hi]
            elif lo > 0:
                lo -= 1; acc += totals[lo]
            else:
                break
        return (levels_sorted[poc_i]["price"],
                levels_sorted[hi]["price"],
                levels_sorted[lo]["price"])

    sorted_buckets = sorted(buckets.values(), key=lambda b: b["key"])
    total_bars = len(sorted_buckets)
    end_idx = total_bars - offset
    start_idx = max(0, end_idx - n_bars)
    sorted_buckets = sorted_buckets[start_idx:end_idx]

    profile_levels: dict[float, dict] = {}
    out_bars = []
    for b in sorted_buckets:
        lvls = sorted(
            ({"price": p, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2)}
             for p, v in b["_levels"].items()),
            key=lambda r: r["price"])
        for r in lvls:
            agg = profile_levels.setdefault(r["price"],
                                            {"buy": 0.0, "sell": 0.0})
            agg["buy"]  += r["buy"]
            agg["sell"] += r["sell"]
        vol = b["buy_total"] + b["sell_total"]
        delta = b["buy_total"] - b["sell_total"]
        poc, vah, val = _compute_va(lvls, 0.70)
        if poc is None:
            poc = b["c"]
        out_bars.append({
            "ts_open":  pd.Timestamp(b["ts_open"],  unit="ms", tz="UTC").isoformat(),
            "ts_close": pd.Timestamp(b["ts_close"], unit="ms", tz="UTC").isoformat(),
            "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "buy_total":  round(b["buy_total"], 2),
            "sell_total": round(b["sell_total"], 2),
            "vol":        round(vol, 2),
            "delta":      round(delta, 2),
            "delta_pct":  round((delta / vol * 100) if vol > 0 else 0, 2),
            "trades":     b["trades"],
            "levels":     lvls,
            "poc":        poc, "vah": vah, "val": val,
        })

    prof_list = sorted(
        ({"price": p, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2),
          "total": round(v["buy"] + v["sell"], 2)}
         for p, v in profile_levels.items()),
        key=lambda r: r["price"])
    total_vol = sum(r["total"] for r in prof_list)
    pp = [{"price": r["price"], "buy": r["buy"], "sell": r["sell"]} for r in prof_list]
    poc, vah, val = _compute_va(pp, 0.70)

    return jsonify({
        "symbol": sym, "tf": tf, "tick": tick,
        "real_flow": True, "source": source,
        "start": start_iso or None, "end": end_iso or None,
        "offset": offset, "total_bars": total_bars,
        "has_older": start_idx > 0, "has_newer": offset > 0,
        "bars": out_bars,
        "profile": {"levels": prof_list, "total_vol": round(total_vol, 2),
                    "poc": poc, "vah": vah, "val": val},
    })


@app.route("/api/futures/quotes")
def api_futures_quotes():
    """Last-trade prices for futures contracts pulled from the engine's
    in-memory Databento tail. Lag = Databento Historical (~5-7 min).

    Query: ?symbols=ESM6,GCM6,CLM6,NQM6&tf=1m
    Returns {"quotes": {SYM: {price, ts, source: "databento"}}, "enabled": bool}.
    """
    from order_flow_engine.src import ingest

    raw = request.args.get("symbols", "")
    tf  = request.args.get("tf", "1m")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    enabled = bool(os.getenv("DATABENTO_API_KEY"))
    if not enabled or not symbols:
        return jsonify({"quotes": {}, "enabled": enabled})

    quotes: dict[str, dict] = {}
    for sym in symbols:
        bar = ingest.get_latest_bar(sym, tf)
        if not bar:
            continue
        ts = bar.get("ts")
        quotes[sym] = {
            "price":  float(bar["Close"]),
            "ts":     str(ts) if ts is not None else None,
            "source": "databento",
        }
    return jsonify({"quotes": quotes, "enabled": True})


@app.route("/stocks/explorer")
def stocks_explorer_view():
    """Click-through explorer — one ticker detail panel at a time."""
    from stocks.stock_output import read_overview, read_ticker
    from stocks.stock_universe import UNIVERSE, is_known

    requested = (request.args.get("ticker") or "").upper()
    overview = read_overview()

    # Pick default ticker: explicit → strongest non-HOLD in overview → first
    selected = requested if (requested and is_known(requested)) else None
    if not selected and overview:
        non_hold = [
            r for r in (overview.get("stocks") or [])
            if r.get("signal") and r["signal"] != "HOLD" and r.get("total_score") is not None
        ]
        if non_hold:
            selected = max(non_hold, key=lambda r: abs(r["total_score"]))["ticker"]
    if not selected:
        selected = UNIVERSE[0].ticker

    detail = read_ticker(selected) if selected else None

    from stocks.stock_universe import by_sector
    scanned_by_ticker = {
        row["ticker"]: row for row in (overview.get("stocks") if overview else []) or []
    }
    rail_sectors = []
    for sector, stocks in by_sector().items():
        entries = []
        for s in stocks:
            row = scanned_by_ticker.get(s.ticker)
            entries.append({
                "ticker":  s.ticker,
                "name":    s.name,
                "sector":  s.sector,
                "signal":  row.get("signal") if row else None,
                "scanned": row is not None,
                "error":   bool(row and row.get("error")),
            })
        rail_sectors.append({
            "sector":  sector,
            "count":   len(entries),
            "scanned": sum(1 for e in entries if e["scanned"]),
            "entries": entries,
            "has_selected": any(e["ticker"] == selected for e in entries),
        })

    return render_template(
        "stocks_explorer.html",
        overview=overview,
        rail_sectors=rail_sectors,
        selected=selected,
        detail=detail,
        page="stocks",
    )


@app.route("/api/stocks/list")
def api_stocks_list():
    from stocks.stock_universe import UNIVERSE
    return jsonify([
        {"ticker": s.ticker, "name": s.name, "sector": s.sector}
        for s in UNIVERSE
    ])


@app.route("/api/stocks/overview")
def api_stocks_overview():
    from stocks.stock_output import read_overview
    data = read_overview()
    if data is None:
        return jsonify({"ok": False, "error": "no overview yet — run the scanner first"}), 404
    return jsonify(data)


@app.route("/api/stocks/<ticker>")
def api_stocks_ticker(ticker: str):
    from stocks.stock_output import read_ticker
    from stocks.stock_universe import is_known
    if not is_known(ticker):
        return jsonify({"ok": False, "error": f"unknown ticker: {ticker}"}), 404
    data = read_ticker(ticker)
    if data is None:
        return jsonify({"ok": False, "error": "no output yet for this ticker"}), 404
    return jsonify(data)


@app.route("/api/stocks/run", methods=["POST"])
def api_stocks_run():
    """
    Synchronous trigger. Blocks until the scan finishes — full universe
    takes ~2 min. Accepts optional JSON body `{"ticker": "AAPL"}` to scan
    a single name.
    """
    from stocks.stock_pipeline import scan_universe
    from stocks.stock_universe import is_known

    body = request.get_json(silent=True) or {}
    ticker = body.get("ticker")
    fast = bool(body.get("fast", True))   # default fast for web trigger
    tickers = None
    if ticker:
        if not is_known(ticker):
            return jsonify({"ok": False, "error": f"unknown ticker: {ticker}"}), 400
        tickers = [ticker.upper()]

    try:
        overview = scan_universe(tickers=tickers, fast=fast)
    except Exception as e:
        return jsonify({"ok": False, "error": f"scan failed: {e}"}), 500
    return jsonify({"ok": True, "overview": overview})


# ── Scheduler API ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Return current scheduler / run state as JSON."""
    return jsonify(sched.get_status())


@app.route("/api/run", methods=["POST"])
def api_run():
    """Trigger an immediate pipeline run in the background."""
    tf = request.json.get("timeframe") if request.is_json else None
    started = sched.trigger_run(timeframe=tf)
    if started:
        return jsonify({"ok": True,  "message": "Run started"})
    return jsonify({"ok": False, "message": "A run is already in progress"}), 409


# ── OHLC feed for client-side charts (Lightweight Charts) ─────────────────

@app.route("/api/ohlc")
def api_ohlc():
    """Daily OHLCV for any universe ticker. Used by Lightweight Charts.
    Returns [{time: 'YYYY-MM-DD', open, high, low, close, volume}, ...]"""
    from stocks.stock_market import fetch_ohlcv
    from stocks.stock_universe import is_known

    symbol = (request.args.get("symbol") or "").upper()
    lookback = int(request.args.get("lookback") or 250)
    lookback = max(30, min(lookback, 1500))

    if not symbol or not is_known(symbol):
        return jsonify({"error": f"unknown symbol: {symbol}"}), 404

    df = fetch_ohlcv(symbol, lookback_days=lookback)
    if df is None or df.empty:
        return jsonify({"error": "no data"}), 502

    bars = []
    for ts, row in df.iterrows():
        bars.append({
            "time":   ts.strftime("%Y-%m-%d"),
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })
    return jsonify({"symbol": symbol, "bars": bars})


# ── Custom ticker tape feed ───────────────────────────────────────────────

_TAPE_DEFAULT = [
    ("SPY",  "S&P 500"),
    ("QQQ",  "Nasdaq 100"),
    ("^VIX", "VIX"),
    ("XLK",  "Tech"),
    ("XLC",  "Comms"),
    ("XLY",  "Discr."),
    ("XLP",  "Staples"),
    ("XLF",  "Financials"),
    ("XLV",  "Health"),
    ("XLI",  "Industrials"),
    ("XLE",  "Energy"),
    ("XLB",  "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU",  "Utilities"),
]


@app.route("/api/tape")
def api_tape():
    """Latest close + 1d change % for tape symbols. Direct yfinance fetch,
    not restricted to the S&P 500 universe."""
    import yfinance as yf
    symbols_param = (request.args.get("symbols") or "").strip()
    if symbols_param:
        entries = [(s.strip().upper(), s.strip().upper()) for s in symbols_param.split(",") if s.strip()]
    else:
        entries = _TAPE_DEFAULT

    out = []
    for sym, label in entries:
        try:
            hist = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
            if hist is None or hist.empty or len(hist) < 2:
                out.append({"symbol": sym, "label": label, "price": None, "change_pct": None})
                continue
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            chg  = (last - prev) / prev * 100 if prev else 0.0
            out.append({
                "symbol": sym, "label": label,
                "price":  round(last, 2),
                "change_pct": round(chg, 2),
            })
        except Exception:
            out.append({"symbol": sym, "label": label, "price": None, "change_pct": None})
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


# ── Custom technical gauge (no TradingView feed) ──────────────────────────

def _compute_gauge(symbol: str) -> dict:
    """Run 5 oscillators + 5 moving averages on daily OHLC, tally BUY/SELL/NEUTRAL."""
    import numpy as np
    import pandas as pd
    from stocks.stock_market import fetch_ohlcv

    df = fetch_ohlcv(symbol, lookback_days=300)
    if df is None or df.empty or len(df) < 60:
        return {"error": "insufficient data", "symbol": symbol}

    close = df["Close"].astype(float)
    high  = df["High"].astype(float)
    low   = df["Low"].astype(float)

    def _sig(buy: bool, sell: bool) -> str:
        return "BUY" if buy else "SELL" if sell else "NEUTRAL"

    # ── Oscillators ─────────────────────────────────────────────────────
    oscillators = {}

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    oscillators["RSI(14)"] = {
        "value": round(float(rsi), 2),
        "signal": _sig(rsi < 30, rsi > 70),
    }

    low14 = low.rolling(14).min(); high14 = high.rolling(14).max()
    stoch_k = ((close - low14) / (high14 - low14) * 100).iloc[-1]
    oscillators["Stoch %K"] = {
        "value": round(float(stoch_k), 2),
        "signal": _sig(stoch_k < 20, stoch_k > 80),
    }

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    m_now = float(macd.iloc[-1] - macd_sig.iloc[-1])
    m_prev = float(macd.iloc[-2] - macd_sig.iloc[-2])
    oscillators["MACD"] = {
        "value": round(m_now, 2),
        "signal": _sig(m_now > 0 and m_now > m_prev, m_now < 0 and m_now < m_prev),
    }

    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mean_dev = (tp - sma_tp).abs().rolling(20).mean()
    cci = ((tp - sma_tp) / (0.015 * mean_dev)).iloc[-1]
    oscillators["CCI(20)"] = {
        "value": round(float(cci), 2),
        "signal": _sig(cci < -100, cci > 100),
    }

    wr = ((high14 - close) / (high14 - low14) * -100).iloc[-1]
    oscillators["Williams %R"] = {
        "value": round(float(wr), 2),
        "signal": _sig(wr < -80, wr > -20),
    }

    # ── Moving Averages (compare close vs MA) ───────────────────────────
    def _ma_sig(ma_val: float) -> str:
        if ma_val is None or pd.isna(ma_val): return "NEUTRAL"
        return "BUY" if float(close.iloc[-1]) > float(ma_val) else "SELL"

    mas = {
        "SMA20":  close.rolling(20).mean().iloc[-1],
        "SMA50":  close.rolling(50).mean().iloc[-1],
        "SMA200": close.rolling(min(200, len(close))).mean().iloc[-1] if len(close) >= 50 else None,
        "EMA20":  close.ewm(span=20, adjust=False).mean().iloc[-1],
        "EMA50":  close.ewm(span=50, adjust=False).mean().iloc[-1],
    }
    mov_avgs = {}
    for name, v in mas.items():
        mov_avgs[name] = {
            "value": round(float(v), 2) if v is not None and not pd.isna(v) else None,
            "signal": _ma_sig(v),
        }

    def _tally(items):
        buy = sum(1 for it in items if it["signal"] == "BUY")
        sell = sum(1 for it in items if it["signal"] == "SELL")
        neutral = sum(1 for it in items if it["signal"] == "NEUTRAL")
        return {"buy": buy, "sell": sell, "neutral": neutral}

    osc_tally = _tally(oscillators.values())
    ma_tally  = _tally(mov_avgs.values())

    def _verdict(t):
        b, s, n = t["buy"], t["sell"], t["neutral"]
        if b >= s + n + 1:           return "STRONG_BUY"
        if b > s:                    return "BUY"
        if s >= b + n + 1:           return "STRONG_SELL"
        if s > b:                    return "SELL"
        return "NEUTRAL"

    osc_verdict = _verdict(osc_tally)
    ma_verdict  = _verdict(ma_tally)
    total_tally = {k: osc_tally[k] + ma_tally[k] for k in ["buy", "sell", "neutral"]}
    overall     = _verdict(total_tally)

    return {
        "symbol": symbol,
        "price":  float(close.iloc[-1]),
        "overall": {"verdict": overall, "tally": total_tally},
        "oscillators": {"verdict": osc_verdict, "tally": osc_tally, "detail": oscillators},
        "moving_averages": {"verdict": ma_verdict, "tally": ma_tally, "detail": mov_avgs},
    }


@app.route("/api/gauge")
def api_gauge():
    """Custom technical gauge — 5 oscillators + 5 MAs → overall verdict."""
    symbol = (request.args.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        result = _compute_gauge(symbol)
        if "error" in result:
            return jsonify(result), 502
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "symbol": symbol}), 500


if __name__ == "__main__":
    if sched.is_enabled():
        sched.init_scheduler()
    else:
        sched.init_ml_retrain_only()
    app.run(debug=True, port=5001, use_reloader=False)
