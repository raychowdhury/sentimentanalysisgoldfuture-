"""
P2 — R2 paper-simulation dry replay engine.

Forward paper-simulation engine for r2_seller_up. P2 scope = dry replay
only: iterate over the existing joined real-flow frame in chronological
order and produce paper_sim_*.json/.jsonl outputs per the schema frozen
in docs/paper_sim_mvp.md.

Hard invariants (mirrored from outcome tracker + paper_sim_mvp.md):
  * NEVER imports predictor / alert_engine / ingest / ml_engine.
  * NEVER edits config.py or any threshold.
  * NEVER places trades.
  * NEVER touches a broker.
  * NEVER mutates live state — dry replay writes to a separate
    `paper_sim_dryrun/` subdirectory to keep future forward-sim artifacts
    untouched.
  * orders.jsonl + equity.jsonl are append-only.
  * book.json + state.json are rewritten atomically (tmp + os.replace).

Mechanics (per docs/paper_sim_mvp_p1_resolutions.md):
  * Entry fill = close of fire bar.
  * Time stop = OF_FORWARD_BARS[tf] (read at runtime; 12 for 15m).
  * First-touch stop / target / time-stop close.
  * Stop-first tie-break when both touched on the same bar.
  * Per-trade R = (exit_px - entry_px) * direction / atr_at_entry.

Reconciliation: paper-sim realized_R will diverge from outcome
tracker fwd_r_signed by design (close-at-horizon vs first-touch).
This is documented behaviour, not a bug.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import realflow_compare as rfc
from order_flow_engine.src.realflow_compare import NY_TZ


SCHEMA_VERSION = 1
ENGINE_VERSION = "0.1.1"

DEFAULT_RULE = "r2_seller_up"
DEFAULT_TARGET_R = 2.0
DEFAULT_STOP_R = 1.0
DEFAULT_MAX_TRADES_PER_DAY = 4
DEFAULT_MAX_CONSEC_LOSSES = 5
DEFAULT_MAX_DRAWDOWN_R = 15.0


# ── path helpers ────────────────────────────────────────────────────────────

def _default_dryrun_dir() -> Path:
    return Path(of_cfg.OF_OUTPUT_DIR) / "paper_sim_dryrun"


def _forward_out_dir() -> Path:
    """Forward paper-sim artifact directory (top-level per spec §2)."""
    return Path(of_cfg.OF_OUTPUT_DIR)


def _state_path(out_dir: Path) -> Path:
    return out_dir / "paper_sim_state.json"


def _book_path(out_dir: Path) -> Path:
    return out_dir / "paper_sim_book.json"


def _orders_path(out_dir: Path) -> Path:
    return out_dir / "paper_sim_orders.jsonl"


def _equity_path(out_dir: Path) -> Path:
    return out_dir / "paper_sim_equity.jsonl"


# ── small utilities ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def _session_bucket(ts: pd.Timestamp) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    minutes = ts.hour * 60 + ts.minute
    if (13 * 60 + 30) <= minutes < (14 * 60):
        return "RTH_open"
    if (14 * 60) <= minutes < (19 * 60 + 30):
        return "RTH_mid"
    if (19 * 60 + 30) <= minutes < (20 * 60):
        return "RTH_close"
    return "ETH"


def _direction_for_rule(rule: str) -> int:
    return -1 if rule == "r1_buyer_down" else +1


# ── engine state structs ────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    trade_id: str
    rule: str
    direction: int
    entry_ts: str
    entry_bar_ts: str
    entry_px: float
    atr_at_entry: float
    stop_px: float
    target_px: float
    stop_R_distance: float
    target_R_distance: float
    time_stop_bars: int
    bars_held: int = 0
    current_px: float = 0.0
    unrealized_R: float = 0.0
    mfe_R_seen: float = 0.0
    mae_R_seen: float = 0.0
    session_at_entry: str = "ETH"
    sequence_open: int = 0


@dataclass
class EngineState:
    # Default DISABLED per spec invariant: engine must be mechanically inert
    # until an operator explicitly flips the flag via scripts/paper_sim_enable.py.
    # P2 replay() and tests explicitly set enabled=True after construction.
    enabled: bool = False
    enabled_reason: str = "default disabled — operator must enable via CLI"
    enabled_changed_ts: str = field(default_factory=_now_iso)
    last_bar_processed_ts: str | None = None
    sequence: int = 0
    auto_pause_active: bool = False
    auto_pause_reason: str | None = None
    auto_pause_tripped_ts: str | None = None
    auto_pause_tripped_metric: str | None = None
    auto_pause_tripped_value: float | None = None
    trades_opened_total: int = 0
    trades_closed_total: int = 0
    consecutive_losses: int = 0
    trades_today: int = 0
    today_date: str | None = None
    fires_skipped_total: int = 0
    fires_skipped_book_full: int = 0
    fires_skipped_daily_cap: int = 0
    fires_skipped_disabled: int = 0
    fires_skipped_paused: int = 0
    fires_skipped_run_cap: int = 0
    # Optional per-pass open cap. None = no cap. Set via
    # scripts/paper_sim_set_run_cap.py. Persists across iterations until
    # cleared. Tighter than max_trades_per_day; both apply.
    run_cap: int | None = None
    max_realized_R: float = 0.0
    max_drawdown_R: float = 0.0
    equity_R_running: float = 0.0
    equity_peak_R: float = 0.0
    last_updated_ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "enabled": self.enabled,
            "enabled_reason": self.enabled_reason,
            "enabled_changed_ts": self.enabled_changed_ts,
            "last_bar_processed_ts": self.last_bar_processed_ts,
            "sequence": self.sequence,
            "auto_pause": {
                "active": self.auto_pause_active,
                "reason": self.auto_pause_reason,
                "tripped_ts": self.auto_pause_tripped_ts,
                "tripped_metric": self.auto_pause_tripped_metric,
                "tripped_value": self.auto_pause_tripped_value,
            },
            "counters": {
                "trades_opened_total": self.trades_opened_total,
                "trades_closed_total": self.trades_closed_total,
                "consecutive_losses": self.consecutive_losses,
                "trades_today": self.trades_today,
                "today_date": self.today_date,
                "fires_skipped_total": self.fires_skipped_total,
                "fires_skipped_book_full": self.fires_skipped_book_full,
                "fires_skipped_daily_cap": self.fires_skipped_daily_cap,
                "fires_skipped_disabled": self.fires_skipped_disabled,
                "fires_skipped_paused": self.fires_skipped_paused,
                "fires_skipped_run_cap": self.fires_skipped_run_cap,
            },
            "run_cap": self.run_cap,
            "watermarks": {
                "max_realized_R": self.max_realized_R,
                "max_drawdown_R": self.max_drawdown_R,
                "equity_R_running": self.equity_R_running,
                "equity_peak_R": self.equity_peak_R,
            },
            "last_updated_ts": self.last_updated_ts,
        }


# ── engine ──────────────────────────────────────────────────────────────────

class PaperSimEngine:
    """Dry-replay paper simulation engine for a single rule (r2_seller_up MVP)."""

    def __init__(
        self,
        out_dir: Path,
        tf: str = "15m",
        rule: str = DEFAULT_RULE,
        target_R: float = DEFAULT_TARGET_R,
        stop_R: float = DEFAULT_STOP_R,
        max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY,
        max_consec_losses: int = DEFAULT_MAX_CONSEC_LOSSES,
        max_drawdown_R: float = DEFAULT_MAX_DRAWDOWN_R,
    ):
        self.out_dir = Path(out_dir)
        self.tf = tf
        self.rule = rule
        self.direction = _direction_for_rule(rule)
        self.target_R = target_R
        self.stop_R = stop_R
        self.max_trades_per_day = max_trades_per_day
        self.max_consec_losses = max_consec_losses
        self.max_drawdown_R = max_drawdown_R
        # Read horizon from config at runtime — never hardcode 12.
        self.time_stop_bars = int(of_cfg.OF_FORWARD_BARS.get(tf, 12))

        self.state = EngineState()
        self.position: OpenPosition | None = None
        # Defense-in-depth dedup on top of cursor-based gating.
        self._seen_signal_ids: set[str] = set()
        # Per-pass open counter baseline. Set by incremental_pass; None
        # means run_cap is not enforced (replay() path or fresh construction).
        self._pass_open_baseline: int | None = None

    # ── deterministic IDs ───────────────────────────────────────────────

    def _signal_id_for(self, rule: str, ts: pd.Timestamp) -> str:
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts_iso = ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        return "{r}@{t}".format(r=rule, t=ts_iso)

    # ── disk hydration ──────────────────────────────────────────────────

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            with path.open() as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows

    def hydrate_from_disk(self) -> dict:
        """
        Restart-recovery: reload state, book, and seen_signal_ids from
        on-disk artifacts. Returns a small report dict for logging.
        """
        report = {"state_loaded": False, "book_loaded": False,
                  "seen_signal_ids": 0, "position_resumed": False}

        state_doc = self._read_json(_state_path(self.out_dir))
        if state_doc is not None:
            s = self.state
            s.enabled = bool(state_doc.get("enabled", False))
            s.enabled_reason = state_doc.get(
                "enabled_reason", s.enabled_reason)
            s.enabled_changed_ts = state_doc.get(
                "enabled_changed_ts", s.enabled_changed_ts)
            s.last_bar_processed_ts = state_doc.get("last_bar_processed_ts")
            s.sequence = int(state_doc.get("sequence", 0))
            ap = state_doc.get("auto_pause", {}) or {}
            s.auto_pause_active = bool(ap.get("active", False))
            s.auto_pause_reason = ap.get("reason")
            s.auto_pause_tripped_ts = ap.get("tripped_ts")
            s.auto_pause_tripped_metric = ap.get("tripped_metric")
            s.auto_pause_tripped_value = ap.get("tripped_value")
            c = state_doc.get("counters", {}) or {}
            s.trades_opened_total = int(c.get("trades_opened_total", 0))
            s.trades_closed_total = int(c.get("trades_closed_total", 0))
            s.consecutive_losses = int(c.get("consecutive_losses", 0))
            s.trades_today = int(c.get("trades_today", 0))
            s.today_date = c.get("today_date")
            s.fires_skipped_total = int(c.get("fires_skipped_total", 0))
            s.fires_skipped_book_full = int(c.get("fires_skipped_book_full", 0))
            s.fires_skipped_daily_cap = int(c.get("fires_skipped_daily_cap", 0))
            s.fires_skipped_disabled = int(c.get("fires_skipped_disabled", 0))
            s.fires_skipped_paused = int(c.get("fires_skipped_paused", 0))
            s.fires_skipped_run_cap = int(c.get("fires_skipped_run_cap", 0))
            # run_cap is top-level optional; absent = null = no cap.
            rc = state_doc.get("run_cap")
            s.run_cap = int(rc) if rc is not None else None
            w = state_doc.get("watermarks", {}) or {}
            s.max_realized_R = float(w.get("max_realized_R", 0.0))
            s.max_drawdown_R = float(w.get("max_drawdown_R", 0.0))
            s.equity_R_running = float(w.get("equity_R_running", 0.0))
            s.equity_peak_R = float(w.get("equity_peak_R", 0.0))
            report["state_loaded"] = True

        book_doc = self._read_json(_book_path(self.out_dir))
        if book_doc is not None:
            positions = book_doc.get("positions", []) or []
            if positions:
                p = positions[0]
                self.position = OpenPosition(
                    trade_id=p["trade_id"],
                    rule=p["rule"],
                    direction=int(p["direction"]),
                    entry_ts=p["entry_ts"],
                    entry_bar_ts=p["entry_bar_ts"],
                    entry_px=float(p["entry_px"]),
                    atr_at_entry=float(p["atr_at_entry"]),
                    stop_px=float(p["stop_px"]),
                    target_px=float(p["target_px"]),
                    stop_R_distance=float(p["stop_R_distance"]),
                    target_R_distance=float(p["target_R_distance"]),
                    time_stop_bars=int(p["time_stop_bars"]),
                    bars_held=int(p.get("bars_held", 0)),
                    current_px=float(p.get("current_px", p["entry_px"])),
                    unrealized_R=float(p.get("unrealized_R", 0.0)),
                    mfe_R_seen=float(p.get("mfe_R_seen", 0.0)),
                    mae_R_seen=float(p.get("mae_R_seen", 0.0)),
                    session_at_entry=p.get("session_at_entry", "ETH"),
                    sequence_open=int(p.get("sequence_open", 0)),
                )
                report["position_resumed"] = True
            report["book_loaded"] = True

        for r in self._read_jsonl(_orders_path(self.out_dir)):
            if r.get("type") != "open":
                continue
            rule = r.get("rule")
            ts_str = r.get("fire_bar_ts")
            if not rule or not ts_str:
                continue
            ts = pd.Timestamp(ts_str)
            self._seen_signal_ids.add(self._signal_id_for(rule, ts))
        report["seen_signal_ids"] = len(self._seen_signal_ids)
        return report

    # ── path-bound helpers ──────────────────────────────────────────────

    def _persist_state(self) -> None:
        self.state.last_updated_ts = _now_iso()
        _atomic_write_json(_state_path(self.out_dir), self.state.to_dict())

    def _persist_book(self, as_of_ts: str) -> None:
        positions = []
        if self.position is not None:
            p = self.position
            positions.append({
                "trade_id": p.trade_id,
                "rule": p.rule,
                "direction": p.direction,
                "entry_ts": p.entry_ts,
                "entry_bar_ts": p.entry_bar_ts,
                "entry_px": round(p.entry_px, 4),
                "atr_at_entry": round(p.atr_at_entry, 4),
                "stop_px": round(p.stop_px, 4),
                "target_px": round(p.target_px, 4),
                "stop_R_distance": p.stop_R_distance,
                "target_R_distance": p.target_R_distance,
                "time_stop_bars": p.time_stop_bars,
                "bars_held": p.bars_held,
                "current_px": round(p.current_px, 4),
                "unrealized_R": round(p.unrealized_R, 4),
                "mfe_R_seen": round(p.mfe_R_seen, 4),
                "mae_R_seen": round(p.mae_R_seen, 4),
                "session_at_entry": p.session_at_entry,
                "sequence_open": p.sequence_open,
            })
        payload = {
            "schema_version": SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "as_of_ts": as_of_ts,
            "positions": positions,
        }
        _atomic_write_json(_book_path(self.out_dir), payload)

    # ── daily counter rollover ──────────────────────────────────────────

    def _maybe_rollover_day(self, bar_ts_utc: pd.Timestamp) -> None:
        cur_date = bar_ts_utc.strftime("%Y-%m-%d")
        if self.state.today_date != cur_date:
            self.state.today_date = cur_date
            self.state.trades_today = 0

    # ── lifecycle: on_fire ──────────────────────────────────────────────

    def on_fire(
        self,
        bar_ts_utc: pd.Timestamp,
        atr: float,
        close_px: float,
    ) -> str:
        """
        Process a rule fire. Returns one of:
          "opened" | "skip_disabled" | "skip_paused" |
          "skip_book_full" | "skip_daily_cap" | "skip_invalid_atr"
        """
        self._maybe_rollover_day(bar_ts_utc)

        if not self.state.enabled:
            self.state.fires_skipped_total += 1
            self.state.fires_skipped_disabled += 1
            return "skip_disabled"

        if self.state.auto_pause_active:
            self.state.fires_skipped_total += 1
            self.state.fires_skipped_paused += 1
            return "skip_paused"

        if self.position is not None:
            self.state.fires_skipped_total += 1
            self.state.fires_skipped_book_full += 1
            return "skip_book_full"

        if self.state.trades_today >= self.max_trades_per_day:
            self.state.fires_skipped_total += 1
            self.state.fires_skipped_daily_cap += 1
            return "skip_daily_cap"

        # Per-pass run cap. Only enforced when incremental_pass set a
        # baseline AND state.run_cap is non-null. Daily cap already
        # checked above; whichever is tighter wins by sequential check.
        if (self._pass_open_baseline is not None
                and self.state.run_cap is not None):
            opened_this_pass = (self.state.trades_opened_total
                                - self._pass_open_baseline)
            if opened_this_pass >= int(self.state.run_cap):
                self.state.fires_skipped_total += 1
                self.state.fires_skipped_run_cap += 1
                return "skip_run_cap"

        if atr is None or not (atr > 0):
            self.state.fires_skipped_total += 1
            return "skip_invalid_atr"

        # Defense-in-depth dedup. Cursor advancement should already prevent
        # re-processing, but explicit signal_id check guards against
        # operator-driven re-runs and in-memory state staleness.
        signal_id = self._signal_id_for(self.rule, bar_ts_utc)
        if signal_id in self._seen_signal_ids:
            self.state.fires_skipped_total += 1
            return "skip_duplicate"

        # Open new trade
        bar_iso = bar_ts_utc.tz_convert("UTC").isoformat()
        self.state.sequence += 1
        self.state.trades_opened_total += 1
        self.state.trades_today += 1

        trade_id = "{rule}_{ts}_{n:03d}".format(
            rule=self.rule, ts=bar_iso, n=self.state.trades_opened_total,
        )
        stop_px = close_px - self.direction * self.stop_R * atr
        target_px = close_px + self.direction * self.target_R * atr

        self.position = OpenPosition(
            trade_id=trade_id,
            rule=self.rule,
            direction=self.direction,
            entry_ts=bar_iso,
            entry_bar_ts=bar_iso,
            entry_px=float(close_px),
            atr_at_entry=float(atr),
            stop_px=float(stop_px),
            target_px=float(target_px),
            stop_R_distance=self.stop_R,
            target_R_distance=self.target_R,
            time_stop_bars=self.time_stop_bars,
            bars_held=0,
            current_px=float(close_px),
            unrealized_R=0.0,
            mfe_R_seen=0.0,
            mae_R_seen=0.0,
            session_at_entry=_session_bucket(bar_ts_utc),
            sequence_open=self.state.sequence,
        )

        _append_jsonl(_orders_path(self.out_dir), {
            "schema_version": SCHEMA_VERSION,
            "type": "open",
            "sequence": self.state.sequence,
            "trade_id": trade_id,
            "rule": self.rule,
            "direction": self.direction,
            "fire_bar_ts": bar_iso,
            "entry_ts": bar_iso,
            "entry_bar_ts": bar_iso,
            "entry_px": round(float(close_px), 4),
            "atr_at_entry": round(float(atr), 4),
            "stop_px": round(float(stop_px), 4),
            "target_px": round(float(target_px), 4),
            "session_at_entry": self.position.session_at_entry,
            "fill_assumption": "close_of_fire_bar",
            "size_units": 1,
            "engine_version": ENGINE_VERSION,
            "written_ts": _now_iso(),
        })
        self._seen_signal_ids.add(signal_id)
        return "opened"

    # ── lifecycle: on_bar ───────────────────────────────────────────────

    def on_bar(
        self,
        bar_ts_utc: pd.Timestamp,
        high: float,
        low: float,
        close: float,
    ) -> str | None:
        """
        Process a forward bar. Returns the close reason if a trade closed
        on this bar, else None.
        """
        self._maybe_rollover_day(bar_ts_utc)

        bar_iso = bar_ts_utc.tz_convert("UTC").isoformat()
        self.state.last_bar_processed_ts = bar_iso

        close_reason: str | None = None

        if self.position is not None and not self.state.auto_pause_active:
            p = self.position
            p.bars_held += 1

            atr = p.atr_at_entry
            d = p.direction

            # Update mfe / mae from this bar's range, in R units.
            if d > 0:
                bar_mfe_R = (high - p.entry_px) / atr
                bar_mae_R = (low - p.entry_px) / atr
            else:
                bar_mfe_R = (p.entry_px - low) / atr
                bar_mae_R = (p.entry_px - high) / atr
            p.mfe_R_seen = max(p.mfe_R_seen, float(bar_mfe_R))
            p.mae_R_seen = min(p.mae_R_seen, float(bar_mae_R))

            # Touch checks
            if d > 0:
                stop_hit = low <= p.stop_px
                target_hit = high >= p.target_px
            else:
                stop_hit = high >= p.stop_px
                target_hit = low <= p.target_px
            time_stop = p.bars_held >= p.time_stop_bars

            tie_break = bool(stop_hit and target_hit)
            if stop_hit and target_hit:
                # Stop-first conservative tie-break.
                close_reason = "stop"
                exit_px = p.stop_px
            elif stop_hit:
                close_reason = "stop"
                exit_px = p.stop_px
            elif target_hit:
                close_reason = "target"
                exit_px = p.target_px
            elif time_stop:
                close_reason = "time_stop"
                exit_px = float(close)
            else:
                p.current_px = float(close)
                p.unrealized_R = (p.current_px - p.entry_px) * d / atr
                close_reason = None
                exit_px = None  # type: ignore

            if close_reason is not None:
                realized_R = (exit_px - p.entry_px) * d / atr
                self.state.sequence += 1
                self.state.trades_closed_total += 1
                if realized_R < 0:
                    self.state.consecutive_losses += 1
                else:
                    self.state.consecutive_losses = 0

                self.state.equity_R_running += float(realized_R)
                self.state.max_realized_R = max(self.state.max_realized_R,
                                                self.state.equity_R_running)
                self.state.equity_peak_R = max(self.state.equity_peak_R,
                                               self.state.equity_R_running)
                drawdown = self.state.equity_peak_R - self.state.equity_R_running
                self.state.max_drawdown_R = max(self.state.max_drawdown_R,
                                                float(drawdown))

                _append_jsonl(_orders_path(self.out_dir), {
                    "schema_version": SCHEMA_VERSION,
                    "type": "close",
                    "sequence": self.state.sequence,
                    "trade_id": p.trade_id,
                    "rule": p.rule,
                    "direction": p.direction,
                    "exit_ts": bar_iso,
                    "exit_bar_ts": bar_iso,
                    "exit_px": round(float(exit_px), 4),
                    "exit_reason": close_reason,
                    "bars_held": p.bars_held,
                    "realized_R": round(float(realized_R), 4),
                    "mfe_R_seen": round(float(p.mfe_R_seen), 4),
                    "mae_R_seen": round(float(p.mae_R_seen), 4),
                    "tie_break_applied": tie_break,
                    "engine_version": ENGINE_VERSION,
                    "written_ts": _now_iso(),
                })

                self.position = None
                self._check_risk_caps(bar_iso)

        # Equity row for every processed bar (regardless of trade activity).
        unrealized = self.position.unrealized_R if self.position else 0.0
        equity_total = self.state.equity_R_running + unrealized
        _append_jsonl(_equity_path(self.out_dir), {
            "schema_version": SCHEMA_VERSION,
            "bar_ts": bar_iso,
            "realized_R_cumulative": round(float(self.state.equity_R_running), 4),
            "unrealized_R": round(float(unrealized), 4),
            "equity_R_total": round(float(equity_total), 4),
            "open_positions": 1 if self.position else 0,
            "max_drawdown_R_running": round(float(self.state.max_drawdown_R), 4),
            "trade_count_to_date": int(self.state.trades_closed_total),
            "written_ts": _now_iso(),
        })
        return close_reason

    # ── auto-pause checks ───────────────────────────────────────────────

    def _check_risk_caps(self, bar_iso: str) -> None:
        if self.state.consecutive_losses >= self.max_consec_losses:
            self._trip_pause(bar_iso, "max_consecutive_losses",
                             float(self.state.consecutive_losses))
            return
        if self.state.max_drawdown_R >= self.max_drawdown_R:
            self._trip_pause(bar_iso, "max_paper_drawdown_R",
                             float(self.state.max_drawdown_R))
            return

    def _trip_pause(self, bar_iso: str, metric: str, value: float) -> None:
        self.state.auto_pause_active = True
        self.state.auto_pause_reason = (
            "auto-pause: {m} = {v} tripped at {ts}".format(
                m=metric, v=value, ts=bar_iso,
            )
        )
        self.state.auto_pause_tripped_ts = bar_iso
        self.state.auto_pause_tripped_metric = metric
        self.state.auto_pause_tripped_value = float(value)
        self.state.enabled = False
        self.state.enabled_reason = (
            "disabled by auto-pause ({m})".format(m=metric)
        )
        self.state.enabled_changed_ts = _now_iso()

    # ── replay driver ───────────────────────────────────────────────────

    def replay(self, df: pd.DataFrame) -> dict:
        """
        Drive the engine over a chronologically-sorted joined frame `df`.
        Required columns: `r2_seller_up`, `Close`, `High`, `Low`, `atr`.
        Optional column: `bar_proxy_mode` (filter to real-flow only).
        """
        if df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        else:
            df = df.tz_convert("UTC") if hasattr(df, "tz_convert") else df.copy()

        df = df.sort_index()

        if self.rule not in df.columns:
            return {
                "ok": False,
                "reason": "rule column not present in frame: {r}".format(r=self.rule),
            }

        fires_mask = df[self.rule].fillna(False).astype(bool)
        if "bar_proxy_mode" in df.columns:
            real_mask = df["bar_proxy_mode"].fillna(1).astype(int) == 0
            fires_mask = fires_mask & real_mask
        fire_ts_set = set(df.index[fires_mask])

        n_fires_seen = 0
        n_opened = 0
        n_skipped_book_full = 0
        n_skipped_other = 0

        for ts in df.index:
            row = df.loc[ts]
            if ts in fire_ts_set:
                n_fires_seen += 1
                atr_v = float(row["atr"]) if pd.notna(row.get("atr")) else None
                close_v = float(row["Close"])
                action = self.on_fire(ts, atr_v if atr_v else 0.0, close_v)
                if action == "opened":
                    n_opened += 1
                elif action == "skip_book_full":
                    n_skipped_book_full += 1
                else:
                    n_skipped_other += 1

            # Then process the bar for any open position.
            high_v = float(row["High"])
            low_v = float(row["Low"])
            close_v = float(row["Close"])
            self.on_bar(ts, high_v, low_v, close_v)

        # Final flush
        as_of = df.index[-1].tz_convert("UTC").isoformat() if len(df) else _now_iso()
        self._persist_book(as_of)
        self._persist_state()

        return {
            "ok": True,
            "n_bars": int(len(df)),
            "n_fires_seen": int(n_fires_seen),
            "n_opened": int(n_opened),
            "n_skipped_book_full": int(n_skipped_book_full),
            "n_skipped_other": int(n_skipped_other),
            "trades_closed_total": int(self.state.trades_closed_total),
            "equity_R_running": round(float(self.state.equity_R_running), 4),
            "max_drawdown_R": round(float(self.state.max_drawdown_R), 4),
            "auto_pause_active": bool(self.state.auto_pause_active),
            "out_dir": str(self.out_dir),
        }

    # ── incremental pass (P3 driver, NOT yet wired into monitor_loop) ───

    def incremental_pass(
        self,
        symbol: str = "ESM6",
        tf: str | None = None,
        df: pd.DataFrame | None = None,
    ) -> dict:
        """
        Process only bars after `state.last_bar_processed_ts`. Idempotent
        across restarts: hydrates from disk first, then advances cursor.

        If `df` is provided, it is treated as the joined real-flow frame
        (used by tests). Otherwise loads via `rfc._load_pair`.

        This method does NOT wire itself into any external loop. It is
        a callable that an upstream driver (e.g. monitor_loop._step) can
        invoke; without that wiring, calls are explicit only.
        """
        tf = tf or self.tf
        # Always reload from disk so a fresh process picks up prior state.
        # Safe even when called repeatedly within the same process: the
        # in-memory state is overwritten by the persisted version.
        hydrate_report = self.hydrate_from_disk()

        if not self.state.enabled:
            return {
                "ok": True, "skipped": True, "reason": "engine disabled",
                "hydrate": hydrate_report,
                "n_new_bars": 0, "n_new_fires": 0, "n_opened": 0,
            }
        if self.state.auto_pause_active:
            return {
                "ok": True, "skipped": True, "reason": "auto-paused",
                "hydrate": hydrate_report,
                "n_new_bars": 0, "n_new_fires": 0, "n_opened": 0,
            }

        # Anchor the per-pass run-cap baseline AFTER hydrate, BEFORE any
        # bar processing. on_fire reads this to decide whether run_cap
        # has been hit during *this* pass.
        self._pass_open_baseline = int(self.state.trades_opened_total)

        if df is None:
            raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
                rfc._load_pair(symbol, tf)
            df = real_feat.copy()

        if df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        else:
            df = df.copy()
            df.index = df.index.tz_convert("UTC")
        df = df.sort_index()

        if self.rule not in df.columns:
            return {
                "ok": False,
                "reason": "rule column not present: {r}".format(r=self.rule),
                "hydrate": hydrate_report,
            }

        # Cursor: only bars strictly after last_bar_processed_ts.
        cursor = self.state.last_bar_processed_ts
        if cursor:
            cursor_ts = pd.Timestamp(cursor)
            if cursor_ts.tzinfo is None:
                cursor_ts = cursor_ts.tz_localize("UTC")
            else:
                cursor_ts = cursor_ts.tz_convert("UTC")
            new_bars = df[df.index > cursor_ts]
        else:
            new_bars = df

        fires_mask = new_bars[self.rule].fillna(False).astype(bool)
        if "bar_proxy_mode" in new_bars.columns:
            real_mask = new_bars["bar_proxy_mode"].fillna(1).astype(int) == 0
            fires_mask = fires_mask & real_mask
        fire_ts_set = set(new_bars.index[fires_mask])

        n_new_bars = 0
        n_new_fires = 0
        n_opened = 0

        for ts in new_bars.index:
            n_new_bars += 1
            row = new_bars.loc[ts]
            if ts in fire_ts_set:
                n_new_fires += 1
                atr_v = float(row["atr"]) if pd.notna(row.get("atr")) else None
                close_v = float(row["Close"])
                action = self.on_fire(ts, atr_v if atr_v else 0.0, close_v)
                if action == "opened":
                    n_opened += 1

            high_v = float(row["High"])
            low_v = float(row["Low"])
            close_v = float(row["Close"])
            self.on_bar(ts, high_v, low_v, close_v)

        as_of = (new_bars.index[-1].tz_convert("UTC").isoformat()
                 if n_new_bars else (cursor or _now_iso()))
        self._persist_book(as_of)
        self._persist_state()

        # Drop the per-pass baseline so a future replay() call on the same
        # engine instance does not accidentally inherit run_cap enforcement.
        run_cap_in_effect = self.state.run_cap
        self._pass_open_baseline = None

        return {
            "ok": True,
            "skipped": False,
            "hydrate": hydrate_report,
            "n_new_bars": int(n_new_bars),
            "n_new_fires": int(n_new_fires),
            "n_opened": int(n_opened),
            "run_cap": run_cap_in_effect,
            "fires_skipped_run_cap_total": int(self.state.fires_skipped_run_cap),
            "trades_closed_total": int(self.state.trades_closed_total),
            "equity_R_running": round(float(self.state.equity_R_running), 4),
            "auto_pause_active": bool(self.state.auto_pause_active),
            "last_bar_processed_ts": self.state.last_bar_processed_ts,
            "out_dir": str(self.out_dir),
        }


# ── replay entry-point + CLI ────────────────────────────────────────────────

def replay_from_real_flow(
    symbol: str = "ESM6",
    tf: str = "15m",
    out_dir: Path | None = None,
    rule: str = DEFAULT_RULE,
    target_R: float = DEFAULT_TARGET_R,
    stop_R: float = DEFAULT_STOP_R,
) -> dict:
    """
    Load the joined real-flow frame for (symbol, tf) and replay through
    the engine. Writes outputs under `out_dir` (default = paper_sim_dryrun/).
    """
    out_dir = Path(out_dir) if out_dir is not None else _default_dryrun_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = \
        rfc._load_pair(symbol, tf)
    df = real_feat.copy()

    engine = PaperSimEngine(
        out_dir=out_dir, tf=tf, rule=rule,
        target_R=target_R, stop_R=stop_R,
    )
    result = engine.replay(df)
    result["symbol"] = symbol
    result["tf"] = tf
    result["rule"] = rule
    result["target_R"] = target_R
    result["stop_R"] = stop_R
    result["time_stop_bars"] = engine.time_stop_bars
    return result


def main():  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="P2 — R2 paper-simulation dry replay engine.",
    )
    ap.add_argument("--symbol", default="ESM6")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--rule", default=DEFAULT_RULE)
    ap.add_argument("--target-R", type=float, default=DEFAULT_TARGET_R)
    ap.add_argument("--stop-R", type=float, default=DEFAULT_STOP_R)
    ap.add_argument("--out-dir", default=str(_default_dryrun_dir()))
    args = ap.parse_args()

    result = replay_from_real_flow(
        symbol=args.symbol, tf=args.tf,
        out_dir=Path(args.out_dir),
        rule=args.rule, target_R=args.target_R, stop_R=args.stop_R,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
