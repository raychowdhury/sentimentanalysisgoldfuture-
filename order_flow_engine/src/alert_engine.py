"""
Alert gating and persistence.

A prediction becomes an alert only when both:
  - confidence >= OF_ALERT_MIN_CONF
  - label != "normal_behavior"

Alerts are written two ways:
  outputs/order_flow/alerts.json   consolidated array (overwritten per run)
  outputs/order_flow/alerts.jsonl  append-only JSONL for streaming consumers
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from order_flow_engine.src import alert_store, config as of_cfg, rule_engine


def _stamp(ts: Any) -> str:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(ts)


def build_alert(
    *,
    timestamp,
    symbol: str,
    timeframe: str,
    label: str,
    confidence: int,
    price: float,
    atr: float,
    rules_fired: list[str],
    metrics: dict,
    model_info: dict | None = None,
    proxy_mode: bool = True,
    tf_window_capped: bool = False,
    pass_type: str | None = None,
) -> dict:
    """Assemble an alert dict matching the schema in the plan."""
    ts_str = _stamp(timestamp)
    compact_ts = ts_str.replace(":", "").replace("-", "").split(".")[0]
    reason_codes = [rule_engine.RULE_CODES.get(r, r) for r in rules_fired]
    # Derive pass_type from the rules fired if caller didn't set it explicitly.
    if pass_type is None:
        causal_set = set(getattr(rule_engine, "CAUSAL_RULES", []))
        confirm_set = set(getattr(rule_engine, "CONFIRMATION_RULES", []))
        fired = set(rules_fired)
        if fired and fired <= causal_set:
            pass_type = "causal"
        elif fired and fired <= confirm_set:
            pass_type = "confirm"
        elif fired:
            pass_type = "mixed"
        else:
            pass_type = "none"
    return {
        "id": f"of_{compact_ts}_{timeframe}_{symbol}",
        "timestamp_utc": ts_str,
        "symbol": symbol,
        "timeframe": timeframe,
        "label": label,
        "confidence": int(confidence),
        "price": round(float(price), 4),
        "atr": round(float(atr), 4) if atr is not None else None,
        "rules_fired": list(rules_fired),
        "reason_codes": reason_codes,
        "pass_type": pass_type,
        "metrics": {k: (round(float(v), 4) if v is not None else None)
                    for k, v in metrics.items()},
        "model": model_info or {},
        "data_quality": {
            "proxy_mode": bool(proxy_mode),
            "tf_window_capped": bool(tf_window_capped),
        },
    }


def should_emit(
    label: str,
    confidence: int,
    tf: str | None = None,
    min_conf: int | None = None,
) -> bool:
    threshold = of_cfg.OF_ALERT_MIN_CONF if min_conf is None else min_conf
    if label == "normal_behavior" or int(confidence) < int(threshold):
        return False
    if of_cfg.OF_ALERT_ALLOWED_LABELS and label not in of_cfg.OF_ALERT_ALLOWED_LABELS:
        return False
    if tf is not None and of_cfg.OF_ALERT_ALLOWED_TFS and tf not in of_cfg.OF_ALERT_ALLOWED_TFS:
        return False
    return True


def _bar_seconds(tf: str) -> int:
    unit_min = {"m": 1, "h": 60, "d": 1440}.get(tf[-1])
    if unit_min is None:
        return 900
    try:
        return int(tf[:-1]) * unit_min * 60
    except Exception:
        return 900


def in_cooldown(
    *,
    symbol: str,
    tf: str,
    label: str,
    timestamp,
    cooldown_bars: int | None = None,
    output_dir: Path | None = None,
) -> bool:
    """
    True if a same-(symbol,tf,label) alert was emitted within the cooldown
    window. Cooldown is measured in bars of the same tf.
    """
    bars = of_cfg.ALERT_COOLDOWN_BARS if cooldown_bars is None else cooldown_bars
    if bars <= 0:
        return False
    last = alert_store.last_alert_for(symbol, tf, label, output_dir=output_dir)
    if not last:
        return False
    try:
        last_ts = pd.Timestamp(last["ts_utc"])
        new_ts  = pd.Timestamp(timestamp)
    except Exception:
        return False
    delta = (new_ts - last_ts).total_seconds()
    return 0 <= delta < bars * _bar_seconds(tf)


def volume_gate_passes(
    bar_volume: float,
    recent_volumes: pd.Series | list[float],
    pctl: float | None = None,
) -> bool:
    """
    True if `bar_volume` is at or above the pctl-th percentile of the
    recent window. Falls back to True when the window is empty (cold start).
    """
    pctl = of_cfg.VOLUME_GATE_PCTL if pctl is None else pctl
    if not pctl:
        return True
    series = pd.Series(recent_volumes).dropna()
    series = series[series > 0]
    if series.empty:
        return True
    threshold = float(series.quantile(pctl))
    return float(bar_volume) >= threshold


def write_consolidated(alerts: list[dict], output_dir: Path | None = None) -> Path:
    """Overwrite alerts.json with the full list of alerts from this run."""
    out = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "alerts.json"
    with path.open("w") as f:
        json.dump(alerts, f, indent=2, default=str)
    return path


def append_jsonl(alert: dict, output_dir: Path | None = None) -> Path:
    """Append a single alert to alerts.jsonl for streaming consumers."""
    out = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / "alerts.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(alert, default=str) + "\n")
    return path


def emit(
    alert: dict,
    *,
    output_dir: Path | None = None,
    min_conf: int | None = None,
    cooldown_bars: int | None = None,
) -> dict | None:
    """
    Gate + persist a single alert. Returns the alert dict if emitted, else None.

    Gates applied in order:
      1. confidence >= threshold AND label != normal
      2. cooldown — same (symbol, tf, label) not emitted recently

    On success: writes to JSONL stream and sqlite. The consolidated alerts.json
    is rewritten by the caller (predictor / ingest).
    """
    if not should_emit(alert["label"], alert["confidence"],
                       tf=alert.get("timeframe"), min_conf=min_conf):
        return None
    if in_cooldown(
        symbol=alert["symbol"], tf=alert["timeframe"], label=alert["label"],
        timestamp=alert["timestamp_utc"], cooldown_bars=cooldown_bars,
        output_dir=output_dir,
    ):
        return None
    append_jsonl(alert, output_dir=output_dir)
    try:
        alert_store.upsert(alert, output_dir=output_dir)
    except Exception:
        pass  # never let store failures block streaming
    try:
        from order_flow_engine.src import notifier
        notifier.fanout(alert)
    except Exception:
        pass  # notifier failures never block streaming
    return alert
