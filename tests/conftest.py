"""Shared pytest fixtures. Redirects config.OUTPUT_DIR to a tmp dir."""

import sys
import os

# Ensure project root on sys.path when running pytest from the project dir.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
import config


@pytest.fixture
def tmp_output_dir(tmp_path, monkeypatch):
    """Point config.OUTPUT_DIR at a tmp directory for cache tests."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    return tmp_path
