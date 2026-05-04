"""Pytest config for the order-flow engine tests."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure repo root is importable when running `pytest order_flow_engine/tests/`.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def tmp_of_output(tmp_path, monkeypatch):
    """Redirect OF_OUTPUT_DIR + OF_MODELS_DIR to per-test tmp dirs."""
    from order_flow_engine.src import config as of_cfg
    out = tmp_path / "out"
    models = tmp_path / "models"
    out.mkdir()
    models.mkdir()
    monkeypatch.setattr(of_cfg, "OF_OUTPUT_DIR", out)
    monkeypatch.setattr(of_cfg, "OF_MODELS_DIR", models)
    return out


def _make_bar(open_, high, low, close, volume, ts):
    return {"Open": open_, "High": high, "Low": low, "Close": close,
            "Volume": volume, "_ts": ts}


@pytest.fixture
def synthetic_ohlcv():
    """Generate a 200-bar synthetic 15m frame with realistic shape."""
    rng = np.random.default_rng(0)
    n = 200
    ts = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = 4500 + np.cumsum(rng.normal(0, 2, n))
    open_ = close + rng.normal(0, 0.5, n)
    high  = np.maximum(open_, close) + np.abs(rng.normal(1, 0.5, n))
    low   = np.minimum(open_, close) - np.abs(rng.normal(1, 0.5, n))
    vol   = rng.integers(500, 5000, n).astype(float)
    df = pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol,
    }, index=ts)
    return df
