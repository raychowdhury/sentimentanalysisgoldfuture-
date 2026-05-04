"""Tests for per-ticker residual models + registry extensions."""
from __future__ import annotations

import json

import pandas as pd
import pytest


# ── Registry extensions ──────────────────────────────────────────────────────

def test_registry_save_load_residual_round_trip(tmp_registry, dummy_residual):
    tmp_registry.save_residual(
        "AAPL", dummy_residual, residual_acc=0.60, pooled_acc=0.52,
    )
    loaded = tmp_registry.load_residual("AAPL")
    assert loaded is not None
    sample = pd.DataFrame({
        "pooled_logit": [0.1, -0.2, 0.3],
        "vol_20d":      [0.01, 0.02, 0.015],
        "rsi_14":       [50.0, 45.0, 55.0],
        "ret_5d":       [0.01, -0.005, 0.002],
        "ret_20d":      [0.02, 0.00, -0.01],
    })
    assert loaded.predict_proba(sample).shape == (3,)


def test_registry_load_residual_missing_returns_none(tmp_registry):
    assert tmp_registry.load_residual("ZZZ") is None


def test_promote_residual_accepts_when_better(tmp_registry, dummy_residual):
    promoted = tmp_registry.promote_residual(
        "MSFT", dummy_residual, residual_acc=0.58, pooled_acc=0.52,
    )
    assert promoted is True
    assert tmp_registry.load_residual("MSFT") is not None


def test_promote_residual_rejects_when_not_better(tmp_registry, dummy_residual):
    promoted = tmp_registry.promote_residual(
        "MSFT", dummy_residual, residual_acc=0.50, pooled_acc=0.52,
    )
    assert promoted is False
    assert tmp_registry.load_residual("MSFT") is None


def test_promote_residual_rejects_when_equal(tmp_registry, dummy_residual):
    # Equal-or-worse residuals add cost, not signal — reject.
    promoted = tmp_registry.promote_residual(
        "MSFT", dummy_residual, residual_acc=0.52, pooled_acc=0.52,
    )
    assert promoted is False


def test_promote_residual_updates_manifest(tmp_registry, dummy_residual):
    tmp_registry.promote_residual("AAPL", dummy_residual, 0.60, 0.52)
    manifest_path = tmp_registry.base_dir / "residuals_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "AAPL" in manifest
    assert manifest["AAPL"]["residual_acc"] == pytest.approx(0.60)
    assert manifest["AAPL"]["pooled_acc_at_promotion"] == pytest.approx(0.52)
    assert "updated_at" in manifest["AAPL"]


# ── train_residual_for_ticker ────────────────────────────────────────────────

def test_train_residual_returns_model_and_metrics(synthetic_ticker_frame):
    from agents.residual_agent import TickerResidual, train_residual_for_ticker

    train, valid = synthetic_ticker_frame
    residual, metrics = train_residual_for_ticker(train, valid)
    assert isinstance(residual, TickerResidual)
    assert set(metrics.keys()) == {"residual_acc", "pooled_acc", "n_train", "n_valid"}
    assert 0.0 <= metrics["residual_acc"] <= 1.0
    assert 0.0 <= metrics["pooled_acc"] <= 1.0
    assert metrics["n_train"] == len(train)
    assert metrics["n_valid"] == len(valid)


def test_train_residual_skips_when_too_few_rows(tiny_ticker_frame):
    from agents.residual_agent import train_residual_for_ticker

    train, valid = tiny_ticker_frame
    residual, metrics = train_residual_for_ticker(train, valid)
    assert residual is None
    assert metrics["residual_acc"] is None
    assert metrics["pooled_acc"] is None


def test_train_residual_predict_proba_shape(synthetic_ticker_frame):
    from agents.residual_agent import train_residual_for_ticker

    train, valid = synthetic_ticker_frame
    residual, _ = train_residual_for_ticker(train, valid)
    assert residual is not None
    proba = residual.predict_proba(valid)
    assert proba.shape == (len(valid),)
    assert ((proba >= 0.0) & (proba <= 1.0)).all()
