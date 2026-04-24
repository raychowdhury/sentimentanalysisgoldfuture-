"""
Predictor — runs the engine against a symbol/TF and writes outputs.

Two modes:
  rule-only  — no trained model on disk. Confidence is a capped linear
               function of rule_hit_count; label is picked from rule hits.
  blended    — a model pickle exists. Confidence blends model probability
               with rule corroboration and a data-quality haircut.

Outputs (all in outputs/order_flow/):
  flagged_events.csv     all bars with any rule hit
  model_predictions.csv  if model present, per-bar class + probs
  alerts.json            consolidated alerts above threshold
  alerts.jsonl           append-only stream
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    alert_engine,
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    label_generator,
    rule_engine,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _latest_model() -> Path | None:
    """Most recent `of_*.pkl` in the models dir, or None."""
    candidates = sorted(of_cfg.OF_MODELS_DIR.glob("of_*.pkl"))
    return candidates[-1] if candidates else None


def _load_model(path: Path):
    with path.open("rb") as f:
        return pickle.load(f)


def _rule_only_label(row: pd.Series) -> str:
    """Pick a label from a single bar's rule hits (no model)."""
    if bool(row.get("r5_bull_trap")):
        return "bullish_trap"
    if bool(row.get("r6_bear_trap")):
        return "bearish_trap"
    if bool(row.get("r3_absorption_resistance")):
        return "buyer_absorption"
    if bool(row.get("r4_absorption_support")):
        return "seller_absorption"
    if bool(row.get("r1_buyer_down")) or bool(row.get("r2_seller_up")) or bool(row.get("r7_cvd_divergence")):
        return "possible_reversal"
    return "normal_behavior"


def rule_only_confidence(row: pd.Series) -> int:
    n = int(row.get("rule_hit_count", 0) or 0)
    return min(100, 40 + 10 * n)


def blended_confidence(
    probas: np.ndarray,
    pred_label: str,
    row: pd.Series,
    proxy_mode: bool,
) -> int:
    """
    confidence = (0.6*p_class + 0.2*(1-p_normal) + 0.2*rule_support)
                 * data_quality * volume_ok  * 100, clipped to [0,100].
    """
    classes = of_cfg.LABEL_CLASSES
    probs = dict(zip(classes, probas.tolist()))
    p_class  = probs.get(pred_label, 0.0)
    p_normal = probs.get("normal_behavior", 0.0)

    required = rule_engine.rules_for_label(pred_label)
    if required:
        hits = sum(int(bool(row.get(r, False))) for r in required)
        rule_support = min(1.0, hits / max(1, len(required)))
    else:
        rule_support = 0.0

    data_quality = 0.85 if proxy_mode else 1.0
    volume_ok = 1.0 if float(row.get("Volume", 0) or 0) > 0 else 0.7

    raw = (0.6 * p_class + 0.2 * (1 - p_normal) + 0.2 * rule_support) \
          * data_quality * volume_ok
    return int(max(0, min(100, round(100 * raw))))


