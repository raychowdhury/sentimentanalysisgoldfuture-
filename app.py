"""
NewsSentimentScanner — Dashboard Server

Usage:
    python app.py
    # Open http://localhost:5001
"""

import glob
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, render_template, request, send_file

import config
import scheduler as sched
from sentiment import cache as sentiment_cache

app = Flask(__name__)
OUTPUT_DIR = "outputs"


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
    return render_template(
        "stocks_aggregate.html",
        aggregate=aggregate,
        reliability=reliability,
        weights_meta=weights_meta,
        backtest=backtest,
        last_refresh=last_refresh,
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


# ── TradingView webhook bridge ────────────────────────────────────────────────

TV_WEBHOOK_LOG = os.path.join(OUTPUT_DIR, "tv_webhook_hits.jsonl")
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")


def _tv_sentiment_snapshot():
    """Latest cached sentiment score + direction for agreement check."""
    cache = sentiment_cache.load_full()
    if not cache:
        return {"score": None, "direction": "unknown"}
    latest_date = max(cache.keys())
    rec = cache[latest_date]
    score = float(rec.get("avg_score", 0.0))
    direction = "up" if score > 0.05 else "down" if score < -0.05 else "flat"
    return {
        "date":      latest_date,
        "score":     round(score, 4),
        "direction": direction,
        "n":         int(rec.get("n_articles", 0)),
    }


@app.route("/api/tv_webhook", methods=["POST"])
def api_tv_webhook():
    """
    TradingView alert webhook receiver.

    Expected payload (set in the Pine alert message as JSON):
        {
          "secret":   "<TV_WEBHOOK_SECRET>",
          "ticker":   "GC1!",
          "signal":   "LONG"|"SHORT"|"FLAT",
          "price":    2315.5,
          "bias":     0.21,
          "time":     "2026-04-22T13:05:00Z"
        }

    Cross-checks against current sentiment cache and returns whether the
    Pine signal agrees with the live sentiment bias.
    """
    payload = request.get_json(silent=True) or {}

    if TV_WEBHOOK_SECRET:
        if payload.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "bad secret"}), 403

    signal = str(payload.get("signal", "")).upper()
    if signal not in {"LONG", "SHORT", "FLAT"}:
        return jsonify({"ok": False, "error": "signal must be LONG|SHORT|FLAT"}), 400

    sentiment = _tv_sentiment_snapshot()

    # Agreement check: Pine long agrees with positive sentiment, etc.
    agreement = "unknown"
    if sentiment["direction"] != "unknown":
        if signal == "LONG"  and sentiment["direction"] == "up":   agreement = "agree"
        elif signal == "SHORT" and sentiment["direction"] == "down": agreement = "agree"
        elif signal == "FLAT": agreement = "neutral"
        else: agreement = "disagree"

    entry = {
        "received_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "ticker":      payload.get("ticker"),
        "signal":      signal,
        "price":       payload.get("price"),
        "bias":        payload.get("bias"),
        "tv_time":     payload.get("time"),
        "sentiment":   sentiment,
        "agreement":   agreement,
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(TV_WEBHOOK_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return jsonify({"ok": True, **entry})


@app.route("/api/tv_webhook/recent")
def api_tv_webhook_recent():
    """Last 20 webhook hits — consumed by the autoresearch dashboard card."""
    if not os.path.exists(TV_WEBHOOK_LOG):
        return jsonify([])
    lines: list[dict] = []
    with open(TV_WEBHOOK_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    resp = jsonify(lines[-20:][::-1])
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    if sched.is_enabled():
        sched.init_scheduler()
    app.run(debug=True, port=5001, use_reloader=False)
