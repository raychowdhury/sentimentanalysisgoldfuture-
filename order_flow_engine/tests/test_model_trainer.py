"""Tests for model_trainer — gated if xgboost/sklearn are unavailable."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from order_flow_engine.src import (
    config as of_cfg,
    feature_engineering as fe,
    label_generator,
    model_trainer,
    rule_engine,
)


def _synthetic_multi_tf(n=700) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = 4500 + np.cumsum(rng.normal(0, 2, n))
    open_ = close + rng.normal(0, 0.5, n)
    high  = np.maximum(open_, close) + np.abs(rng.normal(1, 0.5, n))
    low   = np.minimum(open_, close) - np.abs(rng.normal(1, 0.5, n))
    vol   = rng.integers(500, 5000, n).astype(float)
    df_15m = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=ts,
    )
    # Build a matching 1h frame so build_feature_matrix has context.
    df_1h = df_15m.resample("1h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()
    return {"15m": df_15m, "1h": df_1h}


def test_downsample_normal_shrinks_majority():
    y = pd.Series(["normal_behavior"] * 100 + ["bullish_trap"] * 5)
    X = pd.DataFrame({"f": range(len(y))})
    X_ds, y_ds = model_trainer._downsample_normal(X, y, frac=0.1)
    assert (y_ds == "normal_behavior").sum() == 10
    assert (y_ds == "bullish_trap").sum() == 5


def test_inverse_freq_weights_balanced():
    y = pd.Series(["a", "a", "a", "a", "b"])
    w = model_trainer._inverse_freq_weights(y)
    # 'b' rare → heavier weight than 'a'
    assert w[-1] > w[0]


def test_fold_slices_walk_forward():
    # n_rows must comfortably exceed n_folds * fold_size so every fold has
    # room for a non-trivial training window.
    folds = list(model_trainer._fold_slices(n_rows=2500, fold_size=500, n_folds=3))
    assert len(folds) == 3
    # Train indices should all be earlier than their test indices.
    for tr, te in folds:
        assert tr.max() < te.min()


def test_train_writes_artefacts(monkeypatch, tmp_of_output):
    multi = _synthetic_multi_tf(n=700)

    def fake_load_multi(*a, **kw):
        return multi

    monkeypatch.setattr(
        "order_flow_engine.src.data_loader.load_multi_tf",
        fake_load_multi,
    )

    meta = model_trainer.train(
        symbol="SYN=F",
        timeframe="15m",
        lookback_days=60,
        use_cache=False,
        output_dir=tmp_of_output,
    )

    assert meta["symbol"] == "SYN=F"
    assert meta["timeframe"] == "15m"
    assert meta["model"] in ("xgboost", "random_forest")
    assert len(meta["feature_names"]) > 0

    # Artefacts on disk
    assert (tmp_of_output / "feature_importance.csv").exists()
    assert (tmp_of_output / "training_report.json").exists()
    # Model pickle in models dir (redirected by fixture)
    pkls = list(of_cfg.OF_MODELS_DIR.glob("of_*.pkl"))
    assert len(pkls) == 1

    fi = pd.read_csv(tmp_of_output / "feature_importance.csv")
    assert {"feature", "importance"}.issubset(fi.columns)
    assert len(fi) > 0
