import csv
import json
import os
from typing import Any


def save_csv(articles: list[dict], path: str) -> None:
    """Write a list of article dicts to a CSV file."""
    if not articles:
        return
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fieldnames = list(articles[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(articles)


def save_json(data: Any, path: str) -> None:
    """Write data as pretty-printed JSON."""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ── Sentiment summary ─────────────────────────────────────────────────────────

def print_summary(summary: dict) -> None:
    """Print a clean run summary to the terminal."""
    sep = "─" * 62
    print(f"\n{sep}")
    print("  NEWSSENTIMENTAL SCANNER — RUN SUMMARY")
    print(sep)
    print(f"  Articles fetched      : {summary.get('total_fetched', 0)}")
    print(f"  Unique articles       : {summary.get('total_unique', 0)}")
    print(f"  Analyzed              : {summary.get('total_analyzed', 0)}")
    print(f"  Successfully scraped  : {summary.get('total_scraped', 0)}")
    print(f"  Failed scrapes        : {summary.get('total_failed', 0)}")
    print(f"  Text mode             : {summary.get('text_mode', '-')}")
    print(f"  Models used           : {', '.join(summary.get('models_used', []))}")
    print(sep)

    dist = summary.get("sentiment_distribution", {})
    for model, counts in dist.items():
        pos = counts.get("positive", 0)
        neu = counts.get("neutral",  0)
        neg = counts.get("negative", 0)
        print(f"  [{model.upper():8s}]  Positive: {pos:3d}  Neutral: {neu:3d}  Negative: {neg:3d}")

    avg = summary.get("average_final_score")
    if avg is not None:
        print(f"\n  Average final score   : {avg:+.4f}")

    top_pos = summary.get("top_positive", [])
    if top_pos:
        print("\n  TOP POSITIVE HEADLINES:")
        for i, h in enumerate(top_pos, 1):
            print(f"    {i}. {h[:80]}")

    top_neg = summary.get("top_negative", [])
    if top_neg:
        print("\n  TOP NEGATIVE HEADLINES:")
        for i, h in enumerate(top_neg, 1):
            print(f"    {i}. {h[:80]}")

    print(f"\n{sep}\n")


# ── Signal summary ────────────────────────────────────────────────────────────

_SIGNAL_STYLE = {
    "STRONG_BUY":  "▲▲ STRONG BUY",
    "BUY":         "▲  BUY",
    "HOLD":        "●  HOLD",
    "SELL":        "▼  SELL",
    "STRONG_SELL": "▼▼ STRONG SELL",
    "NO_TRADE":    "—  NO TRADE",
}

_CONF_STYLE = {
    "HIGH":   "HIGH  ●●●",
    "MEDIUM": "MEDIUM ●●○",
    "LOW":    "LOW   ●○○",
}


def print_signal_summary(out: dict) -> None:
    """Print the gold bias signal and optional trade setup to the terminal."""
    sep = "─" * 62
    print(f"\n{sep}")
    print("  GOLD BIAS ENGINE — SIGNAL OUTPUT")
    print(sep)

    signal     = out.get("signal", "—")
    confidence = out.get("confidence", "—")
    total      = out.get("total_score", 0)

    print(f"  Signal        : {_SIGNAL_STYLE.get(signal, signal)}")
    print(f"  Confidence    : {_CONF_STYLE.get(confidence, confidence)}")
    print(f"  Total score   : {total:+.2f}")
    if out.get("veto_applied"):
        print(f"  (veto applied — raw signal was {out.get('raw_signal')})")
    print(sep)
    print(f"  Sentiment     : {out.get('sentiment_score',  0):+d}")
    print(f"  DXY           : {out.get('dxy_score',        0):+d}")
    print(f"  US 10Y yield  : {out.get('yield_score',      0):+d}")
    print(f"  Gold trend    : {out.get('gold_trend_score', 0):+d}  (weight ×1, range -3..+3)")

    reasons = out.get("reasoning", [])
    if reasons:
        print(f"\n{sep}")
        print("  REASONING")
        print(sep)
        for r in reasons:
            print(f"    · {r}")

    snap = out.get("market_snapshot", {})
    if any(snap.values()):
        print(f"\n{sep}")
        print("  MARKET SNAPSHOT")
        print(sep)
        labels = {"gold": "Gold (GC=F)", "dxy": "DXY", "yield_10y": "US 10Y Yield"}
        for key, label in labels.items():
            ind = snap.get(key)
            if ind:
                print(
                    f"  {label:18s}  price={ind['current']:.3f}  "
                    f"ema20={ind['ema20']:.3f}  "
                    f"ema50={ind['ema50']:.3f}  "
                    f"5d={ind['return_5d_pct']:+.2f}%"
                )
            else:
                print(f"  {label:18s}  — data unavailable")

    trade = out.get("trade_setup")
    if trade:
        print(f"\n{sep}")
        print("  TRADE SETUP")
        print(sep)
        decision = trade.get("trade_decision", "NO_TRADE")
        print(f"  Decision      : {decision}")
        if trade.get("trade_valid"):
            print(f"  Entry         : {trade.get('entry_price')}")
            print(f"  Stop Loss     : {trade.get('stop_loss')}")
            print(f"  Take Profit   : {trade.get('take_profit')}")
            print(f"  Risk          : {trade.get('risk_amount')}")
            print(f"  Reward        : {trade.get('reward_amount')}")
            print(f"  Risk/Reward   : 1:{trade.get('risk_reward_ratio')}")
        note = trade.get("setup_note")
        if note:
            print(f"  Note          : {note}")
        print(f"  Min required RR: 1:{trade.get('minimum_required_rr', 2.0)}")

    print(f"\n{sep}")
    print("  ⚠  This is a rules-based bias engine, not financial advice.")
    print(f"{sep}\n")
