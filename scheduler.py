"""
Auto-fetch scheduler for NewsSentimentScanner.

Runs the full pipeline (news → sentiment → signal → trade setup) on a
configurable interval using APScheduler's BackgroundScheduler.

Usage (from app.py):
    from scheduler import init_scheduler, get_status, trigger_run
    init_scheduler(app)          # start background scheduler
    trigger_run()                # manual one-shot run
    status = get_status()        # dict for the dashboard
"""

import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from utils import progress
from utils.logger import setup_logger

logger = setup_logger(__name__)

# ── Run state (shared across threads, protected by _lock) ─────────────────────
_lock = threading.Lock()

_state: dict = {
    "running":     False,
    "last_run_at": None,     # ISO string
    "last_status": "never",  # "ok" | "error" | "never"
    "last_error":  None,
    "next_run_at": None,     # ISO string, set by scheduler
    "run_count":   0,
    "last_signal": None,     # dict: {signal, total_score, confidence, timeframe, articles}
}

_scheduler: BackgroundScheduler | None = None


# ── Pipeline job ──────────────────────────────────────────────────────────────

def _run_pipeline(
    timeframe: str | None = None,
    mode: str | None = None,
    models: list[str] | None = None,
    limit: int | None = None,
    trade_setup: bool | None = None,
) -> None:
    """
    Execute the full pipeline in the background thread.
    Skips if a run is already in progress.
    """
    with _lock:
        if _state["running"]:
            logger.info("Scheduler: run already in progress — skipping")
            return
        _state["running"] = True

    # Clear any leftover progress counters from the previous run so the
    # dashboard doesn't briefly flash the old N/M before the new run sets
    # its totals.
    progress.reset(total=0, stage="starting")

    tf       = timeframe  or config.SCHEDULER_TIMEFRAME
    md       = mode       or config.SCHEDULER_MODE
    mdls     = models     or config.SCHEDULER_MODELS
    lim      = limit      or config.SCHEDULER_LIMIT
    do_trade = trade_setup if trade_setup is not None else config.SCHEDULER_TRADE_SETUP

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info(f"Scheduler: starting run {ts} (timeframe={tf})")

    try:
        from main import run_sentiment, run_signal

        _, summary = run_sentiment(
            mode=md,
            models=mdls,
            limit=lim,
            output_dir=config.OUTPUT_DIR,
            timestamp=ts,
            timeframe=tf,
        )
        signal_output = run_signal(
            sentiment_summary=summary,
            output_dir=config.OUTPUT_DIR,
            timestamp=ts,
            include_trade=do_trade,
            timeframe=tf,
        )

        with _lock:
            _state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
            _state["last_status"] = "ok"
            _state["last_error"]  = None
            _state["run_count"]  += 1
            _state["last_signal"] = {
                "signal":      signal_output.get("signal"),
                "total_score": signal_output.get("total_score"),
                "confidence":  signal_output.get("confidence"),
                "timeframe":   signal_output.get("timeframe"),
                "articles":    summary.get("total_analyzed"),
            }
        logger.info(f"Scheduler: run {ts} completed OK")

    except Exception as exc:
        with _lock:
            _state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
            _state["last_status"] = "error"
            _state["last_error"]  = str(exc)
        logger.error(f"Scheduler: run {ts} failed — {exc}")

    finally:
        with _lock:
            _state["running"] = False
        _refresh_next_run()


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def _run_ml_retrain() -> None:
    """Weekly ML retrain — runs in scheduler thread, isolates failures."""
    try:
        from ml_engine.retrain_all import run as retrain_run
        logger.info("[ml_retrain] starting weekly retrain")
        out = retrain_run()
        ok = sum(1 for r in out["results"] if "auc_long" in r)
        logger.info(f"[ml_retrain] done — {ok} models retrained")
    except Exception:
        logger.exception("[ml_retrain] failed")


def _add_ml_retrain_job() -> None:
    """Sunday 22:00 UTC — before Asia open Monday."""
    if _scheduler is None:
        return
    _scheduler.add_job(
        func=_run_ml_retrain,
        trigger=CronTrigger(day_of_week="sun", hour=22, minute=0),
        id="ml_retrain_weekly",
        name="ML retrain (weekly Sun 22:00 UTC)",
        replace_existing=True,
        misfire_grace_time=3600,
    )


def init_ml_retrain_only() -> None:
    """Start a scheduler with only the ML retrain job. Used when the main
    auto-fetch scheduler is disabled but we still want weekly retraining."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _add_ml_retrain_job()
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _add_ml_retrain_job()
    _scheduler.start()
    logger.info("Scheduler started (ml_retrain only) — Sun 22:00 UTC weekly")


def _refresh_next_run() -> None:
    """Update _state['next_run_at'] from the live APScheduler job."""
    if _scheduler is None:
        return
    jobs = _scheduler.get_jobs()
    if jobs:
        nxt = jobs[0].next_run_time
        with _lock:
            _state["next_run_at"] = nxt.isoformat(timespec="seconds") if nxt else None


def init_scheduler(app=None) -> None:
    """
    Start the BackgroundScheduler.
    Interval is taken from config based on SCHEDULER_TIMEFRAME.
    Safe to call multiple times — will not create duplicate schedulers.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.info("Scheduler already running — skipping init")
        return

    tf       = config.SCHEDULER_TIMEFRAME
    interval = (
        config.SCHEDULER_INTERVAL_DAY
        if tf == "day"
        else config.SCHEDULER_INTERVAL_SWING
    )

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=_run_pipeline,
        trigger=IntervalTrigger(minutes=interval),
        id="auto_fetch",
        name=f"Auto-fetch ({tf}, every {interval}m)",
        replace_existing=True,
        misfire_grace_time=60,
    )
    _add_ml_retrain_job()
    _scheduler.start()
    _refresh_next_run()
    logger.info(
        f"Scheduler started — timeframe={tf}, interval={interval}m, "
        f"next run at {_state['next_run_at']}"
    )


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


# ── Public API ────────────────────────────────────────────────────────────────

def trigger_run(timeframe: str | None = None) -> bool:
    """
    Fire an immediate one-shot run in a background thread.
    Returns False if a run is already in progress.
    """
    with _lock:
        if _state["running"]:
            return False

    t = threading.Thread(
        target=_run_pipeline,
        kwargs={"timeframe": timeframe},
        daemon=True,
    )
    t.start()
    return True


def get_status() -> dict:
    """Return a snapshot of the current scheduler state (safe copy)."""
    with _lock:
        snap = dict(_state)
    snap["progress"] = progress.snapshot()
    return snap


def is_enabled() -> bool:
    return config.SCHEDULER_ENABLED
