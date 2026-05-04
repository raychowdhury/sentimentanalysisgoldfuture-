#!/bin/bash
# Health monitor wrapper — sources venv, runs Python probe.
# Phase 1: probes + logging + osascript notification only. No self-healing.

set -uo pipefail

PROJECT="/Users/ray/Dev/Sentiment analysis projtect"
cd "$PROJECT" || exit 1
source .venv/bin/activate
exec python scripts/health_monitor.py
