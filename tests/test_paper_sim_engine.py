"""Offline tests T1-T4 for the R2 paper-simulation engine.

T1: replay vs incremental-pass equivalence (same input → same outputs)
T2: restart resilience (incremental pass survives engine recreate + hydrate)
T3: disabled-by-default safety (no writes when state.enabled=false)
T4: error isolation (engine exception does not corrupt state files)

Synthetic frames only — no live data, no broker, no config edits.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from order_flow_engine.src import paper_sim_engine as ps


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_frame(rows: list[dict]) -> pd.DataFrame:
    """Build a joined-frame-shaped DataFrame from row dicts.

    Each row dict needs: ts (utc str), Close, High, Low, atr,
    r2_seller_up (bool), bar_proxy_mode (int).
    """
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _strip_volatile(rec: dict) -> dict:
    """Remove fields that legitimately differ across runs (timestamps)."""
    drop = {"written_ts"}
    return {k: v for k, v in rec.items() if k not in drop}


def _scenario_frame() -> pd.DataFrame:
    """A small frame with two R2 fires and obvious target/stop outcomes.

    Bar layout (15-minute spaced timestamps):
      00:00  flat lead-in (no fire)
      00:15  R2 fire @ 100, ATR=2  (long; stop=98, target=104 at +2R)
      00:30  high=105 → target hit (close at target_px=104, R=+2.0)
      00:45  flat
      01:00  R2 fire @ 100, ATR=2  (long; stop=98, target=104)
      01:15  low=97  → stop hit (close at stop_px=98, R=-1.0)
      01:30  flat
    Two trades, expected equity_R = +1.0, no tie-break, no auto-pause.
    """
    rows = [
        {"ts": "2026-05-08T00:00:00Z", "Close": 99.0, "High": 99.5, "Low": 98.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T00:15:00Z", "Close": 100.0, "High": 100.5, "Low": 99.5,
         "atr": 2.0, "r2_seller_up": True,  "bar_proxy_mode": 0},
        {"ts": "2026-05-08T00:30:00Z", "Close": 104.5, "High": 105.0, "Low": 100.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T00:45:00Z", "Close": 102.0, "High": 103.0, "Low": 101.0,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:00:00Z", "Close": 100.0, "High": 100.5, "Low": 99.5,
         "atr": 2.0, "r2_seller_up": True,  "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:15:00Z", "Close": 97.5,  "High": 99.5,  "Low": 97.0,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:30:00Z", "Close": 98.0,  "High": 98.5,  "Low": 97.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
    ]
    return _make_frame(rows)


# ── T1: replay vs incremental equivalence ───────────────────────────────────

def test_t1_replay_vs_incremental_equivalence(tmp_path):
    df = _scenario_frame()

    # Run A: full replay in one shot.
    out_a = tmp_path / "A"
    eng_a = ps.PaperSimEngine(out_dir=out_a, tf="15m")
    eng_a.state.enabled = True  # default is disabled per spec invariant
    eng_a.state.enabled_reason = "T1 replay-side"
    res_a = eng_a.replay(df)
    assert res_a["ok"]

    orders_a = [_strip_volatile(r) for r in _read_lines(out_a / "paper_sim_orders.jsonl")]
    equity_a = [_strip_volatile(r) for r in _read_lines(out_a / "paper_sim_equity.jsonl")]
    state_a = json.loads((out_a / "paper_sim_state.json").read_text())

    # Run B: same input via two incremental_pass invocations on a fresh engine,
    # split mid-frame.
    out_b = tmp_path / "B"
    out_b.mkdir(parents=True, exist_ok=True)
    # Seed a state file so hydrate finds something — but engine must be enabled.
    eng_b = ps.PaperSimEngine(out_dir=out_b, tf="15m")
    eng_b.state.enabled = True
    eng_b.state.enabled_reason = "T1 test"
    eng_b._persist_state()

    half = len(df) // 2
    eng_b.incremental_pass(df=df.iloc[:half])
    eng_b2 = ps.PaperSimEngine(out_dir=out_b, tf="15m")
    eng_b2.incremental_pass(df=df)  # cursor already advanced; rest processed

    orders_b = [_strip_volatile(r) for r in _read_lines(out_b / "paper_sim_orders.jsonl")]
    equity_b = [_strip_volatile(r) for r in _read_lines(out_b / "paper_sim_equity.jsonl")]
    state_b = json.loads((out_b / "paper_sim_state.json").read_text())

    assert orders_a == orders_b, "orders.jsonl must match modulo written_ts"
    assert equity_a == equity_b, "equity.jsonl must match modulo written_ts"
    # Aggregate state numbers must match.
    for key in ("equity_R_running", "max_drawdown_R", "equity_peak_R"):
        assert state_a["watermarks"][key] == state_b["watermarks"][key]
    for key in ("trades_opened_total", "trades_closed_total"):
        assert state_a["counters"][key] == state_b["counters"][key]


# ── T2: restart resilience ──────────────────────────────────────────────────

def test_t2_restart_resilience(tmp_path):
    df = _scenario_frame()
    out = tmp_path / "restart"
    out.mkdir()

    # First engine: run on the first 3 bars only.
    eng1 = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng1.state.enabled = True
    eng1.state.enabled_reason = "T2 test"
    eng1._persist_state()
    eng1.incremental_pass(df=df.iloc[:3])

    # Second engine instance (simulates process restart): hydrate then continue.
    eng2 = ps.PaperSimEngine(out_dir=out, tf="15m")
    report = eng2.hydrate_from_disk()
    assert report["state_loaded"] is True
    assert report["seen_signal_ids"] >= 1  # at least the first fire
    eng2.incremental_pass(df=df)

    # Reference: full replay in a fresh dir for the same frame.
    out_ref = tmp_path / "ref"
    eng_ref = ps.PaperSimEngine(out_dir=out_ref, tf="15m")
    eng_ref.state.enabled = True
    eng_ref.state.enabled_reason = "T2 reference"
    eng_ref.replay(df)

    orders_after = [_strip_volatile(r) for r in _read_lines(out / "paper_sim_orders.jsonl")]
    orders_ref = [_strip_volatile(r) for r in _read_lines(out_ref / "paper_sim_orders.jsonl")]

    # Same set of trades, same R outcomes, same fire timestamps.
    keys = ("type", "trade_id", "rule", "fire_bar_ts", "exit_ts",
            "entry_px", "exit_px", "realized_R", "exit_reason")
    def _kf(r): return tuple(r.get(k) for k in keys)
    assert sorted(_kf(r) for r in orders_after) == sorted(_kf(r) for r in orders_ref)


# ── T3: disabled-by-default safety ──────────────────────────────────────────

def test_t3_disabled_default_no_writes(tmp_path):
    df = _scenario_frame()
    out = tmp_path / "disabled"
    out.mkdir()

    # Seed a disabled state file explicitly.
    state_path = out / "paper_sim_state.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "engine_version": "0.1.0",
        "enabled": False,
        "enabled_reason": "T3 test seed",
        "enabled_changed_ts": "2026-05-08T00:00:00Z",
        "last_bar_processed_ts": None,
        "sequence": 0,
        "auto_pause": {"active": False, "reason": None, "tripped_ts": None,
                       "tripped_metric": None, "tripped_value": None},
        "counters": {"trades_opened_total": 0, "trades_closed_total": 0,
                     "consecutive_losses": 0, "trades_today": 0,
                     "today_date": None, "fires_skipped_total": 0,
                     "fires_skipped_book_full": 0,
                     "fires_skipped_daily_cap": 0,
                     "fires_skipped_disabled": 0,
                     "fires_skipped_paused": 0},
        "watermarks": {"max_realized_R": 0.0, "max_drawdown_R": 0.0,
                       "equity_R_running": 0.0, "equity_peak_R": 0.0},
        "last_updated_ts": "2026-05-08T00:00:00Z",
    }))

    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    res = eng.incremental_pass(df=df)

    assert res["ok"] is True
    assert res["skipped"] is True
    assert res["reason"] == "engine disabled"
    assert res["n_new_bars"] == 0
    assert not (out / "paper_sim_orders.jsonl").exists()
    assert not (out / "paper_sim_equity.jsonl").exists()

    # State cursor MUST NOT advance.
    state_after = json.loads(state_path.read_text())
    assert state_after.get("last_bar_processed_ts") is None
    assert state_after["counters"]["trades_opened_total"] == 0


# ── T4: error isolation ─────────────────────────────────────────────────────

def _three_fire_frame() -> pd.DataFrame:
    """Frame with 3 R2 fires, each with a clear target hit on the next bar.

    Each fire-bar has Close=100, ATR=2 (long stop=98, target=104).
    Each next bar has High=105 → target hit immediately (R=+2).
    Then a flat bar before the next fire so positions never overlap.
    """
    rows = [
        # cluster 1
        {"ts": "2026-05-08T00:00:00Z", "Close": 100.0, "High": 100.5, "Low": 99.5,
         "atr": 2.0, "r2_seller_up": True, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T00:15:00Z", "Close": 104.5, "High": 105.0, "Low": 100.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T00:30:00Z", "Close": 102.0, "High": 103.0, "Low": 101.0,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        # cluster 2
        {"ts": "2026-05-08T00:45:00Z", "Close": 100.0, "High": 100.5, "Low": 99.5,
         "atr": 2.0, "r2_seller_up": True, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:00:00Z", "Close": 104.5, "High": 105.0, "Low": 100.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:15:00Z", "Close": 102.0, "High": 103.0, "Low": 101.0,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        # cluster 3
        {"ts": "2026-05-08T01:30:00Z", "Close": 100.0, "High": 100.5, "Low": 99.5,
         "atr": 2.0, "r2_seller_up": True, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T01:45:00Z", "Close": 104.5, "High": 105.0, "Low": 100.5,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
        {"ts": "2026-05-08T02:00:00Z", "Close": 102.0, "High": 103.0, "Low": 101.0,
         "atr": 2.0, "r2_seller_up": False, "bar_proxy_mode": 0},
    ]
    return _make_frame(rows)


# ── T5a: cap=1 hard truncation ──────────────────────────────────────────────

def test_t5a_run_cap_one_truncation(tmp_path):
    df = _three_fire_frame()
    out = tmp_path / "cap1"
    out.mkdir()

    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng.state.enabled = True
    eng.state.enabled_reason = "T5a"
    eng.state.run_cap = 1
    eng._persist_state()

    res = eng.incremental_pass(df=df)
    assert res["ok"] is True
    assert res["n_new_fires"] == 3
    assert res["n_opened"] == 1
    assert res["run_cap"] == 1
    # The two later fires were skipped by run_cap.
    assert eng.state.fires_skipped_run_cap == 2

    orders = _read_lines(out / "paper_sim_orders.jsonl")
    opens = [o for o in orders if o["type"] == "open"]
    assert len(opens) == 1


# ── T5b: cap=null = current behavior ────────────────────────────────────────

def test_t5b_run_cap_null_unchanged(tmp_path):
    df = _three_fire_frame()
    out = tmp_path / "cap_null"
    out.mkdir()

    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng.state.enabled = True
    eng.state.enabled_reason = "T5b"
    eng.state.run_cap = None
    eng._persist_state()

    res = eng.incremental_pass(df=df)
    assert res["n_new_fires"] == 3
    assert res["n_opened"] == 3
    assert res["run_cap"] is None
    assert eng.state.fires_skipped_run_cap == 0


# ── T5c: cap > fire count = no skip ─────────────────────────────────────────

def test_t5c_run_cap_high_no_skip(tmp_path):
    df = _three_fire_frame()
    out = tmp_path / "cap_high"
    out.mkdir()

    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng.state.enabled = True
    eng.state.enabled_reason = "T5c"
    eng.state.run_cap = 10
    eng._persist_state()

    res = eng.incremental_pass(df=df)
    assert res["n_opened"] == 3
    assert eng.state.fires_skipped_run_cap == 0


# ── T5d: cap is per-pass, not per-trial ─────────────────────────────────────

def test_t5d_cap_per_pass(tmp_path):
    df = _three_fire_frame()
    out = tmp_path / "cap_per_pass"
    out.mkdir()

    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng.state.enabled = True
    eng.state.enabled_reason = "T5d"
    eng.state.run_cap = 1
    eng._persist_state()

    # First pass: feed only first cluster (1 fire).
    eng.incremental_pass(df=df.iloc[:3])
    # Second pass on same engine: feed remaining (2 fires); cap=1 still
    # active but baseline resets per pass → should open exactly 1 more.
    res2 = eng.incremental_pass(df=df)
    assert res2["n_opened"] == 1
    # Total opened across both passes = 2; one fire remains skipped.
    assert eng.state.trades_opened_total == 2
    assert eng.state.fires_skipped_run_cap == 1


# ── T5e: cap interacts with daily cap (tightest wins) ───────────────────────

def test_t5e_cap_interacts_daily(tmp_path):
    df = _three_fire_frame()
    out = tmp_path / "cap_daily"
    out.mkdir()

    eng = ps.PaperSimEngine(out_dir=out, tf="15m",
                             max_trades_per_day=2)
    eng.state.enabled = True
    eng.state.enabled_reason = "T5e"
    eng.state.run_cap = 1
    eng._persist_state()

    res = eng.incremental_pass(df=df)
    # Tighter cap (run_cap=1) wins this pass.
    assert res["n_opened"] == 1
    assert eng.state.fires_skipped_run_cap >= 1


def test_t4_error_isolation(tmp_path, monkeypatch):
    """
    Simulate the monitor_loop pattern: the upstream caller wraps
    incremental_pass in try/except. An engine exception must:
      - not corrupt prior state.json
      - be reported as ok=False to the caller (via the wrapper)
    """
    df = _scenario_frame()
    out = tmp_path / "err"
    out.mkdir()

    # First, seed state so we have a baseline.
    eng = ps.PaperSimEngine(out_dir=out, tf="15m")
    eng.state.enabled = True
    eng.state.enabled_reason = "T4 baseline"
    eng._persist_state()
    state_before = json.loads((out / "paper_sim_state.json").read_text())

    # Now patch on_fire to raise mid-pass.
    def _boom(*a, **kw):
        raise RuntimeError("simulated engine bug")
    monkeypatch.setattr(eng, "on_fire", _boom)

    # Wrapper-style invocation (mirrors monitor_loop._step try/except).
    rec = {"ts": "2026-05-08T00:00:00Z"}
    try:
        rec["paper_sim"] = eng.incremental_pass(df=df)
        rec["paper_sim"]["ok"] = True
    except Exception as e:
        rec["paper_sim"] = {"ok": False,
                            "error": "{t}: {m}".format(t=type(e).__name__, m=e)}

    assert rec["paper_sim"]["ok"] is False
    assert "simulated engine bug" in rec["paper_sim"]["error"]

    # State on disk must be unchanged from before the failed pass.
    state_after = json.loads((out / "paper_sim_state.json").read_text())
    assert state_after["counters"] == state_before["counters"]
    assert state_after["watermarks"] == state_before["watermarks"]
    # No partial orders.jsonl appended.
    assert not (out / "paper_sim_orders.jsonl").exists() or \
           _read_lines(out / "paper_sim_orders.jsonl") == []
