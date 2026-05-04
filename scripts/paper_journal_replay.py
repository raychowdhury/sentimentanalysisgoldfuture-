"""Paper-Journal Replay — equity curve from settled outcomes.

Replays each settled rule fire as a 1R-risk paper trade. Uses the existing
`fwd_r_signed` field from outcomes JSONL as the per-trade R outcome. NO
broker, NO execution, NO real money. Pure historical analysis.

Read-only on outcomes JSONL. Writes only the report files (atomic
tmp+rename). Stdlib only.

ADVISORY ONLY. Equity curve ignores slippage, spread, commissions,
execution latency, position sizing risk, and overlap risk between
adjacent trades. Not a track record. Not a guarantee of future returns.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, pstdev

PROJECT = Path(__file__).resolve().parents[1]

DEFAULT_OUTCOMES = PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl"
DEFAULT_SHADOW = PROJECT / "outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl"
DEFAULT_REPORT = PROJECT / "outputs/order_flow/paper_journal.md"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def filter_rows(rows: list[dict], rules: list[str] | None,
                modes: list[str], start_iso: str | None, end_iso: str | None) -> list[dict]:
    out = []
    for r in rows:
        if rules and r.get("rule") not in rules:
            continue
        if modes and r.get("mode") not in modes:
            continue
        ts = r.get("fire_ts_utc", "")
        if start_iso and ts < start_iso:
            continue
        if end_iso and ts > end_iso:
            continue
        out.append(r)
    out.sort(key=lambda d: d.get("fire_ts_utc", ""))
    return out


def equity_curve(rows: list[dict], stop_r: float | None = None) -> list[dict]:
    """Build per-trade equity curve. Optional stop-loss simulation:
    if `stop_r` is set, any trade whose mae_r <= -|stop_r| is treated as
    stopped out at -|stop_r| (overrides horizon-close fwd_r). Trades whose
    MAE never reached -|stop_r| keep their original fwd_r."""
    cum = 0.0
    peak = 0.0
    out = []
    cap = abs(stop_r) if stop_r is not None else None
    for r in rows:
        raw_R = float(r.get("fwd_r_signed", 0.0))
        mae = float(r.get("mae_r", 0.0)) if r.get("mae_r") is not None else 0.0
        outcome = r.get("outcome")
        stopped = False
        if cap is not None and mae <= -cap:
            rR = -cap
            outcome = "loss_stopped"
            stopped = True
        else:
            rR = raw_R
        cum += rR
        peak = max(peak, cum)
        dd = peak - cum
        out.append({
            "fire_ts_utc": r.get("fire_ts_utc"),
            "rule": r.get("rule"),
            "mode": r.get("mode"),
            "session": r.get("session"),
            "direction": r.get("direction"),
            "entry_close": r.get("entry_close"),
            "atr": r.get("atr"),
            "mfe_r": r.get("mfe_r"),
            "mae_r": r.get("mae_r"),
            "fwd_r_horizon": raw_R,
            "fwd_r_signed": rR,
            "stopped": stopped,
            "outcome": outcome,
            "equity_after_R": round(cum, 4),
            "drawdown_R": round(dd, 4),
        })
    return out


def aggregate_equity(curve: list[dict]) -> dict:
    if not curve:
        return {"n": 0}
    rs = [c["fwd_r_signed"] for c in curve]
    final = curve[-1]["equity_after_R"]
    max_dd = max(c["drawdown_R"] for c in curve)
    # max DD duration in days: peak index → trough index
    peak_idx = 0
    peak_val = curve[0]["equity_after_R"]
    dd_start = 0
    dd_end = 0
    cur_dd = 0.0
    for i, c in enumerate(curve):
        if c["equity_after_R"] > peak_val:
            peak_val = c["equity_after_R"]
            peak_idx = i
        if c["drawdown_R"] > cur_dd:
            cur_dd = c["drawdown_R"]
            dd_start = peak_idx
            dd_end = i
    dd_dur_days = None
    try:
        t0 = curve[dd_start]["fire_ts_utc"]
        t1 = curve[dd_end]["fire_ts_utc"]
        if t0 and t1:
            dt0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
            dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            dd_dur_days = (dt1 - dt0).total_seconds() / 86400
    except Exception:
        pass

    wins = sum(1 for c in curve if c["outcome"] == "win")
    losses = sum(1 for c in curve if c["outcome"] in ("loss", "loss_stopped"))
    stopped = sum(1 for c in curve if c.get("stopped"))
    flats = sum(1 for c in curve if c["outcome"] == "flat")
    mean_r = sum(rs) / len(rs)
    std_r = pstdev(rs) if len(rs) > 1 else 0.0

    # Daily R distribution
    by_day = defaultdict(float)
    by_day_count = defaultdict(int)
    for c in curve:
        d = (c["fire_ts_utc"] or "")[:10]
        if d:
            by_day[d] += c["fwd_r_signed"]
            by_day_count[d] += 1

    daily_rs = list(by_day.values())
    best_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, None)
    worst_day = min(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, None)

    return {
        "n_trades": len(curve),
        "wins": wins, "losses": losses, "flats": flats,
        "stopped_out": stopped,
        "hit_rate": round(wins / len(curve), 4),
        "mean_R_per_trade": round(mean_r, 4),
        "median_R_per_trade": round(median(rs), 4),
        "std_R_per_trade": round(std_r, 4),
        "expectancy_R": round(mean_r, 4),
        "sharpe_ish": round(mean_r / std_r, 4) if std_r > 0 else None,
        "final_equity_R": round(final, 4),
        "max_drawdown_R": round(max_dd, 4),
        "max_dd_duration_days": round(dd_dur_days, 2) if dd_dur_days else None,
        "trading_days": len(by_day),
        "best_day": {"date": best_day[0], "R": round(best_day[1], 4) if best_day[1] is not None else None},
        "worst_day": {"date": worst_day[0], "R": round(worst_day[1], 4) if worst_day[1] is not None else None},
        "daily_mean_R": round(sum(daily_rs) / len(daily_rs), 4) if daily_rs else None,
    }


def render_equity_text(curve: list[dict], width: int = 60) -> str:
    if not curve:
        return "(no trades)"
    series = [c["equity_after_R"] for c in curve]
    lo = min(0.0, min(series))
    hi = max(0.0, max(series))
    if hi == lo:
        return "(flat curve)"
    lines = [f"R range: {lo:.2f} → {hi:.2f}  (final: {series[-1]:+.2f}R, n={len(series)})"]
    bins = 10
    bin_size = (hi - lo) / bins
    for b in range(bins, -1, -1):
        threshold = lo + b * bin_size
        bar = ""
        for v in series:
            bar += "█" if v >= threshold else " "
        lines.append(f"{threshold:+7.2f} | {bar[:width]}")
    return "\n".join(lines)


def render_drawdown_text(curve: list[dict], width: int = 60) -> str:
    if not curve:
        return "(no trades)"
    dds = [-c["drawdown_R"] for c in curve]   # negative values
    lo = min(dds)
    if lo == 0:
        return "(no drawdown)"
    lines = [f"DD range: {lo:.2f} → 0.00  (max DD: {-lo:.2f}R, n={len(dds)})"]
    bins = 8
    bin_size = -lo / bins
    for b in range(bins, -1, -1):
        threshold = -b * bin_size
        bar = ""
        for v in dds:
            bar += "█" if v <= threshold else " "
        lines.append(f"{threshold:+7.2f} | {bar[:width]}")
    return "\n".join(lines)


def render_report(payload: dict) -> str:
    p = payload
    lines: list[str] = []
    lines.append("# Paper-Journal Replay")
    lines.append(f"_generated: {p['generated_utc']}_\n")

    lines.append("**ADVISORY ONLY.** Replay of already-settled outcomes. NOT a track record. "
                 "NOT a guarantee. NOT real-money trading. Ignores slippage, spread, "
                 "commissions, execution latency, position sizing, and overlap risk.\n")

    lines.append("## Filter\n")
    lines.append(f"- rules: {', '.join(p['filter']['rules']) if p['filter']['rules'] else '(all)'}")
    lines.append(f"- modes: {', '.join(p['filter']['modes']) if p['filter']['modes'] else '(all)'}")
    lines.append(f"- date range: {p['filter']['start'] or '(start)'} → {p['filter']['end'] or '(end)'}")
    lines.append(f"- shadow included: {p['filter']['include_shadow']}")
    stop_r = p['filter'].get('stop_r')
    if stop_r is None:
        lines.append("- stop-loss simulation: OFF (raw 12-bar horizon accounting; max DD inflated)")
    else:
        lines.append(f"- stop-loss simulation: ON at -{abs(stop_r):.2f}R "
                     "(trades with mae_r ≤ -|stop_r| close at -|stop_r|)\n")

    lines.append("## Aggregate\n")
    a = p["aggregate"]
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for k in ["n_trades", "wins", "losses", "flats", "hit_rate",
              "mean_R_per_trade", "median_R_per_trade", "std_R_per_trade",
              "expectancy_R", "sharpe_ish",
              "final_equity_R", "max_drawdown_R", "max_dd_duration_days",
              "trading_days", "daily_mean_R"]:
        lines.append(f"| {k} | {a.get(k, '—')} |")
    lines.append("")
    lines.append(f"best day:  {a['best_day']['date']} ({a['best_day']['R']:+}R)" if a.get("best_day", {}).get("date") else "best day: n/a")
    lines.append(f"worst day: {a['worst_day']['date']} ({a['worst_day']['R']:+}R)" if a.get("worst_day", {}).get("date") else "worst day: n/a")
    lines.append("")

    lines.append("## Equity curve\n")
    lines.append("```")
    lines.append(p["equity_text"])
    lines.append("```\n")

    lines.append("## Drawdown curve\n")
    lines.append("```")
    lines.append(p["drawdown_text"])
    lines.append("```\n")

    lines.append("## By rule\n")
    if p["by_rule"]:
        lines.append("| rule | n | hit_rate | mean_R | final_R | max_DD |")
        lines.append("|---|---|---|---|---|---|")
        for rule, a in sorted(p["by_rule"].items()):
            lines.append(f"| {rule} | {a['n_trades']} | {a['hit_rate']} | {a['mean_R_per_trade']} | {a['final_equity_R']} | {a['max_drawdown_R']} |")
    lines.append("")

    lines.append("## By mode\n")
    if p["by_mode"]:
        lines.append("| mode | n | hit_rate | mean_R | final_R | max_DD |")
        lines.append("|---|---|---|---|---|---|")
        for mode, a in sorted(p["by_mode"].items()):
            lines.append(f"| {mode} | {a['n_trades']} | {a['hit_rate']} | {a['mean_R_per_trade']} | {a['final_equity_R']} | {a['max_drawdown_R']} |")
    lines.append("")

    lines.append("## Per-trade journal (all settled fires)\n")
    if p["curve"]:
        lines.append("| fire_ts_utc | rule | mode | session | dir | entry | atr | mfe_r | mae_r | horizon_R | trade_R | stopped | outcome | equity_R | dd_R |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in p["curve"]:
            lines.append(
                f"| {c['fire_ts_utc']} | {c['rule']} | {c['mode']} | {c['session']} | "
                f"{c['direction']} | {c['entry_close']} | {c['atr']} | {c['mfe_r']} | "
                f"{c['mae_r']} | {c.get('fwd_r_horizon', c['fwd_r_signed']):+.3f} | "
                f"{c['fwd_r_signed']:+.3f} | {'Y' if c.get('stopped') else ''} | "
                f"{c['outcome']} | {c['equity_after_R']:+.3f} | {c['drawdown_R']:+.3f} |"
            )
    lines.append("")

    lines.append("## Caveats\n")
    lines.append("- 1R risk per trade is a simplification; real position sizing depends on account, "
                 "volatility, and correlation.")
    lines.append("- Without `--stop-r`, `fwd_r_signed` is the close-at-12-bar-horizon return; trades that went "
                 "deep adverse before recovering are NOT capped — max DD is inflated.")
    lines.append("- With `--stop-r N`, any trade whose `mae_r ≤ -N` is treated as stopped at -NR. "
                 "Trades that stayed within the stop keep their original horizon-R. This better reflects "
                 "real-world risk-managed accounting.")
    lines.append("- Stop-loss simulation assumes the stop fills exactly at -NR (no slippage). Real fills "
                 "may slip in fast moves.")
    lines.append("- No slippage / spread / commissions / latency / overlap modelled.")
    lines.append("- Sharpe-ish = mean_R / std_R is per-trade, not annualized; use as relative metric only.")
    lines.append("- Drawdown is computed in R units; with real position sizing it would translate to "
                 "drawdown in account currency at risk-per-trade × R.")
    lines.append("- Historical mode = in-sample; live mode = out-of-sample; combined view mixes both.\n")

    lines.append("## Inputs read\n")
    for f in p["inputs_read"]:
        lines.append(f"- `{f}`")
    lines.append("\n## Standing instruction respected\n")
    lines.append("- No code/threshold/model/ml_engine/predictor/alert_engine/ingest/trading change from this report.")
    lines.append("- Read-only on outcomes JSONL. No new fires generated. No engine state mutated.")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules",
                        help="Comma-separated rule names to include (default = all rules in outcomes file)")
    parser.add_argument("--mode", default="all",
                        choices=["all", "historical", "live"],
                        help="Mode filter")
    parser.add_argument("--start-utc",
                        help="ISO start timestamp filter (inclusive)")
    parser.add_argument("--end-utc",
                        help="ISO end timestamp filter (inclusive)")
    parser.add_argument("--include-shadow", action="store_true",
                        help="Also include R7 shadow fires (separate equity track)")
    parser.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    parser.add_argument("--shadow-outcomes", default=str(DEFAULT_SHADOW))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--no-json", action="store_true")
    parser.add_argument("--stop-r", type=float, default=None,
                        help="Optional risk cap in R (e.g. --stop-r 1.0). "
                             "Trades whose mae_r <= -|stop_r| are simulated as "
                             "stopped out at -|stop_r| instead of using horizon "
                             "fwd_r. Default: no stop (raw horizon accounting).")
    args = parser.parse_args()

    rules = [r.strip() for r in args.rules.split(",")] if args.rules else None
    modes = ["historical", "live"] if args.mode == "all" else [args.mode]

    src_main = Path(args.outcomes)
    if not src_main.exists():
        print(f"ERROR: outcomes file not found at {src_main}", file=sys.stderr)
        return 2
    rows = load_jsonl(src_main)
    inputs = [str(src_main.relative_to(PROJECT))]

    if args.include_shadow:
        src_shadow = Path(args.shadow_outcomes)
        if src_shadow.exists():
            shadow_rows = load_jsonl(src_shadow)
            rows.extend(shadow_rows)
            inputs.append(str(src_shadow.relative_to(PROJECT)))

    filtered = filter_rows(rows, rules, modes, args.start_utc, args.end_utc)
    if not filtered:
        print("ERROR: no rows match filter", file=sys.stderr)
        return 2

    curve = equity_curve(filtered, stop_r=args.stop_r)
    agg = aggregate_equity(curve)

    # Per-rule and per-mode breakouts
    by_rule = {}
    rule_groups: dict[str, list[dict]] = defaultdict(list)
    for r in filtered:
        rule_groups[r.get("rule", "?")].append(r)
    for k, v in rule_groups.items():
        by_rule[k] = aggregate_equity(equity_curve(v, stop_r=args.stop_r))

    by_mode = {}
    mode_groups: dict[str, list[dict]] = defaultdict(list)
    for r in filtered:
        mode_groups[r.get("mode", "?")].append(r)
    for k, v in mode_groups.items():
        by_mode[k] = aggregate_equity(equity_curve(v, stop_r=args.stop_r))

    payload = {
        "generated_utc": now_iso(),
        "filter": {
            "rules": rules,
            "modes": modes,
            "start": args.start_utc,
            "end": args.end_utc,
            "include_shadow": args.include_shadow,
            "stop_r": args.stop_r,
        },
        "aggregate": agg,
        "by_rule": by_rule,
        "by_mode": by_mode,
        "curve": curve,
        "equity_text": render_equity_text(curve),
        "drawdown_text": render_drawdown_text(curve),
        "inputs_read": inputs,
    }

    md = render_report(payload)
    out_md = Path(args.out)
    write_atomic(out_md, md)

    if not args.no_json:
        out_json = out_md.with_suffix(".json")
        write_atomic(out_json, json.dumps(payload, indent=2, default=str) + "\n")

    print(f"trades:  {agg['n_trades']}")
    print(f"hit:     {agg['hit_rate']}")
    print(f"mean R:  {agg['mean_R_per_trade']}")
    print(f"final:   {agg['final_equity_R']:+.2f}R")
    print(f"max DD:  {agg['max_drawdown_R']:.2f}R")
    print(f"wrote:   {out_md}")
    if not args.no_json:
        print(f"wrote:   {out_md.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
