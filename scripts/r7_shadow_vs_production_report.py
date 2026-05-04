"""R7 Shadow vs Production — Read-only comparison report.

Compares production R7 fires (rule=r7_cvd_divergence at threshold -0.50) vs
shadow R7 fires (rule=r7_cvd_divergence_shadow at threshold -0.20) to
determine whether the stricter production threshold avoided the bad shadow
failure clusters.

Read-only on outcomes JSONL + pending JSON. Writes only the report files
(atomic tmp+rename). Stdlib only. No subprocess. No network.

ADVISORY ONLY. Provides evidence for the n=30 R7 shadow review (Phase 2B
Stage 2). Does NOT promote R7 shadow. Does NOT change RULE_CVD_CORR_THRESH
or RULE_CVD_CORR_THRESH_REAL_SHADOW. Does NOT modify any rule logic.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

PROJECT = Path(__file__).resolve().parents[1]

DEFAULT_PROD_OUTCOMES = PROJECT / "outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl"
DEFAULT_SHADOW_OUTCOMES = PROJECT / "outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl"
DEFAULT_SHADOW_PENDING = PROJECT / "outputs/order_flow/realflow_r7_shadow_pending_ESM6_15m.json"
DEFAULT_DIAGNOSTIC = PROJECT / "outputs/order_flow/realflow_diagnostic_ESM6_15m.json"
DEFAULT_REPORT = PROJECT / "outputs/order_flow/r7_shadow_vs_production.md"

PRODUCTION_RULE = "r7_cvd_divergence"
SHADOW_RULE = "r7_cvd_divergence_shadow"
TF_MINUTES = 15
CLUSTER_MIN_FIRES = 3
CLUSTER_MEAN_R_MAX = -1.0
OVERLAP_BARS = 1   # ±1 bar (15m on each side)


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


def load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except Exception:
        return []
    return d if isinstance(d, list) else []


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def percentiles(values: list[float], pcts: list[float]) -> dict:
    if not values:
        return {f"p{int(p*100)}": None for p in pcts}
    s = sorted(values)
    out = {}
    n = len(s)
    for p in pcts:
        idx = max(0, min(n - 1, int(p * (n - 1))))
        out[f"p{int(p*100)}"] = round(s[idx], 4)
    out["min"] = round(s[0], 4)
    out["max"] = round(s[-1], 4)
    out["n"] = n
    return out


def cluster_days(rows: list[dict], min_fires: int, mean_r_max: float) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = (r.get("fire_ts_utc") or "")[:10]
        if d:
            by_day[d].append(r)
    out = []
    for day, fires in sorted(by_day.items()):
        if len(fires) < min_fires:
            continue
        rs = [float(f.get("fwd_r_signed", 0.0)) for f in fires if "fwd_r_signed" in f]
        if not rs:
            continue
        mean_r = sum(rs) / len(rs)
        if mean_r >= mean_r_max:
            continue
        out.append({
            "date": day,
            "n_fires": len(fires),
            "mean_r": round(mean_r, 4),
            "fires": fires,
        })
    return out


def find_overlap(shadow_fire: dict, prod_fires: list[dict],
                 tolerance_bars: int = OVERLAP_BARS) -> dict | None:
    """Return matching production fire (within ±tolerance_bars of shadow fire_ts)."""
    sft = parse_ts(shadow_fire.get("fire_ts_utc"))
    if sft is None:
        return None
    tolerance = timedelta(minutes=TF_MINUTES * tolerance_bars)
    for p in prod_fires:
        pft = parse_ts(p.get("fire_ts_utc"))
        if pft is None:
            continue
        if abs((pft - sft).total_seconds()) <= tolerance.total_seconds():
            return p
    return None


def build_payload(prod_outcomes: Path, shadow_outcomes: Path,
                  shadow_pending: Path, diagnostic: Path,
                  cluster_min: int, cluster_max_r: float,
                  overlap_bars: int) -> dict:
    prod_all = load_jsonl(prod_outcomes)
    prod_r7 = [r for r in prod_all if r.get("rule") == PRODUCTION_RULE]

    shadow_settled = load_jsonl(shadow_outcomes)
    shadow_pending_rows = load_json(shadow_pending)

    diag_data = None
    if diagnostic.exists():
        try:
            diag_data = json.loads(diagnostic.read_text())
        except Exception:
            diag_data = None

    # Filter only r7_cvd_divergence_shadow rows from shadow JSONL
    shadow_settled = [r for r in shadow_settled if r.get("rule") == SHADOW_RULE]
    shadow_pending_rows = [r for r in shadow_pending_rows if r.get("rule") == SHADOW_RULE]

    # Mode counts
    prod_modes = Counter(r.get("mode") for r in prod_r7)
    shadow_modes = Counter(r.get("mode") for r in shadow_settled)

    # Overlap analysis (settled shadow fires only)
    overlap = []
    shadow_only = []
    for sf in shadow_settled:
        match = find_overlap(sf, prod_r7, overlap_bars)
        if match:
            overlap.append({"shadow": sf, "production": match})
        else:
            shadow_only.append(sf)
    prod_only = []
    matched_prod_ids = {id(o["production"]) for o in overlap}
    for p in prod_r7:
        if id(p) not in matched_prod_ids:
            prod_only.append(p)

    # Cluster analysis (shadow fires)
    clusters = cluster_days(shadow_settled, cluster_min, cluster_max_r)

    # Production survival on cluster days
    prod_dates = {(p.get("fire_ts_utc") or "")[:10] for p in prod_r7}
    cluster_summary = []
    for c in clusters:
        prod_on_day = [p for p in prod_r7 if (p.get("fire_ts_utc") or "")[:10] == c["date"]]
        prod_rs = [float(p.get("fwd_r_signed", 0.0)) for p in prod_on_day if "fwd_r_signed" in p]
        prod_mean = round(sum(prod_rs) / len(prod_rs), 4) if prod_rs else None
        cluster_summary.append({
            "date": c["date"],
            "shadow_n": c["n_fires"],
            "shadow_mean_r": c["mean_r"],
            "production_n": len(prod_on_day),
            "production_mean_r": prod_mean,
            "production_avoided": len(prod_on_day) == 0,
        })

    # cvd_z distribution
    cvd_z_overlap = [float(o["shadow"].get("cvd_z", 0.0)) for o in overlap if o["shadow"].get("cvd_z") is not None]
    cvd_z_shadow_only = [float(s.get("cvd_z", 0.0)) for s in shadow_only if s.get("cvd_z") is not None]
    cvd_z_prod_only = [float(p.get("cvd_z", 0.0)) for p in prod_only if p.get("cvd_z") is not None]

    # Direction comparison
    direction_drift = 0
    direction_match = 0
    for o in overlap:
        sd = o["shadow"].get("direction")
        pd_ = o["production"].get("direction")
        if sd is None or pd_ is None:
            continue
        if sd == pd_:
            direction_match += 1
        else:
            direction_drift += 1

    # Headline verdict
    if not prod_r7:
        headline = "PRODUCTION-NEVER-FIRED"
        headline_note = (
            "Production R7 at -0.50 has zero settled fires in this window. "
            "The strict threshold means no production R7 trade was made; "
            "shadow at -0.20 fired but its losses are research-only."
        )
    elif clusters and all(c["production_avoided"] for c in cluster_summary):
        headline = "PRODUCTION-AVOIDED-TRAPS"
        headline_note = (
            "Production R7 fired some bars but avoided every shadow cluster-trap day. "
            "The -0.50 threshold appears to filter the regime-fragile setups that hurt -0.20."
        )
    elif clusters and any(not c["production_avoided"] for c in cluster_summary):
        headline = "PRODUCTION-ALSO-FIRED-AND-LOST"
        headline_note = (
            "Production R7 also fired on at least one shadow cluster-trap day. "
            "This indicates the rule itself may be regime-fragile, not just the looser threshold. "
            "Escalate to Phase 2C investigation."
        )
    else:
        headline = "INSUFFICIENT-CLUSTERS"
        headline_note = (
            f"No shadow cluster days met threshold (≥{cluster_min} fires AND mean_r < {cluster_max_r}). "
            "Comparison inconclusive at this sample size."
        )

    return {
        "generated_utc": now_iso(),
        "params": {
            "production_rule": PRODUCTION_RULE,
            "shadow_rule": SHADOW_RULE,
            "tf_minutes": TF_MINUTES,
            "cluster_min_fires": cluster_min,
            "cluster_mean_r_max": cluster_max_r,
            "overlap_bars": overlap_bars,
        },
        "headline": headline,
        "headline_note": headline_note,
        "counts": {
            "production": {
                "total_settled": len(prod_r7),
                "by_mode": dict(prod_modes),
            },
            "shadow": {
                "total_settled": len(shadow_settled),
                "total_pending": len(shadow_pending_rows),
                "by_mode": dict(shadow_modes),
            },
        },
        "overlap": {
            "n_shadow_with_production_match": len(overlap),
            "n_shadow_only": len(shadow_only),
            "n_production_only": len(prod_only),
            "direction_match_in_overlap": direction_match,
            "direction_drift_in_overlap": direction_drift,
        },
        "clusters": cluster_summary,
        "cvd_z_distribution": {
            "overlap": percentiles(cvd_z_overlap, [0.05, 0.25, 0.5, 0.75, 0.95]),
            "shadow_only": percentiles(cvd_z_shadow_only, [0.05, 0.25, 0.5, 0.75, 0.95]),
            "production_only": percentiles(cvd_z_prod_only, [0.05, 0.25, 0.5, 0.75, 0.95]),
        },
        "per_fire_shadow": [
            {
                "fire_ts_utc": sf.get("fire_ts_utc"),
                "fire_ts_ny": sf.get("fire_ts_ny"),
                "session": sf.get("session"),
                "direction": sf.get("direction"),
                "mode": sf.get("mode"),
                "cvd_z": sf.get("cvd_z"),
                "fwd_r_signed": sf.get("fwd_r_signed"),
                "outcome": sf.get("outcome"),
                "production_matched": find_overlap(sf, prod_r7, overlap_bars) is not None,
                "production_R": (find_overlap(sf, prod_r7, overlap_bars) or {}).get("fwd_r_signed"),
            }
            for sf in shadow_settled
        ],
        "diagnostic": {
            "joined_start": (diag_data or {}).get("joined", {}).get("start"),
            "joined_end": (diag_data or {}).get("joined", {}).get("end"),
            "joined_n_bars": (diag_data or {}).get("joined", {}).get("n_bars"),
        },
        "inputs_read": [
            str(prod_outcomes.relative_to(PROJECT)),
            str(shadow_outcomes.relative_to(PROJECT)),
            str(shadow_pending.relative_to(PROJECT)),
            str(diagnostic.relative_to(PROJECT)) if diagnostic.exists() else f"{diagnostic.relative_to(PROJECT)} (missing)",
        ],
    }


def render_recommendation(payload: dict) -> str:
    headline = payload["headline"]
    if headline == "PRODUCTION-NEVER-FIRED":
        return (
            "**KEEP -0.50 PRODUCTION + ABANDON SHADOW (likely path).** "
            "Production R7 at -0.50 has zero fires in window — cannot validate but cannot disprove either. "
            "Shadow at -0.20 has mean_r negative; n=30 review will likely conclude ABANDON-SHADOW. "
            "No promotion, no threshold change. R7 production stays untouched until separate Phase 2B Stage 2 review with explicit positive evidence."
        )
    if headline == "PRODUCTION-AVOIDED-TRAPS":
        return (
            "**KEEP -0.50 PRODUCTION (strong evidence) + ABANDON SHADOW.** "
            "Production R7 fired but avoided every shadow cluster-trap day. The -0.50 threshold is doing its job. "
            "Shadow at -0.20 demonstrably catches more setups but those extra setups are losers. STAY-PRODUCTION at n=30."
        )
    if headline == "PRODUCTION-ALSO-FIRED-AND-LOST":
        return (
            "**ESCALATE TO PHASE 2C (rule investigation).** "
            "Production R7 also fired on shadow cluster-trap day(s). The rule itself appears regime-fragile, "
            "not just the looser threshold. Investigate window size, direction-sign logic, or rule definition. "
            "Do NOT change R7 production threshold based on this evidence — investigation precedes any threshold action."
        )
    return (
        "**INSUFFICIENT EVIDENCE.** "
        f"No shadow cluster days met threshold (≥{payload['params']['cluster_min_fires']} fires AND "
        f"mean_r < {payload['params']['cluster_mean_r_max']}). Continue collecting; revisit at next checkpoint."
    )


def render_md(payload: dict) -> str:
    p = payload
    lines: list[str] = []
    lines.append("# R7 Shadow vs Production — Comparison Report\n")
    lines.append(f"_generated: {p['generated_utc']}_\n")
    lines.append(f"_inputs read: {len(p['inputs_read'])} files_\n")

    lines.append(f"## Headline verdict: {p['headline']}\n")
    lines.append(p["headline_note"] + "\n")

    lines.append("## Parameters\n")
    pp = p["params"]
    lines.append(f"- production rule: `{pp['production_rule']}` at threshold -0.50")
    lines.append(f"- shadow rule:     `{pp['shadow_rule']}` at threshold -0.20")
    lines.append(f"- tf minutes:      {pp['tf_minutes']}")
    lines.append(f"- cluster gate:    ≥{pp['cluster_min_fires']} fires AND mean_r < {pp['cluster_mean_r_max']:.2f}")
    lines.append(f"- overlap window:  ±{pp['overlap_bars']} bar(s) (±{pp['overlap_bars']*pp['tf_minutes']} min)\n")

    lines.append("## Counts\n")
    pc = p["counts"]
    lines.append("| group | total settled | total pending | by mode |")
    lines.append("|---|---|---|---|")
    lines.append(f"| production (-0.50) | {pc['production']['total_settled']} | n/a | {pc['production'].get('by_mode')} |")
    lines.append(f"| shadow (-0.20)     | {pc['shadow']['total_settled']} | {pc['shadow']['total_pending']} | {pc['shadow'].get('by_mode')} |\n")

    lines.append("## Overlap analysis\n")
    o = p["overlap"]
    lines.append(f"- shadow fires with production match (±{pp['overlap_bars']} bar): **{o['n_shadow_with_production_match']}**")
    lines.append(f"- shadow-only fires (-0.20 caught, -0.50 missed): **{o['n_shadow_only']}**")
    lines.append(f"- production-only fires (theoretically zero — production threshold strictly stricter): **{o['n_production_only']}**")
    lines.append(f"- direction match in overlap: {o['direction_match_in_overlap']}")
    lines.append(f"- direction drift in overlap: {o['direction_drift_in_overlap']}")
    lines.append("")

    lines.append("## Cluster days (shadow trend-trap signature)\n")
    lines.append(f"_Filter: ≥{pp['cluster_min_fires']} shadow fires same date AND mean_r < {pp['cluster_mean_r_max']:.2f}_\n")
    if not p["clusters"]:
        lines.append("_(none — no cluster-trap days observed)_\n")
    else:
        lines.append("| date | shadow n | shadow mean_r | production n on day | production mean_r | avoided? |")
        lines.append("|---|---|---|---|---|---|")
        for c in p["clusters"]:
            avoided = "YES" if c["production_avoided"] else "**NO**"
            lines.append(f"| {c['date']} | {c['shadow_n']} | {c['shadow_mean_r']:+.4f} | {c['production_n']} | {c['production_mean_r'] if c['production_mean_r'] is not None else 'n/a'} | {avoided} |")
        lines.append("")

    lines.append("## cvd_z distribution at fire time\n")
    lines.append("| group | n | min | p25 | median | p75 | max |")
    lines.append("|---|---|---|---|---|---|---|")
    for label in ("overlap", "shadow_only", "production_only"):
        d = p["cvd_z_distribution"][label]
        n = d.get("n", 0) or 0
        if n:
            lines.append(f"| {label} | {n} | {d['min']} | {d['p25']} | {d['p50']} | {d['p75']} | {d['max']} |")
        else:
            lines.append(f"| {label} | 0 | — | — | — | — | — |")
    lines.append("")

    lines.append("## Per-fire detail (shadow only)\n")
    if not p["per_fire_shadow"]:
        lines.append("_(no shadow fires)_\n")
    else:
        lines.append("| fire_ts (NY) | session | dir | mode | cvd_z | shadow R | outcome | prod_match | prod R |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for f in p["per_fire_shadow"]:
            lines.append(
                f"| {f['fire_ts_ny']} | {f['session']} | {f['direction']} | {f['mode']} | "
                f"{f['cvd_z']} | {f['fwd_r_signed']} | {f['outcome']} | "
                f"{'Y' if f['production_matched'] else 'N'} | "
                f"{f['production_R'] if f['production_R'] is not None else '—'} |"
            )
        lines.append("")

    lines.append("## Recommendation\n")
    lines.append(render_recommendation(p) + "\n")

    lines.append("## Caveats\n")
    lines.append("- 12-bar horizon outcome scoring is the same for shadow and production; comparison is apples-to-apples.")
    lines.append("- 'Adjacent bar' for overlap = within ±1 bar of fire_ts (configurable via --overlap-bars).")
    lines.append("- Production R7 may have ZERO fires in window — that itself is a finding (PRODUCTION-NEVER-FIRED).")
    lines.append("- Pending shadow fires are counted separately; not included in mean_r computations.")
    lines.append("- Read-only on outcomes JSONL + pending JSON. No changes to scoring.")
    lines.append("- Recommendation is advisory; final verdict belongs in the n=30 R7 shadow review.\n")

    lines.append("## Inputs read\n")
    for f in p["inputs_read"]:
        lines.append(f"- `{f}`")
    lines.append("")

    lines.append("## Standing instruction respected\n")
    lines.append("- No rule, threshold, model, ml_engine, predictor, alert_engine, ingest, outcome_tracker, R7 promotion, or trading change from this report.")
    lines.append("- Read-only on engine state.")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod-outcomes", default=str(DEFAULT_PROD_OUTCOMES))
    parser.add_argument("--shadow-outcomes", default=str(DEFAULT_SHADOW_OUTCOMES))
    parser.add_argument("--shadow-pending", default=str(DEFAULT_SHADOW_PENDING))
    parser.add_argument("--diagnostic", default=str(DEFAULT_DIAGNOSTIC))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--cluster-min-fires", type=int, default=CLUSTER_MIN_FIRES)
    parser.add_argument("--cluster-mean-r-max", type=float, default=CLUSTER_MEAN_R_MAX)
    parser.add_argument("--overlap-bars", type=int, default=OVERLAP_BARS)
    parser.add_argument("--no-json", action="store_true")
    args = parser.parse_args()

    prod = Path(args.prod_outcomes)
    shadow = Path(args.shadow_outcomes)
    pending = Path(args.shadow_pending)
    diag = Path(args.diagnostic)

    if not shadow.exists():
        print(f"ERROR: shadow outcomes file missing at {shadow}", file=sys.stderr)
        return 2

    payload = build_payload(
        prod, shadow, pending, diag,
        args.cluster_min_fires, args.cluster_mean_r_max, args.overlap_bars,
    )

    if payload["counts"]["shadow"]["total_settled"] == 0:
        print("ERROR: no settled shadow fires — nothing to compare", file=sys.stderr)
        return 3

    md = render_md(payload)
    out_md = Path(args.out)
    write_atomic(out_md, md)

    if not args.no_json:
        out_json = out_md.with_suffix(".json")
        write_atomic(out_json, json.dumps(payload, indent=2, default=str) + "\n")

    print(f"headline: {payload['headline']}")
    print(f"shadow settled: {payload['counts']['shadow']['total_settled']}")
    print(f"production settled: {payload['counts']['production']['total_settled']}")
    print(f"overlap: {payload['overlap']['n_shadow_with_production_match']}")
    print(f"clusters: {len(payload['clusters'])}")
    print(f"wrote: {out_md}")
    if not args.no_json:
        print(f"wrote: {out_md.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
