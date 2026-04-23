"""Shared pytest fixtures for stocks-autoResearch tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def tmp_registry(tmp_path):
    """ModelRegistry pointed at a tmp dir."""
    from models.model_registry import ModelRegistry
    return ModelRegistry(base_dir=tmp_path)


def _ticker_frame(n_rows: int, seed: int = 0) -> pd.DataFrame:
    """
    Build a single-ticker frame with the columns residual_agent needs.

    Features are correlated with the target so a logistic regression can
    learn something non-trivial — tests shouldn't be flaky due to zero
    signal in purely random data.
    """
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n_rows)
    noise = rng.normal(scale=0.5, size=n_rows)
    # Direction: positive signal → up day more likely.
    y_next_dir = (signal + noise > 0).astype(int)
    return pd.DataFrame({
        "date":          pd.date_range("2024-01-01", periods=n_rows, freq="B"),
        "ticker":        "TEST",
        "y_next_dir":    y_next_dir,
        "y_next_ret":    signal * 0.01,
        "vol_20d":       rng.uniform(0.005, 0.03, size=n_rows),
        "rsi_14":        rng.uniform(20, 80, size=n_rows),
        "ret_5d":        signal * 0.01 + rng.normal(scale=0.005, size=n_rows),
        "ret_20d":       signal * 0.015 + rng.normal(scale=0.01, size=n_rows),
        "pooled_logit":  signal * 0.3 + rng.normal(scale=1.0, size=n_rows),
    })


@pytest.fixture
def synthetic_ticker_frame():
    """Single-ticker train/valid split with enough rows for residual fit."""
    df = _ticker_frame(n_rows=600, seed=42)
    train = df.iloc[:500].copy()
    valid = df.iloc[500:].copy()
    return train, valid


@pytest.fixture
def tiny_ticker_frame():
    """Train slice too small — residual_agent must skip."""
    df = _ticker_frame(n_rows=60, seed=1)
    train = df.iloc[:40].copy()
    valid = df.iloc[40:].copy()
    return train, valid


class _DummyResidual:
    """Stand-in for a trained residual — supports predict_proba on any frame."""

    def __init__(self, bias: float = 0.6):
        self.bias = bias
        self.features = ["pooled_logit", "vol_20d", "rsi_14", "ret_5d", "ret_20d"]

    def predict_proba(self, X):
        return np.full(len(X), self.bias, dtype=float)

    def predict(self, X):
        return (self.predict_proba(X) >= 0.5).astype(int)


@pytest.fixture
def dummy_residual():
    return _DummyResidual()
