"""
NewsSentimentScanner — Dashboard Server

Usage:
    python app.py
    # Open http://localhost:5001
"""

import glob
import json
import os
from datetime import datetime

from flask import Flask, abort, render_template, request

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

        has_sent = ts in sent_map
        has_sig  = ts in sig_map
        tag = "signal+sentiment" if (has_sent and has_sig) else ("signal" if has_sig else "sentiment")

        runs.append({
            "timestamp":  ts,
            "label":      f"{label}  [{tag}]",
            "has_sent":   has_sent,
            "has_sig":    has_sig,
            "sent_path":  sent_map.get(ts),
            "sig_path":   sig_map.get(ts),
        })
    return runs


def _load_json(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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
    runs = load_runs()
    if not runs:
        return render_template("index.html", runs=[], sentiment=None, signal=None,
                               trade_viz=None, selected=None)

    valid = {r["timestamp"] for r in runs}
    selected = request.args.get("run", runs[0]["timestamp"])
    if selected not in valid:
        selected = runs[0]["timestamp"]

    run      = next(r for r in runs if r["timestamp"] == selected)
    sentiment = _load_json(run["sent_path"])
    signal    = _load_json(run["sig_path"])
    viz       = _trade_viz(signal.get("trade_setup") if signal else None)

    return render_template(
        "index.html",
        runs=runs,
        sentiment=sentiment,
        signal=signal,
        trade_viz=viz,
        selected=selected,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
