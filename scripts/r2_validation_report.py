"""R2-only MVP validation — hybrid historical + live evidence.

Read-only on settled outcomes. Writes only the report files (atomic
tmp+rename). No engine state mutation. No subprocess. No network.
Stdlib only.

Verdict: PASS / WAIT / FAIL based on configurable gates.

ADVISORY ONLY. Not a trade signal. Historical evidence is in-sample for the
threshold calibration; live evidence is out-of-sample. Both required for a
credible verdict. PASS does NOT authorize real-money trading.
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
DEFAULT_REPORT = PROJECT / "outputs/order_flow/r2_validation_report.md"
DEFAULT_RULE = "r2_seller_up"

# Default gates — configurable via CLI
DEFAULTS = {
    "hist_n_min":     100,
    "live_n_min":      15,
    "mean_r_min":     0.0,
    "hit_rate_min":   0.50,
    "max_dd_max":     8.0,    # R units, historical equity curve
    "sessions_min":     2,
    "live_dates_min":   5,
    "mfe_mae_ratio_min": 1.0,
}


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_outcomes(path: Path, rule: str) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("rule") == rule:
                out.append(d)
    out.sort(key=lambda d: d.get("fire_ts_utc", ""))
    return out


def aggregate(rows: list[dict], stop_r: float | None = None) -> dict:
    """Aggregate per-rule outcomes. If stop_r is set, any trade with
    mae_r <= -|stop_r| is treated as stopped out at -|stop_r| for both
    mean_r and max_dd computations. fwd_r_signed-derived metrics use the
    same clipping. mae/mfe distributions remain raw (they are observation
    not outcome)."""
    if not rows:
        return {
            "n": 0, "wins": 0, "losses": 0, "flats": 0, "stopped_out": 0,
            "hit_rate": None, "mean_r": None, "mae_r_med": None,
            "mfe_r_med": None, "max_dd_R": None, "max_dd_dur_days": None,
            "sessions": [], "dates": [],
        }
    cap = abs(stop_r) if stop_r is not None else None

    rs: list[float] = []
    stopped_count = 0
    wins = losses = flats = 0
    for r in rows:
        raw = float(r.get("fwd_r_signed", 0.0))
        mae = float(r.get("mae_r", 0.0)) if r.get("mae_r") is not None else 0.0
        outcome = r.get("outcome")
        if cap is not None and mae <= -cap:
            rR = -cap
            stopped_count += 1
            losses += 1
        else:
            rR = raw
            if outcome == "win":
                wins += 1
            elif outcome == "loss":
                losses += 1
            elif outcome == "flat":
                flats += 1
        rs.append(rR)

    maes = [float(r["mae_r"]) for r in rows if "mae_r" in r]
    mfes = [float(r["mfe_r"]) for r in rows if "mfe_r" in r]
    sessions = sorted({r.get("session") for r in rows if r.get("session")})
    dates = sorted({r.get("fire_ts_utc", "")[:10] for r in rows if r.get("fire_ts_utc")})

    # Equity curve + drawdown (uses stop-clipped rs)
    cum = 0.0
    peak = 0.0
    peak_idx = 0
    max_dd = 0.0
    max_dd_start = max_dd_end = 0
    for i, r in enumerate(rs):
        cum += r
        if cum > peak:
            peak = cum
            peak_idx = i
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
            max_dd_start = peak_idx
            max_dd_end = i
    dd_dur_days = None
    if rs and max_dd > 0 and max_dd_end > max_dd_start:
        try:
            t0 = rows[max_dd_start]["fire_ts_utc"]
            t1 = rows[max_dd_end]["fire_ts_utc"]
            dt0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
            dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            dd_dur_days = (dt1 - dt0).total_seconds() / 86400
        except Exception:
            pass

    return {
        "n": len(rows),
        "wins": wins, "losses": losses, "flats": flats,
        "stopped_out": stopped_count,
        "hit_rate": round(wins / len(rows), 4),
        "mean_r": round(sum(rs) / len(rs), 4) if rs else None,
        "median_r": round(median(rs), 4) if rs else None,
        "std_r": round(pstdev(rs), 4) if len(rs) > 1 else 0.0,
        "mae_r_med": round(median(maes), 4) if maes else None,
        "mfe_r_med": round(median(mfes), 4) if mfes else None,
        "max_dd_R": round(max_dd, 4),
        "max_dd_dur_days": round(dd_dur_days, 2) if dd_dur_days else None,
        "sessions": sessions,
        "dates": dates,
        "equity_curve_final_R": round(cum, 4) if rs else 0.0,
    }


def by_session(rows: list[dict], stop_r: float | None = None) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("session", "?")].append(r)
    return {s: aggregate(rs, stop_r=stop_r) for s, rs in groups.items()}


def by_date(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("fire_ts_utc", "")[:10]].append(r)
    return {
        d: {"n": len(rs), "mean_r": round(sum(float(r["fwd_r_signed"]) for r in rs) / len(rs), 4)}
        for d, rs in groups.items()
    }


def evaluate_gates(hist: dict, live: dict, gates: dict) -> list[dict]:
    g = []

    def add(name: str, requirement: str, observed: str, passed: bool, notes: str = "") -> None:
        g.append({"gate": name, "requirement": requirement, "observed": observed,
                  "pass": passed, "notes": notes})

    # historical n
    add("historical n",
        f">= {gates['hist_n_min']}",
        str(hist["n"]),
        hist["n"] >= gates["hist_n_min"],
        "in-sample evidence (used to set thresholds)")

    # live n
    add("live n",
        f">= {gates['live_n_min']}",
        str(live["n"]),
        live["n"] >= gates["live_n_min"],
        "out-of-sample evidence")

    # mean_r both
    h_ok = isinstance(hist["mean_r"], (int, float)) and hist["mean_r"] > gates["mean_r_min"]
    l_ok = isinstance(live["mean_r"], (int, float)) and live["mean_r"] > gates["mean_r_min"]
    add("mean_r both modes",
        f"> {gates['mean_r_min']:.2f}",
        f"hist={hist['mean_r']} live={live['mean_r']}",
        h_ok and l_ok,
        "both modes must show positive expectancy")

    # hit_rate both
    h_ok = isinstance(hist["hit_rate"], (int, float)) and hist["hit_rate"] >= gates["hit_rate_min"]
    l_ok = isinstance(live["hit_rate"], (int, float)) and live["hit_rate"] >= gates["hit_rate_min"]
    add("hit_rate both modes",
        f">= {gates['hit_rate_min']:.2f}",
        f"hist={hist['hit_rate']} live={live['hit_rate']}",
        h_ok and l_ok,
        "not just lucky tail trades")

    # historical drawdown
    dd_ok = isinstance(hist["max_dd_R"], (int, float)) and hist["max_dd_R"] <= gates["max_dd_max"]
    add("historical max drawdown",
        f"<= {gates['max_dd_max']:.1f} R",
        str(hist["max_dd_R"]),
        dd_ok,
        "portfolio-survivable")

    # sessions covered (combined)
    combined_sessions = sorted(set(hist["sessions"]) | set(live["sessions"]))
    add("sessions covered",
        f">= {gates['sessions_min']}",
        f"{len(combined_sessions)} ({','.join(combined_sessions) or '-'})",
        len(combined_sessions) >= gates["sessions_min"],
        "not single-regime artifact")

    # live calendar dates
    add("live dates covered",
        f">= {gates['live_dates_min']}",
        str(len(live["dates"])),
        len(live["dates"]) >= gates["live_dates_min"],
        "not single-day cluster")

    # MFE/MAE ratio (use live; historical informational)
    if live["mfe_r_med"] is not None and live["mae_r_med"] is not None and live["mae_r_med"] != 0:
        ratio = abs(live["mfe_r_med"] / live["mae_r_med"])
        add("MFE_med / |MAE_med| (live)",
            f">= {gates['mfe_mae_ratio_min']:.1f}",
            f"{ratio:.2f}",
            ratio >= gates["mfe_mae_ratio_min"],
            "risk-reward symmetric")
    else:
        add("MFE_med / |MAE_med| (live)",
            f">= {gates['mfe_mae_ratio_min']:.1f}",
            "n/a",
            False,
            "insufficient live MFE/MAE data")

    return g


def compute_verdict(gate_results: list[dict]) -> tuple[str, str]:
    failed = [g for g in gate_results if not g["pass"]]
    if not failed:
        return "PASS", "all gates pass"
    # WAIT vs FAIL: WAIT if only n-related gates failing AND quality is positive
    n_gates = {"historical n", "live n", "live dates covered", "sessions covered"}
    only_n_failing = all(g["gate"] in n_gates for g in failed)
    if only_n_failing:
        return "WAIT", f"awaiting more samples (failed: {', '.join(g['gate'] for g in failed)})"
    return "FAIL", f"quality gate failed (failed: {', '.join(g['gate'] for g in failed)})"


def render_equity_text(rows: list[dict], width: int = 60) -> str:
    if not rows:
        return "(no data)"
    cum = 0.0
    series = []
    for r in rows:
        cum += float(r["fwd_r_signed"])
        series.append(cum)
    lo = min(0.0, min(series))
    hi = max(0.0, max(series))
    if hi == lo:
        return "(flat curve)"
    lines = []
    lines.append(f"R range: {lo:.2f} → {hi:.2f}  (final: {cum:+.2f}R, n={len(series)})")
    bins = 10
    bin_size = (hi - lo) / bins
    for b in range(bins, -1, -1):
        threshold = lo + b * bin_size
        mark = "─" if b in (0, bins) else " "
        bar = ""
        for v in series:
            bar += "█" if v >= threshold else " "
        lines.append(f"{threshold:+7.2f} | {bar[:width]}")
    return "\n".join(lines)


def render_report(payload: dict) -> str:
    p = payload
    lines: list[str] = []
    lines.append(f"# R2 Validation Report — {p['rule']}")
    lines.append(f"_generated: {p['generated_utc']}_\n")

    lines.append(f"## Verdict: {p['verdict']}")
    lines.append(p["verdict_note"] + "\n")
    lines.append("**ADVISORY ONLY.** This verdict does not authorize real-money trading. "
                 "Historical evidence is in-sample (used to calibrate thresholds); only live "
                 "out-of-sample evidence is decisive. PASS at this stage means project may "
                 "proceed to MVP-soft phase; n=30 confirmation still required.\n")

    lines.append("## Gate evaluation\n")
    lines.append("| gate | requirement | observed | result | notes |")
    lines.append("|---|---|---|---|---|")
    for g in p["gate_results"]:
        mark = "✅" if g["pass"] else "❌"
        lines.append(f"| {g['gate']} | {g['requirement']} | {g['observed']} | {mark} | {g['notes']} |")
    lines.append("")

    lines.append("## Aggregate by mode\n")
    lines.append("| metric | historical (in-sample) | live (out-of-sample) |")
    lines.append("|---|---|---|")
    for k in ["n", "wins", "losses", "flats", "hit_rate", "mean_r", "median_r",
              "std_r", "mae_r_med", "mfe_r_med", "max_dd_R", "max_dd_dur_days",
              "equity_curve_final_R"]:
        lines.append(f"| {k} | {p['historical'].get(k, '—')} | {p['live'].get(k, '—')} |")
    lines.append("")
    lines.append(f"sessions covered (hist): {p['historical']['sessions']}")
    lines.append(f"sessions covered (live): {p['live']['sessions']}")
    lines.append(f"calendar dates (hist):   {len(p['historical']['dates'])}")
    lines.append(f"calendar dates (live):   {len(p['live']['dates'])}\n")

    lines.append("## By session (combined hist + live)\n")
    lines.append("| session | n | hit_rate | mean_r | mae_med | mfe_med |")
    lines.append("|---|---|---|---|---|---|")
    for s, agg in sorted(p["combined_by_session"].items()):
        lines.append(f"| {s} | {agg['n']} | {agg['hit_rate']} | {agg['mean_r']} | {agg['mae_r_med']} | {agg['mfe_r_med']} |")
    lines.append("")

    lines.append("## Live calendar dates (per-day mean R)\n")
    if not p["live_by_date"]:
        lines.append("_(none yet)_\n")
    else:
        lines.append("| date | n | mean_r |")
        lines.append("|---|---|---|")
        for d in sorted(p["live_by_date"].keys()):
            agg = p["live_by_date"][d]
            lines.append(f"| {d} | {agg['n']} | {agg['mean_r']} |")
        lines.append("")

    lines.append("## Historical equity curve (text)\n")
    lines.append("```")
    lines.append(p["historical_equity_text"])
    lines.append("```\n")

    lines.append("## Live equity curve (text)\n")
    lines.append("```")
    lines.append(p["live_equity_text"])
    lines.append("```\n")

    lines.append("## Caveats\n")
    lines.append("- Historical mode = in-sample (rule thresholds were calibrated on this data).")
    lines.append("- Live mode = out-of-sample (true validation evidence).")
    lines.append("- Equity curve assumes 1R risk per trade, no slippage, no spread, no commissions, no overlap risk.")
    stop_r = p["gates"].get("stop_r")
    if stop_r is None:
        lines.append("- Stop-loss simulation: OFF. max_dd uses raw 12-bar horizon R; values inflated vs real risk-managed trading. Pass `--stop-r 1.0` for stop-aware verdict.")
    else:
        lines.append(f"- Stop-loss simulation: ON at -{abs(stop_r):.2f}R. Trades whose mae_r ≤ -|stop_r| treated as stopped out at -|stop_r|R. Reflects real risk-managed accounting.")
    lines.append("- A PASS verdict does NOT authorize real-money trading — n=30 confirmation still required.")
    lines.append("- WAIT means quality looks acceptable but sample is insufficient.")
    lines.append("- FAIL means quality gate failed; investigate before more sample is collected.\n")

    lines.append("## Inputs read\n")
    for f in p["inputs_read"]:
        lines.append(f"- `{f}`")
    lines.append("\n## Standing instruction respected\n")
    lines.append("- No code/threshold/model/ml_engine/predictor/alert_engine/ingest/trading change from this report.")
    lines.append("- Read-only on outcomes JSONL.")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", default=DEFAULT_RULE)
    parser.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--no-json", action="store_true")
    parser.add_argument("--stop-r", type=float, default=None,
                        help="Optional stop-loss simulation in R units. "
                             "Trades whose mae_r ≤ -|stop_r| are treated as "
                             "stopped at -|stop_r|R for mean_r and max_dd. "
                             "Default: OFF (raw 12-bar horizon accounting).")
    for k, v in DEFAULTS.items():
        parser.add_argument(f"--{k.replace('_', '-')}",
                            type=type(v), default=v,
                            help=f"gate (default {v})")
    args = parser.parse_args()

    gates = {k: getattr(args, k) for k in DEFAULTS.keys()}
    gates["stop_r"] = args.stop_r

    src = Path(args.outcomes)
    if not src.exists():
        print(f"ERROR: outcomes file not found at {src}", file=sys.stderr)
        return 2

    rows = load_outcomes(src, args.rule)
    if not rows:
        print(f"ERROR: no rows for rule={args.rule}", file=sys.stderr)
        return 2

    historical = [r for r in rows if r.get("mode") == "historical"]
    live = [r for r in rows if r.get("mode") == "live"]

    hist_agg = aggregate(historical, stop_r=args.stop_r)
    live_agg = aggregate(live, stop_r=args.stop_r)
    combined_by_session = by_session(rows, stop_r=args.stop_r)
    live_by_date = by_date(live)

    # Always include unclipped reference for transparency when stop is on
    if args.stop_r is not None:
        hist_agg["max_dd_R_unclipped"] = aggregate(historical, stop_r=None)["max_dd_R"]
        live_agg["max_dd_R_unclipped"] = aggregate(live, stop_r=None)["max_dd_R"]

    gate_results = evaluate_gates(hist_agg, live_agg, gates)
    verdict, note = compute_verdict(gate_results)

    payload = {
        "rule": args.rule,
        "generated_utc": now_iso(),
        "gates": gates,
        "verdict": verdict,
        "verdict_note": note,
        "historical": hist_agg,
        "live": live_agg,
        "combined_by_session": combined_by_session,
        "live_by_date": live_by_date,
        "historical_equity_text": render_equity_text(historical),
        "live_equity_text": render_equity_text(live),
        "gate_results": gate_results,
        "inputs_read": [str(src.relative_to(PROJECT))],
    }

    md = render_report(payload)
    out_md = Path(args.out)
    write_atomic(out_md, md)

    if not args.no_json:
        out_json = out_md.with_suffix(".json")
        write_atomic(out_json, json.dumps(payload, indent=2, default=str) + "\n")

    print(f"verdict: {verdict}")
    print(f"note:    {note}")
    print(f"wrote:   {out_md}")
    if not args.no_json:
        print(f"wrote:   {out_md.with_suffix('.json')}")
    return 0 if verdict == "PASS" else (1 if verdict == "WAIT" else 2)


if __name__ == "__main__":
    sys.exit(main())