def run(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    output_dir: Path | None = None,
    use_cache: bool = True,
) -> dict:
    """
    End-to-end prediction pass. Returns a dict summary with row counts,
    alert count, model version (or None), and paths to artefacts.
    """
    symbol        = symbol or of_cfg.OF_SYMBOL
    timeframe     = timeframe or of_cfg.OF_ANCHOR_TF
    lookback_days = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    out_dir       = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- load bars for all TFs (anchor + context) ---
    multi_tf_raw = data_loader.load_multi_tf(
        symbol=symbol,
        timeframes=of_cfg.OF_TIMEFRAMES,
        lookback_days=lookback_days,
        use_cache=use_cache,
    )
    if timeframe not in multi_tf_raw:
        raise RuntimeError(f"Anchor timeframe {timeframe} unavailable for {symbol}")

    proxy_mode = True  # OHLCV-only path; tick mode would flip this
    for tf, df in multi_tf_raw.items():
        if data_loader.detect_schema(df) == "tick":
            proxy_mode = False
            break

    # --- build features per TF, then multi-TF join on anchor ---
    multi_tf_feat: dict[str, pd.DataFrame] = {}
    for tf, df in multi_tf_raw.items():
        multi_tf_feat[tf] = fe.build_features_for_tf(df, tf)

    joined = fe.build_feature_matrix(multi_tf_feat, anchor_tf=timeframe)
    joined = rule_engine.apply_rules(joined)

    # --- flagged events (any rule hit) ---
    flagged = joined[joined["rule_hit_count"] > 0].copy()
    flagged_path = out_dir / "flagged_events.csv"
    flagged.to_csv(flagged_path)

    # --- model-aware or rule-only path ---
    model_path = _latest_model()
    model_info = {}
    preds_df = None

    if model_path is not None:
        try:
            bundle = _load_model(model_path)
            model = bundle["model"]
            feat_names = bundle["feature_names"]
            # Model was trained on dense 0..k-1 class indices; map back to the
            # full 6-class LABEL_CLASSES list (missing classes get probability 0).
            reverse_map = bundle.get("class_index_map", {
                i: i for i in range(len(of_cfg.LABEL_CLASSES))
            })
            X = joined.reindex(columns=feat_names).fillna(0.0)
            dense_probs = model.predict_proba(X.values)
            full = np.zeros((len(X), len(of_cfg.LABEL_CLASSES)))
            for dense_i, orig_i in reverse_map.items():
                full[:, int(orig_i)] = dense_probs[:, int(dense_i)]
            pred_idx = full.argmax(axis=1)
            classes = of_cfg.LABEL_CLASSES
            preds_df = pd.DataFrame(
                full, index=joined.index, columns=[f"p_{c}" for c in classes],
            )
            preds_df["pred_label"] = [classes[i] for i in pred_idx]
            preds_df.to_csv(out_dir / "model_predictions.csv")
            model_info = {
                "version": model_path.stem,
                "n_features": int(X.shape[1]),
            }
        except Exception as e:
            logger.warning(f"Model load/predict failed ({e}); falling back to rules")
            model_path = None

    # --- build alerts ---
    alerts: list[dict] = []
    # Clear the JSONL stream for this run so append semantics are clean.
    jsonl_path = out_dir / "alerts.jsonl"
    if jsonl_path.exists():
        jsonl_path.unlink()
    # Trailing volume window for the volume gate
    vol_window = joined["Volume"].tail(of_cfg.VOLUME_GATE_WINDOW)
    last_emit_idx_for: dict[tuple[str, str, str], int] = {}
    cooldown_bars = of_cfg.ALERT_COOLDOWN_BARS

    for i, (ts, row) in enumerate(joined.iterrows()):
        rules_fired = [c for c in rule_engine.ALL_RULE_COLS if bool(row.get(c, False))]
        if not rules_fired and (preds_df is None):
            continue
        # Volume gate — skip noisy low-volume bars in OHLCV-proxy mode
        if proxy_mode and not alert_engine.volume_gate_passes(
            float(row.get("Volume", 0) or 0), vol_window,
        ):
            continue

        if preds_df is not None and ts in preds_df.index:
            pred_label = preds_df.loc[ts, "pred_label"]
            probas_row = preds_df.loc[
                ts, [f"p_{c}" for c in of_cfg.LABEL_CLASSES]
            ].to_numpy(dtype=float)
            confidence = blended_confidence(probas_row, pred_label, row, proxy_mode)
            class_probs = {c: round(float(probas_row[i]), 4)
                           for i, c in enumerate(of_cfg.LABEL_CLASSES)}
            model_payload = {**model_info, "probas": class_probs}
        else:
            pred_label = _rule_only_label(row)
            confidence = rule_only_confidence(row)
            model_payload = {"version": None, "probas": {}}

        if not alert_engine.should_emit(pred_label, confidence):
            continue
        # In-run cooldown: skip same (sym,tf,label) within N bars index
        key = (symbol, timeframe, pred_label)
        last_i = last_emit_idx_for.get(key, -10**9)
        if i - last_i < cooldown_bars:
            continue
        last_emit_idx_for[key] = i

        alert = alert_engine.build_alert(
            timestamp=ts,
            symbol=symbol,
            timeframe=timeframe,
            label=pred_label,
            confidence=confidence,
            price=float(row.get("Close", float("nan"))),
            atr=float(row.get("atr", 0.0) or 0.0),
            rules_fired=rules_fired,
            metrics={
                "delta_ratio": row.get("delta_ratio"),
                "cvd_z":       row.get("cvd_z"),
                "clv":         row.get("clv"),
                "dist_to_recent_high_atr": row.get("dist_to_recent_high_atr"),
                "dist_to_recent_low_atr":  row.get("dist_to_recent_low_atr"),
            },
            model_info=model_payload,
            proxy_mode=proxy_mode,
        )
        alert_engine.append_jsonl(alert, output_dir=out_dir)
        try:
            from order_flow_engine.src import alert_store
            alert_store.upsert(alert, output_dir=out_dir)
        except Exception:
            pass
        alerts.append(alert)

    alerts_path = alert_engine.write_consolidated(alerts, output_dir=out_dir)

    summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "rows_processed": int(len(joined)),
        "rows_flagged": int((joined["rule_hit_count"] > 0).sum()),
        "alerts_emitted": len(alerts),
        "model_used": str(model_path) if model_path else None,
        "proxy_mode": proxy_mode,
        "artefacts": {
            "flagged_events": str(flagged_path),
            "alerts_json": str(alerts_path),
            "model_predictions": (
                str(out_dir / "model_predictions.csv") if preds_df is not None else None
            ),
        },
    }
    logger.info(f"Prediction complete: {json.dumps(summary, default=str)}")
    return summary


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="Run order-flow prediction pass.")
    ap.add_argument("--symbol", default=of_cfg.OF_SYMBOL)
    ap.add_argument("--tf", default=of_cfg.OF_ANCHOR_TF, help="anchor timeframe")
    ap.add_argument("--lookback", type=int, default=of_cfg.OF_LOOKBACK_DAYS)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    summary = run(
        symbol=args.symbol,
        timeframe=args.tf,
        lookback_days=args.lookback,
        use_cache=not args.no_cache,
    )
    print(json.dumps(summary, indent=2, default=str))
