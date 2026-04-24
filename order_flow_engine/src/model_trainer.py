"""
Model training — walk-forward multiclass classifier over order-flow features.

Two classifiers:
  RandomForest (sklearn)  — baseline, always available once sklearn installed.
  XGBClassifier           — primary, skipped if xgboost missing.

Both receive inverse-frequency sample weights. The majority class
(normal_behavior) is downsampled to KEEP_NORMAL_FRAC of its original count
before weighting to avoid overwhelming the signal.

Outputs in outputs/order_flow/:
  feature_importance.csv   rank of features from the primary model
  model_predictions.csv    per-bar predicted class + probabilities
  training_report.json     per-fold accuracy / classification_report

Artefacts in order_flow_engine/models/:
  of_<ts>.pkl              pickled {'model', 'feature_names', 'metadata'}
  of_<ts>.json             sidecar metadata (also embedded in the pickle)
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import (
    config as of_cfg,
    data_loader,
    feature_engineering as fe,
    label_generator,
    rule_engine,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


# ── dataset construction ─────────────────────────────────────────────────────

def build_dataset(
    symbol: str,
    timeframe: str,
    lookback_days: int,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, pd.Series, list[str], bool]:
    """
    Pull bars → features → rules → labels. Returns (X, y, feature_names,
    proxy_mode).
    """
    multi = data_loader.load_multi_tf(
        symbol=symbol,
        timeframes=of_cfg.OF_TIMEFRAMES,
        lookback_days=lookback_days,
        use_cache=use_cache,
    )
    if timeframe not in multi:
        raise RuntimeError(f"Anchor TF {timeframe} unavailable")

    proxy_mode = not any(
        data_loader.detect_schema(df) == "tick" for df in multi.values()
    )

    featured = {tf: fe.build_features_for_tf(df, tf) for tf, df in multi.items()}
    joined = fe.build_feature_matrix(featured, anchor_tf=timeframe)
    joined = rule_engine.apply_rules(joined)
    labels = label_generator.generate_labels(joined, timeframe)
    joined["label"] = labels

    # Drop rows with NaN ATR (warm-up window) — they produce meaningless labels.
    joined = joined[joined["atr"].notna() & (joined["atr"] > 0)]
    joined = joined.dropna(subset=["fwd_ret_n"])

    feat_names = label_generator.feature_columns(joined)
    X = joined[feat_names].fillna(0.0)
    y = joined["label"]
    return X, y, feat_names, proxy_mode


def _downsample_normal(X: pd.DataFrame, y: pd.Series, frac: float, seed: int = 42):
    normal_mask = y == "normal_behavior"
    normal_idx = y[normal_mask].index
    other_idx  = y[~normal_mask].index
    keep_n = max(1, int(round(len(normal_idx) * frac)))
    rng = np.random.default_rng(seed)
    sampled = rng.choice(normal_idx, size=min(keep_n, len(normal_idx)), replace=False)
    kept = pd.Index(sampled).append(other_idx).sort_values()
    return X.loc[kept], y.loc[kept]


def _inverse_freq_weights(y: pd.Series) -> np.ndarray:
    counts = y.value_counts()
    inv = {cls: 1.0 / cnt for cls, cnt in counts.items()}
    total = sum(inv.values())
    norm = {cls: w / total * len(counts) for cls, w in inv.items()}
    return np.array([norm[v] for v in y.values])


def _fold_slices(n_rows: int, fold_size: int, n_folds: int):
    """Yield (train_idx, test_idx) for walk-forward folds."""
    for k in range(n_folds):
        test_start = n_rows - (n_folds - k) * fold_size
        test_end   = test_start + fold_size
        if test_start <= fold_size:
            continue
        train_idx = np.arange(0, test_start)
        test_idx  = np.arange(test_start, min(test_end, n_rows))
        if len(test_idx) == 0:
            continue
        yield train_idx, test_idx


# ── training ─────────────────────────────────────────────────────────────────

def _build_xgb(n_class: int):
    from xgboost import XGBClassifier  # defer import
    params = dict(of_cfg.XGB_PARAMS)
    # Let XGBoost infer num_class from the dense 0..k-1 y we pass. Setting
    # it explicitly clashes when the training set lacks some classes.
    params.pop("num_class", None)
    return XGBClassifier(**params)


def _remap_labels(y_idx: np.ndarray) -> tuple[np.ndarray, dict[int, int]]:
    """
    Remap sparse label indices (e.g. [0, 2, 4]) to dense [0..k-1] so xgboost
    doesn't complain about missing classes. Returns (dense_y, mapping back to
    original index).
    """
    present = sorted(set(int(v) for v in y_idx))
    forward = {orig: dense for dense, orig in enumerate(present)}
    reverse = {dense: orig for orig, dense in forward.items()}
    dense = np.array([forward[int(v)] for v in y_idx], dtype=int)
    return dense, reverse


def _build_rf():
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(**of_cfg.RF_PARAMS)


def _encode_labels(y: pd.Series) -> np.ndarray:
    """Label → int index matching LABEL_CLASSES order."""
    mapping = {c: i for i, c in enumerate(of_cfg.LABEL_CLASSES)}
    return y.map(mapping).astype(int).to_numpy()


def _classification_report(y_true_idx, y_pred_idx) -> dict:
    from sklearn.metrics import classification_report
    target_names = of_cfg.LABEL_CLASSES
    present = sorted(set(y_true_idx.tolist()) | set(y_pred_idx.tolist()))
    return classification_report(
        y_true_idx, y_pred_idx,
        labels=present,
        target_names=[target_names[i] for i in present],
        output_dict=True,
        zero_division=0,
    )


def train(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    lookback_days: int | None = None,
    use_cache: bool = True,
    output_dir: Path | None = None,
) -> dict:
    symbol        = symbol or of_cfg.OF_SYMBOL
    timeframe     = timeframe or of_cfg.OF_ANCHOR_TF
    lookback_days = lookback_days or of_cfg.OF_LOOKBACK_DAYS
    out_dir       = Path(output_dir) if output_dir else of_cfg.OF_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    X, y, feat_names, proxy_mode = build_dataset(
        symbol, timeframe, lookback_days, use_cache=use_cache,
    )
    X_ds, y_ds = _downsample_normal(X, y, of_cfg.KEEP_NORMAL_FRAC)
    y_idx_full = _encode_labels(y_ds)
    y_idx, reverse_map = _remap_labels(y_idx_full)
    weights = _inverse_freq_weights(y_ds)

    # Walk-forward eval
    folds_report = []
    for k, (train_idx, test_idx) in enumerate(_fold_slices(len(X_ds), of_cfg.WF_FOLD_SIZE, of_cfg.WF_N_FOLDS)):
        X_tr, X_te = X_ds.iloc[train_idx], X_ds.iloc[test_idx]
        y_tr, y_te = y_idx[train_idx], y_idx[test_idx]
        w_tr = weights[train_idx]

        # Try xgboost first, fall back to RF if unavailable.
        try:
            model_k = _build_xgb(len(of_cfg.LABEL_CLASSES))
            model_k.fit(X_tr.values, y_tr, sample_weight=w_tr)
            model_name = "xgboost"
        except Exception as e:
            logger.info(f"xgboost unavailable ({e}); using RandomForest")
            model_k = _build_rf()
            model_k.fit(X_tr.values, y_tr, sample_weight=w_tr)
            model_name = "random_forest"

        y_pred = model_k.predict(X_te.values)
        folds_report.append({
            "fold": k,
            "model": model_name,
            "train_rows": int(len(train_idx)),
            "test_rows":  int(len(test_idx)),
            "report":     _classification_report(y_te, y_pred),
        })

    # Final model on all data
    try:
        final_model = _build_xgb(len(of_cfg.LABEL_CLASSES))
        final_model.fit(X_ds.values, y_idx, sample_weight=weights)
        final_name = "xgboost"
    except Exception as e:
        logger.info(f"xgboost unavailable for final ({e}); using RandomForest")
        final_model = _build_rf()
        final_model.fit(X_ds.values, y_idx, sample_weight=weights)
        final_name = "random_forest"

    # Feature importance
    importances = getattr(final_model, "feature_importances_", None)
    if importances is not None:
        fi = pd.DataFrame({
            "feature": feat_names,
            "importance": importances,
        }).sort_values("importance", ascending=False)
        fi.to_csv(out_dir / "feature_importance.csv", index=False)

    # Persist artefacts
    ts = int(time.time())
    version = f"of_m{ts}"
    metadata = {
        "version":            version,
        "symbol":             symbol,
        "timeframe":          timeframe,
        "model":              final_name,
        "proxy_mode":         proxy_mode,
        "feature_names":      feat_names,
        "class_distribution": label_generator.label_distribution(y_ds),
        "class_index_map":    {int(k): int(v) for k, v in reverse_map.items()},
        "folds":              folds_report,
    }
    pkl_path = of_cfg.OF_MODELS_DIR / f"{version}.pkl"
    with pkl_path.open("wb") as f:
        pickle.dump({
            "model":          final_model,
            "feature_names":  feat_names,
            "class_index_map": reverse_map,
            "metadata":        metadata,
        }, f)
    with (of_cfg.OF_MODELS_DIR / f"{version}.json").open("w") as f:
        json.dump(metadata, f, indent=2, default=str)
    with (out_dir / "training_report.json").open("w") as f:
        json.dump(metadata, f, indent=2, default=str)

    logger.info(f"Model saved: {pkl_path.name} ({final_name})")
    return metadata


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="Train order-flow classifier.")
    ap.add_argument("--symbol", default=of_cfg.OF_SYMBOL)
    ap.add_argument("--tf", default=of_cfg.OF_ANCHOR_TF)
    ap.add_argument("--lookback", type=int, default=of_cfg.OF_LOOKBACK_DAYS)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    meta = train(
        symbol=args.symbol,
        timeframe=args.tf,
        lookback_days=args.lookback,
        use_cache=not args.no_cache,
    )
    print(json.dumps(meta, indent=2, default=str))
